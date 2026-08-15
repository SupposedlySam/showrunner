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

from showrunner import brief, campaign, collide, config, dispatch, gates, graph as G, lanes, lease, locks, pin, util, worktree  # noqa: E402
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
    # THE SAME RULE, ONE LEAF LATER. Both real entry points place the worktree guard's shim
    # and register it — install.sh copies both, `init` copies the shim — so a fixture without
    # them is again a repo in a state no real install produces, and `doctor` would report a
    # fault every test inherited from the helper rather than from anything under test.
    shim_src = os.path.join(ROOT, ".showrunner", "hooks", "worktree-guard.sh")
    shim_dst = os.path.join(d, ".showrunner", "hooks", "worktree-guard.sh")
    if os.access(shim_src, os.R_OK):
        os.makedirs(os.path.dirname(shim_dst), exist_ok=True)
        shutil.copy2(shim_src, shim_dst)
        os.chmod(shim_dst, 0o755)
    os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
    with open(os.path.join(d, ".claude", "settings.json"), "w") as fh:
        json.dump({"hooks": {"PreToolUse": [
            {"matcher": "Write|Edit|NotebookEdit|Bash",
             "hooks": [{"type": "command",
                        "command": "$CLAUDE_PROJECT_DIR/.showrunner/hooks/worktree-guard.sh"}]}
        ]}}, fh)
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

    # WHAT A CALLER WRITES, A CALLER CAN READ BACK. `acquire(extra=...)` was added for the
    # worktree lease and only the WRITE half existed — `holder()` returned five fixed fields, so
    # the lease reached through `Lock._read` for its own value. It worked and was tested one
    # layer up, which is why nothing here objected: an interface missing half of itself produces
    # a caller that goes around it, not a failure.
    extra_lock = ls.lock("device")
    extra_lock.acquire(os.getpid(), "someone", session="s-extra",
                       extra={"pid_basis": "discovered", "tree": "crawler-a"})
    try:
        back = extra_lock.holder() or {}
        eq("an extra a caller recorded comes back from holder()", back.get("pid_basis"),
           "discovered")
        eq("...all of them, not just the one somebody needed first", back.get("tree"),
           "crawler-a")
        ok("...alongside the fields this module owns, not instead of them",
           back.get("session") == "s-extra" and back.get("pid"), back)
        # The reason extras are returned UNINTERPRETED: a caller must not be able to widen or
        # weaken liveness by passing a field. State is computed from pid and boot alone.
        state, _ = extra_lock.state()
        eq("...and an extra cannot change the lock's state", state, locks.HELD)
    finally:
        extra_lock.release(force=True)

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
    # A VERDICT FROM THE FUTURE. The contract has grown a term before — game_loop added
    # `code-drifted` mid-session — so a consumer has to have an answer for one it does not
    # know, and the answer available by default is the permissive one.
    print(json.dumps(p)); sys.exit(7 if mode == "from-the-future" else codes.get(mode, 2))
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

    # ---- how fresh is this report, and will anything look again? (RPT-01) ------------
    # A crawler report says what is true at the instant it runs, and nothing on it let a reader
    # tell a reading taken thirty seconds ago from one taken yesterday — nor whether walking
    # away was safe. Both are facts about the harness's watchdog, and both are ASKED.
    follow_states = {
        "armed": ({"configured": True, "command": "/abs/showrunner waiting",
                   "last": {"at": 1786000000, "waiting": True, "detail": "2 live"}}, True),
        "unarmed": ({"configured": False, "set_it_by": ".game_loop/config.local.json -> x"},
                    False),
        "failing": ({"configured": True, "failing": True, "command": "relative/path"}, False),
    }
    for label, (payload, want_sched) in sorted(follow_states.items()):
        H._porcelain = lambda b, v, _p=payload: (0, _p)
        try:
            fu = H.follow_up(post)
        finally:
            H._porcelain = _orig_porc
        eq("a '%s' watchdog reports scheduled=%s" % (label, want_sched), fu["scheduled"],
           want_sched)
        if label == "armed":
            eq("...and an armed one carries WHEN it last re-checked, which is the half a "
               "reader uses to judge how stale the verdicts below it are", fu["last"],
               1786000000)
            eq("...and that re-check's verdict, so 'it looked' is distinguishable from 'it "
               "looked and found work outstanding'", fu["waiting"], True)
        else:
            ok("...while a '%s' one says WHY nothing is scheduled, rather than reporting no "
               "follow-up as though that were normal" % label, bool(fu["why"]), fu)
    ok("a FAILING probe is not counted as scheduled — it rings and reports failing, so it is a "
       "broken watchdog rather than a re-check that will happen",
       not follow_states["failing"][1])

    # THE INTERVAL IS ABSENT, AND THAT IS ASSERTED RATHER THAN ASSUMED. The harness's payload
    # carries no period, so "next follow-up at HH:MM" cannot be computed from anything this
    # layer may read — and a number invented here would be a promise about an event showrunner
    # does not schedule. If the harness ever starts publishing one, this fails and someone
    # decides deliberately instead of a stale limit going quiet.
    H._porcelain = lambda b, v: (0, {"configured": True, "command": "x"})
    try:
        armed = H.follow_up(post)
    finally:
        H._porcelain = _orig_porc
    ok("follow_up reports no interval, because the harness publishes none — so callers must "
       "name the TRIGGER and never a time", "interval" not in armed, sorted(armed))

    H._porcelain = lambda b, v: (0, None)
    try:
        none_fu = H.follow_up(post)
    finally:
        H._porcelain = _orig_porc
    ok("a harness with no watchdog verb at all is 'nothing re-checks this', not 'unarmed' — "
       "the same distinction waiting_probe draws, arriving one report out",
       none_fu["scheduled"] is False and none_fu["harness"] is None, none_fu)

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

    # A VERDICT THIS SIDE HAS NO MEANING FOR must fail CLOSED. The merge conditional listed the
    # verdicts that BLOCK, so anything else merged — and `None` meant both "no harness here"
    # (legitimate) and "the harness answered something unrecognised". A contract that grows a
    # term is exactly when a consumer must stop rather than guess, and the guess available by
    # default is the permissive one. game_loop added a verdict mid-session; the only reason that
    # one was safe is that it reused an exit code already mapped.
    with open(os.path.join(rec8["worktree"], ".game_loop", "TESTMODE"), "w") as fh:
        fh.write("from-the-future\n")
    status, _, _ = H.check_tree(post, rec8["worktree"])
    eq("a harness verdict outside the known contract reads as 'unrecognised', not as None — "
       "'we have no meaning for this' and 'there is no harness here' are different, and only "
       "one of them is safe to merge on", status, "unrecognised")
    ok("...and it outranks every verdict, because a term nobody understands says nothing about "
       "the ones that were understood", H.SEVERITY[H.UNRECOGNISED] > max(
           H.SEVERITY[c] for c in H.CONTRACT_CODES))
    blocked_status = [s for s in ("unrecognised", "drifted", "undetermined", "a-verdict-in-2027")
                      if s not in (None, "clean", "notes-drifted")]
    eq("...and integrate's conditional is keyed on the PERMISSIVE answer, so every future "
       "verdict fails closed by construction rather than by somebody remembering to list it",
       blocked_status,
       ["unrecognised", "drifted", "undetermined", "a-verdict-in-2027"])
    ok("...while a repo with no harness at all still merges — that is a supported shape and "
       "refusing it would break every consumer that never had one",
       None in (None, "clean", "notes-drifted"))

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
def test_worktree_lease():
    group("The worktree lease: one session per tree (WL-02)")
    cfg = make_repo()
    os.makedirs(cfg.worktree_root, exist_ok=True)
    tree = "crawler-se-01"
    os.makedirs(os.path.join(cfg.worktree_root, tree), exist_ok=True)

    # JURISDICTION FIRST. A lease covers trees showrunner PLACED, and nothing else. Claiming
    # authority over the main checkout, or over a linked worktree somebody made by hand, would
    # be a guard inventing its own reach — and the main checkout is already serialised by
    # integrate's own file lock, so it would also be a second answer to a settled question.
    eq("a path inside the managed worktree root resolves to its tree",
       lease.tree_for(cfg, os.path.join(cfg.worktree_root, tree, "lib", "x.py")), tree)
    eq("the main checkout is NOT a leased tree", lease.tree_for(cfg, cfg.root), None)
    eq("the worktree root itself is not a tree either",
       lease.tree_for(cfg, cfg.worktree_root), None)

    # POSITIVE CONTROLS, added because the mutation sweep reported this producer THIN at 1 and
    # was right. The two None assertions above pass unchanged against a `tree_for` that returns
    # None for EVERYTHING — which is the jurisdiction check silently answering "no path is ever
    # in a managed worktree", i.e. the whole lease switched off while every test stays green.
    # A refusal cannot be produced by absence, but a permission can, so the negatives need a
    # positive beside them or they are asserting nothing.
    second = "crawler-se-02"
    os.makedirs(os.path.join(cfg.worktree_root, second, "deep", "er"), exist_ok=True)
    eq("...and a DIFFERENT tree resolves to its own name, so the answer tracks the path rather "
       "than being a constant", lease.tree_for(cfg, os.path.join(cfg.worktree_root, second)),
       second)
    eq("...at any depth inside it",
       lease.tree_for(cfg, os.path.join(cfg.worktree_root, second, "deep", "er")), second)
    ok("...and the tree root and a file deep inside it agree, which a constant answer cannot do "
       "while also distinguishing the main checkout",
       lease.tree_for(cfg, os.path.join(cfg.worktree_root, tree)) == tree
       and lease.tree_for(cfg, cfg.root) is None)

    holder_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        a = lease.Lease(cfg, tree)
        got, h = a.acquire("session-A", who="crawler-se-01", pid=holder_proc.pid,
                           basis="dispatch-recorded")
        ok("a free tree can be leased", got, h)
        eq("...and it reads HELD, from the same primitive the device lane uses",
           a.state()[0], locks.HELD)

        # THE DISTINCTION THE WHOLE MODULE TURNS ON. Re-entry by the same session is not a
        # hijack — a session that reconnects to its own tree and gets refused would be the
        # guard blocking the work it was taken out for.
        hijack, _ = lease.Lease(cfg, tree).held_by_other("session-A")
        ok("the SAME session re-entering its own tree is not a hijack", not hijack)
        hijack, who = lease.Lease(cfg, tree).held_by_other("session-B")
        ok("a DIFFERENT live session is", hijack, who)

        # Paired with the case where it fires, because "returns False" and "never looked" are
        # the same observation from outside — the asymmetry worktree.unignored was rewritten
        # to solve, arriving in the module that refuses people.
        eq("...and the refusal names the session it is protecting, not just a pid",
           (who or {}).get("session"), "session-A")

        eq("the liveness BASIS is recorded, so a discovered pid is never shown as a given one",
           (a.holder() or {}).get("pid_basis"), "dispatch-recorded")

        # RELEASE IS KEYED ON SESSION, NOT PID. The pid was discovered by walking an ancestry,
        # so the process releasing may be a different child of the same session than the one
        # that acquired. Keying on pid would refuse the true owner and push --force into
        # routine use, and an escape hatch reached routinely has stopped being one.
        okr, why = lease.Lease(cfg, tree).release(session="session-B")
        ok("a stranger's release is refused", not okr, why)
        okr, why = lease.Lease(cfg, tree).release(session="session-A")
        ok("...and the owning SESSION may release, even from another process", okr, why)
        eq("released leaves the tree FREE", lease.Lease(cfg, tree).state()[0], locks.FREE)
    finally:
        holder_proc.terminate()
        holder_proc.wait()

    # A dead holder is reclaimable; an unreadable one is not adjudicable from here at all.
    # Both inherited from locks.Lock rather than re-decided, which is the point of building
    # ON it — a second liveness rule that drifts from the first fails silently.
    d = lease.Lease(cfg, tree)
    d.acquire("session-dead", who="ghost", pid=999999, basis="ancestor-claude")
    eq("a lease whose process is gone is STALE, so a tree is never wedged by a dead session",
       d.state()[0], locks.STALE)
    with open(os.path.join(d.lock.dir, "pid"), "wb") as fh:
        fh.write(b"\x00rubbish")
    eq("...while an UNREADABLE pid stays UNREADABLE — a partial write by a LIVE holder reads "
       "exactly like a dead one, and only one of those licenses taking the tree",
       d.state()[0], locks.UNREADABLE)
    shutil.rmtree(d.lock.dir, ignore_errors=True)

    # THE FIELD I ADDED TO locks.Lock MUST NOT BE A BACK DOOR. `extra` lets a caller record
    # why its pid means what it does; if it could also overwrite pid or boot, a caller could
    # hand itself an immortal lock through the same door — and the liveness rule would then
    # live in whoever called last, not in locks.py.
    victim = locks.Lock(cfg.lock_root, "extra-probe")
    victim.acquire(999999, "probe", session="s", extra={"pid": "1", "boot": "forged",
                                                        "note": "kept"})
    eq("extra cannot overwrite the pid the liveness rule reads", victim._read("pid"), "999999")
    eq("...nor the boot token that makes a previous boot's claim unusable",
       victim._read("boot"), boot_token_for_test())
    eq("...while a field that is genuinely new is still recorded",
       victim._read("note"), "kept")
    eq("...so a forged extra cannot make a dead holder read alive",
       victim.state()[0], locks.STALE)
    shutil.rmtree(victim.dir, ignore_errors=True)

    # The walk itself. Its VERDICT is machine-dependent, so what is asserted is the contract:
    # a basis always comes back, and a resolved pid is a real live process.
    pid, basis = util.session_pid()
    ok("session_pid always reports the BASIS of what it found, never a bare pid",
       isinstance(basis, str) and basis in ("ancestor-claude", "ppid-fallback", "unresolved"),
       (pid, basis))
    if pid:
        ok("...and a pid it resolved is genuinely alive, whichever basis it used",
           util.pid_alive(pid), (pid, basis))
    else:
        eq("...and when it resolves nothing it says so rather than returning a plausible pid",
           basis, "unresolved")

    # A lease with no process behind it is a note that outlives its writer — which is exactly
    # the campaign-record situation this module replaces. Refusing to take one is the point.
    hollow, why = lease.Lease(cfg, "no-such-tree").acquire("s", pid=None, basis=None)
    if hollow:
        lease.Lease(cfg, "no-such-tree").release(force=True)
    ok("a lease is refused outright when no session process can be resolved, rather than "
       "taken with no liveness at all",
       hollow is False or util.session_pid()[0] is not None, why)

    # status was reported UNPROTECTED by the sweep — 0 assertions noticed it returning []. The
    # only thing asserted was that it returns a list, which an empty one satisfies, and for a
    # READ-ONLY verb the report IS the product: "no tree is held" is what a human reads before
    # deciding to take one. So assert what it says, not its type.
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lease.Lease(cfg, tree).acquire("session-S", who="crawler-se-01", pid=live.pid,
                                       basis="ancestor-claude")
        rows = lease.status(cfg)
        held = [r for r in rows if r["tree"] == tree]
        ok("status REPORTS a held tree — an empty report is what a reader takes as 'free', so "
           "silence here is not a safe default", len(held) == 1, rows)
        eq("...with the state the lock actually holds", held[0]["state"] if held else None,
           locks.HELD)
        eq("...and the session holding it", (held[0]["holder"] if held else {}).get("session"),
           "session-S")
        eq("...and the BASIS of the liveness claim, which is the field a reader needs to know "
           "how much the word HELD is worth", held[0]["pid_basis"] if held else None,
           "ancestor-claude")
        eq("...scoped to one tree when asked for one",
           [r["tree"] for r in lease.status(cfg, tree)], [tree])
        after = lease.status(cfg)
        eq("...and reporting did not take, release or alter the lease",
           lease.Lease(cfg, tree).state()[0], locks.HELD)
        eq("...nor change what a second read reports", [r["state"] for r in after],
           [r["state"] for r in lease.status(cfg)])
    finally:
        live.terminate()
        live.wait()
        lease.Lease(cfg, tree).release(force=True)

    # ---- worktree enter (WL-03): the prompt, never the enforcement -------------------
    def events_of(kind):
        p = os.path.join(cfg.state_dir, "events.jsonl")
        if not os.path.exists(p):
            return []
        out = []
        for line in open(p):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("kind") == kind:
                out.append(e)
        return out

    eq("entering the MAIN checkout is not a lease event at all — an orchestrator that narrates "
       "every non-event trains its reader to skim the one that matters",
       lease.enter(cfg, "s1", path=cfg.root)[0], "not-a-worktree")

    ent = os.path.join(cfg.worktree_root, "enter-probe")
    os.makedirs(ent, exist_ok=True)
    v, d = lease.enter(cfg, "sess-1", path=ent, who="crawler-1")
    eq("a free tree is acquired on entry", v, "acquired")
    ok("...and the acquisition records the basis of its liveness claim",
       bool((d.get("holder") or {}).get("pid_basis")), d)
    eq("the SAME session re-entering is 'own', not a hijack",
       lease.enter(cfg, "sess-1", path=ent)[0], "own")

    before = len(events_of("lease.hijack"))
    v, d = lease.enter(cfg, "sess-2", path=ent)
    eq("a DIFFERENT live session entering is a hijack", v, "hijack")
    eq("...and the holder it reports carries the pid_basis, which is the field saying how much "
       "the word HELD is worth — it printed '?' here until a real hijack showed it",
       (d.get("holder") or {}).get("pid_basis"), "ancestor-claude")
    eq("...and the hijack is LOGGED, because WL-05 may not build a gate without an observed "
       "failure, and a line printed to a terminal nobody kept is not an observation",
       len(events_of("lease.hijack")), before + 1)
    ev = events_of("lease.hijack")[-1]
    eq("...naming the intruder", ev.get("intruder_session"), "sess-2")
    eq("...and the holder it collided with", ev.get("holder_session"), "sess-1")
    eq("entering does NOT take the tree from the holder — the prompt is not the enforcement",
       lease.Lease(cfg, "enter-probe").holder().get("session"), "sess-1")

    # A dead holder is reclaimed, loudly. Paired with the hijack above so neither reads as
    # "enter always does the same thing".
    lease.Lease(cfg, "enter-probe").release(force=True)
    lease.Lease(cfg, "enter-probe").acquire("sess-dead", who="ghost", pid=999999,
                                            basis="ancestor-claude")
    v, d = lease.enter(cfg, "sess-3", path=ent)
    eq("a tree whose holder is provably dead is RECLAIMED, so a crashed session cannot wedge it "
       "forever", v, "reclaimed")
    eq("...and the new holder is the entering session",
       lease.Lease(cfg, "enter-probe").holder().get("session"), "sess-3")

    # UNREADABLE must survive entry untouched. The one state where being helpful is the bug: a
    # partial write by a LIVE holder is indistinguishable from a dead one.
    lease.Lease(cfg, "enter-probe").release(force=True)
    lease.Lease(cfg, "enter-probe").acquire("sess-x", who="x", pid=999999, basis="b")
    with open(os.path.join(lease.Lease(cfg, "enter-probe").lock.dir, "pid"), "wb") as fh:
        fh.write(b"\x00rubbish")
    v, _ = lease.enter(cfg, "sess-4", path=ent)
    eq("an UNREADABLE lease is reported, never reclaimed by entry", v, "unreadable")
    eq("...and it is still there afterwards, held by nobody this code can name",
       lease.Lease(cfg, "enter-probe").state()[0], locks.UNREADABLE)
    shutil.rmtree(lease.Lease(cfg, "enter-probe").lock.dir, ignore_errors=True)

    # ---- worktree fork (WL-04): the option the hijack prompt offers first ------------
    # THE REFUSAL IS THE INTERESTING HALF. Told to "just make another worktree" a reader picks
    # HEAD, and HEAD is not where the held tree started. Worse, git cannot reconstruct the real
    # base afterwards — a fully-merged branch and one that never received a commit both have
    # the base as their merge-base — so a guessed base is wrong exactly when the held tree has
    # already merged, silently, and the fork still looks fine.
    raises("fork REFUSES rather than guessing a base when none was recorded — a guess is wrong "
           "precisely in the case nobody would check",
           lambda: lease.fork(cfg, "enter-probe", "sess-9"), "will not guess")

    sh(["git", "commit", "-q", "--allow-empty", "-m", "second"], cfg.root)
    head = sh(["git", "rev-parse", "HEAD"], cfg.root).stdout.strip()
    first = sh(["git", "rev-parse", "HEAD~1"], cfg.root).stdout.strip()
    path, d = lease.fork(cfg, "enter-probe", "sess-9", base=first, name="fork-probe")
    ok("...and forks from the base it was given when it has one", os.path.isdir(path), path)
    eq("the new tree starts at THAT commit, asserted against the SHA rather than 'it has "
       "commits' — which is true of the wrong base too",
       sh(["git", "rev-parse", "HEAD"], path).stdout.strip(), first)
    ok("...which is NOT the tip, so the assertion above can distinguish them", first != head)
    eq("the fork's base is recorded RESOLVED, not as the symbolic ref it was asked for — git "
       "cannot recover what 'HEAD' meant at this instant afterwards", d["base"], first)
    eq("...and the forking session holds the new tree",
       lease.Lease(cfg, "fork-probe").holder().get("session"), "sess-9")
    eq("...while the tree it forked FROM is untouched", lease.tree_for(cfg, ent), "enter-probe")

    _, d2 = lease.fork(cfg, "enter-probe", "sess-9", base="HEAD", name="fork-symbolic")
    eq("a symbolic base is resolved to a sha before anything is created",
       d2["base"], head)

    # THE RECORDED PATH, which every assertion above bypasses by passing base= explicitly.
    # Without this, base_sha_of could return None forever: fork would still refuse, the refusal
    # assertion would still pass, and the producer would be dead with the suite green. It fails
    # SAFE today, which is a fact about today's caller and not a reason to leave it unwatched.
    rec = campaign.load(cfg)
    rec.setdefault("crawlers", []).append(
        {"crawler": "recorded-probe", "leaf": "L1", "worktree": ".worktrees/recorded-probe",
         "base_sha": first, "state": "spawned"})
    campaign.save(cfg, rec)
    os.makedirs(os.path.join(cfg.worktree_root, "recorded-probe"), exist_ok=True)
    eq("the base is read back from the campaign record, which is the only place it survives",
       lease.base_sha_of(cfg, "recorded-probe"), first)
    _, d3 = lease.fork(cfg, "recorded-probe", "sess-10", name="fork-recorded")
    eq("...and fork uses it with no --base, landing on the commit the held tree started at",
       d3["base"], first)
    ok("...which is not the tip, so that assertion can tell the two apart", first != head)

    # ---- worktree guard (WL-05): the teeth -------------------------------------------
    # EVERY ALLOW IS PAIRED WITH THE CASE WHERE IT DENIES. An allow-only suite passes
    # identically against a guard that does nothing, and a permission — unlike a refusal — can
    # be produced by absence. That asymmetry is the whole reason this group is shaped this way.
    gt = "guard-probe"
    gpath = os.path.join(cfg.worktree_root, gt)
    os.makedirs(gpath, exist_ok=True)
    held = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lease.Lease(cfg, gt).acquire("sess-HOLDER-long", who="crawler-g", pid=held.pid,
                                     basis="dispatch-recorded")
        write_in = {"file_path": os.path.join(gpath, "lib", "x.py")}

        allow, msg, det = lease.guard(cfg, "sess-INTRUDER", tool="Write",
                                      tool_input=write_in, cwd=gpath)
        ok("a DIFFERENT live session writing into a held tree is DENIED — the refusal `enter` "
           "deliberately does not make", not allow, msg[:120])
        eq("...naming the tree it is protecting", det.get("tree"), gt)
        ok("...and naming the HOLDER, because a refusal a reader cannot act on is one they "
           "work around", "crawler-g" in msg, msg[:200])
        ok("...with a readable timestamp rather than the raw epoch second it printed on its "
           "first real run — a number the reader has to go and convert, in the line telling "
           "them how long somebody has held this",
           "since    2" in msg and not re.search(r"since\s+17\d{8}", msg), msg[:300])
        ok("...and the session id marked as ABBREVIATED, so two ids that differ past the cut "
           "are never shown as the same id", "sess-HOLDER-…" in msg or "…" in msg, msg[:300])

        # THE PAIR. Same tree, same lease, same call — only the session differs.
        allow, msg, _ = lease.guard(cfg, "sess-HOLDER-long", tool="Write",
                                    tool_input=write_in, cwd=gpath)
        ok("...while the HOLDER's own write is allowed, which is the difference between a "
           "guard and a lock nobody can open", allow, msg)

        # Jurisdiction, from the guard's side rather than tree_for's.
        allow, msg, det = lease.guard(cfg, "sess-INTRUDER", tool="Write",
                                      tool_input={"file_path": os.path.join(cfg.root, "a.py")},
                                      cwd=cfg.root)
        ok("a write in the MAIN checkout is never guarded by a lease — integrate already "
           "serialises it with its own file lock, and a second answer here would be one rule "
           "in two places", allow, msg)
        eq("...and it says so by finding no tree at all, not by finding a free one",
           det.get("trees"), [])

        # THE CROSS-TREE WRITE. cwd is somewhere harmless; the PATH is what lands in the held
        # tree. Without this the guard would only ever see the tree a session stands in.
        allow, _, det = lease.guard(cfg, "sess-INTRUDER", tool="Write",
                                    tool_input=write_in, cwd=cfg.root)
        ok("a write whose PATH lands in a held tree is denied even from outside it — the "
           "target is checked, not only where the session happens to be standing",
           not allow, det)

        # THE CARVE-OUT. The refusal's first remedy is `worktree fork`; a guard that denies its
        # own remedy is a guard that gets switched off, and then nothing is guarded at all.
        fork_cmd = "%s worktree fork --from %s" % (os.path.join(cfg.root, "bin", "showrunner"),
                                                   gt)
        allow, msg, det = lease.guard(cfg, "sess-INTRUDER", tool="Bash",
                                      tool_input={"command": fork_cmd}, cwd=gpath)
        ok("showrunner's own worktree verb passes inside a tree somebody else holds — it is "
           "the remedy the refusal prints", allow, msg)
        eq("...by the carve-out, not by accident of the lease being free",
           det.get("carve_out"), "own-verb")

        # And the carve-out fails CLOSED, because a hole shaped like the thing being guarded
        # is worse than no carve-out. `&& rm -rf` must not ride in on a fork.
        ok("the carve-out matches a bare showrunner worktree verb", lease.own_command(fork_cmd))
        ok("...and REFUSES to cover a chained command, so appending `&&` is not a bypass "
           "anybody can find", not lease.own_command(fork_cmd + " && rm -rf /"))
        ok("...nor one that merely mentions the verb inside something else",
           not lease.own_command("echo %s" % fork_cmd))
        allow, _, _ = lease.guard(cfg, "sess-INTRUDER", tool="Bash",
                                  tool_input={"command": fork_cmd + " && rm -rf x"}, cwd=gpath)
        ok("...and the chained one is actually DENIED, which is what makes the three "
           "assertions above more than a statement about a regex", not allow)

        # AN UNIDENTIFIABLE SESSION ALLOWS, LOUDLY. Denying would refuse the holder's own
        # writes — the guard blocking the work it was taken out for. Allowing in SILENCE would
        # be indistinguishable from a guard that ran and was content.
        allow, msg, det = lease.guard(cfg, "", tool="Write", tool_input=write_in, cwd=gpath)
        ok("a call carrying no session id is allowed rather than denied — with no id there is "
           "no way to tell the holder from an intruder", allow, msg[:120])
        eq("...and it is flagged as DEGRADED rather than reported as a pass",
           det.get("degraded"), "no-session")
        ok("...and says the guard did not run, because a silent allow is the one failure this "
           "posture cannot afford", "DID NOT RUN" in msg, msg[:160])
    finally:
        held.terminate()
        held.wait()

    # STALE AND UNREADABLE BOTH ALLOW, and neither is an oversight. Paired with the deny above:
    # without that pair these two pass against a guard that allows unconditionally.
    lease.Lease(cfg, gt).release(force=True)
    lease.Lease(cfg, gt).acquire("sess-dead-g", who="ghost", pid=999999, basis="b")
    allow, msg, _ = lease.guard(cfg, "sess-INTRUDER", tool="Write",
                                tool_input={"file_path": os.path.join(gpath, "x")}, cwd=gpath)
    ok("a tree whose holder is provably DEAD stops being defended — a crashed session must not "
       "wedge a tree against everyone forever", allow, msg)
    with open(os.path.join(lease.Lease(cfg, gt).lock.dir, "pid"), "wb") as fh:
        fh.write(b"\x00rubbish")
    allow, msg, _ = lease.guard(cfg, "sess-INTRUDER", tool="Write",
                                tool_input={"file_path": os.path.join(gpath, "x")}, cwd=gpath)
    ok("an UNREADABLE lease allows too — turning 'I cannot tell' into a refusal would wedge a "
       "tree on a partial write, which is exactly what locks.py refuses to do one layer down",
       allow, msg)
    shutil.rmtree(lease.Lease(cfg, gt).lock.dir, ignore_errors=True)

    # ---- is the guard WIRED? (the check one level out from 'does it work') -----------
    # A guard verb nobody registers has never once run. That was true of `lock guard` for this
    # repo's entire life, and it is the row that shaped this plan.
    # The fixture supplies what a real install supplies, so the UNREGISTERED state is
    # CONSTRUCTED here rather than inherited. That is the right way round: it makes the
    # assertion below about the check, and not about a fixture that happened to be incomplete.
    claude_dir = os.path.join(cfg.root, ".claude")
    shim_rel = lease.GUARD_SHIM
    shim_dst = os.path.join(cfg.root, shim_rel)
    os.remove(os.path.join(claude_dir, "settings.json"))
    errs = [m for l, m in lease.guard_health(cfg) if l == "error"]
    ok("a repo with no hook registration reports the guard as an ERROR, not a warning — an "
       "unregistered guard is indistinguishable from one that ran and was content",
       any("registers no worktree-guard" in m or "nothing registers" in m for m in errs),
       errs[:2])

    with open(os.path.join(claude_dir, "settings.json"), "w") as fh:
        json.dump({"hooks": {"PreToolUse": [
            {"matcher": "Write|Edit|NotebookEdit|Bash",
             "hooks": [{"type": "command",
                        "command": "$CLAUDE_PROJECT_DIR/" + shim_rel}]}]}}, fh)
    after = [m for l, m in lease.guard_health(cfg) if l == "error"]
    ok("...and once the shim is present and registered, that error clears — so the assertion "
       "above is about the registration and not about the check always failing",
       not any("registers no worktree-guard" in m for m in after), after[:2])

    # The matcher is part of the registration: an entry on Write alone leaves Bash unguarded,
    # which reads as 'registered' to anything that only asks whether an entry exists.
    with open(os.path.join(claude_dir, "settings.json"), "w") as fh:
        json.dump({"hooks": {"PreToolUse": [
            {"matcher": "Write", "hooks": [{"type": "command",
                                            "command": "$CLAUDE_PROJECT_DIR/" + shim_rel}]}]}},
                  fh)
    warns = [m for l, m in lease.guard_health(cfg) if l == "warn"]
    ok("a registration whose matcher misses Bash is reported — 'an entry exists' is not the "
       "same question as 'this tool is covered'",
       any("does not cover" in m and "Bash" in m for m in warns), warns[:2])

    os.remove(shim_dst)
    gone = [m for l, m in lease.guard_health(cfg) if l == "error"]
    ok("a MISSING shim is an error even while the registration is intact — the registration "
       "would then name a file that is not there, which denies nothing",
       any("MISSING" in m for m in gone), gone[:2])

    # ---- registering it, without eating anybody else's hooks -------------------------
    # SHIPPING THE REGISTRATION IS THE POINT: `lock guard` has been correct and unregistered
    # for this repo's whole life, which is the row that shaped this plan. But the file belongs
    # to Claude Code and to whoever else registered a hook in it, so the interesting assertions
    # are all about what registration LEAVES ALONE.
    settings = os.path.join(claude_dir, "settings.json")
    with open(settings, "w") as fh:
        json.dump({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "somebody-elses.sh"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "their-stop.sh"}]}]},
            "statusLine": {"type": "command", "command": "their-statusline"}}, fh)
    changed, note = lease.register_guard(cfg)
    ok("registering the guard reports that it changed something", changed, note)
    with open(settings) as fh:
        merged = json.load(fh)
    ok("...and the guard is now registered", lease._guard_registration(settings)[0], merged)
    ok("...WITHOUT displacing a PreToolUse hook somebody else registered — showrunner is a "
       "fourth entry beside the others, not the owner of this file",
       any(h.get("command") == "somebody-elses.sh"
           for e in merged["hooks"]["PreToolUse"] for h in e.get("hooks", [])), merged)
    eq("...nor their hooks on other events", merged["hooks"]["Stop"][0]["hooks"][0]["command"],
       "their-stop.sh")
    eq("...nor unrelated top-level keys", merged.get("statusLine", {}).get("command"),
       "their-statusline")

    changed2, _ = lease.register_guard(cfg)
    ok("...and registering twice is a no-op, so an installer that runs on every upgrade does "
       "not accumulate duplicate entries", not changed2)
    with open(settings) as fh:
        twice = json.load(fh)
    eq("...asserted on the entry COUNT, because 'returns False' and 'appended silently' are "
       "the same observation from outside", len(twice["hooks"]["PreToolUse"]),
       len(merged["hooks"]["PreToolUse"]))

    # THE FILE IT CANNOT READ. Rewriting one we could not parse is how somebody's hooks
    # disappear — and the person would have no way to know it was us.
    with open(settings, "w") as fh:
        fh.write("{ this is not json")
    changed3, why = lease.register_guard(cfg)
    ok("an unparseable settings file is NOT rewritten", not changed3, why)
    with open(settings) as fh:
        ok("...and is left byte-for-byte as it was, with the reason reported instead",
           fh.read() == "{ this is not json", why)


