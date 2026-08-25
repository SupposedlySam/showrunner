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

import collections
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


def _indent(text, pad="    "):
    return "\n".join(pad + line for line in (text or "").splitlines()) or (pad + "(nothing)")


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
        # PRESENT-BUT-UNREACHABLE IS NOT BROKEN, and this path used to report it as broken.
        # `git worktree add` runs the repo's post-checkout hooks, and a hook that fails makes
        # git exit non-zero AFTER the tree exists. Reported as "git worktree add failed", so an
        # operator debugs git while the fault is a tool that is installed and not on this
        # session's PATH — four consecutive spawns in one real run, because nothing in the
        # message pointed at a hook, a PATH, or the dependency that was missing.
        #
        # THE TREE'S EXISTENCE IS THE DISCRIMINATOR and it is free. The two cases have opposite
        # remedies: repair git, versus fix an environment. This repo already refuses to collapse
        # that distinction elsewhere — `check` exits 3 for VOID so a run that could not reach
        # the world is not scored as one that measured a failure.
        if os.path.isdir(os.path.join(path, ".git")) or os.path.exists(path):
            die("the worktree WAS created at %s, and a post-checkout hook then failed.\n"
                "  git did its job; something it ran afterwards did not — most often a tool "
                "that is installed but not on THIS session's PATH.\n"
                "\n"
                "  the hook said:\n%s\n"
                "\n"
                "  PATH as this process sees it:\n    %s\n"
                "\n"
                "  Two sessions on one machine can disagree about PATH, so checking your own "
                "shell can exonerate a tool that is genuinely unreachable HERE — compare "
                "against the list above rather than against `command -v`.\n"
                "  The tree exists and is probably usable; inspect it before removing it, and "
                "`showrunner reap` reports it if it is abandoned."
                % (path, _indent(err.strip() or "(the hook printed nothing)"),
                   os.environ.get("PATH", "(unset)")), code=2)
        die("git worktree add failed and no tree was created at %s: %s" % (path, err.strip()),
            code=2)
    return path


def remove(cfg, name, force=False):
    path = worktree_path(cfg, name)
    rc, _, err = git(["worktree", "remove"] + (["--force"] if force else []) + [path], cwd=cfg.root)
    if rc == 0:
        # THE LEASE GOES WITH THE TREE. `Lease.release` had a write side and no caller at all —
        # the same asymmetry `Lock.holder`'s docstring names as THE defect in `acquire(extra=)`,
        # re-introduced one layer up. The practical effect was that a lease was never handed
        # back deliberately, only reclaimed from a corpse on somebody else's next `enter`, and
        # a lease naming a tree that no longer exists is stale state that outlives the thing it
        # described. Forced, because the tree is gone: there is nothing left to protect and no
        # session left that could be asked.
        from .lease import Lease
        Lease(cfg, name).release(force=True)
    return rc == 0, err.strip()


def _branch_has_commits(cfg, branch, base_sha):
    """Did this branch receive work? Never delete a branch that did, even on an aborted spawn."""
    if not base_sha:
        return True          # cannot prove it is empty; keep it
    rc, out, _ = git(["rev-list", "--count", "%s..%s" % (base_sha, branch)], cwd=cfg.root)
    try:
        return int(out.strip()) > 0
    except ValueError:
        return True


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
IgnoreCheck = collections.namedtuple("IgnoreCheck", "stageable checked")


