#!/usr/bin/env python3
"""showrunner's guarantees, as runnable checks. Issue #1.

The README used to claim "12/12 assertions" behind a script that hardcoded one machine's
`$HOME`, one repo's beads DB, and a `br` binary that was not on PATH. On a clean checkout
that command could not get past its first block, so the central credibility claim of the
repo was an assertion the reader was asked to take on trust — precisely the posture this
project exists to refuse. A repo claiming a passing suite should ship one a stranger can
run.

So this harness is split in two:

* **CORE** — everything that needs nothing installed beyond Python 3 and `git` (which you
  already used to clone this). Green on a clean clone, every time.
* **OPTIONAL** — assertions that need something else (`br`, `tmux`). They **skip loudly**,
  naming the missing dependency, rather than failing obscurely.

Run:  python3 test/run.py [-v]
"""

import argparse
import ast
import filecmp
import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))

from showrunner import brief, campaign, collide, config, dispatch, gates, graph as G, lanes, locks, worktree  # noqa: E402
from showrunner.util import Refused, boot_token as boot_token_for_test  # noqa: E402

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv
PASS, FAIL, SKIP = [], [], []
_GROUP = ["?"]


def group(name):
    _GROUP[0] = name
    print("\n== %s ==" % name)


def ok(label, condition, detail=""):
    if condition:
        PASS.append(label)
        print("  PASS  %s" % label)
    else:
        FAIL.append((_GROUP[0], label, detail))
        print("  FAIL  %s%s" % (label, ("\n        " + str(detail)) if detail else ""))
    return bool(condition)


def eq(label, got, want):
    return ok(label, got == want, "got %r, want %r" % (got, want))


def raises(label, fn, contains=None):
    try:
        fn()
    except Refused as exc:
        if contains and contains.lower() not in str(exc).lower():
            return ok(label, False, "refused, but not for the expected reason: %s" % exc)
        return ok(label, True)
    except Exception as exc:  # noqa: BLE001
        return ok(label, False, "raised %s instead of Refused: %s" % (type(exc).__name__, exc))
    return ok(label, False, "did not refuse")


def skip(label, why):
    SKIP.append((label, why))
    print("  SKIP  %s — %s" % (label, why))


def have(binary):
    return shutil.which(binary) is not None


# --------------------------------------------------------------- fixtures
TMPDIRS = []


def tmpdir(name="showrunner-test"):
    d = tempfile.mkdtemp(prefix=name + "-")
    TMPDIRS.append(d)
    return d


def cleanup():
    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)


