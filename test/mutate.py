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
rather than as a one-time result. The counts below are the baseline to beat.

    python3 test/mutate.py [--target NAME]

Not part of `test/run.py`: it runs the whole suite once per target and is far too slow to
owe on every change. Run it when adding a producer, or when a restraint behaviour changes.
"""

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
    ("lock guard", "lib/showrunner/locks.py",
     r"(    def guard\(self, command, session=None\):\n)",
     "        return True, ''\n    def _neutered_guard(self, command, session=None):\n"),
    ("unignored (silent on allow)", "lib/showrunner/worktree.py",
     r"(def unignored\(worktree, paths\):\n)",
     "    return IgnoreCheck([], [])\ndef _neutered_unignored(worktree, paths):\n"),
    ("config validate", "lib/showrunner/config.py",
     r"(    def validate\(self\):\n)",
     "        return []\n    def _neutered_validate(self):\n"),
    ("stale_claims (reap's evidence)", "lib/showrunner/graph.py",
     r"(    def stale_claims\(self\):\n)",
     "        return []\n    def _neutered_stale(self):\n"),
    ("stop gate", "lib/showrunner/gates.py",
     r"(def stop_gate\(cfg, graph\):\n)",
     "    return True, 'stop OK'\ndef _neutered_stop_gate(cfg, graph):\n"),
    ("no-new-failures comparison", "lib/showrunner/gates.py",
     r"(def compare_to_baseline\(cfg, current, baseline\):\n)",
     "    return True, []\ndef _neutered_compare(cfg, current, baseline):\n"),
    ("lane routing", "lib/showrunner/lanes.py",
     r"(def route\(cfg, leaf\):\n)",
     "    return Decision({'leaf': leaf['id'], 'title': '', 'lane': 'headless',\n"
     "                     'resource': None, 'rule': None, 'why': '', 'matched': True,\n"
     "                     'ts': 0})\ndef _neutered_route(cfg, leaf):\n"),
    ("collision prediction", "lib/showrunner/collide.py",
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
    ("harness drift check", "lib/showrunner/harness.py",
     r"(def check_tree\(cfg, worktree_path\):\n)",
     "    return 'clean', ''\ndef _neutered_check_tree(cfg, worktree_path):\n"),
    # Added by applying the RECENCY lens: the most recently argued producer is the one with
    # the least behind it, because the argument is fresh and feels like evidence.
    ("waiting (orchestrator liveness)", "lib/showrunner/campaign.py",
     r'(def waiting\(cfg, graph, base="HEAD"\):\n)',
     "    return False, {'waiting': False, 'live_crawlers': [], 'parked_crawlers': [],\n"
     "                   'basis': ''}\ndef _neutered_waiting(cfg, graph, base='HEAD'):\n"),
    ("shared-state audit", "lib/showrunner/worktree.py",
     r"(def audit_shared\(cfg\):\n)",
     "    return []\ndef _neutered_audit(cfg):\n"),
]


def run_suite(cwd):
    proc = subprocess.run([sys.executable, os.path.join("test", "run.py")],
                          cwd=cwd, capture_output=True, text=True)
    m = re.search(r"RESULT: (\d+) passed, (\d+) failed", proc.stdout)
    if not m:
        return None, None, proc.stdout[-800:]
    return int(m.group(1)), int(m.group(2)), proc.stdout


def survivors(output):
    """Assertions that still PASS while the producer does nothing."""
    return [l.strip()[6:] for l in output.splitlines() if l.startswith("  PASS  ")]


def main():
    only = None
    if "--target" in sys.argv:
        only = sys.argv[sys.argv.index("--target") + 1]

    base_dir = tempfile.mkdtemp(prefix="mutate-base-")
    shutil.copytree(ROOT, os.path.join(base_dir, "s"), symlinks=True,
                    ignore=shutil.ignore_patterns(".worktrees", "*.db", "scratch"))
    b_pass, b_fail, _ = run_suite(os.path.join(base_dir, "s"))
    shutil.rmtree(base_dir, ignore_errors=True)
    if b_pass is None:
        print("baseline suite did not report a RESULT line; fix that first")
        return 2
    print("baseline: %d passed, %d failed\n" % (b_pass, b_fail))
    print("%-34s %8s %8s   %s" % ("producer neutered", "passed", "failed", "verdict"))
    print("-" * 78)

    weak = []
    for name, relpath, pattern, stub in TARGETS:
        if only and only.lower() not in name.lower():
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
            print("%-34s %8s %8s   PATTERN DID NOT MATCH — fix this entry" % (name, "-", "-"))
            shutil.rmtree(work, ignore_errors=True)
            continue
        with open(target, "w") as fh:
            fh.write(new)
        p, f, out = run_suite(tree)
        shutil.rmtree(work, ignore_errors=True)
        if p is None:
            print("%-34s %8s %8s   SUITE CRASHED (stub may not parse)" % (name, "-", "-"))
            continue
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
        print("%-34s %8d %8d   %s" % (name, p, f, verdict))

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
