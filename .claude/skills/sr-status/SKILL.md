---
name: sr-status
description: Report what a showrunner campaign is doing right now — which Crawlers exist, what each is working on, what is BLOCKED, who holds a lock, and whether anything will re-check the campaign if you walk away. Use when the user asks "what are my agents doing", "campaign status", "showrunner status", "is anything stuck/blocked", "what is my fan-out doing", "can I walk away", or runs /sr-status. Checks first whether this project has showrunner at all: if it does not, it does nothing and stays silent, so a generic "status" question in an unrelated project is answered normally. If showrunner is installed but there is no campaign to report, it runs sr-doctor instead. Read-only: it reports and recommends, it never acts.
---

# sr-status

Answer **"what is this campaign doing right now, and what needs me?"** — rendered as terminal
markdown, fast to read, no files written.

showrunner puts each Crawler agent in its own git worktree and enforces the party-wide rules no
single agent can see. This skill's value is not the CLI call; it is **interpretation** — turning
one JSON blob into "here is what is actually happening and what you should do about it".

The output format lives in `reference/example-output.md` — **read it before rendering.** It is the
target, not a suggestion. This file explains how to get the data and how to decide what it means.

## Non-negotiables

- **Read-only.** Never run `reap --apply`, `integrate`, `claim`, `release`, `park`, `unpark`,
  `close`, `spawn`, `lease`, or anything else that mutates. Every remedy is *printed as a command*
  for the human to run. Reclaiming, merging and killing are statements about somebody else's work.
- **Never arm the watchdog.** `showrunner waiting` is the probe an idle watchdog calls, and a human
  arms it once per install, deliberately. A probe an agent can wire up is a watchdog the watched
  agent can switch off — and a probe of `true` exits 0 forever, which reads as "always waiting" and
  disables the alarm silently. When it is unarmed, print the remedy and stop. Do not write it.
- **One call, not four.** Take `snapshot`. Never assemble the picture from `status` + `reconcile` +
  `waiting` + `plan`: each re-opens the graph separately, so a fan-out landing between them hands
  you a composite of instants that never co-existed. `snapshot` exists to close exactly that window.
- **Never name a time for the next re-check.** The harness's watchdog fires on *idle* and publishes
  no interval, so a clock time here would be a number invented about an event showrunner does not
  schedule. Say the trigger — "when this orchestrator next goes idle" — or say NONE SCHEDULED.
- **A quiet report and a blind one are different.** If `journal_unreadable` is true, say so at the
  top. Never render "nothing is happening" from a read that failed.
- **Brief. This is the point of the skill, not a style note.** The question is "is anything still
  running, and since when" — answer it in a handful of lines with timestamps and stop. No prose, no
  remedies, no offers, no closing sentence. Detail is available by asking, and the user will ask.
  See §6, which is a hard ceiling rather than a suggestion.
- **Terminal markdown only.** No files, no artifacts, no browser.

## 1. Does this project even have showrunner? — if not, do nothing

**This runs first, before anything else, and it is a gate rather than a lookup.** "Status" is an
ordinary English word: this skill will fire on questions that have nothing to do with showrunner,
in projects that have never heard of it. In those it must get out of the way silently.

```sh
git rev-parse --show-toplevel
```

Not a git repo → **not a showrunner project.** Stop here.

Otherwise ask two separate questions, in this order. They are separate because the answers come
apart: this very repo has a config and no `.showrunner/bin/`.

**Is showrunner here at all?** Yes if either holds:

- `<toplevel>/.showrunner/config.json` exists → an installed project, and
- `<toplevel>/bin/showrunner` **and** `<toplevel>/lib/showrunner/cli.py` both exist → the showrunner
  repo itself, which never runs its own installer

Neither → **no showrunner here**, and the rest of this file does not apply.

**Which binary?** First of these that is executable:

1. `<toplevel>/.showrunner/bin/showrunner` — every installed project. Still the entrypoint under a
   central install, where that file is ~20 lines of bash that exec a shared copy.
2. `<toplevel>/bin/showrunner` — the development checkout.

Showrunner is present and **neither is executable** → do not report an empty campaign and do not
hand off to doctor, which cannot run either. One line: the install is here but its binary is
missing, which is the state where every Crawler brief names a path that does not resolve. Re-run
`install.sh` from a showrunner checkout.

**When there is no showrunner here, do nothing.** Specifically:

- Render no report, no heading, no "0 Crawlers" — an empty campaign report about a project that has
  no campaigns is a confident answer to a question nobody asked.
