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

from .util import Refused, boot_token, die, eprint, now, pid_alive, pid_readable, run, slug

OPEN = "open"
IN_PROGRESS = "in_progress"
CLOSED = "closed"
REFUTED = "refuted"
TERMINAL = (CLOSED, REFUTED)


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
            if claim_boot and claim_boot != token:
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
            (IN_PROGRESS, actor, pid or os.getpid(), boot_token(), os.uname().nodename,
             tree, session, now(), now(), leaf_id, OPEN))
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

        self._event(leaf_id, "claim", "%s pid=%s tree=%s" % (actor, pid or os.getpid(), tree))
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
        self.db.execute(
            "UPDATE leaves SET parked=0, park_reason=NULL, claim_pid=?, claim_boot=?, heartbeat_ts=? "
            "WHERE id=?", (os.getpid(), boot_token(), now(), leaf_id))
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
        if outcome not in (CLOSED, REFUTED):
            die("outcome must be 'closed' or 'refuted'", code=2)
        self.db.execute(
            "UPDATE leaves SET status=?, outcome=?, proof=?, close_reason=?, closed_ts=? WHERE id=?",
            (outcome, outcome, proof, reason, now(), leaf_id))
        self._event(leaf_id, outcome, "%s [proof: %s]" % (reason, proof))
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

    def claim(self, leaf_id, actor, pid=None, tree=None, session=None):
        self._br(["update", leaf_id, "--claim", "--actor", actor or "showrunner"])
        return self.show(leaf_id)

    def release(self, leaf_id, reason=""):
        self._br(["update", leaf_id, "--status", OPEN])

    def close(self, leaf_id, outcome, proof, reason):
        tag = "REFUTED" if outcome == REFUTED else "done"
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
