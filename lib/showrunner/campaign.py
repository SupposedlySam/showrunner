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
import time

from .util import (atomic_write_json, boot_token, die, eprint, file_lock, git, now,
                   pid_alive, rel, run, same_boot, short_session, try_file_lock)
from . import events, gates, locks, worktree

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
    events.emit(cfg, "crawler.spawned", {"crawler": entry["crawler"], "leaf": entry.get("leaf"),
                "branch": entry.get("branch"), "worktree": entry.get("worktree"),
                "session": entry.get("session"), "pid": entry.get("pid")})
    return entry


def set_state(cfg, crawler, state, **extra):
    with _exclusive(cfg):
        data = load(cfg)
        was = None
        for c in data.get("crawlers", []):
            if c.get("crawler") == crawler:
                was = c.get("state")
                c["state"] = state
                c.update(extra)
        save(cfg, data)
        # THE one chokepoint for a Crawler's lifecycle: spawned -> running -> finished/retired/
        # abandoned all pass through here, so a viewer sees every transition without this module
        # having to remember to announce each one at its call site.
        events.emit(cfg, "crawler.state", {"crawler": crawler, "state": state, "was": was,
                    "why": extra.get("finished_why")})
        return data


# ----------------------------------------------------------- reconciliation
def existing_branches(cfg):
    """Every local branch name, in ONE git call. None when git could not be asked (#76).

    `branch_exists` spawns a `rev-parse` per branch, and `reconcile` asks it three times per
    Crawler — 544 subprocesses of a 869-subprocess, 20-second `waiting` on a real campaign, with
    essentially all the wall time spent waiting on processes. One `for-each-ref` answers all 544
    in 0.035s against 10.2s, measured by the reporter.

    THE CONSEQUENCE WAS NOT SLOWNESS. game_loop runs `waiting` as its watchdog probe under a
    hardcoded 15s timeout and reads a timeout as "the probe did not run at all" — which it
    reports as a broken watchdog and then STOPS SCHEDULING RE-CHECKS. Six days with no verdict
    logged, three Crawlers dead without committing in that window, each found by a human going
    to look. The watchdog was configured, armed, pointed at the documented command, and mute.

    None, never an empty set, when git fails. Empty means "this repo has no branches", which is
    a real and different answer, and collapsing them would make every branch read as missing —
    turning a failed read into a confident report that every Crawler's work had vanished.
    """
    rc, out, _ = git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], cwd=cfg.root)
    if rc != 0:
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


def branch_exists(cfg, branch, known=None):
    """`known` is a set from `existing_branches`, so a caller asking many times pays ONE call.

    Threaded explicitly rather than cached in a module global: a cache would need a lifetime,
    and the only honest lifetime is "this pass", which is exactly what a parameter says without
    anything to invalidate. A stale cache here reports a branch that exists as gone, which is
    the reading that makes a Crawler's work look lost.
    """
    if known is not None:
        return branch in known
    rc, _, _ = git(["rev-parse", "--verify", "--quiet", "refs/heads/%s" % branch], cwd=cfg.root)
    return rc == 0


def commits_ahead(cfg, branch, base, known=None):
    """How many commits `branch` has that `base` does not."""
    if not branch_exists(cfg, branch, known):
        return 0
    rc, out, _ = git(["rev-list", "--count", "%s..%s" % (base, branch)], cwd=cfg.root)
    if rc != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


def base_branch(cfg, base=None):
    """The ref every "is this merged?" question is asked against.

    A RESOLVER RATHER THAN A DEFAULT ARGUMENT, because the default was already there and was
    already unreachable. `reconcile` declared `base="HEAD"`; an explicit `None` walks straight
    past a default, and `integrate` handed it exactly that. `integrate` has no `--base` default
    ON PURPOSE — alone among the ten verbs that take one — because it compares the base to the
    CHECKED-OUT BRANCH NAME and dies when the two differ, so `default="HEAD"` would make every
    ordinary run refuse. It resolved `base or current` internally and then passed the RAW `None`
    to the reclaim pass afterwards.

    WHAT THAT COST, which is why this is not merely a crash fix: the failure landed in
    `git merge-base --is-ancestor <branch> None` AFTER the merge had already been committed, so
    every default `integrate` merged the work and then died before reclaiming a single tree. The
    trees accumulated silently, and `brief.py` went on telling each Crawler its worktree is
    deleted once the work integrates — the promise the whole scratch-dir discipline rests on,
    unkeepable on the only path anybody runs.
    """
    if base:
        return base
    _rc, out, _err = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cfg.root)
    # Detached HEAD abbreviates to "HEAD", which is the honest answer and the one the callers
    # already handled; an empty read means git could not be asked, and "HEAD" is still the
    # closest true statement about where we stand.
    return (out or "").strip() or "HEAD"


def is_merged(cfg, branch, base, known=None):
    """True when `base` already contains every commit on `branch`."""
    if not branch_exists(cfg, branch, known):
        return None
    rc, _, _ = git(["merge-base", "--is-ancestor", branch, base], cwd=cfg.root)
    return rc == 0


def head_in_no_ref(cfg, tree):
    """Is this tree's HEAD commit reachable from NO ref? True / False / None.

    THE ONE CASE THAT ACTUALLY LOSES COMMITTED WORK (#79). Refs live in the SHARED store, so
    removing a branch-backed worktree never removes its commits — which is what makes the whole
    reclaim lossless. A DETACHED tree is different: its HEAD is reachable only from
    `.git/worktrees/<name>/HEAD`, and once the tree is gone the commit is unreachable and dies at
    the next `git gc`.

    IT REACHES SHOWRUNNER'S OWN TREES, which I had assumed it could not. `spawn` always creates a
    branch, so I expected every recorded tree to be branch-backed and the case to be somebody
    else's problem. Measured instead: detach a recorded tree, commit in it, and `gc` reports it
    RECLAIMABLE — because `is_merged` asks about the recorded BRANCH, which is merged, and
    nothing looked at where the tree's HEAD actually was. `gc --apply` would have deleted it.

    None IS "CANNOT TELL" AND HOLDS THE TREE, per this module's rule that a failed read must
    never license a delete. The caller says so out loud rather than holding silently: if this
    query ever stops working, `gc` becomes a no-op again, and a no-op that explains itself is
    recoverable where a quiet one is the bug this file just finished fixing.
    """
    if not tree or not os.path.isdir(tree):
        return None
    rc, sha, _ = git(["rev-parse", "HEAD"], cwd=tree)
    sha = (sha or "").strip()
    if rc != 0 or not sha:
        return None
    # `for-each-ref --contains` answers with the refs that contain the commit; empty means none.
    # `branch --contains` was the obvious query and is the wrong one — in a detached tree it
    # prints a "(HEAD detached from ...)" pseudo-entry, so the orphan looks contained.
    rc, out, _ = git(["for-each-ref", "--contains", sha, "--count=1", "--format=%(refname)"],
                     cwd=tree)
    if rc != 0:
        return None
    return not (out or "").strip()


