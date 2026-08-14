# showrunner — design notes

Working notes, not a spec. Records the decisions and the reasons, including the ones that only
became obvious by running the thing.

## Two axes (why showrunner ≠ game_loop)

- **game_loop = vertical:** integrity within one session (autonomy engine + safety hooks + the
  "name a real file" honesty gate). Enforced by Claude Code hooks. Power = narrowness.
- **showrunner = horizontal:** breadth across work and agents (dependency graph, parallel lanes,
  shared locks, resume). A different job → a different repo.

Dependency direction: **showrunner → game_loop + br.** The primitives never learn showrunner exists.

## Decision: the work-graph is vendored, with a br adapter

`br ready` was originally described as the only work-discovery entrypoint, which made a Rust
toolchain and a separate tracker load-bearing for every loop iteration. The layer below reaches as
far as it does substantially because it has **no install step worth the name**, and showrunner
inherits that audience; every dependency is a place adoption stops, and this one sat in front of the
*first* command.

So: a minimal graph over Python's built-in `sqlite3`, and a `br` adapter that is preferred when br is
genuinely present. Both sit behind one `Graph` interface, and nothing else in showrunner learns which
backend it got (`lib/showrunner/graph.py`).

Two capabilities the vendored backend has that a general tracker does not, because an orchestrator
needs them:

- **Claims carry liveness** (pid + boot token + worktree + session).
- **`refuted` is a terminal state** distinct from `closed`.

The br adapter cannot answer `stale_claims()` — br records no liveness on a claim — so it **raises
rather than returning an empty list**. Returning `[]` would read as "nothing is stale", which is the
one answer that must never be produced by not knowing.

## The boundary (the seam)

The interesting edge is enforcement of shared resources:

- The **lock** (shared cross-process state) is showrunner's.
- The **guard** that checks it before a risky verb is a game_loop-shaped **PreToolUse hook** running
  inside each Crawler — `showrunner lock guard` exits 2, which is the deny code.
- So game_loop's write-guard grows a generic "check this external lock before these verbs" seam, and
  **showrunner supplies the lock path.** game_loop stays generic — it never learns the word "device."

Likewise: game_loop provides the *hook mechanism* for a Stop gate / a done gate; showrunner provides
the *graph + policy* those gates read (`showrunner stop-gate`, also exit 2).

## The rule the whole design keeps rediscovering

**A degraded guard must fail loud, never quiet.** Every failure mode that actually hurt in a real run
had the same shape: not an error, but *silence*.

| The mechanism | How it goes quiet | What now happens |
|---|---|---|
| the resource lock | a worktree-relative lock root gives N trees N locks | refused at config load |
| the stop gate | `sed`/`grep` over JSON stops matching after a field-order change | parsed as JSON; unrecognised shape refuses |
| the br adapter | an unparseable response reads as an empty graph | refuses, naming the command and the output |
| a claim | its owner dies and the leaf never returns to `ready` | pid + boot token; `reap` reclaims loudly |
| the close gate | any non-empty file satisfies `[ -s "$proof" ]` | proof must postdate the claim, and is recorded |
| a check comparison | a failure with no parseable lines looks like no failure | marked `exit-code-only` and reported as degraded |
| the collision estimate | an unestimable leaf looks like a leaf that collides with nothing | treated as colliding with everything, with the reason printed |
| lane routing | an unmatched leaf silently takes the default | defaults conservatively **and says the rule is missing** |

## Isolation is per-resource; a worktree is not a boundary

A worktree isolates **tracked files** and nothing else. Everything resolved from an absolute path or
from a hook's own script location stays shared, so the audit at spawn enumerates what a Crawler
actually gets rather than letting "it has its own worktree" stand in for independence.

