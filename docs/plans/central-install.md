# Plan — `install.sh --central`: one copy of the code, every project's own config

**Status:** plan, not built. Nothing below is implemented.
**Default is unchanged.** Per-project install stays the default and the documented path. Central is
opt-in, one flag, reversible by re-running the installer without it.

Cited by symbol, not by line — six files moved between drafting and pushing this.

**Read against:** game_loop's `install.sh --central` branch and
`templates/central-shims/game_loop`, in the game_loop source checkout, on 2026-08-14. Note the
limit, because this file is in the stamped claim set and the stamp does not reach it: those two
live in game_loop's **installer**, and `docs/BOUNDARY.md`'s payload digest covers `.game_loop/bin/`
only. A change to the shim's fail-open posture would move no byte the digest hashes and nothing here
would go red. Re-read them before building step 3.

Today every project that installs showrunner gets its own copy of `lib/showrunner/` and
`bin/showrunner`, and from that moment the copies drift. There is no upgrade path that is not
"re-run the installer in N repos and hope you remembered them all."

The split this wants is one showrunner already has: **code is machine-wide, config and state are
per-project.**

---

## The premise, checked — including the part that makes this easy

| Claim | Where | Verdict |
|---|---|---|
| `bin/showrunner` resolves its library **beside itself** (`__file__`-relative) | `bin/showrunner` | Holds. This is the *only* location-dependent thing in the tool. |
| Config, locks, campaign and graph resolve from **cwd via git**, never from the script | `config.load` → `config.find_root` → `util.main_checkout` (`--git-common-dir`) | Holds. |

Those two rows together are the finding: **showrunner is already split along exactly the line
central mode needs.** Code is `__file__`-relative; state is git-relative. Moving the code does not
move the state, and no config-resolution change is required. This was the risk worth checking first
and it came back clean.

| Claim | Where | Verdict |
|---|---|---|
| `.showrunner/bin` and `.showrunner/lib` are tracked in this repo | `git ls-files .showrunner` | **Refuted.** They do not exist here at all. This repo runs the tracked source layout `bin/showrunner` + `lib/showrunner/`; the installed layout is what *consumers* get. |
| A consumer's `.showrunner/bin` + `lib` are either tracked or ignored | `install.sh`, `.showrunner/.gitignore` | **Refuted, and it is a live bug.** The ignore file covers runtime state only — `graph.db`, `locks/`, `scratch/`, `campaign.json`, `routing.jsonl`, `baseline.json`, `integration-commit.json`. `bin/` and `lib/` are **neither tracked nor ignored**, so a consumer's next `git add -A` commits several thousand lines of showrunner into their repo, silently. Re-verified against `origin/main` after the 18-commit catch-up: still true. |

---

## The argument this plan lost, and it should be recorded rather than quietly dropped

The first draft led with a different motivator: `brief.py` handed every Crawler bare `showrunner
close ...` commands, `dispatch.launch` starts that Crawler with `cwd=<worktree>`, `.showrunner/bin`
is untracked so `git worktree add` never carried it, and `showrunner` is not on `PATH`. Every
printed command named something that could not resolve where the Crawler stood — the proof-of-done
gate reached by a command the agent cannot run.

**That was true when drafted and is fixed on `origin/main`** by `c7437f7`, independently and
better than this plan proposed. `brief.sr_bin` now resolves the binary against the filesystem and
names it **absolutely** — installed copy first, then the repo's own `bin/`, then the canonical path
anyway so the error message still names the right place. Its docstring makes the point this plan
was going to make the hard way:

> Nothing needs copying: `config.load` resolves the main checkout from `--git-common-dir`, so one
> binary serves every worktree, one graph, one campaign.

One absolute path serves every worktree. So **the "provision showrunner into each worktree" step is
deleted from this plan**, not deferred — it was solving a problem that no longer exists, and a
build step that survives its own justification is how a plan turns into a pile.