def content_in_base(cfg, branch, base, known=None):
    """Does `base` already contain every byte this branch changed? True / False / None.

    THE SQUASH-MERGE HOLE (#79). `is_merged` asks about ANCESTRY, and a squash-merge deliberately
    creates a new commit with no parent link to the branch — so a branch whose every byte is
    already in main reads as unmerged, forever. Reported from a checkout where `gc` reclaimed 0
    of 48 trees, some five months old, ~24 GB. In any repo that squash-merges, the merge test
    could never pass and `gc` was a no-op that printed a false reason.

    MEASURED against showrunner's own implementation before building, because the report named
    `git cherry` (patch-id matching) and showrunner uses `merge-base --is-ancestor`. Different
    mechanism, identical outcome: squash the branch, main holds every byte, `--is-ancestor`
    still answers no. The conclusion held even though the cause did not.

    None IS "CANNOT TELL" AND MUST NEVER LICENSE A REMOVAL. Every git failure, and the
    no-files-changed case, answers None rather than True — a branch that changed nothing is
    `is_empty`'s question, and answering True here would let this stand in for it.
    """
    if not branch or not branch_exists(cfg, branch, known):
        return None
    rc, mb, _ = git(["merge-base", base, branch], cwd=cfg.root)
    if rc != 0 or not (mb or "").strip():
        return None
    rc, out, _ = git(["diff", "--name-only", mb.strip(), branch], cwd=cfg.root)
    if rc != 0:
        return None
    files = [f for f in (out or "").splitlines() if f.strip()]
    if not files:
        return None
    # ONLY THE PATHS THE BRANCH TOUCHED. Comparing the whole trees would answer False for every
    # branch the moment base moved on independently, which is most of them.
    rc, _, _ = git(["diff", "--quiet", base, branch, "--"] + files, cwd=cfg.root)
    return rc == 0


def is_empty(cfg, branch, base_sha, known=None):
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
    if not base_sha or not branch_exists(cfg, branch, known):
        return None
    rc, _, _ = git(["cat-file", "-e", "%s^{commit}" % base_sha], cwd=cfg.root)
    if rc != 0:
        return None
    # (cfg, BRANCH, BASE) — `commits_ahead` takes the branch first, and handing it these two the
    # other way round put a SHA where a branch name goes. `branch_exists` then looked for
    # refs/heads/<sha>, missed, and returned 0, so this read True for every branch ever spawned.
    # It survived because the only assertion over it covered the genuinely-empty branch, which is
    # the case a constant True gets right.
    return commits_ahead(cfg, branch, base_sha, known) == 0


def lingering_crawlers(cfg):
    """Crawler processes still alive well after their leaf closed. Read-only. (#29)

    The DETECTION already existed and was reachable only through `reap` — a verb somebody has
    to decide to run. That is the whole defect: a lingering process is invisible by
    construction. `status` says closed, the campaign reads quiet, and nothing prompts anyone to
    look, so "remember to reap after every wave" is advice followed on the days you do not need
    it.

    What it cost, measured by the consumer who reported it: two sessions ran 3h47m and 4h56m
    past their own closes. A finished session does not idle — it keeps polling whatever it was
    told to poll — and that polling exhausted a shared rate limit. The resulting 429 landed on a
    turn-end gate, so the orchestrator could neither confirm nobody was waiting nor withdraw
    from the rooms to fix it. A finished agent nobody noticed took down the checking machinery
    for everyone still working.
    """
    from . import dispatch as _dispatch
    out = []
    for entry in load(cfg).get("crawlers", []):
        ling = _dispatch.lingering(entry)
        if ling:
            out.append({"crawler": entry.get("crawler"), "leaf": entry.get("leaf"),
                        "pid": entry.get("pid"), "why": ling})
    return out


def live(entry):
    """A Crawler is live only if its PID responds AND it was recorded this boot."""
    # THE SHARED COMPARISON, not a third copy of the rule. This site was named in #68's blast
    # radius and I fixed the other two while closing it — leaving the drifting-seconds defect
    # live in one of the three places the reporter listed. A raw `!=` here reads a one-second
    # NTP adjustment as proof of death, and would have read every pre-upgrade record as a
    # different boot the moment the token format changed.
    if entry.get("boot") and same_boot(entry["boot"], boot_token()) is False:
        return False
    return pid_alive(entry.get("pid"))


def tree_bytes(path):
    """Bytes under a tree, or None if it cannot be walked. None is NOT zero."""
    # `os.walk` DOES NOT RAISE ON A MISSING PATH — it yields nothing, so the naive version
    # returned 0 for a directory that was not there. That is this file's own identity-element
    # defect committed inside the function whose docstring warns about it: 0 reads as "nothing
    # to reclaim" and None reads as "could not measure", and the caller prints `?` for one and a
    # size for the other. Caught by the assertion written for exactly this, which failed first.
    if not os.path.isdir(path):
        return None
    failed = []
    total = 0
    for root, _dirs, files in os.walk(path, onerror=failed.append):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                # One unreadable FILE is not an unreadable tree; the walk carries on and the
                # total is a floor. A directory that could not be opened is different, and
                # `onerror` catches that below.
                continue
    return None if failed else total


