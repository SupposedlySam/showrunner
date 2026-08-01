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
baseline.json
integration-commit.json
EOF
  echo "  wrote   .showrunner/.gitignore (runtime state ignored; config.json is source)"
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
