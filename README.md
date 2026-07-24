# showrunner

**A multi-agent orchestrator for Claude Code — the one who runs the whole crawl.**

If [`game_loop`](https://github.com/SupposedlySam/game_loop) keeps a single Crawler alive, honest,
and safe through one unattended session, `showrunner` runs the *show*: it decomposes the quest,
sends a party of Crawlers into separate rooms in parallel, enforces the party-wide rules that no
single Crawler can see, and keeps the campaign coherent across sessions.

> Status: **early, but the approach is proven.** The primitives are validated (see
> [`prototype/`](prototype/)), and the orchestration loop has been dogfooded end-to-end on a real
> multi-service feature epic — see [What's been proven](#whats-been-proven). The generic loop is
> being crystallized here from that experience.

## The cast

| Piece | Role |
|-------|------|
| **`showrunner`** (this repo) | The orchestrator. Owns the work-graph, routes work to lanes, holds party-wide locks, spawns + monitors Crawlers, integrates their results. |
| **`game_loop`** (dependency) | Per-Crawler survival rails — autonomy + safety + honesty, enforced by Claude Code hooks. `showrunner` installs it into each Crawler; it stays independently useful for solo runs. |
| **`br`** (dependency) | The quest log — a local SQLite dependency graph (`br ready` = unblocked work) that survives sessions. |
| **Crawler** | One Claude Code agent in its own git worktree + tmux session. |

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
the others: **shared, single-consumer resources** (a device, a deploy target, a port). showrunner owns
the **cross-process lock** that makes "only one Crawler in the boss room at a time" *physically true*,
not a wish in a prompt. See [`docs/DESIGN.md`](docs/DESIGN.md).

## The loop

```
br ready                    # the only work-discovery entrypoint: unblocked leaves
  └─ for each ready leaf:
       claim it (actor)     # so lanes never collide
       route to a lane:
         headless  → spawn a Crawler in a fresh worktree + tmux, in parallel
         serialized→ take the shared lock first; one at a time
       Crawler works under game_loop (kept alive, honest, safe)
       close only through the proof-of-done gate  # names a real artifact, or it doesn't close
  └─ integrate results; repeat until br ready is dry
```

Two mechanics carry the weight:

- **Proof-of-done gate** — a leaf can't be marked complete unless it cites a real, non-empty
  artifact (a passing test, a live endpoint, a committed file). "Cite the file" applied to *done*.
- **Dependency-gated fan-out** — `br` hides blocked work, so a shared prerequisite (e.g. "set up the
  environment") gates everything behind it; the moment it closes, the dependents become `ready` and
  fan out.

## Lanes

| Lane | Runs | Parallel? |
|------|------|-----------|
| **Headless** | tests, backend, pure logic, analyze | ✅ one worktree+tmux Crawler per ready leaf |
| **Serialized** | anything touching a single-consumer resource (device deploy, on-device driving, a bound port) | ❌ one at a time, behind the shared lock |
| **Orchestration** | showrunner itself: graph, routing, review gates, integration | single |

## What's been proven

The loop above isn't theoretical — it was dogfooded to drive a real, multi-service feature epic
(a frontend feature plus the three backend endpoints it needed) all the way to a locally-running app:

- **Fan-out that converges.** Independent backend endpoints and the frontend integration were
  implemented by concurrent Crawlers on non-overlapping files, then integrated — the wall-clock was
  the slowest single chain, not the sum.
- **Dependency-gated setup.** An "environment setup" leaf gated the whole fan-out; closing it (with a
  real health-check artifact) released the dependents automatically.
- **The serialized lane held.** Device build/deploy/drive ran strictly one-at-a-time behind the lock
  while headless implementation and analysis ran in parallel — no wedged installs, no collisions.
- **Proof gates caught hand-waving.** Leaves that couldn't name a passing test or a live artifact
  didn't close.
- **It even built its own tooling.** Mid-run, showrunner dispatched Crawlers to add the missing
  developer-tooling the campaign surfaced — orchestration writing its own scaffolding.

The gaps that surfaced (worktree secret injection, "no *new* failures" vs "all green" as the gate
criterion, shared single-consumer resources beyond the device) are captured in
[`docs/DESIGN.md`](docs/DESIGN.md) — the loop earns its shape by being run, the way `game_loop` did.

## Prototype

[`prototype/`](prototype/) holds the validated proof-of-concept primitives — run `bash prototype/demo.sh`
(12/12 assertions):

- **`device_lane.sh`** — the cross-process single-consumer lock (atomic `mkdir` + live-PID holder +
  stale reclaim + an acquire/exec/release `run` wrapper) and a `guard` in the shape of a game_loop
  write-guard.
- **`br_gate.sh`** — `close-gate` (proof-of-done) and `stop-gate` (refuse turn-end while a claimed
  leaf is still open; containers/epics excluded).
- **`demo.sh`** — the assertion harness.

These are deliberately simple and shell-based; the real implementation lifts the primitives out of the
scripts and into showrunner proper.

## Generic, not project-specific

Like `game_loop`, showrunner is generic; a project's specifics (which verbs are "serialized," which
resources are single-consumer, the graph location) are **config**, not code.
