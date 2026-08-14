# showrunner

**A multi-agent orchestrator for Claude Code — the one who runs the whole crawl.**

If [`game_loop`](https://github.com/SupposedlySam/game_loop) keeps a single Crawler alive, honest,
and safe through one unattended session, `showrunner` runs the *show*: it decomposes the quest,
sends a party of Crawlers into separate rooms in parallel, enforces the party-wide rules that no
single Crawler can see, and keeps the campaign coherent across sessions.

> Status: **implemented and self-hosting.** The orchestration loop is real code
> ([`lib/showrunner/`](lib/showrunner/)), it installs in one line with no packages, and it has been
> run against its own issue list — see [Dogfooding](#dogfooding-showrunner-on-its-own-issues).
> `python3 test/run.py` → **395 assertions, no setup beyond Python 3 and git.**

## Requirements

- **Python 3.7+** (standard library only — nothing to install)
- **git**
- **[Claude Code](https://claude.com/claude-code)** for the Crawlers themselves

`br` and `tmux` are **optional**. Everything showrunner guarantees works without them.

## Install

```bash
git clone https://github.com/SupposedlySam/showrunner.git
cd showrunner
./install.sh /path/to/your/project
```

Then, in your project:

```bash
$EDITOR .showrunner/config.json      # resources, lanes, inject, checks
./.showrunner/bin/showrunner doctor  # refuses configs that would degrade silently
./.showrunner/bin/showrunner baseline
```

`showrunner` is the project-local binary `./.showrunner/bin/showrunner` — not a global command.

## The cast

| Piece | Role |
|-------|------|
| **`showrunner`** (this repo) | The orchestrator. Owns the work-graph, routes work to lanes, holds party-wide locks, spawns + monitors Crawlers, integrates their results. |
| **`game_loop`** (optional) | Per-Crawler survival rails — autonomy + safety + honesty, enforced by Claude Code hooks. Independently useful for solo runs. |
| **`br`** (optional) | An existing beads work-graph. showrunner ships its own; if you already run `br`, it defers to yours. |
| **Crawler** | One Claude Code agent in its own git worktree, with its own scratch dir and its own brief. |

Dependency arrow points one way: **`showrunner` → `game_loop` + `br`.** Neither knows showrunner exists.

## Why it's a separate thing from game_loop

Two different axes:

- **`game_loop` is vertical** — depth and integrity *within one session*. Its power is its narrowness
  ("enforcement lives in tools, never instructions"; "name a real file"). Bloating it with a tracker
  or an orchestrator would wreck that.
- **`showrunner` is horizontal** — breadth *across work and agents*: a dependency graph, parallel
  lanes, shared locks, resume. That's a different job, so it's a different repo.

## The one hard rule it exists to enforce

Parallelism has a serialization point that no single Crawler can enforce, because none of them can see
the others: **shared, single-consumer resources** (a device, a deploy target, a bound port). showrunner
owns the **cross-process lock** that makes "only one Crawler in the boss room at a time" *physically
true*, not a wish in a prompt.

```bash
showrunner lock run device --holder crawler-a -- ./deploy.sh
```

The holder is a **live PID** — the consuming process itself. A dead holder is stale and reclaimable; a
holder recorded on a previous boot cannot possibly still be running. Named resources are independent,
so unrelated work never queues behind unrelated work, and the lock root is **one absolute path shared
by every worktree**, validated at config load. A worktree-relative lock root is refused outright,
because N worktrees would get N sibling lock directories and the mutex would silently do nothing —
which is the worst available failure, since it looks like it is working.

> **What the lock does not cover**, stated in the guard itself: `lock guard` is a verb matcher, and a
> rogue raw command that matches no pattern escapes it. Routing and guarding are optimisations; the
> lock is the guarantee only where the *consumer* takes it. Belt and suspenders.

## The loop

```
showrunner ready                # the only work-discovery entrypoint: unblocked, UNCLAIMED leaves
  └─ showrunner plan            # group them into waves whose file sets do not overlap
  └─ for each leaf in a wave:
       showrunner spawn <leaf> --launch
                                # worktree INSIDE the repo + private scratch + injected secrets
                                #   + a brief that demands the premise be verified first,
                                #   then STARTS a real session in it on the lane's model.
                                #   Omit --launch to prepare the room and start it yourself.
       Crawler works under game_loop (kept alive, honest, safe)
       showrunner close <leaf> --proof <real artifact> --premise <verdict> --premise-read <file>
  └─ showrunner integrate       # serial merge, checks re-run on each MERGED result
  └─ showrunner reap            # reclaim what dead Crawlers left claimed
     repeat until ready is dry
```

The mechanics that carry the weight, each with the failure it exists to prevent. (This said "six"
and there have not been six for a long time — an ungated number in prose, which is the same thing
the boundary doc's stamp exists to stop and the third one this repo has had to correct.)

**Proof-of-done gate.** A leaf cannot close unless it cites a real, non-empty artifact that is *newer
than the claim* — an artifact older than the work is evidence about something else. The proof is
recorded on the close, because existence is checkable and *relevance is not*, and the gate says so.

**Premise verification.** `--premise` is a required argument, not an aside. Over one real 14-issue
run, **three issues had premises that did not survive contact with the codebase**. A Crawler that
fixes a bug that is not there is indistinguishable from one that did the work — same commit, same
green tests, same satisfied gate — because the gate checks that work *happened*, not that it was
*needed*. `--refuted` is a first-class successful outcome, distinct from done and from failed.

**Dependency-gated fan-out.** Blocked work is hidden, so a shared prerequisite gates everything behind
it; the moment it closes, its dependents become ready and fan out together.

**Each Crawler is a real session, not a subagent.** `showrunner spawn <leaf> --launch` starts
`claude -p` inside the worktree, on the model that leaf's lane declares. A subagent would be the
easier thing to build and the wrong one: game_loop's rails are Claude Code hooks registered per
project, its usage park and watchdog key on a session, and the model it observes is read from a
session transcript. A Crawler that is not a session has no commit gate of its own, no Stop gate,
and nothing to reap when it dies — which is the entire point of sending one somewhere unattended.
The worktree carries the hooks because `.claude/settings.json` is tracked and `git worktree add`
copies tracked files.

Without `--launch`, spawn prepares the room and stops, and you start the agent yourself. **That
used to be the only behaviour, and this section did not say so** — it said showrunner "sends a
party of Crawlers into separate rooms in parallel", and the documented workflow ran
`plan → route → spawn → integrate` with no step that started anything. A reader concluded spawn
launched them, which is the correct reading of what was written and not of what ran. A limitation
nobody wrote down is one the reader has to discover by being wrong.

**A finished Crawler spins itself down.** Closing a leaf marks its Crawler finished and
closes its chat room immediately — both safe, because the leaf is already closed. The
*process* is deliberately left alone at that moment: a Crawler closes its own leaf from inside
its own session, so it is mid-call right then, and terminating it would truncate the work it
just certified. `showrunner reap` takes a process that is still alive well after its leaf
closed (SIGTERM, never SIGKILL), and closes rooms belonging to Crawlers that died without
closing anything. Under repeated fan-out those two leaks — a stacking process and a room per
dead Crawler — are what fill a machine and make a channel list unreadable.

**A Crawler can also stop without dying, and that is the harder one.** Its turn-end gate refuses
while its leaf is still open — correct, and the reason it no longer exits with work unfinished —
but a headless session has nothing to deliver "go back to work" to itself. It stays alive and
inert, and every signal reads healthy: a live pid, an open leaf, a report already on disk, `reap`
correctly proposing nothing. `showrunner reconcile` reports those as **BLOCKED**, ranked above
LIVE precisely because they *are* live. One sat that way for 44 minutes here and then woke,
reported and closed correctly the moment a message reached it. Making the old failure loud
created a quieter one; this is what reads it back.

**Talking to a running Crawler is unbundled — and under `--launch`, not optional.** Clone a chat
tool and point showrunner at it; nothing is vendored and no package manager is assumed:

```bash
git clone https://github.com/SupposedlySam/llm_chat.git ../llm_chat
```

```jsonc
// .showrunner/config.json  (or config.local.json for paths only your machine has)
"dispatch": {
  "chat": {
    "enabled": true,
    "cli":       "../llm_chat/bin/llm_chat",
    "installer": "../llm_chat/install.sh"
  }
}
```

`spawn --launch` then opens a room per Crawler, installs the tool into its worktree, and tells
it in its brief to join and to ask rather than guess.

**This said "leave `chat` out entirely and dispatch works exactly the same, minus the
conversation", and that was wrong.** A Crawler whose turn-end is refused by the stop gate stays
alive and inert until something external prompts it, and the room is the only thing that can.
One sat for 44 minutes — live pid, open leaf, report already on disk — and woke, reported and
closed correctly the moment a message arrived. So with `chat` disabled, "inert until rung"
becomes "inert", and every signal still reads healthy: `waiting` returns 0, the watchdog stays
quiet, `reap` correctly proposes nothing. Chat is load-bearing for **correctness** under
`--launch`, not a convenience. Without it, prefer `spawn` without `--launch` and drive the
Crawlers yourself.

**`.showrunner/config.local.json` is an untracked overlay** merged over `config.json` by
top-level key. Machine-specific values — an absolute path only you have — belong there, not in
the file that ships to every clone.

**The model is declared, observed, and compared — never enforced.** A lane names a model,
`spawn --launch` passes it, game_loop records what actually ran, and `showrunner reconcile`
reports a mismatch or a mid-run fallback. Nothing blocks: an Opus-priced Crawler doing Sonnet work
produces perfectly good output, which is exactly why nothing else notices and why it can run for a
week. A missing observation reads as UNKNOWN rather than as agreement.

**Collision prediction.** The graph answers "what is unblocked?" — a question about *dependencies*. It
models nothing about what two agents will *touch*. `showrunner plan` estimates each leaf's blast
radius and refuses to fan out two leaves whose estimates intersect. The estimate does not need to be
good, only conservative: a false collision costs one wave of latency, a missed one costs a merge
conflict in an unattended run with nobody watching. Shared surfaces (the one test file every change
touches) are configured as such and owed to serialized integration instead of blocking parallelism.

**Claims carry liveness.** A claim records the owning PID and a boot token. Without that, a Crawler
that dies leaves its leaf claimed forever — `ready` means unblocked *and unclaimed*, so the work
silently leaves the queue, `ready` goes dry, and the loop terminates **reporting success** on work
nobody did. `showrunner reap` reclaims loudly and is dry-run by default; an abandoned worktree may
hold the only copy of real work, so it is surfaced, never deleted. A Crawler *parked* at a usage limit
is not dead and its claim survives.

**Integration means green on the merged result.** Green on a branch is evidence about a trunk that no
longer exists once the second branch lands — two Crawlers adding an entry to the same dispatch table
touch different lines and still break the trunk. Merges are serial, checks re-run after each, and the
run stops and rewinds on the first failure rather than stacking onto a broken trunk. The criterion is
**no *new* failures versus a recorded baseline**, never "all green": a repo with pre-existing failures
cannot satisfy "all green", so that version of the gate gets switched off on contact with reality.

## Lanes

| Lane | Runs | Parallel? |
|------|------|-----------|
| **Headless** | tests, backend, pure logic, analyze | ✅ one worktree Crawler per ready leaf, in collision-free waves |
| **Serialized** | anything touching a single-consumer resource | ❌ one at a time, behind the named lock |
| **Orchestration** | showrunner itself: graph, routing, review gates, integration | single |

Routing is config, and **conservative by default**: an unmatched leaf serializes and says so out loud.
The costs are not symmetric — a wrongly-headless leaf collides on a single-consumer resource hours in
with nobody watching; a wrongly-serialized leaf just runs slower. Every decision is logged with the
rule that produced it, so a wrong route is diagnosable after the fact rather than re-derived from
behaviour.

## What a Crawler actually gets

`showrunner spawn` creates all of it and refuses if any of it is unsafe:

- **A worktree inside the repo** (`.worktrees/<crawler>/`, gitignored by showrunner itself). The
  sibling-directory instinct is wrong: a Crawler's own write guard treats everything outside the
  project as read-only, so a sibling worktree is a workspace it is structurally forbidden to work in.
  Inside also means each Crawler gets its own copy of the harness — **a Crawler editing the guard can
  only brick itself**, not the whole party.
- **Its own scratch directory.** Two Crawlers in a real run both reached for `commitmsg.txt` in one
  shared temp dir; the second noticed only by luck. Had it not, one would have committed the other's
  commit message onto its own changes — a real commit, a plausible message, describing work it does
  not contain, every gate green. Crawlers are the same model solving similar tasks from similar
  prompts, so they converge on the same obvious filename far more often than independent actors would.
- **The gitignored files the build actually needs**, from an explicit configured list — symlinked
  where possible, added to the worktree's exclude file so `git add -A` cannot stage them, and verified
  after injection. A missing declared path **aborts the spawn**, because a Crawler that cannot reach a
  service will write the service up as broken in the same confident tone as a real finding.
- **An audit of what it still shares.** A worktree isolates tracked files and *nothing else* — not the
  harness's state directory, not lock paths, not caches, not anything resolved from an absolute path
  or from a hook's own script location. The brief names the shared-state case and says what to do
  instead, because `--no-verify` starts looking reasonable exactly when an agent is stuck under a
  mandate to finish.

## Integration commits and provenance

A provenance check that compares a commit's staged files against the set *this session* edited fires
on **every** integration commit an orchestrator makes, correctly by its own definition: the
integrating session never edits those files — Crawlers wrote them, in worktrees, and `git merge`
brought them in. Silence is the wrong fix; a warning that fires every time is one people learn to
scroll past, and then it stops working for the case it was built for.

```bash
showrunner integration-commit --crawler crawler-a --crawler crawler-b
```

The honest version is a **different question**: *does the staged set match the union of what the
merged Crawlers edited?* That is answerable, strictly more useful, and it catches the real
orchestration failure — a file appearing in an integration commit that **no Crawler ever touched**.

## Dogfooding: showrunner on its own issues

This repo's first 14 issues were loaded into showrunner's own graph and run through the loop. Two
things worth reporting because they are evidence rather than claims:

- **Dependency-gated fan-out fired for real.** The work-graph decision gated six issues; closing it
  released all six at once.
- **`showrunner plan` refused to parallelize its own issue list, and was right to.** Every one of the
  14 issues names the same three prototype scripts, so the estimates all intersect. That is precisely
  the finding the collision issue reported from doing it by hand — and the run says *why* it is
  serializing rather than looking like an unexplained slow run.

**The rounds after that are the more useful evidence, because they came from running it rather
than reading it.** A later batch of seven was filed by an agent working in a *consuming* repo, and
all seven premises held — a better rate than the first run's, where three of fourteen did not
survive contact with the code. Every one of the seven was something only a real `--launch` could
surface: a brief naming a binary that could not resolve from a worktree, a permission mode that
left a Crawler unable to run any command, a claim whose liveness named the shell that spawned it
rather than the session it launched.

The batch after *that* came from watching the fixes run, and two of them were caused by earlier
fixes of mine — a wired turn-end gate that turned a loud failure into a silent one, and a brief
instruction that cost the orchestrator one blocked turn-end per Crawler under fan-out. That is
the honest shape of dogfooding: the second-order defects only exist once the first-order ones are
gone, and nothing but running it finds them.

## Verifying it

```bash
python3 test/run.py            # 395 CORE assertions — Python 3 + git, nothing else
bash prototype/demo.sh         # the original shell POC: 7 run anywhere, 5 skip loudly
```

The CORE half runs green on a clean clone with **zero setup**. Assertions that genuinely need external
tooling (`br`, `tmux`) **skip loudly, naming the missing dependency**, rather than failing obscurely —
a repo claiming a passing suite should ship one a stranger can run.

## Prototype

[`prototype/`](prototype/) holds the original shell proof-of-concept, kept for the record. The
primitives it proved now live in [`lib/showrunner/`](lib/showrunner/); see
[`prototype/README.md`](prototype/README.md) for what changed in the lift and why.

## Generic, not project-specific

Like `game_loop`, showrunner is generic; a project's specifics (which resources are single-consumer,
which verbs are "serialized," the graph location, the owed checks) are **config, not code** — see
[`.showrunner/config.json`](.showrunner/config.json) for this repo's own.

## Running more than one orchestrator

A graph that survives sessions is a graph more than one agent will open — several Claude Code
sessions driving one build is a supported shape, not an accident. The state showrunner shares
between them is protected, and it was measured before it was fixed:

| Shared state | The race | Now |
|---|---|---|
| a leaf claim | check-then-write: **6 of 12** concurrent claims won the same leaf | one conditional `UPDATE`; measured 1 of 12 |
| the campaign record | read-modify-write: **3 of 10** spawns survived | `flock` + write-then-rename; 10 of 10 |
| the main checkout | two `integrate` runs rewinding each other | exclusive, and it **refuses** rather than queueing |

Take work with the primitive built for it, not by reading `ready` and claiming the first entry —
`ready` hands the same list to everyone who asks:

```bash
showrunner claim --next --actor crawler-a     # atomically take ANY free leaf; exit 1 when dry
```

Losing a race there is not an error: it means a sibling got there first, which is the system
working. Eight concurrent orchestrators against eight leaves claim eight distinct leaves and none
of them fails — asserted in the suite.

Two things stay deliberately single: **integration** (it merges, runs checks, and rewinds with
`git reset --hard`, so two at once would rewind each other's work) and any **single-consumer
resource** you have configured. Both refuse loudly instead of waiting silently, because a
multi-minute silent wait is indistinguishable from a hang.

`showrunner waiting` exits 0 while dispatched work has a **working** owner — a live PID that is
not blocked — or an explicit park. That is the recomputable fact an idle watchdog needs, since it
cannot see a subagent. A Crawler refused at a turn-end is live and is deliberately *not* counted:
it is doing nothing, only a message restarts it, and calling that "waiting" would silence the
watchdog on the one run that needs it. Those are reported separately, because somebody has to go
and prompt them.

**Arming that watchdog is a manual step, once per install.** `showrunner doctor` names it and
prints the exact line to paste, including the absolute path. It cannot do it for you: the verb
that would arm it is callable by the sessions being watched, and a probe that always exits 0
reads as "always waiting" — the watchdog switched off by the thing it watches. Until it is armed,
an orchestrator that has correctly dispatched a full wave looks exactly like one that fell
asleep, gets rung, and eventually pages you for a run that was behaving perfectly.

## What a Crawler's harness gets

A harness that resolves its commit gate **per tree** — the correct design, since what a change
owes is a fact about a tree — must refuse when the tree being committed carries no record. That
lands on the orchestrator, because `git worktree add` copies **tracked files only**: an untracked
harness never crosses, and the Crawler is denied its first commit.

The loud failure is the easy one. The quiet one is why `showrunner spawn` provisions the harness
itself rather than running its installer: an installer seeds user-owned files **only if absent**,
so a fresh install in a worktree yields a blank `verify.yaml` — **a commit gate that owes nothing
and reports success** — plus default invariants and default write roots. Nothing errors. The party
simply plays by two rule sets, and the weaker one is the one running unattended in N worktrees.

So showrunner prefers the harness's **own installer** when one is configured, because that is the
thing that knows how to provision a tree from a parent rather than from blank templates. Failing
that it copies the harness minus whatever it declares as runtime state — read from the harness's
*own* ignore file, because session state belongs to a session and must never be handed to a
Crawler.

It **never copies the hook-registration file.** That was the first version and it was wrong: the
installer *merges* its hooks, preserving the project's own statusLine, permissions and unrelated
hooks, and warning about a pre-existing non-harness hook on an event it manages — a stray Stop
hook from an older harness fights it over turn-ends and presents as "the orchestrator is
mysteriously flaky". A wholesale copy discards the settings and silently drops the warning.

And showrunner does **not** compare the rule files itself. It asks the harness, which answers
about its own trees: which files are rules, which are notes, and whether this tree matches its
parent. An earlier version hardcoded that list and was *already* drifting — it knew nothing of the
harness's notes tier, so a diverged ledger was invisible to it. A verdict of drifted or
undetermined aborts the spawn; "could not tell" and "matched" are never the same answer.

> This paragraph described the deleted design for weeks after it was deleted — copying the hook
> file, comparing rules here — which is the same stale-claim failure the boundary doc exists to
> catch, in the human-facing doc rather than the machine-facing one.

## Docs

- [`docs/DESIGN.md`](docs/DESIGN.md) — the design notes and what remains open.
- [`docs/BOUNDARY.md`](docs/BOUNDARY.md) — who owns what across showrunner and `game_loop`, the
  standing direction for cross-repo fixes, and what showrunner currently assumes about the layer
  below (with the line numbers it was verified against).
- [`llms.txt`](llms.txt) — the operational brief, if you are an agent.
