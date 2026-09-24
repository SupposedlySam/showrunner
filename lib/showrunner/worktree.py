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


# ALWAYS IN A CONE, whatever the leaf says (#90). Cone mode keeps the ROOT FILES and the listed
# directories and nothing else, so `.claude/settings.json` — which lives in a directory, not at
# the root — would vanish, and every hook registered in it with it. A Crawler with no
# registrations runs with no rails and reports nothing wrong.
CONE_ALWAYS = (".claude",)


def hook_dirs(cfg, base="HEAD"):
    """Top-level directories that a registered hook command points into. Never raises.

    DERIVED, NOT LISTED. A hook registered as `$CLAUDE_PROJECT_DIR/.game_loop/bin/...` needs
    `.game_loop/` in the cone or the registration names a file that is not there. Which tools a
    consumer installed, and where, is theirs to decide — so this reads both settings layers at
    spawn time rather than hard-coding `.showrunner` and `.game_loop` and missing the third tool.
    """
    import json as _json
    import re as _re
    # ANY VARIABLE, NOT ONE SPELLING (from game_loop's owner). Hooks write
    # `$CLAUDE_PROJECT_DIR/.game_loop/...`, but game_loop's statusline goes through a variable:
    # `p="${CLAUDE_PROJECT_DIR:-.}"; gl="$p/.game_loop_self/..."`. A parser keyed on the one
    # spelling misses the second form. So take the first path segment after ANY variable — and
    # then keep only names that are real TRACKED top-level directories at the base, which drops
    # `$HOME/...` noise and gitignored dirs like `.game_loop_self` that no cone can supply anyway.
    # The segment may END at a quote, a space or `;` as well as a slash: game_loop's hooks write
    # `d="$CLAUDE_PROJECT_DIR/.game_loop"; ... exec "$d"/bin/X`, so requiring a trailing slash
    # found only `bin` from `$d/bin/` and MISSED `.game_loop` — the exact gap this exists for.
    var_path = _re.compile(
        r'\$\{?[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}?"?/([^/\s"\';]+)(?=[/"\s\';]|$)')
    rc, listing, _ = git(["ls-tree", "--name-only", "-d", base], cwd=cfg.root)
    tracked = set(listing.split()) if rc == 0 else None
    out = set()
    for name in ("settings.json", "settings.local.json"):
        try:
            with open(os.path.join(cfg.root, ".claude", name)) as fh:
                data = _json.load(fh)
        except (OSError, ValueError):
            continue
        for entries in ((data.get("hooks") or {}).values() if isinstance(data, dict) else []):
            for entry in entries or []:
                for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
                    cmd = (h or {}).get("command") or ""
                    for m in var_path.finditer(cmd):
                        out.add(m.group(1))
    return out if tracked is None else (out & tracked)


def _leaf_list(leaf, key):
    """A leaf's labels or paths AS A LIST. The graph stores them comma-joined; `Leaf` exposes
    `labels_list` / `paths_list`, and a plain dict gets the same split.

    Iterating the raw field walks its CHARACTERS: the first cut of the cone did exactly that, so
    `sparse_by_label` could never match a real label and no out-of-cone path was ever reported —
    while `--sparse`, which reads no leaf field, worked and hid both.
    """
    prop = getattr(leaf, key + "_list", None)
    if isinstance(prop, list):
        return prop
    raw = leaf.get(key) if hasattr(leaf, "get") else None
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if x]
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def resolve_cone(cfg, leaf, explicit=None, base="HEAD"):
    """(dirs, source) for this leaf's sparse checkout, or (None, why) for a full tree.

    Most specific first: `spawn --sparse`, then `sparse_by_label` in config (the UNION of every
    label the leaf carries — a leaf labelled both `flutter` and `docs` needs both), then nothing,
    which is a full checkout exactly as before. Whatever is chosen gets CONE_ALWAYS and every
    hook directory added, so a cone can narrow the work tree and never the rails.
    """
    dirs, source = None, None
    if explicit:
        dirs, source = list(explicit), "--sparse"
    else:
        by_label = cfg.get("sparse_by_label") if hasattr(cfg, "get") else None
        if isinstance(by_label, dict):
            hit = []
            for label in _leaf_list(leaf, "labels"):
                val = by_label.get(label)
                if isinstance(val, list):
                    hit += [str(v) for v in val]
            if hit:
                dirs, source = hit, "sparse_by_label"
    if not dirs:
        return None, "no cone configured for this leaf — full checkout"
    full = set(d.strip("/") for d in dirs if d and d.strip("/"))
    full |= set(CONE_ALWAYS) | hook_dirs(cfg, base)
    return sorted(full), source