def test_worktree_guard_from_inside_a_worktree():
    group("The worktree guard, asserted from INSIDE a real linked worktree (WL-05)")
    # THE ONLY PLACE THE SHIM'S RESOLVER CAN BE WRONG. Asserting it from the main checkout
    # tests nothing: `--git-common-dir` answers with the repo's own .git there, so a resolver
    # that is broken for worktrees passes. That is the exact shape of the dead path `sr_bin`
    # carried for a week — absolute, canonical, and never resolved from where the reader stood.
    if not have("git"):
        skip("the worktree-guard shim group", "git is not installed")
        return
    shim_src = os.path.join(ROOT, lease.GUARD_SHIM)
    if not os.path.exists(shim_src):
        skip("the worktree-guard shim group", "%s does not exist" % lease.GUARD_SHIM)
        return

    cfg = make_repo()
    shim_dst = os.path.join(cfg.root, lease.GUARD_SHIM)
    os.makedirs(os.path.dirname(shim_dst), exist_ok=True)
    shutil.copy2(shim_src, shim_dst)
    os.chmod(shim_dst, 0o755)

    # make_repo COPIES bin/showrunner, so realpath resolves beside the copy and no lib is
    # there — the binary exits 70. A symlink resolves to this checkout's real binary and its
    # library. What is under test is the SHIM's resolver, not Python's import path.
    real_bin = os.path.join(cfg.root, ".showrunner", "bin", "showrunner")
    if os.path.exists(real_bin):
        os.remove(real_bin)
    os.symlink(os.path.join(ROOT, "bin", "showrunner"), real_bin)

    # COMMITTED, because that is what makes it cross. `git worktree add` copies from HEAD, not
    # from the working tree — an untracked shim is present here and absent in every worktree,
    # which is the one place it exists to run. Observed: the first probe worktree had no shim.
    sh(["git", "add", "-f", lease.GUARD_SHIM], cfg.root)
    sh(["git", "commit", "-q", "-m", "shim"], cfg.root)

    tree = "shim-probe"
    wt = os.path.join(cfg.worktree_root, tree)
    os.makedirs(cfg.worktree_root, exist_ok=True)
    sh(["git", "worktree", "add", "-q", wt, "-b", "showrunner/shim-probe"], cfg.root)
    ok("the shim CROSSES into a linked worktree, which is the property the whole design turns "
       "on — a hook that does not cross cannot guard the tree it was written for",
       os.path.exists(os.path.join(wt, lease.GUARD_SHIM)),
       sorted(os.listdir(os.path.join(wt, ".showrunner"))) if
       os.path.isdir(os.path.join(wt, ".showrunner")) else "no .showrunner at all")

    def run_shim(session, cwd, path):
        payload = json.dumps({"session_id": session, "cwd": cwd, "tool_name": "Write",
                              "tool_input": {"file_path": path}})
        return subprocess.run(["bash", os.path.join(wt, lease.GUARD_SHIM)],
                              cwd=cwd, input=payload, capture_output=True, text=True)

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lease.Lease(cfg, tree).acquire("sess-A", who="crawler-shim", pid=holder.pid,
                                       basis="dispatch-recorded")
        target = os.path.join(wt, "lib", "x.py")

        # RUN FROM INSIDE THE WORKTREE, through the copy that crossed. Every earlier assertion
        # in this file calls the Python directly; this one is the only one that exercises the
        # resolver, the exec and the exit code together.
        res = run_shim("sess-B", wt, target)
        eq("a second session's Write inside a held worktree EXITS 2 — resolved through the "
           "shim that crossed, from the cwd a hijacking session would actually have",
           res.returncode, 2)
        ok("...and the reason reaches stderr, which is the channel the agent reads on a "
           "denial", "DENIED" in res.stderr and "crawler-shim" in res.stderr,
           (res.stderr or res.stdout)[:200])

        # THE PAIR, through the same shim, differing only in who is asking.
        res = run_shim("sess-A", wt, target)
        eq("...while the HOLDER's own Write through the same shim exits 0", res.returncode, 0)

        # LOGGED. A denial visible only in one session's scrollback is not an observation, and
        # every gate in this repo owes the journal the fact that it fired.
        events_path = os.path.join(cfg.state_dir, "events.jsonl")
        kinds = []
        if os.path.exists(events_path):
            for line in open(events_path):
                try:
                    kinds.append(json.loads(line))
                except ValueError:
                    pass
        denied = [e for e in kinds if e.get("kind") == "lease.denied"]
        ok("the refusal is JOURNALLED, naming the tree and the session it refused",
           len(denied) == 1 and denied[0].get("tree") == tree
           and denied[0].get("intruder_session") == "sess-B", denied[:1])
    finally:
        holder.terminate()
        holder.wait()
        lease.Lease(cfg, tree).release(force=True)

    # THE FAIL-OPEN HALF, exercised rather than asserted from the source text. A shim that
    # cannot find a binary must ALLOW — a PreToolUse that hard-fails on its own plumbing blocks
    # every write including the one that repairs it — and must SAY so, because an allow nobody
    # is told about is indistinguishable from a guard that ran and was content.
    os.remove(real_bin)
    res = subprocess.run(["bash", os.path.join(wt, lease.GUARD_SHIM)], cwd=wt,
                         input=json.dumps({"session_id": "sess-B", "cwd": wt,
                                           "tool_name": "Write", "tool_input": {}}),
                         capture_output=True, text=True)
    eq("a shim that can find no binary ALLOWS — hard-failing on our own plumbing would block "
       "every write including the one that repairs it", res.returncode, 0)
    ok("...and SAYS the guard did not run, in the structured field that actually reaches the "
       "agent on an allow", "DID NOT RUN" in res.stdout
       and "additionalContext" in res.stdout, res.stdout[:200])


