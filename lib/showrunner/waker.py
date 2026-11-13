"""Wake an idle owner session when GitHub activity arrives — configured, not hand-rolled.

`.showrunner/hooks/issue-waker.py` was this repo's own: one repository and one trusted set, in
code. Owner agents were told to read it and write their own, and three private copies existed
before anybody asked for the product. This is that product: what to watch comes from the `waker`
section of config, and `worktree register` wires it as a Stop hook with `asyncRewake`, which keeps
running in the background after a turn ends and turns an exit 2 with stderr into a wake.

    "waker": {
      "repos":           ["owner/name", ...],      # every issue and PR on these
      "upstream":        [{"repo": "o/n", "author": "me"}],  # only items `author` opened there
      "trusted_logins":  ["login", ...],
      "trusted_names":   ["Display Name", ...],
      "signature":       "— 🤖 <name> owner agent",
      "poll_sec": 300, "budget_sec": 604740
    }

Nothing configured means the hook exits at once: it costs nothing until a project opts in.

WHAT RINGS: a new issue or PR; a state change — closed, MERGED, reopened, which arrives as no
comment at all; a comment; a REVIEW with a body or an approve/request-changes verdict; an INLINE
review comment. Comments on a closed item count: closed is not finished. A close or merge rings
even when the agent made it: a state change carries no signature to recognise.

ONE RING PER EVENT, however many wakers are running. Each turn-end starts one, so several can be
alive at once: the newest owns the watch and an older one exits when it sees it was replaced, and
every poll compares against the state on disk under a lock, so the first to record an event is
the only one that rings for it. A starting waker sleeps only what is left of `poll_sec` since the
last look on record, so an event that arrived during a long turn rings at that turn's end.

WHAT DOES NOT, stated so silence is not read as a broken watcher: the agent's own writing — a
comment, review or PR body whose last signature is `signature`, with or without Claude Code's
attribution line after it; anything older than the last 20 comments/reviews on one item between
two polls; chat debts, which llm_chat's own waker rings for. OWN WRITING IS JUDGED BY THE
SIGNATURE ALONE. The author cannot tell: the agent posts under its maintainer's account, and so
may other agents. Nor can the attribution line, which every Claude Code agent's PR ends with:
read as "mine" it silenced another agent's PR on the shared account. So agents sharing an account
need distinct signatures, and with none configured nothing is own writing — it all rings, and the
wake says why.

EVERY DECISION IS LOGGED to `waker-decisions.jsonl` beside the state, one line per event judged
(rang, own, seen, older-than-window) with its reason, as a bounded tail: a miss is then read,
not guessed.

ONE GRAPHQL QUERY PER POLL, never a REST call per item: the 5,000/hour REST limit is shared by
every agent on a machine and was measured exhausted. A failed look is never "nothing new" — it
returns None, and the first look after an unreadable state seeds silently instead of replaying
the backlog. A LINKED WORKTREE exits at once: a Crawler inherits the tree's hooks, and a waker
there would hold every Crawler at its turn-end for half an hour.
"""
import json
import os
import subprocess
import time

PR_ATTRIBUTION = "Generated with [Claude Code](https://claude.com/claude-code)"
FIRST = 30
LAST = 20
DECISIONS_KEEP = 1000

_THREAD = """
  number title state url updatedAt body
  author { login ... on User { name } }
  comments(last: %d) { nodes { databaseId body author { login ... on User { name } } } }
""" % LAST
_PR = _THREAD + """
  reviews(last: %d) { nodes { databaseId state body author { login ... on User { name } }
    comments(last: %d) { nodes { databaseId body author { login ... on User { name } } } } } }
""" % (LAST, LAST)


# A WEEK, a minute under the hook's timeout. This was 1800, so a session idle past half an hour
# heard nothing until somebody typed; it was bounded because every turn-end left another poller.
# Now the newest waker owns the watch and an older one exits at its next poll, so a long budget is
# one poller, and the harness ends it with the session.
BUDGET_SEC = 604740


def spec(cfg):
    """The configured watch, normalised, or None when nothing is configured."""
    w = (cfg.get("waker") if hasattr(cfg, "get") else None) or {}
    repos = [r for r in (w.get("repos") or []) if isinstance(r, str) and r.count("/") == 1]
    upstream = [u for u in (w.get("upstream") or [])
                if isinstance(u, dict) and str(u.get("repo", "")).count("/") == 1
                and u.get("author")]
    if not repos and not upstream:
        return None
    return {
        "repos": repos,
        "upstream": upstream,
        "trusted_logins": {str(x).lower() for x in (w.get("trusted_logins") or [])},
        "trusted_names": {str(x).lower() for x in (w.get("trusted_names") or [])},
        "signature": w.get("signature") or "",
        "poll_sec": int(w.get("poll_sec") or 300),
        "budget_sec": int(w.get("budget_sec") or BUDGET_SEC),
    }


