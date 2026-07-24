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
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"; export RUST_LOG=error
BR_DB="${BR_DB:-$HOME/development/drops/.beads/beads.db}"
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
