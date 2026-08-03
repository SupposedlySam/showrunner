# The showrunner ↔ game_loop boundary

Who owns what, what each side may assume, and the standing direction for fixes and feature
work across the two repos. Written down because it was re-derived twice, and the second time
only because a stale assertion got caught by re-reading the source.

Dependency arrow points one way: **showrunner → game_loop.** game_loop never learns the words
"Crawler", "campaign" or "showrunner".

## The division

| Concept | Owner | Why |
|---|---|---|
| Autonomy, safety and honesty **within one session** | game_loop | Its power is its narrowness. |
| The dependency graph, lanes, waves, campaign record | showrunner | Breadth across work and agents. |
| **Cross-process locks** over single-consumer resources | showrunner | No single Crawler can see the others. |
| The **guard** that checks a lock before a risky verb | game_loop | It owns PreToolUse. showrunner supplies the lock path. |
| Whether a **turn** may end | game_loop | Its Stop gate. showrunner supplies the graph the gate reads. |
| Whether a **leaf** may close | showrunner | Proof-of-done and the premise verdict. |
| What a change **owes**, and whether evidence postdates it | game_loop | A fact about a tree. |
| Whether **checks pass on the merged result** | showrunner | A fact about a trunk that exists only after integration. |
| Where a Crawler may **write**, and what it gets | showrunner | Worktree placement, injection, scratch. |
| What is **runtime state** vs. a **rule** inside the harness | game_loop | It is the thing that knows. See the open ask below. |

## The three rules this boundary keeps producing

**1. Enforcement lives in tools, never in instructions.** game_loop's INV1, inherited whole.

**2. A degraded guard must fail loud, never quiet.** showrunner's addition, because its guards
are cross-process and their failure mode is silence rather than an error: a worktree-relative
lock root, a JSON parser that stopped matching, a claim whose owner died. Nobody inside a
single Crawler can observe any of those.

**3. Two layers must never disagree about the rules silently.** The one this document exists
for. When showrunner puts a Crawler in a worktree, that Crawler runs its own copy of the
harness — and a harness that is *present but different* is worse than one that is absent,
because absent is loud. A blank `verify.yaml` is a commit gate that owes nothing and reports
success; default `allow_write_roots` is an allowlist that just got emptied. So the rule files
are compared **byte-for-byte** at spawn and a mismatch aborts, rather than being noted.

## Standing direction for fixes and feature work

Applied in this order when something breaks across the boundary:

**Ask which layer owns the concept, and put the check there.** If showrunner is modelling
game_loop's internals, that is a missing verb in game_loop, not a feature in showrunner. Today
`harness.DEFAULT_RULE_FILES` hardcodes `["config.json", "INVARIANTS.md", "verify.yaml"]` —
showrunner asserting which of game_loop's files are rules. It works and it will be wrong the
moment game_loop adds one. That is tracked as game_loop#30.

**Prefer a different question over silence.** When a lower-layer check fires wrongly because
orchestration broke an assumption it was built on, the fix is never to suppress it. A warning
that fires every time is one people learn to scroll past, and then it stops working for the
case it was built for. Ask the question that *is* answerable: not "did you edit these?" (an
orchestrator never does) but "does the staged set match the union of what the merged agents
edited?" That is game_loop#29.

**Make the declaration cite something checkable.** Any escape hatch across this boundary must
name a real ref, a real path, a real file — never a list the caller could have invented.
An attribution that names branches lets the consumer recompute the answer; an attribution that
names filenames is a plausible string, which is the one thing a model produces for free.

**Escape hatches are single-use, logged, and cost a sentence.** `authorize`, `--stale-proof-reason`,
`--premise unverifiable`. None of them have an env-var override, because an advertised bypass
is a bypass.

**Verify premises about our own other repo, too.** showrunner shipped a paragraph asserting
game_loop behaviour that game_loop had already fixed, and briefed it into every Crawler. Being
the same author is not evidence. An orchestrator carrying a stale model of the layer below
propagates it N ways in parallel, and none of the N can notice.

**When a lower-layer fix lands, ask what it hands back.** game_loop's per-tree commit gate
(#28) was strictly correct and created showrunner's harness-provisioning problem in the same
stroke. Correct changes below produce new obligations above; that is normal, and worth looking
for on purpose rather than discovering at spawn time.

## Open asks on game_loop

| # | Ask | Unblocks |
|---|---|---|
| [#29](https://github.com/SupposedlySam/game_loop/issues/29) | Let a commit declare provenance (`attribute --merge <branch>`), recomputed from the ref rather than trusted | Moves `showrunner integration-commit` from a command someone must remember to run, to a check that fires at commit time |
| [#30](https://github.com/SupposedlySam/game_loop/issues/30) | A first-class "make this tree carry the SAME harness" verb, plus drift being loud on its own | Lets showrunner stop modelling game_loop's file layout in `harness.py` |

## What showrunner assumes about game_loop today

Stated so it can be re-checked rather than trusted, with the version it was verified against
(`VERSION 2f51021e`):

- The commit gate resolves **per target tree** and denies when that tree carries no harness.
  (`guard-writes-impl.sh` lines 365-408.)
- The edited-file set is scoped to the **session**, not the tree — one session is one session
  however many trees it touches. (`guard-writes-impl.sh:91`.)
- The blast-radius check is a **warning, never a denial**, and is silent when the session
  recorded no edits at all. (lines 437-543, 459-460.)
- `install.sh` seeds user-owned files **only if absent**, and ships `verify.yaml` empty.
  (lines 85-99.)
- Hooks are registered in `<project>/.claude/settings.json`, so a worktree without one has no
  rails at all. (lines 118-166.)

If any of these stops being true, `lib/showrunner/harness.py` and the shared-state audit in
`lib/showrunner/worktree.py` are the two places that need re-reading.
