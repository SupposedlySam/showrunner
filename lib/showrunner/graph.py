"""The work-graph: one internal interface, two backends.

Decision (issue #2): **vendor a minimal graph over Python's built-in sqlite3, and keep a
`br` adapter.** The layer below installs in one line with no packages; asking a user to
install a Rust toolchain and a separate tracker before the *first* command is where
adoption stops. Detecting `br` and deferring to it when present means nothing already
proven is thrown away.

The rest of showrunner talks only to `Graph` and never learns which backend it got.

Two things the vendored backend does that a plain tracker does not, both because an
orchestrator needs them (issues #7 and #12):

* **A claim carries liveness.** `in_progress` with no live owner is how work silently
  leaves the queue: `ready` means unblocked *and unclaimed*, so a dead Crawler's leaf is
  never handed out again, `ready` goes dry, and the run terminates reporting success on
  work nobody did.
* **`refuted` is a terminal state distinct from `done` and `failed`.** A run that
  correctly declines to build something has produced real value; if the only shapes are
  done/failed, the incentive is to build something.
"""

import json
import os
import shutil
import sqlite3
import time

from .util import (Refused, boot_token, die, eprint, now, pid_alive, pid_readable, run,
                   session_pid,
                   same_boot, slug, transcript_activity)

# How long a LIVE claim's transcript must sit unchanged before `stalled_claims` says so.
#
# WHY AN INVENTED NUMBER IS DEFENSIBLE HERE, held to the same rule as `dispatch.LINGER_GRACE_
# SECONDS`: a number that yields a FACT is a fabrication, one that budgets a decision toward
# caution is a budget. This one authorises no action at all -- `stalled` is reported and never
# reclaimed -- so all it budgets is ATTENTION, and the direction of error is stated: it errs
# toward SILENCE.
#
# The floor is set by the longest legitimate quiet stretch, because the transcript gains a line
# when a tool call RETURNS, not while it runs. showrunner's own `verify` re-runs the suite once
# per stale pattern and is documented to its Crawlers as taking "minutes, not seconds", so a
# healthy session genuinely writes nothing for several minutes at a time. Fifteen minutes clears
# that with room; the incident that filed #69 sat frozen for fifty-five.
#
# And the raw measurement rides along with every verdict, so a reader who thinks this number is
# wrong for their run can discount it without having to trust it.
STALL_IDLE_SECONDS = 900

OPEN = "open"
IN_PROGRESS = "in_progress"
CLOSED = "closed"
REFUTED = "refuted"
# A TRUE premise attached to code nothing reaches (#55). Its own status because the two that
# existed are both wrong for it: `closed` claims a user-visible change that does not exist, and
# `refuted` says the analysis was wrong when it was right. A Crawler forced to pick between them
# picks `closed`, because by then it is holding a real commit and real tests.
UNREACHABLE = "unreachable"
# UNREACHABLE is TERMINAL. Omitting it would leave the leaf counted as outstanding forever:
# `ready` would keep offering it, and the stop gate would refuse a turn-end over work that was
# correctly finished — the gate punishing the one outcome that took the most care to reach.
TERMINAL = (CLOSED, REFUTED, UNREACHABLE)


