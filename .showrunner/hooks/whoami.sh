#!/usr/bin/env bash
# SessionStart / PostCompact: announce what this session IS (#36).
#
# THE SEAM SHOWRUNNER DID NOT OWN. A consumer's 16-hour run dispatched 42 sessions in a repo with
# showrunner installed, wired, and carrying a campaign with 38 leaves done — and not one went
# through it. game_loop owns SessionStart and PostCompact, so every session it guards is GREETED
# by it without choosing it, and re-greeted after every compaction. showrunner owned neither, so
# its adoption was instruction-only and each compaction eroded it further.
#
# BOTH SEAMS, and the second is the one that matters: a rule that survives only until the next
# compaction is a rule for the first hour.
#
# THE OUTPUT IS THE WHOLE MECHANISM, because a SessionStart hook cannot block. So this must never
# be silent: additionalContext is what actually reaches the agent, and every failure path below
# still says something.
set -u

common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=""
if [ -n "$common" ]; then
  case "$common" in /*) ;; *) common="$PWD/$common" ;; esac
  root="$(cd "$(dirname "$common")" 2>/dev/null && pwd)" || root=""
fi

SR=""
for c in "${root:-.}/.showrunner_self/bin/showrunner" \
         "${root:-.}/.showrunner/bin/showrunner" "${root:-.}/bin/showrunner"; do
  [ -x "$c" ] && { SR="$c"; break; }
done

if [ -z "$SR" ]; then
  # NOT SILENCE. A session that never hears from showrunner is the reported failure exactly.
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' \
    "showrunner: NO BINARY FOUND from $PWD, so this session was told nothing about what it is or what it may not do. If this repo runs a campaign, that is the state in which 42 raw dispatches went unnoticed. Check: showrunner doctor"
  exit 0
fi

# THE REASON, NOT A GUESS AT IT. The first version reported "whoami produced nothing" whenever
# the call failed for ANY reason — and the first real failure was a self-vendored pin that
# predated the verb, which that message would have sent somebody looking at whoami instead of at
# the pin. A wrong subject in an error costs more than a vague one.
# THE SESSION ID, WITHOUT WHICH THE RE-SEAT CANNOT FIRE. `whoami` rebinds a seat whose session
# matches and whose pid is gone — a window reload — and its own rule is that an EMPTY session id
# matches nothing, because treating absent-as-equal would hand out unidentified seats. This hook
# called `whoami` with no session at all, so that rule made the feature unreachable from the one
# channel a reloaded session is guaranteed to read. Built and unfired is the shape this repo keeps
# catching; it deserved catching here before an operator tested it and saw nothing.
#
# Read the same way every other hook here reads its payload. Empty or unparseable yields no flag
# and the announcement proceeds exactly as before — this must never be the thing that breaks.
payload="$(cat 2>/dev/null || true)"
sess=""
if [ -n "$payload" ]; then
  sess="$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    print((json.load(sys.stdin).get("session_id") or "").strip())
except Exception:
    pass
' 2>/dev/null)" || sess=""
fi
[ -z "$sess" ] && sess="${CLAUDE_SESSION_ID:-}"
[ -z "$sess" ] && sess="${SHOWRUNNER_SESSION:-}"

set -- whoami
[ -n "$sess" ] && set -- whoami --session "$sess"

if ! out="$("$SR" "$@" 2>&1)"; then
  out="showrunner: COULD NOT SAY WHAT THIS SESSION IS — \`$SR whoami\` exited non-zero. This
session was told nothing about what it is or what it may not do, which is the state in which 42
raw dispatches went unnoticed in one real run. First line of what it said:
  ${out%%$'\n'*}
If that names an unknown verb, the binary above is a PINNED copy older than the verb — refresh it
with \`bin/showrunner self --pin HEAD --dest .showrunner_self\`, and \`showrunner doctor\` says
how far behind it is."
elif [ -z "$out" ]; then
  out="showrunner: whoami exited 0 and printed NOTHING, which is the one outcome it is not allowed to have. Check: showrunner doctor"
elif ! "$SR" guide --index --role worker >/dev/null 2>&1; then
  # THE TOOLS INDEX IS PRINTED BY whoami ITSELF, so a hook older than the index still gets it
  # from a new binary. The other way round is this branch: a binary that predates `guide --index`
  # printed its announcement without one, and a missing index reads the same as having nothing
  # to use. The probe asks the flag, not the text.
  out="$out

showrunner: NO TOOLS INDEX — $SR predates \`guide --index\`, so this session was not handed the one-line-per-verb list it reads after every compaction. Re-pin it: \`bin/showrunner self --pin HEAD --dest .showrunner_self\`."
fi

# A WAKE THAT NEVER BECAME A TURN. Both wakers exit after ringing, and only a turn end starts the
# next, so a ring the host dropped left the repo unwatched until a human noticed. This reports
# such a ring once and looks at GitHub once (a short timeout of its own), after the announcement
# so a slow look cannot cost it. Silent when there is nothing to say; a binary too old to know
# the flag is named, not hidden.
# The transcript path rides along in the environment: from it `waker --session-start` tells a
# ring rung under a host process that had already been replaced. A binary too old to read it
# ignores it.
tx=""
if [ -n "$payload" ]; then
  tx="$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    print((json.load(sys.stdin).get("transcript_path") or "").strip())
except Exception:
    pass
' 2>/dev/null)" || tx=""
fi
if lost="$(SHOWRUNNER_TRANSCRIPT="$tx" "$SR" waker --session-start 2>&1)"; then
  [ -n "$lost" ] && out="$out

$lost"
else
  out="$out

showrunner: the lost-wake check did not run (\`waker --session-start\` exited non-zero: ${lost%%$'\n'*}), so a wake that rang while no turn followed is not reported here."
fi

python3 - "$out" <<'PY'
import json, sys
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart", "additionalContext": sys.argv[1]}}))
PY
