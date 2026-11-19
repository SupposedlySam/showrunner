"""A fake GitHub GraphQL endpoint for the waker's two documents, served from one full answer.

The waker sends a probe (number, state, updatedAt per item) and then a fetch of the items that
moved, one alias per item named for its probe alias and number (`r0_12`, `u0_5`). A test writes
the world once, in the shape the whole look used to return:

    {"data": {"r0": {"issues": {"nodes": [...]}, "pullRequests": {"nodes": [...]}},
              "u0": {"nodes": [...]}}}

and this answers either document from it: the probe gets each node cut to its probe fields, the
fetch gets the full node for each alias, or null for an item the world does not hold. An answer
that is empty, not JSON, or carries `errors` is returned as it is, to either document.

Run as a script it is a `gh` stub: `gh api graphql -f query=<q>` answered from the world in the
file named by $FAKE_GH_WORLD.
"""
import json
import os
import re
import sys

PROBE_FIELDS = ("number", "state", "updatedAt")
_FETCH = re.compile(r'(\w+)_(\d+): repository\(owner: "[^"]*", name: "[^"]*"\) \{\s*'
                    r'issueOrPullRequest\(number: (\d+)\)')


def _nodes(src):
    if not isinstance(src, dict):
        return []
    if "nodes" in src:
        return src.get("nodes") or []
    return ((src.get("issues") or {}).get("nodes") or []) + \
        ((src.get("pullRequests") or {}).get("nodes") or [])


def answer(world, q):
    """The answer `world` (a JSON string, or None) gives to the GraphQL document `q`."""
    if not world or not world.strip():
        return world
    try:
        body = json.loads(world)
    except ValueError:
        return world
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or body.get("errors"):
        return world
    if "issueOrPullRequest" in q:
        out = {}
        for alias, number, _n in _FETCH.findall(q):
            node = next((n for n in _nodes(data.get(alias)) if n and n.get("number") == int(number)),
                        None)
            out["%s_%s" % (alias, number)] = {"issueOrPullRequest": node}
        return json.dumps({"data": out})

    def cut(nodes):
        return [{k: n[k] for k in PROBE_FIELDS if k in n} if n else n for n in nodes or []]
    out = {}
    for alias, src in data.items():
        if isinstance(src, dict) and "nodes" in src:
            out[alias] = {"nodes": cut(src["nodes"])}
        elif isinstance(src, dict):
            out[alias] = {k: {"nodes": cut((src.get(k) or {}).get("nodes"))}
                          for k in ("issues", "pullRequests") if k in src}
        else:
            out[alias] = src
    return json.dumps({"data": out})


def served(world_for):
    """A waker runner from `world_for(q)`, a test's runner that returns the whole world. It is
    called ONCE per look, at the probe, so a test counting its calls counts looks; the fetch
    that follows is answered from the world that probe saw."""
    last = {}

    def runner(q):
        if "issueOrPullRequest" in q and "world" in last:
            return answer(last["world"], q)
        last["world"] = world_for(q)
        return answer(last["world"], q)
    return runner


if __name__ == "__main__":
    q = next((a[len("query="):] for a in sys.argv[1:] if a.startswith("query=")), "")
    try:
        with open(os.environ["FAKE_GH_WORLD"]) as fh:
            world = fh.read()
    except (KeyError, OSError):
        sys.exit(1)
    sys.stdout.write(answer(world, q) or "")
