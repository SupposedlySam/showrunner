"""Spawning a Crawler: where it may write, what it needs, and what it still shares.

Four issues meet at this one moment in time, because they are all the same question —
*what does this agent actually get?*

**#4 — worktrees live INSIDE the repo.** The sibling-directory instinct is strong and it
is wrong here: each Crawler runs a per-agent harness whose write guard denies writes
outside `CLAUDE_PROJECT_DIR`, so a sibling worktree is a workspace the Crawler is
structurally forbidden to work in — it is denied its first edit. Inside the repo
(`.worktrees/<crawler>/`, gitignored) the writes are inside the boundary, and each
worktree gets its own copy of the harness's `bin/`, which has a second benefit worth
stating: **a Crawler editing the guard can only brick itself.** An orchestrator that can
take out its whole party with one bad edit is not fit to run unattended.

**#10 — secrets are injected, never committed.** A fresh worktree gets tracked files
only; every gitignored file the build actually needs is absent, and the Crawler fails in
a way that reads like a *code* problem. The obvious fixes are the wrong ones: committing
the files, or loosening `.gitignore`, put secrets into history across N branches that
later merge. So: an explicit configured list (never inferred from `.gitignore` — a wrong
guess either leaks a secret or produces the silent environment failure), symlinked where
possible so there is one copy and one lifetime, added to the worktree's exclude file so
`git add -A` cannot stage them, and **verified after injection** so a missing path fails
the spawn loudly instead of surfacing later as a mysterious runtime error.

**#11 — every Crawler gets its own scratch directory.** Two Crawlers in different
worktrees both reached for `commitmsg.txt` in the one shared scratch dir; the second
noticed only by luck. Had it not, the first would have committed the other's commit
message onto its own changes — a real commit with a plausible message describing work it
does not contain, and every gate green. Crawlers are the same model solving similar tasks
from similar prompts, so they converge on the same obvious filename far more often than
independent actors would: identical reasoning is a feature everywhere else in this design
and a hazard here.

**#13 — a worktree is not a boundary; say what is still shared.** It isolates tracked
files and nothing else. The harness's state directory, lock paths, caches, and anything
resolved from an absolute path or a hook's own script location stay shared. The audit is
cheap, and it is exactly the "state what this does not cover" discipline the rest of the
design already follows.
"""

import fnmatch
import json
import os
import shutil

from .util import die, eprint, git, now, rel, run, slug

MARKER = "showrunner-injected"


# ---------------------------------------------------------------- worktrees
def ensure_root(cfg):
    """Create the worktree root inside the repo and make sure git ignores it."""
    root = cfg.worktree_root
    os.makedirs(root, exist_ok=True)
    ignore = os.path.join(root, ".gitignore")
    if not os.path.exists(ignore):
        # showrunner adds the ignore entry itself — the reason for the placement is
        # non-obvious, so it should not also be the user's job to remember the chore.
        with open(ignore, "w") as fh:
            fh.write("# Created by showrunner: Crawler worktrees live inside the repo so each\n"
                     "# Crawler's own write guard (everything outside the repo is READ-ONLY)\n"
                     "# does not deny its first edit. See issue #4.\n*\n")
    return root


def crawler_name(leaf_id, actor=None):
    return slug("%s-%s" % (actor or "crawler", leaf_id), 60)


def worktree_path(cfg, name):
    return os.path.join(cfg.worktree_root, name)


def create(cfg, name, branch, base="HEAD"):
    """Create the worktree. Refuses rather than degrading if placement is unsafe."""
    cfg.require_valid()
    ensure_root(cfg)
    path = worktree_path(cfg, name)
    if os.path.exists(path):
        die("worktree path already exists: %s\n"
            "  It may hold uncommitted work — inspect it rather than reusing it blindly "
            "(`showrunner reap` reports abandoned trees)." % path, code=2)
    rc, _, err = git(["worktree", "add", "-b", branch, path, base], cwd=cfg.root)
    if rc != 0:
        die("git worktree add failed: %s" % err.strip(), code=2)
    return path


def remove(cfg, name, force=False):
    path = worktree_path(cfg, name)
    rc, _, err = git(["worktree", "remove"] + (["--force"] if force else []) + [path], cwd=cfg.root)
    return rc == 0, err.strip()


