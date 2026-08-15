---
name: sr-install
description: Install showrunner into a project, or check and refresh an existing install. Detects whether this machine has a central install and what mode you prefer, so it asks nothing it can already answer. If the project is already installed it runs doctor first, then offers — leave it, upgrade the code in place, or wipe and reinstall fresh (destructive, and never done without naming what is lost). Use when the user says "install showrunner here", "set up showrunner in this project", "reinstall showrunner", "upgrade showrunner", "sr-install", or runs /sr-install.
---

# sr-install

Get showrunner into a project, or work out what to do about the one already there.

## Non-negotiables

- **Detect before asking.** Whether a central install exists, what it is pinned at, what mode this
  project is already in, and what mode the user prefers are all *readable*. Ask only what is left.
- **Already installed → `doctor` first, then ask.** Never reinstall over a working install because
  the word "install" was used. The answer is often "nothing to do", and that is a real answer.
- **"Fresh" is destructive and is never the default.** A wipe destroys the leaf graph, the campaign
  record, the baseline and the project's own `config.json`. Name exactly what dies, get an explicit
  yes, and refuse while any Crawler is live or any worktree is dirty.
- **A preference is not a fact.** When the saved preference and the machine disagree — preference
  says central, central is gone — say so and ask. Never let a stored default quietly override what
  is actually there.
- **Never `git add`, never commit, in the target repo.** The installer says to commit the guard
  shim; that commit is the user's.
- **One line per step.** Echo the command, print the result. The installer's output is already good
  — do not re-narrate it.

## 1. Where is the installer, and which project

**The installer** lives in the showrunner checkout. Find it from this skill's own location — the
skills live inside that checkout and reach `~/.claude/skills` by symlink:

```sh
cd "<this skill's base directory>" && cd ../../.. && pwd    # → the showrunner checkout
```

It must contain `install.sh` and `bin/showrunner`. If it does not (the skills were copied, not
linked), ask where the checkout is. Do not install from a central pin — `self --pin` extracts `bin/`
and `lib/` only, so there is no installer there.

**The project** is the cwd's git toplevel unless the user named another, and it must be a git repo.

**Writes into another repo may be refused** by a harness write-guard. That means you are standing in
the wrong repo: run this from a session inside the target project, or have the user authorize the
path. Do not work around the guard.

## 2. Read the machine before asking anything

Three reads, all cheap, all read-only.

**a. Does a central install exist, and is it sound?**

```sh
CENTRAL="${SHOWRUNNER_CENTRAL:-$HOME/.claude/showrunner-central}"
<checkout>/bin/showrunner self --dest "$CENTRAL"; echo "exit=$?"
```

| exit | what it means | what to do with it |
|:--|:--|:--|
| **0** | populated and consistent; prints the pinned sha and ref | central is available — offer it as the default |
| **2** | `VERSION` and `PINNED` disagree — something edited the directory after the pin | central exists but names nothing trustworthy. Say so; re-pinning is the fix, and it is the user's call |
| **1** | `no pinned checkout at …` — never populated | central is not available. Installing `--central` now leaves the project non-functional until it is populated |

**b. What mode is this project already in?** Only meaningful if `.showrunner/bin/showrunner` exists:

```sh
ls -d <project>/.showrunner/lib/showrunner 2>/dev/null
```

Present → a **local copy**. Absent (with the binary present) → a **central dispatcher shim**. That is
the same signal the installer itself keys on, and it is the answer to "what am I upgrading".

**c. What did the user say last time?**

```sh
cat "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/showrunner-install.json" 2>/dev/null
```

```json
{"mode": "central", "central": "/Users/morgan/.claude/showrunner-central",
 "set_at": "2026-08-14", "set_by": "sr-install", "why": "..."}
```

`central` is an **absolute** path — a `~` here would be a string nothing expands. Missing file → no
preference, ask. Present → use it as the **default**, say you are using it in half a line, and let
them override. Never use it silently.

## 3. Which mode — decided, not asked twice

Precedence, highest first:

1. **What the user just said** in this request ("install it central", "keep it local").
2. **What the project already is** (§2b) — an upgrade never silently flips a project's mode.
   Switching is legitimate and reversible, but it is a decision, so name it: re-running
   `install.sh` *without* `--central` is the deliberate revert from central to a local copy.
3. **The saved preference** (§2c), when it agrees with reality.
4. **What the machine has** (§2a): central populated and consistent → central is the sensible
   default. No central → local, and say why rather than offering a mode that would not work.

