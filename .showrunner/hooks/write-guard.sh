#!/usr/bin/env bash
# PreToolUse (Write|Edit|MultiEdit|NotebookEdit|Bash): refuse a write this session's ROLE may not
# make — through an edit tool or through the shell. `showrunner write-guard` decides; this only
# locates a binary and carries its verdict.
#
# EXIT 3 FROM THE VERB IS A REFUSAL, and only that becomes the 2 Claude Code blocks on. argparse
# exits 2 on an unknown verb, and `.showrunner_self` is a pinned copy that can predate this verb —
# passing 2 straight through would refuse every tool call in the repo. Any status other than 0 or
# 3 means "this binary cannot answer": ask the next one, and if none can, allow LOUDLY.
set -u

_record_fail_open() {
  _r="${CLAUDE_PROJECT_DIR:-}"
  [ -n "$_r" ] || _r="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)" || return 0
  [ -d "$_r/.showrunner" ] || return 0
  printf '{"ts":%s,"notice":"%s guard failed open"}\n' "$(date +%s)" "write" \
    >> "$_r/.showrunner/fail-open.jsonl" 2>/dev/null || true
}

notice() {
  _record_fail_open
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}\n' "$1"
  exit 0
}

anchor="$PWD"
common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=""
if [ -z "$common" ] && [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  common="$(git -C "$CLAUDE_PROJECT_DIR" rev-parse --git-common-dir 2>/dev/null)" || common=""
  [ -n "$common" ] && anchor="$CLAUDE_PROJECT_DIR"
fi
if [ -z "$common" ]; then
  root="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)" || root=""
else
  case "$common" in /*) ;; *) common="$anchor/$common" ;; esac
  root="$(cd "$(dirname "$common")" 2>/dev/null && pwd)" || root=""
fi
[ -n "$root" ] || notice "⚠ THE WRITE GUARD DID NOT RUN — no repository could be located, so this call was ALLOWED WITHOUT BEING CHECKED against the session's role."

# Read once and replay: the first candidate consumes stdin.
payload="$(cat 2>/dev/null)" || payload=""
err_file="$(mktemp 2>/dev/null || echo "/tmp/.sr-write-err.$$")"
last=""
for candidate in "$root/.showrunner_self/bin/showrunner" \
                 "$root/.showrunner/bin/showrunner" \
                 "$root/bin/showrunner"; do
  [ -x "$candidate" ] || continue
  out="$(printf '%s' "$payload" | (cd "$root" && "$candidate" write-guard) 2>"$err_file")"; rc=$?
  err="$(cat "$err_file" 2>/dev/null)"
  if [ "$rc" = 0 ]; then
    rm -f "$err_file"
    [ -n "$out" ] && printf '%s\n' "$out"
    exit 0
  fi
  if [ "$rc" = 3 ]; then
    rm -f "$err_file"
    printf '%s\n' "$err" >&2
    exit 2
  fi
  last="$candidate exited $rc: $(printf '%s' "$err" | head -1)"
done
rm -f "$err_file"
notice "⚠ THE WRITE GUARD DID NOT RUN — no showrunner binary here could answer (${last:-none found}), so this call was ALLOWED WITHOUT BEING CHECKED against the session's role. Check: showrunner doctor"
