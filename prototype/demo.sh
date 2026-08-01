#!/usr/bin/env bash
# Proof harness for the ORIGINAL shell prototype.
#
# This is kept for the record. The real suite is ../test/run.py (Python 3 + git, no other
# setup). See ./README.md for why these scripts moved out of the way.
#
# Split in two (issue #1):
#   * the device-lane half needs nothing but bash and RUNS on a clean clone;
#   * the br-gate half needs `br` and a beads DB, and SKIPS LOUDLY naming what is missing
#     rather than failing obscurely several blocks in.
cd "$(dirname "$0")"
DL=./device_lane.sh; GATE=./br_gate.sh
export DEVICE_LANE_LOCK="${DEVICE_LANE_LOCK:-$PWD/device.lock}"
pass=0; fail=0; skipped=0
chk() { local want="$1" label="$2"; shift 2; set +e; "$@" >/tmp/sr_out 2>/tmp/sr_err; local got=$?; set -e
  if [ "$got" = "$want" ]; then echo "  PASS [exit $got] $label"; pass=$((pass+1));
  else echo "  FAIL want=$want got=$got — $label"; sed 's/^/      /' /tmp/sr_err; fail=$((fail+1)); fi; }
skip() { echo "  SKIP $1 — $2"; skipped=$((skipped+1)); }
lane_free() { ! [ -d "$DEVICE_LANE_LOCK" ]; }
seed_lock() { mkdir -p "$DEVICE_LANE_LOCK"; printf '%s\n' "$1" >"$DEVICE_LANE_LOCK/pid"; \
              printf '%s\n' "$2" >"$DEVICE_LANE_LOCK/holder"; date +%s >"$DEVICE_LANE_LOCK/ts"; }
rm -rf "$DEVICE_LANE_LOCK"

echo "== DEVICE LANE (needs only bash — runs on a clean clone) =="
chk 0 "status: free"                                        $DL status
sleep 60 & LIVE=$!
seed_lock "$LIVE" "agent-A frontend_deploy"
chk 2 "guard BLOCKS a 2nd frontend_deploy while the holder is alive" \
      $DL guard "frontend_deploy --platform ios --target device"
chk 0 "guard ALLOWS a non-device command"                   $DL guard "fvm flutter test test/foo_test.dart"
kill $LIVE 2>/dev/null; wait $LIVE 2>/dev/null || true
chk 0 "guard ALLOWS once the holder is dead"                $DL guard "frontend_deploy --platform ios"
rm -rf "$DEVICE_LANE_LOCK"
seed_lock 999999 "dead-agent"
chk 0 "acquire RECLAIMS a stale (dead-pid) lock"            $DL acquire "agent-C"
$DL release --force >/dev/null 2>&1 || true
chk 0 "run wrapper: acquire -> exec -> release"             $DL run "solo-deploy" -- echo "deploy body ran"
chk 0 "the lane is FREE after run releases"                 lane_free
rm -rf "$DEVICE_LANE_LOCK"

echo
echo "== BR GATES (needs external tooling) =="
missing=""
command -v br >/dev/null 2>&1 || missing="the \`br\` binary is not on PATH"
BR_DB="${BR_DB:-}"
if [ -z "$missing" ] && [ -n "$BR_DB" ] && [ ! -f "$BR_DB" ]; then
  missing="\$BR_DB points at $BR_DB, which does not exist"
elif [ -z "$missing" ] && [ -z "$BR_DB" ]; then
  missing="\$BR_DB is not set (point it at a beads database)"
fi

if [ -n "$missing" ]; then
  for label in "stop-gate REFUSES a turn-end with claimed-open work" \
               "close-gate REFUSES with no proof" \
               "close-gate REFUSES a nonexistent proof file" \
               "close-gate ALLOWS a real artifact" \
               "stop-gate OK after the claimed work is closed"; do
    skip "$label" "$missing"
  done
  echo "  → these five assertions are the ONLY ones here that need anything installed."
  echo "    The same guarantees run with no setup at all in ../test/run.py."
else
  export BR_DB
  br() { command br --db "$BR_DB" "$@"; }
  ID=$(br create "PROTO gate throwaway" -t task --json 2>/dev/null | sed -E 's/.*"id":"([^"]+)".*/\1/')
  echo "  throwaway issue: $ID"
  br update "$ID" --claim --actor proto >/dev/null 2>&1
  chk 2 "stop-gate REFUSES a turn-end with claimed-open work" $GATE stop-gate
  chk 2 "close-gate REFUSES with no proof"                    $GATE close-gate "$ID" --reason "done"
  chk 2 "close-gate REFUSES a nonexistent proof file"         $GATE close-gate "$ID" --proof /no/such/file --reason "done"
  chk 0 "close-gate ALLOWS a real artifact"                   $GATE close-gate "$ID" --proof ./device_lane.sh --reason "proto verified"
  chk 0 "stop-gate OK after the claimed work is closed"       $GATE stop-gate
  br delete "$ID" >/dev/null 2>&1 || true
fi

echo
echo "RESULT: $pass passed, $fail failed, $skipped skipped"
[ "$fail" = 0 ]
