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

- llms.txt — read whole at 0608d32 on 2026-08-20 — found four stale passages a search would not
  have: `doctor`'s watchdog remedy still pointed at `showrunner waiting` directly, which became a
  trap the day that verb grew a third exit code; `worktree register` said "BOTH hooks" after it
  became four across three events; and two descriptions of `waiting`'s exit codes predated the
  BLOCKED case. Also added five sections for surfaces that existed in neither doc.
- README.md — NOT YET read whole since the campaign/roles/seams work landed. Recorded as an
  absence rather than left blank: an empty ledger and an unread file look identical, and that is
  the confusion this file exists to remove.
