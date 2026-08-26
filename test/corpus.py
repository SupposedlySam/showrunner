#!/usr/bin/env python3
"""Measure showrunner's turn-end gates against a REAL transcript, using the SHIPPED hooks.

WHY THIS EXISTS, and it is not convenience. Every corpus number this project has published —
the promise gate's false-block rate on 227 turn-final closings, the pipeline gate's fire rate on
3,586 Bash commands — was produced by a throwaway script written for the occasion and then
deleted. Nobody could re-run one. The numbers were in commit messages and in llms.txt, and the
instrument that produced them existed for about four minutes.

That is the same defect as publishing a rate from a standalone classifier while shipping a
different predicate, which this project did and had to correct: a classifier written to STUDY a
defect and a gate written to BLOCK it are different programs, and only one of them is deployed.
An ad-hoc grep is a standalone classifier wearing another name.

So this tool has exactly one rule: IT NEVER IMPLEMENTS A PREDICATE. It extracts a population and
feeds it to the hook files themselves. If a hook changes, this number changes. If a hook is
broken, this reports a broken hook rather than a clean sweep — see `--self-check`.

WHAT IT CANNOT TELL YOU. Whether a fire was CORRECT. It reports the population, the rate, and
every matching item so a reader can judge; classifying them is a human reading the list. A rate
without its items is the summary-over-richer-data shape this repo keeps finding, so the items
are printed by default and suppressing them takes a flag.

    python3 test/corpus.py                          # this project's newest transcript
    python3 test/corpus.py --transcript PATH
    python3 test/corpus.py --gate promise           # just the promise gate
    python3 test/corpus.py --quiet                  # rates only, no items
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOKS = os.path.join(ROOT, ".showrunner", "hooks")

PROMISE_GATE = os.path.join(HOOKS, "future-tense-gate.sh")
PIPELINE_GATE = os.path.join(HOOKS, "pipeline-status-gate.sh")


def transcripts():
    """Transcript files for THIS project, newest first.

    Named by the same convention Claude Code uses: a directory per project, with the path
    slugified. Derived rather than configured, because a configured path is one more thing that
    can point at a transcript from a different repo and be believed.
    """
    slug = ROOT.replace("/", "-")
    pat = os.path.expanduser("~/.claude/projects/%s/*.jsonl" % slug)
    return sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)


def _records(path):
    with open(path, errors="ignore") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def closings(path):
    """Assistant messages that ENDED A TURN, in order.

    A turn-final message is one with no tool calls of its own AND nothing after it but the user.
    Getting this wrong is how a false-block rate ends up seven times too large: an earlier
    version of this extraction counted every assistant message, 1,650 of them, against 222 real
    turn-finals, and reported a 31% rate that was arithmetic on the wrong denominator.
    """
    recs = list(_records(path))

    def is_tool_result(r):
        c = (r.get("message") or {}).get("content")
        return isinstance(c, list) and any(b.get("type") == "tool_result" for b in c)

    out = []
    for i, r in enumerate(recs):
        m = r.get("message") or {}
        if r.get("type") != "assistant" or not isinstance(m.get("content"), list):
            continue
        if any(b.get("type") == "tool_use" for b in m["content"]):
            continue
        text = "".join(b.get("text", "") for b in m["content"] if b.get("type") == "text")
        if not text.strip():
            continue
        nxt = next((x for x in recs[i + 1:] if x.get("type") in ("user", "assistant")), None)
        if nxt is not None and (nxt.get("type") == "assistant" or is_tool_result(nxt)):
            continue
        out.append(text)
    return out


def bash_commands(path):
    out = []
    for r in _records(path):
        m = r.get("message") or {}
        if r.get("type") != "assistant" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if b.get("type") == "tool_use" and b.get("name") == "Bash":
                c = (b.get("input") or {}).get("command") or ""
                if c:
                    out.append(c)
    return out


def _env():
    """Never let a measurement write the checkout's own hook heartbeat.

    The heartbeat answers "did this hook run on the last TURN". A sweep invoking every hook a
    few thousand times would make all of them look freshly reached — which is exactly how the
    heartbeat's own first reading was corrupted, by the test suite.
    """
    d = tempfile.mkdtemp(prefix="sr-corpus-")
    return dict(os.environ,
                SHOWRUNNER_HEARTBEAT=os.path.join(d, "heartbeat.jsonl"),
                CLAUDE_PROJECT_DIR=ROOT)


def run_promise_gate(texts, env):
    """Feed each closing to the REAL Stop hook and collect what it refuses."""
    d = tempfile.mkdtemp(prefix="sr-corpus-t-")
    tp = os.path.join(d, "t.jsonl")
    fired = []
    for t in texts:
        with open(tp, "w") as fh:
            fh.write(json.dumps({"type": "assistant",
                                 "message": {"content": [{"type": "text", "text": t}]}}) + "\n")
        p = subprocess.run(["bash", PROMISE_GATE], input=json.dumps({"transcript_path": tp}),
                           capture_output=True, text=True, env=env)
        if p.returncode == 2:
            para = [x for x in t.strip().split("\n\n") if x.strip()]
            fired.append((para[-1] if para else "").replace("\n", " "))
    return fired


def run_pipeline_gate(cmds, env):
    fired = []
    for c in cmds:
        p = subprocess.run(["bash", PIPELINE_GATE], input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": c}}),
            capture_output=True, text=True, env=env)
        if p.stdout.strip():
            line = next((l.strip() for l in c.split("\n") if "|" in l and "$?" in l), c)
            fired.append(line)
    return fired


def self_check(env):
    """A gate that cannot run answers exactly like a gate with nothing to say.

    Without this, a syntax error anywhere in a hook produces a sweep reporting ZERO fires over
    thousands of items, and zero reads as good news. This repo shipped a hook that could not
    parse for a full day under 1,190 green assertions for precisely this reason.
    """
    problems = []
    for name, hook in (("promise", PROMISE_GATE), ("pipeline", PIPELINE_GATE)):
        if not os.path.isfile(hook):
            problems.append("%s gate is MISSING at %s" % (name, hook))
            continue
        p = subprocess.run(["bash", "-n", hook], capture_output=True, text=True)
        if p.returncode != 0:
            problems.append("%s gate does not PARSE: %s" % (name, (p.stderr or "").strip()[:160]))

    # POSITIVE CONTROLS. Parsing is not firing: a hook can parse and still match nothing after a
    # bad edit, and the sweep would report a clean corpus rather than a broken instrument.
    d = tempfile.mkdtemp(prefix="sr-corpus-c-")
    tp = os.path.join(d, "c.jsonl")
    with open(tp, "w") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Suite green. Next I'll pay those debts."}]}}) + "\n")
    p = subprocess.run(["bash", PROMISE_GATE], input=json.dumps({"transcript_path": tp}),
                       capture_output=True, text=True, env=env)
    if p.returncode != 2:
        problems.append("promise gate did not refuse a KNOWN promise (exit %d) — every zero it "
                        "reports below would be meaningless" % p.returncode)
    p = subprocess.run(["bash", PIPELINE_GATE], input=json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": 'cmd 2>&1 | head -3; echo "rc=$?"'}}),
        capture_output=True, text=True, env=env)
    if not p.stdout.strip():
        problems.append("pipeline gate did not fire on a KNOWN truncated status — every zero it "
                        "reports below would be meaningless")

    # THE REDIRECT IS CHECKED, NOT ASSERTED. This tool's docstring says it never writes the
    # checkout's own hook heartbeat. game_loop's auditor wrote the same sentence in the same
    # kind of tool, on a false mechanism — they redirected HOME while the path derived from
    # __file__ — and it stamped on its first run. A true conclusion resting on an unverified
    # mechanism, in the instrument built to stop unverified claims.
    #
    # BOTH HALVES OR NEITHER. "The real record did not grow" is also what "the gate silently
    # stopped stamping" looks like, so the redirected file must have GROWN. The promise gate is
    # a Stop hook and stamps on every invocation; the pipeline gate is PreToolUse and does not,
    # which is why only the first is evidence here.
    redirect = env.get("SHOWRUNNER_HEARTBEAT") or ""
    live = os.path.join(ROOT, ".showrunner", "hook-heartbeat.jsonl")
    live_before = os.path.getsize(live) if os.path.exists(live) else 0
    subprocess.run(["bash", PROMISE_GATE], input=json.dumps({"transcript_path": tp}),
                   capture_output=True, text=True, env=env)
    if os.path.exists(live) and os.path.getsize(live) != live_before:
        problems.append("running the promise gate GREW the checkout's own hook heartbeat (%s). "
                        "A sweep invoking it thousands of times would make every Stop hook look "
                        "freshly reached, which is the one thing that file answers." % live)
    if not (redirect and os.path.exists(redirect) and os.path.getsize(redirect) > 0):
        problems.append("the promise gate stamped NOTHING, not even the redirected heartbeat "
                        "(%s). An unwritten real record and a gate that stopped stamping are "
                        "the same observation, so the redirect is unproven." % (redirect or "-"))
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--transcript", help="a .jsonl transcript; default is this project's newest")
    ap.add_argument("--gate", choices=("promise", "pipeline", "both"), default="both")
    ap.add_argument("--quiet", action="store_true", help="rates only; do not list the items")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    args = ap.parse_args()

    env = _env()
    problems = self_check(env)
    if problems:
        # EXIT 3, not 1. A broken instrument is not a finding about the corpus, and a caller
        # treating non-zero as "there were fires" would read it exactly backwards.
        for pr in problems:
            sys.stderr.write("INSTRUMENT: %s\n" % pr)
        sys.stderr.write("\nRefusing to report a rate from a gate that cannot answer. "
                         "A sweep with a dead gate returns zero, and zero reads as good news.\n")
        return 3

    path = args.transcript
    if not path:
        found = transcripts()
        if not found:
            sys.stderr.write("no transcript found for %s — pass --transcript\n" % ROOT)
            return 2
        path = found[0]
    if not os.path.isfile(path):
        sys.stderr.write("no such transcript: %s\n" % path)
        return 2

    # EVERY READING IS A SNAPSHOT, AND MUST SAY SO. This project's corpus is its own transcript,
    # which GROWS WHILE THE WORK HAPPENS — so a rate quoted without a date is a number whose
    # denominator has moved since. game_loop's auditor measured their own headline going from
    # 73 closings to 81 with the numerator unchanged: they had been quoting 0-of-4 as a constant
    # for two days, and nothing in how they stated it would have changed had a fifth appeared.
    #
    # WORSE, AND UNFIXABLE BY ANY EXEMPTION: writing about the gate ADDS TO THE CORPUS THE GATE
    # IS MEASURED ON. One of their four blocks is a message about running the measurement. Mine
    # has the same property — this comment will be in a future reading. The self-reference
    # cannot be widened or exempted away, so the only honest handling is to date the reading.
    stat = os.stat(path)
    asof = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
    report = {"transcript": path, "as_of": asof, "transcript_bytes": stat.st_size}
    if args.gate in ("promise", "both"):
        texts = closings(path)
        fired = run_promise_gate(texts, env)
        report["promise"] = {"population": len(texts), "fired": len(fired), "items": fired}
    if args.gate in ("pipeline", "both"):
        cmds = bash_commands(path)
        fired = run_pipeline_gate(cmds, env)
        report["pipeline"] = {"population": len(cmds), "fired": len(fired), "items": fired}

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("transcript: %s" % path)
    print("AS OF %s (%d bytes) — this corpus GROWS; every rate below is a snapshot, and the "
          "denominator moves whenever work happens." % (asof, stat.st_size))
    print("gates measured AS SHIPPED — this tool implements no predicate of its own")
    for key, unit in (("promise", "turn-final closings"), ("pipeline", "Bash commands")):
        if key not in report:
            continue
        r = report[key]
        pct = (100.0 * r["fired"] / r["population"]) if r["population"] else 0.0
        print("\n%s gate: %d of %d %s (%.1f%%)"
              % (key, r["fired"], r["population"], unit, pct))
        if r["population"] == 0:
            print("   POPULATION IS EMPTY — this is a fact about the extraction, not about the "
                  "gate. A 0.0%% rate over nothing is not a clean result.")
        elif not args.quiet:
            for item in r["items"]:
                print("   %s" % item[:150])
    if not args.quiet:
        print("\nWhether any of these is CORRECT is not measured here. The items are printed so "
              "a reader can judge; a rate without them is a summary over data that knows more.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