def test_self_pin():
    group("Pinning the tool's own code at a ref (CI-02)")
    if not have("git"):
        skip("the self --pin group", "git is not installed")
        return

    # AGAINST THE REAL REPO, because `git archive` needs a repo that actually carries bin/ and
    # lib/ — and a fixture that faked them would be asserting against the fake. Nothing here
    # writes to ROOT: `git archive` reads, and every destination is a temp dir.
    cfg = config.load(start=ROOT)
    head = sh(["git", "rev-parse", "HEAD"], ROOT).stdout.strip()

    dest = os.path.join(tmpdir("central"), "central")
    d = pin.pin(cfg, "HEAD", dest)
    eq("a symbolic ref is RESOLVED to a sha before anything is stamped — git cannot recover "
       "what 'HEAD' meant at this instant afterwards", d["sha"], head)
    with open(os.path.join(dest, pin.VERSION_FILE)) as fh:
        eq("...and VERSION carries that sha, not the ref it was asked for", fh.read().strip(),
           head)

    # THE ASSERTION THIS REPO LEARNED TWICE. `sr_bin` named a path that was absolute,
    # canonical and dead; `init` placed a binary without the library beside it, so what it left
    # was executable and died on every invocation. Asserting the file EXISTS reproduces both.
    ran = subprocess.run([d["binary"], "--version"], cwd=dest, capture_output=True, text=True)
    eq("the pinned copy RUNS — a central install that exists and cannot execute is the shape "
       "this project has now shipped twice (%s)" % (ran.stderr or ran.stdout).strip()[:40],
       ran.returncode, 0)

    # THE READ SIDE, exercised. A stamp nobody can read back is a comment in a file — this
    # repo shipped a lock field with a write side and no read side, and the one caller that
    # needed it printed "?" in the report the field existed for.
    back = pin.read_pin(dest)
    eq("the stamp reads back", (back or {}).get("sha"), head)
    eq("...naming the ref it was asked for, beside the sha that ref resolved to",
       (back or {}).get("ref"), "HEAD")
    ok("...and reports the two files AGREE, which is the only thing that can distinguish a "
       "clean pin from a directory somebody edited afterwards", (back or {}).get("consistent"),
       back)
    ok("...and a directory that was never pinned reads as nothing, rather than as an empty pin",
       pin.read_pin(tmpdir("notapin")) is None)

    # The consumer's own state must not ride along. A central copy carrying one project's
    # config is the misread that makes a shared install report on the wrong repo.
    ok("the pinned payload carries the TOOL and not the project — no .showrunner/ comes with "
       "it", not os.path.exists(os.path.join(dest, ".showrunner")), sorted(os.listdir(dest)))

    # DISAGREEMENT IS SURFACED, not reconciled. Reachable only by editing the directory after
    # the pin, which is the one thing this module admits it cannot otherwise see.
    with open(os.path.join(dest, pin.VERSION_FILE), "w") as fh:
        fh.write("0" * 40 + "\n")
    edited = pin.read_pin(dest)
    ok("a VERSION edited after the pin makes the stamp report INCONSISTENT rather than quietly "
       "preferring one of the two files", edited and not edited.get("consistent"), edited)

    # Re-pinning over our OWN pin is allowed — that is the upgrade path.
    d2 = pin.pin(cfg, head, dest)
    eq("re-pinning over an existing pin works, which is the whole upgrade path", d2["sha"], head)
    ok("...and it repaired the edited stamp, so an upgrade is also the remedy for a modified "
       "central directory", (pin.read_pin(dest) or {}).get("consistent"))

    # THROUGH THE CLI, because `consistent` being False is only useful if something ACTS on it.
    # A flag computed correctly and reported as success is the shape of a check nobody notices.
    sr = os.path.join(ROOT, "bin", "showrunner")
    good = subprocess.run([sys.executable, sr, "self", "--dest", dest],
                          capture_output=True, text=True, cwd=ROOT)
    eq("`self --dest` on a clean pin exits 0", good.returncode, 0)
    with open(os.path.join(dest, pin.VERSION_FILE), "w") as fh:
        fh.write("0" * 40 + "\n")
    bad = subprocess.run([sys.executable, sr, "self", "--dest", dest],
                         capture_output=True, text=True, cwd=ROOT)
    eq("...and on a directory edited since it was pinned it exits NON-ZERO, so the "
       "inconsistency is a refusal and not a line in a report", bad.returncode, 2)
    ok("...naming both values, because 'they disagree' without them is a fact the reader "
       "cannot act on", "DISAGREE" in bad.stdout, bad.stdout[-200:])
    pin.pin(cfg, head, dest)

    # THE DELETION SAFETY. `pin` removes its destination wholesale, so the question "is this
    # mine?" is deciding a deletion. A mistyped --dest must not eat a directory.
    stranger = tmpdir("stranger")
    keep = os.path.join(stranger, "someones-work.txt")
    with open(keep, "w") as fh:
        fh.write("the only copy\n")
    raises("a destination that is not a pin is REFUSED rather than deleted — a pin overwrites "
           "wholesale, so it only ever overwrites something it recognises as its own",
           lambda: pin.pin(cfg, "HEAD", stranger), "refusing to delete")
    ok("...and the file that was there is still there", os.path.exists(keep))
    ok("...and an empty directory is not mistaken for a pin either, since 'exists' is the test "
       "that would make a typo destructive", not pin.looks_pinned(tmpdir("empty")))

    raises("an unresolvable ref is refused before anything is created",
           lambda: pin.pin(cfg, "no-such-ref-here", os.path.join(tmpdir("d2"), "c")),
           "cannot resolve")

    # ---- WHAT CODE IS RUNNING, and what may name it ---------------------------------
    # `__version__` has read "0.1.0" since the first commit and has never been bumped, so
    # `--version` could not tell a checkout from this morning from an install three weeks
    # stale. Establishing that seven real consumers were out of date needed a file-by-file
    # diff, because the one field whose job is that question could not answer it.
    eq("provenance is resolved from where the CODE lives, never from the cwd — under a central "
       "install the cwd is some consumer project, and answering from it would report that "
       "project's HEAD as showrunner's version", pin.code_root(), ROOT)
    here = pin.running()
    eq("...so this checkout reports itself as a checkout", here["source"], "checkout")
    eq("...naming the commit that is actually checked out", here["sha"], head)
    ok("...and saying whether that sha still describes the working tree, because uncommitted "
       "edits make it an overstatement", here["dirty"] in (True, False), here)

    # THE REGRESSION. A plain `install.sh` copy lands at <consumer>/.showrunner/, which is
    # INSIDE the consumer's git repo — so asking git for HEAD there answers with the
    # consumer's commit and reports it as showrunner's version. Confident, precise, and about
    # the wrong repository. Observed: a fresh copy reported the test project's own seed commit.
    consumer = tmpdir("version-consumer")
    sh(["git", "init", "-q", "-b", "main"], consumer)
    sh(["git", "config", "user.email", "t@t"], consumer)
    sh(["git", "config", "user.name", "t"], consumer)
    sh(["git", "commit", "-q", "--allow-empty", "-m", "seed"], consumer)
    consumer_head = sh(["git", "rev-parse", "HEAD"], consumer).stdout.strip()
    sh([os.path.join(ROOT, "install.sh"), consumer], ROOT)
    said = subprocess.run([os.path.join(consumer, ".showrunner", "bin", "showrunner"),
                           "--version"], cwd=consumer, capture_output=True, text=True).stdout
    ok("a plain copied install does NOT claim the consumer's commit as its own version — the "
       "code root must BE the repo, not merely sit inside one",
       consumer_head[:12] not in said, said.strip()[:160])
    ok("...it reports the absence instead, because a working-tree copy genuinely has no commit "
       "that names it — which is the entire argument for pinning",
       "no commit names this code" in said.lower() or "copied from a working tree" in said,
       said.strip()[:160])

    # THE PINNED BRANCH, against a REAL pin directory. Only `code_root` is redirected — the
    # stamp, its reader and the consistency check are all the real ones. Redirecting where the
    # code lives is the one thing that cannot vary inside a single process.
    _orig_root = pin.code_root
    pin.code_root = lambda _d=dest: _d
    try:
        pinned_info = pin.running()
        line = pin.describe()
    finally:
        pin.code_root = _orig_root
    eq("a pinned copy reports itself as pinned", pinned_info["source"], "pinned")
    eq("...naming the commit it was extracted from", pinned_info["sha"], head)
    ok("...and the one line says so", "pinned" in line and head[:12] in line, line[:120])

    with open(os.path.join(dest, pin.VERSION_FILE), "w") as fh:
        fh.write("0" * 40 + "\n")
    pin.code_root = lambda _d=dest: _d
    try:
        tampered = pin.running()
        tline = pin.describe()
    finally:
        pin.code_root = _orig_root
    ok("a pin edited after the fact is reported as no longer described by its own sha, rather "
       "than reporting the sha as though nothing happened",
       tampered["dirty"] is True and "EDITED" in tline, tline[:160])
    pin.pin(cfg, head, dest)

    # A HALF-WRITTEN PIN IS WORSE THAN NONE: it is a directory that exists, looks installed to
    # anything checking for a path, and cannot run. A fixture repo carries no bin/ or lib/, so
    # the extraction genuinely fails — this is not a simulated error.
    empty_repo = make_repo()
    doomed = os.path.join(tmpdir("doomed"), "central")
    raises("a commit that carries no bin/ and lib/ is refused",
           lambda: pin.pin(empty_repo, "HEAD", doomed), "self --pin")
    ok("...and leaves NOTHING behind — a half-extracted central directory would read as "
       "installed to anything that checks for a path", not os.path.exists(doomed))