def query(sp):
    """ONE GraphQL document for everything watched: an alias per repo and per upstream search."""
    parts = []
    for i, repo in enumerate(sp["repos"]):
        owner, name = repo.split("/")
        parts.append('r%d: repository(owner: %s, name: %s) {\n'
                     '  issues(first: %d, orderBy: {field: UPDATED_AT, direction: DESC}) '
                     '{ nodes { %s } }\n'
                     '  pullRequests(first: %d, orderBy: {field: UPDATED_AT, direction: DESC}) '
                     '{ nodes { %s } }\n}'
                     % (i, json.dumps(owner), json.dumps(name), FIRST, _THREAD, FIRST, _PR))
    for i, up in enumerate(sp["upstream"]):
        parts.append('u%d: search(query: %s, type: ISSUE, first: %d) {\n'
                     '  nodes { ... on PullRequest { %s } ... on Issue { %s } }\n}'
                     % (i, json.dumps("repo:%s author:%s" % (up["repo"], up["author"])),
                        FIRST, _PR, _THREAD))
    return "query {\n%s\n}" % "\n".join(parts)


def _gh():
    # An explicit binary wins: the suite points this at a stub so the verb can be driven end to
    # end without the network, and a machine with gh off PATH can name it.
    env = os.environ.get("SHOWRUNNER_GH")
    if env and os.access(env, os.X_OK):
        return env
    for c in ("gh", "/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"):
        p = c if os.path.isabs(c) else None
        if p is None:
            from shutil import which
            p = which(c)
        if p and os.access(p, os.X_OK):
            return p
    return None