def cone_misses(leaf, cone):
    """Declared leaf paths that fall outside the cone — each one a file the Crawler cannot see."""
    if not cone:
        return []
    tops = set(cone)
    return [p for p in _leaf_list(leaf, "paths")
            if "/" in p.strip("/") and p.strip("/").split("/")[0] not in tops]


def hook_gaps(cfg, tree):
    """Registered hooks that would find NONE of their directories in the finished tree.

    THE CHECK THAT MATTERS FOR EVERY TREE, not just a sparse one (game_loop's owner, #90). A
    hook whose file is missing exits non-zero WITHOUT blocking, so a tree lacking a hook's
    directory is a Crawler with that gate silently off. `cone_gaps` only sees TRACKED
    directories, because only those can be in a cone — but `install.sh --local` registers hooks in
    the untracked `settings.local.json` against an untracked directory, and a plain worktree gets
    neither. showrunner's own hooks and game_loop's harness are copied in by `spawn`; anything
    else registered that way was not, and nothing said so.

    PER COMMAND, "at least one of". game_loop's commands try a pinned `.game_loop_self` first and
    fall back to `.game_loop`, and the pinned copy is gitignored — so requiring every directory a
    command names would refuse every spawn. A command is satisfied when ANY directory it names is
    present. Only names that exist in the main checkout count, which keeps `$HOME/...` paths out.

    Returns human-readable strings, one per unsatisfied command. Run AFTER all provisioning.
    """
    import json as _json
    import re as _re
    var_path = _re.compile(
        r'\$\{?[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}?"?/([^/\s"\';]+)(?=[/"\s\';]|$)')
    out = []
    for name in ("settings.json", "settings.local.json"):
        try:
            with open(os.path.join(cfg.root, ".claude", name)) as fh:
                data = _json.load(fh)
        except (OSError, ValueError):
            continue
        for event, entries in ((data.get("hooks") or {}).items() if isinstance(data, dict) else []):
            for entry in entries or []:
                for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
                    cmd = (h or {}).get("command") or ""
                    names = {m.group(1) for m in var_path.finditer(cmd)}
                    names = {n for n in names if os.path.exists(os.path.join(cfg.root, n))}
                    if names and not any(os.path.isdir(os.path.join(tree, n)) for n in names):
                        out.append("a %s hook in %s needs %s, and the Crawler's tree has none of "
                                   "them — that gate would be silently OFF"
                                   % (event, name, " or ".join(sorted(n + "/" for n in names))))
    return out


def cone_gaps(cfg, tree, base="HEAD"):
    """What a sparse tree lacks that its hooks need. [] when nothing is missing.

    PROVE IT ACTED (#90). game_loop's owner measured it: a registered hook whose file is gone
    exits 126, and only exit 2 blocks, so the tool call proceeds and the gate is simply OFF —
    including a guard designed to fail CLOSED, because a missing shim never reaches that code. A
    wrong cone is therefore an UNGUARDED Crawler rather than a stuck one, and nothing reports
    it. So the checked-out tree is compared against what the hooks need, and a gap refuses.
    """
    need = set(CONE_ALWAYS) | hook_dirs(cfg, base)
    rc, listing, _ = git(["ls-tree", "--name-only", "-d", base], cwd=cfg.root)
    tracked = set(listing.split()) if rc == 0 else set(need)
    gaps = sorted(d + "/" for d in need
                  if d in tracked and not os.path.isdir(os.path.join(tree, d)))
    rc, has_settings, _ = git(["ls-tree", "--name-only", base, ".claude/settings.json"],
                              cwd=cfg.root)
    if rc == 0 and has_settings.strip() and not os.path.isfile(
            os.path.join(tree, ".claude", "settings.json")):
        gaps.append(".claude/settings.json")
    return gaps


