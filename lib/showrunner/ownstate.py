"""Was a close, merge or reopen the agent's OWN? Judged from evidence, never from the author.

A state change carries no text to sign, and GitHub's `mergedBy`/`closedBy` is the maintainer's
login for every agent sharing that account, so neither tells whose it was. MEASURED on a consumer:
of 32 rings in an afternoon, 10 were MERGED on PRs that session had just merged itself, and each
ring is a turn that re-reads the whole context.

TWO KINDS OF EVIDENCE, tried in order; with neither the change rings, as it always did:

1. THE SESSION'S OWN TRANSCRIPT (the path the hook's stdin carries). The change is own when the
   transcript holds a tool call that made it, dated at or before the change and no more than
   `waker.own_state_window_sec` before it, whose result was not an error. The calls read:
   a Bash `gh pr merge|close|reopen N` or `gh issue close|reopen N` (the repo from `--repo`/`-R`,
   a URL target, or the remote of the directory it ran in, `cd` included); a Bash `gh api` call
   with a mutating method on `repos/o/n/pulls/N/merge`, or setting `state=closed|open` on the
   item; and an MCP `mergePullRequest` or `closePullRequest` naming owner, repo and number. Those
   two are the github MCP's only tools that change a state: its `updatePullRequest` takes no
   state, and it has no tool that closes an issue. The transcript is parsed, never executed. A
   GraphQL mutation names a node id, not a number, and is not read.
2. FALLBACK, when the transcript cannot be read or holds no matching call: the item's NEWEST
   comment or review is own-signed (`waker.judge`) and was posted within the window of the
   change, either side. An agent that merges and says so in one breath is caught; one whose
   signed comment came ten minutes earlier is not, because somebody else may have merged since.

AN ISSUE GITHUB CLOSED for a "Fixes #N" is a third case: no call closed it, so neither rule above
can match it. Its newest closing event names the closer, and the close is own when that closer
landed by the agent's own hand, judged at the close's own time:

3. A CLOSER PR, or a PR that carried the closer commit, merged within the window of the close by
   a merge the rules above call own (`by_closer`).
4. A CLOSER COMMIT that is the new tip of a `git push` in the transcript to that repo, at or up to
   the window before the close (`push_refs`). Only the tip: whether a commit lies inside a pushed
   range takes the repository's history, which the transcript does not hold, and a push run with
   `-q` prints no range at all. Those closes ring.

A closer by anybody else, or none (closed by hand), rings, and the decision log names the closer.

The window bounds the transcript rule from below too: a close the agent made last week is no
evidence for a close somebody made today after a reopen. A reopen has no timestamp of its own in
the query, so `updatedAt` stands in for it: it is at or after the reopen, and a later comment
moves it further, which only makes a match less likely, never a false one more likely.
"""
import os
import re
import shlex

WINDOW_SEC = 120
KIND = {"MERGED": "merge", "CLOSED": "close", "OPEN": "reopen"}
MCP_KIND = {"mergePullRequest": "merge", "closePullRequest": "close"}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_URL = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/(?:pull|issues)/(\d+)")
_API = re.compile(r"^/?repos/([^/]+)/([^/]+)/(pulls|issues)/(\d+)(/merge)?/?$")
_REMOTE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def _epoch(s):
    from .lostring import _iso_epoch
    return _iso_epoch(s) if s else None


def change_time(item, now):
    """(epoch or None, which field said so) for the change to `now`."""
    field = {"MERGED": "mergedAt", "CLOSED": "closedAt", "OPEN": "updatedAt"}.get(now)
    return (_epoch(item.get(field)), field) if field else (None, None)


def _git_config(path):
    """The text of the git config for the checkout holding `path`, or None. Read, never run."""
    path = os.path.abspath(path)
    while True:
        dot = os.path.join(path, ".git")
        if os.path.isdir(dot):
            gitdir = dot
            break
        if os.path.isfile(dot):
            try:
                with open(dot) as fh:
                    gitdir = fh.read().split("gitdir:", 1)[1].strip()
            except (OSError, IndexError):
                return None
            gitdir = os.path.join(path, gitdir)
            try:
                with open(os.path.join(gitdir, "commondir")) as fh:
                    gitdir = os.path.join(gitdir, fh.read().strip())
            except OSError:
                pass
            break
        up = os.path.dirname(path)
        if up == path:
            return None
        path = up
    try:
        with open(os.path.join(gitdir, "config")) as fh:
            return fh.read()
    except OSError:
        return None


