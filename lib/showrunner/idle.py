"""Reclaim IDLE worktrees — every registered one, not only the campaign's — and archive first (#105).

`gc` reclaims a tree only when the campaign record knows it AND its branch is merged AND it is
clean. A busy monorepo reached 172 registered worktrees and about 97 GB that it would not touch:
review and closeout trees made by other tooling, lead scratch trees, trees dirty only with
harness output, and refuted work whose branch should stay but whose tree has no reason to.

So this asks a different question — has anybody used this tree lately? — of EVERY worktree git
has registered for the repo, and makes removal lossless instead of making it rare: before a tree
goes, its uncommitted diff, its untracked files and its HEAD are kept, and a HEAD no branch or tag
contains is pinned under `refs/worktree-archive/`. That is what lets the idle bar be aggressive.

A TREE IS KEPT when any of these holds — each is a reason printed beside it:
  * it is the main checkout;
  * git has it LOCKED;
  * it is named by --keep;
  * a live Crawler in the campaign record works in it, or an in-progress claim names it;
  * a live session holds its lease;
  * a process has its working directory inside it;
  * it was ACTIVE within the window: the newest of its HEAD commit time, the mtimes of its own
    git `index`, `HEAD` and `logs/HEAD`, and the mtimes of its dirty files.
Anything else is IDLE. Unknown is never idle: a tree whose activity cannot be read is kept.

WHAT THIS CANNOT SEE: an agent that went quiet and means to come back. The archive is the answer
to that — `git worktree add <path> <branch>`, then `git apply <name>.patch` and untar the
untracked files — rather than a guess about intent.
"""
import json
import os
import re
import subprocess
import time

from .util import git, now


def parse_duration(text):
    """Seconds for "90m", "24h", "2d", "1w"; None when it is not one."""
    m = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", str(text or ""))
    if not m:
        return None
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]


def registered(cfg):
    """[{path, head, branch, locked}] for every worktree git has registered, main included."""
    rc, out, _ = git(["worktree", "list", "--porcelain"], cwd=cfg.root)
    if rc != 0:
        return None
    trees, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur.get("path"):
                trees.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = {"path": os.path.realpath(val), "head": None, "branch": None,
                   "locked": False, "prunable": False}
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val[len("refs/heads/"):] if val.startswith("refs/heads/") else val
        elif key == "locked":
            cur["locked"] = True
        elif key == "prunable":
            cur["prunable"] = True
    return trees


def _gitdir(path):
    rc, out, _ = git(["rev-parse", "--absolute-git-dir"], cwd=path)
    return out.strip() if rc == 0 and out.strip() else None


def last_active(path):
    """Epoch seconds of the newest sign of use, or None when nothing could be read."""
    times = []
    rc, out, _ = git(["log", "-1", "--format=%ct"], cwd=path)
    if rc == 0 and out.strip().isdigit():
        times.append(int(out.strip()))
    gd = _gitdir(path)
    if gd:
        for rel in ("index", "HEAD", os.path.join("logs", "HEAD")):
            try:
                times.append(int(os.path.getmtime(os.path.join(gd, rel))))
            except OSError:
                continue
    # --no-optional-locks: a plain `git status` REFRESHES the index and so rewrites its mtime —
    # the act of asking whether a tree is idle would make it look active, on every later look.
    rc, out, _ = git(["--no-optional-locks", "status", "--porcelain"], cwd=path)
    if rc == 0:
        for line in out.splitlines():
            p = line[3:].strip().strip('"')
            if " -> " in p:
                p = p.split(" -> ", 1)[1]
            try:
                times.append(int(os.path.getmtime(os.path.join(path, p))))
            except OSError:
                continue
    return max(times) if times else None


def _claimed_trees(cfg, graph):
    out = set()
    try:
        from .graph import IN_PROGRESS
        for leaf in graph.list(status=IN_PROGRESS):
            if leaf.get("claim_tree"):
                out.add(os.path.realpath(leaf["claim_tree"]))
    except Exception:                                           # noqa: BLE001
        pass
    return out


def _held_leases(cfg):
    from . import lease as _lease, locks as _locks
    held = set()
    try:
        for row in _lease.status(cfg):
            if row.get("state") == _locks.HELD:
                held.add(os.path.realpath(row["path"]))
    except Exception:                                           # noqa: BLE001
        pass
    return held