def reclaimable(cfg, graph, base="HEAD"):
    """Worktrees that are provably redundant, and — separately — every one held back and why.

    NOTHING EVER REMOVED A TREE (#75). `spawn` makes one per leaf, `integrate` leaves it,
    `reap` handles claims and locks whose owners are DEAD, and there was no gc. One reported
    checkout carried 178 trees and 133 GB, of which one belonged to a live Crawler. The cost
    that actually hurt was not disk: an AV suite at ~64% CPU across four processes continuously
    rescanning a duplicated monorepo, plus Spotlight, on a machine reported as "running slow"
    while almost nothing was running.

    AND EVERY BRIEF PROMISED OTHERWISE. brief.py tells each Crawler "your tree is deleted once
    your work is integrated and everything inside it goes too" — the justification for the whole
    scratch-dir discipline, and it was not true. A false sentence in a brief is worse than a
    missing feature: it is the reason a Crawler puts its `--proof` somewhere else, so the rule
    survives on an argument that does not hold.

    THREE CONDITIONS, ALL REQUIRED, AND `unknown` IS NOT ONE OF THEM.
      merged  — the branch is in `base`, so every commit survives the tree's removal. This is
                what makes the removal lossless: `spawn` can recreate the tree from the branch.
      clean   — no uncommitted changes. `reconcile` answers clean/dirty/UNKNOWN, and unknown is
                a failed read, not an empty one. It must never license a delete: the identity
                element this repo keeps finding, with somebody's only copy of their work on the
                other side of it.
      not alive — a live session's tree is its workspace.

    Returns (reclaimable, held_back). BOTH, because a gc that lists only what it will remove
    invites the reading that everything else is gone already — and the held-back list is where
    a dirty tree carrying real work announces itself.
    """
    take, held = [], []
    for f in reconcile(cfg, graph, base):
        if not f.get("worktree_exists"):
            continue
        row = {"crawler": f.get("crawler"), "leaf": f.get("leaf"),
               "branch": f.get("branch"), "worktree": f.get("worktree"),
               "bytes": tree_bytes(cfg.abspath(f.get("worktree")))}
        orphan = head_in_no_ref(cfg, cfg.abspath(f.get("worktree")))
        if f.get("alive"):
            row["why"] = "its session is ALIVE — this is somebody's workspace right now"
        elif f.get("tree") == "unknown":
            row["why"] = ("git could not be read in it, so whether it holds uncommitted work is "
                          "UNKNOWN — which is not the same as clean, and the difference is "
                          "somebody's only copy")
        elif f.get("tree") == "dirty":
            row["why"] = ("%d uncommitted change(s) — SURFACED, never deleted"
                          % len(f.get("uncommitted") or []))
        elif orphan is not False:
            # BEFORE the merge question, because it outranks it: the branch being merged says
            # the BRANCH's commits are safe, and says nothing about a HEAD sitting somewhere
            # else. This is the only state in the list where removing the tree destroys
            # committed work rather than merely inconveniencing somebody.
            row["why"] = ("its HEAD is on no branch and no ref contains it, so this tree is the "
                          "only thing keeping that commit alive — removing it loses the work"
                          if orphan else
                          "whether any ref contains its HEAD could not be determined, and an "
                          "unreadable answer must not license a delete. If this persists, `gc` "
                          "is holding everything for a reason you can fix: check `git "
                          "for-each-ref --contains` works in that tree")
        elif not (f.get("merged") or f.get("content_in_base")):
            # THE WORDING WAS FALSE FOR EVERY BRANCH-BACKED TREE, and the reporter was right to
            # say so: refs live in the SHARED store, so removing a worktree never removes its
            # branch, and the tree is not "the only remaining copy" of anything committed. A
            # sentence asserted where it is provably untrue trains people to ignore it — and it
            # is the exact sentence that would have mattered on the one tree in their checkout
            # that really was irrecoverable, where it was never printed.
            row["why"] = ("its branch is not in %s and %s does not already carry the bytes it "
                          "changed, so the work is not integrated anywhere. The branch itself "
                          "survives removing the tree — refs are shared — but nothing has "
                          "picked this work up" % (base, base))
        else:
            # SAY WHICH ANSWER MADE IT REDUNDANT. A squash-merge is reclaimed on a different
            # fact from an ancestry merge, and an operator reading a deletion should be able to
            # tell them apart without re-deriving it.
            row["why"] = ("merged into %s" % base if f.get("merged") else
                          "%s already carries every byte this branch changed (squash-merge)"
                          % base)
            take.append(row)
            continue
        held.append(row)
    return take, held


def _shallow_tail(cfg, graph, entry, f, wt):
    """Leaf status, parked and blocked — the fields BOTH modes need (#76).

    Extracted rather than copied into the shallow path. Two computations of "is this Crawler
    blocked" are two statements of one policy, free to disagree, and this one decides whether a
    watchdog rings — the failure would be a `waiting` that says one thing and a `reconcile`
    that says another about the same live session.

    The blocked question costs a `stop_gate` call, but only for a Crawler that is ALIVE, so it
    is bounded by how many are running rather than by how many the campaign ever had. That is
    the distinction that makes the shallow mode worth having.
    """
    leaf = None
    try:
        leaf = graph.show(entry["leaf"]) if entry.get("leaf") else None
    except Exception:
        pass
    f["leaf_status"] = leaf.get("status") if leaf else "unknown"
    f["parked"] = bool(leaf.get("parked")) if leaf else False

    # BLOCKED IS NOT WORKING (issue #24). Asked only of a tree that is still alive, because
    # a refused turn-end matters exactly while the process is up: that is the state where
    # every other signal here reads healthy and the Crawler is doing nothing.
    f["blocked"], f["blocked_detail"] = (None, "")
    if f["alive"] and f["worktree_exists"]:
        from . import harness as _h
        f["blocked"], f["blocked_detail"] = _h.stop_gate(cfg, wt, entry.get("session"))
        # JOURNALLED AS A TRANSITION, not as a state. reconcile computes `blocked` fresh on
        # every call and a watchdog may call it every few seconds, so emitting the state
        # would give a viewer one identical line per poll — the signal drowning in its own
        # repetition. Only a CHANGE is an event.
        #
        # This makes a read verb write, which is worth naming: reconcile documents itself as
        # reporting rather than acting, and that still holds — an observation of a campaign
        # is not a mutation of it, and `waiting` already appends its own verdict log for the
        # same reason. What reconcile still never does is change a claim, a branch or a tree.
        if f["blocked"] is not None:
            kind = "crawler.blocked" if f["blocked"] else "crawler.unblocked"
            prev = events.latest(cfg, ("crawler.blocked", "crawler.unblocked"),
                                 "crawler", f["crawler"])
            if (prev or {}).get("kind") != kind:
                events.emit(cfg, kind, {"crawler": f["crawler"], "leaf": f["leaf"],
                                        "why": f["blocked_detail"] or None})
    return f


def _is_tracked(cfg, path):
    """True/False/None — does git carry this file? None means git could not be asked.

    Never a bare False on failure: "git does not track this" and "I could not find out" lead to
    opposite readings of whether the evidence survives a clone.
    """
    rc, out, _ = git(["ls-files", "--error-unmatch", path], cwd=cfg.root)
    if rc == 0:
        return True
    rc2, _, _ = git(["rev-parse", "--git-dir"], cwd=cfg.root)
    return False if rc2 == 0 else None


