#!/usr/bin/env bash
# PreToolUse (Bash): refuse a raw `claude -p` dispatch that skips every showrunner guarantee.
#
# A GUARD VERB NOBODY REGISTERS HAS NEVER ONCE RUN. That was true of `lock guard` for this repo's
# entire life, and it was true of THIS verb for the hours between building it and noticing —
# which is the failure #37 reports, arriving in the fix for #37. The verb existing is not the
# feature; the registration is.
#
# ON BASH, which is the mechanism actually used. A consumer's prototype matched `Agent` and
# guarded the in-process subagent tool while every real dispatch went out through Bash, 42
# consecutive times, reporting nothing.
#
# Same shape and same fail-open posture as worktree-guard.sh: a found-but-BROKEN binary is a loud
# allow, not a silent one, because this repo edits the tool its own guards run.
set -u

notice() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}\n' "$1"
  exit 0
}

common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=""
if [ -z "$common" ]; then
  notice "⚠ THE DISPATCH GUARD DID NOT RUN — git could not resolve a repository here, so this call was ALLOWED WITHOUT BEING CHECKED. A raw \`claude -p\` would skip the worktree, the lease, the claim and the room. Check: showrunner doctor"
fi
case "$common" in /*) ;; *) common="$PWD/$common" ;; esac
root="$(cd "$(dirname "$common")" 2>/dev/null && pwd)" || root=""

for candidate in "$root/.showrunner_self/bin/showrunner" \
                 "$root/.showrunner/bin/showrunner" \
                 "$root/bin/showrunner"; do
  if [ -x "$candidate" ]; then
    out="$("$candidate" dispatch guard 2>/tmp/.sr-dispatch-err.$$)"; rc=$?
    err="$(cat /tmp/.sr-dispatch-err.$$ 2>/dev/null)"; rm -f /tmp/.sr-dispatch-err.$$
    if [ "$rc" = 0 ]; then
      printf '%s\n' "$out"
      exit 0
    fi
    if [ "$rc" = 2 ]; then
      printf '%s\n' "$err" >&2
      exit 2
    fi
    notice "⚠ THE DISPATCH GUARD DID NOT RUN — $candidate exited $rc instead of answering, so this call was ALLOWED WITHOUT BEING CHECKED. If that names an unknown verb, the binary is a PINNED copy older than it. First line: $(printf '%s' "$err" | head -1)"
  fi
done

notice "⚠ THE DISPATCH GUARD DID NOT RUN — no showrunner binary was found under the main checkout, so this call was ALLOWED WITHOUT BEING CHECKED. Check: showrunner doctor"
