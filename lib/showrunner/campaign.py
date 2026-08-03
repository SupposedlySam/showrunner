"""The campaign record: lifecycle, reconciliation on resume, and serial integration.

Issues #7 and #9 are the same missing thing — showrunner has no memory of a campaign
between the moment a Crawler finishes and the moment its work is part of the trunk, and
no way to notice that a Crawler stopped existing.

**Lifecycle (#7).** The two pieces of state a Crawler holds are reclaimed by completely
different mechanisms, and only one of them is safe. The resource lock already handles
death correctly: the holder is a live PID, and a lock whose holder is not alive is
reclaimable. The graph claim does not — a claim marks a leaf in progress with no liveness
attached at all. A Crawler that dies (killed, out of context, usage limit, crashed tmux)
leaves its leaf claimed forever, and `ready` means unblocked *and unclaimed*, so **the
work silently leaves the queue**: `ready` goes dry, the orchestrator concludes the
campaign is complete, and the leaf is neither done nor visible as undone. That is worse
than a crash, because the loop terminates reporting success.

So a claim gets the same liveness the lock has, and reclaim is **loud**: a leaf that was
claimed and abandoned is evidence about either the work or the harness, and a run that
quietly re-queues it three times has learned nothing. On reclaim the worktree is not
necessarily garbage — it may hold the only copy of real work — so it is reported, never
deleted. Same for the Crawler's scratch dir.

**Resume (#9).** A campaign outlives a session, but its state lives in three places that
know nothing about each other: the graph, the filesystem (worktrees and branches), and
the locks. Nothing reconciles them, so a resumed orchestrator cannot answer its first
question: *which of these branches is already merged, which is abandoned, and which is a
live Crawler I must not disturb?* The record here is reconstructible from the graph plus
git rather than being a second source of truth that can disagree.

**Integration (#9).** A branch is integrated only when the checks pass on the **merged
result**, not when they passed on the branch. Green on a branch is evidence about a trunk
that no longer exists by the time the second branch lands — two Crawlers adding a
subcommand to the same dispatch table touch different lines and still produce a broken
trunk. So: merge serially, re-run the owed checks after each merge, and stop on the first
failure rather than stacking branches onto a broken trunk.
"""

import contextlib
import json
import os

from .util import (atomic_write_json, boot_token, die, eprint, file_lock, git, now,
                   pid_alive, rel, run, try_file_lock)
from . import gates, locks, worktree

RECORD = "campaign.json"


# ------------------------------------------------------------------ record
def path_for(cfg):
    return os.path.join(cfg.state_dir, RECORD)


def load(cfg):
    p = path_for(cfg)
    if not os.path.exists(p):
        return {"crawlers": [], "integrated": [], "base": None}
    with open(p) as fh:
        return json.load(fh)


def save(cfg, data):
    return atomic_write_json(path_for(cfg), data)


@contextlib.contextmanager
def _exclusive(cfg):
    """Serialize a read-modify-write of the campaign record across orchestrators.

    Without this the record silently loses entries: ten concurrent spawns left **three**
    surviving Crawlers. That is not a cosmetic loss — a Crawler missing from the record is
    one `reconcile` cannot find, `reap` cannot reclaim and `integrate` will not merge. The
    work would sit finished on a branch nobody looks at, which is this project's signature
    failure (an outcome that looks like completion and is not) arriving through a new door.
    """
    with file_lock(path_for(cfg)):
        yield


def record_spawn(cfg, spawn_record, pid=None, session=None):
    with _exclusive(cfg):
        return _record_spawn_locked(cfg, spawn_record, pid, session)


def _record_spawn_locked(cfg, spawn_record, pid=None, session=None):
    data = load(cfg)
    entry = dict(spawn_record)
    entry.update({
        "pid": pid,
        "boot": boot_token(),
        "session": session,
        "state": "spawned",
        "worktree": rel(spawn_record["worktree"], cfg.root),
        "scratch": rel(spawn_record["scratch"], cfg.root),
    })
    entry.pop("shares", None)  # an audit for the brief, not durable state
    data["crawlers"] = [c for c in data.get("crawlers", []) if c.get("crawler") != entry["crawler"]]
    data["crawlers"].append(entry)
    save(cfg, data)
    return entry


def set_state(cfg, crawler, state, **extra):
    with _exclusive(cfg):
        data = load(cfg)
        for c in data.get("crawlers", []):
            if c.get("crawler") == crawler:
                c["state"] = state
                c.update(extra)
        save(cfg, data)
        return data


# ----------------------------------------------------------- reconciliation
def branch_exists(cfg, branch):
    rc, _, _ = git(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % branch], cwd=cfg.root)
    return rc == 0


