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
    # Answering None everywhere restores the exact defect: doctor goes back to reporting only
    # PROVENANCE, which fires identically whether a copy is current or twenty commits behind. A
    # producer whose death returns the tool to its previous, working-looking state dies
    # invisibly, and this one cost a consumer an evening rediscovering a fix that already
    # existed.
    ("is this copy behind its source", "pin.staleness", "lib/showrunner/pin.py",
     r"(def staleness\(source_repo=None\):\n)",
     "    return None\ndef _neutered_staleness(source_repo=None):\n"),
    # Answering [] everywhere is the SAFE-looking direction — every prose option simply loses its
    # file twin and the CLI still works — which is exactly why it needs a mutant: the failure is
    # that a caller with backticked prose has nowhere safe to put it, and nothing about the run
    # says so. A producer whose death restores the previous, working-looking state dies invisibly.
    ("prose file twins", "cli._add_prose_twins", "lib/showrunner/cli.py",
     r"(def _add_prose_twins\(parser\):\n)",
     "    return []\ndef _neutered_add_prose_twins(parser):\n"),
    # #58. Answering [] everywhere is the SAFE direction — it restores the old behaviour of not
    # reading the command string at all — which is exactly why it needs a mutant: a producer
    # whose death returns the system to its previous, working state dies invisibly.
    ("paths a shell command names", "lease.command_paths", "lib/showrunner/lease.py",
     r"(def command_paths\(command\):\n)",
     "    return []\ndef _neutered_command_paths(command):\n"),
    # It gates an ACCEPT: None means "this path is fine", so a neutered version lets every
    # unexpanded variable through silently. `$HOME/x` survives an isabs() check because abspath
    # makes anything absolute, and for a lock root that is a different directory per caller — a
    # mutex that is quietly a no-op.
    ("a path that will not mean what it says", "config.path_problem", "lib/showrunner/config.py",
     r"(def path_problem\(label, raw\):\n)",
     "    return None\ndef _neutered_path_problem(label, raw):\n"),
    # An always-None removes doctor's only warning about the most invisible spawn failure there
    # is: a gitignored harness never crosses into a worktree, so the Crawler is denied its first
    # commit and nothing said so at spawn time.
    ("the untracked-harness warning", "worktree.harness_gap", "lib/showrunner/worktree.py",
     r"(def harness_gap\(cfg, worktree_path=None\):\n)",
     "    return None\ndef _neutered_harness_gap(cfg, worktree_path=None):\n"),
    # MEASURED ZERO — the only genuinely unprotected producer the OWED queue held. Its note said
    # an always-None "would silently drop the provenance command from integration", and nothing
    # noticed: `integration-commit` still exits 0, still reports every staged file accounted
    # for, and the block simply stops printing. Six assertions now; the mutant kills 4.
    ("the provenance declaration", "gates.attribution", "lib/showrunner/gates.py",
     r"(def attribution\(cfg, entries, harness_bin=None\):\n)",
     "    return None\ndef _neutered_attribution(cfg, entries, harness_bin=None):\n"),
    # Note claimed no coverage; measured 6. Stale, like locks.Lock.acquire's — registering it
    # was the whole debt, and only running the mutant distinguished it from the one that was
    # genuinely uncovered while both sat in the same list worded the same way.
    ("the baseline a comparison rests on", "gates.load_baseline", "lib/showrunner/gates.py",
     r"(def load_baseline\(cfg\):\n)",
     "    return None\ndef _neutered_load_baseline(cfg):\n"),
    # Note claimed no coverage; measured 26. Also stale.
    ("releasing a held lock", "locks.Lock.release", "lib/showrunner/locks.py",
     r"(    def release\(self, pid=None, force=False\):\n)",
     "        return False\n    def _neutered_release(self, pid=None, force=False):\n"),
    # Its note said an always-empty answer reports every abandoned worktree as clean — a real
    # loss-of-work path. Measured before acting: the note was ACCURATE, one assertion noticed.
    # Now five, on content and on CHANGE (it empties once work is committed), which is the
    # assertion a producer stuck replaying its last answer cannot pass.
    ("uncommitted work in a tree", "worktree.dirty", "lib/showrunner/worktree.py",
     r"(def dirty\(path, tracked_only=False\):\n)",
     "    return []\ndef _neutered_dirty(path, tracked_only=False):\n"),
    # NEUTERED TO TRUE, NOT FALSE, and the note is why: "always-False is loud (nothing
    # acquires), but always-True would hand two callers the same resource." A mutant must take
    # the SILENT direction or it measures how loudly the code fails rather than whether anything
    # was watching. Its note claimed no coverage; measured, 35 assertions notice. The note was
    # years behind its own subject and sat in the same list as one that was accurate.
    ("mutual exclusion itself", "locks.Lock.acquire", "lib/showrunner/locks.py",
     r"(    def acquire\(self, pid, who, session=None, wait=0, poll=1\.0, extra=None\):\n)",
     "        return True\n    def _neutered_acquire(self, pid, who, session=None, wait=0, "
     "poll=1.0, extra=None):\n"),
    # FIRST DEBT PAID FROM THE OWED QUEUE. Its exclusion read "SHOULD BE SWEPT, IS NOT YET --
    # doctor's harness lines would vanish and no assertion currently requires them", which is a
    # work item, not a decision. Three assertions now require those lines, so it can be swept
    # rather than excused.
    ("doctor's account of the harness", "harness.report", "lib/showrunner/harness.py",
     r"(def report\(cfg\):\n)",
     "    return []\ndef _neutered_report(cfg):\n"),
    # The placeholder-check detector (consumer report). Answering "nothing is unconfigured"
    # everywhere restores the exact bug: `baseline` accepts a command that cannot fail and
    # records it as clean, and every later no-new-failures verdict is measured against it. A
    # detector whose neutered form is SILENT needs a mutant, because silence is also its
    # ordinary output on a correctly configured repo.
    ("placeholder-check detector", "gates.unconfigured_checks", "lib/showrunner/gates.py",
     r"(def unconfigured_checks\(cfg\):\n)",
     "    return []\ndef _neutered_unconfigured_checks(cfg):\n"),
    # The corroborating signal for a blocked report (#54). Answering "no evidence" everywhere is
    # the SAFE direction -- it restores the old always-refuse gate -- which is exactly why it
    # needs a mutant: a producer that fails safe is one whose death is invisible, and the four
    # restraint assertions around it agree with a neutered version. Only the positive case can
    # notice, and this is what proves that is still true.
    ("work-since-block evidence", "campaign.work_since_block", "lib/showrunner/campaign.py",
     r"(def work_since_block\(cfg, crawler, branch, worktree\):\n)",
     "    return False, \"\"\ndef _neutered_work_since_block(cfg, crawler, branch, worktree):\n"),
    # Added when the release gate's accounting refused: five producers arrived from another
    # session unswept. These two are registered rather than excused, because both are POLICY.
    #
    # Whether this worktree is one `spawn` PLACED or one somebody added by hand. None everywhere
    # makes every tree look hand-added -- but this same function is what stops `git worktree add`
    # being a way to grant yourself a role, and a mutant proves the assertions tell those apart.
    ("crawler leaf resolution", "roles.crawler_leaf", "lib/showrunner/roles.py",
     r"(def crawler_leaf\(cfg\):\n)",
     "    return None\ndef _neutered_crawler_leaf(cfg):\n"),
    # The identity two worktrees of one repo must AGREE on. None everywhere means no two trees
    # ever share a git dir, so lease and lock identity silently stop matching -- every check still
    # runs, still returns, and never fires. Same shape as the jurisdiction mutant below.
    ("shared git dir identity", "util.git_common_dir", "lib/showrunner/util.py",
     r"(def git_common_dir\(path\):\n)",
     "    return None\ndef _neutered_git_common_dir(path):\n"),
    # The lease's JURISDICTION. Answering None everywhere means no path is ever inside a
    # managed worktree, so every lease check silently becomes a no-op — the guard is still
    # called, still returns, and never fires. That is the exact shape this file exists to
    # catch, and it is indistinguishable from "no hijack happened" without a mutant.
    ("worktree-lease jurisdiction", "lease.tree_for", "lib/showrunner/lease.py",
     r"(def tree_for\(cfg, path=None\):\n)",
     "    return None\ndef _neutered_tree_for(cfg, path=None):\n"),
    # Reporting, but the reporting IS the product for a read-only verb: an empty list reads
    # as "no tree is held" to a human deciding whether to take one.
    ("worktree-lease status", "lease.status", "lib/showrunner/lease.py",
     r"(def status\(cfg, tree=None\):\n)",
     "    return []\ndef _neutered_status(cfg, tree=None):\n"),
    # The hijack DETECTOR. 'not-a-worktree' is its silent, reassuring answer — every session
    # reads as being nowhere in particular, no lease is ever taken, no hijack is ever seen, and
    # the journal WL-05's gate is owed stays empty. A detector that never fires and a world
    # with nothing to detect are the same observation from outside.
    # Answering None always means fork can never find a recorded base — which lands in the
    # REFUSAL, not in a wrong tree, so this one degrades safely. Swept anyway rather than
    # excused, because "it fails safe" is a claim about today's caller: the moment anything
    # falls back to HEAD instead of refusing, a silent None becomes a fork off the wrong
    # commit, and nothing would be watching.
    ("fork's recorded base", "lease.base_sha_of", "lib/showrunner/lease.py",
     r"(def base_sha_of\(cfg, tree\):\n)",
     "    return None\ndef _neutered_base_sha_of(cfg, tree):\n"),
    ("worktree enter (hijack detection)", "lease.enter", "lib/showrunner/lease.py",
     r"(def enter\(cfg, session, path=None, who=None\):\n)",
     "    return 'not-a-worktree', {}\n"
     "def _neutered_enter(cfg, session, path=None, who=None):\n"),
    # THE TEETH. An always-allow guard is the exact failure this file exists for: every
    # assertion about a lease being HELD still passes, `enter` still detects and prints, the
    # journal still fills with hijack events — and nothing is ever refused. "Allowed" and
    # "never looked" are the same observation from outside, which is why the plan named this
    # mutant specifically before the guard was written.
    ("worktree guard (the teeth)", "lease.guard", "lib/showrunner/lease.py",
     r"(def guard\(cfg, session, tool=None, tool_input=None, cwd=None, sr=None\):\n)",
     "    return True, '', {}\n"
     "def _neutered_guard(cfg, session, tool=None, tool_input=None, cwd=None, sr=None):\n"),
    # The carve-out, in the direction that switches the guard OFF rather than the one that
    # blocks a remedy. Always-True means every Bash command reads as showrunner's own verb.
    ("guard carve-out", "lease.own_command", "lib/showrunner/lease.py",
     r"(def own_command\(command\):\n)",
     "    return True\ndef _neutered_own_command(command):\n"),
    # Is the guard WIRED? An always-empty answer removes every one of doctor's three checks
    # AND the line `worktree enter` prints when the guard is inert — so a repo with no
    # registration at all would report clean, which is precisely the state this leaf found the
    # repo in and the state nothing could see.
    ("guard wiring checks", "lease.guard_health", "lib/showrunner/lease.py",
     r"(def guard_health\(cfg\):\n)",
     "    return []\ndef _neutered_guard_health(cfg):\n"),
    # An always-no-op registration is the SILENT half of shipping one: `init` prints nothing,
    # exits 0, and the guard it just placed is never wired to anything. That is the state this
    # leaf found `lock guard` in after the repo's entire life, reproduced by a stub.
    # ANCHORED ON THE NAME, NOT THE FULL SIGNATURE. This matched `def register_guard(cfg):`
    # exactly, so adding a `local=False` parameter made it match nothing — and the sweep
    # correctly reported UNSCOREABLE rather than pretending a measurement had happened.
    ("guard registration", "lease.register_guard", "lib/showrunner/lease.py",
     r"(def register_guard\([^)]*\):\n)",
     "    return False, ''\ndef _neutered_register_guard(*a, **k):\n"),
    # Every lock directory that EXISTS, which is not the same set as the configured resources —
    # a worktree lease is named `worktree:<tree>` and never appears in config. An always-empty
    # answer makes `reap` blind to abandoned leases again (the state it exists to surface) and
    # makes `lock release <lease>` refuse the one name a human needs to clear.
    # NOT auto-derived either — it always returns a dict, so it can never answer "nothing" in
    # the shape the derivation looks for. Swept anyway, for the same reason `looks_pinned` is:
    # its WRONG answer is the reassuring one. An always-empty `missing` restores exactly the
    # silence #33 was filed about, where a chained leaf starts without its dependency and ships
    # half the item with every gate green.
    ("the base a spawn will actually use", "worktree.base_report", "lib/showrunner/worktree.py",
     r'(def base_report\(cfg, graph, leaf, base="HEAD"\):\n)',
     "    return {'base': base, 'sha': None, 'branch': base, 'explicit': False,\n"
     "            'missing': [], 'present': [], 'unknown': []}\n"
     "def _neutered_base_report(cfg, graph, leaf, base='HEAD'):\n"),
    # An always-empty answer is the SILENT half of not provisioning: the spawn succeeds, the
    # record says nothing, and the guard is absent in the one place it runs — which is the exact
    # state #31 was filed about, restored without a word.
    ("showrunner's own hooks, crossing", "lease.provision_hooks", "lib/showrunner/lease.py",
     r"(def provision_hooks\(cfg, worktree_path\):\n)",
     "    return []\ndef _neutered_provision_hooks(cfg, worktree_path):\n"),
    # The reader that turns an accumulating log into a signal. An always-(None, None) answer is
    # the SILENT half of not having it: doctor prints "nothing recorded yet", which is exactly
    # what a repo with no routing gap looks like, and the missing lane rules stay invisible.
    ("routing gaps, read back", "lanes.unmatched", "lib/showrunner/lanes.py",
     r"(def unmatched\(cfg, tail=200\):\n)",
     "    return None, None\ndef _neutered_unmatched(cfg, tail=200):\n"),
    # THE ENFORCED BLOCK (#36/#40). An empty answer means a session is greeted, told its seat,
    # and told NOTHING about what it may not do — while the guards go on refusing. Announcement
    # and enforcement disagreeing is the failure generating one from the other exists to prevent,
    # and an empty generation is the quietest form of that disagreement.
    ("what the seat may not do", "roles.enforced_lines", "lib/showrunner/roles.py",
     r"(def enforced_lines\(role_def\):\n)",
     "    return []\ndef _neutered_enforced_lines(role_def):\n"),
    # THE CHEAP DISPATCH PATH. An always-allow guard is the state this issue reports: 42 raw
    # `claude -p` calls in one run, every showrunner guarantee absent for all 42, and nothing
    # said a word. "Allowed" and "never looked" are the same observation from outside.
    ("the dispatch guard", "dispatch.dispatch_guard", "lib/showrunner/dispatch.py",
     r"(def dispatch_guard\(cfg, session=None, tool=None, tool_input=None\):\n)",
     "    return True, '', {}\n"
     "def _neutered_dispatch_guard(cfg, session=None, tool=None, tool_input=None):\n"),
    # THE VALIDITY PRECONDITION (#41). An always-empty answer is the exact silence it exists to
    # break: a run that could not reach the world reports its failure count as though the count
    # meant something, and a confident verdict lands about code that was never exercised.
    ("could this run measure anything", "gates._void_hits", "lib/showrunner/gates.py",
     r"(def _void_hits\(cfg, blob\):\n)",
     "    return []\ndef _neutered_void_hits(cfg, blob):\n"),
    # An always-None answer is the silent half of not having it: doctor says nothing, and every
    # Crawler keeps being provisioned from whatever is uncommitted in somebody's clone.
    ("where the harness came from", "harness.installer_provenance", "lib/showrunner/harness.py",
     r"(def installer_provenance\(cfg, sp=None\):\n)",
     "    return None\ndef _neutered_installer_provenance(cfg, sp=None):\n"),
    # THE ROLE VALIDATOR. An always-empty answer is the silent half of having no policy: doctor
    # prints nothing, every config reads as valid, and an org whose escalation cycles or whose
    # fallback may dispatch passes review. A validator that never objects and a configuration
    # with nothing wrong are the same output.
    ("role shape validation", "roles.validate", "lib/showrunner/roles.py",
     r"(def validate\(roles\):\n)",
     "    return []\ndef _neutered_validate(roles):\n"),
    # An empty roster reads as "no seat is held", which is exactly what a session checks before
    # claiming one — so it is the answer that lets two sessions hold the same role.
    ("the role roster", "roles.roster", "lib/showrunner/roles.py",
     r"(def roster\(cfg\):\n)",
     "    return []\ndef _neutered_roster(cfg):\n"),
    ("locks present on disk", "locks.LockSet.on_disk", "lib/showrunner/locks.py",
     r"(    def on_disk\(self\):\n)",
     "        return []\n    def _neutered_on_disk(self):\n"),
    # NOT auto-derived as a candidate — it has one return and no "nothing" branch — and swept
    # anyway, because it is the most dangerous predicate in this repo: it decides whether `pin`
    # may DELETE its destination wholesale. Always-True turns a mistyped --dest into rm -rf on
    # somebody's directory. The derivation finds producers that can answer 'nothing'; it cannot
    # find one whose wrong answer is 'yes'.
    ("is this destination ours to delete", "pin.looks_pinned", "lib/showrunner/pin.py",
     r"(def looks_pinned\(dest\):\n)",
     "    return True\ndef _neutered_looks_pinned(dest):\n"),
    # The READ side of the stamp. Always-None means every consumer reports "no pin here" about
    # a directory that is correctly pinned — which reads as 'not configured', the reassuring
    # answer, and is the exact write-side-with-no-read-side defect this module was written to
    # avoid repeating.
    ("the pin's stamp, read back", "pin.read_pin", "lib/showrunner/pin.py",
     r"(def read_pin\(dest\):\n)",
     "    return None\ndef _neutered_read_pin(dest):\n"),
    # NOT auto-derived — both its returns are dicts, so it never answers "nothing" — and swept
    # in the REASSURING direction, which is the one that matters here. A crawler report saying
    # "something will re-check this" when nothing will is how a reader decides it is safe to
    # walk away from a fan-out. The loud direction (always 'NONE SCHEDULED') would be noticed
    # by the first person who read it; this one never would.
    ("is anything re-checking this campaign", "harness.follow_up", "lib/showrunner/harness.py",
     r"(def follow_up\(cfg\):\n)",
     "    return {'harness': None, 'last': None, 'waiting': None, 'scheduled': True, 'why': ''}\n"
     "def _neutered_follow_up(cfg):\n"),
    # NOT auto-derived (it always returns a string), and swept because the mutant is the exact
    # state this repo was in until today: `--version` printing a bare literal that has never
    # been bumped. That answered every "which build is this?" with the same six characters, and
    # proving seven consumers were stale took a file-by-file diff because of it. A regression
    # here would be invisible — the output still looks like a version string.
    ("what code is running", "pin.describe", "lib/showrunner/pin.py",
     r"(def describe\(\):\n)",
     "    return 'showrunner 0.1.0'\ndef _neutered_describe():\n"),
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
    # TWO BACKENDS, TWO TARGETS. One anchor matched BOTH and `count=1` neutered only the
    # first, so this producer scored as covered on the strength of mutating SqliteGraph while
    # BrGraph was never touched. The sweep now refuses an ambiguous anchor outright, which is
    # what surfaced this; the repair is to anchor each implementation separately so both are
    # actually measured rather than one standing in for the pair.
    ("stale_claims (reap's evidence, SqliteGraph)", "graph.SqliteGraph.stale_claims",
     "lib/showrunner/graph.py",
     r"(class SqliteGraph:(?:.|\n)*?    def stale_claims\(self\):\n)",
     "        return []\n    def _neutered_stale_sqlite(self):\n"),
    ("stale_claims (reap's evidence, BrGraph)", "graph.BrGraph.stale_claims",
     "lib/showrunner/graph.py",
     r"(class BrGraph:(?:.|\n)*?    def stale_claims\(self\):\n)",
     "        return []\n    def _neutered_stale_br(self):\n"),
    ("stop gate", "gates.stop_gate", "lib/showrunner/gates.py",
     r"(def stop_gate\(cfg, graph, leaf_id=None, tree=None\):\n)",
     "    return True, 'stop OK'\n"
     "def _neutered_stop_gate(cfg, graph, leaf_id=None, tree=None):\n"),
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
    # The mutant returns the CURRENT arity on purpose. When check_tree grew a third element the
    # old two-tuple mutant started raising instead of lying, and a mutant that crashes is caught
    # by every test that merely touches the call — so the sweep would still have scored this
    # green while no longer testing the thing it names. A neutered producer has to be plausible
    # to be evidence: it must report the reassuring answer, not fail to report at all.
    ("harness drift check", "harness.check_tree", "lib/showrunner/harness.py",
     r"(def check_tree\(cfg, worktree_path\):\n)",
     "    return 'clean', '', False\ndef _neutered_check_tree(cfg, worktree_path):\n"),
    # All four below were invisible until the candidate scan learned that `return None, ""` is
    # the same report as `return None`. Two are new; two have been in this file since the
    # harness contract was written, uncovered AND unlisted, which is the exact defect this tool
    # exists to find, in the tool.
    # `return False` is how emit reports a journal it could not write. Neutered, every event
    # silently vanishes and a viewer shows a busy campaign as idle — the exact failure the
    # module exists to make loud, so it owes a sweep rather than an exclusion.
    # `(None, None)` is "no cursor given, no error" — so neutered, EVERY cursor reads as absent
    # and a cross-instance one resumes from the start of the wrong campaign in silence. That is
    # the whole failure the cursor exists to prevent, which is why it owes a sweep.
    ("cursor scoping (whose seq is this?)", "events.parse_cursor", "lib/showrunner/events.py",
     r"(def parse_cursor\(cfg, raw\):\n)",
     "    return None, None\ndef _neutered_parse_cursor(cfg, raw):\n"),
    # `return None` is "this crawler has no previous transition". Neutered, EVERY poll looks
    # like a first sighting, so `crawler.blocked` is re-emitted on every reconcile and a viewer
    # drowns in identical lines — the edge detection silently becoming state reporting.
    ("transition edge detection", "events.latest", "lib/showrunner/events.py",
     r"(def latest\(cfg, kinds, field, value, tail_bytes=64 \* 1024\):\n)",
     "    return None\ndef _neutered_latest(cfg, kinds, field, value, tail_bytes=0):\n"),
    ("the event journal", "events.emit", "lib/showrunner/events.py",
     r"(def emit\(cfg, kind, fields=None\):\n)",
     "    return False\ndef _neutered_emit(cfg, kind, fields=None):\n"),
    ("blocked-vs-working (a refused turn-end)", "harness.stop_gate", "lib/showrunner/harness.py",
     r"(def stop_gate\(cfg, worktree_path, session\):\n)",
     "    return None, ''\ndef _neutered_stop_gate(cfg, worktree_path, session):\n"),
    # An empty list is "no inject path conflicts with the harness" — the reassuring answer, and
    # the one a broken detector also gives. Neutered, doctor goes quiet and every spawn goes back
    # to aborting with a message blaming the harness for a config conflict (#22).
    # THE rule, shared by both callers — `worktree.inject` refuses an entry as it materialises
    # and `doctor` reports it from the config. Written twice it would be two rules that agree
    # today, and the quieter one wins when they drift. Sweeping the predicate covers both;
    # `inject_conflicts` above it is now a loop and is excluded as reporting.
    ("inject-vs-harness rule", "harness.owns_path", "lib/showrunner/harness.py",
     r"(def owns_path\(cfg, path\):\n)",
     "    return None\ndef _neutered_owns_path(cfg, path):\n"),
    # An empty list is "nothing is stacking" — the reassuring answer, and the one a broken
    # detector also gives. Neutered, status and snapshot both go quiet and the condition returns
    # to being invisible by construction, which is the whole of #29.
    # It returns a populated dict, so the candidate scan (which looks for producers that report
    # NOTHING) never sees it — and an empty `overlaps` list is exactly the reassuring answer.
    # Swept explicitly rather than left outside the denominator on a technicality.
    ("cross-branch overlap", "collide.overlap", "lib/showrunner/collide.py",
     r"(def overlap\(cfg, branches, base=None\):\n)",
     "    return {'base': '', 'branches': {}, 'overlaps': [], 'unresolvable': []}\n"
     "def _neutered_overlap(cfg, branches, base=None):\n"),
    ("lingering processes, surfaced", "campaign.lingering_crawlers", "lib/showrunner/campaign.py",
     r"(def lingering_crawlers\(cfg\):\n)",
     "    return []\ndef _neutered_lingering_crawlers(cfg):\n"),
    ("the unarmed watchdog", "harness.waiting_probe", "lib/showrunner/harness.py",
     r"(def waiting_probe\(cfg, dirname\):\n)",
     "    return None, ''\ndef _neutered_waiting_probe(cfg, dirname):\n"),
    ("the harness answering its contract", "harness._verify_with_harness",
     "lib/showrunner/harness.py",
     r"(def _verify_with_harness\(worktree_path, dirname\):\n)",
     "    return None, None\ndef _neutered_verify_with(worktree_path, dirname):\n"),
    ("the porcelain read itself", "harness._porcelain", "lib/showrunner/harness.py",
     r"(def _porcelain\(binary, verb\):\n)",
     "    return None, None\ndef _neutered_porcelain(binary, verb):\n"),
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
    # A TUPLE OF NOTHINGS is the same report with more punctuation. `return None, ""` says "I have
    # no answer" exactly as `return None` does, and this scan could not see it — so two producers
    # added the same afternoon, both answering questions about whether a Crawler is in trouble,
    # joined the source without appearing as candidates. The detector was blind in precisely the
    # direction it exists to see, and the accounting said 0 unaccounted the whole time.
    if isinstance(v, ast.Tuple) and v.elts and all(
            isinstance(e, ast.Constant) and e.value in (None, False, "") for e in v.elts):
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
    # SITE WIRING, not product. `.showrunner/hooks/issue-waker.py` names ONE repo and ONE trusted
    # set of authors — this repo's, and its maintainer's. `install.sh` does not copy it for that
    # reason, so no consumer receives it and no consumer's behaviour depends on it. Generalising
    # it (repo and trust set from config, the way lane rules and the chat path already are) would
    # make it product and bring it into the swept set; until then, declaring it is the honest
    # state rather than letting a tracked Python file sit outside the denominator unnoticed.
    ".showrunner/hooks/issue-waker.py",
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
    "campaign.commits_ahead": "read only by is_merged/is_empty, both reached via reconcile in BOTH directions — the abandoned group asserts a branch with a commit and one without",
    "campaign.is_merged": "reached through reconcile, which the integration group asserts",
    "campaign.is_empty": "reached through reconcile, asserted BOTH ways in the abandoned group. The reason here used to read 'the abandoned-branch verdict asserts it' — true, and it excused a producer that was returning a constant True for every branch, because a constant True is what the empty-branch verdict expects. An exclusion reason has to name the direction it covers, or it excuses the half it never looked at",
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
    # SAME CLASS AS ITS SIBLING ABOVE, and MEASURED before being excused rather than assumed:
    # neutering it CRASHED ten groups instead of flipping their assertions, so the sweep
    # reported UNSCOREABLE — no measurement, which is not a coverage result. Hardening ten
    # groups against a None repo root to score a producer whose neutering takes the whole
    # suite down is work that buys a number and no safety.
    #
    # What the exclusion is NOT covering for: the CLAUDE_PROJECT_DIR fallback this function
    # gained is asserted directly, in `test_guard_entrypoints_agree`, by driving BOTH
    # entrypoints in three environments and requiring them to agree.
    "util.main_checkout": "every fixture resolves through it, so neutering it crashes ten "
                          "groups rather than flipping assertions — measured, and reported "
                          "UNSCOREABLE. Its new fallback is covered by the entrypoint-agreement "
                          "assertions instead",
    "collide.tracked_files": "git listing; plan_waves is swept and consumes it",

    # Return an exit code rather than a finding. Their behaviour is asserted through the CLI
    # group by exit code, which is the contract callers actually depend on.
    "cli.main": "dispatch; asserted by every CLI-group exit code",
    "cli.cmd_claim": "CLI wrapper over graph.claim; exit code asserted",
    # The roles seam's three verbs, arriving with that work. Excused as wrappers ONLY because
    # the suite drives each as a real subprocess and asserts its exit code and its output --
    # checked, not assumed: `role claim` is driven for a free seat, a taken seat and an unknown
    # role; `whoami` in both human and --porcelain form. The underlying policy they wrap
    # (roles.validate, roles.roster, roles.enforced_lines, roles.crawler_leaf) is swept above.
    # Named one key at a time: an unqualified reason here once excused a second function that
    # returned [] unconditionally, so a shared reason is how an unfailable accept hides.
    "cli.cmd_whoami": "CLI wrapper; the announcement's content is asserted by name in both "
                      "human and --porcelain form, and roles.enforced_lines IS swept",
    "cli.cmd_role_claim": "CLI wrapper over the roster; driven for a free seat, a seat already "
                          "held and an unknown role, with exit codes asserted",
    "cli.cmd_role_release": "CLI wrapper; its effect is asserted through roles.roster, which "
                            "IS swept, rather than through this function's own return",
    "cli.cmd_lock_acquire": "CLI wrapper; lock state asserted directly",
    "cli.cmd_lock_guard": "CLI wrapper; exit 2/0 asserted in the CLI group",
    "cli.cmd_worktree_guard": "CLI wrapper over lease.guard, which IS swept. Its exit 2 and its "
                              "fail-open exit 0 are BOTH asserted end to end through the shim, "
                              "from inside a real linked worktree. WHAT THAT DOES NOT COVER: "
                              "the payload plumbing between stdin and lease.guard — a bug that "
                              "read the wrong key would allow everything, and the shim tests "
                              "supply a well-formed payload, so they would not see it.",
    "cli.cmd_self": "CLI wrapper over pin.pin / pin.read_pin, both of which ARE swept. Its own "
                    "decisions are exit codes, and both branches are asserted THROUGH the CLI: "
                    "0 on a clean pin, 2 on a directory edited since it was pinned. WHAT THAT "
                    "DOES NOT COVER: the two argument refusals (--pin without --dest, --dest "
                    "without either), which are asserted nowhere.",
    "lease._register_locked": "the shared body of register_guard and register_stop_trigger, "
                              "split out so the whole read-modify-write sits under one file "
                              "lock. Stubbing register_guard (which IS swept) neuters it.",
    "lease._registration": "the shared reader behind _guard_registration and "
                           "_stop_registration. An always-None answer makes `doctor` report "
                           "both gates unregistered and makes register_* write a duplicate — "
                           "both loud, and both asserted through guard_health, which IS swept.",
    "cli.cmd_dispatch_guard": "CLI wrapper over dispatch.dispatch_guard, which IS swept below. "
                              "It reads a hook payload and maps the verdict onto exit codes; "
                              "the decision it reports is not made here.",
    "cli.cmd_worktree_register": "CLI wrapper over lease.register_guard, which IS swept. Its "
                                 "effect is asserted end to end through install.sh, on the "
                                 "UPGRADE path that had the bug: a repo with an existing config "
                                 "comes out registered, keeps its own hooks, and a second run "
                                 "adds nothing. WHAT THAT DOES NOT COVER: its exit 2 on an "
                                 "unwritable/unparseable settings file, asserted nowhere.",
    "lease._guard_registration": "private; reached only through guard_health, which IS swept, "
                                 "and both of its answers (registered / not) are asserted there",
    "cli.cmd_integrate": "CLI wrapper over campaign.integrate, which is asserted directly",
    "cli.cmd_integration_commit": "CLI wrapper over declare_integration, asserted directly",

    # Honest gaps. Named rather than excused, because a false exclusion is the failure this
    # file is about and an admitted hole is worth more than a tidy list.
    "harness.inject_conflicts": "REPORTING over the rule, not the rule. It is a loop that\n                                 calls harness.owns_path, which IS swept — sweeping the\n                                 wrapper would count the same predicate twice and read as\n                                 more coverage than exists.",
    "cli.cmd_overlap": "A COMMAND, not a producer — it formats what collide.overlap\n                       returns and chooses an exit code. collide.overlap carries the\n                       rule and is swept above; sweeping the printer too would count\n                       one finding twice.",
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