def unignored(worktree, paths):
    """Which of these paths `git add -A` would actually stage inside the worktree.

    Returns an `IgnoreCheck`: `.stageable` is the verdict, `.checked` is the evidence that
    the check ran at all. That second field exists because of an asymmetry worth naming —
    **a refusal cannot be produced by absence, but a permission can.** "Nothing would be
    staged" and "nothing was examined" are the same observation from outside, so a test
    asserting only the empty verdict passes identically against a guard that does nothing.
    Measured: neutering this function to return no findings left every assertion green
    except the refusal case — the permissive half noticed nothing at all.

    A guard that *speaks* when it permits can be tested on its reason. This one is silent by
    nature, so it carries the mark instead — the same trick a Stop gate uses when it records
    every invocation so that the mark's absence proves the hook never fired.

    An earlier version *wrote* the paths into `info/exclude` instead of checking them. That
    was wrong in a way worth recording, because it is this project's own lesson landing on
    its own code: **`info/exclude` is not per-worktree.** `git rev-parse --git-path
    info/exclude` resolves to the shared git dir from inside a linked worktree, and git
    honours no per-worktree equivalent (`$GIT_DIR/info/exclude` under `.git/worktrees/<n>/`
    is simply not read). So excluding a path "for one Crawler" silently changed the ignore
    rules of the main checkout and every sibling — a shared single-consumer resource that
    nobody had named, mutated at every spawn.

    Verifying instead of mutating also puts the fix in the right place: a path that would be
    staged belongs in the repo's own tracked `.gitignore`, which crosses into every worktree
    by itself and needs no per-spawn action at all.
    """
    paths = list(paths)
    if not paths:
        return IgnoreCheck([], [])
    rc, out, _ = git(["check-ignore", "--no-index"] + paths, cwd=worktree)
    ignored = {l.strip() for l in out.splitlines() if l.strip()} if rc in (0, 1) else set()
    missing = []
    for p in paths:
        if p in ignored:
            continue
        if not os.path.lexists(os.path.join(worktree, p)):
            continue           # not there at all; nothing to stage
        missing.append(p)
    return IgnoreCheck(missing, paths)


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

        # AN INJECT PATH INSIDE THE HARNESS DIRECTORY SILENTLY DEFEATS PROVISIONING (#22).
        # `os.makedirs(os.path.dirname(dst))` below creates the parent, so injecting
        # `<harness>/bin/anything` creates `<harness>/` as a side effect — and `harness.provision`
        # runs AFTER this, sees a directory that already exists, and takes its "already present,
        # left alone" branch. The harness is never provisioned, so it cannot answer the contract,
        # and the spawn aborts with a refusal that is entirely about the harness's embedding
        # contract. Every word of it is true and it points at the wrong party.
        #
        # Refused HERE rather than left to that downstream abort, because the reader's next move
        # matters: the honest response to that message is to go read the harness's docs and
        # consider `harness.require=false` — accepting a Crawler with unverified rules to work
        # around a self-inflicted config error. The minimal workaround for a gap in the layer
        # below quietly disabling the guard that checks the layer below is the exact shape this
        # project exists to catch.
        from . import harness as _harness
        owned_by = _harness.owns_path(cfg, src_rel)
        if owned_by:
            problems.append(
                "inject %s is INSIDE the harness directory %s, and injecting it would create "
                "that directory before the harness is provisioned into this worktree — after "
                "which provisioning leaves it alone and the harness cannot answer "
                "`worktree --porcelain`. The spawn would then abort blaming the harness's "
                "embedding contract for a config conflict.\n"
                "  The harness owns what crosses into a worktree: set harness.installer, or "
                "track %s in git. Do not inject into it, and do not reach for "
                "harness.require=false — that accepts a Crawler whose rules are unverified."
                % (src_rel, owned_by, owned_by))
            continue

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

    # Verify, never mutate: see unignored().
    paths = [(e if isinstance(e, str) else e.get("path")) for e in declared]
    stageable = unignored(worktree, [p for p in paths if p]).stageable
    if stageable:
        problems.append(
            "these injected path(s) are NOT ignored by the repo, so an agent running "
            "`git add -A` would commit them onto its branch: %s\n"
            "Add them to the repo's tracked .gitignore — that crosses into every worktree by "
            "itself. showrunner will not write git's shared exclude file: it is not "
            "per-worktree, so doing so would change the ignore rules of the main checkout and "
            "every sibling Crawler." % ", ".join(stageable))
    elif paths:
        results.append("all injected paths are ignored by the repo, so `git add -A` cannot "
                       "stage them")
    return results, problems