def commits_ahead(cfg, branch, base):
    """How many commits `branch` has that `base` does not."""
    if not branch_exists(cfg, branch):
        return 0
    rc, out, _ = git(["rev-list", "--count", "%s..%s" % (base, branch)], cwd=cfg.root)
    if rc != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


def is_merged(cfg, branch, base):
    """True when `base` already contains every commit on `branch`."""
    if not branch_exists(cfg, branch):
        return None
    rc, _, _ = git(["merge-base", "--is-ancestor", branch, base], cwd=cfg.root)
    return rc == 0


def is_empty(cfg, branch, base_sha):
    """Did this branch ever receive a commit?

    A branch with no commits of its own is not merged work — it is an **empty** branch,
    and it is exactly the shape a Crawler that died before its first commit leaves behind.
    Git cannot tell the two apart after the fact (a fully-merged branch and an empty one
    both have the base as their merge-base), so the base commit is recorded at spawn and
    compared against directly. Getting this wrong reads as "MERGED — safe to clean up"
    over a tree holding the only copy of unstaged work, which is the failure this whole
    module exists to prevent.

    Returns None when the answer is unknowable (no recorded base), so callers can say
    "unknown" rather than guess.
    """
    if not base_sha or not branch_exists(cfg, branch):
        return None
    rc, _, _ = git(["cat-file", "-e", "%s^{commit}" % base_sha], cwd=cfg.root)
    if rc != 0:
        return None
    return commits_ahead(cfg, base_sha, branch) == 0


def live(entry):
    """A Crawler is live only if its PID responds AND it was recorded this boot."""
    if entry.get("boot") and entry["boot"] != boot_token():
        return False
    return pid_alive(entry.get("pid"))


def reconcile(cfg, graph, base="HEAD"):
    """Answer the first question a resumed orchestrator has to ask.

    Returns a list of per-Crawler findings. Nothing here mutates anything: reconciliation
    reports, and `reap` acts.
    """
    data = load(cfg)
    findings = []
    for entry in data.get("crawlers", []):
        wt = cfg.abspath(entry.get("worktree"))
        scratch = cfg.abspath(entry.get("scratch"))
        f = {
            "crawler": entry.get("crawler"),
            "leaf": entry.get("leaf"),
            "branch": entry.get("branch"),
            "worktree": entry.get("worktree"),
            "scratch": entry.get("scratch"),
            "state": entry.get("state"),
            "alive": live(entry),
            "branch_exists": branch_exists(cfg, entry.get("branch") or ""),
            "worktree_exists": bool(wt and os.path.isdir(wt)),
            "scratch_files": [],
            "uncommitted": [],
        }
        f["merged"] = is_merged(cfg, entry.get("branch") or "", base)
        f["empty"] = is_empty(cfg, entry.get("branch") or "", entry.get("base_sha"))
        if f["worktree_exists"]:
            f["uncommitted"] = worktree.dirty(wt) or []
        if scratch and os.path.isdir(scratch):
            f["scratch_files"] = [x for x in sorted(os.listdir(scratch)) if x != "README.txt"]

        if f["worktree_exists"]:
            from . import harness
            f["harness"], f["harness_detail"] = harness.check_tree(cfg, wt)
        else:
            f["harness"], f["harness_detail"] = None, ""

        leaf = None
        try:
            leaf = graph.show(entry["leaf"]) if entry.get("leaf") else None
        except Exception:
            pass
        f["leaf_status"] = leaf.get("status") if leaf else "unknown"
        f["parked"] = bool(leaf.get("parked")) if leaf else False

        if f["harness"] == "rules-drifted":
            # Louder than LIVE: this tree's gate is answering a different question than the
            # orchestrator's, so anything it certifies means less than it appears to.
            f["verdict"] = ("RULES DRIFTED — this Crawler's harness no longer matches the "
                            "project's; its commit gate owes something else")
        elif f["harness"] == "undetermined":
            f["verdict"] = "HARNESS UNDETERMINED — cannot tell whether its rules match"
        elif f["alive"]:
            f["verdict"] = "LIVE — do not disturb"
        elif f["parked"]:
            f["verdict"] = "PARKED — paused at a usage limit, claim intentionally survives"
        elif f["empty"] and f["leaf_status"] not in ("closed", "refuted"):
            # Contained in base only because it never contributed anything.
            f["verdict"] = "ABANDONED — the branch never received a commit"
        elif f["merged"]:
            f["verdict"] = "MERGED — safe to clean up"
        elif f["leaf_status"] in ("closed", "refuted") and f["branch_exists"]:
            f["verdict"] = "DONE BUT NOT INTEGRATED — awaiting `showrunner integrate`"
        elif not f["branch_exists"] and not f["worktree_exists"]:
            f["verdict"] = "GONE — nothing on disk"
        else:
            f["verdict"] = "ABANDONED — owner is not alive and the work is not integrated"
        findings.append(f)
    return findings


