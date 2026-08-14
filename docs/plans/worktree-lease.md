# Plan — the worktree lease: one session per tree, enforced

**Status:** plan, not built. Nothing below is implemented.
**Owner:** showrunner. See "Which layer owns this" for why it is not a game_loop ask.

Cited by symbol, not by line. Six of these files moved between drafting this and pushing it, and
a number I cannot check is confidence rather than precision.

A worktree today has an *owner* on paper and no *holder* in fact. A second Claude Code session can
open `.worktrees/crawler-se-01/`, start editing, and nothing anywhere refuses. The campaign record
knows who spawned the tree; nothing consults it at the moment somebody else writes.

This plan gives a worktree the primitive the device lane already has — an atomic `mkdir` whose
holder is a **live PID**, paired with a boot token — and then gives it teeth via a PreToolUse
guard, so a hijack is denied rather than described.

---

## The premise, checked

| Claim | Where | Verdict |
|---|---|---|
| `worktree.create()` refuses an existing path | `worktree.create` | Holds — but it refuses the **orchestrator's own re-spawn**, not a stranger's session. |
| Crawler names are deterministic | `worktree.crawler_name` | Holds. Two orchestrators spawning the same leaf collide on the path, which is the *only* thing standing in the way today — a coincidence of naming, not a lock. |
| Graph claims carry liveness (pid, boot, session, worktree) | `graph.stale_claims`, `docs/DESIGN.md` | Holds. Protects the **leaf**, not the **tree**. A session that never touches the graph is invisible to it. |
| `campaign.live()` = live PID **and** matching boot token | `campaign.live` | Holds. The right liveness rule already exists and is not applied to trees. |
| The lock primitive has four states and refuses to reclaim `UNREADABLE` | `locks.Lock.state`, `locks.Lock.acquire` | Holds. Reusable verbatim; `locks.py` is one of the few files the last 18 commits did not touch. |
| `showrunner lock guard` exists as a verb | `cli.cmd_lock_guard` | Holds. |
| ...and is registered as a hook somewhere | `.claude/settings.json` | **Refuted.** That file registers three game_loop hooks and no showrunner hook. Still true after the 18-commit catch-up. |

That last row is the one that shapes the plan. A guard verb nobody registers is a guard that has
never once run. Whatever we build here ships its own registration or inherits that fate.

---

## Which layer owns this

`docs/BOUNDARY.md` assigns **"Where a Crawler may write, and what it gets"** to showrunner, and
**PreToolUse** to game_loop. That reads like a split, and the first instinct was to file a game_loop
ask for a generic "check this external lock before writing here" seam.

That instinct is wrong, and checking cost one file read. PreToolUse is *Claude Code's* event, not
game_loop's — `.claude/settings.json` already carries three independent hook entries and any hook
that denies wins. showrunner registers a fourth beside them and game_loop never learns the word
"worktree". game_loop owns PreToolUse **for the concerns game_loop owns**; it does not own the event.

So: no cross-repo dependency, and BOUNDARY gets a row rather than a renegotiation.

**Worth raising with game_loop, but not blocking:** two PreToolUse hooks that both deny produce two
denial messages, and an agent under a mandate reading "denied" twice is exactly the agent that
starts finding `--no-verify`-shaped thinking reasonable.

---

## The lease

A new module, `lib/showrunner/lease.py`, built on `locks.Lock` rather than beside it. The lock root
is already one absolute path shared by every worktree and validated at config load — including the
refusal when `lock_root` sits inside `worktree_root`, which is the failure that makes a mutex
silently a no-op. That is exactly the property a lease needs and exactly the one that is easy to get
wrong.

```
<lock_root>/worktree:<crawler-name>.lock/
    pid       the Claude Code process, for liveness
    boot      boot token — a claim from a previous boot cannot still be running
    holder    crawler name, or "interactive"
    session   the Claude Code session id
    pid_basis how the pid was found — never silently absent
    ts
```

Four states, inherited whole and non-negotiable:

- **FREE** — take it.
- **HELD** — a live PID recorded this boot. The hijack case.
- **STALE** — proved dead. Reclaim, loudly, naming the holder.
- **UNREADABLE** — a partial write by a *live* holder reads exactly like a dead one. Refuse, never
  reclaim, make a human adjudicate. `locks.py` already says this better than a paraphrase would.