def remote_repo(path, _cache={}):
    """"owner/name" of the GitHub remote of the checkout at `path`: origin, else the only one."""
    if not path:
        return None
    if path not in _cache:
        text, urls = _git_config(path) or "", {}
        remote = None
        for line in text.splitlines():
            m = re.match(r'\s*\[remote "([^"]+)"\]', line)
            if m:
                remote = m.group(1)
            elif line.strip().startswith("["):
                remote = None
            elif remote and re.match(r"\s*url\s*=", line):
                urls[remote] = line.split("=", 1)[1].strip()
        url = urls.get("origin") or (next(iter(urls.values())) if len(urls) == 1 else None)
        m = _REMOTE.search(url or "")
        _cache[path] = "%s/%s" % (m.group(1), m.group(2)) if m else None
    return _cache[path]


def _segments(command):
    """The command split into simple commands, as token lists; None when it does not parse."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        lex.commenters = "#"
        toks = list(lex)
    except ValueError:
        return None
    out, cur = [], []
    for t in toks:
        if t and set(t) <= set(";&|"):
            out.append(cur)
            cur = []
        else:
            cur.append(t)
    return out + [cur]


def _opt(args, *names):
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                return args[i + 1]
            if n.startswith("--") and a.startswith(n + "="):
                return a[len(n) + 1:]
    return None


def bash_calls(command, cwd):
    """[(kind, repo or None, number, what)] for the state-changing gh calls in `command`."""
    out = []
    for seg in _segments(command.replace("\n", ";")) or []:
        if not seg:
            continue
        if seg[0] == "cd" and len(seg) > 1:
            cwd = os.path.join(cwd or "/", os.path.expanduser(seg[1]))
            continue
        while seg and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0]):
            seg = seg[1:]
        if not seg or os.path.basename(seg[0]) != "gh" or len(seg) < 2:
            continue
        here = remote_repo(cwd)
        if seg[1] in ("pr", "issue") and len(seg) >= 4:
            verb = seg[2]
            kind = {"merge": "merge", "close": "close", "reopen": "reopen"}.get(verb)
            if kind is None or (verb == "merge" and seg[1] != "pr"):
                continue
            args = seg[3:]
            repo = _opt(args, "--repo", "-R")
            target = next((a for a in args if not a.startswith("-")
                           and a != repo), None)
            m = _URL.search(target or "")
            if m:
                repo, num = "%s/%s" % (m.group(1), m.group(2)), int(m.group(3))
            elif target and target.lstrip("#").isdigit():
                num = int(target.lstrip("#"))
            else:
                continue
            out.append((kind, repo or here, num, "gh %s %s %d" % (seg[1], verb, num)))
        elif seg[1] == "api":
            args = seg[2:]
            vals = {_opt(args, "-X", "--method"), _opt(args, "-H", "--header"),
                    _opt(args, "--jq", "-q"), _opt(args, "--input")}
            path = next((a for a in args if not a.startswith("-") and a not in vals
                         and "=" not in a), None)
            m = _API.match(path or "")
            if not m:
                continue
            fields = [args[i + 1] for i, a in enumerate(args[:-1])
                      if a in ("-f", "-F", "--field", "--raw-field")]
            method = (_opt(args, "-X", "--method") or ("POST" if fields else "GET")).upper()
            if method not in MUTATING:
                continue
            state = next((f.split("=", 1)[1] for f in fields if f.startswith("state=")), None)
            kind = "merge" if m.group(5) else {"closed": "close", "open": "reopen"}.get(state)
            if kind is None:
                continue
            o, n = m.group(1), m.group(2)
            repo = here if "{" in o + n else "%s/%s" % (o, n)
            out.append((kind, repo, int(m.group(4)), "gh api -X %s %s" % (method, path)))
    return out


def mcp_call(name, inp):
    """(kind, repo, number, what) for an MCP tool call that changes a state, or None."""
    kind = MCP_KIND.get(name.rsplit("__", 1)[-1])
    if kind is None or not isinstance(inp, dict):
        return None
    num = inp.get("pull_number")
    if not (inp.get("owner") and inp.get("repo")) or not str(num).isdigit():
        return None     # an omitted owner/repo defaults to the server's env, which we cannot see
    return kind, "%s/%s" % (inp["owner"], inp["repo"]), int(num), "%s #%s" % (name, num)


_PUSH_TO = re.compile(r"^To\s+(\S+)\s*$", re.M)
_PUSH_REF = re.compile(r"^\s*\+?\s*([0-9a-f]{7,40})\.\.\.?([0-9a-f]{7,40})\s+\S+\s+->\s+\S+", re.M)


def is_push(command):
    """Does `command` run a `git push`? Parsed, never run."""
    return any(seg and os.path.basename(seg[0]) == "git" and "push" in seg[1:]
               for seg in _segments(command.replace("\n", ";")) or [])


def push_refs(text):
    """[(repo, old, new)] for every ref update a `git push` printed, under its `To` line."""
    out, repo, at = [], None, 0
    tos = [(m.start(), m.group(1)) for m in _PUSH_TO.finditer(text or "")]
    for m in _PUSH_REF.finditer(text or ""):
        while at < len(tos) and tos[at][0] < m.start():
            repo = tos[at][1]
            at += 1
        r = _REMOTE.search(repo or "")
        if r:
            out.append(("%s/%s" % (r.group(1), r.group(2)), m.group(1), m.group(2)))
    return out


def _text(content):
    if isinstance(content, str):
        return content
    return "\n".join(str(c.get("text") or "") for c in content or [] if isinstance(c, dict))


def calls(transcript):
    """Every state-changing tool call in the transcript whose result was not an error, as dicts
    {kind, repo, number, at, what}. Raises OSError when it cannot be read: could not look is
    never "no calls"."""
    from .lostring import _entries
    pending, pushes = {}, {}
    for at, rec in _entries(transcript, (b'"tool_use"', b'"tool_result"')):
        msg = rec.get("message") if isinstance(rec, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                name, inp = c.get("name") or "", c.get("input") or {}
                if name == "Bash":
                    found = bash_calls(str(inp.get("command") or ""), rec.get("cwd"))
                    if is_push(str(inp.get("command") or "")):
                        pushes[c.get("id")] = at
                else:
                    one = mcp_call(name, inp)
                    found = [one] if one else []
                if found:
                    pending[c.get("id")] = [dict(kind=k, repo=r, number=n, at=at, what=w,
                                                 ok=None) for k, r, n, w in found]
            elif c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                for d in pending[c["tool_use_id"]]:
                    d["ok"] = not c.get("is_error")
            if c.get("type") == "tool_result" and c.get("tool_use_id") in pushes \
                    and not c.get("is_error"):
                t = pushes.pop(c["tool_use_id"])
                for repo, old, new in push_refs(_text(c.get("content"))):
                    pending.setdefault(c["tool_use_id"], []).append(dict(
                        kind="push", repo=repo, number=None, tip=new, at=t, ok=True,
                        what="git push %s..%s" % (old, new)))
    return [d for ds in pending.values() for d in ds if d["ok"]]


def made_it(call, key, now):
    """Is `call` one that would make THIS change: the same kind, item number and repo?"""
    repo, _, num = key.rpartition("#")
    return call["kind"] == KIND.get(now) and str(call["number"]) == num and \
        (call["repo"] or "").lower() == repo.lower()


def before_change(call_at, at, window):
    """At or before the change, and no more than `window` before it."""
    return at - window <= call_at <= at


def near_change(t, at, window):
    """Within `window` of the change, either side."""
    return abs(t - at) <= window


def by_transcript(found, key, now, at, window):
    """(matched, why) for the transcript rule."""
    want = KIND.get(now)
    for d in sorted(found, key=lambda d: -d["at"]):
        if made_it(d, key, now) and before_change(d["at"], at, window):
            return True, "transcript: %s at %s, %ds before the change" % (
                d["what"], _stamp(d["at"]), round(at - d["at"]))
    return False, "no %s call on %s in the transcript at or up to %ds before the change" % (
        want, key, window)


def _stamp(t):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def newest_writing(item):
    """(epoch, text) of the item's newest comment, review or inline comment, or None."""
    nodes = list((item.get("comments") or {}).get("nodes") or [])
    for r in (item.get("reviews") or {}).get("nodes") or []:
        nodes.append(r)
        nodes += (r.get("comments") or {}).get("nodes") or []
    dated = [(_epoch(n.get("createdAt")), n.get("body") or "") for n in nodes if n]
    dated = [d for d in dated if d[0] is not None]
    return max(dated, key=lambda d: d[0]) if dated else None