# ------------------------------------------------------- shared-state audit
DEFAULT_SHARED_STATE = [
    {
        "what": "the per-agent harness's session state (edited-file set, claims, authorizations)",
        "detect": [".game_loop", ".loop"],
        "why": "a harness scopes this to the SESSION, not the tree — one session is one session "
               "however many trees it touches. So the set of files 'you' edited spans every "
               "worktree you have worked in, and an orchestrator's set will not contain what its "
               "Crawlers wrote at all.",
        "consequence": "a provenance check keyed to 'did YOU edit this?' answers no for work a "
                       "sibling did, and no for every file a merge brought in.",
        "instead": "for an integration commit, declare the provenance: "
                   "`showrunner integration-commit --crawler <name>` answers the better question "
                   "— does the staged set match the union of what the merged Crawlers edited?",
    },
    {
        "what": "the harness's COMMIT gate, which is resolved per-tree and needs one HERE",
        "detect": [".game_loop", ".loop"],
        "why": "what a change owes, and whether the evidence is newer than the change, are facts "
               "about a TREE. A harness that gets this right resolves the record from the tree the "
               "commit targets and REFUSES when that tree carries no harness, rather than "
               "reporting confidence about files the commit does not contain.",
        "consequence": "your first `git commit` in this worktree is DENIED outright if the harness "
                       "is absent here — and `git worktree add` copies tracked files only, so a "
                       "gitignored harness directory does not come across.",
        "instead": "install the harness into this worktree (its own installer, pointed here), or "
                   "ask the orchestrator to. Do NOT reach for --no-verify: a stuck agent under a "
                   "mandate to finish is exactly the situation where bypassing starts looking "
                   "reasonable, and this denial is telling you something true.",
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


HARNESS_DIRS = (".game_loop", ".loop")


def harness_gap(cfg, worktree_path=None):
    """Will the Crawler land in a worktree with no per-agent harness? Returns a note or None.

    A harness that resolves its commit gate per-tree — the correct design, since what a change
    owes is a fact about a tree — must refuse when the tree being committed carries no record,
    rather than answer from a tree whose files the commit does not contain. That refusal is
    right, and it lands on the orchestrator: **`git worktree add` copies tracked files only**,
    so a gitignored harness directory never crosses into the worktree and the Crawler is denied
    its first commit.

    This is the sibling of the secret-injection problem (#10) with the harness as the missing
    file, and it is exactly as invisible at spawn time.
    """
    present = [d for d in HARNESS_DIRS if os.path.isdir(os.path.join(cfg.root, d))]
    if not present:
        return None
    tracked = set()
    rc, out, _ = run(["git", "ls-files"], cwd=cfg.root)
    if rc == 0:
        tracked = {line.split("/")[0] for line in out.splitlines() if line.strip()}
    missing = [d for d in present if d not in tracked]
    if not missing:
        return None
    if worktree_path:
        missing = [d for d in missing if not os.path.exists(os.path.join(worktree_path, d))]
        if not missing:
            return None
    return (
        "%s is present in the main checkout but NOT tracked by git, so it does not cross into a "
        "worktree. If its commit gate resolves per-tree it will DENY the Crawler's first commit; "
        "if it resolves from the main checkout instead, it will answer about files the commit "
        "does not contain. Install the harness into the worktree at spawn, or commit it."
        % ", ".join(missing))


# -------------------------------------------------------------- the spawn
def branch_for(leaf_id):
    """The branch `spawn` gives a leaf. One rule, so callers stop re-deriving it."""
    return "showrunner/%s" % slug(leaf_id, 60)


def base_report(cfg, graph, leaf, base="HEAD"):
    """What `base` actually resolves to, and whether the leaf's dependencies are in it.

    THE DEFAULT IS INVISIBLE AND CONTEXT-DEPENDENT, which is the whole issue (#33). `spawn`
    cuts from the PRIMARY checkout's HEAD, so the identical command produces a correct base or
    a wrong one depending on where an unrelated checkout happens to be pointing. The reported
    case: five chained leaves, the checkout moved back to `main` between spawns, and the last
    Crawler came up with none of its prerequisite in history.

    THE COST IS NOT A WASTED WORKTREE. A brief that says "if L4 has landed do both halves,
    otherwise ship the smaller one and say so" is a GOOD brief — and with a silently wrong base
    it becomes a trap: the Crawler correctly observes the prerequisite is absent, correctly
    takes the smaller path, and correctly reports a complete honest outcome, for a reason that
    is purely the orchestrator's dispatch error. Half the item ships and every gate is green.
    It was caught only because that Crawler refuted the orchestrator's `--finding`, which is
    `--finding` working as designed and too thin a thread to hang this on.

    Returns a dict; never raises. `missing` is the finding — a dependency whose branch is not
    an ancestor of the base. `unknown` is the honest third answer: a backend that cannot list
    dependencies (br) or a dependency that was never spawned answers neither yes nor no, and
    saying "nothing is missing" there would be a claim about a graph this could not read.
    """
    rc, sha, _ = git(["rev-parse", "%s^{commit}" % base], cwd=cfg.root)
    sha = (sha or "").strip() if rc == 0 else None
    rc2, named, _ = git(["rev-parse", "--abbrev-ref", base], cwd=cfg.root)
    named = (named or "").strip() if rc2 == 0 else base
    if named == "HEAD":
        rc3, head_branch, _ = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cfg.root)
        named = (head_branch or "").strip() or "HEAD"

    out = {"base": base, "sha": sha, "branch": named, "explicit": base != "HEAD",
           "missing": [], "present": [], "unknown": []}
    if not sha:
        out["unknown"].append("git cannot resolve %r in %s" % (base, cfg.root))
        return out

    try:
        deps = graph.deps_of(leaf["id"])
    except Exception as exc:                        # noqa: BLE001
        # Refused from the br adapter, which declines to answer rather than returning [].
        out["unknown"].append("dependencies could not be listed: %s" % str(exc).split("\n")[0])
        return out

    for dep in deps:
        dep_branch = branch_for(dep)
        rc, _, _ = git(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % dep_branch],
                       cwd=cfg.root)
        if rc != 0:
            out["unknown"].append(
                "%s has no branch %s — it was never spawned, or its work landed under a name "
                "this cannot derive" % (dep, dep_branch))
            continue
        rc, _, _ = git(["merge-base", "--is-ancestor", dep_branch, sha], cwd=cfg.root)
        (out["present"] if rc == 0 else out["missing"]).append((dep, dep_branch))
    return out


