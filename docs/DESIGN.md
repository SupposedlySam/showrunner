# showrunner — design notes

Working notes, not a spec. Captures decisions made while prototyping against the Drops monorepo.

## Two axes (why showrunner ≠ game_loop)

- **game_loop = vertical:** integrity within one session (autonomy engine + safety hooks + the
  "name a real file" honesty gate). Enforced by Claude Code hooks. Power = narrowness.
- **showrunner = horizontal:** breadth across work and agents (dependency graph, parallel lanes,
  shared locks, resume). A different job → a different repo.

Dependency direction: **showrunner → game_loop + br.** The primitives never learn showrunner exists.

## The boundary (the seam)

The interesting edge is enforcement of shared resources:

- The **lock** (shared cross-process state) is showrunner's.
- The **guard** that checks it before a risky verb is a game_loop-shaped **PreToolUse hook** running
  inside each Crawler.
- So game_loop's write-guard grows a generic "check this external lock before these verbs" seam, and
  **showrunner supplies the lock path.** game_loop stays generic — it never learns the word "device."

Likewise: game_loop provides the *hook mechanism* for a Stop gate / a done gate; showrunner provides
the *graph + policy* those gates read.

## Validated primitives (see `prototype/`, 12/12 passing against real `br` + worktrees)

1. **Cross-process device/serialized-resource lock** (`device_lane.sh`): atomic `mkdir` lock whose
   holder is a **live PID** (the long-running consumer itself, via the `run` wrapper). Blocks a second
   consumer while the holder is alive; auto-reclaims when the holder is dead. This is what makes
   "one at a time" physically true across separate processes/worktrees.
2. **Proof-of-done close gate** (`br_gate.sh close-gate`): `br close` refused unless a real, non-empty
   artifact is named (test / golden / commit). game_loop's "cite the file" applied to "done."
3. **Leaf-scoped Stop gate** (`br_gate.sh stop-gate`): refuse turn-end with claimed-open **leaf** work;
   epics are containers and are excluded.

## Open questions (to resolve by running a real multi-Crawler campaign first)

- **Lock completeness / rung.** The PreToolUse guard is only as good as its verb classifier; a rogue
  raw command escapes it. To reach rung-1 IMPOSSIBLE, the lock likely belongs *inside* the consumer
  tool (e.g. the deploy tool) **and** the guard — belt + suspenders.
- **Shared lock path across worktrees.** Proven cross-process; cross-worktree needs the lock at one
  absolute shared path (env-configured), not per-tree.
- **Failure modes not yet exercised:** a Crawler dying mid-claim, lock fairness/starvation, merge
  ordering across N branches, dev-server port collisions.
- **PID reuse** in stale detection (tighten with a boot token).

## Sequence

Prove the multi-Crawler loop by hand on a headless-heavy task → collect the real gap list → *then*
crystallize the orchestration loop here. game_loop earned its shape by being battle-tested; showrunner
should too.

## Theme

Dungeon Crawler Carl. The crawl is a produced show: **Crawlers** run the rooms, `game_loop` keeps each
one alive, `br` is the quest log, and the **showrunner** runs the whole production.