def reconcile(cfg, graph, base="HEAD", deep=True):
    """Answer the first question a resumed orchestrator has to ask.

    Returns a list of per-Crawler findings. Nothing here mutates anything: reconciliation
    reports, and `reap` acts.

    `deep=False` SKIPS the per-tree git work — dirty, merged, empty, harness, model,
    session_health, scratch listing — and is what `waiting` uses (#76). Those are questions
    about history and drift; `waiting` asks only whether anything is alive, parked or blocked,
    and it paid for the rest on every Crawler a campaign has ever had. On a real campaign that
    was 869 subprocesses and 20 seconds, against a consumer's hardcoded 15s probe timeout — and
    a timeout there is read as "the probe did not run", which stops the watchdog re-checking
    entirely. Six days mute, three Crawlers dead without committing inside that window.

    THE SKIPPED KEYS ARE ABSENT, NOT None. A caller that reads `merged` off a shallow finding
    gets a KeyError, which is loud and immediate; None would be indistinguishable from "not
    merged" and would quietly invert the answer. This file spends most of its comments on
    exactly that class of mistake, so the shallow mode must not introduce one.
    """
    data = load(cfg)
    # ONE ref read for the whole pass (#76). Three branch questions per Crawler, each its own
    # `rev-parse`, was 544 of the 869 subprocesses that made `waiting` take 20s and silently
    # disarm a consumer's watchdog. None means git could not be asked, and every callee falls
    # back to asking per-branch rather than treating "could not look" as "no branches".
    base = base_branch(cfg, base)
    known = existing_branches(cfg)
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
            "branch_exists": branch_exists(cfg, entry.get("branch") or "", known),
            "worktree_exists": bool(wt and os.path.isdir(wt)),
        }
        if not deep:
            # SHALLOW STOPS HERE. Everything below asks git about a tree or a branch's history,
            # which is what made `waiting` cost a subprocess fan-out proportional to every
            # Crawler the campaign ever had. The keys are left ABSENT on purpose — see the
            # docstring — so reading one is a KeyError rather than a quiet wrong answer.
            f["scratch_files"] = []
            findings.append(_shallow_tail(cfg, graph, entry, f, wt))
            continue
        f["scratch_files"] = []
        f["uncommitted"] = []
        f["merged"] = is_merged(cfg, entry.get("branch") or "", base, known)
        # SEPARATE FACT, NOT FOLDED INTO `merged`. "its commits are ancestors of base" and "base
        # already has its bytes" are different statements, and an operator deciding whether to
        # delete a tree deserves to be told which one is true. Deep mode only: this is two more
        # git calls per Crawler, and the shallow path exists because that fan-out once put
        # `waiting` past a consumer's probe timeout.
        f["content_in_base"] = (True if f["merged"] else
                                content_in_base(cfg, entry.get("branch") or "", base, known))
        f["empty"] = is_empty(cfg, entry.get("branch") or "", entry.get("base_sha"), known)
        # What was DISPATCHED against what actually RAN. Imported here rather than at module
        # scope because dispatch imports campaign — the comparison lives with reconciliation,
        # which is where every other "recorded vs real" question in this file is answered.
        from . import dispatch as _dispatch
        f["model"] = _dispatch.model_finding(cfg, entry) if entry.get("session") else None
        # Beside `alive`, never instead of it: a PID that exists and a session that is working
        # are two different facts, and reporting only the first calls an errored Crawler
        # healthy. Observed doing exactly that.
        f["session_health"] = _dispatch.session_health(cfg, entry)
        if f["worktree_exists"] and deep:
            # `dirty` returns None when git itself failed, and `or []` turned that into "no
            # uncommitted work" — a positive claim about somebody's tree derived from a read
            # that did not happen, and the one that decides whether a dead Crawler's tree is
            # garbage. Guarded by `worktree_exists` today, which makes it unlikely rather than
            # unreachable: a tree can exist and still be unreadable by git.
            found = worktree.dirty(wt)
            f["uncommitted"] = [] if found is None else found
            f["uncommitted_unknown"] = found is None
        # THE TREE IS EVIDENCE, AND UNKNOWN IS NOT CLEAN. Read once here because the verdict
        # ladder below asks it three times, and because "clean" is the only answer that licenses
        # the words "safe to clean up". A failed read must not collapse into it: `reap` already
        # refuses to print "no uncommitted work found" over a git that errored, and a verdict
        # derived from the same read owes the same restraint.
        f["tree"] = ("unknown" if f.get("uncommitted_unknown")
                     else "dirty" if f["uncommitted"] else "clean")
        if scratch and os.path.isdir(scratch):
            f["scratch_files"] = [x for x in sorted(os.listdir(scratch)) if x != "README.txt"]

        if f["worktree_exists"]:
            from . import harness
            f["harness"], f["harness_detail"], f["harness_mis_certified"] = \
                harness.check_tree(cfg, wt)
        else:
            f["harness"], f["harness_detail"], f["harness_mis_certified"] = None, "", False

        f = _shallow_tail(cfg, graph, entry, f, wt)
        if f["harness"] == "drifted" and (f["alive"] or f["blocked"]):
            # Louder than LIVE: this tree's gate is answering a different question than the
            # orchestrator's, so anything it certifies means less than it appears to.
            f["verdict"] = ("HARNESS DRIFTED (LIVE) — this Crawler's rules or its harness "
                            "scripts no longer match the project's; its commit gate owes "
                            "something else, and it is still working")
        elif f["harness"] == "drifted":
            # IDLE DRIFT IS INERT BY CONSTRUCTION (#66). The cost of drift is a refused commit
            # on finished work — which needs somebody still working in the tree. A closed leaf
            # with no live holder cannot pay it.
            #
            # Reported at the same severity, it trained the reader to skim: measured in one real
            # campaign, 48 trees carrying a harness, 42 drifted, ZERO live. Forty-two identical
            # lines is how the one that matters gets scrolled past — the same collapse this repo
            # refuses in the inert-Crawler gate, where an allow nobody is told about cannot be
            # told from a check that ran and was content.
            #
            # And the count moved the wrong way: re-provisioning the main checkout, which was
            # the correct action, raised drift from 26 to 42. A reader taking the number as
            # "how much is wrong" learned the opposite of the truth.
            f["verdict"] = ("harness drifted (idle) — no live holder, so its gate cannot refuse "
                            "anybody; re-provision on resume")
        elif f["harness"] == "undetermined":
            f["verdict"] = "HARNESS UNDETERMINED — cannot tell whether its rules match"
        elif f["blocked"]:
            # Ranked ABOVE live, because it is live — and that is the whole problem. Nothing
            # else in this report can tell it apart from a Crawler mid-thought.
            f["verdict"] = ("BLOCKED — alive but refused at a turn-end and nothing here can "
                            "prompt it; send it a message or it stays inert")
        elif f["alive"]:
            f["verdict"] = "LIVE — do not disturb"
        elif f["parked"]:
            f["verdict"] = "PARKED — paused at a usage limit, claim intentionally survives"
        elif f["empty"] and f["leaf_status"] not in ("closed", "refuted"):
            # Contained in base only because it never contributed anything.
            f["verdict"] = "ABANDONED — the branch never received a commit"
        elif f["empty"] and f["tree"] != "clean":
            # CLOSING A LEAF IS A CLAIM; THE BRANCH IS THE FACT (issue: reconcile-never-committed).
            #
            # The clause above deliberately excuses a closed leaf from the abandoned verdict, and
            # that exclusion used to hand it straight to `merged` — which answers True here for
            # the wrong reason. `is_merged` asks whether base contains every commit on the branch,
            # and a branch with NO commits satisfies that vacuously. So a Crawler that was refused
            # by its commit gate, closed its leaf anyway and exited with its work staged and
            # uncommitted was reported as "MERGED — safe to clean up" over the only copy of it.
            # Observed; the near-miss was caught by a human's standing rule about never deleting a
            # dirty worktree, not by this report. The uncommitted-changes line was printed, but
            # subordinate to a headline contradicting it, and a guard that depends on the reader
            # distrusting its own headline is not a guard.
            #
            # "No unique commits" and "merged" are different states with OPPOSITE remedies, and
            # this is the only one where the cheap action is irreversible — so it outranks
            # `merged`, and it never says safe. The leaf's status is what the Crawler asserted;
            # the branch is what actually happened, and the branch wins.
            #
            # The `in_progress` path above is untouched on purpose: that one is already correct
            # (it says "never received a commit", which is true), it is what `reap` keys on, and
            # a fix that made the common path noisier would be paid on every wave.
            f["verdict"] = (
                "NEVER COMMITTED — leaf %s, but the branch has no commits, so the %s in %s "
                "%s the only copy; commit from that tree or it is lost"
                % (f["leaf_status"],
                   "tree could NOT BE READ" if f["tree"] == "unknown"
                   else "%d uncommitted change(s)" % len(f["uncommitted"]),
                   f["worktree"],
                   "may be" if f["tree"] == "unknown" else "are"))
        elif f["empty"]:
            # THE AMBIGUOUS CORNER, decided rather than inherited: closed, nothing committed, and
            # the tree is clean. This is the shape of a legitimately REFUTED premise — a leaf that
            # correctly declined to build something produces no commit and leaves no tree — so it
            # is not an alarm and does not get one. But it is not `merged` either: nothing was
            # ever merged, and saying so is a false factual claim in the one report a reader uses
            # to decide what to delete. It says what is true of both halves instead — there is
            # nothing at risk, and there is nothing waiting to be integrated.
            f["verdict"] = ("NOTHING TO INTEGRATE — leaf %s and the branch never received a "
                            "commit; the tree is clean, so nothing is at risk here" % f["leaf_status"])
        elif f["merged"] and f["tree"] != "clean":
            # Merged is a fact about COMMITS, and "safe to clean up" is a claim about the whole
            # tree. They come apart exactly here: every commit on this branch really is in base,
            # and the worktree still holds changes that are not in anything. Deleting it on the
            # strength of the first fact destroys work the first fact says nothing about.
            f["verdict"] = (
                "MERGED, BUT THE TREE IS NOT CLEAN — its commits are in the base and %s; "
                "resolve that before deleting anything"
                % ("%s could NOT BE READ, so whether more is there is UNKNOWN" % f["worktree"]
                   if f["tree"] == "unknown"
                   else "%d uncommitted change(s) in %s are in nothing"
                        % (len(f["uncommitted"]), f["worktree"])))
        elif f["merged"]:
            f["verdict"] = "MERGED — safe to clean up"
        elif f["leaf_status"] in ("closed", "refuted") and f["branch_exists"]:
            f["verdict"] = "DONE BUT NOT INTEGRATED — awaiting `showrunner integrate`"
        elif not f["branch_exists"] and not f["worktree_exists"]:
            f["verdict"] = "GONE — nothing on disk"
        elif f["leaf_status"] in ("closed", "refuted"):
            # DONE IS NOT ABANDONED. The clause below says "the work is not integrated", which
            # is simply false once the leaf closed — the close gate demanded a real artifact to
            # get here, and a refuted premise is a successful outcome. Observed: a Crawler that
            # finished, closed its leaf and was cleanly retired came back labelled ABANDONED,
            # and `reap` then set its state to match. Under fan-out every completed Crawler
            # would report that way, which trains a reader to skim past abandonment notices —
            # the one report that must never become routine.
            f["verdict"] = "RETIRED — leaf %s and the Crawler is no longer running" % f["leaf_status"]
        else:
            f["verdict"] = "ABANDONED — owner is not alive and the work is not integrated"
        findings.append(f)
    return findings



