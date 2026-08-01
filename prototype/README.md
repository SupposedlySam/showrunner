# prototype/ — the original proof-of-concept

These are the shell scripts that proved showrunner's three load-bearing primitives before
any of it existed as a product. They are kept for the record; **they are not the test
suite and not the implementation.**

- The primitives now live in [`lib/showrunner/`](../lib/showrunner/) — `locks.py` (the
  cross-process single-consumer mutex) and `gates.py` (the proof-of-done and stop gates).
- The runnable proof is [`test/run.py`](../test/run.py), which needs nothing but Python 3
  and `git`.

## Why these scripts moved out of the way

They were written against one machine and one repo (issue #1): a hardcoded
`$HOME/.cargo/bin` on `PATH`, a `BR_DB` pointing at a beads database inside a specific
monorepo, and a lock directory resolved **relative to the script's own location** — which
across N worktrees means N sibling lock directories and a mutex that silently does
nothing. That last one is the worst available failure mode, because it looks like it is
working.

The shape of all three primitives survived the lift unchanged. What changed is that the
project's specifics became config rather than code.

## Running them anyway

`demo.sh` now checks its dependencies up front and **skips loudly**, naming what is
missing, rather than failing obscurely several blocks in:

```bash
bash prototype/demo.sh
```

| Half | Needs | On a clean clone |
|------|-------|------------------|
| device lane (8 assertions) | `bash` + coreutils | runs |
| br gates (5 assertions) | `br` on `PATH` and a beads DB via `$BR_DB` | skips, loudly |

See [`../docs/DESIGN.md`](../docs/DESIGN.md).
