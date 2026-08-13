#!/usr/bin/env python3
"""Mutation sweep: which producers can stop working without the suite noticing?

A check whose PASS is silence cannot distinguish being satisfied from never having run.
That is not a guard problem — it is any assertion whose evidence is the *non-occurrence* of
something, and those are everywhere: empty diffs, absent warnings, "no errors", "not in the
list". Every one is satisfied by a broken producer.

Reading the suite does not find them. Manufacturing the absence does: neuter one producer at
a time, run the whole suite, and count what still passes. A producer that can be replaced by
a stub while the suite stays green is a producer the suite is not testing.

**Where the weak ones cluster is the finding, not the count.** They gather on *restraint* —
the behaviours a project argued itself into: reap is dry-run, an abandoned worktree is
surfaced rather than deleted, notes-drift warns instead of aborting, a parked leaf does not
block a turn-end. Each of those was a deliberate decision to *not* act, each was defended,
and each is asserted by observing that nothing happened — which is exactly what a broken
implementation also produces. Restraint is expensive to design and free to break.

This is a question to re-ask whenever a producer is added, so it is committed as a script
rather than as a one-time result.

    python3 test/mutate.py [--target NAME]

Not part of `test/run.py`: it runs the whole suite once per target and is far too slow to
owe on every change. Run it when adding a producer, or when a restraint behaviour changes.

WHAT THIS CANNOT SEE — stated here because a number in a docstring becomes a target, and a
target gets optimised rather than understood:

* It measures whether an assertion NOTICES a producer that stopped producing. It says
  nothing about whether the producer is RIGHT. A wrong message and a correct one are killed
  identically, because both are non-empty.
* **It cannot see an assertion that is CONFIDENTLY WRONG, and this is the sharpest limit
  here.** The sweep asks whether an assertion is load-bearing. A wrong assertion is perfectly
  load-bearing: it kills its mutant, reports a healthy number, and reads as coverage. llm_chat
  shipped `test_an_unreadable_pidfile_is_not_mistaken_for_a_live_waker` asserting that
  unreadable EQUALS absent — written from the same belief as the code it covered, so it
  confirmed the bug instead of catching it, and no sweep could have known. This file's own
  near-miss was smaller: a corrupt-lock fixture that also carried a stale boot token, where
  the boot check returned first, so the test passed for a reason unrelated to what it claimed.
  A fixture is falsifiable only by something that did not share the belief — a fixture that
  PREDATES the distinction (the past did not share it) or a second party (they were not
  there). Mutation never qualifies, because sharing the belief is not what it measures.
* It only tests the broken form written down here — silence. It cannot see a validator that
  wrongly ACCEPTS, a detector that fires on EVERYTHING, or a threshold that drifted. Those
  are different mutations and this file does not contain them.
* **A kill count is not coverage.** Ten assertions reading one line of output all flip
  together and count ten. A high number can mean one well-tested behaviour or ten; the
  number cannot tell you which, and neither can it be made to.
* **Nothing here reads shell.** `install.sh` and `prototype/*.sh` have no AST and no
  mutation harness, so they are outside this entirely — stated as a limit rather than left
  to be discovered, because a denominator that silently excludes a language is the same
  false green as one that silently excludes a file.
* **It only covers the producers in TARGETS**, chosen as the things that *gate or decide*:
  guards, validators, detectors, routers. Everything else is either excluded in `NOT_SWEPT`
  with a reason or fails the accounting. So "0 unprotected" is a statement about the swept
  set, not about showrunner.

  No counts are written in this docstring on purpose. One used to be, and it was wrong two
  commits after it was typed — a stale number inside the tool whose job is finding stale
  numbers. `--accounting` prints the real figures in a fraction of a second. A number that is
  computed and a number that is remembered should never be the same number.

So treat a count as a floor with a reason attached, never as a score. The honest use is the
ORDERING — strengthen in ascending kill order — because that is the one thing the numbers
genuinely rank.
"""

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Each target: the file, a regex anchored on the def line, and the stub body that makes the
# producer silently do nothing. The stub must PARSE — a syntax error would be caught by
# import machinery rather than by the assertions, which is a different question.
TARGETS = [
    ("lock guard", "locks.LockSet.guard", "lib/showrunner/locks.py",
     r"(    def guard\(self, command, session=None\):\n)",
     "        return True, ''\n    def _neutered_guard(self, command, session=None):\n"),
    ("unreadable-vs-dead pid", "util.pid_readable", "lib/showrunner/util.py",
     r"(def pid_readable\(pid\):\n)",
     "    return True\ndef _neutered_readable(pid):\n"),
    ("unignored (silent on allow)", "worktree.unignored", "lib/showrunner/worktree.py",
     r"(def unignored\(worktree, paths\):\n)",
     "    return IgnoreCheck([], [])\ndef _neutered_unignored(worktree, paths):\n"),
    ("config validate", "config.Config.validate", "lib/showrunner/config.py",
     r"(    def validate\(self\):\n)",
     "        return []\n    def _neutered_validate(self):\n"),
    ("stale_claims (reap's evidence)", "graph.SqliteGraph.stale_claims", "lib/showrunner/graph.py",
     r"(    def stale_claims\(self\):\n)",
     "        return []\n    def _neutered_stale(self):\n"),
    ("stop gate", "gates.stop_gate", "lib/showrunner/gates.py",
     r"(def stop_gate\(cfg, graph\):\n)",
     "    return True, 'stop OK'\ndef _neutered_stop_gate(cfg, graph):\n"),
    ("no-new-failures comparison", "gates.compare_to_baseline", "lib/showrunner/gates.py",
     r"(def compare_to_baseline\(cfg, current, baseline\):\n)",
     "    return True, []\ndef _neutered_compare(cfg, current, baseline):\n"),
    ("chat tool path from CONFIG", "dispatch.chat_path", "lib/showrunner/dispatch.py",
     r"(def chat_path\(cfg, key\):\n)",
     "    return None\ndef _neutered_chat_path(cfg, key):\n"),
    ("spin-down on close", "campaign.finish", "lib/showrunner/campaign.py",
     r"(def finish\(cfg, leaf_id, why=\"leaf closed\"\):\n)",
     "    return []\ndef _neutered_finish(cfg, leaf_id, why=\"x\"):\n"),
    ("lingering process detection", "dispatch.lingering", "lib/showrunner/dispatch.py",
     r"(def lingering\(entry, grace=LINGER_GRACE_SECONDS\):\n)",
     "    return None\ndef _neutered_lingering(entry, grace=LINGER_GRACE_SECONDS):\n"),
    ("session health beside liveness", "dispatch.session_health", "lib/showrunner/dispatch.py",
     r"(def session_health\(cfg, entry\):\n)",
     "    return None\ndef _neutered_health(cfg, entry):\n"),
    ("alias-vs-full model agreement", "dispatch.models_agree", "lib/showrunner/dispatch.py",
     r"(def models_agree\(declared, observed\):\n)",
     "    return False\ndef _neutered_agree(declared, observed):\n"),
    ("the Crawler's chat channel", "dispatch.channel_for", "lib/showrunner/dispatch.py",
     r"(def channel_for\(cfg, record\):\n)",
     "    return None\ndef _neutered_channel_for(cfg, record):\n"),
    ("the model game_loop OBSERVED", "dispatch.observed_models", "lib/showrunner/dispatch.py",
     r"(def observed_models\(cfg, entry\):\n)",
     "    return None\ndef _neutered_observed(cfg, entry):\n"),
    ("lane routing", "lanes.route", "lib/showrunner/lanes.py",
     r"(def route\(cfg, leaf\):\n)",
     "    return Decision({'leaf': leaf['id'], 'title': '', 'lane': 'headless',\n"
     "                     'resource': None, 'rule': None, 'why': '', 'matched': True,\n"
     "                     'ts': 0})\ndef _neutered_route(cfg, leaf):\n"),
    ("collision prediction", "collide.plan_waves", "lib/showrunner/collide.py",
     r"(def plan_waves\(cfg, leaves, files=None\):\n)",
     "    return [[l['id'] for l in leaves]], {l['id']: {'paths': set(), 'exclusive': set(),\n"
     "            'shared': set(), 'symbols': set(), 'basis': '', 'estimable': True}\n"
     "            for l in leaves}, []\ndef _neutered_plan(cfg, leaves, files=None):\n"),
    # ACCEPTED AS THIN, declared rather than inflated. The integrate-refusal test STUBS
    # check_tree deliberately, to isolate integrate's decision from the detector's — so the
    # sweep correctly gives it no credit here. The two assertions that do cover it exercise
    # the real function against a fixture harness. Padding this number by un-stubbing a test
    # that is right to stub would be gaming the metric, which is the same rounding-up the
    # sweep exists to catch.
    ("harness drift check", "harness.check_tree", "lib/showrunner/harness.py",
     r"(def check_tree\(cfg, worktree_path\):\n)",
     "    return 'clean', ''\ndef _neutered_check_tree(cfg, worktree_path):\n"),
    # Added by applying the RECENCY lens: the most recently argued producer is the one with
    # the least behind it, because the argument is fresh and feels like evidence.
    ("waiting (orchestrator liveness)", "campaign.waiting", "lib/showrunner/campaign.py",
     r'(def waiting\(cfg, graph, base="HEAD"\):\n)',
     "    return False, {'waiting': False, 'live_crawlers': [], 'parked_crawlers': [],\n"
     "                   'basis': ''}\ndef _neutered_waiting(cfg, graph, base='HEAD'):\n"),
    # Both added by the derived candidate scan rather than by me noticing them.
    ("claim --next (fleet work division)", "graph.SqliteGraph.claim_next", "lib/showrunner/graph.py",
     r"(    def claim_next\(self, actor, pid=None, tree=None, session=None, prefer=None\):\n)",
     "        return None\n    def _neutered_claim_next(self, actor, pid=None, tree=None,\n"
     "                                 session=None, prefer=None):\n"),
    ("campaign reconcile (resume)", "campaign.reconcile", "lib/showrunner/campaign.py",
     r'(def reconcile\(cfg, graph, base="HEAD"\):\n)',
     "    return []\ndef _neutered_reconcile(cfg, graph, base='HEAD'):\n"),
    ("shared-state audit", "worktree.audit_shared", "lib/showrunner/worktree.py",
     r"(def audit_shared\(cfg\):\n)",
     "    return []\ndef _neutered_audit(cfg):\n"),
]