def finish(cfg, leaf_id, why="leaf closed"):
    """Mark every Crawler on this leaf finished, and close its room. Called when a leaf closes.

    The SAFE half of spin-down, and it runs immediately because nothing here can damage work:
    the room is a convenience and the leaf is already closed. The process is deliberately NOT
    touched — a Crawler closes its own leaf from inside its own session, so at this instant it
    is mid-call, and killing it would truncate the very work it just certified. `reap` handles
    a process that is still alive well after this, which is a different and later question.
    """
    from . import dispatch as _dispatch
    done = []
    for entry in load(cfg).get("crawlers", []):
        if entry.get("leaf") != leaf_id or entry.get("state") in ("finished", "retired"):
            continue
        state, detail = _dispatch.close_channel(cfg, entry)
        # ONLY AN OBSERVED CLOSURE IS RECORDED. UNKNOWN leaves channel_closed False so the room
        # is retried, rather than writing down a closure nobody watched happen — a rate-limited
        # close that records success leaves a room open with the record saying otherwise, and
        # nothing ever looks again.
        set_state(cfg, entry["crawler"], "finished", finished_at=now(), finished_why=why,
                  channel_closed=(state == _dispatch.CLOSE_DONE))
        done.append({"crawler": entry["crawler"], "channel": detail,
                     "channel_closed": state == _dispatch.CLOSE_DONE,
                     "channel_state": state})
    return done


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

    # 1b. Claims whose owner is ALIVE and has stopped producing (#69). SURFACED, NEVER
    #     RELEASED -- the one verdict here that reap deliberately walks past, even with
    #     --apply. A stalled Crawler still owns its process, and that process still holds the
    #     only copy of whatever it has not committed: in the filing incident the worktree held
    #     four uncommitted files and an already-green 6473-test suite, and releasing the claim
    #     would have destroyed both. The correct recovery is to unstick the agent and leave the
    #     claim where it is, which is what this line tells the reader to do.
    #
    #     Note this is the OPPOSITE failure to the one above. A stale claim is reported because
    #     its owner cannot be running; a stalled claim is reported because its owner IS running
    #     and that is exactly what makes it invisible. Neither check can see the other's case.
    try:
        stalled = graph.stalled_claims()
    except Exception as exc:                                            # noqa: BLE001
        stalled = []
        warnings.append(str(exc))
    for leaf, why in stalled:
        actions.append({
            "kind": "stalled",
            "leaf": leaf["id"],
            "actor": leaf.get("actor"),
            "why": why,
            "action": "SURFACED, not released — and --apply will not touch it either. Its "
                      "process is alive and may hold uncommitted work, so prompt %s to see "
                      "whether it is wedged; reclaim only after its tree is safe."
                      % (leaf.get("actor") or "its owner"),
        })

    # 2. Locks whose holder is dead. The lock already reclaims lazily on the next
    #    acquire; reaping makes the abandonment *visible* instead of silently absorbed.
    # CONFIGURED RESOURCES **AND** WORKTREE LEASES. This iterated `ls.names()`, which is the
    # configured resources only — and a lease is named `worktree:<tree>`, which is not one and
    # never will be. So the reaper, whose stated job two lines up is to make abandonment
    # VISIBLE rather than silently absorbed, could not see the locks a dead Crawler is most
    # likely to leave behind: it holds a lease for its whole life. A lease outliving its session
    # self-heals on the next `worktree enter` (it goes STALE and is reclaimed), which is exactly
    # the "silently absorbed" this loop exists to end, one lock namespace over.
    ls = locks.LockSet(cfg)
    for name in sorted(set(ls.names()) | set(ls.on_disk())):
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
                # RECLAIM IS NOT RELEASE. A holder letting go and a reaper taking a resource
                # off a dead one are different facts, and only the second says something went
                # wrong. Collapsing them into one event would hand a viewer a lock that looks
                # tidily handed back by an agent that never came home.
                events.emit(cfg, "lock.reclaimed", {"resource": name,
                                                    "dead_pid": holder.get("pid"),
                                                    "was_held_by": holder.get("who")})
        elif state == locks.UNREADABLE:
            # Surfaced and never released, even with --apply. This is the one lock state
            # nobody can adjudicate from here: an unreadable pid may belong to a process
            # still holding the resource, and `--apply` meaning "reclaim whatever looks
            # broken" is how a mutex becomes a suggestion.
            actions.append({
                "kind": "lock",
                "resource": name,
                "why": "holder pid is UNREADABLE (%r) — cannot be proved dead"
                       % ((holder.get("pid") or "")[:40]),
                "action": "SURFACED, not released — check whether %s is still running, then "
                          "`showrunner lock release %s --force`"
                          % (holder.get("who") or "the holder", name),
            })

    # 2b. Processes that outlived their work. A Crawler whose leaf is FINISHED has, by
    #     definition, nothing left to lose — but the instant of closing is the instant it is
    #     busiest, so only a process still alive well after the grace window counts. Sent
    #     SIGTERM, never SIGKILL: a Crawler that ignores a term is a finding, not something
    #     to escalate against silently. Left stacking, these are what fills a machine under
    #     repeated fan-out.
    from . import dispatch as _dispatch
    import signal as _signal
    for entry in load(cfg).get("crawlers", []):
        ling = _dispatch.lingering(entry)
        if not ling:
            continue
        actions.append({
            "kind": "process",
            "crawler": entry.get("crawler"),
            "leaf": entry.get("leaf"),
            "why": "finished %ds ago and pid %s is still alive"
                   % (ling["seconds_since_finished"], ling["pid"]),
            "action": "SIGTERM" if apply else "would SIGTERM",
        })
        if apply:
            try:
                os.kill(ling["pid"], _signal.SIGTERM)
            except OSError as exc:
                warnings.append("could not terminate pid %s: %s" % (ling["pid"], exc))
                continue
            # PROVE IT ACTED. `os.kill` returning without error means the SIGNAL WAS
            # DELIVERED, not that the process stopped — two different facts, and recording
            # "retired" off the first is the effector reporting a result it never observed.
            # A process is free to ignore SIGTERM, and this module's own comment says one
            # that does is a FINDING rather than something to escalate against. So: watch
            # for the exit, briefly, and say which of the two actually happened.
            for _ in range(20):
                if not pid_alive(ling["pid"]):
                    break
                time.sleep(0.1)
            if pid_alive(ling["pid"]):
                # State stays `finished`, so `lingering` reports it again next run rather
                # than a record that claims a retirement nobody witnessed.
                set_state(cfg, entry["crawler"], "finished",
                          terminate_sent_at=now())
                warnings.append(
                    "pid %s was sent SIGTERM and is still alive — it is ignoring the signal. "
                    "NOT escalating to SIGKILL: a Crawler that declines to stop is something "
                    "to look at, not something to kill quietly." % ling["pid"])
            else:
                set_state(cfg, entry["crawler"], "retired", retired_at=now())

    # 2c. Rooms belonging to Crawlers that are done. A channel per Crawler is right while it
    #     works and a leak once it stops; closing on `close` covers the normal path, and this
    #     covers the Crawler that died without ever closing its leaf.
    for entry in load(cfg).get("crawlers", []):
        if entry.get("channel_indeterminate") and not entry.get("channel_closed"):
            # NOT AUTO-RETRIED (#61). Code 4 means nobody can say what landed, and `open` is two
            # writes — a throttle between them leaves a room half-created, so blindly closing
            # again is how a topic and briefing get silently discarded. Reported every run so it
            # cannot be forgotten, and left for a human, which is the difference between this
            # and UNKNOWN.
            actions.append({
                "kind": "room",
                "crawler": entry.get("crawler"),
                "why": "a previous close came back INDETERMINATE — what landed is unknown",
                "action": "NEEDS A HUMAN: inspect %s before closing it again. Retrying blind "
                          "can discard a half-written room's topic and briefing."
                          % entry["channel"],
            })
            continue
        if entry.get("channel") and not entry.get("channel_closed") and not live(entry):
            # THE ACTION LINE IS WRITTEN AFTER THE ATTEMPT, not before it. It used to say
            # "close <room>" unconditionally, printed BELOW the warning about the failure — so
            # the confident-sounding line was the second one a reader saw, and a close that did
            # not happen printed a line that reads exactly like one that did.
            act = {
                "kind": "room",
                "crawler": entry.get("crawler"),
                "why": "owner not alive and its room is still open",
                "action": "would close %s" % entry["channel"],
            }
            actions.append(act)
            if apply:
                state, detail = _dispatch.close_channel(cfg, entry)
                set_state(cfg, entry["crawler"], entry.get("state") or "abandoned",
                          channel_closed=(state == _dispatch.CLOSE_DONE),
                          channel_indeterminate=(state == _dispatch.CLOSE_INDETERMINATE))
                if state != _dispatch.CLOSE_DONE:
                    # UNKNOWN and FAILED both warn, and the detail says which. They differ in
                    # what a reader should DO — retry versus investigate — so they must not
                    # arrive as the same sentence.
                    warnings.append({
                        _dispatch.CLOSE_UNKNOWN: "could not tell: ",
                        _dispatch.CLOSE_INDETERMINATE: "INDETERMINATE, not retried: ",
                    }.get(state, "") + detail)
                act["action"] = {
                    _dispatch.CLOSE_DONE: "closed %s" % entry["channel"],
                    _dispatch.CLOSE_UNKNOWN: "COULD NOT TELL whether %s closed — it is still "
                                             "recorded as open, and the next reap tries again"
                                             % entry["channel"],
                    _dispatch.CLOSE_INDETERMINATE:
                        "INDETERMINATE for %s — not retried automatically, because what landed "
                        "is unknown and a blind retry can discard a half-written room"
                        % entry["channel"],
                }.get(state, "FAILED to close %s — see the warning above" % entry["channel"])

    # 3. Worktrees and scratch dirs of dead Crawlers. Reported, never deleted: they may
    #    hold the only copy of real work.
    for f in reconcile(cfg, graph, base):
        # NEVER COMMITTED BELONGS IN THIS BLOCK, whatever the leaf says. The filter used to key
        # on ABANDONED alone, which reads as "whose owner is gone" — but the property this block
        # is about is "this tree may hold the only copy of real work", and a leaf closed over an
        # uncommitted tree has exactly that property while claiming the opposite. Before the
        # verdict existed such a Crawler reported MERGED and was skipped here too, so the tree
        # holding the work appeared in no line `reap` printed.
        never = f["verdict"].startswith("NEVER COMMITTED")
        if f["verdict"].startswith("ABANDONED") or never:
            detail = []
            if f.get("uncommitted_unknown"):
                # READ BACK WHERE IT MATTERS, not written and forgotten: this line is the one
                # that tells a human whether a tree about to be cleaned up holds the only copy
                # of real work, and "(no uncommitted work found)" over a failed git read is the
                # confident-and-wrong version of that sentence.
                detail.append("COULD NOT READ %s — git failed, so whether it holds uncommitted "
                              "work is UNKNOWN, not none" % f["worktree"])
            elif f["uncommitted"]:
                detail.append("%d uncommitted change(s) in %s" % (len(f["uncommitted"]), f["worktree"]))
            if f["scratch_files"]:
                detail.append("%d file(s) in scratch %s" % (len(f["scratch_files"]), f["scratch"]))
            actions.append({
                "kind": "crawler",
                "crawler": f["crawler"],
                "leaf": f["leaf"],
                "why": ("leaf %s, but the branch never received a commit" % f["leaf_status"])
                       if never else "owner not alive, work not integrated",
                "action": "SURFACED, not deleted" + (": " + "; ".join(detail) if detail else
                                                     " (no uncommitted work found)"),
            })
            if apply:
                # NOT relabelled `abandoned` when the leaf closed: the Crawler did stop, and
                # overwriting its own recorded state with a word that contradicts its leaf is how
                # the next reader loses the one fact that matters here — that a close was claimed
                # over work that never landed.
                if not never:
                    set_state(cfg, f["crawler"], "abandoned", reaped_ts=now())
    return actions, warnings