# A MUTANT THAT HANGS MEASURES NOTHING, AND WAITING FOR IT MEASURES NOTHING EITHER. A stub that
# returns the wrong shape can put a caller into a poll that never ends — `settled_state` and
# `lock run` both wait on something — and without a deadline the sweep stops at that producer
# forever, which reads to whoever started it as "still running" rather than as a result.
# Generous on purpose: the whole suite runs in well under a minute here, so this only fires on
# a genuine hang and never on a slow machine.
SUITE_DEADLINE = 600


def run_suite(cwd):
    try:
        proc = subprocess.run([sys.executable, os.path.join("test", "run.py")],
                              cwd=cwd, capture_output=True, text=True, timeout=SUITE_DEADLINE)
    except subprocess.TimeoutExpired:
        # Reported as its own outcome rather than as zero kills. "Nothing noticed" and "nothing
        # finished" are the same number and opposite findings — the second is unscoreable.
        return None, None, ("HUNG — the suite did not finish within %ss under this mutant, so "
                            "no assertion was measured. A stub that makes a caller poll forever "
                            "produces this; fix the stub or give the producer a bounded one."
                            % SUITE_DEADLINE)
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


def crashed_groups(output):
    """Groups that died mid-run. A kill count from such a run is NOT a coverage measure.

    A neutered producer often returns None where callers subscript it, and an exception takes
    the whole GROUP down: test/run.py catches it, records one "group crashed" entry, and the
    group's remaining assertions never execute. So the mutant produces exactly ONE new FAIL
    line, and every assertion that would have flipped is simply absent from the run.

    The number that comes out is not high or low — it is meaningless, and it reads as THIN,
    which is a verdict about the SUITE. It sent me strengthening a detector that was already
    covered by three assertions, none of which had run.

    This file's docstring has warned about mutants killed by crashing since it was written.
    Prose did not stop it happening; a check does. Reported separately so nobody reads a
    crash-shaped run as evidence of anything.
    """
    # index 1: the line is "  FAIL  <group> crashed: <exc>", and split() drops the
    # leading spaces — [2] picked the literal word "crashed:" for every group.
    return {l.split()[1] for l in output.splitlines()
            if l.startswith("  FAIL  ") and " crashed:" in l}


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
    # AN EXCLUSION THAT NAMES A REMEDY IS A DEBT, NOT A DECISION. Eight entries here read
    # "SHOULD BE SWEPT, IS NOT YET" — each one a work item wearing an exclusion's clothes — and
    # because they were excused the accounting counted them as ACCOUNTED FOR and printed
    # "0 unaccounted". The tool that would have chased the debt was the tool hiding it.
    #
    # Printed as a QUEUE rather than made an error: they are genuinely accounted for, and
    # failing on them would either stop the sweep or teach somebody to soften the wording, which
    # would lose the only thing that makes them findable. A note that names a REMEDY has a
    # done-state; one that names a REASON does not.
    owed = sorted(k for k, v in NOT_SWEPT.items() if "SHOULD BE SWEPT" in v)
    if owed:
        print("OWED — excluded with a reason that names its own remedy (%d):" % len(owed))
        for k in owed:
            print("  %-28s %s" % (k, NOT_SWEPT[k].split("—", 1)[-1].strip()[:74]))
        print("  These count as accounted, deliberately. They are not resolved.")
    if not problems:
        print("accounting ok: %d files, %d candidates, %d swept, %d excluded, 0 unaccounted, "
              "0 stale, 0 unscanned"
              % (len(python_sources()), len(candidates()), len(SWEPT_KEYS), len(NOT_SWEPT)))
    return 1 if problems else 0