Ask only when these leave a real choice — and ask once, in one line, with the reason attached:

> A central install is pinned at `94959a0` and 7 projects already use it. Central here too, or a
> local copy?

**After a new install** — not an upgrade — offer to remember it, once:

> Save `central` as the default for future installs? (writes
> `~/.claude/showrunner-install.json`, one file, delete it any time)

On yes, write that JSON with `mode`, `central`, `set_at` and `set_by`. Do not write it without being
told, do not rewrite it on every run, and if it already says something else, show both and ask which
wins. It is a preference file, not a lock.

## 4. Not installed — install it

| | |
|:--|:--|
| **local copy** | `./install.sh <project>` — the code is copied into `.showrunner/`. Self-contained, depends on nothing outside the repo |
| **central** | `./install.sh --central <project>` — one ~20-line dispatcher shim, no local copy; the code lives once at `$SHOWRUNNER_CENTRAL`. Every centrally-wired project on the machine runs one pinned commit. Reversible |

If central is chosen and §2a said exit 1, say the consequence plainly before running: every non-hook
verb in that project exits 1 until it is populated, and the hook verbs allow *and say* they did not
run. The fix is one command, and it is the user's:
`<checkout>/bin/showrunner self --pin <ref> --dest "$CENTRAL"`.

Also pass a skills flag, because this skill runs non-interactively and the installer's prompt only
appears on a TTY: `--skills` to link `showrunner`, `sr-status`, `sr-doctor` and `sr-install` into
`~/.claude/skills`; `--no-skills` otherwise. If you are running as one of those skills, they are
already installed — `--no-skills` is the honest default.

Run it, print its output as it stands, and add only what it cannot know: what the user must decide
in `config.json` — resources, lanes, checks, inject.

## 5. Already installed — doctor first

```sh
<project>/.showrunner/bin/showrunner doctor; echo "exit=$?"
```

Render it the way `/sr-doctor` does: verdict line, one line per finding as a consequence, `N checks
passed`. Add one line naming the mode from §2b and, under central, the pinned sha from §2a — that is
the thing doctor does not check (CI-04).

Then three options, with **a** stated plainly rather than buried:

**a. Leave it.** Doctor is green, the pin is consistent. Usually the right answer.

**b. Upgrade the code in place** — `./install.sh <project>` again. Idempotent and non-destructive:
replaces `bin/` and `lib/`, refreshes the guard shim, appends missing ignore rules, registers the
guard if it never was, and **keeps `config.json`**. Graph, campaign record and baseline all survive.
Under central, the upgrade for *every* project at once is one `self --pin` — **not** a loop over
`install.sh`, which without `--central` is the revert.

**c. Wipe and reinstall fresh** — §6, only when asked for by name.

## 6. Fresh — the destructive path

Inventory first, and put the numbers in the question:

```sh
<sr> status         # leaves: ready / in progress / done / refuted
<sr> reconcile      # Crawlers on record, and whether any is LIVE
git -C <project> worktree list
```

**Refuse while any of these hold**, and say which:

- a Crawler's verdict is **LIVE**, **BLOCKED** or **PARKED** — a live session's tree is about to go
- a worktree has uncommitted changes — an abandoned tree may hold the only copy of real work
- a leaf is **in progress** with a claim

The remedy is the campaign's own — `reconcile`, then `reap` (dry run) — never a wipe that outruns it.

When it is genuinely clear, state the loss as facts:

> Wiping `.showrunner/` deletes: the graph (N leaves, M closed), the campaign record (K Crawlers),
> the baseline, and `config.json` — your resources, lanes, checks and inject, which the installer
> re-seeds as defaults and cannot restore.

Explicit yes, then:

```sh
cp <project>/.showrunner/config.json <somewhere the user names>   # if they want it kept
rm -rf <project>/.showrunner
./install.sh [--central] <project>
```

Offer to keep the config every time: it is the one file in there that is theirs, and a "fresh"
install that preserves it is what most people mean.

## 7. After any install

```sh
<project>/.showrunner/bin/showrunner doctor
```

Print the verdict line. Two things are the user's to finish and neither is yours to do:

- **commit `.showrunner/hooks/worktree-guard.sh`** — `git worktree add` copies tracked files only,
  so until it is committed the guard is absent in every worktree, which is the one place it runs
- **`showrunner baseline`** on a known-good tree — without it, integration cannot tell a new failure
  from a pre-existing one

Then stop. `/sr-status` and `/sr-doctor` cover everything after this.
