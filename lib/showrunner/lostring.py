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
ambiguous one. A ring the other waker `covered` (see `cover`) is not reported: the cover ring
carried its text, and is itself a ring reported here if it was dropped too.

WITH THE TRANSCRIPT (SessionStart's own, see below), a ring counts as heard only when the
transcript holds its wake text in an entry dated at or after it: the user entry a delivered wake
becomes, or the `queued_command` attachment of one that arrived during a turn. A later turn end
is not enough, since a turn the user started ends after a dropped ring too. That marks it
`delivered: "transcript"`, and the turn-end rule is left for a ring with no text or a session
with no readable transcript.

THE CATCH-UP is one GitHub look through the waker's locked compare-and-advance (`--once`
semantics), with a short timeout, so what arrived after a lost ring is reported here and is
recorded as seen — the first waker after this turn does not ring it again. A look that failed
says so; it is never "nothing new".

A RING UNDER A HOST ALREADY GONE is reported apart, whatever it was marked. A waker outlives the
claude process that started it, and before it checked its host (see `waker`) one rang into a
replaced host: the ring was then `covered` by the other orphan, the events it rang were recorded
as seen, and the resumed session heard "nothing new". THE RULE, read from the session's own
transcript (the path the host hands SessionStart): a ring is under a gone host when the session
was started or resumed (`SessionStart:startup`/`resume`, not `compact`) after the ringing waker
started and before the ring, and the transcript does not hold its wake text. The waker's start is the `waker_started` it recorded,
or for a ring recorded before that, its waker's last turn-end stamp at or before the ring. A ring
whose recorded host is still running is never one: an in-process resume keeps the host. Nor is
one the transcript shows heard (below). Each is
reported once, marked `host_gone`, with the events it marked seen. A cover ring for a ring
reported here is not repeated. With no transcript path this rule is not applied, and an
unreadable transcript is said, not read as "none".

