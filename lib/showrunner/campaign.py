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
    # (cfg, BRANCH, BASE) — `commits_ahead` takes the branch first, and handing it these two the
    # other way round put a SHA where a branch name goes. `branch_exists` then looked for
    # refs/heads/<sha>, missed, and returned 0, so this read True for every branch ever spawned.
    # It survived because the only assertion over it covered the genuinely-empty branch, which is
    # the case a constant True gets right.
    return commits_ahead(cfg, branch, base_sha) == 0


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
        # What was DISPATCHED against what actually RAN. Imported here rather than at module
        # scope because dispatch imports campaign — the comparison lives with reconciliation,
        # which is where every other "recorded vs real" question in this file is answered.
        from . import dispatch as _dispatch
        f["model"] = _dispatch.model_finding(cfg, entry) if entry.get("session") else None
        # Beside `alive`, never instead of it: a PID that exists and a session that is working
        # are two different facts, and reporting only the first calls an errored Crawler
        # healthy. Observed doing exactly that.
        f["session_health"] = _dispatch.session_health(cfg, entry)
        if f["worktree_exists"]:
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
    for f in reconcile(cfg, graph, base):
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
        data.setdefault("integrated", []).append(
            {"crawler": entry["crawler"], "branch": branch, "ts": now()})
        save(cfg, data)
    return results, True
