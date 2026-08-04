# INVARIANTS

The non-negotiables `game_loop stepback` re-injects. This is the template that ships with game_loop —
**edit it to your project's north star.** Keep the general ones (INV1–INV6); add your own as observed
failures demand. Each should earn its place from a real mistake, not from wanting a tidy list — INV7 is
the worked example: it exists because one session shipped three false findings off one event.

---

## INV1 — Enforcement lives in tools, never in instructions

A rule the agent has to *remember* is followed only some of the time — long sessions and context
compaction break that promise. A rule a hook *consumes* holds every time.

Test for any guard here: **if the agent ignored every instruction, would this still hold?** If no, it
is not enforcement; it is a wish. This is why the keystone check is always "name a real file that
exists" — the one check prose cannot satisfy.

## INV2 — Read a real file before asserting (THE gate)

Every claim about external reality — a dependency, a harness, another repo — must name the real file
that backs it: `game_loop claim --assert ".." --read <path>`. A research subagent's citation is not a
source; it *finds* the file, it does not *read* it. Cite the file you read.

## INV3 — Everything outside this repo is READ-ONLY

Read other projects, mine them, use their data as fixtures. Never write, never run their tooling,
never deploy. Access is not permission — logged-in accounts, tokens, and an always-on prod connection
are not permission. Enforced by `.game_loop/bin/guard-writes.sh`, not by this paragraph, because a
paragraph exactly like it is the kind of thing that already fails.

## INV4 — No gate without a logged, observed failure

Ceremony has a certain cost and a hypothetical benefit. Do not add a rung until `log.jsonl` shows a
real failure that demands it. When tempted, name the entry that justifies it.

## INV5 — A guard must never block its own fix

A guard that blocks the fix it recommends is a guard that gets switched off — and a guard disabled
once is disabled forever. Every guard needs a legitimate path through it. Here the escape hatch is the
*human* (`game_loop authorize`), never an env var, because an advertised bypass is a bypass.

## INV6 — ENCODE, don't remember; and state what a guard misses

A learning is a bug in the system with a countdown. Its deliverable is an **artifact**, not a
sentence: `game_loop harden --learning ".." --artifact <real path> --mechanism ".." --rung N`. Take the
highest rung that applies — **1 IMPOSSIBLE · 2 LOUD · 3 CHECKED · 4 AUTOMATED · 5 VISIBLE · 6
doc/memory** (last resort). And a guard that overstates its reach buys false confidence: say what it
does *not* catch, in the guard itself. Silence from a guard is not evidence of safety.

## INV7 — A sum is not a distribution

An aggregate hides its own shape, and a run optimizing against one will read structure into a single
outlier. Before stating an effect derived from a **total, a mean, or a percentage**, show the per-event
values — and when one event carries most of the total, explain it or exclude it *with the reason on the
record*, because an exclusion nobody wrote down gets rediscovered.

The failure: 1066.7 units of damage against 0.0 read as a total elimination and was written up as a
finding. One event of thirty carried 96% of it, and it was an artifact already identified and dismissed
earlier in the same session. Corrected: 1.5 per event against 0 — no effect at that sample size. Nothing
about the totals revealed this; only printing the per-event values did, and the same event produced
three findings before anyone did. Enforced by `claim --metric --aggregate`, not by this paragraph.

---

## INV8 — A degraded guard must fail loud, never quiet

showrunner's guards are cross-process. Their failure mode is not an error, it is *silence*: N worktrees
each making their own sibling lock directory, a `close-gate` whose JSON parser stopped matching, a
router whose regex no longer fires. Each looks exactly like a guard that is running and content.

So every shared-state mechanism here must **refuse to run misconfigured** rather than degrade. A mutex
that is quietly a no-op is worse than no mutex, because the whole point is that nobody inside a single
Crawler can observe the collision.