def classify(cfg, graph, idle_seconds, keep=(), cwds=None, clock=None):
    """(idle, kept, unregistered). Rows carry path, branch, head, last_active and why."""
    from . import campaign as _campaign
    trees = registered(cfg)
    if trees is None:
        return None, None, None
    main = os.path.realpath(cfg.root)
    clock = clock or now
    keep = {os.path.realpath(k) if os.path.isabs(k) else k for k in keep or ()}
    live_trees = set()
    for e in _campaign.load(cfg).get("crawlers", []):
        if e.get("worktree") and _campaign.live(e):
            live_trees.add(os.path.realpath(cfg.abspath(e["worktree"])))
    claimed = _claimed_trees(cfg, graph)
    leased = _held_leases(cfg)
    if cwds is None:
        from .util import process_cwds
        cwds = process_cwds()
    busy = set()
    for _pid, cwd in (cwds or {}).items():
        real = os.path.realpath(cwd or "/")
        for t in trees:
            if real == t["path"] or real.startswith(t["path"] + os.sep):
                busy.add(t["path"])

    idle, kept = [], []
    for t in trees:
        path = t["path"]
        row = dict(t, last_active=None, why="")
        if path == main:
            continue
        if t["prunable"] or not os.path.isdir(path):
            row["why"] = "registered but its directory is gone — `git worktree prune` clears it"
            kept.append(row)
            continue
        name = os.path.basename(path)
        reasons = []
        if t["locked"]:
            reasons.append("git has it LOCKED")
        if path in keep or name in keep:
            reasons.append("named by --keep")
        if path in live_trees:
            reasons.append("a LIVE Crawler works in it")
        if path in claimed:
            reasons.append("an in-progress claim names it")
        if path in leased:
            reasons.append("a live session holds its lease")
        if path in busy:
            reasons.append("a process has its working directory inside it")
        if cwds is None:
            reasons.append("the process table could not be read, so a process inside it cannot "
                           "be ruled out")
        last = last_active(path)
        row["last_active"] = last
        if last is None:
            reasons.append("its activity could not be read — unknown is never idle")
        elif clock() - last < idle_seconds:
            reasons.append("active %s ago" % _age(clock() - last))
        if reasons:
            row["why"] = "; ".join(reasons)
            kept.append(row)
        else:
            row["why"] = "idle %s" % _age(clock() - last)
            idle.append(row)

    unregistered = []
    root = cfg.worktree_root
    known = {t["path"] for t in trees}
    if os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            full = os.path.realpath(os.path.join(root, d))
            if os.path.isdir(full) and full not in known:
                unregistered.append(full)
    return idle, kept, unregistered


def _age(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return "%dd%dh" % (seconds // 86400, (seconds % 86400) // 3600)
    if seconds >= 3600:
        return "%dh%dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dm" % (seconds // 60)


def _contained(cfg, sha):
    """Does any branch or tag contain `sha`? None when it cannot be told."""
    for kind in ("--branches", "--tags"):
        rc, out, _ = git(["for-each-ref", "--contains", sha, "--format=%(refname)",
                          "refs/heads" if kind == "--branches" else "refs/tags"], cwd=cfg.root)
        if rc != 0:
            return None
        if out.strip():
            return True
    return False


def archive_dir(cfg):
    return os.path.join(cfg.state_dir, "archive", "worktrees")


def archive(cfg, row, stamp=None):
    """Make removing `row`'s tree lossless. Returns (record, None) or (None, why it could not)."""
    path, name = row["path"], os.path.basename(row["path"])
    base = os.path.join(archive_dir(cfg), "%s-%s" % (name, stamp or time.strftime("%Y%m%dT%H%M%S")))
    os.makedirs(base, exist_ok=True)
    head = row.get("head")
    pinned = None
    if head:
        contained = _contained(cfg, head)
        if contained is None:
            return None, "could not tell whether any ref contains HEAD %s" % head[:12]
        if not contained:
            pinned = "refs/worktree-archive/%s" % name
            rc, _, err = git(["update-ref", pinned, head], cwd=cfg.root)
            if rc != 0:
                return None, "could not pin HEAD %s: %s" % (head[:12], err.strip())
    rc, diff, err = git(["diff", "--binary", "HEAD"], cwd=path)
    if rc != 0:
        return None, "could not read its uncommitted diff: %s" % err.strip()
    with open(os.path.join(base, "%s.patch" % name), "w") as fh:
        fh.write(diff)
    rc, others, err = git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=path)
    if rc != 0:
        return None, "could not list its untracked files: %s" % err.strip()
    files = [f for f in others.split("\0") if f]
    tgz = None
    if files:
        tgz = os.path.join(base, "%s.untracked.tgz" % name)
        p = subprocess.run(["tar", "czf", tgz, "--null", "-T", "-"], cwd=path,
                           input="\0".join(files), text=True, capture_output=True)
        if p.returncode != 0:
            return None, "could not archive its untracked files: %s" % p.stderr.strip()
    record = {"path": path, "branch": row.get("branch"), "head": head, "pinned": pinned,
              "patch_bytes": len(diff), "untracked_files": len(files),
              "untracked": tgz, "why": row.get("why"), "archived_at": now()}
    with open(os.path.join(base, "%s.json" % name), "w") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
    return dict(record, archive=base), None


def reclaim(cfg, row):
    """Archive, then remove. (record, None) or (None, why). The branch is never touched."""
    record, why = archive(cfg, row)
    if record is None:
        return None, why
    rc, _, err = git(["worktree", "remove", "--force", row["path"]], cwd=cfg.root)
    if rc != 0 or os.path.isdir(row["path"]):
        return None, "archived at %s, but `git worktree remove` failed: %s" % (
            record["archive"], err.strip() or "the directory is still there")
    return record, None