# ---------------------------------------------------------------- candidates
# The list below is a DENYLIST unless something derives the set it should cover. A tool built
# to find unprotected producers, whose own scope is a hand-written list nobody audits, has the
# exact defect it exists to catch: a producer nobody added is uncovered AND does not appear as
# a gap. So the candidate set is derived from the source, and every candidate must be either
# swept or explicitly excluded with a reason.
#
# Two signatures, because one is not enough. game_loop proposed the first; applied here it
# missed `Config.validate` — the site of the worst defect this project has had — because that
# function accumulates into a local and returns the variable, so it has no literal empty
# return to spot. The accumulator pattern is how most real producers are written.
EMPTY_LITERALS = (ast.List, ast.Dict, ast.Set, ast.Tuple)


def _is_nothing(ret):
    v = ret.value
    if v is None:
        return True
    if isinstance(v, ast.Constant) and v.value in (None, False, ""):
        return True
    if isinstance(v, EMPTY_LITERALS) and not (getattr(v, "elts", None) or getattr(v, "keys", None)):
        return True
    return False


def _returns_empty_accumulator(fn):
    """`out = []` ... `return out` — a producer that reports nothing by returning an empty
    collection it built. Invisible to a scan for literal empty returns, and it is the shape
    `Config.validate` had when its only real check turned out to be unfailable."""
    empties = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)
               and isinstance(n.value, EMPTY_LITERALS)
               and not (getattr(n.value, "elts", None) or getattr(n.value, "keys", None))}
    return any(isinstance(r.value, ast.Name) and r.value.id in empties
               for r in ast.walk(fn) if isinstance(r, ast.Return) and r.value is not None)