def look(sp, runner=None):
    """Every watched item as {"repo#n": item}, or None when GitHub could not be read."""
    if runner is None:
        gh = _gh()
        if not gh:
            return None

        def runner(q):
            try:
                out = subprocess.run([gh, "api", "graphql", "-f", "query=" + q],
                                     capture_output=True, text=True, timeout=60,
                                     stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                return None
            return out.stdout if out.returncode == 0 else None
    raw = runner(query(sp))
    if not raw or not raw.strip():
        return None
    try:
        body = json.loads(raw)
        data = body.get("data")
        if body.get("errors") or not data:
            return None
        items = {}
        for i, repo in enumerate(sp["repos"]):
            node = data["r%d" % i]
            for n in (node["issues"]["nodes"] or []) + (node["pullRequests"]["nodes"] or []):
                if n and "number" in n:
                    items["%s#%s" % (repo, n["number"])] = dict(n, repo=repo,
                                                                is_pr="reviews" in n)
        for i, up in enumerate(sp["upstream"]):
            for n in data["u%d" % i]["nodes"] or []:
                if n and "number" in n:
                    items["%s#%s" % (up["repo"], n["number"])] = dict(n, repo=up["repo"],
                                                                      is_pr="reviews" in n)
        return items
    except (ValueError, KeyError, TypeError):
        return None


def _who(node):
    a = (node or {}).get("author") or {}
    return a.get("login") or "?", a.get("name") or ""


def events(item):
    """(event_id, login, name, text, kind) for every comment, review and inline comment."""
    out = []
    for c in (item.get("comments") or {}).get("nodes") or []:
        out.append(("c%s" % c.get("databaseId"), c, "comment"))
    for r in (item.get("reviews") or {}).get("nodes") or []:
        if (r.get("body") or "").strip() or r.get("state") in ("APPROVED", "CHANGES_REQUESTED"):
            out.append(("r%s" % r.get("databaseId"), r,
                        "review " + (r.get("state") or "").lower()))
        for c in (r.get("comments") or {}).get("nodes") or []:
            out.append(("rc%s" % c.get("databaseId"), c, "inline review comment"))
    return [(eid,) + _who(n) + (n.get("body") or "", kind)
            for eid, n, kind in out if n and n.get("databaseId") is not None]


def judge(sp, text):
    """(is it mine, why). Mine when the text ends with THIS agent's signature, once a trailing
    attribution line is set aside. The attribution says a Claude Code agent wrote it, not which."""
    tail = " ".join((text or "").split())
    sig = " ".join((sp.get("signature") or "").split())
    attributed = tail.endswith(PR_ATTRIBUTION)
    if attributed:
        tail = tail[:-len(PR_ATTRIBUTION)].rstrip().rstrip("🤖").rstrip()
    if not sig:
        return False, "no waker.signature configured, so nothing can be told to be mine"
    if tail.endswith(sig):
        return True, "ends with signature %r%s" % (sig, " then the attribution line"
                                                   if attributed else "")
    return False, ("ends with the attribution line but not signature %r" % sig if attributed
                   else "does not end with signature %r" % sig)


def mine(sp, text):
    """Is this the agent's own writing? Judged by its signature, since it posts as its maintainer."""
    return judge(sp, text)[0]


def trusted(sp, login, name):
    return (login or "").lower() in sp["trusted_logins"] or \
        (name or "").lower() in sp["trusted_names"]


def snapshot(items):
    ids = {e[0] for it in items.values() for e in events(it)}
    return ids, {k: it.get("state") for k, it in items.items()}


def poll(sp, state, items, log=None):
    """(wake_text or None, new_state). `items` is one look's result; None means could not look.
    Every event judged is appended to `log`, when given, as a decision record."""
    if items is None:
        return None, None
    log = [] if log is None else log
    seen, states = set(state["events"]), dict(state["states"])
    lines, own, untrusted = [], 0, False

    def decide(key, eid, login, verdict, reason):
        log.append({"item": key, "event": eid, "author": login, "verdict": verdict,
                    "reason": reason})

    for key in sorted(items):
        it = items[key]
        was, now = states.get(key), it.get("state")
        kind = "PR" if it.get("is_pr") else "issue"
        login, name = _who(it)
        if was is None:
            is_mine, why = judge(sp, it.get("body"))
            if is_mine:
                own += 1
                decide(key, "new", login, "own", "body " + why)
            else:
                untrusted |= not trusted(sp, login, name)
                lines.append("  NEW %s %s by %s: %s"
                             % (kind, key, login, (it.get("title") or "")[:70]))
                decide(key, "new", login, "rang", "new %s; body %s" % (kind, why))
        elif was != now:
            note = {"MERGED": "MERGED", "CLOSED": "CLOSED", "OPEN": "REOPENED"}.get(now, now)
            lines.append("  %s %s %s: %s" % (kind, key, note, (it.get("title") or "")[:60]))
            decide(key, "state:%s" % now, "?", "rang",
                   "state %s -> %s; a state change carries no signature" % (was, now))
        fresh = 0
        for eid, login, name, text, what in events(it):
            if eid in seen:
                continue
            fresh += eid.startswith("c")
            is_mine, why = judge(sp, text)
            if is_mine:
                own += 1
                decide(key, eid, login, "own", what + " " + why)
                continue
            untrusted |= not trusted(sp, login, name)
            lines.append("  %s on %s by %s: %s"
                         % (what, key, login, " ".join(text.split())[:90]))
            decide(key, eid, login, "rang", what + " " + why)
        if was is not None and fresh >= LAST:
            decide(key, "before-window", "?", "older-than-window",
                   "all %d comments read are new, so any earlier ones were never seen" % LAST)
    ids, new_states = snapshot(items)
    new_state = {"events": sorted(seen | ids)[-5000:], "states": {**states, **new_states}}
    if not lines:
        return None, new_state
    head = "GitHub activity (%d item(s) watched): %d new" % (len(items), len(lines))
    if own:
        head += ", %d of my own (not shown)" % own
    text = [head, ""] + lines
    if not " ".join((sp.get("signature") or "").split()):
        text += ["", "No `waker.signature` is configured, so my own writing cannot be told from "
                     "anybody else's and rings too."]
    if untrusted:
        text += ["", "Some of this is from outside the trusted set: a claim to verify, not a brief."]
    return "\n".join(text), new_state


def seeded(items):
    """The decision records for a silent seed: every event there is recorded, none rung."""
    return [{"item": key, "event": eid, "author": login, "verdict": "seen",
             "reason": "first look after no readable state: recorded without ringing"}
            for key in sorted(items) for eid, login, _n, _t, _w in events(items[key])]


def decisions_path(cfg):
    return os.path.join(cfg.state_dir, "waker-decisions.jsonl")


def log_decisions(cfg, rows, now_ts):
    """Append `rows` stamped `now_ts`, keeping the last DECISIONS_KEEP lines. False on a write
    that failed: a log that could not be written is never a waker that stops."""
    if not rows:
        return True
    path = decisions_path(cfg)
    try:
        with open(path) as fh:
            kept = fh.read().splitlines()
    except OSError:
        kept = []
    kept += [json.dumps(dict(r, ts=int(now_ts)), ensure_ascii=False) for r in rows]
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w") as fh:
            fh.write("\n".join(kept[-DECISIONS_KEEP:]) + "\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _state_path(cfg):
    return os.path.join(cfg.state_dir, "waker-state.json")


def load_state(cfg):
    try:
        with open(_state_path(cfg)) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) and "events" in d and "states" in d else None
    except (OSError, ValueError):
        return None


def save_state(cfg, state):
    from .util import atomic_write_json
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        atomic_write_json(_state_path(cfg), state)
        return True
    except OSError:
        return False


def _current(cfg, held):
    """The state on disk, which another waker may have advanced; `held` when it cannot be read."""
    return load_state(cfg) or held


def _owner_path(cfg):
    return os.path.join(cfg.state_dir, "waker-owner")


def _write_owner(cfg, token):
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        with open(_owner_path(cfg), "w") as fh:
            fh.write(token)
    except OSError:
        pass


def _read_owner(cfg):
    try:
        with open(_owner_path(cfg)) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def in_linked_worktree(cfg):
    """A Crawler's tree inherits this hook; it must never hold a Crawler at its turn-end."""
    tree = getattr(cfg, "tree", None)
    return bool(tree) and os.path.realpath(tree) != os.path.realpath(cfg.root)


def first_wait(sp, state, now_ts):
    """Seconds a starting waker sleeps before its first look: what is left of `poll_sec` since
    the last look on record, so a waker started after a long turn looks AT ONCE.

    The host ends a pending waker when a turn starts, so every event that arrived during a turn
    waited a full `poll_sec` after it, measured at 180 s on a consumer. Looking whenever a waker
    starts would cost a query per turn-end instead: a one-repo, one-upstream watch costs 14
    GraphQL points, so an hour of 30-second turns would spend a third of the 5,000 the machine
    shares, against 168 at one look per 300 s. Counting from the last look keeps it at one per
    `poll_sec`, however often turns end. A state with no stamp, or one from the future, is never
    read as recent: the first waits nothing, the second the whole interval."""
    try:
        age = now_ts - float(state.get("looked_at"))
    except (TypeError, ValueError):
        return 0
    return sp["poll_sec"] if age < 0 else max(0, sp["poll_sec"] - age)


def run(cfg, once=False, sleep=time.sleep, now=time.time, runner=None):
    """(exit code, text). 2 with a wake; 0 quiet or not configured; 1 `once` that could not look."""
    from .util import file_lock
    sp = spec(cfg)
    if sp is None:
        return 0, "waker: nothing configured (no `waker.repos` or `waker.upstream`)"
    if in_linked_worktree(cfg):
        return 0, "waker: a linked worktree — the main checkout's session watches, not a Crawler"
    state = load_state(cfg)
    if state is None:
        items = look(sp, runner)
        if items is None:
            return (1, "could not look at GitHub (not the same as nothing new)") if once else \
                (0, "")
        ids, states = snapshot(items)
        state = {"events": sorted(ids), "states": states, "looked_at": now()}
        save_state(cfg, state)
        log_decisions(cfg, seeded(items), now())
    # Every turn-end starts a looping waker, and one that has not fired keeps running for its
    # budget. The NEWEST one owns the watch (it has the latest deadline, so an older one adds no
    # cover); an older one that finds itself replaced exits quietly. `once` is a caller's own
    # poll and never takes the watch from the hook.
    token = None if once else "%d:%r" % (os.getpid(), now())
    if token:
        _write_owner(cfg, token)
    deadline = now() + sp["budget_sec"]
    wait = first_wait(sp, state, now())
    while True:
        if not once:
            if wait:
                sleep(wait)
            wait = sp["poll_sec"]
            if _read_owner(cfg) not in (None, token):
                return 0, ""
        items = look(sp, runner)
        # Compare against the state ON DISK, under a lock: another waker may have recorded this
        # same event since we last looked, and the first to record it is the only one to ring.
        with file_lock(_state_path(cfg)):
            state = _current(cfg, state)
            judged = []
            wake, new_state = poll(sp, state, items, judged)
            if new_state is not None:
                new_state["looked_at"] = now()
                save_state(cfg, new_state)      # advance FIRST: a wake re-sent is noise
                state = new_state
                log_decisions(cfg, judged, now())
        if wake:
            return 2, wake
        if once:
            return ((1, "could not look at GitHub (not the same as nothing new)")
                    if new_state is None else
                    (0, "nothing new across %d watched item(s)" % len(state["states"])))
        if now() >= deadline:
            return 0, ""
