#!/usr/bin/env bash
# Install showrunner into a target repo.
#
# Deliberately the same shape as game_loop's installer: copy a payload, touch nothing
# else, and be idempotent. showrunner is Python 3 standard library only — there is nothing
# to compile, nothing to fetch, and no package manager involved (issue #2).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
CENTRAL=0
LOCAL=0
SKILLS=ask
TARGET="."
for arg in "$@"; do
  case "$arg" in
    --central) CENTRAL=1 ;;
    --local)   LOCAL=1 ;;
    --skills) SKILLS=yes ;;
    --no-skills) SKILLS=no ;;
    -h|--help)
      echo "usage: ./install.sh [--local] [--central] [--skills|--no-skills] /path/to/your/project"
      echo
      echo "  --local     install for YOU, not for the team. Hooks go into the untracked"
      echo "              .claude/settings.local.json instead of the source-controlled"
      echo "              .claude/settings.json, and .showrunner/ is excluded LOCALLY."
      echo "              Nothing this installs then reaches anybody who clones the repo."
      echo "              Without it the install is SHARED, which is the default on purpose:"
      echo "              the payload and the gates are committed and everyone gets both."
      echo "  --central   do not copy the tool's code into the project at all. Write one tiny"
      echo "              dispatcher shim that execs a shared, machine-wide install instead."
      echo "              Populate that with \`showrunner self --pin <ref> --dest <path>\`."
      echo "              Opt-in and REVERSIBLE: re-run without --central to restore local copies."
      echo "  --skills    also link the Claude Code skills (showrunner, sr-status, sr-doctor,"
      echo "              sr-install) into ~/.claude/skills — the ONLY thing this script writes"
      echo "              outside the target repo. Without a flag it asks, and only on a TTY."
      echo "  --no-skills never touch ~/.claude, and do not ask."
      exit 0 ;;
    -*) echo "install.sh: unknown option $arg" >&2; exit 64 ;;
    *)  TARGET="$arg" ;;
  esac
done

if [ ! -d "$TARGET" ]; then
  echo "install.sh: no such directory: $TARGET" >&2
  exit 64
fi
TARGET="$(cd "$TARGET" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "install.sh: python3 is required (standard library only — no packages)." >&2
  exit 70
fi
if ! git -C "$TARGET" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "install.sh: $TARGET is not a git repository. showrunner orchestrates a repo: it puts" >&2
  echo "  Crawlers in git worktrees inside it." >&2
  exit 64
fi
TARGET="$(git -C "$TARGET" rev-parse --show-toplevel)"

echo "Installing showrunner into: $TARGET"
mkdir -p "$TARGET/.showrunner/bin"

# WAS this repo centrally wired BEFORE this run? Decided HERE, before anything below overwrites
# the evidence. A central install writes the shim and deliberately leaves no lib/, so lib/'s
# absence beside an existing binary is the signal — the same shape game_loop uses, keyed on the
# file its own central branch never writes.
WAS_CENTRAL=0
if [ -f "$TARGET/.showrunner/bin/showrunner" ] && [ ! -d "$TARGET/.showrunner/lib/showrunner" ]; then
  WAS_CENTRAL=1
fi

SRC_CENTRAL="${SHOWRUNNER_CENTRAL:-$HOME/.claude/showrunner-central}"
if [ "$CENTRAL" = 1 ]; then
  # Do not copy the tool at all — write one dispatcher shim that execs a shared copy. The shim
  # is machine-agnostic, so it is byte-identical in every project wired this way.
  cp "$SRC/templates/central-shims/showrunner" "$TARGET/.showrunner/bin/showrunner"
  chmod +x "$TARGET/.showrunner/bin/showrunner"
  # REMOVE WHAT A PRIOR LOCAL INSTALL LEFT. Switching modes must not leave a dead copy of the
  # library sitting beside a shim that ignores it: two copies of the code where one is
  # unreferenced is exactly the drift this mode exists to end, arriving inside the fix.
  rm -rf "$TARGET/.showrunner/lib"
  echo "  wrote   .showrunner/bin/showrunner — ONE dispatcher shim, no local copy of the tool"
  if [ -x "$SRC_CENTRAL/bin/showrunner" ]; then
    echo "  central install found at $SRC_CENTRAL — reachable right now"
    # AND WHETHER THE SHIM WILL RESOLVE THE SAME PATH, which is a different question. Nothing
    # here persists SHOWRUNNER_CENTRAL: the shim re-reads it at run time, and a Claude Code hook
    # process does not carry this shell's environment. So an install done with a custom path
    # reported success and then resolved $HOME/.claude/showrunner-central at every hook.
    if [ -n "${SHOWRUNNER_CENTRAL:-}" ] && \
       [ "$SRC_CENTRAL" != "$HOME/.claude/showrunner-central" ]; then
      echo "  ⚠ that path came from SHOWRUNNER_CENTRAL in THIS shell, and nothing records it."
      echo "    The shim re-reads that variable at run time and a hook process will not have it,"
      echo "    so hooks will look in $HOME/.claude/showrunner-central instead. Export it from"
      echo "    your shell profile, or pin to the default path."
    fi
  else
    echo "  ⚠ NO central install at $SRC_CENTRAL yet. Until one exists, every non-hook verb in"
    echo "    this repo exits 1, and the hook verbs allow AND SAY they did not run."
    echo "    Populate it:  $SRC/bin/showrunner self --pin <ref> --dest $SRC_CENTRAL"
  fi