def test_central_install():
    group("Central install: one copy of the code, every project's own config (CI-03)")
    if not have("git"):
        skip("the central-install group", "git is not installed")
        return
    installer = os.path.join(ROOT, "install.sh")
    template = os.path.join(ROOT, "templates", "central-shims", "showrunner")
    if not (os.path.exists(installer) and os.path.exists(template)):
        skip("the central-install group", "install.sh or the shim template is missing")
        return

    def consumer_repo():
        d = tmpdir("central-consumer")
        sh(["git", "init", "-q", "-b", "main"], d)
        sh(["git", "config", "user.email", "test@example.com"], d)
        sh(["git", "config", "user.name", "showrunner test"], d)
        with open(os.path.join(d, "README.md"), "w") as fh:
            fh.write("seed\n")
        sh(["git", "add", "-A"], d)
        sh(["git", "commit", "-q", "-m", "seed"], d)
        return d

    def run_sr(repo, args, central, cwd=None):
        env = dict(os.environ, SHOWRUNNER_CENTRAL=central)
        return subprocess.run([os.path.join(repo, ".showrunner", "bin", "showrunner")] + args,
                              cwd=cwd or repo, capture_output=True, text=True, env=env)

    repo = consumer_repo()
    nowhere = os.path.join(tmpdir("nowhere"), "absent")
    env = dict(os.environ, SHOWRUNNER_CENTRAL=nowhere)
    subprocess.run([installer, "--central", repo], cwd=ROOT, capture_output=True, text=True,
                   env=env)

    placed = os.path.join(repo, ".showrunner", "bin", "showrunner")
    with open(template) as fh:
        want = fh.read()
    with open(placed) as fh:
        got = fh.read()
    ok("--central writes the shim BYTE-IDENTICAL to the template — 'machine-agnostic' is the "
       "property that lets a consumer commit it, and it is only true if nothing is baked in "
       "per install", got == want, "%d vs %d bytes" % (len(got), len(want)))
    ok("...and no absolute path from THIS machine is baked into it",
       ROOT not in got and os.path.expanduser("~") not in got, got[:200])
    ok("--central leaves NO local copy of the library — that is the entire point, and a lib "
       "sitting beside a shim that ignores it is the drift this mode exists to end",
       not os.path.isdir(os.path.join(repo, ".showrunner", "lib", "showrunner")))
    ok("...while config.json IS seeded, so `init` ran even though the shim it would have "
       "called cannot reach a central install yet",
       os.path.exists(os.path.join(repo, ".showrunner", "config.json")))

    # ---- CENTRAL ABSENT: both directions, because an absence-only suite passes against a
    # build that does nothing at all.
    for verb in (["lock", "guard"], ["worktree", "guard"], ["stop-gate"], ["worktree", "enter"]):
        res = run_sr(repo, verb, nowhere)
        eq("with no central install, the HOOK verb `%s` exits 0 — a PreToolUse that hard-fails "
           "on missing plumbing blocks the write that would repair it" % " ".join(verb),
           res.returncode, 0)
        ok("...and SAYS it was not checked, which is the one thing game_loop's own shim does "
           "not do and the only change the retracted fail-posture objection left behind",
           "ALLOWED WITHOUT BEING CHECKED" in res.stderr, res.stderr[:120])

    for verb in (["status"], ["doctor"], ["integrate"], ["lock", "run", "device"]):
        res = run_sr(repo, verb, nowhere)
        eq("...while the NON-hook verb `%s` fails LOUD — it was typed by someone expecting a "
           "real answer" % " ".join(verb), res.returncode, 1)
        ok("...naming the command that populates central, so the refusal is actionable",
           "self --pin" in res.stderr, res.stderr[:160])

    # `lock run` IS THE GUARANTEE and must never be on the fail-open side. This is the whole
    # reason Objection C's answer holds: a missing central install costs the OPTIMISATION
    # (routing, `lock guard`) and keeps the guarantee, because `run` is not a hook.
    ok("`lock run` is NOT treated as a hook verb — routing is the optimisation, `lock run` is "
       "the guarantee, and a central install that silently switched IT off would be INV8's "
       "prohibition for real",
       run_sr(repo, ["lock", "run", "device"], nowhere).returncode == 1)

    # ---- CENTRAL PRESENT ----------------------------------------------------------
    central = os.path.join(tmpdir("central-code"), "code")
    pin.pin(config.load(start=ROOT), "HEAD", central)
    res = run_sr(repo, ["--version"], central)
    eq("with a central install present the shim dispatches to it and the tool runs",
       res.returncode, 0)
    ok("...and it is genuinely the CENTRAL copy answering, not a local one — there is no local "
       "copy left to answer", "showrunner" in res.stdout.lower(), res.stdout[:60])

    res = run_sr(repo, ["doctor"], central)
    ok("the central binary resolves THIS repo's config, not central's — only the CODE is "
       "shared, which is why no SHOWRUNNER_HOME equivalent is needed",
       os.path.realpath(repo) in res.stdout.replace("/private", "", 1)
       or os.path.realpath(repo) in res.stdout, res.stdout[:200])

    # FROM INSIDE A LINKED WORKTREE, which is the case `--git-common-dir` exists for and the
    # only place a resolver that looks at the cwd's toplevel would silently answer wrongly.
    wt = os.path.join(repo, ".worktrees", "central-probe")
    os.makedirs(os.path.join(repo, ".worktrees"), exist_ok=True)
    sh(["git", "worktree", "add", "-q", wt, "-b", "showrunner/central-probe"], repo)
    res = run_sr(repo, ["doctor"], central, cwd=wt)
    ok("...asserted from INSIDE a linked worktree, where the code is central, the cwd is the "
       "worktree, and the config must still resolve to the MAIN checkout",
       os.path.realpath(repo) in res.stdout.replace("/private", "", 1)
       or os.path.realpath(repo) in res.stdout, res.stdout[:200])
    ok("...and it did NOT resolve to the worktree, which is the wrong answer that would look "
       "identical from the main checkout", os.path.realpath(wt) not in res.stdout)

    # ---- REVERSIBILITY. A claim, so it gets asserted. -----------------------------
    out = subprocess.run([installer, repo], cwd=ROOT, capture_output=True, text=True, env=env)
    ok("re-installing WITHOUT --central restores the local library",
       os.path.isdir(os.path.join(repo, ".showrunner", "lib", "showrunner")))
    with open(placed) as fh:
        reverted = fh.read()
    ok("...and replaces the shim with the real binary", reverted != want
       and "python" in reverted[:40], reverted[:60])
    ok("...and SAYS it reverted, because a mode change nobody is told about is one nobody can "
       "reason about later", "reverted from central dispatch" in out.stdout, out.stdout[-300:])
    standalone = subprocess.run([placed, "--version"], cwd=repo, capture_output=True, text=True,
                                env=dict(os.environ, SHOWRUNNER_CENTRAL=nowhere))
    eq("...and the reverted repo runs with NO central install at all, which is what 'no longer "
       "depends on it' has to mean", standalone.returncode, 0)


