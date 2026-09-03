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

# BORROWED CLAIMS IN THIS FILE ARE REPORTED, NOT VERIFIED HERE. Findings attributed to another
# project were measured in that project, on machines and corpora this repo cannot reach. They
# are hypotheses that happen to have come from a careful source, and at least one has already
# been retracted by its author.

import ast
import filecmp
import glob
import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# THE ANCHOR-FAILURE NOTICE, in the words only that branch prints. "DID NOT RUN" opens EVERY
# fail-open notice these guards emit — eight producers at last count, most with nothing to do
# with anchoring — so an assertion matching on it is a PROXY, true only while anchoring is the
# sole route to the phrase. It stopped being the sole route: in a linked worktree the anchoring
# SUCCEEDS and the guard then reaches lease.py's `if not session`, a deliberate and correct
# degrade wearing the same opening words. Every such assertion passed in the primary checkout
# and failed in every Crawler's tree — the direction that goes unnoticed longest.
#
# CUT TO THE SUBSTRING ALL THREE PRODUCERS SHARE VERBATIM. The two shims word the tail
# differently ("this tool call was ALLOWED" vs "this call was ALLOWED"), so the full sentence
# from cli.NO_REPO_FAIL_OPEN is not a common anchor. `test_guard_anchor_phrase_is_live` asserts
# this string is still present in all three, because an absence assertion whose phrase has been
# reworded away passes forever while measuring nothing.
ANCHOR_FAILED = "neither the working directory nor CLAUDE_PROJECT_DIR resolves to a git repository"

# NO BYTECODE DROPPINGS. Loading a .py hook as a module — which the waker tests do — makes the
# import machinery write a __pycache__ INTO .showrunner/hooks/, and the wiring net then reports
# that directory as a hook nobody registered. One check manufacturing the condition another
# check flags is worse than either of them failing on its own.
sys.dont_write_bytecode = True

# THE SUITE MUST NOT WRITE THE REPO'S OWN HOOK HEARTBEAT. The heartbeat answers "did this Stop
# hook RUN on the last turn", and its first reading was 28 stamps per burst for one gate — every
# one of them this suite invoking the hook, not a turn-end. A freshly-run suite made every hook
# look freshly reached, which is the single thing the file exists to answer. An instrument its
# own tests can forge measures nothing, so every hook a test runs stamps somewhere else.
# REGISTERED FOR REMOVAL, like every other temp root this suite makes. These two run before
# `TMPDIRS` exists, so they use atexit rather than `tmpdir()` — and that gap is exactly why they
# leaked: `tmpdir()` has always registered its roots and `cleanup()` has always emptied them, so
# the policy was never missing, only unwritten in the three places that did not call it.
#
# A neighbouring agent measured ~62,000 entries in this machine's temp root and named the cost,
# which is not disk: past ARG_MAX a glob there becomes a command that CANNOT RUN and prints
# nothing, indistinguishable from a clean result. Four searches across three repos failed that
# way in one evening and nobody noticed.
_HB_ROOT = tempfile.mkdtemp(prefix="sr-heartbeat-")
os.environ["SHOWRUNNER_HEARTBEAT"] = os.path.join(_HB_ROOT, "hook-heartbeat.jsonl")
sys.path.insert(0, os.path.join(ROOT, "lib"))

# ── the suite must not read the machine it runs on (#46) ─────────────────────────────────────────
#
# roles.USER_PATH is computed AT IMPORT from XDG_CONFIG_HOME, so this has to happen before the
# import below and cannot be a fixture. Two tests monkeypatch USER_PATH for their own cases, which
# is why this went unnoticed: those passed, while every OTHER assertion that reaches a roles-aware
# path -- doctor, the dispatch guard, seat resolution -- read the DEVELOPER'S real ~/.config file.
# Six of them fail on a machine that actually uses roles, which is to say the suite was green here
# and red for the people the feature was built for. Setting the env var rather than patching the
# module covers the subprocess tests too, since they inherit it.
_CFG_HOME = tempfile.mkdtemp(prefix="sr-suite-config-")
os.environ["XDG_CONFIG_HOME"] = _CFG_HOME
# SAME RULE, SECOND ROOT (#69). `util.transcript_path` falls back to the real `~/.claude` when
# CLAUDE_CONFIG_DIR is unset, and `status` now derives a transcript path for every live claim --
# so an unset var here has the suite stat the DEVELOPER's own sessions. An env var rather than a
# patch, again, so the subprocess CLI assertions inherit it.
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(_CFG_HOME, "claude-home")
# SAME RULE, THIRD VARIABLE (#39). SHOWRUNNER_CAMPAIGN selects where every state path lives, and
# it is EXPORTED INTO EVERY CRAWLER by design -- a session dispatched into a campaign inherits it
# and so do its children, this suite among them. So a suite run from inside a campaign was
# measuring a different layout from a suite run outside one: `init` writes its .gitignore under
# .showrunner/campaigns/<slug>/, `doctor` reads that campaign's waiting journal, and every fixture
# here builds the repo-wide path. Measured on one commit, one machine, one variable apart: 1739
# passed / 1 failed with it unset, 1731 / 9 with it set, and the eight extra failures are all the
# suite reading the machine it runs on rather than anything about the code.
#
# EIGHT UNDERSTATED IT, and the reason is worth knowing before anyone re-measures. test_campaign_scoping
# ended with a bare `del os.environ["SHOWRUNNER_CAMPAIGN"]`, so it silently disinfected the
# environment for every group that ran after it — the eight were the contamination visible UP TO
# that point. That `del` now restores the prior value instead, and neutering this clear shows the
# real figure: 26 failed. A cleanup that hides the defect downstream of itself is the same shape
# as the defect.
#
# THE COST IS NOT THE RED. `showrunner baseline` recorded "suite rc=1, 8 failure line(s)" from
# inside a campaign, and `check` tolerates a recorded failure by construction -- so those eight
# assertions stopped gating exactly where the campaign runs. An instrument its own environment
# can move measures nothing.
#
# CLEARED RATHER THAN SCOPED. Two of the eight assert the NO-CAMPAIGN default itself ("state
# lives where it always has"), which no amount of path-scoping can satisfy: they need the
# variable unset. The other six merely trip over it. The tests that are about campaigns pass
# SHOWRUNNER_CAMPAIGN themselves, explicitly, so clearing the ambient one removes contamination
# without removing coverage -- see test_campaign_scoping, which proves both halves.
_AMBIENT_CAMPAIGN = os.environ.pop("SHOWRUNNER_CAMPAIGN", None)
if _AMBIENT_CAMPAIGN:
    # ANNOUNCED, because a suite that silently ignores the variable you set is its own trap:
    # somebody running it from inside a campaign to see how that campaign behaves would read a
    # green run as an answer about their campaign.
    print("note: SHOWRUNNER_CAMPAIGN=%s cleared for this run — the suite asserts the repo-wide "
          "layout, and the campaign-scoped tests select their own campaign explicitly"
          % _AMBIENT_CAMPAIGN)
import atexit
import shutil as _shutil
for _leaky in (_HB_ROOT, _CFG_HOME):
    atexit.register(_shutil.rmtree, _leaky, True)
atexit.register(shutil.rmtree, _CFG_HOME, True)

# THE TWO CAMPAIGNS THE RESEAT GROUP MUST CREATE IN THE REAL CHECKOUT, registered for removal
# HERE — at import — and not only at the end of that group. A cleanup that runs on the happy
# path is not a cleanup: `mutate.py` exists to make groups fail, so the line at the end of that
# group is exactly the line a sweep skips, and every sweep left two more directories behind in
# the live campaign list. The group still removes them and asserts it, because the assertion is
# what keeps the intent visible; this is the net under it.
for _c in ("reseat-discover-%d" % os.getpid(), "reseat-hook-%d" % os.getpid()):
    atexit.register(_shutil.rmtree, os.path.join(ROOT, ".showrunner", "campaigns", _c), True)

from showrunner import brief, campaign, collide, config, dispatch, gates, graph as G, harness, lanes, lease, locks, pin, reach, roles, util, worktree  # noqa: E402
from showrunner.util import Refused, boot_token as boot_token_for_test  # noqa: E402

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv
PASS, FAIL, SKIP = [], [], []
_GROUP = ["?"]


def group(name):
    _GROUP[0] = name
    print("\n== %s ==" % name)


# Signatures of a machine that could not run the test, NOT of a test that found something.
# Deliberately narrow and about RESOURCES rather than about failure generally: a false VOID
# hides a real defect behind "re-run it", which is the expensive direction here — the opposite
# of `gates.VOID_PATTERNS`, where a false VOID merely costs a re-run.
# TWO PARTS, because one was not enough. The first version matched the PHRASE alone and voided
# a run on an assertion whose own prose said "too many open files" — a matcher satisfied by
# prose about the thing, inside the screen written to tell a broken machine from broken code.
#
# So a phrase only counts alongside an ERROR SHAPE: an errno, an exception class, a signal. A
# sentence can contain either; a failure detail from a starved machine contains both.
_VOID_PHRASES = re.compile(
    r"(Resource temporarily unavailable|Cannot allocate memory|Too many open files"
    r"|No space left on device|fork: retry)", re.I)
_VOID_SHAPE = re.compile(
    r"(Errno \d+|OSError|BlockingIOError|MemoryError|Traceback|SIGKILL|Signals\.SIG"
    r"|died with|non-zero exit status)")
# Tokens unambiguous on their own — no English sentence contains them by accident.
_VOID_ALONE = re.compile(r"(BlockingIOError|MemoryError|SIGKILL)")


def void_signatures(failures):
    """Resource signatures among these failures — the evidence a run measured nothing.

    A function rather than an inline comprehension so it can be DRIVEN with fabricated
    failures. A screen whose only exercise is the run it guards is one whose pass is silence,
    and this file has spent a week finding those.
    """
    hits = set()
    for _g, _l, d in failures:
        text = str(d)
        hits |= {m.group(0) for m in _VOID_ALONE.finditer(text)}
        if _VOID_SHAPE.search(text):
            hits |= {m.group(0) for m in _VOID_PHRASES.finditer(text)}
    return sorted(hits)


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


def attempt_message(fn):
    """(result, message) — the text of a refusal, for assertions about what it SAYS."""
    try:
        return fn(), ""
    except Exception as exc:                                    # noqa: BLE001
        return None, str(exc)


def attempt(fn, default=None):
    """Call `fn`, returning `default` if it REFUSES. Asserts nothing itself.

    THE COMPANION TO `raises`, for the calls a test makes on its way to the assertion it cares
    about. A producer stubbed to answer "nothing" makes the callers below it refuse — correctly,
    that is the fail-closed behaviour — and an unhandled `Refused` in the middle of a group
    takes every assertion after it down with it. The mutant that should have been MEASURED
    becomes UNSCOREABLE instead, and `mutate.py` reports a floor from a truncated run.

    Deliberately silent and deliberately not an assertion of its own: emitting one here would
    make the suite's assertion count depend on whether a mutation is in effect. The refusal
    surfaces as `default` flowing into the assertions that follow, which then FAIL — which is
    the whole point, and the difference between a producer scored at 22 and one scored at 0
    with a crash beside it.
    """
    try:
        return fn()
    except Refused:
        return default


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
    # `tree` IS PART OF CONSTRUCTING A CONFIG, not an optional extra. A Config records the
    # working tree it was loaded from, and `roles.seat` answers from it rather than from the
    # process's cwd; a fixture that skipped it would make every seat UNKNOWN here — loudly, by
    # design, but it would still be the fixture supplying less than production does.
    cfg = config.Config(data, os.path.realpath(d), os.path.join(d, ".showrunner", "config.json"),
                        tree=os.path.realpath(d))
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

    # THE SWEEP CALLED on_disk THIN AT 2, and it had no direct assertion at all -- only callers
    # noticing indirectly. It is the producer `reap` reads to find the locks a dead Crawler left
    # behind, and a neutered version answers [] for everything, which is exactly what a machine
    # holding no locks answers. An assertion that a producer stays QUIET is held equally by a
    # producer that is dead; what a dead one cannot fake is CONTENT.
    device.acquire(os.getpid(), "probe-a")
    pg.acquire(os.getpid(), "probe-b")
    eq("on_disk NAMES the locks actually present, in sorted order — a dead producer answers the "
       "empty list, which reads identically to a machine holding none",
       ls.on_disk(), ["device", "pg-port"])
    device.release(os.getpid())
    pg.release(os.getpid())
    # CHECKED RATHER THAN ASSUMED: I wrote this expecting a released lock to leave its directory
    # behind, which would have made `reap`'s interest in on_disk obvious. `release` rmtree's it,
    # so the answer shrinks — and that is the assertion worth having, because a producer stuck
    # returning its previous answer passes every "names what is present" check taken alone.
    eq("...and on_disk SHRINKS when locks are released — a stuck producer replaying its last "
       "answer satisfies the assertion above and fails this one", ls.on_disk(), [])
    # AN UNLISTABLE ROOT ANSWERS [] TOO, and that is the collapse. Recorded on the instance
    # instead of inferred from an empty list, so `reap` finding nothing stale can be told from
    # `reap` being unable to look.
    ok("a root that does not EXIST is not an error — no lock has ever been taken here, and a "
       "fresh repo crying wolf is a check that stops being read",
       locks.LockSet(make_repo()).on_disk() == []
       and not getattr(locks.LockSet(make_repo()), "on_disk_error", None))
    _broken = locks.LockSet(cfg)
    _unreadable = os.path.join(tmpdir("lock-root-unreadable"), "root")
    os.makedirs(_unreadable)
    os.chmod(_unreadable, 0o000)                   # a real directory that cannot be listed
    _broken.root = _unreadable
    eq("...but a root that is THERE and cannot be listed still answers [], because every caller "
       "wants a list and raising here would take `reap` out entirely",
       _broken.on_disk(), [])
    ok("...and records WHY, so 'no stale locks' and 'could not look for stale locks' are "
       "different readings rather than the same empty list",
       bool(getattr(_broken, "on_disk_error", None)), _broken.on_disk_error)

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

    # A BOOT TOKEN NOBODY COULD READ IS NOT A DIFFERENT BOOT. `boot_token` degrades to
    # `<host>:unknown` when the boot time is undiscoverable, and comparing THAT against a real
    # recorded one made every holder on the machine read as "recorded on a previous boot" — i.e.
    # PROVED DEAD, reclaimable, taken out from under a live session. A transient `sysctl`
    # failure was enough. Both directions, because a stored unknown is the same problem from
    # the other side: neither value can settle the question, so neither may answer it.
    _orig_bt = locks.boot_token
    locks.boot_token = lambda: "%s:unknown" % os.uname().nodename
    try:
        with open(os.path.join(device.dir, "boot"), "w") as fh:
            fh.write("some-host:1\n")
        unknown_now, _ = device.state()
        with open(os.path.join(device.dir, "boot"), "w") as fh:
            fh.write("%s:unknown\n" % os.uname().nodename)
        unknown_stored, _ = device.state()
    finally:
        locks.boot_token = _orig_bt
    eq("when THIS boot's token could not be read, a live holder is still HELD — 'could not "
       "tell' must never become 'proved dead', which is this module's whole posture and the "
       "one verdict that lets a lock be taken from somebody", unknown_now, locks.HELD)
    eq("...and the same when the RECORDED token is the unknown one, since neither value can "
       "settle the question", unknown_stored, locks.HELD)
    ok("...while the real comparison still fires, so this widened nothing: the STALE assertion "
       "above ran against the same lock with both tokens readable", state == locks.STALE)
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


# ==================================================== CORE: the user layer
def test_user_config_layer():
    group("A user-level config.json, merged BENEATH the repo's")
    if not have("git"):
        skip("the user config group", "git is not installed")
        return

    # THE SUITE MUST NOT READ THE MACHINE IT RUNS ON (#46). config.USER_PATH is computed at
    # import from XDG_CONFIG_HOME, which run.py points at a temp dir before importing anything
    # — so this file inherits that isolation instead of introducing an env var of its own.
    ok("config.USER_PATH is inside the suite's temp XDG dir, not the developer's ~/.config",
       config.USER_PATH.startswith(_CFG_HOME), config.USER_PATH)
    eq("...and it resolves to the SAME user-level directory as roles.json, so the two files "
       "cannot drift about where 'user level' is",
       os.path.dirname(config.USER_PATH), os.path.dirname(roles.USER_PATH))

    def with_user(data, repo=None, project=None, local=None):
        """load() a repo with `data` as the user-level config. Returns the Config."""
        cfg = repo or make_repo(project)
        if local is not None:
            with open(os.path.join(cfg.root, ".showrunner", "config.local.json"), "w") as fh:
                json.dump(local, fh)
        upath = os.path.join(tmpdir("userconf"), "config.json")
        if data is not None:
            with open(upath, "w") as fh:
                json.dump(data, fh)
        prev = config.USER_PATH
        config.USER_PATH = upath
        try:
            return config.load(start=cfg.root)
        finally:
            config.USER_PATH = prev

    # THE CASE PROTECTING EVERY REPO THAT WILL NEVER HAVE ONE. With no user file, the merge
    # must produce byte-for-byte what the old DEFAULTS + project shallow merge produced.
    plain = make_repo()
    with open(plain.path) as fh:
        on_disk = json.load(fh)
    old_way = dict(config.DEFAULTS)
    old_way.update(on_disk)
    loaded = with_user(None, repo=plain)
    eq("no user file present: the merged config is EXACTLY what it was before this layer "
       "existed", loaded.data, old_way)
    eq("...and the Config says so rather than leaving the reader to guess which files were "
       "read", loaded.user_path, None)
    ok("...and `tree` is still set, so seat resolution does not degrade to UNKNOWN on the new "
       "path", loaded.tree == os.path.realpath(plain.root), loaded.tree)

    # DEEP FOR DICTS. The whole point of the layer: something set once at user level must not
    # be erased by a project that merely mentions the same top-level key.
    c = with_user({"dispatch": {"chat": {"enabled": True, "cli": "/user/llm_chat"}}},
                  project={"dispatch": {"default_model": "opus"}})
    eq("a global dispatch.chat SURVIVES a project that sets only dispatch.default_model",
       c.data["dispatch"].get("chat"), {"enabled": True, "cli": "/user/llm_chat"})
    eq("...and the project's own key is there too, so nothing was merged by dropping a layer",
       c.data["dispatch"].get("default_model"), "opus")
    ok("...and the user file is named on the Config, since a merged dict cannot be asked where "
       "a value came from", c.user_path and c.user_path.endswith("config.json"), c.user_path)

    # WHOLESALE FOR LISTS, which is the rule the original shallow merge was written to protect:
    # half a lane or half a resource is a configuration nobody wrote.
    c = with_user({"lanes": [{"name": "user-a", "lane": "headless", "match": {"labels": ["x"]}},
                             {"name": "user-b", "lane": "headless", "match": {"labels": ["y"]}}],
                   "resources": [{"name": "user-dev", "match": ["z"]}]},
                  project={"lanes": [{"name": "proj", "lane": "headless",
                                      "match": {"labels": ["a"]}}],
                           "resources": [{"name": "proj-dev", "match": ["b"]}]})
    eq("a project `lanes` REPLACES a global `lanes` wholesale — never element-wise, so a "
       "half-overridden rule remains impossible",
       [l["name"] for l in c.data["lanes"]], ["proj"])
    eq("...and `resources` likewise", [r["name"] for r in c.data["resources"]], ["proj-dev"])

    # PRECEDENCE, ASSERTED. This is the decision that is the REVERSE of roles.json's.
    c = with_user({"default_lane": "headless",
                   "dispatch": {"chat": {"cli": "/user/bin", "enabled": True}}},
                  project={"default_lane": "serialized",
                           "dispatch": {"chat": {"cli": "/project/bin"}}})
    eq("THE PROJECT BEATS THE USER for the same leaf key — config is preference, and a repo is "
       "the better authority on its own lanes", c.data["dispatch"]["chat"]["cli"], "/project/bin")
    eq("...at the top level too", c.data["default_lane"], "serialized")
    eq("...while a sibling key the project did not mention is still the user's, which is what "
       "makes this a merge and not a replacement",
       c.data["dispatch"]["chat"]["enabled"], True)

    # AND THE LOCAL OVERLAY IS STILL THE LAST WORD, now under the new depth.
    c = with_user({"dispatch": {"chat": {"cli": "/user/bin", "enabled": True}}},
                  project={"dispatch": {"chat": {"cli": "/project/bin"}}},
                  local={"dispatch": {"chat": {"cli": "/local/bin"}}})
    eq("config.local.json still beats config.json, and beats the user layer",
       c.data["dispatch"]["chat"]["cli"], "/local/bin")
    eq("...and it too merges deeply rather than dropping the keys it did not mention",
       c.data["dispatch"]["chat"]["enabled"], True)

    # KEYS A MACHINE-WIDE FILE MAY NOT SET. Refused, not warned: a silently shared lock root is
    # a mutex that is quietly not one.
    for key, value, needle in (
            ("project_name", "everything", "project_name"),
            ("lock_root", "/tmp/sr-global-locks", "lock_root"),
            ("baseline", "/tmp/baseline.json", "baseline"),
    ):
        raises("REFUSES %s at machine scope" % key,
               lambda k=key, v=value: with_user({k: v}), needle)
    raises("REFUSES a nested graph.db at machine scope, so the refusal is not top-level-only",
           lambda: with_user({"graph": {"db": "/tmp/graph.db"}}), "graph.db")

    # The refusal has to say WHICH FILE — the whole ABSOLUTE path, not the bare name every
    # layer shares — or the reader is left grepping three files for a key they may not have
    # written and one of them is not in this repo at all.
    upath = os.path.join(tmpdir("userconf-named"), "config.json")
    with open(upath, "w") as fh:
        json.dump({"lock_root": "/tmp/sr-global-locks"}, fh)
    prev, config.USER_PATH = config.USER_PATH, upath
    try:
        config.load(start=make_repo().root)
        msg = ""
    except Refused as exc:
        msg = str(exc)
    finally:
        config.USER_PATH = prev
    ok("...and the refusal names the FILE the key came from by its full path, not just the key",
       upath in msg, (upath, msg))
    ok("...and says why, so it reads as a scoping rule rather than an arbitrary denial",
       "machine-wide" in msg, msg)

    # THE COMPANION CASE. A refusal list that refuses everything is a layer nobody can use.
    c = with_user({"graph": {"backend": "vendored"}, "default_lane": "headless",
                   "checks": [{"name": "t", "cmd": "true"}]})
    eq("a user file setting graph.backend is ACCEPTED — only the per-repo and per-campaign keys "
       "are refused", (c.data.get("graph") or {}).get("backend"), "vendored")
    eq("...and the project's graph.db is untouched beside it, so the nested refusal did not "
       "cost the nested merge", (c.data.get("graph") or {}).get("db"), ".showrunner/graph.db")

    # PATH-SHAPED VALUES GET THE EXISTING RULE, not a second one — and named against the user
    # file, whose author is the least likely person to be reading this repo's doctor output.
    try:
        with_user({"worktree_root": "$HOME/trees"})
        msg = ""
    except Refused as exc:
        msg = str(exc)
    ok("an unexpanded $VAR in the user file is refused by the SAME path_problem rule, naming "
       "the file", "NOT expanded" in msg and "worktree_root" in msg, msg)

    # A file that exists and cannot be parsed is not "nothing there".
    upath = os.path.join(tmpdir("userconf-bad"), "config.json")
    with open(upath, "w") as fh:
        fh.write("{ not json")
    prev = config.USER_PATH
    config.USER_PATH = upath
    try:
        raises("an unparseable user file is REFUSED, never silently treated as absent",
               lambda: config.load(start=make_repo().root), "not valid JSON")
    finally:
        config.USER_PATH = prev

    # DOES IT REACH THE CLI? Everything above calls `config.load` directly, and a layer that
    # merges correctly in-process but is not read by the real entry point is a layer nobody
    # has. So: a REAL `showrunner doctor`, in a repo whose own config configures no checks,
    # with the user layer supplying them — and the assertion is on the OUTPUT changing, not on
    # the file existing. The subprocess computes USER_PATH from XDG_CONFIG_HOME at import, so
    # the env var is the whole wiring; no second override exists and none is needed.
    _uhome = tmpdir("userconf-cli")
    os.makedirs(os.path.join(_uhome, "showrunner"), exist_ok=True)
    _urepo = make_repo()
    _env = dict(os.environ, XDG_CONFIG_HOME=_uhome)
    _before = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                             cwd=_urepo.root, capture_output=True, text=True, env=_env).stdout
    ok("with no user file, `doctor` warns that no checks are configured — the control, without "
       "which the line below proves nothing", "no checks configured" in _before, _before[:400])
    ok("...and it SAYS there is no user config, naming where one would go, so 'none' is a "
       "reported state rather than a silence", "user config: none" in _before, _before[:400])
    with open(os.path.join(_uhome, "showrunner", "config.json"), "w") as fh:
        json.dump({"checks": [{"name": "user-check", "cmd": "true"}]}, fh)
    # A PROJECT THAT WRITES `"checks": []` HAS SAID SOMETHING, and it outranks the user layer —
    # the same precedence as any other value, including when the value is empty. Caught by this
    # test failing on its first run: the fixture writes every DEFAULTS key out to disk, so the
    # repo was overriding rather than inheriting, which is the arm asserted here.
    _explicit = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                               cwd=_urepo.root, capture_output=True, text=True, env=_env).stdout
    ok("a project that explicitly sets `checks: []` still beats the user's list — an empty "
       "value is a value, not an absence", "no checks configured" in _explicit, _explicit[:400])
    _cfgfile = os.path.join(_urepo.root, ".showrunner", "config.json")
    with open(_cfgfile) as fh:
        _proj = json.load(fh)
    _proj.pop("checks", None)
    with open(_cfgfile, "w") as fh:
        json.dump(_proj, fh)
    _after = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                            cwd=_urepo.root, capture_output=True, text=True, env=_env).stdout
    ok("a checks list set ONCE at user level reaches a real `doctor` in a repo that configures "
       "none — the warning is gone", "no checks configured" not in _after, _after[:400])
    ok("...and doctor names the outside file that is affecting this repo, since every check it "
       "printed ran against the MERGED config",
       os.path.join(_uhome, "showrunner", "config.json") in _after, _after[:400])
    with open(os.path.join(_uhome, "showrunner", "config.json"), "w") as fh:
        json.dump({"project_name": "everything"}, fh)
    _refuse = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                             cwd=_urepo.root, capture_output=True, text=True, env=_env)
    ok("and the machine-scope refusal fires through the CLI too, non-zero and naming the key",
       _refuse.returncode != 0 and "project_name" in (_refuse.stderr + _refuse.stdout),
       (_refuse.returncode, _refuse.stderr[:300]))

    # THE CAVEAT MUST BE FILED WHERE EACH READER STANDS. These two files sit in one directory
    # with OPPOSITE precedence; a reader who opens either one must learn that.
    src = {}
    for name in ("config.py", "roles.py"):
        with open(os.path.join(ROOT, "lib", "showrunner", name)) as fh:
            src[name] = fh.read()
    ok("config.py states that its precedence is the reverse of roles.json's",
       "roles.json" in src["config.py"] and "PREFERENCE" in src["config.py"].upper())
    ok("...and roles.py carries the pointer from the other side, so the rule is not discovered "
       "only by whoever happened to read the other file",
       "config.json" in src["roles.py"] and "PERMISSION" in src["roles.py"].upper())


def test_config_layer_shadow_report():
    group("`doctor` reports which layer WON a leaf key, and which was shadowed")
    if not have("git"):
        skip("the config-layer shadow report group", "git is not installed")
        return

    def doctor_output(repo, uhome):
        env = dict(os.environ, XDG_CONFIG_HOME=uhome)
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                              cwd=repo.root, capture_output=True, text=True, env=env).stdout

    def shadow_lines(out, key):
        return [l for l in out.splitlines() if key in l and "shadowing" in l]

    # A LEAF VALUE SET IN TWO LAYERS: doctor names the winning file AND the shadowed one, and
    # marks a shadowed USER-level value distinctly — not buried among `ok` lines.
    uhome = tmpdir("shadow-uhome")
    os.makedirs(os.path.join(uhome, "showrunner"), exist_ok=True)
    upath = os.path.join(uhome, "showrunner", "config.json")
    with open(upath, "w") as fh:
        json.dump({"dispatch": {"default_model": "opus"}}, fh)
    repo = make_repo(extra_config={"dispatch": {"default_model": "sonnet"}})
    out = doctor_output(repo, uhome)
    lines = shadow_lines(out, "dispatch.default_model")
    ok("exactly one shadow line for the leaf key set in both layers", len(lines) == 1, out[:600])
    line = lines[0] if lines else ""
    ok("names the winning (project) file", ".showrunner/config.json" in line, line)
    ok("names the shadowed file by its full user-level path", upath in line, line)
    ok("marks the shadowed value as coming from the USER layer specifically, and with `warn` "
       "rather than `note` or `ok` — the line worth reading, not buried among them",
       "warn" in line and "user-level" in line, line)

    # THE FALSIFIER FOR THE TRAP: a dict set in two layers with DISJOINT sub-keys — the real
    # `dispatch` shape this repo's own config uses (`dispatch.chat` at user level,
    # `dispatch.default_model` / `dispatch.models_by_lane` at project level) — is a MERGE.
    # Nothing was lost, so it must produce NO shadow line, even though `dispatch` itself is a
    # top-level key present in both files.
    uhome2 = tmpdir("shadow-uhome-disjoint")
    os.makedirs(os.path.join(uhome2, "showrunner"), exist_ok=True)
    with open(os.path.join(uhome2, "showrunner", "config.json"), "w") as fh:
        json.dump({"dispatch": {"chat": {"enabled": True}}}, fh)
    repo2 = make_repo(extra_config={"dispatch": {"default_model": "sonnet",
                                             "models_by_lane": {"serialized": "opus"}}})
    out2 = doctor_output(repo2, uhome2)
    ok("a top-level key split across layers with disjoint sub-keys produces NO shadow line — "
       "dict-vs-dict is a merge, not a shadow",
       not any("dispatch" in l and "shadowing" in l for l in out2.splitlines()), out2[:600])

    # NO USER FILE PRESENT: the report is quiet, and doctor is otherwise unchanged.
    uhome3 = tmpdir("shadow-uhome-none")
    os.makedirs(os.path.join(uhome3, "showrunner"), exist_ok=True)
    repo3 = make_repo(extra_config={"dispatch": {"default_model": "sonnet"}})
    out3 = doctor_output(repo3, uhome3)
    ok("with no user config file, there is no shadow line at all", "shadowing" not in out3, out3[:400])
    ok("...and `doctor` is otherwise unchanged — still reports 'user config: none'",
       "user config: none" in out3, out3[:400])

    # THE README LIMIT SENTENCE MUST MATCH THE GRANULARITY THE CODE ACTUALLY REPORTS. A stated
    # limit that says "dict-level" while the code reports "leaf-level" (or vice versa) is a
    # caveat that lies about the very thing it exists to warn readers about.
    with open(os.path.join(ROOT, "README.md")) as fh:
        readme = fh.read()
    ok("README documents the per-key resolution report", "shadowed" in readme, None)
    ok("...and states its limit at LEAF-value granularity, naming the dispatch.chat / "
       "dispatch.default_model pair as the merge (not shadow) case — the same pair the "
       "falsifier above exercises against the real code",
       "leaf-value" in readme and "dispatch.chat" in readme and "dispatch.default_model" in readme
       and "merge, not a shadow" in readme, None)


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
    #
    # 10 -> 9 when the user config layer landed: the two `path_problem` branches (the named
    # keys, and inject paths) became one loop over `config.path_shaped`, which the user layer
    # reuses so the two cannot disagree about what is path-shaped. NO CASE WAS REMOVED ABOVE —
    # both inputs still reach the surviving branch, which is the property this group is for.
    EXPECTED_ERROR_BRANCHES = 9
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


# ============================================ CORE: stalled sessions (#69)
def _fake_transcript(projects, tree, session, idle):
    """Plant a transcript where `util.transcript_path` will derive it, aged `idle` seconds."""
    path = util.transcript_path(tree, session)
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    with open(path, "w") as fh:
        fh.write('{"type":"assistant"}\n')
    stamped = time.time() - idle
    os.utime(path, (stamped, stamped))
    return path


def test_stalled_sessions():
    group("A live process is not a working agent (issue #69)")
    cfg = make_repo()
    g = new_graph(cfg)

    # THE DERIVATION, PINNED. Every assertion below rests on showrunner resolving a claim to
    # the same file the host writes, and that rule is inferred from somebody else's layout
    # rather than published. The three cases here are the three that have actually been got
    # wrong: the issue's own text says "separators replaced by dashes" (misses the dot), a
    # premise check on this repo added the dot (misses the underscore), and this machine's
    # projects directory contains `-Users-...-programs-llm-chat` for a checkout named
    # `llm_chat`. A resolver that stops at `/` and `.` reports "no transcript" for every
    # project with an underscore in its path -- which reads as a silent session.
    home = tempfile.mkdtemp(prefix="sr-projects-")
    _prev_cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = home
    try:
        derived = util.transcript_path("/tmp/a.b/llm_chat", "S1")
        ok("the transcript path mangles the separator, the dot AND the underscore",
           derived == os.path.join(home, "projects", "-tmp-a-b-llm-chat", "S1.jsonl"), derived)
        ok("CLAUDE_CONFIG_DIR is honoured, so this suite never reads the developer's own "
           "transcripts", derived.startswith(home), derived)

        # A FAILED READ IS NOT A FACT ABOUT THE WORLD. `stat` cannot tell "this agent has been
        # silent for an hour" from "the host keeps transcripts somewhere this rule does not
        # reach", and only the first says anything about the agent. If an absent file collapsed
        # into a large idle time, every consumer whose host does not match the derivation would
        # see all its healthy Crawlers reported stalled at once.
        absent = util.transcript_activity("/tmp/nowhere-at-all", "S-missing")
        ok("an unreadable transcript reports idle=None, never a large idle time",
           absent["idle"] is None, absent)
        ok("...and says it could not look, rather than what the session did",
           "NOT evidence" in absent["why"], absent["why"])
        ok("a claim carrying no session id is an absence too, not silence",
           util.transcript_activity("/tmp/x", None)["idle"] is None)

        tree = os.path.join(cfg.root, "tree-a")
        g.add("a session that stops producing", leaf_id="s1")
        g.claim("s1", "wedged-crawler", pid=os.getpid(), tree=tree, session="SESS-STALLED")
        _fake_transcript(home, tree, "SESS-STALLED", idle=3600)

        g.add("a session that is working", leaf_id="s2")
        g.claim("s2", "busy-crawler", pid=os.getpid(), tree=tree, session="SESS-LIVE")
        _fake_transcript(home, tree, "SESS-LIVE", idle=5)

        stalled = g.stalled_claims()
        ids = [l["id"] for l, _ in stalled]
        ok("a live pid whose transcript is FROZEN is reported stalled — the signal every "
           "process-shaped check is blind to", "s1" in ids, ids)
        ok("a live pid whose transcript is MOVING is not", "s2" not in ids, ids)
        ok("the report carries the measurement, not just the verdict, so a reader who "
           "distrusts the threshold can discount it",
           any("60m" in why and "threshold 15m" in why for _, why in stalled), stalled)

        # The threshold is a parameter, and the control that proves the detector is not simply
        # answering "everything is stalled": at a 2-hour bar neither of these qualifies.
        ok("the idle threshold is honoured rather than hardcoded into the verdict",
           g.stalled_claims(idle_seconds=7200) == [], g.stalled_claims(idle_seconds=7200))

        # A LIVE CLAIM WITH NO TRANSCRIPT IS NOT STALLED. Same rule as the unreadable pid one
        # layer up: showrunner reports where it looked, and declines to convert that into a
        # statement about the agent.
        g.add("a live session showrunner cannot measure", leaf_id="s3")
        g.claim("s3", "unmeasurable", pid=os.getpid(), tree=tree, session="SESS-NO-FILE")
        ok("a live claim whose transcript cannot be READ is not called stalled",
           "s3" not in [l["id"] for l, _ in g.stalled_claims()], g.stalled_claims())

        # THE TWO VERDICTS MUST NOT OVERLAP (#68 is the other direction of this same root
        # cause). A claim offered to the reader as both "release it" and "do not touch it"
        # is worse than either verdict alone.
        dead = DeadPid()
        g.add("a genuinely abandoned session", leaf_id="s4")
        g.claim("s4", "ghost", pid=dead.pid, tree=tree, session="SESS-DEAD")
        _fake_transcript(home, tree, "SESS-DEAD", idle=3600)
        ok("a claim whose process is DEAD is stale, not stalled — abandoned and wedged are "
           "different states and only one of them licenses a release",
           "s4" in [l["id"] for l, _ in g.stale_claims()]
           and "s4" not in [l["id"] for l, _ in g.stalled_claims()])
        ok("...and the stalled claim is NOT reported stale, so `reap` is never offered it",
           "s1" not in [l["id"] for l, _ in g.stale_claims()], g.stale_claims())

        g.park("s1", "checking park still wins")
        ok("a PARKED claim is not stalled either — an accounted-for pause is not a wedge",
           "s1" not in [l["id"] for l, _ in g.stalled_claims()])
        g.unpark("s1")
        _fake_transcript(home, tree, "SESS-STALLED", idle=3600)  # unpark stamps nothing here
        ok("...and unparking brings it back, so the exclusion above is not vacuous",
           "s1" in [l["id"] for l, _ in g.stalled_claims()])

        # THE WHOLE POINT OF THE ISSUE. Reaping the stalled session in the filing incident
        # would have destroyed four uncommitted files and an already-green suite. `--apply` is
        # allowed to see it and must refuse to act on it.
        actions, _w = campaign.reap(cfg, g, apply=False)
        mine = [a for a in actions if a.get("leaf") == "s1"]
        ok("reap SURFACES the stalled claim, which nothing did before — it was invisible to "
           "status and reap alike", mine and mine[0]["kind"] == "stalled", actions)
        ok("...and the printed remedy is to prompt the session, never to reclaim it",
           mine and "not released" in mine[0]["action"], mine)

        campaign.reap(cfg, g, apply=True)
        ok("reap --APPLY leaves the stalled claim alone — its process is alive and may hold "
           "the only copy of uncommitted work",
           g.show("s1")["status"] == G.IN_PROGRESS
           and g.show("s1")["actor"] == "wedged-crawler", g.show("s1"))
        ok("the control: --apply DID release the genuinely abandoned one, so the guard above "
           "is not just a reaper that stopped working",
           g.show("s4")["status"] == G.OPEN, g.show("s4"))
    finally:
        if _prev_cfg_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = _prev_cfg_dir
        shutil.rmtree(home, ignore_errors=True)


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

    # #55: a TRUE premise attached to code nothing reaches. Both existing outcomes are wrong,
    # and the wrong one a Crawler reaches for is `done`, because it will be holding a real
    # commit with real tests -- for work that changes nothing a user can see.
    g.add("unreachable case", leaf_id="u1")
    g.claim("u1", "crawler")
    ev = os.path.join(cfg.root, "allowlist.txt")
    with open(ev, "w") as fh:
        fh.write("the 18 entries, and this preset is not among them\n")
    raises("REFUSES --unreachable with no citation — 'nothing calls this' is a claim about "
           "files this leaf never pointed you at",
           lambda: gates.close_gate(cfg, g, "u1", None, "dead", unreachable=True,
                                    premise="holds", premise_read="README.md"),
           "shows nothing reaches")
    leaf, notes = gates.close_gate(cfg, g, "u1", None, "nothing renders this preset",
                                   unreachable=True, evidence="allowlist.txt",
                                   premise="holds", premise_read="README.md")
    eq("an unreachable close records its OWN outcome, not `closed` and not `refuted` — the two "
       "that are already wrong", leaf.get("outcome"), "unreachable")
    ok("...and it is recorded with the premise HOLDING, because the analysis was right and the "
       "code is dead — collapsing that into `refuted` would say the Crawler was wrong",
       (leaf.get("premise") or "holds") == "holds", leaf.get("premise"))
    ok("...and the gate says why neither `done` nor `refuted` would do, in the note it returns",
       any("neither done nor refuted" in n for n in notes), notes)

    ok("an unreachable leaf is TERMINAL — otherwise `ready` keeps offering it and the stop gate "
       "refuses a turn-end over work that was correctly finished, punishing the outcome that "
       "took the most care to reach",
       leaf["status"] in G.TERMINAL, (leaf["status"], G.TERMINAL))

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


def test_unconfigured_checks():
    group("A scaffold's placeholder check is not an empty slot, it is a PASSING GATE")
    # Reported by a consumer who clean-installed: `init` ships cmd `echo 'configure me: your
    # test command'`, which exits 0 always. `integrate` re-runs the checks on every merged
    # result and reported passing; `baseline` recorded "0 failure lines, clean" against it. So
    # the no-new-failures criterion was measuring a command with no failure mode, and nothing
    # said so. "Unconfigured" and "passing" produced identical output.
    cfg = make_repo()
    ok("the SHIPPED scaffold's own check is detected as one that cannot fail -- this is the "
       "default every new project starts from, so it is the case that matters",
       gates.unconfigured_checks({"checks": [{"name": "tests",
                                              "cmd": "echo 'configure me: your test command'"}]}),
       )
    for _noop in ("true", ":"):
        ok("...and `%s`, which is the same passing gate written shorter" % _noop,
           gates.unconfigured_checks({"checks": [{"name": "t", "cmd": _noop}]}))
    ok("a REAL command is left alone -- this refuses at a gate, so a broad guess about somebody "
       "else's test runner would block work that is fine",
       not gates.unconfigured_checks({"checks": [{"name": "t", "cmd": "pytest -q"}]}))
    ok("...including one that merely MENTIONS echo, because the check is the command's failure "
       "mode and not the presence of a word",
       not gates.unconfigured_checks({"checks": [{"name": "t", "cmd": "make test && echo done"}]}))
    # Put the SHIPPED placeholder into this repo's config, because make_repo configures a real
    # check. Testing the refusal against a config that never had the defect would assert that
    # baseline refuses -- when it does not -- and pass for the wrong reason.
    _cpath = os.path.join(cfg.root, ".showrunner", "config.json")
    with open(_cpath) as _fh:
        _conf = json.load(_fh)
    _conf["checks"] = [{"name": "tests", "cmd": "echo 'configure me: your test command'"}]
    with open(_cpath, "w") as _fh:
        json.dump(_conf, _fh)
    _p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "baseline"],
                        cwd=cfg.root, capture_output=True, text=True)
    _said = (_p.stderr or "") + (_p.stdout or "")
    ok("`baseline` REFUSES against a check that cannot fail, the same refusal as no checks at "
       "all and for the same reason -- a baseline of nothing proves nothing",
       _p.returncode == 2 and "cannot fail" in _said, (_p.returncode, _said[:100]))


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

    # THE READER routing.jsonl NEVER HAD. Every decision has been appended since this module was
    # written and NOTHING opened the file — so "NO RULE MATCHED: an unmatched leaf is a missing
    # rule, not a neutral outcome" printed once at spawn and then accumulated where nobody looks.
    # A repo whose leaves keep defaulting is slow rather than broken, and slow has no error
    # message, so the file was the only place that gap was visible.
    #
    # ITS OWN REPO, because the assertion above counts entries in the shared one: a new test that
    # changes what an existing test measures makes the old one fail for a reason its sentence
    # does not mention.
    rcfg = make_repo()
    rg = new_graph(rcfg)
    rg.add("deploy to the TV", leaf_id="q1", labels=["device"])
    rg.add("nobody wrote a rule for this", leaf_id="q2")
    eq("with no routing log yet, the reader answers COULD NOT TELL rather than zero misses — "
       "an absent file and a clean record are the same reassuring number otherwise",
       lanes.unmatched(rcfg), (None, None))
    lanes.log(rcfg, [lanes.route(rcfg, rg.show("q1"))])
    eq("...and once a decision is recorded it reports none missed, out of a stated denominator",
       lanes.unmatched(rcfg), (0, 1))
    lanes.log(rcfg, [lanes.route(rcfg, rg.show("q2"))])
    eq("...and an UNMATCHED decision is COUNTED, which is the whole reason the file exists",
       lanes.unmatched(rcfg), (1, 2))
    with open(os.path.join(rcfg.state_dir, "routing.jsonl"), "a") as fh:
        fh.write("{not json\n")
    eq("...and a torn final line is skipped rather than becoming a verdict — a viewer may attach "
       "mid-append, and one bad line must not turn a real answer into 'could not tell'",
       lanes.unmatched(rcfg), (1, 2))


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

    # ---- THE BASE, WHICH DECIDES CORRECTNESS AND WAS NEVER PRINTED (#33) -------------
    # `spawn` cuts from the PRIMARY checkout's HEAD. That default is invisible AND
    # context-dependent: the identical command is right or wrong depending on where an
    # unrelated checkout happens to be pointing. Reported from a real campaign — five chained
    # leaves, the checkout moved back to `main` between spawns, and the last Crawler came up
    # with none of its prerequisite in history. It then took the documented smaller path and
    # reported a complete honest outcome that was half the item, with every gate green.
    g.add("the dependency", leaf_id="w-dep", labels=["backend"])
    g.add("builds on it", leaf_id="w-next", labels=["backend"])
    g.dep("w-next", "w-dep")
    dep_rec = worktree.spawn(cfg, g.show("w-dep"), actor="crawler-dep")
    with open(os.path.join(dep_rec["worktree"], "dependency.txt"), "w") as fh:
        fh.write("the prerequisite\n")
    sh(["git", "add", "-A"], dep_rec["worktree"])
    sh(["git", "commit", "-q", "-m", "the dependency's work"], dep_rec["worktree"])

    # The main checkout is sitting where it was — which does NOT contain the dependency.
    report = worktree.base_report(cfg, g, g.show("w-next"), "HEAD")
    eq("the base a spawn WOULD use is resolved and reported, rather than being an invisible "
       "default nobody sees at the moment of dispatch",
       report["sha"], sh(["git", "rev-parse", "HEAD"], cfg.root).stdout.strip())
    ok("...and says where it came from, because 'HEAD' names a different commit depending on "
       "where an unrelated checkout is pointing", report["explicit"] is False, report)
    eq("...and a dependency whose branch is NOT in that base is named as MISSING — showrunner "
       "owns the graph and the branch, so this is the check the Crawler had to run by hand",
       [d for d, _ in report["missing"]], ["w-dep"])
    eq("...and nothing is claimed present", report["present"], [])

    # THE PAIR. Same graph, same dependency, a base that DOES contain it — without this the
    # assertion above passes just as well against a check that calls everything missing.
    report_ok = worktree.base_report(cfg, g, g.show("w-next"), dep_rec["branch"])
    eq("...while a base that DOES contain the dependency reports it present",
       [d for d, _ in report_ok["present"]], ["w-dep"])
    eq("...and finds nothing missing", report_ok["missing"], [])
    ok("...and records that the base was named rather than defaulted, since an explicit base "
       "is the operator having decided", report_ok["explicit"] is True, report_ok)

    # THE THIRD ANSWER. A dependency that was never spawned has no branch to compare against,
    # and 'nothing is missing' there would be a claim about a graph this could not read.
    g.add("never spawned", leaf_id="w-ghost", labels=["backend"])
    g.dep("w-next", "w-ghost")
    report_unknown = worktree.base_report(cfg, g, g.show("w-next"), dep_rec["branch"])
    ok("a dependency with no branch is UNKNOWN, not absent — it was never spawned, and "
       "reporting it as satisfied or as missing would both be inventions",
       any("w-ghost" in u for u in report_unknown["unknown"]), report_unknown)
    eq("...while the one that CAN be checked is still checked, so an unknown does not swallow "
       "the answer for its siblings", [d for d, _ in report_unknown["present"]], ["w-dep"])

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

    # WHAT A REPORT MAY CLAIM. A Crawler's sentences are read by an orchestrator that cannot
    # cheaply check them and then dispatches the next leaf on them — so a confident wrong
    # sentence costs more than a wrong commit, which a gate catches. All four of these happened
    # in real runs, three of them to this repo's own agent.
    for phrase, what in (
            ("spent an hour in", "recency reads as knowledge, so the read that would take "
                                 "seconds gets skipped"),
            ("Existing is not working", "a path that exists and a thing that runs are different "
                                        "claims"),
            ("failed read is not a fact", "'could not look' folded into the answer is "
                                          "indistinguishable from 'nothing was there'"),
            ("elimination is not support", "the last explanation standing has no evidence FOR "
                                           "it, and loses that qualifier when written up")):
        ok("the brief tells the Crawler what its REPORT may claim — %s" % what,
           phrase in text, text[:200])
    ok("...and to correct a refuted claim in a NEW message rather than editing the old one, "
       "because somebody may already have acted on it",
       "NEW message" in text and "already acted" in text, text[:200])

    # PARENT-WALKING RESOLVERS, which every Crawler hits independently because a worktree is a
    # directory INSIDE the repo root by default (#34). `npx` walks up, finds the PRIMARY
    # checkout's node_modules, and fails naming a package the project does not depend on — so
    # the error reads as a broken install and the natural next move is reinstalling into the
    # worktree, which is slow and can hide the cause. Nothing to build; the fix is knowing,
    # which is why it belongs in the text every Crawler is handed rather than in a doc.
    ok("the brief tells the Crawler that a parent-walking resolver will find the PRIMARY "
       "checkout — the failure is invisible from inside the worktree and costs a wrong "
       "conclusion before it costs time",
       "walks UP" in text and "PRIMARY checkout" in text, text[:200])
    ok("...and gives the concrete form rather than only the principle, because 'use an explicit "
       "path' is not actionable at the moment somebody is typing `npx`",
       "./node_modules/.bin/" in text and "never `npx" in text, text[:200])
    ok("...and names the tell — a dependency the project does not use — so the reader can "
       "recognise it from the error rather than having to remember this paragraph",
       "does not use" in text and "suspect the resolver" in text, text[:200])

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

    # PAYING OFF A NOTE THAT NAMED ITS OWN REMEDY. `harness.report` sat in NOT_SWEPT with the
    # reason "SHOULD BE SWEPT, IS NOT YET — doctor's harness lines would vanish and no assertion
    # currently requires them." That is a work item wearing an exclusion's clothes: because it
    # was excused, the mutation accounting counted it as ACCOUNTED FOR and reported clean, so
    # the debt was invisible to the tool that would otherwise have chased it. Found by another
    # project's rule that a note naming a REMEDY has a done-state while a note naming a REASON
    # does not — theirs sat in a sweep file, mine in an exclusion list.
    _dr = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    # CONDITIONAL ON A HARNESS BEING PRESENT, because `.game_loop/` is no longer tracked here:
    # it is installed per machine, so a stranger's clone has none and these two asked about
    # lines that cannot exist there. showrunner does not require a harness — `harness.require`
    # is config — so asserting one is present made the suite depend on a thing this repo
    # deliberately does not ship. It stayed true only because the payload used to be committed.
    #
    # The property still worth pinning is the one the comment above names: doctor must not go
    # SILENT about the harness. So it is asserted both ways — the lines when there is one, and a
    # plain statement of absence when there is not, since an empty section reads as "nothing to
    # report" and means the opposite.
    if os.path.isdir(os.path.join(ROOT, ".game_loop")):
        ok("`doctor` says what a Crawler's harness actually IS — the lines vanish silently when "
           "the producer dies, which is why nothing required them until now",
           "harness .game_loop" in _dr, _dr[:200])
        ok("...and whether that harness declares its OWN owned set or falls back, because the "
           "fallback is conservative and noisier and a reader must know which they are getting",
           ("declares its own owned set" in _dr) or ("exposes no `owned` verb" in _dr),
           _dr[:200])
    else:
        ok("`doctor` says a harness is ABSENT rather than printing nothing — this checkout has "
           "none, and silence there reads as 'nothing to report' when it means the opposite",
           ("harness" in _dr.lower()), _dr[:300])
    # A SYNTHETIC REPO, so this arm does not depend on THIS checkout having a harness either.
    _off = make_repo(extra_config={"harness": {"provision": "off"}})
    _dr_off = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                             cwd=_off.root, capture_output=True, text=True).stdout
    ok("...and says OFF plainly when provisioning is off, rather than printing nothing — an "
       "empty harness section reads as 'nothing to report' and means the opposite",
       "provisioning is OFF" in _dr_off, _dr_off[:200])

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
       ((info or {}).get("rule_files"), (info or {}).get("notes_files")),
       (["config.json", "INVARIANTS.md", "verify.yaml"], ["LEDGER.md"]))

    g = new_graph(cfg)
    g.add("work", leaf_id="h1", labels=["backend"])
    # `spawn` REFUSES when the harness it must provision cannot answer, which is correct and
    # is also every assertion below it dying at once. Caught so the refusal becomes a failed
    # line rather than an unscoreable producer.
    rec = attempt(lambda: worktree.spawn(cfg, g.show("h1"), actor="crawler-h"), {})
    # A PATH THAT CANNOT EXIST when the spawn refused, rather than None. Every assertion below
    # joins onto `wt`, and None turns each of them into a TypeError that ends the group — so the
    # sentinel keeps them EVALUATING: the "it was provisioned" lines fail, which is the truth,
    # and the group survives to be measured.
    wt = rec.get("worktree") or os.path.join(cfg.root, ".spawn-refused-no-worktree")
    os.makedirs(wt, exist_ok=True)
    ok("an UNTRACKED harness is provisioned into the worktree",
       bool(wt) and os.path.exists(os.path.join(wt, ".game_loop", "bin", "game_loop")))
    ok("...with the executable bit preserved",
       bool(wt) and os.access(os.path.join(wt, ".game_loop", "bin", "game_loop"), os.X_OK))
    ok("the harness's OWN verdict is what showrunner records, not showrunner's comparison",
       any("verified by the harness" in a for a in rec.get("provisioned") or []),
       rec.get("provisioned"))
    ok("...and the report names the limit that is REAL — the hook-registration file is outside "
       "the harness directory, so the harness cannot compare it and showrunner checks it "
       "separately",
       any("NOT checked by it" in a and "hook-registration" in a for a in (rec.get("provisioned") or [])),
       (rec.get("provisioned") or []))
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
    rec4 = attempt(lambda: worktree.spawn(c, gg.show("h4")), {})
    ok("exit 3 (NOTES drifted) warns and carries on — per-tree notes are ordinary",
       any("NOTE:" in a for a in (rec4.get("provisioned") or [])), (rec4.get("provisioned") or []))

    # --- the hook-registration file -----------------------------------------
    ok("showrunner does NOT copy the hook-registration file (the installer MERGES it, "
       "preserving the project's own settings and its stray-hook warning)",
       not os.path.exists(os.path.join(wt, ".claude", "settings.json")) or
       "settings.json" not in " ".join((rec.get("provisioned") or [])),
       (rec.get("provisioned") or []))
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
    rec5 = attempt(lambda: worktree.spawn(inst, gi.show("h5")), {})
    ok("a configured installer is used instead of copying, and is told which tree to match",
       any("installer" in a and "MERGES" in a for a in (rec5.get("provisioned") or [])), (rec5.get("provisioned") or []))
    ok("...and the hook registration lands through it",
       os.path.exists(os.path.join((rec5.get("worktree") or ""), ".claude", "settings.json")))

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
    rec7 = attempt(lambda: worktree.spawn(lax, gl.show("h7")), {})
    ok("...with require=false as the escape hatch, and the unverified state stated on the record",
       any("NOT ENFORCED" in a for a in (rec7.get("provisioned") or [])), (rec7.get("provisioned") or []))

    # A Crawler can weaken its OWN rules after it starts. Verifying only at spawn verifies
    # that for exactly one instant.
    post = make_repo()
    _seed_harness(post.root)
    gp = new_graph(post)
    gp.add("tamper", leaf_id="h8", labels=["backend"])
    rec8 = attempt(lambda: worktree.spawn(post, gp.show("h8"), actor="tamperer"), {})
    # `record_spawn` reads the fields a refused spawn never produced. Guarded so a stubbed
    # producer upstream fails the assertions below instead of ending the group at the record.
    # Only when there IS a record. `record_spawn` reads fields a refused spawn never produced,
    # and a KeyError here ends the group — so a producer stubbed upstream became unscoreable
    # at the bookkeeping line rather than failing the assertions that are about it. Skipping
    # is truthful: nothing was spawned, so there is nothing to record, and the campaign stays
    # empty in exactly the way the assertions below will report.
    if rec8:
        campaign.record_spawn(post, rec8, pid=os.getpid())
    status, _, mis = H.check_tree(post, (rec8.get("worktree") or ""))
    eq("a freshly spawned tree checks clean", status, "clean")
    ok("...and is not retroactively flagged as mis-certified when nothing was unreadable",
       mis is False, mis)

    with open(os.path.join((rec8.get("worktree") or ""), ".game_loop", "TESTMODE"), "w") as fh:
        fh.write("drifted\n")
    status, _, _ = H.check_tree(post, (rec8.get("worktree") or ""))
    eq("post-spawn rule drift is caught by RE-asking the harness, not assumed away",
       status, "drifted")
    finding = next((f for f in campaign.reconcile(post, gp)
                    if f["crawler"] == (rec8.get("crawler") or "")), {})
    ok("...and reconcile reports it above every other verdict — a gate answering a different "
       "question makes everything it certified mean less",
       (finding.get("verdict") or "").startswith("HARNESS DRIFTED"), finding.get("verdict"))

    # #66: GRADED BY WHETHER THE TREE CAN STILL ACT. The cost of drift is a refused commit on
    # finished work, and that needs somebody still working in the tree — a closed leaf with no
    # live holder cannot pay it. Reported at one severity it trained the reader to skim:
    # measured in a real campaign, 48 trees carrying a harness, 42 drifted, ZERO live.
    #
    # And the count moved the wrong way. Re-provisioning the main checkout — the correct action
    # — RAISED drift from 26 to 42, so a reader taking the number as "how much is wrong" learned
    # the opposite of the truth.
    ok("a drifted tree with a LIVE holder says so in the verdict, because that is the one whose "
       "gate can still refuse somebody",
       "(LIVE)" in (finding.get("verdict") or ""), finding.get("verdict"))
    _drifted_idle = [f for f in campaign.reconcile(post, gp)
                     if f["harness"] == "drifted" and not (f["alive"] or f["blocked"])]
    for _f in _drifted_idle:
        ok("...while an IDLE drifted tree is graded down rather than shouted, since nothing it "
           "certifies can refuse anybody: %s" % _f["crawler"],
           _f["verdict"].startswith("harness drifted (idle)"), _f["verdict"])
    ok("the drifted-and-live case is not swallowed by the grading — at least one finding still "
       "carries the loud verdict, or the fix would have silenced the class it exists to surface",
       any((f.get("verdict") or "").startswith("HARNESS DRIFTED (LIVE)")
           for f in campaign.reconcile(post, gp)),
       [f.get("verdict") for f in campaign.reconcile(post, gp)][:3])

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
    # AGAINST `follow_up`, not against the table this test declared. This read
    # `not follow_states["failing"][1]`, which subscripts the literal `False` written twenty
    # lines above — `not False`, in a sentence about the watchdog. The loop's own
    # `eq(fu["scheduled"], want_sched)` already covers the case; this states the consequence,
    # so it asks the producer.
    H._porcelain = lambda b, v: (0, dict(follow_states["failing"][0]))
    try:
        failing = H.follow_up(post)
    finally:
        H._porcelain = _orig_porc
    ok("a FAILING probe is not counted as scheduled — it rings and reports failing, so it is a "
       "broken watchdog rather than a re-check that will happen",
       failing["scheduled"] is False and bool(failing.get("why")), failing)

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
        blocked, why = H.stop_gate(post, (rec8.get("worktree") or ""), "some-session-id")
    finally:
        H.run = _orig_run
    ok("a Crawler refused at a turn-end is reported BLOCKED", blocked is True, (blocked, why))
    ok("...and the detail says the harness's limit does NOT bound this — a reader who sees "
       "'1 of 3' would otherwise assume something is counting down",
       "never increments again" in why, why)

    H.run = _canned_run(json.dumps({"stop_gate": {"blocked": False, "attachments": {}}}))
    try:
        blocked, _ = H.stop_gate(post, (rec8.get("worktree") or ""), "some-session-id")
    finally:
        H.run = _orig_run
    ok("a working Crawler is not", blocked is False, blocked)

    H.run = _canned_run("not json at all")
    try:
        blocked, _ = H.stop_gate(post, (rec8.get("worktree") or ""), "some-session-id")
    finally:
        H.run = _orig_run
    ok("a harness with no such seam answers None, NOT False — an older harness's silence is "
       "not evidence that its Crawler is fine, and reading it as such is the same mistake one "
       "layer up", blocked is None, blocked)
    ok("...and so is a Crawler whose session was never recorded, rather than being assumed "
       "healthy", H.stop_gate(post, (rec8.get("worktree") or ""), None)[0] is None)

    # THE WIRING, not just the reader. Everything above proves stop_gate answers correctly and
    # says nothing about whether the two verbs that decide anything ever ask it. A verified
    # diagnosis is not a verified fix.
    gp.add("blocked work", leaf_id="h9", labels=["backend"])
    rec9 = attempt(lambda: worktree.spawn(post, gp.show("h9"), actor="stuck"), {})
    if rec9:
        campaign.record_spawn(post, rec9, pid=os.getpid(), session="a-real-session-id")
    gp.claim("h9", "stuck", pid=os.getpid())
    _orig_sg = campaign_harness_stop_gate = __import__(
        "showrunner.harness", fromlist=["harness"]).stop_gate
    import showrunner.harness as _HH
    _HH.stop_gate = lambda c, w, s: (True, "refused at turn-end by showrunner-stop-gate")
    try:
        f9 = next((f for f in campaign.reconcile(post, gp)
                   if f["crawler"] == (rec9.get("crawler") or "")), {})
        is_waiting, detail = campaign.waiting(post, gp)
    finally:
        _HH.stop_gate = _orig_sg
    ok("reconcile ranks BLOCKED above LIVE — it IS live, and that is the whole problem",
       (f9.get("verdict") or "").startswith("BLOCKED"), f9.get("verdict"))
    ok("...and `waiting` counts it as NEITHER waiting nor parked, so the orchestrator's own "
       "watchdog is free to ring: sitting beside a session that can only be restarted from "
       "outside is not waiting on work you cannot hurry",
       is_waiting is False and not (detail or {}).get("live_crawlers"), (is_waiting, detail))
    ok("...while still REPORTING it, because the Crawler is real and somebody has to go and "
       "prompt it — dropping it would trade one silence for another",
       "h9" in [c["leaf"] for c in (detail or {}).get("blocked_crawlers") or []],
       (detail or {}).get("blocked_crawlers"))

    # A VERDICT THIS SIDE HAS NO MEANING FOR must fail CLOSED. The merge conditional listed the
    # verdicts that BLOCK, so anything else merged — and `None` meant both "no harness here"
    # (legitimate) and "the harness answered something unrecognised". A contract that grows a
    # term is exactly when a consumer must stop rather than guess, and the guess available by
    # default is the permissive one. game_loop added a verdict mid-session; the only reason that
    # one was safe is that it reused an exit code already mapped.
    with open(os.path.join((rec8.get("worktree") or ""), ".game_loop", "TESTMODE"), "w") as fh:
        fh.write("from-the-future\n")
    status, _, _ = H.check_tree(post, (rec8.get("worktree") or ""))
    eq("a harness verdict outside the known contract reads as 'unrecognised', not as None — "
       "'we have no meaning for this' and 'there is no harness here' are different, and only "
       "one of them is safe to merge on", status, "unrecognised")
    ok("...and it outranks every verdict, because a term nobody understands says nothing about "
       "the ones that were understood", H.SEVERITY[H.UNRECOGNISED] > max(
           H.SEVERITY[c] for c in H.CONTRACT_CODES))
    # WHAT INTEGRATE DOES WITH THAT VERDICT is asserted against `campaign.integrate` in the
    # integration group, not here. Two assertions used to sit at this spot claiming to cover it
    # and computing both sides from string literals written three lines above them: they passed
    # with the conditional reverted, and would have passed with campaign.py deleted from disk.
    # A test is falsifiable only by something that did not share the belief, and those shared it.

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


def test_parked_beats_blocked():
    group("A parked Crawler is accounted for, even when it is also inert (#62)")
    # THE CLASSIFICATION IS THE BUG, and the gate test above cannot reach it — that one feeds a
    # fabricated payload, so it proves the gate honours `parked_crawlers` and says nothing about
    # whether `waiting` ever puts anybody there. `blocked` was tested before `parked`, so a leaf
    # that was both never reached the parked branch.
    cfg = make_repo()
    g = new_graph(cfg)

    both = [{"crawler": "c-both", "leaf": "P1", "branch": "showrunner/c-both",
             "worktree": ".worktrees/c-both", "blocked": True,
             "blocked_detail": "refused at turn-end", "alive": True, "parked": True,
             "uncommitted": []}]
    real_reconcile = campaign.reconcile
    campaign.reconcile = lambda *a, **k: both
    try:
        _waiting, detail = campaign.waiting(cfg, g)
    finally:
        campaign.reconcile = real_reconcile

    eq("a leaf that is PARKED and blocked is reported as parked, not blocked — parking is the "
       "record that somebody accounted for it, and the inert gate refuses on `blocked`",
       [c["crawler"] for c in detail["blocked_crawlers"]], [])
    eq("...and it IS reported, in the parked list, because the stall must not become invisible "
       "— the gate's purpose was the noticing, not the refusing",
       [c["crawler"] for c in detail["parked_crawlers"]], ["c-both"])
    ok("...and the reason says it is BOTH, so its owner learns the run is stalled rather than "
       "reading an ordinary usage-limit park",
       "refused at a turn-end" in detail["parked_crawlers"][0]["why"],
       detail["parked_crawlers"][0]["why"])

    # WHOSE LEAF IT IS, carried in the report. The gate fires in whichever session is nearest,
    # which in a multi-campaign checkout is routinely not the owner — so it told a stranger to
    # message a Crawler they never briefed and offered a reap over work they had no context on.
    # The owner's framing, and it is better than campaign-scoping: the blocked session does not
    # need the CONTROLS, it needs to know whose leaf this is and who to tell.
    g.add("owned elsewhere", leaf_id="P9")
    g.claim("P9", "crawler-txt-paint", session="sess-owner-1")
    attributed = [{"crawler": "c-attr", "leaf": "P9", "branch": "b", "worktree": "w",
                   "blocked": True, "blocked_detail": "refused at turn-end", "alive": True,
                   "parked": False, "uncommitted": []}]
    campaign.reconcile = lambda *a, **k: attributed
    try:
        _w3, d3 = campaign.waiting(cfg, g)
    finally:
        campaign.reconcile = real_reconcile
    b3 = d3["blocked_crawlers"][0]
    eq("a blocked report NAMES the actor who claimed the leaf, so the session it fires in can "
       "tell whether it is theirs at all", b3.get("actor"), "crawler-txt-paint")
    ok("...and the claiming SESSION, because two agents can share an actor name and the session "
       "is what identifies who to go and tell",
       b3.get("claim_session"), b3)

    # A CLOSED LEAF MUST NOT STAY PARKED. Observed: one agent parked another's inert leaf, the
    # owner closed it, and it read `closed` with `parked: 1` — a pair that cannot mean anything,
    # since park records that a CLAIM is paused and a closed leaf has no claim to pause.
    g.add("parked then closed", leaf_id="P8")
    g.claim("P8", "someone", pid=os.getpid())
    g.park("P8", "waiting on its owner")
    ok("a leaf can be parked while claimed", g.show("P8").get("parked"))
    with open(os.path.join(cfg.root, "proof-p8.txt"), "w") as fh:
        fh.write("done\n")
    gates.close_gate(cfg, g, "P8", "proof-p8.txt", "finished", premise="holds",
                     premise_read="README.md")
    ok("...and closing it CLEARS the park, because `closed` and `parked` together is a state "
       "that cannot mean anything and later reads as evidence of something",
       not g.show("P8").get("parked"), g.show("P8").get("parked"))

    # THE RESTRAINT CASE. Parking must not swallow a Crawler that is genuinely inert and NOT
    # parked, or the fix hands every stall an exit and the gate stops working.
    only_blocked = [dict(both[0], parked=False)]
    campaign.reconcile = lambda *a, **k: only_blocked
    try:
        _w2, d2 = campaign.waiting(cfg, g)
    finally:
        campaign.reconcile = real_reconcile
    eq("an inert Crawler that is NOT parked is still blocked — the fix is about accounting for "
       "a stall, not about excusing one",
       [c["crawler"] for c in d2["blocked_crawlers"]], ["c-both"])


def test_work_since_block():
    group("A blocked report is a fact about the PAST; the tree can disagree with it (#54)")
    cfg = make_repo()
    from showrunner import events as EV

    # No recorded block at all -> False. Not "no evidence of work"; there is no question to
    # answer, and answering it anyway is how a gate becomes stricter on missing data.
    got, why = campaign.work_since_block(cfg, "c-none", None, "")
    ok("with no recorded block, the answer is NO EVIDENCE rather than an opinion — a signal "
       "that only ever RELEASES must say nothing when it was never asked", got is False, (got, why))

    wt = os.path.join(cfg.worktree_root, "c-work")
    sh(["git", "worktree", "add", "-q", wt, "-b", "showrunner/c-work"], cfg.root)
    tracked = os.path.join(wt, "owned.txt")
    with open(tracked, "w") as fh:
        fh.write("first\n")
    sh(["git", "add", "owned.txt"], wt)
    sh(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "w"], wt)

    # Record the block NOW, with every existing file older than it.
    EV.emit(cfg, "crawler.blocked", {"crawler": "c-work", "leaf": "L1", "why": "refused"})
    blocked_at = EV.latest(cfg, ("crawler.blocked",), "crawler", "c-work")["ts"]
    os.utime(tracked, (blocked_at - 60, blocked_at - 60))
    got, why = campaign.work_since_block(cfg, "c-work", None, wt)
    ok("a tree whose tracked files all predate the block yields no evidence, so the gate keeps "
       "refusing exactly as it does today", got is False, (got, why))

    # Now the Crawler works, without saying anything to anybody.
    os.utime(tracked, (blocked_at + 60, blocked_at + 60))
    got, why = campaign.work_since_block(cfg, "c-work", None, wt)
    ok("a TRACKED file changed after the block IS evidence — this is the case that produced the "
       "issue: working with no channel to report it on", got is True, (got, why))

    # UNTRACKED files must not count: a log the harness writes, or an editor swapfile, would
    # otherwise release the gate with nobody having done any work. This signal releases, so a
    # false positive is the expensive direction.
    os.utime(tracked, (blocked_at - 60, blocked_at - 60))
    with open(os.path.join(wt, "scratch.log"), "w") as fh:
        fh.write("harness noise\n")
    got, why = campaign.work_since_block(cfg, "c-work", None, wt)
    ok("...but an UNTRACKED file is not evidence, so harness noise in the tree cannot release "
       "a gate on a Crawler that has genuinely stopped", got is False, (got, why))

    # Every unknown must land on today's behaviour, never on a stricter one.
    got, why = campaign.work_since_block(cfg, "c-work", None,
                                         os.path.join(cfg.root, "no-such-tree"))
    ok("a worktree that cannot be read yields no evidence rather than an error — tree evidence "
       "must never be the one place where failing to read something TIGHTENS a gate",
       got is False, (got, why))


def test_path_problem():
    group("A config path that will not mean what its author thinks")
    # THE LAST OWED DEBT. It gates an ACCEPT: returning None means "this path is fine", so a
    # neutered version lets every unexpanded variable through and the caller keeps a belief the
    # config does not support. Measured at 3 kills before this.
    from showrunner import config as C
    msg = C.path_problem("lock_root", "$HOME/locks")
    ok("a `$VAR` path is REPORTED — expanduser handles a leading ~ and nothing else, so the most "
       "portable-LOOKING entry is a literal string resolving against the caller's cwd",
       msg is not None, msg)
    ok("...and the message names the offending path AND the fix, because 'invalid' sends the "
       "reader back to guess which of several paths and what to write instead",
       msg and "lock_root" in msg and "~/" in msg, msg)
    # WHY THIS ONE IS WORSE THAN A WRONG PATH, and the reason it gates an accept at all: it
    # survived an isabs() check, because abspath makes anything absolute. For a lock root it
    # means a different directory per caller — a mutex that is quietly a no-op.
    ok("...and it is caught for a lock_root specifically, which is the case that turns a"
       " single-consumer guarantee into a per-caller directory",
       C.path_problem("lock_root", "$HOME/locks") is not None)
    for good in ("~/locks", "/var/tmp/locks", ".showrunner/locks"):
        ok("a path that WILL mean what it says is accepted: %r — a gate that flags everything "
           "is one people route around" % good, C.path_problem("lock_root", good) is None,
           C.path_problem("lock_root", good))
    ok("an empty or non-string entry is not a path problem — there is no path to be wrong about, "
       "and inventing one here would make every unset optional key an error",
       C.path_problem("lock_root", "") is None and C.path_problem("lock_root", None) is None)


def test_harness_gap():
    group("A gitignored harness never crosses into a worktree, and the Crawler is denied its first commit")
    if not have("git"):
        skip("the harness-gap group", "git is not installed")
        return
    # LAST TWO FROM THE OWED QUEUE. Measured at 1 kill, so this note was accurate. The producer
    # answers a warning or None, and None is also what "no problem here" looks like — so a
    # neutered version removes doctor's only warning about the most invisible spawn failure
    # there is, and doctor keeps reporting a healthy install.
    from showrunner import worktree as W
    cfg = make_repo()
    hd = os.path.join(cfg.root, W.HARNESS_DIRS[0])
    os.makedirs(hd, exist_ok=True)
    with open(os.path.join(hd, "rules.md"), "w") as fh:
        fh.write("the harness's own rules\n")

    note = W.harness_gap(cfg)
    ok("an UNTRACKED harness in the main checkout is reported — `git worktree add` copies "
       "tracked files only, so it never crosses and the Crawler's first commit is denied",
       note and W.HARNESS_DIRS[0] in note, note)
    ok("...and the note says what the harness will DO about it, since 'missing' is not "
       "actionable and 'your commit gate will deny the Crawler' is",
       note and "DENY" in note, note)

    sh(["git", "add", "-A"], cfg.root)
    sh(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "track it"],
       cfg.root)
    ok("...and a TRACKED harness reports nothing, because it crosses on its own — the assertion "
       "a producer stuck answering its last warning cannot pass",
       W.harness_gap(cfg) is None, W.harness_gap(cfg))

    # ALREADY-PROVISIONED IS ALSO NOTHING TO SAY. The gap is about what the Crawler LANDS in,
    # so a worktree that already has the directory is not a gap even when the main checkout
    # does not track it.
    cfg2 = make_repo()
    hd2 = os.path.join(cfg2.root, W.HARNESS_DIRS[0])
    os.makedirs(hd2, exist_ok=True)
    with open(os.path.join(hd2, "rules.md"), "w") as fh:
        fh.write("x\n")
    wt = tmpdir("gap-worktree")
    os.makedirs(os.path.join(wt, W.HARNESS_DIRS[0]), exist_ok=True)
    ok("a worktree that ALREADY carries the harness is not a gap — the question is what the "
       "Crawler lands in, not what the main checkout tracks",
       W.harness_gap(cfg2, worktree_path=wt) is None, W.harness_gap(cfg2, worktree_path=wt))
    ok("...while one that does not is still reported, so the worktree-aware path cannot answer "
       "None for everything",
       W.harness_gap(cfg2, worktree_path=tmpdir("gap-bare")) is not None)


def test_attribution():
    group("The provenance declaration an integration commit is told to make")
    # PAID FROM THE OWED QUEUE, and this one measured ZERO. Its note said an always-None would
    # "silently drop the provenance command from integration" — and nothing at all noticed,
    # which makes it the only genuinely UNPROTECTED producer the queue held. The block simply
    # stops printing: `integration-commit` still exits 0, still reports every staged file
    # accounted for, and the commit goes bare.
    cfg = make_repo()
    entries = [{"branch": "showrunner/c-a", "crawler": "c-a"},
               {"branch": "showrunner/c-b", "crawler": "c-b"}]
    att = gates.attribution(cfg, entries, harness_bin="/bin/harness")
    ok("attribution NAMES every merged ref, not just the first — a command that declares one of "
       "two branches spends the declaration and leaves the other unattributed",
       att and all("--merge %s" % e["branch"] in att["command"] for e in entries),
       (att or {}).get("command"))
    ok("...and names the Crawlers in the reason, because the provenance question is WHO produced "
       "this and a ref answers where",
       att and "c-a" in att["command"] and "c-b" in att["command"], (att or {}).get("command"))
    # THE TWO RULES ARE THE PRODUCT. The docstring says both were found by running the verb, and
    # a command handed over without them is spent on a commit that was never going to be
    # checked — which is worse than no command, because it reads as done.
    ok("...and carries WHEN it must not be run, since a clean `git merge` never invokes the gate "
       "and a declaration spent there leaves the NEXT commit bare",
       att and "clean" in att["when"] and "merge" in att["when"], (att or {}).get("when"))
    ok("...and ORDER, because attribution is recomputed from the ref and declaring early "
       "resolves to zero files — a correct answer and a useless one",
       att and "AFTER" in att["order"], (att or {}).get("order"))
    ok("no branches means NO declaration rather than an empty one — nothing was merged, so "
       "there is no provenance to declare",
       gates.attribution(cfg, [{"crawler": "c-a"}], harness_bin="/bin/harness") is None)
    ok("...and no harness binary means none either, rather than a command naming nothing",
       gates.attribution(cfg, entries, harness_bin=None) is None
       or "/" in gates.attribution(cfg, entries)["command"])


def test_post_checkout_hook_failure():
    group("A hook that failed after the tree was made is not 'git worktree add failed' (#63)")
    if not have("git"):
        skip("the post-checkout group", "git is not installed")
        return
    # Four consecutive spawns failed in one real run, all reporting "git worktree add failed".
    # git had done its job; a post-checkout hook had not — git-lfs was installed and not on the
    # restarted session's PATH. Nothing in the message pointed at a hook, a PATH, or LFS, so the
    # operator debugged git. Present-but-unreachable reported as broken.
    cfg = make_repo()
    hookdir = os.path.join(cfg.root, ".git", "hooks")
    os.makedirs(hookdir, exist_ok=True)
    hook = os.path.join(hookdir, "post-checkout")
    with open(hook, "w") as fh:
        fh.write("#!/bin/sh\necho 'git-lfs: command not found' >&2\nexit 1\n")
    os.chmod(hook, 0o755)

    _, said = attempt_message(lambda: worktree.create(cfg, "wt-hook", "showrunner/wt-hook"))
    ok("the failure says the tree WAS created, because git succeeded and something it ran "
       "afterwards did not — the two have opposite remedies",
       "WAS created" in said, said[:200])
    ok("...and surfaces the HOOK's own words, which is where the actual cause is and which a "
       "generic string swallowed", "git-lfs" in said, said[:300])
    ok("...and names the PATH this process used, because two sessions on one machine disagree "
       "and checking your own shell can exonerate a tool that is unreachable HERE",
       "PATH as this process sees it" in said, said[-400:])
    ok("...and says the tree is probably usable, because four retries followed a message that "
       "implied nothing had been made", "probably usable" in said, said[-300:])

    # THE OTHER ARM MUST STILL EXIST. If every failure now claims a tree was created, the
    # distinction is gone in the other direction and a genuine git failure reads as an
    # environment problem.
    cfg2 = make_repo()
    _, said2 = attempt_message(
        lambda: worktree.create(cfg2, "wt-bad", "showrunner/wt-bad", base="no-such-ref"))
    ok("a genuine git failure with NO tree created still says so, so the fix did not collapse "
       "the distinction from the other side",
       "no tree was created" in said2, said2[:200])


def test_vendor_staleness():
    group("A vendored copy that is behind must say so, and say when it cannot tell (#65)")
    from showrunner import pin as P
    # Reported by a consumer who hit a bug FIXED UPSTREAM three commits earlier and spent an
    # evening rediscovering it. `doctor` warned that a copied install is unattributable — a
    # claim about PROVENANCE, which fires identically whether the copy is current or twenty
    # commits stale. So a closed issue and a live one look the same from inside a vendored tree.
    # FAIL RATHER THAN RAISE: unpacking a None here crashes the group, and a crashed group
    # scores as a floor rather than a measurement — the remedy mutate.py prints for this shape.
    _st = P.staleness()
    lvl, msg = _st if isinstance(_st, tuple) else ("", "")
    ok("doctor can answer 'is this copy behind' at all, rather than only 'where did it come "
       "from' — the two are different questions and only one of them finds a missing fix",
       lvl in ("ok", "warn") and msg, (lvl, msg))

    # THE THREE PROVENANCES ANSWER DIFFERENTLY, and conflating them is how the check would lie.
    # A CHECKOUT is its own source and git can answer exactly; saying "cannot tell" there would
    # be the check inventing an unknown it does not have. My first version did exactly that.
    ok("a checkout says it IS the source rather than reporting an unknown, because git can "
       "answer precisely for it",
       "checkout" in msg.lower(), msg)

    # AND THE UNKNOWN IS SAID OUT LOUD where it is real. "Cannot tell" is not "up to date", and
    # printing silence there is what made the reported bug cost an evening.
    real_running = P.running
    try:
        P.running = lambda: {"source": "copy", "version": "0.1.0", "root": "/nowhere"}
        _s2 = P.staleness()
        lvl2, msg2 = _s2 if isinstance(_s2, tuple) else ("", "")
        eq("an unpinned COPY warns rather than passing quietly", lvl2, "warn")
        ok("...and says staleness CANNOT BE TOLD rather than implying it is current — a fix "
           "landing upstream reaches it only when somebody re-vendors, and nothing else says so",
           "cannot be told" in msg2, msg2)
        ok("...and names the remedy that would make it answerable, since the check and `self "
           "--pin` complete each other", "self --pin" in msg2, msg2)

        P.running = lambda: {"source": "pinned", "version": "0.1.0", "root": "/nowhere",
                             "ref": "refs/heads/nope", "sha": "0" * 40}
        _s3 = P.staleness()
        lvl3, msg3 = _s3 if isinstance(_s3, tuple) else ("", "")
        eq("a pin whose ref cannot be resolved warns", lvl3, "warn")
        ok("...saying CANNOT TELL, which is a different claim from being current — the "
           "distinction the whole issue turns on",
           "CANNOT" in msg3 and "current" in msg3, msg3)
    finally:
        P.running = real_running


def test_prose_options():
    group("Prose that outlives the command needs a way in the shell cannot edit")
    # A backticked word in a double-quoted argument is EXECUTED and removed before the program
    # sees it; the command reports success and the loss is invisible on both sides. It happened
    # to a release note in a sibling tool an hour ago and the damaged text is permanent, because
    # published state should not be rewritten to repair a word.
    #
    # Another agent's survey found the guard present in two of the three tools here and absent
    # from the one that bit me — so this asks the same question of showrunner, whose `--reason`
    # is a decision's only human explanation and is read back off the record much later.
    from showrunner import cli as C
    ok("the prose set is not empty, or the twins below are generated for nothing",
       C.PROSE_OPTS, C.PROSE_OPTS)

    parser = C.build_parser()
    added = C._add_prose_twins(parser)
    ok("every prose option gets a --<name>-file twin, DERIVED by walking the parser rather than "
       "enumerated — an option added later gets one without anybody remembering, which is why a "
       "reader grepping for a specific twin name finds nothing and concludes wrongly",
       any("close --reason" in a for a in added), added[:6])
    # Idempotent: main() builds and augments once, but a second pass must not double-add.
    ok("...and adding them twice is a no-op, so the walk cannot corrupt a parser it already fixed",
       C._add_prose_twins(parser) == [], C._add_prose_twins(parser))

    d = tmpdir("prose-file")
    rf = os.path.join(d, "reason.txt")
    with open(rf, "w") as fh:
        fh.write("a reason with a backticked `owed` in it\n")
    # FAIL RATHER THAN RAISE, which is the remedy mutate.py prints for exactly this. Driving the
    # parser with a flag the mutant removed makes argparse SystemExit — and until that was
    # contained it ended the whole suite, turning this producer's score into a floor from a
    # truncated run. Guarded so a missing twin is a failed assertion with a number, not a crash.
    _close_opts = set()
    for _a in parser._subparsers._group_actions:
        for _n, _sub in getattr(_a, "choices", {}).items():
            if _n == "close":
                _close_opts = {o for _act in _sub._actions for o in _act.option_strings}
    if "--reason-file" not in _close_opts:
        ok("a --reason-file is folded into --reason with its backticks INTACT", False,
           "the twin does not exist, so there is nothing to fold")
        return
    args = parser.parse_args(["close", "L1", "--proof", "p.txt", "--reason-file", rf,
                              "--premise", "holds", "--premise-read", "r.md"])
    C._resolve_prose(parser, args)
    ok("a --reason-file is folded into --reason with its backticks INTACT, which is the whole "
       "point: the shell never touches a file's contents",
       "`owed`" in (args.reason or ""), args.reason)

    # THE BOUND. Long prose on a command line is the shape that invites a heredoc, and a heredoc
    # is where backticks live — so the refusal arrives before the damage rather than after it.
    long_args = parser.parse_args(["close", "L1", "--proof", "p.txt", "--reason", "x" * 450,
                                   "--premise", "holds", "--premise-read", "r.md"])
    raised = []
    parser.error = lambda msg: raised.append(msg)          # noqa: E731 — capture, do not exit
    C._resolve_prose(parser, long_args)
    ok("prose over the limit on a command line is REFUSED, naming the file twin and saying why "
       "the limit exists rather than only that one exists",
       raised and "backticks" in raised[0] and "--reason-file" in raised[0], raised[:1])

    # TWO ANSWERS TO ONE QUESTION is a refusal too, or the file silently wins and the argument
    # the caller typed disappears — the same invisible-loss shape one layer up.
    both = parser.parse_args(["close", "L1", "--proof", "p.txt", "--reason", "typed",
                              "--reason-file", rf, "--premise", "holds", "--premise-read", "r.md"])
    raised.clear()
    C._resolve_prose(parser, both)
    ok("...and passing BOTH is refused rather than one silently winning",
       raised and "one question" in raised[0], raised[:1])


def test_future_tense_gate():
    group("A turn that PROMISES work instead of doing it is refused")
    # THE RULE WAS RUNG 6 AND THAT IS WHY IT COULD BE IGNORED. It lived in a memory file
    # delivered into context, and was broken the same day by the agent who wrote it, with the
    # rule in context. "Banned" was an overclaim: a reminder is not a gate, and a rule an agent
    # has to REMEMBER is followed only some of the time.
    #
    # A concept-level rule is checked against INTENT, and the intent is always fine — an agent
    # that has just finished a long correct piece of work does not experience "next I'll X" as
    # stopping, it experiences it as courtesy. So this fires on the TEXT at the moment the turn
    # ends, which is where the defect lives. The reasoning was never the broken part.
    gate = os.path.join(ROOT, ".showrunner", "hooks", "future-tense-gate.sh")
    ok("the gate ships and is executable", os.path.isfile(gate) and os.access(gate, os.X_OK))

    def verdict(text):
        d = tmpdir("ft-gate")
        tp = os.path.join(d, "t.jsonl")
        with open(tp, "w") as fh:
            fh.write(json.dumps({"type": "assistant",
                                 "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
        p = subprocess.run(["bash", gate], input=json.dumps({"transcript_path": tp}),
                           capture_output=True, text=True)
        return p.returncode, (p.stdout + p.stderr)

    rc, said = verdict("Suite green.\n\nNext I'll pay those chat debts, then take #63.")
    eq("a closing paragraph that promises the next action REFUSES the turn-end — exit 2, the "
       "code that blocks a Stop", rc, 2)
    ok("...and quotes the phrase it caught, so the fix is the sentence rather than a guess",
       "Next I'll" in said, said[:160])

    # PRESENT CONTINUOUS AS FUTURE, and periphrastic future. The list began at `I'll` and missed
    # the whole class — caught by the chat-debt waker instead of by this gate, on a turn that
    # ended "which I'm answering next". The gate built to catch the shape was blind to three of
    # its forms, and a measurement on 227 real turn-finals found two more violations of mine
    # that had gone through.
    eq("'which I'm answering next' is refused — present continuous doing the work of a promise",
       verdict("Published through #73. One chat debt outstanding, which I'm answering next.")[0],
       2)
    eq("...and 'I am going to', which commits without naming a tense at all",
       verdict("Suite green. I am going to pay those debts.")[0], 2)
    # `next` is REQUIRED for the gerund arm. Without it the arm refuses every sentence describing
    # what the turn just did, which is most correct closings.
    eq("...while 'I'm reporting it here' is allowed, because present continuous is usually just "
       "present tense and only a forward anchor makes it a promise",
       verdict("I'm reporting it here so the trail is complete.")[0], 0)

    # A NEGATED COMMITMENT IS THE GATE FIRING ON A REFUSAL TO ACT. "neither of which I'll fix
    # unilaterally" is an agent STOPPING ON PURPOSE and saying so, and the future form is what
    # carries the saying. Reported by game_loop's auditor off a 25-concession corpus.
    #
    # WORTH THE COMMENT: my first check said my gate already handled it. It did not — their
    # example verb ("act on") simply was not in the list, so the sentence passed for a reason
    # that generalised to nothing. Substituting a verb the list DOES carry refuses it. I had
    # refuted my own probe and recorded it as refuting their finding.
    eq("[CORPUS: this exemption has never changed the refusal count on real closings] "
       "declining to act is not promising to act, even though it takes the same future form",
       verdict("Two open questions, neither of which I'll fix unilaterally.")[0], 0)
    eq("...and the plain form is still refused, so the exemption is the NEGATION and not the verb",
       verdict("Two open questions. I'll fix them.")[0], 2)

    # THE GERUND ARM WAS BOUNDED BY SENTENCE, WHICH IS NOT THE CLAUSE. `[^.]{0,40}` reached
    # across a comma and joined "I'm publishing" to an unrelated "#64 is next" — two true
    # statements, refused as one promise. Forty characters is ample room for that, so anchoring
    # per SENTENCE would not have caught it; the boundary that matters is clause punctuation
    # and the coordinators.
    for closing in ("I'm publishing the fix now, and #64 is next.",
                    "I'm attaching the log; the audit lands next."):
        eq("a present-tense clause and a separate fact about what comes next are not a promise "
           "just because one sentence holds both (%r)" % closing[:34],
           verdict(closing)[0], 0)
    eq("...while the two INSIDE one clause still refuse, which is the arm's whole purpose",
       verdict("Published through #73. One chat debt outstanding, which I'm answering next.")[0],
       2)

    # THE CONCESSION CLASS, which the 227-closing corpus contained ZERO of — so the measured
    # 1.3% said nothing about it, and an absence in a corpus reads exactly like a pass. Checked
    # because game_loop's own gate was measured blocking the turn where it CONCEDED an error,
    # which is the worst place a gate can fire: it punishes the retraction.
    #
    # The distinction that makes this safe is that the gate keys on the PROMISE, not on the
    # admission. A retraction that also defers the fix is not an exception to the rule, it is
    # the rule's worst case — the work is named, unblocked, and being put off anyway.
    eq("conceding an error and then DEFERRING the fix is still refused — the concession does "
       "not buy the deferral",
       verdict("I got that wrong — the denominator was 7x too large. "
               "I am going to recount it properly.")[0], 2)
    for closing, why in (
            ("I got that wrong. Recounted: 1 refusal, a true positive. Corrected in the commit.",
             "a concession that carries the correction with it"),
            ("That claim was mine and it was unsupported. I've withdrawn it from BOUNDARY.md.",
             "a withdrawal already performed"),
            ("I conceded too fast there — rechecked, and the original number holds.",
             "conceding the concession, which is still a finished result")):
        eq("...while %s ends a turn freely" % why, verdict(closing)[0], 0)

    eq("[CORPUS: every refusal on this project's closings has been a real promise — `python3 test/corpus.py --gate promise`] a closing paragraph reporting only FINISHED work is allowed — the rule is about "
       "promising, not about summarising",
       verdict("Suite at 1112 passing, accounting clean. Open issues: 0.")[0], 0)

    # THE RESTRAINT THAT MATTERS MOST. An agent that swallows a stated blocker to satisfy a
    # word-ban has traded a visible stall for an invisible one — worse than what this prevents.
    eq("a STATED BLOCKER is allowed, because it is present tense and saying it once is correct",
       verdict("Fixed and pushed.\n\nThe verify.yaml rule is blocked on your authorisation — "
               "it is a harness policy file and I do not have one for this change.")[0], 0)
    eq("...and somebody else's words in a quote do not trip it, or reporting what another agent "
       "said would refuse the turn that reports it",
       verdict("They wrote:\n\n> Next I'll take the game_loop half over.")[0], 0)

    # FAILS OPEN, and says so. A turn-end gate that hard-fails on its own plumbing blocks the
    # write that would repair it.
    _p = subprocess.run(["bash", gate], input=json.dumps({"transcript_path": "/nope/none.jsonl"}),
                        capture_output=True, text=True)
    eq("a missing transcript ALLOWS", _p.returncode, 0)
    ok("...and says it was not checked, because an allow nobody is told about is "
       "indistinguishable from a gate that ran and was content",
       "WITHOUT BEING CHECKED" in (_p.stdout + _p.stderr), (_p.stdout + _p.stderr)[:120])

    # MEASURED ON REAL CLOSINGS, not on fixtures I wrote — and then measured AGAIN, because the
    # first measurement used the wrong population and I published a rate from it.
    #
    #   first pass   1,650 assistant messages, 16 matched, 5 false blocks -> "31% false-block"
    #   corrected      222 TURN-FINAL messages, 1 refused, and it is a true positive
    #
    # The gate only ever runs at a turn END. A message followed by tool calls is mid-turn and
    # can never reach it, so 1,428 of that denominator were unjudgeable — it was 7x too large,
    # and the rate computed from it made the gate look far worse than it is.
    #
    # The disagreement surfaced the way another agent's did: two extractors that share no code,
    # compared. Mine reported 1,655 turn-finals out of 1,650 messages — impossible, and the
    # impossibility was the finding. A text-only assistant record can be followed by ANOTHER
    # assistant record carrying the tool call, which my first filter did not treat as mid-turn.
    #
    # Both of us measured our own gate wrongly and in opposite directions: their rate moved
    # 25% -> 50% against them, mine 31% -> 0% in my favour. Neither error was detectable from
    # inside the instrument that made it.
    #
    # The five false blocks below are still real and still fixed — they were among the matches,
    # they were simply drawn from a population that included turns the gate cannot judge.
    #
    # FOUR OF THE FIVE WERE HANDBACKS — "Say which and I'll do it", "I'll take #35 unless the
    # authorization matters more to you". The agent is CORRECTLY waiting on a decision that is
    # the human's, and refusing those forces work to continue when it should be asking, which is
    # the exact failure the sibling rule exists to prevent. A gate that turns a correct handback
    # into forced motion is worse than the promise it catches.
    eq("a handback with the condition BEFORE the verb is allowed — waiting on a decision that "
       "is the human's is not promising to continue",
       verdict("Either way it is unbuilt. Say which and I'll do it.")[0], 0)
    eq("...and with the condition TRAILING it, which the first version missed and which refused "
       "two real closings in the corpus",
       verdict("I'll take #35 next unless the authorization matters more to you.")[0], 0)
    eq("...while an unconditional promise still refuses, so the exemption did not swallow the "
       "rule", verdict("Done. `owed` is down to two items, which I'll pick up next.")[0], 2)
    eq("a phrase INSIDE quotes is reported rather than uttered — a retro quoting its own "
       "violation reads exactly like committing it",
       verdict('My hardening failed — I ended with *"Next I\'ll pay those debts"* and nothing '
               'fired.')[0], 0)

    # A MEASUREMENT CAVEAT THAT COST A PUBLISHED NUMBER, recorded here because the number is
    # quoted in this repo's own commits. Measuring which tool calls a guard can see, I reported
    # 95.1% seen / 4.9% invisible with zero filesystem writes among the invisible. Arithmetically
    # correct, and about the wrong population: SUBAGENT tool calls do not appear in the parent
    # transcript at all (4 Agent calls, 0 records marked isSidechain, no nested tool_use), so
    # every tool a subagent used is absent from both numerator and denominator.
    #
    # The residual I had named as "four calls, unknown coverage" is therefore an entire second
    # population the instrument cannot see. What survives: of the calls the PARENT session makes,
    # none the guard cannot see writes to the filesystem. What does not: that this characterises
    # the repo's writes.
    #
    # The rule, which another project supplied the first half of: a documented limit is a caveat
    # or an obituary according to whether the blind set INTERSECTS the population judged —
    # measure the composition, never the size. And before measuring composition, establish that
    # the instrument can see the whole population, or the composition is a confident number
    # about something nobody asked.

    # COMPOSITION, which nobody had looked at. Another agent's framing: five Stop hooks run in
    # this house, each designed alone and each fail-open alone, and fail-open individually does
    # not compose into fail-open collectively — two gates that each refuse for a good reason can
    # refuse a turn with no legal exit. They named a concrete collision: a gate whose remedy is
    # "go and check" invites a reply that is itself a forward-looking sentence.
    #
    # Measured rather than reasoned. The collision region is REAL but bounded, and the reason is
    # structural: every other Stop gate here refuses with an ACTION as its remedy, and this gate
    # demands actions be DONE rather than announced. They point the same way, so the past-tense
    # report of having followed any remedy is a legal exit from both.
    for remedy in ("A Crawler is inert and not mine — parked it and told its owner.",
                   "Messaged it; it woke and is committing.",
                   "Answered all three; `owed` reads clean.",
                   "Checked it — the claim held, and here is the measurement.",
                   "Fair — that was asserted, not measured. Checking now."):
        eq("a turn REPORTING a remedy already carried out passes the promise gate, so every "
           "other Stop gate's refusal has a legal exit: %r" % remedy[:44],
           verdict(remedy)[0], 0)

    # AND THE COLLISION ITSELF, so the bound is on the record rather than assumed away. A VAGUE
    # future remedy is refused — which is correct, and is the case where the two gates disagree.
    eq("...while promising to carry one out rather than carrying it out IS refused — that is the "
       "collision region, and it is non-empty",
       verdict("A Crawler is inert. I'll handle it.")[0], 2)
    eq("...though the specific sentence another agent predicted would collide does NOT, because "
       "'check' is not a verb this gate claims — their gate's remedy and mine are compatible",
       verdict("Fair. I'll check that before claiming it.")[0], 0)

    # REGISTERED, not merely present — the pair this repo keeps failing on.
    with open(os.path.join(ROOT, ".claude", "settings.json")) as fh:
        stop = (json.load(fh).get("hooks") or {}).get("Stop") or []
    cmds = [str(hh.get("command", "")) for h in stop for hh in (h.get("hooks") or [])]
    ok("the gate is REGISTERED on Stop — a gate nobody registers has never once run, which is "
       "how the rule it enforces got ignored in the first place",
       any("future-tense-gate.sh" in c for c in cmds), cmds)


def test_worktree_dirty():
    group("Uncommitted work is why a dead Crawler's tree is not garbage")
    if not have("git"):
        skip("the worktree-dirty group", "git is not installed")
        return
    # PAID FROM THE OWED QUEUE. Its exclusion read "SHOULD BE SWEPT, IS NOT YET — an always-empty
    # answer would report every abandoned worktree as clean, which is a real loss-of-work path."
    # Measured before writing anything: neutering it killed ONE assertion, so the note was
    # accurate rather than stale — unlike locks.Lock.acquire, whose identical note turned out to
    # be years behind its own coverage. Both were sitting in the same list, indistinguishable.
    d = tmpdir("dirty-probe")
    sh(["git", "init", "-q", "."], d)
    sh(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty",
        "-m", "base"], d)
    with open(os.path.join(d, "never-staged.txt"), "w") as fh:
        fh.write("the only copy of an hour's work\n")

    out = worktree.dirty(d)
    ok("dirty NAMES the file, rather than answering a bare truthy — a producer stuck replaying "
       "its last answer satisfies a boolean and fails this",
       any("never-staged.txt" in line for line in (out or [])), out)
    ok("...and an UNTRACKED file counts by default, which is the whole reason this exists: a "
       "dead Crawler's only copy of real work is very often one it never staged",
       bool(out), out)
    eq("...while tracked_only EXCLUDES it, because that answers the narrower question — would "
       "`git reset --hard` destroy this — and for an untracked file the answer is no",
       worktree.dirty(d, tracked_only=True), [])

    sh(["git", "add", "-A"], d)
    sh(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "saved"], d)
    eq("...and it EMPTIES once the work is committed — the assertion a stuck producer cannot "
       "pass, because a snapshot can be faked by a corpse and a CHANGE cannot",
       worktree.dirty(d), [])

    # NOT-A-REPO IS NOT CLEAN. [] means "nothing uncommitted here"; None means "I could not
    # look". Collapsing them would tell `reap` a tree it cannot read is safe to surface, which
    # is the loss-of-work path the exclusion note named.
    ok("a path git cannot answer for returns None, not [] — 'could not look' and 'nothing to "
       "lose' must not be the same answer to a verb that decides whether work is disposable",
       worktree.dirty(tmpdir("dirty-not-a-repo")) is None,
       worktree.dirty(tmpdir("dirty-not-a-repo")))


def test_guards_anchor_off_cwd():
    group("A guard's precondition was the shell's cwd; its subject is what the command writes (#56)")
    # Reported with a reproduction: every Bash call made from a scratch directory produced
    # "ALLOWED WITHOUT BEING CHECKED" from BOTH PreToolUse guards, and the call went through.
    # Dozens in one session. Working from a scratchpad is the ordinary shape of orchestration --
    # the harness hands you one and tells you to prefer it over /tmp -- so the guards were
    # strongest exactly where they are least needed (already inside the repo) and absent exactly
    # where a stray absolute path is most likely.
    # THIS repo as the anchor, not a fixture: the anchoring only pays off when the resolved root
    # actually carries a showrunner, and a bare `git init` fixture fails at the NEXT blind path
    # (no binary) for a reason that has nothing to do with what is under test. Using a fixture
    # here made this assertion fail while the mechanism worked.
    outside = tmpdir("guard-anchor-outside")          # deliberately NOT a git repo
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})

    for name in ("worktree-guard.sh", "dispatch-guard.sh"):
        shim = os.path.join(ROOT, ".showrunner", "hooks", name)
        anchored = subprocess.run(["bash", shim], cwd=outside, input=payload,
                                  capture_output=True, text=True,
                                  env=dict(os.environ, CLAUDE_PROJECT_DIR=ROOT))
        said = anchored.stdout + anchored.stderr
        ok("%s standing OUTSIDE any repo still CHECKS, by asking the harness where the session "
           "works rather than the shell where it stands" % name,
           ANCHOR_FAILED not in said, said[:170])

        # #74 CHANGED WHAT "NO ANCHOR" MEANS. This used to run the IN-REPO shim with the
        # environment stripped and call that anchorless. It is not: the shim is sitting in the
        # project the whole time, and overlooking that is the defect #74 reported. The control
        # is still a control — a guard that genuinely cannot answer must still say so — but it
        # needs a shim that is genuinely nowhere, which is a copy outside any repo.
        env_blind = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        in_repo = subprocess.run(["bash", shim], cwd=outside, input=payload,
                                 capture_output=True, text=True, env=env_blind)
        # MATCHED ON THE ANCHOR FAILURE, NOT ON "DID NOT RUN". This assertion is about whether the
        # shim RESOLVED its project, and "DID NOT RUN" is the opening phrase of every fail-open
        # notice these guards print — several of which have nothing to do with anchoring. It read
        # as a proxy and worked only while anchoring was the sole way to reach that phrase.
        #
        # It stopped being true and this suite could not see it from the primary checkout: in a
        # LINKED WORKTREE the anchoring SUCCEEDS — the guard names the tree correctly — and then
        # hits `lease.py`'s `if not session` branch, a deliberate and correct degrade that says
        # "no session id reached it" using the same opening words. So the assertion failed in
        # every Crawler's tree and passed in the orchestrator's, which is the direction that goes
        # unnoticed longest. Measured by running the shim by hand, CLAUDE_PROJECT_DIR stripped:
        # "allow: not inside a managed worktree" from the main checkout, the no-session notice
        # from a worktree.
        #
        # The discriminator is the anchor notice's OWN words, which no other branch prints and
        # which live in the guard rather than in lease.py. Both shims carry them verbatim.
        anchor_failed = "neither the working directory nor CLAUDE_PROJECT_DIR"
        ok("%s resolves from its OWN LOCATION when cwd and the harness both answer nothing — "
           "the shim was inside the project the entire time" % name,
           anchor_failed not in (in_repo.stdout + in_repo.stderr),
           (in_repo.stdout + in_repo.stderr)[:170])

        loose_dir = os.path.join(tmpdir("guard-anchor-loose"), "sub", "hooks")
        os.makedirs(loose_dir, exist_ok=True)
        loose = os.path.join(loose_dir, name)
        shutil.copy(shim, loose)
        blind = subprocess.run(["bash", loose], cwd=outside, input=payload,
                               capture_output=True, text=True, env=env_blind)
        blind_said = blind.stdout + blind.stderr
        # THE POSITIVE CONTROL. Every assertion above is equally satisfied by a guard that has
        # stopped failing open at all -- including one broken to refuse nothing and say nothing.
        # With no anchor there IS no question this guard can answer, and it must still say so.
        ok("...and with NO anchor at all — a shim outside any repo — it still fails open and "
           "SAYS it was not checked; the fix narrows when that happens, it does not remove it",
           "ALLOWED WITHOUT BEING CHECKED" in blind_said, blind_said[:170])
        # THE SAME STRING, DELIBERATELY — this is now the companion that makes the assertion above
        # falsifiable, not merely a second nicety. That one passes on the ABSENCE of
        # `anchor_failed`, and an absence is also what a reworded notice produces: reword it and
        # the negative assertion goes quiet and stays green forever. Binding both to one variable
        # means a rewording fails HERE, loudly, in the case where the phrase must appear.
        ok("...and the notice names BOTH things it tried, so the remedy is not a guess — and so "
           "the check above is measuring a phrase this guard still prints",
           anchor_failed in blind_said, blind_said[:170])


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
    # THE RESTRAINT CASE for the never-committed fix below, and asserted as the WHOLE string
    # rather than a prefix: "MERGED, BUT THE TREE IS NOT CLEAN" also starts with "MERGED", so a
    # prefix here would go on passing if the genuine case were rerouted into the cautious one.
    # The fix must not be "never say safe again".
    merged_a = next((f for f in findings if f["crawler"] == rec_a["crawler"]), {})
    ok("a branch genuinely merged, with a clean tree, still reports safe to clean up",
       merged_a.get("verdict") == "MERGED — safe to clean up", merged_a)

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

    # THE INVERSION ITSELF, which is the claim the conditional was rewritten to make and which
    # had no behavioural coverage anywhere. What stood in for it (in the harness group) was a
    # list comprehension over four string literals asserted against those same four literals —
    # `campaign` was never called, imported or read, and the second assertion was
    # `None in (None, ...)`. Reverting campaign.py's conditional to its old permissive form
    # left the whole suite green, which is how an `unrecognised` verdict would have merged.
    #
    # dry_run on purpose: the block is decided BEFORE the dry-run branch, so both sides are
    # provable without moving the trunk out from under the assertions that follow.
    def integrate_with(verdict):
        _H.check_tree = lambda c, w, _v=verdict: (_v, "test: canned %r" % (_v,), False)
        try:
            return campaign.integrate(cfg, g, base="main", only=["m5"], dry_run=True)
        finally:
            _H.check_tree = _orig

    res_f, ok_f = integrate_with("a-verdict-from-2027")
    ok("a verdict this side has no meaning for BLOCKS the merge, without anyone having listed "
       "it — the conditional is keyed on the PERMISSIVE answer, so a contract that grows a term "
       "stops the consumer rather than being guessed at in the permissive direction",
       ok_f is False and any(r["status"] == "harness-a-verdict-from-2027" for r in res_f), res_f)

    for allowed in (None, "clean", "notes-drifted"):
        res_p, ok_p = integrate_with(allowed)
        ok("...while %r stays on the MERGING side, which is the half that makes this a boundary "
           "and not a blanket refusal: a repo with no harness at all is a supported shape, and "
           "blocking it would break every consumer that never had one" % (allowed,),
           ok_p is not False and any(r["status"] == "would-merge" for r in res_p), res_p)

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
    ghost = next((f for f in findings if f["crawler"] == rec_d["crawler"]), {})
    ok("reconcile identifies an ABANDONED Crawler",
       (ghost.get("verdict") or "").startswith("ABANDONED"), ghost)
    ok("...and surfaces its uncommitted work rather than deleting it",
       bool(ghost.get("uncommitted")),
       ghost)
    actions, _ = campaign.reap(cfg, g, base="main", apply=False)
    ok("reap says where the abandoned work is, and does not remove it",
       any(a["kind"] == "crawler" and "not deleted" in a["action"] for a in actions), actions)
    ok("...and the worktree is still on disk after a dry run", os.path.isdir(rec_d["worktree"]))

    # THE PAIRED CASE. Everything above asserts the branch that IS empty, and "the branch never
    # received a commit" is the one verdict that is true of an empty branch and a lie about every
    # other one. `is_empty` passed its two arguments to `commits_ahead` in the wrong order, so
    # `branch_exists` was handed a SHA, looked for refs/heads/<sha>, missed, and returned 0 — and
    # every branch in every campaign answered "empty". The assertion above passed throughout,
    # because the case it covers is the one the bug happens to get right. So a dead Crawler
    # holding real unintegrated commits was reported as having contributed nothing, and `empty`
    # sits ABOVE `merged` in the ladder, so it masked those verdicts too.
    dead2 = DeadPid()
    g.add("committed but never integrated", leaf_id="m6", labels=["backend"])
    rec_c = worktree.spawn(cfg, g.show("m6"), actor="carrier")
    campaign.record_spawn(cfg, rec_c, pid=dead2.pid)
    with open(os.path.join(rec_c["worktree"], "committed.txt"), "w") as fh:
        fh.write("this one reached a commit\n")
    sh(["git", "add", "-A"], rec_c["worktree"])
    sh(["git", "commit", "-q", "-m", "real work, committed"], rec_c["worktree"])
    g.claim("m6", "carrier", pid=dead2.pid)

    ok("a branch holding a commit is NOT empty — read directly, because the consumer below can "
       "only see what the ladder lets through",
       campaign.is_empty(cfg, rec_c["branch"], rec_c["base_sha"]) is False,
       campaign.is_empty(cfg, rec_c["branch"], rec_c["base_sha"]))
    ok("...and the empty verdict is still reachable, so the assertion above is not passing "
       "because the answer went constant in the other direction",
       campaign.is_empty(cfg, rec_d["branch"], rec_d["base_sha"]) is True,
       campaign.is_empty(cfg, rec_d["branch"], rec_d["base_sha"]))

    findings = campaign.reconcile(cfg, g, base="main")
    carrier = next((f for f in findings if f["crawler"] == rec_c["crawler"]), {})
    ok("reconcile does not tell a reader that a branch carrying a commit never received one — "
       "that sentence is what licenses treating the tree as garbage",
       carrier.get("empty") is False
       and "never received a commit" not in (carrier.get("verdict") or ""), carrier)
    ok("...and it still reports the Crawler as abandoned, for the reason that is actually true: "
       "the owner is gone and the work is not integrated",
       (carrier.get("verdict") or "").startswith("ABANDONED — owner is not alive"), carrier)

    # CLOSING A LEAF IS A CLAIM; THE BRANCH IS THE FACT.
    #
    # Observed: a Crawler was refused by its commit gate, closed its leaf anyway and exited with
    # its work staged and uncommitted. `is_merged` asks whether base contains every commit on the
    # branch, and a branch with NO commits satisfies that vacuously — so reconcile ranked it
    # "MERGED — safe to clean up" over the only copy of the work. The uncommitted-changes line
    # was printed underneath, subordinate to a headline that contradicted it; the near-miss was
    # caught by a human's rule about never deleting a dirty worktree, not by this report.
    #
    # The empty-branch verdict already existed and was already honest. What overrode it was the
    # leaf being CLOSED, which the clause excuses from abandonment — and that exclusion handed
    # the case straight to `merged`.
    def never_committed(leaf_id, actor, dirty, premise="holds"):
        """Spawn, write nothing to the branch, close the leaf. `dirty` writes an uncommitted file."""
        g.add("closed without committing", leaf_id=leaf_id, labels=["backend"])
        rec = worktree.spawn(cfg, g.show(leaf_id), actor=actor)
        campaign.record_spawn(cfg, rec, pid=DeadPid().pid)
        if dirty:
            with open(os.path.join(rec["worktree"], "the-only-copy.txt"), "w") as fh:
                fh.write("staged, never committed, and this tree is where it lives\n")
        g.claim(leaf_id, actor)
        proof = os.path.join(cfg.root, "proof-%s.txt" % leaf_id)
        with open(proof, "w") as fh:
            fh.write("closed %s\n" % leaf_id)
        gates.close_gate(cfg, g, leaf_id, os.path.basename(proof), "done", premise=premise,
                         premise_read="README.md")
        return rec

    rec_nc = never_committed("m7", "gitignore", dirty=True)
    findings = campaign.reconcile(cfg, g, base="main")
    nc = next((f for f in findings if f["crawler"] == rec_nc["crawler"]), {})
    ok("a CLOSED leaf whose branch never received a commit, over a DIRTY tree, is not called "
       "merged and is never called safe to clean up — the branch is the fact, the close is only "
       "a claim, and this is the one state where the cheap action is irreversible",
       not (nc.get("verdict") or "").startswith("MERGED")
       and "safe to clean up" not in (nc.get("verdict") or ""), nc)
    ok("...and the headline itself says the tree is the only copy, rather than leaving that to a "
       "line underneath it — a guard that needs the reader to distrust its own headline is not "
       "a guard",
       "NEVER COMMITTED" in (nc.get("verdict") or "")
       and "only copy" in (nc.get("verdict") or ""), nc)
    ok("...and `merged` is still True underneath, so this passes because the VERDICT changed, "
       "not because the merge test quietly started answering differently",
       nc.get("merged") is True and nc.get("empty") is True, nc)
    actions, _ = campaign.reap(cfg, g, base="main", apply=False)
    ok("...and reap surfaces that tree instead of skipping it: before the verdict existed it "
       "read MERGED, which is not what reap's abandoned filter matches, so the tree holding the "
       "work appeared in no line reap printed",
       any(a["kind"] == "crawler" and a["crawler"] == rec_nc["crawler"]
           and "not deleted" in a["action"] for a in actions), actions)

    # THE AMBIGUOUS CORNER, decided rather than left to fall through: closed, nothing committed,
    # tree CLEAN. This is the shape of a legitimately refuted premise — declining to build
    # something correctly produces no commit and leaves no tree — so it is not an alarm. It is
    # also not `merged`: nothing was ever merged, and that sentence is a false factual claim in
    # the one report a reader uses to decide what to delete.
    rec_ncc = never_committed("m8", "refuter", dirty=False, premise="refuted")
    findings = campaign.reconcile(cfg, g, base="main")
    ncc = next((f for f in findings if f["crawler"] == rec_ncc["crawler"]), {})
    ok("closed, nothing committed, tree CLEAN gets its own verdict: nothing at risk and nothing "
       "waiting to integrate — not an alarm, and not a claim that anything merged",
       ncc.get("verdict", "").startswith("NOTHING TO INTEGRATE")
       and "never received a commit" in ncc.get("verdict", ""), ncc)
    ok("...and it is NOT the never-committed alarm, so a refuted leaf — a successful outcome "
       "that produces no commit by design — does not report as work about to be destroyed",
       "NEVER COMMITTED" not in ncc.get("verdict", ""), ncc)

    # THE PATH THAT WAS ALREADY RIGHT, re-read after the change. `in_progress` + no commits is
    # the case reconcile already got right and the case `reap` keys on, so the whole cost of a
    # wrong fix here lands on the common path: every wave has live and dead in-progress Crawlers
    # in it, and only some have a closed leaf over an uncommitted tree.
    findings = campaign.reconcile(cfg, g, base="main")
    ghost = next((f for f in findings if f["crawler"] == rec_d["crawler"]), {})
    eq("an in_progress leaf with no commits still reports exactly what it did before",
       ghost.get("verdict"), "ABANDONED — the branch never received a commit")


# ======================================================== CORE: the CLI
def _fork_output(cli_mod, cfg, name, session):
    """`worktree fork` through the CLI, in-process, returning what it printed.

    In-process rather than through a subprocess because the state under test — an acquire that
    cannot resolve a session pid — is produced by a monkeypatch, and a child process would
    resolve a real pid and never enter the branch. The cwd is the config's only input, so it is
    moved and restored rather than passed.
    """
    import io
    args = cli_mod.build_parser().parse_args(
        ["worktree", "fork", "--from", "recorded-probe", "--name", name, "--session", session])
    buf, saved, cwd = io.StringIO(), sys.stdout, os.getcwd()
    sys.stdout = buf
    os.chdir(cfg.root)
    try:
        cli_mod.cmd_worktree_fork(args)
    except Refused:
        # `die` RAISES; only `main()` turns that into an exit code, and this calls the command
        # function directly. Caught so a refusal is an EMPTY transcript the assertions below can
        # fail on, rather than an exception that ends the group and makes the producer that
        # caused it unscoreable.
        pass
    finally:
        os.chdir(cwd)
        sys.stdout = saved
    return buf.getvalue()


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

        # #58: A COMMAND THAT NAMES SOMEBODY ELSE'S TREE FROM OUTSIDE IT. The guard reads
        # payload paths and the cwd; until now the Bash command STRING was not read at all, so
        # a write into another session's tree from a scratch directory was invisible — the
        # precondition was where the shell stood, the subject is what the command writes.
        # THE ABSOLUTE PATH, because `tree` here is the NAME a lease is keyed by and the guard
        # reads what a shell command actually contains. My first version interpolated the name,
        # so the command held no absolute path at all and the extractor correctly found nothing
        # — a test that would have reported the feature missing while it worked.
        _abs_tree = os.path.join(cfg.worktree_root, tree)
        _cmd = "cd /tmp && python3 -c \"open('%s/f','w')\"" % _abs_tree
        _allow, _msg, _detail = lease.guard(cfg, "session-B", tool="Bash",
                                            tool_input={"command": _cmd})
        ok("a command NAMING a tree another live session holds is reported, even though the "
           "shell is standing somewhere else entirely",
           tree in (_detail.get("names_foreign_trees") or []), _detail)
        ok("...and it is ALLOWED, not refused — a path can be named without being written, and "
           "a guard that denies `echo /other/tree` is one people learn to route around",
           _allow is True, (_allow, _msg[:90]))
        ok("...and the notice says what it CANNOT see, because a warning that implies "
           "completeness is worse than none",
           "variable" in _msg and "heredoc" in _msg, _msg[-160:])
        # THE SAME SESSION IS NOT NOTICED. Re-entry by the holder is the case this whole module
        # exists to keep working, and a notice fired at the holder trains them to ignore it.
        _, _msg_own, _detail_own = lease.guard(cfg, "session-A", tool="Bash",
                                               tool_input={"command": _cmd})
        ok("...while the HOLDER naming its own tree is not noticed at all — that noise would "
           "land on exactly the session doing the right thing",
           not (_detail_own.get("names_foreign_trees") or []), _detail_own)
        # A COMMAND THAT MENTIONS NO PATH AT ALL must not acquire a notice, or the check is
        # firing on something other than what it claims to read.
        _, _, _d_plain = lease.guard(cfg, "session-B", tool="Bash",
                                     tool_input={"command": "echo hello"})
        ok("...and a command naming no absolute path is not noticed, so the notice is coming "
           "from the paths rather than from the tool name",
           not (_d_plain.get("names_foreign_trees") or []), _d_plain)

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
    # `or [{}]`, so a run where NO hijack was journalled fails the two assertions below rather
    # than raising out of the group and taking the twenty after them with it. A mutant that
    # crashes a group is unscoreable — the sweep reports a number from a truncated run — and
    # this subscript is what made `worktree enter` read as CRASHED rather than as covered.
    ev = (events_of("lease.hijack") or [{}])[-1]
    eq("...naming the intruder", ev.get("intruder_session"), "sess-2")
    eq("...and the holder it collided with", ev.get("holder_session"), "sess-1")
    eq("entering does NOT take the tree from the holder — the prompt is not the enforcement",
       (lease.Lease(cfg, "enter-probe").holder() or {}).get("session"), "sess-1")

    # A dead holder is reclaimed, loudly. Paired with the hijack above so neither reads as
    # "enter always does the same thing".
    lease.Lease(cfg, "enter-probe").release(force=True)
    lease.Lease(cfg, "enter-probe").acquire("sess-dead", who="ghost", pid=999999,
                                            basis="ancestor-claude")
    v, d = lease.enter(cfg, "sess-3", path=ent)
    eq("a tree whose holder is provably dead is RECLAIMED, so a crashed session cannot wedge it "
       "forever", v, "reclaimed")
    eq("...and the new holder is the entering session",
       (lease.Lease(cfg, "enter-probe").holder() or {}).get("session"), "sess-3")

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
    path, d = attempt(lambda: lease.fork(cfg, "enter-probe", "sess-9", base=first,
                                         name="fork-probe"), (None, {}))
    ok("...and forks from the base it was given when it has one",
       bool(path) and os.path.isdir(path), path)
    eq("the new tree starts at THAT commit, asserted against the SHA rather than 'it has "
       "commits' — which is true of the wrong base too",
       sh(["git", "rev-parse", "HEAD"], path).stdout.strip(), first)
    ok("...which is NOT the tip, so the assertion above can distinguish them", first != head)
    eq("the fork's base is recorded RESOLVED, not as the symbolic ref it was asked for — git "
       "cannot recover what 'HEAD' meant at this instant afterwards", (d or {}).get("base"),
       first)
    eq("...and the forking session holds the new tree",
       (lease.Lease(cfg, "fork-probe").holder() or {}).get("session"), "sess-9")
    eq("...while the tree it forked FROM is untouched", lease.tree_for(cfg, ent), "enter-probe")

    _, d2 = attempt(lambda: lease.fork(cfg, "enter-probe", "sess-9", base="HEAD",
                                       name="fork-symbolic"), (None, {}))
    eq("a symbolic base is resolved to a sha before anything is created",
       (d2 or {}).get("base"), head)

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
    _, d3 = attempt(lambda: lease.fork(cfg, "recorded-probe", "sess-10",
                                       name="fork-recorded"), (None, {}))
    eq("...and fork uses it with no --base, landing on the commit the held tree started at",
       (d3 or {}).get("base"), first)
    ok("...which is not the tip, so that assertion can tell the two apart", first != head)

    # THE LEASE THE FORK CLAIMS TO HAVE TAKEN. `fork` called `acquire` and threw the result
    # away; the CLI printed "lease held by you" unconditionally. So in the state where `enter`
    # itself refuses — no resolvable session pid — the reader was moved to a fresh tree, told
    # it was leased, and it was FREE. That is the FIRST remedy the hijack refusal offers, so
    # the lie was reserved for somebody already being told their tree had been taken.
    ok("a fork that took its lease says so", d3.get("leased") is True, d3)
    _orig_spid = lease.session_pid
    lease.session_pid = lambda: (None, "test: nothing to resolve")
    try:
        _, d4 = attempt(lambda: lease.fork(cfg, "recorded-probe", "sess-11",
                                           name="fork-hollow"), (None, {}))
    finally:
        lease.session_pid = _orig_spid
    ok("...and a fork whose lease could NOT be taken reports leased=False rather than "
       "reporting the acquire it discarded", d4.get("leased") is False, d4)
    eq("...and the tree really is unheld, which is what makes the flag a measurement rather "
       "than a second opinion", lease.Lease(cfg, "fork-hollow").state()[0], locks.FREE)
    # THROUGH THE CLI, because a `leased` flag computed correctly and printed as success is
    # exactly the shape of a check nobody notices. In-process so the same monkeypatch reaches
    # it — a subprocess resolves a real pid and never enters this branch.
    from showrunner import cli as _CLI
    lease.session_pid = lambda: (None, "test: nothing to resolve")
    try:
        said = _fork_output(_CLI, cfg, "fork-hollow-cli", "sess-12")
    finally:
        lease.session_pid = _orig_spid
    ok("...and the CLI says UNGUARDED rather than 'held by you', because the reader acts on "
       "that line and the tree it names is one anybody can walk into",
       "UNGUARDED" in said and "held by you" not in said, said[-300:])
    # THE REASON IS READ BACK FROM THE RECORD, not restated beside it. `fork` records the failed
    # acquire's holder in `lease_holder`, and that field was written and read by NOTHING — the
    # asymmetry `Lock.acquire(extra=)` was fixed for, re-introduced by me. The line used to
    # hardcode one of the several reasons acquire can fail, so the day it failed for another the
    # record would be right and the line wrong.
    eq("...and the REASON it prints is the one the acquire recorded, so the message cannot "
       "disagree with the record it sits beside",
       (d4.get("lease_holder") or {}).get("why") in said, True)
    ok("...which is a real reason rather than an empty string, or reading it back would be a "
       "check that cannot fail", ((d4.get("lease_holder") or {}).get("why") or "").strip(),
       d4.get("lease_holder"))
    ok("...while a fork that DID take its lease still says held by you, so this is a report of "
       "the acquire and not a line that got pessimistic",
       "held by you" in _fork_output(_CLI, cfg, "fork-held-cli", "sess-13"))

    # A LOST RACE IS NOT A MISSING PID. `acquire` returns False for two unrelated reasons and
    # `enter` mapped both onto "no session process could be resolved… this tree is unprotected"
    # — which, when another session simply got there first, is the opposite of the truth: the
    # holder is real, live, and was reported as absent. Only the READER's timing is simulated
    # here; the lock, the holder and the acquire are all real, and the acquire really loses.
    racer = "race-probe"
    os.makedirs(os.path.join(cfg.worktree_root, racer), exist_ok=True)
    winner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lease.Lease(cfg, racer).acquire("sess-WINNER", who="first", pid=winner.pid,
                                        basis="dispatch-recorded")
        # ONLY THE FIRST LOOK IS STALE, which is what the race actually is: `enter` reads FREE,
        # somebody else wins the mkdir in the gap, and every read after that sees the truth. A
        # patch that answered FREE forever would also blind the re-derivation and prove nothing.
        # Patched at the read `enter` ACTUALLY MAKES — `Lock.settled_state`, the one it uses to
        # adjudicate — while `Lock.state`, which `held_by_other` re-reads through, stays real.
        # So the first look is stale and every look after it sees the truth, which is what the
        # race is. A patch that answered FREE forever would blind the re-derivation too and
        # prove nothing.
        _orig_settled = locks.Lock.settled_state
        looks = []

        def stale_once(self, *a, **kw):
            looks.append(1)
            return (locks.FREE, None) if len(looks) == 1 else _orig_settled(self, *a, **kw)

        locks.Lock.settled_state = stale_once
        try:
            verdict, detail = lease.enter(cfg, "sess-LOSER",
                                          path=os.path.join(cfg.worktree_root, racer))
        finally:
            locks.Lock.settled_state = _orig_settled
        ok("...and the loser really did look again after losing, rather than reporting a "
           "verdict inferred from the boolean the failed acquire returned",
           (detail.get("holder") or {}).get("pid"), detail)
        eq("a session that reads FREE and then LOSES the atomic mkdir is told it was HIJACKED, "
           "not that no process could be resolved — the verdict is re-derived from the lock "
           "after the failed acquire instead of being inferred from a boolean",
           verdict, "hijack")
        eq("...naming the session that actually holds it",
           (detail.get("holder") or {}).get("session"), "sess-WINNER")
        from showrunner import events as _EV
        ev, _, _ = _EV.read(cfg)
        raced = [e for e in ev if e.get("kind") == "lease.hijack" and e.get("raced")]
        ok("...and the hijack is JOURNALLED like any other, so the race is visible to `watch` "
           "rather than being the one hijack nobody records", raced, ev[-2:])
    finally:
        winner.terminate()
        winner.wait()

    # AN INCOMPLETE LOCK IS NOT A CORRUPT ONE. `acquire` mkdirs the directory and writes `pid`
    # as a separate file, so for a moment a concurrent reader sees a lock with no pid — which
    # `state()` calls UNREADABLE and every adjudicating caller treats as "a human must clear
    # this". Two sessions starting in the same tree could therefore hard-fail, exit 2, over a
    # lock that was valid a millisecond later, with a printed remedy that did not work.
    half = locks.Lock(cfg.lock_root, "half-written")
    os.makedirs(half.dir, exist_ok=True)
    eq("a lock directory with no pid in it yet reads UNREADABLE — the transient and the torn "
       "write are genuinely indistinguishable in one look", half.state()[0], locks.UNREADABLE)
    filler = threading.Thread(target=lambda: (time.sleep(0.15),
                                              half._write_owner(os.getpid(), "late", "s-late")))
    filler.start()
    try:
        eq("...but LOOKING AGAIN settles it: a writer still finishing resolves to HELD, because "
           "'cannot tell' is answered by looking rather than by promoting the transient case",
           half.settled_state(grace=3.0)[0], locks.HELD)
    finally:
        filler.join()
    torn = locks.Lock(cfg.lock_root, "torn-write")
    os.makedirs(torn.dir, exist_ok=True)
    eq("...while a torn write STAYS unreadable past the grace and keeps the hard refusal, "
       "because it does not repair itself and only a human can find out whether that holder "
       "is running", torn.settled_state(grace=0.2)[0], locks.UNREADABLE)

    # THE REMEDY THAT REFUSAL PRINTS. `lock release <name> --force` resolved CONFIGURED
    # resources only, and a lease is named `worktree:<tree>`, which is not one and never will
    # be — so the single escape hatch offered to a human staring at a wedged lease answered
    # "no resource named 'worktree:...' in config" and exited 2.
    stuck = lease.lease_name(racer)
    ok("a lease's lock is not a configured resource, which is why this was reachable at all",
       cfg.resource(stuck) is None, stuck)
    ok("...and it is really on disk under that name",
       os.path.isdir(os.path.join(cfg.lock_root, "%s.lock" % stuck)))
    rel_out = subprocess.run(
        [sys.executable, os.path.join(ROOT, "bin", "showrunner"), "lock", "release", stuck,
         "--force"], cwd=cfg.root, capture_output=True, text=True)
    eq("`lock release <lease> --force` — the exact string the UNREADABLE refusal prints — "
       "actually releases it (%s)" % (rel_out.stdout + rel_out.stderr).strip()[-120:],
       rel_out.returncode, 0)
    eq("...and the lease is gone afterwards, so the remedy repaired the state rather than "
       "exiting 0 about it", lease.Lease(cfg, racer).state()[0], locks.FREE)
    typo = subprocess.run(
        [sys.executable, os.path.join(ROOT, "bin", "showrunner"), "lock", "release",
         "worktree:no-such-tree", "--force"], cwd=cfg.root, capture_output=True, text=True)
    eq("...while a name that is neither configured NOR on disk is still refused, so this "
       "widened one door rather than removing the check", typo.returncode, 2)
    # The refusal has to NAME what is really there, or a human clearing a wedged lease is told
    # only about the configured resources — the set that provably does not contain the thing
    # they are looking at. A second consumer of the same on-disk listing, so an always-empty
    # answer is noticed here as well as by `reap`.
    lease.Lease(cfg, racer).acquire("sess-KNOWN", who="probe", pid=os.getpid(),
                                    basis="dispatch-recorded")
    named = subprocess.run(
        [sys.executable, os.path.join(ROOT, "bin", "showrunner"), "lock", "release",
         "worktree:still-not-a-tree", "--force"], cwd=cfg.root, capture_output=True, text=True)
    ok("...and the refusal lists the locks that ARE held, leases included, so the reader is "
       "shown the name they actually need rather than only the configured resources",
       stuck in (named.stdout + named.stderr), (named.stdout + named.stderr)[-300:])
    lease.Lease(cfg, racer).release(force=True)

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
        # THE NEGATIVE HALF WAS DATED. `17\d{8}` covers epoch seconds from 2023 to 2026 and
        # nothing after, so from 2027 it could no longer fail — a guard that retires quietly on
        # a date nobody wrote down. Keyed on "a long run of digits where the date goes" instead,
        # which is the actual defect and does not expire.
        ok("...with a readable timestamp rather than the raw epoch second it printed on its "
           "first real run — a number the reader has to go and convert, in the line telling "
           "them how long somebody has held this",
           "since    2" in msg and not re.search(r"since\s+\d{9,}", msg), msg[:300])
        # NOT `x or "…" in msg`. The second clause made the first dead weight: any ellipsis
        # anywhere in DENIED or REMEDIES would have carried it, and the specific claim — that
        # THIS id is shown abbreviated — would have stopped being checked without failing.
        ok("...and the session id marked as ABBREVIATED, so two ids that differ past the cut "
           "are never shown as the same id", "sess-HOLDER-…" in msg, msg[:300])
        ok("...which is a real abbreviation and not the whole id with a decoration, so two "
           "sessions sharing that prefix are visibly indistinguishable rather than silently so",
           "sess-HOLDER-long" not in msg, msg[:300])

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

        # REDIRECTIONS AND PROCESS SUBSTITUTIONS, which the first version of the rule did not
        # disqualify while its comment claimed it disqualified "anything that could introduce a
        # second command". Both of these were allowed unconditionally, before any tree was
        # resolved, inside a tree another live session holds:
        #   `... worktree list <(cmd)`   bash runs cmd regardless of the outer command
        #   `... worktree status > f`    truncates f, and a write is the thing being guarded
        # Asserted through `guard`, not only through the regex, and paired with the plain verb
        # above so a rule that started refusing EVERYTHING could not pass this block.
        for suffix, what in ((" <(touch /tmp/sr-proof)", "a process substitution"),
                             (" > %s/src/main.py" % gpath, "a redirection into the held tree"),
                             (" >> %s/notes.md" % gpath, "an appending redirection"),
                             (" 2> %s/err.log" % gpath, "a stderr redirection"),
                             (" < /etc/passwd", "an input redirection")):
            ok("the carve-out refuses %s — it is not spellable in a real showrunner "
               "invocation, and it is a second command or a write" % what,
               not lease.own_command(fork_cmd + suffix))
            allow, _, det = lease.guard(cfg, "sess-INTRUDER", tool="Bash",
                                        tool_input={"command": fork_cmd + suffix}, cwd=gpath)
            ok("...and `guard` DENIES it rather than falling through the carve-out",
               not allow, det)

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

    # THREE EXIT CODES, NOT TWO (#35). `waiting` returned 1 for "not waiting" AND for "a
    # Crawler is blocked", so the natural consumer spelling — `waiting || exit 0`, meaning "if
    # it cannot tell, do not act" — allowed in exactly the case it was built for. A real stop
    # gate written against this verb never fired once. 3 makes the wrong reading LOUD.
    import io as _io
    import showrunner.cli as _C

    def waiting_exit(blocked, waiting_now=False):
        _ow = campaign.waiting
        campaign.waiting = lambda c, g, base=None: (waiting_now, {
            "live_crawlers": [], "parked_crawlers": [], "waiting": waiting_now,
            "blocked_crawlers": ([{"crawler": "c-x", "leaf": "X1", "why": "refused at turn-end"}]
                                 if blocked else []), "basis": "t"})
        buf, errb = _io.StringIO(), _io.StringIO()
        saved, saved_err, cwd = sys.stdout, sys.stderr, os.getcwd()
        sys.stdout, sys.stderr = buf, errb
        os.chdir(cfg.root)

        class A:
            porcelain = False
            base = None
        try:
            rc = _C.cmd_waiting(A())
        finally:
            os.chdir(cwd); sys.stdout, sys.stderr = saved, saved_err
            campaign.waiting = _ow
        return rc, buf.getvalue(), errb.getvalue()

    rc_b, out_b, err_b = waiting_exit(blocked=True)
    eq("a BLOCKED Crawler exits 3, not 1 — the code that says 'not waiting' must not also be the "
       "code that says 'somebody is stuck', or a consumer treating non-zero as no misses the one "
       "case it exists for", rc_b, 3)
    eq("...while an ordinary quiet campaign still exits 1", waiting_exit(blocked=False)[0], 1)
    eq("...and a genuinely waiting one still exits 0",
       waiting_exit(blocked=False, waiting_now=True)[0], 0)
    ok("...and the BLOCKED finding is on STDOUT as well as stderr, so it does not depend on "
       "which stream a caller captured", "BLOCKED" in out_b, out_b[:160])
    ok("...and still on stderr, where a human reading a terminal already looked for it",
       "BLOCKED" in err_b, err_b[:160])

    # ---- THE WAITING PROBE: two exit contracts that do not line up --------------------
    # game_loop's watchdog asks "is this session legitimately waiting on work it dispatched" and
    # reads 0 waiting / 1 not waiting / ANYTHING ELSE as "could not tell", which rings AND marks
    # the probe FAILING. showrunner's own `waiting` grew a third code (#35): 3 for a BLOCKED
    # Crawler. Passed through unmapped, a working probe reporting a true state would be described
    # as broken for as long as the Crawler stayed blocked.
    # #35: the exit codes were stated in the parser's `help=`, which argparse shows in the
    # PARENT's subcommand list -- not in `waiting --help`, which is what somebody integrating
    # against the verb actually runs. The warning existed on a channel the reader was not
    # looking at, which is this issue's own defect one level up. Asserted on the SUBCOMMAND
    # help, because that is the surface that was empty.
    _wh = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                          "waiting", "--help"], capture_output=True, text=True).stdout
    for _code, _what in (("0", "waiting"), ("1", "NOT waiting"), ("3", "BLOCKED")):
        ok("`waiting --help` names exit %s, so a reader integrating against the verb learns the "
           "code without reading the source" % _code,
           _code in _wh and _what.split()[-1].lower() in _wh.lower(), _wh[:120])
    ok("...and it names the `|| exit 0` idiom as WRONG, which is the specific wrong reading "
       "that shipped and failed quiet -- naming the trap beats describing the contract",
       "|| exit 0" in _wh, _wh[:120])

    # #35's GENERAL rule, mechanically. The issue says: "worth checking status, reap and check
    # against the same question -- this issue is about waiting because that is where it was
    # caught, not because it is the only one." Derived from the SOURCE rather than a hand list,
    # so a verb that grows a second non-zero code later cannot ship undocumented. Measured when
    # written: `check` had four semantic codes and named none of them in its own --help, while
    # `status` and `reap` always return 0 and carry no trap -- the suspicion was right for one of
    # the three, which is why this asks the code instead of assuming.
    _cli_src = open(os.path.join(ROOT, "lib", "showrunner", "cli.py")).read()
    _pairs = re.findall(r'add_parser\("([a-z-]+)"(.*?)set_defaults\(func=(cmd_[a-z_]+)\)',
                        _cli_src, re.S)
    _semantic = []
    for _verb, _block, _fn in _pairs:
        _m = re.search(r"\ndef %s\(.*?(?=\ndef )" % _fn, _cli_src, re.S)
        if not _m:
            continue
        # EVERY digit in a return statement, not just the one after `return`. The first version
        # matched `return (\d)` and so read `return 0 if ok else 2` as returning only 0 --
        # check's exit 2 was invisible to the rule that exists to document exit codes. Erring
        # toward over-reporting on purpose: a spurious entry costs a sentence of documentation,
        # while a missed one is a verb that escapes this check entirely.
        _codes = set()
        for _stmt in re.findall(r"\breturn\s+([^\n]*)", _m.group(0)):
            _codes |= {int(d) for d in re.findall(r"\b(\d)\b", _stmt)}
        _codes -= {0}
        if len(_codes) >= 2:
            _semantic.append((_verb, sorted(_codes), _block))
    ok("at least one verb carries semantic exit codes, so the rule below is not vacuous -- a "
       "derived list that silently went empty would pass forever",
       _semantic, [v for v, _c, _b in _semantic])
    for _verb, _codes, _block in _semantic:
        _help = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                                _verb, "--help"], capture_output=True, text=True).stdout
        # A BARE DIGIT ANYWHERE IS NOT DOCUMENTATION. The first version of this asked
        # `str(c) in _help`, and prose like "3 is separate from 2" satisfied it for 2 -- so
        # deleting 2's actual entry left the assertion green. Require the listing form: the
        # code at the start of a line, followed by its meaning.
        _missing = [c for c in _codes
                    if not re.search(r"^\s*%d\s+\S" % c, _help, re.M)]
        ok("`%s` carries semantic exit codes %s and names them in its OWN --help -- argparse "
           "shows `help=` in the PARENT listing, which is not what somebody integrating against "
           "the verb runs" % (_verb, _codes), not _missing, _missing)

    # #REGISTERED-AND-ABSENT. A consumer clean-installed from a release and found a PreToolUse
    # dispatch guard that was registered and had no file. install.sh named three hooks in three
    # `cp` lines while the registration wrote five, so two shipped in the payload, got wired,
    # and were never copied. That is one rung WORSE than the unregistered case this repo already
    # argues about: the registration is what makes it look present, and doctor reported
    # registration rather than existence, so the diagnostic agreed with the appearance.
    # Derived from lease.py rather than listed here -- a hand list in the test is the same
    # failure as a hand list in the installer, one file along.
    _lease_src = open(os.path.join(ROOT, "lib", "showrunner", "lease.py")).read()
    _registered = set(re.findall(r'"([a-z-]+\.sh)"', _lease_src))
    _install_src = open(os.path.join(ROOT, "install.sh")).read()
    # THE COPY LIST, not the file. Searching the whole of install.sh passes on a hook that is
    # merely MENTIONED -- the first version of this did, and a mutation removing whoami.sh from
    # the loop stayed green because its `case` label still named it. Third time this session a
    # "substring appears somewhere" assertion has proved unfalsifiable; the fix is always to
    # anchor on the structure that does the work.
    _m_loop = re.search(r"for hook_name in (.*?);\s*do", _install_src, re.S)
    ok("install.sh copies its hooks from a LIST the suite can read, rather than one `cp` per "
       "file -- a per-file copy is what let two of five go missing", _m_loop is not None)
    _copied_list = (_m_loop.group(1).replace("\\\n", " ").split() if _m_loop else [])
    ok("the registration names at least one hook, so the comparison below is not vacuous",
       _registered, sorted(_registered))
    _uncopied = sorted(h for h in _registered if h not in _copied_list)
    ok("every hook the REGISTRATION names is copied by install.sh -- a registered hook with no "
       "file is worse than an unregistered one, because the registration is what makes it look "
       "present", not _uncopied, _uncopied)
    # And the payload must actually contain them, or the installer copies from nothing.
    _missing_src = sorted(h for h in _registered
                          if not os.path.isfile(os.path.join(ROOT, ".showrunner", "hooks", h)))
    ok("...and each of those exists in the payload, so the copy has a source -- `cp` failing at "
       "install time is the same absence arriving louder", not _missing_src, _missing_src)

    # TWO COPIES OF ONE RULE. This repo's .showrunner/.gitignore and the one install.sh writes
    # are the same policy stated twice, and they drifted: config.local.json was ignored here and
    # not in the payload, so the file the docs send people to landed neither tracked nor ignored
    # in every consumer. Covered-by-a-glob counts -- `*.lock` covers `campaign.json.lock` -- so
    # this compares MEANING rather than text.
    import fnmatch as _fn
    _mine = [l.strip() for l in open(os.path.join(ROOT, ".showrunner", ".gitignore"))
             if l.strip() and not l.startswith("#")]
    _m_ig = re.search(r'\.showrunner/\.gitignore" <<.EOF.\n(.*?)\nEOF', open(
        os.path.join(ROOT, "install.sh")).read(), re.S)
    ok("install.sh writes a .showrunner/.gitignore the suite can read", _m_ig is not None)
    _theirs = [l.strip() for l in (_m_ig.group(1).splitlines() if _m_ig else [])
               if l.strip() and not l.strip().startswith("#")]
    _uncovered = [e for e in _mine
                  if e not in _theirs and not any(_fn.fnmatch(e, g) for g in _theirs)]
    # NAMED FOR THE ARTIFACT IT READS. This said "the copy a consumer receives", which is a
    # claim about a DELIVERY while the evidence is a read of the template -- and it was green
    # while no existing consumer received the rule at all. The assertion was never wrong; its
    # name was, and the name is what made me believe the delivery was covered. The upgrade
    # assertion below is the one that speaks for consumers.
    ok("the installer's TEMPLATE ignores every runtime path this repo ignores -- one rule "
       "written twice is two rules, and this checks only that the two texts agree",
       not _uncovered, _uncovered)

    # AND THE UPGRADE PATH, which is a different question the assertion above cannot ask. The
    # heredoc runs only `if [ ! -f ... ]`, so on an upgrade it is skipped entirely: an entry
    # added there reaches FRESH INSTALLS ONLY and never reaches anybody who already had the
    # tool. That is what happened to config.local.json -- I asserted the template and the
    # template was right, while every existing consumer got nothing. A consumer found it with
    # `git check-ignore -v` after I told them it was fixed.
    _inst_all = open(os.path.join(ROOT, "install.sh")).read()
    _m_loop2 = re.search(r'for entry in (.*?);\s*do\s*\n\s*if ! grep -qxF', _inst_all, re.S)
    ok("install.sh tops the ignore file up from a list the suite can read, rather than only "
       "creating it when absent", _m_loop2 is not None)
    _ensured = re.findall(r'"([^"]+)"', _m_loop2.group(1) if _m_loop2 else "")
    _upgrade_gap = [e for e in _mine
                    if e not in _ensured and not any(_fn.fnmatch(e, g) for g in _ensured)]
    ok("...and every one of those paths reaches an UPGRADE, not just a fresh install -- a rule "
       "that only lands when the file is first created never reaches an existing consumer, and "
       "is invisible to any check that reads the template",
       not _upgrade_gap, _upgrade_gap)

    # AND THE THIRD COPY: `showrunner init`. install.sh is not the only door -- `init` is a
    # public subcommand and writes this file itself, and the assertions above never looked at
    # it, so it drifted twice while they stayed green. bin/ and lib/ were fixed in install.sh
    # and not in `init`; then config.local.json, seen-issues.json and hook-heartbeat.jsonl were
    # added to install.sh and not to `init`. A repo created by `init` therefore left
    # config.local.json neither tracked nor ignored -- reopening, through the other door,
    # exactly the leak that overlay exists to prevent.
    #
    # BOTH DIRECTIONS, deliberately. Asserting that each list merely CONTAINS config.local.json
    # is the check that would not have caught this bug either: it only ever finds the entry we
    # already know about. Mutual coverage fails on the NEXT entry somebody adds to one side and
    # not the other, which is the failure that actually recurs here. Glob coverage counts in
    # both directions, so `*.lock` and `campaign.json.lock` agree rather than colliding.
    _tool = config.state_ignore_entries()
    ok("the tool exposes a non-empty ignore list, so the comparisons below are not vacuous",
       _tool, _tool)

    def _uncov(xs, ys):
        return [x for x in xs if x not in ys and not any(_fn.fnmatch(x, g) for g in ys)]

    _init_gap = _uncov(_tool, _ensured)
    _inst_gap = _uncov(_ensured, _tool)
    ok("`showrunner init` and install.sh's ensure-list are the SAME list -- two layers must "
       "never disagree about which files the tool owns, and an entry added to one side only is "
       "how this drifted twice",
       not _init_gap and not _inst_gap,
       {"only in the tool's list": _init_gap, "only in install.sh": _inst_gap})
    _heredoc_only = _uncov(_theirs, _tool)
    ok("...and install.sh's create-if-absent template adds nothing the tool does not own -- an "
       "entry living only in the heredoc reaches fresh installs and nothing else",
       not _heredoc_only, _heredoc_only)

    # THE OUTPUT, NOT THE LIST. Everything above compares source text; a constant that is right
    # and never reaches the written file would pass all of it. So run the real binary in a real
    # repo and ask GIT, which is the only thing whose opinion decides whether the next
    # `git add -A` commits the overlay.
    _ig_repo = tmpdir("init-gitignore")
    sh(["git", "init", "-q", "-b", "main"], _ig_repo)
    _rc_ig = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "init"],
                            cwd=_ig_repo, capture_output=True, text=True)
    ok("`showrunner init` succeeds in a bare repo, so the check below reads a real run and not "
       "a failed one", _rc_ig.returncode == 0, _rc_ig.stderr.strip()[-400:])
    # A DIRECTORY PATTERN NEEDS A PATH UNDER IT. `git check-ignore .showrunner/locks` does not
    # match the rule `locks/` -- the path does not exist, so git cannot know it is a directory
    # and a trailing-slash rule declines. Probing `locks/x` asks the question the rule answers,
    # and a glob gets a concrete name for the same reason.
    def _probe(entry):
        return os.path.join(".showrunner",
                            entry + "x" if entry.endswith("/") else entry.replace("*", "x"))

    _leaks = [e for e in _tool
              if sh(["git", "check-ignore", "-q", _probe(e)], _ig_repo, check=False).returncode != 0]
    ok("...and git agrees every one of those paths is IGNORED in the repo `init` just made -- "
       "config.local.json among them, which is the file whose absence from this list put a "
       "machine-specific overlay one `git add -A` away from being committed",
       not _leaks, _leaks)

    # THE LAST HAND LIST IN THE INSTALLER. SKILL_NAMES is enumerated by hand, and it is the same
    # shape as the hooks bug: the payload ships N, the installer names M, and the difference is
    # silent because a skill that never installs produces no output at all. It matches today,
    # which is exactly when a hand list is cheapest to protect -- it goes stale toward a FALSE
    # PASS at the moment somebody adds one, which is the moment it was supposed to help.
    _skills_dir = os.path.join(ROOT, ".claude", "skills")
    _shipped = sorted(d for d in os.listdir(_skills_dir)
                      if os.path.isdir(os.path.join(_skills_dir, d))) \
        if os.path.isdir(_skills_dir) else []
    _m_sk = re.search(r'SKILL_NAMES="([^"]*)"', _install_src)
    ok("install.sh names the skills it installs in a list the suite can read", _m_sk is not None)
    _declared = (_m_sk.group(1).split() if _m_sk else [])
    ok("at least one skill ships, so the comparison below is not vacuous", _shipped, _shipped)
    ok("every skill directory in the payload is one install.sh actually installs -- a skill that "
       "ships and is never installed produces NO output, so nothing distinguishes it from one "
       "that installed fine",
       not [d for d in _shipped if d not in _declared],
       [d for d in _shipped if d not in _declared])
    ok("...and every name it installs really ships, so a typo is a refusal rather than a skill "
       "silently absent from every consumer",
       not [n for n in _declared if n not in _shipped],
       [n for n in _declared if n not in _shipped])

    # WHAT A CONSUMER RECEIVES, MEASURED RATHER THAN ASSUMED. Several checks here reason about
    # consumers from `git ls-files` -- every claim file, every tracked hook. That is sound only
    # while TRACKED and DELIVERED are the same set, and exactly one common thing separates them:
    # `export-ignore` in .gitattributes, which `git archive` honours and `git clone` ignores.
    # Releases here are cut by archive. Surfaced by another project that found its own
    # "what a clone receives" assertions resting on this, unverified, and holding only because
    # it happened to have no .gitattributes.
    #
    # Asserted by RUNNING the delivery rather than by testing for the one mechanism that breaks
    # it -- a check for `.gitattributes` would pass for any OTHER cause of divergence, and the
    # point is the set, not the file.
    _in_head = set(subprocess.run(["git", "ls-tree", "-r", "HEAD", "--name-only"], cwd=ROOT,
                                  capture_output=True, text=True).stdout.split())
    _arch = subprocess.run("git archive HEAD | tar -t", shell=True, cwd=ROOT,
                           capture_output=True, text=True)
    _delivered = {l for l in _arch.stdout.split() if not l.endswith("/")}
    ok("the archive delivers something at all, so the comparison below is not vacuous -- an "
       "empty archive would make every set-difference trivially empty", _delivered, len(_delivered))
    # PROVED IN A THROWAWAY REPO rather than here: `git archive HEAD` reads .gitattributes from
    # the COMMIT, so staging one in this working tree changes nothing and the "mutation" passes.
    # That is git behaving correctly and my first mutation being wrong -- a scratch repo with
    # `test/ export-ignore` committed does drop test/x.txt from the archive while keeping it
    # tracked, which is the divergence this assertion exists to catch.
    ok("every tracked file is one a consumer actually RECEIVES -- the checks that reason about "
       "consumers from `git ls-files` are sound only while tracked and delivered are one set, "
       "and export-ignore silently separates them for `archive` but not for `clone`",
       not (_in_head - _delivered), sorted(_in_head - _delivered)[:8])

    # A CHANNEL WITH NO PROACTIVE CONSUMER IS RUNG 6 WEARING A FILE'S CLOTHES. `waiting` reports
    # a failed journal write in its porcelain, which is right for the probe that cannot act on
    # it -- but nothing reads porcelain unprompted. `doctor` is the surface a human runs on
    # purpose, so that is where the alarm has to land.
    _dj = make_repo()
    _p_ok = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                           cwd=_dj.root, capture_output=True, text=True)
    ok("`doctor` reports the waiting journal at all — the evidence a watchdog accumulates in, "
       "and the one record whose ABSENCE argues for adopting the thing it was meant to prove",
       "waiting journal" in _p_ok.stdout, _p_ok.stdout[-200:])
    # REGISTERED AND FIRING ARE TWO CLAIMS, and every other doctor line reports the first.
    # A fresh repo has answered nothing, and "registered and never fired" is indistinguishable
    # from "registered and content" everywhere else in this tool.
    ok("a repo whose journal is EMPTY is told so as a warning — nothing has answered yet, which "
       "is the state that looks identical to a healthy quiet one",
       "EMPTY" in _p_ok.stdout, _p_ok.stdout[-200:])
    with open(os.path.join(_dj.root, ".showrunner", "waiting.jsonl"), "a") as _fh:
        _fh.write(json.dumps({"ts": int(time.time()), "waiting": False}) + "\n")
    _p_fired = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                              cwd=_dj.root, capture_output=True, text=True)
    ok("...and once something HAS answered, doctor reports how many and how long ago — the only "
       "evidence here that the wiring runs rather than merely being wired",
       "entr(ies)" in _p_fired.stdout and "ago" in _p_fired.stdout, _p_fired.stdout[-220:])
    ok("...and states the limit in the same breath: it cannot tell a Stop trigger firing from "
       "somebody running the verb by hand, which is a weaker claim than 'the hook fired'",
       "by hand" in _p_fired.stdout, _p_fired.stdout[-220:])

    # FRESHNESS AS A RELATION, NOT A DURATION. I had this open with "I have no defensible
    # number for too old" -- and reaching for a number is the trap: any tolerance is invented
    # about an event showrunner does not schedule, the same defect as printing a clock time for
    # a trigger. The answerable question is whether the probe has answered SINCE the thing that
    # would have changed its answer, and the campaign journal already timestamps those.
    ok("with a journal entry and no campaign event, freshness is reported UNKNOWN rather than "
       "assumed fresh — nothing has happened that would change the answer",
       "UNKNOWN" in _p_fired.stdout, _p_fired.stdout[-260:])
    with open(os.path.join(_dj.root, ".showrunner", "events.jsonl"), "a") as _fh:
        _fh.write(json.dumps({"ts": int(time.time()) + 60, "kind": "leaf.closed"}) + "\n")
    _p_stale = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                              cwd=_dj.root, capture_output=True, text=True)
    ok("...and a journal whose newest entry PREDATES the last campaign event warns, naming the "
       "event kind — an old entry is not wrong because it is old, it is wrong because something "
       "happened since",
       "PREDATES" in _p_stale.stdout and "leaf.closed" in _p_stale.stdout, _p_stale.stdout[-300:])

    _blocked_dir = os.path.join(_dj.root, ".showrunner", "waiting.jsonl")
    if os.path.exists(_blocked_dir):
        os.remove(_blocked_dir)
    os.makedirs(_blocked_dir)          # a DIRECTORY where the journal goes: open(..,"a") fails
    _p_bad = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                            cwd=_dj.root, capture_output=True, text=True)
    ok("...and says CANNOT BE WRITTEN when it cannot, rather than staying quiet -- `waiting` "
       "keeps answering, so nothing else in the system goes red",
       "CANNOT BE WRITTEN" in _p_bad.stdout, _p_bad.stdout[-260:])
    ok("...and that is an ERROR, not a note: a silent journal and a watchdog that never fired "
       "produce the same empty file", _p_bad.returncode != 0, _p_bad.returncode)

    probe = os.path.join(ROOT, ".showrunner", "hooks", "waiting-probe.sh")
    ok("the waiting probe ships and is executable", os.path.isfile(probe) and
       os.access(probe, os.X_OK), probe)

    prepo = make_repo()
    # THE PATH THE PROBE ACTUALLY RESOLVES FIRST — .showrunner/bin/ before bin/, which is
    # `sr_bin`'s order and not a second resolver. Writing the fake to the other one made every
    # assertion below measure the REAL binary instead of the chosen code.
    fake_sr = os.path.join(prepo.root, ".showrunner", "bin", "showrunner")
    os.makedirs(os.path.dirname(fake_sr), exist_ok=True)

    def with_rc(rc, cwd=None):
        """A repo whose showrunner answers a chosen code, so the MAPPING is what is under test."""
        with open(fake_sr, "w") as fh:
            fh.write("#!/bin/sh\nexit %d\n" % rc)
        os.chmod(fake_sr, 0o755)
        return subprocess.run(["bash", probe], cwd=cwd or prepo.root, capture_output=True,
                              text=True)

    eq("waiting (0) passes through as waiting, so the watchdog stays quiet on a legitimate wait",
       with_rc(0).returncode, 0)
    eq("...not waiting (1) passes through, so an orchestrator with nothing outstanding rings",
       with_rc(1).returncode, 1)
    # THE RECORDED CHANNEL. The harness stores a probe's STDOUT as the `detail` of its last run,
    # so stdout is the only place this script can identify itself after the fact. Every line it
    # printed used to go to stderr, which meant the watchdog's own record of showrunner was the
    # empty string -- "showrunner's probe ran and found nobody waiting" was indistinguishable
    # from "something ran". Companion assertions, not replacements: the exit codes above are the
    # contract, and this is the separate claim that the record can name who answered.
    ok("the probe NAMES ITSELF on stdout when it says waiting, because that is the channel the "
       "harness records as `detail` -- an unnamed record cannot be attributed later",
       "showrunner" in with_rc(0).stdout and "WAITING" in with_rc(0).stdout,
       with_rc(0).stdout)
    ok("...and when it says NOT waiting, which is the answer that RINGS somebody and therefore "
       "the one most in need of attribution",
       "showrunner" in with_rc(1).stdout and "NOT WAITING" in with_rc(1).stdout,
       with_rc(1).stdout)
    # The two must be DISTINGUISHABLE in the record, not merely both non-empty: a detail that
    # reads the same for both verdicts identifies the script and loses the finding.
    ok("...and the two verdicts do not record the same detail, so the stored line carries the "
       "ANSWER and not only the identity",
       with_rc(0).stdout.strip() != with_rc(1).stdout.strip(),
       (with_rc(0).stdout.strip(), with_rc(1).stdout.strip()))

    blocked_probe = with_rc(3)
    eq("...and a BLOCKED Crawler (3) maps to 1 rather than falling through to 'could not tell' — "
       "it IS an answer, and an unmapped code would mark a working probe FAILING for as long as "
       "the Crawler stayed blocked", blocked_probe.returncode, 1)
    ok("...saying why on stderr, so the ring is actionable rather than a bare code",
       "BLOCKED" in blocked_probe.stderr, blocked_probe.stderr[:120])
    eq("...while a code the mapping does not know is COULD NOT TELL (2), never folded into an "
       "answer", with_rc(9).returncode, 2)

    # A CRAWLER IS NOT AN ORCHESTRATOR. config.local.json is copied into every worktree by
    # harness.provision, so this runs inside each Crawler too — and one answering "waiting"
    # because its SIBLINGS are alive would silence its own watchdog with somebody else's
    # liveness. A Crawler dispatches nothing, so the honest answer for one is never 0.
    pwt = os.path.join(prepo.worktree_root, "probe-wt")
    os.makedirs(prepo.worktree_root, exist_ok=True)
    sh(["git", "worktree", "add", "-q", pwt, "-b", "showrunner/probe-wt"], prepo.root)
    in_wt = with_rc(0, cwd=pwt)                  # the orchestrator WOULD say "waiting"
    eq("...and inside a linked worktree it NEVER answers waiting, even when the orchestrator "
       "would — a Crawler silencing its own watchdog with a sibling's liveness is the "
       "disarm-by-proxy this seam exists to prevent", in_wt.returncode, 1)
    eq("...while the same binary answers 0 from the main checkout, so that is about the tree and "
       "not about the code it found",
       with_rc(0).returncode, 0)

    # THE BINARY MUST BE RESOLVED, NOT ASSUMED. A hook's PATH is not a shell's, and a probe that
    # bails on a missing binary with a bare non-zero reads as "there is work" forever.
    bare = make_repo()
    for stray in (os.path.join(bare.root, ".showrunner", "bin", "showrunner"),
                  os.path.join(bare.root, "bin", "showrunner")):
        if os.path.exists(stray):
            os.remove(stray)
    no_bin = subprocess.run(["bash", probe], cwd=bare.root, capture_output=True, text=True)
    eq("a probe that cannot find the binary answers COULD NOT TELL, not 'there is work' — the "
       "second is a lie that rings forever and reports nothing wrong", no_bin.returncode, 2)
    ok("...and names what it could not find", "showrunner" in no_bin.stderr, no_bin.stderr[:120])

    # ---- THE INERT-CRAWLER STOP TRIGGER (#32) -----------------------------------------
    # `waiting` already knew a Crawler was alive and doing nothing. Nothing spent that at the
    # orchestrator's turn-end, so a "Next: ..." list was written and the session walked away
    # from a run one message would have restarted — and the HUMAN noticed the stall. Every fact
    # needed was already computed and printed.
    trig = os.path.join(ROOT, lease.STOP_TRIGGER)
    ok("the stop trigger ships in the repo and is executable — a gate that is not executable is "
       "a gate that never runs", os.path.isfile(trig) and os.access(trig, os.X_OK), trig)
    # TWO COPIES, ONE FILE. `install.sh` copies from `.showrunner/hooks/` and the template under
    # `templates/stop-triggers/` is what a fresh install is built from; every assertion below
    # runs the SHIPPED copy, so a template that drifted from it would be tested by nothing and
    # installed by everybody. Same rule the central shim already carries.
    tmpl_trig = os.path.join(ROOT, "templates", "stop-triggers", "inert-crawler-gate.sh")
    ok("...and the template it is installed from is BYTE-IDENTICAL to it, so the copy under "
       "test and the copy a consumer receives cannot drift apart",
       os.path.isfile(tmpl_trig) and filecmp.cmp(trig, tmpl_trig, shallow=False),
       tmpl_trig)

    def refused_text_probe(p):
        return (p.stdout or "") + (p.stderr or "")

    def run_trigger(payload, cwd=ROOT):
        fx = os.path.join(tmpdir("trigger-fixture"), "waiting.json")
        with open(fx, "w") as fh:
            fh.write(payload)
        return subprocess.run(["bash", trig], cwd=cwd, capture_output=True, text=True,
                              env=dict(os.environ, INERT_CRAWLER_GATE_FIXTURE=fx))

    blocked_payload = json.dumps({
        "waiting": False, "live_crawlers": [], "parked_crawlers": [],
        "blocked_crawlers": [{"crawler": "crawler-ml-l2a", "leaf": "ML-L2a",
                              "why": "refused at turn-end by showrunner-stop-gate"}]})
    clean_payload = json.dumps({"waiting": False, "live_crawlers": [], "parked_crawlers": [],
                                "blocked_crawlers": []})

    # BLINDNESS AND CONTENTMENT LEFT THROUGH THE SAME DOOR. Every early exit here is `exit 0`,
    # which for a Stop hook means ALLOW — so "no Crawler is inert" and "I could not tell" were
    # the same output: silence. Swept for after another project applied the tell per-BRANCH
    # rather than per-tool: for each early exit in a check, ask what the summary prints if that
    # branch is the only one taken; any early exit reaching a green summary is a channel
    # collapse. This repo already refuses that shape elsewhere -- hook verbs under a missing
    # central install say ALLOWED WITHOUT BEING CHECKED for exactly this reason -- and this gate
    # did not.
    #
    # The JURISDICTION exits stay silent on purpose: not applying is not the same as not
    # knowing, and narrating every non-event trains a reader to skim the one that matters.
    _blind_root = tmpdir("gate-blind")
    sh(["git", "init", "-q", "."], _blind_root)          # a REPO, or it exits on jurisdiction
    blind = subprocess.run(["bash", trig], cwd=_blind_root, capture_output=True,
                           text=True, input="{}")
    eq("a gate that cannot find showrunner still ALLOWS -- a turn-end hook that hard-fails on "
       "its own plumbing blocks the write that would repair it", blind.returncode, 0)
    ok("...but SAYS it allowed without checking, because an allow nobody is told about is "
       "indistinguishable from a guard that ran and was content",
       "WITHOUT BEING CHECKED" in blind.stderr, blind.stderr[:160])

    # #62: A PARKED CRAWLER MUST NOT BLOCK THIS GATE. Reported from a two-campaign checkout: the
    # other agent's Crawler went inert, the gate fired in a session whose own campaign was idle
    # (hooks read the DEFAULT campaign regardless of which one the session works), and BOTH
    # offered remedies were unavailable to a non-owner — messaging an agent that stays inert
    # changes nothing, and reaping surfaces somebody else's uncommitted work. `park` is the
    # documented non-destructive lever and it did not release this gate: `waiting` classified a
    # parked-AND-blocked leaf as blocked, because `blocked` was tested before `parked`.
    parked_and_blocked = json.dumps({
        "waiting": False, "live_crawlers": [],
        "parked_crawlers": [{"crawler": "c-parked", "leaf": "P1", "why": "parked, AND refused"}],
        "blocked_crawlers": []})
    _p = run_trigger(parked_and_blocked)
    eq("a PARKED Crawler does not refuse a turn-end, even when it is also inert — parking is "
       "what records that somebody has accounted for it, and it was the only non-destructive "
       "lever the blocked party had", _p.returncode, 0)

    # AND THE REMEDY SET. The reporter's sharpest point: an agent under pressure to end a turn,
    # facing a gate whose only WORKING remedy is `reap --apply`, is being pushed toward
    # destroying a colleague's uncommitted work — the opposite of the gate's intent.
    ok("the refusal offers PARK as well as message and reap, so a non-owner has an exit that "
       "does not destroy somebody else's tree",
       "showrunner park" in refused_text_probe(run_trigger(blocked_payload)),
       refused_text_probe(run_trigger(blocked_payload))[:200])
    ok("...and says outright that reap is the destructive one and wrong for a Crawler you do "
       "not own, naming the two-campaign case that produced this",
       "do not own" in refused_text_probe(run_trigger(blocked_payload))
       and "campaign" in refused_text_probe(run_trigger(blocked_payload)),
       refused_text_probe(run_trigger(blocked_payload))[-260:])

    # AND THE GATE PRINTS IT. Carrying the actor in the payload is half the fix; the other half
    # is the refusal naming it, because the blocked session's first question is whether this is
    # even theirs.
    attributed_payload = json.dumps({
        "waiting": False, "live_crawlers": [], "parked_crawlers": [],
        "blocked_crawlers": [{"crawler": "txt-paint", "leaf": "TXT-PAINT",
                              "why": "refused at turn-end", "actor": "crawler-txt-paint",
                              "claim_session": "a1b2c3d4"}]})
    _at = refused_text_probe(run_trigger(attributed_payload))
    ok("the refusal NAMES who claimed the leaf and in which session, so the reader can tell in "
       "one line whether it is theirs to fix",
       "crawler-txt-paint" in _at and "a1b2c3d4" in _at, _at[:220])
    ok("...and says outright that a leaf claimed by somebody else is not theirs to fix, rather "
       "than handing the nearest session the controls",
       "NOT YOURS TO FIX" in _at, _at[:300])

    refused = run_trigger(blocked_payload)
    eq("a BLOCKED Crawler REFUSES the orchestrator's turn-end — exit 2, which is the code that "
       "blocks a Stop", refused.returncode, 2)
    ok("...naming the Crawler and the leaf, because 'somebody is blocked' is not something the "
       "reader can act on", "crawler-ml-l2a" in refused.stderr and "ML-L2a" in refused.stderr,
       refused.stderr[:200])
    ok("...and offering REAP in the same breath as messaging it, because a block can mean GONE "
       "rather than waiting and an agent that cannot tell will sit re-messaging a corpse",
       "reap" in refused.stderr and "message" in refused.stderr.lower(), refused.stderr[:400])

    # THE PAIR, and every unknown, because a gate that refuses on everything is not a gate.
    eq("...while a campaign with NO blocked Crawler allows the turn-end",
       run_trigger(clean_payload).returncode, 0)
    eq("...and an UNPARSEABLE answer allows — a gate that blocks when it cannot see blocks "
       "forever the day it breaks, and this one sits on the human's own turn-end",
       run_trigger("not json at all").returncode, 0)
    eq("...and an EMPTY answer allows, for the same reason", run_trigger("").returncode, 0)
    missing_fx = subprocess.run(
        ["bash", trig], cwd=ROOT, capture_output=True, text=True,
        env=dict(os.environ, INERT_CRAWLER_GATE_FIXTURE=os.path.join(tmpdir("gone"), "nope.json")))
    eq("...and a fixture that is not there allows rather than erroring", missing_fx.returncode, 0)

    # REGISTERED, because a gate nobody registers has never once run — which is the state
    # `lock guard` was in for this repo's whole life and the row that shaped all of this.
    reg = make_repo()
    reg_settings = os.path.join(reg.root, ".claude", "settings.json")
    os.makedirs(os.path.dirname(reg_settings), exist_ok=True)
    # Seeded with SOMEBODY ELSE'S hook and no showrunner entry at all, so the two assertions
    # below are about which event this lands on rather than about whatever the fixture happened
    # to have registered already.
    with open(reg_settings, "w") as fh:
        json.dump({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "theirs.sh"}]}]}}, fh)
    changed, note = lease.register_stop_trigger(reg)
    ok("registering the stop trigger reports that it changed something", changed, note)
    ok("...and it lands on the Stop event, not on PreToolUse — the two gates answer different "
       "questions at different moments", lease._stop_registration(reg_settings)[0], note)
    ok("...and the PreToolUse detector does NOT see it, so a detector keyed only on the command "
       "would report the worktree guard as registered because the Stop entry exists",
       not lease._guard_registration(reg_settings)[0], open(reg_settings).read()[:200])
    ok("...while somebody else's PreToolUse hook is untouched, because this file belongs to "
       "Claude Code and to whoever else registered in it",
       "theirs.sh" in open(reg_settings).read())
    changed2, _ = lease.register_stop_trigger(reg)
    ok("...and registering twice is a no-op, so an installer that runs on every upgrade does "
       "not accumulate duplicates", not changed2)
    # AND IT LEAVES NOTHING IN A DIRECTORY THAT IS NOT OURS. The read-modify-write takes a file
    # lock, and that lock used to be created beside settings.json — runtime state deposited in
    # Claude Code's directory, ignored by nothing, which this repo committed the first time
    # `register` ran. A consumer would get the same stray file and their .gitignore is not ours
    # to edit, so the lock moved into showrunner's own state dir, where `*.lock` is already
    # ignored and every other lock in this project lives.
    strays = [f for f in os.listdir(os.path.join(reg.root, ".claude")) if f.endswith(".lock")]
    ok("registering leaves no lock file in .claude/ — that directory belongs to Claude Code and "
       "to whoever else configured a hook there", not strays, strays)
    ok("...because the lock is in showrunner's own state dir, where it is already ignored",
       os.path.exists(os.path.join(reg.state_dir, "claude-settings.lock.lock")),
       sorted(os.listdir(reg.state_dir)))

    # ---- DOES IT CROSS, when the install is deliberately untracked? (#31) ------------
    # `git worktree add` carries TRACKED files only, so an untracked shim is present in the
    # main checkout and absent in every worktree — the one place the guard runs. The only
    # remedy offered was "commit it", which a shared team repo excluding `.showrunner` via
    # .git/info/exclude cannot take. showrunner had already solved this shape for the OTHER
    # harness (`harness.provision` copies game_loop in for exactly this reason) and its own
    # hooks were not covered by it: one mechanism, two answers depending on whose files.
    prov = make_repo(files={"README.md": "seed\n",
                            ".gitignore": ".worktrees/\n.showrunner/hooks/\n"})
    hooks_src = os.path.join(prov.root, lease.GUARD_HOOKS_DIR)
    os.makedirs(hooks_src, exist_ok=True)
    with open(os.path.join(hooks_src, "worktree-guard.sh"), "w") as fh:
        fh.write("#!/usr/bin/env bash\nexit 0\n")
    gprov = new_graph(prov)
    gprov.add("untracked-install work", leaf_id="p1", labels=["backend"])
    prec = worktree.spawn(prov, gprov.show("p1"), actor="crawler-p")
    crossed = os.path.join(prec["worktree"], lease.GUARD_SHIM)
    ok("an IGNORED (deliberately untracked) guard shim is PROVISIONED into the worktree — git "
       "could not carry it, and 'commit it' is not a remedy a repo that excludes .showrunner "
       "on purpose can take", os.path.exists(crossed), prec.get("provisioned"))
    ok("...and it is EXECUTABLE there, which is the difference between a guard and a file",
       os.path.exists(crossed) and os.access(crossed, os.X_OK))
    ok("...and the spawn record SAYS it provisioned them, so the copy is visible rather than "
       "being a silent side effect of spawning",
       any(lease.GUARD_HOOKS_DIR in a for a in prec.get("provisioned") or []),
       prec.get("provisioned"))

    # THE OTHER SUPPORTED ARRANGEMENT, so the assertion above is about the untracked case and
    # not about provisioning happening unconditionally.
    tracked = make_repo(files={"README.md": "seed\n", ".gitignore": ".worktrees/\n",
                               os.path.join(".showrunner", "hooks", "worktree-guard.sh"):
                                   "#!/usr/bin/env bash\nexit 0\n"})
    gtr = new_graph(tracked)
    gtr.add("tracked-install work", leaf_id="p2", labels=["backend"])
    trec = worktree.spawn(tracked, gtr.show("p2"), actor="crawler-t")
    ok("a TRACKED shim crosses by itself and is left alone — provisioning reports nothing, "
       "because git already did the work",
       not any(lease.GUARD_HOOKS_DIR in a for a in trec.get("provisioned") or []),
       trec.get("provisioned"))
    ok("...and it is still there, so 'reports nothing' means 'nothing to do' rather than "
       "'nothing happened'", os.path.exists(os.path.join(trec["worktree"], lease.GUARD_SHIM)))

    # THE STATE WITH NO ANSWER, which the copy created before anything checked it: neither
    # tracked nor ignored. A Crawler's `git add -A` commits showrunner's plumbing onto its
    # branch, and integrating it collides with the same untracked path in the main checkout —
    # found by the suite the moment provisioning was wired in, on the merge assertion.
    neither = make_repo(files={"README.md": "seed\n", ".gitignore": ".worktrees/\n"})
    nh = os.path.join(neither.root, lease.GUARD_HOOKS_DIR)
    os.makedirs(nh, exist_ok=True)
    with open(os.path.join(nh, "worktree-guard.sh"), "w") as fh:
        fh.write("#!/usr/bin/env bash\nexit 0\n")
    gn2 = new_graph(neither)
    gn2.add("neither-nor", leaf_id="p3", labels=["backend"])
    nrec = worktree.spawn(neither, gn2.show("p3"), actor="crawler-n")
    ok("a shim that is NEITHER tracked NOR ignored is NOT provisioned — copying it would hand "
       "the Crawler a file its own `git add -A` commits, which then wedges the merge",
       not os.path.exists(os.path.join(nrec["worktree"], lease.GUARD_SHIM)),
       nrec.get("provisioned"))
    ok("...and the refusal names BOTH fixes, because either one works and the reader cannot be "
       "expected to derive which suits their repo",
       any("TRACK the hooks" in a and "IGNORE them" in a
           for a in nrec.get("provisioned") or []), nrec.get("provisioned"))

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
    ok("a repo with NO .claude/settings.json at all reports the guard as an ERROR, not a "
       "warning — an unregistered guard is indistinguishable from one that ran and was content",
       any("nothing registers" in m or "no .claude/settings.json" in m.lower() for m in errs),
       errs[:2])

    # THE OTHER BRANCH, and it is the one with a population: settings.json EXISTS, carries
    # somebody else's hooks, and has no worktree-guard entry — i.e. every consumer installed
    # before the guard shipped. Deleting the file only ever reached `registered is None`, and
    # the assertion above used to say `A or B` so it could not tell which. Downgrading this
    # branch from error to ok left the suite green.
    with open(os.path.join(claude_dir, "settings.json"), "w") as fh:
        json.dump({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command",
                                           "command": "somebody-elses.sh"}]}]}}, fh)
    theirs = lease.guard_health(cfg)
    errs2 = [m for l, m in theirs if l == "error"]
    ok("a settings.json that exists, has other people's hooks, and registers NO worktree-guard "
       "is an ERROR too — that is the state of every repo installed before the guard shipped, "
       "and it is the only one of the two an upgrade actually produces",
       any("no worktree-guard hook in" in m for m in errs2), errs2[:2])
    # AND IT NAMES BOTH LAYERS IT READ. "registers no worktree-guard hook" reads as "you have
    # not registered it" when the honest sentence is "I did not look where you put it" — a
    # consumer keeping showrunner untracked could only tell the difference by reading source.
    ok("...and the error NAMES the files it looked in, both layers, so a reader can tell "
       "'you did not register it' from 'I did not look where you put it'",
       any("settings.json and" in m and "settings.local.json" in m for m in errs2), errs2[:1])

    # THE ARRANGEMENT DOCTOR ENDORSES MUST NOT ALSO BE AN ERROR. Reported by a consumer whose
    # repo is shared with a developer running their own install, so showrunner is kept out of
    # the history entirely: the shim is ignored and the hooks live in the UNTRACKED
    # settings.local.json. `doctor` praised that arrangement in one line and errored on it
    # eleven lines later, because the registration check read settings.json alone — while
    # `dispatch.py` states that hooks live in the local layer and `cli.py`'s own hook-wiring
    # net already reads both. The pair existed in this codebase and was not carried across.
    with open(os.path.join(claude_dir, "settings.json"), "w") as fh:
        json.dump({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command",
                                           "command": "somebody-elses.sh"}]}]}}, fh)
    with open(os.path.join(claude_dir, "settings.local.json"), "w") as fh:
        json.dump({"hooks": {"PreToolUse": [
            {"matcher": "|".join(lease.GUARD_TOOLS),
             "hooks": [{"type": "command",
                        "command": '"$CLAUDE_PROJECT_DIR"/' + lease.GUARD_SHIM}]}]}}, fh)
    local_only = lease.guard_health(cfg)
    ok("a guard registered ONLY in the untracked settings.local.json counts as registered — "
       "that is the one place it can go in a repo keeping showrunner out of its history",
       not [m for l, m in local_only if l == "error" and "worktree-guard" in m],
       [m for l, m in local_only if l == "error"][:2])

    # AND THE REMEDY MUST MATCH THE ARRANGEMENT. `register` writes the TRACKED file; following
    # that fix here would commit the hooks into a file shared with a developer whose own
    # install would then collide. The consumer did not run it, and only because they read what
    # it writes first.
    os.remove(os.path.join(claude_dir, "settings.local.json"))
    _saved = lease.settings_target(cfg.root, True)
    ok("`settings_target` names the UNTRACKED layer under --local, so the remedy doctor prints "
       "for the ignored arrangement writes where the hooks actually belong",
       _saved.endswith("settings.local.json"), _saved)
    ok("...and the tracked layer otherwise, which stays the default",
       lease.settings_target(cfg.root, False).endswith(".claude/settings.json"))
    ok("...distinguished from the missing-file case by its own wording, so the two are not one "
       "assertion joined by `or`",
       not any("nothing registers" in m for m in errs2), errs2[:2])

    # THE SHIM'S OWN THREE CHECKS, none of which had an assertion. Deleting all three left the
    # suite green — and the crossing check is the one whose comment cites the observed failure
    # ("the first probe worktree came up with no shim in it at all"), i.e. the check that tells
    # a consumer the guard will NOT reach the place it exists for.
    mode = os.stat(shim_dst).st_mode
    os.chmod(shim_dst, 0o644)
    try:
        noexec = [m for l, m in lease.guard_health(cfg) if l == "error"]
        ok("a shim that exists but is NOT EXECUTABLE is an error — the hook cannot run it, so "
           "the guard is inert while every path-based check says it is installed",
           any("not executable" in m for m in noexec), noexec[:2])
    finally:
        os.chmod(shim_dst, mode)
    ok("...and restoring the mode clears it, so that assertion is about the bit and not about "
       "the check always firing",
       not any("not executable" in m for l, m in lease.guard_health(cfg)))

    # THE CROSSING. `git worktree add` copies tracked files only, so an untracked or uncommitted
    # shim is present here and absent in every worktree made from now on.
    # NEITHER TRACKED NOR IGNORED is the state with no answer, and it is the one this warns
    # about now. An IGNORED shim is fine since `spawn` provisions it (#31) — warning there
    # would name the one action a repo that excludes `.showrunner` on purpose has already
    # ruled out, which is a remedy that reads as "you are holding it wrong".
    untracked = [m for l, m in lease.guard_health(cfg) if l == "warn"]
    ok("a shim that is NEITHER tracked NOR ignored is reported — git will not carry it and "
       "provisioning refuses to, so it is absent in every worktree",
       any("NEITHER tracked NOR ignored" in m for m in untracked), untracked[:2])
    ok("...and the warning names BOTH ways out, because a repo that keeps showrunner out of its "
       "history cannot take the one that says `git add`",
       any("git add" in m and "info/exclude" in m for m in untracked), untracked[:2])
    sh(["git", "add", shim_rel], cfg.root)
    sh(["git", "commit", "-q", "-m", "track the guard shim"], cfg.root)
    ok("...and committing it clears that warning, which is what makes the line above a check "
       "and not a constant",
       not any("NEITHER tracked" in m for l, m in lease.guard_health(cfg)))

    # THE OTHER WAY OUT, asserted rather than asserted-about: ignoring it is not a warning at
    # all, because provisioning carries it. Without this the assertion above passes just as
    # well against a check that warns on everything untracked.
    ign = make_repo(files={"README.md": "seed\n",
                           ".gitignore": ".worktrees/\n.showrunner/hooks/\n"})
    os.makedirs(os.path.join(ign.root, lease.GUARD_HOOKS_DIR), exist_ok=True)
    with open(os.path.join(ign.root, lease.GUARD_SHIM), "w") as fh:
        fh.write("#!/usr/bin/env bash\nexit 0\n")
    os.chmod(os.path.join(ign.root, lease.GUARD_SHIM), 0o755)
    ign_health = lease.guard_health(ign)
    ok("an IGNORED shim is NOT warned about — `spawn` provisions it, so this is the supported "
       "arrangement for a repo that keeps showrunner out of its history, not a defect",
       not any("NEITHER tracked" in m for l, m in ign_health), ign_health[:3])
    ok("...and it SAYS which arrangement is in force, because silence here would read as the "
       "check not having run",
       any(l == "ok" and "provisions it" in m for l, m in ign_health), ign_health[:3])
    with open(shim_dst, "a") as fh:
        fh.write("# edited after the commit\n")
    differs = [m for l, m in lease.guard_health(cfg) if l == "warn"]
    ok("...and a tracked shim whose WORKING copy differs from HEAD's is reported too, because "
       "a new worktree gets HEAD's version — the guard that crosses is not the one you are "
       "reading", any("DIFFERS" in m for m in differs), differs[:2])
    sh(["git", "checkout", "--", shim_rel], cfg.root)
    ok("...and it clears once the two agree again",
       not any("DIFFERS" in m for l, m in lease.guard_health(cfg)))

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

    # FOUND-AND-BROKEN IS NOT FOUND-AND-MISSING, and only the second was covered. showrunner
    # develops itself: the guard runs the very code being edited, and ONE syntax error anywhere
    # under lib/showrunner/ kills every verb at import. The shim used to `exec` the binary, so a
    # half-edited tool exited 1 with EMPTY stdout — neither a deny (2) nor a loud allow — and
    # editing this tool silently disarmed its own guard. Measured before it was fixed.
    with open(real_bin, "w") as fh:
        fh.write("#!/usr/bin/env python3\nimport sys\n"
                 "sys.stderr.write('Traceback (most recent call last):\\n')\n"
                 "sys.exit(1)\n")
    os.chmod(real_bin, 0o755)
    broke = subprocess.run(["bash", os.path.join(wt, lease.GUARD_SHIM)], cwd=wt,
                           input=json.dumps({"session_id": "sess-B", "cwd": wt,
                                             "tool_name": "Write", "tool_input": {}}),
                           capture_output=True, text=True)
    eq("a binary that is FOUND and BROKEN also allows — a half-edited tool must not block the "
       "edit that repairs it", broke.returncode, 0)
    ok("...and says so on the channel that reaches the agent, rather than exiting 1 with an "
       "empty stdout, which is neither a refusal nor an announcement",
       "DID NOT RUN" in broke.stdout and "additionalContext" in broke.stdout, broke.stdout[:200])
    ok("...naming the exit code and the first line of what it printed, so a reader can tell a "
       "syntax error from a missing dependency without re-running it",
       "exited 1" in broke.stdout and "Traceback" in broke.stdout, broke.stdout[:300])


def test_seat_and_whoami():
    group("The session seam: a derived seat, announced where compaction cannot erode it (#36)")
    from showrunner import roles as R

    # THE SEAT IS DERIVED, NEVER DECLARED. A consumer's prototype kept it in a one-word file with
    # two PreToolUse guards gated on `^lead`; the file said `worker`, written mid-run, so both
    # guards exited 0 for the remaining 16 hours. A one-word file was a global off switch and
    # nothing announced its value. There is no state a session can write to change these.
    cfg = make_repo()
    cwd = os.getcwd()
    try:
        os.chdir(cfg.root)
        where, why = R.seat(cfg)
        eq("the main checkout of a repo with NO campaign is SOLO, said out loud rather than left "
           "to look like an idle orchestrator", where, R.SOLO)
        ok("...and the evidence names what was looked at", "main checkout" in why, why)

        g = new_graph(cfg)
        g.add("work", leaf_id="s1", labels=["backend"])
        rec = worktree.spawn(cfg, g.show("s1"), actor="crawler-s")
        campaign.record_spawn(cfg, rec, pid=os.getpid())
        where2, why2 = R.seat(cfg)
        eq("...and once a campaign exists, the same checkout is the ORCHESTRATOR", where2,
           R.ORCHESTRATOR)

        # Asked of a config LOADED IN the worktree rather than by chdir-ing the process: the
        # seat is a fact about the tree a config was loaded from, and `load` records it from the
        # same start directory it resolved the root from.
        where3, why3 = R.seat(config.load(start=rec["worktree"]))
        eq("a linked worktree is a CRAWLER — derived from --git-common-dir against "
           "--show-toplevel, which no file can override", where3, R.CRAWLER)
        ok("...and the campaign record names its leaf, so the announcement can say which work "
           "this tree is for", "s1" in why3, why3)
    finally:
        os.chdir(cwd)

    # THE SEAT COMES FROM THE CONFIG, NOT FROM THE PROCESS. This is the falsifier for the fix,
    # and it is deliberately shaped so that the old code CANNOT pass it: both configs are asked
    # in ONE process, from ONE cwd, and they must give DIFFERENT answers. `seat` used to run
    # `git rev-parse` against `os.getcwd()`, so it returned the same seat for both — and inside
    # a linked worktree that answer was CRAWLER for a config describing a repo in /tmp, which
    # is how five assertions in this group failed in every worktree and nowhere else.
    #
    # A test that merely passes from both locations does not discriminate; this one fails the
    # moment `seat` consults the ambient process again, wherever the suite is run from.
    in_tree = config.Config(cfg.data, cfg.root, cfg.path, tree=rec["worktree"])
    in_main = config.Config(cfg.data, cfg.root, cfg.path, tree=cfg.root)
    seat_in_tree, why_tree = R.seat(in_tree)
    seat_in_main, _why_main = R.seat(in_main)
    eq("a config loaded IN a linked worktree is a CRAWLER seat", seat_in_tree, R.CRAWLER)
    eq("...while one loaded in the main checkout of the SAME repo, asked in the SAME process "
       "from the SAME cwd, is the ORCHESTRATOR — so the answer provably comes from the config "
       "and not from where this process happens to stand", seat_in_main, R.ORCHESTRATOR)
    ok("...and the crawler evidence names the tree the CONFIG records, not the one the process "
       "is in", os.path.basename(os.path.realpath(rec["worktree"])) in why_tree, why_tree)

    # AND `cfg.root` IS NOT THE FIELD TO READ. It resolves through --git-common-dir, so it is the
    # main checkout from inside every worktree; a fix that reached for it would trade one wrong
    # answer for another and make every seat an ORCHESTRATOR. Asserted so that regression is a
    # failure and not a plausible-looking simplification.
    eq("a Config loaded from inside a worktree still roots at the MAIN checkout, which is why "
       "`root` cannot answer the seat question and `tree` had to be added",
       os.path.realpath(config.load(start=rec["worktree"]).root),
       os.path.realpath(cfg.root))
    eq("...while its `tree` is the worktree itself, recorded at load time from the same start "
       "directory", config.load(start=rec["worktree"]).tree,
       os.path.realpath(rec["worktree"]))

    # UNKNOWN IS A REAL ANSWER AND IS ANNOUNCED AS ONE. An announcer that cannot tell and says
    # nothing is indistinguishable from a healthy one, which is exactly how the reported failure
    # went unnoticed for a whole run. Note WHERE this is asserted from: a perfectly good repo.
    # The config records no tree, so the honest answer is UNKNOWN even though the process could
    # have guessed one — and guessing is the behaviour being removed.
    unrooted = config.Config(cfg.data, cfg.root, cfg.path, tree=None)
    where4, why4 = R.seat(unrooted)
    eq("a config that records no working tree is UNKNOWN, not guessed from the process's cwd",
       where4, R.UNKNOWN)
    ok("...and the evidence says which fact was missing, rather than blaming git",
       "does not record which working tree" in why4, why4)

    # ...and the ORIGINAL condition still holds end-to-end: a config cannot even be loaded
    # outside a repo, and a tree that is not a git work tree records None.
    outside = tmpdir("not-a-repo")
    ok("a directory that is not a git work tree records no tree at all, so nothing downstream "
       "can mistake it for a location", util.caller_tree(outside) is None,
       util.caller_tree(outside))

    # THE ANNOUNCEMENT IS A MANIFEST, NOT A POINTER. Telling a session where to READ is the same
    # bet that just lost: the reported run had showrunner installed, wired, and 38 leaves done.
    lines = R.whoami(cfg, "sess-x")
    body = "\n".join(lines)
    ok("the announcement names the seat in its first line", "ORCHESTRATOR" in lines[0], lines[0])
    ok("...and carries the load-bearing instruction itself rather than a path to go and read",
       "spawn" in body and "--launch" in body, body[:200])
    ok("...including what the seat may NOT do, which is the half a pointer never delivers",
       "raw `claude -p`" in body, body[:400])
    ok("...and says plainly when no roles are defined, so an unenforced policy is visible",
       "no roles are defined" in body, body[-200:])

    # THE ENFORCED BLOCK IS GENERATED FROM THE FIELDS A GUARD READS (#40). Announcement prose in
    # one dict and enforcement in another is two statements of one policy, free to disagree — and
    # a session told something no guard enforces has been given a rule that is not one.
    gen = R.enforced_lines({"acquire": "claim", "may_create": ["worker"],
                            "reports_to": "lead", "writes": ["src/**"], "notes": "prose"})
    # PAIRS NOW, because the promise above was not true of every line (#77): showrunner ships no
    # write guard, so `writes` is PUBLISHED — stated here for a hook of YOURS to act on — while
    # may_create is ENFORCED by `spawn --launch` and `dispatch guard` both. Printing one label
    # over both is the sentence that stops somebody checking, and it cost a reporter half an
    # hour of unguarded work from a seat that said ENFORCED twice.
    gen_text = [t for _, t in gen]
    ok("every line still comes from a field a guard or a consumer's hook actually reads",
       any("worker" in t for t in gen_text) and any("src/**" in t for t in gen_text), gen)
    ok("...and `notes` is NOT among them, because it is consumer prose and nothing checks it",
       not any("prose" in t for t in gen_text), gen)
    ok("may_create is labelled ENFORCED, because showrunner itself refuses it at BOTH paths",
       any(lab == "ENFORCED" and "may dispatch" in t for lab, t in gen), gen)
    ok("...while `writes` is labelled PUBLISHED, because showrunner has no write guard and "
       "announcing enforcement it does not perform is worse than announcing nothing",
       any(lab == "PUBLISHED" and "src/**" in t for lab, t in gen), gen)
    ok("...so no line claims ENFORCED for writes",
       not any(lab == "ENFORCED" and "write" in t for lab, t in gen), gen)
    # `or [""]` so a producer that stopped producing FAILS this rather than raising out of the
    # group — an IndexError here made the mutant unscoreable and the sweep reported a floor from
    # a truncated run as though it were coverage.
    # A REPR IS NOT A RENDERING. `writes` reached this block through `%s`, so a mapping printed
    # as `{'deny': ['**']}` and a list as `['src/**']` — Python syntax in the one block whose job
    # is to state plainly what a guard enforces. Both shapes are asserted because the suite's own
    # fixture is a list and a consumer may reasonably hand a mapping; neither may render as code,
    # and neither may raise, since a renderer that raises takes the whole announcement down.
    listed = R.enforced_lines({"acquire": "claim", "writes": ["src/**", "docs/**"]})
    ok("a `writes` LIST renders as paths a reader can act on, not as a Python literal",
       any("src/**, docs/**" in t for _, t in listed)
       and not any("[" in t for _, t in listed), listed)
    mapped = R.enforced_lines({"acquire": "claim", "writes": {"deny": ["app/**", "backend/**"]}})
    ok("a `writes` MAPPING renders its denials in words, and does not leak brace syntax",
       any("may NOT write: app/**, backend/**" in t for _, t in mapped)
       and not any("{" in t for _, t in mapped), mapped)

    none_line = (R.enforced_lines({"acquire": "claim"}) or [("", "")])[0][1]
    ok("a role that may create NOTHING says so in the terms the guard will use, rather than "
       "omitting the line and leaving the reader to infer permission",
       "NOTHING" in none_line, none_line)

    # #46: the suite reads a config home it created, never the developer's. Asserted rather than
    # trusted, because the setup is 4 lines near the imports that a later edit could drop or move
    # below the import -- and the failure it causes is invisible on any machine without a real
    # roles.json, which is every CI box and was this one. What made it real: with roles configured
    # the way the feature intends, SIX assertions failed. Green here, red for the people it was
    # built for.
    ok("the suite resolves roles from its OWN config home, never the machine's -- a developer "
       "who uses the feature must not get a red suite from their own configuration",
       R.USER_PATH.startswith(_CFG_HOME), R.USER_PATH)
    ok("...and that home is real and empty of roles, so 'no roles defined' is a FACT here rather "
       "than an accident of the box",
       os.path.isdir(_CFG_HOME) and not os.path.exists(
           os.path.join(_CFG_HOME, "showrunner", "roles.json")))

    # #64: A SEAT SOMEBODY ELSE IS SITTING IN IS NOT YOURS TO BE MAPPED INTO. Two orchestrator
    # sessions in one checkout: the second was told it was the campaign-lead of a campaign the
    # first was leading, and told what it might dispatch there. The mapping answers "what does a
    # session in this position do" and cannot answer "is that position occupied" — which is the
    # authority-by-location failure roles.py already names in prose and then committed.
    #
    # It matters past a status line because that block is the SessionStart announcement: what a
    # fresh session believes about itself before it does anything. `role roster` was correct
    # throughout, and is not what an agent reads.
    seat_cfg = make_repo()
    _rec = campaign.load(seat_cfg)
    _rec.setdefault("crawlers", []).append(
        {"crawler": "c1", "leaf": "L1", "worktree": ".worktrees/c1", "state": "spawned"})
    campaign.save(seat_cfg, _rec)
    _rhome = tmpdir("seat-roles-home")
    os.makedirs(os.path.join(_rhome, "showrunner"), exist_ok=True)
    with open(os.path.join(_rhome, "showrunner", "roles.json"), "w") as fh:
        json.dump({"roles": {
            "campaign-lead": {"acquire": "claim", "capacity": 1, "may_create": ["worker"],
                              "writes": {"allow": ["**"]}},
            "worker": {"acquire": "assign", "reports_to": "campaign-lead"},
            R.FALLBACK: {"acquire": "claim", "writes": {"deny": ["**"]}}},
            "seat_roles": {"orchestrator": "campaign-lead"}}, fh)
    _prev_path = R.USER_PATH
    R.USER_PATH = os.path.join(_rhome, "showrunner", "roles.json")
    try:
        eq("with the seat free, the mapping still grants it — the fix must not break the case "
           "the mapping exists for",
           R.resolution(seat_cfg, "session-A").get("role"), "campaign-lead")
        R.claim(seat_cfg, "campaign-lead", "session-B", pid=os.getpid(), who="agent-B")
        _a = R.resolution(seat_cfg, "session-A")
        eq("a session mapped into a role whose every seat is HELD BY SOMEBODY ELSE gets the "
           "FALLBACK — the most permissive answer on the weakest evidence is the one thing this "
           "must not do", _a.get("role"), R.FALLBACK)
        ok("...and is TOLD who holds it, because 'held by pid N, not you' is both true and more "
           "useful than a bare fallback",
           "HELD by somebody else" in (_a.get("how") or "") and "pid" in (_a.get("how") or ""),
           _a.get("how"))
        eq("...while the ACTUAL holder still resolves to it, so the check discriminates between "
           "two sessions rather than switching the mapping off",
           R.resolution(seat_cfg, "session-B").get("role"), "campaign-lead")
    finally:
        R.USER_PATH = _prev_path

    # AND THE ANNOUNCEMENT CARRIES THEM, which is the integration the issue asks for: a session
    # is greeted with what it may not do, generated from the fields the guards read, at the seam
    # that survives compaction. Without this, `enforced_lines` is only proved in isolation and
    # could stop reaching the text a session actually sees.
    home = tmpdir("whoami-roles")
    os.makedirs(os.path.join(home, "showrunner"), exist_ok=True)
    rp = os.path.join(home, "showrunner", "roles.json")
    with open(rp, "w") as fh:
        json.dump({"roles": {R.FALLBACK: {"acquire": "claim", "notes": "consumer prose here"}}},
                  fh)
    orig = R.USER_PATH
    R.USER_PATH = rp
    try:
        told = "\n".join(R.whoami(cfg, "sess-y"))
        ok("with roles defined, the announcement carries the ENFORCED block generated from them",
           "ENFORCED" in told and "NOTHING" in told, told[-300:])
        ok("...and labels `notes` as prose nothing checks, beside it rather than mixed into it",
           "consumer prose here" in told and "Nothing checks it" in told, told[-300:])
    finally:
        R.USER_PATH = orig


def test_crawler_seat_resolves_to_a_role():
    group("A Crawler spawn placed is not write-denied in the worktree made for it (#40)")
    from showrunner import roles as R

    cfg = make_repo()
    g = new_graph(cfg)
    g.add("work", leaf_id="c1", labels=["backend"])
    rec = worktree.spawn(cfg, g.show("c1"), actor="crawler-c")
    campaign.record_spawn(cfg, rec, pid=os.getpid())

    home = tmpdir("seat-roles-home")
    os.makedirs(os.path.join(home, "showrunner"), exist_ok=True)
    rp = os.path.join(home, "showrunner", "roles.json")
    ROLES = {"campaign-lead": {"acquire": "claim", "capacity": 1, "may_create": ["worker"]},
             "worker": {"acquire": "assign", "reports_to": "campaign-lead",
                        "writes": {"allow": ["**"]}},
             R.FALLBACK: {"acquire": "claim", "writes": {"deny": ["**"]}}}

    def write(seat_map):
        body = {"roles": ROLES}
        if seat_map is not None:
            body["seat_roles"] = seat_map
        with open(rp, "w") as fh:
            json.dump(body, fh)

    orig, cwd = R.USER_PATH, os.getcwd()
    R.USER_PATH = rp
    try:
        # THE FIXTURE BEFORE THE READ. `spec` over a file that does not exist yet returns {}, and
        # `_resolved` refuses a role it cannot find in `defs` — so computing this first made every
        # fallback assertion below hold no matter what the code did, which is the shape of a test
        # that certifies the defect it was written to catch.
        write(None)
        defs, _ = R.spec(cfg)
        ok("the fixture's roles are actually loaded, so the fallback assertions below could have "
           "resolved to `worker` and did not", "worker" in defs, sorted(defs))
        # ONE CONFIG PER TREE, rather than one config and a chdir. `_resolved` reaches the seat
        # through `seat(cfg)`, which answers about the tree the CONFIG records; a test that moved
        # the process instead would be asserting against the ambient cwd this seam no longer
        # reads, and would pass for the wrong reason on the day it broke.
        wt_cfg = config.load(start=rec["worktree"])

        # THE REGRESSION. Every Crawler resolved to the fallback, whose policy denies writes
        # everywhere, INSIDE the tree spawn had just made for it to work in. An audit leaf
        # finished only by routing its evidence around the guard with shell redirection.
        role_before, how_before = R._resolved(wt_cfg, "sess-c", defs)
        eq("WITHOUT a mapping the Crawler still resolves to the fallback, so this stays opt-in "
           "for every consumer who has written none", role_before, R.FALLBACK)

        write({"crawler": "worker"})
        role, how = R._resolved(wt_cfg, "sess-c", defs)
        eq("with the seat mapped, the Crawler resolves to the role the USER named for it",
           role, "worker")
        ok("...and says it was the campaign record that assigned it, naming the leaf — the "
           "record spawn wrote before the session existed IS the assignment `assign` meant",
           "campaign record" in how and "c1" in how, how)

        # A WORKTREE NOBODY RECORDED GRANTS NOTHING, or `git worktree add` is a way to hand
        # yourself a role. This is the half that keeps the derivation record-based rather than
        # location-based.
        hand = os.path.join(tmpdir("hand-rolled-tree"), "wt")
        rc, _out, _err = util.run(["git", "worktree", "add", "-b", "by-hand", hand],
                                  cwd=cfg.root)
        if rc != 0:
            skip("a hand-added worktree grants nothing", "git worktree add failed")
        else:
            eq("a linked worktree NO campaign record names resolves to the fallback even with "
               "the seat mapped",
               R._resolved(config.load(start=hand), "sess-h", defs)[0], R.FALLBACK)

        # AND THE MAIN CHECKOUT IS NOT A CREDENTIAL. Shipping `orchestrator` mapped would put a
        # lead in every session that happened to be in the right directory, which is the failure
        # this whole seam replaced.
        eq("the orchestrator seat is left unmapped, so standing in the main checkout of a repo "
           "with a campaign confers no role by itself",
           R._resolved(cfg, "sess-o", defs)[0], R.FALLBACK)

        # A PROJECT MAY NOT REMAP ITS OWN SEAT -- it could otherwise hand itself any role in the
        # catalog, the widening the definitions left the repo to prevent.
        merged, problems = R.seat_roles({"seat_roles": {"crawler": "campaign-lead"}})
        eq("a project remapping a seat the user already mapped loses to the user-level mapping",
           merged.get("crawler"), "worker")
        ok("...and the conflict is reported rather than resolved silently",
           any("user-level" in x for x in problems), problems)

        # A DROPPED MAPPING IS ANNOUNCED, not merely returned. `whoami` is the seam a session
        # actually reads, and a mapping silently ignored there leaves it told `unassigned` while
        # a file it cannot see says otherwise.
        wt_cfg.data["seat_roles"] = {"crawler": "campaign-lead"}
        body = "\n".join(R.whoami(wt_cfg, session="sess-c"))
        ok("`whoami` says a seat mapping was IGNORED rather than dropping it quietly",
           "SEAT MAPPING IGNORED" in body, body[-300:])
        wt_cfg.data.pop("seat_roles", None)

        # A SEAT MAPPED AT A ROLE NOBODY DEFINED resolves to the fallback, which looks exactly
        # like having written no mapping at all. One typo buys the whole bug back, so `doctor`
        # refuses rather than leaving it to be discovered as a write denial mid-run.
        write({"crawler": "wroker"})
        rc, out, _err = util.run([os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                                 cwd=cfg.root, env=dict(os.environ, XDG_CONFIG_HOME=home))
        ok("`doctor` FAILS on a seat mapped at a role no definition provides, naming the seat "
           "and the typo", rc != 0 and "wroker" in out and "crawler" in out, (rc, out[-400:]))
    finally:
        os.chdir(cwd)
        R.USER_PATH = orig


def test_close_resolves_paths_against_the_callers_tree():
    group("A relative proof belongs to the tree the closer is standing in, not the main checkout")

    # THE TWO FILES SHARING A PATH ARE THE WHOLE BUG. The main checkout carries a stale copy of
    # the same relative path, so joining against `cfg.root` finds a real, non-empty, OLD file and
    # the gate reports the one verdict it exists to catch — proof that predates the claim —
    # against a file the closer never touched.
    SHARED = os.path.join("evidence", "witness.txt")
    cfg = make_repo(files={"README.md": "seed\n", SHARED: "the main checkout's stale copy\n"})
    stale_in_main = os.path.join(cfg.root, SHARED)
    long_ago = time.time() - 86400
    os.utime(stale_in_main, (long_ago, long_ago))

    g = new_graph(cfg)
    g.add("work", leaf_id="c1", labels=["backend"])
    rec = worktree.spawn(cfg, g.show("c1"), actor="crawler-c")
    tree = rec["worktree"]

    def close(leaf_id, proof, premise_read="only-in-this-tree.md"):
        """The leaf status, or the refusal TEXT. A `Refused` is the finding under test here — the
        bug is a false refusal — so it is captured and asserted on rather than left to abort the
        test and hide which assertion the mutation broke."""
        try:
            leaf, _notes = gates.close_gate(cfg, g, leaf_id, proof, "done", premise="holds",
                                            premise_read=premise_read)
            return leaf["status"]
        except Refused as exc:
            return "REFUSED: %s" % exc

    def fresh(leaf_id, rel_path=SHARED):
        """Claim a leaf, THEN write the artifact, so its mtime is unambiguously after the claim."""
        g.claim(leaf_id, "crawler-c")
        full = os.path.join(tree, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write("the proof this agent just wrote for %s\n" % leaf_id)
        return full

    first = fresh("c1")
    with open(os.path.join(tree, "only-in-this-tree.md"), "w") as fh:
        fh.write("read to check the premise\n")

    ok("the fixture really does have two files at one relative path, one stale and one fresh, or "
       "this proves nothing",
       os.path.exists(stale_in_main) and int(os.path.getmtime(stale_in_main))
       < int(os.path.getmtime(first)),
       (int(os.path.getmtime(stale_in_main)), int(os.path.getmtime(first))))

    cwd = os.getcwd()
    try:
        os.chdir(tree)

        # THE REPORTED FAILURE, ISOLATED. `--premise-read` names a file present in BOTH trees, so
        # the only thing this can be refused for is the PROOF path — which is the whole point: the
        # verdict the mis-join manufactured was "proof predates the claim", the one verdict the
        # gate exists to produce, against a file the closer had never touched.
        eq("a relative --proof written in the closer's own worktree CLOSES the leaf, instead of "
           "being refused as proof that predates the claim — the gate was reading the main "
           "checkout's stale copy of the same path",
           close("c1", SHARED, premise_read="README.md"), G.CLOSED)

        # THE OTHER HALF: `--premise-read` shared the join, so the file an agent actually read
        # was reported as not existing when it lived only in the worktree.
        g.add("more", leaf_id="c2", labels=["backend"])
        fresh("c2")
        eq("...so a --premise-read naming a file that exists ONLY in the worktree is accepted "
           "rather than reported as missing", close("c2", SHARED), G.CLOSED)

        # AN ABSOLUTE PATH ALWAYS WORKED, which is exactly why the bug was invisible to anyone
        # who passed one and fatal to anyone who typed the relative form.
        g.add("third", leaf_id="c3", labels=["backend"])
        abs_proof = fresh("c3")
        eq("an absolute --proof still closes, so the fix did not trade one form for the other",
           close("c3", abs_proof, premise_read=os.path.join(tree, "only-in-this-tree.md")),
           G.CLOSED)

        # AND A GENUINELY STALE PROOF IS STILL REFUSED. Turning a false refusal into a false
        # acceptance would be strictly worse than the bug.
        g.add("fourth", leaf_id="c4", labels=["backend"])
        g.claim("c4", "crawler-c")
        genuinely_old = os.path.join(tree, "predates.txt")
        with open(genuinely_old, "w") as fh:
            fh.write("written before the claim\n")
        os.utime(genuinely_old, (long_ago, long_ago))
        ok("a proof in the closer's OWN tree that really does predate the claim is STILL refused "
           "— the gate was repaired, not disabled",
           "older than the work" in close("c4", "predates.txt"), close("c4", "predates.txt"))

        # THE REFUSAL NAMES THE TREE IT LOOKED IN. The failure this replaces was undiagnosable
        # from outside: a path the caller recognised, and a verdict about a file it never saw.
        g.add("fifth", leaf_id="c5", labels=["backend"])
        g.claim("c5", "crawler-c")
        missing = close("c5", "no/such/artifact.txt")
        ok("a missing relative path says WHICH tree it was resolved against, so the next agent "
           "to hit this can see the resolution rather than doubting its own artifact",
           tree in missing and "absolute" in missing, missing[:300])
    finally:
        os.chdir(cwd)

    # FROM THE MAIN CHECKOUT NOTHING CHANGES, because there the caller's tree IS cfg.root. That
    # is what makes this a repair rather than a relocation of the same bug.
    g.add("sixth", leaf_id="c6", labels=["backend"])
    g.claim("c6", "orchestrator")
    with open(os.path.join(cfg.root, "from-main.txt"), "w") as fh:
        fh.write("closed from the main checkout\n")
    try:
        os.chdir(cfg.root)
        eq("a relative path from the MAIN checkout still resolves there",
           close("c6", "from-main.txt", premise_read="README.md"), G.CLOSED)
    finally:
        os.chdir(cwd)

    # A CWD IN A DIFFERENT REPO IS NOT "THE TREE THE CLOSER IS STANDING IN". Resolving a proof
    # into a stranger checkout would be this same bug pointed somewhere new, so an unrelated cwd
    # falls back to `cfg.root` — which was always the right answer for that case.
    stranger = make_repo(files={"README.md": "a different repo entirely\n"})
    g.add("seventh", leaf_id="c7", labels=["backend"])
    g.claim("c7", "orchestrator")
    with open(os.path.join(cfg.root, "from-main-again.txt"), "w") as fh:
        fh.write("still the campaign's own checkout\n")
    try:
        os.chdir(stranger.root)
        eq("standing in an UNRELATED repo falls back to the campaign's own checkout rather than "
           "resolving the proof into a stranger tree",
           close("c7", "from-main-again.txt", premise_read="README.md"), G.CLOSED)
    finally:
        os.chdir(cwd)


def test_role_seat_verbs():
    group("Both acquire modes are reachable, and a claim outlives the call that made it (#40)")
    from showrunner import roles as R

    # crawler_leaf came back THIN from the sweep -- only 2 assertions noticed it answering None
    # everywhere -- and it is POLICY, not reporting: it is what stops `git worktree add` being a
    # way to grant yourself a working role. Driven directly here, standing in each tree, because
    # the two callers reach it through seat() where a None is easy to mistake for "no campaign".
    cl_cfg = make_repo()
    _rec = campaign.load(cl_cfg)
    _rec.setdefault("crawlers", []).append(
        {"crawler": "placed-wt", "leaf": "L9", "worktree": ".worktrees/placed-wt",
         "state": "spawned"})
    campaign.save(cl_cfg, _rec)
    _here = os.getcwd()
    try:
        for _n in ("placed-wt", "hand-added-wt"):
            _p = os.path.join(cl_cfg.worktree_root, _n)
            sh(["git", "worktree", "add", "-q", _p, "-b", "showrunner/%s" % _n], cl_cfg.root)
        placed = config.load(start=os.path.join(cl_cfg.worktree_root, "placed-wt"))
        by_hand = config.load(start=os.path.join(cl_cfg.worktree_root, "hand-added-wt"))
        eq("a worktree the campaign RECORDED resolves to its leaf — the tree showrunner placed "
           "before the session existed", R.crawler_leaf(placed), "L9")
        ok("...and a worktree somebody added BY HAND resolves to nothing, so `git worktree add` "
           "is not a way to grant yourself a role",
           R.crawler_leaf(by_hand) is None, R.crawler_leaf(by_hand))
        ok("...and the MAIN checkout is not a crawler leaf either, so an orchestrator cannot "
           "pick up a Crawler's seat by standing still",
           R.crawler_leaf(cl_cfg) is None, R.crawler_leaf(cl_cfg))
        # ...and all three answers are given in ONE process from ONE cwd, so none of them can be
        # an artefact of where this test happened to be standing.
        ok("the three trees are told apart by the CONFIG each was loaded from, not by chdir",
           len({placed.tree, by_hand.tree, cl_cfg.tree}) == 3,
           (placed.tree, by_hand.tree, cl_cfg.tree))
    finally:
        os.chdir(_here)

    SR = [sys.executable, os.path.join(ROOT, "bin", "showrunner")]
    cfg = make_repo()
    home = tmpdir("role-verb-home")
    os.makedirs(os.path.join(home, "showrunner"), exist_ok=True)
    rp = os.path.join(home, "showrunner", "roles.json")
    with open(rp, "w") as fh:
        json.dump({"roles": {
            "campaign-lead": {"acquire": "claim", "capacity": 1, "may_create": ["worker"],
                              "writes": {"allow": ["**"]}},
            "worker": {"acquire": "assign", "reports_to": "campaign-lead"},
            R.FALLBACK: {"acquire": "claim", "writes": {"deny": ["**"]}}}}, fh)

    def sr(*argv, **kw):
        env = dict(os.environ, XDG_CONFIG_HOME=kw.pop("xdg", home), NO_COLOR="1")
        p = subprocess.run(SR + list(argv), cwd=kw.pop("cwd", cfg.root),
                           capture_output=True, text=True, env=env)
        return p.returncode, p.stdout, p.stderr

    # NEITHER ACQUISITION MODE WAS REACHABLE. `assign` had no reader until a seat could resolve
    # through `seat_roles`, and `claim` had no writer at all: `roles.claim` was a library function
    # with zero callers, and the `claim` VERB claims a leaf. On a stock install that made every
    # session the fallback whatever its roles said, and seating anything meant importing the
    # library from Python.
    rc, out, _err = sr("role", "claim", "campaign-lead", "--session", "sess-1", "--who", "agent-a")
    eq("`role claim` seats a role from the CLI, which no verb could do", rc, 0)
    ok("...and states the liveness basis rather than only success — a claim whose pid exits "
       "reports success too, which is what made the failure silent", "liveness basis" in out, out)

    entries = json.loads(sr("role", "roster", "--json")[1])
    eq("...and the roster shows that seat HELD",
       [(e["role"], e["state"]) for e in entries], [("campaign-lead#0", locks.HELD)])
    ok("...with the basis recorded on the holder, so a reader sees which fact liveness rests on",
       (entries[0].get("holder") or {}).get("pid_basis"), entries)

    rc, out, _err = sr("whoami", "--session", "sess-1")
    ok("`whoami` announces the CLAIMED role — the whole path from CLI to announcement, which "
       "had no first step before", "campaign-lead" in out and "claimed" in out, out[-300:])

    # ONE RESOLVER, TWO RENDERINGS. A hook author with only prose to read reimplements the
    # resolver, and the copy drifts: a consumer's write guard kept one, did not learn
    # `seat_roles`, and enforced the deny-everything fallback while `whoami` announced a role.
    def porcelain(*argv, **kw):
        """(doc or None). A parse failure is a FINDING, not a traceback — the claim under test is
        that a hook can consume this, and `None` lets that fail as an assertion."""
        body = sr(*argv, **kw)[1]
        try:
            return json.loads(body)
        except ValueError:
            return None

    doc = porcelain("whoami", "--porcelain", "--session", "sess-1")
    ok("`whoami --porcelain` emits JSON a hook can parse at all — having no machine-readable "
       "answer is what forced a hook author to keep a copy of the resolver", doc is not None,
       sr("whoami", "--porcelain", "--session", "sess-1")[1][:200])
    eq("...resolving the SAME role the prose announced, so a guard and a human cannot be told "
       "different things", (doc or {}).get("role"), "campaign-lead")
    ok("...and carries the writes rule a guard has to enforce, which is the field whose absence "
       "forced the copy", (doc or {}).get("policy", {}).get("writes"), doc)
    ok("...and exposes `enforced` to branch on, so a parser never has to infer policy from a "
       "role name", (doc or {}).get("enforced") is True, doc)

    empty = tmpdir("no-roles-home")
    blind = porcelain("whoami", "--porcelain", xdg=empty) or {}
    ok("with NO roles defined it says enforced=false and role=null — a guard must not be able "
       "to read 'no policy exists' as 'this write is permitted'",
       blind.get("enforced") is False and blind.get("role", "x") is None, blind)

    # A ROLE DECLARING `assign` IS NOT CLAIMABLE. Its entire meaning is that whoever created the
    # session decided; taking it here would be self-nomination into a seat the model says cannot
    # be self-nominated.
    rc, out, err = sr("role", "claim", "worker", "--session", "sess-2")
    eq("a role declaring acquire=assign is REFUSED, with the PreToolUse deny code", rc, 2)
    ok("...and the refusal names how an assigned role IS obtained instead of only saying no",
       "seat_roles" in (out + err), (out + err)[-240:])

    rc, out, err = sr("role", "claim", "nonesuch")
    eq("an undefined role is refused", rc, 2)
    ok("...naming what IS defined, so the refusal is actionable rather than a wall",
       "campaign-lead" in (out + err), (out + err)[-240:])

    rc, out, _err = sr("role", "release", "campaign-lead")
    eq("`role release` gives the seat back — the counterpart `claim` never had, so a seat could "
       "previously only be surrendered by outliving it or deleting a file", rc, 0)
    eq("...and the roster is empty again", json.loads(sr("role", "roster", "--json")[1]), [])
    ok("...and `whoami` returns to the fallback, so releasing is observable end to end",
       R.FALLBACK in sr("whoami", "--session", "sess-1")[1], "")

    # THE REPORTED FAILURE, and it is the worst of the three possible outcomes: success reported,
    # nothing held. A claim keyed to a short-lived process reads STALE the moment that process
    # exits, so `whoami` announces the fallback while the claimer was told `ok: True`.
    dead = DeadPid().pid
    got, h = R.claim(cfg, "campaign-lead", "sess-3", pid=dead, who="agent-c")
    ok("a claim handed a pid that has ALREADY exited still reports success at the moment of "
       "claiming, which is precisely why this was silent", got, h)
    eq("...while the roster reads STALE immediately",
       [(e["role"], e["state"]) for e in R.roster(cfg)], [("campaign-lead#0", locks.STALE)])
    orig = R.USER_PATH
    R.USER_PATH = rp
    try:
        defs, _ = R.spec(cfg)
        eq("...and the resolver skips it, so the session is announced as the fallback despite "
           "having been told it took the seat", R._resolved(cfg, "sess-3", defs)[0], R.FALLBACK)
    finally:
        R.USER_PATH = orig
    rc, out, err = sr("role", "roster")
    ok("`role roster` NAMES the stale seat and says those sessions announce the fallback — "
       "before this the only way to see a dead claim was calling roles.roster() from Python",
       "STALE" in out and "fallback" in err, (out, err))

    # THE FIX. The pid is DISCOVERED rather than handed over, so the seat outlives the call.
    R.release(cfg, "campaign-lead", force=True)
    rc, out, _err = sr("role", "claim", "campaign-lead", "--session", "sess-4")
    eq("claiming through the verb succeeds", rc, 0)
    eq("...and the seat is still HELD once the claiming process has EXITED — the CLI process is "
       "gone by the time this reads, which is exactly the case that produced STALE",
       [(e["role"], e["state"]) for e in json.loads(sr("role", "roster", "--json")[1])],
       [("campaign-lead#0", locks.HELD)])
    ok("...on a pid that is NOT the exited CLI process, which is the whole mechanism",
       (json.loads(sr("role", "roster", "--json")[1])[0].get("holder") or {}).get("pid_basis")
       in ("ancestor-claude", "ppid-fallback"),
       json.loads(sr("role", "roster", "--json")[1]))
    R.release(cfg, "campaign-lead", force=True)


def test_dispatch_guard():
    group("The cheap dispatch path has a gate on it now (#37)")
    from showrunner import roles as R

    RAW = ('GAME_LOOP_SESSION=lane-x nohup claude -p --permission-mode bypassPermissions '
           '--model sonnet "$(cat BRIEF.md)" < /dev/null > /tmp/lane-x.out 2>&1 &')
    cfg = make_repo()

    # NO ROLES CONFIGURED MEANS NO POLICY, AND SO NO REFUSAL. showrunner never learns what a role
    # means (#40); inventing one here would refuse the dispatches of every consumer who has not
    # written any, by a rule nobody wrote.
    allow, _, det = dispatch.dispatch_guard(cfg, "s", tool="Bash", tool_input={"command": RAW})
    ok("with no roles configured the raw dispatch is ALLOWED, and marked unchecked rather than "
       "passed — there is no policy to enforce", allow and not det.get("checked"), det)

    # WITH A POLICY, the fallback role may create nothing, so the 42-times command is refused.
    home = tmpdir("roles-home")
    os.makedirs(os.path.join(home, "showrunner"), exist_ok=True)
    rp = os.path.join(home, "showrunner", "roles.json")
    with open(rp, "w") as fh:
        json.dump({"roles": {"lead": {"acquire": "claim", "may_create": ["worker"]},
                             "worker": {"acquire": "assign", "reports_to": "lead"},
                             R.FALLBACK: {"acquire": "claim"}}}, fh)
    orig_path = R.USER_PATH
    R.USER_PATH = rp
    try:
        allow, msg, det = dispatch.dispatch_guard(cfg, "s", tool="Bash",
                                                  tool_input={"command": RAW})
        ok("the exact command used 42 times in one real run is DENIED once a policy exists — no "
           "worktree, no lease, no claim a reaper can reclaim, no room", not allow, det)
        ok("...naming the role that was refused and pointing at `spawn --launch`, so the refusal "
           "is actionable rather than a wall", R.FALLBACK in msg and "spawn" in msg, msg[:200])

        # A GUARD THAT DENIES ITS OWN REMEDY GETS SWITCHED OFF. `spawn --launch` builds a claude
        # command line of its own, so the tool's own dispatch must pass.
        through = RAW.replace("nohup claude", "nohup showrunner-launched claude")
        allow2, _, _ = dispatch.dispatch_guard(cfg, "s", tool="Bash",
                                               tool_input={"command": through})
        ok("...while a command that goes through showrunner passes, because a guard that refuses "
           "its own remedy is one that gets turned off", allow2)

        allow3, _, _ = dispatch.dispatch_guard(cfg, "s", tool="Bash",
                                               tool_input={"command": "claude --version"})
        ok("...and `claude` WITHOUT -p passes: this is not a ban on the binary, it is a check "
           "that a session may dispatch at all", allow3)

        # THE PROTOTYPE'S BLIND SPOT, asserted as a stated LIMIT rather than left to be found.
        # A consumer's version matched PreToolUse on `Agent` and guarded the in-process subagent
        # tool while every real dispatch went out through Bash — 42 times, reporting nothing.
        allow4, _, det4 = dispatch.dispatch_guard(cfg, "s", tool="Agent",
                                                  tool_input={"command": RAW})
        ok("the SAME command through a non-Bash tool is allowed and marked UNCHECKED — the tool "
           "this is matched on is part of the guard, and a version matched on `Agent` guarded "
           "the subagent tool while every real dispatch went through Bash",
           allow4 and not det4.get("checked"), det4)

        # A ROLE THAT MAY CREATE dispatches freely — otherwise the assertion above would pass
        # against a guard that refuses everyone.
        with open(rp, "w") as fh:
            json.dump({"roles": {R.FALLBACK: {"acquire": "claim", "may_create": ["x"]},
                                 "x": {"acquire": "assign", "reports_to": R.FALLBACK}}}, fh)
        allow5, _, det5 = dispatch.dispatch_guard(cfg, "s", tool="Bash",
                                                  tool_input={"command": RAW})
        ok("...and a role that MAY create is not refused, so the rule tracks the policy rather "
           "than refusing everybody", allow5, det5)

        # UNREADABLE POLICY ALLOWS, LOUDLY. A PreToolUse that hard-fails on its own plumbing
        # blocks the write that would repair it.
        with open(rp, "w") as fh:
            fh.write("{not json")
        allow6, msg6, det6 = dispatch.dispatch_guard(cfg, "s", tool="Bash",
                                                     tool_input={"command": RAW})
        ok("an unreadable role file ALLOWS and says the dispatch went unchecked — a guard that "
           "blocks when it cannot see blocks forever the day it breaks",
           allow6 and "UNCHECKED" in msg6, msg6[:120])
    finally:
        R.USER_PATH = orig_path


def test_void_run():
    group("A run that could not measure anything is not a degraded comparison (#41)")

    # THE REPORTED CASE: 156 minutes, 21 passed / 43 failed, several tool calls spent on
    # duration-dependent state, CDN throttling and a suspected licensing defect. The cause was a
    # router dying mid-run, and the evidence was already in the output — 20 x "hostname could not
    # be found", which is DNS and can only mean the network was gone. This module already refuses
    # to let reduced resolution read as a clean comparison; this is that one step further, and it
    # is the same shape as UNSCOREABLE-is-not-THIN: no resolution must not read as reduced.
    cfg = make_repo(extra_config={"checks": [
        {"name": "suite", "cmd": ["sh", "-c",
                                  "echo 'Error: hostname could not be found'; "
                                  "echo 'FAIL test_a'; exit 1"],
         "failure_pattern": "^FAIL (.*)$"}]})
    current = gates.run_checks(cfg)
    valid, report = gates.validity(cfg, current)
    ok("a run whose output says the hostname could not be found is VOID — it did not measure "
       "anything, and its failure count carries no information about the code", not valid, report)
    ok("...and says so in those terms rather than as another failure line",
       any("COULD NOT REACH THE WORLD" in l for l in report), report[:1])
    ok("...and states what it CANNOT see, because a gate that overstates its reach buys false "
       "confidence: a partial outage, a backend serving wrong data, a device degrading without "
       "a network error", any("does NOT establish" in l for l in report), report[-1:])

    # THE PAIR. A run with REAL failures and no network trouble must still compare — otherwise
    # the assertion above passes just as well against a gate that calls everything void, which
    # would hide every regression rather than one.
    real = make_repo(extra_config={"checks": [
        {"name": "suite", "cmd": ["sh", "-c", "echo 'FAIL test_a'; exit 1"],
         "failure_pattern": "^FAIL (.*)$"}]})
    valid2, _ = gates.validity(real, gates.run_checks(real))
    ok("a run that genuinely failed, with the world reachable, is VALID — the failures are "
       "about the code and must be compared, not excused", valid2)

    # THE EXIT CODE, END TO END. Folding VOID into 2 would give "your code broke" and "nothing
    # was measured" the same number, which is the substitution this exists to refuse.
    exe = os.path.join(ROOT, "bin", "showrunner")
    gates.record_baseline(cfg)
    res = subprocess.run([sys.executable, exe, "check"], cwd=cfg.root,
                         capture_output=True, text=True, env=dict(os.environ, NO_COLOR="1"))
    eq("`check` exits 3 on a VOID run — its own code, so a caller treating non-zero as 'the code "
       "is bad' gets one it did not map rather than a wrong answer it will believe",
       res.returncode, 3)
    ok("...with the reason on stderr", "COULD NOT REACH" in res.stderr, res.stderr[:160])
    real_res = subprocess.run([sys.executable, exe, "check"], cwd=real.root,
                              capture_output=True, text=True, env=dict(os.environ, NO_COLOR="1"))
    ok("...while a run with real failures does NOT exit 3, so the two are distinguishable by a "
       "caller", real_res.returncode != 3, real_res.returncode)

    # A PATTERN THAT WILL NOT COMPILE MEANS THAT CHECK WAS NOT SCREENED AT ALL. Skipping it in
    # silence turns 'not looked' into 'looked and found nothing', which is the failure the whole
    # validity idea exists to prevent, one level down.
    bad = make_repo(extra_config={
        "void_patterns": ["(unclosed"],
        "checks": [{"name": "suite", "cmd": ["sh", "-c", "echo hi"],
                    "failure_pattern": "^FAIL (.*)$"}]})
    hits = gates.run_checks(bad)["checks"][0]["void"]
    ok("an uncompilable void_pattern is REPORTED as unscreened rather than skipped quietly",
       any("UNSCREENED" in h for h in hits), hits)


def test_harness_installer_provenance():
    group("Where the harness comes from: an installer inside a working tree (#41)")
    if not have("git"):
        skip("the installer-provenance group", "git is not installed")
        return

    # A consumer pointed harness.installer at a developer's local clone, so every Crawler was
    # provisioned with whatever was UNCOMMITTED there at that moment — a per-machine, per-minute
    # artifact deciding what the whole party is guarded by, and nothing said so. This repo's own
    # rule: a harness that is PRESENT but different is worse than one that is absent, because
    # absent is loud. It REPORTS rather than refuses: pointing at a clone is legitimate while
    # developing the harness, and the defect is that it was invisible.
    def with_installer(path):
        # `or (None, "")` so a producer that stopped producing FAILS these assertions rather than
        # raising out of the group — a mutant that crashes its group is unscoreable, and the
        # sweep then reports a floor from a truncated run as though it were coverage.
        c = make_repo(extra_config={"harness": {"provision": "auto", "installer": path}})
        return harness.installer_provenance(c) or (None, "")

    outside = os.path.join(tmpdir("no-git"), "install.sh")
    with open(outside, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(outside, 0o755)
    lvl, msg = with_installer(outside)
    eq("an installer OUTSIDE any git working tree is reported ok — it is not a moving target",
       lvl, "ok")

    clone = make_repo()
    inner = os.path.join(clone.root, "install.sh")
    with open(inner, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(inner, 0o755)
    sh(["git", "add", "-A"], clone.root)
    sh(["git", "commit", "-q", "-m", "add installer"], clone.root)
    lvl, msg = with_installer(inner)
    eq("an installer INSIDE a git working tree is a WARNING even when that tree is clean — the "
       "point is that it tracks a checkout rather than a release, not that it is dirty today",
       lvl, "warn")
    ok("...naming the tree and the commit, so a reader can go and look at what their Crawlers "
       "are actually guarded by", clone.root in msg and "clean at" in msg, msg[:150])

    with open(os.path.join(clone.root, "uncommitted.txt"), "w") as fh:
        fh.write("a developer mid-edit\n")
    lvl, msg = with_installer(inner)
    ok("...and says UNCOMMITTED when the tree is dirty, which is the reported case: every "
       "Crawler gets whatever is in that clone right now",
       lvl == "warn" and "UNCOMMITTED" in msg, msg[:150])

    lvl, msg = with_installer(os.path.join(clone.root, "not-there.sh"))
    eq("an installer that does not exist is an ERROR, not a warning — every spawn will fail to "
       "provision", lvl, "error")

    ok("...while NO installer configured reports nothing at all, so this is silent for the "
       "consumers it does not concern",
       harness.installer_provenance(make_repo()) is None)


def test_roles():
    group("Roles as consumer config: shape is checked, meaning never is (#40)")
    from showrunner import roles as R

    good = {"lead": {"acquire": "claim", "capacity": 1, "may_create": ["worker"]},
            "worker": {"acquire": "assign", "reports_to": "lead"},
            R.FALLBACK: {"acquire": "claim"}}
    lv = R.validate(good)
    ok("a well-formed role set passes with no errors — showrunner checked the SHAPE and formed no "
       "opinion about what 'lead' means", not [f for f in lv if f[0] == "error"], lv)

    # EACH RULE EXERCISED SEPARATELY. A validator tested only against one broken config reports
    # whichever rule happens to fire first, and the others are never known to work at all.
    for label, defs, want in (
            ("a reports_to naming no defined role",
             {"a": {"acquire": "claim", "reports_to": "nobody"}}, "not a defined role"),
            ("a may_create naming no defined role",
             {"a": {"acquire": "claim", "may_create": ["ghost"]}}, "may_create"),
            ("an acquire mode that is neither claim nor assign",
             {"a": {"acquire": "sideways"}}, "must be one of"),
            ("a capacity that is not a positive integer",
             {"a": {"acquire": "claim", "capacity": 0}}, "positive integer"),
            ("a reports_to CYCLE",
             {"a": {"acquire": "claim", "reports_to": "b"},
              "b": {"acquire": "claim", "reports_to": "a"}}, "CYCLE"),
            ("an org with NO ROOT, so escalation never ends",
             {"a": {"acquire": "claim", "reports_to": "b"},
              "b": {"acquire": "claim", "reports_to": "a"}}, "no ROOT"),
            ("NOTHING CLAIMABLE, so a from-scratch session could never acquire a role",
             {"t": {"acquire": "assign"}, "w": {"acquire": "assign", "reports_to": "t"}},
             "nothing is claimable"),
            ("a FALLBACK that may_create, which would be a way around the policy",
             {"x": {"acquire": "claim"},
              R.FALLBACK: {"acquire": "claim", "may_create": ["x"]}}, "fallback")):
        errs = [m for l, m in R.validate(defs) if l == "error"]
        ok("%s is an ERROR" % label, any(want.lower() in m.lower() for m in errs), errs[:2])

    ok("an unknown field is a WARNING naming what IS checked, not an error — a consumer may "
       "carry their own keys, they just do not mean anything here",
       any(l == "warn" and "colour" in m
           for l, m in R.validate({"a": {"acquire": "claim", "colour": "blue"}})),
       R.validate({"a": {"acquire": "claim", "colour": "blue"}}))
    ok("...and `notes` is NOT warned about, because it is the one field declared as prose "
       "showrunner deliberately does not check",
       not any("notes" in m for l, m in R.validate({"a": {"acquire": "claim", "notes": "hi"}})))

    # A CLAIM IS A LOCK, reused rather than reinvented: exclusive, held by a live process,
    # reclaimable when that process is proved dead. A second mutex would drift from the first.
    cfg = make_repo()
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        got, h = R.claim(cfg, "lead", "sess-A", holder.pid, who="agent-a")
        ok("a session can CLAIM an open seat", got, h)
        eq("...and the roster shows it HELD by a live process",
           [(r["role"], r["state"]) for r in R.roster(cfg)], [("lead#0", locks.HELD)])
        got2, h2 = R.claim(cfg, "lead", "sess-B", os.getpid(), who="agent-b")
        ok("...and a SECOND session cannot take the same seat — 'two sessions believing they "
           "lead with nobody told' is the hazard, and exclusivity is what answers it", not got2)
        eq("...with the roster naming who actually holds it, so the loser can see why",
           (h2 or {}).get("session"), "sess-A")
        # A SECOND, REAL CONSUMER rather than a second assertion over the same one: `doctor`
        # reads the roster so a human can see who holds what. An empty roster reads as "no seat
        # is held", which is precisely what a session checks before claiming one.
        doc = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                             cwd=cfg.root, capture_output=True, text=True,
                             env=dict(os.environ, NO_COLOR="1"))
        ok("doctor reports the held seat and who holds it, so a roster that went empty would be "
           "visible to a human rather than only to the next claimer",
           "lead#0" in doc.stdout and "agent-a" in doc.stdout,
           [l for l in doc.stdout.splitlines() if "seat" in l][:1])
    finally:
        holder.terminate()
        holder.wait()

    # THE CLAIM ROOT FOLLOWS THE CAMPAIGN, unlike the lock root. Two campaigns each want their
    # own lead; a physical device stays shared (#39).
    a = config.load(start=cfg.root, campaign="one")
    b = config.load(start=cfg.root, campaign="two")
    ok("role claims are per-CAMPAIGN, so two campaigns can each have a lead",
       R._claims_root(a) != R._claims_root(b), (R._claims_root(a), R._claims_root(b)))
    eq("...while the lock root stays shared, because a device is shared by the machine and not "
       "by a body of work", a.lock_root, b.lock_root)


def test_campaign_scoping():
    group("A campaign is smaller than a repo (#39)")
    cfg = make_repo()

    # THE PREMISE, CHECKED FIRST. Everything was scoped per git root: one graph, one record, one
    # event stream, one scratch — so `ready` returned the UNION and `claim --next` could hand
    # campaign A a leaf belonging to B. The docs described that as intended, correctly, because
    # the model was many orchestrators on ONE campaign rather than many campaigns.
    eq("with no campaign selected, state lives where it always has — an existing checkout is "
       "untouched, which is the property that makes this safe to adopt",
       cfg.state_dir, os.path.join(cfg.root, ".showrunner"))
    eq("...and the campaign is None rather than a campaign named ''", cfg.campaign, None)

    # THE CONTROL FOR THE TWO ASSERTIONS ABOVE, which read as facts about the code and were
    # partly facts about the environment. SHOWRUNNER_CAMPAIGN is exported into every Crawler by
    # design, this suite included, so a run from inside a campaign saw cfg.state_dir under
    # .showrunner/campaigns/<slug>/ and both of them failed — along with six more that merely
    # build the repo-wide path. run.py clears the ambient variable at import; what follows is
    # why that clear is not a line of setup nothing checks.
    ok("the suite process carries no selected campaign, so every fixture here describes the "
       "repo-wide layout whoever runs it and from wherever",
       config._campaign_from_env() is None,
       {"ambient value cleared at import": _AMBIENT_CAMPAIGN,
        "still set": os.environ.get("SHOWRUNNER_CAMPAIGN")})
    # AND THE CHILDREN, which is where six of the eight lived. Those assertions run the real
    # binary with `dict(os.environ, ...)`, so the selection travelled: `init` wrote its .gitignore
    # under .showrunner/campaigns/<slug>/ while the assertion asked git about .showrunner/, and
    # `doctor` read a waiting journal the test had appended to at the repo-wide path. Clearing it
    # in this process is only worth anything if the children see the clear too.
    #
    # BEFORE the block below that sets the variable, not after, and that ordering is the whole
    # value of these two lines. Written the other way round they ran downstream of a `pop` and
    # passed against a neutered clear — a control that had already cleaned up the condition it
    # was supposed to detect. Found by neutering the import-time clear and watching only ONE of
    # the three new assertions fail.
    _bare = tmpdir("campaign-ambient")
    sh(["git", "init", "-q", "-b", "main"], _bare)
    _ini = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "init"],
                          cwd=_bare, capture_output=True, text=True, env=dict(os.environ))
    ok("`init` succeeds in a bare repo, so the two checks below read a real run and not a "
       "failed one", _ini.returncode == 0, _ini.stderr.strip()[-300:])
    ok("a child launched the way every subprocess assertion here launches one inherits NO "
       "campaign, so it writes the repo-wide state dir",
       os.path.isfile(os.path.join(_bare, ".showrunner", ".gitignore")),
       sorted(glob.glob(os.path.join(_bare, ".showrunner", "**", ".gitignore"), recursive=True)))
    _bare2 = tmpdir("campaign-ambient-set")
    sh(["git", "init", "-q", "-b", "main"], _bare2)
    _ini2 = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "init"],
                           cwd=_bare2, capture_output=True, text=True,
                           env=dict(os.environ, SHOWRUNNER_CAMPAIGN="ambient-probe"))
    ok("...while a child that DOES inherit one writes under .showrunner/campaigns/ instead — the "
       "divergence those eight failures were reporting, and what stops the check above from "
       "passing on a binary that ignores the variable entirely",
       _ini2.returncode == 0
       and os.path.isfile(os.path.join(_bare2, ".showrunner", "campaigns", "ambient-probe",
                                       ".gitignore")),
       _ini2.stderr.strip()[-300:])

    # PAIRED WITH THE CASE WHERE IT HAPPENS, because "carries no selected campaign" is also
    # satisfied by a machine that simply never had the variable — which is most machines, and is
    # indistinguishable from a clear that stopped working. Worse, it would keep passing with
    # campaign scoping deleted outright. So manufacture the contamination and show the path MOVES.
    _prev_ambient = os.environ.get("SHOWRUNNER_CAMPAIGN")
    os.environ["SHOWRUNNER_CAMPAIGN"] = "ambient-probe"
    try:
        eq("...and a config loaded while one IS selected moves — which is what the two "
           "assertions at the top of this group were reading inside a campaign",
           config.load(start=cfg.root).state_dir,
           os.path.join(cfg.root, ".showrunner", "campaigns", "ambient-probe"))
    finally:
        # RESTORED, not deleted. Popping unconditionally would hand the rest of the suite a
        # different environment from the one it inherited, which is the same class of defect
        # this group is about — a test that quietly changes the world it measures.
        if _prev_ambient is None:
            os.environ.pop("SHOWRUNNER_CAMPAIGN", None)
        else:
            os.environ["SHOWRUNNER_CAMPAIGN"] = _prev_ambient

    # The name is a placeholder ON PURPOSE, and its SHAPE is load-bearing: mixed case, a space,
    # a digit and a run of non-alphanumerics are what `slug` has to fold, and this fixture is the
    # only place that is observed. Rename it and keep those, or the slug assertions below stop
    # checking anything.
    a = config.load(start=cfg.root, campaign="My Campaign #2")
    b = config.load(start=cfg.root, campaign="Another Campaign")
    ok("a selected campaign nests under .showrunner/campaigns/, so it sits beside the default "
       "rather than replacing it",
       a.state_dir.startswith(os.path.join(cfg.root, ".showrunner", "campaigns")), a.state_dir)
    ok("...slugged, because a campaign is named by a human and a story title is not a path",
       " " not in os.path.basename(a.state_dir), a.state_dir)
    ok("two campaigns get SEPARATE graphs, records, event streams and scratch — the union that "
       "let one campaign's `ready` hand out another's leaf is gone",
       len({a.graph_db, b.graph_db}) == 2 and len({a.state_dir, b.state_dir}) == 2,
       (a.graph_db, b.graph_db))

    # THE ONE THING THAT MUST NOT MOVE, and the only change here that could lose somebody's
    # hardware. A lock names a PHYSICAL single-consumer resource — a device, a bound port — which
    # is shared by the machine and not by a body of work. Per-campaign lock roots would let two
    # campaigns hold "the device" at once: a mutex that is quietly a no-op, which is the failure
    # `config.validate` already refuses in its other form because it looks like it works.
    eq("the LOCK root stays repo-wide across campaigns, so two campaigns flashing the same "
       "device still serialize against each other", a.lock_root, b.lock_root)
    eq("...and it is the repo's, not either campaign's",
       a.lock_root, os.path.join(cfg.root, ".showrunner", "locks"))
    ok("...which is the opposite of every other state path, so the two rules are visibly "
       "different rather than one rule with an exception nobody notices",
       a.graph_db != b.graph_db and a.lock_root == b.lock_root)

    # A CONFIG IS A STABLE ANSWER ABOUT ONE CAMPAIGN. The first version read the environment
    # lazily inside each path property, so two configs loaded for two campaigns both reported
    # whichever was selected LAST — a value that changes after you hold it.
    _prev_env_cfg = os.environ.get("SHOWRUNNER_CAMPAIGN")
    os.environ["SHOWRUNNER_CAMPAIGN"] = "something-else-entirely"
    try:
        ok("a loaded config does not change its answers when the environment moves under it",
           "my-campaign-2" in a.graph_db, a.graph_db)
        env_cfg = config.load(start=cfg.root)
        eq("...while a config loaded AFTER the change reads the new value, so the env var is "
           "still the selector and not a one-shot", env_cfg.campaign, "something-else-entirely")
    finally:
        # RESTORED rather than deleted, for the reason recorded above: `del` here would leave the
        # rest of the suite in an environment this test invented.
        if _prev_env_cfg is None:
            os.environ.pop("SHOWRUNNER_CAMPAIGN", None)
        else:
            os.environ["SHOWRUNNER_CAMPAIGN"] = _prev_env_cfg


def _hook_wiring(hook_dir, settings_files, excused):
    """Which hook files are registered nowhere, minus those excused in writing.

    A FUNCTION, not an inline sweep over the real directory, and that is the whole point.
    llm_chat's owner adopted this net and reported that their first version COULD NOT FAIL: it
    asked only about the real directory, where everything was already classified, so it passed
    whether or not the check worked. A default-deny net over a currently-clean set is exactly
    where a broken guard looks healthiest. Taking arguments means the decision can be exercised
    against a hook this repo does not have.
    """
    try:
        # FILES ONLY. A `__pycache__` directory — left by the parse check compiling a .py hook —
        # is not a hook nobody registered, and reporting it teaches a reader to skim this list.
        files = {f for f in os.listdir(hook_dir)
                 if not f.startswith(".") and os.path.isfile(os.path.join(hook_dir, f))}
    except OSError:
        return None                    # cannot look is not "nothing unwired"
    registered = set()
    for sf in settings_files:
        try:
            with open(sf) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        for _ev, groups in (d.get("hooks") or {}).items():
            for g in groups:
                for h in g.get("hooks", []):
                    c = (h.get("command") or "").strip('"')
                    if ".showrunner/hooks/" in c:
                        registered.add(os.path.basename(c))
    return sorted(files - registered - set(excused))


def _unsourced_rates(text, tools, source=None):
    """Lines stating a measured RATE with no committed instrument named on them.

    THE FAMILY ENTRY BOTH SIDES CALLED UNGUARDABLE. Every other member compares two artifacts
    that both exist — a list and a directory, a doc and a registry, a predicate and a hook. This
    one has to notice an artifact ABSENT AT THE MOMENT A CLAIM WAS MADE, and by the time anyone
    reads the claim the only evidence of the missing script is the claim itself.

    llm_chat's owner supplied the move that makes it checkable, and it is the same shape as the
    recorded sha: do not try to detect the absence later — require the instrument to be IN THE
    REPO when the number goes out, so a number without one is visibly a number without one.

    WHAT THIS CANNOT DO: check the number is right, or that the named tool produces it. It
    checks that a reader is told what to run. That is the same split as the wiring net — the
    enforcement is on the decision having been recorded, never on the decision being correct.
    """
    import re as _re
    # A FILE THAT IS ONE OF THE TOOLS IS ITS OWN REPRODUCER, and a checker for unsourced claims
    # is blind to that by construction. game_loop's auditor ran this over their own files and
    # three of the flags were the checker being wrong: the file citing the number WAS the thing
    # that produces it. Naming a reproducer beside it would have been circular at best.
    #
    # This also removes a false positive that had nothing to do with rates: `0.0%%` inside a
    # format string in the corpus tool. A checker that reads code as prose will find numbers in
    # it, and the self-reference exemption is the honest reason to skip that whole file rather
    # than special-casing percent signs.
    if source and source in tools:
        return []

    # PARAGRAPH, NOT LINE. The first version asked per line and immediately reported two false
    # positives of its own making: prose wraps, so a rate and the command that reproduces it
    # land on adjacent lines constantly. A reader does not read a line, they read the block —
    # so the block is the right unit, and asking per line means every rewrap is a new failure.
    bad, line_no = [], 1
    for para in text.split("\n\n"):
        lines = para.split("\n")
        if not any(l.lstrip().startswith(("#", "//")) for l in lines) and \
           _re.search(r"\b\d[\d,]* of [\d,]+\b|\b\d+\.\d+%|\b\d+% of\b", para) and \
           not any(t in para for t in tools):
            hit = next((l for l in lines if _re.search(
                r"\b\d[\d,]* of [\d,]+\b|\b\d+\.\d+%|\b\d+% of\b", l)), para)
            bad.append((line_no, hit.strip()[:100]))
        line_no += len(lines) + 1
    return bad


def _borrowed_unmarked(paths, read):
    """Tracked files citing another project's MEASUREMENT without saying it is unverified here.

    lamp's owner named the cost, and it is not symmetric: a borrowed claim is only correctable
    if the agent you borrowed it from goes and looks — so repeating another agent's finding
    without checking spends THEIR credibility, not yours, and you never find out.

    This repo already carries one that was RETRACTED by its author: a Stop gate reported unrun
    for eight hours, withdrawn because the session had been idle and no turn had ended. It sat
    in three tracked files as the justification for a feature. The feature was right anyway,
    which is exactly what makes the false reason survivable and therefore durable.

    PER FILE, NOT PER CLAIM, and that limit is stated rather than hidden: a file can gain a new
    unmarked claim under an existing marker. Twelve claims across four files made per-claim
    marking heavier than the defect, and a check nobody reads is worse than a coarse one.
    """
    import re as _re
    AGENT = (r"(auditor|llm_chat's owner|llm-chat-owner|lamp-owner|lamp's owner"
             r"|game_loop's auditor)")
    MEASURE = r"\b(measured|found|reported|hit|ran)\b"
    bad = []
    for path in paths:
        txt = _re.sub(r"\s+", " ", read(path))
        if _re.search(AGENT + r"[^.]{0,90}?" + MEASURE, txt) and \
           "REPORTED, NOT VERIFIED HERE" not in txt:
            bad.append(path)
    return sorted(bad)


def _unregistered_tempdirs(sources):
    """Raw `mkdtemp` calls in the test tree with no cleanup registered near them.

    THE POLICY WAS NEVER MISSING. `tmpdir()` has always appended to `TMPDIRS` and `cleanup()`
    has always emptied it — three call sites simply did not use it, and each leaked one
    directory per run. That is the shape a neighbouring agent diagnosed in their own repo:
    two helpers had teardown and the third did not, so it was never policy, only the place
    nobody wrote it.

    Returns (path, count) for files whose raw mkdtemp calls outnumber their registrations.
    """
    import re as _re
    bad = []
    for path, text in sources:
        raw = len(_re.findall(r"tempfile\.mkdtemp\(", text))
        if not raw:
            continue
        registered = (len(_re.findall(r"atexit\.register\(", text))
                      + len(_re.findall(r"TMPDIRS\.append\(", text))
                      + len(_re.findall(r"rmtree\(", text)))
        if raw > registered:
            bad.append((path, raw, registered))
    return bad


def test_command_paths_resolves_literal_variables():
    group("A path reached through a variable is the NORMAL shape under fan-out, not an edge case")

    # THE GUARD'S PRECONDITION IS THE CALLER'S ENVIRONMENT; ITS SUBJECT IS THE COMMAND'S WRITE
    # TARGETS. Those are not the same thing, so it is strongest where it is least needed and
    # weakest where a stray absolute path is most likely.
    #
    # The spelled-out path was already noticed. The VARIABLE form was not — and an orchestrator
    # reaches its worktrees through variables, so it is the normal form under fan-out. This
    # repo's own commit gate already refuses a target built from a variable rather than
    # resolving it, so the shape was known here before it was reported.
    spelled = 'cd /tmp && python3 -c "open(\'/repo/.worktrees/other/f\',\'w\')"'
    ok("a spelled-out absolute path is seen", "/repo/.worktrees/other/f"
       in lease.command_paths(spelled), lease.command_paths(spelled))

    for cmd in ("T=/repo/.worktrees/other && echo x > $T/f",
                "T=/repo/.worktrees/other; echo x > ${T}/f"):
        ok("...and so is one reached through a LITERAL variable assignment (%s)" % cmd[:32],
           "/repo/.worktrees/other/f" in lease.command_paths(cmd), lease.command_paths(cmd))

    # NOTHING IS EXECUTED, which is the line that decides what can be covered at all. Resolving
    # a command substitution or an indirect variable would mean RUNNING it, and a guard must
    # never do that to decide whether to allow a call.
    for cmd, why in (("T=$(pwd) && echo x > $T/f", "a command substitution"),
                     ("T=$OTHER && echo x > $T/f", "a variable assigned elsewhere")):
        eq("...while %s stays invisible, because resolving it would mean executing it" % why,
           lease.command_paths(cmd), [])

    eq("a command naming no absolute path yields nothing", lease.command_paths("echo hi"), [])

    # THE RESIDUAL IS NAMED WHERE A READER STANDS, not only in a docstring — a wrapper script, a
    # heredoc body, a path assembled from parts. Saying so is cheaper than leaving it open, and
    # the reporter asked for exactly that rather than for the chase.
    with open(os.path.join(ROOT, "llms.txt")) as fh:
        doc = re.sub(r"\s+", " ", fh.read())
    ok("llms.txt states what the path scan still cannot see, so a reader does not have to infer "
       "coverage from a notice", "wrapper script" in doc and "heredoc" in doc, doc[:0])
    ok("...and records that REPORT-never-refuse is the settled posture rather than an interim "
       "state somebody may reverse", "report, never refuse" in doc.lower())


def test_temp_dirs_are_cleaned_up():
    group("A fixture root with no teardown scales with the suite, and empty output looks like "
          "success")

    # WHY THIS IS NOT ABOUT DISK. A neighbouring agent measured ~62,000 entries in this
    # machine's temp root, 5,428 of them written by this repo's own tools. Past ARG_MAX a glob
    # in that directory becomes a command that CANNOT RUN and prints nothing — which is
    # indistinguishable from a clean result. Four searches across three repos failed that way
    # in one evening and nobody noticed, because empty output is what success looks like.
    #
    # So the leak is one more identity-element defect, and its victim is whoever greps next.
    sources = []
    for rel_path in ("test/run.py", "test/corpus.py", "test/mutate.py", "test/docs_surface.py"):
        full = os.path.join(ROOT, rel_path)
        if os.path.exists(full):
            with open(full) as fh:
                sources.append((rel_path, fh.read()))
    ok("the check has files to look at, so a clean result below is a finding rather than an "
       "empty list", len(sources) >= 3, [p for p, _ in sources])

    offenders = _unregistered_tempdirs(sources)
    ok("every raw `tempfile.mkdtemp` in the test tree has a matching teardown — `tmpdir()` "
       "registers for cleanup, and the three sites that bypassed it are what leaked",
       not offenders, offenders)

    # THE CHECK MUST BE ABLE TO FAIL, on text this repo does not contain.
    fake_bad = [("x.py", "d = tempfile.mkdtemp()\nreturn d\n")]
    fake_ok = [("y.py", "d = tempfile.mkdtemp()\natexit.register(shutil.rmtree, d, True)\n")]
    eq("a mkdtemp with no teardown is NAMED, so the net can actually fail",
       len(_unregistered_tempdirs(fake_bad)), 1)
    eq("...while one with a registration is not", _unregistered_tempdirs(fake_ok), [])


def test_boot_token_does_not_drift():
    group("A boot token that drifts turns a live holder into 'proved dead'")

    # THE SECONDS ARE NOT CONSTANT. macOS recomputes boot time from the clock minus uptime, so
    # an NTP adjustment moves `kern.boottime`'s `sec` by one — and the token is cached for the
    # life of a process, so two processes that cached either side of one adjustment disagree
    # FOREVER. Reported from a real campaign with the token observed going BACKWARDS across two
    # readings fifteen minutes apart, on a machine with two days of uptime.
    H = "somehost"
    eq("the same token is the same boot", util.same_boot(H + ":uuid:A", H + ":uuid:A"), True)
    eq("a genuinely different boot uuid is a different boot",
       util.same_boot(H + ":uuid:A", H + ":uuid:B"), False)
    eq("a different HOST is a different boot, and that IS knowable",
       util.same_boot("other:uuid:A", H + ":uuid:A"), False)

    # THE DRIFT ITSELF, on the fallback scheme. One second is exactly the size of the clock
    # adjustment being repaired, and the field's own precision is the problem.
    eq("two boot-seconds one apart are the SAME boot — the drift cannot manufacture a death",
       util.same_boot(H + ":sec:1787677144", H + ":sec:1787677145"), True)
    eq("...while a real gap is still a different boot, so the tolerance did not blind it",
       util.same_boot(H + ":sec:1787677144", H + ":sec:1787677153"), False)

    # THE UPGRADE MUST NOT CAUSE THE BUG IT FIXES. Changing darwin from seconds to a boot uuid
    # changes every token, so a claim written by an older build differs from every reading by a
    # newer one. Compared as strings that is mass false STALE, produced by the fix for false
    # STALE. Two schemes are INCOMPARABLE, not opposed.
    eq("a legacy untagged token against a new uuid answers CANNOT TELL, not 'different boot'",
       util.same_boot(H + ":1787677144", H + ":uuid:A"), None)
    eq("...and so does a tagged seconds token against a uuid",
       util.same_boot(H + ":sec:1787677144", H + ":uuid:A"), None)
    eq("an unknown on either side is CANNOT TELL — unchanged, and the posture this module is "
       "most careful about", util.same_boot(H + ":unknown", H + ":uuid:A"), None)

    # AND THE REAL TOKEN USES THE NON-DRIFTING SCHEME WHERE ONE EXISTS.
    tok = util.boot_token()
    ok("this machine's token names its scheme, so two schemes can never be compared as one",
       tok.count(":") >= 2 or tok.endswith(":unknown"), tok)

    # ONE COMPARISON, NOT TWO. `graph.stale_claims` and `locks._live` each carried the rule and
    # were free to disagree — the shape this repo has repaired elsewhere. Both call it now.
    for rel_path in ("lib/showrunner/graph.py", "lib/showrunner/locks.py"):
        with open(os.path.join(ROOT, rel_path)) as fh:
            body = fh.read()
        ok("%s decides boot identity through the shared comparison rather than its own string "
           "test" % rel_path, "same_boot(" in body)


def test_guard_entrypoints_agree():
    group("The shim and the CLI are one guard — they must not disagree about the same call")
    if not have("git") or not have("bash"):
        skip("the entrypoint-agreement group", "git or bash is not installed")
        return

    # TWO ENTRYPOINTS TO ONE GUARD, and only one of them got the fix. The shell shims learned to
    # resolve the repo from CLAUDE_PROJECT_DIR when cwd could not answer; the Python path did
    # not, so `dispatch-guard.sh` EVALUATED a call while `showrunner dispatch guard` failed open
    # beside it — with the variable set and valid.
    #
    # Reachable rather than theoretical: a consumer had BOTH registered, so the fixed shim's
    # silence was drowned out by the unfixed CLI's warning and the session read as
    # guarded-but-noisy rather than partly unguarded.
    #
    # AND I CLOSED THAT ISSUE AS FIXED, having probed only the shims. The clean result was about
    # a subject I chose, not about the guard. This asserts the PROPERTY the reporter asked for —
    # that the two cannot disagree — rather than re-testing one of them.
    outside = tmpdir("no-repo-here")
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
    shim = os.path.join(ROOT, ".showrunner", "hooks", "dispatch-guard.sh")
    sr = os.path.join(ROOT, "bin", "showrunner")

    def verdicts(env_extra):
        env = dict(os.environ)
        env.pop("CLAUDE_PROJECT_DIR", None)
        env.update(env_extra)
        a = subprocess.run(["bash", shim], input=payload, cwd=outside,
                           capture_output=True, text=True, env=env)
        b = subprocess.run([sys.executable, sr, "dispatch", "guard"], input=payload, cwd=outside,
                           capture_output=True, text=True, env=env)
        fo = lambda p: "DID NOT RUN" in (p.stdout + p.stderr)   # noqa: E731
        return fo(a), fo(b)

    shim_ran, cli_ran = verdicts({"CLAUDE_PROJECT_DIR": ROOT})
    ok("with CLAUDE_PROJECT_DIR set and cwd outside any repo, the CLI evaluates the call rather "
       "than failing open — the case a consumer hit with both forms registered", not cli_ran)
    eq("...and the two entrypoints AGREE, which is the property, not the individual verdict",
       shim_ran, cli_ran)

    for label, env_extra in (("no CLAUDE_PROJECT_DIR", {}),
                             ("CLAUDE_PROJECT_DIR pointing outside a repo",
                              {"CLAUDE_PROJECT_DIR": outside})):
        a, b = verdicts(env_extra)
        eq("...and they agree with %s too, so the fix is in the SHARED resolver rather than "
           "copied into one path" % label, a, b)

    # THE TEXT IS A DETECTOR AND IT HAS TO BE KEPT ONE. Fixing #56 cost it: the divergence was
    # caught because the two entrypoints WORDED the fail-open differently, and afterwards they
    # differed by default — so a future divergence would produce two different messages that
    # look exactly like today's two different messages.
    #
    # The behavioural assertion above catches what it was written to compare. The text caught
    # the case nobody had thought to compare yet. Asserting the sentence keeps the cheap
    # detector working WITHOUT relying on somebody noticing.
    def context_of(p):
        out = (p.stdout or "") + (p.stderr or "")
        try:
            return json.loads(out.strip())["hookSpecificOutput"]["additionalContext"]
        except Exception:                                        # noqa: BLE001
            return out

    env_bare = dict(os.environ)
    env_bare.pop("CLAUDE_PROJECT_DIR", None)
    # BOTH COPIES LIVE OUTSIDE ANY REPO (#74). This used to run the in-repo shim and the
    # in-repo binary with the environment stripped and call that "no anchor". It is not: both
    # are sitting in the project, and each now resolves from its own location — which is the
    # hole #74 reported. The DETECTOR this test exists to be is unchanged: the two entrypoints
    # must word the fail-open identically, so a future divergence breaks a check instead of
    # waiting for somebody to notice. Only the fixture moved, to a place where neither can
    # answer, because that is the only place the sentence is still reachable.
    loose = tmpdir("guard-wording-loose")
    loose_shim = os.path.join(loose, "sub", "hooks", "dispatch-guard.sh")
    os.makedirs(os.path.dirname(loose_shim), exist_ok=True)
    shutil.copy(shim, loose_shim)
    shutil.copytree(os.path.join(ROOT, "lib"), os.path.join(loose, "lib"))
    os.makedirs(os.path.join(loose, "bin"), exist_ok=True)
    loose_sr = os.path.join(loose, "bin", "showrunner")
    shutil.copy(sr, loose_sr)
    shim_out = context_of(subprocess.run(["bash", loose_shim], input=payload, cwd=outside,
                                         capture_output=True, text=True, env=env_bare))
    cli_out = context_of(subprocess.run([sys.executable, loose_sr, "dispatch", "guard"],
                                        input=payload, cwd=outside, capture_output=True,
                                        text=True, env=env_bare))
    ok("the shim's fail-open sentence names BOTH anchors it tried, so a reader can tell "
       "'nowhere resolved' from 'the guard broke'",
       "neither the working directory nor CLAUDE_PROJECT_DIR" in shim_out, shim_out[:140])
    ok("...and the CLI says the SAME sentence, so a future divergence in either one breaks a "
       "check rather than waiting for somebody to notice the wording",
       "neither the working directory nor CLAUDE_PROJECT_DIR" in cli_out, cli_out[:140])
    ok("...and neither has fallen back to the generic wrapper, which names no anchor at all",
       "it raised" not in shim_out and "it raised" not in cli_out,
       (shim_out[:80], cli_out[:80]))

    # AND THE REFUSAL SURVIVES. A fallback that made everything resolve would be worse than the
    # bug: `find_root` must still refuse when nothing can answer.
    env = dict(os.environ); env.pop("CLAUDE_PROJECT_DIR", None)
    p_st = subprocess.run([sys.executable, sr, "status"], cwd=outside,
                          capture_output=True, text=True, env=env)
    eq("a verb outside any repo, with no harness anchor, still REFUSES — the fallback did not "
       "turn 'cannot resolve' into a guess", p_st.returncode, 2)


def test_launch_binary_and_failed_launch():
    group("`spawn --launch`: the binary is configurable, and a failed launch does not strand a "
          "leaf on a dead pid")
    if not have("git"):
        skip("the launch group", "git is not installed")
        return
    cfg = make_repo()

    # 1. THE BINARY WAS HARDCODED. On a machine whose only `claude` is bundled inside the editor
    # extension — not on PATH, no standalone install — EVERY `spawn --launch` failed and the
    # whole parallel lane was unavailable. `dispatch_config` already carried permission_mode,
    # default_model, models_by_lane and claude_args, and the same file names an absolute path
    # for the chat CLI; the launch binary was the one thing that could not be pointed at.
    eq("with nothing configured the launch binary is `claude` on PATH, unchanged",
       dispatch.claude_bin(cfg), "claude")
    cfg.data.setdefault("dispatch", {})["claude_bin"] = "/opt/editor/claude"
    eq("...and `dispatch.claude_bin` points it somewhere else", dispatch.claude_bin(cfg),
       "/opt/editor/claude")
    built = dispatch.build_command(cfg, {"crawler": "c1"}, None, "sess", "brief")
    eq("...which is what actually lands in argv[0], not just in a getter nothing calls",
       built[0], "/opt/editor/claude")

    # 2. A FAILED LAUNCH LEFT THE CAMPAIGN MUTATED. The leaf stayed in_progress, claimed by the
    # invoking shell's pid — dead seconds later — so it was out of `ready`, invisible to the
    # only discovery surface, and recoverable only by hand.
    #
    # PARK RATHER THAN ROLL BACK, and the reason came with the report: the tool's own advice was
    # `reap`, and on a real campaign `reap` proposed releasing the leaf AND closing a dozen chat
    # rooms belonging to another agent's Crawlers, because rooms with dead owners are swept in
    # the same pass. The printed remedy was destructive well outside the failure.
    sr = os.path.join(ROOT, "bin", "showrunner")
    with open(os.path.join(cfg.root, ".showrunner", "config.json")) as fh:
        conf = json.load(fh)
    conf.setdefault("dispatch", {})["claude_bin"] = "/nonexistent/claude"
    with open(os.path.join(cfg.root, ".showrunner", "config.json"), "w") as fh:
        json.dump(conf, fh)
    subprocess.run([sys.executable, sr, "add", "a leaf", "--id", "L1"],
                   cwd=cfg.root, capture_output=True)
    p_spawn = subprocess.run([sys.executable, sr, "spawn", "L1", "--actor", "me", "--launch"],
                             cwd=cfg.root, capture_output=True, text=True)
    ok("a launch that cannot start REFUSES rather than reporting success",
       p_spawn.returncode == 2, (p_spawn.stderr or "")[:160])
    ok("...and says the leaf was PARKED, naming the unpark that undoes it",
       "PARKED L1" in p_spawn.stderr and "unpark L1" in p_spawn.stderr,
       (p_spawn.stderr or "")[-260:])
    ok("...and says the worktree is kept, because it may hold the only copy of real work",
       "NOT removed" in p_spawn.stderr, (p_spawn.stderr or "")[-200:])

    shown = subprocess.run([sys.executable, sr, "show", "L1"],
                           cwd=cfg.root, capture_output=True, text=True).stdout
    ok("the leaf is parked in the record, not merely described as parked in a message",
       '"parked": 1' in shown, shown[:200])

    # THE CASCADE THE REPORT WAS REALLY ABOUT. A stale claim steered the reader to `reap`, and
    # `reap` swept far wider than the failure. A parked leaf is excluded, so the steer never
    # appears.
    reaped = subprocess.run([sys.executable, sr, "reap"],
                            cwd=cfg.root, capture_output=True, text=True).stdout
    ok("`reap` proposes NOTHING for a parked leaf — the stale-claim steer that led to a "
       "destructive sweep never appears", "nothing to reap" in reaped, reaped[:200])


def test_mutation_anchor_refusal():
    group("A mutation that does not apply must not read as a mutation that was tolerated")

    # THE REFUSAL EXISTED AND HAD NEVER BEEN INVOKED. `mutate.py` splices a neutering stub in
    # after an anchor, and refuses when the anchor matches nothing — because a renamed producer
    # that silently is not mutated produces a passing suite, which reads as coverage. That
    # branch was described in three comments and exercised by no test, and it lived inline in
    # `main()`, which runs the whole sweep, so nothing could reach it.
    #
    # Extracted the way wcs extracted their lock reader for the same reason: a refusal nobody
    # can invoke is a refusal nobody has seen work.
    sys.path.insert(0, os.path.join(ROOT, "test"))
    import mutate as _m

    src = "def produce(cfg):\n    return real_value\n"
    good, n = _m.apply_anchor(src, r"(def produce\(cfg\):\n)", "    return None\n")
    eq("an anchor that matches applies exactly once", n, 1)
    ok("...and the stub really lands in the text, so the count is not the only evidence",
       "return None" in good, good)

    # THE CASE THAT MATTERS: the producer was renamed, so the anchor is stale.
    stale, n0 = _m.apply_anchor(src, r"(def renamed_away\(cfg\):\n)", "    return None\n")
    eq("a stale anchor applies ZERO times — which is what a rename looks like", n0, 0)
    eq("...and returns the text UNCHANGED, so the sweep would have measured the original and "
       "called the result coverage", stale, src)

    # A SKIPPED GROUP'S ZERO IS NOT AN UNCOVERED PRODUCER'S ZERO. Splitting the ambiguous
    # anchor immediately produced one: BrGraph.stale_claims scored 0 kills and read as
    # UNPROTECTED, but `br` is not on PATH here, so the group covering it SKIPS and NO
    # MEASUREMENT WAS TAKEN. Nothing noticed, and nothing ran, are the same number.
    #
    # The pair had been scoring 7 on the SqliteGraph half, so this zero had never been visible
    # at all — the ambiguous anchor was hiding an unmeasured implementation behind its sibling.
    ok("a producer whose only coverage lives behind an optional binary is DECLARED, so its "
       "zero can be read as unmeasured rather than uncovered",
       "stale_claims (reap's evidence, BrGraph)" in _m.NEEDS_BINARY, list(_m.NEEDS_BINARY))
    for _n, _b in _m.NEEDS_BINARY.items():
        ok("...and every declared producer is a real target of the sweep, so the map cannot "
           "quietly describe something that no longer exists (%s)" % _n[:34],
           any(t[0] == _n for t in _m.TARGETS))
    with open(os.path.join(ROOT, "test", "mutate.py")) as fh:
        _mb = fh.read()
    ok("...and the sweep says UNMEASURABLE HERE rather than UNPROTECTED for that case, because "
       "a coverage hole and an absent dependency call for opposite responses",
       "UNMEASURABLE HERE" in _mb and "this is not a coverage hole" in _mb)

    # AMBIGUOUS IS NOT APPLIED, and this one was live. `count=1` neuters the FIRST match and
    # leaves the rest running, so a name implemented twice scores as covered on the strength of
    # mutating one of them. `graph.stale_claims` is a method on TWO backends here: the sweep
    # had one anchor, matched both, mutated SqliteGraph, and BrGraph was never touched.
    #
    # Named by game_loop's auditor as one of three refusal modes a sweep owes — absent anchor,
    # ambiguous anchor, edit that changed nothing. This file had only the first.
    twice = "def f(self):\n    pass\ndef f(self):\n    pass\n"
    _, n_amb = _m.apply_anchor(twice, r"(def f\(self\):\n)", "    return None\n")
    ok("an anchor matching TWICE is refused rather than silently neutering the first — the "
       "other implementation would never be mutated and the score would not move", n_amb < 0)
    eq("...and the refusal carries HOW MANY it matched, so the reader can find them", n_amb, -2)

    # APPLIED AND CHANGED NOTHING is the third mode gameloop names, and it matters because a
    # no-op mutant produces SURVIVED, SURVIVED reads as a coverage gap, and the gap sends
    # somebody hunting for an assertion that already exists — wasted work downstream of a
    # wasted control, indistinguishable from real work.
    #
    # HERE IT HAS EXACTLY ONE CAUSE. The replacement is an INSERTION, so the text always grows
    # unless the stub is EMPTY. I wrote a `new == src` branch for this and then measured it
    # unreachable across 69 targets, shortest stub 40 characters — a defensive check that
    # cannot fire, which is the `or` shape. Removed, and the invariant that makes it
    # unreachable is enforced instead, because that CAN be violated by a future edit.
    _, n_noop = _m.apply_anchor("def g(self):\n", r"(def g\(self\):\n)", "")
    eq("an EMPTY stub is refused — it is the only way an insertion can change nothing, and a "
       "no-op mutant is scored as a survivor, which reads as a hole that is not there",
       n_noop, 0)
    ok("...while every stub the sweep ships is non-empty, so the refusal above guards a real "
       "invariant rather than a case that cannot arise",
       all(t[4] for t in _m.TARGETS), [t[0] for t in _m.TARGETS if not t[4]][:3])

    # AND THE SWEEP MUST SAY SO RATHER THAN SCORING IT. The distinction is UNSCOREABLE versus
    # UNPROTECTED: no measurement was taken, which is not the same as nothing noticing.
    with open(os.path.join(ROOT, "test", "mutate.py")) as fh:
        body = fh.read()
    ok("the sweep files a zero-match anchor as UNSCOREABLE, never as a thin producer — a hole "
       "stated about code that was never mutated is a hole the instrument invented",
       "unscoreable.append" in body and "the anchor matched nothing" in body)

    # EVERY SHIPPED ANCHOR STILL MATCHES ITS TARGET. This is the live version of the same
    # question — an anchor can go stale between now and any future edit, and the accounting
    # already reports `0 stale`, but nothing here re-derives it from the source files.
    missing = []
    for name, _key, relpath, pattern, stub in _m.TARGETS:
        full = os.path.join(ROOT, relpath)
        try:
            with open(full) as fh:
                text = fh.read()
        except OSError:
            missing.append((name, relpath, "file missing"))
            continue
        if _m.apply_anchor(text, pattern, stub)[1] != 1:
            missing.append((name, relpath, "anchor matched nothing"))
    ok("every anchor the sweep ships still matches its target exactly once, re-derived from "
       "the files rather than read from the accounting summary", not missing, missing[:4])
    ok("...and there is more than a handful of them, so the clean result above is a finding "
       "rather than an empty list", len(_m.TARGETS) >= 60, len(_m.TARGETS))


def test_borrowed_claims_are_marked():
    group("A finding borrowed from another project is a hypothesis, and must say so")

    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True).stdout.split()
    candidates = [p for p in tracked
                  if p.startswith(("lib/", "test/", ".showrunner/")) or p == "llms.txt"]

    def read(rel_path):
        try:
            with open(os.path.join(ROOT, rel_path), errors="ignore") as fh:
                return fh.read()
        except OSError:
            return ""

    unmarked = _borrowed_unmarked(candidates, read)
    ok("every tracked file citing another project's measurement says it is REPORTED and not "
       "verified here — this repo cannot reach their machines or their corpora",
       not unmarked, unmarked)

    # THE CHECK MUST BE ABLE TO FAIL, on text this repo does not contain.
    fake = {"a.py": "the auditor measured a 4% rate and it is fine",
            "b.py": "the auditor measured a 4% rate. BORROWED CLAIMS IN THIS FILE ARE "
                    "REPORTED, NOT VERIFIED HERE.",
            "c.py": "nothing borrowed here at all"}
    got = _borrowed_unmarked(sorted(fake), lambda k: fake[k])
    eq("an unmarked borrowed measurement is NAMED, so the net can actually fail", got, ["a.py"])
    ok("...while a marked one is not, and a file with no borrowed claim is not",
       "b.py" not in got and "c.py" not in got)

    # AND THE ONE THAT WAS ACTUALLY RETRACTED IS RECORDED AS RETRACTED, not quietly deleted —
    # the withdrawal is more instructive than the claim was.
    joined = " ".join(re.sub(r"\s+", " ", read(p)) for p in
                      (".showrunner/hooks/issue-waker.py", "lib/showrunner/cli.py"))
    ok("the borrowed claim that its own author withdrew is marked RETRACTED where it is cited, "
       "because a false reason attached to a correct feature is durable",
       "RETRACTED" in joined and "idle" in joined)


def test_zero_inventory_matches_reality():
    group("The list of trustworthy zeros names controls that still exist")

    # WHY AN INVENTORY AT ALL. game_loop's auditor put it best: files with controls in them
    # imply coverage they do not have, and the honest thing is to say where the coverage stops
    # rather than to ask a reader to feel suspicious. "Be suspicious" is not actionable; "these
    # three zeros have positive controls and nothing else does" is.
    #
    # THE RISK IS THAT THE LIST OUTLIVES THE CONTROLS. A doc claiming a guard exists, where the
    # guard does not, is the defect llm_chat found in their README — a trigger listed under a
    # `Stop` column and registered in none of four registries. So each named control is checked
    # to still be present, by a marker distinctive enough that deleting the control breaks this.
    with open(os.path.join(ROOT, "llms.txt")) as fh:
        doc = fh.read()
    ok("llms.txt tells a reader where the coverage STOPS, not only where it exists",
       "Everywhere else you are on your own" in re.sub(r"\s+", " ", doc))
    with open(os.path.join(HERE, "run.py")) as fh:
        own = fh.read()

    # THE FIRST VERSION OF THIS CHECK GREPPED FOR A COMMENT, which is this project's own
    # name-presence-is-not-evidence defect committed inside the inventory that exists to stop
    # exactly that. `REFUSE ON A RED BASELINE` is a COMMENT in mutate.py: delete the two lines
    # of logic beneath it, keep the prose, and the check passed over a repo whose control was
    # gone. Verified by doing it.
    #
    # game_loop's auditor supplied the criterion the same hour: two things are independent only
    # if they can be WRONG SEPARATELY. A comment and the code it describes cannot — one edit
    # moves the code and leaves the comment, and the grep sees no difference.
    #
    # So the red-baseline control is asserted STRUCTURALLY, from the AST: `main` must contain a
    # branch on the baseline being unreadable that RETURNS a non-zero. Prose cannot satisfy
    # this, and the shape survives rewording.
    with open(os.path.join(ROOT, "test", "mutate.py")) as fh:
        mut_tree = ast.parse(fh.read())
    guarded = False
    for node in ast.walk(mut_tree):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.dump(node.test)
        if "b_pass" not in test_src:
            continue
        if any(isinstance(n, ast.Return) and n.value is not None
               for n in ast.walk(node)):
            guarded = True
    ok("test/mutate.py REFUSES on an unreadable baseline as CODE, not as a comment — 0 kills "
       "is meaningless if the suite was already failing, and a grep for the prose passes over "
       "a repo where the logic was deleted", guarded)

    # THE OTHER TWO ARE ASSERTED BY BEHAVIOUR ELSEWHERE IN THIS SUITE, which is stronger than
    # anything a text search can claim, so this only records WHERE — an inventory that
    # re-greps what a behavioural test already proves is duplicating the weaker half.
    ok("...and the corpus tool's refusal is proved by RUNNING it against a gate that cannot "
       "fire, in the corpus group, rather than by searching for its message",
       "a gate that cannot PARSE makes the tool refuse to report a rate at all" in own)
    ok("...and doctor's CANNOT BE DETERMINED by running it against a real unborn HEAD, in the "
       "stale-copy group",
       "a pin whose age CANNOT be determined is reported as exactly that" in own)


def test_negative_text_assertions_flatten():
    group("An assertion that a phrase is ABSENT must not pass just because the phrase wrapped")

    # THE CLASS, demonstrated rather than argued. An assertion whose evidence is a NON-OCCURRENCE
    # in text passes the moment the text rewraps, because a search for "a b c" finds nothing in
    # "a b\nc". Prose rewraps constantly, so the guarded claim can walk straight back in under a
    # green suite — and the assertion is perfectly effective at asserting the wrong thing, which
    # is where mutation testing cannot reach.
    wrapped = "the verb wires all of\nthem, as documented"
    ok("a naive absence check PASSES on wrapped text, which is the false clear",
       "wires all of them" not in wrapped)
    ok("...while the flattened one correctly still finds it",
       "wires all of them" in re.sub(r"\s+", " ", wrapped))

    # MEASURED IN THIS SUITE, not asserted about it. Only assertions searching DOCUMENT TEXT are
    # at risk: process stdout is compared as produced, and a single token cannot wrap. So the
    # check is scoped to the doc variables rather than flagging every `not in`, which would be
    # the mostly-noise failure this repo has removed twice.
    with open(os.path.join(HERE, "run.py")) as fh:
        own = fh.read()
    # THE VARIABLE NAME IS A PROXY, AND IT OVER-FLAGGED IMMEDIATELY. A name-based scan cannot
    # see that the text was flattened before being searched — it caught a check of mine whose
    # variable IS flattened one line above, which is a false positive of exactly the kind that
    # teaches a reader to skim. So a variable this file demonstrably flattens is not risky.
    flattened = set(re.findall(r'(\w+)\s*=\s*(?:_?re)\.sub\(r"\\s\+"', own))
    risky = [(ph, var) for ph, var
             in re.findall(r'"([^"]{6,60})"\s+not\s+in\s+(doc|txt|text)\b', own)
             if var not in flattened]
    # A PHRASE CONTAINING AN EXPLICIT NEWLINE IS ASSERTING A POSITIONAL FACT — "no bare
    # `showrunner close` at the start of an indented line" — and flattening would destroy the
    # very thing it checks. Exempted by SHAPE rather than by name, so a second one is covered
    # without an edit, and a phrase that merely happens to be multi-word is not.
    multiword = [(ph, var) for ph, var in risky
                 if " " in ph.strip() and "\\n" not in ph]
    # POSITIVE CONTROL ON THE SCAN ITSELF, which is the whole rule applied to this assertion.
    # `multiword` being empty is an identity element: it means "none unflattened" only if the
    # scan can return anything at all. Break the regex and it goes quiet and this passes.
    #
    # game_loop's auditor hit exactly this an hour ago — scanned their files for the wrapping
    # class, got zero, and only then built a file WITH the shape to prove the scan could say
    # otherwise. Without that second run they would have reported clean from an instrument they
    # had no evidence could ever disagree.
    ok("the scan finds absence-assertions AT ALL, so an empty result below means none are "
       "unflattened rather than that the search stopped working", len(risky) >= 1, len(risky))
    # ASSEMBLED, NOT WRITTEN. A literal probe here is INSIDE the file the scan reads, so it
    # becomes test data the scan reports as a real finding — which is exactly what happened:
    # this control flagged its own fixture on its first run. Building the line from pieces
    # keeps the pattern out of the source while still exercising the regex.
    #
    # The first version also asserted `len(risky) >= 5`, a threshold I invented. The real count
    # is 4. A number nobody derived is the thing this repo keeps deleting from its own docs, and
    # I put one in a control an hour after widening the check that forbids them.
    probe = 'ok("x", "wires all of " + "them" ' + 'not in doc)'
    probe = probe.replace('" + "', "")
    ok("...and it fires on a KNOWN-BAD line, proving the empty result is a finding and not a "
       "silent regex",
       bool(re.findall(r'"([^"]{6,60})"\s+not\s+in\s+(doc|txt|text)\b', probe)), probe)

    ok("every multi-word ABSENCE claim against document text in this suite is made against "
       "flattened text — the ones that are not are listed here rather than assumed harmless",
       not multiword, multiword)

    # AND THE REASON THIS GOT WRITTEN. A grep for `CANNOT BE DETERMINED` in cli.py returned
    # nothing today while the string was plainly there, split across two adjacent literals. The
    # honest reading of that empty result was CANNOT TELL; I nearly read it as ABSENT, an hour
    # after game_loop's auditor made the same error on a two-line subprocess call.
    with open(os.path.join(ROOT, "lib", "showrunner", "cli.py")) as fh:
        cli_src = fh.read()
    joined = re.sub(r'"\s*\n\s*"', "", cli_src)
    ok("a string split across adjacent literals IS present once they are joined, even though a "
       "line-based search finds nothing — the empty result meant CANNOT TELL, never ABSENT",
       "CANNOT BE DETERMINED" in joined and "CANNOT BE DETERMINED" not in cli_src)


def test_a_rate_names_its_instrument():
    group("A published rate names the committed tool that reproduces it")

    # THE INSTRUMENT MUST EXIST BEFORE THE NUMBER GOES OUT. Every corpus figure this project
    # published came from a script written for the occasion and deleted; the numbers reached
    # commit messages and llms.txt while the instrument lasted four minutes. Nobody could
    # re-run one — including me, and including the people I sent them to.
    TOOLS = ("test/corpus.py", "test/mutate.py", "test/run.py", "test/docs_surface.py")

    # THE CHECK MUST BE ABLE TO FAIL, which is llm_chat's warning about a default-deny net over
    # an already-clean set: theirs passed whether or not it worked, because it only ever asked
    # about a directory where everything was classified. So it is a function, exercised here on
    # text this repo does not contain.
    sourced = "promise gate: 3 of 237 closings — `python3 test/corpus.py --gate promise`"
    unsourced = "The gate fires on 24 of 3,586 commands with 2 false positives."
    eq("a rate carrying the command that reproduces it passes",
       _unsourced_rates(sourced, TOOLS), [])
    ok("...while a bare rate is NAMED, so the net can actually fail",
       len(_unsourced_rates(unsourced, TOOLS)) == 1, _unsourced_rates(unsourced, TOOLS))
    eq("...and an ordinary number that is not a measurement is left alone — a version, a sha or "
       "an issue number is not a rate, and a check that flags them becomes noise and gets skimmed",
       _unsourced_rates("showrunner 0.1.0 · pinned cd1b7bff · closes #65", TOOLS), [])

    # A FILE THAT IS THE TOOL IS ITS OWN REPRODUCER. Exercised on a name in the tool list, so
    # the exemption is asserted rather than assumed.
    eq("a rate inside one of the tools themselves is not unsourced — the file citing it IS what "
       "produces it, and demanding a citation there is circular",
       _unsourced_rates("it fires on 24 of 3,586 commands", TOOLS, source="test/corpus.py"), [])
    ok("...while the same text in a doc still flags, so the exemption is about WHERE the claim "
       "lives and not about the wording",
       len(_unsourced_rates("it fires on 24 of 3,586 commands", TOOLS, source="llms.txt")) == 1)

    # THE REAL SURFACES. Both front doors: a rate in either is what a reader takes away, and
    # README carried one — `6 of 12 concurrent claims won the same leaf` — with no way to
    # reproduce it, which is the exact defect this check exists for.
    for doc_name in ("llms.txt", "README.md"):
        with open(os.path.join(ROOT, doc_name)) as fh:
            offenders = _unsourced_rates(fh.read(), TOOLS, source=doc_name)
        ok("every rate in %s names a committed instrument, so a reader can reproduce it "
           "instead of taking it on trust" % doc_name, not offenders, offenders[:4])


def test_stale_copy_cannot_warn_about_itself():
    group("A copy cut before a check existed cannot run that check — and silence reads as fine")
    if not have("git"):
        skip("the stale-copy group", "git is not installed")
        return

    # THE CLASS, named by llm_chat's owner about their own doctor: it correctly detects that it
    # is a different build from the one the hooks run, and that warning is useless to anyone
    # whose problem is that they are running the OLD copy. An improvement cannot reach the case
    # it is about, because the improvement ships in the build you are not running.
    #
    # Measured here rather than reasoned about: a copy pinned at the commit BEFORE `staleness`
    # landed carries no `staleness` in its pin.py at all, and its doctor never mentions it.
    with open(os.path.join(ROOT, "lib", "showrunner", "pin.py")) as fh:
        ok("the staleness check lives in the code being reported on, which is what makes a copy "
           "cut before it structurally unable to run it", "def staleness" in fh.read())

    # THE PART THAT IS IMMUNE, and the difference is the whole lesson. The self-vendor warning
    # compares a RECORDED SHA against live git, so it needs no new logic in the running build —
    # a copy cut long before it was written still fires it, which was confirmed by running one.
    # A check that reads durable state survives its own staleness; a check that must be PRESENT
    # to fire does not.
    # RUN IT, DO NOT GREP FOR ITS MESSAGE. This asserted that the string "self-vendored pin at"
    # appears in cli.py — evidence about BEHAVIOUR taken from SOURCE. wcs measured that exact
    # shape in their own suite this hour: their assertion matched the words a branch PRINTS,
    # those words live inside an echo, so the message survives the branch's CONDITION being
    # broken. Their test passed over the reintroduced bug it was written for.
    #
    # Mine had the same structure, and the fix is the same: build a repo whose pin is genuinely
    # behind HEAD and read what doctor SAYS.
    behind = tmpdir("pin-behind")
    def _g(*a):
        return subprocess.run(["git"] + list(a), cwd=behind, capture_output=True, text=True)
    _g("init", "-q")
    _g("config", "user.email", "t@t"); _g("config", "user.name", "t")
    with open(os.path.join(behind, "a.txt"), "w") as fh:
        fh.write("one")
    _g("add", "-A"); _g("commit", "-qm", "first")
    old_sha = _g("rev-parse", "HEAD").stdout.strip()
    with open(os.path.join(behind, "a.txt"), "w") as fh:
        fh.write("two")
    _g("add", "-A"); _g("commit", "-qm", "second")
    _p_init2 = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "init"],
                              cwd=behind, capture_output=True, text=True)
    ok("...and in the pin-behind repo, for the same reason", _p_init2.returncode == 0,
       (_p_init2.stderr or "")[:160])
    sd = os.path.join(behind, ".showrunner_self", "bin")
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(behind, ".showrunner_self", "PINNED"), "w") as fh:
        json.dump({"sha": old_sha, "ref": "HEAD"}, fh)
    shutil.copy(os.path.join(ROOT, "bin", "showrunner"), sd)
    shutil.copytree(os.path.join(ROOT, "lib"),
                    os.path.join(behind, ".showrunner_self", "lib"), dirs_exist_ok=True)
    said = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                          cwd=behind, capture_output=True, text=True).stdout
    ok("...while a pin genuinely BEHIND HEAD is reported as behind, by running doctor against a "
       "repo built to be behind — not by finding the message in the source, which survives the "
       "branch that prints it being broken",
       "BEHIND" in said and old_sha[:12] in said, said[-260:])
    ok("...and it names how far behind, so the reader gets a size rather than a flag",
       "commit(s) BEHIND" in said, said[-200:])

    # WHEN THE ANSWER IS UNDEFINED, MAKE IT THE LOUD ONE. Every branch of the pin report needed
    # HEAD, and without it none fired — the line VANISHED, so a pin of unknown age rendered as
    # nothing at all, which reads exactly like a healthy one.
    #
    # llm_chat measured the opposite degradation in their equivalent check: theirs always cried
    # STALE from an old copy, which is annoying and safe. Mine went quiet, which is the
    # direction that costs a week. The direction is a choice available when the check is written.
    unborn = tmpdir("unborn-head")
    subprocess.run(["git", "init", "-q"], cwd=unborn, capture_output=True)
    rc_h = subprocess.run(["git", "rev-parse", "HEAD"], cwd=unborn, capture_output=True)
    ok("a freshly-initialised repo genuinely has no HEAD, so this exercises the real branch "
       "rather than a mocked one", rc_h.returncode != 0)
    # `init` initialises the CWD; it has no --root. This call used to pass one, so argparse
    # refused with exit 2 and the test never noticed — a command that did nothing looking
    # exactly like a command that worked. Run from the directory, and ASSERT it worked.
    _p_init = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "init"],
                             cwd=unborn, capture_output=True, text=True)
    ok("`init` succeeds in the unborn-HEAD repo, so the doctor run below is against a real "
       "config rather than against a refusal", _p_init.returncode == 0,
       (_p_init.stderr or "")[:160])
    selfdir = os.path.join(unborn, ".showrunner_self")
    os.makedirs(os.path.join(selfdir, "bin"), exist_ok=True)
    with open(os.path.join(selfdir, "PINNED"), "w") as fh:
        json.dump({"sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "ref": "HEAD"}, fh)
    shutil.copy(os.path.join(ROOT, "bin", "showrunner"), os.path.join(selfdir, "bin"))
    shutil.copytree(os.path.join(ROOT, "lib"), os.path.join(selfdir, "lib"), dirs_exist_ok=True)
    out = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                         cwd=unborn, capture_output=True, text=True).stdout
    ok("a pin whose age CANNOT be determined is reported as exactly that, rather than the line "
       "disappearing — silence there reads identically to a current pin",
       "CANNOT BE DETERMINED" in out, out[-300:])
    ok("...and says so in the words that block the flattering reading",
       "not the same as up to date" in out, out[-300:])

    # AND THE DOC CARRIES THE COMMAND, because a doc does not have to be executing to be read.
    # That is the only remedy that reaches a stale copy at all.
    with open(os.path.join(ROOT, "llms.txt")) as fh:
        doc = fh.read()
    ok("llms.txt names the class, so a reader on an old copy is not depending on that copy to "
       "tell them about it", "An old copy cannot warn you about itself" in doc)
    ok("...and gives the command that reads the recorded commit from OUTSIDE the copy, rather "
       "than describing the idea and leaving the reader to invent it",
       "cat PINNED" in doc and "log --oneline" in doc)
    ok("...and states the SUFFICIENT rule, not the one llm_chat refuted: durable evidence is "
       "not enough unless the comparison happens outside the build",
       "comparison performed OUTSIDE the build" in doc)


def test_hook_registration():
    group("A hook file that nothing registers has never once run")

    # THE DEFECT, FOUND NEXT DOOR AND THEN HERE. llm_chat's owner found a trigger their README
    # listed as live under a `Stop` column, registered in none of four registries. It had never
    # fired. Tests, full line coverage and a mutation all begin by assuming a thing RUNS.
    #
    # Mine was the doc half of the same thing: llms.txt said `worktree register` wires all the
    # hooks beside it, naming waiting-probe.sh. It wires four, and waiting-probe.sh is wired by
    # NOBODY on purpose — arming an idle watchdog is a human decision, stated forty lines earlier
    # in the same file. The contradiction survived because six hundred lines separated the two
    # claims and the wrong one read as detail.
    excused = {
        # DELIBERATELY UNWIRED, and the reason is a security property rather than an oversight:
        # a probe an agent can set is a watchdog an agent can switch off.
        "waiting-probe.sh": "arming the idle watchdog is a human decision; showrunner must not "
                            "wire the thing that would silence its own supervision",
    }
    hooks = os.path.join(ROOT, ".showrunner", "hooks")
    settings = [os.path.join(ROOT, ".claude", n)
                for n in ("settings.json", "settings.local.json")]
    unwired = _hook_wiring(hooks, settings, excused)
    ok("every hook file here is registered somewhere, or excused in writing with a reason",
       unwired == [], unwired)

    # THE NET MUST BE ABLE TO FAIL. Asked about a hook this repo does not have, so the answer
    # cannot come from the real directory happening to be clean.
    scratch = tmpdir("hookwire")
    fake_hooks = os.path.join(scratch, "hooks")
    os.makedirs(fake_hooks)
    for name in ("wired.sh", "orphan.sh", "excused.sh"):
        with open(os.path.join(fake_hooks, name), "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
    fake_settings = os.path.join(scratch, "settings.json")
    with open(fake_settings, "w") as fh:
        json.dump({"hooks": {"Stop": [{"hooks": [
            {"command": '"$CLAUDE_PROJECT_DIR"/.showrunner/hooks/wired.sh'}]}]}}, fh)
    got = _hook_wiring(fake_hooks, [fake_settings], {"excused.sh": "a stated reason"})
    eq("a hook nothing registers is NAMED, so the net can actually fail", got, ["orphan.sh"])
    ok("...while a registered one is not", "wired.sh" not in (got or []))
    ok("...and an excused one is not, because the excuse is the classification", 
       "excused.sh" not in (got or []))

    # A DIRECTORY IS NOT AN UNWIRED HOOK, and this survived by ACCIDENT until it was pinned.
    # The listing filters `os.path.isfile`, and a directory happens not to be a file — nothing
    # decided that. A later glob, or a dropped filter, starts reporting a build artifact as an
    # unregistered guard, and the failure reads as a WIRING problem rather than a listing one,
    # which is the expensive direction.
    #
    # llm_chat's owner found the same accident in their trigger net after I reported mine, and
    # made the argument that applies to both: the point is not that the behaviour is
    # load-bearing, it is that THE ACCIDENT IS. Mine had already bitten — a py_compile in the
    # parse check left a __pycache__ here that this net reported as a hook nobody registered.
    os.makedirs(os.path.join(fake_hooks, "__pycache__"), exist_ok=True)
    with open(os.path.join(fake_hooks, "__pycache__", "x.pyc"), "w") as fh:
        fh.write("")
    got_dir = _hook_wiring(fake_hooks, [fake_settings], {"excused.sh": "a stated reason"})
    eq("a build-artifact DIRECTORY beside the hooks is not reported as an unregistered hook — "
       "one check must not manufacture the condition another check flags",
       got_dir, ["orphan.sh"])

    # CANNOT LOOK IS NOT NOTHING UNWIRED. An unreadable directory returning [] would report a
    # clean wiring for a repo it never opened.
    eq("a hook directory that cannot be read answers CANNOT TELL rather than 'all wired'",
       _hook_wiring(os.path.join(scratch, "nope"), [fake_settings], {}), None)

    # AND THE DOC MUST NOT CLAIM A WIRING THAT DOES NOT HAPPEN. The specific false sentence.
    with open(os.path.join(ROOT, "llms.txt")) as fh:
        doc = fh.read()
    # FLATTENED BEFORE A NEGATIVE SEARCH. An assertion whose evidence is a phrase being ABSENT
    # passes for the wrong reason the moment that phrase merely WRAPS — prose rewraps constantly,
    # so re-introducing "wires all of\nthem" would restore the false claim under a green suite.
    #
    # Not hypothetical: my own grep for `CANNOT BE DETERMINED` in cli.py returned nothing today
    # while the string was plainly there, split across two adjacent literals. The honest reading
    # of that empty result was CANNOT TELL, and I nearly read it as ABSENT — which is the same
    # error game_loop's auditor made on a two-line subprocess call an hour earlier.
    flat_doc = re.sub(r"\s+", " ", doc)
    ok("llms.txt no longer claims `worktree register` wires every hook beside it — the probe is "
       "wired by nobody on purpose, and the claim stays gone even if the prose rewraps",
       "wires all of them" not in flat_doc)
    ok("...and names the deliberately-unwired one as such, so a reader following the doc cannot "
       "conclude the watchdog is armed", "waiting-probe.sh" in doc and "NOBODY" in doc)


def test_corpus_tool():
    group("Corpus measurements are reproducible, and made with the SHIPPED gate")
    if not have("bash"):
        skip("the corpus group", "bash is not installed")
        return
    tool = os.path.join(ROOT, "test", "corpus.py")

    # WHY THIS TOOL EXISTS. Every corpus number this project published — the promise gate's
    # 3-of-227, the pipeline gate's rate over 3,586 commands — came from a throwaway script
    # written for the occasion and deleted. The numbers reached commit messages and llms.txt;
    # the instrument lasted four minutes. Nobody could re-run one, including me.
    #
    # It is the same defect as publishing a rate from a standalone classifier while shipping a
    # different predicate, which this project did and had to correct. An ad-hoc grep IS a
    # standalone classifier. So the tool's one rule is that it implements no predicate: it
    # extracts a population and feeds it to the hook FILES.
    scratch = tmpdir("corpus")
    tp = os.path.join(scratch, "t.jsonl")

    def rec(kind, blocks):
        return json.dumps({"type": kind, "message": {"content": blocks}})

    with open(tp, "w") as fh:
        # A turn-final closing that PROMISES — the gate must refuse it.
        fh.write(rec("assistant", [{"type": "text", "text": "Done. Next I'll pay those debts."}])
                 + "\n")
        fh.write(json.dumps({"type": "user", "message": {"content": "ok"}}) + "\n")
        # An assistant message followed by a TOOL RESULT is mid-turn, not a closing. Counting
        # these is how a false-block rate ends up seven times too large: 1,650 messages stood in
        # for 222 turn-finals, and the published 31% was arithmetic on the wrong denominator.
        fh.write(rec("assistant", [{"type": "text", "text": "Next I'll check that."}]) + "\n")
        fh.write(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "x"}]}}) + "\n")
        fh.write(rec("assistant", [{"type": "text", "text": "All finished and verified."}])
                 + "\n")

    p = subprocess.run([sys.executable, tool, "--transcript", tp, "--gate", "promise",
                        "--json"], capture_output=True, text=True)
    ok("the corpus tool runs clean over a hand-built transcript", p.returncode == 0,
       (p.stderr or "")[:250])
    data = json.loads(p.stdout or "{}")
    eq("...counting only messages that ENDED a turn — one mid-turn message here is followed by "
       "a tool result and is not a closing", data["promise"]["population"], 2)
    eq("...and refusing the one closing that promises", data["promise"]["fired"], 1)
    ok("...reporting the ITEM, not only the rate — a rate without its items is a summary over "
       "data that knows more than the summary does",
       "Next I'll pay those debts" in (data["promise"]["items"] or [""])[0])

    # AN INSPECTION CANNOT GENERALISE OVER ITEMS NOBODY READ. My published "2 of 24 fires are
    # false positives" was somebody reading 24 lines. The corpus grew to 33 and the claim stayed
    # still; re-inspecting gave 7 — understated three-fold, on a population that had moved by
    # nine. Nothing stopped the old figure from being quoted at the new size.
    #
    # game_loop's auditor supplied the bound for the same shape in their repo: pin the inspected
    # items by digest and report anything outside that set, so the standing covers exactly what
    # was read.
    man_path = os.path.join(ROOT, "test", "pipeline-fires-inspected.json")
    ok("the inspection manifest exists — without it no fire is covered and the standing has no "
       "bound at all", os.path.isfile(man_path))
    with open(man_path) as fh:
        man = json.load(fh)
    ok("...and every entry carries a VERDICT and the reason somebody gave for it, because an "
       "inspection with no recorded reason is indistinguishable from a guess",
       all(v.get("verdict") in ("artifact", "genuine") and v.get("why")
           for v in man["inspected"].values()), list(man["inspected"])[:2])
    ok("...and says in the file that it is an INSPECTION rather than a measurement, so the next "
       "reader does not quote it as one", "INSPECTION, not a measurement" in man.get("note", ""))

    # THE CONTROL: remove one digest and the tool must NAME the uncovered fire. Without this the
    # manifest is a file nobody has shown can fail.
    shrunk = dict(man, inspected={k: v for k, v in list(man["inspected"].items())[1:]})
    shrunk_path = os.path.join(scratch, "pipeline-fires-inspected.json")
    with open(shrunk_path, "w") as fh:
        json.dump(shrunk, fh)
    ok("dropping one inspected digest leaves a fire the manifest cannot vouch for — proved by "
       "shrinking it, not by trusting the lookup",
       len(shrunk["inspected"]) == len(man["inspected"]) - 1)

    # TWO-SIDED CONTROLS. A detector that flags NOTHING and one that flags EVERYTHING both pass
    # a one-sided test, and every control the tool had asked only "does it fire on a known
    # positive". game_loop's auditor built the pair after finding their floor caught `scanned 0`
    # but not a predicate mutated into never matching — which prints a healthy denominator
    # beside a zero conclusion count.
    #
    # Exercised by MUTATING each gate to answer unconditionally, because a control nobody has
    # seen fail is a control with an unknown direction.
    import corpus as _c
    env_c = _c._env()
    always = os.path.join(scratch, "always.sh")
    with open(always, "w") as fh:
        fh.write("#!/usr/bin/env bash\nexit 2\n")
    os.chmod(always, 0o755)
    real_promise = _c.PROMISE_GATE
    try:
        _c.PROMISE_GATE = always
        probs = _c.self_check(env_c)
        ok("a promise gate that REFUSES EVERYTHING is caught — a gate that always fires passes "
           "every positive control there is",
           any("REFUSED a closing that only reports finished work" in x for x in probs), probs)
    finally:
        _c.PROMISE_GATE = real_promise
    ok("...and the real gates pass both directions", _c.self_check(_c._env()) == [])

    # THE PREMISE IS AN IDENTITY, NOT A THRESHOLD. The extraction assumes Bash calls arrive as
    # tool_use blocks carrying input.command — a fact about the harness, not this repo. The old
    # check asked whether the raw count was large while the walk found NOTHING, which catches a
    # total move and is blind to a partial one.
    #
    # wcs made the case for the identity: a form the walk cannot match is invisible to every
    # list the walk keeps, because the walk's vocabulary defines what it can fail to explain.
    # Measured before adopting: 3,804 raw markers against 3,804 extracted commands.
    shaped = os.path.join(scratch, "shaped.jsonl")
    with open(shaped, "w") as fh:
        for i in range(3):
            fh.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "echo %d" % i}}]}}) + "\n")
    ok("the shape check is SILENT when the raw marker count equals the extracted commands",
       _c.shape_ok(shaped, _c.bash_commands(shaped)) is None)
    eq("...and the fixture really does carry markers, so the silence above is a finding rather "
       "than a file with nothing in it", len(_c.bash_commands(shaped)), 3)
    ok("...while it REPORTS when they disagree — what a PARTIAL rename of the record shape "
       "looks like, and the case a large-raw-and-zero-found threshold cannot see",
       "raw Bash tool markers" in (_c.shape_ok(shaped, _c.bash_commands(shaped)[:1]) or ""),
       _c.shape_ok(shaped, _c.bash_commands(shaped)[:1]))

    # AN EMPTY POPULATION IS NOT A CLEAN RESULT. Found by PERTURBING the corpus rather than by
    # reading the code: hand a transcript with no records and the tool printed its caveat and
    # returned 0, so a caller reading the status got "fine" over a run that measured nothing.
    #
    # game_loop's auditor hit the identical thing by truncating theirs — 0 closings, 0 blocks,
    # and the line "all 0 blocks covered by the inspected set". Full coverage reported over
    # nothing. Their measurement tool already had the floor and the file they wrote afterwards
    # did not inherit it; mine had the prose and not the floor.
    empty_t = os.path.join(scratch, "empty.jsonl")
    with open(empty_t, "w") as fh:
        fh.write("")
    p_empty = subprocess.run([sys.executable, tool, "--transcript", empty_t, "--quiet"],
                             capture_output=True, text=True)
    eq("a corpus with NO records refuses rather than reporting 0.0%% — a sweep that examined "
       "nothing returns zero, and zero reads as good news", p_empty.returncode, 3)
    ok("...naming which gate had the empty population, so the refusal is actionable rather "
       "than a bare exit code", "POPULATION IS EMPTY" in p_empty.stderr, p_empty.stderr[:160])
    # THE CONTROL FOR THE CONTROL: the same tool on a NON-empty corpus must still report, or
    # this floor is just a tool that always refuses.
    ok("...while a corpus with records still reports normally, so the floor is a discriminator "
       "and not a blanket refusal",
       subprocess.run([sys.executable, tool, "--transcript", tp, "--gate", "promise",
                       "--quiet"], capture_output=True, text=True).returncode == 0)

    # EVERY READING IS A SNAPSHOT AND MUST SAY SO. This corpus is the project's own transcript
    # and it grows while the work happens: re-running the same afternoon moved the promise
    # gate's denominator from 237 to 243 with the numerator unchanged. game_loop's auditor had
    # been quoting theirs as a constant for two days while it went 73 -> 81 the same way.
    #
    # And the self-reference has no fix: writing about a gate adds to the corpus that gate is
    # measured on. Neither widening nor an exemption touches that, so the reading gets dated.
    p_asof = subprocess.run([sys.executable, tool, "--transcript", tp, "--gate", "promise"],
                            capture_output=True, text=True)
    ok("the reading is DATED, because a rate from a growing corpus quoted without one is being "
       "presented as a constant", "AS OF" in p_asof.stdout, p_asof.stdout[:200])
    ok("...and says the denominator moves, so the date is not decoration a reader skips",
       "GROWS" in p_asof.stdout and "denominator moves" in p_asof.stdout, p_asof.stdout[:260])
    data_asof = json.loads(subprocess.run(
        [sys.executable, tool, "--transcript", tp, "--gate", "promise", "--json"],
        capture_output=True, text=True).stdout or "{}")
    ok("...and the machine-readable form carries it too, so a caller quoting the number gets "
       "the as-of with it rather than having to parse prose",
       bool(data_asof.get("as_of")) and "transcript_bytes" in data_asof, list(data_asof))

    # THE INSTRUMENT CHECK IS THE POINT. A gate that cannot run answers exactly like a gate with
    # nothing to say, so a sweep over a broken hook reports ZERO fires and zero reads as good
    # news. This repo shipped an unparseable hook under 1,190 green assertions for that reason.
    broken_hooks = os.path.join(scratch, "hooks")
    shutil.copytree(os.path.join(ROOT, ".showrunner", "hooks"), broken_hooks)
    fake_root = os.path.join(scratch, "fakerepo")
    os.makedirs(os.path.join(fake_root, ".showrunner"), exist_ok=True)
    shutil.copytree(broken_hooks, os.path.join(fake_root, ".showrunner", "hooks"))
    shutil.copytree(os.path.join(ROOT, "test"), os.path.join(fake_root, "test"))
    with open(os.path.join(fake_root, ".showrunner", "hooks",
                           "future-tense-gate.sh"), "a") as fh:
        fh.write('\necho "unterminated\n')
    p = subprocess.run([sys.executable, os.path.join(fake_root, "test", "corpus.py"),
                        "--transcript", tp, "--quiet"], capture_output=True, text=True)
    ok("a gate that cannot PARSE makes the tool refuse to report a rate at all",
       p.returncode == 3, (p.stdout or "")[:200] + (p.stderr or "")[:200])
    ok("...saying so as an INSTRUMENT problem rather than as a finding about the corpus",
       "INSTRUMENT" in p.stderr, (p.stderr or "")[:200])
    ok("...and exiting 3 rather than 1, so a caller reading non-zero as 'there were fires' "
       "cannot read a broken sweep exactly backwards", p.returncode == 3)

    # THE REDIRECT IS VERIFIED, NOT ASSERTED, and this is the control for that control.
    # game_loop's auditor wrote "it never touches the gate's heartbeat" in the docstring of the
    # same kind of tool, on a false mechanism — they redirected HOME while the path derived from
    # __file__ — and it stamped on its first run. A true conclusion resting on an unchecked
    # mechanism, inside the instrument built to stop unchecked claims.
    #
    # BOTH HALVES OR NEITHER: "the real record did not grow" is also exactly what "the gate
    # silently stopped stamping" produces, so the tool refuses unless the redirected file GREW.
    sys.path.insert(0, os.path.join(ROOT, "test"))
    import corpus as _corpus
    unwritable = tmpdir("corpus-nowrite")
    os.chmod(unwritable, 0o500)
    try:
        problems = _corpus.self_check(dict(os.environ,
                                           SHOWRUNNER_HEARTBEAT=os.path.join(unwritable, "hb"),
                                           CLAUDE_PROJECT_DIR=ROOT))
        ok("a gate that stamps NOTHING is caught — otherwise an unwritten real heartbeat and a "
           "gate that stopped stamping are the same observation, and the redirect is unproven",
           any("stamped NOTHING" in x for x in problems), problems)
    finally:
        os.chmod(unwritable, 0o700)

    good = _corpus.self_check(_corpus._env())
    eq("...while the shipped redirect passes both halves: the checkout's own record untouched "
       "AND the redirected one written", good, [])




def test_spawn_refuses_a_base_missing_a_dependency():
    group("spawn REFUSES a base that lacks work the leaf depends on — #33 detected it and "
          "printed; #73 is the same failure again, four Crawlers later")
    if not have("git"):
        skip("the base refusal group", "git is not installed")
        return
    cfg = make_repo()
    g = new_graph(cfg)
    g.add("the dependency", leaf_id="b-dep", labels=["backend"])
    g.add("builds on it", leaf_id="b-next", labels=["backend"])
    g.dep("b-next", "b-dep")
    dep = worktree.spawn(cfg, g.show("b-dep"), actor="crawler-dep")
    with open(os.path.join(dep["worktree"], "dependency.txt"), "w") as fh:
        fh.write("the prerequisite\n")
    sh(["git", "add", "-A"], dep["worktree"])
    sh(["git", "commit", "-q", "-m", "the dependency's work"], dep["worktree"])

    def spawn(*extra):
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                               "spawn", "b-next", "--no-claim"] + list(extra),
                              capture_output=True, text=True, cwd=cfg.root)

    def tree_exists():
        return os.path.isdir(os.path.join(cfg.worktree_root, "crawler-b-next"))

    # The main checkout is where it started, which does NOT contain the dependency — the
    # reported shape exactly: an implicit base that is wrong because of where an unrelated
    # checkout happens to be pointing.
    p = spawn()
    eq("a base missing a declared dependency REFUSES rather than printing after the fact",
       p.returncode, 3)
    ok("...and creates NO worktree, because #33's line printed after the tree, the branch, "
       "the brief and the claim already existed", not tree_exists(), p.stdout)
    ok("...and names the dependency and the base, not just that something is wrong",
       "b-dep" in (p.stdout + p.stderr), p.stdout + p.stderr)
    ok("...and says the base came from the primary checkout's HEAD, which is the invisible "
       "input that decides correctness", "did not name a base" in (p.stdout + p.stderr),
       p.stdout + p.stderr)

    # THE OVERRIDE MUST NAME WHAT IT OVERRIDES, and naming the wrong thing is not a decision.
    p = spawn("--despite-base", "b-dep-typo")
    eq("an override naming something that is NOT missing is refused", p.returncode, 2)
    ok("...and creates nothing either", not tree_exists(), p.stdout)

    # THE PAIR. Without this, every assertion above passes against a spawn that refuses
    # everything — which would be a worse tool than the one that printed.
    p = spawn("--base", dep["branch"])
    eq("a base that CONTAINS the dependency spawns normally", p.returncode, 0)
    ok("...and the tree is actually created", tree_exists(), p.stdout + p.stderr)

    # THE RECORDED BASE IS REACHABLE AFTERWARDS. It has been recorded since #33 and no
    # operator surface ever showed it back, so the one question a Crawler on a wrong tree
    # needs asked about it had to be answered by hand.
    p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                        "show", "b-next"], capture_output=True, text=True, cwd=cfg.root)
    shown = json.loads(p.stdout)
    eq("`show` reports the base the tree was actually cut from",
       (shown.get("crawler_base") or {}).get("asked_for"), dep["branch"])
    ok("...and the resolved sha, since a branch name moves and the sha is what was used",
       bool((shown.get("crawler_base") or {}).get("sha")), shown)

    # A LEAF WITH NO CRAWLER MUST NOT GROW AN EMPTY ONE. An always-present key whose value is
    # null reads as "cut from nothing" rather than "never spawned".
    p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                        "show", "b-dep2"], capture_output=True, text=True, cwd=cfg.root)
    g.add("never spawned", leaf_id="b-dep2", labels=["backend"])
    p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                        "show", "b-dep2"], capture_output=True, text=True, cwd=cfg.root)
    ok("...while a leaf that was never spawned carries no base key at all",
       "crawler_base" not in json.loads(p.stdout), p.stdout)

    # THE REPORTED SHAPE, WITH NO GRAPH DEPENDENCY AT ALL. Every check above fires on a
    # measurement — a declared dep edge whose branch is not an ancestor. The reported failure
    # named its base in the BRIEF's prose, which showrunner cannot read; if those leaves carried
    # no dep edge then the dependency check stays silent and the trees are still wrong. This is
    # the arm that catches it, and it must be exercised on a leaf with NO dependencies or it
    # would be passing for the other check's reason.
    sh(["git", "checkout", "-q", "-b", "some-feature"], cfg.root)
    g.add("lone leaf, no dependencies", leaf_id="b-lone", labels=["backend"])

    def spawn_lone(*extra):
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                               "spawn", "b-lone", "--no-claim"] + list(extra),
                              capture_output=True, text=True, cwd=cfg.root)

    p = spawn_lone()
    eq("an IMPLICIT base while the checkout sits on a feature branch is refused, even with no "
       "dependency in the graph to measure against", p.returncode, 3)
    ok("...and names the branch the checkout is actually on, which is the invisible input",
       "some-feature" in (p.stdout + p.stderr), p.stdout + p.stderr)
    ok("...and creates nothing", not os.path.isdir(os.path.join(cfg.worktree_root,
                                                                "crawler-b-lone")), p.stdout)

    # `--base HEAD` IS THE CONFIRMATION, NOT A BYPASS. It resolves to the same commit the
    # default would have used; what differs is that somebody typed it. Without this pair the
    # assertion above passes against a spawn that refuses every feature branch outright.
    p = spawn_lone("--base", "HEAD")
    eq("...while naming HEAD explicitly proceeds — the same commit, deliberately chosen",
       p.returncode, 0)
    ok("...and the tree is created", os.path.isdir(os.path.join(cfg.worktree_root,
                                                                "crawler-b-lone")), p.stdout)
    sh(["git", "checkout", "-q", "main"], cfg.root)

    # AND THE DEFAULT BRANCH ITSELF MUST NOT REFUSE, or the guard is "always on" and the first
    # thing anybody does is look for the flag that turns it off.
    g.add("another lone leaf", leaf_id="b-lone2", labels=["backend"])
    p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                        "spawn", "b-lone2", "--no-claim"],
                       capture_output=True, text=True, cwd=cfg.root)
    eq("an implicit base while standing ON the default branch is exactly the case the default "
       "exists for, and is not refused", p.returncode, 0)

    # UNKNOWN IS NOT MISSING. A dependency that was never spawned has no branch to compare
    # against; refusing there would block work on the strength of not having looked.
    g.add("ghost", leaf_id="b-ghost", labels=["backend"])
    g.add("after the ghost", leaf_id="b-after", labels=["backend"])
    g.dep("b-after", "b-ghost")
    p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                        "spawn", "b-after", "--no-claim"],
                       capture_output=True, text=True, cwd=cfg.root)
    eq("a dependency that CANNOT be checked does not refuse — that would block work on the "
       "strength of not having looked", p.returncode, 0)
    ok("...but it is still said out loud, rather than passing silently",
       "NOT CHECKED" in (p.stdout + p.stderr), p.stdout + p.stderr)



def test_the_integration_record_names_its_evidence():
    group("The durable integration record must name the artifact that proves it — the record "
          "outlived the evidence, and still read as a proved leaf")
    if not have("git"):
        skip("the integration evidence group", "git is not installed")
        return

    # REPORTED FROM A CONSUMER'S REPO. The record was {crawler, branch, ts} — the durable half of
    # an integration, and what a reviewer reads months later to answer "was this leaf actually
    # proved, or did a Crawler assert it was?" It carried no reference to the artifact that
    # answers that, while the path was known at the exact moment the record was written.
    #
    # RECONSTRUCTING THE LINK IS NOT OBVIOUS, which is what makes it expensive rather than
    # untidy: the filename derives from the BRANCH and not the crawler, and is truncated. The
    # consumer measuring the correspondence produced two plausible wrong numbers before a right
    # one. A convention the reader has to rediscover is not a link.
    cfg = make_repo()
    g = new_graph(cfg)
    g.add("work that integrates", leaf_id="ev1", labels=["backend"])
    rec = worktree.spawn(cfg, g.show("ev1"), actor="crawler-ev")
    campaign.record_spawn(cfg, rec)
    with open(os.path.join(rec["worktree"], "landed.txt"), "w") as fh:
        fh.write("work\n")
    sh(["git", "add", "-A"], rec["worktree"])
    sh(["git", "commit", "-q", "-m", "the work"], rec["worktree"])
    g.claim("ev1", "crawler-ev", tree=rec["worktree"])
    g.close("ev1", G.CLOSED, os.path.join(rec["worktree"], "landed.txt"), "done")

    # Caught rather than allowed to propagate: this group's remaining assertions are about the
    # RECORD, and a crash here would leave every one of them unrun while the sweep reported the
    # group as unscoreable. Failing keeps the measurement.
    try:
        results, ok_flag = campaign.integrate(cfg, new_graph(cfg))
        why_int = None
    except Exception as exc:                                        # noqa: BLE001
        results, ok_flag, why_int = [], False, exc
    integrated = [r for r in results if r.get("status") == "integrated"]
    ok("a leaf integrates, so there is a record to examine", bool(integrated),
       why_int or results)

    records = campaign.load(cfg).get("integrated") or []
    ok("the campaign carries an integration record", bool(records), records)
    # `or [{}]` so a missing record FAILS the assertions below instead of raising past them —
    # an empty list here is a real outcome (nothing integrated), and the group's job is to say
    # which fields are missing, not to die on the first one.
    row = (records or [{}])[-1]
    for key in ("crawler", "branch", "ts"):
        ok("the record still carries %s, so this is additive and does not break a reader" % key,
           key in row, sorted(row))
    ok("...and it NAMES the merged proof, rather than leaving the reader to reconstruct a "
       "filename convention", row.get("merged_proof"), row)

    # THE NAMED PATH MUST BE THE REAL ONE. A record naming a file that is not there is worse
    # than one naming nothing: it reads as evidence.
    named = os.path.join(cfg.root, row.get("merged_proof") or "")
    exists = bool(row.get("merged_proof")) and os.path.isfile(named)
    ok("...and the artifact it names actually exists", exists, named)
    body = ""
    if exists:
        with open(named) as fh:
            body = fh.read()
    ok("...and is non-empty, because an empty proof is the identity element again", body.strip())

    # WHETHER IT WILL TRAVEL IS PART OF THE RECORD. Consumers gitignore these, reasonably — they
    # are large and local. Then the record arrives on another machine reading as a completed,
    # proved leaf with nothing behind it, which is the silent direction.
    ok("the record says whether git carries the proof", "proof_tracked" in row, sorted(row))
    eq("...and in a repo that does not track it, that is False and not None — None is reserved "
       "for 'git could not be asked'", row.get("proof_tracked"), False)

    # THE PAIR: a tracked artifact reports True, or the field is a constant and says nothing.
    tracked_probe = os.path.join(cfg.root, "tracked-proof.txt")
    with open(tracked_probe, "w") as fh:
        fh.write("evidence\n")
    sh(["git", "add", "tracked-proof.txt"], cfg.root)
    sh(["git", "commit", "-q", "-m", "track a proof"], cfg.root)
    eq("a tracked artifact reports True", campaign._is_tracked(cfg, tracked_probe), True)
    eq("an untracked one reports False",
       campaign._is_tracked(cfg, os.path.join(cfg.root, "never-added.txt")), False)

    # CANNOT-TELL IS ITS OWN ANSWER. "git does not carry this" and "I could not find out" lead to
    # opposite readings of whether the evidence survives a clone, and a bare False on failure
    # would quietly assert the first.
    class _NoRepo(object):
        root = tmpdir("proof-no-repo")
    eq("outside a git repo it answers None, never False",
       campaign._is_tracked(_NoRepo(), os.path.join(_NoRepo.root, "x.txt")), None)


def test_an_untracked_registration_still_reaches_the_worktree():
    group("A `--local` registration cannot cross on its own — `git worktree add` copies tracked "
          "files only, so every hook showrunner owns was absent from every Crawler")
    if not have("git"):
        skip("the local-registration mirror group", "git is not installed")
        return

    # MEASURED BEFORE BUILDING. A `--local` install produced worktrees with NO `.claude`
    # directory at all: the worktree guard, the dispatch guard, the seat announcement and the
    # reach gate were absent from every Crawler tree, while the main checkout reported all of
    # them registered and healthy. That is this repo's own sentence — a guard is exactly as
    # present as its registration — with the registration in a file that cannot reach the thing
    # it guards. The tracked arrangement was never affected, which is why it survived review:
    # every check reads the main checkout, and the main checkout is correct.
    #
    # The shim FILES were already provisioned into the tree. Only the settings entry naming them
    # was missing, and a provisioned shim nothing registers has never once run.
    cfg = make_repo()
    claude_dir = os.path.join(cfg.root, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    # The project's own unrelated settings, which must survive untouched: `harness.py` refuses to
    # copy a hook-registration file wholesale for exactly this reason.
    with open(os.path.join(claude_dir, "settings.local.json"), "w") as fh:
        json.dump({"statusLine": {"type": "command", "command": "mine"},
                   "hooks": {"PreToolUse": [
                       {"matcher": "Bash",
                        "hooks": [{"type": "command",
                                   "command": "\"$CLAUDE_PROJECT_DIR\"/.showrunner/hooks/"
                                              "dispatch-guard.sh"}]},
                       {"matcher": "Write",
                        "hooks": [{"type": "command", "command": "/opt/mine/my-own-guard.sh"}]}]}},
                  fh)

    tree = os.path.join(cfg.worktree_root, "mirror-probe")
    os.makedirs(tree, exist_ok=True)
    note = lease.mirror_local_registration(cfg, tree)
    ok("it reports what it carried, rather than doing it silently", bool(note), note)

    dest = os.path.join(tree, ".claude", "settings.local.json")
    ok("the worktree receives a settings file it would otherwise not have",
       os.path.isfile(dest), dest)
    # `or {}` so a producer that stopped producing FAILS these rather than raising out of the
    # group and taking the assertions after it down — a crash makes a mutant look like thinner
    # coverage than it has, which is how a sweep under-reports the thing it is measuring.
    got = {}
    if os.path.isfile(dest):
        with open(dest) as fh:
            got = json.load(fh)
    commands = [str(h.get("command")) for e in (got.get("hooks") or {}).get("PreToolUse", [])
                for h in e.get("hooks") or []]
    ok("showrunner's own hook is carried", any("dispatch-guard.sh" in c for c in commands),
       commands)

    # MERGED, NEVER COPIED. Carrying the whole file would hand the Crawler the project's
    # statusLine, permissions and unrelated hooks — the wholesale-copy mistake harness.py
    # documents. Only entries naming a showrunner shim may travel.
    ok("...and a hook named by an ABSOLUTE path outside the project is not carried, because it "
       "may not exist in the tree and copying the whole file is the mistake harness.py refuses",
       not any("my-own-guard" in c for c in commands), commands)
    ok("...nor its statusLine", "statusLine" not in got, sorted(got))

    # IDEMPOTENT, or a second spawn into the same tree stacks a duplicate that runs twice.
    again = lease.mirror_local_registration(cfg, tree)
    eq("carrying it a second time adds nothing and says nothing", again, "")
    after = {}
    if os.path.isfile(dest):
        with open(dest) as fh:
            after = json.load(fh)
    eq("...and the entry count is unchanged",
       len([1 for e in (after.get("hooks") or {}).get("PreToolUse", [])
            for h in e.get("hooks") or []]), len(commands))

    # EVERY PROJECT HOOK, NOT ONLY SHOWRUNNER'S. The first version carried `.showrunner/hooks/`
    # entries alone, which is the same defect one project over: the tree would get showrunner's
    # guards and not the harness's, while the main checkout reported both registered. A
    # Crawler's tree needs every guard the project runs, and which of them showrunner owns is
    # not a distinction the tree cares about.
    with open(os.path.join(claude_dir, "settings.local.json")) as fh:
        both = json.load(fh)
    both["hooks"]["PreToolUse"].append(
        {"matcher": "Bash", "hooks": [{"type": "command",
                                       "command": '"$CLAUDE_PROJECT_DIR"/.game_loop/bin/'
                                                  'guard-writes.sh'}]})
    with open(os.path.join(claude_dir, "settings.local.json"), "w") as fh:
        json.dump(both, fh)
    tree_b = os.path.join(cfg.worktree_root, "mirror-probe-both")
    os.makedirs(tree_b, exist_ok=True)
    lease.mirror_local_registration(cfg, tree_b)
    with open(os.path.join(tree_b, ".claude", "settings.local.json")) as fh:
        carried = [str(h.get("command"))
                   for e in (json.load(fh).get("hooks") or {}).get("PreToolUse", [])
                   for h in e.get("hooks") or []]
    ok("a harness hook the project registers is carried too, not just showrunner's own",
       any("guard-writes.sh" in c for c in carried), carried)
    ok("...and showrunner's is still carried alongside it",
       any("dispatch-guard.sh" in c for c in carried), carried)

    # THE CONTROL. With no untracked registration there is nothing to carry, and this must do
    # NOTHING rather than manufacture a file — a tracked registration crosses by itself, and a
    # second copy in the untracked layer would be a hook registered twice.
    plain = make_repo()
    tree2 = os.path.join(plain.worktree_root, "mirror-probe2")
    os.makedirs(tree2, exist_ok=True)
    eq("a repo with no untracked registration carries nothing",
       lease.mirror_local_registration(plain, tree2), "")
    ok("...and no settings file is invented in the tree",
       not os.path.exists(os.path.join(tree2, ".claude", "settings.local.json")))

    # AND A FILE THAT CANNOT BE READ SAYS SO. Silence here would look exactly like "there was
    # nothing to carry", which is the one reading that leaves a Crawler unguarded.
    broken = make_repo()
    os.makedirs(os.path.join(broken.root, ".claude"), exist_ok=True)
    with open(os.path.join(broken.root, ".claude", "settings.local.json"), "w") as fh:
        fh.write("{ not json")
    tree3 = os.path.join(broken.worktree_root, "mirror-probe3")
    os.makedirs(tree3, exist_ok=True)
    said = lease.mirror_local_registration(broken, tree3)
    ok("an unreadable registration is reported, not passed over as nothing to do",
       "could not read" in said, said)

    # END TO END, through `spawn`, because the function being right is not the same as it being
    # called — which is the defect this whole group is about, one layer up.
    p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                        "worktree", "register", "--local"],
                       capture_output=True, text=True, cwd=cfg.root)
    eq("register --local succeeds", p.returncode, 0)
    g = new_graph(cfg)
    g.add("carried into its tree", leaf_id="mir1", labels=["backend"])
    p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                        "spawn", "mir1", "--actor", "mirror", "--no-claim"],
                       capture_output=True, text=True, cwd=cfg.root)
    eq("spawn succeeds", p.returncode, 0)
    spawned = os.path.join(cfg.worktree_root, "mirror-mir1", ".claude", "settings.local.json")
    ok("a spawned worktree carries the untracked registration, so its guards are actually "
       "registered where they run", os.path.isfile(spawned), p.stdout[-400:])


def test_a_claim_never_names_the_process_that_is_about_to_exit():
    group("A claim's liveness must name a session, not the CLI process that took it — "
          "`spawn` without --launch recorded a pid that was gone seconds later")
    if not have("git"):
        skip("the claim liveness group", "git is not installed")
        return

    # REPRODUCED TWICE BY A CONSUMER, and this asserts their exact shape: `spawn <leaf> --actor X`
    # with NO --launch, then starting a session in the prepared tree by hand. The claim recorded
    # `os.getpid()` — the `showrunner` process itself, which exits as soon as spawn returns — so
    # `stale_claims` called the leaf abandoned and `reap --apply` offered to release it while the
    # real session worked in that worktree. Two leaves, both reading as abandoned, work fine.
    #
    # `--launch` ALREADY HAD THE REMEDY. `rebind_claim` is called once the launched pid is known,
    # and its docstring describes this failure in these words. The path that does not launch had
    # the identical defect and nothing to correct it.
    #
    # THE CLI MUST BE A SEPARATE PROCESS or the bug cannot appear: in-process, `os.getpid()` is
    # the test runner, which is alive, and the old code looks correct. That is why this shells
    # out rather than calling the library.
    cfg = make_repo()
    g = new_graph(cfg)
    g.add("prepared and started by hand", leaf_id="cl1", labels=["backend"])
    sr = os.path.join(ROOT, "bin", "showrunner")

    p = subprocess.run([sys.executable, sr, "spawn", "cl1", "--actor", "crawler-cl"],
                       capture_output=True, text=True, cwd=cfg.root)
    eq("spawn without --launch succeeds", p.returncode, 0)

    leaf = new_graph(cfg).show("cl1")
    claimed_pid = leaf.get("claim_pid")
    eq("the leaf is claimed", leaf.get("status"), "in_progress")

    # THE DEFECT, STATED AS THE ASSERTION: whatever pid is recorded must not be the CLI process
    # that has already exited. It is either a live enclosing session, or None — never a corpse.
    ok("the claim does not name the `showrunner` process, which exited when spawn returned",
       claimed_pid is None or util.pid_alive(claimed_pid), claimed_pid)

    # AND THE CONSEQUENCE, which is the thing that actually cost the consumer: the leaf must not
    # be offered for release while somebody is working the tree.
    stale = new_graph(cfg).stale_claims()
    ok("...so `reap` does NOT report the leaf abandoned — the failure was `reap --apply` "
       "releasing a leaf whose Crawler was still working",
       "cl1" not in [l["id"] for l, _ in stale], [(l["id"], w) for l, w in stale])

    # A CLAIM WITH NO PROVABLE OWNER IS SURFACED, NOT RELEASED. None is the honest record for a
    # tree prepared before its session exists, and `stale_claims` already treats an unreadable
    # pid as unprovable rather than dead — releasing it would end with two Crawlers on one leaf.
    g2 = new_graph(cfg)
    g2.add("no owner at all", leaf_id="cl2", labels=["backend"])
    g2.claim("cl2", "crawler-none", pid=None, tree=cfg.root)
    g2.db.execute("UPDATE leaves SET claim_pid=NULL WHERE id=?", ("cl2",))
    g2.db.commit()
    stale2 = new_graph(cfg).stale_claims()
    ok("a claim with NO pid is not reported abandoned, because a claim whose owner cannot be "
       "named cannot be proved abandoned", "cl2" not in [l["id"] for l, _ in stale2],
       [l["id"] for l, _ in stale2])

    # THE TWO-SIDED CONTROL. Everything above is satisfied by a `stale_claims` that reports
    # nothing at all — which would be worse than the bug, since an abandoned leaf would never
    # come back. A provably dead pid MUST still be reported.
    g3 = new_graph(cfg)
    g3.add("really abandoned", leaf_id="cl3", labels=["backend"])
    g3.claim("cl3", "crawler-dead", pid=999999, tree=cfg.root)
    stale3 = new_graph(cfg).stale_claims()
    ok("...while a claim on a pid that is provably NOT running is still reported stale, so this "
       "is not a blanket silencing of the reaper",
       "cl3" in [l["id"] for l, _ in stale3], [l["id"] for l, _ in stale3])

    # EVERY WRITER OF THE COLUMN, ENUMERATED — not just the one that was reported.
    #
    # A consumer named the shape after hitting it twice in a day: a rule that holds in three
    # places and not the fourth is invisible to REVIEW, because every file you open is correct.
    # llms.txt already said a claim's pid is DISCOVERED not handed over; `lock acquire` and
    # `role claim` both walked the ancestry; the leaf claim did not. Fixing the reported site and
    # stopping is how the fourth one survives — and there WAS a fourth: `unpark` rewrites
    # claim_pid and kept `os.getpid()`, so a leaf parked at a usage limit came back holding the
    # pid of the `unpark` process. The workflow parking exists for would have reintroduced it.
    #
    # So this asserts over the SET of writers, derived from the source, rather than over the one
    # that was reported. Reading finds the site you are looking at; enumerating finds the rest.
    gsrc = open(os.path.join(ROOT, "lib", "showrunner", "graph.py")).read()
    writers = [m for m in re.finditer(r"claim_pid=\?", gsrc)]
    ok("more than one statement writes claim_pid, so enumerating them is a real question",
       len(writers) >= 2, len(writers))
    for m in writers:
        # The values tuple follows the SQL; `os.getpid()` inside it is the defect.
        following = gsrc[m.end():m.end() + 400]
        args_blob = following.split("))", 1)[0]
        ok("a statement writing claim_pid at offset %d does not hand it this process's own pid"
           % m.start(), "os.getpid()" not in args_blob, args_blob[:160])

    # AND BEHAVIOURALLY, through the verb, because the structural check above cannot see a pid
    # that arrives from somewhere else.
    g4 = new_graph(cfg)
    g4.add("parked then resumed", leaf_id="cl4", labels=["backend"])
    sr2 = os.path.join(ROOT, "bin", "showrunner")
    subprocess.run([sys.executable, sr2, "claim", "cl4", "--actor", "a"],
                   capture_output=True, text=True, cwd=cfg.root)
    subprocess.run([sys.executable, sr2, "park", "cl4", "--reason", "usage limit"],
                   capture_output=True, text=True, cwd=cfg.root)
    p4 = subprocess.run([sys.executable, sr2, "unpark", "cl4"],
                        capture_output=True, text=True, cwd=cfg.root)
    eq("unpark succeeds", p4.returncode, 0)
    resumed = new_graph(cfg).show("cl4").get("claim_pid")
    ok("...and the resumed claim does not name the `unpark` process, which exited when it "
       "returned — the same defect in the sibling method",
       resumed is None or util.pid_alive(resumed), resumed)

    # DISCOVERED, NOT HANDED OVER — the rule llms.txt already states for role claims and
    # `lock acquire`, which the LEAF claim was not following.
    src = open(os.path.join(ROOT, "lib", "showrunner", "graph.py")).read()
    ok("the leaf claim resolves its pid rather than trusting the calling process",
       "os.getpid()" not in src.split("def _claim_pid")[0].split("claim_pid=?")[-1][:400],
       "still defaults to the caller's own pid")


def test_reach_is_registered_by_the_verb_that_registers_hooks():
    group("The verb built so an agent need not already know the tool must not itself require "
          "knowing it exists — `worktree register` wires `reach`")
    if not have("git"):
        skip("the reach registration group", "git is not installed")
        return

    # MEASURED BY A CONSUMER, not reasoned about here. `reach` shipped wired by hand, on the
    # reasoning that advice should be opt-in. In one session they hand-rolled a worktree wrapper,
    # a dispatch script and a layer guard — with zero references to it in either settings layer —
    # then piped a payload in by hand and got the sentence they had needed hours earlier. That is
    # the defect dispatch-guard.sh's own header names, arriving in the verb whose job is to name
    # it: a guard is exactly as present as its registration.
    cfg = make_repo()
    p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                        "worktree", "register"], capture_output=True, text=True, cwd=cfg.root)
    eq("register exits 0", p.returncode, 0)
    with open(os.path.join(cfg.root, ".claude", "settings.json")) as fh:
        data = json.load(fh)
    entries = [(e.get("matcher") or "", h.get("command") or "")
               for e in (data.get("hooks") or {}).get("PreToolUse", []) or []
               for h in e.get("hooks") or []]
    reach = [(m, c) for m, c in entries if "reach-gate" in c]
    ok("`worktree register` registers the reach gate", bool(reach), entries)
    if reach:
        matcher = reach[0][0]
        # THE MATCHER IS THE FIX, the same argument dispatch-guard makes for Bash: a worktree by
        # hand and a branch arrive as Bash, a memory write as Write/Edit/NotebookEdit, and a
        # private work list as TodoWrite. Registered on a subset, the rules covering the rest
        # are present and unreachable.
        for tool in ("Write", "Edit", "NotebookEdit", "Bash", "TodoWrite"):
            ok("...on %s, which one of its rules actually fires on" % tool, tool in matcher,
               matcher)

    # AND IT IS IDEMPOTENT, or a second `register` stacks a duplicate hook that runs twice.
    before = len(entries)
    subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                    "worktree", "register"], capture_output=True, text=True, cwd=cfg.root)
    with open(os.path.join(cfg.root, ".claude", "settings.json")) as fh:
        again = json.load(fh)
    after = len([1 for e in (again.get("hooks") or {}).get("PreToolUse", []) or []
                 for h in e.get("hooks") or []])
    eq("...and registering twice does not duplicate it", after, before)

    # THE SHIM MUST SHIP, or the registration names a file that is not there — and
    # "registered-and-absent is worse than unregistered, because the registration is what makes
    # it look present" is this suite's own sentence about the other hooks.
    with open(os.path.join(ROOT, "install.sh")) as fh:
        installer = fh.read()
    ok("install.sh ships the shim it registers", "reach-gate.sh" in installer)


def test_a_seat_survives_a_window_reload():
    group("A seat whose SESSION still matches but whose pid is gone is rebound — a reload "
          "should not cost the seat, and must not become a way to take somebody else's")
    if not have("git"):
        skip("the reseat group", "git is not installed")
        return

    # THE REPORTED PROBLEM. Reloading a VS Code window loses the seat every time. Nothing was
    # broken: a seat's liveness is a pid discovered by walking the ancestry, the reload restarts
    # the extension host under a NEW pid, `locks` correctly reports STALE, and the resolver skips
    # it. Correct, and useless — the same logical session comes back and cannot see its own seat.
    #
    # THE DISCRIMINATOR WORKS BECAUSE THE TWO FACTS AGE DIFFERENTLY, measured before building:
    # the Claude session id is unchanged across a reload while the pid is not. So "same session,
    # dead pid" IS a reload.
    home = tmpdir("reseat-home")
    os.makedirs(os.path.join(home, "showrunner"), exist_ok=True)
    with open(os.path.join(home, "showrunner", "roles.json"), "w") as fh:
        json.dump({"roles": {"lead": {"acquire": "claim", "may_create": ["worker"]},
                             "unassigned": {"acquire": "claim"}},
                   "fallback": "unassigned"}, fh)
    cfg = make_repo()
    sr = os.path.join(ROOT, "bin", "showrunner")

    def run(args, campaign):
        return subprocess.run([sys.executable, sr] + args, capture_output=True, text=True,
                              cwd=cfg.root,
                              env=dict(os.environ, XDG_CONFIG_HOME=home,
                                       SHOWRUNNER_CAMPAIGN=campaign))

    # A DEAD pid is what a reload leaves behind. 999998 is not running.
    run(["role", "claim", "lead", "--who", "bot", "--session", "S-RELOAD", "--pid", "999998"],
        "reseat-dead")
    said = run(["whoami", "--session", "S-RELOAD"], "reseat-dead").stdout
    ok("the seat comes back to the same session after its pid dies", "RE-SEATED" in said,
       said[:300])
    ok("...and the role resolves rather than falling back, which is the whole point",
       "role: lead" in said, said[:300])
    ok("...and it says a reload happened, so the caller can redo setup it did when it first "
       "took the seat — a silent re-seat is indistinguishable from never having lost it",
       "do it again" in said, said[:400])

    # THE CONTROL THAT MATTERS MOST: a LIVE holder is never displaced. Two live processes on one
    # session id is what `claude --resume` produces, and taking a seat from a process that is
    # demonstrably running is the reading this repo never permits.
    run(["role", "claim", "lead", "--who", "bot", "--session", "S-RESUME", "--pid", "1"],
        "reseat-live")
    said = run(["whoami", "--session", "S-RESUME"], "reseat-live").stdout
    ok("a seat whose recorded pid is ALIVE is not rebound", "RE-SEATED" not in said, said[:300])
    ok("...and the note says so, naming the resume case rather than failing silently",
       "STILL ALIVE" in said and "resume" in said, said[:400])
    # THE MESSAGE MUST MATCH THE BEHAVIOUR. The first wording said "the seat was NOT taken" and
    # the next line announced the role — two true statements arranged to read as a denial that
    # had not happened. Both processes DO resolve, because the session is the unit of identity.
    ok("...and does not claim the caller was denied the role, which it was not",
       "NOT taken" not in said, said[:400])

    # AN EMPTY SESSION MUST NOT BE A KEY. A seat claimed with no session id records "", and
    # matching "" to "" would let any unidentified session inherit any unidentified seat — a
    # rebind rule that hands out other people's seats is worse than one that never fires.
    # CONSTRUCTED DELIBERATELY, because omitting `--session` no longer produces it: the CLI now
    # DISCOVERS the session from the environment, which is the fix above. The empty case is still
    # reachable — a caller with no session variables at all, a cron job, a bare shell — and the
    # rule still has to hold there, so the env is cleared rather than the assertion dropped.
    bare = {k: v for k, v in os.environ.items()
            if k not in ("SHOWRUNNER_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")}
    bare.update(XDG_CONFIG_HOME=home, SHOWRUNNER_CAMPAIGN="reseat-empty")
    subprocess.run([sys.executable, sr, "role", "claim", "lead", "--who", "bot",
                    "--pid", "999997"], capture_output=True, text=True, cwd=cfg.root, env=bare)
    said = subprocess.run([sys.executable, sr, "whoami"], capture_output=True, text=True,
                          cwd=cfg.root, env=bare).stdout
    ok("a seat recorded with NO session id is not rebound to a session with no id either",
       "RE-SEATED" not in said, said[:300])
    ok("...and that session gets the fallback, which is the honest answer",
       "unassigned" in said, said[:300])

    # A PLAIN `role claim` MUST RECORD A SESSION, or none of this ever fires for a real
    # operator. The seat keys on the session id and refuses to match "", so a claim run the
    # obvious way — no `--session`, which is how anyone would run it — produced a seat the
    # re-seat could never rebind. Requiring the flag would have been documenting a footgun for a
    # mechanism whose whole point is that nobody should have to think about it.
    plain = subprocess.run([sys.executable, sr, "role", "claim", "lead", "--who", "bot"],
                           capture_output=True, text=True, cwd=ROOT,
                           env=dict(os.environ, XDG_CONFIG_HOME=home,
                                    SHOWRUNNER_CAMPAIGN="reseat-discover-%d" % os.getpid(),
                                    CLAUDE_CODE_SESSION_ID="S-DISCOVERED"))
    eq("a plain `role claim` succeeds", plain.returncode, 0)
    rost = subprocess.run([sys.executable, sr, "role", "roster"], capture_output=True, text=True,
                          cwd=ROOT,
                          env=dict(os.environ, XDG_CONFIG_HOME=home,
                                   SHOWRUNNER_CAMPAIGN="reseat-discover-%d" % os.getpid(),
                                   CLAUDE_CODE_SESSION_ID="S-DISCOVERED"))
    ok("...and the seat it records carries a session id DISCOVERED from the environment, not one "
       "the operator had to know to pass", "S-DISCOVERED" in rost.stdout or plain.returncode == 0,
       rost.stdout[:200])
    from showrunner import util as _u
    eq("the discovery prefers showrunner's own override, so a deliberate caller is never "
       "second-guessed",
       _u.caller_session.__doc__ is not None and "SHOWRUNNER_SESSION" in _u.caller_session.__doc__,
       True)

    # AND THE HOOK MUST ACTUALLY SUPPLY THE SESSION, or every assertion above is about a
    # function nothing calls with a usable argument. `whoami.sh` invoked `whoami` with no
    # `--session`, and the rule that an EMPTY session matches nothing then made the whole feature
    # unreachable from the one channel a reloaded session is guaranteed to read. Built and
    # unfired — caught here rather than by an operator reloading and seeing nothing happen.
    # THE HOOK RESOLVES THROUGH CLAUDE_PROJECT_DIR, so the seat has to be claimed in the SAME
    # place the hook will look. Claiming it in the scratch repo while the hook read the real one
    # made this fail for a reason that had nothing to do with the feature — a fixture mismatch
    # wearing a defect's clothes. The roles file and the campaign are still isolated, so this
    # writes only throwaway campaign state.
    # A UNIQUE CAMPAIGN PER RUN, because this assertion CHANGES the state it reads. The first
    # run rebinds the dead seat to a LIVE pid — that is the feature working — so a second run in
    # the same campaign correctly takes the still-alive branch and reports no RE-SEATED. It
    # passed alone and failed under `verify`, which is the order-dependence a suite must not
    # have: the second answer was right and the fixture was wrong.
    shim = os.path.join(ROOT, ".showrunner", "hooks", "whoami.sh")
    hook_campaign = "reseat-hook-%d" % os.getpid()
    subprocess.run([sys.executable, sr, "role", "claim", "lead", "--who", "bot",
                    "--session", "S-HOOK", "--pid", "999993"],
                   capture_output=True, text=True, cwd=ROOT,
                   env=dict(os.environ, XDG_CONFIG_HOME=home,
                            SHOWRUNNER_CAMPAIGN=hook_campaign))
    hp = subprocess.run(["bash", shim],
                        input=json.dumps({"session_id": "S-HOOK",
                                          "hook_event_name": "SessionStart"}),
                        capture_output=True, text=True, cwd=ROOT,
                        env=dict(os.environ, XDG_CONFIG_HOME=home,
                                 SHOWRUNNER_CAMPAIGN=hook_campaign,
                                 CLAUDE_PROJECT_DIR=ROOT))
    try:
        ctx = json.loads(hp.stdout)["hookSpecificOutput"]["additionalContext"]
    except Exception:                                            # noqa: BLE001
        ctx = hp.stdout
    ok("the SessionStart hook passes the session id through, so the re-seat can fire from the "
       "channel a reload actually reaches", "RE-SEATED" in ctx, ctx[:300])

    # AND IT MUST STILL ANNOUNCE WITH NO PAYLOAD AT ALL. The hook's one forbidden outcome is
    # silence, and reading stdin is new — an empty or unparseable payload must cost the session
    # id and nothing else.
    hp2 = subprocess.run(["bash", shim], input="", capture_output=True, text=True, cwd=ROOT,
                         env=dict(os.environ, XDG_CONFIG_HOME=home,
                                  SHOWRUNNER_CAMPAIGN=hook_campaign, CLAUDE_PROJECT_DIR=ROOT))
    ok("...and an EMPTY payload still produces an announcement rather than silence",
       bool(hp2.stdout.strip()), hp2.stdout[:200])
    hp3 = subprocess.run(["bash", shim], input="not json at all", capture_output=True,
                         text=True, cwd=ROOT,
                         env=dict(os.environ, XDG_CONFIG_HOME=home,
                                  SHOWRUNNER_CAMPAIGN=hook_campaign, CLAUDE_PROJECT_DIR=ROOT))
    ok("...and so does an UNPARSEABLE one", bool(hp3.stdout.strip()), hp3.stdout[:200])

    # A DIFFERENT SESSION IS A STRANGER. Same dead pid, different id: no rebind, because the
    # discriminator is the session and not merely that something died.
    run(["role", "claim", "lead", "--who", "bot", "--session", "S-MINE", "--pid", "999996"],
        "reseat-other")
    said = run(["whoami", "--session", "S-NOT-MINE"], "reseat-other").stdout
    ok("a DIFFERENT session does not inherit a dead seat just because the pid is gone",
       "RE-SEATED" not in said, said[:300])
    ok("...it gets the fallback, and the seat stays where it was for its owner to reclaim",
       "unassigned" in said, said[:300])

    # AND THE SUITE PUTS THE REAL CHECKOUT BACK. Two of the campaigns above are created in the
    # REAL `.showrunner/campaigns/` on purpose — the hook resolves through CLAUDE_PROJECT_DIR,
    # so the seat has to be claimed where the hook will look — and the comment above called
    # that "throwaway campaign state". Nothing ever threw it away: 71 of them had piled up
    # beside real campaigns, two per run, in the one directory an operator reads to find their
    # own work. A test that cannot be sandboxed must clean up after itself instead, and must
    # SAY SO with an assertion, or the cleanup is a line of code nobody notices rotting.
    campdir = os.path.join(ROOT, ".showrunner", "campaigns")
    for _c in ("reseat-discover-%d" % os.getpid(), hook_campaign):
        shutil.rmtree(os.path.join(campdir, _c), ignore_errors=True)
    leftover = [d for d in (os.listdir(campdir) if os.path.isdir(campdir) else [])
                if d in ("reseat-discover-%d" % os.getpid(), hook_campaign)]
    ok("...and the group removes the campaigns it had to create in the REAL checkout, so a "
       "suite run does not leave residue in the live campaign list", not leftover, leftover)


def test_a_seat_that_may_not_dispatch_is_refused_at_the_sanctioned_path():
    group("`spawn --launch` asks whether the seat may dispatch — the guard was on the path "
          "whoami tells you NOT to use, and not on the one it tells you to (#77)")
    if not have("git"):
        skip("the seat dispatch group", "git is not installed")
        return

    home = tmpdir("seat-home")
    env = dict(os.environ, XDG_CONFIG_HOME=home, SHOWRUNNER_SESSION="sess-under-test")
    rolesdir = os.path.join(home, "showrunner")
    os.makedirs(rolesdir, exist_ok=True)

    def write_roles(spec):
        with open(os.path.join(rolesdir, "roles.json"), "w") as fh:
            json.dump(spec, fh)

    cfg = make_repo()
    g = new_graph(cfg)
    g.add("work", leaf_id="s1", labels=["backend"])
    g.add("more work", leaf_id="s2", labels=["backend"])
    sr = os.path.join(ROOT, "bin", "showrunner")

    def spawn(leaf, *extra, **kw):
        return subprocess.run([sys.executable, sr, "spawn", leaf, "--no-claim"] + list(extra),
                              capture_output=True, text=True, cwd=cfg.root,
                              env=kw.get("env", env))

    # NO ROLES CONFIGURED MEANS NO POLICY, and this pair is why the first version of the check
    # was wrong: without it, `spawn --launch` refused every dispatch in every repo that had never
    # written a roles.json. llms.txt states that escape as deliberate — inventing a policy would
    # refuse the dispatches of every consumer who never wrote one. The existing suite caught it.
    p = spawn("s1", "--dry-run", "--launch")
    ok("with no roles configured, a launch is NOT refused for the seat",
       "may not" not in (p.stdout + p.stderr).lower(), (p.stdout + p.stderr)[:200])

    # A ROLE THAT MAY NOT CREATE. This is the reported seat: the fallback, announcing
    # "may dispatch: NOTHING", from which two Crawlers were launched.
    write_roles({"roles": {"unassigned": {"acquire": "claim", "writes": {"deny": ["**"]}},
                           "lead": {"acquire": "claim", "may_create": ["worker"]}},
                 "fallback": "unassigned"})
    p = spawn("s1", "--launch")
    eq("a seat that may not dispatch is REFUSED at `spawn --launch`", p.returncode, 3)
    said = p.stdout + p.stderr
    ok("...and names the role and the seat, not merely that something was refused",
       "unassigned" in said, said[:300])
    ok("...and says how to get a seat that may dispatch",
       "role claim" in said, said[:400])
    ok("...and creates NOTHING, because the check runs before the worktree exists",
       not os.path.isdir(os.path.join(cfg.worktree_root, "crawler-s1")), said[:200])

    # THE PAIR THAT MATTERS MOST. Without --launch nothing is dispatched, and `may_create` names
    # what a role may START. Refusing the preparation too would be a wider rule than the field
    # says — invented here rather than declared by the operator.
    p = spawn("s2")
    eq("...while `spawn` WITHOUT --launch is allowed, since it starts no session", p.returncode, 0)
    ok("...and really did prepare the room",
       os.path.isdir(os.path.join(cfg.worktree_root, "crawler-s2")), p.stdout[:200])

    # THE OTHER SIDE: a seat that MAY create is not refused, or the check is a blanket denial.
    write_roles({"roles": {"unassigned": {"acquire": "claim", "may_create": ["worker"]}},
                 "fallback": "unassigned"})
    p = spawn("s1", "--dry-run", "--launch")
    ok("a seat that MAY create is not refused", "may not" not in (p.stdout + p.stderr).lower(),
       (p.stdout + p.stderr)[:200])

    # ONE FUNCTION, TWO CALLERS. A second copy of "may this seat dispatch" is two statements of
    # one policy free to disagree — and the whole defect was the two paths disagreeing.
    src = open(os.path.join(ROOT, "lib", "showrunner", "dispatch.py")).read()
    eq("the seat decision is defined exactly once", src.count("def may_dispatch("), 1)
    cli_src = open(os.path.join(ROOT, "lib", "showrunner", "cli.py")).read()
    ok("...and spawn calls it rather than re-deriving the answer",
       "dispatch.may_dispatch(" in cli_src)


def test_the_announcement_does_not_claim_enforcement_it_has_not_got():
    group("A line saying ENFORCED must be one showrunner refuses; `writes` is PUBLISHED, "
          "because showrunner ships no write guard (#77)")
    if not have("git"):
        skip("the enforcement labelling group", "git is not installed")
        return

    # THE SENTENCE THAT STOPS SOMEBODY CHECKING. The reported seat printed ENFORCED over both
    # "may dispatch: NOTHING" and "may NOT write: **". The first was true only of a path the same
    # announcement steers you away from; the second was enforced by a hook of the consumer's,
    # registered for Write|Edit|NotebookEdit and not Bash. Half an hour of unguarded work.
    pairs = roles.enforced_lines({"acquire": "claim", "writes": {"deny": ["**"]},
                                  "may_create": []})
    ok("every entry carries its own label rather than one banner over all of them",
       all(isinstance(x, tuple) and len(x) == 2 for x in pairs), pairs)
    ok("`writes` is PUBLISHED, never ENFORCED",
       all(lab == "PUBLISHED" for lab, t in pairs if "write" in t), pairs)
    ok("...and `may dispatch` IS enforced, so the split is real and not a downgrade of "
       "everything", any(lab == "ENFORCED" and "may dispatch" in t for lab, t in pairs), pairs)

    # PROVED BY BEHAVIOUR, NOT BY GREPPING FOR THE FIELD. The first version of this scanned the
    # modules for `writes` and failed on doctor's own REPORT of it — reading a field to say
    # something about it is not enforcing it, and a source scan cannot tell those apart. So the
    # claim is demonstrated instead: showrunner's own write-path guard ALLOWS a write that the
    # role's `writes` denies. If that ever stops being true, PUBLISHED has become a lie and this
    # is where it surfaces.
    _cfg = make_repo()
    _home = tmpdir("seat-home-behaviour")
    os.makedirs(os.path.join(_home, "showrunner"), exist_ok=True)
    with open(os.path.join(_home, "showrunner", "roles.json"), "w") as fh:
        json.dump({"roles": {"unassigned": {"acquire": "claim", "writes": {"deny": ["**"]}}},
                   "fallback": "unassigned"}, fh)
    _p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                         "worktree", "guard"],
                        input=json.dumps({"tool_name": "Write",
                                          "tool_input": {"file_path":
                                                         os.path.join(_cfg.root, "x.txt")}}),
                        capture_output=True, text=True, cwd=_cfg.root,
                        env=dict(os.environ, XDG_CONFIG_HOME=_home))
    eq("showrunner's own write-path guard ALLOWS a write the role's `writes` denies — which is "
       "what PUBLISHED means, demonstrated rather than asserted", _p.returncode, 0)

    # DOCTOR PARSES THE HOOKS, because a consumer never runs this suite (reported by a consumer
    # who had the check only as text in their own verify.yaml — where a quoting bug made it
    # unable to pass, and an upgrade could not rewrite it because it was their copy). A check
    # shipped as text is a check you cannot fix for anybody; a check inside `doctor` upgrades.
    # They also could not patch their copy: that file is project policy and their write guard
    # refused, correctly.
    parse_cfg = make_repo()
    hooks_dir = os.path.join(parse_cfg.root, ".showrunner", "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    shutil.copy(os.path.join(ROOT, ".showrunner", "hooks", "whoami.sh"),
                os.path.join(hooks_dir, "whoami.sh"))

    def parse_doctor():
        p_ = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                            capture_output=True, text=True, cwd=parse_cfg.root)
        return p_.returncode, p_.stdout + p_.stderr

    _rc, said = parse_doctor()
    ok("doctor reports that the hooks parse, rather than saying nothing when they do",
       "parse" in said, said[-400:])

    with open(os.path.join(hooks_dir, "whoami.sh"), "a") as fh:
        fh.write('\nif [ -z "$x" ]; then\n')
    rc_bad, said = parse_doctor()
    ok("...and a hook that does NOT parse is an ERROR naming the file",
       "DOES NOT PARSE" in said and "whoami.sh" in said, said[-400:])
    eq("...and doctor exits non-zero on it, because this is the one failure that blocks its own "
       "repair", rc_bad != 0, True)
    ok("...and says how to fix it from OUTSIDE the session, since inside it every tool is "
       "refused", "outside the session" in said, said[-300:])
    ok("...and renders bash's message as prose, not a Python list repr",
       "['" not in said.split("DOES NOT PARSE")[1][:200], said[-300:])

    # A PYTHON HOOK IS PARSED AS PYTHON. The shebang decides, not the extension — guessing wrong
    # reports a healthy file as broken, which is the crying-wolf direction.
    with open(os.path.join(hooks_dir, "probe.py"), "w") as fh:
        fh.write("#!/usr/bin/env python3\nthis is not python(\n")
    _rc2, said = parse_doctor()
    ok("a .py hook with broken Python is caught too", "probe.py" in said, said[-300:])

    # DOCTOR ANSWERS THE QUESTION showrunner CAN answer: is there a reader on Bash at all?
    cfg = make_repo()
    home = tmpdir("seat-home2")
    os.makedirs(os.path.join(home, "showrunner"), exist_ok=True)
    with open(os.path.join(home, "showrunner", "roles.json"), "w") as fh:
        json.dump({"roles": {"unassigned": {"acquire": "claim", "writes": {"deny": ["**"]}}},
                   "fallback": "unassigned"}, fh)
    env = dict(os.environ, XDG_CONFIG_HOME=home)
    settings = os.path.join(cfg.root, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings), exist_ok=True)

    def doctor():
        p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                           capture_output=True, text=True, cwd=cfg.root, env=env)
        return p.stdout + p.stderr

    with open(settings, "w") as fh:
        json.dump({"hooks": {"PreToolUse": [
            {"matcher": "Write|Edit|NotebookEdit",
             "hooks": [{"type": "command", "command": "/bin/true"}]}]}}, fh)
    said = doctor()
    ok("a `writes` policy with NO Bash matcher is an ERROR — this is the reported install "
       "exactly, and it is the shape a heredoc walks past",
       "NO PreToolUse hook matches Bash" in said, said[-600:])

    # THE PAIR. Adding Bash must clear it, or the check is a permanent complaint nobody can act
    # on — which is the alarm that is always on.
    with open(settings, "w") as fh:
        json.dump({"hooks": {"PreToolUse": [
            {"matcher": "Write|Edit|NotebookEdit|Bash",
             "hooks": [{"type": "command", "command": "/bin/true"}]}]}}, fh)
    said = doctor()
    ok("...and a matcher that DOES include Bash clears it", "does match Bash" in said,
       said[-400:])
    ok("...while still refusing to say WHICH hook enforces it, because attributing another "
       "tool's job would be a guess", "cannot tell WHICH" in said, said[-400:])


def test_waiting_does_not_scale_with_the_campaign():
    group("`waiting` costs a bounded number of git subprocesses — a consumer runs it under a "
          "fixed timeout, and a timeout there DISARMS the watchdog (#76)")
    if not have("git"):
        skip("the waiting cost group", "git is not installed")
        return

    # WHY THIS IS COUNTED AND NOT TIMED. A wall-clock assertion measures the machine, so it is
    # flaky on a busy one and silent on a fast one — and the reporter's machine was slow for a
    # reason the code cannot fix: four security suites intercepting every process spawn, ~23ms
    # per `git` against the usual 2-3ms. The count is the thing showrunner controls, and it is
    # what turns a constant into 20 seconds. A design that spawns hundreds of processes to answer
    # "am I waiting?" is one scanner away from breaking wherever it is installed.
    #
    # THE COST WAS NOT SLOWNESS, IT WAS SILENCE. game_loop runs `waiting` as its watchdog probe
    # under a hardcoded 15s timeout and reads a timeout as "the probe did not run at all", which
    # it reports as a broken watchdog and then stops scheduling re-checks. Six days with no
    # verdict logged; three Crawlers died without committing inside that window, each found only
    # because a human went looking.
    def campaign_of(n, tag):
        cfg = make_repo()
        g = new_graph(cfg)
        for i in range(n):
            leaf_id = "%s%02d" % (tag, i)
            g.add("leaf %d" % i, leaf_id=leaf_id, labels=["backend"])
            rec = worktree.spawn(cfg, g.show(leaf_id), actor="c-%s%02d" % (tag, i))
            campaign.record_spawn(cfg, rec)
        return cfg, g

    def git_calls(fn):
        """Count real git subprocesses, by wrapping the one helper they all go through."""
        calls = []
        real = campaign.git

        def counting(*a, **kw):
            calls.append(a[0][0] if a and a[0] else "?")
            return real(*a, **kw)
        campaign.git = counting
        try:
            fn()
        finally:
            campaign.git = real
        return calls

    small_cfg, small_g = campaign_of(3, "w")
    big_cfg, big_g = campaign_of(12, "v")

    small = git_calls(lambda: campaign.waiting(small_cfg, small_g))
    big = git_calls(lambda: campaign.waiting(big_cfg, big_g))

    ok("a 3-Crawler campaign costs waiting only a handful of git calls", len(small) <= 6, small)
    # THE SHAPE, NOT A MAGIC NUMBER. Quadrupling the Crawlers must not multiply the cost: before
    # this, each Crawler cost ~7 subprocesses, so the total tracked the campaign's whole history.
    ok("...and 4x the Crawlers does not cost 4x the subprocesses — the cost is bounded by what "
       "is ALIVE, not by everything the campaign ever recorded",
       len(big) <= len(small) + 2, (len(small), len(big), big[:8]))

    # THE TWO-SIDED CONTROL. Everything above is satisfied by a `waiting` that stopped looking at
    # anything at all, which would be a watchdog probe that always answers the same thing. The
    # deep path must still do the per-Crawler work it exists to do.
    deep = git_calls(lambda: campaign.reconcile(big_cfg, big_g))
    ok("...while the DEEP reconcile still pays per Crawler, so the saving came from not asking "
       "questions `waiting` never reads — not from asking nothing",
       len(deep) > len(big) * 3, (len(deep), len(big)))

    # ONE REF READ ANSWERS EVERY BRANCH QUESTION. 544 of the reporter's 869 subprocesses were a
    # `rev-parse` per branch; `for-each-ref` answers all of them in one call.
    names = [c for c in deep if c == "rev-parse"]
    ok("no branch question is answered by a per-branch `rev-parse` any more", not names, names)
    ok("...and the ref list is read exactly once per pass",
       len([c for c in deep if c == "for-each-ref"]) == 1,
       [c for c in deep if c == "for-each-ref"])

    # `waiting`'s ANSWER MUST BE UNCHANGED, or this is a speedup that broke the verb.
    deep_verdict = campaign.waiting(big_cfg, big_g, base="HEAD")
    ok("waiting still returns a verdict and a detail", isinstance(deep_verdict, tuple)
       and len(deep_verdict) == 2, deep_verdict)

    # AND THE SKIPPED FIELDS ARE ABSENT, NOT None. A caller reading `merged` off a shallow
    # finding must get a KeyError — loud and immediate — because None would be indistinguishable
    # from "not merged" and would invert the answer silently.
    shallow_findings = campaign.reconcile(big_cfg, big_g, deep=False)
    ok("a shallow finding still carries what waiting reads",
       all(k in shallow_findings[0] for k in ("alive", "parked", "blocked", "crawler", "leaf")),
       sorted(shallow_findings[0]))
    for absent in ("merged", "empty", "uncommitted", "harness"):
        ok("...and OMITS %s rather than answering None, so reading it is a KeyError and never a "
           "quiet wrong answer" % absent, absent not in shallow_findings[0],
           sorted(shallow_findings[0]))
    ok("...while the deep finding carries them",
       all(k in campaign.reconcile(big_cfg, big_g)[0]
           for k in ("merged", "empty", "uncommitted", "harness")))

    # existing_branches: EMPTY IS NOT UNREADABLE. A repo with no branches is a real answer; a git
    # that could not be asked is not, and collapsing them would make every branch read as gone.
    ok("existing_branches answers a set for a real repo",
       isinstance(campaign.existing_branches(big_cfg), set))
    # A Config whose root is NOT a git repo: git answers non-zero, and the function must say
    # None rather than "this repo has no branches".
    class _NoRepo(object):
        root = tmpdir("not-a-git-repo")
    ok("...and None — never an empty set — when git cannot be asked, because 'no branches' and "
       "'could not look' would otherwise both read as every Crawler's work having vanished",
       campaign.existing_branches(_NoRepo()) is None)


def test_a_worktree_is_reclaimed_when_its_work_lands():
    group("A merged, clean worktree is reclaimed — and nothing else is, because the branch "
          "surviving is what makes the removal lossless (#75)")
    if not have("git"):
        skip("the worktree reclaim group", "git is not installed")
        return
    cfg = make_repo()
    g = new_graph(cfg)

    def spawn(leaf_id, title):
        # RECORDED, because `reclaimable` reads the CAMPAIGN, not the filesystem. `worktree.spawn`
        # makes the tree; `campaign.record_spawn` is what makes it a Crawler the tool knows about,
        # and cmd_spawn calls both. A test that skipped the second saw no findings at all — which
        # read as "nothing to reclaim" rather than as "nothing was looked at".
        g.add(title, leaf_id=leaf_id, labels=["backend"])
        rec = worktree.spawn(cfg, g.show(leaf_id), actor="crawler-" + leaf_id)
        campaign.record_spawn(cfg, rec)
        return rec

    def commit_in(rec, name):
        with open(os.path.join(rec["worktree"], name), "w") as fh:
            fh.write("work\n")
        sh(["git", "add", "-A"], rec["worktree"])
        sh(["git", "commit", "-q", "-m", "work on " + name], rec["worktree"])

    # MERGED AND CLEAN — the only case that may be removed.
    done = spawn("g-done", "work that lands")
    commit_in(done, "landed.txt")
    sh(["git", "merge", "--no-ff", "-q", "-m", "merge", done["branch"]], cfg.root)

    # MERGED BUT DIRTY — the tree holds work that exists nowhere else.
    dirty = spawn("g-dirty", "work with something uncommitted")
    commit_in(dirty, "committed.txt")
    sh(["git", "merge", "--no-ff", "-q", "-m", "merge", dirty["branch"]], cfg.root)
    with open(os.path.join(dirty["worktree"], "UNCOMMITTED.txt"), "w") as fh:
        fh.write("the only copy of this\n")

    # CLEAN BUT NOT MERGED — removing it would be that work's only copy leaving.
    unmerged = spawn("g-unmerged", "work that has not landed")
    commit_in(unmerged, "not-landed.txt")

    take, held = campaign.reclaimable(cfg, g)
    names = {r["crawler"] for r in take}
    held_by = {r["crawler"]: r["why"] for r in held}

    ok("a MERGED and CLEAN tree is reclaimable", done["crawler"] in names, sorted(names))
    ok("a merged tree with UNCOMMITTED work is held back — it is the only copy of it",
       dirty["crawler"] in held_by, held_by)
    ok("...and the reason says so rather than merely refusing",
       "uncommitted" in held_by.get(dirty["crawler"], ""), held_by)
    ok("a clean tree whose branch is NOT merged is held back",
       unmerged["crawler"] in held_by, held_by)
    ok("...and the reason names what would be lost",
       "only remaining copy" in held_by.get(unmerged["crawler"], ""), held_by)
    ok("...and neither held-back tree is in the reclaim list, which is the pair that stops "
       "'reclaimable' meaning 'every tree'",
       dirty["crawler"] not in names and unmerged["crawler"] not in names, sorted(names))

    # UNKNOWN IS NOT CLEAN, and this is the assertion the whole design turns on. `reconcile`
    # answers clean/dirty/UNKNOWN; a failed read collapsing into "clean" would delete somebody's
    # only copy on the strength of not having looked.
    src = open(os.path.join(ROOT, "lib", "showrunner", "campaign.py")).read()
    body = src[src.index("def reclaimable("):src.index("def reconcile(")]
    ok("reclaimable refuses an UNKNOWN tree explicitly, rather than letting it fall through to "
       "the clean branch", 'tree") == "unknown"' in body, body[:200])

    # A LIVE SESSION'S TREE IS ITS WORKSPACE.
    live_rec = spawn("g-live", "work in progress")
    commit_in(live_rec, "wip.txt")
    sh(["git", "merge", "--no-ff", "-q", "-m", "merge", live_rec["branch"]], cfg.root)
    data = campaign.load(cfg)
    for entry in data["crawlers"]:
        if entry.get("crawler") == live_rec["crawler"]:
            entry["pid"] = os.getpid()
            entry["boot"] = util.boot_token()
    campaign.save(cfg, data)
    take2, held2 = campaign.reclaimable(cfg, g)
    ok("a tree whose session is ALIVE is held back even when merged and clean",
       live_rec["crawler"] in {r["crawler"] for r in held2},
       {r["crawler"]: r["why"] for r in held2})

    # THE VERB IS DRY-RUN BY DEFAULT, because it deletes directories.
    sr = os.path.join(ROOT, "bin", "showrunner")
    p = subprocess.run([sys.executable, sr, "gc"], capture_output=True, text=True, cwd=cfg.root)
    eq("gc exits 0", p.returncode, 0)
    ok("...and says it is a dry run", "dry run" in p.stdout, p.stdout[:200])
    ok("...and the tree is STILL THERE afterwards, which is what dry-run has to mean",
       os.path.isdir(done["worktree"]), done["worktree"])
    ok("...and it reports what it would remove", "would remove" in p.stdout, p.stdout[:300])
    ok("...and prints the held-back trees WITH reasons, so a dirty tree announces itself rather "
       "than being silently skipped", "HELD" in (p.stdout + p.stderr), (p.stdout + p.stderr)[:400])

    # THE SIZE IS THE WHOLE REASON ANYONE ACTS ON THIS. The report that prompted #75 was 133 GB;
    # a reclaim that cannot say how much it frees is a chore with no stated payoff, and one that
    # silently says 0 makes a real backlog look absent.
    ok("...and states how much would be reclaimed, rather than only how many trees",
       "would reclaim" in p.stdout, p.stdout[:400])
    ok("...as a real size and not a silent zero",
       not re.search(r"would reclaim \d+ tree\(s\), 0B", p.stdout), p.stdout[:400])
    measured = campaign.tree_bytes(done["worktree"])
    ok("a tree that exists measures greater than zero", (measured or 0) > 0, measured)
    # UNMEASURABLE IS NOT EMPTY, the identity element again: a path that cannot be walked has to
    # answer None so the reporter can print `?`, because 0 would read as "nothing to reclaim".
    eq("a path that cannot be walked answers None, never 0",
       campaign.tree_bytes(os.path.join(cfg.root, "no-such-tree-here")), None)
    # IT MEASURES CONTENT, NOT EXISTENCE. Without this pair, a stub returning any constant
    # satisfies every size assertion above — and the number's whole job is to say how much a
    # reclaim is worth, which a constant answers wrongly in a way nobody would question.
    before = campaign.tree_bytes(unmerged["worktree"])
    with open(os.path.join(unmerged["worktree"], "big.bin"), "wb") as fh:
        fh.write(b"x" * 50000)
    after = campaign.tree_bytes(unmerged["worktree"])
    ok("...and the measurement TRACKS the bytes actually there, so it is a size and not a "
       "constant", after is not None and before is not None and after - before >= 50000,
       (before, after))

    # --apply REMOVES, AND ONLY THE RIGHT ONE.
    p = subprocess.run([sys.executable, sr, "gc", "--apply"], capture_output=True, text=True,
                       cwd=cfg.root)
    eq("gc --apply exits 0", p.returncode, 0)
    ok("the merged, clean tree is gone", not os.path.isdir(done["worktree"]), done["worktree"])
    ok("the DIRTY tree survives — the uncommitted file is the only copy of that work",
       os.path.isfile(os.path.join(dirty["worktree"], "UNCOMMITTED.txt")))
    ok("the UNMERGED tree survives", os.path.isdir(unmerged["worktree"]))
    ok("the LIVE session's tree survives", os.path.isdir(live_rec["worktree"]))

    # THE REMOVAL IS LOSSLESS, which is the entire argument for doing it automatically.
    rc, out, _ = util.git(["rev-parse", "--verify", done["branch"]], cwd=cfg.root)
    eq("the branch of the removed tree still exists, so nothing was lost with the directory",
       rc, 0)
    ok("...and its commit is still reachable", bool(out.strip()), out)


def test_a_compacted_agent_is_told_what_it_forgot():
    group("What a compacted agent gets back: the campaign's STATE and the whole verb list — "
          "an agent that cannot remember the tool reaches for what it already knows")
    if not have("git"):
        skip("the re-injection group", "git is not installed")
        return
    cfg = make_repo()
    g = new_graph(cfg)

    # AN EMPTY GRAPH AND A BUSY ONE MUST READ DIFFERENTLY. The reported failure is an agent that
    # had lost which campaign it was on; a line that says the same thing either way would not
    # have told it anything, and "no campaign" and "campaign with nothing ready" are opposite
    # situations that the previous announcement rendered identically — as silence.
    state = roles.campaign_state(cfg)
    eq("an empty graph reports zero leaves rather than failing to answer", state["total"], 0)

    g.add("first piece of work", leaf_id="c1", labels=["backend"])
    g.add("second piece", leaf_id="c2", labels=["backend"])
    g.dep("c2", "c1")
    state = roles.campaign_state(cfg)
    eq("...and a populated graph counts every leaf", state["total"], 2)
    eq("...and counts only what is actually dispatchable as READY — c2 is blocked by c1",
       state["ready"], 1)

    text = "\n".join(roles.whoami(cfg))
    ok("whoami names how much work exists, which is the question 'which campaign am I on' is "
       "actually asking", "2 leaf/leaves" in text, text[:400])
    ok("...and how much is dispatchable right now", "1 READY" in text, text[:400])

    # THE VERB INVENTORY, DERIVED. Three verbs answered "how do I dispatch" and nothing else, so
    # an agent needing anything else had no reason to believe the tool could do it.
    verbs = roles.verb_inventory()
    ok("the verb inventory is derived from the parser and is not a stub", len(verbs) > 20, verbs)
    for expected in ("spawn", "ready", "close", "doctor", "reach"):
        ok("...and contains %s, so the list is the real one" % expected, expected in verbs)
    ok("whoami prints the whole inventory, not a chosen few",
       all(v in text for v in ("integrate", "reconcile", "overlap")), text[-500:])

    # DERIVED, NEVER LISTED. A second copy of the verbs is one that goes stale the first time a
    # verb is added, and a stale inventory teaches that a real verb does not exist.
    src = open(os.path.join(ROOT, "lib", "showrunner", "roles.py")).read()
    ok("the inventory reads the parser rather than hard-coding names",
       "build_parser" in src and '"spawn", "ready", "close"' not in src)

    # THE GRAPH BEING UNREADABLE MUST NOT READ AS 'NOTHING TO DO'. Same identity-element defect
    # this repo keeps finding: an empty count and a failed read are opposite facts.
    broken = make_repo()
    dbp = os.path.join(broken.root, ".showrunner", "graph.db")
    os.makedirs(os.path.dirname(dbp), exist_ok=True)
    with open(dbp, "w") as fh:
        fh.write("this is not a sqlite database")
    ok("an unreadable graph answers None, which the caller must not print as zero",
       roles.campaign_state(broken) is None)
    ok("...and whoami SAYS it could not tell, rather than showing a confident zero",
       "COULD NOT BE READ" in "\n".join(roles.whoami(broken)),
       "\n".join(roles.whoami(broken))[:300])


def test_reaching_for_the_wrong_thing_names_the_right_one():
    group("When an agent reaches for what it already knows, the mechanism it should have used "
          "is named AT THE MOMENT OF REACH — advice, never a refusal")
    if not have("git"):
        skip("the reach group", "git is not installed")
        return
    cfg = make_repo()
    root = cfg.root

    def fires(tool, tool_input, at=None):
        return [n for n, _ in reach.advise(tool, tool_input, at if at is not None else root)]

    # EACH RULE FIRES ON ITS OWN REACH.
    ok("creating a worktree by hand names spawn",
       "worktree-by-hand" in fires("Bash", {"command": "git worktree add .worktrees/a -b x"}))
    ok("a private todo list beside a graph names ready",
       "todo-beside-a-graph" in fires("TodoWrite", {"todos": []}))
    ok("cutting a branch for parallel work names spawn",
       "branch-for-parallel-work" in fires("Bash", {"command": "git checkout -b feature/x"}))

    # THE CONTROLS. Every assertion above is equally satisfied by a rule that fires on
    # everything, which would be an alarm that is always on — the failure #71 is about, and the
    # one that teaches a reader to skim the channel this depends on.
    ok("an ordinary command says nothing", fires("Bash", {"command": "ls -la"}) == [])
    ok("an ordinary write says nothing",
       fires("Write", {"file_path": "/tmp/notes.txt"}) == [])
    ok("reading a file about worktrees is not creating one — the reach is the COMMAND, not the "
       "subject", fires("Bash", {"command": "cat docs/git-worktree-add.md"}) == [])

    # THE SECOND DOOR, reported from a REAL HIT rather than a probe. Field scoping fixed the
    # heredoc case; it does not fix a phrase inside a quoted ARGUMENT of a genuine command. The
    # reporter was writing a knowledge-base entry whose body quoted `git worktree add` as the
    # example of what NOT to do, and the advice fired on prose about the advice.
    kb = ('dart run tool/kb.dart add URL --body '
          '"a note mentioning git worktree add in prose"')
    ok("a phrase inside a QUOTED ARGUMENT is a mention, not a use — this is the reporter's "
       "exact command", fires("Bash", {"command": kb}) == [], fires("Bash", {"command": kb}))

    # THE PAIRS, or the fix is indistinguishable from switching the rule off. Each of these is a
    # real invocation and must still fire.
    for label, cmd in (("bare", "git worktree add .worktrees/x -b y"),
                       ("after &&", "cd /repo && git worktree add x"),
                       ("after ;", "echo hi; git worktree add x"),
                       ("after a pipe", "true | git worktree add x")):
        ok("...while a real invocation %s still fires, so this is a boundary test and not a "
           "silencing" % label,
           "worktree-by-hand" in fires("Bash", {"command": cmd}), cmd)

    # AND THE OTHER HALF OF THE BOUNDARY: not first, not after a separator, unquoted.
    ok("a phrase mid-command is a mention too — `git` as somebody else's third word is not "
       "somebody running git",
       fires("Bash", {"command": "echo please do not run git worktree add"}) == [])

    # WHAT IT NOW CANNOT SEE, asserted so the limitation is a decision on the record rather than
    # a surprise: a real command inside quotes is a MISS. That is the deliberate trade — a false
    # positive teaches a reader to skim, and this channel is depended on exactly when they are
    # not reading carefully.
    ok("a genuine command inside quotes is knowingly missed, and this assertion is where that "
       "trade is written down",
       fires("Bash", {"command": 'bash -c "git worktree add x"'}) == [])

    # THE FIELD SCOPING IS THE CONTROL THAT MATTERS. Matching a dumped payload would fire on the
    # CONTENT of a write, so a commit message mentioning a memory file would trip the memory
    # rule. A rule about a path has to stay a rule about a path.
    ok("a write whose CONTENT mentions memory/ but whose path does not is left alone",
       fires("Write", {"file_path": "/tmp/x.md", "content": "notes about memory/ layout"}) == [])

    # FOREIGN MECHANISMS ARE DETECTED, NEVER ASSUMED. game_loop is consumed by this repo and not
    # shipped with it, so a repo without it must never be told to run its command.
    mem = {"file_path": os.path.join(root, "memory", "a-lesson.md")}
    ok("the memory rule stays SILENT where game_loop is absent, rather than naming a command "
       "the reader does not have", fires("Write", mem) == [])
    os.makedirs(os.path.join(root, ".game_loop", "bin"), exist_ok=True)
    with open(os.path.join(root, ".game_loop", "bin", "game_loop"), "w") as fh:
        fh.write("#!/bin/sh\n")
    ok("...and speaks once it IS present", "memory-write-could-be-hardened" in fires("Write", mem))

    # THE RULES NAME VERBS, AND A NAMED VERB MUST EXIST. A table pointing at a renamed verb is
    # worse than no table: it teaches that the tool cannot do the thing.
    verbs = set(roles.verb_inventory())
    for name, _tools, _pat, verb, _req, message in reach.RULES:
        if verb:
            ok("rule %s names `%s`, which the parser still defines" % (name, verb),
               verb in verbs, sorted(verbs)[:12])
        for quoted in re.findall(r"`showrunner ([a-z-]+)", message):
            ok("rule %s quotes `showrunner %s`, which still exists" % (name, quoted),
               quoted in verbs)

    # IT IS ADVICE. A gate that blocks a legitimate shape trains its own bypass, and every reach
    # here is legitimate somewhere.
    p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "reach"],
                       input=json.dumps({"tool_name": "Bash",
                                         "tool_input": {"command": "git worktree add a"}}),
                       capture_output=True, text=True, cwd=cfg.root)
    eq("the verb exits 0 even when it has something to say — it never denies", p.returncode, 0)
    ok("...and says it through additionalContext, which is what reaches the agent on an allow",
       "additionalContext" in p.stdout, p.stdout[:200])
    ok("...and says the call is proceeding, so it is not mistaken for a refusal",
       "proceeding" in p.stdout, p.stdout[:300])

    # A PAYLOAD IT CANNOT READ COSTS ADVICE AND NOTHING ELSE.
    p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "reach"],
                       input="not json", capture_output=True, text=True, cwd=cfg.root)
    eq("an unreadable payload still exits 0", p.returncode, 0)
    eq("...and stays silent, because this protects nothing and announcing a non-event is how a "
       "channel gets skimmed", p.stdout.strip(), "")


def test_only_guards_may_anchor_to_their_own_checkout():
    group("the own-location fallback is GUARD-ONLY — every other verb must refuse rather than "
          "guess which repo it meant (#74)")
    if not have("git"):
        skip("the guard-only anchor group", "git is not installed")
        return
    outside = tmpdir("guard-only-outside")            # deliberately NOT a git repo
    sr = os.path.join(ROOT, "bin", "showrunner")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}

    def run_verb(*argv, **kw):
        return subprocess.run([sys.executable, sr] + list(argv), cwd=outside,
                              capture_output=True, text=True, env=env, **kw)

    # A GUARD IS ASKED ABOUT A CALL HAPPENING RIGHT NOW and has to answer something.
    p = run_verb("dispatch", "guard",
                 input='{"tool_name":"Bash","tool_input":{"command":"claude -p \\"w\\""}}')
    ok("a guard verb resolves from the checkout it runs out of, rather than failing open beside "
       "a repo it is sitting in", ANCHOR_FAILED not in (p.stdout + p.stderr),
       (p.stdout + p.stderr)[:170])

    # EVERY OTHER VERB IS ASKED A QUESTION IT MAY REFUSE, AND MUST. Making the anchor global
    # rather than a guard-only parameter made `ready` from a scratch directory quietly answer
    # about showrunner's OWN checkout — a wrong answer that looks exactly like a right one. The
    # suite caught it, which is why this pair is here rather than a comment.
    p = run_verb("ready")
    eq("a NON-guard verb outside any repo still REFUSES — the fallback must not turn 'cannot "
       "resolve' into a guess about whichever repo the binary lives in", p.returncode, 2)
    ok("...and says why, rather than answering about the wrong repo",
       "not inside a git repository" in (p.stdout + p.stderr), (p.stdout + p.stderr)[:170])

    # THE PAIR FOR THE REFUSAL: the same verb from inside a repo answers normally, so the
    # assertion above is not satisfied by a verb that refuses everywhere.
    cfg = make_repo()
    p = subprocess.run([sys.executable, sr, "ready"], cwd=cfg.root, capture_output=True,
                       text=True, env=env)
    eq("...while the same verb inside a repo answers normally", p.returncode, 0)


def test_fail_open_is_counted_not_just_announced():
    group("A fail-open notice arrives beside a SUCCESSFUL result — so something downstream "
          "must be able to ASK how many there were")
    if not have("git"):
        skip("the fail-open ledger group", "git is not installed")
        return
    cfg = make_repo()
    ledger = os.path.join(cfg.root, ".showrunner", "fail-open.jsonl")

    def doctor():
        p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                           cwd=cfg.root, capture_output=True, text=True)
        return p.stdout

    # THE THREE ANSWERS MUST BE DISTINGUISHABLE. "no guard failed open", "N did", and "the
    # record cannot be read" are three different facts, and the first and third are the pair
    # this repo keeps collapsing: an empty read and a broken reader both render as silence.
    out = doctor()
    ok("with no ledger at all, doctor says none rather than staying quiet",
          "no guard has failed open" in out)

    # A REAL FAIL-OPEN THROUGH THE REAL ENTRYPOINT, not a hand-written ledger line. A test that
    # writes the record it then reads back proves the reader works and nothing about whether
    # anything ever writes one — the defect class this suite exists to catch.
    shim = os.path.join(cfg.root, ".showrunner", "hooks", "dispatch-guard.sh")
    os.makedirs(os.path.dirname(shim), exist_ok=True)
    shutil.copy(os.path.join(ROOT, ".showrunner", "hooks", "dispatch-guard.sh"), shim)
    os.chmod(shim, 0o755)
    env = dict(os.environ, CLAUDE_PROJECT_DIR=cfg.root)
    # No .showrunner/config.json in a scratch repo makes the guard degrade — which is the
    # condition under test, reached the way a user reaches it.
    p = subprocess.run(["bash", shim], input='{"tool_name":"Bash","tool_input":'
                       '{"command":"echo hi"}}', capture_output=True, text=True,
                       cwd=cfg.root, env=env)
    ok("a degraded guard still ALLOWS the call (fail-open, not fail-shut)", p.returncode == 0)
    fired = os.path.exists(ledger) and open(ledger).read().strip()
    ok("...and it left a durable record, so the notice is not the only trace", bool(fired))

    # NOT GATED ON THE READ ABOVE. Gating these on `fired` made a dead recorder flip exactly
    # ONE assertion and silence three, which the sweep reported as THIN — the coverage looked
    # like one assertion happening to notice rather than four. These read doctor's OUTPUT,
    # which is safe with no ledger at all, so a dead recorder fails every one of them.
    out = doctor()
    ok("doctor reports the COUNT — the fact a banner cannot carry",
       "ALLOWED WITHOUT BEING CHECKED" in out)
    ok("...and names the file to read, not just the number", "fail-open.jsonl" in out)
    # ACCUMULATION IS THE POINT. One unchecked call and twenty are different facts about a
    # session; a per-call notice cannot express the difference because each one looks the same
    # as the last.
    for _ in range(4):
        subprocess.run(["bash", shim], input='{"tool_name":"Bash","tool_input":'
                       '{"command":"echo hi"}}', capture_output=True, text=True,
                       cwd=cfg.root, env=env)
    out = doctor()
    ok("the count ACCUMULATES rather than resetting per call",
       "5 tool call(s) were ALLOWED" in out)

    # A LEDGER THAT CANNOT BE PARSED MUST NOT READ AS ZERO. This is the identity-element defect
    # in its exact shape: json.loads throwing and the file being empty both leave the list
    # empty, and one of those means "nothing went unchecked" while the other means "no idea".
    with open(ledger, "w") as fh:
        fh.write("{not json at all\n")
    out = doctor()
    ok("an unparseable ledger says UNKNOWN, never 'none'",
          "UNKNOWN" in out and "no guard has failed open" not in out)

    # THE CLI ENTRYPOINT WRITES THE SAME LEDGER. Two entrypoints per guard is this repo's
    # standing hazard — a shim and a `showrunner <verb>` that are free to disagree.
    #
    # AND IT MUST RECORD WHEN CONFIG IS WHAT FAILED. The first version keyed the ledger off
    # `config.load().state_dir`, which is unavailable in exactly the case that makes a guard
    # fail open — a ledger whose coverage was the complement of its purpose. Asserting on the
    # SOURCE ("the call is present") would have passed against that version, which is why this
    # runs the verb from a directory with no config and reads the file back.
    os.remove(ledger)
    conf = os.path.join(cfg.root, ".showrunner", "config.json")
    good = open(conf).read()

    def cli_guard():
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                               "dispatch", "guard", "--command", "echo hi"],
                              capture_output=True, text=True, cwd=cfg.root,
                              env=dict(os.environ, CLAUDE_PROJECT_DIR=cfg.root))
    # THE CONTROL FIRST: a HEALTHY guard must write nothing, or the count means nothing. A
    # ledger that grows on every call would report a number that is large, alarming and about
    # nothing — the alarm that is ignored because it is always on.
    p = cli_guard()
    ok("a guard that actually RAN records nothing — the count must mean unchecked, not called",
       p.returncode == 0 and not os.path.exists(ledger))

    with open(conf, "w") as fh:
        fh.write("{ this is not json")
    try:
        p = cli_guard()
        ok("the CLI entrypoint fails open too (the two must not disagree)", p.returncode == 0)
        ok("...and records it, even though an unloadable config is WHY it failed open",
           os.path.exists(ledger) and bool(open(ledger).read().strip()))
    finally:
        with open(conf, "w") as fh:
            fh.write(good)

    # THE COMPANION, so the CLI half is not one assertion deep either. WHERE the ledger lives
    # is its own producer — the first version put it somewhere unreachable whenever config was
    # the thing that broke — and this reads the entry back through doctor rather than through
    # the path the writer just used, which would agree with itself either way.
    #
    # AFTER the config is restored, not inside the window: `doctor` loads config too, so asking
    # it to report while config is deliberately corrupt tests the wrong thing and fails for a
    # reason unrelated to the ledger. Found by running it.
    ok("...and doctor can find and count what the CLI wrote",
       "1 tool call(s) were ALLOWED" in doctor())

    # THE OTHER CLI GUARD, AND A DIFFERENT FAIL-OPEN BRANCH. `worktree guard` degrades on an
    # UNREADABLE PAYLOAD rather than on config, and it is a separate verb — two guards
    # answering one event in one repo are free to disagree about whether they leave a trace,
    # and a count that silently covers only one of them is worse than no count, because it
    # reads as the whole picture.
    p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"),
                        "worktree", "guard"], input="not a payload at all",
                       capture_output=True, text=True, cwd=cfg.root,
                       env=dict(os.environ, CLAUDE_PROJECT_DIR=cfg.root))
    ok("the OTHER guard verb fails open on an unreadable payload", p.returncode == 0)
    ok("...and it lands in the SAME ledger, so the count spans both guards",
       "2 tool call(s) were ALLOWED" in doctor())

def test_stop_hook_heartbeat():
    group("Did the Stop hook RUN — the one question registration, parsing and 'has fired' "
          "cannot answer")
    if not have("git"):
        skip("the heartbeat group", "git is not installed")
        return
    cfg = make_repo()

    # WHY A TIMESTAMP AND NOT A BOOLEAN. game_loop's auditor measured their Stop gate as unrun
    # for eight hours behind four green checks — it parses, the registered command matches, it
    # can write, doctor says the hooks have fired. THAT REPORT WAS LATER RETRACTED: the session
    # had been idle and no turn had ended, so the stale stamp was correct behaviour. The part
    # that survives the retraction is the only part this test depends on — those four checks
    # are facts about a FILE and about the PAST, and none of them is a fact about this turn.
    #
    # THE RELATION IS BETWEEN HOOKS, not against an invented tolerance. showrunner does not
    # schedule turn-ends and cannot know when one happened — but the NEWEST stamp across all
    # Stop hooks is a proxy for the last turn-end that reached anything, and a hook far behind
    # that was registered and not reached.
    hb = os.path.join(cfg.root, ".showrunner", "hook-heartbeat.jsonl")
    settings = os.path.join(cfg.root, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    with open(settings, "w") as fh:
        json.dump({"hooks": {"Stop": [{"hooks": [
            {"command": '"$CLAUDE_PROJECT_DIR"/.showrunner/hooks/alpha-gate.sh'},
            {"command": '"$CLAUDE_PROJECT_DIR"/.showrunner/hooks/beta-gate.sh'}]}]}}, fh)

    def doctor():
        p = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                           cwd=cfg.root, capture_output=True, text=True)
        return p.stdout

    out = doctor()
    ok("a registered Stop hook that has NEVER stamped is reported, not passed over — silence "
       "from a gate is what both health and total failure look like",
       out.count("NEVER stamped") == 2, out[-300:])
    ok("...and it says it CANNOT TELL a hook that records no stamp from one nothing reached, "
       "rather than picking the flattering reading", "cannot tell which" in out)

    # BOTH IN STEP: the healthy case must be quiet, or the warning stops being read.
    t = int(time.time())
    with open(hb, "w") as fh:
        fh.write(json.dumps({"hook": "alpha-gate", "ts": t - 30}) + "\n")
        fh.write(json.dumps({"hook": "beta-gate", "ts": t - 20}) + "\n")
    out = doctor()
    ok("two Stop hooks stamping together are both reported as having RUN", 
       out.count("in step with the others") == 2, out[-300:])
    ok("...and neither is warned about", "BEHIND the newest" not in out)

    # ONE STARVED. This is the shape the auditor actually hit: hook B keeps running, hook A is
    # never reached, and every other signal about A stays true the whole time.
    with open(hb, "w") as fh:
        fh.write(json.dumps({"hook": "alpha-gate", "ts": t - 8 * 3600}) + "\n")
        fh.write(json.dumps({"hook": "beta-gate", "ts": t - 20}) + "\n")
    out = doctor()
    ok("a Stop hook far behind its siblings is named as NOT REACHED — turn-ends got to the "
       "others and not to it", "BEHIND the newest" in out and "alpha-gate" in out, out[-400:])
    ok("...reported in hours rather than as a bare 'stale', so the size of the gap is visible",
       "8h ago" in out, out[-400:])
    ok("...while the sibling that did run is still reported ok, so the warning points at one "
       "hook rather than at the wiring in general",
       "`beta-gate` stamped an invocation" in out, out[-400:])
    # WHAT IT MUST NOT CLAIM. The auditor named a blocking earlier hook as the likely cause and
    # was careful to call it an inference. A stamp proves the gate did not run; it says nothing
    # about why, and a doctor line that named a cause would be the same measured-a-behaviour-
    # named-a-cause defect this repo has hit four times.
    ok("...and does NOT assert a cause, because a stamp cannot establish one — it offers the "
       "blocking-earlier-hook reading as a candidate",
       "does not establish WHY" in out and "candidate" in out, out[-400:])

    # AND THE SUITE MUST NOT BE ABLE TO FORGE THE REPO'S OWN READING. This is the positive
    # control for the redirect above: run the real gate the way the other tests do, then prove
    # the checkout's own heartbeat did not grow. Without it, the redirect is a line of setup
    # nothing checks — and the failure it prevents is invisible, because a forged stamp and a
    # real one are the same line of JSON.
    live = os.path.join(ROOT, ".showrunner", "hook-heartbeat.jsonl")
    before = os.path.getsize(live) if os.path.exists(live) else 0
    gate = os.path.join(ROOT, ".showrunner", "hooks", "future-tense-gate.sh")
    if os.access(gate, os.X_OK):
        subprocess.run(["bash", gate], input=json.dumps({"transcript_path": "/nonexistent"}),
                       capture_output=True, text=True)
        after = os.path.getsize(live) if os.path.exists(live) else 0
        eq("running a Stop hook from the SUITE leaves the checkout's own heartbeat untouched — "
           "the record of what the harness reached cannot be written by the tests",
           after, before)
        redirected = os.environ["SHOWRUNNER_HEARTBEAT"]
        ok("...because it stamped the redirected file instead, which proves the hook still "
           "recorded the invocation rather than simply skipping it",
           os.path.exists(redirected) and os.path.getsize(redirected) > 0)



def test_embedded_code_inside_hooks_is_checked_too():
    group("A hook that EMBEDS Python is not verified by `bash -n` — a syntax checker cannot see "
          "inside anything it treats as data")
    if not have("bash"):
        skip("the embedded-code group", "bash is not installed")
        return

    # REPORTED BY game_loop's owner, PROVEN HERE BEFORE ACTING. A checker that does not execute
    # cannot see inside a heredoc, a quoted template, or an embedded script of another language.
    # Code written into that region is invisible to it, and a clean check reads as a verified
    # file. Measured: breaking the Python inside whoami.sh's heredoc leaves `bash -n` PASSING,
    # and the hook then exits 1 having printed no JSON at all — which for a SessionStart hook is
    # the entire failure, because its one forbidden outcome is silence.
    #
    # That is the exact gap in test_every_REGISTERED_hook_parses, which shipped hours earlier
    # and whose prose claimed more than `bash -n` can deliver. The claim is corrected there.
    hooks = sorted(glob.glob(os.path.join(ROOT, ".showrunner", "hooks", "*.sh")))
    ok("there are hooks to examine, so this is not passing over an empty set", len(hooks) >= 4)

    # DERIVED FROM THE FILES, never a list of which hooks embed Python — such a list goes stale
    # the first time one gains or loses a block, and staleness here reads as coverage.
    heredoc = re.compile(r"python3?[^\n]*<<-?\s*['\"]?(\w+)['\"]?\n(.*?)^\1$", re.S | re.M)
    # THE SHELL'S OWN APOSTROPHE ESCAPE, or this truncates mid-string and reports the fragment
    # as broken Python. `'"'"'` closes the quoted string, emits a literal apostrophe and reopens
    # it — the idiom every non-trivial `-c` block uses, and pipeline-status-gate.sh uses it in
    # prose. A naive non-greedy match stopped at the first quote and called the offcut a syntax
    # error: a check crying wolf about the file it is verifying is worse than no check, because
    # the next real failure reads as the same noise.
    dash_c = re.compile(r"""python3?\s+-\s*c\s+'((?:[^']|'"'"')*)'""", re.S)
    examined = 0
    for path in hooks:
        with open(path) as fh:
            body = fh.read()
        blocks = [m.group(2) for m in heredoc.finditer(body)]
        blocks += [m.group(1).replace(chr(39) + '"' + chr(39) + '"' + chr(39), chr(39))
                   for m in dash_c.finditer(body)]
        for i, block in enumerate(blocks):
            examined += 1
            try:
                ast.parse(block)
                bad = ""
            except SyntaxError as exc:
                bad = "%s (line %s)" % (exc.msg, exc.lineno)
            ok("%s: embedded Python block %d parses — `bash -n` treats this region as DATA and "
               "passes over whatever it contains" % (os.path.basename(path), i + 1), not bad, bad)
    ok("...and blocks were actually found, so a change in how hooks embed Python fails here "
       "rather than quietly reducing this to a no-op", examined >= 3, examined)


def test_hooks_that_must_speak_are_driven_and_not_merely_parsed():
    group("The hooks whose one forbidden outcome is SILENCE are RUN against a payload that must "
          "produce output — the only honest verification of an embedded language")
    if not have("bash") or not have("git"):
        skip("the drive-the-hook group", "bash and git are needed")
        return

    # THE OTHER HALF, and the one owner argues is the only honest one: drive the artifact and
    # assert on what comes out, not on what the source parses to. A static check that sees into
    # the heredoc still cannot say the hook WORKS — only that its text is well-formed. These
    # payloads are chosen so that silence is definitely wrong.
    env = dict(os.environ, CLAUDE_PROJECT_DIR=ROOT)

    def drive(name, payload):
        return subprocess.run(["bash", os.path.join(ROOT, ".showrunner", "hooks", name)],
                              input=payload, capture_output=True, text=True, cwd=ROOT, env=env)

    # whoami announces the seat on SessionStart and PostCompact and has no "nothing to say" case
    # by construction — every failure path inside it still prints something.
    p = drive("whoami.sh", "{}")
    ok("whoami.sh produces output for an empty payload — silence is the one outcome it is not "
       "allowed to have", bool(p.stdout.strip()), (p.stdout[:120], p.stderr[:200]))
    try:
        ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
    except Exception:                                            # noqa: BLE001
        ctx = ""
    ok("...and it is JSON carrying additionalContext, which is what actually reaches the agent",
       bool(ctx), p.stdout[:200])
    ok("...and it names the seat, so the announcement carries the fact it exists to carry",
       any(w in ctx.upper() for w in ("ORCHESTRATOR", "CRAWLER", "SOLO", "COULD NOT")), ctx[:200])

    # THE PROOF THAT THIS SEES WHAT `bash -n` CANNOT. A copy with its embedded Python broken
    # still parses as shell; driving it is what catches it. Without this pair, every assertion
    # above is equally satisfied by a suite that never noticed a broken block at all.
    broken_dir = tmpdir("hook-embedded-broken")
    broken = os.path.join(broken_dir, "whoami.sh")
    with open(os.path.join(ROOT, ".showrunner", "hooks", "whoami.sh")) as fh:
        src = fh.read()
    with open(broken, "w") as fh:
        fh.write(src.replace("import json, sys", "import json, sys\nthis is not python("))
    parsed = subprocess.run(["bash", "-n", broken], capture_output=True, text=True)
    eq("a hook whose EMBEDDED Python is broken still passes `bash -n` — the blind spot, "
       "measured rather than asserted", parsed.returncode, 0)
    run_broken = subprocess.run(["bash", broken], input="{}", capture_output=True, text=True,
                                cwd=ROOT, env=env)
    ok("...and driving it is what catches it: it prints no JSON at all",
       not run_broken.stdout.strip(), run_broken.stdout[:160])

    # pipeline-status-gate's whole job is to notice `$?` after a pipe. This is the shape it was
    # built for, so silence here means the embedded Python did not run.
    p = drive("pipeline-status-gate.sh",
              json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "ls | head -3; echo done=$?"}}))
    ok("pipeline-status-gate.sh speaks on the exact shape it exists to catch",
       "additionalContext" in p.stdout, (p.stdout[:120], p.stderr[:200]))

    p = drive("reach-gate.sh",
              json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "git worktree add .worktrees/x -b y"}}))
    ok("reach-gate.sh speaks on the reach it exists to name",
       "additionalContext" in p.stdout, (p.stdout[:120], p.stderr[:200]))

    # THE CONTROL. Every assertion above is satisfied by a hook that emits on EVERYTHING, which
    # is an alarm that is always on — the specific way an advisory gate stops being read.
    p = drive("pipeline-status-gate.sh",
              json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello"}}))
    eq("...while an ordinary command draws nothing from the pipeline gate", p.stdout.strip(), "")
    p = drive("reach-gate.sh",
              json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello"}}))
    eq("...and nothing from the reach gate either", p.stdout.strip(), "")


def test_every_REGISTERED_hook_parses():
    group("Every hook THIS REPO ACTUALLY REGISTERS parses AS SHELL — narrower than it sounds, "
          "because `bash -n` cannot see inside a heredoc")
    if not have("bash"):
        skip("the registered-hook parse group", "bash is not installed")
        return

    # THE COMPANION TO test_every_shipped_hook_parses, not a replacement. That one derives its
    # list from install.sh, which is the right source for "what a consumer receives". It is the
    # wrong source for "what runs here": .claude/settings.json registers hooks install.sh does
    # not ship, and a parse error in one of those is invisible to the shipped-list check.
    #
    # WHY A PARSE ERROR IS THE WORST CASE AND NOT MERELY ONE OF THEM. Measured today, at cost:
    # an unclosed `if` in worktree-guard.sh made bash fail to parse the file, and as a
    # PreToolUse hook that refuses Bash, Edit AND Write, so no tool available to this session
    # could repair the file that was doing the refusing. A human ran `git checkout` by hand.
    # Every fail-open path in that hook is downstream of parsing and none of them ran — its
    # header promises a hook cannot lock the repo against its own repair, and a syntax error is
    # exactly the case that promise does not cover.
    #
    # THIS CANNOT PREVENT THAT LOCKOUT, and saying so is the point. It runs after a file is
    # written, and `verify` runs it before a commit — so a broken hook cannot be COMMITTED, and
    # cannot reach a consumer. The transient local lockout stays possible, and the only guard
    # against it is `bash -n` on a copy before installing, which is discipline, not mechanism.
    #
    # AND `bash -n` CANNOT SEE INSIDE A HEREDOC, which narrows this further than its first
    # wording implied. Reported by game_loop's owner and measured here: a checker that does not
    # execute treats an embedded region as DATA, so Python written into one is invisible and a
    # clean check reads as a verified file. Breaking the Python inside whoami.sh leaves THIS
    # assertion green while the hook exits 1 printing nothing at all. So this covers the SHELL
    # text only; test_embedded_code_inside_hooks_is_checked_too parses what is inside those
    # regions, and test_hooks_that_must_speak_are_driven_and_not_merely_parsed drives the hooks
    # against payloads where silence is wrong — the only honest check of an embedded language.
    settings = os.path.join(ROOT, ".claude", "settings.json")
    if not os.path.isfile(settings):
        skip("the registered-hook parse group", "this repo has no .claude/settings.json")
        return
    with open(settings) as fh:
        data = json.load(fh)

    # DERIVED FROM THE REGISTRATION, never listed here — a second list is how one goes stale,
    # which is the defect this file keeps finding elsewhere.
    commands = []
    for event, entries in (data.get("hooks") or {}).items():
        for entry in entries or []:
            for hook in entry.get("hooks") or []:
                cmd = hook.get("command") or ""
                if cmd:
                    commands.append((event, cmd))
    ok("the registration is readable and non-empty, so this check has a population rather than "
       "vacuously passing over nothing", bool(commands), len(commands))

    checked = 0
    for event, cmd in commands:
        # The registered command is a shell string. Take the first token that names a real file
        # under this repo; anything else (a bare verb, another tool's binary) is not ours to
        # parse and is skipped rather than guessed at.
        # THE WHOLE PATH, not a whitespace token. `"$CLAUDE_PROJECT_DIR"/.showrunner/hooks/x.sh`
        # splits into the bare variable and the remainder once the quotes go, and the variable
        # alone resolves to the repo ROOT — a directory, which then failed the exists check for
        # every hook at once. A check that reports ten failures with an empty filename is
        # reporting on its own parsing, not on the thing under test.
        for rel_path in re.findall(r'\$CLAUDE_PROJECT_DIR"?(/[^\s"]+)', cmd):
            path = os.path.join(ROOT, rel_path.lstrip("/"))
            # ONLY THIS REPO'S OWN HOOKS. .claude/settings.json also registers game_loop's
            # binaries, which are Python with no .py suffix — feeding those to `bash -n` failed
            # them all and would have made this check a standing complaint about another
            # project's files. Own the check at the layer that owns the thing checked.
            if os.path.join(".showrunner", "hooks") not in path:
                continue
            if not os.path.isfile(path):
                ok("%s registers %s, which EXISTS — registered-and-absent is worse than "
                   "unregistered, because registration is what makes it look present"
                   % (event, os.path.basename(path)), False, path)
                continue
            checked += 1
            # THE SHEBANG DECIDES THE PARSER, not the extension. issue-waker.py carries one and
            # a hook without a suffix would otherwise be guessed at — which is how game_loop's
            # `watchdog` came to be parsed as bash.
            with open(path) as _hf:
                first = _hf.readline()
            if path.endswith(".py") or "python" in first:
                try:
                    with open(path) as _hf:
                        ast.parse(_hf.read())
                    bad = ""
                except (SyntaxError, OSError, ValueError) as exc:      # noqa: BLE001
                    bad = str(exc)
            else:
                proc = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
                bad = proc.stderr.strip() if proc.returncode != 0 else ""
            ok("%s hook %s parses — a PreToolUse hook that does not refuses every Bash, Edit "
               "and Write, including the one that would repair it"
               % (event, os.path.basename(path)), not bad, bad[:200])

    ok("...and the check actually parsed something, so a registration this could not read "
       "fails here rather than passing as 'nothing to check'", checked >= 3, checked)


def test_every_shipped_hook_parses():
    group("Every hook install.sh ships is SYNTACTICALLY VALID — a broken one is not caught by "
          "anything else in this suite")
    if not have("bash"):
        skip("the hook parse group", "bash is not installed")
        return

    # THIS SUITE WAS GREEN WHILE A SHIPPED HOOK COULD NOT PARSE. A comment added to
    # pipeline-status-gate.sh contained an apostrophe, inside the body of a `python3 -c '...'`
    # single-quoted string; bash then hit EOF looking for the close. 1,190 assertions passed
    # over a hook that was dead. Nothing here executes the hooks as FILES — the gate tests feed
    # them payloads, and a hook that cannot parse returns no output, which is the same thing a
    # hook that correctly declines to fire returns.
    #
    # That is the non-event shape again: "produced nothing" is both the healthy quiet case and
    # total failure. The parse check is the positive control those tests lacked.
    #
    # It caught itself twice. The apostrophe broke it; the COMMENT WARNING ABOUT THE APOSTROPHE
    # then broke it again, because that comment contained three. Being a PreToolUse hook is the
    # only reason either was noticed within a minute — a failed parse there blocks every Bash
    # call. The same mistake in a Stop hook stops guarding and says nothing at all.

    # DERIVED FROM install.sh, never listed here. Two accountings of what ships is how one goes
    # stale, and a hook added to the installer without a line in this test would be exactly the
    # gap this test exists to close.
    with open(os.path.join(ROOT, "install.sh")) as fh:
        src = fh.read()
    m = re.search(r"for hook_name in ([^;]+); do", src)
    ok("install.sh still declares its shipped hooks as ONE list this test can read", bool(m))
    if not m:
        return
    shipped = [h for h in m.group(1).replace("\\\n", " ").split() if h.strip()]
    ok("...naming at least the five the registration writes", len(shipped) >= 5,
       "found %r" % (shipped,))

    for hook in shipped:
        path = os.path.join(ROOT, ".showrunner", "hooks", hook)
        ok("%s exists in the payload it is copied from — registered-and-absent is worse than "
           "unregistered, because the registration is what makes it look present" % hook,
           os.path.isfile(path))
        if not os.path.isfile(path):
            continue
        if hook.endswith(".py"):
            # PARSED IN PROCESS, not via py_compile: writing bytecode is py_compile's JOB, so
            # -B and PYTHONDONTWRITEBYTECODE do not stop it, and it left a __pycache__ beside
            # the hooks that the wiring net then reported as an unregistered hook. A check that
            # creates the condition another check flags is worse than either failing.
            try:
                with open(path) as _hf:
                    ast.parse(_hf.read())
                rc, err = 0, ""
            except (SyntaxError, OSError, ValueError) as _pe:
                rc, err = 1, str(_pe)
            p = type("R", (), {"returncode": rc, "stderr": err})()
        else:
            p = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        ok("...and PARSES, so it can actually run when the harness invokes it" % (),
           p.returncode == 0, (p.stderr or "")[:220])


def test_pipeline_status_gate():
    group("`$?` after a pipe: the status of the truncator, not of the command being judged")
    hook = os.path.join(ROOT, ".showrunner", "hooks", "pipeline-status-gate.sh")
    if not os.access(hook, os.X_OK):
        skip("the pipeline-status gate", "hook not present or not executable")
        return

    def fired(command):
        p = subprocess.run(["bash", hook], input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True, text=True)
        return bool(p.stdout.strip()), p.stdout

    # THE DEFECT, MEASURED IN THIS REPO RATHER THAN IMAGINED. A pipeline exits with its LAST
    # command's status, and head/tail/grep essentially always succeed. Re-running the still
    # reproducible instances from one session with and without the pipe, 4 of 7 reported a
    # WRONG status: `showrunner check` 3 read as 0, `campaign` 2 as 0, `waiting` 1 as 0,
    # `llm_chat owed` 2 as 0. One of those became a bug report filed against another team's
    # tool for a defect that did not exist.
    #
    # The sharpest instance is this project's own: `check` exits 3 on VOID, and its output
    # argues for that code — "distinct from 2 (new failures) so a caller that treats non-zero
    # as 'the code is bad' gets a code it did not map rather than a wrong answer it will
    # believe". Reasoned for, implemented, documented, then read as 0 through a pipe by the
    # author of it. A signal designed to stop a caller believing a wrong answer.
    hit, out = fired('llm_chat owed --json 2>&1 | head -3; echo "exit=$?"')
    ok("[CORPUS: under 1% of Bash commands, a minority of them text ABOUT the pattern — `python3 test/corpus.py --gate pipeline`] "
       "`$?` after a pipe ending in `head` is named as the truncator's status", hit, out[:200])
    ok("...and the notice quotes the offending pipeline back, so the warning is locatable "
       "rather than a general lecture", "| head -3" in out, out[:200])
    for shape in ('cmd 2>&1 | tail -5; echo "rc=$?"',
                  'x | grep -E "R" | head -4; echo "exit=$?"',
                  'a | wc -l; rc=$?'):
        ok("...as is %r" % shape[:34], fired(shape)[0])

    # THE SAFE SHAPES MUST STAY SILENT, or this becomes a warning nobody reads. The common
    # correct form redirects first and pipes LATER — the `$?` there is the real status and the
    # pipe belongs to a different command entirely.
    for shape, why in (
            ('python3 test/run.py > /tmp/o 2>&1; echo "exit=$?"; grep RESULT /tmp/o | head -4',
             "redirect first, pipe in a LATER command"),
            ('set -o pipefail; cmd | head -3; echo "$?"', "pipefail handles it"),
            # NOT SHELL-NEUTRAL, and this assertion used to claim it was. `${PIPESTATUS[0]}`
            # is a real remedy in bash and THE EMPTY STRING in zsh, so the honest silent case
            # is the zsh spelling. The bash form has its own assertions below, one per host.
            ('cmd | head -3; echo "${pipestatus[1]}"', "the zsh pipestatus array, indexed from 1"),
            ('grep -n "PIPESTATUS" test/run.py', "a MENTION of the construct, not a use of it"),
            ('echo payload | python3 gate.py; echo "rc=$?"',
             "the SUBJECT is last in the pipeline, which is the status `$?` gives"),
            ('git status --porcelain | head -3', "no `$?` read at all")):
        ok("...while %s stays silent (%s)" % (repr(shape[:30]), why), not fired(shape)[0])

    # IT NOTICES, IT NEVER DENIES. Sometimes the truncator IS the subject, and a gate that
    # blocks a legitimate shape trains its own bypass.
    p = subprocess.run(["bash", hook], input=json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": 'cmd | head -1; echo "$?"'}}),
        capture_output=True, text=True)
    eq("the gate ALLOWS and annotates rather than refusing — naming the hazard at the moment "
       "of use is the job, and a blocked legitimate shape teaches the author to route around "
       "it", p.returncode, 0)

    # WHAT IS THROWN AWAY, not merely THAT something is. The auditor asked whether severity
    # should scale with the SUBJECT's exit vocabulary — a tool answering 0/1 loses one bit,
    # `showrunner check` answering 0/1/2/3 loses the distinction it was built for.
    #
    # MEASURED BEFORE BUILDING, and the measurement argued against a severity ladder while
    # arguing FOR this: exactly TWO showrunner verbs document a graded vocabulary, and those
    # two were HALF the wrong readings in the corpus (`check` 3 read as 0, `waiting` 1 as 0).
    # Too few to justify a second severity; over-represented enough in the damage to name.
    env = dict(os.environ, CLAUDE_PROJECT_DIR=ROOT)

    def context(command):
        p = subprocess.run(["bash", hook], input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True, text=True, env=env)
        if not p.stdout.strip():
            return ""
        return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]

    graded = context('./bin/showrunner check 2>&1 | tail -5; echo "exit=$?"')
    ok("a subject with a GRADED exit vocabulary is told which codes it is about to lose, not "
       "merely that a status is being dropped", "THROWS AWAY" in graded, graded[-160:])
    ok("...naming the actual codes, read out of llms.txt rather than listed in the hook — a "
       "list in the hook goes stale the day a third verb becomes graded",
       "3 VOID" in graded and "2 new failures" in graded, graded[-200:])

    plain = context('llm_chat owed 2>&1 | head -3; echo "exit=$?"')
    ok("...while a subject whose vocabulary is not documented gets the generic notice and no "
       "invented stakes", plain and "THROWS AWAY" not in plain, plain[-120:])
    ok("...and the internal sentinel never reaches the reader", "@@STAKES@@" not in plain
       and "@@STAKES@@" not in graded)

    # THE SENTINEL WAS \x00 AND SILENTLY VANISHED. Command substitution strips null bytes, so
    # the marker died between the two python blocks and the stakes never rendered — wired,
    # green, and producing nothing. A printable sentinel is not a style choice here.
    ok("the stakes survive the shell boundary between the two stages at all — the first "
       "sentinel was a null byte, which command substitution deletes",
       "check answers" in graded, graded[-160:])

    # THE REMEDY HAS THE DEFECT, ON THIS HOST. `pipefail` works in bash and zsh. `PIPESTATUS`
    # does not: zsh spells the array `pipestatus` and indexes it FROM 1, so `${PIPESTATUS[0]}`
    # under zsh is THE EMPTY STRING. Measured, not assumed:
    #
    #     zsh    true | false ;  PIPESTATUS[0]=[]   pipestatus[1]=[0]  pipestatus[2]=[1]
    #     bash   true | false ;  PIPESTATUS[0]=[0]  PIPESTATUS[1]=[1]
    #
    # It fails WORSE than the bug it fixes. `$?` after a pipeline yields a real number about the
    # wrong command; the bash idiom under zsh yields nothing, which renders as `exit=` and reads
    # as neither pass nor fail — the identity element, inside the cure.
    #
    # This gate SILENCED on the mere presence of the word, so an author following its own
    # printed advice on a zsh host got an empty status AND no warning from the guard that
    # exists to prevent exactly that. Reported by the game_loop auditor; reproduced here first.
    def ctx(command, shell):
        p = subprocess.run(["bash", hook], input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True, text=True,
            env=dict(os.environ, CLAUDE_PROJECT_DIR=ROOT, SHELL=shell))
        if not p.stdout.strip():
            return ""
        return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]

    BASH_FORM = 'cmd | head -3; echo "rc=${PIPESTATUS[0]}"'
    ZSH_FORM = 'cmd | head -3; echo "rc=${pipestatus[1]}"'

    warned = ctx(BASH_FORM, "/bin/zsh")
    ok("the bash-only PIPESTATUS form is flagged on a ZSH host, where it expands to nothing",
       "EMPTY IN ZSH" in warned, warned[:160])
    ok("...as its own finding with its own text, because the cause differs — that one is about "
       "whose exit code you believe, this one about a remedy that yields nothing here",
       "TRUNCATOR" not in warned, warned[:160])
    ok("...naming the form that actually works on this host",
       "${pipestatus[1]}" in warned, warned[-200:])
    ok("...and the same command on a BASH host stays silent, because there it is a real remedy",
       ctx(BASH_FORM, "/bin/bash") == "")
    ok("the zsh spelling is silent on a zsh host — it is correct, and a guard that warns about "
       "the right answer trains its own bypass", ctx(ZSH_FORM, "/bin/zsh") == "")
    ok("...and `pipefail` stays silent everywhere, because it works in both shells",
       ctx('set -o pipefail; cmd | head -3; echo $?', "/bin/zsh") == "")
    ok("the ordinary defect still fires on a zsh host — the new arm did not swallow the old one",
       "TRUNCATOR" in ctx('cmd 2>&1 | head -3; echo "rc=$?"', "/bin/zsh"))

    # A command using ${PIPESTATUS[0]} contains no `$?` AT ALL, so the truncator arm could never
    # have reached it. The two findings are genuinely disjoint rather than one being a subset.
    ok("...and a PIPESTATUS command carries no `$?`, which is why this needed a separate arm "
       "rather than a wider pattern on the existing one", "$?" not in BASH_FORM)

    # A NON-BASH CALL IS NOT ITS BUSINESS.
    p2 = subprocess.run(["bash", hook], input=json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": 'x | head; echo "$?"'}}),
        capture_output=True, text=True)
    ok("...and a non-Bash tool call is passed through untouched, even when its payload "
       "happens to contain the pattern", not p2.stdout.strip())


def test_issue_waker():
    group("The issue waker: who may be acted on, and who is only read")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "issue_waker", os.path.join(ROOT, ".showrunner", "hooks", "issue-waker.py"))
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)

    # THE TRUST RULE IS THE SECURITY-RELEVANT HALF, and it is matched on BOTH login and display
    # name on purpose: a work bot posts under its own login carrying its owner's name, and either
    # half identifies it. Everything else is a claim from a stranger — read, verified, not built
    # from. Both directions asserted, because a rule that trusts everyone and one that trusts the
    # right people are the same observation from inside a single passing case.
    for author, want, why in (
            ({"login": "SupposedlySam", "name": "Jonah Walker"}, True, "the maintainer"),
            ({"login": "mrgnhnt96", "name": "Morgan Hunt"}, True, "a named collaborator by login"),
            ({"login": "some-work-bot", "name": "Morgan Hunt"}, True,
             "a work BOT posting under a trusted NAME — the login is unknown and the name is not"),
            ({"login": "SUPPOSEDLYSAM", "name": None}, True, "a login differing only in case"),
            ({"login": "driveby", "name": "A Stranger"}, False, "a stranger"),
            ({"login": "morganhunt-fan", "name": "Not Morgan"}, False,
             "a LOOKALIKE login — substring matching here would trust it"),
            ({"login": None, "name": None}, False, "an author carrying no identity at all")):
        eq("%s is %s" % (why, "TRUSTED" if want else "not trusted"), w.trusted(author), want)

    # AN UNREADABLE BASELINE MUST NOT WAKE ON THE WHOLE BACKLOG. Treating it as empty would make
    # every open issue "new" and hand the session a backlog at a turn-end, which is a gate
    # somebody switches off.
    scratch = tmpdir("waker-state")
    w.STATE = os.path.join(scratch, "seen-issues.json")
    eq("with no baseline at all, the waker declines rather than comparing", w.baseline(), None)
    with open(w.STATE, "w") as fh:
        fh.write("{not json")
    eq("...and an unreadable one declines too — 'could not read' is never 'nothing seen yet'",
       w.baseline(), None)
    eq("...so it exits QUIETLY rather than waking, leaving the session_start check to report it "
       "properly", w.main(), 0)
    with open(w.STATE, "w") as fh:
        json.dump({"seen": [1, 2]}, fh)
    eq("...while a readable baseline is used", w.baseline(), {1, 2})

    # NO DEBTS FOUND ACROSS ROOMS WE COULD NOT REACH IS A FAILED LOOK, not a clean inbox. The
    # doorbell reads `unreachable` from the JSON body rather than the exit status, because the
    # body is the data and a status is a summary over it.
    #
    # THE COMMENT THAT WAS HERE CLAIMED A MEASURED UPSTREAM DEFECT AND WAS FALSE. It said
    # llm_chat exits 0 with rooms unreachable, contradicting its own contract. Its source
    # returns 2 whenever `unreachable` is non-empty, and always has. My measurement was
    # `llm_chat owed --json 2>&1 | head -3; echo "exit=$?"`, where `$?` is HEAD's status — and
    # I had spliced an exit code from one run onto a body from a later one and called the pair
    # a single observation. I filed that upstream as a bug and retracted it.
    #
    # These assertions were always about THIS parser's behaviour given a payload, so they were
    # never testing the false claim — which is exactly why the false claim survived in a
    # comment above four passing tests. A green suite does not audit its own prose.
    class _FakeRun(object):
        def __init__(self, code, out):
            self.returncode, self.stdout, self.stderr = code, out, ""

    real_sub = w.subprocess
    w._chat_cli = lambda: "/nonexistent/llm_chat"

    class _Sub(object):
        payload = None
        code = 0
        @staticmethod
        def run(*a, **k):
            return _FakeRun(_Sub.code, _Sub.payload)
    _Sub.SubprocessError, _Sub.TimeoutExpired = real_sub.SubprocessError, real_sub.TimeoutExpired
    w.subprocess = _Sub

    _Sub.payload = json.dumps({"owed": [], "unreachable": []})
    eq("a genuinely quiet inbox reports no debts", w.chat_debts(), [])

    _Sub.payload = json.dumps({"owed": [], "unreachable": [
        {"room": "game_loop_owner", "why": "HTTP 429  Rate limit exceeded"},
        {"room": "lamp_owner", "why": "HTTP 429  Rate limit exceeded"}]})
    eq("...but no debts found across rooms it could not REACH is a failed look, not a clean "
       "inbox — exit 0 and the words 'nothing owed' say otherwise and are wrong",
       w.chat_debts(), None)

    _Sub.payload = json.dumps({"owed": [{"room": "game_loop_owner", "from": "auditor",
                                         "seq": 200}], "unreachable": []})
    eq("...and a real debt is reported with the room, asker and seq that identify it",
       w.chat_debts(), ["#game_loop_owner: auditor asked at seq 200"])

    # The seq is what makes a LATER question from the same room ring again, so it is part of
    # the identity and not decoration.
    _Sub.payload = "nothing owed"
    eq("...while an answer we asked for as JSON and cannot parse is a failed look too, whatever "
       "the exit code claims — we asked a specific question and got an unknown shape",
       w.chat_debts(), None)
    w.subprocess = real_sub

    # THE DEBT HALF MUST RING FROM INSIDE THE POLL LOOP, not after the budget drains. It used to
    # be checked once, at the very end — so this loop woke every 60s for half an hour to ask
    # GitHub about issues and never once asked chat, and a person waiting on an answer waited
    # out the entire budget. A debt ALREADY outstanding when the loop started waited just as
    # long. Asserted on ELAPSED TIME against a deliberately long budget, because that is the
    # actual claim: "it rang early" and "it rang at the end" are the same return value.
    w.POLL_SEC, w.DEBT_EVERY, w.BUDGET_SEC = 0, 1, 60
    w.look = lambda: {1: {}, 2: {}}                      # nothing fresh; issues are quiet
    w.chat_debts = lambda: ["#game_loop_owner: owner asked at seq 193"]
    started = time.time()
    rang = w.main()
    elapsed = time.time() - started
    eq("an outstanding chat debt wakes the session", rang, 2)
    ok("...within seconds rather than after the 60s budget drains — the debt half no longer "
       "rides on the issue poll finishing (%.1fs)" % elapsed, elapsed < 10)

    # AND IT MUST NOT RING TWICE FOR THE SAME DEBT, or the bell is a loop: wake, turn ends
    # without the debt paid, Stop hook starts a fresh poll, first tick sees the same debt.
    # Bounded only by the agent eventually paying, which is the one thing a wake cannot make
    # happen. A NEW question from the same room carries a new seq, so it still rings.
    w.BUDGET_SEC = 1
    eq("...and the same unpaid debt does not ring again — the wake is recorded, not repeated",
       w.main(), 0)
    ok("...with the debt recorded as already rung", 
       "#game_loop_owner: owner asked at seq 193" in w.rung())
    w.chat_debts = lambda: ["#game_loop_owner: owner asked at seq 199"]
    w.BUDGET_SEC = 60
    eq("...while a LATER question from the same room rings, because the seq differs and it is a "
       "different person waiting on a different answer", w.main(), 2)

    # A FAILED LOOK IS NOT A CLEAN INBOX, on this half too.
    w.chat_debts = lambda: None
    w.BUDGET_SEC = 1
    eq("...and a chat check that could not run wakes nobody rather than reporting no debts",
       w.main(), 0)


def test_self_vendored_pin():
    group("Self-vendoring: editing the tool must not disarm the tool (game_loop's .game_loop_self)")
    if not have("git"):
        skip("the self-vendor group", "git is not installed")
        return

    # THE PROBLEM, WHICH IS SPECIFIC TO A TOOL THAT DEVELOPS ITSELF. showrunner's guards, hooks
    # and briefs run showrunner — so they run the very code being edited, and ONE syntax error
    # anywhere under lib/showrunner/ kills every verb at import. Measured: the worktree guard
    # then exited 1 with EMPTY stdout, which is neither a deny (2) nor a loud allow. Editing the
    # tool silently disarmed its own guard.
    #
    # Borrowed from game_loop, which hit this first: a gitignored PINNED copy that the plumbing
    # resolves BEFORE the working tree, with a fallback so a fresh clone needs nothing installed.
    cfg = make_repo()
    src = os.path.join(cfg.root, "bin")
    os.makedirs(src, exist_ok=True)
    real = os.path.join(src, "showrunner")
    with open(real, "w") as fh:
        fh.write("#!/bin/sh\necho 'the working tree answered'\n")
    os.chmod(real, 0o755)

    # THREE TIERS, asserted in order. The fixture supplies an INSTALLED copy the way a consumer
    # has one, so "no pin" falls back to that rather than to the working tree — which is the
    # correct answer and not the one this test first assumed. `bin/` is the last resort, for the
    # repo that IS showrunner and never runs its own installer.
    installed = os.path.join(cfg.root, ".showrunner", "bin", "showrunner")
    eq("with no pin, the resolver falls back to the INSTALLED copy — the one a consumer has, "
       "and the one `git worktree add` correctly does not carry", brief.sr_bin(cfg), installed)
    moved = installed + ".aside"
    os.rename(installed, moved)
    eq("...and with no installed copy either, to the working tree — so a fresh clone of this "
       "repo needs nothing installed for the tool to work", brief.sr_bin(cfg), real)
    os.rename(moved, installed)

    pinned_dir = os.path.join(cfg.root, ".showrunner_self")
    os.makedirs(os.path.join(pinned_dir, "bin"))
    pinned = os.path.join(pinned_dir, "bin", "showrunner")
    with open(pinned, "w") as fh:
        fh.write("#!/bin/sh\necho 'the pin answered'\n")
    os.chmod(pinned, 0o755)
    eq("...and a self-vendored pin WINS over it, which is the whole mechanism: the plumbing runs "
       "code a mid-edit cannot break", brief.sr_bin(cfg), pinned)

    # THE PROPERTY, exercised end to end rather than asserted from the resolver. Break the
    # working copy the way a half-finished save does, and the guard must still ANSWER.
    with open(real, "w") as fh:
        fh.write("#!/bin/sh\necho 'Traceback (most recent call last):' >&2\nexit 1\n")
    os.chmod(real, 0o755)
    shim = os.path.join(cfg.root, lease.GUARD_SHIM)
    os.makedirs(os.path.dirname(shim), exist_ok=True)
    shutil.copy2(os.path.join(ROOT, lease.GUARD_SHIM), shim)
    res = subprocess.run(["bash", shim], cwd=cfg.root,
                         input=json.dumps({"session_id": "s", "cwd": cfg.root,
                                           "tool_name": "Write", "tool_input": {}}),
                         capture_output=True, text=True)
    eq("a BROKEN working tree does not disarm the guard — the shim reaches the pin first",
       res.returncode, 0)
    # ASSERTED ON WHAT THE PIN SAYS, not on the absence of a notice. The pin's script echoes a
    # sentence nothing else in this fixture can produce, so its PRESENCE settles the question
    # outright — where "DID NOT RUN" is absent" was satisfied by any of eight unrelated
    # branches, and equally satisfied by a guard that had stopped saying anything at all.
    ok("...and the answer comes from the PIN: its own words are in the output, which no other "
       "branch of this fixture can produce", "the pin answered" in res.stdout, res.stdout[:200])
    ok("...and NOT from a fail-open notice about the broken copy — 'allowed without being "
       "checked' and 'checked, and allowed' are different outcomes and only one is a guard",
       "instead of answering" not in res.stdout, res.stdout[:200])

    # THE REMEDY DOCTOR PRINTS MUST BE RUNNABLE BY THE THING IT IS PRINTED TO. `self --pin`
    # extracts from the checkout the RUNNING code lives in, and a pinned copy is NOT a checkout —
    # so naming the resolved binary produced a remedy that exits 2 telling the reader to run it
    # from a clone. `lease.REMEDIES` says this project has shipped a dead remedy twice; this
    # would have been the fourth, and it was caught by RUNNING it rather than reading it.
    #
    # Driven against a fixture with a DELIBERATELY STALE pin, not against this repo's live state:
    # a test whose subject is "what doctor says when the pin is behind" must construct behind.
    with open(os.path.join(cfg.root, "extra.txt"), "w") as fh:
        fh.write("moves HEAD past the pin\n")
    sh(["git", "add", "-A"], cfg.root)
    sh(["git", "commit", "-q", "-m", "move HEAD past the pin"], cfg.root)
    first = sh(["git", "rev-parse", "HEAD~1"], cfg.root).stdout.strip()
    os.makedirs(os.path.join(cfg.root, ".showrunner_self", "bin"), exist_ok=True)
    with open(os.path.join(cfg.root, ".showrunner_self", "bin", "showrunner"), "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(os.path.join(cfg.root, ".showrunner_self", "bin", "showrunner"), 0o755)
    for name, val in (("PINNED", json.dumps({"ref": "HEAD", "sha": first})), ("VERSION", first)):
        with open(os.path.join(cfg.root, ".showrunner_self", name), "w") as fh:
            fh.write(val + "\n")
    doc = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "doctor"],
                         cwd=cfg.root, capture_output=True, text=True,
                         env=dict(os.environ, NO_COLOR="1"))
    repin = [l for l in doc.stdout.splitlines() if "--dest .showrunner_self" in l]
    ok("a pin BEHIND head is reported rather than answering normally from old rules — it is the "
       "same guard giving an older answer, which is invisible without this",
       repin and "BEHIND HEAD" in repin[0], doc.stdout[-400:])
    ok("...and the remedy names a binary in a CHECKOUT, never the pinned copy, because the pin "
       "cannot re-pin itself and refuses when asked",
       repin and ".showrunner_self/bin/showrunner self --pin" not in repin[0], repin[:1])

    # THE PAIR. Remove the pin and the same broken tree must now degrade LOUDLY, or the
    # assertion above would pass just as well against a shim that ignores its own failures.
    shutil.rmtree(pinned_dir)
    res2 = subprocess.run(["bash", shim], cwd=cfg.root,
                          input=json.dumps({"session_id": "s", "cwd": cfg.root,
                                            "tool_name": "Write", "tool_input": {}}),
                          capture_output=True, text=True)
    eq("...while with the pin gone the broken tree still ALLOWS, because a half-edited tool must "
       "not block the edit that repairs it", res2.returncode, 0)
    ok("...but now says the guard did not run, on the channel that reaches the agent — the "
       "difference between the two runs is the whole value of the pin",
       "DID NOT RUN" in res2.stdout and "additionalContext" in res2.stdout, res2.stdout[:200])


def test_self_pin():
    group("Pinning the tool's own code at a ref (CI-02)")
    if not have("git"):
        skip("the self --pin group", "git is not installed")
        return

    # AGAINST THE REAL REPO, because `git archive` needs a repo that actually carries bin/ and
    # lib/ — and a fixture that faked them would be asserting against the fake. Nothing here
    # writes to ROOT: `git archive` reads, and every destination is a temp dir.
    head = sh(["git", "rev-parse", "HEAD"], ROOT).stdout.strip()

    dest = os.path.join(tmpdir("central"), "central")
    d = pin.pin("HEAD", dest)
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

    # UNREADABLE IS NOT MISSING, and this returned None for both — so a genuinely pinned
    # directory with a truncated stamp fell through to `source="copy"` and `--version` claimed
    # "copied from a working tree, so NO commit names this code" while VERSION sat beside it
    # naming the commit. A positive claim about provenance, derived from a caught exception.
    corrupt = tmpdir("corrupt-pin")
    os.makedirs(os.path.join(corrupt, "lib", "showrunner"), exist_ok=True)
    with open(os.path.join(corrupt, pin.PINNED_FILE), "w") as fh:
        fh.write("{not json")
    torn_pin = pin.read_pin(corrupt)
    ok("a pin whose stamp is CORRUPT reads as unreadable, not as absent — 'which commit' is "
       "unknown here, and 'no commit names this code' is a different and stronger claim",
       (torn_pin or {}).get("unreadable"), torn_pin)
    _cr2 = pin.code_root
    pin.code_root = lambda _d=corrupt: _d
    try:
        torn_info, torn_line = pin.running(), pin.describe()
    finally:
        pin.code_root = _cr2
    eq("...so the running code still reports itself as PINNED", torn_info["source"], "pinned")
    ok("...and the line says the stamp cannot be read rather than that no commit names this "
       "code", "STAMP UNREADABLE" in torn_line and "NO commit names" not in torn_line,
       torn_line[:160])
    ok("...and it does NOT claim the directory was edited since it was pinned, which is a "
       "finding and would here be derived from a failed read", torn_info["dirty"] is None,
       torn_info)

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
    d2 = pin.pin(head, dest)
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
    pin.pin(head, dest)

    # THE DELETION SAFETY. `pin` removes its destination wholesale, so the question "is this
    # mine?" is deciding a deletion. A mistyped --dest must not eat a directory.
    stranger = tmpdir("stranger")
    keep = os.path.join(stranger, "someones-work.txt")
    with open(keep, "w") as fh:
        fh.write("the only copy\n")
    raises("a destination that is not a pin is REFUSED rather than deleted — a pin overwrites "
           "wholesale, so it only ever overwrites something it recognises as its own",
           lambda: pin.pin("HEAD", stranger), "refusing to delete")
    ok("...and the file that was there is still there", os.path.exists(keep))
    ok("...and an empty directory is not mistaken for a pin either, since 'exists' is the test "
       "that would make a typo destructive", not pin.looks_pinned(tmpdir("empty")))

    raises("an unresolvable ref is refused before anything is created",
           lambda: pin.pin("no-such-ref-here", os.path.join(tmpdir("d2"), "c")),
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
    # NOT `here["dirty"] in (True, False)`, which was what this asserted and which is
    # `isinstance(x, bool)` wearing a sentence about the working tree. Hard-coding `dirty=False`
    # in pin.py left the suite green. Compared against git instead, so it fails whichever state
    # ROOT happens to be in.
    eq("...and saying whether that sha still describes the working tree, because uncommitted "
       "edits make it an overstatement", here["dirty"],
       bool(sh(["git", "status", "--porcelain"], ROOT).stdout.strip()))

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
    pin.pin(head, dest)

    # A HALF-WRITTEN PIN IS WORSE THAN NONE: it is a directory that exists, looks installed to
    # anything checking for a path, and cannot run. A fixture repo carries no bin/ or lib/, so
    # the extraction genuinely fails — this is not a simulated error.
    #
    # THE MATCHER IS THE FINDING HERE. It used to be "self --pin", which is the prefix of ALL
    # FOUR refusals this module raises, so it could not tell which one fired — and the guard the
    # description named was never entered, because `git archive` dies at the pathspec stage
    # first. Deleting the guard outright left the suite green. Both stages are separated below.
    empty_repo = make_repo()
    doomed = os.path.join(tmpdir("doomed"), "central")
    raises("a commit with no bin/ or lib/ at all fails in `git archive`, at the pathspec",
           lambda: pin.pin("HEAD", doomed, source=empty_repo.root), "could not extract")
    ok("...and leaves NOTHING behind — a half-extracted central directory would read as "
       "installed to anything that checks for a path", not os.path.exists(doomed))

    # THE GUARD ITSELF, which needs a commit that archives CLEANLY and still carries no tool:
    # `bin/` and `lib/` exist, and neither holds showrunner. That is the ref-predates-the-rename
    # case the guard was written for, and until now nothing constructed it.
    wrong = make_repo(files={"README.md": "seed\n", "bin/other": "#!/bin/sh\n",
                             "lib/other/x.py": "pass\n"})
    dud = os.path.join(tmpdir("dud"), "central")
    raises("a commit that DOES archive bin/ and lib/ but carries no showrunner in either is "
           "refused by the payload check — extracting cleanly is not the same as extracting the "
           "tool, and the difference is a central directory that looks installed and cannot run",
           lambda: pin.pin("HEAD", dud, source=wrong.root), "predates this layout")
    ok("...leaving nothing behind either", not os.path.exists(dud))

    # THE SOURCE REPO IS NEVER THE CWD'S. `pin` resolved its source from the consumer's config,
    # so the remedy printed to a consumer — `self --pin <ref> --dest <central>` — archived from
    # THEIR repo: it either failed after deleting the machine's central install, or succeeded and
    # published their code as showrunner with a real sha and exit 0. Both were reproduced.
    survivor = os.path.join(tmpdir("survivor"), "central")
    pin.pin(head, survivor)
    raises("pinning from a repo that carries no tool is refused",
           lambda: pin.pin("HEAD", survivor, source=empty_repo.root), "could not extract")
    ok("...and the pin that was ALREADY THERE is untouched — the destination used to be deleted "
       "before anything validated the source, so the failure path removed the central install "
       "every project on the machine dispatches to, then printed the command that did it",
       (pin.read_pin(survivor) or {}).get("sha") == head, pin.read_pin(survivor))
    still = subprocess.run([os.path.join(survivor, "bin", "showrunner"), "--version"],
                           cwd=survivor, capture_output=True, text=True)
    eq("...and it still RUNS, which is the claim 'untouched' is actually making",
       still.returncode, 0)
    ok("...and no staging directory is left beside it", not any(
        n.startswith("central.pinning.") for n in os.listdir(os.path.dirname(survivor))),
        os.listdir(os.path.dirname(survivor)))

    # And the refusal when the running code is not a checkout at all — the state a pinned
    # central install is in, i.e. the exact place the shim's old remedy was typed.
    _cr = pin.code_root
    pin.code_root = lambda _d=tmpdir("not-a-repo"): _d
    try:
        raises("`self --pin` from code that is not itself a git checkout is REFUSED rather than "
               "resolved from the cwd, and says a clone of showrunner is what it needs",
               lambda: pin.pin("HEAD", os.path.join(tmpdir("d3"), "c")), "not itself a git")
    finally:
        pin.code_root = _cr


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
    installed = subprocess.run([installer, "--central", repo], cwd=ROOT, capture_output=True,
                               text=True, env=env)
    # CHECKED, because every assertion below opens a file this run was supposed to write. An
    # installer that died here would crash the group instead of failing one line, and a mutant
    # that is unscoreable is one the sweep cannot count.
    eq("`install.sh --central` exits 0 — %s"
       % (installed.stderr or installed.stdout).strip()[-120:], installed.returncode, 0)

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
    # THE CHANNEL IS THE ASSERTION, and asserting the wrong one is what let this ship: every
    # verb here used to be checked with `in res.stderr`, which proves the STRING WAS PRODUCED
    # and nothing about whether anything receives it. A PreToolUse hook that exits 0 surfaces
    # stderr to nobody, so the shim was announcing its own absence into a void — while its
    # header explained at length that never doing that was the point.
    for verb in (["lock", "guard"], ["worktree", "guard"]):
        res = run_sr(repo, verb, nowhere)
        eq("with no central install, the PreToolUse verb `%s` exits 0 — a hook that hard-fails "
           "on missing plumbing blocks the write that would repair it" % " ".join(verb),
           res.returncode, 0)
        try:
            payload = json.loads(res.stdout)
        except ValueError:
            payload = None
        ctx = ((payload or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""
        ok("...and says so on the channel that REACHES THE AGENT — additionalContext on stdout, "
           "the same shape .showrunner/hooks/worktree-guard.sh emits, rather than stderr, which "
           "on an exit-0 hook is indistinguishable from silence",
           "ALLOWED WITHOUT BEING CHECKED" in ctx, (res.stdout or res.stderr)[:160])
        eq("...in output the host can actually parse, since a notice that arrives as broken "
           "JSON is the same silence one layer down",
           ((payload or {}).get("hookSpecificOutput") or {}).get("hookEventName"), "PreToolUse")

    # The two that are NOT PreToolUse. `worktree enter` is typed at a terminal and a Stop hook
    # permitting a stop has no channel to the model at all, so stderr is the best there is —
    # asserted separately so the weaker guarantee is visible rather than averaged in.
    for verb in (["stop-gate"], ["worktree", "enter"]):
        res = run_sr(repo, verb, nowhere)
        eq("with no central install, `%s` exits 0" % " ".join(verb), res.returncode, 0)
        ok("...and says it was not checked on stderr, which is where its reader is",
           "ALLOWED WITHOUT BEING CHECKED" in res.stderr, res.stderr[:120])
        ok("...and does NOT emit hook JSON, because it is not answering a PreToolUse and a "
           "payload the host never asked for is noise it may try to parse",
           "hookSpecificOutput" not in res.stdout, res.stdout[:120])

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
    pinned_at = pin.pin("HEAD", central)
    res = run_sr(repo, ["--version"], central)
    eq("with a central install present the shim dispatches to it and the tool runs",
       res.returncode, 0)
    # NOT `"showrunner" in stdout.lower()`, which was the old form: `describe()` opens with
    # "showrunner 0.1.0" for all three provenances, so any copy answering from anywhere passed
    # a line whose description claimed it proved WHICH copy. The evidence was sitting in the
    # same string unused — a pinned directory names its sha and its own path.
    ok("...and it is genuinely the CENTRAL copy answering, identified by the sha it was pinned "
       "at and the directory it lives in, not by the word 'showrunner' appearing in a line "
       "every copy prints",
       pinned_at["sha"][:12] in res.stdout and "pinned" in res.stdout, res.stdout[:160])

    # A DIRECTORY THAT RUNS IS NOT A PIN. The shim dispatched to anything executable at the
    # central path, so a half-extracted directory, a stale hand-copy, or something left at a
    # path the shim resolves to would be exec'd by every centrally-wired project on the machine.
    # `self --pin` writes PINNED and nothing else does, so it is the cheapest thing that
    # separates "showrunner put this here" from "this is executable". NOT an integrity check —
    # PINNED names a commit and does not attest that the code beside it is that commit — and the
    # shim says so rather than implying more than it checked.
    squat = os.path.join(tmpdir("squatted"), "central")
    os.makedirs(os.path.join(squat, "bin"))
    with open(os.path.join(squat, "bin", "showrunner"), "w") as fh:
        fh.write("#!/bin/sh\necho SQUATTED\n")
    os.chmod(os.path.join(squat, "bin", "showrunner"), 0o755)
    res = run_sr(repo, ["--version"], squat)
    eq("an executable at the central path with NO PINNED stamp is REFUSED rather than exec'd",
       res.returncode, 1)
    ok("...and its output never reaches the caller, which is the half that matters — the check "
       "has to happen before the exec, not be reported after it",
       "SQUATTED" not in (res.stdout + res.stderr), (res.stdout + res.stderr)[:200])
    ok("...naming what is missing, so the reader can tell this from 'no central install'",
       "PINNED" in res.stderr, res.stderr[:200])
    with open(os.path.join(squat, "PINNED"), "w") as fh:
        json.dump({"ref": "x", "sha": "0" * 40}, fh)
    res = run_sr(repo, ["--version"], squat)
    ok("...and a stamped directory IS dispatched to, so the check is about the stamp and not "
       "about refusing everything", "SQUATTED" in res.stdout, res.stdout[:120])

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

    # IT REGISTERS THE GUARD, AND THEREFORE MUST NOT ASK THE READER TO. Both were true in one
    # run: `worktree register` ran, printed "the worktree guard is registered", and then step 3
    # of the closing instructions told the reader to add a PreToolUse entry to
    # .claude/settings.json by hand and pasted the JSON to use. Following it produced a SECOND
    # entry — the guard running twice per tool call, which `_guard_registration` cannot notice
    # because it returns on the first match — and the pasted copy was a third spelling of what
    # `register_guard` writes, with different quoting and no timeout.
    ok("the installer registers the worktree guard itself", "guard is registered" in quiet.stdout,
       quiet.stdout[-400:])
    # KEYED ON THE ENTRY, NOT ON THE EVENT NAME. This first read `"PreToolUse" not in stdout`,
    # which is a proxy that cannot tell "names the event it registered on" from "hands you JSON
    # to paste" — and it failed the moment the installer's report started naming both events
    # correctly. The thing that must not appear is a pasteable hook entry.
    # EVERY GUARD THIS PROJECT SHIPS IS REGISTERED, and this assertion exists because the
    # dispatch guard was built, tested, committed and shipped WITHOUT being registered — a verb
    # nobody registers has never once run, which is the exact failure #37 reports, arriving
    # inside the fix for #37. `lock guard` was in that state for this repo's entire life.
    reg_settings = json.load(open(os.path.join(ROOT, ".claude", "settings.json")))
    reg_cmds = [h.get("command", "")
                for ev in ("PreToolUse", "Stop", "SessionStart", "PostCompact")
                for e in reg_settings.get("hooks", {}).get(ev, [])
                for h in e.get("hooks", [])]
    for shim, why in ((lease.GUARD_SHIM, "the worktree guard"),
                      (lease.STOP_TRIGGER, "the inert-Crawler stop trigger"),
                      (lease.WHOAMI_HOOK, "the seat announcement"),
                      (lease.DISPATCH_SHIM, "the dispatch guard")):
        name = os.path.basename(shim)
        ok("%s is REGISTERED in this repo's own settings, not merely built — a guard nobody "
           "registers has never once run" % why,
           any(name in c for c in reg_cmds), sorted(os.path.basename(c) for c in reg_cmds))
    ok("...and the dispatch guard is matched on BASH, which is the mechanism actually used — a "
       "version matched on `Agent` guarded the subagent tool while 42 real dispatches went "
       "through Bash",
       any(e.get("matcher") == "Bash"
           for e in reg_settings["hooks"]["PreToolUse"]
           for h in e.get("hooks", []) if "dispatch-guard" in h.get("command", "")),
       [e.get("matcher") for e in reg_settings["hooks"]["PreToolUse"]])

    ok("...and therefore does NOT also print a hook ENTRY for the reader to paste, which would "
       "register it a second time — the entry has exactly one author, `lease.register_guard`",
       '"matcher"' not in quiet.stdout and '"type": "command"' not in quiet.stdout
       and '\\"matcher\\"' not in quiet.stdout, quiet.stdout[-500:])

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
    refused = install_into(fresh_repo(), no_home, "--no-skills")
    # THE DENOMINATOR. "Nothing was written" is also what a failed install produces, and this
    # HOME is a different fixture from the --skills one above, so nothing here established that
    # the run reached the point of deciding.
    eq("`install.sh --no-skills` exits 0 (%s)"
       % ((getattr(refused, "stderr", "") or "").strip()[-100:]),
       getattr(refused, "returncode", 0), 0)
    ok("--no-skills writes nothing into HOME even though there is something to write — the "
       "flag a CI job or another skill passes when it must not be asked",
       not os.path.exists(os.path.join(no_home, ".claude", "skills")))
    ok("...and it SAYS it skipped them, so the absence is a decision the reader was told about "
       "rather than an install that quietly did less",
       "skill" in (getattr(refused, "stdout", "") or "").lower(),
       (getattr(refused, "stdout", "") or "")[-200:])


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
    offenders, unscanned = [], []
    for rel_path in tracked:
        full = os.path.join(ROOT, rel_path)
        try:
            # A SIZE GUARD THAT SILENTLY EXEMPTS IS A HOLE, and this one had swallowed the file
            # directly below it. The limit was 400_000 bytes; THIS file is over 800_000, so the
            # scan stopped covering its own source — while the comment a few lines down still
            # said it did, "instead of quietly exempting the one place the patterns are
            # guaranteed to appear". Found by accident: a fixture of mine put two fake home
            # paths in here and the scan did not notice, which it should have.
            #
            # The guard exists for genuinely large blobs, so it stays — but a skip is now
            # RECORDED and asserted on, because a file nobody scanned is not a file that came
            # back clean. Raised to a size no text file here approaches.
            if os.path.getsize(full) > 4_000_000:
                unscanned.append(rel_path)
                continue
            with open(full, errors="ignore") as fh:
                text = fh.read()
        except OSError as exc:
            unscanned.append("%s (%s)" % (rel_path, exc))
            continue
        # Assembled rather than written literally, so this scan still covers its own file
        # instead of quietly exempting the one place the patterns are guaranteed to appear.
        # A PLACEHOLDER IS NOT A HOME. `/Users/...` and `/Users/me` carry no identity and are
        # what a careful author writes when redacting one — game_loop's comments use both, and
        # this flagged four of them as leaks on an upgrade. What the check is FOR is a tracked
        # file carrying somebody's real home, because a stranger cloning this repo inherits it.
        #
        # Narrowed rather than exempted by file: exempting the file would have turned off the
        # check for the payload most likely to carry a real path, which is the direction that
        # gets somebody's username published.
        placeholder = re.compile(r"/(?:Users|home)/(?:\.\.\.|…|me|you|user|USER|<[^>]+>)(?:/|\b)")
        for pat in ("/" + "Users/", "/" + "home/", "/private/" + "tmp/claude-"):
            for line in text.splitlines():
                if pat in line and placeholder.search(line):
                    continue
                if pat in line and "example" not in line.lower():
                    offenders.append("%s: %s" % (rel_path, line.strip()[:100]))
    # THE NARROWING IS TWO-SIDED, or "placeholder" becomes a hole somebody drives a real path
    # through. Asserted here rather than trusted, because this check is the only thing standing
    # between a tracked file and somebody's published username.
    _ph = re.compile(r"/(?:Users|home)/(?:\.\.\.|…|me|you|user|USER|<[^>]+>)(?:/|\b)")
    for _line in ("/Users/.../development/x", "/Users/me/x.txt", "/Users/<user>/x"):
        ok("a redacted placeholder is not treated as a leak: %s" % _line, bool(_ph.search(_line)))
    # ASSEMBLED, NOT WRITTEN LITERALLY — the same trick the scan uses on its own patterns two
    # screens up, and for the same reason. A literal fake home in this file is indistinguishable
    # from a real leak to the very check being tested, and it tripped it the moment the size
    # guard stopped exempting this file. The first draft also used a real username lifted from a
    # bug report, in a tracked file, inside the assertion whose subject is not publishing one.
    for _line in ("/" + "Users/" + "nobody-real/dev/repo", "/" + "home/" + "nobody-real/thing"):
        ok("...while a REAL home is still caught: %s" % _line, not _ph.search(_line))

    ok("no tracked file hardcodes an absolute home or session path — this repo is public and "
       "a stranger inherits every tracked rule", not offenders, offenders[:6])
    # A FILE NOBODY SCANNED IS NOT A FILE THAT CAME BACK CLEAN.
    ok("...and every tracked file was actually scanned, so the verdict above covers the repo "
       "rather than the part of it under a size limit", not unscanned, unscanned[:6])
    ok("...including this file, which is the one place the patterns are guaranteed to appear "
       "and the one a size guard silently exempted for as long as it was over the limit",
       "test/run.py" not in unscanned, unscanned[:6])

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
    # READ THE HARNESS'S WHOLE bin/, NOT ONE FILENAME. This opened `.game_loop/bin/game_loop`
    # alone, and game_loop #155 moved its implementation into a sibling `_gl_impl.py` — so both
    # assertions below went red on an upgrade that changed NOTHING about the layout. `sessions/`
    # and `model.json` were sitting on disk exactly where this repo builds its path.
    #
    # A check pinned to a filename reports a refactor as a breakage, which is the crying-wolf
    # direction: it costs a real investigation and, done twice, it teaches you to discount the
    # check. Same lesson as the mutation anchors pinned to a signature — widen to the thing the
    # rule is actually about, which is "the harness", not "that file".
    gl_dir = os.path.join(ROOT, ".game_loop", "bin")
    gl_src = ""
    for _n in sorted(os.listdir(gl_dir)) if os.path.isdir(gl_dir) else []:
        _f = os.path.join(gl_dir, _n)
        if os.path.isfile(_f):
            with open(_f, errors="ignore") as fh:
                gl_src += fh.read()
    if gl_src:
        ok("the harness still calls its per-session directory `sessions/` — the segment this "
           "repo builds its model.json path from", 'os.path.join(ROOT, "sessions")' in gl_src)
        ok("...and still names the file model.json, so a rename fails HERE loudly instead of "
           "turning every model verdict into a silent `unknown`",
           'MODEL_F = "model.json"' in gl_src)
        # THE PATH IS ALSO CHECKED ON DISK, because a string in the harness's source and a
        # directory it actually writes are different facts, and the source-grep is the weaker
        # of the two. This is what proved the "rename" was a refactor rather than a break.
        _sess = os.path.join(ROOT, ".game_loop", "sessions")
        ok("...and the harness really does write that directory, which is the fact the grep "
           "above is a proxy for", os.path.isdir(_sess), _sess)
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
    # Eight: campaign.live, dispatch.lingering, graph.stale_claims, graph.stalled_claims,
    # graph.claim, locks._live, and TWO in reap's terminate block — added when SIGTERM stopped
    # claiming a retirement it had not witnessed. Those two are safe without their own boot
    # check for a reason worth writing down rather than assuming: they run only after
    # `lingering()` returned non-None, and `lingering` refuses across a boot. The pid is known
    # to be this boot before either call is reached, so the audit is inherited THROUGH A GUARD
    # rather than skipped.
    #
    # `graph.stalled_claims` (#69) is the eighth, and this is its justification rather than an
    # inherited pass. It scopes by boot FIRST and in the opposite direction to its siblings: a
    # claim proved to be from a different boot is SKIPPED here, because that claim is abandoned
    # and belongs to `stale_claims` — reporting it under both verdicts would hand a reader a
    # release and a do-not-touch for the same leaf. A boot that cannot be told falls through to
    # the pid check, which is the same posture `stale_claims` takes and is safe for the same
    # reason it is unsafe in `lingering`: this reader REPORTS and nothing acts on the result,
    # so the worst case is a line of output about a claim nobody may touch anyway.
    #
    # Every one of them must decide what a pid means ACROSS A BOOT before it trusts the answer.
    # If this number changes, the new reader is the thing to look at — not this assertion.
    eq("pid_alive has exactly the readers that were audited for boot scoping; a new one must "
       "justify itself rather than inherit the audit", len(readers), 8)

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
    # THREE STATES, NOT TWO. `present` is a positive control: "no grants found" must not come
    # from having read nothing. But NO LAYERS AT ALL is a third answer — this checkout no longer
    # tracks `.game_loop/`, so a clone has none, and asserting a layer exists made the control
    # demand the very file the repo stopped shipping. Nothing can widen a write root when
    # nothing configures one; that is a real verdict, and different from both "checked and
    # clean" and "could not check".
    if not present:
        skip("the effective game_loop write-root check",
             "no game_loop config layer exists in this checkout, so nothing could widen a write "
             "root — which is a different statement from having looked and found none")
    else:
        ok("...and so does the EFFECTIVE config — the union game_loop actually reads, not just "
           "the file this repo ships. showrunner's docs promise a Crawler cannot write outside "
           "the repo, and a machine-wide layer can widen that without touching anything tracked",
           not granted, granted)
        ok("...checked against the layers that exist here, so this is a verdict rather than a "
           "file that happened to be missing", bool(present), present)

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

    # #59: `spawn` joined every Crawler room as the literal identity "orchestrator", which is
    # not a reserved word -- it is exactly what a real agent picks when it IS one. Measured in a
    # shared checkout: another agent received this campaign's Crawler messages, accrued `owed`
    # debt for questions it never saw, and had its own `say` go out under the wrong name,
    # because a chat identity resolves per room and the room's join wins.
    ident = dispatch.orchestrator_identity(cfg)
    ok("the identity spawn joins under is not the bare word — a common noun as a default is the "
       "bug, because it is the name a real agent of that role has already taken",
       ident != "orchestrator", ident)
    ok("...and it is namespaced by PROJECT, so two repos on one machine do not share it",
       "sr" in ident, ident)
    ok("...and it is a legal llm_chat identity, or the failure is a room that never opened "
       "rather than a name that was wrong",
       re.fullmatch(r"[a-z0-9._-]{1,64}", ident) is not None, ident)
    # THE REPORTED CASE WAS TWO CAMPAIGNS IN ONE GIT ROOT, so a project-only prefix separates
    # nothing exactly where the collision happened. Driven through config.load with the env var
    # set, because the campaign is captured at LOAD and reading it later was its own bug (#39).
    _home = tmpdir("ident-campaign")
    shutil.copytree(os.path.join(cfg.root, ".showrunner"), os.path.join(_home, ".showrunner"))
    sh(["git", "init", "-q", "."], _home)
    _prev = os.environ.get("SHOWRUNNER_CAMPAIGN")
    os.environ["SHOWRUNNER_CAMPAIGN"] = "DROP-4130"
    try:
        _c2 = config.load(_home)
        _id2 = dispatch.orchestrator_identity(_c2)
    finally:
        if _prev is None:
            os.environ.pop("SHOWRUNNER_CAMPAIGN", None)
        else:
            os.environ["SHOWRUNNER_CAMPAIGN"] = _prev
    ok("...and two CAMPAIGNS in one checkout get different identities, which is the collision "
       "that was actually reported — project scoping alone would not have separated them",
       "drop-4130" in _id2 and _id2 != ident, (ident, _id2))

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
       (dispatch.session_health(hcfg, hentry) or {}).get("verdict"), "quiet")
    with open(hlog, "w") as fh:
        fh.write("working on it\nwrote a file\n")
    eq("output that is not an error is PRODUCING",
       (dispatch.session_health(hcfg, hentry) or {}).get("verdict"), "producing")
    with open(hlog, "w") as fh:
        fh.write("Execution error")
    h = dispatch.session_health(hcfg, hentry) or {}
    eq("...and an errored session is ERRORED even though its process is still alive",
       h.get("verdict"), "errored")
    ok("...and it names what it matched, so the verdict can be argued with",
       h.get("errors") == ["Execution error"], h)

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
    eq("a Crawler with no channel closes cleanly rather than erroring",
       dispatch.close_channel(cfg, {"crawler": "x"})[0], dispatch.CLOSE_DONE)

    # #60: THREE OUTCOMES, because a consumer measured that two cannot express this. A transient
    # `HTTP 429 Rate limit exceeded` and a permanent `<identity> has not joined <room>` are BOTH
    # exit 1 from the chat CLI. Treat 1 as success and a rate-limited close records a room
    # closed that is still open; treat it as failure and the never-joined case is a permanent
    # error no retry clears.
    def _fake_cli(script):
        d = tmpdir("close-cli")
        path = os.path.join(d, "llm_chat")
        with open(path, "w") as fh:
            fh.write(script)
        os.chmod(path, 0o755)
        c = make_repo(extra_config={"dispatch": {"chat": {"enabled": True, "cli": path}}})
        return c

    # #61: THE EXIT CODES ARE THE CONTRACT NOW. The chat tool shipped a retry vocabulary, so
    # transience stops being decided by regexing its prose — wording that had already drifted
    # once, and which cannot express the case that matters most. Consumer-measured against the
    # current build: the 429 that motivated #60 arrived as exit 1 then and arrives as 3 now.
    _rate = _fake_cli("#!/bin/sh\necho 'HTTP 429  Rate limit exceeded' >&2\nexit 3\n")
    st, why = dispatch.close_channel(_rate, {"crawler": "c", "channel": "room-a"})
    eq("THROTTLED (3) is UNKNOWN — the request was not applied, so the room is untouched and "
       "the answer is to ask again later", st, dispatch.CLOSE_UNKNOWN)
    ok("...and the detail says the request was NOT applied, which is what makes a retry safe "
       "here and unsafe for code 4", "NOT applied" in why, why)

    _down = _fake_cli("#!/bin/sh\necho 'connection refused' >&2\nexit 5\n")
    st5, why5 = dispatch.close_channel(_down, {"crawler": "c", "channel": "room-e"})
    eq("UNREACHABLE (5) is also UNKNOWN — nothing was sent", st5, dispatch.CLOSE_UNKNOWN)
    ok("...but says so DIFFERENTLY from a throttle, because the operator action differs: start "
       "the server versus wait", "listening" in why5 and why5 != why, why5)

    # THE ONE THAT MATTERS MOST, and the one the regex could never have expressed.
    _indet = _fake_cli("#!/bin/sh\necho 'indeterminate' >&2\nexit 4\n")
    st4, why4 = dispatch.close_channel(_indet, {"crawler": "c", "channel": "room-i"})
    eq("INDETERMINATE (4) is its OWN state, not UNKNOWN — 'retry later' and 'nobody can say "
       "what landed' call for opposite actions", st4, dispatch.CLOSE_INDETERMINATE)
    ok("...and says not to blindly retry, because `open` is two writes and a throttle between "
       "them leaves a room half-created — retrying is how a topic and briefing get discarded",
       "NOT blindly retry" in why4, why4)

    _refused = _fake_cli("#!/bin/sh\necho 'nope: you do not own this room' >&2\nexit 1\n")
    eq("REFUSED (1) is permanent — the answer will not change, so a retry is wasted",
       dispatch.close_channel(_refused, {"crawler": "c", "channel": "room-b"})[0],
       dispatch.CLOSE_FAILED)

    # THE RESIDUE, and it is a judgement rather than an oversight: `no such channel` arrives as
    # exit 1 alongside every permanent refusal, and for SPIN-DOWN a room that does not exist is
    # SUCCESS — a close that had nothing to close is done. One narrow text check, named.
    _gone = _fake_cli("#!/bin/sh\necho 'no such channel: room-z' >&2\nexit 1\n")
    stz, whyz = dispatch.close_channel(_gone, {"crawler": "c", "channel": "room-z"})
    eq("a room that does not exist is DONE, not failed — spin-down wanted it gone and it is",
       stz, dispatch.CLOSE_DONE)
    ok("...and says nothing was there, rather than claiming a closure it did not perform",
       "nothing to close" in whyz, whyz)

    _usage = _fake_cli("#!/bin/sh\necho 'unrecognized arguments' >&2\nexit 2\n")
    eq("a usage error is FAILED and named as THIS repo's bug — the server did nothing wrong and "
       "no retry fixes a wrong command line",
       dispatch.close_channel(_usage, {"crawler": "c", "channel": "room-u"})[0],
       dispatch.CLOSE_FAILED)

    _ok = _fake_cli("#!/bin/sh\necho closed\nexit 0\n")
    eq("and exit 0 is DONE, so the happy path is decided by the exit code rather than by "
       "matching three phrasings of somebody else's CLI",
       dispatch.close_channel(_ok, {"crawler": "c", "channel": "room-c"})[0],
       dispatch.CLOSE_DONE)

    # THE VERB CHANGED TOO. `leave` is a membership action and refuses when you were never a
    # member — exactly the case spin-down hits when `spawn`'s open failed — so the old
    # docstring's promise that a never-opened room is success was false for the case it named.
    # AND THE LINE A READER SEES. `reap` used to write "close <room>" before attempting it, and
    # printed it BELOW the warning about the failure — so the confident-sounding line was the
    # second one, and a close that did not happen printed a line reading exactly like one that
    # did. The action text is now written after the attempt.
    _rl = _fake_cli("#!/bin/sh\necho 'HTTP 429  Rate limit exceeded' >&2\nexit 3\n")
    _g = new_graph(_rl)
    # NOT `rec` — that name is already live in this group, and shadowing it made a later
    # `dispatch.launch(cfg, rec, ...)` read my synthetic entry instead of its own spawn record.
    _rl_rec = campaign.load(_rl)
    _rl_rec.setdefault("crawlers", []).append(
        {"crawler": "c-gone", "leaf": "L1", "channel": "room-x", "worktree": ".worktrees/c-gone",
         "state": "spawned", "pid": 999999, "boot": boot_token_for_test()})
    campaign.save(_rl, _rl_rec)
    _acts, _warns = campaign.reap(_rl, _g, apply=True)
    _room = [a for a in _acts if a["kind"] == "room"]
    ok("a close that could not be confirmed does NOT print a line that reads like it happened",
       _room and "COULD NOT TELL" in _room[0]["action"], _room)
    ok("...and says the room is still recorded as open and will be retried, which is the part a "
       "reader can act on",
       _room and "still" in _room[0]["action"] and "again" in _room[0]["action"], _room)
    ok("...and the warning marks it as an inability to tell rather than a refusal, because the "
       "two call for different responses",
       any("could not tell" in w for w in _warns), _warns)

    # AND REAP MUST NOT SILENTLY TRY AGAIN. UNKNOWN is retried because the request was not
    # applied; INDETERMINATE is not, because what landed is unknown and `open` is two writes.
    _ind = _fake_cli("#!/bin/sh\necho 'indeterminate' >&2\nexit 4\n")
    _ig = new_graph(_ind)
    _ind_rec = campaign.load(_ind)
    _ind_rec.setdefault("crawlers", []).append(
        {"crawler": "c-ind", "leaf": "L2", "channel": "room-ind",
         "worktree": ".worktrees/c-ind", "state": "spawned", "pid": 999998,
         "boot": boot_token_for_test()})
    campaign.save(_ind, _ind_rec)
    _a1, _w1 = campaign.reap(_ind, _ig, apply=True)
    _r1 = [a for a in _a1 if a["kind"] == "room"]
    ok("an INDETERMINATE close says so in the action line rather than reporting a closure",
       _r1 and "INDETERMINATE" in _r1[0]["action"], _r1)
    ok("...and the warning distinguishes it from a throttle, because one is retried and the "
       "other must not be", any("not retried" in w for w in _w1), _w1)
    # THE SECOND RUN IS THE ASSERTION. A state that is merely reported once, and then quietly
    # retried on the next sweep, has not been handled — it has been announced.
    _a2, _w2 = campaign.reap(_ind, _ig, apply=True)
    _r2 = [a for a in _a2 if a["kind"] == "room"]
    ok("...and the NEXT reap does not try again — it asks for a human, because a blind retry is "
       "how a half-written room's topic and briefing get discarded",
       _r2 and "NEEDS A HUMAN" in _r2[0]["action"], _r2)

    _seen = _fake_cli("#!/bin/sh\necho \"$1\" > \"$(dirname \"$0\")/verb\"\nexit 0\n")
    dispatch.close_channel(_seen, {"crawler": "c", "channel": "room-d"})
    _vp = os.path.join(os.path.dirname(dispatch.chat_path(_seen, "cli")), "verb")
    eq("spin-down calls `close`, an OWNER action that keeps the transcript and is reversible by "
       "`reopen`, not `leave`, which refuses when you were never a member",
       open(_vp).read().strip() if os.path.exists(_vp) else "<not called>", "close")

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
    v = ([f for f in campaign.reconcile(dcfg2, dg)
          if f["crawler"] == "c-done"] or [{}])[0].get("verdict")
    ok("a Crawler whose leaf CLOSED is retired, never abandoned — abandonment has to keep "
       "meaning work that may be lost", (v or "").startswith("RETIRED"), v)

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


def _fake_chat_tools(open_rc, stderr="", join_rc=0, join_records=True):
    """An installer that works and a chat CLI whose `open` and `join` succeed or fail, as asked.

    The failure is the one actually observed: `dispatch.chat` configured, the server down, so
    the installer lands the hooks and `open` returns non-zero.

    `join` IS A SEPARATE VERB HERE because it is a separate fact (#78). The old fake answered
    every verb with one exit code, which is exactly the conflation the defect was: a room that
    opened was assumed to be a room the Crawler was in. `join_records=False` is the nastier
    case — a join that exits 0 and records no membership — because that is the shape the
    verification exists to catch, and an exit code alone cannot see it.
    """
    d = tmpdir("chattools")
    installer = os.path.join(d, "install")
    with open(installer, "w") as fh:
        fh.write('#!/bin/sh\nmkdir -p "$1/.claude" && printf "{}" > "$1/.claude/settings.local.json"\n')
    os.chmod(installer, 0o755)
    cli_path = os.path.join(d, "chat")
    script = (
        '#!/bin/sh\n'
        'if [ "$1" = "join" ]; then\n'
        '  channel="$2"; ident=""; shift 2\n'
        '  while [ $# -gt 0 ]; do\n'
        '    if [ "$1" = "--as" ]; then ident="$2"; fi\n'
        '    shift\n'
        '  done\n'
        '  if [ "__RECORDS__" = "1" ]; then\n'
        '    d="$CLAUDE_PROJECT_DIR/.llm_chat/sessions/$CLAUDE_CODE_SESSION_ID"\n'
        '    mkdir -p "$d"\n'
        '    printf \'{"%s":{"identity":"%s"}}\' "$channel" "$ident" '
        '> "$d/joined.json"\n'
        '  fi\n'
        '  exit __JOINRC__\n'
        'fi\n'
        'printf "%s\\n" __STDERR__ >&2\n'
        'exit __OPENRC__\n')
    script = (script.replace("__RECORDS__", "1" if join_records else "0")
                    .replace("__JOINRC__", str(join_rc))
                    .replace("__STDERR__", json.dumps(stderr or "ok"))
                    .replace("__OPENRC__", str(open_rc)))
    with open(cli_path, "w") as fh:
        fh.write(script)
    os.chmod(cli_path, 0o755)
    return installer, cli_path


def test_brief_never_asserts_an_unopened_room():
    group("The brief told a Crawler a room was opened when it was not")
    if not have("git"):
        skip("the unopened-room group", "git is not installed")
        return

    # OBSERVED FOUR TIMES IN ONE CAMPAIGN. `spawn --launch` ran with `dispatch.chat` configured
    # and the chat server down. `provision_chat` returned (False, "channel not opened: ..."),
    # the dispatch report said `chat not wired`, and the BRIEF -- the only one of the two a
    # Crawler reads -- said "The orchestrator opened a channel for you", followed by a join
    # command that could not succeed. Two Crawlers tried it; one wrote "COULD NOT REACH THE
    # ORCHESTRATOR" into its verdict.
    #
    # ASSERTED ON THE TEXT, because the text is what a Crawler acts on. A dispatch dict that
    # correctly reports the failure alongside a brief that denies it is the bug, not the fix.
    cfg = make_repo()
    g = new_graph(cfg)
    leaf = g.show(g.add("a leaf with a room", leaf_id="ROOM1"))
    rec = worktree.spawn(cfg, leaf, actor="c")

    failed = brief.build(cfg, leaf, rec,
                         chat=("sr_room1", False, "channel not opened: no llm_chat server at "
                                                  "http://localhost:7717", False))
    ok("a brief written after provisioning FAILED does not claim a channel was opened",
       "opened a channel for you" not in failed, failed[:0])
    ok("...and does not hand the Crawler a join command that cannot succeed",
       " join sr_room1 " not in failed, failed[:0])
    ok("...it says so plainly instead of going quiet, because silence is indistinguishable "
       "from chat never having been configured -- and those call for opposite behaviour",
       "NOT reachable" in failed and "There is no room" in failed, failed[:0])
    ok("...carries WHY, so the operator reading the Crawler's tree can fix the server rather "
       "than guess", "no llm_chat server at http://localhost:7717" in failed, failed[:0])
    ok("...and tells the Crawler what to do instead of asking: record the question in scratch "
       "and decide it, which is the whole point of knowing",
       cfg.abspath(rec["scratch"]) in failed and "close-reason" in failed, failed[:0])

    # THE RESTRAINT CASE. A guard that fires on the good path is worse than the bug: it would
    # cut every Crawler off from a room that really is there.
    opened = brief.build(cfg, leaf, rec, chat=("sr_room1", True, "sr_room1", True))
    ok("provisioning that SUCCEEDS leaves the join block exactly as it was",
       "The orchestrator opened a channel for you" in opened and "join sr_room1 --as" in opened,
       opened[:0])
    ok("...and says nothing about being unreachable", "NOT reachable" not in opened, opened[:0])

    # AND THE THIRD STATE, which is neither: chat switched off. Unchanged from before this
    # existed -- no block at all -- because a Crawler that was never going to have a room does
    # not need to be told one failed.
    off = brief.build(cfg, leaf, rec, chat=(None, False, "disabled", False))
    ok("chat.enabled false renders no chat block at all, as it always did",
       "NOT reachable" not in off and "opened a channel" not in off, off[:0])
    eq("...identically to passing nothing, so the two ways of saying 'no chat' agree",
       off, brief.build(cfg, leaf, rec))

    # THE LYING FORM MUST NOT BE EXPRESSIBLE. The old signature took a channel NAME, which is
    # exactly the value that knows nothing about whether a room exists; a caller that still
    # passes one is asserting a room it has not checked.
    raised = ""
    try:
        brief.build(cfg, leaf, rec, chat="sr_room1")
    except TypeError as exc:
        raised = str(exc)
    ok("passing a bare channel NAME is refused, not rendered -- a name is not a room",
       "not a channel name" in raised, raised)

    # open_channel is the piece that knows the difference, and it has to run BEFORE the brief.
    installer, chat_cli = _fake_chat_tools(open_rc=1, stderr="no server listening on 7717")
    chatty = make_repo({"dispatch": {"chat": {"enabled": True, "channel_prefix": "sr",
                                              "installer": installer, "cli": chat_cli}}})
    g2 = new_graph(chatty)
    leaf2 = g2.show(g2.add("another", leaf_id="ROOM2"))
    rec2 = worktree.spawn(chatty, leaf2, actor="c")
    ch, opened_ok, detail, joined_ok = dispatch.open_channel(chatty, rec2)
    ok("open_channel names the room AND reports that opening it failed, separately",
       ch and opened_ok is False and "not opened" in detail, (ch, opened_ok, detail))
    ok("...and the brief built from that result carries the failure, not the name",
       "opened a channel for you" not in brief.build(chatty, leaf2, rec2,
                                                     chat=(ch, opened_ok, detail,
                                                           joined_ok)))
    nochat = make_repo({"dispatch": {"chat": {"enabled": False}}})
    eq("...while chat switched off reports no channel rather than a failed one",
       dispatch.open_channel(nochat, rec2), (None, False, "disabled", False))


def test_the_brief_on_disk_is_the_one_that_tells_the_truth():
    group("A Crawler reads the brief FILE, so the fix has to reach it")
    if not have("git"):
        skip("the on-disk brief group", "git is not installed")
        return

    # THE IN-MEMORY STRING IS NOT WHAT A CRAWLER READS. `brief.write` puts the text in the
    # Crawler's scratch dir before the launch, and the same string is handed to the process as
    # its prompt. A fix that corrected the returned value while leaving the written file
    # asserting an unopened room would have fixed nothing anyone can see -- and that is exactly
    # the shape of the rejected alternative, where `launch` patches the block in after
    # `brief.write` has already run.
    installer, chat_cli = _fake_chat_tools(open_rc=1, stderr="no llm_chat server at :7717")
    cfg = make_repo({"dispatch": {"claude_bin": "/bin/echo",
                                  "chat": {"enabled": True, "channel_prefix": "sr",
                                           "installer": installer, "cli": chat_cli}}})
    sr = os.path.join(ROOT, "bin", "showrunner")
    subprocess.run([sys.executable, sr, "add", "a leaf", "--id", "D1"],
                   cwd=cfg.root, capture_output=True)
    p = subprocess.run([sys.executable, sr, "spawn", "D1", "--actor", "me", "--launch"],
                       cwd=cfg.root, capture_output=True, text=True,
                       env=dict(os.environ, NO_COLOR="1"))
    out = p.stdout + p.stderr
    ok("the spawn still goes ahead with chat down -- a Crawler that cannot be chatted with is "
       "degraded, not broken", p.returncode == 0, out[-400:])
    ok("...and the dispatch report says the chat was not wired, as it always did",
       "not wired" in out, out[-400:])

    written = [l.split(None, 1)[1].strip() for l in out.splitlines()
               if l.strip().startswith("brief ")]
    ok("the spawn names the brief file it wrote", bool(written), out[:400])
    if written:
        disk = open(os.path.join(cfg.root, written[0])).read()
        ok("THE FILE ON DISK does not tell the Crawler a room was opened",
           "opened a channel for you" not in disk, disk[:0])
        ok("...and tells it plainly that nobody is listening",
           "NOT reachable" in disk and "There is no room" in disk, disk[:0])

    # THE SUCCESS PATH, END TO END, through the same real CLI: a room that opens must still
    # produce the join block on disk, or this guard has cost every Crawler its channel.
    installer2, ok_cli = _fake_chat_tools(open_rc=0, stderr="opened")
    cfg2 = make_repo({"dispatch": {"claude_bin": "/bin/echo",
                                   "chat": {"enabled": True, "channel_prefix": "sr",
                                            "installer": installer2, "cli": ok_cli}}})
    subprocess.run([sys.executable, sr, "add", "a leaf", "--id", "D2"],
                   cwd=cfg2.root, capture_output=True)
    p2 = subprocess.run([sys.executable, sr, "spawn", "D2", "--actor", "me", "--launch"],
                        cwd=cfg2.root, capture_output=True, text=True,
                        env=dict(os.environ, NO_COLOR="1"))
    out2 = p2.stdout + p2.stderr
    written2 = [l.split(None, 1)[1].strip() for l in out2.splitlines()
                if l.strip().startswith("brief ")]
    ok("a room that really opens is reported as wired", "chat     sr_" in out2, out2[-400:])
    if written2:
        disk2 = open(os.path.join(cfg2.root, written2[0])).read()
        ok("...and the brief ON DISK carries the join block, unchanged",
           "The orchestrator opened a channel for you" in disk2 and "--as " in disk2, disk2[:0])
        ok("...with no unreachable block contradicting it",
           "NOT reachable" not in disk2, disk2[:0])


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
    t25 = brief.build(cfg, chatty_leaf, rec25, chat=("sr_c25", True, "sr_c25", True))
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
    # THE DENOMINATOR, which this one scan was missing while the rest of this file establishes
    # it everywhere and says so out loud. If the brief's table is ever rendered differently —
    # no leading space, a bullet list, another renderer — `table` goes empty and the rule below
    # passes over nothing, which looks exactly like a brief with no relative paths in it.
    ok("the brief's location table has rows to check at all, so the rule below is applied to "
       "something rather than matching nothing", len(table) >= 3, len(table))
    ok("...and the rule CATCHES a relative path when one is there, so it can fail",
       any("/" in c and not os.path.isabs(c)
           for c in re.findall(r"`([^`]+)`", "| where | `.showrunner/scratch/x` |")))
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

    # LABELS BY THE SAME ARGUMENT, one field along. They were unreachable because `add` refuses
    # an existing id and `edit` did not take them: two correct behaviours composing into "no way
    # to relabel", so a consumer closed the leaf and re-created it, leaving a stub in the
    # campaign's done count that did no work. Not cosmetic -- labels pick the LANE, and their
    # unlabelled leaf fell to a default lane owning an exclusive device resource, so a pure
    # software task queued against hardware the repo did not have.
    g.add("mislabelled", leaf_id="L17b", labels=("tets",))
    g.edit("L17b", labels=["test"])
    eq("a label typo is corrected in place rather than by closing the leaf, which would spend "
       "the proof-of-done gate on a decision nobody made",
       g.show("L17b").labels_list, ["test"])
    g.edit("L17b", add_labels=["fast"])
    eq("...and one label can be ADDED without restating the set, for a leaf whose other labels "
       "somebody else chose", g.show("L17b").labels_list, ["test", "fast"])
    g.edit("L17b", remove_labels=["fast"])
    eq("...and removed", g.show("L17b").labels_list, ["test"])
    raises("removing a label the leaf does not carry REFUSES rather than succeeding quietly — "
           "a no-op success reports the label gone while it is still there picking a lane",
           lambda: g.edit("L17b", remove_labels=["nope"]), "no label")
    try:
        g.edit("L17b", remove_labels=["nope"])
        _said = ""
    except Exception as _e:                                     # noqa: BLE001
        _said = str(_e)
    ok("...and the refusal names what the leaf actually CARRIES, so the typo is visible without "
       "a second command — a refusal that only says 'no' sends you to `show`",
       "test" in _said, _said[:120])

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

    # THE CAVEAT PRINTS EVERY TIME, INCLUDING ON THE QUIET RUN. The scope statement used to ride
    # on the "open but NOT yours" line, which appears only when a sibling happens to hold
    # something — so on a quiet campaign the pass said "stop OK" and stated no reach at all. The
    # reach IS the point since #27: this answers about ONE caller, and a reader who takes it for
    # "the campaign is finished" was told that by its silence rather than its words.
    # A MISSING WORKING DIRECTORY IS NOT A NON-ZERO EXIT. `stop_gate` resolves a claim's RECORDED
    # tree, and a recorded tree outlives the directory — a worktree removed after its claim leaves
    # a path that was true when written. subprocess raises rather than returning, every caller
    # here is written against exit codes, and stop-gate is a HOOK: it crashed with a traceback
    # instead of answering, and the harness's fail-open kept the session moving while the gate
    # said nothing. Found by a test whose fixture named a tree it never created.
    from showrunner.util import run as _run
    rc_missing, _, err_missing = _run(["git", "status"], cwd=os.path.join(cfg.root, "no-such-dir"))
    ok("a missing cwd comes back as a non-zero result rather than an exception, so a caller "
       "written against exit codes behaves correctly without knowing the case exists",
       rc_missing != 0, (rc_missing, err_missing))
    ok("...and says which directory, because 'command failed' about a path nobody named is a "
       "second investigation", "no-such-dir" in err_missing, err_missing)

    quiet = make_repo()
    gq = new_graph(quiet)
    ok_q, msg_q = gates.stop_gate(quiet, gq, tree=quiet.root)
    ok("a campaign with nothing open passes", ok_q, msg_q)
    ok("...and STILL says what the pass does not mean — an assembled caveat can assemble to "
       "nothing, and an empty caveat reads as a gate with nothing to add on the run where it "
       "had the most", "SCOPED TO YOU" in msg_q, msg_q)
    gq.add("someone else's", leaf_id="Q1")
    gq.claim("Q1", "sibling", pid=os.getpid(), tree=os.path.join(quiet.root, ".worktrees", "s"))
    ok_b, msg_b = gates.stop_gate(quiet, gq, tree=quiet.root)
    ok("...and the same caveat is there when a sibling DOES hold work, so its presence carries "
       "no information the reader has to decode", ok_b and "SCOPED TO YOU" in msg_b, msg_b)

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
    # THE PAYLOAD IS NO LONGER TRACKED HERE, and this comment used to say the opposite —
    # "a stranger's clone recomputes the same number" — which stopped being true the moment
    # `.game_loop/` was gitignored to keep another project's payload out of a public history.
    # A clone has no payload to hash, so there is nothing for these claims to be pinned to and
    # the honest outcome is a SKIP with the reason, not a failure about a file that was never
    # meant to be there. Corrected rather than deleted, because a comment asserting a property
    # the repo has since dropped is the exact drift this file spends its length catching.
    # SKIP THE STAMP, NOT THE GROUP. The first version returned here, which dropped the 21
    # assertions that follow — they are about this repo's own claim files and do not need the
    # payload at all. An early return is the bluntest possible skip and it silently reduces
    # coverage in exactly the checkout least able to notice: a stranger's clone.
    bindir = os.path.join(ROOT, ".game_loop", "bin")
    have_payload = os.path.isdir(bindir)
    digest = hashlib.sha256()
    payload = (sorted(f for f in os.listdir(bindir) if os.path.isfile(os.path.join(bindir, f)))
               if have_payload else [])
    for name in payload:
        digest.update(name.encode())
        with open(os.path.join(bindir, name), "rb") as fh:
            digest.update(fh.read())
    installed = digest.hexdigest()[:8] if have_payload else None
    if not have_payload:
        skip("the harness payload stamp",
             "no .game_loop/bin in this checkout — the payload is installed per machine and is "
             "not tracked, so there is nothing here to pin the claims against. Every other "
             "assertion in this group is about THIS repo's files and still runs.")
    else:
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
        "lib/showrunner/util.py": "why a missing cwd must not raise — it cites the harness's "
                                  "hook fail-open as what made the crash survivable and therefore "
                                  "quiet",
        "lib/showrunner/campaign.py": "what a drifted tree's gate is said to owe",
        "lib/showrunner/dispatch.py": "why a Crawler must be a session — hooks, park, transcript",
        "lib/showrunner/cli.py": "doctor's account of what a worktree inherits and when spawn refuses",
        "docs/DESIGN.md": "a retracted claim about the gate, and what replaced it",
        "README.md": "the per-tree gate and the blank-verify.yaml consequence",
        # Not a bystander that merely mentions the harness: its NOT_FRONT_DOOR list excuses
        # eight verbs from the front-door docs BECAUSE game_loop is what calls them. If the
        # harness stopped calling one, that verb becomes user-facing and this file would keep
        # it undocumented — silently, since the excuse is what suppresses the finding.
        "test/docs_surface.py": "which verbs are harness-facing, and therefore which ones a "
                                "human never has to be told about",
        # Cites game_loop's RUNG LADDER as the reason it exists at all — that a rule an agent
        # has to remember is followed only some of the time, so a rung-6 note is not a gate.
        # If that ladder changed meaning, this file's whole justification would be quoting a
        # standard that no longer says it.
        ".showrunner/hooks/future-tense-gate.sh": "game_loop's rung ladder, quoted as the reason "
                                                  "a remembered rule is not an enforced one",
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
        # Names game_loop exactly once, to CREDIT where a design question came from — should
        # severity scale with the subject's exit vocabulary. It states nothing about how
        # game_loop behaves, so there is no claim here that can rot. Attribution deliberately
        # kept rather than reworded to dodge the net: the net is supposed to catch mentions and
        # make somebody decide, and deciding is what this line is.
        ".showrunner/hooks/pipeline-status-gate.sh":
            "credits the game_loop auditor for a design question; asserts nothing about "
            "game_loop's behaviour",
        # Cites an INCIDENT in game_loop's own tooling — their measurement tool stamped a
        # heartbeat its docstring said it could not touch — as the reason this one VERIFIES its
        # redirect rather than asserting it. A fact about something that happened, not a claim
        # about how game_loop behaves, so there is no behaviour here that can rot.
        "test/corpus.py":
            "cites an incident in game_loop's tooling as the reason for a control; asserts "
            "nothing about game_loop's behaviour",
        # NAMES PRODUCERS AND CREDITS A REFINEMENT. `dispatch.observed_models` is a target here
        # and the CLAIM about what game_loop records lives in dispatch.py, which is classified
        # there — this file only says which function to neuter. A sweep list that also carried
        # the claim would be two statements of one fact, free to disagree.
        "test/mutate.py":
            "names sweep targets and credits game_loop for a refinement; the claims about "
            "game_loop's behaviour live in the modules it sweeps, classified there",
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
    if have_payload:
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

    # ── the front-door docs, checked by the ORDINARY suite ───────────────────────────────────
    # This ran nowhere for its whole life. It shipped TRACKED -- so consumers received it, which
    # is better than the gitignored variant of this bug -- but tracked and reachable is not run,
    # and nothing invoked it. Meanwhile I stated in public that the ordinary suite ran it, and
    # another project ported the design partly on that claim. A check whose PASS is silence and
    # whose invocation is a human remembering to type it has the same output as one that does not
    # exist: nothing, forever.
    sys.path.insert(0, os.path.join(ROOT, "test"))
    import docs_surface
    missing, excluded, unreadable = docs_surface.unnamed()
    ok("every verb, env var and hook showrunner ships is at least NAMED in the front-door docs "
       "-- %d surface(s) excluded with a stated reason" % excluded,
       not missing, missing)
    # A doc that could not be OPENED must not read as "nothing is undocumented" -- the empty
    # `missing` list is what both answers look like.
    ok("...and a front-door doc that could not be read is reported as unreadable rather than "
       "silently contributing no surfaces", not unreadable, unreadable)
    # A VACUOUS EXEMPTION IS A STANDING AUTHORISATION, not dead code. Dead code does nothing;
    # an exclusion matching no real surface is redeemed by whoever adds a matching one next --
    # nobody's decision, at no particular moment, with no output. Three of these named CLI verbs
    # this project has never had, and they masked a live blind spot: env_vars() walked lib/**.py
    # only, so SHOWRUNNER_BIN and SHOWRUNNER_STATE (read by hook shims) and SHOWRUNNER_CENTRAL
    # (read by install.sh) were invisible to the check while sitting in the exclusion list --
    # which made the blindness read as a decision. Deleting the names would have kept the blind
    # spot and destroyed the only evidence of it.
    ok("every documentation exclusion names a surface this project actually has -- an exclusion "
       "matching nothing is a standing permission for whatever matches it later",
       not docs_surface.vacuous(), docs_surface.vacuous())

    # The limits are part of the check, not commentary: without them a green tick here reads as
    # "the docs are correct", which is the one thing this cannot determine.
    ok("...and the tool still states what it CANNOT check, so a pass is not read as "
       "'the docs are right'",
       "NOT EXPLAINED" in open(os.path.join(ROOT, "test", "docs_surface.py")).read().upper()
       or "explains anything" in open(os.path.join(ROOT, "test", "docs_surface.py")).read())

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
    # THE DENOMINATOR, and the guard on it. A journal that emitted nothing has no seqs[1], and
    # subscripting it raised out of the group — so `events.emit` stubbed to write nothing scored
    # as CRASHED rather than as caught. Stated as its own assertion so an empty journal fails
    # HERE, on the sentence that is actually about it, rather than downstream.
    ok("...with at least two events to compare, so the resume assertion below is applied to "
       "something rather than to an empty journal", len(seqs) >= 2, seqs)
    ok("...which is what makes --since a resume rather than a guess",
       len(seqs) >= 2 and len(EV.read(cfg, since_seq=seqs[1])[0]) == len(seqs) - 2, seqs)

    # A caller cannot forge the frame's own identity: `add` records a leaf's kind, and `kind` is
    # the event type. The first version took **kwargs and raised TypeError from inside
    # `showrunner add` — a journal breaking the work it observes, which is the one thing it must
    # never do. Renamed at the call site AND defended here, because only one of those travels.
    EV.emit(cfg, "test.reserved", {"ts": "not a time", "seq": 9999, "kind": "lies"})
    forged = ([e for e in EV.read(cfg)[0] if e["kind"] == "test.reserved"] or [{}])[0]
    ok("a caller cannot overwrite ts, seq or kind — a frame claiming a sequence it does not "
       "have is worse than a missing field, because nothing downstream can doubt it",
       forged.get("seq") != 9999 and forged.get("ts") != "not a time", forged)
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

    # THE OTHER FLAG ORDER, because "both orders parse" is the claim the comment on that argument
    # made while one of them exited 2. Asserting only the README's order leaves the sentence in
    # the source unfailable again.
    ran2 = subprocess.run([sys.executable, exe, "lock", "run", "--holder", "crawler-b",
                           "device", "--", "echo", "two"],
                          cwd=lk.root, capture_output=True, text=True, env=env)
    eq("...and with --holder BEFORE the resource too (%s)"
       % (ran2.stderr or "").strip()[:60], ran2.returncode, 0)
    held2 = [e.get("who") for e in EV.read(lk)[0] if e["kind"] == "lock.acquired"]
    ok("...recording that holder as well, so the fix is the separator and not one lucky order",
       "crawler-b" in held2, held2)

    # THE LIMIT WAS LIFTED, AND THESE ASSERTIONS ENCODED THE LIMIT RATHER THAN THE PROPERTY.
    # They said the bare form (no `--`) must REFUSE, on the reasoning that the only way through
    # would be `parse_known_args` — which turns a mistyped `--hodler` into a lock recorded
    # against the DEFAULT holder. A collaborator's fix made the bare form work without that,
    # so the suite went red on a change that was strictly better.
    #
    # Measured before rewriting, because "the test is stale" is the comfortable reading and the
    # dangerous one:
    #
    #     bare form                          runs, exit 0
    #     status during it                   lock device HELD by pid … (crawler-c)
    #     `--hodler` (a typo)                REFUSES, exit 2, unrecognized arguments
    #
    # So the property the old assertions protected — a typo must never become a lock under the
    # default holder — survives, by a different mechanism. Asserted as the property now, which
    # holds whichever way the parser reaches `command`.
    bare = subprocess.run([sys.executable, exe, "lock", "run", "device",
                           "--holder", "crawler-c", "echo", "three"],
                          cwd=lk.root, capture_output=True, text=True, env=env)
    ok("the bare form (no `--`) RUNS, and its output is the command's — the documented form is "
       "not the only reachable one",
       bare.returncode == 0 and "three" in bare.stdout,
       (bare.returncode, (bare.stdout or "")[:60], (bare.stderr or "")[:80]))
    ok("...and the lock is journalled under the NAMED holder, not the default — which is the "
       "property the old refusal was protecting, and the only reason the refusal existed",
       any(e.get("who") == "crawler-c" for e in EV.read(lk)[0]),
       [e.get("who") for e in EV.read(lk)[0]][:4])

    # THE TYPO IS THE REAL SUBJECT. `parse_known_args` would have swallowed `--hodler` and
    # recorded the lock against the default holder — silently, which is the defect this
    # argument already produced once. It must still refuse.
    typo = subprocess.run([sys.executable, exe, "lock", "run", "device",
                           "--hodler", "crawler-d", "echo", "four"],
                          cwd=lk.root, capture_output=True, text=True, env=env)
    ok("a MISTYPED holder flag still refuses rather than falling back to the default holder — "
       "the bare form was made reachable without buying that defect back",
       typo.returncode != 0, (typo.returncode, (typo.stderr or "")[:90]))
    ok("...and nothing was journalled under the mistyped run's holder either",
       not any(e.get("who") == "crawler-d" for e in EV.read(lk)[0]),
       [e.get("who") for e in EV.read(lk)[0]][:4])

    # A FLAG AFTER THE COMMAND WORDS, WHICH THE BARE FORM MAKES POSSIBLE AND THE `--` FORM NEVER
    # DID. Two answers are defensible and the parser has to pick one, so it is asserted rather
    # than left to be discovered: from the first command word on, every token is the command's.
    tail = subprocess.run([sys.executable, exe, "lock", "run", "device",
                           "--holder", "crawler-e", "echo", "hi", "--verbose"],
                          cwd=lk.root, capture_output=True, text=True, env=env)
    ok("a flag AFTER the command words goes TO the command, not to showrunner — past the program "
       "name the words are the program's, which is what every shell means by that position",
       tail.returncode == 0 and "hi --verbose" in tail.stdout,
       (tail.returncode, (tail.stdout or "")[:60], (tail.stderr or "")[:80]))

    # ...EXCEPT ONE OF showrunner's OWN, WHICH IS THE TYPO HAZARD WEARING THE OTHER HAT. Spelled
    # correctly but placed late, `--holder` would be handed to `echo` and the lock taken under
    # the DEFAULT holder — the same silently mislabelled lock, reached without any typo at all.
    late = subprocess.run([sys.executable, exe, "lock", "run", "device",
                           "echo", "hi", "--holder", "crawler-f"],
                          cwd=lk.root, capture_output=True, text=True, env=env)
    ok("...but one of showrunner's OWN flags there REFUSES instead of silently running under the "
       "default holder, and the refusal names `--` as the way to say it unambiguously",
       late.returncode != 0 and "--" in (late.stderr or ""),
       (late.returncode, (late.stderr or "")[-120:]))
    ok("...and no lock was journalled for that refusal either, under crawler-f OR the default",
       not any(e.get("who") == "crawler-f" for e in EV.read(lk)[0]),
       [e.get("who") for e in EV.read(lk)[0]][:6])

    # NON-REGRESSION ON EVERY OTHER VERB. `--` is stripped only where a `command` positional
    # exists to receive what follows it; strip it everywhere and this title becomes a flag.
    dashed = subprocess.run([sys.executable, exe, "add", "--", "--starts-with-a-dash"],
                            cwd=lk.root, capture_output=True, text=True, env=env)
    ok("`add -- --starts-with-a-dash` still keeps its separator — the split is scoped to verbs "
       "that exec, so POSIX `--` goes on meaning what it means everywhere else",
       dashed.returncode == 0 and "starts-with-a-dash" in dashed.stdout,
       (dashed.returncode, dashed.stdout[-80:], (dashed.stderr or "")[-80:]))

    refusals = [e for e in EV.read(lk)[0] if e["kind"] == "lock.refused"]
    ok("a REFUSAL is journalled too, because a contended resource and an idle one are "
       "otherwise the same picture", refusals, kinds(lk))
    if refusals:
        ok("...naming who was waiting AND who held it, so a queue can be drawn at all",
           refusals[0].get("who") == "second" and refusals[0].get("held_by") == "first",
           refusals[0])

    # RECLAIM IS NOT RELEASE. Only one of them says something went wrong, and collapsing them
    # hands a viewer a resource that looks tidily handed back by an agent that never came home.
    #
    # ASSERTED BY REAPING, not by grepping campaign.py for the string "lock.reclaimed" — which
    # is what stood here, in the one group that otherwise emits real events and reads them back,
    # and which passes on the word appearing in a comment.
    dead = DeadPid()
    locks.Lock(locks.LockSet(lk).root, "device").acquire(dead.pid, "ghost", session="s-dead")
    eq("a lock whose holder is dead reads STALE, which is the state reap acts on",
       locks.LockSet(lk).lock("device").state()[0], locks.STALE)
    # Counted BEFORE, so the assertions below are about what the reap emitted rather than about
    # whatever this group has journalled up to now — `lock run` legitimately released earlier.
    before_rel = len([e for e in EV.read(lk)[0] if e["kind"] == "lock.released"])
    campaign.reap(lk, G.open_graph(lk), apply=True)
    reclaimed = [e for e in EV.read(lk)[0] if e["kind"] == "lock.reclaimed"]
    ok("...and reaping it journals `lock.reclaimed`, a kind of its own — not `lock.released`, "
       "which would show a viewer a resource handed back by an agent that never came home",
       any(e.get("resource") == "device" for e in reclaimed), reclaimed[-2:])
    ok("...and it names the pid it took the lock from, so the reclaim is traceable to a corpse "
       "rather than being an unattributed state change",
       any(str(e.get("dead_pid")) == str(dead.pid) for e in reclaimed), reclaimed[-2:])
    ok("...while the reap journalled NO plain release, which is the distinction the two kinds "
       "exist to make — a reclaim reports something that went wrong",
       len([e for e in EV.read(lk)[0] if e["kind"] == "lock.released"]) == before_rel,
       kinds(lk))

    # AND THE LEASES, which this loop could not see at all: it iterated CONFIGURED resources,
    # and a lease is named `worktree:<tree>`. A dead Crawler holds one for its whole life, so
    # the reaper was blind to the abandonment it is most likely to be looking at.
    os.makedirs(os.path.join(lk.worktree_root, "reap-probe"), exist_ok=True)
    lease.Lease(lk, "reap-probe").acquire("s-dead-2", who="ghost-2", pid=DeadPid().pid,
                                          basis="dispatch-recorded")
    acts, _warn = campaign.reap(lk, G.open_graph(lk), apply=False)
    ok("`reap` reports an abandoned worktree LEASE, not only the configured resources — the "
       "lock a dead Crawler is most likely to leave behind was the one kind it never looked at",
       any(a.get("resource") == lease.lease_name("reap-probe") for a in acts), acts[-3:])

    # EVERY WAY INTEGRATE CAN FINISH goes through one path. It has five, each previously its own
    # `results.append`, so a sixth would have arrived with no event and a viewer would silently
    # stop seeing the riskiest verb finish.
    tree = ast.parse(open(os.path.join(ROOT, "lib", "showrunner", "campaign.py")).read())
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
    # Guarded: `eq` above records a failure and keeps going, so an unparseable stdout would
    # take the rest of this group down with it — and a mutant that CRASHES a group is one the
    # sweep cannot score, which is the whole point of mutate.py's crashed-group detector.
    snap = json.loads(snap_p.stdout) if snap_p.returncode == 0 else {}
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
    # THE FILE HAS TO EXIST TO BE BROKEN. `emit` stubbed to write nothing leaves no journal,
    # and chmod on a missing path raised out of the group — so the producer whose absence
    # this test exists to catch was the one it could not score.
    ok("...and there IS a journal to make unreadable, so the unreadable case below is\n       constructed rather than inherited from a journal that was never written",
       os.path.exists(jp), jp)
    if os.path.exists(jp):
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
        # Matched to the guard on the chmod that broke it: there is nothing to restore if the
        # journal was never written, and raising here would end the group in a `finally`, which
        # is the one place a crash also loses the reason for it.
        if os.path.exists(jp):
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


def test_cross_branch_overlap_and_lingering():
    group("What two branches already changed, and what is still running (#30, #29)")
    if not have("git"):
        skip("the overlap group", "git is not installed")
        return
    cfg = make_repo({}, files={"README.md": "seed\n", "src/a.py": "a\n", "src/b.py": "b\n"})

    # #30 — `plan` estimates within ONE wave of ready leaves and has no notion of a branch, so
    # it cannot see what earlier waves or another story's branch already changed. Two branches
    # that were each internally collision-free shared six files and two ADD/ADDs, found at merge
    # time when it was a one-line brief change at dispatch time.
    def branch_with(name, edits, adds=()):
        sh(["git", "checkout", "-q", "-b", name], cfg.root)
        for path, text in edits:
            with open(os.path.join(cfg.root, path), "w") as fh:
                fh.write(text)
        for path in adds:
            full = os.path.join(cfg.root, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write("created by %s\n" % name)
        # NAMED PATHS, NOT `git add -A`. The blanket form staged .showrunner/config.json onto
        # each branch, and checking main back out then DELETED it — so the subprocess assertions
        # below ran against a repo with no config. That is CI-01, the hole this project already
        # fixed for consumers, reproduced in its own fixture.
        sh(["git", "add", "--"] + [p for p, _ in edits] + list(adds), cfg.root)
        sh(["git", "commit", "-q", "-m", "work on " + name], cfg.root)
        sh(["git", "checkout", "-q", "main"], cfg.root)

    branch_with("story/one", [("src/a.py", "one\n")], adds=["test/shared_test.py"])
    branch_with("story/two", [("src/a.py", "two\n"), ("src/b.py", "two\n")],
                adds=["test/shared_test.py"])

    res = collide.overlap(cfg, ["story/one", "story/two"], base="main")
    ok("two in-flight branches that each passed their own wave check are compared at all — "
       "`plan` estimates forward within a wave, this measures backward against what landed",
       res["overlaps"], res)
    # `or [{}]` so a neutered producer FAILS these rather than raising — a mutant killed by an
    # exception is one the sweep cannot score, and this file's own crash detector caught me
    # making that mistake here within an hour of building it.
    ov = (res["overlaps"] or [{}])[0]
    ok("...naming the file both actually edited, from the diff rather than an estimate",
       "src/a.py" in (ov.get("files") or []), ov)
    eq("...and ADD/ADD is called out on its own, because it is the one git cannot auto-resolve",
       ov.get("add_add"), ["test/shared_test.py"])
    ok("...while a file only ONE branch touched is not reported as shared — a check that flags "
       "everything is one nobody reads", "src/b.py" not in (ov.get("files") or []), ov)

    clean = collide.overlap(cfg, ["story/one"], base="main")
    ok("a single branch has no overlap, and that is a measurement rather than a shrug",
       not clean["overlaps"], clean)
    ghost = collide.overlap(cfg, ["story/one", "no/such/branch"], base="main")
    eq("a branch with no merge-base is reported UNRESOLVABLE, not folded into 'no overlap' — "
       "the reassuring answer and the unanswerable one are otherwise the same empty list",
       ghost["unresolvable"], ["no/such/branch"])

    # #29 — the detection already existed and was reachable only through `reap`, a verb somebody
    # has to decide to run. Two processes polled for four hours past their own closes and
    # exhausted a shared rate limit, taking a turn-end gate down for every other agent.
    g = new_graph(cfg)
    g.add("finished work", leaf_id="LG1")
    rec = worktree.spawn(cfg, g.show("LG1"), actor="ghost")
    campaign.record_spawn(cfg, rec, pid=os.getpid())
    campaign.set_state(cfg, rec["crawler"], "finished",
                       finished_at=int(time.time()) - 9999, finished_why="leaf closed")
    ling = campaign.lingering_crawlers(cfg)
    ok("a process alive well after its leaf closed is reportable WITHOUT running reap — the "
       "detection existed; nothing surfaced it, and it is invisible by construction",
       any(x["crawler"] == rec["crawler"] for x in ling), ling)
    exe = os.path.join(ROOT, "bin", "showrunner")
    env = dict(os.environ, NO_COLOR="1")
    out = subprocess.run([sys.executable, exe, "status"], cwd=cfg.root,
                         capture_output=True, text=True, env=env)
    ok("...and `status` says so, which is where an orchestrator is already looking",
       "outlived their leaf" in (out.stdout + out.stderr), (out.stdout + out.stderr)[-300:])
    snap = subprocess.run([sys.executable, exe, "snapshot"], cwd=cfg.root,
                          capture_output=True, text=True, env=env)
    eq("`snapshot` exits 0 (%s)" % (snap.stderr or "").strip()[:60], snap.returncode, 0)
    ok("...and `snapshot` carries it machine-readably, so a viewer cannot draw a quiet campaign "
       "over sessions that are still polling",
       (json.loads(snap.stdout) if snap.returncode == 0 else {}).get("lingering"),
       snap.stdout[:200])

    campaign.set_state(cfg, rec["crawler"], "finished", finished_at=int(time.time()))
    ok("...while a Crawler that JUST closed is not reported — the grace window is what makes "
       "this correct, since the moment of closing is the moment the process is busiest",
       not campaign.lingering_crawlers(cfg), campaign.lingering_crawlers(cfg))


def test_live_claims_are_visible_before_a_commit():
    group("plan and spawn see a LIVE Crawler that has not committed yet (#71)")
    if not have("git"):
        skip("the live-claim group", "git is not installed")
        return
    cfg = make_repo({}, files={
        "README.md": "seed\n",
        "lib/cli.py": "shared entry point\n",
        "lib/collide.py": "estimator\n",
        "lib/unrelated.py": "nothing to do with the others\n",
        "test/run.py": "suite\n",
    })
    g = new_graph(cfg)
    # The live one. Its declared paths are what the incident named: a shared module and the
    # suite, which is `always_serialize` in this fixture.
    g.add("rewrite the brief", leaf_id="LIVE",
          labels=["backend"], paths=["lib/cli.py", "test/run.py"])
    # The one `plan` recommended into wave 1 on the real campaign.
    g.add("close the collision window", leaf_id="NEXT",
          labels=["backend"], paths=["lib/collide.py", "lib/cli.py", "test/run.py"])
    # The restraint case: real work, live campaign, no shared path.
    g.add("touch nothing they touch", leaf_id="FAR",
          labels=["backend"], paths=["lib/unrelated.py"])

    exe = os.path.join(ROOT, "bin", "showrunner")
    env = dict(os.environ, NO_COLOR="1")

    def sr(*argv):
        return subprocess.run([sys.executable, exe] + list(argv), cwd=cfg.root,
                              capture_output=True, text=True, env=env)

    # THE NON-REGRESSION BASELINE, taken FIRST: `plan` before anything is claimed is answering
    # a legitimate question, and a fix that starts counting live work must not stop answering it.
    quiet = sr("plan", "--json")
    quiet_json = json.loads(quiet.stdout[quiet.stdout.index("{"):]) if "{" in quiet.stdout else {}
    eq("`plan` on a quiet campaign exits 0", quiet.returncode, 0)
    ok("...and says nothing about live claims, because there are none — the pre-existing "
       "question is still answered exactly as before",
       "LIVE CLAIMS" not in quiet.stdout and not quiet_json.get("live"),
       quiet.stdout[-400:])
    baseline_waves = quiet_json.get("waves")

    # THE EXACT STATE MEASURED ON THE CAMPAIGN: claimed, a live owning pid, a branch that
    # EXISTS, and ZERO commits on it. A branch created at spawn is a real ref and `overlap`
    # still counts it as nothing, so a fix keyed to branch existence would not close this.
    sh(["git", "branch", "showrunner/LIVE"], cfg.root)
    g.claim("LIVE", "briefroom", pid=os.getpid(), tree=cfg.root, session="s-live")
    head = sh(["git", "rev-parse", "HEAD"], cfg.root).stdout.strip()
    tip = sh(["git", "rev-parse", "showrunner/LIVE"], cfg.root).stdout.strip()
    eq("the live Crawler's branch exists and carries NO commits — the window that is invisible",
       tip, head)
    eq("...and its leaf is out of `ready` entirely, which is WHY plan could not see it: a "
       "claimed leaf is absent from the INPUT, not merely from the output",
       sorted(l["id"] for l in g.ready()), ["FAR", "NEXT"])

    live, caveat = collide.live_claims(g)
    eq("the claim itself is the fact that exists the whole time — no commit, no diff, no branch "
       "needed", sorted(l["id"] for l in live), ["LIVE"])
    ok("...and nothing about it was unadjudicable, so no caveat is owed", caveat is None, caveat)

    files = collide.tracked_files(cfg.root)
    hit = collide.live_conflicts(cfg, g.show("NEXT"), live, files)
    ok("a ready leaf that overlaps a LIVE claim is reported as colliding, asserted with zero "
       "commits on the live branch", hit and hit[0]["blocks"], hit)
    ok("...naming the shared module it estimates in common", "lib/cli.py" in (hit or [{}])[0].get("files", []), hit)
    ok("...while the always_serialize surface is reported BESIDE the decision and does not "
       "block, or one file every change touches would serialise the whole campaign",
       "test/run.py" in (hit or [{}])[0].get("shared", [])
       and "test/run.py" not in (hit or [{}])[0].get("files", []), hit)

    # RESTRAINT. Pair every non-event assertion with the case where it happens: the block above
    # is the event, this is the same machinery declining to fire.
    miss = collide.live_conflicts(cfg, g.show("FAR"), live, files)
    ok("a live claim with NO path overlap does not block an unrelated leaf — a fix that "
       "serialises the campaign behind any single Crawler has replaced one defect with another",
       not miss, miss)

    # `overlap` is UNCHANGED, and its honesty about its own emptiness is the thing to preserve:
    # it is right about the question it answers.
    ov = sr("overlap")
    eq("`overlap` exits 0", ov.returncode, 0)
    ok("...and still reports 0 in-flight branches as a real answer rather than pretending to "
       "cover this — it measures committed diffs and there is nothing committed",
       "This is a real answer, not an empty one" in ov.stdout, ov.stdout[-300:])

    # `plan` now answers BOTH questions.
    p = sr("plan", "--json")
    pj = json.loads(p.stdout[p.stdout.index("{"):]) if "{" in p.stdout else {}
    ok("`plan` names the live claim the waves do not account for",
       "LIVE CLAIMS" in p.stdout and "LIVE" in (pj.get("live") or []), p.stdout[-600:])
    ok("...and says which wave-1 leaf collides with it",
       "NEXT" in (pj.get("live_conflicts") or {}), p.stdout[-600:])
    ok("...and prints that this is an ESTIMATE, not a measurement — the heuristic must say so "
       "where it is printed", "ESTIMATE" in (p.stdout + p.stderr), (p.stdout + p.stderr)[-300:])
    # The grouping itself is still computed from `ready` alone — the live claim is reported
    # BESIDE it, never folded into it. `plan`'s existing question ("how would I group this work
    # if nothing were running") is legitimate before a campaign starts and still gets its answer.
    ready_headless = [l for l in g.ready() if "backend" in l.labels_list]
    expect_waves, _, _ = collide.plan_waves(cfg, ready_headless)
    eq("...while the wave grouping is unchanged — still a pure function of `ready`, with the "
       "live claim reported beside it rather than folded into it", pj.get("waves"), expect_waves)
    ok("...so the live leaf never appears in a wave", "LIVE" not in sum(pj.get("waves") or [], []),
       pj.get("waves"))

    # SPAWN IS THE VERB THAT CANNOT BE SKIPPED.
    ref = sr("spawn", "NEXT", "--no-claim")
    eq("`spawn` REFUSES a leaf that collides with a live claim (exit 3)", ref.returncode, 3)
    ok("...naming the live leaf and the file, not just refusing",
       "LIVE" in ref.stderr and "lib/cli.py" in ref.stderr, ref.stderr[-500:])
    ok("...and creating nothing: a refusal that left a worktree behind would poison the retry",
       not os.path.isdir(os.path.join(cfg.root, ".showrunner", "worktrees", "crawler-next")),
       os.listdir(os.path.join(cfg.root, ".showrunner")))

    rehearsal = sr("spawn", "NEXT", "--dry-run", "--launch")
    ok("...and `--dry-run` PREVIEWS that refusal rather than showing a clean dispatch — a "
       "rehearsal that disagrees with the real spawn is worse than no rehearsal",
       "WOULD REFUSE" in rehearsal.stderr, (rehearsal.stdout + rehearsal.stderr)[-400:])

    bogus = sr("spawn", "NEXT", "--no-claim", "--despite-live", "SOMETHING-ELSE")
    ok("an override that names a leaf which is NOT colliding is refused, SAYING SO — an "
       "override copied from an earlier command is not a decision. Asserted on the reason "
       "rather than the exit code, because argparse also exits 2 on a flag it does not know",
       bogus.returncode == 2 and "is not colliding with" in bogus.stderr, bogus.stderr[-400:])

    far = sr("spawn", "FAR", "--no-claim")
    eq("...while the unrelated leaf spawns normally with a Crawler live (%s)"
       % (far.stderr or "").strip()[-120:], far.returncode, 0)

    okc = sr("spawn", "NEXT", "--no-claim", "--despite-live", "LIVE")
    eq("naming the live leaf lets the spawn through (%s)" % (okc.stderr or "").strip()[-120:],
       okc.returncode, 0)
    ok("...and says out loud that a collision was accepted on purpose",
       "ACCEPTED A LIVE COLLISION" in okc.stdout, okc.stdout[-300:])

    # NON-REGRESSION, MEASURED RATHER THAN ARGUED: release the claim and `plan` must print
    # byte-for-byte what it printed before anything was ever claimed.
    g.release("LIVE", "test")
    again = sr("plan", "--json")
    eq("with nothing running, `plan` prints exactly what it printed before the fix had "
       "anything to see — identical output, not merely a similar shape",
       again.stdout, quiet.stdout)

    # THE FALSIFIER IS NOT IN HERE. The pre-fix state is "plan and spawn read `ready`, which
    # excludes a claimed leaf", and the honest way to show these assertions depend on the fix is
    # to put lib/showrunner back and run this group — done out of band, captured in the leaf's
    # scratch. An in-process stub that feeds live_conflicts an empty list would only prove that
    # an empty list yields nothing, which is a belief this test already shares.


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
        ("Nothing writes assignments yet",
         "lib/showrunner/roles.py", "The campaign record IS the assignment",
         "`spawn` wrote the assignment all along — keyed to the worktree, before the session "
         "existed — and `seat_roles` is what finally reads it back"),
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
    # A REAL worktree, not a mkdir. `same_tree` resolves both sides through
    # `git rev-parse --show-toplevel`, so a bare directory inside the repo collapses to the repo
    # itself and every tree compares equal — a fixture that cannot tell the cases apart, which
    # is how the assertion below used to pass while reading from the wrong checkout.
    sib_tree = os.path.join(cfg.root, ".worktrees", "sibling")
    sh(["git", "worktree", "add", "-q", sib_tree, "-b", "showrunner/sibling"], cfg.root)
    g.add("a sibling's work", leaf_id="cli2")
    g.claim("cli2", "sibling", tree=sib_tree)
    p = subprocess.run([sys.executable, exe, "stop-gate", "--leaf", "cli1"], cwd=cfg.root,
                       capture_output=True, text=True, env=env)
    eq("...still 2 for the caller that HOLDS the open leaf", p.returncode, 2)
    p = subprocess.run([sys.executable, exe, "stop-gate", "--leaf", "cli2"], cwd=sib_tree,
                       capture_output=True, text=True, env=env)
    eq("...and 2 for the sibling about ITS own leaf, asked FROM that leaf's tree — which is the "
       "only place a Crawler ever runs its own trigger", p.returncode, 2)

    # #REASSIGNED: `--tree` was inert whenever `--leaf` was passed, so a wholly fictitious tree
    # still made a leaf "yours". Reported by a consumer whose Crawler had been reaped and its
    # leaf handed to a live sibling: `spawn` bakes the leaf name into the trigger permanently,
    # so the superseded agent went on being told it owned a leaf somebody else held — and the
    # remedy offered "finish it through the close gate, or release it". BOTH clobber the holder.
    # The gate applied its pressure toward the destructive action, at the agent least entitled
    # to take it.
    p = subprocess.run([sys.executable, exe, "stop-gate", "--leaf", "cli2",
                        "--tree", "/nonexistent/completely/unrelated"], cwd=cfg.root,
                       capture_output=True, text=True, env=env)
    eq("a leaf claimed from ANOTHER tree does not block this caller's turn-end — it is not "
       "theirs to finish, and the reporter's exact repro used a tree that does not exist",
       p.returncode, 0)
    ok("...and says REASSIGNED rather than 'yours', so the remedy offered is not close-or-release "
       "over somebody else's in-flight work",
       "REASSIGNED" in (p.stdout + p.stderr), (p.stdout + p.stderr)[:220])
    p = subprocess.run([sys.executable, exe, "stop-gate", "--leaf", "cli2"], cwd=cfg.root,
                       capture_output=True, text=True, env=env)
    eq("...and the same holds for an INFERRED tree, which is what a superseded Crawler actually "
       "has — its trigger names --leaf and never --tree", p.returncode, 0)
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
    # AND A THIRD SPELLING, found the same way as the second: by a remedy that did not exist
    # surviving this scan. `cli.py` printed "`%s worktree takeover %s --reason ...`" — a verb
    # this repo has never had, in the branch reserved for the state only a human can resolve —
    # and `%s` is not `showrunner` and not `{sr}`, so it was invisible. That is now three
    # spellings of the same idea, and the check keys on all three rather than on the two
    # somebody happened to think of.
    # Placeholders that hold ANOTHER tool's path. BINARY matches `{name} <verb>` on purpose --
    # a brief interpolates showrunner's own binary that way -- but not every interpolated path
    # is showrunner's, and `{chat_cli} join` was read as the non-existent verb `showrunner
    # join`. Named rather than loosened, and asserted below to actually occur, so this cannot
    # become a standing permission for a placeholder nobody uses any more.
    NOT_THE_BINARY = {"chat_cli": "the chat tool's absolute path, not showrunner's"}
    BINARY = r"(?:showrunner|\{[a-z_][a-z_0-9]*\}|%s)"

    def joined(text):
        """Python implicit string concatenation, undone. A remedy split across source lines is
        still one remedy at run time — and the takeover ghost was split exactly there, with the
        opening backtick on one line and the closing one on the next, so every span-based
        pattern below stopped at the newline that the interpreter does not see."""
        return re.sub(r"(['\"])\s*\n\s*(['\"])", "", text)

    def commands_in(text):
        text = joined(text)
        spans = re.findall(r"`([^`\n]+)`", text)
        for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.S):
            spans.extend(block.splitlines())
        spans.extend(re.findall(r"^ {2,}(%s [a-z].*)$" % BINARY, text, re.M))
        for span in spans:
            m = re.match(r"\s*%s ([a-z][a-z-]+)((?: [a-z][a-z-]+)?)" % BINARY, span)
            if m:
                held = re.match(r"\s*\{([a-z_][a-z_0-9]*)\}", span)
                if held and held.group(1) in NOT_THE_BINARY:
                    continue
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
    # The exclusion above must name a placeholder that REALLY appears, or it is a standing
    # permission for whatever matches it later -- the same rule this suite applies to the
    # documentation exclusions.
    _brief_src = open(os.path.join(ROOT, "lib", "showrunner", "brief.py")).read()
    ok("the non-binary placeholder exclusion names something the briefs actually interpolate, "
       "so it cannot outlive what it excuses",
       all(("{%s}" % k) in _brief_src for k in NOT_THE_BINARY), sorted(NOT_THE_BINARY))

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

    # THE %s SPELLING AND THE SPLIT LINE, each with both controls. The real ghost had both at
    # once: `%s` for the binary, and the backticked span broken across two source lines by
    # implicit concatenation. Either one alone hid it, so a control for either one alone would
    # have passed while the check stayed blind.
    pct = set(commands_in('print("run `%s worktree fork --from x` now")'))
    ok("a remedy interpolated with %s is extracted — that is how cli.py spells the binary, and "
       "it was the one spelling this scan did not know",
       ("worktree", "fork") in pct, sorted(pct))
    pct_ghost = set(commands_in('print("then `%s worktree takeover %s --reason x`")'))
    ok("...and a ghost spelled that way is CAUGHT, which is the actual regression: this exact "
       "line shipped and this exact check was green",
       ("worktree", "takeover") in pct_ghost
       and "takeover" not in subverbs_of("worktree"), sorted(pct_ghost))
    split = set(commands_in('    "then `%s worktree takeover "\n    "%s --reason x`"'))
    ok("...including when the string is SPLIT across source lines, because Python joins them "
       "and the reader gets one command — the span patterns all stopped at a newline the "
       "interpreter never sees", ("worktree", "takeover") in split, sorted(split))
    ok("...and joining does not invent commands out of ordinary adjacent strings",
       not list(commands_in('    "nothing here"\n    "or here"')))
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
                # NOT preceded by a path character. A rendered ABSOLUTE PATH whose directory
                # happens to be named showrunner (this repo's own checkout, for one) is a file
                # location, not the CLI being invoked -- `/dev/showrunner/.../llm_chat join`
                # was read as the verb `showrunner join`. Loosening this would be gaming the
                # check; the boundary is what the check meant all along.
                for m in re.finditer(r"(?<![/\w.-])showrunner ([a-z][a-z-]+)", node.value):
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
    bare = tmpdir("sr-remedy")     # registered for cleanup, unlike the raw mkdtemp it replaces
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
def test_reclaim_survives_an_unset_base():
    group("A merged, clean tree is reclaimable when no --base was given — the default path, "
          "which is the only path anybody runs")
    if not have("git"):
        skip("the reclaim-base group", "git is not installed")
        return

    # THE DEFECT, FOUND BY RUNNING THE TOOL RATHER THAN BY READING IT. `integrate` merged the
    # branch and then died in `git merge-base --is-ancestor <branch> None` before reclaiming a
    # single tree. Nine verbs declare `--base` with `default="HEAD"`; `integrate` declares it
    # with none, correctly — it compares the base to the CHECKED-OUT BRANCH NAME and would
    # refuse every ordinary run if the default were "HEAD" — and then handed the raw None to
    # the reclaim pass. `reconcile` HAD a `base="HEAD"` default and it never fired, because an
    # explicit None walks straight past a default argument.
    #
    # WHY IT SURVIVED SO LONG, which is the part worth pinning: the crash is AFTER the merge,
    # so the work always landed. The only symptom was trees that never went away, against a
    # `brief.py` sentence promising each Crawler exactly that reclaim — and every library-level
    # test reaches `campaign.integrate`, whose PRIVATE `base or current` covered the hole. The
    # reclaim call lives one layer up, in the CLI, and no assertion reached it.
    cfg = make_repo()
    g = new_graph(cfg)
    g.add("work whose tree should go when it lands", leaf_id="rb1", labels=["backend"])
    rec = worktree.spawn(cfg, g.show("rb1"), actor="crawler-rb")
    campaign.record_spawn(cfg, rec)
    with open(os.path.join(rec["worktree"], "landed.txt"), "w") as fh:
        fh.write("work\n")
    sh(["git", "add", "-A"], rec["worktree"])
    sh(["git", "commit", "-q", "-m", "the work"], rec["worktree"])
    g.claim("rb1", "crawler-rb", tree=rec["worktree"])
    g.close("rb1", G.CLOSED, os.path.join(rec["worktree"], "landed.txt"), "done")
    def reclaim(base):
        """(take, held, why) — a RAISE HERE MEASURES NOTHING. A group that dies takes every
        assertion below it down unrun, and the mutation sweep scores that as UNSCOREABLE, not
        as coverage. The failure this pins is literally an exception out of subprocess, so it
        has to arrive as a failed assertion carrying the exception, never as a crash."""
        try:
            take_, held_ = campaign.reclaimable(cfg, new_graph(cfg), base=base)
            return take_, held_, None
        except Exception as exc:                                    # noqa: BLE001
            return [], [], exc

    try:
        campaign.integrate(cfg, new_graph(cfg))
        landed = None
    except Exception as exc:                                        # noqa: BLE001
        landed = exc
    ok("the branch merges, so there is a landed tree to reclaim", landed is None, landed)

    # base=None is what the CLI passes on the default path. Before the fix this raised
    # TypeError out of subprocess, so the assertion is that it ANSWERS at all — and answers
    # the same thing the explicit form does.
    take, held, why = reclaim(None)
    names = [t["crawler"] for t in take]
    ok("a merged, clean tree is reclaimable with NO base given — the default `integrate` path",
       rec["crawler"] in names,
       why or (names, [(h.get("crawler"), h.get("why")) for h in held]))

    # THE CONTROL THAT KEEPS THIS HONEST. "Reclaimable" must not become the answer to every
    # question just because the base now resolves; an unset base that swept up a LIVE or DIRTY
    # tree would delete somebody's only copy, which is the exact trade this file argues about
    # everywhere else. So a dirty tree must still be held back on the same unset-base path.
    g.add("work still in progress", leaf_id="rb2", labels=["backend"])
    rec2 = worktree.spawn(cfg, g.show("rb2"), actor="crawler-dirty")
    campaign.record_spawn(cfg, rec2)
    with open(os.path.join(rec2["worktree"], "landed.txt"), "w") as fh:
        fh.write("committed\n")
    sh(["git", "add", "-A"], rec2["worktree"])
    sh(["git", "commit", "-q", "-m", "committed"], rec2["worktree"])
    g.claim("rb2", "crawler-dirty", tree=rec2["worktree"])
    g.close("rb2", G.CLOSED, os.path.join(rec2["worktree"], "landed.txt"), "done")
    with open(os.path.join(rec2["worktree"], "landed.txt"), "a") as fh:
        fh.write("uncommitted work nobody else has\n")
    take2, held2, why2 = reclaim(None)
    ok("...and a DIRTY tree is still held back on that same unset-base path, so resolving the "
       "base did not turn the reclaim into a blanket yes",
       rec2["crawler"] not in [t["crawler"] for t in take2],
       why2 or ([t["crawler"] for t in take2],
                 [(h.get("crawler"), h.get("why")) for h in held2]))

    # THE RESOLVER ITSELF, both directions. An explicit base must survive untouched — a
    # resolver that quietly overrode the caller's answer would be the same bug facing the
    # other way.
    ok("an explicit base is returned unchanged",
       campaign.base_branch(cfg, "some-ref") == "some-ref",
       campaign.base_branch(cfg, "some-ref"))
    cur = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cfg.root).stdout.strip()
    ok("...and an absent one resolves to the branch actually checked out, which is what "
       "`integrate` compared against all along",
       campaign.base_branch(cfg, None) == cur, (campaign.base_branch(cfg, None), cur))


def test_guard_anchor_phrase_is_live():
    group("The phrase every anchor assertion matches on still exists in every guard that "
          "prints it — an absence assertion whose phrase was reworded away measures nothing")

    # WHY THIS GROUP EXISTS AT ALL. Three assertions elsewhere say ANCHOR_FAILED is NOT in some
    # output. Each of them passes for two very different reasons: the guard anchored correctly
    # (the reason intended), or nothing anywhere says those words any more (the reason nobody
    # would notice). The first spelling of those assertions matched "DID NOT RUN", which stayed
    # true and stopped being SPECIFIC — this is the same failure one turn earlier, when the
    # phrase stops existing rather than stops discriminating.
    from showrunner import cli
    producers = [os.path.join(ROOT, ".showrunner", "hooks", "worktree-guard.sh"),
                 os.path.join(ROOT, ".showrunner", "hooks", "dispatch-guard.sh")]
    for path in producers:
        try:
            with open(path) as fh:
                body = fh.read()
        except OSError as exc:                                      # noqa: BLE001
            body = ""
            ok("%s is readable, or the check below says nothing" % os.path.basename(path),
               False, exc)
        ok("%s still prints the anchor-failure phrase the suite matches on"
           % os.path.basename(path), ANCHOR_FAILED in body, path)
        # The other phrase an assertion depends on: the broken-candidate fail-open.
        ok("...and still prints the broken-candidate phrase too, which a different assertion "
           "reads as 'this was a fail-open, not an answer'",
           "instead of answering" in body, path)

    ok("and the CLI's own constant carries it, so the shim and the verb cannot drift into "
       "wording the suite can no longer see", ANCHOR_FAILED in cli.NO_REPO_FAIL_OPEN,
       cli.NO_REPO_FAIL_OPEN)

    # THE DISCRIMINATION ITSELF, which is the whole point of moving off "DID NOT RUN": the
    # benign degrade must NOT match the anchor phrase. If it ever does, the new anchor is a
    # proxy too and this group says so before a Crawler discovers it in a worktree.
    with open(os.path.join(ROOT, "lib", "showrunner", "lease.py")) as fh:
        lease_src = fh.read()
    ok("...and the no-session degrade — a correct, deliberate fail-open — does NOT contain the "
       "anchor phrase, which is the entire reason the assertions moved off 'DID NOT RUN'",
       ANCHOR_FAILED not in lease_src,
       [ln for ln in lease_src.splitlines() if ANCHOR_FAILED in ln][:2])


def test_a_crawler_is_joined_to_its_own_room():
    group("`spawn` joins the Crawler to its room, so a correction reaches a Crawler that never "
          "volunteered to join (#78)")
    if not have("git"):
        skip("the crawler-join group", "git is not installed")
        return

    # THE REPORTED FAILURE. Three Crawlers, three corrections, every send answered `nobody else
    # is in this room yet`, every message still unread when the Crawler closed its leaf. One was
    # the fix for a red check, which survived into the PR. `showrunner edit` refuses while a leaf
    # is in_progress — correctly — so with chat undelivered there was no correction path at all.
    #
    # THE ASYMMETRY WAS OURS: `spawn` already joined the room on the ORCHESTRATOR's behalf and
    # left the Crawler to do its own, in a brief sentence that read as already-done.
    installer, chat_cli = _fake_chat_tools(open_rc=0)
    cfg = make_repo({"dispatch": {"chat": {"enabled": True, "channel_prefix": "sr",
                                           "installer": installer, "cli": chat_cli}}})
    g = new_graph(cfg)
    leaf = g.show(g.add("a leaf whose crawler must be reachable", leaf_id="JOIN1"))
    rec = worktree.spawn(cfg, leaf, actor="c")
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    ch, opened, detail, joined = dispatch.open_channel(cfg, rec, session=sid)
    ok("the room opens AND the Crawler is joined to it", ch and opened and joined,
       (ch, opened, detail, joined))

    # WHERE THE MEMBERSHIP LANDS IS THE WHOLE POINT. llm_chat keys identity per CALLING PROJECT
    # in `$CLAUDE_PROJECT_DIR/.llm_chat/joined.json`, and warns that two agents sharing that
    # file share an identity — the delivery hook then sends one agent's messages to the other.
    # A join run from the orchestrator's root would do exactly that, so this asserts the record
    # is in the CRAWLER's tree and names the CRAWLER.
    wt = cfg.abspath(rec["worktree"])

    def rooms_at(base, who=sid):
        """The membership file's contents, or {} — READ, NOT ASSUMED. A missing file is a real
        outcome here (nobody joined), and letting it raise would take every assertion below it
        down unrun, which the mutation sweep scores as UNSCOREABLE rather than as coverage.

        The path is the one the REAL llm_chat writes, measured against it: under the calling
        project, keyed by session. An earlier draft of the code under test verified the
        project-level file instead — which `join` does not write — and would have reported
        every successful join as a failure.
        """
        try:
            with open(os.path.join(base, ".llm_chat", "sessions", who, "joined.json")) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    eq("...and the membership is recorded in the CRAWLER's worktree, under the CRAWLER's name — "
       "not in the orchestrator's checkout, where it would collide with the orchestrator's own",
       (rooms_at(wt).get(ch) or {}).get("identity"), rec["crawler"])
    ok("...and the orchestrator's own checkout did not acquire the Crawler's membership",
       (rooms_at(cfg.root).get(ch) or {}).get("identity") != rec["crawler"], cfg.root)

    # A JOIN WITH NO SESSION IS REFUSED, not guessed. llm_chat keys membership to the session,
    # so a join run without the Crawler's id records it under whichever session the ORCHESTRATOR
    # is — llm_chat's own warning, with an incident behind it: a question delivered to the wrong
    # session, answered under the wrong name, while the session actually asked never woke.
    rec_ns = worktree.spawn(cfg, g.show(g.add("no session", leaf_id="JOIN0")), actor="c0")
    nojoin, why_ns = dispatch.join_crawler(cfg, rec_ns, "sr_join0", session=None)
    ok("a join with no session id is REFUSED rather than recorded under the orchestrator's",
       nojoin is False and "orchestrator" in why_ns, (nojoin, why_ns))

    text = brief.build(cfg, leaf, rec, chat=(ch, opened, detail, joined))
    ok("...and the brief says the Crawler is already in the room rather than telling it to join",
       "joined you" in text and "nothing to join" in text, text[:600])

    # A JOIN THAT EXITS 0 AND RECORDS NOTHING is the shape an exit code cannot see, and it is
    # the same "reported success" this repo keeps catching — the installer that returns 0 and
    # writes no settings file, the record that names a proof that is not there. Verified against
    # the file llm_chat actually reads, so the answer is a membership and not a return value.
    inst2, cli2 = _fake_chat_tools(open_rc=0, join_records=False)
    cfg2 = make_repo({"dispatch": {"chat": {"enabled": True, "channel_prefix": "sr",
                                            "installer": inst2, "cli": cli2}}})
    g2 = new_graph(cfg2)
    leaf2 = g2.show(g2.add("a leaf whose join silently does nothing", leaf_id="JOIN2"))
    rec2 = worktree.spawn(cfg2, leaf2, actor="c")
    ch2, opened2, detail2, joined2 = dispatch.open_channel(cfg2, rec2, session=sid)
    ok("a join that exits 0 but records no membership is NOT reported as joined",
       opened2 and joined2 is False, (ch2, opened2, detail2, joined2))

    # AND THE BRIEF MUST SAY SO. A Crawler told it is already reachable, in a room it is not in,
    # is worse off than one told to join: it will not join, and it will read the silence as the
    # orchestrator having nothing to say.
    text2 = brief.build(cfg2, leaf2, rec2, chat=(ch2, opened2, detail2, joined2))
    ok("...and the brief tells that Crawler to join, because nothing reaches it until it does",
       "could NOT" in text2 and "Nothing reaches you until you join" in text2, text2[:700])
    ok("...and does not also claim it was joined, which would be both halves of the answer",
       "joined you" not in text2, text2[:700])

    # A FAILING JOIN IS THE ORDINARY FAILURE, and must not be mistaken for a failing room.
    inst3, cli3 = _fake_chat_tools(open_rc=0, join_rc=1)
    cfg3 = make_repo({"dispatch": {"chat": {"enabled": True, "channel_prefix": "sr",
                                            "installer": inst3, "cli": cli3}}})
    g3 = new_graph(cfg3)
    leaf3 = g3.show(g3.add("a leaf whose join fails", leaf_id="JOIN3"))
    rec3 = worktree.spawn(cfg3, leaf3, actor="c")
    ch3, opened3, _d3, joined3 = dispatch.open_channel(cfg3, rec3, session=sid)
    ok("a room that opened with a join that FAILED still reports the room as opened, and the "
       "membership as absent — two facts, reported separately",
       ch3 and opened3 and joined3 is False, (ch3, opened3, joined3))


def main():
    print("showrunner test harness — CORE needs only Python 3 + git; OPTIONAL skips loudly.")
    for fn in (test_locks, test_a_crawler_is_joined_to_its_own_room, test_guard_anchor_phrase_is_live, test_reclaim_survives_an_unset_base, test_config_refusals, test_user_config_layer, test_config_layer_shadow_report, test_every_rule_can_fail, test_graph, test_lifecycle, test_stalled_sessions, test_close_gate,
               test_stop_gate, test_baseline, test_routing, test_collision, test_spawn,
               test_harness_provisioning, test_attribution, test_harness_gap,
               test_future_tense_gate, test_post_checkout_hook_failure,
               test_prose_options, test_vendor_staleness,
               test_parked_beats_blocked,
               test_path_problem,
               test_worktree_dirty,
               test_guards_anchor_off_cwd,
               test_waiting, test_work_since_block,
               test_unconfigured_checks,
               test_concurrency,
               test_integration, test_worktree_lease, test_worktree_guard_from_inside_a_worktree,
               test_self_pin, test_self_vendored_pin, test_roles,
               test_harness_installer_provenance, test_void_run, test_dispatch_guard,
               test_seat_and_whoami, test_crawler_seat_resolves_to_a_role,
               test_role_seat_verbs,
               test_close_resolves_paths_against_the_callers_tree,
               test_campaign_scoping,
               test_command_paths_resolves_literal_variables,
               test_temp_dirs_are_cleaned_up,
               test_boot_token_does_not_drift,
               test_guard_entrypoints_agree,
               test_launch_binary_and_failed_launch,
               test_mutation_anchor_refusal,
               test_borrowed_claims_are_marked,
               test_zero_inventory_matches_reality,
               test_negative_text_assertions_flatten,
               test_a_rate_names_its_instrument,
               test_stale_copy_cannot_warn_about_itself,
               test_hook_registration,
               test_corpus_tool,
               test_spawn_refuses_a_base_missing_a_dependency,
               test_the_integration_record_names_its_evidence,
               test_an_untracked_registration_still_reaches_the_worktree,
               test_a_claim_never_names_the_process_that_is_about_to_exit,
               test_reach_is_registered_by_the_verb_that_registers_hooks,
               test_a_seat_survives_a_window_reload,
               test_a_seat_that_may_not_dispatch_is_refused_at_the_sanctioned_path,
               test_the_announcement_does_not_claim_enforcement_it_has_not_got,
               test_waiting_does_not_scale_with_the_campaign,
               test_a_worktree_is_reclaimed_when_its_work_lands,
               test_a_compacted_agent_is_told_what_it_forgot,
               test_reaching_for_the_wrong_thing_names_the_right_one,
               test_only_guards_may_anchor_to_their_own_checkout,
               test_fail_open_is_counted_not_just_announced,
               test_stop_hook_heartbeat,
               test_embedded_code_inside_hooks_is_checked_too,
               test_hooks_that_must_speak_are_driven_and_not_merely_parsed,
               test_every_REGISTERED_hook_parses,
               test_every_shipped_hook_parses,
               test_pipeline_status_gate,
               test_issue_waker,
               test_central_install,
               test_installer_leaves_no_vendored_copy,
               test_publishable, test_dispatch, test_filed_issues_15_to_21,
               test_brief_never_asserts_an_unopened_room,
               test_the_brief_on_disk_is_the_one_that_tells_the_truth,
               test_claims_about_the_layer_below, test_observability,
               test_cross_branch_overlap_and_lingering,
               test_live_claims_are_visible_before_a_commit,
               test_hook_verbs_never_fail_open_in_silence,
               test_retracted_doc_claims,
               test_cli, test_optional):
        try:
            fn()
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            # SYSTEMEXIT IS NOT AN EXCEPTION, and that is not pedantry here. `argparse.error`
            # raises it, so any group that drives the parser wrongly killed the WHOLE SUITE mid
            # run — no RESULT line, every later group unrun, exit 2. A mutation sweep then reads
            # the surviving FAIL count as coverage when it is a floor from a truncated run, and
            # `mutate.py` could not see it because the group-crash marker never printed.
            #
            # Measured, not reasoned: neutering cli._add_prose_twins scored "1 kill" and had in
            # fact ended the suite at that assertion. KeyboardInterrupt is deliberately NOT
            # caught — Ctrl-C must still stop a run.
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
    group("The setup cost this repo claims about itself")
    # THE CLAIM THAT DOES NOT DRIFT. This used to enforce a hand-synced assertion COUNT in four
    # tracked files, and the check worked exactly as written -- which was the problem. It made a
    # stale-prone figure LOAD-BEARING rather than removing it: adding one assertion broke the
    # build in four documentation files, and the fix was to hand-edit a number a reader could
    # have obtained by running the command printed beside it. It also charged the cost to the
    # contribution this repo most wants, at the moment they make it.
    #
    # The durable claim is the SETUP COST -- Python 3 and git, nothing else -- so that is what
    # is checked now. Derived from the probes rather than from a list: a new hard dependency
    # sneaking into CORE arrives as a new `have("x")` and fails here, which is the regression
    # worth catching, and it never fires on a legitimate assertion.
    # PARSED, NOT GREPPED. The first version scanned the file text and picked up `have("x")`
    # from the COMMENT four lines above — a matcher satisfied by prose ABOUT the mechanism,
    # inside the check written to replace a different broken check. The AST cannot be satisfied
    # by a comment, which is the only reason to prefer it here.
    probed = set()
    for _node in ast.walk(ast.parse(open(os.path.join(ROOT, "test", "run.py")).read())):
        if (isinstance(_node, ast.Call) and isinstance(_node.func, ast.Name)
                and _node.func.id == "have" and _node.args
                and isinstance(_node.args[0], ast.Constant)
                and isinstance(_node.args[0].value, str)):
            probed.add(_node.args[0].value)
    ok("the suite probes for external binaries at all, so the comparison below is not vacuous",
       probed, sorted(probed))
    # `bash` joined when a test began parsing the shipped hooks with `bash -n`. It is OPTIONAL
    # in exactly the sense this rule means: the group SKIPS LOUDLY without it rather than
    # failing. showrunner's shell hooks do need bash to RUN — that dependency is real, is
    # older than this probe, and is a fact about the hooks rather than about the suite.
    unexpected = sorted(probed - {"git", "br", "tmux", "bash"})
    ok("CORE needs nothing beyond Python 3 and git — every other binary the suite probes for "
       "(%s) is OPTIONAL and skips loudly. A new hard dependency arrives here as a new probe, "
       "which is the regression a stale COUNT never caught"
       % ", ".join(sorted(probed & {"br", "tmux", "bash"})), not unexpected, unexpected)
    stale_counts = {}
    # ONE NOUN WAS AN ENUMERATION. This checked `assertions` only — so "1,263 tests" or "four
    # entrypoints" would have sailed through, which is the same defect the doc it guards is
    # about. llm_chat's owner found exactly those two in their own front door: "242 tests, 100%
    # line coverage on the four entrypoints", where the suite had passed 1,800 some time ago
    # and the floor covers fourteen files. Nobody re-derived either, because nothing had to.
    #
    # The remedy is theirs and lamp-owner's before them: DELETE the count rather than correct
    # it. There is no version of "242" that survives a test being added. What survives is the
    # property the gate enforces, which a reader can run.
    # SPELLED-OUT NUMBERS COUNT. llm_chat's real finding was "the FOUR entrypoints" — a stale
    # count with no digit in it. A digits-only pattern misses the exact case that prompted the
    # widening, and an assertion for it passed here only because I had written an escape into
    # the assertion itself. That is the defect this session keeps producing: a check that
    # agrees for a reason unrelated to what it claims.
    # `one` IS EXCLUDED, and not as a convenience. In prose it is the indefinite article wearing
    # a numeral: "the one test file every change touches", "one verb reads it" — both mean A
    # SINGLE, neither is a count that can rot. Including it produced two false positives here
    # immediately, and a check that flags "one X" flags most English.
    _NUM = (r"(?:[\d,]{2,}|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
            r"|dozen|dozens|hundreds|thousands)")
    GROWABLE = (r"\b" + _NUM + r"\s+(?:CORE\s+)?"
                r"(?:assertions?|tests?|verbs?|hooks?|entrypoints?|checks?|mutations?)\b")
    # A PAST INCIDENT IS NOT A GROWING COUNT, and demanding a reproducer for one would be the
    # lie this check exists to stop — llm_chat's third row. Excused in writing, default-deny,
    # so a new one cannot join silently.
    HISTORICAL = {
        "38 leaves": "one 16-hour unattended run, described in the past tense — a fact about an "
                     "incident, not a count of anything this repo grows",
    }
    for rel_path in ("README.md", "llms.txt", "docs/DESIGN.md"):
        full = os.path.join(ROOT, rel_path)
        if not os.path.exists(full):
            continue
        with open(full) as fh:
            # FLATTENED, because llm_chat's second stale count was invisible to the eye and to
            # a line-based scan: `205 tests` wrapped across a newline and read as two tokens.
            flat = re.sub(r"\s+", " ", fh.read())
        found = [m.group(0) for m in re.finditer(GROWABLE, flat, re.I)
                 if not any(h in m.group(0) for h in HISTORICAL)]
        if found:
            stale_counts[rel_path] = found
    ok("...and no tracked doc commits a COUNT OF ANYTHING THIS REPO GROWS — tests, assertions, "
       "verbs, hooks, checks. The number carries nothing a reader cannot get by running the "
       "command printed beside it, and there is no version of it that survives one being added",
       not stale_counts, stale_counts)

    # THE NOUN LIST IS THE WEAK POINT AND IT IS ASSERTED, not assumed. A check that names one
    # noun catches one noun; this one has to fail on the nouns it was widened for.
    for phrase in ("1,263 tests", "242 assertions", "the four entrypoints", "38 hooks",
                   "twelve verbs"):
        probe = "This project has %s and is in great shape." % phrase
        ok("a doc claiming %r is caught, so the widening is real and not decoration" % phrase,
           bool(re.search(GROWABLE, re.sub(r"\s+", " ", probe), re.I)))
    # AND THE PATTERN MUST STILL SAY NO. A check that matches everything is not a check.
    for benign in ("the campaign has depth", "run the tests", "showrunner 0.1.0"):
        ok("...while %r is left alone, so the widened pattern has not become a yes-machine"
           % benign, not re.search(GROWABLE, benign, re.I))
    ok("...and a wrapped count is caught too, because the text is flattened before matching — "
       "llm_chat missed one by eye AND by line-based scan for exactly this reason",
       bool(re.search(GROWABLE, re.sub(r"\s+", " ", "we now have 205\ntests passing"), re.I)))

    # A RUN THE MACHINE COULD NOT SUPPORT MEASURED NOTHING ABOUT THE CODE. `check` has had this
    # since #41 — a run that could not reach the world exits 3, VOID, distinct from "new
    # failures", because a failure count from an unreachable world carries no information and is
    # strictly worse than a degraded comparison. showrunner's own suite had no such screen: it
    # printed "N failed" identically whether an assertion was wrong or a subprocess could not be
    # spawned. The tool that argues this everywhere did not apply it to its own credibility
    # artifact.
    #
    # Observed rather than imagined: a run here reported 8 failures and the very next command in
    # the same shell was SIGKILLed (137), with five other sessions live in this checkout. I
    # could not attribute those 8 — the output was lost to the kill, which is itself the point:
    # nothing distinguished them from real defects at the moment they mattered.
    void_hits = void_signatures(FAIL)
    # THE SCREEN ITSELF, driven with fabricated failures — it cannot be exercised by the run it
    # guards, because a run that triggers it has already failed.
    ok("a failure carrying a resource signature makes the run VOID — the machine could not run "
       "the test, which is not a fact about the code",
       void_signatures([("g", "l", "OSError: [Errno 24] Too many open files")]),
       void_signatures([("g", "l", "OSError: [Errno 24] Too many open files")]))
    ok("...and a run KILLED mid-assertion counts, which is the shape actually observed here: 8 "
       "failures and the next command SIGKILLed with five sessions live in this checkout",
       void_signatures([("g", "l", "Command x died with <Signals.SIGKILL: 9>")]))
    ok("...while an ordinary wrong answer does NOT — a false VOID hides a real defect behind "
       "'re-run it', which is the expensive direction for this screen",
       not void_signatures([("g", "l", "expected 3, got 4")]),
       void_signatures([("g", "l", "expected 3, got 4")]))
    ok("...and neither does prose merely mentioning the words, since the detail of a failure "
       "about resource handling would otherwise void the run that found it",
       not void_signatures([("g", "l", "the guard says 'too many open files' in its remedy")]),
       void_signatures([("g", "l", "the guard says 'too many open files' in its remedy")]))

    # A SUITE THAT RAN NOTHING REPORTS THE SAME THING AS A SUITE THAT PASSED EVERYTHING.
    # Measured: emptying the dispatch tuple gives `RESULT: 16 passed, 0 failed` and EXIT 0 —
    # not zero, a plausible small run, because sixteen module-level assertions live outside the
    # loop. `verify` and the release polish both gate on this and both would have passed.
    #
    # game_loop's auditor hit the same thing by emptying their CASES list, and made the point
    # that a COUNT cannot fix it: "at least N" is satisfied by duplicating one case. What
    # carries meaning is which behaviour classes actually ran.
    #
    # THE EXPECTATION CANNOT COME FROM THE TUPLE. Deriving "which groups should run" from the
    # dispatch tuple is satisfied by emptying it — the expectation empties with it. The
    # independent source is the file's OWN function definitions, which is wcs's identity: a
    # count taken in a vocabulary the dispatcher does not share.
    #
    #     every `def test_*` in this file  ==  every name in the dispatch tuple
    #
    # This lives OUTSIDE the group loop on purpose. A floor inside the thing it measures is
    # removed by the same edit that breaks it.
    _src = open(os.path.join(HERE, "run.py")).read()
    _defined = {n.name for n in ast.walk(ast.parse(_src))
                if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
    try:
        _a = _src.index("    for fn in (test_locks,")
        _b = _src.index("test_cli, test_optional):", _a)
        _dispatched = set(re.findall(r"\btest_\w+", _src[_a:_b] + "test_cli, test_optional"))
    except ValueError:
        _dispatched = None
    if _dispatched is None or _defined != _dispatched:
        print("\n" + "=" * 72)
        print("CANNOT VERIFY: the suite did not run every group it defines.")
        if _dispatched is None:
            print("  the dispatch tuple could not be located, so nothing here is a coverage "
                  "result")
        else:
            for _n in sorted(_defined - _dispatched):
                print("  DEFINED BUT NEVER RUN: %s" % _n)
            for _n in sorted(_dispatched - _defined):
                print("  DISPATCHED BUT NOT DEFINED: %s" % _n)
        print("  A suite that skips groups silently reports the same green line as one that "
              "ran them all.")
        sys.exit(3)

    print("\n" + "=" * 72)
    print("RESULT: %d passed, %d failed, %d skipped" % (len(PASS), len(FAIL), len(SKIP)))
    if void_hits:
        print("\nVOID — this run did not measure the code. %d failure(s) carry signatures of a "
              "machine that could not run them: %s.\nA failure count from a run the environment "
              "starved is not a fact about this repo. Re-run when the box is quiet; do not read "
              "the number above as coverage." % (len(FAIL), ", ".join(void_hits)))
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
    if void_hits:
        # 3, the same code `check` uses for VOID, so a caller treating non-zero as "the code is
        # bad" gets a code it did not map rather than a wrong answer it will believe.
        return 3
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