WHAT THIS CANNOT DO: start a turn. SessionStart context reaches the agent with its first turn,
and the waker starts again only when that turn ends. Nor can it see why a ring was lost: the
host's side is not showrunner's to read. In a linked worktree it says nothing, as the wakers do.
"""
import json
import os
import time

LOOK_TIMEOUT_SEC = 5
HOST_STARTS = ("SessionStart:startup", "SessionStart:resume")


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


def _iso_epoch(text):
    import calendar
    try:
        base, _, frac = str(text).rstrip("Z").partition(".")
        return calendar.timegm(time.strptime(base, "%Y-%m-%dT%H:%M:%S")) + \
            float("0." + frac if frac else 0)
    except (ValueError, OverflowError):
        return None


def _entries(path, wanted=None):
    """Yield (epoch, record) for transcript lines; only lines containing one of the byte strings
    in `wanted` are parsed. Raises OSError when the file cannot be read."""
    with open(path, "rb") as fh:
        for line in fh:
            if wanted is not None and not any(w in line for w in wanted):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            t = _iso_epoch(rec.get("timestamp")) if isinstance(rec, dict) else None
            if t is not None:
                yield t, rec


def host_starts(path):
    """When this session's host process started, as sorted epochs: each `startup` or `resume`
    SessionStart in the transcript. Raises OSError when the transcript cannot be read."""
    out = set()
    for t, rec in _entries(path, (b"SessionStart:",)):
        att = rec.get("attachment")
        if isinstance(att, dict) and att.get("hookName") in HOST_STARTS:
            out.add(int(t))
    return sorted(out)


def _needles(text):
    """Byte strings a transcript line holds when it carries this wake text: its first line after
    the heading (the heading alone is shared by every ring), as JSON writes it either way."""
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    pick = (lines[1] if len(lines) > 1 else lines[0] if lines else "")[:80]
    if not pick:
        return set()
    return {json.dumps(pick, ensure_ascii=a)[1:-1].encode("utf-8") for a in (False, True)}


def heard(path, rings):
    """{index: True/False/None} for `rings`: did the transcript take the wake in? True when an
    entry dated at or after the ring carries its text, as a delivered wake's user entry or, for
    one that arrived during a turn, its `queued_command` attachment. None for a ring with no text.
    One pass over the transcript; raises OSError when it cannot be read."""
    want = {i: _needles(r.get("text")) for i, r in enumerate(rings)}
    out = {i: (False if n else None) for i, n in want.items()}
    every = set().union(*want.values()) if want else set()
    if not every:
        return out
    for t, rec in _entries(path, every):
        line = json.dumps(rec, ensure_ascii=False).encode("utf-8") + \
            json.dumps(rec).encode("utf-8")
        for i, n in want.items():
            if out[i] is False and t >= (rings[i].get("at") or 0) - 1 and \
                    any(x in line for x in n):
                out[i] = True
    return out


def _stamps(cfg, hook):
    """Every turn-end stamp `hook` left, sorted; [] when the heartbeat cannot be read."""
    out = []
    try:
        with open(heartbeat_path(cfg)) as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if row.get("hook") == hook:
                        out.append(int(row.get("ts") or 0))
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        return []
    return sorted(out)


def waker_start(ring, stamps):
    """When the waker that rang `ring` started: what it recorded, or its last turn-end stamp at
    or before the ring. None when neither is known."""
    if ring.get("waker_started") is not None:
        return float(ring["waker_started"])
    at = ring.get("at") or 0
    before = [s for s in stamps if s <= at]
    return float(before[-1]) if before else None


def under_gone_host(ring, starts, stamps, was_heard):
    """(the host start that replaced the ringing waker's host, or None). `was_heard` is
    `heard`'s verdict for the ring: a wake the transcript took in reached a live host."""
    from .util import host_gone
    if ring.get("host_gone") or ring.get("surfaced"):
        return None
    at = ring.get("at") or 0
    began = waker_start(ring, stamps)
    if began is None:
        return None
    later = [s for s in starts if began < s <= at]
    if not later:
        return None
    host = ring.get("host")
    if host and not host_gone(host):
        return None
    if was_heard:
        return None
    return later[-1]


def lost(rings, turn_end):
    """The rings no turn end followed, not yet reported. `turn_end` None (unreadable) counts every
    unmarked ring as lost: the caller says it could not tell."""
    return [r for r in rings or []
            if not r.get("surfaced") and not r.get("delivered") and not r.get("covered")
            and (turn_end is None or (r.get("at") or 0) >= turn_end)]


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ts))


