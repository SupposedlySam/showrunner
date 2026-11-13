#!/usr/bin/env bash
# Stop (asyncRewake): wake the idle orchestrator when one of its Crawlers needs it — blocked past
# the quiet window, stood down with its leaf open, or stalled. `showrunner crawler-waker` decides
# everything, from `waiting`'s own classification, so the suite and the mutation sweep reach it.
#
# PROBE FIRST, as github-waker.sh does. Exit 2 from an asyncRewake hook IS a wake, and argparse
# exits 2 on an unknown verb: a pinned copy older than `crawler-waker` would wake the session with
# its usage text on every turn-end. So each candidate is asked `crawler-waker --probe`, and only
# one that answers 0 is handed the job. In a linked worktree (a Crawler), or with no binary that
# answers, this exits 0, silently.
set -u
# HEARTBEAT, as the other Stop hooks stamp, so `doctor` can tell a hook reached from one not.
_hb="${SHOWRUNNER_HEARTBEAT:-}"
if [ -z "$_hb" ]; then
  _hb_root="${CLAUDE_PROJECT_DIR:-}"
  [ -n "$_hb_root" ] && [ -d "$_hb_root/.showrunner" ] && _hb="$_hb_root/.showrunner/hook-heartbeat.jsonl"
fi
[ -n "$_hb" ] && { printf '{"hook":"crawler-waker","ts":%s}\n' "$(date +%s)" >> "$_hb" 2>/dev/null || true; }
anchor="${CLAUDE_PROJECT_DIR:-$PWD}"
common="$(git -C "$anchor" rev-parse --git-common-dir 2>/dev/null)" || exit 0
case "$common" in /*) ;; *) common="$anchor/$common" ;; esac
root="$(cd "$(dirname "$common")" 2>/dev/null && pwd)" || exit 0
top="$(git -C "$anchor" rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ "$(cd "$top" 2>/dev/null && pwd -P)" = "$(cd "$root" 2>/dev/null && pwd -P)" ] || exit 0
for candidate in "$root/.showrunner_self/bin/showrunner" \
                 "$root/.showrunner/bin/showrunner" \
                 "$root/bin/showrunner"; do
  [ -x "$candidate" ] || continue
  "$candidate" crawler-waker --probe >/dev/null 2>&1 || continue
  cd "$anchor" 2>/dev/null || exit 0
  exec "$candidate" crawler-waker
done
exit 0