def _qualified(module, tree):
    """(name, node) for every function, methods qualified by their class.

    Unqualified names collide: `graph.stale_claims` is a method on TWO backends, and the
    sweep's regex mutates only the first match — so the second read as accounted-for while
    nothing swept it. Seventeen names in this library collide that way.
    """
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            out.append(("%s.%s" % (module, node.name), node))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef):
                    out.append(("%s.%s.%s" % (module, node.name, sub.name), sub))
    return out


def _is_python_source(path):
    """Parses as Python AND declares something. game_loop's refinement, and it fixes a flaw in
    BOTH obvious tests: `ast.parse` alone accepts JSON and YAML (they are valid Python
    expressions), while an extension-or-shebang gate silently skips a Python file that has
    neither. Requiring a def, class or import keeps config out and cannot miss real source."""
    try:
        tree = ast.parse(open(path).read())
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return None
    if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                          ast.Import, ast.ImportFrom)) for n in tree.body):
        return tree
    return None


def python_sources():
    """Every Python file in the product, found by CONTENT rather than by extension.

    The scan used to read `lib/showrunner/*.py` and nothing else, so `bin/showrunner` — Python
    with no extension — was ABSENT rather than unaccounted-for, and absent is the state that
    produces no report. It happens to define no functions today, which is precisely the
    trap: the set was complete by accident and would have shrunk silently the first time
    somebody added one. Discovery is a predicate now, so a new file cannot fall outside it.
    """
    out = []
    for rel in PRODUCT_ROOTS:
        d = os.path.join(ROOT, rel)
        if not os.path.isdir(d):
            continue
        for dirpath, dirnames, files in os.walk(d):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for f in sorted(files):
                full = os.path.join(dirpath, f)
                if _is_python_source(full) is not None:
                    out.append((f[:-3] if f.endswith(".py") else f, full))
    return out