def work_since_block(cfg, crawler, branch, worktree):
    """Has this Crawler worked SINCE showrunner recorded it blocked? (issue #54)

    `harness.stop_gate` says outright that `blocked` alone is a fact about the past: it reports
    that a turn-end was refused and when, and cannot report that nothing has happened since.
    The gate consumed `blocked` as a present-tense fact anyway. This supplies the corroborating
    signal that docstring names, from the one place a Crawler's activity is attributable -- its
    OWN worktree, which is the same jurisdiction argument the lease already rests on.

    The failure that produced this: the chat bus went down, a Crawler went inert because its
    only reporting channel was gone, and the gate then refused the ORCHESTRATOR's turn-end and
    offered two remedies -- message it (needs the dead bus) or reap it (releases the claim and
    surfaces a tree holding live uncommitted work). After an out-of-band wake it kept refusing
    while the Crawler was demonstrably working. A gate whose only remedies are "use the broken
    thing" or "destroy the work" is one a later orchestrator learns to route around.

    Returns (bool, why). ONLY EVER RELEASES. Every unknown -- no recorded block, an unreadable
    tree, a missing branch, a clock that disagrees -- returns False, which is today's behaviour
    exactly. This must never become the one place in showrunner where a failure to read
    something makes a gate stricter.
    """
    ev = events.latest(cfg, ("crawler.blocked",), "crawler", crawler)
    since = (ev or {}).get("ts")
    if not isinstance(since, int):
        return False, ""                    # never recorded blocked here: not our call to make

    if branch:
        rc, out, _ = run(["git", "log", "-1", "--format=%ct", branch], cwd=cfg.root)
        if rc == 0 and (out or "").strip().isdigit() and int(out.strip()) > since:
            return True, "committed on %s after the block was recorded" % branch

    tree = worktree if os.path.isabs(worktree or "") else os.path.join(cfg.root, worktree or "")
    if not os.path.isdir(tree):
        return False, ""
    # TRACKED files only. An untracked build artefact, a log the harness writes, or an editor
    # swapfile would all report "work" without anybody having done any -- and this signal
    # releases a gate, so a false positive here is the expensive direction.
    rc, out, _ = run(["git", "ls-files", "-z"], cwd=tree)
    if rc != 0:
        return False, ""
    newest = 0
    for rel in (out or "").split("\0"):
        if not rel:
            continue
        try:
            newest = max(newest, int(os.path.getmtime(os.path.join(tree, rel))))
        except OSError:
            continue                        # a file that vanished mid-scan is not evidence
    if newest > since:
        return True, "a tracked file in its worktree changed after the block was recorded"
    return False, ""


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
    live, parked, blocked = [], [], []
    # SHALLOW (#76). `waiting` reads alive, parked, blocked and the ids; it never touches
    # merged, empty, uncommitted, tree, harness, model or session_health. It was paying for all
    # of them on every Crawler the campaign had ever recorded, which is what put it past the 15s
    # probe timeout a consumer runs it under — and a timeout there reads as "the probe did not
    # run at all", which stops the watchdog scheduling any further re-check.
    for f in reconcile(cfg, graph, base, deep=False):
        worked, why_worked = (False, "")
        if f["blocked"]:
            worked, why_worked = work_since_block(cfg, f["crawler"], f.get("branch"),
                                                  f.get("worktree") or "")
        if f["parked"]:
            # PARKED IS CHECKED FIRST, and that ordering is the whole bug (#62). It used to sit
            # after `blocked`, so a Crawler that was parked AND refused at a turn-end never
            # reached this branch — it was reported as blocked, and the inert-Crawler gate
            # refused turn-ends over work somebody had already accounted for. llms.txt claimed
            # "a parked leaf ... does not block `stop-gate`", which was true of that gate and
            # read as a general property of parking. It was not one.
            #
            # An accounted-for Crawler is not an abandoned one; that distinction is the entire
            # reason `park` exists. But it is still REPORTED, and says when it is also inert, so
            # the owner learns their run is stalled — the gate's purpose was never the refusal,
            # it was the noticing.
            parked.append({"crawler": f["crawler"], "leaf": f["leaf"],
                           "why": ("parked, AND refused at a turn-end — accounted for, so it "
                                   "blocks nobody, but it is doing nothing and only its owner "
                                   "can restart it")
                           if f["blocked"] else
                           "parked at a usage limit — not dead, and its claim survives"})
        elif f["blocked"] and not worked:
            # NOT WAITING. This orchestrator is not waiting on work it cannot hurry — it is
            # sitting next to a session that stopped and can only be restarted from outside.
            # Counting it as legitimate waiting is a false "waiting", which silences the
            # watchdog on the one run that needs it, and this verb exists to prevent exactly
            # that. Reported separately rather than dropped: the Crawler is real, it is alive,
            # and somebody has to go and prompt it.
            # WHOSE LEAF IT IS, carried in the report. The gate that consumes this fires in
            # whichever session is nearest, which in a multi-campaign checkout is routinely not
            # the owner — so it told a stranger to message a Crawler they never briefed and
            # offered them a reap that would have destroyed work they had no context on. The
            # blocked session does not need the controls; it needs to know whose leaf this is
            # and who to tell. `show` already holds both, so nothing new is computed.
            _who = {}
            try:
                _leaf = graph.show(f["leaf"]) if f.get("leaf") else None
                if _leaf:
                    _who = {"actor": _leaf.get("actor"),
                            "claim_session": short_session(_leaf.get("claim_session"))}
            except Exception:                                   # noqa: BLE001
                _who = {}
            blocked.append(dict({"crawler": f["crawler"], "leaf": f["leaf"],
                                 "why": f["blocked_detail"]}, **_who))
        elif f["blocked"] and worked:
            # Blocked report, and the TREE disagrees. It is working without a phone line --
            # which is legitimate waiting, and the case that produced this issue.
            live.append({"crawler": f["crawler"], "leaf": f["leaf"], "branch": f["branch"],
                         "was_blocked": True, "evidence": why_worked})
        elif f["alive"]:
            live.append({"crawler": f["crawler"], "leaf": f["leaf"], "branch": f["branch"]})

    detail = {
        "waiting": bool(live or parked),
        "live_crawlers": live,
        "parked_crawlers": parked,
        "blocked_crawlers": blocked,
        "basis": "a live owning PID recorded at spawn, or an explicit park — never a guess "
                 "about activity. A Crawler refused at a turn-end is counted in neither: it "
                 "is alive and doing nothing, and calling that waiting silences the watchdog "
                 "on the run that needs it most",
    }
    # Log every verdict. Whether this ever silences a watchdog, and for how long, has to be
    # a FACT rather than a hunch — otherwise the first time someone argues the ring cap is
    # too aggressive there is no evidence either way. It is also the evidence a consumer
    # needs before adopting this at all: a gate wants a logged, observed failure behind it,
    # and this is where that record accumulates.
    try:
        path = os.path.join(cfg.state_dir, "waiting.jsonl")
        os.makedirs(cfg.state_dir, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({
                "ts": now(),
                "waiting": detail["waiting"],
                "live": len(live),
                "parked": len(parked),
                "blocked": len(blocked),
                "leaves": [c["leaf"] for c in live + parked],
                "blocked_leaves": [c["leaf"] for c in blocked],
            }, sort_keys=True) + "\n")
        detail["journal"] = "written"
    except OSError as e:
        # SILENT BECAUSE THE READER CANNOT ACT IS STILL SILENT. This runs from a probe under a
        # watchdog, which can do nothing about a read-only state dir -- so swallowing was right
        # for that reader and wrong for everybody. The comment above calls this record "the
        # evidence a consumer needs before adopting this at all", and a write that stops
        # silently makes "the gate never fired" and "the journal could not be written" the same
        # reading, with the second one arguing FOR adoption.
        #
        # So it goes on the channel that reaches somebody who CAN act: the porcelain a human or
        # a doctor reads, rather than a stderr line the probe discards.
        detail["journal"] = "FAILED: %s" % e
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
    base = base_branch(cfg, base)

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
            status, detail, mis_certified = harness.check_tree(cfg, wt)
            # COMPARED AGAINST THE PERMISSIVE ANSWER, NOT THE RESTRICTIVE ONE. This listed
            # the verdicts that BLOCK, so anything else merged — including a verdict the
            # harness adds later, and including None, which meant both "no harness here"
            # (legitimate) and "the harness answered something we do not recognise" until
            # those were split. game_loop shipped a new verdict mid-session; the only reason
            # it was safe is that it reused an exit code already mapped.
            #
            # Inverted: merging requires an exact confident match, so every value added on
            # either side of this boundary from now on fails CLOSED by construction. `None`
            # is in the allow-set deliberately and narrowly — a repo with no harness
            # configured is a supported shape, and refusing it would break every consumer
            # that never had one.
            if status not in (None, "clean", "notes-drifted"):
                entry = dict(entry)
                entry["_harness_block"] = status
                entry["_harness_detail"] = detail
                if mis_certified:
                    # The gate that is refusing this branch today is the one that would have
                    # WAVED IT THROUGH before game_loop #66. Said here rather than only in the
                    # doctor, because this is the moment somebody is deciding about this branch.
                    entry["_harness_detail"] += (
                        "\nA harness before game_loop #66 reported this tree as clean, exit 0. "
                        "If this branch was ever integrated, check what that merge certified.")
        candidates.append(entry)

    # Deterministic order: oldest spawn first. Order has to be *owned* by something.
    candidates.sort(key=lambda e: (e.get("created_ts") or 0, e.get("crawler") or ""))

    results = []

    def record(outcome):
        """Append an outcome AND journal it. One path on purpose.

        integrate has five ways to finish — refused for a drifted harness, would-merge,
        conflict, checks-failed-on-the-merged-result, integrated — and each was an
        independent `results.append`. Emitting beside each one means a sixth arrives without
        an event and a viewer silently stops seeing the riskiest verb finish. Routing every
        outcome through here makes forgetting impossible rather than unlikely, which is the
        difference between a rail and a habit.
        """
        results.append(outcome)
        events.emit(cfg, "integrate.%s" % outcome["status"],
                    {"crawler": outcome.get("crawler"), "branch": outcome.get("branch"),
                     "base": base, "dry_run": bool(dry_run),
                     # The report can be long and is already on disk for the case that matters;
                     # a journal line is a summary, not a document.
                     "detail": (outcome.get("detail") or "")[:200] or None,
                     "merged_proof": outcome.get("merged_proof")})

    for entry in candidates:
        branch = entry["branch"]
        if entry.get("_harness_block"):
            record({
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
            record({"crawler": entry["crawler"], "branch": branch, "status": "would-merge"})
            continue

        rc, out, err = git(["merge", "--no-ff", "--no-edit", branch], cwd=cfg.root)
        if rc != 0:
            git(["merge", "--abort"], cwd=cfg.root)
            record({"crawler": entry["crawler"], "branch": branch, "status": "conflict",
                            "detail": (err or out).strip()[:2000]})
            return results, False

        current_checks = gates.run_checks(cfg)
        ok, report = gates.compare_to_baseline(cfg, current_checks, baseline)
        if ok is False:
            git(["reset", "--hard", before], cwd=cfg.root)
            record({"crawler": entry["crawler"], "branch": branch,
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

        record({
            "crawler": entry["crawler"], "branch": branch, "status": "integrated",
            "merged_proof": proof_path,
            "report": report,
            "note": None if ok else "no baseline — merged without a no-new-failures comparison",
        })
        set_state(cfg, entry["crawler"], "integrated", integrated_ts=now())
        data = load(cfg)
        # THE RECORD MUST NAME ITS EVIDENCE. This was {crawler, branch, ts} — the durable half
        # of an integration, the thing a reviewer reads months later to answer "was this leaf
        # actually proved, or did a Crawler assert it was?" — and it carried no reference to the
        # artifact that answers it. The path was known right here and dropped.
        #
        # RECONSTRUCTING IT IS NOT OBVIOUS, which is what makes the omission expensive rather
        # than untidy. The filename derives from the BRANCH, not the crawler, and is truncated;
        # a consumer measuring the correspondence got two plausible wrong numbers before a right
        # one. A convention the reader has to rediscover is not a link.
        #
        # AND IT SAYS WHETHER THE EVIDENCE WILL TRAVEL. A consumer may not carry these, and then
        # on another machine the record is present and the proof is not — while the record still
        # reads as a completed, proved leaf. That is the silent direction, and it is the whole
        # reason for the field. `proof_tracked` is False when git does not carry it, None when
        # git could not be asked, because "not tracked" and "could not look" are different
        # answers.
        #
        # THIS COMMENT USED TO SAY "reasonably, they are large and local", which was the
        # reporter's own gloss and which they then MEASURED and retracted: 69 proofs totalled
        # 15 KB, the largest was 247 bytes, and one ordinary source file in the same repo was
        # three times the whole set. "Large" had come from the count, never from the bytes. They
        # dropped their ignore rule and tracked all 69.
        #
        # Kept as a correction rather than deleted, because the field is right for a reason the
        # wrong gloss obscured: showrunner cannot know what a consumer will carry, so the record
        # has to be honest about absence. Where a consumer measures and finds it costs nothing,
        # PRESENT beats honest-about-absent — this is the floor, not the ceiling.
        data.setdefault("integrated", []).append(
            {"crawler": entry["crawler"], "branch": branch, "ts": now(),
             "merged_proof": rel(proof_path, cfg.root) if proof_path else None,
             "proof_tracked": _is_tracked(cfg, proof_path) if proof_path else None})
        save(cfg, data)
    return results, True