def create(cfg, name, branch, base="HEAD", cone=None):
    """Create the worktree. Refuses rather than degrading if placement is unsafe.

    With `cone`, the tree is SPARSE from the first byte (#90): added with `--no-checkout`, the
    cone set, then checked out — so the full tree is never written and then deleted. A brief
    telling the Crawler to narrow its own tree ran after the full checkout had already landed,
    which is how a campaign of worktrees filled a 1 TB disk.
    """
    cfg.require_valid()
    ensure_root(cfg)
    path = worktree_path(cfg, name)
    if os.path.exists(path):
        # NAME THE WAY OUT. A launch whose post-checkout hook failed leaves the tree behind, so
        # the retry hits this refusal — and the operator then needs the tree AND the branch gone
        # before they can try again. Reported after hitting it twice in one night: the refusal
        # was correct and cost three commands to act on, none of them printed.
        #
        # NOT cleaned up automatically. This is the one path where the tree may hold the only
        # copy of an hour's work, and a retry that silently deletes it is the loss this whole
        # file is built to prevent. The commands are shown; the decision stays with a human.
        die("worktree path already exists: %s\n"
            "  It may hold uncommitted work — inspect it rather than reusing it blindly "
            "(`showrunner reap` reports abandoned trees).\n"
            "\n"
            "  If a previous launch left it behind — a post-checkout hook can fail AFTER the\n"
            "  tree is created — this is the way out, in order:\n"
            "      git -C %s status --porcelain     # empty means nothing is lost\n"
            "      git worktree remove %s\n"
            "      git branch -D %s"
            % (path, path, path, branch), code=2)
    if cone:
        rc, _, err = git(["worktree", "add", "--no-checkout", "-b", branch, path, base],
                         cwd=cfg.root)
        if rc == 0:
            rc, _, err = git(["sparse-checkout", "set", "--cone"] + list(cone), cwd=path)
            if rc == 0:
                rc, _, err = git(["checkout"], cwd=path)
                if rc == 0:
                    missing = cone_gaps(cfg, path, base)
                    if missing:
                        git(["worktree", "remove", "--force", path], cwd=cfg.root)
                        git(["branch", "-D", branch], cwd=cfg.root)
                        die("REFUSED: the sparse tree for %s is missing what its hooks need: %s.\n"
                            "  A hook whose file is absent exits non-zero WITHOUT blocking, so every "
                            "gate in this Crawler would be off and nothing would say so. The tree "
                            "was removed rather than handed over." % (name, ", ".join(missing)),
                            code=2)
            else:
                # A tree with no cone and no checkout is empty and useless. Remove it so the
                # retry after fixing the cone does not hit "already exists".
                git(["worktree", "remove", "--force", path], cwd=cfg.root)
                git(["branch", "-D", branch], cwd=cfg.root)
                die("could not set the sparse cone %s for %s: %s"
                    % (" ".join(cone), name, err.strip()), code=2)
    else:
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
        # A REPO WITH NO COMMITS IS THE OTHER CASE WITH ITS OWN REMEDY, and it reached an owner
        # of a brand-new repo as `fatal: invalid reference: HEAD` (reported by balooga-owner,
        # onboarding a fresh Flutter app). `install.sh` succeeds on such a repo and so does
        # `add`, so the first thing that fails is `spawn` — several steps after the cause, with
        # a message about a git internal rather than about the one missing action.
        #
        # Worth its own branch for the same reason as the post-checkout case above: the remedy
        # is unrelated to git. Nothing is broken and nothing needs repairing — a branch simply
        # cannot be cut from a history that does not exist yet. Detected by asking whether HEAD
        # resolves at all, which is cheap and only runs on the failure path.
        rc_head, _, _ = git(["rev-parse", "--verify", "HEAD"], cwd=cfg.root)
        if rc_head != 0:
            die("this repository has no commits yet, so there is no HEAD to branch from and no "
                "worktree can be created.\n"
                "  Nothing is broken: `spawn` cuts a branch, and a branch needs a history to "
                "start from.\n"
                "\n"
                "  Make one commit and run this again:\n"
                "      git -C %s add -A\n"
                "      git -C %s commit -m 'initial commit'\n"
                "\n"
                "  `install.sh` and `add` both succeed on an empty repo, so this is the first "
                "step that could have told you — which is why it says it here rather than "
                "passing git's `invalid reference: HEAD` along.\n"
                "\n"
                "  git said:\n%s"
                % (cfg.root, cfg.root, _indent(err.strip() or "(git printed nothing)")), code=2)
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