def surface(cfg, state_file, hook, label, now_ts, transcript=None, starts=None, skip=None):
    """Report the lost rings in one waker's state, marking them so each is reported once. Rings
    under a gone host come first (see the module note) when `starts`, the session's host starts,
    is given. `skip(ring)` says a cover ring whose ring was already reported here."""
    from .util import atomic_write_json, file_lock
    path = os.path.join(cfg.state_dir, state_file)
    if not os.path.exists(path):
        return []
    turn_end = last_turn_end(cfg, hook)
    stamps = _stamps(cfg, hook) if starts else []
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
        # WHAT THE TRANSCRIPT SAYS, when there is one. A turn end after a ring is not proof it
        # was heard: a turn the user started ends after it too. The wake text in the transcript
        # is. A ring already settled by it, reported, or covered is not judged again.
        open_ = [r for r in rings if r.get("delivered") != "transcript" and not r.get("surfaced")
                 and not r.get("covered") and not r.get("host_gone")]
        verdict = {}
        if transcript and starts is not None:
            try:
                got = heard(transcript, open_)
                verdict = {id(r): got[i] for i, r in enumerate(open_)}
            except OSError:
                verdict = {}
        gone = []
        for r in rings if starts else []:
            s = under_gone_host(r, starts, stamps, verdict.get(id(r)) is True)
            if s is not None:
                r["host_gone"] = int(now_ts)
                r.setdefault("surfaced", int(now_ts))
                gone.append((r, s))
        changed = bool(gone)
        for r in open_:
            if verdict.get(id(r)) is True and not r.get("host_gone"):
                r["delivered"] = "transcript"
                changed = True
        # Judged by the transcript: lost when it does not hold the wake. Otherwise (no
        # transcript, or a ring with no text) the heartbeat rule.
        by_tx = [r for r in open_ if verdict.get(id(r)) is False and not r.get("host_gone")]
        rest = [r for r in rings if not r.get("host_gone") and verdict.get(id(r)) is None
                and r.get("delivered") != "transcript"]
        found = by_tx + lost(rest, turn_end)
        for r in rings:
            if r in found:
                r["surfaced"] = int(now_ts)
                changed = True
            elif turn_end and not r.get("delivered") and not r.get("surfaced") \
                    and not r.get("covered") and (r.get("at") or 0) < turn_end \
                    and id(r) not in verdict:
                r["delivered"] = True
                changed = True
        if changed:
            try:
                atomic_write_json(path, state)
            except OSError:
                return ["%s: found %d lost ring(s) but could not mark them reported in %s, so "
                        "the next start will repeat them." % (label, len(found), path)]
    out = []
    for r, s in gone:
        if skip and skip(r):
            continue
        began = waker_start(r, stamps)
        seen = [e for e in r.get("events") or [] if e != "cover"]
        out.append("%s rang at %s under a host that had already gone: this session was resumed "
                   "under a new claude process at %s, after that waker started at %s, so no turn "
                   "heard it.%s\n%s"
                   % (label, _when(r.get("at") or 0), _when(s), _when(began or 0),
                      " It marked these seen, so no later look rang them: %s." % ", ".join(seen)
                      if seen else "", _indent(r.get("text") or "(no text)")))
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


def _covers_reported(cfg, other_state):
    """skip(ring): is this cover ring for a ring of the other waker marked `host_gone`, within a
    few seconds of when that ring was marked covered? Then its text is already reported."""
    try:
        with open(os.path.join(cfg.state_dir, other_state)) as fh:
            rings = (json.load(fh) or {}).get("rings") or []
    except (OSError, ValueError, AttributeError):
        rings = []
    marks = [r.get("covered") for r in rings if r.get("host_gone") and r.get("covered")]

    def skip(ring):
        return ring.get("events") == ["cover"] and any(
            abs((ring.get("at") or 0) - m) <= 5 for m in marks)
    return skip


def session_start(cfg, now=time.time, runner=None, timeout=LOOK_TIMEOUT_SEC, transcript=None):
    """Every line SessionStart should print about wakes: lost rings first, then the catch-up.
    `transcript` is this session's (from SessionStart's stdin), from which rings under a host
    that had already gone are told."""
    from . import crawlerwaker, waker
    if waker.in_linked_worktree(cfg):
        return []
    if os.environ.get("SHOWRUNNER_LOSTRING") == "off":
        # For a harness that drives `whoami.sh` against a REAL checkout — the suite does — which
        # must neither call GitHub nor mark that checkout's own lost rings reported.
        return []
    t = now()
    starts, lines = None, []
    if transcript:
        try:
            starts = host_starts(transcript)
        except OSError as exc:
            if any(os.path.exists(os.path.join(cfg.state_dir, f))
                   for f in ("waker-state.json", crawlerwaker.STATE)):
                lines.append("(could not read this session's transcript %s (%s), so a ring rung "
                             "under a host that had already gone cannot be told here.)"
                             % (transcript, type(exc).__name__))
    lines += surface(cfg, "waker-state.json", "github-waker", "GitHub activity", t,
                     transcript, starts)
    lines += surface(cfg, crawlerwaker.STATE, "crawler-waker", "The Crawler waker", t,
                     transcript, starts, _covers_reported(cfg, "waker-state.json"))
    lines += catch_up(cfg, runner, timeout)
    return lines
