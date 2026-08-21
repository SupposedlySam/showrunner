# showrunner — design notes

Working notes, not a spec. Records the decisions and the reasons, including the ones that only
became obvious by running the thing against a real codebase and losing.

Structure: the two axes, then the seam with the layer below, then the rule the whole design keeps
rediscovering, then the areas where that rule cost something to learn — isolation, adoption,
premises, observability, distribution — then what is still open.

---

## Two axes (why showrunner ≠ game_loop)

- **game_loop = vertical:** integrity within one session — the autonomy engine, the safety hooks,
  the "name a real file" honesty gate. Enforced by Claude Code hooks. Its power is its narrowness.
- **showrunner = horizontal:** breadth across work and agents — a dependency graph, parallel
  lanes in separate worktrees, cross-process locks over single-consumer resources, and resume. A
  different job, so a different repo.

Dependency direction: **showrunner → game_loop + br**, and the primitives never learn showrunner
exists. Both are optional; everything showrunner guarantees works without either.

**Crawler** is the only piece of vocabulary that matters: one Claude Code session, in its own
worktree, with its own scratch dir and its own brief, working one leaf.

---

## The seam

The interesting edge is enforcement over shared resources, and it splits cleanly:

- The **lock** — shared cross-process state — is showrunner's.
- The **guard** that consults it before a risky verb is a game_loop-shaped **PreToolUse hook**
  running inside each Crawler. `showrunner lock guard` exits 2, the deny code.
- So game_loop grows a generic "check this external lock before these verbs" seam and showrunner
  supplies the lock path. game_loop never learns the word "device".

Same shape for turn-ends: game_loop provides the hook mechanism, showrunner provides the graph and
the policy those gates read (`showrunner stop-gate`, also exit 2).

`lock run` is deliberately **not** a hook verb. Routing and guarding are optimisations; `lock run`
is the guarantee. A missing install costs the optimisation and keeps the guarantee, which is the
only reason the fail-open posture below is acceptable.

---

## The rule the whole design keeps rediscovering

**A degraded guard must fail loud, never quiet.** Every failure mode that actually hurt had the
same shape — not an error, but *silence*. A mutex that is silently a no-op is worse than no mutex,
because nobody inside a single Crawler can observe the collision.

| The mechanism | How it goes quiet | What now happens |
|---|---|---|
| the resource lock | a worktree-relative lock root gives N trees N locks | refused at config load |
| the stop gate | `sed`/`grep` over JSON stops matching after a field-order change | parsed as JSON; an unrecognised shape refuses |
| the br adapter | an unparseable response reads as an empty graph | refuses, naming the command and the output |
| a claim | its owner dies and the leaf never returns to `ready` | pid + boot token; `reap` reclaims loudly |
| the close gate | any non-empty file satisfies `[ -s "$proof" ]` | proof must postdate the claim, and is recorded |
| a check comparison | a failure with no parseable lines looks like no failure | marked `exit-code-only`, reported as degraded |
| the collision estimate | an unestimable leaf looks like one that collides with nothing | treated as colliding with everything, reason printed |
| lane routing | an unmatched leaf silently takes the default | defaults conservatively **and says the rule is missing** |
| a fail-open hook | an allow nobody is told about looks like a guard that ran and was content | prints `ALLOWED WITHOUT BEING CHECKED` |
| the waiting probe | an empty queue and a wedged run both stop producing output | `waiting` distinguishes them, and its BLOCKED lines go to stderr with a distinct exit code |
| **showrunner itself** | a party that never registered leaves every showrunner guard correctly idle | `whoami` on both session seams, and `dispatch guard` on the path the bypass actually uses |

The last row is the uncomfortable one: it took a real run to see, and it was the only row where
the answer had to be built rather than tightened.

---

## Adoption is a design surface, not a documentation problem

**The failure.** In a 16-hour unattended run in a consumer repo that had *both* harnesses
installed and wired, one orchestrator dispatched 42 worker sessions. Every one used game_loop.
None used showrunner. The same orchestrator had run a showrunner campaign in that repo the week
before — 38 leaves closed, the campaign still on disk — and still hand-rolled `git worktree add`
isolation, hand-diffed file lists to sequence waves, and collected results from `.out` files.