What remains is the feature that was asked for and nothing borrowed: N copies of the code drift,
and there is no upgrade path. That is a weaker argument than the dead-binary one, and this section
exists so nobody re-derives the strong version and finds it already spent.

---

## The design

Mirrors game_loop's, which is already working on this machine (`install.sh --central`,
`templates/central-shims/`). Where it differs, it differs for a stated reason.

### One shim, not five

game_loop needs five shims because it has five hook entrypoints. showrunner has exactly one
executable, so central mode writes one file:

```
.showrunner/bin/showrunner     # ~20 lines of bash, machine-agnostic, no baked absolute path
```

It resolves `SHOWRUNNER_CENTRAL` (default `~/.claude/showrunner-central`) and execs the central
binary. No `SHOWRUNNER_HOME` equivalent is needed — unlike game_loop, showrunner already finds its
project from the cwd's git root, so the central binary run from a consumer repo reads that repo's
`.showrunner/config.json` without being told.

This shim and the tracked hook shim in [`worktree-lease.md`](worktree-lease.md) are the same
resolver wearing two hats. Build one.

### Populating central

`showrunner self --pin <ref> --dest <path>`, mirroring game_loop's: extract `bin/` and `lib/` at a
**named git ref** into the central directory, stamp `VERSION` with the resolved SHA and a `PINNED`
marker with the ref, SHA and time.

Pinned to a ref, not copied from a working tree, so "what is central running" has an answer that is
a commit and not a vibe. That is the fact `doctor` and the campaign record cite below.

### `--central` also fixes the ignore hole

The `--central` branch appends `bin/` and `lib/` to `.showrunner/.gitignore`. Not tidiness: it is
what makes Objection A below inapplicable **by construction** rather than by argument. Re-running
the installer without `--central` removes those lines and restores the local copies, so the mode is
reversible in both directions.

---

## Answering this repo's own rejection of `--central`

Commit `8a51bf1` declined `install.sh --central` for game_loop **in this repo**, with two specific
reasons. Both were right about game_loop here. Neither transfers unexamined, and the second mutates
into something real this plan has to answer.

**Objection A — "committing shims ships every clone a harness that enforces nothing."**
Load-bearing for game_loop because `.game_loop/bin/` is *tracked here on purpose* — tracking is what
makes a Crawler worktree carry the harness. Under central those tracked files become shims that fail
open on a machine with no central install.

Does not apply: `.showrunner/bin` is not tracked, in this repo or in the installed layout, and
`--central` adds it to `.gitignore` explicitly. A clone of a centrally-wired consumer repo receives
no shim, so it cannot receive one that enforces nothing. **A difference in the repositories, not a
difference of opinion, and checkable with `git ls-files`.**

**Objection B — "worktree drift detection compares scripts between trees; under central both trees
carry identical shims, so the comparison passes trivially. A check that cannot fail is not a weak
check."** The sharper objection, and the one this project keeps rediscovering — `config.validate`
deleted a predicate with no failing input for the same reason.

The literal form does not apply: `harness.check_tree` compares **game_loop**, not showrunner, and it
asks the harness itself rather than comparing files.

But the shape does, in a new place, and worse than the original:

> **Central code can be swapped mid-campaign.** A `self --pin` while Crawlers are running changes
> what every one of them executes, in every tree at once, with nothing comparing anything. Under the
> per-project install a mid-campaign upgrade reaches one repo; under central it reaches the whole
> machine. Objection B was right about the mechanism and aimed at the wrong target.

**The answer, required here and not deferred:** record the resolved central SHA in the campaign
record at spawn, beside `boot`, `session` and `base_sha`, which are there for exactly this class of
reason. `reconcile` reports a Crawler whose central SHA differs from the running one. `integrate`
**refuses** — merging work certified by a different version of the gate is the same error as merging
work certified by a drifted harness, which `integrate` already refuses. Same failure, same refusal,
one more input.