# A PRODUCER WHOSE ONLY COVERAGE LIVES BEHIND AN OPTIONAL BINARY. Stated per producer rather
# than guessed, because the sweep cannot see WHY a group skipped — and a wrong guess here would
# convert a real hole into a reassurance, which is the direction that costs.
NEEDS_BINARY = {
    "stale_claims (reap's evidence, BrGraph)": "br",
}


def apply_anchor(src, pattern, stub):
    """Splice a neutering stub in after the anchor. Returns (new_text, times_applied).

    EXTRACTED SO THE REFUSAL CAN BE TESTED. The `n != 1` branch is what stops a renamed
    producer from reading as a clean sweep — and it lived inline in `main()`, which runs the
    entire sweep, so nothing could reach it. It was described in three comments and exercised
    by nothing.

    That is the shape wcs hit and fixed the same way: their lock reader was unreachable where
    it sat because the script exited before it, so they moved it out to be fed a state
    directly. A refusal nobody can invoke is a refusal nobody has seen work.
    """
    hits = len(re.findall(pattern, src))
    if hits > 1:
        # AMBIGUOUS IS NOT APPLIED. `count=1` silently neuters the FIRST match and leaves the
        # rest running, so a name implemented twice — `graph.stale_claims` is a method on TWO
        # backends here — scores as covered on the strength of mutating one of them. The other
        # can be entirely unprotected and the number never moves.
        #
        # Named by game_loop's auditor as one of three refusal modes a sweep owes; this file
        # had only the first.
        return src, -hits
    if not stub:
        # APPLIED AND CHANGED NOTHING is the third refusal a sweep owes, and here it can only
        # arise ONE way. The replacement is `group(1) + stub`, an INSERTION, so the text always
        # grows unless the stub is empty — I added a `new == src` branch for this and then
        # measured it unreachable: 69 targets, shortest stub 40 characters.
        #
        # A defensive branch that cannot fire is gameloop's `or` — belt-and-braces that does
        # nothing, or worse. So the unreachable check is gone and the INVARIANT that makes it
        # unreachable is enforced instead, which is a thing that can actually be violated by a
        # future edit.
        return src, 0
    return re.subn(pattern, lambda m: m.group(1) + stub, src, count=1)