### Finding the PID, honestly — ANSWERED (WL-01)

A dispatched Crawler is easy: `dispatch.launch` already records `proc.pid`.

An interactive session was the open question, and it is now closed. **No PID is available**, from
two independent directions:

- The hooks reference states it outright, and lists what a hook does get: `session_id`,
  `prompt_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, plus `tool_name` /
  `tool_input` / `tool_use_id` on PreToolUse, `model` on SessionStart, and `agent_id` / `agent_type`
  inside a subagent. Environment carries `CLAUDE_PROJECT_DIR`, not a PID.
- game_loop — a mature consumer with hooks on every event — reads eleven payload fields across its
  guards and never a PID, and does no ancestor discovery of its own.

So the ancestor walk is **required**, not contingent. Measured in a live session: two hops from the
hook's shell to `claude`, `ps -o ppid=,comm=`, no ambiguity.

```
pid=17354 comm=zsh
pid=37158 comm=claude   <- found
```

**Its named failure mode, since one measurement on one machine is not a property.** The match is on
the process name, and here `claude` is a native binary whose `argv` is literally `claude`. An
install that launches through a wrapper — `npx`, a node shim — presents as `node` and the walk finds
nothing. That is not hypothetical enough to ignore and not common enough to block on, so it is
handled rather than assumed away: when the walk fails, record the hook's own parent and set
`pid_basis` to `"ppid-fallback"`, which `lease status` prints. A lease whose liveness rests on a
weaker fact says so, and "we could not find the session process" never reads as "we found it".

**What the walk does not establish:** that the process it found is *this* session rather than
another `claude` in the same ancestry. Nothing observed suggests that shape, and nothing here would
detect it. `session_id` is recorded beside the PID for exactly that reason — the PID answers
"alive?", the session id answers "who?", and neither is asked to do the other's job.

**Also settled, and it lands on WL-05:** `CLAUDE_PROJECT_DIR` is the *worktree* for a session opened
in one. That confirms the registration analysis below rather than leaving it as reasoning.

---

## The two entrypoints

### `showrunner worktree enter` — SessionStart

1. Resolve whether cwd is a showrunner-managed worktree — under the main checkout's
   `worktree_root`, which `util.main_checkout` already resolves correctly from inside a linked
   worktree via `--git-common-dir`. Not a worktree → silent no-op.
2. Read the lease.
3. **FREE** → acquire. One line of context: you hold this tree.
4. **HELD, same session id** → nothing. Re-entry is not a hijack.
5. **HELD, different live session** → emit the options block below into agent context. Do not touch
   the lease.
6. **STALE** → reclaim, naming whose it was and why it was reclaimable.
7. **UNREADABLE** → refuse to touch it, and print the same remedy `locks.py` prints.

SessionStart cannot block a session, so this is where the *prompt* lives and not where the
*enforcement* lives. Confusing the two is how a guard becomes advice.

### `showrunner worktree guard` — PreToolUse

Registered on `Write|Edit|NotebookEdit|Bash`. Exit 2 denies. Denies only when the lease is `HELD` by
a **different live session**.

Carve-outs, because a guard that blocks its own fix gets switched off (INV5):

- `showrunner worktree fork|takeover|status|release` always pass.
- Reads are never touched. Reading someone else's tree is legitimate and common.
- The main checkout has no lease and is never guarded by this. `campaign.integrate` already
  serializes the main checkout with its own file lock. Stated as a limit, not an oversight.

### Registration has to survive the crossing, and that is the real work

`.claude/settings.json` is tracked, so it crosses into every worktree — the README says this is
precisely why a Crawler has rails at all. But the hook *command* has to resolve on the other side,
and the two obvious spellings both fail:

- `"$CLAUDE_PROJECT_DIR"/.showrunner/bin/showrunner` — in a worktree `CLAUDE_PROJECT_DIR` is the
  worktree, and `.showrunner/` is runtime state that `git worktree add` does not carry. Dead.
- An absolute path baked into the tracked file — resolves here, and is wrong in every other clone.

`brief.sr_bin` solved the same problem for brief text by resolving against the filesystem at
generation time, but a hook command is a static string in a tracked file, so it cannot borrow that.

So the registration points at a **tracked shim** — `.showrunner/hooks/worktree-guard.sh`, a few
lines of machine-agnostic bash that resolves the main checkout with `git rev-parse --git-common-dir`
and execs the real binary. Tracked, so it crosses; machine-agnostic, so it can be. That is the same
shape as the dispatcher shim in [`central-install.md`](central-install.md), and the two should share
one resolver rather than growing two.

**Fail posture for that shim:** exit 0 when it cannot find a binary. A PreToolUse that hard-fails on
its own plumbing blocks every write including the fix. `doctor` carries the loudness instead, and
`worktree enter` says one line into agent context when the guard is inert.

---

## What the prompt offers

The expectation — "it'll likely be to create a new worktree" — is the first option, and it is a
**command**. INV: a printed remedy is a claim that a command exists, so each of these ships or none
of them is printed.

```
This worktree is held by another live session.

  holder   crawler-se-01  (session 4f2a…, pid 88213, alive, this boot)
  since    2026-08-14T09:12:04
  leaf     SE-01 — the staleness class the issue describes