def dirty(path, tracked_only=False):
    """Uncommitted work in a worktree — the reason a dead Crawler's tree is not garbage.

    Untracked files count by default: a dead Crawler's only copy of real work is very
    often a file it never got as far as staging. `tracked_only` is for the narrower
    question "would `git reset --hard` destroy this?", where the answer for an untracked
    file is no.
    """
    args = ["status", "--porcelain"] + (["--untracked-files=no"] if tracked_only else [])
    rc, out, _ = git(args, cwd=path)
    if rc != 0:
        return None
    return [line for line in out.splitlines() if line.strip()]


# ------------------------------------------------------------------ scratch
def scratch_for(cfg, name):
    """A private scratch dir, created at spawn and named for the Crawler.

    Handing every Crawler the orchestrator's own temp dir is the natural thing to do and
    the thing that nearly committed one Crawler's message onto another's changes.
    Convention ("use a unique filename") is exactly the kind of rule that holds only some
    of the time.
    """
    path = os.path.join(cfg.scratch_root, name)
    os.makedirs(path, exist_ok=True)
    readme = os.path.join(path, "README.txt")
    if not os.path.exists(readme):
        with open(readme, "w") as fh:
            fh.write(
                "This scratch directory belongs to Crawler %r alone.\n\n"
                "Put commit messages, captured output, before/after artifacts and fixtures\n"
                "HERE, not in a shared temp dir. Sibling Crawlers are the same model solving\n"
                "similar tasks, so they pick the same obvious filename far more often than\n"
                "independent actors would; a clobbered scratch file produces no error, no\n"
                "conflict and no failed check — the second write simply succeeds.\n\n"
                "Nothing here is deleted automatically: it may hold the only copy of real work.\n"
                % name)
    return path


def shared_drop(cfg):
    """The one explicitly shared, append-only exchange surface."""
    path = os.path.join(cfg.scratch_root, "_shared")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- injection
def _exclude_file(worktree):
    rc, out, _ = git(["rev-parse", "--git-path", "info/exclude"], cwd=worktree)
    if rc != 0:
        return None
    p = out.strip()
    return p if os.path.isabs(p) else os.path.join(worktree, p)


def _add_exclude(worktree, paths):
    """Never let an injected path reach the index. Agents run `git add -A` constantly."""
    excl = _exclude_file(worktree)
    if not excl:
        return None
    os.makedirs(os.path.dirname(excl), exist_ok=True)
    existing = ""
    if os.path.exists(excl):
        with open(excl) as fh:
            existing = fh.read()
    lines = [l for l in ("/" + p.lstrip("/") for p in paths) if l not in existing.split("\n")]
    if lines:
        with open(excl, "a") as fh:
            fh.write("\n# %s — injected, must never be staged\n" % MARKER)
            fh.write("\n".join(lines) + "\n")
    return excl


def inject(cfg, worktree):
    """Materialize the configured paths into a fresh worktree.

    Returns (results, problems). A declared path that is missing is a **problem**, not a
    warning: letting the Crawler discover it as a mysterious runtime failure is how a
    broken environment becomes a confident, detailed, wrong finding about a service.
    """
    results, problems = [], []
    declared = cfg.get("inject") or []
    for entry in declared:
        if isinstance(entry, str):
            entry = {"path": entry}
        src_rel = entry.get("path")
        if not src_rel:
            problems.append("an inject entry has no 'path': %r" % entry)
            continue
        mode = entry.get("mode", "symlink")
        optional = bool(entry.get("optional"))
        src = os.path.join(cfg.root, src_rel)
        dst = os.path.join(worktree, src_rel)

        if not os.path.exists(src):
            msg = "declared inject path is missing from the source repo: %s" % src_rel
            (results if optional else problems).append(
                (msg + " (optional — skipped)") if optional else msg)
            continue

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(dst):
            results.append("%s already present in the worktree — left alone" % src_rel)
            continue
        try:
            if mode == "symlink":
                # One copy, one lifetime, nothing to clean up, and it cannot drift.
                os.symlink(src, dst)
            elif mode == "copy":
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            else:
                problems.append("inject %s: unknown mode %r (use 'symlink' or 'copy')"
                                % (src_rel, mode))
                continue
        except OSError as exc:
            problems.append("inject %s failed: %s" % (src_rel, exc))
            continue

        # Assert the observable state, not the exit code of the copy.
        if not os.path.exists(dst):
            problems.append("inject %s reported success but %s does not resolve" % (src_rel, dst))
            continue
        results.append("%s → %s (%s)" % (src_rel, rel(dst, cfg.root), mode))

    excl = _add_exclude(worktree, [
        (e if isinstance(e, str) else e.get("path")) for e in declared
        if (e if isinstance(e, str) else e.get("path"))])
    if declared and excl:
        results.append("excluded from the index via %s" % rel(excl, cfg.root))
    return results, problems