class Leaf(dict):
    """A row. dict so it serializes for free; attributes for readability."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    @property
    def is_epic(self):
        return self.get("kind") == "epic"

    @property
    def labels_list(self):
        return [x for x in (self.get("labels") or "").split(",") if x]

    @property
    def paths_list(self):
        return [x for x in (self.get("paths") or "").split(",") if x]


SCHEMA = """
CREATE TABLE IF NOT EXISTS leaves (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  body          TEXT NOT NULL DEFAULT '',
  kind          TEXT NOT NULL DEFAULT 'task',
  status        TEXT NOT NULL DEFAULT 'open',
  labels        TEXT NOT NULL DEFAULT '',
  paths         TEXT NOT NULL DEFAULT '',
  actor         TEXT,
  claim_pid     INTEGER,
  claim_boot    TEXT,
  claim_host    TEXT,
  claim_tree    TEXT,
  claim_session TEXT,
  claim_ts      INTEGER,
  heartbeat_ts  INTEGER,
  parked        INTEGER NOT NULL DEFAULT 0,
  park_reason   TEXT,
  outcome       TEXT,
  proof         TEXT,
  close_reason  TEXT,
  closed_ts     INTEGER,
  created_ts    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS deps (
  child  TEXT NOT NULL,
  parent TEXT NOT NULL,
  PRIMARY KEY (child, parent)
);
CREATE TABLE IF NOT EXISTS events (
  ts    INTEGER NOT NULL,
  leaf  TEXT,
  kind  TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
"""


def _claim_pid():
    """The pid a claim should record when the caller did not name one — or None.

    `pid or os.getpid()` recorded the CLI PROCESS, which exits seconds later. Every liveness
    question then answers about a process that is already gone: `stale_claims` calls the leaf
    abandoned and `reap --apply` releases it while somebody is working the tree. Reproduced
    twice by a consumer with `spawn <leaf> --actor X` (no `--launch`) followed by starting a
    session in the prepared tree by hand — two leaves read as abandoned while the work ran fine.

    `--launch` already had a remedy: `rebind_claim`, called once the launched pid is known, and
    its docstring describes exactly this failure. The path that does NOT launch had the same
    defect and no remedy at all.

    THE LESSON WAS ALREADY LEARNED ONE LAYER OVER. llms.txt states it for role claims: *a
    claim's pid is DISCOVERED, not handed over* — `lock acquire` and `role claim` both walk the
    ancestry rather than trusting the calling process. The LEAF claim kept `os.getpid()`.

    NONE RATHER THAN A PID THAT IS ABOUT TO DIE. A claim with an unreadable pid is reported by
    `stale_claims` as UNPROVABLE — surfaced with a note and never released — which is the honest
    state for a tree prepared before its session exists. A pid that will be dead in a second is
    not weaker evidence than none; it is evidence for the wrong answer, and it licenses a
    release. Role claims REFUSE in this situation; a leaf claim cannot, because preparing a tree
    before its session exists is the documented workflow.
    """
    pid, basis = session_pid()
    # ONLY THE STRONG BASIS. `ppid-fallback` can be the very shell that is about to exit, which
    # is the defect wearing a different hat.
    return pid if basis == "ancestor-claude" else None


class SqliteGraph:
    name = "vendored"

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level="IMMEDIATE")
        self.db.row_factory = sqlite3.Row
        # WAL lets readers and one writer proceed concurrently; busy_timeout makes a
        # contended writer wait rather than raising. Both matter because more than one
        # orchestrator may share this graph.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # -- reads -------------------------------------------------------------
    def _rows(self, sql, args=()):
        return [Leaf(dict(r)) for r in self.db.execute(sql, args).fetchall()]

    def show(self, leaf_id):
        rows = self._rows("SELECT * FROM leaves WHERE id = ?", (leaf_id,))
        if not rows:
            die("no such leaf: %s" % leaf_id, code=2)
        return rows[0]

    def list(self, status=None, kind=None):
        sql, args = "SELECT * FROM leaves", []
        where = []
        if status:
            where.append("status = ?")
            args.append(status)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self._rows(sql + " ORDER BY created_ts, id", args)

    def deps_of(self, leaf_id):
        return [r["parent"] for r in self.db.execute(
            "SELECT parent FROM deps WHERE child = ?", (leaf_id,)).fetchall()]

    def blockers(self, leaf_id):
        """Unfinished parents. `refuted` counts as finished: a premise that did not hold
        is a resolved question, not a permanent block on everything behind it."""
        out = []
        for parent in self.deps_of(leaf_id):
            rows = self._rows("SELECT * FROM leaves WHERE id = ?", (parent,))
            if not rows:
                out.append(parent)  # dangling dep is a blocker, loudly
            elif rows[0]["status"] not in TERMINAL:
                out.append(parent)
        return out

    def ready(self):
        """Unblocked, unclaimed, non-epic, open work. The only work-discovery entrypoint."""
        out = []
        for leaf in self.list(status=OPEN):
            if leaf.is_epic:
                continue
            if self.blockers(leaf["id"]):
                continue
            out.append(leaf)
        return out

    def stale_claims(self):
        """(leaf, why) for every claim whose owner cannot be alive.

        Parked claims are excluded: usage-limit exhaustion is the *expected* way a
        Crawler pauses in a long unattended run, and a parked Crawler is not dead.
        """
        token = boot_token()
        out, unprovable = [], []
        for leaf in self.list(status=IN_PROGRESS):
            if leaf.get("parked"):
                continue
            claim_boot = leaf.get("claim_boot")
            pid = leaf.get("claim_pid")
            # ONE COMPARISON, shared with `locks._live`. This was `claim_boot != token`, which
            # read a DRIFTING value as proof of death: macOS recomputes boot seconds from the
            # clock, so an NTP adjustment moved the token by one second and a live claim was
            # offered for release. It also would have read every pre-upgrade claim as a
            # different boot the moment the token format changed.
            #
            # `same_boot` answers True / False / None, and None means CANNOT TELL — which falls
            # through to the pid check below rather than to a release.
            if claim_boot and same_boot(claim_boot, token) is False:
                out.append((leaf, "claimed on a different boot (%s, now %s) — its process "
                                  "cannot still be running" % (claim_boot, token)))
            elif not pid_readable(pid):
                # NOT reported as stale. `reap --apply` RELEASES a stale claim, which returns
                # the leaf to `ready` for someone else to take — so an unprovable pid here
                # ends with two Crawlers on one leaf, which is the same failure as two on one
                # device, one layer up. A claim whose owner cannot be named cannot be proved
                # abandoned, and abandonment is the thing that licenses the release.
                unprovable.append((leaf, "owning pid %r cannot be read, so this claim cannot "
                                         "be proved abandoned" % pid))
            elif not pid_alive(pid):
                out.append((leaf, "owning pid %s is not alive" % pid))
        for leaf, why in unprovable:
            eprint("note: %s holds a claim that cannot be adjudicated — %s" % (leaf["id"], why))
        return out

    def stalled_claims(self, idle_seconds=STALL_IDLE_SECONDS):
        """(leaf, why) for LIVE claims whose session has stopped producing (#69).

        THE THIRD VERDICT, and deliberately not a kind of stale. `stale_claims` answers "can
        this claim's owner still be running", and a stalled Crawler answers YES to that: its
        process is alive, its boot matches, and there is nothing about it a pid check can see.
        The two together partition a claim three ways --

            live       the process is alive and its transcript is moving
            stalled    the process is alive and its transcript is FROZEN   <- this method
            abandoned  the process cannot be running                       <- stale_claims

        -- which is the point of the issue: #68 is the false-STALE direction (a live claim read
        as dead because `boot_token` drifted) and this is the false-LIVE direction. One root
        cause, agent state inferred from process state, failing in opposite directions. So this
        is ADDITIVE and `stale_claims` above is untouched; a fix here that widened that method
        would re-break the side somebody has already fixed twice.

        NOTHING IN THIS REPO ACTS ON THE RESULT, and that is a design constraint rather than an
        unfinished edge. Reaping a stalled session in the filing incident would have destroyed
        four files of uncommitted work and an already-green suite; the correct recovery was to
        unstick the agent and leave the claim alone, after which it committed and closed
        normally. A stalled Crawler is the one state where the process still holds the only
        copy of the work, so `reap --apply` must surface it and walk past it.

        Claims whose transcript cannot be READ are reported on stderr and excluded, never
        counted as stalled: a derived path that does not resolve says where showrunner looked,
        not what the agent did.
        """
        token = boot_token()
        out, unreadable = [], []
        for leaf in self.list(status=IN_PROGRESS):
            if leaf.get("parked"):
                continue
            # A claim `stale_claims` would report is ABANDONED, not stalled, and must not be
            # counted twice -- a leaf appearing under both verdicts would offer a reader a
            # release and a do-not-touch for the same claim in one report.
            claim_boot = leaf.get("claim_boot")
            if claim_boot and same_boot(claim_boot, token) is False:
                continue
            pid = leaf.get("claim_pid")
            if not pid_readable(pid) or not pid_alive(pid):
                continue
            act = transcript_activity(leaf.get("claim_tree"), leaf.get("claim_session"))
            if act["idle"] is None:
                unreadable.append((leaf, act["why"]))
                continue
            if act["idle"] < idle_seconds:
                continue
            out.append((leaf, "process %s is alive, but its transcript has not changed for "
                              "%dm (threshold %dm) — %s"
                              % (pid, act["idle"] // 60, idle_seconds // 60, act["path"])))
        for leaf, why in unreadable:
            eprint("note: %s holds a live claim whose activity cannot be measured — %s"
                   % (leaf["id"], why))
        return out

    # -- writes ------------------------------------------------------------
    def _event(self, leaf, kind, detail=""):
        self.db.execute("INSERT INTO events (ts, leaf, kind, detail) VALUES (?,?,?,?)",
                        (now(), leaf, kind, detail))

    def add(self, title, body="", kind="task", labels=(), paths=(), leaf_id=None):
        leaf_id = leaf_id or self._mint_id(title)
        if self.db.execute("SELECT 1 FROM leaves WHERE id = ?", (leaf_id,)).fetchone():
            die("leaf %s already exists" % leaf_id, code=2)
        self.db.execute(
            "INSERT INTO leaves (id, title, body, kind, labels, paths, created_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (leaf_id, title, body, kind, ",".join(labels), ",".join(paths), now()))
        self._event(leaf_id, "add", title)
        self.db.commit()
        return leaf_id

    def _mint_id(self, title):
        base = slug(title, 32)
        cand, n = base, 1
        while self.db.execute("SELECT 1 FROM leaves WHERE id = ?", (cand,)).fetchone():
            n += 1
            cand = "%s-%d" % (base, n)
        return cand

    def dep(self, child, parent):
        self.show(child)
        self.show(parent)
        if child == parent:
            die("a leaf cannot block itself", code=2)
        if self._would_cycle(child, parent):
            die("dep %s <- %s would create a cycle" % (child, parent), code=2)
        self.db.execute("INSERT OR IGNORE INTO deps (child, parent) VALUES (?,?)", (child, parent))
        self._event(child, "dep", "blocked by %s" % parent)
        self.db.commit()

    def undep(self, child, parent):
        """Remove an edge. Returns True when one was there, False when none was.

        THE RETURN VALUE IS THE POINT. `DELETE` succeeds identically whether it removed an edge
        or matched nothing, so a verb that reported "removed" either way would tell an operator
        their graph changed when it did not — and a wrong edge HIDES work, because `ready` means
        unblocked and a false parent keeps a leaf out of the discovery surface entirely. Somebody
        who mistyped an id would go on believing the leaf was freed.

        No cycle check, and no `show` on either id: removing an edge cannot create a cycle, and
        refusing to clean up after a leaf that has since been deleted would strand the edge
        forever — the exact trap this verb exists to open.
        """
        cur = self.db.execute("DELETE FROM deps WHERE child = ? AND parent = ?", (child, parent))
        removed = bool(cur.rowcount)
        if removed:
            self._event(child, "undep", "no longer blocked by %s" % parent)
        self.db.commit()
        return removed

    def _would_cycle(self, child, parent):
        seen, stack = set(), [parent]
        while stack:
            node = stack.pop()
            if node == child:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(self.deps_of(node))
        return False

    def claim(self, leaf_id, actor, pid=None, tree=None, session=None):
        """Take a leaf. Exactly one caller can win, even across processes.

        The check-then-write shape this replaced was a real race, not a theoretical one:
        twelve concurrent claims on one leaf produced **six winners**. More than one
        orchestrator may share a graph — that is the point of a graph that survives
        sessions — and six Crawlers dispatched onto the same leaf is six branches that
        will conflict, discovered at integration with nobody watching.

        The transition is therefore a single conditional UPDATE guarded on the status it
        expects. SQLite makes that atomic, so the winner is whoever's UPDATE matched a row;
        everyone else sees rowcount 0 and is told why, re-read *after* the fact.
        """
        leaf = self.show(leaf_id)
        if leaf["status"] in TERMINAL:
            die("%s is already %s" % (leaf_id, leaf["status"]), code=2)
        blockers = self.blockers(leaf_id)
        if blockers:
            die("%s is blocked by: %s" % (leaf_id, ", ".join(blockers)), code=2)

        cur = self.db.execute(
            "UPDATE leaves SET status=?, actor=?, claim_pid=?, claim_boot=?, claim_host=?, "
            "claim_tree=?, claim_session=?, claim_ts=?, heartbeat_ts=?, parked=0, park_reason=NULL "
            "WHERE id=? AND status=?",
            (IN_PROGRESS, actor, pid if pid is not None else _claim_pid(), boot_token(),
             os.uname().nodename,
             # A CLAIM WITH NO TREE CANNOT BE ATTRIBUTED, and the turn-end gate is scoped by
             # exactly this column — so an unrecorded tree is a leaf nobody can be gated on.
             # `spawn` always passes the Crawler's worktree; every OTHER path (a manual claim,
             # `claim --next` from an orchestrator) passed nothing, which is the common case
             # and would have left the gate silently inert for the party that claims for itself.
             tree or os.getcwd(), session, now(), now(), leaf_id, OPEN))
        if cur.rowcount != 1:
            self.db.rollback()
            current = self.show(leaf_id)
            if current["status"] == IN_PROGRESS:
                # Boot-scoped like every other liveness question here. `stale_claims` two
                # methods up already refuses to call a claim live across a boot; this path did
                # not, so after a reboot a REUSED pid made an abandoned claim look held and
                # refused a leaf that was actually free. Milder than the SIGTERM version of
                # this bug — a wrong refusal rather than a wrong action — and recoverable via
                # `reap`, but it blocks real work with a confident and false explanation.
                claim_boot = current.get("claim_boot")
                same_boot = not claim_boot or claim_boot == boot_token()
                if (same_boot and pid_alive(current.get("claim_pid"))) or current.get("parked"):
                    die("%s is already claimed by %s (pid %s)%s"
                        % (leaf_id, current.get("actor"), current.get("claim_pid"),
                           " [parked]" if current.get("parked") else ""), code=2)
                die("%s holds a stale claim by %s — run `showrunner reap` first, so the "
                    "abandonment is recorded rather than papered over"
                    % (leaf_id, current.get("actor")), code=2)
            die("%s could not be claimed (status is %s)" % (leaf_id, current["status"]), code=2)

        # THE JOURNAL MUST NAME WHAT WAS STORED. This logged `pid or os.getpid()` while the row
        # now records the resolved session, so a viewer reading the event saw a pid the claim
        # never held — two statements of one fact, and the readable one was the wrong one.
        self._event(leaf_id, "claim",
                    "%s pid=%s tree=%s" % (actor, self.show(leaf_id).get("claim_pid"), tree))
        self.db.commit()
        return self.show(leaf_id)

    def claim_next(self, actor, pid=None, tree=None, session=None, prefer=None):
        """Atomically take ANY ready leaf. Returns the leaf, or None if none is available.

        The primitive several orchestrators actually need. `ready` hands the same list to
        everyone who asks, so a fleet that reads it and claims the first entry has every
        member fighting over one leaf and most of them failing — and the naive fix (retry
        the whole list) is a race written by hand, differently, in every caller.

        Losing a race here is not an error: it means a sibling got there first, which is
        the system working. So contention is absorbed, and only "nothing left" is reported.
        """
        candidates = list(prefer or []) + [l["id"] for l in self.ready()]
        seen = set()
        for leaf_id in candidates:
            if leaf_id in seen:
                continue
            seen.add(leaf_id)
            try:
                return self.claim(leaf_id, actor, pid=pid, tree=tree, session=session)
            except Refused:
                continue          # somebody else won it, or it stopped being ready
        return None

    def edit(self, leaf_id, title=None, body=None, paths=None, labels=None,
             add_labels=(), remove_labels=()):
        """Correct a leaf's title, body or paths. The BODY IS THE BRIEF a Crawler is dispatched
        with, so a typo in it is not cosmetic — it is the whole instruction set for an agent.

        Before this, `add` refused an existing id and there was no other verb, so a bad body
        was permanent: the only exit was to close the leaf, which spends the proof-of-done gate
        on nothing and records a decision that never happened. Correcting the instructions is
        not an outcome and must not have to be laundered through one.

        Refuses on a leaf that is no longer open, because rewriting the brief under a Crawler
        that is already working from it is a different and worse thing than a typo.

        LABELS FOR THE SAME REASON, one field along. They were unreachable by exactly the
        argument above -- `add` correctly refuses an existing id and `edit` did not take them --
        so two correct behaviours composed into "no way to relabel", and a consumer had to close
        the leaf and re-create it, leaving a stub in the campaign's done count that did no work.
        Labels pick the LANE, so a typo is not cosmetic: an unmatched leaf falls to the default
        lane, and a default lane that owns an exclusive resource queues that leaf against
        hardware it never needed.

        `labels` REPLACES the set; `add_labels`/`remove_labels` amend it. Both exist because a
        wholesale replace is what you want for a typo and the wrong thing for a leaf whose other
        labels somebody else chose.
        """
        leaf = self.show(leaf_id)
        if leaf["status"] != OPEN:
            die("%s is %s, not open — its brief is already in somebody's hands. Editing it now "
                "would change the instructions under a Crawler working from them."
                % (leaf_id, leaf["status"]), code=2)
        sets, args = [], []
        for col, val in (("title", title), ("body", body)):
            if val is not None:
                sets.append("%s=?" % col)
                args.append(val)
        if paths is not None:
            sets.append("paths=?")
            args.append(",".join(paths))
        if labels is not None or add_labels or remove_labels:
            current = list(labels) if labels is not None else leaf.labels_list
            for lb in add_labels:
                if lb not in current:
                    current.append(lb)
            missing = [lb for lb in remove_labels if lb not in current]
            if missing:
                # A remove that matched nothing is a typo in the REMOVE, and silently succeeding
                # tells you the label is gone when it is still there picking a lane.
                die("%s has no label(s) %s — nothing was changed. Its labels are: %s"
                    % (leaf_id, ", ".join(missing), ", ".join(current) or "(none)"), code=2)
            current = [lb for lb in current if lb not in remove_labels]
            sets.append("labels=?")
            args.append(",".join(current))
        if not sets:
            die("nothing to edit — pass --title, --body/--body-file, --path, --label, "
                "--add-label or --remove-label", code=64)
        with self.db:
            self.db.execute("UPDATE leaves SET %s WHERE id=?" % ", ".join(sets),
                            args + [leaf_id])
            self._event(leaf_id, "edited", ", ".join(s.split("=")[0] for s in sets))
        return self.show(leaf_id)

    def rebind_claim(self, leaf_id, pid):
        """Point an existing claim's liveness at the process that is really doing the work.

        A claim is taken BEFORE the Crawler's session exists — it has to be, because the
        session id must be recorded first (see dispatch). So the pid it records is whichever
        short-lived shell ran `spawn`, and that shell exits seconds later. Every liveness
        question then answers about a process that is already gone: `stale_claims` calls the
        leaf abandoned, and `reap --apply` RELEASES it while the Crawler is still working.

        Measured in a consuming repo: claim pid 4635 (the shell, gone) against a `claude -p`
        at 4784 alive for fifteen minutes, both naming the same session. One Crawler, and the
        record said nobody was there.
        """
        with self.db:
            self.db.execute(
                "UPDATE leaves SET claim_pid=?, claim_boot=?, heartbeat_ts=? "
                "WHERE id=? AND status=?",
                (pid, boot_token(), now(), leaf_id, IN_PROGRESS))
        return self.show(leaf_id)

    def heartbeat(self, leaf_id):
        self.db.execute("UPDATE leaves SET heartbeat_ts=? WHERE id=?", (now(), leaf_id))
        self.db.commit()

    def park(self, leaf_id, reason):
        leaf = self.show(leaf_id)
        if leaf["status"] != IN_PROGRESS:
            die("only a claimed leaf can be parked (%s is %s)" % (leaf_id, leaf["status"]), code=2)
        self.db.execute("UPDATE leaves SET parked=1, park_reason=? WHERE id=?", (reason, leaf_id))
        self._event(leaf_id, "park", reason)
        self.db.commit()

    def unpark(self, leaf_id):
        """Return a parked leaf to its claim — with a pid that names a SESSION, not this process.

        THE FOURTH PLACE THE SAME RULE APPLIES, found by enumerating rather than by reading.
        `claim` was fixed to discover the pid instead of recording `os.getpid()`; this method
        REWRITES the same column and kept the old default, so a leaf parked at a usage limit and
        unparked came back holding the pid of the `showrunner unpark` process — gone the instant
        the command returned. The bug would have reappeared on the exact workflow parking exists
        for, in a sibling method, days after the fix.

        A consumer named the shape after hitting it twice in one day: a rule that holds in three
        places and not the fourth is invisible to REVIEW, because every file you open is correct.
        What finds it is asking where else the rule applies and enumerating the answers.
        """
        self.db.execute(
            "UPDATE leaves SET parked=0, park_reason=NULL, claim_pid=?, claim_boot=?, heartbeat_ts=? "
            "WHERE id=?", (_claim_pid(), boot_token(), now(), leaf_id))
        self._event(leaf_id, "unpark", "")
        self.db.commit()

    def release(self, leaf_id, reason=""):
        self.db.execute(
            "UPDATE leaves SET status=?, actor=NULL, claim_pid=NULL, claim_boot=NULL, "
            "claim_host=NULL, claim_tree=NULL, claim_session=NULL, parked=0, park_reason=NULL "
            "WHERE id=?", (OPEN, leaf_id))
        self._event(leaf_id, "release", reason)
        self.db.commit()

    def close(self, leaf_id, outcome, proof, reason):
        if outcome not in (CLOSED, REFUTED, UNREACHABLE):
            die("outcome must be one of 'closed', 'refuted', 'unreachable'", code=2)
        # CLOSING CLEARS THE PARK. A leaf that is closed AND parked is a state combination that
        # cannot mean anything: park records that a CLAIM is paused and accounted for, and a
        # closed leaf has no claim to pause. Observed in the wild — one agent parked another's
        # inert leaf, the owner then closed it, and it read `closed` with `parked: 1` forever.
        # Harmless on its own, and exactly the kind of impossible pair that later reads as
        # evidence of something.
        self.db.execute(
            "UPDATE leaves SET status=?, outcome=?, proof=?, close_reason=?, closed_ts=?, "
            "parked=0, park_reason=NULL WHERE id=?",
            (outcome, outcome, proof, reason, now(), leaf_id))
        self._event(leaf_id, outcome, "%s [proof: %s]" % (reason, proof))
        self.db.commit()
        return self.show(leaf_id)

    def amend(self, leaf_id, premise, reason, evidence):
        """Correct the VERDICT on a leaf that is already closed. Returns the leaf.

        A Crawler closed a leaf `--refuted` with a well-cited report, then came back on its own:
        a parallel trace it had dispatched found a real bug it had missed, and the verdict should
        have been `partial`. It did exactly what this project asks — kept looking, refuted its own
        conclusion, unprompted — and the tooling had nowhere to put it. No `reopen`, and `edit`
        correctly refuses a closed leaf, so the permanent record said `refuted` for work that
        needed doing.

        That is not a cosmetic row. `refuted` means *nobody needs to build this*, so a wrong one
        removes real work from the cycle for good, and the more disciplined the Crawler the more
        likely it is to produce the correction that has nowhere to go.

        SUPERSEDES, NEVER OVERWRITES. The original close and its proof stay in the record and the
        correction is appended beneath them, because a verdict that quietly changed is a record
        nobody can audit — and the first close was not a mistake to erase, it was the honest
        conclusion from what was known then.

        The inverse of `edit`, deliberately: `edit` refuses a CLOSED leaf because the brief is
        already in somebody's hands, and this refuses an OPEN one because there is no verdict yet
        to correct — that is what `close` is for.

        WHAT THIS DOES NOT DO: queue the work the correction implies. A verdict moving from
        `refuted` to `partial` means something real was missed, and the residue needs its own leaf
        — `add` it. Making amend spawn work would hide a new piece of work inside a bookkeeping
        verb, and the point of the premise field is that outcomes stay visible.
        """
        # Imported here rather than at module scope: the premise VOCABULARY belongs to the
        # gate, not to storage, and graph.py must not grow an opinion about it that can drift
        # from the one `close` enforces. gates.py imports nothing from here, so this is safe.
        from .gates import PREMISE_VERDICTS
        if premise not in PREMISE_VERDICTS:
            die("--premise must be one of %s" % "/".join(PREMISE_VERDICTS), code=2)
        leaf = self.show(leaf_id)
        if leaf["status"] not in TERMINAL:
            die("%s is %s, not closed — there is no verdict to correct yet. Close it through "
                "the gate instead." % (leaf_id, leaf["status"]), code=2)
        # A corrected verdict decides the same question the original did, so it moves `outcome`
        # with it: `refuted` is the one that says nobody needs to build this, and leaving it in
        # place under a `partial` premise would keep the exact claim being withdrawn.
        outcome = REFUTED if premise == "refuted" else CLOSED
        appended = ("%s\n  AMENDED: premise %s [was: %s] — %s [evidence: %s]"
                    % (leaf.get("close_reason") or "", premise,
                       leaf.get("close_reason", "").split("[premise: ")[-1].split("]")[0]
                       or "unrecorded",
                       reason, evidence))
        self.db.execute("UPDATE leaves SET outcome=?, close_reason=? WHERE id=?",
                        (outcome, appended.strip(), leaf_id))
        self._event(leaf_id, "amended", "premise -> %s: %s [evidence: %s]"
                    % (premise, reason, evidence))
        self.db.commit()
        return self.show(leaf_id)

    def events(self, limit=50):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM events ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)).fetchall()]


class BrGraph:
    """Adapter for an existing `br` (beads) graph.

    Two rules, both from issue #6:

    1. **Parse JSON as JSON.** The prototype's `stop-gate` split records on a literal
       `},{` and grepped out `"issue_type":"epic"`. Any change to br's field order or
       whitespace turns that gate into a no-op that reports success.
    2. **A shape we do not recognise is an error, never an empty list.** The dangerous
       direction is silence: "no claimed-open work" is precisely what a broken parser
       returns, and it is indistinguishable from a clean run.
    """

    name = "br"

    def __init__(self, db=None, binary="br"):
        self.db = db
        self.binary = binary

    @staticmethod
    def available(db=None, binary="br"):
        if not shutil.which(binary):
            return False
        return db is None or os.path.exists(db)

    def _br(self, args, expect_json=False):
        cmd = [self.binary]
        if self.db:
            cmd += ["--db", self.db]
        cmd += list(args)
        rc, out, err = run(cmd, timeout=60)
        if rc != 0:
            die("br failed (%s): %s\n%s" % (rc, " ".join(cmd), err.strip()), code=2)
        if not expect_json:
            return out
        try:
            data = json.loads(out or "[]")
        except json.JSONDecodeError as exc:
            die("br returned output this adapter cannot parse as JSON (%s).\n"
                "Refusing rather than reporting an empty graph — an empty answer here is "
                "indistinguishable from a clean run.\ncommand: %s\noutput: %.400s"
                % (exc, " ".join(cmd), out), code=2)
        if isinstance(data, dict):
            for key in ("issues", "items", "results", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
            die("br returned a JSON object with no recognised list field "
                "(saw keys: %s). Refusing rather than treating it as empty."
                % ", ".join(sorted(data)), code=2)
        if not isinstance(data, list):
            die("br returned JSON of type %s; expected a list of records" % type(data).__name__,
                code=2)
        return data

    def _leaf(self, rec):
        if not isinstance(rec, dict):
            die("br record is not an object: %.200r" % (rec,), code=2)
        rid = rec.get("id")
        if not rid:
            die("br record has no 'id' field (keys: %s) — refusing to guess"
                % ", ".join(sorted(rec)), code=2)
        labels = rec.get("labels") or []
        if isinstance(labels, str):
            labels = [x.strip() for x in labels.split(",") if x.strip()]
        return Leaf({
            "id": rid,
            "title": rec.get("title") or rec.get("summary") or "",
            "body": rec.get("description") or rec.get("body") or "",
            "kind": "epic" if (rec.get("issue_type") or rec.get("type")) == "epic" else "task",
            "status": rec.get("status") or OPEN,
            "labels": ",".join(labels),
            "paths": "",
            "actor": rec.get("actor") or rec.get("assignee"),
            "claim_pid": None, "claim_boot": None, "claim_tree": None,
            "parked": 0, "outcome": None, "proof": None,
            "created_ts": 0,
        })

    def list(self, status=None, kind=None):
        args = ["list", "--json"]
        if status:
            args += ["--status", status]
        leaves = [self._leaf(r) for r in self._br(args, expect_json=True)]
        if kind:
            leaves = [x for x in leaves if x["kind"] == kind]
        return leaves

    def show(self, leaf_id):
        recs = self._br(["show", leaf_id, "--json"], expect_json=True)
        if not recs:
            die("no such leaf: %s" % leaf_id, code=2)
        return self._leaf(recs[0])

    def ready(self):
        return [x for x in (self._leaf(r) for r in self._br(["ready", "--json"], expect_json=True))
                if not x.is_epic]

    def blockers(self, leaf_id):
        # NOT []. br owns dependency resolution and this adapter does not read its graph, so
        # an empty list here would assert "nothing blocks this leaf" on no evidence — the same
        # answer a working check returns, which is exactly why stale_claims() raises rather
        # than returning empty. An unfailable accept in a method that gates work is the shape
        # this project keeps finding; refusing is the honest form.
        raise Refused(
            "the br backend does not expose a leaf's blockers to this adapter, so 'nothing "
            "blocks it' cannot be asserted here. Use `br ready`, which resolves dependencies "
            "itself and is what this adapter's ready() calls.", code=3)

    def deps_of(self, leaf_id):
        raise Refused(
            "the br backend does not expose a leaf's dependencies to this adapter. Returning "
            "an empty list would read as 'this leaf depends on nothing', which is a claim "
            "about the graph rather than an admission of not knowing.", code=3)

    def stale_claims(self):
        # br records no liveness on a claim, so this adapter cannot answer the question.
        # Saying so is the honest form; returning [] would read as "nothing is stale".
        raise Refused(
            "the br backend records no owner liveness on a claim, so abandoned claims "
            "cannot be detected here (issue #7). Reap against the vendored backend, or "
            "spawn through `showrunner spawn`, which records a live PID in the campaign "
            "record — `showrunner status` shows it and `showrunner reap` acts on it, "
            "neither of which goes through the graph backend.",
            code=3)

    def stalled_claims(self, idle_seconds=STALL_IDLE_SECONDS):
        # Same posture as stale_claims above, for a different missing fact. Detecting a stall
        # needs `claim_tree` and `claim_session` to derive the transcript from, and this
        # adapter's claim() records neither -- br has nowhere to put them. Returning [] would
        # read as "every live claim is producing", which is the exact false-LIVE answer #69
        # exists to end, arriving from the one backend that cannot know.
        raise Refused(
            "the br backend records no worktree or session id on a claim, so a stalled "
            "session cannot be detected here (issue #69). Reap or run `status` against the "
            "vendored backend, which stores both at claim time.",
            code=3)

    def claim(self, leaf_id, actor, pid=None, tree=None, session=None):
        self._br(["update", leaf_id, "--claim", "--actor", actor or "showrunner"])
        return self.show(leaf_id)

    def release(self, leaf_id, reason=""):
        self._br(["update", leaf_id, "--status", OPEN])

    def close(self, leaf_id, outcome, proof, reason):
        tag = {REFUTED: "REFUTED", UNREACHABLE: "UNREACHABLE"}.get(outcome, "done")
        self._br(["close", leaf_id, "--reason", "%s: %s [proof: %s]" % (tag, reason, proof)])
        return self.show(leaf_id)

    def heartbeat(self, leaf_id):
        pass

    def park(self, leaf_id, reason):
        raise Refused("the br backend has no park state; park is vendored-only", code=3)

    def unpark(self, leaf_id):
        raise Refused("the br backend has no park state; park is vendored-only", code=3)

    def add(self, title, body="", kind="task", labels=(), paths=(), leaf_id=None):
        out = self._br(["create", title, "-t", kind, "--json"])
        try:
            return json.loads(out)["id"]
        except Exception:
            die("could not read the new leaf id out of `br create` output: %.200s" % out, code=2)

    def dep(self, child, parent):
        self._br(["dep", "add", child, parent])

    def undep(self, child, parent):
        """Mirror of `dep`, through br's own verb.

        ANSWERS True RATHER THAN MEASURING, and says so here rather than implying more: `_br`
        raises on a non-zero exit, so reaching this line means br accepted the removal, but br
        reports no count and this adapter will not invent one. The vendored backend can tell
        "removed one" from "there was none" and does; here the honest answer is "br did not
        refuse". NOT EXERCISED against a real br in this checkout — the suite skips those
        assertions when the binary is absent, and it is absent here.
        """
        self._br(["dep", "remove", child, parent])
        return True

    def events(self, limit=50):
        return []


def open_graph(cfg):
    """Resolve the configured backend. `auto` prefers br when it is genuinely usable."""
    backend = cfg.graph_backend
    if backend == "br":
        if not BrGraph.available(cfg.br_db):
            die("graph.backend is 'br' but the br binary is not on PATH%s"
                % ("" if not cfg.br_db else " (or %s is missing)" % cfg.br_db), code=2)
        return BrGraph(cfg.br_db)
    if backend == "vendored":
        return SqliteGraph(cfg.graph_db)
    if backend != "auto":
        die("graph.backend must be one of: auto, vendored, br", code=2)
    if cfg.br_db and BrGraph.available(cfg.br_db):
        return BrGraph(cfg.br_db)
    return SqliteGraph(cfg.graph_db)
