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
| A commit's **provenance** under fan-out | game_loop | `attribute --merge <ref>`, recomputed from the ref. |
| Whether an orchestrator is **waiting on dispatched work** | showrunner | `showrunner waiting` — a live PID or an explicit park; game_loop's watchdog cannot see a subagent. |
| What a change **owes**, and whether evidence postdates it | game_loop | A fact about a tree. |
| **Merge order and timing** | showrunner | Which branch lands when, and whether to stop. |
| Proving a **fix** on the merged trunk | showrunner | `fix --prove` is scoped to the session that proved it — deliberately, so a Crawler's branch-local proof can never satisfy the integrator's handback. Branch-green is not trunk-green, so the integrating session must exercise the fix's own consumer against the merge. `integrate` writes the merged-result checks out as a citable artifact. |
| Whether **the resulting tree is verified** | game_loop | Amended — after per-tree commit gates this is already answered per tree. showrunner decides *when* to merge; game_loop decides whether what came out is verified. |
| Where a Crawler may **write**, and what it gets | showrunner | Worktree placement, injection, scratch. |
| What is **runtime state** vs. a **rule** inside the harness | game_loop | It is the thing that knows: `.game_loop/.gitignore` and `game_loop owned`. |
| What a **new tree** must carry to run the harness as the parent runs it | game_loop, unanswered | `owned` answers for *home*, not *code*. With a pinned checkout the code lives in a gitignored dir wired through a gitignored settings file, so neither crosses a worktree. Raised in `#game_loop_owner`. |

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
game_loop's internals, that is a missing verb in game_loop, not a feature in showrunner. The
worked example: `harness.DEFAULT_RULE_FILES` hardcoded which of game_loop's files were rules.
It worked, and it was *already* wrong — it knew nothing of the notes tier, so a diverged ledger
was invisible to it. Filed as game_loop#30, answered with `owned`/`worktree`, list deleted.

The test to apply is not "is this file getting bigger" but **does showrunner still contain a
claim about the other layer that only the other layer can validate?**

**Prefer a different question over silence — but check first whether it is a false positive.**
game_loop's sharper form of this, worth stealing verbatim: when a check fires wrongly under
orchestration, ask *is there something recomputable that would make the warning wrong?*

