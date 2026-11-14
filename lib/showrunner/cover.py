"""Cross-cover: each waker rings once for the other's ring that no turn followed.

MEASURED on a consumer: 31 of 36 GitHub rings became a turn and 5 did not. The lost ones came
after 7 s to 37 min of idle, and delivered ones after as long, so it is not an idle threshold.
No restart was involved, and the session's transcript was not written between the ring and
the next human message an hour later. The host sometimes drops an asyncRewake exit 2, and
showrunner cannot see why. A ring ends its waker, and only a turn end starts the next, so one
dropped ring stopped all watching. `lostring` reports it at the next SessionStart, but an idle
session has none.

So while the session is idle the Crawler waker and the GitHub waker are both alive, and each
watches the other's recorded rings. THE RULE, for a ring the other waker recorded after this
one started:

* it is DROPPED when `waker_cover.window_sec` (default 60) has passed since it, no turn-end
  stamp of its waker in `hook-heartbeat.jsonl` came after it (the same rule as `lostring`,
  where a same-second stamp does not count), and the session's transcript has not been written
  since it. A delivered wake is written to the transcript within milliseconds, so this guard
  holds during a turn that has not ended yet. With no transcript path, which the host gives a
  hook on stdin, the guard is the heartbeat alone.
* a dropped ring is marked `covered` under the other waker's lock, and this waker rings ONCE
  for it, quoting its text, then exits as any ring does. The next turn end starts both again.
* a ring a turn did follow is marked `delivered` and never rung. A covered ring is never
  covered again, and `lostring` does not report it at SessionStart: the cover ring carried its
  text, and the cover ring is a ring of its own that `lostring` reports if it is dropped too.

Each cover adds a `covered-dropped-ring` line, with both times, to `waker-decisions.jsonl`, so
the host's drop rate is read from the record.

WHAT THIS CANNOT DO: make a ring land. A cover ring goes to the same host and can be dropped
too, and then both wakers have exited and SessionStart is the last net. It needs both wakers:
with no `waker` configured there is no GitHub waker to cover the Crawler waker, and with
`crawler_waker.enabled` false the reverse. It is LATE by the window plus the covering waker's
poll: the Crawler waker looks every `crawler_waker.poll_sec` (20 s), the GitHub waker only at
its `poll_sec` (300 s), so its looks cost no more than they do today.

COST: a stat of the other waker's state file per poll. That file is read again only when it
changed or a ring in it becomes due, and the heartbeat file only when a ring is due.
"""
import json
import os
import time

WINDOW_SEC = 60

WAKERS = {
    "github-waker": {"state": "waker-state.json", "label": "The GitHub waker"},
    "crawler-waker": {"state": "crawler-waker-state.json", "label": "The Crawler waker"},
}


def window(cfg):
    """The cover window in seconds, or None when cover is turned off."""
    c = (cfg.get("waker_cover") if hasattr(cfg, "get") else None) or {}
    if c.get("enabled", True) is False:
        return None
    try:
        v = int(c.get("window_sec", WINDOW_SEC))
        return v if v >= 1 else WINDOW_SEC
    except (TypeError, ValueError):
        return WINDOW_SEC


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ts))


def _mtime(path):
    try:
        return os.path.getmtime(path) if path else None
    except OSError:
        return None


def followed(ring, turn_end, transcript_mtime):
    """Did a turn follow this ring? A stamp of its waker after it (same second excluded, as in
    `lostring`), or a transcript written after it."""
    at = ring.get("at") or 0
    if turn_end and at < turn_end:
        return True
    return transcript_mtime is not None and transcript_mtime > (ring.get("at_f") or at)


def dropped(rings, started, now_ts, window_sec, turn_end, transcript_mtime):
    """(rings to cover, rings a turn followed, the soonest time a pending ring becomes due)."""
    cover, landed, due = [], [], None
    for r in rings or []:
        at = r.get("at") or 0
        if r.get("covered") or r.get("delivered") or r.get("surfaced") or at < int(started):
            continue
        if followed(r, turn_end, transcript_mtime):
            landed.append(r)
        elif now_ts - at >= window_sec:
            cover.append(r)
        else:
            due = at + window_sec if due is None else min(due, at + window_sec)
    return cover, landed, due


def cover_text(label, rings, by_label):
    parts = ["%s rang at %s and no turn followed: %s"
             % (label, _when(r.get("at") or 0), r.get("text") or "(no text)") for r in rings]
    return ("A wake was dropped, so %s is ringing for it once (cross-cover).\n\n%s"
            % (by_label[0].lower() + by_label[1:], "\n\n".join(parts)))


class Cover(object):
    """One waker's watch over the other's rings. `check` is called once per poll."""

    def __init__(self, cfg, other, by, started, transcript=None):
        self.cfg, self.other, self.by, self.started = cfg, other, by, started
        self.transcript = transcript
        self.window = window(cfg)
        self.path = os.path.join(cfg.state_dir, WAKERS[other]["state"])
        self.seen_sig = None
        self.due = None

    def _sig(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime, st.st_size)
        except OSError:
            return None

    def check(self, now_ts):
        """The text to ring for the other waker's dropped ring, or None."""
        from . import lostring
        from .util import atomic_write_json, file_lock
        if self.window is None:
            return None
        sig = self._sig()
        if sig is None or (sig == self.seen_sig and (self.due is None or now_ts < self.due)):
            return None
        with file_lock(self.path):
            try:
                with open(self.path) as fh:
                    state = json.load(fh)
            except (OSError, ValueError):
                return None
            rings = state.get("rings") if isinstance(state, dict) else None
            if not rings:
                self.seen_sig, self.due = sig, None
                return None
            turn_end = lostring.last_turn_end(self.cfg, self.other)
            if turn_end is None:
                # The heartbeat could not be read, so a ring cannot be told dropped. SessionStart
                # says so; ringing here could ring for one a turn followed.
                self.seen_sig, self.due = sig, None
                return None
            tx = _mtime(self.transcript)
            cover, landed, self.due = dropped(rings, self.started, now_ts, self.window,
                                              turn_end, tx)
            for r in landed:
                r["delivered"] = True
            for r in cover:
                r["covered"] = int(now_ts)
                r["covered_by"] = self.by
            if cover or landed:
                try:
                    atomic_write_json(self.path, state)
                except OSError:
                    return None         # unmarked, a cover would repeat: ring nothing
            self.seen_sig = self._sig()
        if not cover:
            return None
        from . import waker
        waker.log_decisions(self.cfg, [{
            "item": "-", "event": "%s ring at %d" % (self.other, r.get("at") or 0), "author": "-",
            "verdict": "covered-dropped-ring", "rang_at": r.get("at") or 0,
            "covered_at": int(now_ts), "rang_by": self.other, "covered_by": self.by,
            "reason": "no %s turn end after it and %ds passed; %s"
                      % (self.other, self.window,
                         "no transcript path to check" if not self.transcript else
                         "transcript could not be read, so the heartbeat alone decided"
                         if tx is None else "transcript not written since")}
            for r in cover], now_ts)
        return cover_text(WAKERS[self.other]["label"], cover, WAKERS[self.by]["label"])
