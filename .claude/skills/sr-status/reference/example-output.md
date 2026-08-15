# Reference output

The **fidelity target** for `/sr-status`. Match the length as strictly as the shape: the whole
answer is "is anything still running, and since when". Everything else is available by asking.

The campaign below is **constructed** — posed to show several states at once, which a real one
rarely does. The field shapes are real: every value comes from a key `snapshot` or the event journal
actually emits.

Note what is **absent**, because that is the format: no headings, no explanation of any verdict, no
remedy, no command, no offer, no closing line.

---

## Something is running

```
showrunner · 16:42 (2m ago)

BLOCKED  crawler-rpt-02  RPT-02  since 16:07 (35m)  — needs a message
LIVE     crawler-ci-04   CI-04   since 16:01 (41m)
PARKED   crawler-wl-06   WL-06   since 16:38 (4m)
DONE     crawler-ci-05   CI-05   since 16:20 (22m)  — not integrated

git-index HELD crawler-ci-04 (22m) · gh-issues FREE
ready 1 · in progress 3 · follow-up NONE SCHEDULED
```

Seven lines for four Crawlers. `BLOCKED` is first because it outranks `LIVE`, and its flag is four
words — the reader knows what a message means, and if they do not, they will ask.

## Nothing is running

```
showrunner · 09:14 (just now)

nothing running · ready 4 · in progress 0 · follow-up NONE SCHEDULED
```

Two lines. Not "no Crawlers on record, both resources free, here are the four ready leaves" — the
counts say all of that.

## An age that is not known

```
zonai · 11:05 (1m ago)

LIVE     crawler-wl-06   WL-06   since ?
BLOCKED  crawler-ci-04   CI-04   since 10:31 (34m)  — needs a message

device FREE
ready 0 · in progress 2 · follow-up on idle, via .game_loop
```

`since ?` because no `crawler.spawned` frame for it survived in the replay window. An estimated age
would be a number nobody measured, and this line is mostly ages.

## Blind, not quiet

```
showrunner · 14:02 (just now)  ⚠ JOURNAL UNREADABLE — counts below are partial

LIVE     crawler-ci-04   CI-04   since ?

git-index HELD crawler-ci-04 · gh-issues FREE
ready 4 · in progress 1 · follow-up NONE SCHEDULED
```

One flag on the header line. A campaign that could not be fully read must never render as a quiet
one.

---

## When there is no campaign at all

`crawlers`, `in_progress` and `ready` all empty — one line, then `sr-doctor` owns the screen:

> No campaign here — nothing claimed, nothing ready. Running `doctor` instead.

Same for a snapshot that could not be taken (missing config, unreachable central install):

> `snapshot` exited 1 — no central install at `~/.claude/showrunner-central`, so campaign state is
> unknown, not empty. Hook verbs are meanwhile exiting 0 unchecked. Running `doctor`.

## When there is no showrunner at all

No output. If the question was generic ("status?"), answer *that* question and never mention
showrunner. If they named showrunner, one sentence:

> This project has no showrunner install.