else
  mkdir -p "$TARGET/.showrunner/lib"
  cp -R "$SRC/lib/showrunner" "$TARGET/.showrunner/lib/"
  cp "$SRC/bin/showrunner" "$TARGET/.showrunner/bin/showrunner"
  chmod +x "$TARGET/.showrunner/bin/showrunner"
  echo "  copied  .showrunner/bin/showrunner + .showrunner/lib/showrunner/"
  if [ "$WAS_CENTRAL" = 1 ]; then
    echo "  reverted from central dispatch to a local copy — this repo no longer depends on"
    echo "  a central install. Reversibility is the whole reason --central is safe to try."
  fi
fi

# THE ONE PART OF THE PAYLOAD THAT IS MEANT TO BE COMMITTED. bin/ and lib/ above are the tool
# and are ignored; this is a few lines of machine-agnostic bash that must be TRACKED, because
# `git worktree add` copies tracked files only and a hook that does not cross into a worktree
# cannot guard one. It names no absolute path and no machine, so it is safe to commit — that
# is the whole reason the registration points at a shim instead of at the binary.
mkdir -p "$TARGET/.showrunner/hooks"
# ONE LIST, LOOPED. This was three `cp` lines naming three files, and the registration wrote
# FIVE — so `whoami.sh` and `dispatch-guard.sh` shipped in the payload, got registered, and were
# never copied. A consumer reported a PreToolUse dispatch guard that was REGISTERED AND ABSENT,
# which is worse than unregistered: the registration is what makes it look present, and doctor
# reported registration rather than existence, so the diagnostic agreed with the appearance.
# A per-file `cp` needs somebody to remember; a list is the thing the suite can compare against
# what the registration actually names.
for hook_name in worktree-guard.sh inert-crawler-gate.sh waiting-probe.sh whoami.sh \
                 dispatch-guard.sh pipeline-status-gate.sh reach-gate.sh; do
  cp "$SRC/.showrunner/hooks/$hook_name" "$TARGET/.showrunner/hooks/$hook_name"
  chmod +x "$TARGET/.showrunner/hooks/$hook_name"
  case "$hook_name" in
    worktree-guard.sh)      note="COMMIT THIS — it must cross into worktrees" ;;
    inert-crawler-gate.sh)  note="Stop: refuses a turn-end while a Crawler is inert" ;;
    waiting-probe.sh)       note="answers a harness watchdog; NOT wired by this script — arming a wait is a human decision, see the note at the end" ;;
    whoami.sh)              note="SessionStart + PostCompact: announces the derived seat" ;;
    dispatch-guard.sh)      note="PreToolUse on Bash: refuses a raw dispatch from a seat that may not create one" ;;
    pipeline-status-gate.sh) note="PreToolUse on Bash: notices when \$? is about to read a pipe's truncator instead of the command" ;;
    reach-gate.sh)          note="PreToolUse: names the showrunner mechanism for what a call reached for; advice only, never refuses" ;;
  esac
  echo "  copied  .showrunner/hooks/$hook_name ($note)"
done

if [ ! -f "$TARGET/.showrunner/.gitignore" ]; then
  cat >"$TARGET/.showrunner/.gitignore" <<'EOF'