# -------------------------------------------------------------------- reap
def reap(cfg, graph, base="HEAD", apply=False):
    """Reclaim claims and locks whose owners are dead. Loud, and never destructive.

    `apply=False` reports what it would do — the default, because reclaiming is a
    statement about somebody else's work.
    """
    actions, warnings = [], []

    # 1. Graph claims with no live owner.
    try:
        stale = graph.stale_claims()
    except Exception as exc:
        stale = []
        warnings.append(str(exc))
    for leaf, why in stale:
        actions.append({
            "kind": "claim",
            "leaf": leaf["id"],
            "actor": leaf.get("actor"),
            "why": why,
            "action": "release back to ready",
        })
        if apply:
            graph.release(leaf["id"], "reaped: %s" % why)

    # 2. Locks whose holder is dead. The lock already reclaims lazily on the next
    #    acquire; reaping makes the abandonment *visible* instead of silently absorbed.
    ls = locks.LockSet(cfg)
    for name in ls.names():
        state, holder = locks.Lock(ls.root, name).state()
        if state == locks.STALE:
            actions.append({
                "kind": "lock",
                "resource": name,
                "why": "holder pid %s (%s) is not alive" % (holder.get("pid"), holder.get("who")),
                "action": "release",
            })
            if apply:
                locks.Lock(ls.root, name).release(force=True)

    # 3. Worktrees and scratch dirs of dead Crawlers. Reported, never deleted: they may
    #    hold the only copy of real work.
    for f in reconcile(cfg, graph, base):
        if f["verdict"].startswith("ABANDONED"):
            detail = []
            if f["uncommitted"]:
                detail.append("%d uncommitted change(s) in %s" % (len(f["uncommitted"]), f["worktree"]))
            if f["scratch_files"]:
                detail.append("%d file(s) in scratch %s" % (len(f["scratch_files"]), f["scratch"]))
            actions.append({
                "kind": "crawler",
                "crawler": f["crawler"],
                "leaf": f["leaf"],
                "why": "owner not alive, work not integrated",
                "action": "SURFACED, not deleted" + (": " + "; ".join(detail) if detail else
                                                     " (no uncommitted work found)"),
            })
            if apply:
                set_state(cfg, f["crawler"], "abandoned", reaped_ts=now())
    return actions, warnings


def waiting(cfg, graph, base="HEAD"):
    """Is this orchestrator legitimately waiting on work it dispatched? (game_loop#32)

    An idle-watchdog whose only signal is transcript growth cannot see a subagent: an
    orchestrator that has fanned out and is waiting looks exactly like one that has stalled.
    Ringing it back to work is wrong, and at the ring cap it pages a human for a run that is
    healthy.

    The fix is not a better heuristic — it is a **recomputable fact**, the same rule this
    boundary follows everywhere else. Liveness here is a live PID or an explicit park, both
    of which the campaign record already carries, and neither of which an agent can assert
    into existence.

    Returns (is_waiting, detail). Deliberately conservative in the *opposite* direction to
    most of showrunner: when in doubt it reports NOT waiting, because a false "waiting"
    silences a watchdog that exists to catch a genuinely wedged run.
    """
    live, parked = [], []
    for f in reconcile(cfg, graph, base):
        if f["alive"]:
            live.append({"crawler": f["crawler"], "leaf": f["leaf"], "branch": f["branch"]})
        elif f["parked"]:
            parked.append({"crawler": f["crawler"], "leaf": f["leaf"],
                           "why": "parked at a usage limit — not dead, and its claim survives"})
    detail = {
        "waiting": bool(live or parked),
        "live_crawlers": live,
        "parked_crawlers": parked,
        "basis": "a live owning PID recorded at spawn, or an explicit park — never a guess "
                 "about activity",
    }
    return bool(live or parked), detail


# ------------------------------------------------------------- integration
def integrate(cfg, graph, base=None, only=None, dry_run=False):
    """Merge Crawler branches serially, re-running the owed checks after each merge.

    Stops on the first failure. Returns (results, ok).

    Exclusive across orchestrators. Integration mutates the ONE main checkout — it merges,
    runs checks, and on failure rewinds with `git reset --hard`. Two of these interleaved
    would rewind each other's work, and the second one's `reset` would discard a merge the
    first had already validated. This is the same "one consumer at a time" rule the device
    lane exists for, applied to the checkout itself; it refuses rather than queueing,
    because a silent multi-minute wait is indistinguishable from a hang.
    """
    cfg.require_valid()
    with try_file_lock(os.path.join(cfg.state_dir, "integrate")) as got:
        if not got:
            die("another integration is already running in this checkout. Integration merges, "
                "runs checks, and rewinds on failure — two at once would rewind each other's "
                "work. Wait for it to finish.", code=2)
        return _integrate_locked(cfg, graph, base, only, dry_run)