- Do not pitch `install.sh`. Do not go hunting in other repos, in parent directories, or in
  `~/.claude/showrunner-central` for a campaign to report on. One repo, or nothing.
- **If the user's words were generic** — "status", "what's the status", "how are things going" —
  they were not asking about showrunner. Answer the question they actually asked, as if this skill
  had never fired, and do not mention showrunner at all.
- **Only if they named it** — "showrunner status", "/sr-status", "what are my Crawlers doing" — say
  one plain sentence: this project has no showrunner install. Then stop. No report follows a
  sentence like that.

## 1a. Installed, but nothing to report → run `/sr-doctor` instead

Take the snapshot first (§2 — you cannot know this without it). Then, if **all** of these hold:

- `crawlers` is empty, **and**
- `in_progress` is empty, **and**
- `ready` is empty

…there is no campaign to describe. Rendering "no Crawlers, no work, both locks free" tells the
reader nothing they did not already know, and the question behind an empty campaign is almost
always **"is this thing even set up right?"** — which is doctor's question, not this one.

So say one line — *"No campaign here yet — nothing claimed, nothing ready. Running `doctor`
instead, since the useful question is whether the install is sound."* — then **invoke the
`sr-doctor` skill** and let it own the rendering. Do not re-implement its ranking here; the layer
that owns the concept owns the check.

Same fallback, for the same reason, when the snapshot **cannot be taken at all**: a missing config
or an unreachable central install (§2). Those are install questions wearing a status question's
clothes. Report the failure in one line, then hand off to `sr-doctor`.

If `sr-doctor` is not available in this session, run `<sr> doctor` yourself and render its lines
**errors first**, each with its consequence — never as a raw dump.

A campaign with `ready` work and no Crawlers is **not** this case. That is a real status — work is
waiting to be fanned out — and it gets a real report.

## 2. Take the snapshot — one call

```sh
<sr> snapshot
```

JSON on stdout. Every field, and what it is for:

| field | what it answers |
|:--|:--|
| `project` · `instance` | which campaign this is. `instance` matters when several are running |
| `at` | epoch seconds this was taken. Render as local time **and** as "N minutes ago" |
| `cursor` | the last event this snapshot could have seen — the honest join to `watch --since` |
| `crawlers[]` | `crawler`, `leaf`, `branch`, `verdict`, `alive`, `blocked`, `harness` |
| `resources[]` | `resource`, `state` (`FREE`/`HELD`/`STALE`), `holder`, `pid` |
| `ready[]` | unblocked, unclaimed leaves — `id`, `title`, `lane` |
| `in_progress[]` | claimed leaves — `id`, `actor`, `parked` |
| `waiting{}` | `waiting` plus counts of `live`, `parked`, `blocked` |
| `follow_up{}` | whether anything will look again: `harness`, `last`, `waiting`, `scheduled`, `why` |
| `journal_unreadable` | the read failed. Not an idle campaign |

Distinguish the failures rather than reporting an empty campaign:

- **Exit 1, `showrunner: no central install at <path>`** → this project dispatches to a shared copy
  of the code that is not there. The campaign state is **unknown**, not empty — and note that the
  hook verbs are meanwhile exiting 0 as `ALLOWED WITHOUT BEING CHECKED`, so any campaign still
  running is running unguarded. Say exactly that in one line, then hand off to `sr-doctor` (§1a).
- **Missing config** → the project was never `showrunner init`-ed. There is no campaign, and the
  real question is the install: one line, then hand off to `sr-doctor` (§1a).
- **`journal_unreadable: true`** → report it at the top and treat every count below as partial.
  This is **not** a doctor case: the campaign exists and part of it was readable.

**Taking a snapshot appends to showrunner's own logs.** It computes the waiting verdict, which
appends a line to `.showrunner/waiting.jsonl`, and a Crawler crossing into or out of BLOCKED is
journalled as an event. In a repo that tracks those files this leaves the working tree dirty —
mention it only if it matters to what the user is about to do. It never touches a claim, a branch,
a lock or a worktree.

## 3. Rank the Crawlers by severity, never by order of appearance

`reconcile` assigns exactly one verdict per Crawler, first match wins, in this order. Report them
in the same order — the ladder *is* the severity:

