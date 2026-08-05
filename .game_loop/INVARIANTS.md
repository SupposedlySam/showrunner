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

**Corollary — a check that cannot fail is not a weak check; it was never a check.** And which
way that goes is decided by which side of the decision the predicate sits on:

| an unfailable predicate gating a… | outcome | how it is found |
|---|---|---|
| **refusal** | refuses everything | LOUD — the first test written kills it |
| **acceptance** | accepts everything | SILENT — green for months |

Validators live in the accepting position, which is why this hides there. `isabs()` applied to
a value already through `abspath()` is true for every string; that predicate sat in the
lock_root validator — written to prevent a per-caller mutex — returning an empty error list,
which reads as *validated*. The mutation sweep cannot find it either: neutering a validator
that already has no opinion changes nothing.

Auditing for it is not a grep. Ask **which predicates gate an ACCEPT, and for each, whether any
input reaches the false branch.** A validator whose error list is empty for every input it has
ever seen looks identical to one with nothing to complain about. Enforced by the
reachable-rules group in `test/run.py`: every error branch must have a case proving it fires,
and the branch count is pinned so a new rule must arrive with one.

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

**WHEN THE PAIRING IS POSSIBLE AT ALL**, which took a second repo to see. game_loop generalised
this as *exercise a presence, version an absence* — its own absences being facts like "no
`claude usage` flag exists", which no run can confirm. But the secret-staging assertion above is
an absence and is soundly exercised, because the decoy makes `git add -A` demonstrably fire and
the secret is still missing from the same output.

So the discriminator is not presence versus absence. It is **whether you control the input that
would produce the effect**. A *conditional* absence — given this input, nothing happens — is
exercisable, and the positive control turns it into a discrimination. An *unconditional* absence
is not, because the case where the fact is false is precisely the one you cannot construct;
there the version stamp of [INV20] is not the weaker instrument, it is the only one. Two of our
git premises are conditional and exercised for that reason: a tracked file crossing into a
worktree, an ignore that is not per-worktree.

## INV16 — Ask the scoping questions before building, not after being asked

Two questions found real defects in this repo, both times because a human asked them and not
because I did: **"is this for anyone cloning it, or just me?"** and **"how many agents run this
at once?"**

The first surfaced my absolute paths — including an `allow_write_roots` entry, which is a
*rule* a stranger inherits — sitting in a public repo. The second surfaced that `claim` let
**6 of 12** concurrent claims win the same leaf and the campaign record lost **7 of 10**
spawns, after I had already written "supports several orchestrators" in the docs.

Both were invisible from inside the work, because the work looked correct *for the case I had
in my head*. So before building, name the audience and the concurrency out loud, and write the
answer down where it can be checked: `test/run.py` asserts the publishable case, the
concurrency group asserts the multi-orchestrator one.

Rung 5 and not higher, deliberately: an artifact can check a scope once it is named, but
nothing can force the question to be asked. That is why it is re-injected every session
instead of being left to occur to me.

## INV17 — Record what a conclusion RESTS ON, and when it stops holding

A design conclusion outlives the reasoning that produced it, and it gets quoted long after the
premise has moved. Two votes against a design were cast here resting on a property that was
**false when they were cast** and true only after a specific commit landed. Recorded as "two
votes against", they would have been inherited. Recorded with the expiry — *if that property is
ever weakened, the against-case collapses and the for-case survives* — they can be re-derived
by someone who was not here.

So a recorded conclusion carries its basis and its expiry: the file, the line range, the
version it was verified against. `docs/BOUNDARY.md` does this for every assumption showrunner
makes about the layer below, which is what let a stale one be caught by re-reading rather than
by being bitten.

## INV18 — A guess about intent is not a reason not to look

I reported an anomaly in another tool and dismissed it in the same breath: *"the closed test
rooms cluttering the output is **presumably** your own scaffolding mid-development."* It was
scaffolding, and it was also a real defect — nineteen rooms, fifteen dead, burying the four
joinable ones, in a list nothing ever prunes and which offers an agent rooms it will be
refused from joining. I had the observation and traded it for a hypothesis about someone
else's intent.

The tell is verbal and easy to catch in my own text, which is what makes it worth naming:
**"presumably", "probably just", "that's likely intentional", "I assume that's deliberate".**
Every one is a guess about intent standing in for a check that would cost a minute. Writing
one about another system is the moment to look, not the moment to move on.

The related failure is the charitable reading that *explains the evidence completely*: a
mangled chat message read as sloppy writing, and it was a transport fault that reports
success. Fitting the evidence is not the same as being true, and the comfortable explanation
is the one that stops the search.

Rung 5, and 1-4 genuinely do not apply: this is a decision *not* to investigate, and an
absent investigation leaves no artifact for a check to find. What can be enforced is that the
words are distinctive and this is re-injected every session.

## INV22 — A caveat filed where the reader does not stand is a caveat they never had

The one failure this week where nothing was stale, nobody was misinformed, and the author had
already done the thinking.

game_loop narrowed its deploy-verb match and recorded the change in `behaviour.json`, the file
consumers read. Three lines below the fix, in a source comment consumers do not read, it also
recorded the limit: the bare verb as a whole word in prose *still* trips it. Both written, both
accurate, minutes apart. From the record, a consumer concludes the false positives are gone and
drops a workaround they still need. The information existed the whole time and was not where the
person acting on it stands.