def _integrate_locked(cfg, graph, base=None, only=None, dry_run=False):
    rc, out, _ = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cfg.root)
    current = out.strip()
    base = base or current

    if current != base:
        die("integrate must run on the integration branch (%s); you are on %s" % (base, current),
            code=2)

    # Only TRACKED modifications matter here: integration rewinds a failed merge with
    # `git reset --hard`, which would destroy those. Untracked files survive it, and
    # refusing over them would block every real repo that keeps local scratch around.
    dirty_now = worktree.dirty(cfg.root, tracked_only=True)
    if dirty_now:
        die("the integration checkout has %d uncommitted change(s) to tracked files. "
            "Integration rewinds a failed merge with `git reset --hard`, which would destroy "
            "them. Commit or stash first." % len(dirty_now), code=2)

    baseline = gates.load_baseline(cfg)
    data = load(cfg)
    candidates = []
    for entry in data.get("crawlers", []):
        if only and entry.get("crawler") not in only and entry.get("leaf") not in only:
            continue
        branch = entry.get("branch")
        if not branch or not branch_exists(cfg, branch):
            continue
        if is_merged(cfg, branch, base):
            continue
        leaf_status = "unknown"
        try:
            leaf_status = graph.show(entry["leaf"])["status"]
        except Exception:
            pass
        if leaf_status not in ("closed", "refuted"):
            continue
        # Do not merge work certified by a gate that was answering a different question.
        wt = cfg.abspath(entry.get("worktree"))
        if wt and os.path.isdir(wt):
            from . import harness
            status, detail = harness.check_tree(cfg, wt)
            if status in ("rules-drifted", "undetermined"):
                entry = dict(entry)
                entry["_harness_block"] = status
                entry["_harness_detail"] = detail
        candidates.append(entry)

    # Deterministic order: oldest spawn first. Order has to be *owned* by something.
    candidates.sort(key=lambda e: (e.get("created_ts") or 0, e.get("crawler") or ""))

    results = []
    for entry in candidates:
        branch = entry["branch"]
        if entry.get("_harness_block"):
            results.append({
                "crawler": entry["crawler"], "branch": branch,
                "status": "harness-%s" % entry["_harness_block"],
                "report": [entry.get("_harness_detail") or "",
                           "Refusing to merge: this Crawler's tree no longer carries the "
                           "project's rules, so whatever its commit gate certified was a "
                           "different question. Restore its harness, or re-run the checks on "
                           "the merged result yourself and say so."]})
            return results, False
        rc, before, _ = git(["rev-parse", "HEAD"], cwd=cfg.root)
        before = before.strip()
        if dry_run:
            results.append({"crawler": entry["crawler"], "branch": branch, "status": "would-merge"})
            continue

        rc, out, err = git(["merge", "--no-ff", "--no-edit", branch], cwd=cfg.root)
        if rc != 0:
            git(["merge", "--abort"], cwd=cfg.root)
            results.append({"crawler": entry["crawler"], "branch": branch, "status": "conflict",
                            "detail": (err or out).strip()[:2000]})
            return results, False

        current_checks = gates.run_checks(cfg)
        ok, report = gates.compare_to_baseline(cfg, current_checks, baseline)
        if ok is False:
            git(["reset", "--hard", before], cwd=cfg.root)
            results.append({"crawler": entry["crawler"], "branch": branch,
                            "status": "checks-failed-on-merged-result", "report": report})
            return results, False

        # A branch-local proof cannot transfer: the harness scopes a proved fix to the
        # SESSION that proved it, deliberately, so a Crawler's proof can never satisfy the
        # integrator's handback. That is the right shape — branch-green is not trunk-green —
        # and it means the integrating session owes a proof against the MERGED artifact.
        # Write that artifact out so there is a real file to cite rather than a claim.
        proof_path = os.path.join(cfg.state_dir, "merged-proof-%s.txt" % branch.replace("/", "-"))
        try:
            os.makedirs(cfg.state_dir, exist_ok=True)
            with open(proof_path, "w") as fh:
                fh.write("merged %s into %s at %s\n\n" % (branch, base, now()))
                for c in current_checks.get("checks", []):
                    fh.write("check %s: rc=%s (%d failure line(s))\n"
                             % (c["name"], c["rc"], len(c["failures"])))
                fh.write("\n" + "\n".join(report) + "\n")
        except OSError:
            proof_path = None

        results.append({
            "crawler": entry["crawler"], "branch": branch, "status": "integrated",
            "merged_proof": proof_path,
            "report": report,
            "note": None if ok else "no baseline — merged without a no-new-failures comparison",
        })
        set_state(cfg, entry["crawler"], "integrated", integrated_ts=now())
        data = load(cfg)
        data.setdefault("integrated", []).append(
            {"crawler": entry["crawler"], "branch": branch, "ts": now()})
        save(cfg, data)
    return results, True