| verdict | what is true | what to do |
|:--|:--|:--|
| **HARNESS DRIFTED** | its rules or harness scripts no longer match the project's | its commit gate owes something else, so anything it certifies means less than it appears. Do not integrate on the strength of its own gate |
| **HARNESS UNDETERMINED** | cannot tell whether its rules match | same posture, less information |
| **BLOCKED** | **alive and inert** at a refused turn-end | send it a message in its room. Never wait |
| **LIVE** | working | do not disturb |
| **PARKED** | paused at a usage limit; the claim survives on purpose | nothing — `unpark` when the limit resets |
| **ABANDONED** | the owner is not alive and the work is not integrated — or the branch never received a commit | `showrunner reap` (dry run) — and read `uncommitted` before anyone proposes deleting a tree |
| **MERGED** | contained in base | `showrunner reap --apply` to clean up |
| **DONE BUT NOT INTEGRATED** | leaf closed, branch still standing | `showrunner integrate` |
| **GONE** | nothing on disk | nothing |
| **RETIRED** | leaf closed or refuted and the Crawler is no longer running | nothing. **This is not abandonment** — the close gate demanded a real artifact to get here |

BLOCKED ranks above LIVE: it is alive and doing nothing, and every other signal reads healthy. Its
line gets a flag of at most four words — `— needs a message` — and **no explanation**. The two
HARNESS verdicts outrank even BLOCKED: they say the certification is suspect, not that one agent is
stopped.

## 4. Timestamps — where they come from

Every Crawler line carries a clock time and an age. `snapshot` does **not** carry per-Crawler times:
the campaign record has no `spawned_at`, so they come from the journal, which is a bounded replay
that ends on its own:

```sh
<sr> watch --limit 100
```

Per Crawler, take the `ts` of its most recent frame of the relevant kind:

| line | frame |
|:--|:--|
| LIVE / PARKED / any running state | `crawler.spawned` |
| BLOCKED | `crawler.blocked` — this is the "since when", and it is the number that matters |
| a held resource | `lock.acquired` |

No frame for it → print `since ?`. **Never estimate an age.** This is a second call and a second
instant, which is fine for an age and is not fine for the picture — the picture is `snapshot`'s.

**Never use `--follow`** — it does not return.

## 5. Three facts that must survive being brief

Brevity cuts prose, never these. Each is one field on one line:

1. **`follow_up`** — `follow-up NONE SCHEDULED` when nothing will re-check the campaign. It is the
   ordinary state, and omitting it lets "nothing is scheduled" look exactly like "something is". The
   remedy is not printed unless asked.
2. **`resources`** — anything not `FREE`, with its holder. `STALE` means the holder is gone.
3. **`journal_unreadable`** — one flag at the top of the header line. Never render a quiet campaign
   from a read that failed.

On request only: `blocked_detail`, `uncommitted` and `scratch_files` come from
`<sr> reconcile --json`, and the lock queue is `lock.refused` frames in the replay. Do not fetch
these to pad a report nobody asked to be padded.

## 6. Render — brief, and brief is a hard rule

Only reached when §1 found showrunner **and** §1a found a campaign to describe.

The reader wants one thing: **is anything still running, and since when.** Everything else is
available by asking. `reference/example-output.md` is the exact target.

```
<project> · <HH:MM> (<age>)

<VERDICT>  <crawler>  <leaf>  since <HH:MM> (<age>)  [<≤4-word flag>]
<VERDICT>  <crawler>  <leaf>  since <HH:MM> (<age>)

<resource> <STATE> <holder> · <resource> <STATE>
ready <n> · in progress <n> · follow-up <NONE SCHEDULED | on idle, via <harness>>
```

**Ceiling: one line per Crawler, plus three. A campaign with no Crawlers is two lines.** If the
output would not fit on a phone screen, it is wrong.

- **No prose.** No headings, no "Needs a human" section, no paragraph explaining a verdict, no
  narration of what you ran.
- **No remedies, no commands, no offers.** Not `showrunner integrate`, not the watchdog config, not
  "let me know if you want more". The user asks when they want them.
- **No closing sentence of any kind.** The last line is the counts line.
- **Every Crawler line carries a clock time and an age** (§4). Ages come from the journal;
  `since ?` when there is no frame, never a guess.
- Severity order (§3), BLOCKED above LIVE.
- Nothing running → say exactly that on the counts line: `nothing running · ready 4 · follow-up
  NONE SCHEDULED`.
- Never paste JSON. Never explain the JSON.

**When asked for more**, answer the narrow question — that Crawler's `blocked_detail`, that lock's
queue, that leaf's title — and stop. A follow-up question is not permission to render the long
report.
