---
name: showrunner
description: Do the work THROUGH showrunner instead of by hand — decompose into a leaf graph, plan waves that do not collide, spawn Crawler sessions in their own git worktrees, then reconcile and integrate. Use whenever the user says "use showrunner", "showrunner spin up another session", "have showrunner do X", "spawn a crawler", "fan this out", "run these in parallel", "orchestrate this", or runs /showrunner. Every step is a real CLI verb and the command is echoed, so "did it actually use showrunner?" is answerable from the transcript. For "what is running" use sr-status; for "is it set up right" use sr-doctor; to install it use sr-install.
---

# showrunner

The user said **use showrunner**. That is an instruction about *mechanism*, not a figure of speech:
the work goes through `./.showrunner/bin/showrunner`, and the transcript shows it.

## Non-negotiables

- **Never simulate it.** Do not spawn a subagent with the Agent tool and call it a Crawler. Do not
  "act as" an orchestrator. A Crawler is a real Claude Code session in its own git worktree, created
  by `showrunner spawn`, holding a real claim in the graph. Nothing else is one, and calling
  something else one is the failure this skill exists to prevent.
- **Echo every command you run**, one line each, before its result. This is the whole answer to
  "is it actually using showrunner under the hood" — the reader should be able to re-run your
  session from the trace. One line, not a paragraph.
- **If showrunner is not installed here, stop and say so.** Do not improvise an orchestration with
  subagents instead. Offer `/sr-install`. That is the *whole* response.
- **Never arm the harness's idle watchdog.** A human does that once per install, deliberately: a
  probe an agent can write is a watchdog the watched sessions can switch off, and a probe of
  `true` exits 0 forever. `showrunner doctor` prints the exact file and line to paste — it asks
  the harness rather than naming its config key, so the instruction survives a rename there.
- **Never bypass a gate.** Not `--no-verify`, not editing `verify.yaml` to widen it, not closing a
  leaf without a real artifact. A gate refusing you is usually the gate being right.
- **Brief.** One line per command, one line per result. No narration of what you are about to do.

## 1. Find the binary — and prove it exists before promising anything

```sh
git rev-parse --show-toplevel
```

- `<toplevel>/.showrunner/bin/showrunner` — an installed project (also the entrypoint under a
  central install, where it is a dispatcher shim).
- `<toplevel>/bin/showrunner` — the showrunner repo itself.

Neither → **not installed.** Say that in one sentence, offer `/sr-install`, and stop.

Exit 1 with `showrunner: no central install at <path>` → the project is wired to code that is not
there. `/sr-doctor`. Stop.

## 2. On resume, reconcile FIRST

Before dispatching anything, on any session that did not itself create the campaign:

```sh
<sr> reconcile
```

An abandoned worktree may hold the only copy of real work. Surface it; never silently reuse it and
never silently delete it. A **BLOCKED** Crawler is alive and inert at a refused turn-end — it needs a
message in its room, not time, and not a second Crawler on the same leaf.

## 3. The dispatch path

The order matters, and each verb answers a question the next one needs.

```sh
<sr> ready                          # unblocked AND unclaimed — the only discovery entrypoint
<sr> plan                           # group ready work into waves whose file sets do not overlap
<sr> route                          # which lane each leaf takes, and the rule that decided
<sr> spawn <leaf> --actor <name>    # worktree, branch, scratch, brief, lock, claim
<sr> spawn <leaf> --actor <name> --launch   # ...and START a real Claude Code session in it
```

- **Do not fan out past what `plan` allows.** Two leaves can be mutually unblocked and still be the
  same edit — the graph models dependencies, not files. A false collision costs one wave of latency;
  a missed one costs a merge conflict in an unattended run.
- **`--launch` needs a chat room.** A Crawler refused at its turn-end stays alive and inert, and a
  message is the only thing that restarts it. If `dispatch.chat` is not configured, use `spawn`
  **without** `--launch` and drive the sessions yourself — say which you did.
- **`--dry-run` with `--launch`** prints the command and starts nothing. Use it when the user asked
  what would happen rather than for it to happen.
- **`--finding "<what you already checked>"`** is worth using. Where you have verified a premise, put
  the finding *and its evidence* in the brief and ask the Crawler to confirm or refute it. An
  independent confirmation with line numbers is worth more than either reading alone.
- **Several orchestrators may be running.** Never read `ready` and take its first entry — everyone
  gets the same list. `<sr> claim --next --actor <you>` takes any free leaf atomically; exit 1 means
  the graph is dry, and losing a race is not an error.

## 4. Putting work INTO the graph

"Spin up a session and do X" usually means X is not a leaf yet. Add it before you can spawn it:

```sh
<sr> add "<title>" --body-file <path>     # the BODY IS THE BRIEF the Crawler reads
<sr> dep <child> <parent>                 # child is blocked by parent
```

Write the body as a brief for someone who has not read this conversation: what to do, what "done"
looks like, what to check the premise against. A one-line title with no body produces a Crawler
guessing at intent.

## 5. Bringing it back

```sh
<sr> reconcile        # merged / abandoned / live / BLOCKED
<sr> integrate        # serial merge, checks re-run on each MERGED result
```

Checks are **no NEW failures**, never "all green" — which is why `baseline` exists and why a repo
without one cannot tell a new failure from a pre-existing one.

## 6. Which verbs you may run, and which need a yes

**Run when the work implies them** — they are additive, reversible, or pure reads: `ready`, `plan`,
`route`, `status`, `snapshot`, `reconcile`, `show`, `list`, `brief`, `add`, `dep`, `edit`, `claim`,
`spawn` (including `--launch` when the user asked for a session), `lock run`, `baseline`.

**Ask first, every time** — they touch somebody else's work or cannot be undone: `integrate`,
`reap --apply`, `close`, `release`, `park`/`unpark` on a Crawler you did not spawn, deleting a
worktree, and anything that would kill a live process. Name what it will do, in one line, and wait.

**Never**: arm the watchdog; `close` a leaf on a Crawler's behalf to make a gate pass; delete a
worktree with uncommitted changes in it.

## 7. Route the neighbouring questions

- "what is running / anything blocked / can I walk away" → **`/sr-status`**
- "is this set up right / why did spawn refuse" → **`/sr-doctor`**
- "install showrunner here / reinstall it" → **`/sr-install`**

Do not re-implement those here.

## 8. What to print

A trace, not an essay. One line per command:

```
$ showrunner ready                     → 4 leaves
$ showrunner plan                      → wave 1: CI-04, WL-06 · wave 2: CI-05
$ showrunner spawn CI-04 --actor crawler-ci-04 --launch
                                       → crawler-ci-04 · .worktrees/ci-04 · pid 48120
```

Then one line of what you did and what the user should do next, if anything. If a verb refused, print
its refusal verbatim — showrunner's refusals carry their own reason and are usually the answer.