**Corrected on re-reading the harness (2026-08).** showrunner originally recorded, from issue #13,
that a commit made inside a worktree is gated on the **main checkout's** verification record. That is
no longer true of current game_loop and the claim is retracted. `guard-writes-impl.sh` resolves the
commit gate from the **tree the commit targets**: it reads the `git commit`'s `-C`/cwd, and when that
resolves to a different tree nested inside the project it uses *that* tree's `.game_loop`. If the
target tree carries no harness it **denies**, on the stated grounds that reading another tree's record
would answer a question about files this commit does not contain. `TARGET_TREE` scopes the
blast-radius check's index the same way. Both of issue #13's consequences — the throughput one and the
serious correctness one — are handled there.

This is worth recording as more than a footnote, because it is the whole premise-verification argument
turned on its author: showrunner shipped a paragraph asserting a harness behaviour that the harness had
already fixed, and only re-reading the source caught it. An orchestrator carrying a stale model of the
layer below will brief every Crawler with it.

**What the fix hands back to showrunner** is a new, concrete constraint: each Crawler worktree must
carry its own harness, or its first commit is denied — and **`git worktree add` copies tracked files
only**, so a gitignored harness directory never crosses. That is the secret-injection problem (#10)
with the harness as the missing file, equally invisible at spawn. `worktree.harness_gap()` detects it
at `spawn` and in `doctor`.

The general lesson stands unchanged: **isolation has to be reasoned about per-resource, not granted
wholesale by the worktree** — and the per-resource answers can differ, since a harness may deliberately
scope one thing to the session (the edited-file set: one session is one session however many trees it
touches) and another to the tree (what a change owes).

## Premise verification is the highest-leverage line in a brief

Over one real run of 14 issues, three had premises that did not survive contact with the codebase: a
failure that was not live in that repo, tooling that was asserted to exist and did not, and a command
from an entirely different harness. That is not a criticism of the issues — a good bug report is
written from the incident, and the incident happened somewhere.

Why it is a *showrunner* problem specifically: a Crawler that quietly implements a fix for a bug that
is not there is **indistinguishable** from one that did the work, and the proof-of-done gate is
satisfied because a real artifact really was produced. The gate checks that work happened, not that it
was needed. And fan-out makes it worse rather than better — a single agent working a queue serially
starts noticing that issue 9 contradicts what it read for issue 3; N isolated agents each see one
issue and cannot notice anything.

Hence: `--premise` and `--premise-read` are **required arguments of the close**, and `--refuted` is a
first-class successful outcome. If the only shapes available are done/failed, the incentive is to
build something.

## Validated primitives

`test/run.py` — 399 assertions, Python 3 + git, no other setup. The `br`/`tmux` assertions skip
loudly. `prototype/` holds the original shell POC (7 assertions run anywhere, 5 skip).

## Still open

- **Lock completeness / rung.** The PreToolUse guard is only as good as its verb classifier; a rogue
  raw command escapes it. To reach rung-1 IMPOSSIBLE the lock belongs *inside* the consumer tool
  **and** in the guard — belt + suspenders. `lock run` is the belt; the guard is the suspenders.
- **Lock fairness.** `acquire --wait` polls; there is no queue, so a starved waiter can lose
  repeatedly. Said out loud in `locks.py` because a fairness property nobody stated is one somebody
  will assume.
- **Blast-radius estimation is a heuristic.** It reads paths named in the issue plus files mentioning
  the issue's symbols. It is deliberately conservative, and it will over-serialize on prose-heavy
  issues (it does exactly that on this repo's own issue list). The fix for that is configuring shared
  surfaces, not loosening the estimate.
- **Relevance of a proof artifact is unknowable by a string check.** The gate checks existence and
  freshness and records the proof for a reviewer. The boundary is stated in the gate itself.
- **`br` adapter is written against br's documented CLI shape and exercised only through its refusal
  paths here** — `br` is not installed on the machine this was built on. It is strict on purpose: it
  will fail loudly rather than quietly report an empty graph.

## Theme

Dungeon Crawler Carl. The crawl is a produced show: **Crawlers** run the rooms, `game_loop` keeps each
one alive, the graph is the quest log, and the **showrunner** runs the whole production.
