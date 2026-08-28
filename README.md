# showrunner

**A multi-agent orchestrator for Claude Code — the one who runs the whole crawl.**

If [`game_loop`](https://github.com/SupposedlySam/game_loop) keeps a single Crawler alive, honest,
and safe through one unattended session, `showrunner` runs the *show*: it decomposes the quest,
sends a party of Crawlers into separate rooms in parallel, enforces the party-wide rules that no
single Crawler can see, and keeps the campaign coherent across sessions.

> Status: **implemented and self-hosting.** The orchestration loop is real code
> ([`lib/showrunner/`](lib/showrunner/)), it installs in one line with no packages, and it has been
> run against its own issue list — see [Dogfooding](#dogfooding-showrunner-on-its-own-issues).
> `python3 test/run.py` → **no setup beyond Python 3 and git.** It prints its own count.

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
// ~/.config/showrunner/config.json — one chat tool serves every repo, so this belongs at
// USER level, not per-project. See the four layers below. ABSOLUTE paths here: a relative
// one resolves against whichever repo is asking, which is not one place.
"dispatch": {
  "chat": {
    "enabled": true,
    "cli":       "/abs/path/to/llm_chat/bin/llm_chat",
    "installer": "/abs/path/to/llm_chat/install.sh"
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

**A guard that fails open is COUNTED, not just announced.** When a guard cannot do its check —
no repo, unreadable config — it allows the call and prints a notice saying it did not check. That
was already true, and it was quiet anyway: the notice arrives beside a *successful* tool result,
which is the channel an agent mid-task skims. Rewording it louder would treat a delivery problem
as a copywriting problem. Every fail-open now appends to `.showrunner/fail-open.jsonl`, and
`doctor` reports how many calls went unchecked — a count is the fact a per-call banner cannot
carry, and `doctor` is read by somebody who has stopped to look. Both entrypoints record through
the same funnel, and an unparseable ledger reports UNKNOWN rather than none.

**The launch binary is configurable, and `doctor` resolves it.** `dispatch.claude_bin` defaults
to `claude` on PATH. On a machine whose only `claude` is bundled inside an editor extension —
not on PATH, no standalone install — every `spawn --launch` failed and the whole parallel lane
was unavailable, with nothing reporting it until a spawn had already created a worktree, a
branch and a claim. An unresolvable binary is now an ERROR from `doctor`, which is what that
verb is for.

**A launch that fails parks the leaf rather than stranding it.** `spawn` records first and
starts second, which is the right order; what was missing was the compensating action. A failed
start left the leaf `in_progress`, claimed by the invoking shell's pid — gone seconds later —
so it was out of `ready` and invisible to the only discovery surface. It is parked with the
launch error as its reason: it survives `reap`, stays visible, and its worktree is kept, because
that tree may hold the only copy of real work. Deliberately not a rollback — and deliberately
not a steer to `reap`, which was reported proposing to close a dozen chat rooms belonging to
another agent's Crawlers, sweeping far wider than the failure.

**Config is four layers, and only the middle one ships.** Each is overlaid on the one above:

| | |
|---|---|
| built-in defaults | the tool's own answer |
| `~/.config/showrunner/config.json` | **the user** — set once, applies to every repo on this machine |
| `.showrunner/config.json` | the project, tracked and shipped to every clone |
| `.showrunner/config.local.json` | **this machine**, untracked — an absolute path only you have |

Dicts merge key by key; **lists and scalars replace wholesale**, at every layer. So a
`dispatch.chat` you configured once at user level survives a project that sets only
`dispatch.default_model`, while a project's `lanes` replaces the user's entirely — half a lane
is a configuration nobody wrote. An empty value is a value: a project writing `"checks": []`
overrides a user-level list rather than inheriting it. `showrunner doctor` prints which
user-level file, if any, was merged, because a merged config cannot be asked where a value came
from.

**`doctor` also reports, per leaf key, which layer's value won and which was shadowed** —
naming the two files, and marking a shadowed *user-level* value distinctly rather than burying
it among `ok` lines. The limit: it reports at **leaf-value** granularity, not dict granularity.
A top-level key set in two layers with disjoint sub-keys — `dispatch.chat` at user level,
`dispatch.default_model` at project level, the exact pair above — is a merge, not a shadow, and
produces no line at all; only a dotted path whose own scalar-or-list value was actually replaced
by a lower layer counts as shadowed. It says nothing about a value that only ever existed at one
layer, and nothing about DEFAULTS, which is the tool's own answer rather than a file.

**The project beats the user — which is the opposite of `roles.json`,** the other file in that
same directory. The two are different kinds of thing: `roles.json` is *permission*, so user
level wins and a project may only add (a project that could redefine its own role would widen
the policy constraining the session editing it); `config.json` is *preference*, so the project
wins, because a repo is the better authority on its own lanes, checks and resources.

**Some keys are refused at user level**, loudly, naming the file: `project_name` (it feeds the
chat channel prefix and orchestrator identity — machine-wide, every repo would open rooms under
one prefix, a collision already measured here), `lock_root` (one absolute root shared by
unrelated repos makes them serialize against each other — a mutex that is quietly the wrong
one), and `graph.db` / `baseline`, which are one campaign's state and mean nothing machine-wide.

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

**...and it counts what is already running.** `plan` groups `ready` work, which is unblocked *and
unclaimed*, so a Crawler working right now is absent from the **input** and its files were never
considered occupied; `overlap` measures committed diffs, so a Crawler twenty minutes in with nothing
committed is not an in-flight branch by that definition — and a branch existing is not enough, since
it counts branches with commits. Between them that left the whole working life of a Crawler up to its
first commit invisible. `plan` now reports live claims **beside** its waves (the grouping itself is
unchanged, because "how would I group this if nothing were running" is a real question before a
campaign starts), and `showrunner spawn` refuses a leaf whose estimate collides with a live claim.
The refusal is overridden by naming what it overrides — `--despite-live <leaf>`, which must name
every colliding leaf — because a guard answered by a reflexive `--force` teaches every later session
to bypass it. It is an **estimate**, from declared paths and grepped symbols, and says so wherever it
is printed: `overlap` measures, this guesses about work that has produced nothing measurable yet.

**A base that is missing work the leaf depends on is REFUSED, not reported.** `spawn` cuts from
the primary checkout's HEAD unless told otherwise, and that default is invisible and
context-dependent: the identical command is right or wrong depending on where an unrelated checkout
happens to be pointing. Printing the base it used was the previous fix, and it was not enough — the
line printed *after* the worktree, branch, brief and claim existed, and under `--launch`, after the
Crawler was already running. Four Crawlers in one run were dispatched onto trees cut from an
unrelated branch; one caught it by hand and held, and the rest had no reason to look.

The failure is silent and plausible, which is what earns a refusal here. The worktree exists, the
branch exists, the code compiles, and every file the brief names is present — just older. A Crawler
that does not think to run `git log -1` finds the function it was sent to fix, finds it does not have
the problem described, and reports **premise refuted** with real evidence: every word true of the
tree it was given and false of the tree under review. That is the most expensive wrong answer this
tool can produce, because refuted is a legitimate close and reads as a successful run.

So `spawn` exits 3 before creating anything when a dependency's branch is definitely not an ancestor
of the base, overridden by `--despite-base <leaf>` naming which one — the same rule as
`--despite-live`. A dependency that *cannot* be checked stays a warning: refusing there would block
work on the strength of not having looked. `showrunner show <leaf>` reports `crawler_base` — what
was asked for, the resolved sha, the branch — which was recorded from the start and had no surface.

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

**showrunner greets its own sessions.** Its guards used to read only showrunner's own state, so a
session that never registered held no lease and was no Crawler — and both guards correctly exited
0 while it ran. In one 16-hour unattended run, 42 worker sessions were dispatched in a repo that
had showrunner installed, wired, and carrying a campaign with 38 leaves done, and not one went
through it. `showrunner whoami` fires on **SessionStart and PostCompact** and announces what this
session IS. The second seam is the point: a rule that survives only until the next compaction is a
rule for the first hour.

The seat is **derived, never declared** — a linked worktree is a CRAWLER, the main checkout of a
repo carrying a campaign is the ORCHESTRATOR, no campaign is SOLO, and UNKNOWN is a real answer
announced as one. A prototype of this idea kept the seat in a one-word file; the file said
`worker`, written mid-run, and both guards that read it exited 0 for the remaining 16 hours.

**The cheap dispatch path has a gate on it.** `spawn --launch` is the correct way to start a
Crawler; the competing path is one Bash line — a raw headless `claude` — which gets no worktree,
no lease, no claim a reaper can reclaim, no leaf-scoped stop gate and no room. `dispatch guard`
refuses it from a session whose role may not create one. Registered on **Bash**, which is the
mechanism actually used: an earlier version matched `Agent`, guarded the in-process subagent tool,
and reported nothing while 42 real dispatches went past it.

**Roles are yours; showrunner checks the shape.** It never learns what a role *means* — the way
lane rules already work. It knows two acquisition modes, `claim` (a session takes an open seat,
exclusive, with pid+boot liveness) and `assign` (written by whoever created the session), and it
refuses a configuration that cannot resolve: a dangling `reports_to`, a cycle, an org with no
root, nothing claimable at all, or a fallback role that may create something. Definitions live at
a user-level path, because an in-repo config is writable by the very session it constrains.

**A seat with no role is a guard that gets routed around.** `assign` had no reader, so every
Crawler resolved to the fallback and ran under the fallback's policy *inside the worktree `spawn`
had just made for it* — with a deny-everything fallback, an audit leaf finished only by routing
its evidence around the write guard with shell redirection. `seat_roles` maps a derived seat onto
one of your roles, `{"seat_roles": {"crawler": "worker"}}`, and the campaign record is the
assignment being read back: `spawn` names the tree's leaf before the session exists. User level
wins and a project may only map a seat the user left unmapped — one that could remap its own seat
would hand itself any role in the catalog. Only a worktree the record NAMES resolves, so `git
worktree add` grants nothing, and **`orchestrator` ships unmapped on purpose**: standing in the
main checkout is a location, not a record, and authority by location is the failure this seam
replaced. `doctor` refuses a seat mapped at a role nothing defines — that seat resolves to the
fallback, so one typo buys the whole write denial back and nothing else would have said so.

**Both acquisition modes had to become reachable before either worked.** `assign` had no reader;
`claim` had no writer — `roles.claim` was a library function nothing called, and the `claim` verb
claims a *leaf*. On a stock install every session got the fallback whatever its roles said.

```
showrunner role claim campaign-lead --who agent-a   # a role declaring acquire=claim
showrunner role roster                              # every seat, its state, its liveness basis
showrunner role release campaign-lead
```

A role declaring `assign` is refused here — its meaning is that whoever created the session
decided, so claiming it would be self-nomination into a seat the model says cannot be
self-nominated; `seat_roles` is how that one is obtained. **A claim's pid is discovered, not handed
over:** liveness is a pid plus a boot token, so a seat keyed to the short-lived process that made
the call reports success and reads STALE the instant that call returns, and `whoami` announces the
fallback again. `lock acquire` warns about exactly this; the roles path shared its mechanism and
not its mitigation. A pid that cannot be resolved is refused rather than recorded, because a claim
with no liveness is not a weaker claim — it is a lock nothing can ever reclaim.

**A guard cannot consume prose.** `whoami` emitted only prose, so a hook author needing the
resolved role had no way to ask and reimplemented the resolver — and a copy drifts. One did: when a
seat learned to resolve through `seat_roles` the copy did not, so the announcement said one role
while the guard still enforced the deny-everything fallback. `showrunner whoami --porcelain` emits
the seat, the resolved role, how it resolved, and the `writes`/`may_create`/`reports_to` a guard
enforces. `whoami` renders that same dict, so the two cannot disagree. Branch on `enforced`; a null
`role` is not "no restriction", and the porcelain exits non-zero if it could not resolve so a
parser can fail closed.

**A campaign is smaller than a repo.** The natural unit of a body of work is often a story, and
the handoff a showrunner charges is only paid for by the parallelism it buys — so several
campaigns in one checkout is the ordinary case. `SHOWRUNNER_CAMPAIGN` scopes graph, record, events
and scratch. **Locks deliberately do not follow it:** a lock names a physical resource shared by
the machine, so two campaigns flashing the same TV must serialize against each other.

**A run that could not measure anything is not a degraded comparison.** `check` already declined
to let reduced resolution read as a clean comparison; it now refuses to let *no* resolution read
as reduced. A suite that could not reach the world did not measure anything, and its failure count
carries no information — so a VOID run exits **3**, distinct from 2, because "your code broke" and
"nothing was measured" must not be the same number. Reported from a real run: 156 minutes, 43
failures, several hours of interpretation, and a dead router.

**showrunner runs a pinned copy of itself.** It develops itself, so its guards run the very code
being edited — and one syntax error under `lib/showrunner/` kills every verb at import, which left
the worktree guard exiting 1 with empty stdout: neither a refusal nor an announcement. Editing the
tool silently disarmed it. The hooks now resolve a gitignored `.showrunner_self` pin first, so the
plumbing runs code a mid-edit cannot break, and `doctor` says how far behind that pin has drifted.

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
python3 test/run.py            # CORE assertions — Python 3 + git, nothing else
python3 test/mutate.py         # which producers anything would NOTICE if they broke
python3 test/corpus.py         # the turn-end gates, measured against a real transcript
bash prototype/demo.sh         # the original shell POC; it prints its own pass/skip counts
```

`test/run.py` exits **3** — not 1 — when it did not run every group it defines. A suite that
skipped groups silently prints the same green line as one that ran them all, so "nothing ran"
and "something failed" are deliberately different codes.

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

## Watching a campaign while it runs

`status` and `reconcile` answer *what is true now*. Neither answers *what just happened*, and a
viewer that polls a snapshot and diffs it invents transitions it never saw and misses any pair
that cancels out between polls. So every state change is appended to a journal, and one verb
streams it:

```bash
showrunner snapshot            # the whole world in one call and one instant
showrunner watch --follow      # then the deltas. One JSON object per line.
```

The two are a pair. An event saying `leaf.closed` is a **delta against a picture**, so a viewer
needs the picture first — and building it from `status` + `reconcile` + `waiting` + `plan` costs
four round trips and returns a composite of instants that never co-existed. They join on a
`cursor`: `snapshot`'s names the last event it could have seen, so `watch --since <that>` gives
exactly what is not already reflected, with no overlap and no gap.

```jsonc
{"kind":"leaf.claimed","seq":2,"leaf":"gh-15","actor":"crawler-a","instance":"/path/to/repo"}
{"type":"ready","replayed":2,"seq":2,"cursor":"fd4620f33271@2","project":"showrunner"}
{"type":"heartbeat","seq":2,"dropped":0,"unparseable":0}
```

Three properties, each with a reason:

- **A viewer asks the verb; it never reads `.showrunner/`.** The journal's name and layout are
  showrunner's business, and a consumer that reaches past the verb is the coupling this project
  deleted a hardcoded rule list to end.
- **Replay comes before follow.** Attaching to a running campaign is never a blank screen — and a
  blank screen cannot be told apart from a broken pipe.
- **A heartbeat, because the journal is sparse.** An orchestrator can integrate for twenty minutes
  without writing one event. A view built on the journal alone freezes exactly when the work is
  hardest, which is when someone is most likely watching it. The heartbeat also carries the count
  of events that could **not** be written and lines that could not be parsed, so "nothing is
  happening" and "I have not been able to see" stay different answers. A journal that cannot be
  read at all is a **refusal**, not an empty replay.
- **Refusals are events too.** `lock.refused` is what lets a view draw a *queue*; with only
  `lock.acquired` a contended resource and an idle one are the same picture — and the
  serialization point is the thing no single Crawler can observe, which is why showrunner owns it
  at all. `lock.reclaimed` stays distinct from `lock.released`: a reaper taking a resource off a
  dead holder is not the holder handing it back, and only one of them says something went wrong.

The cursor names the instance that minted it, and `--since` refuses one from a different
showrunner: a sequence number counts within one journal, and several showrunners in several repos
is the ordinary case. Design notes, and what a server built on this must not do with that cursor,
are in [`docs/plans/observability.md`](docs/plans/observability.md).

## Running more than one orchestrator

A graph that survives sessions is a graph more than one agent will open — several Claude Code
sessions driving one build is a supported shape, not an accident. The state showrunner shares
between them is protected, and it was measured before it was fixed:

| Shared state | The race | Now |
|---|---|---|
| a leaf claim | check-then-write: **6 of 12** concurrent claims won the same leaf | one conditional `UPDATE`; measured 1 of 12 (`python3 test/run.py`, group *More than one orchestrator may share this state*) |
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

`showrunner waiting` answers in three codes: **0** waiting, **1** not waiting, **3** a Crawler is
BLOCKED. That is the recomputable fact an idle watchdog needs, since it cannot see a subagent. A
Crawler refused at a turn-end is live and is deliberately counted as NEITHER waiting nor parked:
it is doing nothing, only a message restarts it, and calling that "waiting" would silence the
watchdog on the one run that needs it.

BLOCKED has its own exit code because it used to share 1 with "not waiting", so the case the gate
exists for produced the same number as an ordinary quiet campaign — and a real stop gate written
against this verb never fired once. Build on `--porcelain`: a verb whose finding, verdict and
status live on three channels gets integrated against incorrectly, and `waiting || exit 0` still
swallows the blocked case, because that idiom collapses every non-zero code.

**Arming that watchdog is a manual step, once per install.** Point it at
`.showrunner/hooks/waiting-probe.sh`, **not** at `showrunner waiting` directly — the probe maps
the three codes above onto the two-plus-unknown contract a watchdog expects, so a BLOCKED Crawler
rings rather than being reported as a broken probe, and it never answers "waiting" from inside a
worktree, where a Crawler would otherwise silence its own watchdog with a sibling's liveness.
`showrunner doctor` names the file and says whether it is armed.

It cannot arm it for you: a probe an agent can set is a watchdog an agent can switch off, and one
that always exits 0 reads as "always waiting". Until it is armed, an orchestrator that has
correctly dispatched a full wave looks exactly like one that fell asleep, gets rung, and
eventually pages you for a run that was behaving perfectly.

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

Design records for things that are **built**, each stating which steps are not:

- [`docs/plans/worktree-lease.md`](docs/plans/worktree-lease.md) — one session per worktree,
  enforced by a lease and a PreToolUse guard rather than described by the campaign record. The
  guard denies, from a tracked shim, registered, with `doctor` checking all three. `worktree
  takeover` is the step that is not built.
- [`docs/plans/central-install.md`](docs/plans/central-install.md) — opt-in `install.sh --central`:
  one machine-wide copy of the code, every project keeping its own config. Works end to end and
  is reversible; `doctor`'s central checks and the campaign record's central SHA are not built,
  so a mid-campaign `self --pin` is invisible to a running campaign.

> This section said **"Planned, not built"** for both while both were shipping. A reader deciding
> whether to use central mode would have concluded it did not exist. Each plan doc carries its own
> status line — the README pointed at them and then restated their status from memory.