# Named roots, but CHECKED — see unscanned_python(). A hardcoded list is the denylist defect
# at the directory level, which is where it landed after being fixed at the target level, the
# signature level, the namespace level and the file level.
PRODUCT_ROOTS = ("lib", "bin")
NOT_PRODUCT = (
    "test/",        # the harness itself; mutating it would test the test
    "prototype/",   # shell, and superseded — see the module docstring's stated limits
    "docs/",
    # The VENDORED HARNESS. Another project's source, tracked here only so it crosses into
    # Crawler worktrees; it carries its own suite and its own mutation sweep upstream, and
    # .game_loop/** is excluded from this repo's owed checks for the same reason. Declared
    # rather than silently absorbed into PRODUCT_ROOTS, because "not mine to test" and "not
    # noticed" are the two states this whole exercise exists to keep apart.
    ".game_loop/",
    ".claude/",
)


def unscanned_python():
    """Python the repo tracks that the sweep never parses. Derived from git, not from guesses."""
    rc, out, _ = None, "", ""
    import subprocess
    proc = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    scanned = {os.path.realpath(p) for _, p in python_sources()}
    missed = []
    for rel in proc.stdout.splitlines():
        if not rel.strip() or rel.startswith(NOT_PRODUCT):
            continue
        full = os.path.join(ROOT, rel)
        if not os.path.isfile(full):
            continue
        if _is_python_source(full) is None:
            continue
        if os.path.realpath(full) not in scanned:
            missed.append(rel)
    return sorted(missed)


def candidates():
    """Every function that can answer 'nothing' as well as 'something'. Derived, not declared."""
    found = []
    for module, path in python_sources():
        tree = ast.parse(open(path).read())
        for qname, node in _qualified(module, tree):
            rets = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
            if not rets:
                continue
            nothings = [r for r in rets if _is_nothing(r)]
            somethings = [r for r in rets if not _is_nothing(r)]
            if (nothings and somethings) or _returns_empty_accumulator(node):
                found.append(qname)
    return found