This is not forgetting, and it is worse than forgetting, because there is nothing to remember
harder. Every individual step is correct — you thought it, you wrote it, you put it somewhere
reasonable — and the defect exists only relative to a reader positioned elsewhere. No moment in
the authoring feels like an omission.

I have no mechanism and am not inventing one to round the shape off; that absence is stated so
it does not read as an oversight somebody plans to fix. The nearest thing that works is a
question, asked of anything written down for someone else: **what would they DO on the strength
of this, and does the limit reach them there?** That is rung 6 in a nicer coat, and it is what
caught this one — from the outside, by the consumer, which may be the only position it is
visible from.

Rung 4 where the subject is countable: the same instinct is why `test_claims_about_the_layer_below`
stamps the claims a Crawler is handed rather than trusting that whoever wrote them also warned
whoever reads them.

## INV21 — A printed remedy is a claim that a command exists

Every refusal here ends by telling the reader what to run. That closing line is an assertion
about the CLI, made in prose, and it was the only kind of assertion nothing checked. The br
backend's stale-claims refusal said to run `showrunner campaign`. There is no such verb;
argparse rejects it outright. It had never been run, because the only way to reach it is to
already be blocked — the string exists precisely for the person least able to work around it.

The usual docs check runs the other direction: every verb the CLI defines must appear in the
docs. That check passes forever while a remedy names a verb that was renamed or never existed,
because it is looking at the set of real verbs and asking whether they are mentioned — never at
the set of mentioned verbs and asking whether they are real. Both directions are needed and
only one is conventional.

Rung 4, in `test_cli`: every `showrunner <verb>` written in backticks or a fenced block, across
the docs and every module, must be a verb the CLI accepts. The discriminator is command
POSITION rather than a vocabulary of prose words — a denylist of English would need a new entry
every time someone writes "showrunner ships", and a list that grows with the language is a list
that will be wrong. Position stays fixed as the docs grow.

Paired with a count, because a scan that matches nothing passes exactly like a scan that
verified everything; and proved end-to-end by reintroducing the real dead command and watching
it fail, rather than trusting that it would.

The count was not enough. A positional rule has a DENOMINATOR — the remedies written in a
position it does not recognise — and those are not wrong, they are absent, which reads exactly
like correctness. Six were invisible when this was written, three of them in the Crawler brief.
So a second assertion walks every string literal by AST and requires each real verb to sit
somewhere the scan can see. Only real verbs are flagged, so prose cannot make noise, and the
day one is renamed it becomes a dead command the first rule already catches.

A LIMIT NEEDS A CORPSE NEXT TO IT, which is llm_chat's idea and the best thing to come out of
this exchange. Their docs check disclaimed, on every run, that it verifies a name APPEARS and
never that the prose is right. Accurate, printed constantly, read as boilerplate by its own
author for a day — and it stopped them looking, because "cannot verify correctness" sounds
unmechanisable while "cannot catch `llm_chat serve`, which shipped in the module docstring for
months" is a bug you go and find. So state limits with an instance attached, not as a category.
Note that theirs never went stale: an accurate limit that reads as decoration is a second and
worse failure than INV20's, because there is nothing to detect.

## INV20 — A stated LIMIT ages exactly like a stated PREMISE

INV14 says to verify premises about our own repos. A premise is a claim I *act* on, so it gets
re-checked the next time I act. A limitation is a claim about what something does NOT do — and
nothing ever makes me reach for it, so it is never the thing that fails. It just sits there
being quoted.

Both rotted within one week, both about game_loop, both fluent and specific and wrong. The
Crawler brief told every agent that a variable-built commit path passes SILENTLY, measured and
true when written; game_loop then made it a hard denial, and the sentence went on reading like
a finding. `docs/BOUNDARY.md` listed the gate's denials with line numbers into a file that had
since grown by 700 lines. Neither could have been caught by re-reading them, because both were
internally coherent — the rot was entirely outside the text.

The fix is not vigilance, it is a version. Any prose stating another layer's behaviour carries
the release it was verified against, and something fails when that release moves. Rung 4:
`test_claims_about_the_layer_below` in `test/run.py` stamps the set and breaks on upgrade, with
default-deny discovery so a new claim file cannot join the set silently — the first version of
that net missed the very file whose claim had rotted.

What it does NOT do — and this line is itself the kind of claim it governs — is check that any
of the claims are TRUE. It cannot. It only removes the option of going stale quietly, and
converts an unbounded re-reading duty into one that arrives on a specific day.

## INV19 — Surviving by elimination is not the same as being supported

Three accounts of one observation were tested. Two died to measurement. I then wrote that the
third "survives, and is the only account requiring nothing to be wrong" — framing survivorship
as though it were evidence. It is not. A hypothesis left standing after its rivals fall has
exactly the support it started with, which in that case was nothing: one observation, no
mechanism, no reproduction.

The tell is a sentence where the subject is the *field* rather than the *claim* — "the only
remaining explanation", "by process of elimination", "nothing else fits". Each describes what
happened to the alternatives and says nothing about the survivor. Ruling out is real work and
it narrows; it never promotes.

So when an account is all that is left, say so in those words — *survives by elimination,
supported by nothing* — and keep the distinction between "we stopped finding other
explanations" and "we found this one". The first is a fact about the search.

Rung 6, and 1-5 do not apply for the same reason INV18 is there: this is a claim made in prose
about evidence, and no artifact can inspect the strength I assign to my own inference.

---

**The outside view outranks my attachment.** The human and fresh review subagents are the real
outside view. When they disagree with me, they are probably right; update rather than defend.