The failure: `prototype/device_lane.sh:11` defaults `LOCKDIR` to a path relative to the *script's own
directory* — so each worktree gets its own lock and the mutex silently does nothing (issue #3).

## INV9 — Isolation is per-resource; a worktree is not a boundary

A git worktree isolates **tracked files** and nothing else. The scratch dir, the harness state dir,
lock paths, caches, and anything resolved from an absolute path or from a hook's own script location
are all still shared. Before spawning, **enumerate what a Crawler actually shares** and say it out
loud; do not let "it has its own worktree" stand in for independence.

The failures: two Crawlers reached for the same obvious scratch filename `commitmsg.txt` and one nearly
committed the other's message (issue #11); every worktree's `git commit` gated on the *main checkout's*
verification record (issue #13).

## INV10 — Verify the premise before building the fix

Work arrives as issues, and issues are written from an incident that happened somewhere — not
necessarily here. Of 14 issues in one real run, **three had premises that did not survive contact with
the codebase.** A Crawler that fixes a bug that is not there is indistinguishable from one that did the
work: same commit, same green tests, same satisfied proof-of-done gate.

So "state whether the premise holds, and cite the file you checked it against" is a **required field**
of every Crawler brief and report, and **premise-refuted is a first-class successful outcome** —
distinct from done and from failed. If the only available shapes are done/failed, the incentive is to
build something (issue #12).

## INV11 — Done means the checks pass on the merged result

Green on a branch is evidence about a trunk that no longer exists once the second branch lands. Two
Crawlers can touch disjoint lines and still produce a broken trunk (two entries in one dispatch table).
Integrate serially, re-run the owed checks after each merge, and stop on the first failure rather than
stacking branches onto a broken trunk. The criterion is **no *new* failures versus a recorded
baseline**, never "all green" — a repo with pre-existing failures cannot satisfy "all green", so that
version of the gate gets switched off on contact with a real codebase (issues #9, #6).

## INV12 — Two layers must never disagree about the rules silently

A Crawler runs its own copy of the harness. A harness that is **present but different** is worse
than one that is absent, because absent is loud: a blank `verify.yaml` is a commit gate that owes
nothing *and reports success*; a default `allow_write_roots` is an allowlist that just got emptied.
Nothing errors — the party simply plays by two rule sets, and the weaker one is the one running
unattended in N parallel worktrees.

So sameness is **proven, not arranged**: rule files are compared byte-for-byte at spawn and a
mismatch aborts. Enforced by `lib/showrunner/harness.py`, not by this paragraph.

The failure: a harness installer seeds user-owned files only if absent, so installing into a fresh
worktree yields the template's `verify.yaml`, the template's INVARIANTS and the template's config —
verified against `install.sh:85-99`, and reproduced end-to-end (`verify --check` in a spawned
worktree answering *"no rules — nothing owes a check"*).

## INV13 — The layer that owns the concept owns the check

If showrunner is modelling game_loop's internals, that is a **missing verb in game_loop**, not a
feature in showrunner. Reimplementing a lower layer's semantics one layer up works, and it goes
stale the moment the lower layer changes — silently, because nothing connects the two.

Standing form when something breaks across the boundary: ask which layer owns the concept; prefer
a **different question** over silencing a check that fires wrongly; make every cross-boundary
declaration cite something **recomputable** (a ref, a path) rather than a list the caller could
have invented; and when a lower-layer fix lands, ask what obligation it hands back up.

Written down in `docs/BOUNDARY.md`, with the open asks tracked as game_loop#29 and #30. The
current known instance: `harness.DEFAULT_RULE_FILES` asserts which of game_loop's files are rules,
and will be wrong the moment game_loop adds one.

## INV14 — Verify premises about our OWN repos too

Being the same author is not evidence. showrunner shipped a paragraph asserting game_loop behaviour
that game_loop had already fixed (per-tree commit gates, game_loop#28) and briefed it into every
Crawler — an orchestrator carrying a stale model of the layer below propagates it N ways in
parallel, and none of the N can notice, because each sees one issue and no others.

Cite the file, in the sibling repo, at the version you read. `docs/BOUNDARY.md` carries the list of
what showrunner currently assumes about game_loop and the line numbers it was verified against, so
the next reader re-checks instead of re-deriving.

## INV15 — Pair every non-event assertion with the case where it happens

An assertion whose evidence is that something did **not** occur cannot tell being satisfied
from never having run. "No errors", "not in the list", "nothing was staged", "the guard
allowed it" — every one is also what a broken producer returns.

So when you assert an absence, capture the positive case in the **same observation**: the run
that is quiet for a clean input must be seen speaking for a dirty one. The remedy is chosen by
what the check has on its happy path — a **reason** if it speaks, a **mark** if it is silent
(written before the first early return, or the cheapest allows stay unprovable), a **positive
control** if it is a pure observation. Add a companion, never a replacement: the original is
not false, only unsupported, and rewriting it loses the restraint claim it encodes.

These cluster on **restraint** — the behaviours a project argued itself into (reap is dry-run,
an abandoned worktree is surfaced not deleted, notes-drift warns instead of aborting). Each is
a deliberate decision to *not* act, each was defended, and each is proved by a non-event.
Restraint is expensive to design and free to break.

Enforced by `test/mutate.py`, not by this paragraph. The failures: an assertion that
`git add -A` could not stage an injected secret — quoted as evidence in an issue close, the
README and to the human — was vacuous, because the worktree was pristine and `add` staged
nothing at all; and `integrate` refusing to merge a rules-drifted tree was implemented,
committed, described, and never tested. Costs one extra capture at write time and cannot be
retrofitted cheaply, which is the worst combination to discover late.

---

**The outside view outranks my attachment.** The human and fresh review subagents are the real
outside view. When they disagree with me, they are probably right; update rather than defend.
