"""Provisioning the per-agent harness into a Crawler's worktree.

A harness that gets its commit gate right resolves it **per tree** — what a change owes, and
whether the evidence is newer than the change, are facts about a tree, so reading another
tree's record would answer a question about files the commit does not contain. game_loop
does exactly this and *denies* when the target tree carries no harness.

That correctness lands squarely on the orchestrator, because **`git worktree add` copies
tracked files only.** A harness directory the parent does not track never crosses, and the
Crawler is denied its first commit. That much is loud, and loud is survivable.

The failure that is *not* loud is the reason this module exists.

**A freshly-installed harness is not the same harness.** Running the harness's own installer
against a worktree seeds the user-owned files only *if absent* — so a worktree gets a blank
`verify.yaml`, a default `INVARIANTS.md`, and a default `config.json`. Every one of those is
a rule, and now the Crawler is operating under different ones than the orchestrator:

* a blank `verify.yaml` means **the commit gate owes nothing** — it stops gating, and reports
  success while doing it;
* a default `INVARIANTS.md` means the project's north star is silently the template's;
* a default `config.json` means different read roots, write roots and deploy verbs — the
  Crawler may be denied reads the orchestrator relies on, or *permitted* writes it should not
  have.

None of that produces an error. The party is simply playing by two rule sets, and the one
with fewer rules is the one running unattended in N parallel worktrees.

So provisioning here is not "make sure a harness is present." It is **make sure the harness
present is the same harness, and prove it byte-for-byte.** Presence is checkable and
sameness is checkable, so both are checked; a spawn that cannot establish either aborts
rather than handing over a Crawler with quietly weaker rails.

What is deliberately *not* copied is the harness's per-session runtime state — its state
file, its session directories, its edited-file set, its logs. Those belong to a session, not
to a tree, and copying them would hand a Crawler another session's claims and
authorizations. The exclusion list is not hardcoded: it is read from the harness's **own**
`.gitignore`, because the harness is the thing that knows which of its files are runtime.
"""

import filecmp
import fnmatch
import os
import shutil

from .util import die, rel, run

# Only used when a harness ships no .gitignore of its own to declare what is runtime state.
FALLBACK_RUNTIME = [
    "state.json", "sessions/", "edited.txt", "log.jsonl", "verified.json", "probe/",
    "*.pid", ".state.*.tmp", "notify.json", "limits.json",
]

# Files whose *content is a rule*. These must match the parent exactly, or the Crawler is
# playing by different rules than the orchestrator.
DEFAULT_RULE_FILES = ["config.json", "INVARIANTS.md", "verify.yaml"]

# Outside the harness dir, but without it none of the hooks are registered at all — which
# would mean showrunner promising a guarded Crawler and delivering an unguarded one.
DEFAULT_COMPANIONS = [".claude/settings.json"]

KNOWN_HARNESS_DIRS = (".game_loop", ".loop")


def spec(cfg):
    """The resolved harness config, with detection filling in what is not declared."""
    raw = dict(cfg.get("harness") or {})
    dirs = raw.get("dirs")
    if dirs is None:
        dirs = [d for d in KNOWN_HARNESS_DIRS if os.path.isdir(os.path.join(cfg.root, d))]
    companions = raw.get("companions")
    if companions is None:
        companions = [c for c in DEFAULT_COMPANIONS if os.path.exists(os.path.join(cfg.root, c))]
    return {
        "dirs": dirs,
        "companions": companions,
        "rule_files": raw.get("rule_files", DEFAULT_RULE_FILES),
        "provision": raw.get("provision", "auto"),   # auto | off
        "require": raw.get("require", True),
    }


def runtime_globs(harness_root):
    """What the harness itself declares is runtime state, from its own .gitignore."""
    gi = os.path.join(harness_root, ".gitignore")
    globs = []
    try:
        with open(gi) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("!"):
                    globs.append(line)
    except OSError:
        globs = list(FALLBACK_RUNTIME)
    return globs or list(FALLBACK_RUNTIME)


def _is_runtime(relpath, globs):
    parts = relpath.split(os.sep)
    for g in globs:
        bare = g.rstrip("/")
        if g.endswith("/"):
            if bare in parts[:-1] or parts[0] == bare:
                return True
        if fnmatch.fnmatch(relpath, g) or fnmatch.fnmatch(parts[-1], bare):
            return True
        if relpath.startswith(bare + os.sep):
            return True
    return False


def tracked_top_levels(cfg):
    rc, out, _ = run(["git", "ls-files"], cwd=cfg.root)
    if rc != 0:
        return set()
    return {line.split("/")[0] for line in out.splitlines() if line.strip()}