It was not a preference. Four causes, and only the first is showrunner's:

1. **game_loop owns `SessionStart` and `PostCompact`; showrunner owns neither.** Every session
   game_loop guards is *greeted* by it without choosing it, and re-greeted after every compaction.
   Nothing in showrunner fires at a session boundary, so each compaction refreshed one tool and
   eroded the other. Adoption decays at exactly the rate context does.
2. **Both showrunner hooks read showrunner's own state.** `worktree-guard` denies when a lease is
   held by a different live session; `inert-crawler-gate` refuses when `waiting` reports a live,
   inert Crawler. A lane that never registered holds no lease and is no Crawler, so both hooks
   correctly exit 0 — and 42 unregistered lanes, five of them dying at a background-task ceiling
   and two landing on one branch, produced a journal that read as a healthy campaign throughout.
3. **The consumer's own seat was declared in a writable file.** A local prototype gated two
   org-chart guards on a one-word `seat` file. It said `worker`, written mid-run, so both exited 0
   for sixteen hours and nothing announced it.
4. **The consumer's dispatch guard matched the wrong tool.** Its PreToolUse matcher was `Agent`
   while every real dispatch went out as Bash `nohup claude -p`. The guard built to stop flat
   dispatch was blind to the mechanism used, 42 consecutive times.

**What this means for the design.** Enforcement-in-artifacts has to apply to a tool's *own entry
point*, not only to the work it guards. The test showrunner already applies to everything else —
*if the agent ignored every instruction, would this still hold?* — was never applied to "is
showrunner reached for at all", and the answer is no. A guard whose enabling condition is "you
already adopted me" is not a guard.

