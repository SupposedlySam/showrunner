---
name: sr-doctor
description: Diagnose a project's showrunner install — is it healthy, and if not, what exactly is wrong and what should be done about it. Ranks doctor's ok/warn/ERROR lines by what each one actually costs, surfaces the remedy each message already carries, and names what doctor does not check. Use when the user asks "is showrunner set up right", "showrunner doctor", "why did spawn refuse", "is my install healthy", "what's wrong with showrunner here", or runs /sr-doctor. Read-only: it reports and recommends, it never repairs.
---

# sr-doctor

Answer **"is this project's showrunner install healthy, and what exactly is wrong if not?"** —
terminal markdown, no files written.

`showrunner doctor` already prints every check. This skill's value is **ranking and consequence**:
which line will bite, when, and what a human should do — plus the honest edge, which is what doctor
does *not* look at.

The output format lives in `reference/example-output.md` — **read it before rendering.**

## Non-negotiables

- **Read-only.** Never repair. Do not `git add` the guard shim, do not edit `.showrunner/config.json`
  or `.claude/settings.json`, do not run `install.sh`, `init`, `baseline` or `self --pin`. Print the
  command; the human runs it. Several of doctor's messages already carry their own remedy — surface
  those verbatim rather than inventing a new one.
- **Never arm the watchdog.** The `waiting_probe` warning is addressed to a human on purpose: a
  probe an agent can write is a watchdog the watched sessions can switch off, and a probe of `true`
  exits 0 forever, which reads as "always waiting" and kills the alarm silently. Print the setting
  and the exact path. Do not write it.
- **Report the exit code, and do not soften it.** `doctor` exits **2** if any ERROR fired, **0**
  otherwise — warnings do not move it. Anything gating on this verb reads `$?`, so say which it was.
- **Never print a remedy naming a command that does not exist.** If a fix has no verb, say the fix
  is manual and show the literal edit — that is why the guard-registration remedy is printed as JSON
  rather than as a command.
- **One line per finding.** Verdict line, then one line each for the errors and warnings that fired,
  each naming its consequence — not its mechanism. The `ok` lines are a count, never a list. Detail
  and remedies come out when asked, and when this skill was reached from `/sr-status` the whole
  answer is the verdict line plus the findings. No paragraphs, no closing offer.
- **Terminal markdown only.** No files, no artifacts.

## 1. Find the binary

```sh
git rev-parse --show-toplevel
```

1. `<toplevel>/.showrunner/bin/showrunner` — every installed project, including central mode, where
   this file is ~20 lines of bash that exec a shared copy.
2. `<toplevel>/bin/showrunner` — only inside the showrunner repo itself.

Neither → the project has no install at all, which is the diagnosis. Name `./install.sh <project>`
(or `./install.sh --central <project>`) from a showrunner checkout, and stop.

## 2. Run it

```sh
<sr> doctor; echo "exit=$?"
```

It runs pre-init and still prints the whole report — but a missing config is an **ERROR**, because
every check below it then ran against **defaults, not against anything anyone chose**. An
uninitialised repo used to report all-green with exit 0.

**If you get the shim's message instead of a report, doctor never ran.** Under a central install
the entrypoint is a dispatcher, and with no shared copy at `$SHOWRUNNER_CENTRAL` (default
`~/.claude/showrunner-central`) every non-hook verb exits **1** with:

```
showrunner: no central install at <path>
```

That is the whole diagnosis: the project is wired to code that is not there. Nothing local is
broken and nothing local has been checked. The remedy the shim prints is
`showrunner self --pin <ref> --dest <path>` — run from a showrunner checkout — or re-run
`install.sh` **without** `--central` to go back to a local copy. Note the split: hook verbs
(`lock guard`, `worktree guard`, `worktree enter`, `stop-gate`) exit **0** in this state and say
`ALLOWED WITHOUT BEING CHECKED`, so a campaign keeps running with its guards absent rather than
blocked. Say that out loud — it is the state that looks like nothing is wrong.

## 3. Read the header — it names the code, not the project

```
showrunner 0.1.0 · pinned 94959a067001 (HEAD) · /Users/morgan/.claude/showrunner-central
repo: /Users/morgan/Development/dart_projects/revali
```

Two different places, on purpose. Under a central install the code lives once and the repo is a
consumer. Three provenances, and only two can name a commit:

| provenance | what it means | what to say |
|:--|:--|:--|
| **pinned** | `self --pin` extracted it from a git ref; the strongest answer | this project runs the same code as every other centrally-wired project on the machine — a bad pin reaches all of them at once |
| **checkout** | the code lives in a showrunner git repo. Reports HEAD, and `dirty` when the tree has uncommitted edits — which makes that sha an overstatement | fine for development; the sha is a claim about HEAD, not about what is running |
| **copy** | `install.sh` without `--central` copied from a working tree. **No commit names this code** | that absence is the argument for pinning. Do not guess a version |

## 4. Rank the findings — by what each one costs, not by print order

**ERRORs first.** Each one blocks something concrete; say what:

| ERROR | what is broken |
|:--|:--|
| no config | every check ran against defaults; `showrunner init` |
| `lock_root` inside `worktree_root` | every Crawler gets its own lock — the mutex is silently a no-op |
| `worktree_root` outside the repo (or *is* the repo root) | each Crawler's write guard denies its very first edit |
| the worktree guard's shim is MISSING / not executable | nothing denies a write into a tree another session holds |
| the guard is not registered on PreToolUse | an unregistered guard is indistinguishable from one that ran and was content |
| the waiting probe is CONFIGURED AND FAILING | "could not answer" rings and reports failing — this reads as a broken watchdog rather than the config error it is. Check the path is absolute and executable |
| `dispatch.chat.*` points at something that does not exist | a neighbour moved their checkout; Crawlers spawn unreachable, and a BLOCKED one can never be prompted |
| the binary every brief names does not resolve | every Crawler brief tells its agent to run it |
| graph backend refused | nothing can be read or claimed |

**Then the warnings that are silent exactly where they matter.** These are the two that look benign
and are not — both are the same mechanism: `git worktree add` copies **tracked** files from HEAD, so
an untracked or uncommitted file is present *here* and absent in *every worktree made from now on*.

- **`.showrunner/hooks/worktree-guard.sh` is not tracked by git yet.** Until it is committed, the
  worktree guard does not cross into this project's worktrees — the one place it exists to run. This
  is the state every fresh install passes through, which is why it is a warning and not an error;
  it is still a real gap. Remedy: `git add .showrunner/hooks/worktree-guard.sh` and commit it.
  A commit in someone else's repo is theirs to make — recommend, never do it.
- **the harness payload is upgraded but NOT COMMITTED.** Every spawn will refuse until it is
  committed. Commit first, then fan out.
- **the shim is tracked but its committed copy DIFFERS from the working one** — the guard that
  crosses is not the guard you are reading.

**Then the ones that degrade a guarantee quietly:**

- **`no baseline recorded`** — integration cannot tell a *new* failure from a pre-existing one, so
  `check` loses its whole premise (no new failures, never "all green"). Remedy:
  `showrunner baseline` on a known-good tree.
- **`no waiting probe`** — a fanned-out orchestrator waiting on Crawlers it cannot hurry looks
  identical to one that fell asleep: it gets rung, and at the ring cap it pages a human for a
  healthy run. Print the `config.local.json` setting and the probe path. **Do not arm it.**
- **`no resources configured`** — nothing is serialized, so `default_lane: serialized` has no lock
  to take.
- **`default_lane is 'headless'`** — unclassified work runs in parallel. Routing a serialized leaf
  into the headless lane collides on a single-consumer resource; the reverse merely runs slower.
  Not comparable, which is why one is the safe default.
- **a resource with no match patterns** — nothing will route to it.

**Then the `ok` lines.** Do not list all of them. Summarise as a count, and name only the ones the
reader would otherwise wonder about — the guard being registered, the harness crossing, the binary
resolving.

## 5. What doctor does not check

Say this every time. A report that lists only what was checked reads as a clean bill of health for
things nobody looked at.

- **Central reachability and pin drift.** Under a central install, doctor does not verify the shared
  copy is present, reachable, or still what was pinned — `VERSION` names what was *extracted*, and
  nothing hashes the directory afterwards. That check is open work (**CI-04**). Until it lands you
  can ask, read-only, when the header says `pinned … · <path>`:
  ```sh
  <sr> self --dest <the path in the header>
  ```
  Three answers, and they are not the same finding: **exit 0** prints the pinned sha and ref;
  **exit 2** means `VERSION` and `PINNED` disagree, so the directory was edited after the pin and
  neither names what is actually there; **exit 1** with `no pinned checkout at …` means central was
  never populated — which, if the header said `pinned`, cannot both be true, so trust neither and
  re-pin.
- **Whether the campaign is healthy.** Blocked Crawlers, held locks, stale claims and whether
  anything will re-check the campaign are `/sr-status`, not this. Route the user there.
- **Whether a check is meaningful.** doctor validates configuration and wiring — not whether the
  project's own checks are real.
- **Anything about other projects.** One repo, one report.

## 6. Render

Follow `reference/example-output.md`. Four lines plus one per finding:

```
<project> · <healthy | healthy, N caveats | BROKEN> · exit <0|2> · <provenance> <sha> <code path>

ERROR  <what is broken, as a consequence>
warn   <what it costs, as a consequence>

<n> checks passed
```

- Every finding is **a consequence**, not a restatement. "not tracked by git" is the symptom; "the
  guard is absent in every worktree" is the finding. One line each.
- **No remedies unless asked** — doctor already printed them, and the reader can ask for the one
  they care about. The exception is an ERROR with a non-obvious fix (`init`, a literal JSON edit):
  name the command, still on one line.
- `exit 0` with warnings is **not** "all clear". `healthy, 2 caveats` says it in two words.
- The **Not checked** list (§5) is one line, and only when it is relevant: central mode, or the user
  asked what doctor covers.
- Never paste the raw output. Never explain the mechanism unprompted.