def _copy_tree_excluding_runtime(src, dst, globs):
    """Copy a harness dir, skipping whatever it declares as runtime state."""
    copied, skipped = [], []
    for dirpath, dirnames, filenames in os.walk(src):
        relroot = os.path.relpath(dirpath, src)
        relroot = "" if relroot == "." else relroot
        pruned = []
        for d in list(dirnames):
            rp = os.path.join(relroot, d) if relroot else d
            if _is_runtime(rp + "/", globs) or _is_runtime(rp, globs):
                dirnames.remove(d)
                pruned.append(rp)
        skipped += pruned
        for f in filenames:
            rp = os.path.join(relroot, f) if relroot else f
            if _is_runtime(rp, globs):
                skipped.append(rp)
                continue
            target = os.path.join(dst, rp)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(dirpath, f), target)   # preserves the exec bit
            copied.append(rp)
    return copied, skipped


def provision(cfg, worktree_path):
    """Make the worktree carry the SAME harness as the main checkout.

    Returns (actions, problems). Problems are fatal to a spawn when `require` is on.
    """
    sp = spec(cfg)
    actions, problems = [], []
    if sp["provision"] == "off" or not (sp["dirs"] or sp["companions"]):
        return actions, problems

    tracked = tracked_top_levels(cfg)

    for d in sp["dirs"]:
        src = os.path.join(cfg.root, d)
        dst = os.path.join(worktree_path, d)
        if not os.path.isdir(src):
            problems.append("configured harness dir %s does not exist in the main checkout" % d)
            continue
        globs = runtime_globs(src)

        if d in tracked and os.path.isdir(dst):
            actions.append("%s is tracked by git and crossed with the worktree" % d)
        else:
            if os.path.isdir(dst):
                actions.append("%s already present in the worktree — left alone" % d)
            else:
                copied, skipped = _copy_tree_excluding_runtime(src, dst, globs)
                actions.append("%s provisioned (%d file(s); %d runtime path(s) deliberately NOT "
                               "copied — session state belongs to a session, not a tree)"
                               % (d, len(copied), len(skipped)))
            # An untracked harness must not become stageable inside the worktree either:
            # the parent does not track it, so `git add -A` here would commit the harness.
            if d not in tracked:
                _exclude(worktree_path, [d])
                actions.append("%s added to the worktree's exclude file (the parent does not "
                               "track it, so `git add -A` must not stage it)" % d)

        # The rules must be the SAME rules. Presence is not sameness.
        for rf in sp["rule_files"]:
            a, b = os.path.join(src, rf), os.path.join(dst, rf)
            if not os.path.exists(a):
                continue
            if not os.path.exists(b):
                problems.append(
                    "%s/%s is missing from the worktree. It is a RULE file: without it the "
                    "Crawler runs under different rules than the orchestrator, and nothing "
                    "reports that." % (d, rf))
                continue
            if not filecmp.cmp(a, b, shallow=False):
                problems.append(
                    "%s/%s in the worktree DIFFERS from the main checkout's. This is the silent "
                    "failure: an installer seeds user-owned files only when absent, so a fresh "
                    "install yields a blank verify.yaml (a commit gate that owes nothing and "
                    "reports success), default INVARIANTS, and default read/write roots. Copy "
                    "the parent's file over it." % (d, rf))
            else:
                actions.append("%s/%s matches the main checkout byte-for-byte" % (d, rf))

    for c in sp["companions"]:
        src = os.path.join(cfg.root, c)
        dst = os.path.join(worktree_path, c)
        if not os.path.exists(src):
            continue
        if not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            actions.append("%s provisioned (without it the harness's hooks are not registered "
                           "in the Crawler's project at all)" % c)
        if not filecmp.cmp(src, dst, shallow=False):
            problems.append(
                "%s differs from the main checkout's. The hooks a Crawler runs under would not "
                "be the hooks the orchestrator runs under." % c)
        if c.split("/")[0] not in tracked:
            _exclude(worktree_path, [c])

    return actions, problems


def _exclude(worktree_path, paths):
    from .worktree import _add_exclude
    return _add_exclude(worktree_path, paths)


def report(cfg, worktree_path=None):
    """Doctor-facing summary: what a Crawler would get, and what is missing."""
    sp = spec(cfg)
    if sp["provision"] == "off":
        return ["harness provisioning is OFF — a Crawler's worktree will carry whatever git "
                "happens to bring across, and its commit gate may owe nothing."]
    if not sp["dirs"]:
        return []
    tracked = tracked_top_levels(cfg)
    lines = []
    for d in sp["dirs"]:
        how = "tracked by git (crosses automatically)" if d in tracked else \
              "untracked — showrunner copies it at spawn, minus its declared runtime state"
        rules = [rf for rf in sp["rule_files"] if os.path.exists(os.path.join(cfg.root, d, rf))]
        lines.append("harness %s: %s; rule files verified byte-for-byte at spawn: %s"
                     % (d, how, ", ".join(rules) or "none found"))
    for c in sp["companions"]:
        lines.append("companion %s: copied so the Crawler's hooks are actually registered" % c)
    return lines
