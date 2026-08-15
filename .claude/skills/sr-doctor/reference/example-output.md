# Reference output

The **fidelity target** for `/sr-doctor`. Match the length as strictly as the shape: one line per
finding, consequences not mechanisms, remedies only when asked.

The three readings below are **real** — what `doctor` actually printed in these repos on
2026-08-14 — rendered the way this skill renders them. The `ok` lines are a count, never a list.

---

## The ordinary case — a consumer project

```
revali · healthy, 1 caveat · exit 0 · pinned 94959a067001 (~/.claude/showrunner-central)

warn   the worktree guard is absent in every worktree made from now on — its shim is untracked

9 checks passed
```

Three lines. Not a paragraph about `git worktree add` copying tracked files — that is the mechanism,
and the reader can ask. Not `git add .showrunner/hooks/worktree-guard.sh` either; doctor already
printed it.

## The development checkout

```
showrunner · healthy, 1 caveat · exit 0 · checkout 94959a067001 (dirty)

warn   nothing re-checks a fan-out here — .game_loop's watchdog has no waiting probe, so a healthy
       fanned-out run gets rung and eventually pages you

9 checks passed
```

`dirty` earns its place on the verdict line: uncommitted edits make that sha an overstatement of
what is running. The watchdog remedy is a human's job and is printed **only when asked** — arming it
is never something this skill does.

## Broken

```
landlord · BROKEN · exit 2 · copy (no commit names this code)

ERROR  every check below ran against defaults, not against anything anyone chose — `showrunner init`
ERROR  nothing denies a write into a tree another session holds — the guard is not registered
       (literal JSON, printed by doctor; there is no verb that writes it)
warn   integration cannot tell a new failure from a pre-existing one — no baseline recorded

6 checks passed
```

Errors first. Both carry a command or an explicit "there is no command", because an ERROR whose fix
is non-obvious is the one exception to remedies-on-request.

---

## Reached from `/sr-status`

When `sr-status` hands off — an empty campaign, or a snapshot that could not be taken — the answer
is its one line plus this block, and nothing else:

```
No campaign here — nothing claimed, nothing ready. Running `doctor` instead.

emptyproj · healthy, 2 caveats · exit 0 · checkout 94959a067001

warn   the worktree guard is absent in every worktree made from now on — its shim is untracked
warn   integration cannot tell a new failure from a pre-existing one — no baseline recorded

8 checks passed
```

## Not checked

One line, and only when it is relevant — central mode, or the user asked what doctor covers:

> Not checked: whether `~/.claude/showrunner-central` is still what was pinned (CI-04) — ask and I
> will run `self --dest`. Campaign health is `/sr-status`.

## When doctor never ran

Under a central install with no shared copy, the entrypoint is a dispatcher and every non-hook verb
exits 1. That is the whole diagnosis:

```
gun_fu · BROKEN · exit 1 · no central install at ~/.claude/showrunner-central

ERROR  wired to code that is not there — nothing local is broken and nothing local was checked
warn   hook verbs are meanwhile exiting 0 as ALLOWED WITHOUT BEING CHECKED, so anything still
       running is running unguarded
```