# Every candidate must be SWEPT or excluded HERE with a reason. An unaccounted-for one fails
# the run, same as UNPROTECTED, because a producer nobody decided about is exactly the case
# this file exists for.
#
# The reasons have to be real. Writing "not a real producer" about one that is, to clear the
# list, is the gaming this whole family is about — so where the honest answer is "should be
# swept, is not yet", it says that and stays visible instead of being excused.
NOT_SWEPT = {
    # Exercised entirely through a swept caller; a wrong answer surfaces there, and sweeping
    # each costs a full suite run for a signal already carried.
    "campaign.commits_ahead": "read only by is_merged/is_empty, both reached via reconcile",
    "campaign.is_merged": "reached through reconcile, which the integration group asserts",
    "campaign.is_empty": "reached through reconcile; the abandoned-branch verdict asserts it",
    "campaign.live": "liveness of one record; the waiting and reap groups assert its effect",
    "graph.SqliteGraph.blockers": "the ready/dependency assertions fail directly if it answers wrongly. NOTE: this reason was written unqualified and silently also excused BrGraph.blockers, which returned [] unconditionally until it was made to refuse — an unfailable accept hidden by an ambiguous key",
    "graph.SqliteGraph.ready": "asserted by name in the graph group, not via a producer stub",
    "graph.SqliteGraph._would_cycle": "private; the cycle refusal asserts it",
    "graph.BrGraph.available": "static probe for the br binary; the OPTIONAL group skips loudly on it",
    "lanes._rule_matches": "private; route's own assertions cover both match and no-match",
    "locks.Lock._read": "private file read; lock state assertions cover it",
    "locks.Lock._live": "private; the STALE/HELD assertions cover both answers",
    "locks.Lock.holder": "reflects _read; covered by the same assertions",
    "locks.LockSet.matching": "reached by guard, which IS swept",
    "harness._is_runtime": "private; the runtime-exclusion assertions cover both answers",
    "harness._install": "private; the installer path is asserted end to end in the harness group",
    "gates._harness_bin": "private lookup for attribution's command string",
    "config.Config.abspath": "one-line join; every path property asserts its result",
    "config.Config.resource": "lookup; the lock group fails if it answers wrongly",
    "util.pid_alive": "the claim-liveness and lock-staleness assertions cover both answers",
    "util.repo_root": "every fixture would fail to build if it answered wrongly",
    "collide.tracked_files": "git listing; plan_waves is swept and consumes it",

    # Return an exit code rather than a finding. Their behaviour is asserted through the CLI
    # group by exit code, which is the contract callers actually depend on.
    "cli.main": "dispatch; asserted by every CLI-group exit code",
    "cli.cmd_claim": "CLI wrapper over graph.claim; exit code asserted",
    "cli.cmd_lock_acquire": "CLI wrapper; lock state asserted directly",
    "cli.cmd_lock_guard": "CLI wrapper; exit 2/0 asserted in the CLI group",
    "cli.cmd_integrate": "CLI wrapper over campaign.integrate, which is asserted directly",
    "cli.cmd_integration_commit": "CLI wrapper over declare_integration, asserted directly",

    # Honest gaps. Named rather than excused, because a false exclusion is the failure this
    # file is about and an admitted hole is worth more than a tidy list.
    "config.path_problem": "SHOULD BE SWEPT, IS NOT YET — it gates an accept and a silent "
                           "always-None would let every unexpanded variable through, which is "
                           "the exact defect that produced this file. Its reachability is "
                           "asserted by the reachable-rules group, so it is covered but not "
                           "by this tool.",
    "gates.load_baseline": "SHOULD BE SWEPT, IS NOT YET — returning None always would make "
                           "every comparison report 'no baseline', which the baseline group "
                           "asserts, but not through a stub.",
    "gates.attribution": "SHOULD BE SWEPT, IS NOT YET — returning None always would silently "
                         "drop the provenance command from integration output.",
    "harness.report": "SHOULD BE SWEPT, IS NOT YET — doctor's harness lines would vanish and "
                      "no assertion currently requires them.",
    "worktree.dirty": "SHOULD BE SWEPT, IS NOT YET — an always-empty answer would report every "
                      "abandoned worktree as clean, which is a real loss-of-work path.",
    "worktree.harness_gap": "SHOULD BE SWEPT, IS NOT YET — an always-None would remove the "
                            "doctor warning about an untracked harness.",
    "locks.Lock.acquire": "SHOULD BE SWEPT, IS NOT YET — always-False is loud (nothing acquires), "
                     "but always-True would hand two callers the same resource.",
    "locks.Lock.release": "SHOULD BE SWEPT, IS NOT YET — always-False would leave locks held.",
}


# DERIVED from TARGETS, never mirrored. A hand-kept parallel set drifts in the silent
# direction: delete a target, leave its key, and a candidate reads as covered while nothing
# sweeps it.
SWEPT_KEYS = {t[1] for t in TARGETS}


