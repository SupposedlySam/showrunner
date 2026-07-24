#!/usr/bin/env bash
# Proof harness for the game_loop<->br integration (personal prototype).
export PATH="$HOME/.cargo/bin:$PATH"; export RUST_LOG=error
cd "$(dirname "$0")"
DL=./device_lane.sh; GATE=./br_gate.sh
BR_DB="$HOME/development/drops/.beads/beads.db"
br() { command br --db "$BR_DB" "$@"; }
pass=0; fail=0
chk() { local want="$1" label="$2"; shift 2; set +e; "$@" >/tmp/gl_out 2>/tmp/gl_err; local got=$?; set -e
  if [ "$got" = "$want" ]; then echo "  PASS [exit $got] $label"; pass=$((pass+1)); else echo "  FAIL want=$want got=$got — $label"; sed 's/^/      /' /tmp/gl_err; fail=$((fail+1)); fi; }
lane_free() { ! [ -d ./device.lock ]; }
seed_lock() { mkdir -p device.lock; printf '%s\n' "$1" >device.lock/pid; printf '%s\n' "$2" >device.lock/holder; date +%s >device.lock/ts; }
rm -rf ./device.lock

echo "== DEVICE LANE (cross-process 'one deploy at a time') =="
chk 0 "status: free"                                   $DL status
sleep 60 & LIVE=$!
seed_lock "$LIVE" "agent-A frontend_deploy"
chk 2 "guard BLOCKS 2nd frontend_deploy while holder alive" $DL guard "frontend_deploy --platform ios --target device"
chk 0 "guard ALLOWS non-device cmd (flutter test)"     $DL guard "fvm flutter test test/foo_test.dart"
kill $LIVE 2>/dev/null; wait $LIVE 2>/dev/null || true
chk 0 "guard ALLOWS once holder is dead"               $DL guard "frontend_deploy --platform ios"
rm -rf device.lock
seed_lock 999999 "dead-agent"
chk 0 "acquire RECLAIMS a stale (dead-pid) lock"       $DL acquire "agent-C"
$DL release --force >/dev/null 2>&1 || true
chk 0 "run wrapper: acquire->exec->release"            $DL run "solo-deploy" -- echo "deploy body ran"
chk 0 "lane FREE after run releases"                   lane_free

echo "== BR GATES (game_loop enforcement on the real graph) =="
ID=$(br create "PROTO gate throwaway" -t task --json 2>/dev/null | sed -E 's/.*"id":"([^"]+)".*/\1/')
echo "  throwaway issue: $ID"
br update "$ID" --claim --actor proto >/dev/null 2>&1
br show "$ID" 2>/dev/null | grep -qi in_progress && echo "  (claimed: in_progress)" || echo "  WARN: claim not visible"
chk 2 "stop-gate REFUSES turn-end with claimed-open work"  $GATE stop-gate
chk 2 "close-gate REFUSES with no proof"                   $GATE close-gate "$ID" --reason "done"
chk 2 "close-gate REFUSES nonexistent proof file"          $GATE close-gate "$ID" --proof /no/such/file --reason "done"
chk 0 "close-gate ALLOWS with a real artifact"             $GATE close-gate "$ID" --proof ./device_lane.sh --reason "proto verified"
chk 0 "stop-gate OK after the claimed work is closed"      $GATE stop-gate
br delete "$ID" >/dev/null 2>&1 || true
rm -rf device.lock
echo; echo "RESULT: $pass passed, $fail failed"; [ "$fail" = 0 ]