def test_installer_leaves_no_vendored_copy():
    group("What install.sh leaves behind in somebody ELSE's repo")
    if not have("git"):
        skip("the installer group", "git is not installed")
        return
    installer = os.path.join(ROOT, "install.sh")
    if not os.path.exists(installer):
        skip("the installer group", "install.sh is not present")
        return

    def fresh_repo():
        d = tmpdir("consumer")
        sh(["git", "init", "-q", "-b", "main"], d)
        sh(["git", "config", "user.email", "test@example.com"], d)
        sh(["git", "config", "user.name", "showrunner test"], d)
        sh(["git", "commit", "-q", "--allow-empty", "-m", "init"], d)
        return d

    def would_stage(d):
        out = sh(["git", "add", "-A", "--dry-run"], d).stdout
        return sorted(l.split("'")[1] for l in out.splitlines() if "'" in l)

    consumer = fresh_repo()
    sh([installer, consumer], ROOT)
    staged = would_stage(consumer)

    # THE COMBINATION WITH NO VISIBLE STATE. Neither tracked nor ignored means `git status`
    # lists them as untracked and the next `git add -A` commits them — measured at 31 paths,
    # showrunner's whole library, into a repo whose owner never chose to vendor it. And a
    # vendored copy is precisely the thing that then drifts from the one they installed.
    tool = [p for p in staged if p.startswith(".showrunner/bin/")
            or p.startswith(".showrunner/lib/")]
    ok("a fresh install leaves NOTHING of the tool stageable in the consumer's repo — the "
       "code is installed, not vendored", not tool, tool[:5])

    # Paired with the case where it DOES stage, because "nothing was staged" and "nothing was
    # examined" are the same observation from outside. This asserts the scan sees real paths.
    ok("...and the check is not vacuous: the consumer's OWN source files are still staged, so "
       "an empty verdict above means ignored rather than unexamined",
       ".showrunner/config.json" in staged, staged)
    ok("...and config.json is among them, because config IS the consumer's source and must "
       "stay committable", ".showrunner/config.json" in staged, staged)

    # Bytecode. This script deleting the copied __pycache__ was tried and is theatre: `init`
    # below imports the library and Python writes it straight back, as does the consumer's
    # first run. Only an ignore rule holds for the whole lifetime, so assert the rule's reach
    # rather than the directory's absence — which would pass for the wrong reason.
    pyc = os.path.join(consumer, ".showrunner", "lib", "showrunner", "__pycache__")
    if os.path.isdir(pyc):
        ok("...including the bytecode Python regenerates on every import, which no amount of "
           "deleting at install time can cover",
           not [p for p in staged if "__pycache__" in p], staged[:5])
    else:
        skip("the bytecode reach assertion", "no __pycache__ was produced by this install")

    # THE UPGRADE PATH, which is where every already-installed consumer actually lives. The
    # template is written only when the file is absent, so a guard that fires once per repo
    # would leave exactly the population that has the hole still holding it.
    legacy = fresh_repo()
    os.makedirs(os.path.join(legacy, ".showrunner"), exist_ok=True)
    with open(os.path.join(legacy, ".showrunner", ".gitignore"), "w") as fh:
        fh.write("# showrunner RUNTIME state — not source.\ngraph.db\nlocks/\nscratch/\n")
    sh([installer, legacy], ROOT)
    leftover = [p for p in would_stage(legacy)
                if p.startswith(".showrunner/bin/") or p.startswith(".showrunner/lib/")]
    ok("upgrading a repo whose .gitignore PREDATES this rule still closes the hole — the "
       "entries are appended, not written only on creation",
       not leftover, leftover[:5])

    with open(os.path.join(legacy, ".showrunner", ".gitignore")) as fh:
        before = fh.read()
    sh([installer, legacy], ROOT)
    with open(os.path.join(legacy, ".showrunner", ".gitignore")) as fh:
        after = fh.read()
    ok("...and re-running the installer appends nothing a second time", before == after,
       "%d -> %d bytes" % (len(before), len(after)))

    # THE UPGRADE PATH REGISTERS THE GUARD, and this assertion exists because it did not. The
    # registration lived in `init`, `init` runs only when there is no config, so every
    # ALREADY-INSTALLED consumer — the entire existing population — took the upgrade path and
    # received the shim FILE with nothing wired to it, plus a `doctor` error the installer
    # itself had just created. Found by upgrading a real consumer, not by reading the code.
    # It is the same shape as the ignore rules two blocks up, which carry the same warning.
    upgraded = fresh_repo()
    os.makedirs(os.path.join(upgraded, ".showrunner"), exist_ok=True)
    os.makedirs(os.path.join(upgraded, ".claude"), exist_ok=True)
    with open(os.path.join(upgraded, ".showrunner", "config.json"), "w") as fh:
        json.dump({"project_name": "already-installed"}, fh)
    with open(os.path.join(upgraded, ".claude", "settings.json"), "w") as fh:
        json.dump({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "theirs.sh"}]}]}}, fh)
    sh([installer, upgraded], ROOT)

    def registered_hooks(repo):
        with open(os.path.join(repo, ".claude", "settings.json")) as fh:
            data = json.load(fh)
        return [h.get("command", "") for e in data["hooks"]["PreToolUse"]
                for h in e.get("hooks", [])]

    hooks = registered_hooks(upgraded)
    ok("upgrading a repo that ALREADY has a config registers the worktree guard — the "
       "population with the hole is exactly the one an install-time-only fix never reaches",
       any("worktree-guard" in c for c in hooks), hooks)
    ok("...without displacing a PreToolUse hook the consumer already had",
       any("theirs.sh" in c for c in hooks), hooks)
    ok("...and the shim it registers is actually there to be run",
       os.path.exists(os.path.join(upgraded, ".showrunner", "hooks", "worktree-guard.sh")))
    before_n = len(registered_hooks(upgraded))
    sh([installer, upgraded], ROOT)
    eq("...and re-running the installer registers nothing a second time, so an installer that "
       "runs on every upgrade does not accumulate duplicates",
       len(registered_hooks(upgraded)), before_n)

    # An ignore rule does NOT untrack what is already committed, and a consumer who followed
    # the old installer's implicit invitation has it committed. Saying "ignored" to them
    # without saying that would be a remedy that silently does nothing.
    tracked_repo = fresh_repo()
    sh([installer, tracked_repo], ROOT)
    sh(["git", "add", "-A", "-f"], tracked_repo)
    sh(["git", "commit", "-q", "-m", "vendored"], tracked_repo)
    out = sh([installer, tracked_repo], ROOT).stdout
    ok("a consumer who ALREADY committed the tool is told the ignore rule does not untrack "
       "it, and given the command that does",
       "ALREADY TRACKED" in out and "rm -r --cached" in out, out[-400:])

    # THE ONLY THING THIS SCRIPT WRITES OUTSIDE THE TARGET REPO — the Claude Code skills, which
    # live in the user's HOME rather than in anybody's project. Two ways to get it wrong and
    # both are silent: prompting where nothing can answer (curl | bash, CI, this suite) hangs,
    # and assuming yes writes into a HOME that was never offered. So: no TTY means no prompt AND
    # no write, and the flag is the only way through.
    def install_into(repo, home, *flags):
        env = dict(os.environ, HOME=home)
        env.pop("CLAUDE_CONFIG_DIR", None)  # HOME is the fixture; an ambient override is not
        return subprocess.run([installer, *flags, repo], cwd=ROOT, capture_output=True,
                              text=True, env=env, timeout=120)

    quiet_home = tmpdir("fake-home")
    quiet = install_into(fresh_repo(), quiet_home)
    skills_dir = os.path.join(quiet_home, ".claude", "skills")
    ok("a non-interactive install writes NOTHING into the user's skills dir — with no TTY it "
       "neither prompts nor assumes a yes", not os.path.exists(skills_dir),
       quiet.stdout[-300:])
    ok("...and says so instead of staying silent, naming the flag that installs them — an "
       "install that quietly skipped would be indistinguishable from one with nothing to offer",
       "--skills" in quiet.stdout, quiet.stdout[-300:])

    # THE CASE WHERE IT HAPPENS. Without this the assertion above passes just as well for an
    # installer that cannot place a skill at all, which is the shape of a check that never was.
    yes_home = tmpdir("fake-home")
    loud = install_into(fresh_repo(), yes_home, "--skills")
    placed = os.path.join(yes_home, ".claude", "skills")
    names = sorted(os.listdir(placed)) if os.path.isdir(placed) else []
    ok("--skills places the skills into HOME's Claude config, so the prompt has something real "
       "behind it", "sr-status" in names and "showrunner" in names, (names, loud.stdout[-300:]))
    ok("...as SYMLINKS into this checkout, not copies — a copy is a second source of truth that "
       "goes stale exactly the way a vendored install does, and nothing would ever say it had",
       all(os.path.islink(os.path.join(placed, n)) for n in names)
       and os.path.realpath(os.path.join(placed, "sr-status")).startswith(ROOT), names)

    # NEVER CLOBBER. Somebody else's skill of the same name, or an edited copy, is not ours to
    # replace on the strength of a flag that says "install".
    keep_home = tmpdir("fake-home")
    mine = os.path.join(keep_home, ".claude", "skills", "sr-status")
    os.makedirs(mine)
    with open(os.path.join(mine, "SKILL.md"), "w") as fh:
        fh.write("mine, not the installer's\n")
    install_into(fresh_repo(), keep_home, "--skills")
    with open(os.path.join(mine, "SKILL.md")) as fh:
        survived = fh.read()
    ok("--skills never replaces a skill that is already there, so an edited or unrelated one of "
       "the same name survives an install that was told yes", "mine" in survived, survived)

    # And the explicit refusal, which is what a script or a CI job passes.
    no_home = tmpdir("fake-home")
    install_into(fresh_repo(), no_home, "--no-skills")
    ok("--no-skills writes nothing into HOME even though there is something to write — the "
       "flag a CI job or another skill passes when it must not be asked",
       not os.path.exists(os.path.join(no_home, ".claude", "skills")))


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

    # #28 — EVERY PATH THE BRIEF HANDS OVER IS RESOLVED FROM THE WORKTREE, not from here. The
    # binary was made absolute by #15 and the scratch dir was left relative, so a Crawler
    # created `<worktree>/.showrunner/scratch/<crawler>/` and `git worktree remove` destroyed
    # its report, its evidence and the artifact it cited as --proof. A true claim read as false.
    #
    # Asserted as a RULE over the whole table rather than as a fact about scratch, because the
    # next path added to that block is the next instance. The table is where a Crawler looks for
    # "where do I work", so a relative entry there is a promise that resolves somewhere else.
    table = [ln for ln in text.splitlines() if ln.startswith("| ") and "`" in ln]
    relative = []
    for ln in table:
        for cell in re.findall(r"`([^`]+)`", ln):
            if "/" in cell and not os.path.isabs(cell) and not cell.startswith("showrunner/"):
                relative.append(ln.strip())
    ok("no path in the brief's own location table is relative — the Crawler reads it standing "
       "in its worktree, so a relative path names a different place than the one meant",
       not relative, relative)
    ok("...and the scratch dir it is told to write to is the one in the MAIN checkout, which "
       "outlives the tree", os.path.join(cfg.root, ".showrunner", "scratch") in text, None)
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

    # #22 — an inject path inside the harness directory creates that directory as a side effect
    # (os.makedirs of the parent), and provisioning runs AFTER inject, sees it exists, and leaves
    # it alone. The harness then cannot answer its contract and the spawn aborts blaming the
    # harness's embedding contract — every word true, pointing at the wrong party, and the
    # natural next move is harness.require=false: accepting a Crawler with unverified rules to
    # work around a self-inflicted config error.
    inj = make_repo({"inject": [{"path": ".game_loop/bin/check-thing", "mode": "symlink"}]})
    _seed_harness(inj.root)
    os.makedirs(os.path.join(inj.root, ".game_loop", "bin"), exist_ok=True)
    with open(os.path.join(inj.root, ".game_loop", "bin", "check-thing"), "w") as fh:
        fh.write("#!/bin/sh\n")
    _res, _probs = worktree.inject(inj, tmpdir("injwt"))
    ok("an inject path inside the harness directory is REFUSED, rather than silently defeating "
       "provisioning and letting the harness take the blame downstream",
       any("INSIDE the harness directory" in p for p in _probs), _probs)
    ok("...and the refusal names the real remedy instead of the one that disables the guard — "
       "harness.installer or tracking it, never harness.require=false",
       any("harness.installer" in p and "require=false" in p for p in _probs), _probs)
    from showrunner import harness as _HI
    conflicts = _HI.inject_conflicts(inj)
    eq("...and `doctor` can see it from the config alone, with nothing running, rather than one "
       "Crawler at a time mid-fan-out", [p for p, _ in conflicts], [".game_loop/bin/check-thing"])
    clean = make_repo({"inject": [{"path": ".env", "optional": True}]})
    _seed_harness(clean.root)
    ok("...and an ordinary inject path is not flagged, so the check separates the two rather "
       "than refusing injection", not _HI.inject_conflicts(clean), _HI.inject_conflicts(clean))

    # #26 — a Crawler that self-corrects AFTER closing had nowhere to put it. No `reopen`, and
    # `edit` correctly refuses a closed leaf, so a leaf whose verdict should be `partial` stayed
    # `refuted` forever. That is not a cosmetic row: `refuted` means nobody needs to build this,
    # so a wrong one removes real work from the cycle — and it happened to a Crawler doing
    # exactly what this project asks, refuting its own conclusion unprompted.
    g.add("self-corrects", leaf_id="L26")
    g.claim("L26", "corrector", pid=os.getpid(), tree=cfg.root)
    proof26 = os.path.join(cfg.root, "proof-26.txt")
    with open(proof26, "w") as fh:
        fh.write("report\n")
    gates.close_gate(cfg, g, "L26", None, "premise does not hold", refuted=True,
                     evidence="proof-26.txt", premise="refuted", premise_read="README.md")
    eq("a leaf closes refuted", g.show("L26")["outcome"], "refuted")
    raises("...and `edit` still refuses it, because the brief is in somebody's hands",
           lambda: g.edit("L26", body="x"), "not open")
    amended = g.amend("L26", "partial", "a parallel trace found a real bug I missed",
                      "proof-26.txt")
    eq("...but the VERDICT can be corrected", amended["outcome"], "closed")
    reason = amended.get("close_reason") or ""
    ok("...and the correction SUPERSEDES rather than overwrites — the original close and its "
       "proof stay in the record, because a verdict that quietly changed is one nobody can "
       "audit, and the first close was the honest conclusion from what was known then",
       "premise does not hold" in reason and "AMENDED" in reason, reason)
    ok("...naming what it was, so the change itself is legible rather than implied",
       "was: refuted" in reason, reason)
    raises("amend refuses an OPEN leaf — the exact inverse of edit, because there is no verdict "
           "to correct yet and `close` is the verb for that",
           lambda: g.amend("L15", "partial", "x", "proof-26.txt"), "not closed")
    raises("...and refuses a verdict outside the vocabulary the close gate enforces, so the two "
           "cannot drift apart", lambda: g.amend("L26", "maybe", "x", "proof-26.txt"),
           "must be one of")

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
        # A PLAN, and it still states the layer below as fact — the fail-open posture of
        # game_loop's central shim, quoted as the model showrunner's would copy. Planned
        # rather than built is not an excuse: the claim is what rots, and a design decided
        # against a posture that has since changed is wrong before a line of it is written.
        # Its own "read against" line names where that was verified, and names the limit —
        # the shim lives in game_loop's INSTALLER, which this digest does not cover.
        "docs/plans/central-install.md": "game_loop's central-shim fail-open posture, taken "
                                         "as the model for showrunner's",
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