That check *can* fail, which is the point.

**Objection C, new and specific to showrunner — the fail posture.** game_loop's shim fails open for
hook entrypoints and loud for everything else, and that is right there. showrunner's hook-shaped
verbs are `lock guard`, `stop-gate`, and the proposed `worktree guard`. "Fails open" for
`lock guard` sounds like the single-consumer mutex silently switching off, which is precisely INV8's
prohibition.

It is not, and `locks.py` says why in source rather than in argument: *routing and guarding are
optimisations; the lock is the guarantee, and only where the consumer itself takes it (`run`).*
`lock run` is not a hook, so it fails **loud**. A missing central install costs the optimisation and
keeps the guarantee.

`worktree guard` has no such fallback — it is teeth or nothing. So:

- **Hook verbs** exit 0 when central is unreachable. A PreToolUse that hard-fails blocks its own fix
  (INV5), and a wedged agent under a mandate is how a bypass starts looking reasonable.
- **Every other verb** fails loud, naming the missing path and the command that populates it.
- **`doctor` carries the loudness the hooks cannot.** It reports the mode, the resolved central path,
  reachability and the pinned SHA — and **errors** when central mode is configured and unreachable.
  `doctor` already errors when the binary every brief names does not resolve; this is the same check
  one level out.
- **`worktree enter`** prints one line into agent context when the guard is inert.

---

## What this does not cover

- **A stale central install is still a single point of failure.** One bad `self --pin` reaches every
  project on the machine at once. The pinned-SHA check makes that *visible* to a running campaign; it
  does not make it *impossible*, and nothing here should be read as saying it does.
- **Nothing is verified about the central copy's contents.** `self --pin` extracts a git ref; it does
  not check signatures and does not check that `~/.claude/showrunner-central` was not edited
  afterwards. `VERSION` names what was extracted, not what is there now. A content hash is a later,
  separate question.
- **Multi-user machines are out of scope.** Central defaults under `$HOME`.
- **The ignore-hole fix does not un-commit anything.** A consumer who already committed
  `.showrunner/bin` and `lib` keeps them. The installer should say so rather than imply a clean tree.
- **This does not make showrunner a global command.** The entrypoint is still
  `./.showrunner/bin/showrunner`. Central changes what that file *is*, not where you type it.

---

## Tests

- The shim is byte-identical across projects — asserted, because "machine-agnostic" is the property
  that lets it be committed by a consumer who ignores our advice.
- Central resolution honours `SHOWRUNNER_CENTRAL` and falls back to the documented default.
- **Absence asserted in both directions.** Central unreachable: hook verbs exit 0, non-hook verbs
  exit non-zero with the populate command in the message, `doctor` returns an *error*. Pair every one
  with the present-and-reachable case — an absence-only suite passes against a build that does
  nothing.
- Config still resolves to the consumer repo when the code lives elsewhere, asserted **from inside a
  linked worktree**, since that is the case `--git-common-dir` exists for.
- `integrate` refuses a Crawler whose recorded central SHA differs from the running one, and
  **proceeds** when they match. The refusal alone does not test the comparison.
- Round-trip: `--central` then a plain re-install restores local copies and removes the ignore lines.
  Reversibility is a claim; claims get asserted.

---

## Build order

1. Fix the ignore hole in `install.sh`. Independent of everything else, a live bug today, and worth
   landing whatever happens to the rest of this.
2. `showrunner self --pin --dest`, plus `VERSION`/`PINNED` stamping. Nothing consumes it yet.
3. The shim template and the `install.sh --central` branch, including the ignore lines and the revert
   path. Shares its resolver with the lease plan's hook shim.
4. `doctor`: mode, resolved path, reachability, pinned SHA — error when configured and unreachable.
5. Central SHA in the campaign record; `reconcile` reports; `integrate` refuses.

Step 1 stands alone and should not wait on the decision about central.
