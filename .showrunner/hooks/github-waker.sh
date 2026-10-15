#!/usr/bin/env bash
# Stop (asyncRewake): wake an idle session when GitHub activity arrives in the repos the `waker`
# section of config names. `showrunner waker` decides everything — what to watch, what is new,
# what is the agent's own writing — so the suite and the mutation sweep can reach it. This only
# finds a binary that KNOWS the verb.
#
# PROBE FIRST. Exit 2 from an asyncRewake hook IS a wake, and argparse exits 2 on an unknown verb:
# a pinned copy older than `waker` would wake the session with its usage text on every turn-end.
# So each candidate is asked `waker --probe`, and only one that answers 0 is handed the job.
# Unconfigured, in a linked worktree, or with no binary that answers, this exits 0, silently.
set -u
anchor="${CLAUDE_PROJECT_DIR:-$PWD}"
common="$(git -C "$anchor" rev-parse --git-common-dir 2>/dev/null)" || exit 0
case "$common" in /*) ;; *) common="$anchor/$common" ;; esac
root="$(cd "$(dirname "$common")" 2>/dev/null && pwd)" || exit 0
for candidate in "$root/.showrunner_self/bin/showrunner" \
                 "$root/.showrunner/bin/showrunner" \
                 "$root/bin/showrunner"; do
  [ -x "$candidate" ] || continue
  "$candidate" waker --probe >/dev/null 2>&1 || continue
  cd "$anchor" 2>/dev/null || exit 0
  exec "$candidate" waker
done
exit 0