Writes here are DENIED while that holds. Pick one:

  1. Your own tree, same starting point — the usual answer:
       showrunner worktree fork --from crawler-se-01
     New worktree, new branch off the same base commit. Moves you there.

  2. Read-only. Stay, read, do not write. Nothing further needed.

  3. Take it over — ONLY if you know that session is gone:
       showrunner worktree takeover crawler-se-01 --reason "<why>"
     Refuses while the holder is alive. Refuses on an unreadable pid. Logged.

  4. Leave.
```

`fork` is the one new piece of real machinery: it resolves the held tree's `base_sha` from the
campaign record — stored at spawn precisely because git cannot reconstruct it afterwards — creates a
worktree and branch from that same commit, runs the same injection and harness provisioning `spawn`
runs, and acquires the lease. It is `spawn` minus the graph claim.

---

## What this does not cover

Named because a guard that overstates its reach buys false confidence (INV6), and every item is a
real hole.

- **A session with no hook registered is not guarded at all.** The guard is exactly as present as
  its registration. `doctor` must check for it and report absence as an error.
- **Anything outside Claude Code.** A human in `vim`, a shell script, a `git checkout` — no
  PreToolUse, no guard. The lease is a fact those tools never consult. This is the largest hole and
  it does not close.
- **The lease protects the tree and nothing else.** INV: isolation is per-resource; a worktree is not
  a boundary. Two sessions in *different* trees still share the git common dir, the lock root, the
  graph and the campaign record. `worktree.audit_shared` already enumerates this and `enter` should
  print its output rather than re-deriving it.
- **Not fair.** Like `Lock.acquire`, there is no queue; a session that keeps losing keeps losing.
  Said out loud because a fairness property nobody states is one somebody will assume.

---

## Tests

The suite is gated on its own claimed count, and the mutation sweep now refuses to measure against a
red baseline. Both matter here: a lock's *permissive* half is the silently untestable one — the same
asymmetry `worktree.unignored` was rewritten to solve. (No assertion count is quoted in this file on
purpose — only three tracked docs are gated on that number, and a fourth would rot unwatched.)

- Every "guard allows" assertion is paired with the case where it denies. An allow-only suite passes
  identically against a guard that does nothing.
- `UNREADABLE` asserted as its own outcome, distinct from `STALE`. Binary and empty pid files.
- A lease survives a simulated boot-token change as **not alive** — the PID-reuse case.
- `fork` produces a tree whose base commit equals the held tree's recorded `base_sha`, asserted
  against the SHA and not against "it has commits".
- **The shim is asserted from inside a real linked worktree**, which is the only place its resolver
  can be wrong. Asserting it from the main checkout tests nothing — that is the shape of the dead
  path `sr_bin` had for a week.
- **Mutation:** neutering `worktree guard` to always allow must turn the suite red.

---

## Build order

1. T0 — confirm the hook stdin/env contract for a session PID. Everything else assumes it.
2. `lease.py` on `locks.Lock`, plus `lease status`. No hooks yet.
3. `worktree enter` and its options text. No teeth — observe it against a real second session and
   check the hijack is detected at all before anything is denied.
4. `worktree fork`.
5. The tracked shim + `worktree guard` + registration + `doctor` checks.
6. `worktree takeover`, last, because it is the one verb that acts on somebody else's work.

Steps 5 and 6 are the only ones that refuse anything, and neither should be written before step 3
has produced a logged, observed hijack. No gate without one.
