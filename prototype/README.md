# prototype

Validated proof-of-concept scripts (12/12 assertions pass — run `bash demo.sh`). These prove the
mechanisms, but are **coupled to the Drops monorepo** (device verbs, `.beads` path) and are **not**
the final form — the real implementation lifts the project-specifics into config.

- `device_lane.sh` — cross-process "one at a time" mutex (atomic mkdir + live-PID holder + stale
  reclaim + `run` acquire/exec/release wrapper) and a `guard` in the shape of a game_loop write-guard.
- `br_gate.sh` — `close-gate` (proof-of-done: `br close` needs a real artifact) and `stop-gate`
  (refuse turn-end with claimed-open leaf work; epics excluded).
- `demo.sh` — the assertion harness.

See `../docs/DESIGN.md`.
