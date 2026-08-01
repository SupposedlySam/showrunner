#!/usr/bin/env bash
# game_loop enforcement philosophy applied to the br work-graph.
#
#   close-gate : `br close` is refused unless you name a REAL, non-empty artifact
#                that proves the work is done (test file / golden png / commit).
#                This is game_loop's claim-gate ("cite the file you read") applied
#                to "done" — it would have caught this session's retracted
#                "device-verified" claim, because no screenshot could be named.
#   stop-gate  : refuse a turn-end while any br issue is claimed (in_progress) —
#                the game_loop Stop gate, but reading the br graph instead of a
#                single mandate string.
#
# KEPT FOR THE RECORD. The maintained implementation is ../lib/showrunner/gates.py, which
# parses the graph as JSON rather than with sed and grep — the version below splits records
# on a literal `},{`, so any change to field order or whitespace turns the stop gate into a
# no-op that reports success (issue #6).
set -euo pipefail
export RUST_LOG=error
[ -d "$HOME/.cargo/bin" ] && export PATH="$HOME/.cargo/bin:$PATH"
BR_DB="${BR_DB:-}"
if ! command -v br >/dev/null 2>&1; then
  echo "br_gate.sh: \`br\` is not on PATH. This script needs it; ../lib/showrunner/gates.py does not." >&2
  exit 3
fi
if [ -z "$BR_DB" ]; then
  echo "br_gate.sh: set \$BR_DB to a beads database path. There is no sensible default — the "\
       "previous one pointed into one specific developer's monorepo (issue #1)." >&2
  exit 3
fi
if [ ! -f "$BR_DB" ]; then
  echo "br_gate.sh: \$BR_DB=$BR_DB does not exist." >&2
  exit 3
fi
br() { command br --db "$BR_DB" "$@"; }

case "${1:-}" in
  close-gate) shift
    id="${1:-}"; shift || true
    proof=""; reason=""
    while [ $# -gt 0 ]; do case "$1" in
      --proof) proof="$2"; shift 2;; --reason) reason="$2"; shift 2;; *) shift;; esac; done
    [ -n "$id" ] || { echo "usage: close-gate <id> --proof <path> --reason <text>" >&2; exit 64; }
    if [ -z "$proof" ] || [ ! -s "$proof" ]; then
      echo "REFUSED to close $id: --proof must name a real, non-empty artifact (got: '${proof:-<none>}')." >&2
      echo "  Name the test/golden/commit that proves it — 'done' in prose is a wish." >&2
      exit 2
    fi
    br close "$id" --reason "${reason:-done} [proof: $proof]"
    echo "closed $id (proof: $proof)" ;;
  stop-gate) shift
    # Only LEAF work counts as "claimed-open". Epics are containers — expected to
    # sit in_progress while their children are worked — so exclude issue_type=epic.
    open="$(br list --status in_progress --json 2>/dev/null || echo '[]')"
    set +e +o pipefail  # grep may legitimately match nothing (no leaf work open)
    ids="$(printf '%s' "$open" | sed -E 's/\},\{/\}\n\{/g' | grep -v '"issue_type":"epic"' | grep -oE '"id":"[^"]+"' | sed -E 's/"id":"([^"]+)"/\1/' | tr '\n' ' ')"
    set -e -o pipefail
    if [ -n "${ids// /}" ]; then
      echo "STOP REFUSED: claimed-but-open work still in progress: $ids" >&2
      echo "  Either finish + close-gate it, or explicitly checkpoint/hand back." >&2
      exit 2
    fi
    echo "stop OK: no claimed-open work" ;;
  *) echo "usage: br_gate.sh {close-gate <id> --proof <path> --reason <text>|stop-gate}" >&2; exit 64 ;;
esac