def spawn(cfg, leaf, actor="crawler", base="HEAD", branch=None):
    """Create everything a Crawler gets. Returns a record; raises on anything unsafe."""
    cfg.require_valid()
    name = crawler_name(leaf["id"], actor)
    branch = branch or branch_for(leaf["id"])
    # Resolve the base to a SHA *before* creating the branch. Afterwards git cannot tell
    # a fully-merged branch from one that never received a commit — both have the base as
    # their merge-base — and that distinction decides whether a worktree is garbage or the
    # only copy of a dead Crawler's work.
    rc, base_sha, _ = git(["rev-parse", "%s^{commit}" % base], cwd=cfg.root)
    base_sha = base_sha.strip() if rc == 0 else None
    path = create(cfg, name, branch, base)
    scratch = scratch_for(cfg, name)
    injected, problems = inject(cfg, path)

    # The harness is provisioned before anything else can go wrong, and its rule files are
    # compared byte-for-byte against the parent's. A Crawler whose rails are quietly weaker
    # than the orchestrator's is worse than one with no rails, because the run looks guarded.
    # SHOWRUNNER'S OWN HOOKS, before the harness and for the same reason (#31). They are the
    # one thing a deliberately-untracked install could not get into a worktree, and the only
    # remedy offered was "commit it" — unavailable in a shared repo that excludes `.showrunner`
    # on purpose. One mechanism, one answer, whoever's files they are.
    from . import lease as _lease
    provisioned = _lease.provision_hooks(cfg, path)

    from . import harness
    hp2, harness_problems, harness_warnings = harness.provision(cfg, path)
    provisioned += hp2
    provisioned += ["NOTE: %s" % w for w in harness_warnings]
    if harness_problems and harness.spec(cfg)["require"]:
        problems += harness_problems
    elif harness_problems:
        provisioned += ["NOT ENFORCED (harness.require is false): %s" % p for p in harness_problems]

    if problems:
        # Fail the spawn loudly rather than handing over a half-built environment — and undo
        # the branch as well as the worktree. Leaving the branch behind means the retry, after
        # the operator has fixed the actual problem, fails with a *different* and misleading
        # error ("a branch named X already exists"), which is how a fixable spawn turns into a
        # wedged one at 3am.
        remove(cfg, name, force=True)
        if not _branch_has_commits(cfg, branch, base_sha):
            git(["branch", "-D", branch], cwd=cfg.root)
        die("spawn aborted — the Crawler's environment is incomplete:\n  - %s\n"
            "A Crawler that cannot reach a service will write the service up as broken, in the "
            "same confident tone as a real finding — and a Crawler running under different rules "
            "than the orchestrator will do it while every gate stays green."
            % "\n  - ".join(problems), code=2)

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
        "provisioned": provisioned,
        "shares": audit_shared(cfg),
        "harness_gap": harness_gap(cfg, path),
        "created_ts": now(),
    }
    return record
