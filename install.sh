#!/usr/bin/env bash
# Install showrunner into a target repo.
#
# Deliberately the same shape as game_loop's installer: copy a payload, touch nothing
# else, and be idempotent. showrunner is Python 3 standard library only — there is nothing
# to compile, nothing to fetch, and no package manager involved (issue #2).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-.}"

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
mkdir -p "$TARGET/.showrunner/bin" "$TARGET/.showrunner/lib"
cp -R "$SRC/lib/showrunner" "$TARGET/.showrunner/lib/"
cp "$SRC/bin/showrunner" "$TARGET/.showrunner/bin/showrunner"
chmod +x "$TARGET/.showrunner/bin/showrunner"
echo "  copied  .showrunner/bin/showrunner + .showrunner/lib/showrunner/"

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
  (cd "$TARGET" && "$TARGET/.showrunner/bin/showrunner" init >/dev/null)
  echo "  seeded  .showrunner/config.json"
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
echo "  3. ./.showrunner/bin/showrunner baseline    # on a known-good tree"
echo "       The gate is 'no NEW failures', never 'all green' — a repo with pre-existing"
echo "       failures cannot satisfy 'all green', so that version gets switched off on contact."
echo
echo "  showrunner is the project-local binary ./.showrunner/bin/showrunner — not a global command."
