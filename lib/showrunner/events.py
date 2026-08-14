"""The append-only record of what this orchestrator DID, for anything watching it live.

showrunner could already answer "what is true now" — `status`, `reconcile`, `waiting`, `plan` all
report current state, several with `--json`. Nothing answered **"what just happened"**. A viewer
had two bad options: poll a snapshot verb and diff it (which invents transitions it did not see,
and misses any pair that cancels out between polls), or read `.showrunner/` directly (which makes
the viewer depend on file layouts that are showrunner's private business, exactly the coupling
`harness.py` deleted a hardcoded rule list to avoid).

So: one line per transition, appended, never rewritten. A watcher replays the file to learn the
past and follows it to learn the present, and it is the same bytes either way.

WHAT THIS IS NOT. It is **derived**, never authoritative. `graph.db` and `campaign.json` remain
the truth; this is an observation of changes made to them. That ordering matters under failure —
a lost event costs a viewer one frame, while a lost claim costs a leaf. So `emit` must never turn
a write failure here into a failure of the work, and it does not.

BUT A JOURNAL THAT FAILS QUIETLY IS THE THING THIS PROJECT KEEPS FIXING. "No events" and "the
journal has not been writable since Tuesday" look identical to a viewer showing an idle campaign.
So a failure to append is counted, reported once per process on stderr, and surfaced by `doctor` —
the write is best-effort, the SILENCE is not.

ORDERING AND IDENTITY. Each line carries a `seq` taken from the JOURNAL — not from the process,
which was the first version and was useless, since every CLI invocation is its own process and
almost every event came out as `seq: 1`. With an `ts` beside it a viewer can order frames without
trusting clock resolution, and an `instance` names which showrunner wrote it. Several orchestrators share one repo by design (that is what `claim --next` is for), so
"which showrunner said this" is a question a multi-instance viewer has to be able to ask. The
append itself is one `write` of one line opened in append mode, which POSIX keeps atomic below
PIPE_BUF; lines are kept small for that reason and payloads are summaries, not documents.
"""

import json
import os

from .util import file_lock, now

JOURNAL = "events.jsonl"

# Reported once per process rather than per failure: a viewer losing frames is one condition, and
# a hundred identical lines on an orchestrator's stderr is how a real signal gets scrolled past.
_state = {"failed": 0, "warned": False}


def path_for(cfg):
    return os.path.join(cfg.state_dir, JOURNAL)


def instance_id(cfg):
    """Which showrunner this is. Stable per repo, cheap, and NOT a claim about uniqueness.

    Several orchestrators can drive one repo, and one machine can hold several repos. A viewer
    watching more than one needs to tell frames apart, and the repo root is the only durable
    identity this tool already has that survives a restart. The PID is appended by `emit` where the
    process matters; keeping them separate means a viewer can group by repo without parsing.
    """
    return os.path.realpath(cfg.root)


RESERVED = ("ts", "seq", "kind", "instance", "pid")


def _next_seq(cfg):
    """The journal's own line count, not this process's.

    A per-PROCESS counter was the first version and it was useless: every CLI invocation is its
    own process, so almost every event in a real campaign was `seq: 1`. A viewer cannot order
    frames by it and cannot resume from it — and both look fine until two events exist.

    So the file is its own counter: the last line's seq plus one, read under the same lock the
    append takes, so two orchestrators sharing a repo cannot mint the same number. Read from the
    END rather than by counting lines, because a campaign's journal only grows.
    """
    p = path_for(cfg)
    try:
        size = os.path.getsize(p)
    except OSError:
        return 1
    try:
        with open(p, "rb") as fh:
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", "ignore").strip().splitlines()
        for raw in reversed(tail):
            try:
                return int(json.loads(raw).get("seq", 0)) + 1
            except (ValueError, TypeError, AttributeError):
                continue
    except OSError:
        pass
    return 1


def cursor(cfg, seq):
    """A resume position that NAMES THE INSTANCE IT CAME FROM.

    `seq` counts within one journal. The whole point of this UI work is that several showrunners
    run in several places and one viewer watches them all — so the moment a bare integer crosses
    an instance boundary it becomes a confident answer about a different campaign. Nothing in the
    shape catches it: both sides are integers, the comparison succeeds, and the viewer resumes
    from a position that exists in some other repo. There is no symptom.

    So the cursor carries its boundary and `parse_cursor` refuses one from elsewhere. A viewer
    merging two streams cannot accidentally order them against each other, because the value it
    holds is not an integer.

    A bare `--since 5` is still accepted, exactly as it is still meaningful: typed by a human
    against one repo, it is a local question with a local answer.
    """
    return "%s@%d" % (_instance_tag(cfg), seq)


def _instance_tag(cfg):
    import hashlib
    return hashlib.sha256(instance_id(cfg).encode()).hexdigest()[:12]


