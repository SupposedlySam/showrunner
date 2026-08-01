#!/usr/bin/env bash
# Cross-process device-lane mutex — the "one deploy at a time" constraint,
# enforced (rung 1/2) instead of wished (rung 6). A game_loop-style write-guard
# would call `guard`; a deploy wrapper (or frontend_deploy itself) calls `run`.
#
# The authoritative primitive is an ATOMIC mkdir lock whose holder is a LIVE pid
# (the deploy process itself). A dead holder = stale = reclaimable. This is what
# makes it work across separate processes / worktrees, not just across turns in
# one session.
#
# KEPT FOR THE RECORD. The maintained implementation is ../lib/showrunner/locks.py, which
# adds named resources (so unrelated work does not queue behind unrelated work), one
# absolute lock root validated at config load, and a boot token beside the PID.
#
# The default LOCKDIR below is the known hazard (issue #3): it resolves relative to THIS
# SCRIPT, so N worktrees get N sibling lock directories and the mutex silently does
# nothing. Set DEVICE_LANE_LOCK to one absolute shared path if you run this directly.
set -euo pipefail
LOCKDIR="${DEVICE_LANE_LOCK:-$(cd "$(dirname "$0")" && pwd)/device.lock}"
case "$LOCKDIR" in
  /*) ;;
  *) echo "device_lane.sh: LOCKDIR must be absolute (got '$LOCKDIR') — a relative lock root is a mutex that quietly is not one." >&2; exit 64 ;;
esac
DEVICE_VERB_RE='frontend_(deploy|build)|frontend[ _]deploy|drops[ _]tizen[ _]deploy|flutter(-tizen)?[ _]run'

_holder_pid() { cat "$LOCKDIR/pid" 2>/dev/null || echo ""; }
_holder_who() { cat "$LOCKDIR/holder" 2>/dev/null || echo "?"; }
_live() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

_write_owner() { echo "$1" >"$LOCKDIR/pid"; echo "$2" >"$LOCKDIR/holder"; date +%s >"$LOCKDIR/ts"; }

acquire() { # <owner-pid> <holder-desc>
  local opid="$1" who="$2"
  if mkdir "$LOCKDIR" 2>/dev/null; then _write_owner "$opid" "$who"; echo "ACQUIRED device lane (pid $opid — $who)"; return 0; fi
  local hpid; hpid="$(_holder_pid)"
  if _live "$hpid"; then
    echo "BLOCKED: device lane held by pid $hpid ($(_holder_who)). One deploy/build/Marionette at a time." >&2
    return 2
  fi
  echo "note: reclaiming stale device lane (holder pid ${hpid:-none} not alive)" >&2
  rm -rf "$LOCKDIR"
  if mkdir "$LOCKDIR" 2>/dev/null; then _write_owner "$opid" "$who"; echo "ACQUIRED device lane after reclaim (pid $opid — $who)"; return 0; fi
  echo "BLOCKED: lost reclaim race" >&2; return 2
}

case "${1:-status}" in
  status)
    if [ -d "$LOCKDIR" ]; then
      hpid="$(_holder_pid)"; _live "$hpid" && s=LIVE || s=STALE
      echo "HELD ($s) by pid $hpid ($(_holder_who))"
    else echo "FREE"; fi ;;
  acquire) shift; acquire "$$" "${1:-manual}" ;;   # NOTE: caller pid is this short-lived shell
  release) shift
    hpid="$(_holder_pid)"
    if [ "$hpid" = "${2:-$$}" ] || [ "${1:-}" = "--force" ]; then rm -rf "$LOCKDIR"; echo "released"; else echo "not owner (holder $hpid)"; exit 1; fi ;;
  guard) shift # <proposed command...>  -> exit 2 blocks the tool call (game_loop write-guard shape)
    line="$*"
    if echo "$line" | grep -qiE "$DEVICE_VERB_RE"; then
      hpid="$(_holder_pid)"
      if _live "$hpid"; then echo "BLOCKED device verb: lane held by pid $hpid ($(_holder_who)). Command: $line" >&2; exit 2; fi
      echo "allow (device verb, lane free): $line"; exit 0
    fi
    echo "allow (not a device verb): $line"; exit 0 ;;
  run) shift # <holder-desc> -- <cmd...>   authoritative: acquire, run (this proc holds the lock, alive), release
    who="$1"; shift; [ "${1:-}" = "--" ] && shift
    acquire "$$" "$who" || exit $?
    trap 'rm -rf "$LOCKDIR"' EXIT
    "$@" ;;
  *) echo "usage: device_lane.sh {status|acquire <who>|release|guard <cmd...>|run <who> -- <cmd...>}" >&2; exit 64 ;;
esac