def by_signature(sp, item, at, window):
    """(matched, why) for the fallback rule."""
    from .waker import judge
    newest = newest_writing(item)
    if newest is None:
        return False, "no dated comment or review on the item"
    t, text = newest
    mine, why = judge(sp, text)
    if not mine:
        return False, "newest comment or review %s" % why
    gap = round(t - at)
    if not near_change(t, at, window):
        return False, "newest comment or review is own-signed but %ds %s the change, outside " \
                      "the %ds window" % (abs(gap), "after" if gap > 0 else "before", window)
    return True, "own-signed comment or review %ds %s the change, within %ds" % (
        abs(gap), "after" if gap > 0 else "before", window)


def closed_event(item):
    """The issue's newest ClosedEvent, or None."""
    nodes = [n for n in ((item.get("timelineItems") or {}).get("nodes") or []) if n]
    return nodes[-1] if nodes else None


def pushed(call, repo, oid):
    """Is `call` a push to `repo` whose new tip is the commit `oid`?"""
    tip = call.get("tip") or ""
    return call["kind"] == "push" and len(tip) >= 7 and (oid or "").startswith(tip) and \
        (call["repo"] or "").lower() == (repo or "").lower()


def own_landing(sp, pr, at, window, found):
    """(matched, why) for one PR that closed the issue at `at`: merged beside it, by my merge."""
    key = "%s#%s" % (((pr.get("repository") or {}).get("nameWithOwner") or "?"), pr.get("number"))
    merged = _epoch(pr.get("mergedAt"))
    if merged is None:
        return False, "PR %s has no mergedAt" % key
    if not near_change(merged, at, window):
        return False, "PR %s merged %ds from the close, outside %ds" % (
            key, abs(round(merged - at)), window)
    hit, why = judge(sp, key, pr, "MERGED", found)
    return hit, "PR %s %s: %s" % (key, "is my own merge" if hit else "is not my own merge", why)


