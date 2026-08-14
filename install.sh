#!/usr/bin/env bash
# Install showrunner into a target repo.
#
# Deliberately the same shape as game_loop's installer: copy a payload, touch nothing
# else, and be idempotent. showrunner is Python 3 standard library only — there is nothing
# to compile, nothing to fetch, and no package manager involved (issue #2).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
CENTRAL=0
TARGET="."
for arg in "$@"; do
  case "$arg" in
    --central) CENTRAL=1 ;;
    -h|--help)
      echo "usage: ./install.sh [--central] /path/to/your/project"
      echo
      echo "  --central   do not copy the tool's code into the project at all. Write one tiny"
      echo "              dispatcher shim that execs a shared, machine-wide install instead."
      echo "              Populate that with \`showrunner self --pin <ref> --dest <path>\`."
      echo "              Opt-in and REVERSIBLE: re-run without --central to restore local copies."
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
cp "$SRC/.showrunner/hooks/worktree-guard.sh" "$TARGET/.showrunner/hooks/worktree-guard.sh"
chmod +x "$TARGET/.showrunner/hooks/worktree-guard.sh"
echo "  copied  .showrunner/hooks/worktree-guard.sh (COMMIT THIS — it must cross into worktrees)"

if [ ! -f "$TARGET/.showrunner/.gitignore" ]; then
  cat >"$TARGET/.showrunner/.gitignore" <<'EOF'
# showrunner RUNTIME state — not source. config.json IS source; commit it.
graph.db
graph.db-*
locks/
scratch/
campaign.json
routing.jsonl
waiting.jsonl
events.jsonl
*.lock
baseline.json
integration-commit.json
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
for entry in "bin/" "lib/"; do
  if ! grep -qxF "$entry" "$sr_ignore" 2>/dev/null; then
    if [ "$sr_added" = 0 ]; then
      printf '\n# The TOOL, not this project. Installed by install.sh and replaced wholesale on\n# upgrade — committing it vendors a copy that drifts from the one you installed.\n# `--central` (when it lands) makes this the only thing here.\n' >>"$sr_ignore"
    fi
    printf '%s\n' "$entry" >>"$sr_ignore"
    sr_added=$((sr_added + 1))
  fi
done
if [ "$sr_added" -gt 0 ]; then
  echo "  ignored .showrunner/bin/ and lib/ — the tool is not your project's source"
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
  (cd "$TARGET" && "$SRC/bin/showrunner" init >/dev/null)
  echo "  seeded  .showrunner/config.json"
fi

# EVERY RUN, not only a fresh one. `init` registers the guard and `init` only runs when there is
# no config — so every ALREADY-INSTALLED consumer took the upgrade path, received the shim file,
# and got no registration: a hook that exists and can never fire, plus a `doctor` error the
# installer itself created. Found by upgrading a real consumer. It is the same shape as the
# ignore rules above, and it is idempotent for the same reason: a second run appends nothing.
if (cd "$TARGET" && "$SRC/bin/showrunner" worktree register 2>/dev/null | grep -q registered); then
  echo "  hooked  the worktree guard is registered in .claude/settings.json"
fi

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
echo "  3. Register the worktree guard in .claude/settings.json, or it never runs:"
echo "       PreToolUse → {\"matcher\": \"Write|Edit|NotebookEdit|Bash\","
echo "                     \"hooks\": [{\"type\": \"command\","
echo "                       \"command\": \"\\\$CLAUDE_PROJECT_DIR/.showrunner/hooks/worktree-guard.sh\"}]}"
echo "       Then commit .showrunner/hooks/. doctor reports both as errors until you do —"
echo "       an unregistered guard is indistinguishable from one that ran and was content."
echo "  4. ./.showrunner/bin/showrunner baseline    # on a known-good tree"
echo "       The gate is 'no NEW failures', never 'all green' — a repo with pre-existing"
echo "       failures cannot satisfy 'all green', so that version gets switched off on contact."
echo
echo "  showrunner is the project-local binary ./.showrunner/bin/showrunner — not a global command."
