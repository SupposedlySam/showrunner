# showrunner

**A multi-agent orchestrator for Claude Code — the one who runs the whole crawl.**

If [`game_loop`](https://github.com/SupposedlySam/game_loop) keeps a single Crawler alive, honest,
and safe through one unattended session, `showrunner` runs the *show*: it decomposes the quest,
sends a party of Crawlers into separate rooms in parallel, enforces the party-wide rules that no
single Crawler can see, and keeps the campaign coherent across sessions.

> Status: **early.** The primitives are validated (see [`prototype/`](prototype/)); the orchestration
> loop is being proven on real work before it's crystallized here.

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

## Lanes

| Lane | Runs | Parallel? |
|------|------|-----------|
| **Headless** | tests, backend, pure logic, analyze | ✅ one worktree+tmux Crawler per ready leaf |
| **Serialized** | anything touching a single-consumer resource (device deploy, Marionette, a bound port) | ❌ one at a time, behind the shared lock |
| **Orchestration** | showrunner itself: graph, routing, review gates, integration | single |

## Generic, not project-specific

Like `game_loop`, showrunner is generic; a project's specifics (which verbs are "serialized," which
resources are single-consumer, the graph location) are **config**, not code.