def parse_cursor(cfg, raw):
    """(seq, error). Refuses a cursor minted by a different instance rather than resuming wrong."""
    if raw is None:
        return None, None
    text = str(raw)
    if "@" not in text:
        try:
            return int(text), None
        except ValueError:
            return None, "not a sequence number or a cursor: %r" % text
    tag, _, num = text.partition("@")
    if tag != _instance_tag(cfg):
        return None, ("that cursor was minted by a different showrunner (%s), and this one is "
                      "%s. Sequence numbers count within ONE journal, so resuming from another "
                      "instance's position would silently replay the wrong campaign — the two "
                      "are both integers and nothing downstream could tell."
                      % (tag, _instance_tag(cfg)))
    try:
        return int(num), None
    except ValueError:
        return None, "cursor %r has no readable sequence" % text


def _ends_with_newline(path):
    """Did the last write finish? Cheap: one byte from the end."""
    try:
        size = os.path.getsize(path)
        if not size:
            return True
        with open(path, "rb") as fh:
            fh.seek(size - 1)
            return fh.read(1) == b"\n"
    except OSError:
        return True


def emit(cfg, kind, fields=None):
    """Append one event. Returns True when it landed.

    FIELDS IS A DICT, NOT **kwargs, and that is a correction. The first version took keyword
    arguments, which read better at every call site right up until a caller wanted to record a
    leaf's `kind` — colliding with the positional parameter of the same name and raising
    TypeError from inside `showrunner add`. Two things were wrong at once: an event vocabulary
    that cannot express a field because the function borrowed its name, and a "never raises"
    promise that was only true INSIDE the try block. An explicit dict makes both impossible
    rather than unlikely.
    """
    line = {"ts": now(), "kind": kind, "instance": instance_id(cfg), "pid": os.getpid()}
    for key, value in (fields or {}).items():
        # A caller cannot overwrite the frame's own identity. Silently letting it would produce
        # an event claiming a timestamp or a sequence it did not have, which is worse than the
        # field being missing — a viewer has no way to doubt it.
        line["field_" + key if key in RESERVED else key] = value
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        # One lock covers "what number is next" and "append it", because two orchestrators
        # sharing a repo is a supported shape and separately-correct halves would still mint
        # duplicate sequence numbers.
        with file_lock(path_for(cfg)):
            line["seq"] = _next_seq(cfg)
            with open(path_for(cfg), "a") as fh:
                # START ON A FRESH LINE. A torn final write — a crash mid-append, a full disk —
                # leaves a line with no newline, and appending straight onto it fuses the two
                # into one unparseable record. So a torn write cost TWO events: the one that was
                # interrupted and the next good one, which is the more expensive half and the
                # one nobody would look for. Found by a test that deliberately tore the file and
                # then could not explain why a later, perfectly good event never arrived.
                if fh.tell() and not _ends_with_newline(path_for(cfg)):
                    fh.write("\n")
                fh.write(json.dumps(line, sort_keys=True, default=str) + "\n")
        return True
    except (OSError, ValueError, TypeError) as exc:
        _state["failed"] += 1
        if not _state["warned"]:
            _state["warned"] = True
            from .util import eprint
            eprint("showrunner: the event journal is not writable (%s) — anything watching this "
                   "campaign live will show it as idle rather than as unobserved. Work continues; "
                   "`showrunner doctor` reports this." % exc)
        return False


def dropped():
    """How many events this process failed to write. `doctor` asks; nothing else should."""
    return _state["failed"]


def read(cfg, since_seq=None, limit=None):
    """Replay the journal. Returns (events, unparseable, unreadable).

    Malformed lines are COUNTED, not skipped in silence: a half-written final line is ordinary —
    a viewer may attach while an append is in flight — and dropping it quietly would make a torn
    write and an empty campaign look the same.

    **A FAILED READ IS NOT A FACT ABOUT THE CAMPAIGN**, and this returned one as if it were. The
    first version caught `OSError` and returned the events it had so far, so a journal that could
    not be opened — a permission change, a full disk, a path that became a directory — came back
    as `([], 0)`: an empty list and no complaint, indistinguishable from an orchestrator that has
    genuinely done nothing. A viewer would have rendered a confident, quiet, wrong "idle".

    So `unreadable` is its own third value rather than an absence folded into the first. Callers
    must not treat it as zero events; `watch` refuses on it rather than printing a clean replay.
    """
    out, bad = [], 0
    p = path_for(cfg)
    if not os.path.exists(p):
        # A journal that has never been written is genuinely empty — that IS a fact about the
        # campaign, and it is a different one from a journal that exists and cannot be opened.
        return out, bad, False
    try:
        with open(p, errors="ignore") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    bad += 1
                    continue
                if since_seq is not None and ev.get("seq", 0) <= since_seq:
                    continue
                out.append(ev)
    except OSError:
        return out, bad, True
    if limit:
        out = out[-limit:]
    return out, bad, False