# showrunner RUNTIME state — not source. config.json IS source; commit it.
graph.db
graph.db-*
locks/
scratch/
campaigns/
campaign.json
routing.jsonl
waiting.jsonl
events.jsonl
hook-heartbeat.jsonl
fail-open.jsonl
*.lock
baseline.json
integration-commit.json
# Machine-specific overrides. The docs point people here for absolute paths, and without this
# line the file lands NEITHER TRACKED NOR IGNORED -- the exact state doctor flags elsewhere.
# Reported by a consumer who had to use .git/info/exclude instead, and who was right that an
# exclusion living outside the payload vanishes on the next upgrade.
config.local.json
seen-issues.json
EOF
  echo "  wrote   .showrunner/.gitignore (runtime state ignored; config.json is source)"
fi

# THE TOOL IS NOT THE PROJECT'S SOURCE, and until now nothing said so in a place git reads.
# bin/ and lib/ were neither tracked nor ignored, which is the one combination with no visible
# state: `git status` lists them as untracked and the next `git add -A` commits them. Measured
# on a fresh install into an empty repo — 31 paths staged, showrunner's whole library among
# them. A consumer ends up carrying a vendored copy they never chose, and the copy is what
# then drifts.
#
# `lib/` also covers __pycache__/, and that is the only thing that can: this script deleting
# the copied bytecode was tried and is theatre, because the `init` below imports the library
# and Python writes it straight back — as does the consumer's very first showrunner run. An
# ignore rule holds for the whole lifetime; a delete holds until the next import.
#
# APPENDED, not written only on creation, because the guard above fires once per repo and
# every already-installed consumer is exactly the population that has the hole. Each line is
# added only if absent, so re-running is idempotent and a consumer who deliberately deleted
# one gets it back — noted rather than hidden, since the alternative is an installer that
# silently honours an edit that re-opens the hole.
sr_ignore="$TARGET/.showrunner/.gitignore"
sr_added=0
# EVERY tool-owned entry, not just the two added last. The heredoc above runs ONLY when the
# file is absent, so on an UPGRADE it is skipped entirely -- which meant config.local.json
# reached fresh installs and never reached anybody who already had showrunner. A consumer found
# it by running `git check-ignore -v` rather than believing me that it was fixed. bin/ and lib/
# got here only because they happened to be added through this loop instead.
#
# Ensure-present rather than rewrite: these are the TOOL's policy about its own runtime files,
# so converging a consumer's file to the full set is correct, while clobbering the file would
# discard entries they added. `grep -qxF` makes it idempotent, so re-running adds nothing.
for entry in "bin/" "lib/" "graph.db" "graph.db-*" "locks/" "scratch/" "campaigns/" \
             "campaign.json" \
             "routing.jsonl" "waiting.jsonl" "events.jsonl" "*.lock" "baseline.json" \
             "integration-commit.json" "config.local.json" "seen-issues.json" \
             "hook-heartbeat.jsonl" "fail-open.jsonl"; do
  if ! grep -qxF "$entry" "$sr_ignore" 2>/dev/null; then
    if [ "$sr_added" = 0 ]; then
      printf '\n# showrunner runtime state and the tool itself, added by install.sh. Present on a\n# fresh install and topped up on every upgrade, because a rule that only lands when the\n# file is first created never reaches anybody who already had the tool.\n' >>"$sr_ignore"
    fi
    printf '%s\n' "$entry" >>"$sr_ignore"
    sr_added=$((sr_added + 1))
  fi
done
if [ "$sr_added" -gt 0 ]; then
  echo "  ignored $sr_added runtime path(s) in .showrunner/.gitignore (topped up on upgrade)"
fi

# OUTSIDE the block above, and a test is why. Nesting this under "we just added the rule" meant
# a consumer whose ignore file was already correct — every upgrade after the first — was never
# told, even while git went on tracking the tool. Being tracked is the condition that matters,
# not the instant the rule arrived, and an ignore rule does NOT untrack what is already
# committed. Saying "ignored" without saying this is a remedy that silently does nothing.
# The same idempotent append for RUNTIME state added since this file was first written, kept
# separate from the block above because that one's header says "the TOOL, not this project" and
# these are neither — they are observations a campaign regenerates by running. An
# already-installed consumer is again the population with the hole.
sr_rt=0
for entry in "waiting.jsonl" "events.jsonl" "*.lock"; do
  if ! grep -qxF "$entry" "$sr_ignore" 2>/dev/null; then
    if [ "$sr_rt" = 0 ]; then
      printf '\n# Runtime state added after this file was first written.\n' >>"$sr_ignore"
    fi
    printf '%s\n' "$entry" >>"$sr_ignore"
    sr_rt=$((sr_rt + 1))
  fi