- **Yes → false positive**, and the check is missing an input. The blast-radius warning names
  files the session never wrote; under fan-out they arrive by merge and git can *prove* where
  from. Hence `attribute --merge <ref>` (game_loop#29).
- **No → true positive**, and the layer above owes real work. The unproved-fix warning fires on
  an integrating run too, and nothing is recomputable there: a proof performed against one
  branch genuinely says nothing about the merged result. **Branch-green is not trunk-green.**

Same symptom, opposite diagnoses. Fan-out does not only break assumptions — sometimes it makes
an existing gap visible for the first time, and then the fix belongs *here*, not below.

**Make the declaration cite something checkable.** Any escape hatch across this boundary must
name a real ref, a real path, a real file — never a list the caller could have invented.
An attribution that names branches lets the consumer recompute the answer; an attribution that
names filenames is a plausible string, which is the one thing a model produces for free.

**Escape hatches are single-use, logged, and cost a sentence.** `authorize`, `--stale-proof-reason`,
`--premise unverifiable`. None of them have an env-var override, because an advertised bypass
is a bypass.

**And a brief is not a human** (game_loop#68). Every Crawler here is dispatched by a document
showrunner wrote, so the text telling it what to do is another agent's — and a Crawler spending
`authorize` on the strength of its brief would put a bypass in the log reading as
human-sanctioned. game_loop's refusal now says so itself. Nothing in `brief.py` points a Crawler
at that hatch and nothing should; the correct move for a blocked Crawler is to report upward,
because whoever briefed it is who must ask. This is the sharpest case of the escape-hatch rule
above: under fan-out, the party most able to produce a convincing authorization is the one with
the least standing to give one.

**Verify premises about our own other repo, too.** showrunner shipped a paragraph asserting
game_loop behaviour that game_loop had already fixed, and briefed it into every Crawler. Being
the same author is not evidence. An orchestrator carrying a stale model of the layer below
propagates it N ways in parallel, and none of the N can notice.

**When a lower-layer fix lands, ask what it hands back.** game_loop's per-tree commit gate
(#28) was strictly correct and created showrunner's harness-provisioning problem in the same
stroke. Correct changes below produce new obligations above; that is normal, and worth looking
for on purpose rather than discovering at spawn time.

## What "same harness" does and does not mean

**Updated 2026-08-04, and the update is the point.** `worktree --porcelain` once compared only
the owned set — config, invariants, check manifest, notes — with `bin/` outside it, so two trees
could match on every rule and run different code. showrunner's spawn report said "same RULES"
and appended that caveat. game_loop has since extended the comparison to its scripts: the
`harness` set now covers `bin/` as well as the owned files, and a drifted script is caught.
Ask the verb rather than trusting a count written here — `worktree --porcelain` reports what
it actually compared.

The caveat survived the upgrade and was being printed to every Crawler as fact. That is a stale
claim about the layer below — INV14 — and the second time this project has shipped one, both
times about the same dependency. The lesson is not "check more carefully"; it is that a limit
recorded as prose ages exactly like a premise does, and re-reading the source is the only thing
that catches either.

The limit that IS real: the hook-registration file lives outside the harness directory, so the
harness cannot compare it. showrunner refuses a spawn when it would be absent — a different
check, by a different party.

This stopped being hypothetical when game_loop added pinned checkouts: the pinned code lives in
a gitignored directory, wired through a gitignored `settings.local.json`, and `git worktree add`
carries tracked files only. So in a pinned project the orchestrator runs the pinned harness and
every Crawler runs the repo's own — and the failure pinning exists to prevent (editing a gate
breaks the session guarding the edit) returns through fan-out, since a Crawler sent to edit a
gate is running the copy it is editing.

showrunner deliberately does **not** fix this by learning where a harness keeps its code. That
is modelling internals, which is what deleting `DEFAULT_RULE_FILES` was meant to end, and it
would rot the same way. The ask belongs below: a pin-aware answer to *what must a new tree carry
to run this harness the way I am running it now*.

## A gate escape that fan-out makes the default

Measured with a control, same tree, same failing checks, same session — differing only in
whether the worktree path is written literally or held in a shell variable:

| how the tree is reached | commit gate |
|---|---|
| literal path | **denied**, correctly |
| variable-built path | **allowed, silently** |

game_loop documents "a path built from a shell variable" as a blind spot, so its existence is
known. What is not obvious is the blast radius here: **an orchestrator reaches every worktree
through a variable.** Loops over Crawlers, generated scripts, anything parameterised. So in an
orchestrated run the commit gate is not occasionally escaped — it is escaped by default, and
silently, which is the worst direction because it is indistinguishable from passing.

The fix belongs below (the gate is game_loop's), and it is raised there. What belongs here is
the mitigation showrunner owns: every Crawler brief now says to commit from the tree it is
already in rather than `cd`-ing to it through a variable.

Worth recording alongside it, because it is the same shape one level out: the write guard
refused a *chat message* whose quoted example text contained shell-looking commands. Right
rule, wrong subject — the mirror of the provenance check firing on merges.

## The interface showrunner consumes

game_loop's side is documented in its `docs/embedding.md`; that page and these verbs are the
interface, and internals are explicitly not. showrunner must never parse `state.json`, guess
which files are rules, or reimplement the hooks merge.

| Verb | Used for |
|---|---|
| `game_loop owned --porcelain` | the owned set and the rule/notes split — showrunner keeps **no list of its own** |
| `game_loop worktree --porcelain` | is this tree the same harness as its parent |
| `install.sh --same-as <parent> <target>` | provision a tree with the parent's rules, not blank templates |
| `game_loop attribute --merge <ref>` | declare an integration commit's provenance |
| `.game_loop/.gitignore` | the authoritative declaration of what is runtime state |

Exit codes from `worktree`, consumed exactly as defined: **0** clean · **1** drifted — a
determined finding that the trees enforce different things, whether the difference is in the
rule files or in the harness's own scripts (abort the spawn) · **2** undetermined (abort —
"could not tell" must never read as "clean") · **3** notes drifted (warn), which now also
covers two trees that **both** carry local wiring and disagree about it. Nothing undetermined
shares a code with anything compared, which is the property that makes asking better than
guessing.

3 is the weight showrunner asked for on that last case and the weight it depends on: `integrate`
blocks on 1 and 2 and never on 3, so a per-tree local override warns rather than stopping a
merge. A worktree with no local wiring while its parent has some stays **clean** — the ordinary
adopted case, and flagging it would make every worktree notes-drifted on day one, which is a
check people stop believing. All four states walked here rather than read: neither, parent only,
both-and-disagreeing, both-and-agreeing. And a drifted harness **script** still wins over a
local-override difference — verified by making both true at once and requiring `code-drifted`,
because a widening that swallows the finding it was added beside is the failure both sides of
this boundary have now shipped once each.

**1 said "rule files drifted" here until game_loop #66, and that was a claim about the layer
below outliving the thing it described.** A drifted harness SCRIPT had no entry in that map and
fell to the default 2, so showrunner aborted for the right reason under the wrong name. The
suite now reads `WORKTREE_EXIT` back out of the installed payload and fails when this table and
that map disagree, because a number this side branches on is not something to keep in prose
alone. The same release added `false_clean_before_fix` to the JSON: true exactly when a harness
before the fix would have called the tree clean at exit 0. It is retrospective and it cannot be
re-derived — the verb compares trees as they are now, so once a branch is merged the tree that
was certified is gone. showrunner carries it out of `check_tree` and into `reconcile` and
`integrate` for that reason.

## Open upstream, measured here and NOT patched here

**`verify` runs the same command once per matching pattern.** Five of this repo's six rules owe
`python3 test/run.py`, so a commit touching `lib/` and `test/` — which is most commits — runs the
suite twice.

Measured rather than reasoned: two stale patterns, **117s**, against ~58s for one run. Exactly
half that gate is a duplicate, and a commit also touching `install.sh` and `templates/` pays
four times over.

The config comment beside those rules already argues the shape, against a catch-all `"*"`:
*verify runs each stale pattern independently.* Avoiding `"*"` avoids the worst case; it does not
avoid two ordinary globs legitimately owing the same command, which is the normal arrangement
when code and its tests are separate patterns.

**Not patched here, deliberately.** `.game_loop/` is vendored and read-only by the rule at the
top of this file — a local edit would be overwritten by the next installer run and would drift
silently until then. Reported upstream, where llm_chat's owner had independently found it in a
three-pattern config; this repo is the worse case at five.

Their suggested fix, which showrunner does not get a vote on beyond agreeing it is sound:
dedupe by (command, resolved cwd) within one invocation, and mark **every** stale pattern fresh
rather than only the first — marking one would leave the others stale forever and turn a
speedup into a gate that never passes. A command is a pure function of the tree at the moment it
runs, so a second identical run in the same invocation is not a second check.

## Closed asks

| # | Outcome |
|---|---|
| [#29](https://github.com/SupposedlySam/game_loop/issues/29) | Built as designed. Two operational rules encoded in `gates.attribution()`: a clean merge auto-commits and never invokes the gate (a declaration spent there is wasted and the *next* commit goes bare), and attribution must be declared **after** the branch has its commit or it resolves to zero files. |
| [#30](https://github.com/SupposedlySam/game_loop/issues/30) | Mechanism confirmed, **precondition refuted** — only `verify.yaml` seeds from `templates/`; `config.json` and `INVARIANTS.md` seed from game_loop's own `.game_loop/`. And showrunner's rule-file list was already incomplete: `install.sh` owns four files, and the notes tier (`LEDGER.md`) was invisible to it. `DEFAULT_RULE_FILES` is deleted. |
| [#31/#33](https://github.com/SupposedlySam/game_loop/issues/31) | game_loop's own; no showrunner stake. |
| [#32](https://github.com/SupposedlySam/game_loop/issues/32) | Filed against game_loop, answered from here: `showrunner waiting` exits 0 on a live Crawler PID or an explicit park. Conservative in the opposite direction to the rest of showrunner — when in doubt it reports **not** waiting, because a false "waiting" silences a watchdog on exactly the wedged run it exists to catch. |

## Two corrections showrunner had to make to itself

**Never copy the hook-registration file.** `install.sh` *merges* its hooks into
`.claude/settings.json`, preserving the project's `statusLine`, permissions and unrelated
hooks — and it warns about pre-existing **non-game_loop** hooks on the events it manages,
because a stray `Stop` hook from an older harness runs alongside and the two fight over
turn-ends. That presents as "the orchestrator is mysteriously flaky." A wholesale copy
discarded the settings and silently dropped the warning.

**Never write git's shared exclude file.** `git rev-parse --git-path info/exclude` resolves to
the **common** git dir from inside a linked worktree, and git honours no per-worktree
equivalent. So excluding a path "for one Crawler" silently changed the ignore rules of the main
checkout and every sibling — this project's own INV9 landing on its own code: a shared
single-consumer resource nobody had named, mutated at every spawn. Replaced with verification:
a path that would be staged is a refusal telling you to put it in the repo's tracked
`.gitignore`, which crosses into every worktree by itself.

## On the shrink test

The handoff for #30 said: *when this lands, `harness.py` should shrink, not grow — if it grows,
the fix went to the wrong layer.* Reporting it honestly: the file **grew**, 200 → 253 code
lines, even after deleting a speculative fallback that INV4 did not justify.

The test was measuring the wrong thing. What went to zero is the part that mattered:
`DEFAULT_RULE_FILES`, the byte-comparison, the notes-tier blindness, and the settings-merge
reimplementation — every place showrunner modelled game_loop's internals. What grew is
*plumbing to call the other layer*: invoking an installer, mapping four exit codes, and refusing
when hook registration would be absent. Calling a boundary costs more lines than guessing at it,
and is still right.

The sharper test for next time, and the one to use: **does showrunner still contain a claim
about the other layer that only the other layer can validate?** Line count is a proxy that
fails; that question does not.

## Verified end to end, not by inspection

Both claims that used to rest on reading source have now been driven against live hooks by
game_loop and reported back:

- **The commit gate denies in a linked worktree, using THAT tree's rules.** A worktree whose
  `verify.yaml` owed an impossible check was denied; the main checkout's clean manifest was
  never consulted. #28 holds under a real drive.
- **The provenance check's third bucket is exact** — with a caveat game_loop volunteered and
  which is worth keeping precise. A file touched by no merged ref and by no session edit was
  named alone: *"COMMIT INCLUDES 1 FILE NOTHING ACCOUNTS FOR — NOT THIS SESSION, NOT ANY
  ATTRIBUTED MERGE"*. The stronger wording an orchestrator wants already exists and is gated on
  an attribution being live; the generic formatter advice is the *no-attribution* message.
  **That result came from driving the hook with a constructed payload, not from a live
  invocation** — it rides on the same mechanism the worktree run above proves is really called,
  but it is one inch short of the standard the first bullet meets. Recorded at its real
  standing rather than rounded up.

Two gaps came out of it, both filed on game_loop: its refusal says "run verify" without naming
**which tree**, which is ambiguous with N Crawlers, and the stale-check line prints twice.
showrunner mitigates the first where it can — every Crawler's brief now names the absolute
path of the tree whose record can clear its own gate.

## What showrunner assumes about game_loop today

Stated so it can be re-checked rather than trusted, each re-read against the harness actually
installed here. `test/run.py` fails when that version moves, so this list cannot quietly
describe a release nobody is running.

The re-read that stamp forces is scoped by `git diff` over the vendored payload, not by
re-reading everything: the assumptions above cite `guard-writes-impl.sh`, `verify` and
`install.sh` by line, so when those files are byte-identical across an upgrade the citations
cannot have moved. That is the difference between a check being satisfied and a check being
skipped, and it is worth writing down because the skip is the tempting one.

<!-- game_loop-verified: 4104291e — payload digest. THIS is the gated value.
     First carried by release fc0c301e. That release name changes ONLY when the digest above
     changes: the two describe the same event, and a release where the digest did not move did
     not re-verify anything. Overwriting it on every upgrade was done twice here by updating
     both fields together out of habit — an ungated number drifting beside a gated one, inside
     the marker whose own caveat says not to. Neither is a claim about what is installed now;
     `.game_loop/VERSION` answers that and is not prose. -->


- The commit gate resolves **per target tree** — the resolution lands in `commit_root`, and the
  no-harness branch then carries it in `_gl_unharnessed` — and denies in two cases: when that
  tree carries no harness (`guard-writes-impl.sh:871`, where the `undetermined:` resolution
  becomes `_gl_unharnessed`), and
  when the target is built from a **shell variable** so the gate cannot resolve it without
  executing it (line 852). The second is newer than the first — it used to pass silently,
  which is the default shape under fan-out.
- A **FOURTH** denial, newest of all and the only one keyed on a *mandate* rather than on a
  path: a Bash `rm` carrying BOTH recursive and force, while an unparked mandate is live
  (`guard-writes-impl.sh:689`). The reasoning is not about the delete — it is that an
  interactive approval is the one hazard in an unattended run that fails SILENT: no timeout, no
  log line, and the watchdog cannot answer it because a run blocked on a prompt is not idle.
  Both flags are required, in any spelling (`-rf`, `-r -f`, `--recursive --force`), and the
  argv is lexed rather than pattern-matched — unbalanced quotes make it say nothing rather than
  guess at a command.

  **This bullet exists because the enumeration above was wrong the moment the harness moved.**
  It read "denies in two cases" plus "a third" against a payload that had grown a fourth, and
  the payload stamp is what caught it — not a reader noticing. An enumeration is complete on the
  day it is written.

- A **third** denial, newer than both and not on the commit path at all: a Write or Edit to
  `config.json`, `INVARIANTS.md` or `verify.yaml` **when the file already exists** (line 432).
  The discriminator is existence, so seeding an absent one is provisioning and passes —
  verified here in a real tree by moving the file aside and re-running the same payload, since
  an orchestrator that provisions worktrees is the party that breaks if that arm is wrong.
  It **now also covers `config.local.json`, and that one is denied whether or not it exists** —
  nothing seeds it, so creating one is the same act as editing one. Written down because the
  sentence here said the opposite for exactly one commit: showrunner found that file merged
  into the same policy with UNION semantics on the trust lists, reported it upstream rather
  than shadowing it with a comparison of its own, and game_loop closed it the same day. A
  boundary note is a measurement, and this one aged in hours.

  The asymmetry that makes it usable: the guard is a PreToolUse hook on a *session's* tool
  calls, so showrunner writing that file into a worktree from its own process is unaffected.
  Verified here, same path both ways — the orchestrator's write lands, the session's is denied.
- The edited-file set is scoped to the **session**, not the tree — one session is one session
  however many trees it touches. (`EDITED_F` at line 263.)
- The blast-radius check is a **warning, never a denial** — `blast_note` reaches `commit_note`
  and nothing else (`blast_note` assigned at 969, consumed at 1464) — and is silent when the
  session's `edited` set is empty, which is no evidence rather than a clean bill (line 984,
  stated as design at 966).
**The two below cite `install.sh` by ANCHOR, not by line, and that is a correction.** It is
the one file here that is not vendored — it lives in game_loop's own checkout — so the payload
digest does not move when it changes and the cited-line check cannot read it. They carried line
numbers anyway, which claimed a precision nothing on this side could maintain, and one of them
slid on the very next upgrade: the hooks citation was three lines off within a day and no check
here noticed. A number you cannot verify is not more precise than a phrase you can grep, it is
only more confident.

- `install.sh` seeds user-owned files **only if absent** — the comment beginning `Seed the
  user-owned files only if absent` — and ships `templates/verify.yaml` empty: 0 non-comment
  lines, re-measured rather than recopied. Verified end to end here, not read: a parent repo
  with a distinctive marker in its `verify.yaml`, a bare worktree, install, and the marker
  arrives — so the seed is the parent's rules and not the blank template.
- Hooks are merged into `<project>/.claude/settings.json` — the block beginning `Merge the
  hooks into .claude/settings.json` — so a worktree without one has no rails at all.
- Adopting a parent also carries that parent's **own** `.game_loop/bin/` project scripts, with
  the executable bit, because a `verify.yaml` rule naming a command absent from the tree is a
  gate that cannot run. Extras only: a divergent parent payload does **not** overwrite the
  shipped one. Tested by diverging a parent's `bin/game_loop` deliberately and confirming the
  worktree took the installer's copy while the project script still crossed — the first check
  compared two trees already on the same version and would have passed either way.

If any of these stops being true, `lib/showrunner/harness.py` and the shared-state audit in
`lib/showrunner/worktree.py` are the two places that need re-reading.