def main():
    if "--accounting" in sys.argv:
        return accounting()
    only = None
    if "--target" in sys.argv:
        only = sys.argv[sys.argv.index("--target") + 1]

    # THE COPY IS THE SAFETY PROPERTY, NOT AN IMPLEMENTATION DETAIL. Every mutation lands in a
    # throwaway tree, so there is no restore step for a SIGKILL to skip and no window in which
    # this repo holds a deliberately broken file. llm_chat lost hours to the other design: a
    # sweep that mutated in place, a kill that missed the `finally`, and four broken files left
    # in a live tree looking like ordinary work.
    #
    # It also makes staleness UNREACHABLE rather than merely detected — the tree under test is
    # copied from source at the moment of the run, so a mutant can never be measured against
    # bytes that no longer exist. A neighbouring project lost a day to the detected-too-late
    # version of that: three false conclusions in a row, each reporting a real change as having
    # had no effect, every one of them against a green suite.
    #
    # The tempting edit is "why copy the whole repo 21 times, just mutate and restore" — it
    # reads as a speedup and it is how both of those failures are reintroduced.
    base_dir = tempfile.mkdtemp(prefix="mutate-base-")
    shutil.copytree(ROOT, os.path.join(base_dir, "s"), symlinks=True,
                    ignore=shutil.ignore_patterns(".worktrees", "*.db", "scratch"))
    b_pass, b_fail, b_out = run_suite(os.path.join(base_dir, "s"))
    shutil.rmtree(base_dir, ignore_errors=True)
    if b_pass is None:
        print("baseline suite did not report a RESULT line; fix that first")
        return 2
    baseline_failures = failing(b_out)
    # REFUSE ON A RED BASELINE. This printed "baseline: 357 passed, 1 failed" and swept anyway,
    # and the numbers it produced were wrong in the direction nobody checks: a kill is
    # `failing(mutant) - baseline_failures`, so an assertion that was ALREADY failing can never
    # count as one. Every producer covered only by such assertions reads UNPROTECTED — the
    # instrument reporting a coverage hole it had just created itself.
    #
    # It happened here, on a copy: an edit landed after the last green run and the sweep, whose
    # tree is a fresh copytree of this one, picked it up while the terminal still showed green.
    # The tell was the one that always works — the number contradicted something already
    # observed directly, a producer that had assertions written for it the same hour.
    #
    # WHAT THIS INSTRUMENT STILL CANNOT SEE, since a refusal is not a blind spot: a mutation
    # killed by CRASHING rather than by being caught (a neutered producer must return the
    # reassuring answer, not fail to return one); anything reached only through the two skipped
    # groups; and any assertion whose subject is this file.
    if b_fail:
        print("REFUSING TO SWEEP — the baseline is not green (%d failing), so every number "
              "below would be computed against it:" % b_fail)
        for name in sorted(baseline_failures)[:10]:
            print("  %s" % name[:100])
        print("\nA kill is measured as 'fails under the mutant, did not fail before'. An "
              "assertion already failing can never count as one, so a producer covered only "
              "by those reads as UNPROTECTED and the tool reports a hole it invented.\n"
              "Fix the suite, then sweep. A number from a red baseline is worse than none.")
        return 2
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

    # UNSCOREABLE IS NOT THIN, and they were printed under one heading. A producer whose
    # mutant CRASHED a group has no measurement at all — its remaining assertions never ran —
    # while a THIN one was measured and came back low. Filing the first under "thinly covered"
    # mislabels the exact category the crashed-group detector was built to separate out.
    weak, unscoreable = [], []
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
        new, n = apply_anchor(src, pattern, stub)
        if n < 0:
            print("%-34s %8s   AMBIGUOUS — the anchor matched %d times; count=1 would have "
                  "neutered only the first and left the rest running"
                  % (name, "-", -n))
            unscoreable.append((name, 0, "the anchor matched %d times, so a mutation would "
                                         "have covered only one of them" % -n))
            shutil.rmtree(work, ignore_errors=True)
            continue
        if n != 1:
            # A named producer that no longer exists is UNPROTECTED, not a skip. A sweep that
            # shrugs at a rename is a check that cannot fail — which is precisely the defect
            # the sweep exists to find, arriving inside the sweep itself.
            print("%-34s %8s   NOT FOUND — the anchor matched nothing (renamed? removed? "
                  "over-escaped?)" % (name, "-"))
            # UNSCOREABLE, not thin. This line USED to say UNPROTECTED and file the producer
            # among the thinly-covered, which reads as "nothing notices this" — a hole, stated
            # about code the sweep never mutated. It is the same conflation the CRASHED case
            # had: no measurement was taken. Caught by hitting it — an anchor that arrived
            # double-escaped reported a real, well-tested producer as UNPROTECTED, and the
            # summary heading was where I believed it.
            unscoreable.append((name, 0, "the anchor matched nothing, so nothing was mutated"))
            shutil.rmtree(work, ignore_errors=True)
            continue
        with open(target, "w") as fh:
            fh.write(new)
        p, f, out = run_suite(tree)
        shutil.rmtree(work, ignore_errors=True)
        if p is None:
            hung = isinstance(out, str) and out.startswith("HUNG")
            print("%-34s %8s   %s" % (name, "-",
                                      "SUITE HUNG (no measurement taken)" if hung
                                      else "SUITE CRASHED (stub may not parse)"))
            unscoreable.append((name, 0, "the suite hung" if hung
                                else "the suite did not run — the stub may not parse"))
            continue
        # Only assertions that FLIPPED count. Anything already failing unmutated is noise.
        killed = failing(out) - baseline_failures
        f = len(killed)
        # A group that crashed under the mutant took its remaining assertions with it, so this
        # count is not comparable with any other. Said before the verdict rather than beside it.
        crashed = crashed_groups(out) - crashed_groups(b_out)
        if crashed:
            print("%-34s %8d   CRASHED (%s) — this count is NOT a coverage measure: the group "
                  "died and its remaining assertions never ran. Make them fail rather than "
                  "raise (`or {}` on a possibly-None result), then re-sweep."
                  % (name, f, ", ".join(sorted(crashed))))
            unscoreable.append((name, f, "group(s) crashed: %s" % ", ".join(sorted(crashed))))
            continue
        # 3+ is comfortable; 1-2 is thin and worth naming rather than rounding up to "ok";
        # 0 is the real defect. Reporting thin as ok would be the same rounding-up this whole
        # exercise exists to refuse.
        # A SKIPPED GROUP'S ZERO IS NOT AN UNCOVERED PRODUCER'S ZERO, and the sweep could not
        # tell them apart. `BrGraph.stale_claims` scored 0 kills here and read as UNPROTECTED —
        # but `br` is not on PATH, so the group that covers it SKIPS, and no measurement was
        # taken at all. Same identity element as everything else: nothing noticed, and nothing
        # ran, produce the same number.
        #
        # Surfaced only because an ambiguous anchor was split: the pair had been scoring 7 on
        # the strength of the SqliteGraph half, so this producer's zero had never been visible.
        need = NEEDS_BINARY.get(name)
        if f == 0 and need and not shutil.which(need):
            print("%-34s %8d   UNMEASURABLE HERE — `%s` is not installed, so the group that "
                  "covers this SKIPS. Nothing was measured; this is not a coverage hole."
                  % (name, f, need))
            unscoreable.append((name, 0, "`%s` absent, so the covering group skipped" % need))
            continue
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
    if unscoreable:
        print("UNSCOREABLE producers (no measurement was taken — not a coverage result):")
        # THE TEMPLATE ASSUMED ONE CAUSE. "N killed before it died" describes a crashed group
        # and reads as nonsense for an anchor that never matched or a group that skipped — and
        # a summary line that does not fit its case is one a reader stops trusting. Each
        # unscoreable now states its own reason as the sentence rather than as a parenthetical
        # after a fixed phrase.
        for name, f, why in unscoreable:
            print("  %-32s %s" % (name, why))
            if f:
                print("  %-32s   (%d assertion(s) had flipped before the measurement stopped)"
                      % ("", f))
        print("  These are NOT coverage results. Until each is scoreable, the number beside it")
        print("  is a floor from a run that stopped early, or no run at all.")
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
        return 1 if (unprotected or unscoreable) else 0
    if unscoreable:
        return 1
    print("Every producer above is noticed by at least two assertions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