done
if [ "$sr_rt" -gt 0 ]; then
  echo "  ignored $sr_rt runtime path(s) added since this repo was installed"
fi

if git -C "$TARGET" ls-files --error-unmatch .showrunner/bin >/dev/null 2>&1; then
  echo "  ⚠ .showrunner/bin is ALREADY TRACKED here, and an ignore rule does not untrack it."
  echo "    git keeps updating it on every pull, and your copy keeps drifting. To stop:"
  echo "      git -C $TARGET rm -r --cached .showrunner/bin .showrunner/lib"
fi

if [ -f "$TARGET/.showrunner/config.json" ]; then
  echo "  kept    .showrunner/config.json (already present — not clobbered)"
else
  # RUN THE INSTALLER'S OWN BINARY, not the one just placed in the target. Under --central the
  # placed file is a shim, and if no central install exists yet it exits 1 — so a fresh
  # `--central` install would abort here, on `set -e`, having written everything correctly.
  # The source binary always works, and `init` resolves the project from the CWD, so it writes
  # the target's config either way. It also declines to place a binary over one that is already
  # executable, so this does not undo the shim.
  # --local REACHES `init` TOO, and forgetting it is how the flag half-works: `init` registers
  # the hooks itself, so a --local install whose init ran shared would write the tracked file
  # first and the untracked one second — a tracked diff the flag promised to avoid AND every
  # guard registered twice.
  (cd "$TARGET" && "$SRC/bin/showrunner" init ${LOCAL:+$( [ "$LOCAL" = "1" ] && echo --local )} >/dev/null)
  echo "  seeded  .showrunner/config.json"
fi

# EVERY RUN, not only a fresh one. `init` registers the guard and `init` only runs when there is
# no config — so every ALREADY-INSTALLED consumer took the upgrade path, received the shim file,
# and got no registration: a hook that exists and can never fire, plus a `doctor` error the
# installer itself created. Found by upgrading a real consumer. It is the same shape as the
# ignore rules above, and it is idempotent for the same reason: a second run appends nothing.
# TWO HOOKS NOW, and the line says so. `worktree register` writes both — the PreToolUse guard
# and the Stop trigger that refuses a turn-end while a Crawler sits alive and inert — and a
# report naming only one is how a reader concludes the other was never installed.
REG_ARGS=""
REG_FILE=".claude/settings.json"
if [ "$LOCAL" = "1" ]; then
  REG_ARGS="--local"
  REG_FILE=".claude/settings.local.json"
fi
if (cd "$TARGET" && "$SRC/bin/showrunner" worktree register $REG_ARGS 2>/dev/null | grep -q registered); then
  echo "  hooked  $REG_FILE — PreToolUse (worktree guard) and Stop (inert-Crawler gate)"
fi

# A HOOK REGISTERED IN BOTH LAYERS FIRES TWICE, and the two arrangements compose without either
# one noticing: `init` registers in the tracked file, `--local` adds the same hooks to the
# untracked one, and every guard then runs twice per tool call. Reported rather than repaired —
# the tracked file may belong to a team who registered these deliberately, and deleting their
# entries because one developer passed a flag would be this installer editing shared source
# control on a private decision.
DOUBLE=$(cd "$TARGET" && "$SRC/bin/showrunner" doctor 2>/dev/null | grep -c "registered in BOTH" || true)
if [ "${DOUBLE:-0}" != "0" ]; then
  echo "  ⚠ some showrunner hooks are now registered in BOTH settings layers, so they will fire"
  echo "    twice. \`showrunner doctor\` names which; remove one copy — usually the one you did"
  echo "    not mean to keep."
fi

