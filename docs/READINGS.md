# When the front-door docs were last read WHOLE

Documentation is searched far more often than it is read. A search confirms what you already
thought to look for; it cannot surface a remedy that now points at a trap, a count that said
"BOTH" after it became four, or a section whose ordering stopped making sense three features ago.
Those are found only by reading the file end to end, as somebody arriving new would.

`test/docs_surface.py` checks the ungameable half — that every verb, environment variable and hook
is at least NAMED. This file records the other half, which nothing can check: that a human or an
agent actually read the thing.

**This ledger is gameable, and that is fine.** Writing a line without reading satisfies it. What it
converts is "nobody re-read this" into "somebody put their name and a commit on it", which is a
lower bar than correctness and a far higher one than silence — and unlike silence, it leaves a
record a reviewer can disagree with. Do not add a gate that checks the quality of these entries;
that is the same unanswerable question one level up.

One line per reading, newest last:

    - <file> — read whole at <short sha> on <date> — <what the reading changed, or "nothing">

## Readings

- ~~llms.txt — read whole at 0608d32 on 2026-08-20~~ — **this entry was false when written.** The
  reading behind it covered lines 1–235 of 457, roughly half. Struck rather than edited down to
  what was true, because the useful record is not "half a file was read" — it is that the ledger's
  own first entry overclaimed, in the commit that introduced the ledger, written by the agent that
  had just finished arguing why whole-file reads matter. Nothing caught it; I found it re-reading
  my own commit message against the file. That is the failure mode this file has, demonstrated on
  day one, and it is why the section above says do not build a gate to check these entries: a gate
  would have passed this line too.
- llms.txt — read whole at ac2b9db on 2026-08-20 — lines 1–235 found four stale passages a search
  would not have: `doctor`'s watchdog remedy still pointed at `showrunner waiting` directly, which
  became a trap the day that verb grew a third exit code; `worktree register` said "BOTH hooks"
  after it became four across three events; and two descriptions of `waiting`'s exit codes predated
  the BLOCKED case. Lines 235–558 found nothing stale — recorded because "read and found nothing"
  and "not read" are the two states this file exists to tell apart, and only one of them is work.
- README.md — read whole at ac2b9db on 2026-08-20 — carried **the same two stale claims** as llms.txt:
  the watchdog remedy naming the verb rather than `waiting-probe.sh`, and `waiting` described
  without exit 3. Fixing llms.txt had not fixed them, and nothing connected the two. That is the
  argument for this file in one line: the defect was duplicated across the front door, and the
  half-read that found it in one file left it standing in the other.

- README.md — read whole at 2f3623a on 2026-08-26 — found two claims a reader would have ACTED on.
  The "Planned, not built" section listed the worktree lease and `install.sh --central`; both
  ship, and each plan doc carries its own status line saying which of its steps are outstanding.
  The README pointed at those docs and then restated their status from memory. Also a
  reproducer that named the WRONG test group for the concurrency table — worse than no citation,
  because a reader who runs it finds the claim absent and cannot tell whether the claim or the
  pointer is wrong.
- llms.txt — read whole at 2f3623a on 2026-08-26 — nothing stale, but the pipeline-gate section had
  been damaged by successive in-place corrections: a sentence about the hook's skip rule appended
  to an unrelated instruction, and one finding split mid-phrase across a line break. Each patch
  was correct on its own and the accumulation was not readable, which only a whole read shows.
  Rewritten as one block.
- both — read whole at 2f3623a on 2026-08-26 — stripped the counts that drift and carry nothing a
  reader acts on: the prototype demo's pass/skip split (it prints its own), the central shim's
  line count, and the pipeline gate's rates, which are now re-derived by `test/corpus.py` rather
  than copied onto a page where the population moves underneath them. Contract numbers were
  VERIFIED instead of removed — `PROSE_MAX = 400` and the Python 3.7 floor both hold.

## What a reading is

Lines read end to end, in order, in one pass — not a search, not a section, not "I wrote most of
this file so I know what is in it". If a reading covered part of a file, say which part. A partial
reading recorded as partial is worth having; recorded as whole it is worse than no entry, because
it retires the question.
