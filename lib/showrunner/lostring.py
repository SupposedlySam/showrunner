"""At SessionStart: report a waker ring that never became a turn, and catch up on what it missed.

Both wakers exit after ringing (exit 2 from an asyncRewake Stop hook IS the wake), and the next
one starts only when a turn ends. So a ring the host never turns into a turn — the hosting
process gone or restarted — leaves the repo unwatched until somebody notices. Measured on a
consumer: one ring at 23:32:50Z reached no turn, and three PRs went unjudged for seventeen hours.

THE RULE: a ring is LOST when no turn end was stamped after it. Every Stop hook stamps
`hook-heartbeat.jsonl` as its first act, so a ring that became a turn is followed, when that turn
ends, by its own waker's stamp. A stamp in the SAME second as the ring does not count as after
it: stamps are whole seconds, and a waker that looks at once rings within the second of the turn
end that started it. A turn that ends within a second of its wake is the rarer case, and the
error it costs is a ring reported that did land, which is the cheaper way to be wrong. A ring is reported once: it is marked `surfaced` when printed.
A ring followed by a turn end is marked `delivered`, so the set left unmarked stays the
ambiguous one.

THE CATCH-UP is one GitHub look through the waker's locked compare-and-advance (`--once`
semantics), with a short timeout, so what arrived after a lost ring is reported here and is
recorded as seen — the first waker after this turn does not ring it again. A look that failed
says so; it is never "nothing new".

WHAT THIS CANNOT DO: start a turn. SessionStart context reaches the agent with its first turn,
and the waker starts again only when that turn ends. Nor can it see why a ring was lost: the
host's side is not showrunner's to read. In a linked worktree it says nothing, as the wakers do.
"""
import json
import os
import time

LOOK_TIMEOUT_SEC = 5


def heartbeat_path(cfg):
    return os.environ.get("SHOWRUNNER_HEARTBEAT") or \
        os.path.join(cfg.root, ".showrunner", "hook-heartbeat.jsonl")


def last_turn_end(cfg, hook):
    """The newest stamp `hook` left in the heartbeat file, 0 when it left none, or None when the
    file could not be read — which is not the same as no turn having ended."""
    newest = 0
    try:
        with open(heartbeat_path(cfg)) as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if row.get("hook") == hook:
                        newest = max(newest, int(row.get("ts") or 0))
                except (ValueError, TypeError, AttributeError):
                    continue
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    return newest


def lost(rings, turn_end):
    """The rings no turn end followed, not yet reported. `turn_end` None (unreadable) counts every
    unmarked ring as lost: the caller says it could not tell."""
    return [r for r in rings or []
            if not r.get("surfaced") and not r.get("delivered")
            and (turn_end is None or (r.get("at") or 0) >= turn_end)]


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ts))


def surface(cfg, state_file, hook, label, now_ts):
    """Report the lost rings in one waker's state, marking them so each is reported once."""
    from .util import atomic_write_json, file_lock
    path = os.path.join(cfg.state_dir, state_file)
    if not os.path.exists(path):
        return []
    turn_end = last_turn_end(cfg, hook)
    with file_lock(path):
        try:
            with open(path) as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            return ["%s: could not read %s, so a ring it recorded cannot be told lost or "
                    "delivered." % (label, path)]
        if not isinstance(state, dict):
            return []
        rings = state.get("rings") or []
        found = lost(rings, turn_end)
        changed = False
        for r in rings:
            if r in found:
                r["surfaced"] = int(now_ts)
                changed = True
            elif turn_end and not r.get("delivered") and not r.get("surfaced") \
                    and (r.get("at") or 0) < turn_end:
                r["delivered"] = True
                changed = True
        if changed:
            try:
                atomic_write_json(path, state)
            except OSError:
                return ["%s: found %d lost ring(s) but could not mark them reported in %s, so "
                        "the next start will repeat them." % (label, len(found), path)]
    out = []
    for r in found:
        out.append("%s rang at %s but no turn followed:\n%s"
                   % (label, _when(r.get("at") or 0), _indent(r.get("text") or "(no text)")))
    if found and turn_end is None:
        out.append("(%s could not be read, so some of these may have landed.)"
                   % heartbeat_path(cfg))
    return out


def _indent(text):
    return "\n".join("    " + line for line in text.splitlines())


def catch_up(cfg, runner=None, timeout=LOOK_TIMEOUT_SEC):
    """One GitHub look through the waker's compare-and-advance. Lines to print."""
    from . import waker
    if waker.spec(cfg) is None:
        return []
    runner = runner or waker.gh_runner(timeout) or (lambda _q: None)
    try:
        code, text = waker.run(cfg, once=True, runner=runner)
    except Exception as exc:                                    # noqa: BLE001
        code, text = 1, "%s: %s" % (type(exc).__name__, exc)
    if code == 2:
        return ["GitHub, one look at this start (recorded as seen, so it will not ring again):\n%s"
                % _indent(text)]
    if code == 1:
        return ["GitHub, one look at this start: FAILED — %s. Activity since the last look is "
                "unknown, not absent; the waker looks again when this session's first turn "
                "ends." % text]
    return ["GitHub, one look at this start: %s." % text] if text else []


def session_start(cfg, now=time.time, runner=None, timeout=LOOK_TIMEOUT_SEC):
    """Every line SessionStart should print about wakes: lost rings first, then the catch-up."""
    from . import crawlerwaker, waker
    if waker.in_linked_worktree(cfg):
        return []
    if os.environ.get("SHOWRUNNER_LOSTRING") == "off":
        # For a harness that drives `whoami.sh` against a REAL checkout — the suite does — which
        # must neither call GitHub nor mark that checkout's own lost rings reported.
        return []
    t = now()
    lines = surface(cfg, "waker-state.json", "github-waker", "GitHub activity", t)
    lines += surface(cfg, crawlerwaker.STATE, "crawler-waker", "The Crawler waker", t)
    lines += catch_up(cfg, runner, timeout)
    return lines