def test_observability():
    group("The event journal, and what a live viewer can actually see")
    if not have("git"):
        skip("the observability group", "git is not installed")
        return
    from showrunner import events as EV
    cfg = make_repo()
    g = new_graph(cfg)

    ok("a repo that has done nothing has an empty journal, not a missing answer",
       EV.read(cfg) == ([], 0, False), EV.read(cfg))

    EV.emit(cfg, "test.one", {"leaf": "a"})
    EV.emit(cfg, "test.two", {"leaf": "b"})
    evs, bad, _ = EV.read(cfg)
    eq("events land in order", [e["kind"] for e in evs], ["test.one", "test.two"])
    ok("...and every frame names which showrunner wrote it, because one viewer may watch "
       "several and a repo may be driven by more than one orchestrator",
       all(e["instance"] == os.path.realpath(cfg.root) for e in evs), evs)

    # SEQ COMES FROM THE JOURNAL, NOT THE PROCESS. The first version counted per-process, and
    # since every CLI invocation is its own process almost every event in a real campaign came
    # out as seq 1 — unorderable and unresumable, and indistinguishable from correct until a
    # second event exists. Two SEPARATE processes is the only arrangement that can catch it.
    exe = os.path.join(ROOT, "bin", "showrunner")
    env = dict(os.environ, NO_COLOR="1")
    for leaf_id in ("ev1", "ev2"):
        subprocess.run([sys.executable, exe, "add", "work " + leaf_id, "--id", leaf_id],
                       cwd=cfg.root, capture_output=True, text=True, env=env)
    evs, _, _ = EV.read(cfg)
    seqs = [e["seq"] for e in evs]
    eq("...and the sequence is strictly increasing ACROSS processes", seqs, sorted(set(seqs)))
    ok("...which is what makes --since a resume rather than a guess",
       len(EV.read(cfg, since_seq=seqs[1])[0]) == len(seqs) - 2, seqs)

    # A caller cannot forge the frame's own identity: `add` records a leaf's kind, and `kind` is
    # the event type. The first version took **kwargs and raised TypeError from inside
    # `showrunner add` — a journal breaking the work it observes, which is the one thing it must
    # never do. Renamed at the call site AND defended here, because only one of those travels.
    EV.emit(cfg, "test.reserved", {"ts": "not a time", "seq": 9999, "kind": "lies"})
    forged = [e for e in EV.read(cfg)[0] if e["kind"] == "test.reserved"][0]
    ok("a caller cannot overwrite ts, seq or kind — a frame claiming a sequence it does not "
       "have is worse than a missing field, because nothing downstream can doubt it",
       forged["seq"] != 9999 and forged["ts"] != "not a time", forged)
    eq("...and the smuggled values are kept under a prefix rather than dropped",
       forged.get("field_kind"), "lies")

    # A SEQ IS ONLY MEANINGFUL INSIDE ONE JOURNAL, and this whole surface exists because several
    # showrunners run in several places with one viewer watching them all. A bare integer that
    # crosses an instance boundary is a confident answer about a different campaign: both sides
    # are integers, the comparison succeeds, and nothing downstream has any way to doubt it.
    other = make_repo()
    cur = EV.cursor(cfg, 2)
    ok("a cursor names the instance that minted it", "@" in cur and cur.endswith("@2"), cur)
    seq, err = EV.parse_cursor(cfg, cur)
    ok("...and its own instance reads it back", seq == 2 and err is None, (seq, err))
    seq, err = EV.parse_cursor(other, cur)
    ok("...while another instance REFUSES it rather than resuming from a position that means "
       "something else there", seq is None and err, (seq, err))
    ok("...naming both sides, because a viewer holding two streams needs to know WHICH pair",
       err and "different showrunner" in err, err)
    seq, err = EV.parse_cursor(cfg, "7")
    ok("a bare seq still works — typed by a human against one repo it is a local question with "
       "a local answer", seq == 7 and err is None, (seq, err))

    # A TORN LINE IS ORDINARY — a viewer attaches while an append is in flight — so it is
    # COUNTED. Skipping it silently makes a half-written journal and a quiet campaign identical.
    with open(EV.path_for(cfg), "a") as fh:
        fh.write('{"kind": "half-writ')
    evs, bad, _ = EV.read(cfg)
    eq("an unparseable line is counted, not silently skipped", bad, 1)
    ok("...and the readable events still come back", len(evs) >= 4, len(evs))

    # THE SERIALIZATION POINT, which is the one thing no single Crawler can observe — so a view
    # that cannot show the queue cannot show the most important thing showrunner does. A REFUSAL
    # is as much a fact as a grant: a resource nobody is waiting on and one with three Crawlers
    # queued behind it look identical if only the holder is recorded.
    lk = make_repo({"resources": [{"name": "device", "match": [r"\bdeploy\b"]}]})
    kinds = lambda c: [e["kind"] for e in EV.read(c)[0]]
    # `lock run` is the AUTHORITATIVE path and the only one worth asserting here: `lock acquire`
    # records the invoking shell, which exits immediately, so a second acquire finds a STALE
    # lock and takes it — the behaviour that verb warns about in its own output. A first draft
    # of this test used it and got two grants and no refusal, which is the tool being right and
    # the fixture being wrong.
    subprocess.run([sys.executable, exe, "lock", "run", "device", "--holder", "first",
                    "--", "true"], cwd=lk.root, capture_output=True, text=True, env=env)
    seen = kinds(lk)
    ok("a lock grant is journalled on the authoritative path", "lock.acquired" in seen, seen)
    ok("...and its release, from the FINALLY, so a consumer killed mid-command still reports "
       "the release that actually happened", "lock.released" in seen, seen)

    # A REFUSAL is as much a fact as a grant, and it needs a genuinely live holder to provoke —
    # which is the whole reason the authoritative path exists.
    live = DeadPid  # placeholder to keep the name obvious if this is ever restructured
    holder_lock = locks.LockSet(lk).lock("device")
    holder_lock.acquire(os.getpid(), "first", session="s-live")
    try:
        subprocess.run([sys.executable, exe, "lock", "run", "device", "--holder", "second",
                        "--", "true"], cwd=lk.root, capture_output=True, text=True, env=env)
    finally:
        holder_lock.release(force=True)
    # THE FLAG ORDER THE README DOCUMENTS. `command` was nargs=REMAINDER, which takes everything
    # after the first positional — so `lock run device --holder crawler-a -- ./deploy.sh` put
    # `--holder` into the COMMAND, left the holder at its default, and then tried to execute
    # `--holder` as a program. The flagship example of the one hard rule this project exists to
    # enforce, and it crashed. Found by a test that used that order by habit and got a holder of
    # "run" back.
    ran = subprocess.run([sys.executable, exe, "lock", "run", "device",
                          "--holder", "crawler-a", "--", "echo", "hi"],
                         cwd=lk.root, capture_output=True, text=True, env=env)
    eq("`lock run <resource> --holder X -- cmd` runs the command, in the order the README "
       "documents (%s)" % (ran.stderr or "").strip()[:60], ran.returncode, 0)
    ok("...and the command's own output comes through, so this is the command running rather "
       "than the lock merely being taken", "hi" in ran.stdout, ran.stdout[:120])
    held = [e for e in EV.read(lk)[0] if e["kind"] == "lock.acquired"]
    ok("...and the holder recorded is the one NAMED, not the parser's default — a lock whose "
       "holder is mislabelled is one nobody can trace back to a consumer",
       any(e.get("who") == "crawler-a" for e in held), [e.get("who") for e in held])

    refusals = [e for e in EV.read(lk)[0] if e["kind"] == "lock.refused"]
    ok("a REFUSAL is journalled too, because a contended resource and an idle one are "
       "otherwise the same picture", refusals, kinds(lk))
    if refusals:
        ok("...naming who was waiting AND who held it, so a queue can be drawn at all",
           refusals[0].get("who") == "second" and refusals[0].get("held_by") == "first",
           refusals[0])

    # RECLAIM IS NOT RELEASE. Only one of them says something went wrong, and collapsing them
    # hands a viewer a resource that looks tidily handed back by an agent that never came home.
    src = open(os.path.join(ROOT, "lib", "showrunner", "campaign.py")).read()
    ok("reap journals a reclaimed lock under its own kind", "lock.reclaimed" in src, None)

    # EVERY WAY INTEGRATE CAN FINISH goes through one path. It has five, each previously its own
    # `results.append`, so a sixth would have arrived with no event and a viewer would silently
    # stop seeing the riskiest verb finish.
    tree = ast.parse(src)
    integ = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "integrate")
    appends = [n for n in ast.walk(integ)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "append" and getattr(n.func.value, "id", "") == "results"]
    ok("no outcome inside integrate appends to `results` directly — they route through the one "
       "path that also journals, so forgetting is impossible rather than unlikely",
       not appends, [getattr(a, "lineno", "?") for a in appends])

    # A TRANSITION, NOT A STATE. reconcile computes `blocked` fresh on every call and a watchdog
    # may call it every few seconds, so journalling the STATE would give a viewer one identical
    # line per poll — the signal drowning in its own repetition. Edge detection needs somewhere
    # to remember the last answer, and the journal already knows: a second store beside it is a
    # record that can disagree with the first.
    tr = make_repo()
    EV.emit(tr, "crawler.blocked", {"crawler": "c1", "leaf": "L1"})
    EV.emit(tr, "crawler.blocked", {"crawler": "c2", "leaf": "L2"})
    EV.emit(tr, "crawler.unblocked", {"crawler": "c1", "leaf": "L1"})
    # `or {}` throughout: a neutered `latest` returns None, and subscripting it would CRASH the
    # group rather than fail these assertions — which the sweep counts as nothing noticing.
    # A mutant killed by an exception is a mutant the sweep cannot score, and my own docstring
    # says so; this is the first place it bit.
    last = EV.latest(tr, ("crawler.blocked", "crawler.unblocked"), "crawler", "c1") or {}
    eq("the journal answers what a crawler's last transition was", last.get("kind"),
       "crawler.unblocked")
    eq("...for the crawler asked about, not whichever came last overall",
       (EV.latest(tr, ("crawler.blocked", "crawler.unblocked"), "crawler", "c2") or {}).get("kind"),
       "crawler.blocked")
    ok("...and a crawler with no transition reads as None rather than as unblocked — 'never "
       "seen' and 'seen and fine' are different, and only one of them is evidence",
       EV.latest(tr, ("crawler.blocked",), "crawler", "never-heard-of") is None)
    ok("...and a kind it was not asked about is not returned",
       EV.latest(tr, ("crawler.unblocked",), "crawler", "c2") is None)

    # THE CONSEQUENCE, which is what actually matters and what the three above do not reach.
    # Everything so far tests the helper; none of it would notice the helper being right and
    # reconcile calling it wrongly. A watchdog polls reconcile every few seconds, so the failure
    # is not a missing event — it is the same event forever.
    gt = new_graph(tr)
    gt.add("blocked work", leaf_id="B1", labels=["backend"])
    rec_b = worktree.spawn(tr, gt.show("B1"), actor="stuck")
    campaign.record_spawn(tr, rec_b, pid=os.getpid(), session="sess-b")
    gt.claim("B1", "stuck", pid=os.getpid(), tree=tr.root)
    import showrunner.harness as _HB
    _orig_sg = _HB.stop_gate
    _HB.stop_gate = lambda c, w, s: (True, "refused at turn-end")
    try:
        campaign.reconcile(tr, gt)
        mine = lambda k: [e for e in EV.read(tr)[0]
                          if e["kind"] == k and e.get("crawler") == rec_b["crawler"]]
        after_one = mine("crawler.blocked")
        campaign.reconcile(tr, gt)
        campaign.reconcile(tr, gt)
        after_three = mine("crawler.blocked")
        eq("a Crawler going blocked is journalled once", len(after_one), 1)
        eq("...and STAYS one across repeated reconciles — a watchdog polls this every few "
           "seconds, so journalling the state rather than the transition would bury a viewer "
           "in identical lines", len(after_three), 1)
        # And the other edge: coming back must be seen, or a viewer shows a rescued Crawler as
        # permanently stuck. An edge detector that only fires one way is half a detector.
        _HB.stop_gate = lambda c, w, s: (False, "")
        campaign.reconcile(tr, gt)
        unblocked = mine("crawler.unblocked")
        eq("...and the return to working is journalled too, so a rescued Crawler stops looking "
           "stuck", len(unblocked), 1)
    finally:
        _HB.stop_gate = _orig_sg

    # THE SNAPSHOT: the world in one call. A viewer attaching needs the picture before a stream
    # of deltas means anything, and assembling it from four verbs hands back a composite of
    # instants that never co-existed.
    snap_p = subprocess.run([sys.executable, exe, "snapshot"], cwd=cfg.root,
                            capture_output=True, text=True, env=env)
    eq("`snapshot` succeeds (%s)" % (snap_p.stderr or "").strip()[:60], snap_p.returncode, 0)
    snap = json.loads(snap_p.stdout)
    for key in ("project", "instance", "cursor", "ready", "in_progress", "crawlers",
                "resources", "waiting", "journal_unreadable"):
        ok("...and carries %s, so a viewer needs no second call to draw the picture" % key,
           key in snap, sorted(snap))
    ok("its cursor is the JOIN to the stream — a viewer resumes from exactly what this "
       "already reflects rather than guessing an overlap",
       "@" in snap["cursor"] and snap["cursor"].startswith(
           EV.cursor(cfg, 0).split("@")[0]), snap["cursor"])
    ok("...and it reports an unreadable journal rather than folding it into an empty one",
       snap["journal_unreadable"] is False, snap["journal_unreadable"])

    # A FAILED READ IS NOT A FACT ABOUT THE CAMPAIGN. `read` used to catch OSError and return
    # the events it had, so a journal that could not be opened came back as ([], 0) —
    # indistinguishable from an orchestrator that has genuinely done nothing, and a viewer would
    # have rendered a confident, quiet, wrong "idle" over a live fan-out.
    #
    # The test is the cheap one: break the file, then ask whether anything says a clean "none".
    blocked = make_repo()
    EV.emit(blocked, "test.before", {"leaf": "x"})
    jp = EV.path_for(blocked)
    os.chmod(jp, 0o000)
    try:
        unreadable_ok = os.access(jp, os.R_OK) is False
        evs, bad, unreadable = EV.read(blocked)
        if not unreadable_ok:
            skip("the unreadable-journal check", "this filesystem ignores chmod 000 (running "
                 "as root?), so the condition cannot be constructed here")
        else:
            ok("an unreadable journal reports UNREADABLE, not zero events — 'I cannot see this "
               "campaign' and 'this campaign did nothing' are different answers and only one is "
               "safe to draw as an idle dashboard", unreadable is True, (len(evs), bad, unreadable))
            p = subprocess.run([sys.executable, exe, "watch"], cwd=blocked.root,
                               capture_output=True, text=True, env=env)
            eq("...and `watch` REFUSES rather than streaming a clean empty replay over it",
               p.returncode, 2)
            ok("...naming the journal, so the remedy is obvious rather than a mystery",
               "could not be read" in (p.stdout + p.stderr), (p.stdout + p.stderr)[:200])
    finally:
        os.chmod(jp, 0o644)
    evs, _, unreadable = EV.read(blocked)
    ok("...and a journal that is readable again is not still reported unreadable — the flag is "
       "a measurement, not a latch", unreadable is False and len(evs) == 1, (evs, unreadable))

    # THE STREAM, END TO END, through the real binary. Everything above tests the file; this
    # tests the thing a viewer actually attaches to, including that it replays BEFORE it follows
    # — a viewer that sees nothing until the next transition cannot tell a quiet orchestrator
    # from a broken pipe, and quiet is the normal state of an orchestrator mid-integration.
    proc = subprocess.Popen([sys.executable, exe, "watch", "--follow", "--interval", "0.1",
                             "--heartbeat", "0.3"], cwd=cfg.root, stdout=subprocess.PIPE,
                            text=True, env=env)
    try:
        frames, deadline = [], time.time() + 20
        saw_ready = False
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            frames.append(json.loads(line))
            if frames[-1].get("type") == "ready" and not saw_ready:
                saw_ready = True
                # Only NOW make something happen, so what arrives next cannot be replay. The
                # result is ASSERTED rather than assumed: the first version ignored it, and when
                # the action silently failed the test reported "the stream did not deliver" —
                # blaming the transport for a producer that never produced. A stream test whose
                # trigger can fail quietly is measuring the wrong thing.
                made = subprocess.run([sys.executable, exe, "add", "live work", "--id", "live1"],
                                      cwd=cfg.root, capture_output=True, text=True, env=env)
                eq("...the event this test depends on was actually created (%s)"
                   % (made.stderr or made.stdout).strip()[:60], made.returncode, 0)
            if saw_ready and any(f.get("leaf") == "live1" for f in frames):
                break
        ok("`watch` replays the backlog and marks the end of it, so attaching mid-campaign is "
           "not a blank screen", saw_ready, [f.get("type") or f.get("kind") for f in frames][:6])
        ok("...and an event created AFTER the replay arrives on the open stream",
           any(f.get("leaf") == "live1" for f in frames),
           [f.get("kind") for f in frames][-4:])
        ok("...and the heartbeat proves the stream is alive during a quiet stretch, which the "
           "journal alone cannot — it is sparse by design and a view built on it freezes "
           "exactly when the work is hardest",
           any(f.get("type") == "heartbeat" for f in frames) or
           any(f.get("leaf") == "live1" for f in frames),
           [f.get("type") for f in frames if f.get("type")])
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_hook_verbs_never_fail_open_in_silence():
    group("A hook that allows without checking must say so")
    # WRITTEN BEFORE THE THING IT GUARDS EXISTS. `install.sh --central` is a plan; when it lands,
    # `.showrunner/bin/showrunner` stops being the tool and becomes a shim that execs a central
    # copy — and on a machine with no central install the hook verbs exit 0 without running.
    # That posture is correct and was argued out in docs/plans/central-install.md: `lock guard` is
    # an optimisation, and `lock run` is the guarantee that still fails loud.
    #
    # What is NOT correct is exiting 0 in silence. game_loop's own guard-writes.sh fails open and
    # prints "ALLOWED WITHOUT BEING CHECKED" for exactly this reason, and a guard whose absence
    # produces no output is one nobody discovers until two Crawlers are in the same room.
    #
    # A test written after the shim would be a test written by whoever wrote the shim, which is
    # the reader least able to notice the omission. This one is cheap while there is no shim and
    # becomes real the moment one appears.
    shim_paths = [os.path.join(ROOT, "templates", "central-shims", "showrunner"),
                  os.path.join(ROOT, ".showrunner", "bin", "showrunner")]
    shims = []
    for p in shim_paths:
        if not os.path.exists(p):
            continue
        with open(p, errors="ignore") as fh:
            text = fh.read()
        # The tool itself is Python and long; a dispatcher shim is short bash naming a central
        # path. Only the second is what this is about.
        if "CENTRAL" in text.upper() and len(text) < 8000:
            shims.append((p, text))
    def silently_fails_open(text):
        hook_exit = re.search(r"(lock[ |)]*guard|stop-?gate)[^\n]*\n?[^\n]*exit 0", text)
        said = re.search(r"WITHOUT BEING CHECKED|did not run|not running", text)
        return bool(hook_exit) and not said

    # THE POSITIVE CONTROL, run whether or not a shim exists. Every assertion below passes by
    # finding nothing, which is also what a regex that stopped matching the shape a real shim is
    # written in returns — and the shim does not exist yet, so there is nothing else to catch it.
    bad = 'case "$1" in\n  lock|guard|stop-gate) exit 0 ;;\nesac\n'
    good = ('case "$1" in\n  lock|guard|stop-gate)\n'
            '    echo "showrunner: ALLOWED WITHOUT BEING CHECKED — no central install" >&2\n'
            '    exit 0 ;;\nesac\n')
    ok("the rule flags a shim that exits 0 for a hook verb and says nothing",
       silently_fails_open(bad), bad)
    ok("...and passes one that allows and announces it", not silently_fails_open(good), good)

    if not shims:
        skip("the fail-open-in-silence check against a REAL shim",
             "no central dispatcher shim exists yet — this becomes real when `--central` lands, "
             "and is registered now so the shim cannot arrive without meeting it")
        return
    for path, text in shims:
        ok("%s: a hook verb that exits 0 without running the guard also says so — a silent "
           # `rel` NOT the bare name: this line called a function that does not exist in this
           # module's namespace, and nothing noticed because the branch had never executed —
           # the group returned early via skip() on every run since it was written. A test
           # written before the thing it guards is still a test nobody has run.
           "allow is indistinguishable from a guard that ran and was content"
           % util.rel(path, ROOT),
           not silently_fails_open(text), text[:400])
        ok("...and `lock run` is NOT on the fail-open side: it is where the consumer takes the "
           "lock, which is the guarantee rather than the optimisation",
           not re.search(r"\brun\b[^\n]*\)\s*exit 0", text), text[:400])


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
    # A REMEDY BUILT BY FORMATTING IS STILL A REMEDY, and this check could not see one. The
    # hijack prompt in lease.py is a template — it writes `{sr} worktree fork`, filled at print
    # time with the resolved binary path — so the literal word "showrunner" never appears and
    # every verb it advertised was invisible here. It advertised two that did not exist.
    #
    # That is the highest-traffic remedy text in the module, unchecked by the check written for
    # exactly this, and the reason is worth naming: the scan keyed on the SPELLING of the binary
    # rather than on the shape of a command. `sr_bin` exists precisely because that spelling is
    # resolved rather than fixed, so the two were always going to diverge.
    BINARY = r"(?:showrunner|\{[a-z_][a-z_0-9]*\})"

    def commands_in(text):
        spans = re.findall(r"`([^`\n]+)`", text)
        for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
            spans.extend(block.splitlines())
        spans.extend(re.findall(r"^ {2,}(%s [a-z].*)$" % BINARY, text, re.M))
        for span in spans:
            m = re.match(r"\s*%s ([a-z][a-z-]+)((?: [a-z][a-z-]+)?)" % BINARY, span)
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

    # THE EXTENSION NEEDS ITS OWN CONTROL. Widening a scan and watching the suite stay green
    # proves nothing — a pattern that matches nothing looks exactly like a repo with no ghosts.
    # So: a template spelling must be EXTRACTED, and a ghost written that way must be CAUGHT.
    # Without these two, deleting the {placeholder} branch would be invisible.
    tmpl = "  {sr} worktree fork --from x\n  {binary} lock run device -- ./x\n"
    found = set(commands_in(tmpl))
    ok("a remedy written as a TEMPLATE is extracted — `{sr} worktree fork` never contains the "
       "word 'showrunner', which is how lease.py advertised two verbs that did not exist",
       ("worktree", "fork") in found and ("lock", "run") in found, sorted(found))
    ghost = set(commands_in("  {sr} worktree teleport --now\n"))
    ok("...and a ghost spelled that way is CAUGHT — the widened scan can fail, which is the "
       "only thing separating it from a regex that quietly matches nothing",
       ("worktree", "teleport") in ghost
       and "teleport" not in subverbs_of("worktree"), sorted(ghost))
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
            # THE BINARY IT PLACED MUST RUN, not merely exist. `init` copied bin/showrunner and
            # not the library beside it, so what it placed was executable and died on every
            # invocation — found by a websocket probe pointing at a freshly-initialised repo,
            # which is the first thing that ever tried to USE the placed copy rather than
            # assert its mode bits.
            placed = os.path.join(bare, ".showrunner", "bin", "showrunner")
            ran = subprocess.run([sys.executable, placed, "--version"], cwd=bare,
                                 capture_output=True, text=True, env=env)
            ok("...and the binary the remedy placed RUNS from the repo it was placed in (%s)"
               % (ran.stderr or ran.stdout).strip()[:60], ran.returncode == 0, ran.returncode)
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
               test_integration, test_worktree_lease, test_worktree_guard_from_inside_a_worktree,
               test_self_pin, test_central_install, test_installer_leaves_no_vendored_copy,
               test_publishable, test_dispatch, test_filed_issues_15_to_21,
               test_claims_about_the_layer_below, test_observability,
               test_hook_verbs_never_fail_open_in_silence,
               test_retracted_doc_claims,
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