def all_functions():
    """Every module.function that exists, so a declaration naming a vanished one is caught."""
    names = set()
    for module, path in python_sources():
        names.update(q for q, _ in _qualified(module, ast.parse(open(path).read())))
    return names


def run_suite(cwd):
    proc = subprocess.run([sys.executable, os.path.join("test", "run.py")],
                          cwd=cwd, capture_output=True, text=True)
    m = re.search(r"RESULT: (\d+) passed, (\d+) failed", proc.stdout)
    if not m:
        return None, None, proc.stdout[-800:]
    return int(m.group(1)), int(m.group(2)), proc.stdout


def failing(output):
    """The SET OF ASSERTION NAMES that failed. Names, not a count.

    A count moves for reasons that have nothing to do with the mutation — one flaky
    failure in the mutated run reads as coverage the suite does not have. What proves an
    assertion noticed is that it FLIPPED: passing unmutated, failing mutated. So the kill
    set is a set difference over names, and the baseline is subtracted from every run.
    """
    return {l[8:].strip() for l in output.splitlines() if l.startswith("  FAIL  ")}


def accounting():
    """The denominator checks only — no mutation, no suite runs. Seconds, not minutes.

    Separated so it can gate a release. The full sweep cannot: it runs the whole suite once
    per target, and a gate that slow is a gate somebody routes around. These three checks are
    the ones that catch a producer arriving without a decision, and they need no suite at all.
    """
    problems = 0
    outside = unscanned_python()
    if outside:
        print("PYTHON THE SWEEP NEVER PARSES — extend PRODUCT_ROOTS or declare it not product:")
        for f in outside:
            print("  %s" % f)
        problems += 1
    stale = sorted((SWEPT_KEYS | set(NOT_SWEPT)) - all_functions())
    if stale:
        print("STALE DECLARATIONS — these name functions that no longer exist:")
        for k in stale:
            print("  %s" % k)
        problems += 1
    unaccounted = sorted(set(candidates()) - SWEPT_KEYS - set(NOT_SWEPT))
    if unaccounted:
        print("UNACCOUNTED CANDIDATES — sweep them, or exclude them in NOT_SWEPT with a reason:")
        for c in unaccounted:
            print("  %s" % c)
        problems += 1
    if not problems:
        print("accounting ok: %d files, %d candidates, %d swept, %d excluded, 0 unaccounted, "
              "0 stale, 0 unscanned"
              % (len(python_sources()), len(candidates()), len(SWEPT_KEYS), len(NOT_SWEPT)))
    return 1 if problems else 0


