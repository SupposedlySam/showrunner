"""Wake an idle orchestrator when one of its Crawlers needs it — and only then.

An orchestrator learned that a Crawler was refused at its turn-end only while something was
polling for it: `waiting --follow`, a hand-rolled watcher, or game_loop's waiting probe under a
bound mandate. A background poll dies every time the host restarts the session, and SRCode
restarts it on every launch. So this is the shape the GitHub waker already has: a Stop hook with
`asyncRewake`, started by the harness after every turn, which exits 2 with a message to wake the
idle session and 0 otherwise. Registered for every checkout; in a linked worktree (a Crawler) it
exits 0 at once.

WHAT RINGS, decided by `waiting`'s own classification so the two never disagree:

* a Crawler `waiting` lists as BLOCKED — refused at a turn-end with no work since — once it has
  stayed that way for the QUIET WINDOW (`crawler_waker.quiet_sec`, default 180): no work since
  the refusal and no transcript write for that long. Measured on a consumer: 19 of 19 refusals
  went on to close their leaf by themselves, 28 s to 19 min later, every one acting within
  seconds. A refusal is the gate working; ringing on it wakes the lead for nothing.
* a Crawler whose harness STOOD DOWN after repeated refusals and let it stop with its leaf open —
  after the same window, or AT ONCE if its process has exited, since nothing is left to carry on.
* a Crawler `waiting` lists as STALLED (a live session whose transcript froze).

WHAT DOES NOT: a block or stall that was already outstanding when the turn ended — the lead's
turn-end saw it (the inert-Crawler gate refuses that turn-end over it); a released block; the
same block twice, however many wakers are alive. A Crawler released and then refused again is a
NEW refusal (the harness rewrites `at` and counts it), so it rings.

COST WHILE IDLE. Each poll reads the campaign record, stats each Crawler's transcript and the
journal's size. `waiting` itself — a harness call per live Crawler — runs only when a Crawler's
transcript has gone quiet for the window, when one's process ends, or when the journal has a new
block to judge; a working Crawler writes its transcript constantly and costs one stat.

ONE RING PER BLOCK. Every turn-end starts a waker; the newest owns the watch and an older one
exits at its next poll, and a ring is recorded under a lock before it is sent.
"""
import json
import os
import time

QUIET_SEC = 180
POLL_SEC = 20
BUDGET_SEC = 604740
STATE = "crawler-waker-state.json"
OWNER = "crawler-waker-owner"


def spec(cfg):
    w = (cfg.get("crawler_waker") if hasattr(cfg, "get") else None) or {}

    def num(key, default):
        try:
            v = int(w.get(key, default))
            return v if v >= 0 else default
        except (TypeError, ValueError):
            return default
    return {"quiet_sec": num("quiet_sec", QUIET_SEC), "poll_sec": max(1, num("poll_sec", POLL_SEC)),
            "budget_sec": num("budget_sec", BUDGET_SEC),
            "enabled": w.get("enabled", True) is not False}


def in_linked_worktree(cfg):
    tree = getattr(cfg, "tree", None)
    return bool(tree) and os.path.realpath(tree) != os.path.realpath(cfg.root)


def _path(cfg, name):
    return os.path.join(cfg.state_dir, name)


def load_state(cfg):
    try:
        with open(_path(cfg, STATE)) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(cfg, state):
    from .util import atomic_write_json
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        atomic_write_json(_path(cfg, STATE), state)
    except OSError:
        pass


def _owner(cfg, token=None):
    p = _path(cfg, OWNER)
    try:
        if token is not None:
            os.makedirs(cfg.state_dir, exist_ok=True)
            with open(p, "w") as fh:
                fh.write(token)
            return token
        with open(p) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def block_key(crawler, refused_at, stood_down):
    """One refusal of one Crawler. The journal's `crawler.blocked` and `waiting` carry both."""
    return "%s|%s|%s" % (crawler, refused_at, stood_down or "")


def outstanding(cfg, upto_seq=None):
    """Keys of the blocks the journal holds as still outstanding (latest transition = blocked)."""
    from . import events
    evs, _bad, _unreadable = events.read(cfg)
    latest = {}
    for ev in evs:
        if upto_seq is not None and (ev.get("seq") or 0) > upto_seq:
            break
        if ev.get("kind") in ("crawler.blocked", "crawler.unblocked") and ev.get("crawler"):
            latest[ev["crawler"]] = ev
    return {block_key(c, ev.get("since"), ev.get("stood_down"))
            for c, ev in latest.items() if ev.get("kind") == "crawler.blocked"}


def _journal_seq(cfg):
    from . import events
    return events._next_seq(cfg) - 1


def _transcript_mtime(cfg, entry):
    from .util import transcript_path
    p = transcript_path(cfg.abspath(entry.get("worktree")), entry.get("session"))
    try:
        return int(os.path.getmtime(p)) if p else None
    except OSError:
        return None


def _crawlers(cfg):
    from . import campaign
    try:
        return campaign.load(cfg).get("crawlers") or []
    except Exception:                                           # noqa: BLE001
        return []


def wake_text(item, quiet_sec):
    leaf = item.get("leaf") or "?"
    if item["kind"] == "stalled":
        head = "Crawler %s (leaf %s) is STALLED: %s." % (item["crawler"], leaf, item["why"])
    elif item.get("stood_down"):
        head = ("Crawler %s (leaf %s): stood down after %s refusals, leaf still open%s. %s"
                % (item["crawler"], leaf, item["stood_down"],
                   "" if item.get("alive") else ", and its process has exited", item["why"]))
    else:
        head = ("Crawler %s (leaf %s) is BLOCKED: %s. It has done no work since the refusal "
                "and stayed that way past the %ds quiet window (`crawler_waker.quiet_sec`), so it "
                "is not carrying on by itself." % (item["crawler"], leaf, item["why"], quiet_sec))
    return ("A Crawler needs you (the Crawler waker rang).\n\n  %s\n\nLook: `showrunner status`, "
            "`showrunner show %s`. It needs a message, not time." % (head, leaf))