# ------------------------------------------------------- shared-state audit
DEFAULT_SHARED_STATE = [
    {
        "what": "the per-agent harness's state directory",
        "detect": [".game_loop", ".loop"],
        "why": "hooks are registered as \"$CLAUDE_PROJECT_DIR\"/<harness>/bin/... and a harness "
               "resolves its root from its own script location, so a commit made inside a "
               "worktree can be gated on the MAIN checkout's verification record — which "
               "describes a different tree altogether.",
        "consequence": "throughput (you serialize on a tree you do not own) AND correctness "
                       "(a green record from an unrelated run can wave your change through).",
        "instead": "poll read-only and wait, or ask the orchestrator to integrate. Do NOT reach "
                   "for --no-verify: a stuck agent under a mandate to finish is exactly the "
                   "situation where bypassing the gate starts looking reasonable.",
    },
    {
        "what": "the single-consumer resource locks",
        "detect": [],
        "why": "one absolute lock root is shared by every worktree on purpose — that is what "
               "makes 'one at a time' true.",
        "consequence": "you will block, by design, on a resource another Crawler holds.",
        "instead": "run the verb through `showrunner lock run <resource> -- <cmd>` so the lock "
                   "is held by the consumer itself.",
    },
]


def audit_shared(cfg):
    """Enumerate what a Crawler will actually share with its siblings."""
    findings = []
    for item in DEFAULT_SHARED_STATE + list(cfg.get("shared_state") or []):
        detect = item.get("detect")
        if detect and not any(os.path.exists(os.path.join(cfg.root, d)) for d in detect):
            continue
        findings.append(item)
    return findings


# -------------------------------------------------------------- the spawn
def spawn(cfg, leaf, actor="crawler", base="HEAD", branch=None):
    """Create everything a Crawler gets. Returns a record; raises on anything unsafe."""
    cfg.require_valid()
    name = crawler_name(leaf["id"], actor)
    branch = branch or "showrunner/%s" % slug(leaf["id"], 60)
    # Resolve the base to a SHA *before* creating the branch. Afterwards git cannot tell
    # a fully-merged branch from one that never received a commit — both have the base as
    # their merge-base — and that distinction decides whether a worktree is garbage or the
    # only copy of a dead Crawler's work.
    rc, base_sha, _ = git(["rev-parse", "%s^{commit}" % base], cwd=cfg.root)
    base_sha = base_sha.strip() if rc == 0 else None
    path = create(cfg, name, branch, base)
    scratch = scratch_for(cfg, name)
    injected, problems = inject(cfg, path)

    if problems:
        # Fail the spawn loudly rather than handing over a half-built environment.
        remove(cfg, name, force=True)
        die("spawn aborted — the Crawler's environment is incomplete:\n  - %s\n"
            "A Crawler that cannot reach a service will write the service up as broken, "
            "in the same confident tone as a real finding." % "\n  - ".join(problems), code=2)

    record = {
        "crawler": name,
        "leaf": leaf["id"],
        "title": leaf.get("title", ""),
        "actor": actor,
        "branch": branch,
        "worktree": path,
        "scratch": scratch,
        "shared_drop": shared_drop(cfg),
        "base": base,
        "base_sha": base_sha,
        "injected": injected,
        "shares": audit_shared(cfg),
        "created_ts": now(),
    }
    return record
