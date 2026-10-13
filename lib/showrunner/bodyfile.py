"""Remember the file a leaf's body came from, so `spawn` can see it drift (#102).

`add --body-file F` copies F's text into the graph and forgets F. A lead who kept appending
hand-offs to those draft files as reviews came back had five Crawlers spawned from the body as it
was at `add` time — two hand-offs silently never done. The file passed to `add` reads like the
brief's source of truth, and nothing said it was not.

So the path is recorded beside the campaign's state, and `spawn` compares the file NOW with the
body it is about to brief from. Only a file that still exists is compared: a moved or deleted
draft is nothing to compare against, not evidence of drift.
"""
import difflib
import hashlib
import json
import os


def _store(cfg):
    return os.path.join(cfg.state_dir, "body-files.json")


def _load(cfg):
    try:
        with open(_store(cfg)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(cfg, data):
    from .util import atomic_write_json
    os.makedirs(cfg.state_dir, exist_ok=True)
    atomic_write_json(_store(cfg), data)


def _sha(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def record(cfg, leaf_id, path, body):
    """Remember that `leaf_id`'s body came from `path`."""
    data = _load(cfg)
    data[leaf_id] = {"path": os.path.abspath(path), "body_sha": _sha(body)}
    _save(cfg, data)


def forget(cfg, leaf_id):
    """The body no longer comes from a file (set inline): stop comparing against one."""
    data = _load(cfg)
    if data.pop(leaf_id, None) is not None:
        _save(cfg, data)


def drift(cfg, leaf):
    """{"path", "summary"} when the recorded file now differs from the stored body, else None."""
    rec = _load(cfg).get(leaf["id"])
    if not rec or not os.path.isfile(rec.get("path") or ""):
        return None
    try:
        with open(rec["path"]) as fh:
            current = fh.read()
    except OSError:
        return None
    stored = leaf.get("body") or ""
    if _sha(current) == _sha(stored):
        return None
    added = removed = 0
    for line in difflib.unified_diff(stored.splitlines(), current.splitlines(), lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"path": rec["path"],
            "summary": "%d line(s) added, %d removed since it was stored" % (added, removed)}