def in_cone(path, cone):
    """Would git's cone-mode sparse checkout materialize `path`? True for a full tree.

    Cone mode includes every file at the root, everything under a cone directory, and the files
    directly inside each ANCESTOR of a cone directory — so `backend/x.env` is present when the
    cone holds `backend/api`, and `backend/other/y` is not.
    """
    if cone is None:
        return True
    path = path.strip("/")
    parent = os.path.dirname(path)
    for d in cone:
        d = d.strip("/")
        if path == d or path.startswith(d + "/"):
            return True
        if parent == "" or d == parent or d.startswith(parent + "/"):
            return True
    return False


def inject(cfg, worktree, cone=None):
    """Materialize the configured paths into a fresh worktree.

    Returns (results, problems). A declared path that is missing is a **problem**, not a
    warning: letting the Crawler discover it as a mysterious runtime failure is how a
    broken environment becomes a confident, detailed, wrong finding about a service.

    AN ENTRY OUTSIDE A SPARSE CONE IS SKIPPED, and said so. Nothing in that tree can use it, and
    the `.gitignore` that covers it in the main checkout usually lives beside it — outside the
    cone too — so provisioning it anyway gets the spawn refused by the ignore check below for a
    path the Crawler was never going to see.

    The reverse — an in-cone path whose ignore rule is out of the cone — cannot occur: a
    `.gitignore` governs only paths beneath its own directory, and cone mode always checks out
    the files directly inside every ancestor of a cone directory. So the ignore check below
    still runs, unchanged, on everything that was provisioned.
    """
    results, problems = [], []
    declared = cfg.get("inject") or []
    skipped = set()
    for entry in declared:
        if isinstance(entry, str):
            entry = {"path": entry}
        src_rel = entry.get("path")
        if not src_rel:
            problems.append("an inject entry has no 'path': %r" % entry)
            continue
        if not in_cone(src_rel, cone):
            skipped.add(src_rel)
            results.append("skip inject %s — outside the cone, so nothing in this tree can "
                           "use it" % src_rel)
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
    paths = [p for p in paths if p and p not in skipped]
    stageable = unignored(worktree, paths).stageable
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


def default_branch(cfg):
    """The branch an implicit base is defensible from, or None if it cannot be determined.

    DERIVED, NEVER CONFIGURED. A `trunk` setting would be one more thing to get wrong, and it
    would be wrong silently: a stale value reads exactly like a correct one. `origin/HEAD` is
    what the remote itself says, and the local fallbacks are checked for EXISTENCE rather than
    assumed, so a repo with neither answers None instead of asserting `main`.

    None is the honest third answer and callers must treat it as "cannot tell", never as "not
    the default branch" — the difference between not looking and having looked.
    """
    rc, out, _ = git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=cfg.root)
    if rc == 0 and out.strip():
        return out.strip().split("/", 1)[-1]
    for name in ("main", "master"):
        rc, _, _ = git(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % name], cwd=cfg.root)
        if rc == 0:
            return name
    return None