def sh(cmd, cwd, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError("%s failed in %s: %s" % (cmd, cwd, p.stderr))
    return p


def make_repo(extra_config=None, files=None):
    """A throwaway git repo with a showrunner config. Returns a Config."""
    d = tmpdir("repo")
    sh(["git", "init", "-q", "-b", "main"], d)
    sh(["git", "config", "user.email", "test@example.com"], d)
    sh(["git", "config", "user.name", "showrunner test"], d)
    for rel_path, content in (files or {"README.md": "seed\n"}).items():
        full = os.path.join(d, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(content)
    sh(["git", "add", "-A"], d)
    sh(["git", "commit", "-q", "-m", "seed"], d)

    data = dict(config.DEFAULTS)
    data.update({
        "project_name": "test",
        "graph": {"backend": "vendored", "db": ".showrunner/graph.db", "br_db": None},
        "resources": [
            {"name": "device", "match": [r"\bdeploy\b", r"\bflutter run\b"]},
            {"name": "pg-port", "match": [r"\bpg_ctl\b"]},
        ],
        "lanes": [
            {"name": "device-work", "lane": "serialized", "resource": "device",
             "match": {"labels": ["device"]}},
            {"name": "pure", "lane": "headless", "match": {"labels": ["backend", "docs"]}},
        ],
        "default_lane": "serialized",
        "collision": {"extra_globs": [], "always_serialize": ["test/**"]},
    })
    data.update(extra_config or {})
    cfg = config.Config(data, os.path.realpath(d), os.path.join(d, ".showrunner", "config.json"))
    config.write(cfg)
    # THE FIXTURE SUPPLIED LESS THAN PRODUCTION, which is the quieter direction of the usual
    # warning. Both real entry points place this binary — install.sh copies it, and `init`
    # copies it — and only this helper wrote a config straight to disk and skipped it. So every
    # repo the suite exercised was one where the path all briefs name did not exist, and no
    # assertion could have noticed because the fixture made the broken state universal.
    src = os.path.join(ROOT, "bin", "showrunner")
    dst = os.path.join(d, ".showrunner", "bin", "showrunner")
    if os.access(src, os.R_OK):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
    return cfg


def new_graph(cfg):
    return G.SqliteGraph(cfg.graph_db)


class DeadPid:
    """A pid that is definitely not alive: spawn a trivial child and reap it."""

    def __init__(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.pid = p.pid


# =========================================================== CORE: locks
def test_locks():
    group("Single-consumer resource locks (issue #3)")
    cfg = make_repo()
    ls = locks.LockSet(cfg)
    device = ls.lock("device")
    pg = ls.lock("pg-port")

    # AN UNREADABLE PID IS NOT A DEAD ONE. `pid_alive` returns False both for "not running"
    # and for "not a pid", and only the first licenses deleting somebody else's lock. A
    # partial write by a LIVE holder reads exactly like a holder that died — which is how a
    # mutex hands a single-consumer resource to a second Crawler and logs that it reclaimed
    # a stale lock. llm_chat hit this shape as a lockfile holding binary instead of a pid,
    # where the consequence was a kill that silently did nothing.
    corrupt = ls.lock("device")
    os.makedirs(corrupt.dir, exist_ok=True)
    with open(os.path.join(corrupt.dir, "boot"), "w") as fh:
        fh.write(boot_token_for_test())
    with open(os.path.join(corrupt.dir, "holder"), "w") as fh:
        fh.write("crawler-a")
    for label, raw in (("binary", b"\x00\x01rubbish"), ("empty", b"")):
        with open(os.path.join(corrupt.dir, "pid"), "wb") as fh:
            fh.write(raw)
        eq("a %s pid reads UNREADABLE, never STALE — STALE is a licence to delete the lock"
           % label, corrupt.state()[0], locks.UNREADABLE)
    # The control: this must not have become "never reclaim anything". A pid that parses and
    # does not respond, recorded this boot, is still proved dead and still reclaimable.
    with open(os.path.join(corrupt.dir, "pid"), "w") as fh:
        fh.write("999999")
    eq("...while a readable pid that is genuinely gone is still STALE, so reclaiming still "
       "works", corrupt.state()[0], locks.STALE)
    with open(os.path.join(corrupt.dir, "pid"), "wb") as fh:
        fh.write(b"\x00rubbish")
    raises("acquiring against an unreadable lock REFUSES rather than reclaiming or silently "
           "polling to a timeout, which reads the same as a busy resource",
           lambda: corrupt.acquire(os.getpid(), "crawler-b"), "UNREADABLE")
    shutil.rmtree(corrupt.dir, ignore_errors=True)

    state, _ = device.state()
    eq("a fresh lock is FREE", state, locks.FREE)

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ok("acquire succeeds when free", device.acquire(holder.pid, "agent-A deploy"))
        state, h = device.state()
        eq("held by a live pid reads HELD", state, locks.HELD)

        ok("a second acquire is BLOCKED while the holder is alive",
           device.acquire(os.getpid(), "agent-B") is False)

        allow, msg = ls.guard("frontend deploy --platform ios")
        ok("guard BLOCKS a matching verb while held", allow is False, msg)
        ok("the block names the resource and the holder",
           "device" in msg and str(holder.pid) in msg, msg)

        # An "allowed" assertion that only checks the verdict passes identically against a
        # guard that never ran. Assert the REASON too, so the test can tell a guard that
        # evaluated and permitted from one that is absent. (Mutation-tested: neutering
        # LockSet.guard to `return True, ""` must fail these.)
        allow, msg = ls.guard("python3 -m pytest test/foo.py")
        ok("guard ALLOWS a command matching no resource", allow is True, msg)
        ok("...and SAYS it evaluated the command, so a dead guard cannot pass this",
           "matches no single-consumer resource" in msg, msg)

        # Named resources are independent: unrelated work must not queue behind unrelated work.
        allow, msg = ls.guard("pg_ctl start")
        ok("holding 'device' does not block the unrelated 'pg-port' resource", allow is True, msg)
        ok("...and names the resource it checked and found free, not merely 'allowed'",
           "pg-port" in msg and "free" in msg, msg)

        # INV5: a guard that blocks its own holder is a guard that gets switched off.
        with open(os.path.join(device.dir, "session"), "w") as fh:
            fh.write("sess-A\n")
        allow, msg = ls.guard("frontend deploy", session="sess-A")
        ok("guard ALLOWS the session that already holds the lock", allow is True, msg)
        ok("...and names the resource, so 'the holder may proceed' is distinguishable from "
           "'nothing was checked'", "device" in msg, msg)
        allow, msg = ls.guard("frontend deploy", session="sess-B")
        ok("guard still BLOCKS a different session", allow is False, msg)
    finally:
        holder.terminate()
        holder.wait()

    state, _ = device.state()
    eq("a dead holder reads STALE, not HELD", state, locks.STALE)
    ok("acquire RECLAIMS a stale lock", device.acquire(os.getpid(), "agent-C"))
    device.release(force=True)

    # PID reuse: a pid recorded on a previous boot cannot still be running.
    device.acquire(os.getpid(), "agent-D")
    with open(os.path.join(device.dir, "boot"), "w") as fh:
        fh.write("some-host:1\n")
    state, _ = device.state()
    eq("a claim from a different boot is STALE even when the pid is alive", state, locks.STALE)
    device.release(force=True)

    marker = os.path.join(tmpdir("lockrun"), "ran.txt")
    lock = ls.lock("device")
    acquired = lock.acquire(os.getpid(), "run-wrapper")
    ok("run wrapper acquires", acquired)
    subprocess.run([sys.executable, "-c", "open(%r,'w').write('body ran')" % marker])
    lock.release(pid=os.getpid())
    ok("run wrapper's body executed", os.path.exists(marker))
    state, _ = ls.lock("device").state()
    eq("the lane is FREE after release", state, locks.FREE)


# ========================================================== CORE: config
def test_config_refusals():
    group("Config refuses what would degrade silently (INV8, issues #3, #4)")
    cfg = make_repo()
    findings = cfg.validate()
    errs = [m for lvl, m in findings if lvl == "error"]
    # "No errors" is satisfied by a validator with no opinions at all, so require that it
    # actually reached its conclusions. Same shape as an absence assertion over an empty
    # observation: the verdict alone is also what a broken producer returns.
    ok("the validator actually evaluated the config, so 'no errors' is not vacuous",
       any(lvl == "ok" for lvl, _ in findings), findings)
    ok("a sane config produces no errors", not errs, errs)

    bad = make_repo({"lock_root": None, "worktree_root": ".worktrees"})
    bad.data["lock_root"] = os.path.join(bad.root, ".worktrees", "locks")
    errs = [m for lvl, m in bad.validate() if lvl == "error"]
    ok("REFUSES a lock_root inside the worktree root (N trees, N locks, mutex is a no-op)",
       any("no-op" in m for m in errs), errs)

    outside = make_repo()
    outside.data["worktree_root"] = tmpdir("sibling")
    errs = [m for lvl, m in outside.validate() if lvl == "error"]
    ok("REFUSES a worktree_root outside the repo (the Crawler's own guard would deny its "
       "first edit)", any("outside the repo" in m for m in errs), errs)
    raises("require_valid() raises on an unsafe config", outside.require_valid, "not safe")

    # `expanduser` handles a leading ~ and NOTHING else, so "$HOME/x" stays a literal string
    # and resolves against whatever directory the caller is in. For a lock root that is a
    # different directory per caller — a mutex that is quietly a no-op — and it passed an
    # isabs() check, because abspath makes anything absolute.
    var = make_repo()
    var.data["lock_root"] = "$HOME/sr-locks"
    errs = [m for lvl, m in var.validate() if lvl == "error"]
    ok("an unexpanded shell variable in a path is REFUSED — only a leading ~ expands, so it "
       "would silently resolve against the caller's cwd",
       any("NOT expanded" in m for m in errs), errs)
    cwd_before = os.getcwd()
    try:
        os.chdir("/tmp")
        a = var.lock_root
        os.chdir(ROOT)
        b = var.lock_root
    finally:
        os.chdir(cwd_before)
    eq("...and every configured path now resolves against the REPO ROOT, never the process "
       "cwd, so it cannot differ per caller", a, b)

    # The companion: the check must PASS the forms that are actually fine, or it is a rule
    # nobody can satisfy and it gets switched off (INV5).
    for good in ("~/sr-locks", "/tmp/sr-locks", "relative/locks"):
        fine = make_repo()
        fine.data["lock_root"] = good
        bad = [m for lvl, m in fine.validate() if lvl == "error" and "NOT expanded" in m]
        ok("...and %r is accepted, so the check is not simply refusing everything" % good,
           not bad, bad)


# =========================================================== CORE: graph
def test_every_rule_can_fail():
    group("Every validation rule must have a reachable failing input")
    if not have("git"):
        skip("the reachable-rules group", "git is not installed")
        return

    # The lesson that produced this: `isabs(abspath(x))` is True for EVERY string, so the
    # lock_root rule was a predicate with no failing input at all — sitting in the validator
    # written to prevent exactly that failure, returning an empty error list, which reads as
    # "validated". A check that cannot fail is not a weak check; it was never a check.
    #
    # The mutation sweep cannot find this. Neutering a validator that already has no opinion
    # changes nothing, so the suite notices no difference. The property this asserts is
    # different: for every error branch the validator can emit, SOME input reaches it.
    def cfg_with(**over):
        c = make_repo()
        for k, v in over.items():
            c.data[k] = v
        return c

    def errors(c):
        return [m for lvl, m in c.validate() if lvl == "error"]

    outside_root = tmpdir("outside")
    cases = [
        ("lock_root inside worktree_root", cfg_with(lock_root=".worktrees/locks"), "no-op"),
        ("worktree_root outside the repo", cfg_with(worktree_root=outside_root), "outside the repo"),
        ("worktree_root unset", cfg_with(worktree_root=None), "unset"),
        ("worktree_root is the repo itself", cfg_with(worktree_root="."), "repo root itself"),
        ("a resource with no name", cfg_with(resources=[{"match": ["x"]}]), "no name"),
        ("serialized lane naming no resource",
         cfg_with(lanes=[{"name": "l", "lane": "serialized", "match": {"labels": ["a"]}}]),
         "names no resource"),
        ("lane naming an unknown resource",
         cfg_with(resources=[], lanes=[{"name": "l", "lane": "serialized", "resource": "ghost",
                                        "match": {"labels": ["a"]}}]),
         "unknown resource"),
        ("default_lane that is neither", cfg_with(default_lane="sideways"), "default_lane must be"),
        ("an unexpanded variable in a path", cfg_with(lock_root="$HOME/locks"), "NOT expanded"),
        ("an unexpanded variable in an inject path",
         cfg_with(inject=[{"path": "$HOME/.env"}]), "NOT expanded"),
    ]
    reached = set()
    for label, c, needle in cases:
        found = [m for m in errors(c) if needle in m]
        if ok("reachable: %s" % label, bool(found), errors(c)):
            reached.add(needle)

    # A tripwire, not a coverage ratio. Comparing "distinct messages reached" against a raw
    # branch count cannot balance — two branches here legitimately emit the same message — and
    # a metric that can never be satisfied is its own kind of dead check. So: pin the number
    # of error branches. Adding one trips this, and the fix is to add a case above proving the
    # new rule can fire, or to say here why it cannot.
    EXPECTED_ERROR_BRANCHES = 10
    with open(os.path.join(ROOT, "lib", "showrunner", "config.py")) as fh:
        branches = fh.read().count('"error"') - 1        # -1: the require_valid() filter
    eq("the validator has exactly the error branches this group accounts for — a new one must "
       "arrive with a case proving it can fire", branches, EXPECTED_ERROR_BRANCHES)

    clean = make_repo()
    ok("...and a sane config still produces none of them, so these are not firing on everything",
       not errors(clean), errors(clean))


def test_graph():
    group("Work graph: dependency-gated fan-out (issue #2)")
    cfg = make_repo()
    g = new_graph(cfg)

    setup = g.add("set up the environment", leaf_id="setup")
    a = g.add("endpoint A", leaf_id="ep-a")
    b = g.add("endpoint B", leaf_id="ep-b")
    epic = g.add("the epic", kind="epic", leaf_id="epic-1")
    g.dep(a, setup)
    g.dep(b, setup)

    ready = [x["id"] for x in g.ready()]
    ok("blocked leaves are hidden from ready", "ep-a" not in ready and "ep-b" not in ready, ready)
    ok("the shared prerequisite IS ready", "setup" in ready, ready)
    ok("epics are never handed out as ready work", "epic-1" not in ready, ready)

    g.claim("setup", "crawler-1")
    still_ready = [x["id"] for x in g.ready()]
    ok("a claimed leaf leaves ready", "setup" not in still_ready)
    ok("...and ready() is still answering, so that absence is not just an empty list",
       g.list(status=G.IN_PROGRESS), still_ready)
    raises("a second claim on the same leaf is refused",
           lambda: g.claim("setup", "crawler-2"), "already claimed")

    g.close("setup", G.CLOSED, "README.md", "done")
    ready = [x["id"] for x in g.ready()]
    ok("closing the prerequisite releases BOTH dependents at once",
       "ep-a" in ready and "ep-b" in ready, ready)

    g.claim("ep-a", "crawler-1")
    g.close("ep-a", G.REFUTED, "README.md", "premise did not hold")
    eq("refuted is a distinct terminal status", g.show("ep-a")["status"], G.REFUTED)

    c = g.add("depends on the refuted leaf", leaf_id="ep-c")
    g.dep(c, "ep-a")
    ok("a refuted parent unblocks its dependents (a resolved question, not a permanent block)",
       "ep-c" in [x["id"] for x in g.ready()])

    raises("a dependency cycle is refused", lambda: g.dep("setup", "ep-b"), "cycle")


# ================================================ CORE: claim liveness
def test_lifecycle():
    group("Crawler lifecycle: a claim carries liveness (issue #7)")
    cfg = make_repo()
    g = new_graph(cfg)
    g.add("work that outlives its Crawler", leaf_id="w1")
    dead = DeadPid()
    g.claim("w1", "doomed-crawler", pid=dead.pid)

    ok("a claim whose owner is dead is NOT ready (the bug: work silently leaves the queue)",
       "w1" not in [x["id"] for x in g.ready()])
    stale = g.stale_claims()
    ok("the dead claim is detected as stale", any(l["id"] == "w1" for l, _ in stale), stale)
    ok("the reason names the dead pid", any("pid" in why for _, why in stale), stale)

    # SAME CONFLATION AS THE LOCK, ONE LAYER UP. `reap --apply` RELEASES a stale claim, which
    # returns the leaf to `ready` for somebody else — so a claim whose pid cannot be read must
    # not be called stale, or the result is two Crawlers on one leaf. The lock version of this
    # was two Crawlers on one device; the licence is the same word, "proved abandoned".
    g.db.execute("UPDATE leaves SET claim_pid=NULL WHERE id=?", ("w1",))
    g.db.commit()
    ok("a claim whose pid cannot be READ is not reported stale — it cannot be proved "
       "abandoned, and abandonment is what licenses releasing somebody else's claim",
       "w1" not in [l["id"] for l, _ in g.stale_claims()], g.stale_claims())
    # The control, again: this must not have become "nothing is ever stale".
    g.db.execute("UPDATE leaves SET claim_pid=? WHERE id=?", (dead.pid, "w1"))
    g.db.commit()
    ok("...while a readable pid that is genuinely gone is still stale, so reap still works",
       "w1" in [l["id"] for l, _ in g.stale_claims()])

    actions, _ = campaign.reap(cfg, g, apply=False)
    ok("reap DRY RUN reports without mutating",
       any(a.get("leaf") == "w1" for a in actions) and g.show("w1")["status"] == G.IN_PROGRESS)

    campaign.reap(cfg, g, apply=True)
    ok("reap --apply returns the leaf to ready", "w1" in [x["id"] for x in g.ready()])
    ok("the abandonment is recorded as an event, not silently absorbed",
       any(e["kind"] == "release" for e in g.events()))

    g.add("parked work", leaf_id="w2")
    g.claim("w2", "limited-crawler", pid=dead.pid)
    g.park("w2", "usage limit — window resets at 14:00")

    live_claim = g.add("live work", leaf_id="w3")
    g.claim(live_claim, "live-crawler", pid=os.getpid())

    # A control in the SAME observation: a genuinely abandoned claim the detector must still
    # find. Without it, "w2 is not stale" and "w3 is not stale" are both satisfied by a
    # detector that finds nothing at all — which is exactly the state `reap` would be in
    # while reporting a clean campaign.
    g.add("genuinely abandoned", leaf_id="w4")
    g.claim("w4", "ghost", pid=DeadPid().pid)
    stale_ids = [l["id"] for l, _ in g.stale_claims()]
    ok("the stale detector is still finding real abandonment, so the exclusions below are "
       "not vacuous", "w4" in stale_ids, stale_ids)
    ok("a PARKED claim is not stale (a Crawler at a usage limit is not dead)",
       "w2" not in stale_ids, stale_ids)
    ok("a live claim is never reaped", "w3" not in stale_ids, stale_ids)


# =========================================================== CORE: gates
def test_close_gate():
    group("Proof-of-done gate (issues #6, #12)")
    cfg = make_repo()
    g = new_graph(cfg)
    g.add("prove it", leaf_id="p1")
    g.claim("p1", "crawler")

    def close(**kw):
        args = dict(proof=None, reason="done", premise="holds", premise_read="README.md")
        args.update(kw)
        return lambda: gates.close_gate(cfg, g, "p1", args.pop("proof"), args.pop("reason"), **args)

    raises("REFUSES a close with no proof", close(), "must name a real")
    raises("REFUSES a proof path that does not exist",
           close(proof="no/such/file.txt"), "does not exist")

    empty = os.path.join(cfg.root, "empty.txt")
    open(empty, "w").close()
    raises("REFUSES an empty file as proof", close(proof="empty.txt"), "empty file")

    raises("REFUSES a close with no --premise verdict",
           close(proof="README.md", premise=None), "--premise is required")
    raises("REFUSES a premise verdict with nothing cited",
           close(proof="README.md", premise_read=None), "--premise-read")
    raises("REFUSES a --premise-read naming a path that does not exist",
           close(proof="README.md", premise_read="nope.md"), "does not exist")

    # An artifact older than the claim is evidence about something else.
    old = os.path.join(cfg.root, "old.txt")
    with open(old, "w") as fh:
        fh.write("pre-existing\n")
    os.utime(old, (time.time() - 86400, time.time() - 86400))
    raises("REFUSES proof that predates the claim", close(proof="old.txt"), "older than the work")
    leaf, notes = gates.close_gate(cfg, g, "p1", "old.txt", "done", premise="holds",
                                   premise_read="README.md",
                                   stale_proof_reason="this test already covered it")
    eq("a stale proof CAN be accepted with a recorded reason", leaf["status"], G.CLOSED)
    ok("the reason is recorded on the close, not waved through",
       "stale-proof accepted" in (leaf.get("close_reason") or ""), leaf.get("close_reason"))
    ok("the gate states what it does NOT check (relevance)",
       any("NOT CHECKED" in n for n in notes), notes)

    g.add("fresh proof", leaf_id="p2")
    g.claim("p2", "crawler")
    fresh = os.path.join(cfg.root, "fresh.txt")
    with open(fresh, "w") as fh:
        fh.write("artifact\n")
    leaf, _ = gates.close_gate(cfg, g, "p2", "fresh.txt", "did the thing",
                               premise="holds", premise_read="README.md")
    eq("ALLOWS a close naming a real, fresh artifact", leaf["status"], G.CLOSED)
    ok("the proof is recorded so a reviewer can judge relevance later",
       leaf.get("proof") == "fresh.txt", leaf.get("proof"))

    g.add("premise did not hold", leaf_id="p3")
    g.claim("p3", "crawler")
    # README.md is the SEED file: older than the claim, on purpose. Refuting a premise means
    # reading pre-existing source, so this is the realistic shape and it must not be refused.
    leaf, notes = gates.close_gate(cfg, g, "p3", None, "the described failure is not live here",
                                   refuted=True, evidence="README.md", premise="refuted",
                                   premise_read="README.md")
    eq("a refuted premise closes as REFUTED, a first-class successful outcome",
       leaf["status"], G.REFUTED)
    ok("...and evidence that PREDATES the claim is accepted for a refutation — it is the "
       "pre-existing source you read, and demanding freshness would make the honest outcome "
       "the hard one to record",
       leaf.get("proof") == "README.md", leaf.get("proof"))
    ok("refuting is reported as a success, not a failure",
       any("first-class" in n for n in notes), notes)


def test_stop_gate():
    group("Stop gate (issue #6)")
    cfg = make_repo()
    g = new_graph(cfg)
    g.add("leaf work", leaf_id="s1")
    g.add("a container", kind="epic", leaf_id="s-epic")

    ok_, msg = gates.stop_gate(cfg, g)
    ok("stop is OK with nothing claimed", ok_, msg)

    g.claim("s-epic", "orchestrator")
    ok_, msg = gates.stop_gate(cfg, g)
    ok("an in-progress EPIC does not block a turn-end (it is a container)", ok_, msg)

    g.claim("s1", "crawler")
    ok_, msg = gates.stop_gate(cfg, g)
    ok("REFUSES a turn-end while a claimed leaf is open", not ok_, msg)
    ok("the refusal names the leaf and its actor", "s1" in msg and "crawler" in msg, msg)

    g.park("s1", "usage limit")
    ok_, msg = gates.stop_gate(cfg, g)
    ok("a PARKED leaf does not block (accounted-for work, not abandoned work)", ok_, msg)
    ok("parked work is still reported, not hidden", "parked" in msg, msg)

    g.unpark("s1")
    # The proof must postdate the claim; citing the seed README passes only by luck of the
    # clock, which is a flaky test AND a misuse of the gate it is exercising.
    fresh = os.path.join(cfg.root, "stop-proof.txt")
    with open(fresh, "w") as fh:
        fh.write("checks passed\n")
    gates.close_gate(cfg, g, "s1", "stop-proof.txt", "done", premise="holds",
                     premise_read="README.md")
    ok_, msg = gates.stop_gate(cfg, g)
    ok("stop is OK once the claimed work is closed through the gate", ok_, msg)


def test_baseline():
    group("No NEW failures, not 'all green' (issues #6, #9)")
    flag = os.path.join(tmpdir("checks"), "extra")
    cmd = ("%s -c \"import os,sys; print('FAILED test_preexisting'); "
           "print('FAILED test_new') if os.path.exists(%r) else None; sys.exit(1)\""
           % (sys.executable, flag))
    cfg = make_repo({"checks": [{"name": "suite", "cmd": cmd}]})

    current = gates.run_checks(cfg)
    verdict, report = gates.compare_to_baseline(cfg, current, None)
    ok("with no baseline the comparison SAYS SO rather than passing", verdict is None, report)

    base = gates.record_baseline(cfg)
    ok("the baseline records the pre-existing failure",
       any("test_preexisting" in s for c in base["checks"] for s in c["failures"]), base)

    verdict, report = gates.compare_to_baseline(cfg, gates.run_checks(cfg), gates.load_baseline(cfg))
    ok("a repo that was ALREADY red passes the gate — 'all green' would switch this off",
       verdict is True, report)

    open(flag, "w").close()
    verdict, report = gates.compare_to_baseline(cfg, gates.run_checks(cfg), gates.load_baseline(cfg))
    ok("a genuinely NEW failure fails the gate", verdict is False, report)
    ok("the report names the new failure", any("test_new" in line for line in report), report)

    coarse = make_repo({"checks": [{"name": "silent", "cmd": "%s -c \"import sys; sys.exit(1)\""
                                                             % sys.executable}]})
    res = gates.run_checks(coarse)
    eq("a check that fails without parseable failure lines is marked exit-code-only",
       res["checks"][0]["resolution"], "exit-code-only")


# ========================================================= CORE: routing
def test_routing():
    group("Lane routing (issue #8)")
    cfg = make_repo()
    g = new_graph(cfg)
    g.add("deploy to the TV", leaf_id="r1", labels=["device"])
    g.add("add an endpoint", leaf_id="r2", labels=["backend"])
    g.add("something nobody wrote a rule for", leaf_id="r3")

    d1 = lanes.route(cfg, g.show("r1"))
    eq("a labelled device leaf routes to the serialized lane", d1["lane"], lanes.SERIALIZED)
    eq("...and carries the resource it must serialize on", d1["resource"], "device")
    ok("the decision names the rule that produced it", d1["rule"] == "device-work", d1)

    d2 = lanes.route(cfg, g.show("r2"))
    eq("a backend leaf routes to the headless lane", d2["lane"], lanes.HEADLESS)

    d3 = lanes.route(cfg, g.show("r3"))
    eq("an UNMATCHED leaf defaults to serialized (the costs are not symmetric)",
       d3["lane"], lanes.SERIALIZED)
    ok("...and says so out loud — an unmatched leaf is a missing rule",
       d3["matched"] is False and "NO RULE MATCHED" in d3["why"], d3)

    path = lanes.log(cfg, [d1, d2, d3])
    with open(path) as fh:
        logged = [json.loads(l) for l in fh if l.strip()]
    ok("every routing decision is logged with its rule, so a wrong route is diagnosable",
       len(logged) == 3 and logged[0]["rule"] == "device-work", logged)


# ======================================================= CORE: collision
def test_collision():
    group("Collision prediction before fan-out (issue #5)")
    files = {
        "src/alpha.py": "def alpha_handler():\n    pass\n",
        "src/beta.py": "def beta_handler():\n    pass\n",
        "test/run.py": "# every issue adds a case here\n",
        "README.md": "seed\n",
    }
    cfg = make_repo(files=files)
    g = new_graph(cfg)
    g.add("fix alpha", leaf_id="c1", labels=["backend"], body="`src/alpha.py` is wrong")
    g.add("also touch alpha", leaf_id="c2", labels=["backend"], body="change `src/alpha.py` too")
    g.add("fix beta", leaf_id="c3", labels=["backend"], body="`src/beta.py` needs work")
    g.add("vague", leaf_id="c4", labels=["backend"], body="make it better somehow")

    leaves = [g.show(i) for i in ("c1", "c2", "c3")]
    waves, est, notes = collide.plan_waves(cfg, leaves)
    wave_of = {leaf: i for i, w in enumerate(waves) for leaf in w}
    ok("two leaves that name the SAME file do not fan out together",
       wave_of["c1"] != wave_of["c2"], waves)
    ok("two leaves on different files DO share a wave", wave_of["c1"] == wave_of["c3"], waves)
    ok("holding a leaf back is explained, not silent",
       any("c2" in n and "overlap" in n for n in notes), notes)

    waves, est, notes = collide.plan_waves(cfg, [g.show("c4")])
    ok("a leaf with no estimable blast radius is treated as colliding with everything",
       est["c4"]["estimable"] is False, est["c4"])
    ok("...and the reason is stated", any("c4" in n and "unknown blast radius" in n for n in notes),
       notes)

    shared_cfg = make_repo(files=files,
                           extra_config={"collision": {"extra_globs": ["test/**"],
                                                       "always_serialize": ["test/**"]}})
    gs = new_graph(shared_cfg)
    gs.add("adds a case", leaf_id="s1", labels=["backend"], body="`src/alpha.py`")
    gs.add("adds another case", leaf_id="s2", labels=["backend"], body="`src/beta.py`")
    leaves = [gs.show("s1"), gs.show("s2")]
    waves, est, notes = collide.plan_waves(shared_cfg, leaves)
    ok("the shared test file lands in EVERY leaf's radius",
       "test/run.py" in est["s1"]["paths"] and "test/run.py" in est["s2"]["paths"], est)
    ok("...but does not force a serial run — it is owed to serialized integration instead",
       len(waves) == 1, waves)
    ok("...and the shared surface is reported", any("shared surface" in n for n in notes), notes)


# ================================================== CORE: spawn (git)
def test_spawn():
    group("Spawning a Crawler (issues #4, #10, #11, #13)")
    if not have("git"):
        skip("the whole spawn group", "git is not installed")
        return

    secret = ".env"
    cfg = make_repo(files={"README.md": "seed\n", "src/app.py": "x = 1\n",
                           ".gitignore": ".env\n.game_loop/\n"},
                    extra_config={"inject": [{"path": ".env", "mode": "symlink"}]})
    with open(os.path.join(cfg.root, secret), "w") as fh:
        fh.write("API_KEY=super-secret\n")

    g = new_graph(cfg)
    g.add("do the work", leaf_id="w1", labels=["backend"])
    rec = worktree.spawn(cfg, g.show("w1"), actor="crawler-a")

    wt = os.path.realpath(rec["worktree"])
    ok("the worktree is created INSIDE the repo (or the Crawler's own guard denies its first edit)",
       wt.startswith(os.path.realpath(cfg.root) + os.sep), wt)
    ok("the worktree root is gitignored by showrunner itself",
       os.path.exists(os.path.join(cfg.worktree_root, ".gitignore")))
    ok("tracked files are present in the worktree", os.path.exists(os.path.join(wt, "src/app.py")))

    injected = os.path.join(wt, secret)
    ok("the gitignored secret IS injected (a fresh worktree gets tracked files only)",
       os.path.exists(injected))
    ok("...as a symlink — one copy, one lifetime, and it cannot drift", os.path.islink(injected))
    with open(injected) as fh:
        ok("...and it resolves to the real content", "super-secret" in fh.read())

    # A decoy the Crawler legitimately created. Without it `git add -A` stages NOTHING —
    # a pristine worktree has no changes — and "the secret is not staged" is then trivially
    # true while demonstrating nothing about the ignore mechanism. The decoy makes the
    # absence evidence: add DID stage something, and the secret was not it.
    with open(os.path.join(wt, "crawler-work.txt"), "w") as fh:
        fh.write("work the Crawler actually did\n")
    sh(["git", "add", "-A"], wt)
    staged = sh(["git", "diff", "--cached", "--name-only"], wt).stdout.split()
    ok("`git add -A` really does stage the Crawler's own new file, so the next assertion is "
       "not vacuous", "crawler-work.txt" in staged, staged)
    ok("`git add -A` in the worktree CANNOT stage the injected secret", secret not in staged, staged)
    ok("...because the repo's own tracked .gitignore covers it — showrunner does NOT write "
       "git's shared exclude file, which is not per-worktree and would change the main "
       "checkout's ignores too",
       not worktree.unignored(wt, [secret]).stageable, worktree.unignored(wt, [secret]))
    ok("...and the check proves it RAN, not merely that it found nothing — a silent guard's "
       "'nothing to report' is indistinguishable from 'never looked'",
       worktree.unignored(wt, [secret]).checked == [secret],
       worktree.unignored(wt, [secret]))

    loose = make_repo(files={"README.md": "seed\n"},
                      extra_config={"inject": [{"path": "creds.txt"}]})
    with open(os.path.join(loose.root, "creds.txt"), "w") as fh:
        fh.write("token\n")
    gl2 = new_graph(loose)
    gl2.add("work", leaf_id="loose1")
    raises("an injected path that is neither tracked nor ignored is REFUSED — `git add -A` "
           "would otherwise commit it onto the Crawler's branch",
           lambda: worktree.spawn(loose, gl2.show("loose1")), "environment is incomplete")

    rec_b = worktree.spawn(cfg, g.add("other work", leaf_id="w2") and g.show("w2"),
                           actor="crawler-b")
    ok("each Crawler gets its OWN scratch dir (the commitmsg.txt near-miss)",
       rec["scratch"] != rec_b["scratch"], (rec["scratch"], rec_b["scratch"]))
    for r in (rec, rec_b):
        ok("...created at spawn, not left to convention: %s" % os.path.basename(r["scratch"]),
           os.path.isdir(r["scratch"]))
    ok("both scratch dirs sit under one scratch root, so they are findable on reap",
       os.path.dirname(rec["scratch"]) == os.path.dirname(rec_b["scratch"]))

    # ONE CRAWLER, ONE CHECKOUT — and this is load-bearing for a NEIGHBOUR, which is why it is
    # asserted rather than left as a property of how spawn happens to work. game_loop resolves
    # a Crawler's harness home from CLAUDE_PROJECT_DIR, and it reasoned about our fan-out on
    # the assumption that each Crawler is its own checkout: its account-scoped snapshot shares
    # across linked worktrees precisely because ROOT does not. If this repo ever put several
    # Crawlers in ONE checkout, that assumption silently inverts — and game_loop has no way to
    # find out, because nothing in their tree can see our topology. So the day this assertion
    # fails is the day somebody owes them a message; that is the whole reason it exists.
    homes = {os.path.realpath(os.path.join(r["worktree"], ".game_loop")) for r in (rec, rec_b)}
    ok("each Crawler's harness home is its own — two Crawlers never share one checkout. If this "
       "ever changes, TELL game_loop: their per-account lease assumes the opposite",
       len(homes) == 2, sorted(homes))
    ok("...and neither is the orchestrator's own harness home, so a Crawler cannot quietly "
       "inherit the parent's session state, claims or authorizations",
       os.path.realpath(os.path.join(cfg.root, ".game_loop")) not in homes, sorted(homes))

    # `git worktree add` copies TRACKED files only, so an untracked harness never crosses.
    hcfg = make_repo(files={"README.md": "seed\n"})
    os.makedirs(os.path.join(hcfg.root, ".game_loop", "bin"), exist_ok=True)
    with open(os.path.join(hcfg.root, ".game_loop", "bin", "verify"), "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    ok("an UNTRACKED harness dir is flagged as a gap before the Crawler hits a denied commit",
       worktree.harness_gap(hcfg) is not None, worktree.harness_gap(hcfg))
    sh(["git", "add", "-f", ".game_loop"], hcfg.root)
    sh(["git", "commit", "-q", "-m", "track harness"], hcfg.root)
    ok("...and NOT flagged once the harness is tracked and so crosses into worktrees",
       worktree.harness_gap(hcfg) is None, worktree.harness_gap(hcfg))

    shares = rec["shares"]
    ok("spawn enumerates what the Crawler still SHARES with its siblings", bool(shares), shares)
    ok("...and each entry says what to do instead of bypassing the gate",
       all(s.get("instead") for s in shares), shares)

    text = brief.build(cfg, g.show("w1"), rec)
    ok("the brief actually carries the shared-state section — an empty audit would silently "
       "drop it and the Crawler would never be told what its worktree fails to isolate",
       "does NOT isolate" in text, text[:200])
    ok("the brief demands a premise verdict before any code is written",
       "verify the premise" in text.lower(), text[:200])
    ok("the brief names 'premise refuted' as a successful outcome", "--refuted" in text)
    ok("the brief points the Crawler at its own scratch dir",
       os.path.basename(rec["scratch"]) in text)
    ok("the brief names the ABSOLUTE tree to run verify in — the harness's refusal cannot say "
       "which tree, and with several Crawlers there is more than one candidate",
       os.path.realpath(rec["worktree"]) in text or rec["worktree"] in text, )
    ok("the brief warns against reaching the tree through a shell variable — measured: the "
       "gate DENIES a literal path and silently ALLOWS a variable-built one, and orchestration "
       "uses variables by default",
       "shell variable" in text and "silently" in text, )
    ok("the brief warns about the shared-state refusal without offering a bypass",
       "--no-verify" in text and "never bypass" in text.lower(), )

    # A declared inject path that is missing must fail the SPAWN, loudly.
    bad = make_repo(files={"README.md": "seed\n", ".gitignore": "service-account.json\n"},
                    extra_config={"inject": [{"path": "service-account.json"}]})
    gb = new_graph(bad)
    gb.add("needs a secret", leaf_id="x1")
    raises("a MISSING declared inject path aborts the spawn instead of surfacing later as a "
           "mysterious runtime failure",
           lambda: worktree.spawn(bad, gb.show("x1")), "environment is incomplete")
    ok("...and the aborted spawn leaves no half-built worktree behind",
       not os.path.isdir(os.path.join(bad.worktree_root, worktree.crawler_name("x1", "crawler"))))
    branches = sh(["git", "branch", "--list", "showrunner/*"], bad.root).stdout
    ok("...nor an orphaned branch, so the retry after fixing the environment does not fail "
       "with a different, misleading error", "showrunner/x1" not in branches, branches)
    with open(os.path.join(bad.root, "service-account.json"), "w") as fh:
        fh.write("{}\n")
    rec_retry = worktree.spawn(bad, gb.show("x1"))
    ok("...so the retry actually succeeds once the real problem is fixed",
       os.path.isdir(rec_retry["worktree"]), rec_retry)

    opt = make_repo(extra_config={"inject": [{"path": "maybe.json", "optional": True}]})
    go = new_graph(opt)
    go.add("optional secret", leaf_id="x2")
    rec_o = worktree.spawn(opt, go.show("x2"))
    ok("an OPTIONAL missing path is reported but does not abort",
       any("optional" in line for line in rec_o["injected"]), rec_o["injected"])


# ============================================ CORE: integration (git)
FAKE_HARNESS = """#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(here)
mode_f = os.path.join(root, "TESTMODE")
mode = open(mode_f).read().strip() if os.path.exists(mode_f) else "clean"
OWNED = {"owned": [{"path": "config.json", "seed_from": ".game_loop/config.json", "rule": True},
                   {"path": "INVARIANTS.md", "seed_from": ".game_loop/INVARIANTS.md", "rule": True},
                   {"path": "verify.yaml", "seed_from": "templates/verify.yaml", "rule": True},
                   {"path": "LEDGER.md", "seed_from": ".game_loop/LEDGER.md", "rule": False}],
         "rule_files": ["config.json", "INVARIANTS.md", "verify.yaml"],
         "notes_files": ["LEDGER.md"]}
verb = sys.argv[1] if len(sys.argv) > 1 else ""
if verb == "owned":
    print(json.dumps(OWNED)); sys.exit(0)
if verb == "worktree":
    codes = {"clean": 0, "drifted": 1, "undetermined": 2, "notes-drifted": 3}
    p = dict(OWNED); p["status"] = mode; p["detail"] = "test harness reports " + mode
    print(json.dumps(p)); sys.exit(codes.get(mode, 2))
sys.exit(64)
"""


def _seed_harness(root, verbs=True, mode="clean", track_settings=True, ignore_dir=True):
    """A harness fixture. With `verbs`, it answers `owned` and `worktree` like the contract."""
    d = os.path.join(root, ".game_loop")
    os.makedirs(os.path.join(d, "bin"), exist_ok=True)
    binp = os.path.join(d, "bin", "game_loop")
    with open(binp, "w") as fh:
        fh.write(FAKE_HARNESS if verbs else "#!/bin/sh\nexit 64\n")
    os.chmod(binp, 0o755)
    for name, body in (("verify.yaml", "rules:\n  - suite\n"),
                       ("INVARIANTS.md", "INV1 real\n"),
                       ("config.json", '{"read_roots": ["/somewhere"]}\n'),
                       ("LEDGER.md", "notes\n")):
        with open(os.path.join(d, name), "w") as fh:
            fh.write(body)
    with open(os.path.join(d, "TESTMODE"), "w") as fh:
        fh.write(mode + "\n")
    with open(os.path.join(d, ".gitignore"), "w") as fh:
        fh.write("state.json\nsessions/\nedited.txt\nlog.jsonl\n")
    os.makedirs(os.path.join(d, "sessions", "abc123"), exist_ok=True)
    with open(os.path.join(d, "sessions", "abc123", "state.json"), "w") as fh:
        fh.write('{"mandate": "somebody else\'s work"}\n')
    with open(os.path.join(d, "edited.txt"), "w") as fh:
        fh.write("some/other/file.py\n")
    os.makedirs(os.path.join(root, ".claude"), exist_ok=True)
    with open(os.path.join(root, ".claude", "settings.json"), "w") as fh:
        fh.write('{"statusLine": "the project\'s own", "hooks": {"PreToolUse": []}}\n')
    if ignore_dir:
        gi = os.path.join(root, ".gitignore")
        prev = open(gi).read() if os.path.exists(gi) else ""
        if ".game_loop" not in prev:
            with open(gi, "w") as fh:
                fh.write(prev + ".game_loop/\n")
            sh(["git", "add", ".gitignore"], root)
            sh(["git", "commit", "-q", "-m", "ignore harness runtime"], root)
    if track_settings:
        # The recommended shape: tracked, so it crosses with every worktree and the
        # installer never has to run per-Crawler.
        sh(["git", "add", "-f", ".claude/settings.json"], root)
        sh(["git", "commit", "-q", "-m", "track hook registration"], root)
    return d


def test_harness_provisioning():
    group("The Crawler's harness must be the SAME harness, and the HARNESS decides what that means")
    if not have("git"):
        skip("the harness provisioning group", "git is not installed")
        return
    from showrunner import harness as H

    ok("showrunner keeps NO list of which harness files are rules (that list drifts silently; "
       "the harness owns it)",
       not hasattr(H, "DEFAULT_RULE_FILES"),
       [n for n in dir(H) if "RULE" in n])

    # --- the harness answers for itself -------------------------------------
    cfg = make_repo()
    _seed_harness(cfg.root)
    info = H.owned(cfg, ".game_loop")
    ok("showrunner asks the harness for its owned set", bool(info), info)
    eq("...and takes the rule/notes split from it, rather than assuming",
       (info["rule_files"], info["notes_files"]),
       (["config.json", "INVARIANTS.md", "verify.yaml"], ["LEDGER.md"]))

    g = new_graph(cfg)
    g.add("work", leaf_id="h1", labels=["backend"])
    rec = worktree.spawn(cfg, g.show("h1"), actor="crawler-h")
    wt = rec["worktree"]
    ok("an UNTRACKED harness is provisioned into the worktree",
       os.path.exists(os.path.join(wt, ".game_loop", "bin", "game_loop")))
    ok("...with the executable bit preserved",
       os.access(os.path.join(wt, ".game_loop", "bin", "game_loop"), os.X_OK))
    ok("the harness's OWN verdict is what showrunner records, not showrunner's comparison",
       any("verified by the harness" in a for a in rec["provisioned"]), rec["provisioned"])
    ok("...and the report names the limit that is REAL — the hook-registration file is outside "
       "the harness directory, so the harness cannot compare it and showrunner checks it "
       "separately",
       any("NOT checked by it" in a and "hook-registration" in a for a in rec["provisioned"]),
       rec["provisioned"])
    ok("another session's state is NOT copied into the Crawler",
       not os.path.exists(os.path.join(wt, ".game_loop", "sessions", "abc123", "state.json")))
    ok("...nor its edited-file set", not os.path.exists(os.path.join(wt, ".game_loop", "edited.txt")))

    with open(os.path.join(wt, "crawler-work.txt"), "w") as fh:
        fh.write("work\n")
    sh(["git", "add", "-A"], wt)
    staged_h = sh(["git", "diff", "--cached", "--name-only"], wt).stdout
    ok("add stages the Crawler's own file here too, so the next assertion is not vacuous",
       "crawler-work.txt" in staged_h, staged_h)
    ok("`git add -A` cannot stage the provisioned harness", ".game_loop/" not in staged_h)

    # --- exit-code contract --------------------------------------------------
    def spawn_with(mode, leaf, installer=None):
        c = make_repo(extra_config={"harness": {"provision": "auto", "require": True,
                                                "installer": installer}} if installer else None)
        _seed_harness(c.root, mode=mode)
        gg = new_graph(c)
        gg.add("work", leaf_id=leaf, labels=["backend"])
        return c, gg

    c, gg = spawn_with("drifted", "h2")
    raises("exit 1 (RULE files drifted) ABORTS the spawn — the Crawler would enforce different "
           "things than the orchestrator",
           lambda: worktree.spawn(c, gg.show("h2")), "environment is incomplete")

    c, gg = spawn_with("undetermined", "h3")
    raises("exit 2 (could not tell) ABORTS too — 'could not tell' must never read as 'clean'",
           lambda: worktree.spawn(c, gg.show("h3")), "environment is incomplete")

    c, gg = spawn_with("notes-drifted", "h4")
    rec4 = worktree.spawn(c, gg.show("h4"))
    ok("exit 3 (NOTES drifted) warns and carries on — per-tree notes are ordinary",
       any("NOTE:" in a for a in rec4["provisioned"]), rec4["provisioned"])

    # --- the hook-registration file -----------------------------------------
    ok("showrunner does NOT copy the hook-registration file (the installer MERGES it, "
       "preserving the project's own settings and its stray-hook warning)",
       not os.path.exists(os.path.join(wt, ".claude", "settings.json")) or
       "settings.json" not in " ".join(rec["provisioned"]),
       rec["provisioned"])
    nohooks = make_repo()
    _seed_harness(nohooks.root, track_settings=False)
    gnh = new_graph(nohooks)
    gnh.add("work", leaf_id="h1b", labels=["backend"])
    raises("a worktree that would get a harness but NO registered hooks ABORTS — showrunner "
           "would otherwise promise a guarded agent and deliver an unguarded one",
           lambda: worktree.spawn(nohooks, gnh.show("h1b")), "environment is incomplete")

    inst = make_repo()
    _seed_harness(inst.root, track_settings=False)
    script = os.path.join(inst.root, "fake-install.sh")
    with open(script, "w") as fh:
        fh.write("#!/bin/sh\nmkdir -p \"$3/.claude\"\n"
                 "cp -R \"$2/.game_loop\" \"$3/.game_loop\" 2>/dev/null\n"
                 "cp \"$2/.claude/settings.json\" \"$3/.claude/settings.json\"\n"
                 "echo merged\n")
    os.chmod(script, 0o755)
    inst.data["harness"] = {"provision": "auto", "require": True, "installer": script}
    config.write(inst)
    gi = new_graph(inst)
    gi.add("work", leaf_id="h5", labels=["backend"])
    rec5 = worktree.spawn(inst, gi.show("h5"))
    ok("a configured installer is used instead of copying, and is told which tree to match",
       any("installer" in a and "MERGES" in a for a in rec5["provisioned"]), rec5["provisioned"])
    ok("...and the hook registration lands through it",
       os.path.exists(os.path.join(rec5["worktree"], ".claude", "settings.json")))

    # --- a harness with no verbs --------------------------------------------
    noverb = make_repo()
    _seed_harness(noverb.root, verbs=False)
    gn = new_graph(noverb)
    gn.add("work", leaf_id="h6", labels=["backend"])
    raises("a harness that does not answer the contract is REFUSED by name, not papered over "
           "with a comparison showrunner invented",
           lambda: worktree.spawn(noverb, gn.show("h6")), "does not answer")
    lax = make_repo(extra_config={"harness": {"provision": "auto", "require": False}})
    _seed_harness(lax.root, verbs=False)
    gl = new_graph(lax)
    gl.add("work", leaf_id="h7", labels=["backend"])
    rec7 = worktree.spawn(lax, gl.show("h7"))
    ok("...with require=false as the escape hatch, and the unverified state stated on the record",
       any("NOT ENFORCED" in a for a in rec7["provisioned"]), rec7["provisioned"])

    # A Crawler can weaken its OWN rules after it starts. Verifying only at spawn verifies
    # that for exactly one instant.
    post = make_repo()
    _seed_harness(post.root)
    gp = new_graph(post)
    gp.add("tamper", leaf_id="h8", labels=["backend"])
    rec8 = worktree.spawn(post, gp.show("h8"), actor="tamperer")
    campaign.record_spawn(post, rec8, pid=os.getpid())
    status, _, mis = H.check_tree(post, rec8["worktree"])
    eq("a freshly spawned tree checks clean", status, "clean")
    ok("...and is not retroactively flagged as mis-certified when nothing was unreadable",
       mis is False, mis)

    with open(os.path.join(rec8["worktree"], ".game_loop", "TESTMODE"), "w") as fh:
        fh.write("drifted\n")
    status, _, _ = H.check_tree(post, rec8["worktree"])
    eq("post-spawn rule drift is caught by RE-asking the harness, not assumed away",
       status, "drifted")
    finding = next(f for f in campaign.reconcile(post, gp)
                   if f["crawler"] == rec8["crawler"])
    ok("...and reconcile reports it above every other verdict — a gate answering a different "
       "question makes everything it certified mean less",
       finding["verdict"].startswith("HARNESS DRIFTED"), finding["verdict"])

    # THE UNARMED WATCHDOG (issue #23). `showrunner waiting` was built for one consumer and
    # nothing connected them, so a two-layer install defaults to the guard and the answer not
    # talking — and the failure is the kind that trains people to disable alarms: a correctly
    # fanned-out orchestrator gets rung, then pages a human, then somebody raises the threshold
    # until the genuinely wedged run is invisible too.
    #
    # Every state walked, because three of the four are silences that look alike from the call
    # site, and because showrunner must never NAME the harness's config key — it asks a verb
    # and repeats the path the answer carries.
    probe_states = {
        "armed": ({"configured": True, "command": "/abs/showrunner waiting"}, "armed"),
        "unarmed": ({"configured": False, "set_it_by": ".game_loop/config.local.json -> x"},
                    "unarmed"),
        # `failing` outranks `configured`: the probe contract's third state is "could not
        # answer", which RINGS and reports failing, so a relative path or a lost executable bit
        # presents as a broken watchdog rather than as the config error it is.
        "failing": ({"configured": True, "failing": True, "command": "relative/path"},
                    "failing"),
    }
    _orig_porc = H._porcelain
    for label, (payload, want) in sorted(probe_states.items()):
        H._porcelain = lambda b, v, _p=payload: (0, _p)
        try:
            state, detail = H.waiting_probe(post, ".game_loop")
        finally:
            H._porcelain = _orig_porc
        eq("the harness's waiting probe reads as '%s'" % label, state, want)
        if label == "unarmed":
            ok("...and the remedy repeats the path the HARNESS gave, so showrunner never names "
               "a config key belonging to the layer below", "config.local.json" in detail, detail)
    H._porcelain = lambda b, v: (0, None)
    try:
        state, _ = H.waiting_probe(post, ".game_loop")
    finally:
        H._porcelain = _orig_porc
    ok("a harness answering no such verb reads as None, not as unarmed — 'it has no watchdog' "
       "and 'its watchdog is unwired' are different, and only one is a warning about this repo",
       state is None, state)

    # BLOCKED IS NOT WORKING (issue #24), and this is a defect showrunner helped build. Before
    # the turn-end gate was wired, a Crawler that could not finish EXITED with its leaf open —
    # loud, caught by one liveness poll. After, it stays alive and inert while every signal
    # reads healthy, and one sat that way for 44 minutes before a chat message woke it.
    #
    # The fixture harness answers the seam so the states can be walked; the real one was
    # exercised by hand first, per session, with a fabricated block and two negative controls.
    def _seam(payload):
        orig = H._porcelain
        H._porcelain = lambda b, v, _p=payload: (0, _p)
        try:
            return payload
        finally:
            H._porcelain = orig

    _orig_run = H.run
    def _canned_run(out):
        return lambda cmd, cwd=None, check=False, timeout=None, env=None: (0, out, "")

    H.run = _canned_run(json.dumps({"stop_gate": {
        "blocked": True, "blocks_total": 1, "limit": 3,
        "attachments": {"showrunner-stop-gate": {"verdict": "blocked", "consecutive": 1}}}}))
    try:
        blocked, why = H.stop_gate(post, rec8["worktree"], "some-session-id")
    finally:
        H.run = _orig_run
    ok("a Crawler refused at a turn-end is reported BLOCKED", blocked is True, (blocked, why))
    ok("...and the detail says the harness's limit does NOT bound this — a reader who sees "
       "'1 of 3' would otherwise assume something is counting down",
       "never increments again" in why, why)

    H.run = _canned_run(json.dumps({"stop_gate": {"blocked": False, "attachments": {}}}))
    try:
        blocked, _ = H.stop_gate(post, rec8["worktree"], "some-session-id")
    finally:
        H.run = _orig_run
    ok("a working Crawler is not", blocked is False, blocked)

    H.run = _canned_run("not json at all")
    try:
        blocked, _ = H.stop_gate(post, rec8["worktree"], "some-session-id")
    finally:
        H.run = _orig_run
    ok("a harness with no such seam answers None, NOT False — an older harness's silence is "
       "not evidence that its Crawler is fine, and reading it as such is the same mistake one "
       "layer up", blocked is None, blocked)
    ok("...and so is a Crawler whose session was never recorded, rather than being assumed "
       "healthy", H.stop_gate(post, rec8["worktree"], None)[0] is None)

    # THE WIRING, not just the reader. Everything above proves stop_gate answers correctly and
    # says nothing about whether the two verbs that decide anything ever ask it. A verified
    # diagnosis is not a verified fix.
    gp.add("blocked work", leaf_id="h9", labels=["backend"])
    rec9 = worktree.spawn(post, gp.show("h9"), actor="stuck")
    campaign.record_spawn(post, rec9, pid=os.getpid(), session="a-real-session-id")
    gp.claim("h9", "stuck", pid=os.getpid())
    _orig_sg = campaign_harness_stop_gate = __import__(
        "showrunner.harness", fromlist=["harness"]).stop_gate
    import showrunner.harness as _HH
    _HH.stop_gate = lambda c, w, s: (True, "refused at turn-end by showrunner-stop-gate")
    try:
        f9 = next(f for f in campaign.reconcile(post, gp) if f["crawler"] == rec9["crawler"])
        is_waiting, detail = campaign.waiting(post, gp)
    finally:
        _HH.stop_gate = _orig_sg
    ok("reconcile ranks BLOCKED above LIVE — it IS live, and that is the whole problem",
       f9["verdict"].startswith("BLOCKED"), f9["verdict"])
    ok("...and `waiting` counts it as NEITHER waiting nor parked, so the orchestrator's own "
       "watchdog is free to ring: sitting beside a session that can only be restarted from "
       "outside is not waiting on work you cannot hurry",
       is_waiting is False and not detail["live_crawlers"], (is_waiting, detail))
    ok("...while still REPORTING it, because the Crawler is real and somebody has to go and "
       "prompt it — dropping it would trade one silence for another",
       "h9" in [c["leaf"] for c in detail["blocked_crawlers"]], detail["blocked_crawlers"])

    # RANKING, and it is showrunner's own bug rather than the harness's. A tree may carry more
    # than one harness directory, and the chain of pairwise comparisons that used to pick the
    # verdict let a notes-drifted second harness lose to a clean first one. The milder answer
    # winning is the direction nobody notices, because the output looks like agreement.
    class _TwoHarnesses(object):
        root = ROOT

        def get(self, key, default=None):
            return {"dirs": [".game_loop", ".loop"]} if key == "harness" else default

    def _ranked(canned):
        orig = H._verify_with_harness
        H._verify_with_harness = lambda wt, d: canned[d]
        try:
            return H.check_tree(_TwoHarnesses(), "/does/not/need/to/exist")
        finally:
            H._verify_with_harness = orig

    status, detail, _ = _ranked({".game_loop": (H.CLEAN, {"detail": "the first is clean"}),
                                 ".loop": (H.NOTES_DRIFTED, {"detail": "the second took notes"})})
    eq("a second harness's notes drift is not swallowed by a first one reporting clean",
       status, "notes-drifted")
    ok("...and the detail carried out is the one that raised the verdict, not the one that passed",
       "took notes" in detail, detail)

    status, _, _ = _ranked({".game_loop": (H.DRIFTED, {"detail": "rules differ"}),
                            ".loop": (H.UNDETERMINED, {"detail": "one file was unreadable"})})
    eq("'could not tell' outranks a determined finding — what was proved about the files that "
       "WERE read says nothing about the one that was not", status, "undetermined")

    status, _, mis = _ranked(
        {".game_loop": (H.CLEAN, {"detail": "this one matched"}),
         ".loop": (H.UNDETERMINED, {"detail": "unreadable script",
                                    "false_clean_before_fix": True})})
    ok("a tree that ANY harness would once have called clean is flagged mis-certified, even "
       "with another harness passing — the one that agreed never opened the file in question",
       mis is True and status == "undetermined", (status, mis))
    ok("...and the flag is off by default rather than absent, so a harness that does not "
       "report it cannot read as a confession", _ranked(
           {".game_loop": (H.CLEAN, {"detail": "matched"}),
            ".loop": (H.CLEAN, {"detail": "matched"})})[2] is False)

    off = make_repo(extra_config={"harness": {"provision": "off", "require": False}})
    _seed_harness(off.root)
    a, p, w = H.provision(off, off.root)
    ok("provisioning can be turned OFF, and then does nothing", not (a or p or w), (a, p, w))
    ok("...but doctor still says so out loud", any("OFF" in l for l in H.report(off)), H.report(off))


def test_waiting():
    group("An orchestrator waiting on dispatched work is a FACT, not a heuristic (game_loop#32)")
    if not have("git"):
        skip("the waiting group", "git is not installed")
        return
    cfg = make_repo()
    g = new_graph(cfg)
    is_waiting, detail = campaign.waiting(cfg, g)
    ok("with nothing dispatched, the orchestrator is NOT waiting", is_waiting is False, detail)

    g.add("dispatched", leaf_id="wq1", labels=["backend"])
    rec = worktree.spawn(cfg, g.show("wq1"), actor="live-one")
    campaign.record_spawn(cfg, rec, pid=os.getpid())
    is_waiting, detail = campaign.waiting(cfg, g)
    ok("a LIVE owning pid means waiting — the signal is a real process, not activity",
       is_waiting is True and detail["live_crawlers"], detail)

    dead = make_repo()
    gd = new_graph(dead)
    gd.add("abandoned", leaf_id="wq2", labels=["backend"])
    rec_d = worktree.spawn(dead, gd.show("wq2"), actor="ghost")
    campaign.record_spawn(dead, rec_d, pid=DeadPid().pid)
    is_waiting, detail = campaign.waiting(dead, gd)
    ok("a DEAD Crawler does not count as waiting — a false 'waiting' would silence the "
       "watchdog on exactly the wedged run it exists to catch", is_waiting is False, detail)

    gd.claim("wq2", "ghost", pid=DeadPid().pid)
    gd.park("wq2", "usage limit")
    is_waiting, detail = campaign.waiting(dead, gd)
    ok("an explicitly PARKED Crawler does count — parked is accounted-for, not stalled",
       is_waiting is True and detail["parked_crawlers"], detail)


def test_concurrency():
    group("More than one orchestrator may share this state (real races, not theoretical)")
    if not have("git"):
        skip("the concurrency group", "git is not installed")
        return
    note = __import__("showrunner.util", fromlist=["x"]).concurrency_note()
    if note:
        skip("cross-process state protection", note)

    cfg = make_repo()
    g = new_graph(cfg)
    g.add("contested", leaf_id="race1")
    lib = os.path.join(ROOT, "lib")

    worker = os.path.join(tmpdir("race"), "claim.py")
    with open(worker, "w") as fh:
        fh.write("import sys, os\n"
                 "sys.path.insert(0, %r)\n"
                 "from showrunner import graph as G\n"
                 "from showrunner.util import Refused\n"
                 "g = G.SqliteGraph(%r)\n"
                 "try:\n"
                 "    g.claim('race1', 'a'+sys.argv[1], pid=os.getpid()); print('WON')\n"
                 "except Refused: print('lost')\n"
                 "except Exception as e: print('ERR', type(e).__name__)\n"
                 % (lib, cfg.graph_db))
    procs = [subprocess.Popen([sys.executable, worker, str(i)], stdout=subprocess.PIPE, text=True)
             for i in range(12)]
    outs = [p.communicate()[0].strip() for p in procs]
    eq("exactly ONE of 12 concurrent claims wins the same leaf (check-then-write gave six)",
       outs.count("WON"), 1)
    ok("...and nobody crashes racing for it", "ERR" not in " ".join(outs), outs)

    spawner = os.path.join(tmpdir("race2"), "spawn.py")
    with open(spawner, "w") as fh:
        fh.write("import sys, os\n"
                 "sys.path.insert(0, %r)\n"
                 "from showrunner import config, campaign\n"
                 "cfg = config.load(start=%r)\n"
                 "campaign.record_spawn(cfg, {'crawler':'c'+sys.argv[1],'leaf':'l'+sys.argv[1],"
                 "'branch':'b','worktree':'w','scratch':'s','created_ts':0}, pid=os.getpid())\n"
                 % (lib, cfg.root))
    procs = [subprocess.Popen([sys.executable, spawner, str(i)]) for i in range(10)]
    [p.wait() for p in procs]
    eq("every concurrent spawn survives in the campaign record (read-modify-write lost 7 of 10)",
       len(campaign.load(cfg).get("crawlers", [])), 10)

    # A fleet dividing work is the actual multi-orchestrator use case: `ready` hands the
    # same list to everyone, so claiming the first entry has everyone fighting over one leaf.
    fleet_cfg = make_repo()
    fg = new_graph(fleet_cfg)
    for i in range(8):
        fg.add("leaf %d" % i, leaf_id="L%d" % i)
    nxt = os.path.join(tmpdir("fleet"), "next.py")
    with open(nxt, "w") as fh:
        fh.write("import sys, os\n"
                 "sys.path.insert(0, %r)\n"
                 "from showrunner import graph as G\n"
                 "g = G.SqliteGraph(%r)\n"
                 "l = g.claim_next('a'+sys.argv[1], pid=os.getpid())\n"
                 "print(l['id'] if l else 'NONE')\n" % (lib, fleet_cfg.graph_db))
    procs = [subprocess.Popen([sys.executable, nxt, str(i)], stdout=subprocess.PIPE, text=True)
             for i in range(8)]
    got = [p.communicate()[0].strip() for p in procs]
    claimed = [x for x in got if x != "NONE"]
    eq("8 concurrent orchestrators claim 8 DISTINCT leaves via claim --next", len(set(claimed)), 8)
    ok("...and none of them fails: losing a race means a sibling got there first, which is "
       "the system working", "NONE" not in got, got)
    ok("...and the graph agrees every leaf is spoken for",
       len(fg.ready()) == 0, [l["id"] for l in fg.ready()])

    # Every verdict is logged, because "did this ever silence a watchdog, and for how long"
    # must be a fact before anyone adopts it as a gate.
    wpath = os.path.join(fleet_cfg.state_dir, "waiting.jsonl")
    campaign.waiting(fleet_cfg, fg)
    ok("every waiting verdict is logged, so the evidence a consumer needs accumulates rather "
       "than being asserted", os.path.exists(wpath), wpath)

    empty = fg.claim_next("latecomer", pid=os.getpid())
    ok("a latecomer to a dry graph gets None, not an error", empty is None, empty)

    from showrunner.util import try_file_lock
    lockp = os.path.join(cfg.state_dir, "integrate")
    with try_file_lock(lockp) as first:
        with try_file_lock(lockp) as second:
            ok("integration is exclusive: a second attempt is REFUSED, not queued silently — "
               "two would rewind each other's work", first is True and second is False,
               (first, second))


def test_integration():
    group("Integration: checks on the MERGED result, and resume (issues #9, #14)")
    if not have("git"):
        skip("the whole integration group", "git is not installed")
        return

    marker = os.path.join(tmpdir("integ"), "broken")
    # The check passes unless BOTH files declare the same symbol — two disjoint diffs
    # that still produce a broken trunk, which is the case a per-branch check cannot see.
    check = ("%s -c \"import sys,os; "
             "a=open('src/a.py').read() if os.path.exists('src/a.py') else ''; "
             "b=open('src/b.py').read() if os.path.exists('src/b.py') else ''; "
             "dup = 'register(\\\"x\\\")' in a and 'register(\\\"x\\\")' in b; "
             "print('FAILED duplicate registration') if dup else print('ok'); "
             "sys.exit(1 if dup else 0)\"" % sys.executable)
    cfg = make_repo(files={"README.md": "seed\n", "src/a.py": "# a\n", "src/b.py": "# b\n"},
                    extra_config={"checks": [{"name": "trunk", "cmd": check}]})
    g = new_graph(cfg)
    gates.record_baseline(cfg)

    def crawler(leaf_id, path, content):
        g.add("work on %s" % path, leaf_id=leaf_id, labels=["backend"])
        rec = worktree.spawn(cfg, g.show(leaf_id), actor=leaf_id)
        # A finished Crawler's process has exited — that is the normal case on resume.
        campaign.record_spawn(cfg, rec, pid=DeadPid().pid)
        full = os.path.join(rec["worktree"], path)
        with open(full, "w") as fh:
            fh.write(content)
        sh(["git", "add", "-A"], rec["worktree"])
        sh(["git", "commit", "-q", "-m", "work on %s" % path], rec["worktree"])
        g.claim(leaf_id, leaf_id)
        # The proof must postdate the claim, so it is written now — citing the seed
        # README here is exactly what the freshness check refuses, and correctly.
        proof = os.path.join(cfg.root, "proof-%s.txt" % leaf_id)
        with open(proof, "w") as fh:
            fh.write("checks passed for %s\n" % leaf_id)
        gates.close_gate(cfg, g, leaf_id, os.path.basename(proof), "done", premise="holds",
                         premise_read="README.md")
        return rec

    rec_a = crawler("m1", "src/a.py", "# a\ndef f():\n    pass\n")
    results, ok_ = campaign.integrate(cfg, g, base="main")
    ok("a closed Crawler branch is merged", ok_ and results[0]["status"] == "integrated", results)
    mp = results[0].get("merged_proof")
    ok("...and the checks on the MERGED result are written out as a citable artifact, because "
       "a fix proved on a branch cannot transfer to the integrating session",
       mp and os.path.exists(mp), mp)
    ok("the merged trunk carries the Crawler's work",
       "def f()" in open(os.path.join(cfg.root, "src/a.py")).read())

    findings = campaign.reconcile(cfg, g, base="main")
    ok("reconcile reports the merged Crawler as MERGED",
       any(f["crawler"] == rec_a["crawler"] and f["verdict"].startswith("MERGED") for f in findings),
       findings)

    # Two branches, disjoint files, semantically incompatible.
    rec_b = crawler("m2", "src/a.py", "# a\nregister(\"x\")\n")
    rec_c = crawler("m3", "src/b.py", "# b\nregister(\"x\")\n")
    results, ok_ = campaign.integrate(cfg, g, base="main")
    ok("integration STOPS on the first merged result that fails the checks", ok_ is False, results)
    ok("...naming the branch it stopped on",
       results[-1]["status"] == "checks-failed-on-merged-result", results)
    head_a = open(os.path.join(cfg.root, "src/a.py")).read()
    head_b = open(os.path.join(cfg.root, "src/b.py")).read()
    ok("...and rewinds the failing merge instead of stacking onto a broken trunk",
       not ("register(\"x\")" in head_a and "register(\"x\")" in head_b), (head_a, head_b))

    # A drifted tree must not be merged: whatever its gate certified was answering a
    # different question. This is the most consequential thing check_tree drives and it was
    # untested — found by the mutation sweep, not by reading.
    g.add("drifts after closing", leaf_id="m5", labels=["backend"])
    rec_drift = worktree.spawn(cfg, g.show("m5"), actor="drifter")
    campaign.record_spawn(cfg, rec_drift, pid=DeadPid().pid)
    with open(os.path.join(rec_drift["worktree"], "src/c.py"), "w") as fh:
        fh.write("# c\n")
    sh(["git", "add", "-A"], rec_drift["worktree"])
    sh(["git", "commit", "-q", "-m", "drifter work"], rec_drift["worktree"])
    g.claim("m5", "drifter")
    proof5 = os.path.join(cfg.root, "proof-m5.txt")
    with open(proof5, "w") as fh:
        fh.write("done\n")
    gates.close_gate(cfg, g, "m5", "proof-m5.txt", "done", premise="holds",
                     premise_read="README.md")
    real_check = campaign.harness.check_tree if hasattr(campaign, "harness") else None
    from showrunner import harness as _H
    _orig = _H.check_tree
    _H.check_tree = lambda c, w: ("drifted", "test: this tree enforces different things", False)
    try:
        res_d, ok_d = campaign.integrate(cfg, g, base="main", only=["m5"])
    finally:
        _H.check_tree = _orig
    ok("integrate REFUSES to merge a tree whose rules drifted — whatever its gate certified "
       "was answering a different question", ok_d is False, res_d)
    ok("...and says so by name rather than failing obscurely",
       any(r["status"].startswith("harness-") for r in res_d), res_d)

    # Provenance of an integration commit (#14).
    merged_branch = results[0]["branch"]
    sh(["git", "reset", "--hard", "main"], cfg.root)
    sh(["git", "merge", "--no-ff", "--no-commit", merged_branch], cfg.root, check=False)
    decl = gates.declare_integration(cfg, [{"crawler": "m2", "branch": merged_branch}], base="main")
    ok("an integration commit's provenance is answerable: staged ⊆ what the Crawlers edited",
       decl["ok"] is True, decl)

    stray = os.path.join(cfg.root, "stray.txt")
    with open(stray, "w") as fh:
        fh.write("nobody wrote this\n")
    sh(["git", "add", "stray.txt"], cfg.root)
    decl = gates.declare_integration(cfg, [{"crawler": "m2", "branch": merged_branch}], base="main")
    ok("a file no Crawler ever touched IS flagged — the real orchestration failure",
       decl["ok"] is False and "stray.txt" in decl["unexplained"], decl)
    sh(["git", "reset", "--hard", "main"], cfg.root)

    # Abandoned Crawler on resume.
    dead = DeadPid()
    g.add("abandoned", leaf_id="m4", labels=["backend"])
    rec_d = worktree.spawn(cfg, g.show("m4"), actor="ghost")
    campaign.record_spawn(cfg, rec_d, pid=dead.pid)
    with open(os.path.join(rec_d["worktree"], "unsaved.txt"), "w") as fh:
        fh.write("the only copy of real work\n")
    g.claim("m4", "ghost", pid=dead.pid)
    findings = campaign.reconcile(cfg, g, base="main")
    ghost = next(f for f in findings if f["crawler"] == rec_d["crawler"])
    ok("reconcile identifies an ABANDONED Crawler", ghost["verdict"].startswith("ABANDONED"), ghost)
    ok("...and surfaces its uncommitted work rather than deleting it", bool(ghost["uncommitted"]),
       ghost)
    actions, _ = campaign.reap(cfg, g, base="main", apply=False)
    ok("reap says where the abandoned work is, and does not remove it",
       any(a["kind"] == "crawler" and "not deleted" in a["action"] for a in actions), actions)
    ok("...and the worktree is still on disk after a dry run", os.path.isdir(rec_d["worktree"]))


# ======================================================== CORE: the CLI
def test_publishable():
    group("What a stranger gets when they clone this repo")
    if not have("git"):
        skip("the publishable group", "git is not installed")
        return

    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True).stdout.split()
    ok("the repo has tracked files to check", bool(tracked))

    # A tracked config carrying the author's absolute paths is not a cosmetic leak: an
    # allow_write_roots entry is a RULE, and a stranger inherits a write permission they
    # never chose, aimed at a path that does not exist on their machine.
    offenders = []
    for rel_path in tracked:
        full = os.path.join(ROOT, rel_path)
        try:
            if os.path.getsize(full) > 400_000:
                continue
            with open(full, errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        # Assembled rather than written literally, so this scan still covers its own file
        # instead of quietly exempting the one place the patterns are guaranteed to appear.
        for pat in ("/" + "Users/", "/" + "home/", "/private/" + "tmp/claude-"):
            for line in text.splitlines():
                if pat in line and "example" not in line.lower():
                    offenders.append("%s: %s" % (rel_path, line.strip()[:100]))
    ok("no tracked file hardcodes an absolute home or session path — this repo is public and "
       "a stranger inherits every tracked rule", not offenders, offenders[:6])

    # The overlay is what makes the rule above LIVEABLE. Without somewhere untracked to put a
    # machine-specific path, it goes in the tracked config, and that is precisely how internal
    # tooling reached this repo. A rule with no legitimate alternative gets broken.
    ocfg = make_repo({"project_name": "base", "worktree_root": ".worktrees"})
    with open(os.path.join(ocfg.root, ".showrunner", "config.local.json"), "w") as fh:
        json.dump({"project_name": "overlaid",
                   "dispatch": {"chat": {"cli": "/opt/chat/bin/chat"}}}, fh)
    merged = config.load(ocfg.root)
    eq("an untracked local overlay wins over the tracked config", merged.get("project_name"),
       "overlaid")
    eq("...and carries machine paths the tracked file must never hold",
       (merged.get("dispatch") or {}).get("chat", {}).get("cli"), "/opt/chat/bin/chat")
    eq("...while keys it does not mention are untouched, so an overlay cannot silently drop "
       "half a config", merged.get("worktree_root"), ".worktrees")

    # NOTHING ASSEMBLES A SHELL COMMAND FROM DATA. `util.run` runs a STRING through a shell and
    # a LIST through argv — deliberate, because a configured check is a shell command and a
    # leaf title is not. The hazard is the shape that blurs them: `run("git log %s" % branch)`
    # is one convenience edit away and turns any leaf title, branch name or crawler name into
    # shell. This repo takes leaf titles from issue trackers, so that is attacker-adjacent
    # input. A neighbouring project lost three words out of a message to this exact shape and
    # the command still reported success, which is why the tell is worth asserting.
    #
    # The discrimination matters as much as the rule: `[path] + args` and `["git"] + list(a)`
    # are LIST concatenation and go to argv untouched. A first version flagged every `+` and
    # reported both as injectable — too wide, and noise is how a guard gets ignored before it
    # ever catches anything.
    def builds_a_string(node):
        if isinstance(node, ast.JoinedStr):          # f"..."
            return True
        if not isinstance(node, ast.BinOp):
            return False
        if isinstance(node.op, ast.Mod):             # "..." % x — Mod implies a string
            return True
        if not isinstance(node.op, ast.Add):
            return False
        for side in (node.left, node.right):         # `+` is safe when a list is extended
            if isinstance(side, (ast.List, ast.ListComp)):
                return False
            if isinstance(side, ast.Call) and getattr(side.func, "id", "") == "list":
                return False
        return True

    injectable, examined = [], 0
    for name in ("campaign.py", "cli.py", "collide.py", "dispatch.py", "gates.py",
                 "graph.py", "harness.py", "locks.py", "util.py", "worktree.py"):
        with open(os.path.join(ROOT, "lib", "showrunner", name)) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "run"):
                continue
            if not node.args:
                continue
            examined += 1
            first = node.args[0]
            if builds_a_string(first):
                injectable.append("%s:%d" % (name, node.lineno))
    ok("no run() argument is BUILT by interpolation — a list goes to argv and a bare config "
       "string is an intended shell command, but a formatted string is data becoming code",
       not injectable, injectable)
    # A SCANNER WHOSE MATCHER GOES STALE PASSES IDENTICALLY TO A CLEAN TREE. This one finds
    # calls named `run` in a hardcoded module list: rename the helper, or rename a file, and it
    # examines nothing and reports success. The finding is the absence of hits, so the count of
    # things looked at is the only thing separating "clean" from "did not look".
    ok("...and it examined a real number of run() call sites, so a PASS means they were "
       "checked rather than that the scan matched nothing", examined > 8, examined)
    # Both directions pinned, because this rule is one edit from useless in either. Too wide
    # and it flags every list concat and gets switched off; too narrow and it misses the
    # f-string that turns a leaf title into shell.
    for src, want in [('run(["git"] + list(a))', False),        # list concat — argv
                      ('run([path] + args)', False),            # list concat — argv
                      ('run(chk["cmd"])', False),               # config string — intended
                      ('run("git log %s" % branch)', True),     # data becoming code
                      ('run(f"git log {branch}")', True),       # same, newer syntax
                      ('run("git " + branch)', True)]:          # same, plainest
        call = ast.parse(src).body[0].value
        eq("interpolation rule: %s" % src, builds_a_string(call.args[0]), want)

    # A CHECK THAT FAILS SAFE CANNOT BE OBSERVED WORKING. `model_finding` answers "unknown"
    # when it cannot read the harness's model.json — deliberately, because absence must never
    # read as agreement. That safe direction is also perfectly silent: if the PATH were wrong,
    # every Crawler would report unknown forever and nothing would look broken. A neighbouring
    # project shipped exactly that and it was inert in every real consumer from the moment it
    # landed, with a green suite, because their fixture was the one environment where the path
    # resolved as assumed.
    #
    # My fixtures write model.json themselves, so they prove the reader can read what the test
    # wrote — not that it can read what the HARNESS writes. This is the missing half: the two
    # sides of that path, compared against the installed harness's own source.
    gl_bin = os.path.join(ROOT, ".game_loop", "bin", "game_loop")
    if os.path.exists(gl_bin):
        with open(gl_bin, errors="ignore") as fh:
            gl_src = fh.read()
        ok("the harness still calls its per-session directory `sessions/` — the segment this "
           "repo builds its model.json path from", 'os.path.join(ROOT, "sessions")' in gl_src)
        ok("...and still names the file model.json, so a rename fails HERE loudly instead of "
           "turning every model verdict into a silent `unknown`",
           'MODEL_F = "model.json"' in gl_src)
        with open(os.path.join(ROOT, "lib", "showrunner", "dispatch.py")) as fh:
            disp = fh.read()
        ok("...and this repo builds that same path rather than a remembered one",
           '"sessions"' in disp and '"model.json"' in disp, )

    # THE SWEEP MUST NEVER MUTATE THIS TREE, and prose alone has not held that anywhere it has
    # been tried. Two neighbours lost real time to the alternative: one mutated in place, took
    # a SIGKILL that skipped its restore, and left four broken files in a live tree looking
    # like ordinary work; the other measured mutants against a stale binary and drew three
    # false conclusions in a day, each reporting a real change as having had no effect.
    #
    # The tempting edit is "why copy the whole repo per target, just mutate and restore" — it
    # reads as a speedup and reintroduces both. A comment saying so is what the neighbours had.
    # This is the part that fails.
    with open(os.path.join(ROOT, "test", "mutate.py")) as fh:
        sweep = fh.read()
    eq("the mutation sweep copies the tree for the baseline AND for every target — mutating in "
       "place is one edit away and its failure mode is a broken file left in a live repo by a "
       "kill that skipped the restore", sweep.count("shutil.copytree("), 2)
    eq("...into throwaway directories, so there is no restore step to skip and no window where "
       "this repo holds a deliberately broken file", sweep.count("tempfile.mkdtemp("), 2)
    ok("...and the reason is stated where the edit would happen, because a property nobody "
       "explains is one somebody optimises away",
       "THE COPY IS THE SAFETY PROPERTY" in sweep)

    # REFERENCE COUNTS AS DESIGN FACTS — game_loop's instrument, and it covers the gap the
    # payload stamp cannot reach. A stamp compares prose against a dependency that moved; it is
    # blind inside one file, where a comment, its code and its digest all change together. A
    # reference count asserts something different and cheaper: this name has exactly N
    # legitimate readers, so the N+1th fails LOUDLY and has to be argued for.
    #
    # Both counts below are here because a new reader is how the real bug arrived. `lingering`
    # was a sixth reader of pid_alive that skipped the boot check, and it was the only one
    # wired to a kill.
    src = {}
    for name in ("campaign.py", "dispatch.py", "graph.py", "locks.py", "util.py"):
        with open(os.path.join(ROOT, "lib", "showrunner", name)) as fh:
            src[name] = fh.read()

    kills = []
    for name, body in src.items():
        for i, line in enumerate(body.splitlines(), 1):
            if "os.kill(" in line and "os.kill(int(pid), 0)" not in line:
                kills.append("%s:%d" % (name, i))
    ok("exactly one place sends a real signal to a process — a second way to terminate a "
       "Crawler is a decision, not a refactor, and this repo has already shipped one that "
       "could hit a stranger's pid", len(kills) == 1, kills)

    readers = []
    for name, body in src.items():
        if name == "util.py":
            continue          # defines it
        for i, line in enumerate(body.splitlines(), 1):
            if "pid_alive(" in line and not line.strip().startswith(("#", "from", "import")):
                readers.append("%s:%d" % (name, i))
    # Seven: campaign.live, dispatch.lingering, graph.stale_claims, graph.claim, locks._live,
    # and TWO in reap's terminate block — added when SIGTERM stopped claiming a retirement it
    # had not witnessed. Those two are safe without their own boot check for a reason worth
    # writing down rather than assuming: they run only after `lingering()` returned non-None,
    # and `lingering` refuses across a boot. The pid is known to be this boot before either
    # call is reached, so the audit is inherited THROUGH A GUARD rather than skipped.
    # Every one of them must decide what a pid means ACROSS A BOOT before it trusts the answer.
    # If this number changes, the new reader is the thing to look at — not this assertion.
    eq("pid_alive has exactly the readers that were audited for boot scoping; a new one must "
       "justify itself rather than inherit the audit", len(readers), 7)

    # INTERNAL TOOLING MUST NOT REACH A PUBLIC CLONE. The package manager used to dogfood
    # these repos is ours; a stranger has never heard of it, cannot install it, and should
    # not inherit a path pinned to it. This shipped anyway: a lockfile, a packager marker, and
    # a module that looked for its vendored layout by hardcoded path — so a clone without that
    # tool had a chat feature that could only ever fail. Scanning the tracked set is the check,
    # because the failure is not that the tool is used, it is that the USE became visible.
    internal = []
    for rel_path in tracked:
        full = os.path.join(ROOT, rel_path)
        try:
            if os.path.getsize(full) > 400_000:
                continue
            with open(full, errors="ignore") as fh:
                body = fh.read()
        except OSError:
            continue
        # Assembled so this file's own explanation of the rule does not trip it.
        needle = "l" + "amp"
        # THE PATH COUNTS TOO, and this scan read only contents until a fourth consumer found
        # the gap in their own tree. A lockfile named after the tool contains ZERO occurrences
        # of its name — so tracking one sailed past a guard written to stop exactly that
        # disclosure. It was clean here because the file is untracked, which is the right
        # answer reached without the check having looked.
        #
        # The disclosure that matters is the NAME, not the contents. An earlier version of this
        # comment said the file leaks "the origin git URL of a private repo"; measured, every
        # dependency URL in it is public and only the package manager itself is private. So a
        # cloner learns which private tool this project uses — real, and the reason the human
        # asked for it — while the addresses inside are reachable by anyone. Four of us argued
        # about that disclosure for an afternoon and none checked the visibility that decides
        # whether it is one; `gh repo view --json visibility` answers it in a second.
        if needle in rel_path.lower():
            internal.append("%s: the FILENAME names it (contents need not)" % rel_path)
        for line in body.splitlines():
            low = line.lower()
            if needle in low and "internal" not in low and "example" not in low:
                internal.append("%s: %s" % (rel_path, line.strip()[:90]))
    ok("no tracked file names or pins to the internal package manager — a public clone must "
       "not depend on tooling only we have", not internal, internal[:6])

    harness_cfg = os.path.join(ROOT, ".game_loop", "config.json")
    if os.path.exists(harness_cfg):
        with open(harness_cfg) as fh:
            hc = json.load(fh)
        ok("the tracked harness config grants no write root outside the repo",
           not hc.get("allow_write_roots"), hc.get("allow_write_roots"))

    # THE SUBJECT GREW UNDER THIS CHECK. game_loop's config used to be one per-project file; it
    # now UNIONS three — `~/.game_loop/config.json`, the project's, and config.local.json — so
    # the assertion above answers "what does a stranger inherit from this repo" and is silent
    # about what is actually enforced on this machine. Today those are the same thing because
    # no machine-wide file exists here, and a check that passes for the right reason today
    # passes identically for the wrong one tomorrow. Mirrors the guard's own source list.
    layered = [os.path.expanduser("~/.game_loop/config.json"),
               os.path.join(ROOT, ".game_loop", "config.json"),
               os.path.join(ROOT, ".game_loop", "config.local.json")]
    present, granted = [], []
    for src in layered:
        try:
            with open(src) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        present.append(os.path.basename(src) if not src.startswith(os.path.expanduser("~/.game"))
                       else "~/.game_loop/config.json")
        for root in d.get("allow_write_roots") or []:
            if not os.path.abspath(root).startswith(ROOT):
                granted.append("%s → %s" % (src, root))
    ok("...and so does the EFFECTIVE config — the union game_loop actually reads, not just the "
       "file this repo ships. showrunner's docs promise a Crawler cannot write outside the "
       "repo, and a machine-wide layer can widen that without touching anything tracked",
       not granted, granted)
    ok("...checked against the layers that exist here, so this is a verdict rather than a file "
       "that happened to be missing", bool(present), present)

    sr_cfg = os.path.join(ROOT, ".showrunner", "config.json")
    with open(sr_cfg) as fh:
        sc = json.load(fh)
    ok("showrunner's own config uses no absolute lock_root, so a clone resolves it locally",
       sc.get("lock_root") in (None, ""), sc.get("lock_root"))
    ok("...and its checks are commands a clone can actually run",
       all(not c.get("cmd", "").startswith("/") for c in sc.get("checks", [])),
       sc.get("checks"))


def test_dispatch():
    group("Dispatching a Crawler as a real session (issue #15)")
    if not have("git"):
        skip("the dispatch group", "git is not installed")
        return

    cfg = make_repo({
        "lanes": [
            {"name": "device-work", "lane": "serialized", "resource": "device",
             "match": {"labels": ["device"]}, "model": "opus"},
            {"name": "pure-logic", "lane": "headless", "match": {"labels": ["test"]},
             "model": "sonnet"},
            {"name": "docs-work", "lane": "headless", "match": {"labels": ["docs"]}},
        ],
        "dispatch": {"default_model": "haiku", "models_by_lane": {"serialized": "opus"},
                     "chat": {"enabled": True, "channel_prefix": "sr"}},
    })

    # The bug the exact-match rewrite fixed: two rules share the `headless` lane, and only one
    # of them declares a model. Resolving by lane picks whichever rule is listed first, so
    # docs-work would silently inherit pure-logic's sonnet. A wrong model is not an error
    # downstream — it is a bill — so this is the assertion that has to be exact.
    eq("the model comes from the rule that actually MATCHED, not the first sharing its lane",
       dispatch.resolve_model(cfg, {"rule": "docs-work", "lane": "headless"}), "haiku")
    eq("...and a rule that declares one gets it",
       dispatch.resolve_model(cfg, {"rule": "pure-logic", "lane": "headless"}), "sonnet")
    eq("...a lane default applies when the rule declares nothing",
       dispatch.resolve_model(cfg, {"rule": "unnamed", "lane": "serialized"}), "opus")
    eq("...and the config default is the last resort",
       dispatch.resolve_model(cfg, {"rule": "nope", "lane": "headless"}), "haiku")
    empty = make_repo({"lanes": [], "dispatch": {}})
    ok("...and with nothing declared it inherits rather than inventing a model",
       dispatch.resolve_model(empty, {"rule": "x", "lane": "headless"}) is None)

    rec = {"crawler": "crawler-a", "worktree": ".worktrees/a", "scratch": ".showrunner/scratch/a"}
    cmd = dispatch.build_command(cfg, rec, "sonnet", "abc-123", "THE BRIEF")
    ok("the launch command starts a real session with the brief as its prompt",
       cmd[0] == "claude" and "-p" in cmd and "THE BRIEF" in cmd, cmd)
    ok("...pins the session id showrunner already recorded, so the claim and the process agree",
       "--session-id" in cmd and cmd[cmd.index("--session-id") + 1] == "abc-123", cmd)
    ok("...names the model when one is declared", cmd[cmd.index("--model") + 1] == "sonnet", cmd)
    ok("...and omits --model entirely when none is, rather than passing a made-up default",
       "--model" not in dispatch.build_command(cfg, rec, None, "abc", "b"))
    ok("...and carries a display name, so a fan-out is legible in /resume",
       "-n" in cmd and cmd[cmd.index("-n") + 1] == "crawler-a", cmd)

    ok("two dispatches never share a session id",
       dispatch.new_session_id() != dispatch.new_session_id())

    ok("the chat channel is per-Crawler", dispatch.channel_for(cfg, rec) == "sr_crawler-a")
    ok("...and two Crawlers never share one — a shared room mixes two agents' work and the "
       "orchestrator cannot tell which one answered",
       dispatch.channel_for(cfg, rec) != dispatch.channel_for(cfg, dict(rec, crawler="crawler-b")))
    nochat = make_repo({"dispatch": {"chat": {"enabled": False}}})
    ok("...and is None when chat is switched off, so nothing half-wires",
       dispatch.channel_for(nochat, rec) is None)

    # WHERE the chat tool lives, which the sweep found nothing noticed at all: neuter chat_path
    # to return None and every assertion still passed. None is how this function says "nobody
    # configured one", so a broken resolver and an empty config are the same answer — and the
    # difference matters, because doctor's reciprocal check only fires on a path that RESOLVES
    # and then does not exist. A neighbour who moves their checkout cannot fail this suite;
    # they do not know we point at them. This is the only place it can show.
    def cli_doctor_lines(c):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                           cwd=c.root, capture_output=True, text=True,
                           env=dict(os.environ, NO_COLOR="1"))
        return (p.stdout + p.stderr).splitlines()

    tool = os.path.join(ROOT, "bin", "showrunner")     # any real file will do as a stand-in
    chatty = make_repo({"dispatch": {"chat": {"enabled": True, "cli": tool, "installer": tool}}})
    eq("an ABSOLUTE chat path is returned as given", dispatch.chat_path(chatty, "cli"), tool)
    relative = make_repo({"dispatch": {"chat": {"enabled": True, "cli": "vendor/chat"}}})
    eq("...and a relative one resolves against the repo root, not the caller's cwd",
       dispatch.chat_path(relative, "cli"), os.path.join(relative.root, "vendor", "chat"))
    ok("...while an unconfigured key stays None, which is a different thing from unresolvable",
       dispatch.chat_path(relative, "installer") is None)
    lines = "\n".join(cli_doctor_lines(chatty))
    ok("doctor reports a configured chat path that RESOLVES — the happy path speaks, so a "
       "silent pass cannot be mistaken for a check that ran", "chat cli resolves" in lines, lines)
    gone = make_repo({"dispatch": {"chat": {"enabled": True,
                                            "cli": os.path.join(ROOT, "no", "such", "tool")}}})
    lines = "\n".join(cli_doctor_lines(gone))
    ok("...and ERRORS on one that resolves to nothing, rather than warning it away — every "
       "Crawler would spawn unreachable and only this repo can see it",
       "which does not exist" in lines, lines)

    # game_loop's own warning, asserted rather than trusted: `changed: false` means the model
    # did not MOVE, never that it matched what was asked for — and an absent file means nobody
    # looked. Both must read as UNKNOWN, because an absence that reads as agreement is the
    # exact defect this comparison exists to catch.
    entry = {"session": "s1", "worktree": ".worktrees/a", "model_declared": "sonnet"}
    eq("a session with no model.json is UNKNOWN, never a match",
       dispatch.model_finding(cfg, entry)["verdict"], "unknown")

    sess = os.path.join(cfg.root, ".worktrees", "a", ".game_loop", "sessions", "s1")
    os.makedirs(sess, exist_ok=True)

    def write_model(**kw):
        with open(os.path.join(sess, "model.json"), "w") as fh:
            json.dump(kw, fh)

    # `--model` takes an ALIAS; the transcript records the full id. Comparing them as strings
    # calls every correctly-dispatched Crawler a mismatch, which is the false-positive
    # direction — the one that gets a check ignored rather than costing four seconds.
    ok("the alias you dispatch with matches the full id that gets recorded",
       dispatch.models_agree("sonnet", "claude-sonnet-5"))
    ok("...a full id declared verbatim still matches",
       dispatch.models_agree("claude-opus-5", "claude-opus-5"))
    ok("...and the match is not so loose that a different model passes",
       not dispatch.models_agree("opus", "claude-sonnet-5"))
    ok("...nor does a prefix collision slip through",
       not dispatch.models_agree("sonnet", "claude-sonnet2-5"))

    write_model(models=["claude-sonnet-5"], changed=False)
    eq("a session that ran what it was dispatched as reports match",
       dispatch.model_finding(cfg, entry)["verdict"], "match")
    write_model(models=["claude-opus-5"], changed=False)
    eq("a session that ran something else is a MISMATCH — an Opus-priced Crawler doing Sonnet "
       "work produces fine output, which is why nothing else notices",
       dispatch.model_finding(cfg, entry)["verdict"], "mismatch")
    write_model(models=["claude-sonnet-5", "claude-haiku-4-5"], changed=True)
    eq("a model that changed UNDER the run is caught even though it started correct — "
       "--fallback-model degrades mid-run, silently, exactly when every Crawler hits the cap",
       dispatch.model_finding(cfg, entry)["verdict"], "changed-mid-run")

    # A LIVE PID IS NOT A WORKING AGENT. Measured: a dispatched Crawler printed "Execution
    # error", stopped, and kept its process, and reconcile called it running/alive — correct
    # about the PID and silent about the work. Liveness stays PID-based on purpose; this is a
    # second fact beside it, so the two states stop being indistinguishable.
    hcfg = make_repo({})
    hentry = {"scratch": ".showrunner/scratch/h", "session": "s"}
    ok("no log at all reports NOTHING, not health — an absence is not a verdict",
       dispatch.session_health(hcfg, hentry) is None)
    hdir = os.path.join(hcfg.root, ".showrunner", "scratch", "h")
    os.makedirs(hdir, exist_ok=True)
    hlog = os.path.join(hdir, "session.log")
    open(hlog, "w").close()
    eq("an empty log is QUIET — started, said nothing yet",
       dispatch.session_health(hcfg, hentry)["verdict"], "quiet")
    with open(hlog, "w") as fh:
        fh.write("working on it\nwrote a file\n")
    eq("output that is not an error is PRODUCING",
       dispatch.session_health(hcfg, hentry)["verdict"], "producing")
    with open(hlog, "w") as fh:
        fh.write("Execution error")
    h = dispatch.session_health(hcfg, hentry)
    eq("...and an errored session is ERRORED even though its process is still alive",
       h["verdict"], "errored")
    ok("...and it names what it matched, so the verdict can be argued with",
       h["errors"] == ["Execution error"], h)

    # SPIN-DOWN. The dangerous half is knowing when NOT to stop something. A Crawler closes
    # its own leaf from inside its own session, so at that instant it is mid-call — writing
    # its last commit, running its Stop gate. Terminating there truncates the work it just
    # certified, and the tell is that the record looks finished while the process is busiest.
    import os as _os
    mypid = _os.getpid()
    ok("a live process with no finish time is never lingering — it is just working",
       dispatch.lingering({"pid": mypid}) is None)
    ok("...and one that finished a moment ago is protected by the grace window, which is the "
       "assertion that stops this from killing a Crawler mid-commit",
       dispatch.lingering({"pid": mypid, "finished_at": time.time()}) is None)
    old = time.time() - (dispatch.LINGER_GRACE_SECONDS + 60)
    ling = dispatch.lingering({"pid": mypid, "finished_at": old})
    ok("...but one still alive long after its leaf closed IS lingering, which is what stacks "
       "up under repeated fan-out", ling and ling["pid"] == mypid, ling)
    ok("...and a dead process is not lingering, so a reaped Crawler is not reported twice",
       dispatch.lingering({"pid": 999999, "finished_at": old}) is None)
    # A PID names a process only inside the boot that issued it. Across a reboot the number is
    # reused, so `pid_alive` answers a question about a stranger — and unlike the usual
    # cross-namespace error this one is a false POSITIVE wired to a SIGTERM.
    from showrunner.util import boot_token as _bt
    ok("a pid from a PREVIOUS boot is never lingering — the number belongs to someone else now, "
       "and this is the one check here that acts rather than reports",
       dispatch.lingering({"pid": mypid, "finished_at": old, "boot": "a-previous-boot"}) is None)
    ok("...while the same pid recorded THIS boot still is, so the guard did not just disable "
       "the feature", dispatch.lingering({"pid": mypid, "finished_at": old, "boot": _bt()}))

    # Closing a room must never be the thing that fails a close.
    ok("a Crawler with no channel closes cleanly rather than erroring",
       dispatch.close_channel(cfg, {"crawler": "x"})[0] is True)

    # Spin-down that stops happening is invisible — the leaf still closes, the report still
    # looks right, and the processes and rooms just accumulate. So assert the record actually
    # MOVES, not merely that close succeeded.
    fcfg = make_repo({})
    campaign.record_spawn(fcfg, {"crawler": "c-fin", "leaf": "L1", "branch": "b",
                                 "worktree": ".worktrees/c-fin",
                                 "scratch": ".showrunner/scratch/c-fin"}, pid=None, session="s")
    campaign.set_state(fcfg, "c-fin", "running", channel="room_c-fin")
    done = campaign.finish(fcfg, "L1")
    ok("closing a leaf spins its Crawler down", [d["crawler"] for d in done] == ["c-fin"], done)
    ent = [c for c in campaign.load(fcfg)["crawlers"] if c["crawler"] == "c-fin"][0]
    eq("...and the record says finished, which is what reap keys on", ent["state"], "finished")
    ok("...and stamps WHEN, because the grace window is measured from it",
       isinstance(ent.get("finished_at"), (int, float)), ent.get("finished_at"))
    ok("...and a room it could not close is recorded as NOT closed rather than assumed shut — "
       "a leaked room that believes it is tidy is the one nobody goes looking for",
       ent.get("channel_closed") is False, ent)
    eq("...and spinning down twice does nothing the second time, so reap is safe to re-run",
       campaign.finish(fcfg, "L1"), [])

    # DONE IS NOT ABANDONED. Observed on a real spin-down: a Crawler that finished, closed its
    # leaf and was cleanly retired came back as "ABANDONED — the work is not integrated",
    # which is false once the close gate has demanded an artifact. Under fan-out every
    # completed Crawler reports that way, and an abandonment notice that fires on success is
    # one a reader learns to skim.
    dcfg2 = make_repo({})
    dg = G.open_graph(dcfg2)
    dl = dg.add("done leaf", labels=["test"], leaf_id="done-leaf")
    dg.claim(dl, "who", pid=999999)
    dg.close(dl, "refuted", "README.md", "proved by spin-down")
    campaign.record_spawn(dcfg2, {"crawler": "c-done", "leaf": dl, "branch": "nope",
                                  "worktree": ".", "scratch": "."}, pid=999999, session="s")
    v = [f for f in campaign.reconcile(dcfg2, dg) if f["crawler"] == "c-done"][0]["verdict"]
    ok("a Crawler whose leaf CLOSED is retired, never abandoned — abandonment has to keep "
       "meaning work that may be lost", v.startswith("RETIRED"), v)

    # Through the consumer, not just the predicate: a finished Crawler whose process outlived
    # the grace window has to be REPORTED by reap, or the detector is correct and unused. Dry
    # run, so nothing is signalled — reap stays a report until --apply, including here.
    campaign.set_state(fcfg, "c-fin", "finished", pid=mypid,
                       finished_at=time.time() - (dispatch.LINGER_GRACE_SECONDS + 60))
    fg = G.open_graph(fcfg)
    racts, _ = campaign.reap(fcfg, fg, apply=False)
    procs = [a for a in racts if a.get("kind") == "process"]
    ok("reap reports a process that outlived its work, so lingering Crawlers cannot pile up "
       "unnoticed across waves", len(procs) == 1 and procs[0]["crawler"] == "c-fin", racts)
    ok("...and says it WOULD terminate rather than having done so — reap is a report until "
       "--apply, and stopping someone's process is exactly the kind of act that must be asked",
       procs and procs[0]["action"].startswith("would"), procs)

    # AN EFFECTOR MUST PROVE IT ACTED. `os.kill` returning without error means the SIGNAL was
    # delivered, not that the process stopped — and recording "retired" off the first is a
    # result nobody observed. A neighbouring project shipped the same shape in a relay that
    # counted messages it never sent. Here the subject declines to die: a child that traps
    # SIGTERM and keeps running must leave the record saying so, and must stay reportable.
    stubborn = subprocess.Popen(
        [sys.executable, "-c",
         "import signal,time\nsignal.signal(signal.SIGTERM, lambda *a: None)\n"
         "while True: time.sleep(0.2)"])
    try:
        campaign.set_state(fcfg, "c-fin", "finished", pid=stubborn.pid,
                           boot=boot_token_for_test(),
                           finished_at=time.time() - (dispatch.LINGER_GRACE_SECONDS + 60))
        acts, warns = campaign.reap(fcfg, fg, apply=True)
        ent2 = [c for c in campaign.load(fcfg)["crawlers"] if c["crawler"] == "c-fin"][0]
        ok("a Crawler that IGNORES SIGTERM is not recorded as retired — the signal landing is "
           "not the process stopping", ent2["state"] != "retired", ent2)
        ok("...and it says so out loud rather than leaving a silent disagreement between the "
           "record and the machine", any("still alive" in w for w in warns), warns)
        ok("...and stays reportable, so the next reap raises it again instead of a record that "
           "claims a retirement nobody witnessed",
           bool(dispatch.lingering(ent2)), ent2)
    finally:
        stubborn.kill()
        stubborn.wait()

    # A dispatch that never started must still be visible. Recording after launching would
    # leave a live agent no record names, which cannot be reaped and cannot be reclaimed.
    out = dispatch.launch(cfg, rec, {"rule": "pure-logic", "lane": "headless"},
                          "brief text", "sess-9", dry_run=True)
    ok("a dry run starts nothing and says so", out["launched"] is False, out)
    ok("...and still reports the model and session it WOULD have used, so the plan is checkable "
       "before it costs anything", out["model"] == "sonnet" and out["session"] == "sess-9", out)
    # A companion for channel_for, which the sweep found THIN: neutering it to return None
    # was noticed by exactly one assertion, so a Crawler silently unreachable by chat would
    # have looked like a working dispatch. This catches the same failure through the path
    # that actually consumes it.
    eq("...and the dispatch carries the chat channel, so a Crawler that cannot be reached is "
       "not mistaken for one nobody has messaged yet", out["channel"], "sr_crawler-a")


def test_filed_issues_15_to_21():
    group("Issues 15-21, filed from a real --launch in a consuming repo")
    if not have("git"):
        skip("the filed-issues group", "git is not installed")
        return
    cfg = make_repo({"dispatch": {"default_model": "sonnet"}})
    g = new_graph(cfg)

    # #15 — the brief named a bare `showrunner`, which does not resolve inside a worktree:
    # .showrunner/ is runtime state and `git worktree add` carries tracked files only.
    leaf = g.show(g.add("a leaf", leaf_id="L15"))
    rec = worktree.spawn(cfg, leaf, actor="c")
    text = brief.build(cfg, leaf, rec)
    ok("the brief names the showrunner binary by ABSOLUTE path — a bare command cannot resolve "
       "from a worktree, and the whole proof-of-done design routes through it",
       os.path.join(cfg.root, ".showrunner", "bin", "showrunner") in text)
    ok("...and no bare `showrunner close` survives anywhere in it",
       "\n    showrunner close" not in text, text[:0])
    # #25 — the brief told every Crawler to announce what it was about to do, and with a
    # turn-end gate that blocks on unanswered messages an announcement is indistinguishable
    # from a question. A 3-leaf wave cost three blocked turn-ends before any Crawler had
    # produced a finding, on the one session whose attention is not parallel.
    chatty_leaf = g.show(g.add("with a room", leaf_id="L25"))
    rec25 = worktree.spawn(cfg, chatty_leaf, actor="c25")
    t25 = brief.build(cfg, chatty_leaf, rec25, chat_channel="sr_c25")
    ok("the brief tells a Crawler NOT to post a start notice — the orchestrator wrote the brief "
       "and already has that content", "not post a start notice" in t25.lower(), t25[:0])
    ok("...while keeping the ask-rather-than-guess property that makes the room worth its cost",
       "refuted" in t25 and "same file" in t25, t25[:0])
    # ABSOLUTE IS HALF THE JOB. #15 made the brief name the binary by full path, and that path
    # is the copy install.sh places — which a DEVELOPMENT checkout never has, because a repo
    # working on itself does not run its own installer. So this repo shipped a brief naming a
    # binary that did not exist, canonical and absolute and dead, for a week. The remedy checks
    # in test_cli read every `showrunner <verb>` and ask whether the VERB is real; not one of
    # them asked whether the thing being invoked was.
    real = brief.sr_bin(config.load(ROOT))
    ok("the binary this repo's OWN briefs name actually exists and runs — not merely that it "
       "was written absolutely", os.access(real, os.X_OK), real)
    ok("...and it is resolved against the filesystem rather than assumed, so an installed copy "
       "and a development checkout both name something real",
       os.path.isabs(real) and os.path.basename(real) == "showrunner", real)
    p = subprocess.run([sys.executable, real, "--version"], cwd=ROOT,
                       capture_output=True, text=True)
    ok("...and RUNNING it works, which is the half a path check does not establish",
       p.returncode == 0, (p.returncode, p.stdout, p.stderr)[:3])

    # #19 — acceptEdits left a Crawler able to edit files and nothing else: every bash call
    # refused for want of a human, so it could not run its harness or close its own leaf.
    cmd = dispatch.build_command(cfg, {"crawler": "c"}, "sonnet", "s", "b")
    eq("a launched Crawler can actually run commands — the narrow permission mode did not "
       "narrow the blast radius, it removed the work",
       cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")

    # #18 — a brief's out-of-scope section was read as a list of targets, so the better the
    # brief the more collision-prone the leaf. 9815 paths for a one-file leaf.
    scoped = {"title": "t", "body": "Edit NOTES.md.\n\n## Out of scope\n\nDo not touch "
                                    "lib/showrunner/graph.py or test/run.py.\n\n## After\n\n"
                                    "Also README.md.\n"}
    txt = collide._text_of(scoped)
    ok("the blast-radius estimator drops an explicitly out-of-scope SECTION",
       "graph.py" not in txt and "test/run.py" not in txt, txt)
    ok("...while keeping what the brief does name, before and after that section",
       "NOTES.md" in txt and "README.md" in txt, txt)

    # #17 — the body IS the brief, and a bad one was permanent: add refused an existing id and
    # the only exit spent the proof-of-done gate on a decision that never happened.
    g.add("bad", leaf_id="L17", body="")
    g.edit("L17", body="the real brief")
    eq("a leaf's body can be corrected rather than closed to escape it",
       g.show("L17")["body"], "the real brief")
    g.claim("L17", "someone", pid=os.getpid())
    raises("...but not once it is claimed — that would rewrite the instructions under a Crawler "
           "already working from them", lambda: g.edit("L17", body="x"), "not open")

    # #20 — the claim's liveness named the shell that ran spawn, gone seconds later, while the
    # session it launched ran for fifteen minutes. reap --apply would release a live leaf.
    g.add("live", leaf_id="L20")
    g.claim("L20", "c", pid=999999)
    g.rebind_claim("L20", os.getpid())
    eq("a claim can be rebound to the process really doing the work", 
       g.show("L20")["claim_pid"], os.getpid())
    ok("...so it is no longer reported stale while its Crawler is alive",
       "L20" not in [l["id"] for l, _ in g.stale_claims()])

    # #21 — `stop-gate` was documented under "The gates, as hooks" with no path into a Crawler,
    # so a launched one could end its session with its leaf still open. This wiring was the fix
    # and it went in untested; the assertions below were added when the harness's turn-end
    # budget turned out to be something showrunner had been overriding.
    tf = os.path.join(rec["worktree"], ".game_loop", "triggers.json")
    os.makedirs(os.path.dirname(tf), exist_ok=True)
    with open(tf, "w") as fh:
        json.dump({"stop": [{"name": "the project's own", "command": "true"}],
                   "commit": [{"name": "unrelated", "command": "true"}]}, fh)
    wired, what = dispatch.wire_stop_gate(cfg, rec)
    with open(tf) as fh:
        triggers = json.load(fh)
    ok("the turn-end gate is wired into the Crawler's own tree", wired, what)
    names = [t["name"] for t in triggers["stop"]]
    ok("...MERGED into the trigger file rather than replacing it — the file is the harness's "
       "and a project may already have attached to that moment",
       "the project's own" in names and "showrunner-stop-gate" in names, triggers)
    ok("...leaving moments showrunner knows nothing about untouched",
       [t["name"] for t in triggers["commit"]] == ["unrelated"], triggers)
    mine = next(t for t in triggers["stop"] if t["name"] == "showrunner-stop-gate")
    ok("...naming the binary absolutely, for the same reason #15 did — the trigger fires "
       "inside the worktree, where a bare `showrunner` resolves to nothing",
       mine["command"].startswith(os.path.join(cfg.root, ".showrunner", "bin", "showrunner")),
       mine)
    ok("...and setting NO timeout, so the harness's own turn-end budget governs. An explicit "
       "value is honoured uncapped there, and this ran with one three times the default the "
       "layer below had chosen — for a trigger that fires on EVERY turn-end and fails open",
       "timeout_sec" not in mine, mine)
    wire_again, _ = dispatch.wire_stop_gate(cfg, rec)
    with open(tf) as fh:
        again = json.load(fh)
    ok("...and wiring twice leaves one gate, not two — a spawn retried is not a Crawler gated "
       "twice per turn", wire_again and
       [t["name"] for t in again["stop"]].count("showrunner-stop-gate") == 1, again)


def test_claims_about_the_layer_below():
    group("Claims about game_loop, and whether they still describe what is installed")
    if not have("git"):
        skip("the cross-layer claim group", "git is not installed")
        return

    version_f = os.path.join(ROOT, ".game_loop", "VERSION")
    if not os.path.exists(version_f):
        skip("the cross-layer claim group", "no .game_loop/VERSION — no harness installed here")
        return
    with open(version_f) as fh:
        release = fh.read().strip()[:8]

    # Keyed to the PAYLOAD, not the release. Keying on the release was measured wrong: it
    # fired twice in one hour, and the second time game_loop had shipped a docs-and-manifest
    # change that touched none of the files these claims cite. A stamp that mostly cries wolf
    # is one somebody deletes — game_loop declined this very mechanism for that reason, and
    # was right. The digest fires when the thing the claims are ABOUT moves, and not before.
    # `.game_loop/bin/` is tracked, so a stranger's clone recomputes the same number.
    digest = hashlib.sha256()
    bindir = os.path.join(ROOT, ".game_loop", "bin")
    payload = sorted(f for f in os.listdir(bindir) if os.path.isfile(os.path.join(bindir, f)))
    for name in payload:
        digest.update(name.encode())
        with open(os.path.join(bindir, name), "rb") as fh:
            digest.update(fh.read())
    installed = digest.hexdigest()[:8]
    ok("the installed harness payload hashes to something these claims can be pinned to",
       len(payload) > 3 and len(installed) == 8, "%d files → %s" % (len(payload), installed))
    ok("...and the release is recorded too, so a human can say WHICH game_loop this was",
       len(release) == 8, release)

    # Prose that states another layer's BEHAVIOUR is a measurement with a shelf life, and it
    # reads exactly the same whether it was verified this morning or against a release nobody
    # runs any more. Two rotted under us in one week: BOUNDARY.md described a commit gate that
    # had since grown a second denial, and every Crawler brief said a variable-built commit
    # path passes SILENTLY — still fluent, still specific, and false the moment game_loop
    # fixed it. Neither could be caught by reading; both were caught by the version moving.
    # So one stamp covers the whole set and this fails when the layer moves under it.
    claim_files = {
        "docs/BOUNDARY.md": "the assumptions list, with file:line citations into the guard",
        "lib/showrunner/brief.py": "what every Crawler is told about the commit gate",
        "lib/showrunner/harness.py": "why a tree carrying no harness must be refused",
        "lib/showrunner/worktree.py": "the per-tree gate the shared-state audit rests on",
        "lib/showrunner/campaign.py": "what a drifted tree's gate is said to owe",
        "lib/showrunner/dispatch.py": "why a Crawler must be a session — hooks, park, transcript",
        "lib/showrunner/cli.py": "doctor's account of what a worktree inherits and when spawn refuses",
        "docs/DESIGN.md": "a retracted claim about the gate, and what replaced it",
        "README.md": "the per-tree gate and the blank-verify.yaml consequence",
        ".gitignore": "tracking .game_loop/ is JUSTIFIED by the per-tree gate holding",
    }
    # Matched the tokens but assert nothing about how game_loop behaves.
    not_claims = {
        ".claude/settings.json": "wiring — registers the guards, describes no behaviour",
        "test/run.py": "showrunner's own blast-radius predictor, and this check itself",
        "install.sh": "the close gate's no-NEW-failures rule — showrunner's, not game_loop's",
        "llms.txt": "`lock guard`, `stop-gate` and the close gate — all showrunner's own",
    }

    for rel_path in claim_files:
        ok("a claim file that no longer exists cannot still be covered: %s" % rel_path,
           os.path.exists(os.path.join(ROOT, rel_path)))

    stamp = None
    with open(os.path.join(ROOT, "docs", "BOUNDARY.md")) as fh:
        m = re.search(r"game_loop-verified:\s*([0-9a-f]{8})", fh.read())
        if m:
            stamp = m.group(1)
    ok("docs/BOUNDARY.md carries the harness payload its claims were verified against",
       stamp is not None)
    ok("...and it is the payload actually installed — when this fails, the %d files above "
       "state game_loop's behaviour as fact and NONE of them has been re-read against the "
       "guards running here" % len(claim_files),
       stamp == installed, "stamped %s, installed %s" % (stamp, installed))

    # The comparison is the whole check, so prove it can fail rather than trusting that it
    # would: a stamp that does not match must be rejected. Without this, a regex that quietly
    # stopped matching would read as a permanent pass.
    ok("...and a stamp naming a different payload is rejected, not waved through",
       "2f51021e" != installed)
    # What this does NOT fire on, stated with the case rather than as a category: a behaviour
    # change that touches no file in .game_loop/bin/ — a different installer, a changed default
    # — moves no byte here and this stays green. That channel is behaviour.json, which game_loop
    # now gates on its own side; seq 1 (verify going from seconds to minutes) reached every
    # Crawler here and would not have moved this digest.
    ok("...and the payload it hashes is the one that actually enforces the claims — the guards, "
       "not the installer that placed them",
       any(f.startswith("guard-") for f in payload), payload)

    tokens = ("guard-writes", "commit gate", "blast radius", "the gate")
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True).stdout.split()
    unclassified = []
    for rel_path in tracked:
        # The vendored payload IS game_loop; it is the source these claims cite, not a claim.
        if rel_path.startswith(".game_loop/"):
            continue
        if rel_path in claim_files or rel_path in not_claims:
            continue
        full = os.path.join(ROOT, rel_path)
        try:
            if os.path.getsize(full) > 400_000:
                continue
            with open(full, errors="ignore") as fh:
                text = fh.read().lower()
        except OSError:
            continue
        if any(t in text for t in tokens) and "game_loop" in text:
            unclassified.append(rel_path)
    # Default-deny: a new file describing the layer below joins the stamped set or is excused
    # in writing. The first version of this net used a narrower token list and missed
    # brief.py — the one file whose claim had actually rotted.
    ok("every tracked file stating game_loop's behaviour is either stamped or excused with a "
       "reason — a new one cannot join silently", not unclassified, unclassified)

    # A NUMBER showrunner branches on, read back from the layer that owns it. The digest above
    # catches a payload move but says nothing about WHAT moved, and this is the case that
    # proved the difference: game_loop shipped a `code-drifted` verdict with no entry in its
    # exit map, so it fell to the default 2. showrunner aborted — the right action — while
    # reporting that the harness could not tell, about a comparison the harness had made and
    # won. A wrong reason is what somebody reads when deciding whether to override.
    from showrunner import harness as H
    binp = os.path.join(ROOT, ".game_loop", "bin", "game_loop")
    payload_text = ""
    exit_map = None
    if os.path.exists(binp):
        with open(binp, errors="ignore") as fh:
            payload_text = fh.read()
        m = re.search(r"WORKTREE_EXIT\s*=\s*(\{.*?\})", payload_text, re.S)
        if m:
            try:
                exit_map = ast.literal_eval(m.group(1))
            except (ValueError, SyntaxError):
                exit_map = None
    if not isinstance(exit_map, dict) or not exit_map:
        skip("the exit-map agreement check",
             "the installed harness declares no readable WORKTREE_EXIT")
    else:
        ok("every code the installed harness can exit with is one showrunner has a meaning "
           "for — an unmapped code reads as 'could not tell', which blocks, and says nothing",
           set(exit_map.values()) <= set(H.CONTRACT_CODES), exit_map)
        ok("...and 'clean' is the only status that exits 0, so nothing that found a difference "
           "can arrive here as a pass",
           sorted(s for s, c in exit_map.items() if c == H.CLEAN) == ["clean"], exit_map)
        ok("...and a drifted harness SCRIPT arrives as a determined finding (1), not as the "
           "default 2 — showrunner blocks on either, but only one of them is true",
           exit_map.get("code-drifted") == H.DRIFTED, exit_map)
        for status, code in sorted(exit_map.items()):
            ok("the harness's '%s' is let past only if it means clean or per-tree notes" % status,
               (code in (H.CLEAN, H.NOTES_DRIFTED)) == (status in ("clean", "notes-drifted")),
               (status, code))
        # The flag is retrospective and nothing else records it, so if game_loop dropped the
        # field showrunner's mis-certified warning would simply stop firing — and a warning
        # that stopped firing reads exactly like a tree that was never mis-certified.
        ok("the retrospective flag showrunner surfaces is one the installed harness still "
           "emits — this is the only thing standing between that warning and going quiet",
           "false_clean_before_fix" in payload_text, binp)

    # THE LINE NUMBERS, which the stamp above does not check. It fires when the payload moves
    # and says so — it never says WHICH citation is now wrong, and the answer has twice been
    # "most of them": one insertion of fifty lines shifted six numbers at once, and every
    # sentence around them stayed fluent and specific. A citation that has slid onto an
    # unrelated line reads exactly like one that was re-read this morning.
    #
    # The anchor is taken from the PROSE rather than from a table kept here, because a table of
    # expected tokens is one more thing to rot beside the thing it is checking. Each bullet
    # already names its subject in backticks — `blast_note`, `EDITED_F`, `config.local.json` —
    # so the rule is: that identifier must appear within a few lines of the number the bullet
    # cites. A bullet naming no identifier is not wrong, it is unanchored, and those are
    # COUNTED rather than skipped in silence.
    impl = os.path.join(ROOT, ".game_loop", "bin", "guard-writes-impl.sh")
    with open(os.path.join(ROOT, "docs", "BOUNDARY.md")) as fh:
        boundary = fh.read()
    impl_lines = []
    if os.path.exists(impl):
        with open(impl, errors="ignore") as fh:
            impl_lines = fh.read().splitlines()
    # Only bullets governed by the vendored guard. install.sh lives in game_loop's own repo and
    # is not vendored here, so its citations cannot be checked from this side at all — named as
    # a hole rather than quietly passing with the rest.
    governed = [b for b in re.split(r"\n- ", boundary)
                if "guard-writes-impl.sh" in b or "blast_note" in b or "EDITED_F" in b]
    def lands_near(num, anchors):
        window = "\n".join(impl_lines[max(0, num - 4):num + 3])
        return any(a in window for a in anchors)

    checked, unanchored, wrong = 0, 0, []
    for bullet in governed:
        anchors = [t for t in re.findall(r"`([A-Za-z_][A-Za-z0-9_.]{2,})`", bullet)
                   if not t.endswith(".md")]
        for num in [int(n) for n in re.findall(r"(?:guard-writes-impl\.sh:|lines? )(\d+)", bullet)]:
            if not anchors:
                unanchored += 1
                continue
            checked += 1
            if not lands_near(num, anchors):
                wrong.append("line %d cites %s, window has none of them" % (num, anchors))
    if not impl_lines:
        skip("the cited-line check", "no vendored guard-writes-impl.sh to read")
    else:
        ok("every line this repo cites into the guard still lands within reach of the thing "
           "the sentence is about — a number that slid is a citation nobody can follow back",
           not wrong, wrong)
        ok("...and it checked citations rather than finding none, which is what a regex that "
           "stopped matching also returns", checked >= 4, (checked, unanchored))
        # The positive control, because the assertion above passes identically whether the rule
        # works or the window is so wide that everything lands in it. Same rule, same anchor,
        # a number known to be wrong.
        real = next((i + 1 for i, l in enumerate(impl_lines) if l.startswith("EDITED_F=")), None)
        ok("the rule finds the anchor at the line that really holds it", real and
           lands_near(real, ["EDITED_F"]), real)
        ok("...and REPORTS A MISS when pointed somewhere else — without this, a window that "
           "matched everything would read exactly like citations that were all correct",
           real and not lands_near(real + 200, ["EDITED_F"]), real)


def test_retracted_doc_claims():
    group("Claims the docs used to make that are now false")
    # A DOCUMENTATION PASS FOUND FOUR, and none of them could have been caught by reading the
    # docs — every one was internally coherent and specific. They were caught by reading the
    # source beside them. The README described showrunner copying the hook-registration file
    # (the one thing harness.py says never to do), comparing rule files itself (the hardcoded
    # list that was deleted), and chat being optional; both docs said `waiting` exits 0 while
    # dispatched work has a live owner, which stopped being true the day a blocked Crawler
    # became a thing this can see.
    #
    # WHAT THIS CATCHES AND WHAT IT DOES NOT. It catches a retracted claim COMING BACK — from a
    # revert, a copy-paste out of git history, or a rewrite by someone who read the old version.
    # It cannot catch a NEW wrong claim; nothing here reads a sentence and judges it. That is
    # the same limit as the layer-below stamp and it is worth stating rather than implying,
    # because a green run here means "the four known corpses are still buried", never "the docs
    # are true".
    #
    # Each entry carries the SOURCE FACT that makes it false, asserted in the same breath — so
    # if the code ever changes back, this fails as a stale guard rather than silently forbidding
    # a sentence that has become true again.
    retracted = [
        ("copies the hook registration",
         "lib/showrunner/harness.py", "Never copy the hook-registration file",
         "the installer MERGES hooks; a wholesale copy discards the project's own settings"),
        ("minus the conversation",
         "lib/showrunner/dispatch.py", "def wire_stop_gate",
         "a wired turn-end gate means a refused Crawler needs a message to restart, so the "
         "room is load-bearing for correctness under --launch"),
        ("exits 0 while dispatched work has a live owner\n",
         "lib/showrunner/campaign.py", "blocked_crawlers",
         "a blocked Crawler IS a live owner and now counts as neither waiting nor parked"),
        ("compares every\nrule file **byte-for-byte** against the main checkout",
         "lib/showrunner/harness.py", "showrunner keeps no list of its own",
         "the harness answers which files are rules; showrunner asks and never compares"),
    ]
    # WHITESPACE-NORMALISED, because the first version of this scan was VACUOUS and passed. The
    # docs wrap at 96 columns, so "minus the conversation" is stored as "minus the\nconversation"
    # and a substring search for the sentence found nothing — in a README that quotes it three
    # lines below. A scan whose pattern does not match the format its subject is written in
    # returns "clean" and "never looked" as the same answer, and the tell was that the result
    # contradicted something already seen directly.
    def flat(text):
        return " ".join(text.split())

    docs = {}
    for rel_path in ("README.md", "llms.txt", "docs/DESIGN.md", "docs/BOUNDARY.md"):
        full = os.path.join(ROOT, rel_path)
        if os.path.exists(full):
            with open(full, errors="ignore") as fh:
                docs[rel_path] = flat(fh.read())
    ok("there are docs to check, so a pass here is not a pass over nothing", len(docs) >= 3, list(docs))

    # QUOTING A RETRACTED CLAIM IS NOT MAKING IT. Naming what a doc used to say, next to why it
    # was wrong, is the practice that makes these findable at all — so an occurrence is only a
    # failure when nothing near it marks it as retracted.
    MARKERS = ("was wrong", "no longer", "used to", "stopped being true", "is not true",
               "and that was", "until ")
    for phrase, src, anchor, why in retracted:
        with open(os.path.join(ROOT, src), errors="ignore") as fh:
            source = fh.read()
        ok("the source still says what makes '%s' false — %s" % (flat(phrase)[:40], why),
           anchor in source, (src, anchor))
        needle = flat(phrase)
        asserted = []
        for doc, text in docs.items():
            at = text.find(needle)
            while at != -1:
                window = text[max(0, at - 240):at + len(needle) + 240]
                if not any(m in window for m in MARKERS):
                    asserted.append("%s @%d" % (doc, at))
                at = text.find(needle, at + 1)
        ok("...and no tracked doc asserts it again (quoting it as retracted is fine)",
           not asserted, asserted)

    # THE POSITIVE CONTROL, and it has to use a RETRACTED phrase rather than any old word: the
    # question is whether this scan can find one of these sentences in the docs as they are
    # actually written, which is precisely what it could not do before.
    quoted = [p for p, _, _, _ in retracted if any(flat(p) in t for t in docs.values())]
    ok("the scan finds a retracted phrase that IS present — so 'no doc asserts it' means "
       "checked, not that the pattern stopped matching the way the docs wrap",
       quoted, [flat(p)[:40] for p, _, _, _ in retracted])


def test_cli():
    group("CLI surface")
    exe = os.path.join(ROOT, "bin", "showrunner")
    p = subprocess.run([sys.executable, exe, "--version"], capture_output=True, text=True)
    ok("bin/showrunner runs from a clone with no PYTHONPATH and no install", p.returncode == 0,
       p.stderr)

    cfg = make_repo()
    env = dict(os.environ, NO_COLOR="1")
    p = subprocess.run([sys.executable, exe, "doctor"], cwd=cfg.root, capture_output=True,
                       text=True, env=env)
    ok("`doctor` exits 0 on a valid config", p.returncode == 0, p.stdout + p.stderr)

    bad = make_repo()
    bad.data["worktree_root"] = tmpdir("outside")
    config.write(bad)
    p = subprocess.run([sys.executable, exe, "doctor"], cwd=bad.root, capture_output=True,
                       text=True, env=env)
    ok("`doctor` exits non-zero on a config that would degrade silently", p.returncode != 0,
       p.stdout)

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        locks.LockSet(cfg).lock("device").acquire(holder.pid, "someone else")
        p = subprocess.run([sys.executable, exe, "lock", "guard", "--", "frontend deploy"],
                           cwd=cfg.root, capture_output=True, text=True, env=env)
        eq("`lock guard` exits 2 (the PreToolUse deny code) when the resource is held",
           p.returncode, 2)
        p = subprocess.run([sys.executable, exe, "lock", "guard", "--", "echo hello"],
                           cwd=cfg.root, capture_output=True, text=True, env=env)
        eq("`lock guard` exits 0 for a command matching no resource", p.returncode, 0)
        ok("...and prints WHY it allowed — exit 0 alone is what an absent guard also returns",
           "matches no single-consumer resource" in p.stdout, p.stdout)
    finally:
        holder.terminate()
        holder.wait()
        locks.LockSet(cfg).lock("device").release(force=True)

    g = new_graph(cfg)
    g.add("open work", leaf_id="cli1")
    # tree= is what production records: the claiming process is in the repo it orchestrates.
    # This fixture claimed from the showrunner checkout while operating on a temp repo, which
    # made the two sides of the gate's scope comparison genuinely different trees.
    g.claim("cli1", "someone", tree=cfg.root)
    p = subprocess.run([sys.executable, exe, "stop-gate"], cwd=cfg.root, capture_output=True,
                       text=True, env=env)
    eq("`stop-gate` exits 2 while a claimed leaf is open", p.returncode, 2)
    # #27 — the gate asked "is ANY leaf open in this campaign" and spawn writes it into EVERY
    # Crawler's triggers, so each was gated on its siblings: with N dispatched, N-1 refused at
    # least once, and a headless Crawler has no next turn in which to act on a refusal.
    g.add("a sibling's work", leaf_id="cli2")
    g.claim("cli2", "sibling", tree=os.path.join(cfg.root, ".worktrees", "sibling"))
    p = subprocess.run([sys.executable, exe, "stop-gate", "--leaf", "cli1"], cwd=cfg.root,
                       capture_output=True, text=True, env=env)
    eq("...still 2 for the caller that HOLDS the open leaf", p.returncode, 2)
    p = subprocess.run([sys.executable, exe, "stop-gate", "--leaf", "cli2"], cwd=cfg.root,
                       capture_output=True, text=True, env=env)
    eq("...and 2 for the sibling about ITS own leaf", p.returncode, 2)
    g.close("cli2", "closed", "README.md", "the sibling finished")
    p = subprocess.run([sys.executable, exe, "stop-gate", "--leaf", "cli2"], cwd=cfg.root,
                       capture_output=True, text=True, env=env)
    eq("a Crawler whose OWN leaf is closed passes while a sibling's is still open — the case "
       "that sent three Crawlers inert in one afternoon", p.returncode, 0)
    ok("...and says whose the open ones are, rather than telling this caller to close work it "
       "cannot reach", "NOT yours" in (p.stdout + p.stderr), p.stdout + p.stderr)

    # A REFUSAL MUST BE VISIBLE ON THE STREAM AGENTS FILTER. Every consumer of this CLI is an
    # agent, the Crawler brief tells them to run `showrunner close ...`, and agents pipe stdout
    # aggressively to protect context. A refusal that lives only on stderr is, to
    # `... | grep -i closed`, indistinguishable from a command that did nothing and succeeded —
    # a neighbouring project spent an afternoon reporting a working tool as broken for exactly
    # that reason, three times in one day, each through a different filter.
    p = subprocess.run([sys.executable, exe, "close", "no-such-leaf", "--proof", "README.md"],
                       cwd=cfg.root, capture_output=True, text=True, env=env)
    ok("a refusal exits non-zero", p.returncode != 0, p.returncode)
    ok("...and puts a marker on STDOUT, so a filter cannot read a refusal as silence",
       "REFUSED" in p.stdout, {"stdout": p.stdout, "stderr": p.stderr[:80]})
    ok("...while the REASON stays on stderr rather than being duplicated onto both",
       "no such leaf" in p.stderr and "no such leaf" not in p.stdout,
       {"stdout": p.stdout, "stderr": p.stderr[:80]})

    # A REMEDY IS A CLAIM THAT A COMMAND EXISTS, and nothing here was checking it. The usual
    # docs check runs the other way — every verb the CLI defines must be documented — which
    # cannot notice a printed instruction naming a verb that was renamed or never existed.
    # Found live: the br backend's stale-claims refusal told the reader to run `showrunner
    # campaign`, which argparse rejects outright. That string fires only when someone is
    # ALREADY blocked, so the one path that reaches it is the one that hands out a dead end.
    p = subprocess.run([sys.executable, exe, "--help"], capture_output=True, text=True)
    m = re.search(r"\{([a-z0-9,\-]+)\}", p.stdout)
    verbs = set(m.group(1).split(",")) if m else set()
    ok("the CLI's own verb list is readable, so this check has something to compare against",
       len(verbs) > 10, sorted(verbs))

    # Command POSITION, not a vocabulary of prose words: a denylist of English would have to
    # grow every time someone writes "showrunner ships" in a sentence, and a list that grows
    # with the language is a list that will be wrong. A remedy is written in backticks or in
    # a fenced block; ordinary prose is not. That discriminator stays fixed as the docs grow.
    # Three command positions. Backticks mean the same thing everywhere. A fenced block is
    # markdown. An INDENTED line is a markdown code block — and that one is only safe because
    # what it is applied to here is markdown: brief.py is a Python file whose strings are the
    # markdown document handed to every Crawler. llm_chat hit the false positive this invites,
    # applying the same indent rule to Python docstring PROSE and reading `llm_chat instead`
    # out of "a consumer vendored llm_chat instead of pointing at a sibling clone". A
    # positional rule stays fixed only while it means the same thing in what it is read over.
    def commands_in(text):
        spans = re.findall(r"`([^`\n]+)`", text)
        for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
            spans.extend(block.splitlines())
        spans.extend(re.findall(r"^ {4,}(showrunner [a-z].*)$", text, re.M))
        for span in spans:
            m = re.match(r"\s*showrunner ([a-z][a-z-]+)((?: [a-z][a-z-]+)?)", span)
            if m:
                yield m.group(1), m.group(2).strip()

    # Only the FIRST word was ever checked. `lock` has five subcommands and eight places print
    # `lock run` or `lock guard`; rename `run` and all eight remedies go dead while this stays
    # green. Same denominator as everywhere else — the check was not wrong, it stopped early.
    # Resolved from argparse rather than parsed out of the source: llm_chat's version regexed
    # `add_parser("literal")`, missed two verbs registered through a loop variable, and then
    # reported two REAL commands as ghosts. A parser that misreads the source is confidently
    # wrong in whichever direction you point it, so ask the thing that knows.
    subverb_cache = {}

    def subverbs_of(verb):
        # A `choices=` positional renders EXACTLY like a subparser group, so reading the first
        # {...} in the help text conflates them: `add` offers {task,epic} and `close` offers
        # {holds,partial,...} as FLAG values, and treating those as subcommands reports
        # `showrunner close mytask` — a valid command — as a ghost. That is the direction that
        # costs trust in the check rather than four seconds. Flag choices are always attached
        # to their flag; a subparser group never is. (llm_chat hit this shape and read the true
        # warning as a false positive, one keystroke from suppressing it.)
        if verb not in subverb_cache:
            out = subprocess.run([sys.executable, exe, verb, "--help"],
                                 capture_output=True, text=True).stdout
            out = re.sub(r"--?[a-z][a-z-]* \{[a-z0-9,\-]+\}", "", out)
            found = re.search(r"\{([a-z0-9,\-]+)\}", out)
            subverb_cache[verb] = set(found.group(1).split(",")) if found else set()
        return subverb_cache[verb]

    scanned = ["README.md", "llms.txt"] + sorted(
        os.path.join("lib", "showrunner", f)
        for f in os.listdir(os.path.join(ROOT, "lib", "showrunner")) if f.endswith(".py"))
    dead, seen = [], 0
    for rel_path in scanned:
        try:
            with open(os.path.join(ROOT, rel_path), errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for word, sub in commands_in(text):
            seen += 1
            if word not in verbs:
                dead.append("%s: showrunner %s" % (rel_path, word))
            elif sub and subverbs_of(word) and sub not in subverbs_of(word):
                dead.append("%s: showrunner %s %s" % (rel_path, word, sub))
    ok("every `showrunner <verb>` this repo prints or documents is a verb the CLI actually "
       "accepts — a remedy naming a command that does not exist is worse than no remedy",
       not dead, sorted(set(dead)))
    ok("...including the SUBcommand, for the verbs that have them — `lock run` is eight "
       "remedies deep here and only its first word was ever checked",
       "run" in subverbs_of("lock") and "guard" in subverbs_of("lock"),
       sorted(subverbs_of("lock")))
    # Pure observation passes by finding nothing, which is also what a broken regex returns.
    ok("...and it found commands to check at all, so a PASS means they were verified rather "
       "than that the scan matched nothing", seen > 5, seen)
    ok("...and a verb the CLI rejects is caught rather than waved through",
       "campaign" not in verbs and bool(list(commands_in("run `showrunner campaign` now"))))

    # The DENOMINATOR. Everything above only checks commands the positional rule can SEE, so
    # a remedy written in a position it does not recognise is not wrong — it is absent, and
    # absence reads here exactly like correctness. Six were, when this was written: three of
    # them in the Crawler brief, the highest-traffic remedy text this repo ships. Only real
    # verbs are flagged, so prose ("showrunner is generic") cannot make noise, and the day one
    # is renamed the string becomes a dead command the rule can already catch.
    invisible = []
    for rel_path in scanned:
        if not rel_path.endswith(".py"):
            continue
        with open(os.path.join(ROOT, rel_path)) as fh:
            src = fh.read()
        visible = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                seen_here = set(w for w, _ in commands_in(node.value))
                for m in re.finditer(r"showrunner ([a-z][a-z-]+)", node.value):
                    if m.group(1) not in verbs:
                        continue          # prose, not a command
                    if m.group(1) in seen_here or m.group(1) in visible:
                        continue
                    invisible.append("%s: showrunner %s" % (rel_path, m.group(1)))
                visible.update(seen_here)
    ok("every real verb printed inside a source string sits in a position the scan can see — "
       "a remedy the checker cannot read is not checked, and looks identical to a passing one",
       not invisible, sorted(set(invisible)))

    # BOTH FIXES ABOVE REDUCED FINDINGS, which is the direction that ends in a check that has
    # quietly stopped discriminating — and a suite that goes green right after you tune it is
    # the moment to trust it least. So: one fixture holding all four cases at once, asserting
    # the rule separates them rather than that the repo happens to be clean today. (llm_chat's
    # practice, adopted after two of its own fixes each cut the finding count.)
    fixture = (
        "run `showrunner campaign` to see it\n"          # ghost: no such verb
        "run `showrunner lock guard -- cmd` first\n"     # real, with a real subcommand
        "run `showrunner lock sprint -- cmd` first\n"    # real verb, ghost subcommand
        "run `showrunner close mytask --proof p` now\n"  # real verb + ARGUMENT, not a subverb
        "showrunner spawns a Crawler per leaf, and showrunner is generic\n")  # prose
    flagged = set()
    for word, sub in commands_in(fixture):
        if word not in verbs:
            flagged.add(word)
        elif sub and subverbs_of(word) and sub not in subverbs_of(word):
            flagged.add("%s %s" % (word, sub))
    eq("the rule separates a ghost verb, a ghost subcommand, a real command, a real command "
       "with an argument, and prose — one fixture, so a clean repo cannot be mistaken for a "
       "working check", flagged, {"campaign", "lock sprint"})
    ok("...and `close`'s flag VALUES are not mistaken for subcommands, which would report a "
       "valid command as dead", not subverbs_of("close"), sorted(subverbs_of("close")))

    # Pinned to what argparse ACTUALLY emits, by building real parsers and asking them — not
    # to this CLI's current help text, which covered the bracketed form only by accident of
    # `close` also printing a bare option line. llm_chat implemented this same discriminator
    # requiring whitespace before the flag, and argparse writes an optional as `[--to {a,b}]`,
    # so the bracket blocked the match and the guard never fired on the shape it exists for.
    # Their first fixtures were hand-written and omitted the `options:` section and the `[-h]`
    # that real argparse emits — a test measuring its author's memory of a format, which is
    # right or wrong by luck and teaches nothing either way. So: no invented help text here.
    strip = lambda out: re.sub(r"--?[a-z][a-z-]* \{[a-z0-9,\-]+\}", "", out)

    flag_p = argparse.ArgumentParser(prog="x")
    flag_p.add_argument("--to", choices=["a", "b"])
    flag_p.add_argument("-k", choices=["task", "epic"])
    sub_p = argparse.ArgumentParser(prog="x")
    _s = sub_p.add_subparsers()
    _s.add_parser("status")
    _s.add_parser("run")

    flag_help, sub_help = flag_p.format_help(), sub_p.format_help()
    ok("the flag fixture is real argparse output, carrying both renderings it emits — "
       "`[--to {a,b}]` in usage and `--to {a,b}` in the options list",
       "[--to {a,b}]" in flag_help and "\n  --to {a,b}" in flag_help, flag_help)
    ok("flag choices in both renderings are stripped, so they cannot be read as subcommands",
       not re.search(r"\{[a-z0-9,\-]+\}", strip(flag_help)), strip(flag_help))
    # The ONLY assertion here that fails if the rule starts removing everything. Three
    # assertions that things get stripped are all satisfied by a regex that strips the lot.
    left = re.search(r"\{([a-z0-9,\-]+)\}", strip(sub_help))
    eq("...and a real subparser group SURVIVES the strip",
       left.group(1) if left else None, "status,run")
    # A passing test and a load-bearing test are different claims: re-introduce llm_chat's
    # exact bug and confirm this notices, rather than inferring it from a green run.
    naive = re.sub(r"\s--?[a-z][a-z-]* \{[a-z0-9,\-]+\}", "", flag_help)
    ok("...and the whitespace-requiring version this replaces is CAUGHT, so these assertions "
       "are load-bearing rather than merely green",
       bool(re.search(r"\{[a-z0-9,\-]+\}", naive)), naive)

    # Everything above reads ONE string literal at a time, so a remedy assembled from f-string
    # pieces or concatenation puts the verb in a different AST node from the rest and no
    # amount of reading either node matches it. llm_chat shipped exactly that and its clean
    # validator run WAS the blind spot rather than the absence of one. There are none here
    # today — so rather than write that down as a caveat that ages, this keeps it true: a
    # remedy split across nodes fails the suite instead of quietly leaving the check's range.
    # The verb itself can come from an expression, so it appears in no literal anywhere and
    # commands_in cannot see it. But POSITION still decides, exactly as it does everywhere
    # else in this check: matching the name followed by an interpolation flagged
    # `f"showrunner {n} leaves are ready"` — an ordinary sentence — as a split remedy. That was
    # the too-wide error, written into the fix for the too-narrow one an hour earlier. llm_chat
    # made the identical mistake in the same hour, in the one function it had built to be its
    # honest instrument, having applied position correctly in two other places in that file.
    def dynamic_command(txt):
        for m in re.finditer(r"showrunner (\{|['\"]\s*\+)", txt):
            before = txt[txt.rfind("\n", 0, m.start()) + 1:m.start()]
            if "`" in before or re.match(r"^ {4,}$", before):
                return True
        return False

    assembled = []
    def _string_assembly(node):
        """A `+` that joins TEXT. `[finding] + list(rest)` is a list concatenation and no more
        a split remedy than a list is a sentence — but it unparses to source containing the
        remedy, so a rule keyed on the operator alone flags it. This guard has now produced
        that false positive twice, in two different forms, which is what makes it a shape
        rather than a slip: the operator is not the subject, the operand type is."""
        if isinstance(node, ast.JoinedStr):
            return True
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
            return False
        def texty(side):
            if isinstance(side, ast.Constant):
                return isinstance(side.value, str)
            if isinstance(side, ast.JoinedStr):
                return True
            if isinstance(side, ast.BinOp):
                return _string_assembly(side)
            return False   # a call, a name, a list — unknown, and not evidence of text
        return texty(node.left) or texty(node.right)

    for rel_path in scanned:
        if not rel_path.endswith(".py"):
            continue
        with open(os.path.join(ROOT, rel_path)) as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if _string_assembly(node):
                try:
                    txt = ast.unparse(node)
                except Exception:
                    continue
                # The SUBJECT is a remedy, not any mention of the tool. Matching on the name
                # alone flagged `f"showrunner is generic, at {path}"` — prose, failing the
                # build with a message about remedies. Latent only because no such f-string
                # exists yet, which is how the `choices=` false positive hid too: a count of
                # zero says nothing when it is a count of the wrong thing.
                if dynamic_command(txt) or any(v in verbs for v, _ in commands_in(txt)):
                    assembled.append("%s:%d" % (rel_path, node.lineno))
    # EXECUTING one, which is a different question from every check above. Those confirm a
    # printed remedy names a verb the CLI accepts; none of them confirms that RUNNING it leaves
    # the person unstuck. A remedy can name a real command, exit 0, and return you to the same
    # refusal — a symptom fixed while the cause stands. So this takes the one refusal every new
    # user meets first, runs exactly what it prints, and requires the original command to work
    # afterwards. Only one remedy is executed here; the rest remain checked for existence alone,
    # which is stated rather than implied because a sweep that names its own reach is the only
    # kind that cannot be mistaken for a complete one.
    bare = tempfile.mkdtemp(prefix="sr-remedy-")
    try:
        sh(["git", "init", "-q"], bare)
        sh(["git", "config", "user.email", "t@t"], bare)
        sh(["git", "config", "user.name", "t"], bare)
        first = subprocess.run([sys.executable, exe, "doctor"], cwd=bare,
                               capture_output=True, text=True, env=env)
        printed = first.stdout + first.stderr
        ok("a repo with no config refuses rather than inventing defaults",
           first.returncode != 0, printed[:200])
        m = re.search(r"`showrunner ([a-z\-]+)`", printed)
        ok("...and the refusal prints a remedy to run", m is not None, printed[:200])
        if m:
            fix = subprocess.run([sys.executable, exe, m.group(1)], cwd=bare,
                                 capture_output=True, text=True, env=env)
            eq("...the remedy it prints actually runs (`%s`)" % m.group(1), fix.returncode, 0)
            after = subprocess.run([sys.executable, exe, "doctor"], cwd=bare,
                                   capture_output=True, text=True, env=env)
            ok("...and running it leaves the person UNSTUCK — the command that refused now "
               "works, which is the half that a remedy naming a real verb does not establish",
               after.returncode == 0, (after.stdout + after.stderr)[-400:])
            ok("...and the same refusal does not simply reappear",
               "run `showrunner %s`" % m.group(1) not in (after.stdout + after.stderr),
               (after.stdout + after.stderr)[-200:])
    finally:
        shutil.rmtree(bare, ignore_errors=True)

    ok("no remedy is assembled across AST nodes — the scan reads one literal at a time, so a "
       "command split over an f-string or a concatenation leaves its range silently. Keep the "
       "command in ONE literal, or teach the scan to join the pieces",
       not assembled, sorted(set(assembled)))

    def is_assembled(src):
        for node in ast.walk(ast.parse(src)):
            if _string_assembly(node):
                txt = ast.unparse(node)
                if dynamic_command(txt) or any(v in verbs for v, _ in commands_in(txt)):
                    return True
        return False

    # Zero is only meaningful once it is a count of the RIGHT thing. Both prose cases here
    # failed the build under the first version of this rule, and both assembly cases slipped
    # past the second — so the clean run said "no split remedies" while meaning neither.
    for src, want in [('X = f"showrunner is generic, at {p}"', False),      # prose, interpolated
                      ('Y = "see " + "showrunner docs"', False),            # prose, concatenated
                      ('A = f"showrunner {n} leaves are ready"', False),    # prose, name then {}
                      ('B = "showrunner " + w + " orchestrates"', False),   # prose, name then +
                      ('Z = f"run `showrunner lock {s}` now"', True),       # arg interpolated
                      ('V = f"run `showrunner {v}` now"', True),            # VERB interpolated
                      ('W = "run `showrunner " + v + " now`"', True),       # verb concatenated
                      # A LIST concatenation whose element happens to hold a remedy. Flagged
                      # by the operator-keyed version of this rule, which is how the guard
                      # came to fail the build over a message it had no business reading.
                      ('L = [("error", "Run `showrunner init`.")] + list(rest)', False),
                      ('M = paths + ["showrunner close"]', False)]:
        eq("the split-remedy subject separates prose from assembly: %s" % src.split(" = ")[1][:38],
           is_assembled(src), want)


# ===================================================== OPTIONAL: br, tmux
def test_optional():
    group("OPTIONAL — needs external tooling")
    if not have("br"):
        skip("br adapter against a real beads graph",
             "`br` is not on PATH. Install beads to exercise this; the vendored backend "
             "covers the same interface and runs above with no setup.")
    else:
        cfg = make_repo({"graph": {"backend": "br", "db": None, "br_db": None}})
        try:
            g = G.BrGraph(None)
            ready = g.ready()
            ok("br `ready` returns a parseable list of records", isinstance(ready, list), ready)
        except Refused as exc:
            ok("br adapter REFUSES loudly on output it cannot parse (never a silent empty graph)",
               "parse" in str(exc).lower() or "recognised" in str(exc).lower(), str(exc))

    if not have("tmux"):
        skip("tmux-hosted Crawler sessions",
             "`tmux` is not installed. showrunner's worktree/lock/graph guarantees do not "
             "depend on it; only the interactive Crawler host does.")
    else:
        p = subprocess.run(["tmux", "-V"], capture_output=True, text=True)
        ok("tmux is usable for hosting Crawler sessions", p.returncode == 0, p.stderr)


# ==========================================================================
def main():
    print("showrunner test harness — CORE needs only Python 3 + git; OPTIONAL skips loudly.")
    for fn in (test_locks, test_config_refusals, test_every_rule_can_fail, test_graph, test_lifecycle, test_close_gate,
               test_stop_gate, test_baseline, test_routing, test_collision, test_spawn,
               test_harness_provisioning, test_waiting, test_concurrency,
               test_integration, test_publishable, test_dispatch, test_filed_issues_15_to_21,
               test_claims_about_the_layer_below, test_retracted_doc_claims,
               test_cli, test_optional):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAIL.append((fn.__name__, "group crashed", traceback.format_exc()))
            print("  FAIL  %s crashed: %s" % (fn.__name__, exc))
            if VERBOSE:
                traceback.print_exc()

    # LAST, so the total it compares against is final. A count quoted in a README is a
    # claim about this suite, and claims about reality are exactly what this project refuses
    # to take on trust — issue #1 was the README asserting a number a stranger could not
    # verify. Making the suite runnable fixed that and then the number rotted anyway: four
    # tracked files said 117 while the suite reported 182. Stale in the repo's own headline
    # credibility line, which is the one place nobody re-derives.
    group("The counts this repo claims about itself")
    claimed = {}
    for rel_path in ("README.md", "llms.txt", "docs/DESIGN.md"):
        full = os.path.join(ROOT, rel_path)
        if not os.path.exists(full):
            continue
        with open(full) as fh:
            for n in re.findall(r"(\d{2,4})\s+(?:CORE\s+)?assertions?", fh.read()):
                claimed.setdefault(rel_path, set()).add(int(n))
    ok("the docs actually claim an assertion count, so this check is not vacuous",
       bool(claimed), claimed)
    # Computed AFTER the vacuity check has run and BEFORE the last one, so +1 counts exactly
    # the assertion still to come. Computing it earlier made `total` one short of the RESULT
    # line — a check about stale numbers publishing a stale number.
    total = len(PASS) + len(FAIL) + 1
    wrong = {f: sorted(v) for f, v in claimed.items() if v != {total}}
    ok("every assertion count claimed in tracked docs matches what the suite reports (%d)"
       % total, not wrong, wrong)

    print("\n" + "=" * 72)
    print("RESULT: %d passed, %d failed, %d skipped" % (len(PASS), len(FAIL), len(SKIP)))
    if SKIP:
        print("\nskipped (missing external tooling — these are the ONLY assertions that need it):")
        for label, why in SKIP:
            print("  - %s: %s" % (label, why))
    if FAIL:
        print("\nfailures:")
        for grp, label, detail in FAIL:
            print("  [%s] %s" % (grp, label))
            if detail:
                print("      %s" % str(detail).replace("\n", "\n      ")[:1500])
    cleanup()
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