if [ "$LOCAL" = "1" ]; then
  # THE OTHER HALF OF --local. Hooks in settings.local.json reach nobody, but a committed
  # .showrunner/ still would — the payload, the config, the state.
  #
  # `.git/info/exclude`, NOT the repo's `.gitignore`, and the distinction is the whole promise of
  # the flag. `.gitignore` is source-controlled: appending to it makes a private decision a
  # tracked diff that reaches the team, which is the thing --local exists to avoid. info/exclude
  # is per-clone and needs nobody's agreement. game_loop's --local writes .gitignore; a consumer
  # raised the asymmetry (#81) and they are right, so showrunner takes the stricter one.
  # RESOLVED AGAINST THE TARGET, NOT THE CALLER'S CWD. `rev-parse --git-common-dir` answers
  # RELATIVE to the repo it was asked about — plain `.git` for an ordinary checkout — so joining
  # it to the installer's own working directory silently writes the exclude file of whatever repo
  # the installer happens to be run FROM. Measured the hard way: a hand-run of this wrote
  # `.showrunner/` into showrunner's own .git/info/exclude while the target repo got nothing, and
  # the manual check said it had worked because it read the file it had just wrongly written.
  GITDIR="$(git -C "$TARGET" rev-parse --git-common-dir)"
  case "$GITDIR" in
    /*) ;;                       # already absolute — a separate git dir, or a worktree
    *)  GITDIR="$TARGET/$GITDIR" ;;
  esac
  EXCLUDE="$GITDIR/info/exclude"
  if [ -n "$GITDIR" ]; then
    mkdir -p "$(dirname "$EXCLUDE")"
    # BOTH THINGS THIS INSTALL WROTE, because the promise is "nothing reaches the team" and
    # an UNTRACKED file still reaches them through the next `git add -A`. Claude Code usually
    # ignores settings.local.json already; usually is not a guarantee, and the one repo where
    # it does not is the one where a developer's private hooks get committed under their name.
    sr_excl=0
    for sr_path in ".showrunner/" ".claude/settings.local.json"; do
      if grep -qxF "$sr_path" "$EXCLUDE" 2>/dev/null; then continue; fi
      if [ "$sr_excl" = 0 ]; then
        { [ -s "$EXCLUDE" ] && [ -n "$(tail -c 1 "$EXCLUDE")" ] && echo ""; } >> "$EXCLUDE" 2>/dev/null || true
        echo "# showrunner, installed with --local: this clone only, not the team." >> "$EXCLUDE"
      fi
      echo "$sr_path" >> "$EXCLUDE"
      sr_excl=$((sr_excl + 1))
    done
    if [ "$sr_excl" -gt 0 ]; then
      echo "  excluded $sr_excl path(s) in .git/info/exclude — local to this clone, not a tracked diff"
    else
      echo "  ok      .git/info/exclude already excludes what --local writes"
    fi
  fi
fi

# ------------------------------------------------------------------- skills
# THE ONE THING THIS SCRIPT WRITES OUTSIDE THE TARGET REPO, so it is the one thing it asks about.
# The skills are what makes showrunner reachable without remembering the CLI, and they are global
# by nature: the question "what are my agents doing" gets asked from whichever project you are
# standing in, not from the one that happens to have been installed last.
#
# ASKS ONLY ON A TTY, and defaults to doing nothing. `curl | bash`, CI and the test suite all run
# with stdin closed or piped — an installer that blocks there for an answer nobody can type is an
# installer that hangs, and one that assumes "yes" writes into a HOME nobody offered it.
SKILLS_SRC="$SRC/.claude/skills"
SKILLS_DEST="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
SKILL_NAMES="showrunner sr-status sr-doctor sr-install"

skills_missing() {
  local name missing=""
  for name in $SKILL_NAMES; do
    [ -d "$SKILLS_SRC/$name" ] || continue
    if [ ! -e "$SKILLS_DEST/$name" ] && [ ! -L "$SKILLS_DEST/$name" ]; then
      missing="$missing $name"
    fi
  done
  echo "${missing# }"
}

install_skills() {
  local name linked=0 copied=0 kept=0
  mkdir -p "$SKILLS_DEST"
  for name in $SKILL_NAMES; do
    [ -d "$SKILLS_SRC/$name" ] || continue
    if [ -e "$SKILLS_DEST/$name" ] || [ -L "$SKILLS_DEST/$name" ]; then
      kept=$((kept + 1))
      continue
    fi
    # SYMLINK, NOT COPY, when it can: a copy is a second source of truth that goes stale in
    # exactly the way `--central` exists to stop, and nothing would ever tell you it had.
    if ln -s "$SKILLS_SRC/$name" "$SKILLS_DEST/$name" 2>/dev/null; then
      linked=$((linked + 1))
    else
      cp -R "$SKILLS_SRC/$name" "$SKILLS_DEST/$name"
      copied=$((copied + 1))
    fi
  done
  [ "$linked" = 0 ] || echo "  linked  $linked skill(s) → $SKILLS_DEST (they follow this checkout)"
  [ "$copied" = 0 ] || echo "  copied  $copied skill(s) → $SKILLS_DEST (a COPY — it will not follow this checkout)"
  [ "$kept" = 0 ]   || echo "  kept    $kept skill(s) already in $SKILLS_DEST — not replaced"
}

MISSING="$(skills_missing)"
case "$SKILLS" in
  no)
    # SAID, not silent. "Nothing was written" is also what a failed install looks like, and a
    # flag whose whole effect is an absence should leave a line the reader can point at.
    if [ -n "$MISSING" ]; then
      echo "  note    --no-skills: Claude Code skills NOT installed ($MISSING)."
      echo "          Nothing was written to $SKILLS_DEST. Add them later with:"
      echo "            $SRC/install.sh --skills $TARGET"
    fi ;;
  yes) install_skills ;;
  *)
    if [ -z "$MISSING" ]; then
      : # every skill is already there; nothing to ask about
    elif [ -t 0 ] && [ -t 1 ]; then
      echo
      echo "Claude Code skills for showrunner are not installed for your user:"
      echo "   $MISSING"
      echo "  They are global on purpose — you ask 'what are my agents doing' from whatever"
      echo "  project you are standing in. Linked, not copied, so they follow this checkout."
      printf '  Install them into %s? [y/N] ' "$SKILLS_DEST"
      read -r reply || reply=""
      case "$reply" in
        [yY]*) install_skills ;;
        *) echo "  skipped — re-run with --skills, or link them yourself" ;;
      esac
    else
      echo "  note    Claude Code skills are available but NOT installed ($MISSING)."
      echo "          Non-interactive run, so nothing was written to $SKILLS_DEST."
      echo "          Add them with:  $SRC/install.sh --skills $TARGET"
    fi ;;
esac

echo
echo "Done. Next:"
echo "  1. \$EDITOR $TARGET/.showrunner/config.json"
echo "       resources  — every single-consumer thing (a device, a deploy target, a bound port)"
echo "       lanes      — which work is headless and which must serialize, and on what"
echo "       inject     — the gitignored files a build actually needs in a fresh worktree"
echo "       checks     — the commands integration re-runs after EACH merge"
echo "  2. ./.showrunner/bin/showrunner doctor"
echo "       It REFUSES a config that would degrade silently — a worktree-relative lock root,"
echo "       or worktrees outside the repo. A mutex that is quietly a no-op is worse than none."
# STEP 3 USED TO PASTE THE HOOK ENTRY, and this script had already written it — every install
# runs `worktree register` above. So a reader who followed the instruction got a SECOND
# PreToolUse entry and the guard ran twice per tool call, which `_guard_registration` does not
# notice because it returns on the first match. The literal JSON also lived in two places with
# different quoting and no timeout; `lease.register_guard` is the only copy now.
echo "  3. git add .showrunner/hooks/ && commit — the guard is registered already, but"
echo "       \`git worktree add\` copies TRACKED files only, so until it is committed the guard"
echo "       is present here and ABSENT in every worktree, which is the one place it runs."
echo "       (Re-register any time with: ./.showrunner/bin/showrunner worktree register —"
echo "        it is idempotent. doctor reports both states as errors until they are fixed.)"
echo "  4. OPTIONAL — arm your harness's idle watchdog against this orchestrator:"
echo "       .showrunner/hooks/waiting-probe.sh answers \"is this session legitimately waiting"
echo "       on work it dispatched\" from the campaign record. An orchestrator that fanned work"
echo "       out is idle BY DESIGN, and a watchdog that rings through that gets switched off."
echo "       Point your harness at it — for game_loop that is watchdog.waiting_probe in"
echo "       .game_loop/config.local.json. NOT done for you: a wait an agent can declare for"
echo "       itself is an off switch for the watchdog watching it, so this is a human's call."
echo "       CHECK IT IS IGNORED FIRST — \`git check-ignore .game_loop/config.local.json\`."
echo "       That file is a MACHINE-LOCAL layer, and an ignore rule only protects the installs"
echo "       that received it: a tree installed before the rule existed carries it tracked, so a"
echo "       local override lands in your history. If it is not ignored, add it to"
echo "       .git/info/exclude, which is local and needs no upgrade from anybody."
echo "  5. ./.showrunner/bin/showrunner baseline    # on a known-good tree"
echo "       The gate is 'no NEW failures', never 'all green' — a repo with pre-existing"
echo "       failures cannot satisfy 'all green', so that version gets switched off on contact."
echo
echo "  showrunner is the project-local binary ./.showrunner/bin/showrunner — not a global command."