**What shipped (#36, #37, #40).** `whoami`, registered on `SessionStart` **and** `PostCompact`, prints three things: the seat this session holds, the stack that seat uses, and
what that seat may not do. With two properties that the prototype got wrong first:

- **The seat is derived, never declared.** A linked worktree is a `crawler`, and `campaign.json`
  names its leaf. The main checkout of a repo carrying a campaign is the `orchestrator`. No
  campaign is `solo`, said out loud rather than implied. There is no writable state that changes
  the answer, which is the entire point of cause 3.
- **The announcement carries the content, not a pointer to it.** Telling a session to go read a
  file is the same bet that just lost.

And a companion PreToolUse guard on **Bash** refusing raw `claude -p` from the orchestrator seat,
because the competing path is one line and needs to cost something. **No env-var override**: an
override the agent can set is cause 3 again, and it gets set at the moment the agent is most stuck.
The refusal prints the two legitimate routes instead — `spawn --launch` for work, the Agent tool
for read-only fan-out — both cheaper than what was refused.

**Stated limit.** Hooks are read at session start, so registering this cannot protect the session
that registers it. `doctor` reports the registration separately for the same reason it does for the
worktree-guard shim.

**Half of that was inert, and the half that was not is the more interesting story.** `_resolved`
used to say it in its own docstring — *"Assignment has no writer yet"* — so every role declaring
`assign` fell through to the fallback. With a deny-everything fallback that is not a safe default:
a Crawler ran write-denied INSIDE the worktree `spawn` had just made for it, and an audit leaf
finished only by routing its evidence around the guard with shell redirection. A guard whose reward
for holding is a workaround teaches every later session to route around it.

The assignment had been written all along. `spawn` records the tree's leaf before the session
exists, keyed to its worktree, which is what `assign` was specified to mean — so `seat_roles` reads
back a fact showrunner already kept rather than inventing a second source of truth. Deliberately
asymmetric: only a worktree the campaign record NAMES resolves, or `git worktree add` becomes a way
to grant yourself a role, and `orchestrator` ships unmapped because standing in the main checkout is
a location, not a record.

`roles.claim()` had no caller either, so BOTH declared modes were unreachable and the whole role
model resolved to one seat on a stock install. `role claim` / `role release` / `role roster` expose
it, and `claim` refuses a role declaring `assign`: that one is decided by whoever created the
session, so taking it here would be self-nomination into a seat the model says cannot be
self-nominated.

The liveness that made it a lock is also what made the first version of the verb useless. A pid
plus a boot token means a claim keyed to the short-lived process that MADE the call reports success
and reads STALE the instant that call returns — so the claimer is told it holds a seat while the
resolver skips it and `whoami` announces the fallback. `util.session_pid` already resolved the
long-lived session and already returned a `basis` separating "proved it" from "could not tell";
`lease` already consumed it and refused when nothing resolved; `lock acquire` already printed the
warning. The roles path shared `locks.Lock` with all three and inherited none of the mitigation. It
does now, and `role roster` surfaces STALE, because the only way to see a dead claim used to be
calling `roles.roster()` from Python (#42, #48).

**And the identity layers have nowhere to live.** `FIELDS` covers the mechanism — acquire, capacity,
reports_to, may_create, writes, notes — and an unknown key warns rather than rejecting, so nothing
breaks. But there is no per-role `description` and no top-level `charter`: prose true of *every* role,
announced to all of them. A consumer's standing mandate is exactly that shape, and theirs survived
nine days only because each compaction summary happened to re-quote it. One summary that compressed
it away and it was gone with nothing able to notice — the same class of failure this section exists
for, one level up (#42).

---

## Isolation is per-resource; a worktree is not a boundary

A worktree isolates **tracked files** and nothing else. Anything resolved from an absolute path,
or from a hook's own script location, stays shared — so the audit at spawn enumerates what a
Crawler actually gets rather than letting "it has its own worktree" stand in for independence.

**Corrected on re-reading the harness (2026-08).** showrunner originally recorded, from issue #13,
that a commit made inside a worktree is gated on the **main checkout's** verification record. That
is no longer true of current game_loop and the claim is retracted: `guard-writes-impl.sh` resolves
the commit gate from the tree the commit *targets*, reading the `git commit`'s `-C`/cwd, and when
that resolves to a different tree nested in the project it uses *that* tree's `.game_loop`. If the
target tree carries no harness it **denies**, on the stated grounds that reading another tree's
record would answer a question about files this commit does not contain.

Worth recording as more than a footnote, because it is premise verification turned on its author:
showrunner shipped a paragraph asserting a harness behaviour the harness had already fixed, and
only re-reading the source caught it. An orchestrator carrying a stale model of the layer below
briefs every Crawler with it.

**What the fix hands back** is a concrete constraint: each Crawler worktree must carry its own
harness or its first commit is denied — and `git worktree add` copies tracked files only, so a
gitignored harness directory never crosses. That is the secret-injection problem (#10) with the
harness as the missing file, equally invisible at spawn. `worktree.harness_gap()` detects it at
`spawn` and in `doctor`.

The same tracked-vs-ignored split governs the guard shim itself, and there is no third state:
commit it and git carries it, or keep `.showrunner/` out of history and `spawn` provisions it. A
shim that is neither cannot be carried *or* provisioned — copying it would hand the Crawler a file
its own `git add -A` commits onto its branch, colliding with the same untracked path in the main
checkout — so `doctor` names which arrangement you are in, and warns when you are in neither.

**One session per worktree.** A worktree had an owner on paper in `campaign.json` and no holder in
fact, and nothing consulted that record when a second session opened the directory and started
editing. So a lease's holder is a **live process**, and `pid_basis` records how that pid was
learned, because a hook is handed no PID and the answer comes from walking an ancestry: a lease
resting on `ppid-fallback` is worth less than one resting on `ancestor-claude`. `enter` never
blocks — a SessionStart hook cannot — so `enter` is where the prompt lives and `guard` is where the
enforcement lives. It denies on exactly one condition: HELD by a different live session. FREE,
STALE and UNREADABLE all allow, and none is an oversight — a stale holder is proved dead, and an
unreadable one cannot be adjudicated, so refusing on it would wedge a tree on a partial write.

The general lesson stands unchanged: **isolation has to be reasoned about per-resource, not granted
wholesale by the worktree** — and the per-resource answers legitimately differ, since a harness may
scope one thing to the session (the edited-file set: one session is one session however many trees
it touches) and another to the tree (what a change owes).

---

## A blocked Crawler needs a message, not time

The state that most needs a human is the one every other signal reads as healthy: a Crawler
refused at its own turn-end is **alive and inert**. `reap` correctly proposes nothing, the pid is
live, the exit code is 0, and the artifacts on disk look finished. One sat that way for 44 minutes.

Three adjacent mechanisms all miss it, which is why it needed its own gate:

| | why it does not cover this |
|---|---|
| the harness watchdog | fires on *idle*, which is after the orchestrator already stopped |
| the harness Stop gate | refuses a turn-end that asks a question or claims to be continuing; "I am done for now, here is what is next" is neither |
| a stall gate | asks "did anything move this turn"; a turn that read files and edited tickets answers yes and still leaves a Crawler inert |

The unmet question is narrower than all three: **is somebody waiting on a message from me.**
`waiting` already computed it and nothing spent it at the one moment that decides anything, so
`.showrunner/hooks/inert-crawler-gate.sh` refuses the orchestrator's turn-end while that is true.

Two further lessons came from wiring that gate at all — both from making a documented gate
*reachable*:

- **Read `--porcelain`, not the prose.** The obvious spelling is `waiting | grep BLOCKED`, and it
  cannot work: the BLOCKED lines go to **stderr**, and `waiting` exits non-zero precisely when it
  is *not* waiting, which is the state a blocked Crawler produces. The natural script discards the
  channel the finding is on and then reads the exit code as "cannot see". Both halves fail toward
  silence.
- **`stop-gate --leaf <id>` — the scope is load-bearing.** Unscoped, the gate asks "is *any* leaf
  open in this campaign", and since the same gate is written into every Crawler's triggers, with N
  dispatched, N−1 are structurally guaranteed to be refused at least once — each advised to close
  work in worktrees it cannot reach, and a headless Crawler has no next turn in which to act on it.

Jurisdiction is the other half: the gate does not fire inside a Crawler's own worktree, because a
Crawler has no channel to its siblings and no authority to reap them, and a gate demanding an
action its subject cannot take is worse than no gate.

---

## Premise verification is the highest-leverage line in a brief

Over one run of 14 issues, three had premises that did not survive contact with the codebase: a
failure not live in that repo, tooling asserted to exist that did not, and a command from an
entirely different harness. In a later batch of seven, all seven held. **The rate is not the
point** — you cannot tell which batch you are in from the inside, and the two look identical until
you read the source.

Why this is *showrunner's* problem specifically: a Crawler that quietly implements a fix for a bug
that is not there is **indistinguishable** from one that did the work, and the proof-of-done gate
is satisfied because a real artifact really was produced. The gate checks that work happened, not
that it was needed. And fan-out makes it worse rather than better — one agent working a queue
serially starts noticing that issue 9 contradicts what it read for issue 3; N isolated agents each
see one issue and cannot notice anything.

Hence `--premise` and `--premise-read` are **required arguments of the close**, and `--refuted` is
a first-class successful outcome. If the only available shapes are done and failed, the incentive
is to build something.

Two smaller pieces of the same argument:

- **`--finding` in a brief.** Where the orchestrator has already checked a premise, the brief
  carries the finding *and its evidence* and asks the Crawler to confirm or refute rather than to
  trust. An independent confirmation with line numbers is worth strictly more than either reading.
- **`edit` exists because the body IS the brief.** A leaf's body is interpolated into the document
  a Crawler works from, so a wrong body dispatches a wrong task — and before `edit` the only exit
  was closing the leaf, which spends the proof-of-done gate on a decision that never happened. It
  refuses on a leaf that is not open, so it cannot rewrite instructions under a working Crawler.

---

## Observability: snapshot first, then watch

An event saying `leaf.closed` is not a picture, it is a delta against one. So `snapshot` returns
the whole world in one call at one instant — ready leaves, in-progress leaves, every Crawler with
its verdict, every resource with its holder, the waiting verdict — and `watch --since <cursor>`
returns exactly what is not already reflected in it. Assembling the same thing from
`status` + `reconcile` + `waiting` + `plan` costs four round trips and yields a composite of
instants that never co-existed. It is **not** a transaction; what it removes is the four-call
window, not the milliseconds.

Decisions worth keeping:

- **The cursor names its instance.** A seq counts within one journal, and several showrunners in
  several repos is the ordinary case, so a bare integer crossing that boundary is a confident
  answer about a different campaign — both sides integers, the comparison succeeding, nothing able
  to notice. `--since` refuses a cursor from a different showrunner.
- **The cursor is the consumer's.** `watch` keeps no durable position. Never record one as proof of
  delivery before the sink has taken the frames; that turns at-least-once into at-most-once,
  silently.
- **Frames carry their own type**, because three kinds of nothing look alike: `ready` ends the
  replay so attaching is never a blank screen, `heartbeat` proves a sparse stream is alive (an
  orchestrator can integrate for twenty minutes writing no event), and `bye` marks a clean end — a
  stream that simply stops did not end, it broke.
- **`crawler.blocked` is journalled on the transition, not the state.** `reconcile` recomputes it
  every call, so a poller would otherwise get one identical line per poll.
- **`lock.refused` is an event.** Without it a contended resource and an idle one draw the same
  picture, and there is no way to render a queue. `lock.reclaimed` is a reaper taking a resource
  off a dead holder — not the holder letting go.
- **Freshness is reported, not assumed.** `reconcile` opens with `checked`, `last re-check` and
  `next re-check`; `snapshot` carries the same as `follow_up`. A reading taken thirty seconds ago
  is otherwise indistinguishable from one taken yesterday. And `next re-check` names a **trigger,
  not a time**, because the watchdog fires on idle and publishes no interval — a clock time there
  would be a number showrunner invented about an event it does not schedule. With no probe armed
  it says `NONE SCHEDULED` and why.

Consumers should ask the verbs, not read `.showrunner/events.jsonl`: the file's name and layout are
showrunner's business, and a consumer reaching past the verb breaks the next time either changes.

---

## The work graph, and why it is vendored

`br ready` was originally described as the only work-discovery entrypoint, which made a Rust
toolchain and a separate tracker load-bearing for every loop iteration. The layer below reaches as
far as it does substantially because it has **no install step worth the name**, and showrunner
inherits that audience; every dependency is a place adoption stops, and this one sat in front of
the *first* command.

So: a minimal graph over Python's built-in `sqlite3`, plus a `br` adapter preferred when br is
genuinely present. Both sit behind one `Graph` interface and nothing else learns which backend it
got (`lib/showrunner/graph.py`).

Two capabilities the vendored backend has that a general tracker does not, because an orchestrator
needs them:

- **Claims carry liveness** — pid, boot token, worktree, session.
- **`refuted` is a terminal state**, distinct from `closed`.

The br adapter cannot answer `stale_claims()`, because br records no liveness on a claim, so it
**raises rather than returning an empty list**. `[]` would read as "nothing is stale", the one
answer that must never be produced by not knowing.

**Field data, 2026-08.** br 0.2.18 was installed on a consumer machine and its CLI shape confirmed
first-hand: `br ready` / `blocked` / `coordination status` / `scheduler`, JSON everywhere, and a
close policy that refuses `update --status closed` so closes carry a reason. It is a good tracker.
It also left **four claims sitting `in_progress` for a month** in that repo with nothing able to
tell they were dead — which is the `stale_claims()` gap in the field rather than in theory. That
consumer has since removed br and pinned the vendored backend. The adapter stays: br is a
reasonable choice for a human-facing tracker, and the honest framing is that **choosing br costs
you `reap`**, stated at the point of choice rather than discovered a month later.

---

## Distribution: one copy of the code, every project's own config

Per-project install stays the default and the documented path. Central is opt-in, one flag, and
reversible in both directions. Under `--central`, `.showrunner/bin/showrunner` stops being the tool
and becomes ~20 lines of bash that exec a shared copy — machine-agnostic and byte-identical in
every project, so a consumer who commits it anyway commits nothing about their machine.

**Only the code is shared.** There is no `SHOWRUNNER_HOME` and none is needed: `config.load`
resolves the project from the cwd's git root, so the central binary run inside a consumer repo — or
one of its worktrees — reads *that* repo's config, graph and locks.

**`--version` answers with a commit, and resolves it from where the CODE lives, never from the
cwd.** Under a central install the cwd is some consumer project, so answering from it would report
that project's HEAD as showrunner's version. Three provenances, and only two can name a commit:
**pinned** (extracted from a git ref; the strongest, and why central mode can say what every
project on the machine runs), **checkout** (reports HEAD, and whether the tree is dirty, because
uncommitted edits make that sha an overstatement), and **copy** (no commit names this code, and
none is invented — that absence is the argument for pinning). `__version__` alone was `0.1.0` for
the project's whole life and could not distinguish a checkout from this morning from an install
three weeks stale.

**When central is missing the fail posture splits.** Hook verbs exit 0 and *say* `ALLOWED WITHOUT
BEING CHECKED`; every other verb exits 1 naming the populate command. What pinning does **not**
establish: `VERSION` names what was extracted, not what is there now. Nothing hashes the central
directory afterwards, so one bad pin reaches every project on the machine at once.

---

## Checks: no new failures, never "all green"

A repo with pre-existing failures cannot satisfy "all green", so that version of the gate gets
switched off on contact with a real codebase. `baseline` records, `check` compares, and a check
that fails without producing parseable failure lines degrades to exit-code granularity **and says
so** — reduced resolution must never read as a clean comparison.

`integration-commit` exists because a provenance check keyed to "did *you* edit this?" answers no
for every integration commit, correctly: Crawlers wrote the files, `git merge` brought them in. The
useful question is different — does the staged set match the union of what the merged Crawlers
edited? — and it catches the real failure: a file in an integration commit that no Crawler touched.

---

## Several orchestrators at once

Supported, and the discipline is one line: do not read `ready` and claim its first entry, because
everyone gets the same list. `claim --next` takes any free leaf atomically and exits 1 when dry.
Losing a claim race is not an error; a sibling got there first. Integration and single-consumer
resources stay exclusive and **refuse rather than queue**, so a refusal there means "someone else
is mid-merge", not "something broke".

**Do not fan out past what `plan` allows.** Two leaves can be mutually unblocked and still be the
same edit: the graph models dependencies, not files. A false collision costs one wave of latency; a
missed one costs a merge conflict in an unattended run with nobody watching.

---

## Validated primitives

`test/run.py` — 980 assertions, Python 3 + git, no other setup. Assertions needing `br` or `tmux`
skip loudly, naming the dependency. `prototype/` holds the original shell POC (7 assertions run
anywhere, 5 skip), kept for the record; `prototype/br_gate.sh` is superseded by
`lib/showrunner/gates.py`, which parses the graph as JSON instead of splitting records on a literal
`},{` — a shape change there turned the stop gate into a no-op that reported success (#6).

---

## Still open

- **A role's identity layers are unmodelled.** Both acquisition modes are reachable now — `assign`
  through `seat_roles`, `claim` through the `role` verb — so what remains of #42 is not the
  mechanism but the prose: there is no per-role `description` and no top-level `charter`.
  The layer that says *who you are* before *what you may not do* still has to be folded into
  `notes` by the consumer, duplicated per role (#42).
- **Lock completeness / rung.** The PreToolUse guard is only as good as its verb classifier; a
  rogue raw command escapes it. Rung-1 IMPOSSIBLE needs the lock *inside* the consumer tool as
  well as in the guard. `lock run` is the belt, the guard is the suspenders.
- **Lock fairness.** `acquire --wait` polls; there is no queue, so a starved waiter can lose
  repeatedly. Said out loud in `locks.py`, because a fairness property nobody states is one
  somebody will assume.
- **Blast-radius estimation is a heuristic.** It reads paths named in the issue plus files
  mentioning the issue's symbols, is deliberately conservative, and will over-serialize on
  prose-heavy issues — it does exactly that on this repo's own issue list. The fix is configuring
  shared surfaces, not loosening the estimate.
- **Relevance of a proof artifact is unknowable by a string check.** The gate checks existence and
  freshness and records the proof for a reviewer. The boundary is stated in the gate itself.
- **`spawn --launch` requires a chat room and nothing enforces that the room is read.** A Crawler
  refused at its turn-end stays alive and inert, and a message is the only thing that restarts it —
  so the room is not optional. Whether anyone is *listening* is still a matter of habit.

---

## Theme

Dungeon Crawler Carl. The crawl is a produced show: **Crawlers** run the rooms, `game_loop` keeps
each one alive, the graph is the quest log, and the **showrunner** runs the whole production.