def main():
    if "--accounting" in sys.argv:
        return accounting()
    only = None
    if "--target" in sys.argv:
        only = sys.argv[sys.argv.index("--target") + 1]

    base_dir = tempfile.mkdtemp(prefix="mutate-base-")
    shutil.copytree(ROOT, os.path.join(base_dir, "s"), symlinks=True,
                    ignore=shutil.ignore_patterns(".worktrees", "*.db", "scratch"))
    b_pass, b_fail, b_out = run_suite(os.path.join(base_dir, "s"))
    shutil.rmtree(base_dir, ignore_errors=True)
    if b_pass is None:
        print("baseline suite did not report a RESULT line; fix that first")
        return 2
    baseline_failures = failing(b_out)
    # Default-deny: a candidate nobody decided about fails the run. Without this the target
    # list is a denylist that nobody audits, which is this tool having the very defect it
    # exists to find — a producer nobody added is uncovered AND invisible as a gap.
    cands = set(candidates())
    # A stale declaration is the same defect pointing the other way: an exclusion whose
    # function is gone silently shrinks nothing today and silently covers a FUTURE function
    # that happens to reuse the name.
    outside = unscanned_python()
    if outside:
        print("PYTHON THE SWEEP NEVER PARSES — extend PRODUCT_ROOTS or declare it not product:")
        for f in outside:
            print("  %s" % f)
        return 1
    exists = all_functions()
    stale = sorted((SWEPT_KEYS | set(NOT_SWEPT)) - exists)
    if stale:
        print("STALE DECLARATIONS — these name functions that no longer exist:")
        for k in stale:
            print("  %s" % k)
        return 1
    unaccounted = sorted(cands - SWEPT_KEYS - set(NOT_SWEPT))
    print("candidates derived from source: %d  ·  swept: %d  ·  excluded with a reason: %d"
          % (len(cands), len(SWEPT_KEYS & cands), len(set(NOT_SWEPT) & cands)))
    if unaccounted:
        print("\nUNACCOUNTED CANDIDATES — sweep them, or exclude them in NOT_SWEPT with a "
              "reason:")
        for c in unaccounted:
            print("  %s" % c)
        return 1
    print("baseline: %d passed, %d failed\n" % (b_pass, b_fail))
    print("%-34s %8s   %s" % ("producer neutered", "killed", "verdict"))
    print("-" * 78)

    weak = []
    # A --target that matches nothing swept NOTHING and then printed "every producer above is
    # noticed", which is a clean bill of health for a run that did no work. Found by typing a
    # KEY where the filter only ever read the label. Both are matched now, and matching zero
    # is an error rather than a quiet success — the whole point of this tool is that a check
    # which cannot fail is worse than no check.
    if only:
        matched = [t for t in TARGETS
                   if only.lower() in t[0].lower() or only.lower() in t[1].lower()]
        if not matched:
            print("--target %r matched no producer. Names and keys available:" % only)
            for name, key, _r, _p, _s in TARGETS:
                print("  %-34s %s" % (name, key))
            return 1
    for name, _key, relpath, pattern, stub in TARGETS:
        if only and not (only.lower() in name.lower() or only.lower() in _key.lower()):
            continue
        work = tempfile.mkdtemp(prefix="mutate-")
        tree = os.path.join(work, "s")
        shutil.copytree(ROOT, tree, symlinks=True,
                        ignore=shutil.ignore_patterns(".worktrees", "*.db", "scratch"))
        target = os.path.join(tree, relpath)
        with open(target) as fh:
            src = fh.read()
        new, n = re.subn(pattern, lambda m: m.group(1) + stub, src, count=1)
        if n != 1:
            # A named producer that no longer exists is UNPROTECTED, not a skip. A sweep that
            # shrugs at a rename is a check that cannot fail — which is precisely the defect
            # the sweep exists to find, arriving inside the sweep itself.
            print("%-34s %8s   UNPROTECTED — producer not found (renamed? removed?)"
                  % (name, "-"))
            weak.append((name, 0))
            shutil.rmtree(work, ignore_errors=True)
            continue
        with open(target, "w") as fh:
            fh.write(new)
        p, f, out = run_suite(tree)
        shutil.rmtree(work, ignore_errors=True)
        if p is None:
            print("%-34s %8s   SUITE CRASHED (stub may not parse)" % (name, "-"))
            weak.append((name, 0))
            continue
        # Only assertions that FLIPPED count. Anything already failing unmutated is noise.
        killed = failing(out) - baseline_failures
        f = len(killed)
        # 3+ is comfortable; 1-2 is thin and worth naming rather than rounding up to "ok";
        # 0 is the real defect. Reporting thin as ok would be the same rounding-up this whole
        # exercise exists to refuse.
        if f == 0:
            verdict = "UNPROTECTED — nothing notices"
        elif f <= 2:
            verdict = "THIN — only %d assertion(s) notice" % f
        else:
            verdict = "ok"
        if f <= 2:
            weak.append((name, f))
        print("%-34s %8d   %s" % (name, f, verdict))

    print()
    unprotected = [w for w in weak if w[1] == 0]
    if weak:
        print("Thinly covered producers (2 or fewer assertions notice):")
        for name, f in weak:
            print("  %-32s %d%s" % (name, f, "   <-- UNPROTECTED" if f == 0 else ""))
        print("\nThe remedy is chosen by what the check has on its happy path:")
        print("  speaks when it permits  -> assert the reason")
        print("  silent when it permits  -> make it carry a mark, assert the mark advanced")
        print("  a pure observation      -> pair it with a positive control in the same call")
        print("Add a companion assertion, never a replacement: the original is not false,")
        print("only unsupported, and rewriting it loses the restraint claim it encodes.")
        # Thin is information; only UNPROTECTED is a failure. A sweep that nags on thin
        # coverage is one that gets run with its output ignored.
        return 1 if unprotected else 0
    print("Every producer above is noticed by at least two assertions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