def base_report(cfg, graph, leaf, base="HEAD", explicit=None):
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

    # `explicit` CANNOT BE DERIVED FROM THE STRING. `--base HEAD` and no --base at all produce
    # the identical value, and they are opposite facts: one is an operator confirming the
    # checkout is what they mean, the other is nobody having decided. Deriving it made the
    # confirmation indistinguishable from the default, so the guard that asks for a decision
    # rejected the decision. Callers that know pass it; the old derivation stays for those that
    # do not, because it is right for every caller that never had the distinction.
    out = {"base": base, "sha": sha, "branch": named,
           "explicit": (base != "HEAD") if explicit is None else bool(explicit),
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


def drift_report(cfg, leaf, base_sha):
    """What the default branch has done to this leaf's declared paths since the base.

    A BASE CAN BE BEHIND IN A WAY THE DEPENDENCY CHECK CANNOT SEE. `base_report` asks whether
    work this leaf NEEDS is in the base; this asks whether work the default branch has since
    DONE undoes the leaf. A path deleted there is still present in an older base, so the Crawler
    finds it, changes it, tests it green, and the work is for code that no longer exists — found
    out only at merge.

    Compared against `origin/<default>` when that ref exists, else the local default branch. It
    does NOT fetch: a fetch per spawn is slow and a network failure would read as a clean
    answer, so the report names the ref it read and says it was not fetched.

    Returns {"ref", "merge_base", "deleted": [(path, "<sha> <subject>")], "modified": [...],
    "unknown": reason-or-None}. Never raises.
    """
    out = {"ref": None, "merge_base": None, "deleted": [], "modified": [], "unknown": None}
    paths = _leaf_list(leaf, "paths")
    if not paths:
        return out
    default = default_branch(cfg)
    if not default:
        out["unknown"] = "this repo has no origin/HEAD, main or master to compare against"
        return out
    for ref in ("refs/remotes/origin/%s" % default, "refs/heads/%s" % default):
        rc, _, _ = git(["rev-parse", "--verify", "--quiet", ref], cwd=cfg.root)
        if rc == 0:
            out["ref"] = ref.split("/", 2)[-1]
            break
    if not out["ref"] or not base_sha:
        out["unknown"] = ("%s does not resolve" % default) if base_sha else "the base did not resolve"
        return out
    rc, mb, _ = git(["merge-base", base_sha, out["ref"]], cwd=cfg.root)
    if rc != 0 or not mb.strip():
        out["unknown"] = "the base shares no history with %s" % out["ref"]
        return out
    mb = out["merge_base"] = mb.strip()
    span = "%s..%s" % (mb, out["ref"])
    for path in paths:
        rc, gone, _ = git(["log", "--diff-filter=D", "--format=%h %s", "-1", span, "--", path],
                          cwd=cfg.root)
        # Deleted AND still absent at the tip: a path deleted and later restored is a
        # modification, not a removal.
        rc2, _, _ = git(["cat-file", "-e", "%s:%s" % (out["ref"], path.strip("/"))],
                        cwd=cfg.root)
        if rc == 0 and gone.strip() and rc2 != 0:
            out["deleted"].append((path, gone.strip()))
            continue
        rc, touched, _ = git(["log", "--format=%h %s", "-1", span, "--", path], cwd=cfg.root)
        if rc == 0 and touched.strip():
            out["modified"].append((path, touched.strip()))
    return out


def spawn(cfg, leaf, actor="crawler", base="HEAD", branch=None, sparse=None, drift=None):
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
    cone, cone_source = resolve_cone(cfg, leaf, sparse, base)
    path = create(cfg, name, branch, base, cone=cone)
    scratch = scratch_for(cfg, name)
    injected, problems = inject(cfg, path, cone=cone)

    # The harness is provisioned before anything else can go wrong, and its rule files are
    # compared byte-for-byte against the parent's. A Crawler whose rails are quietly weaker
    # than the orchestrator's is worse than one with no rails, because the run looks guarded.
    # SHOWRUNNER'S OWN HOOKS, before the harness and for the same reason (#31). They are the
    # one thing a deliberately-untracked install could not get into a worktree, and the only
    # remedy offered was "commit it" — unavailable in a shared repo that excludes `.showrunner`
    # on purpose. One mechanism, one answer, whoever's files they are.
    from . import lease as _lease
    provisioned = _lease.provision_hooks(cfg, path)
    # AND THE REGISTRATION THAT ACTIVATES THEM. The shim FILES were provisioned here; the
    # settings entry naming them was not, and an untracked one cannot cross on its own —
    # `git worktree add` copies tracked files only. Measured: a `--local` install produced
    # worktrees with no `.claude` directory at all, so every hook showrunner owns was absent
    # from every Crawler while the main checkout reported them all registered and healthy. A
    # provisioned shim nothing registers has never once run, which is this file's own sentence
    # about the guard one layer up.
    _mirrored = _lease.mirror_local_registration(cfg, path)
    if _mirrored:
        provisioned.append(_mirrored)

    from . import harness
    hp2, harness_problems, harness_warnings = harness.provision(cfg, path)
    provisioned += hp2
    provisioned += ["NOTE: %s" % w for w in harness_warnings]
    if harness_problems and harness.spec(cfg)["require"]:
        problems += harness_problems
    elif harness_problems:
        provisioned += ["NOT ENFORCED (harness.require is false): %s" % p for p in harness_problems]

    # LAST, after everything spawn copies in: every registered hook must find its directory.
    problems += hook_gaps(cfg, path)

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
        # RECORDED so a reviewer can see what the Crawler could not (#90).
        "sparse": ({"dirs": cone, "source": cone_source,
                    "misses": cone_misses(leaf, cone)} if cone else None),
        "drift": drift if drift and (drift.get("deleted") or drift.get("modified")) else None,
        "created_ts": now(),
    }
    return record