def by_closer(sp, item, at, window, found):
    """(matched, why) for a close GitHub made because a PR or commit said it fixes the issue."""
    ev = closed_event(item)
    t = _epoch((ev or {}).get("createdAt"))
    if t is None or not near_change(t, at, window):
        return False, "no closing event at the close"
    closer = ev.get("closer") or {}
    typ = closer.get("__typename")
    if typ == "PullRequest":
        hit, why = own_landing(sp, closer, at, window, found)
        return hit, "closer: " + why
    if typ != "Commit":
        return False, "closer: none (closed by hand, or by %s)" % (typ or "nobody named")
    oid, repo = closer.get("oid") or "", (closer.get("repository") or {}).get("nameWithOwner")
    whys = []
    for pr in (closer.get("associatedPullRequests") or {}).get("nodes") or []:
        hit, why = own_landing(sp, pr or {}, at, window, found)
        if hit:
            return True, "closer: commit %s, landed by %s" % (oid[:7], why)
        whys.append(why)
    if isinstance(found, list):
        for d in sorted(found, key=lambda d: -d["at"]):
            if pushed(d, repo, oid) and before_change(d["at"], at, window):
                return True, "closer: commit %s, the tip of my %s at %s, %ds before the close" % (
                    oid[:7], d["what"], _stamp(d["at"]), round(at - d["at"]))
        whys.append("no push in the transcript with %s as its tip, at or up to %ds before the "
                    "close" % (oid[:7], window))
    else:
        whys.append(found)
    return False, "closer: commit %s on %s; %s" % (oid[:7], repo, "; ".join(whys))


def judge(sp, key, item, now, found):
    """(is it mine, why) for a state change of `item` to `now`. `found` is `calls(transcript)`,
    or an explanation string when the transcript could not be read or there was none."""
    window = sp.get("own_state_window_sec", WINDOW_SEC)
    at, field = change_time(item, now)
    if at is None:
        return False, "no %s on the item, so the change has no time to compare" % field
    if isinstance(found, list):
        hit, why_tx = by_transcript(found, key, now, at, window)
        if hit:
            return True, why_tx + " (%s)" % field
    else:
        why_tx = found
    hit, why_sig = by_signature(sp, item, at, window)
    if hit:
        return True, "fallback: %s; %s" % (why_sig, why_tx)
    if now == "CLOSED" and not item.get("is_pr"):
        hit, why_cl = by_closer(sp, item, at, window, found)
        if hit:
            return True, why_cl
        return False, "%s; %s; %s" % (why_cl, why_tx, why_sig)
    return False, "%s; %s" % (why_tx, why_sig)


def load(transcript):
    """`calls(transcript)`, or the reason there are none to read, as a string."""
    if not transcript:
        return "no transcript path"
    try:
        return calls(transcript)
    except OSError as e:
        return "transcript could not be read (%s)" % e.__class__.__name__