def judge(cfg, sp, detail, seen, rung, now_ts, mtimes):
    """(items to ring, the soonest time a pending block becomes due or None)."""
    ring, due = [], None
    for b in detail.get("blocked_crawlers") or []:
        key = block_key(b["crawler"], b.get("refused_at"), b.get("stood_down"))
        if key in seen or key in rung:
            continue
        if b.get("stood_down") and b.get("alive") is False:
            ring.append(dict(b, kind="blocked", key=key))
            continue
        quiet_from = max(x for x in (b.get("refused_at"), mtimes.get(b["crawler"]), 0)
                         if x is not None)
        at = quiet_from + sp["quiet_sec"]
        if now_ts >= at:
            ring.append(dict(b, kind="blocked", key=key))
        else:
            due = at if due is None else min(due, at)
    for s in detail.get("stalled_crawlers") or []:
        key = "stalled|%s|%s" % (s["crawler"], mtimes.get(s["crawler"]))
        if key in seen or key in rung:
            continue
        ring.append(dict(s, kind="stalled", key=key))
    return ring, due


def _ask(cfg):
    from . import campaign
    from .graph import open_graph
    return campaign.waiting(cfg, open_graph(cfg))[1]


def run(cfg, once=False, sleep=time.sleep, now=time.time, ask=None):
    """(exit code, text). 2 with a wake; 0 quiet; `once` reports what it would ring now."""
    from .util import file_lock
    ask = ask or _ask
    sp = spec(cfg)
    if in_linked_worktree(cfg):
        return 0, "crawler-waker: a linked worktree — the orchestrator's checkout watches"
    if not sp["enabled"]:
        return 0, "crawler-waker: disabled (`crawler_waker.enabled` is false)"
    start = now()
    base_seq = _journal_seq(cfg)
    # SEEN AT THE TURN-END: every block the journal holds outstanding now, and every stall that
    # was already a stall. The lead's own turn-end ran the inert-Crawler gate over them.
    seen = outstanding(cfg, base_seq)
    from .graph import STALL_IDLE_SECONDS
    for e in _crawlers(cfg):
        m = _transcript_mtime(cfg, e)
        if m is not None and m <= start - STALL_IDLE_SECONDS:
            seen.add("stalled|%s|%s" % (e.get("crawler"), m))
    token = None if once else "%d:%r" % (os.getpid(), start)
    if token:
        _owner(cfg, token)
    deadline = start + sp["budget_sec"]
    asked = {}                      # crawler -> (transcript mtime, alive) when last judged
    journal_size = None
    due = None
    first = True
    while True:
        if not once and not first:
            sleep(sp["poll_sec"])
            if _owner(cfg) not in (None, token):
                return 0, ""
        first = False
        t = now()
        from . import campaign
        entries = [e for e in _crawlers(cfg) if e.get("crawler")]
        mtimes, need = {}, once or (due is not None and t >= due)
        for e in entries:
            c = e["crawler"]
            m = _transcript_mtime(cfg, e)
            mtimes[c] = m
            alive = campaign.live(e)
            state = (m, alive)
            prev = asked.get(c)
            if alive and m is not None and t - m >= sp["quiet_sec"] and prev != state:
                need = True
            elif prev is not None and prev[1] and not alive:
                need = True                     # its process ended since the last look
            asked.setdefault(c, state)
        try:
            size = os.path.getsize(os.path.join(cfg.state_dir, "events.jsonl"))
        except OSError:
            size = None
        if journal_size is not None and size != journal_size:
            from . import events
            if any(ev.get("kind") == "crawler.blocked" and (ev.get("seq") or 0) > base_seq
                   for ev in events.read(cfg, since_seq=base_seq)[0]):
                need = True
        journal_size = size
        if need:
            detail = ask(cfg)
            for e in entries:
                asked[e["crawler"]] = (mtimes.get(e["crawler"]), campaign.live(e))
            with file_lock(_path(cfg, STATE)):
                state = load_state(cfg)
                rung = set(state.get("rung") or [])
                ring, due = judge(cfg, sp, detail, seen, rung, t, mtimes)
                if ring and not once:
                    text = "\n\n".join(wake_text(i, sp["quiet_sec"]) for i in ring)
                    state["rung"] = sorted(rung | {i["key"] for i in ring})[-2000:]
                    state["last_fired"] = int(t)
                    state["last_crawlers"] = [i["crawler"] for i in ring]
                    _save_state(cfg, state)     # recorded FIRST: a wake re-sent is noise
                    return 2, text
            if once:
                if ring:
                    return 0, "would ring now:\n\n" + "\n\n".join(
                        wake_text(i, sp["quiet_sec"]) for i in ring)
                return 0, ("nothing to ring: %d blocked, %d stalled, all already seen or "
                           "inside the %ds quiet window" % (
                               len(detail.get("blocked_crawlers") or []),
                               len(detail.get("stalled_crawlers") or []), sp["quiet_sec"]))
        elif once:
            return 0, "nothing to ring"
        if t >= deadline:
            return 0, ""
        if due is None and not any(st[1] for st in asked.values()):
            # NOTHING LEFT TO BLOCK: no Crawler is alive and none is pending. A spawn happens in
            # one of the lead's turns, and that turn's end starts a fresh waker.
            return 0, ""
