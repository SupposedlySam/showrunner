#!/usr/bin/env python3
"""Which of showrunner's own surfaces do the front-door docs never NAME?

THE UNGAMEABLE HALF, AND ONLY THAT HALF. Every verb the CLI accepts, every environment variable
the code reads, and every hook this project registers, checked against README.md and llms.txt.
Prose cannot satisfy it: the name appears or it does not.

WHAT IT CANNOT CHECK, and this is the larger half — said here rather than left to be assumed:

  * whether the prose is CORRECT
  * whether it still describes what the code does
  * whether it EXPLAINS the surface or merely mentions it
  * whether the file reads coherently front to back for somebody arriving new

A mention is the floor, not the goal. Documentation is searched far more often than it is read,
so the failures that survive are exactly the ones a search cannot surface: a remedy that now
points at a trap, a count that says BOTH when it became four, a section whose ordering stopped
making sense three features ago. Those need a human or an agent to READ THE FILE WHOLE, which is
why this tool refuses to be the only gate — see docs/READINGS.md, which records when each file was
last read end to end and at which commit.

    python3 test/docs_surface.py            # report; exit 1 if anything is unnamed
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ("README.md", "llms.txt")
READINGS = os.path.join("docs", "READINGS.md")

# Surfaces that are deliberately not front-door material, each with the reason. An exclusion is a
# stated decision that stays visible here and is counted in the report — never a silent skip.
NOT_FRONT_DOOR = {
    "stop-gate": "a hook verb, not something a reader invokes; documented where the gate is",
    "limitgate": "harness-facing",
    "statusline": "harness-facing",
    "sessionstart": "harness-facing",
    "integration-commit": "the second half of `integrate`, described with it",
    "SHOWRUNNER_STATE": "test seam for the issue waker, not a consumer knob",
    "SHOWRUNNER_SESSION": "set by the harness for a Crawler, never by a reader",
    "SHOWRUNNER_BIN": "an override for the probe's own resolution, not consumer surface",
    "SHOWRUNNER_CENTRAL": "central mode, documented in its own section by name",
    "XDG_CONFIG_HOME": "a standard variable this honours rather than defines",
    "NO_COLOR": "a standard variable this honours rather than defines",
    "GAME_LOOP_REPO": "the harness's own variable, read here to cooperate with it; it belongs to "
                      "game_loop's docs and naming it here would be this project documenting "
                      "somebody else's surface",
    "SHOWRUNNER_CRAWLER": "set by `spawn` onto a Crawler's session and read back by the lock "
                          "guard; a reader never sets it",
    "issue-waker.py": "SITE WIRING, not product — it names one repo and one trusted author set, "
                      "and install.sh does not copy it, so no consumer receives it. Excluded "
                      "here for the same reason test/mutate.py excludes it from the sweep; two "
                      "accountings disagreeing about what ships is how one of them goes stale.",
}


def verbs():
    out = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "showrunner"), "--help"],
                         capture_output=True, text=True).stdout
    m = re.search(r"\{([a-z0-9,\-]+)\}", out)
    return sorted(set(m.group(1).split(","))) if m else []


def env_vars():
    found = set()
    for base, _, files in os.walk(os.path.join(ROOT, "lib")):
        for f in files:
            if f.endswith(".py"):
                with open(os.path.join(base, f), errors="ignore") as fh:
                    found |= set(re.findall(r'environ(?:\.get)?[\(\[]"([A-Z][A-Z0-9_]+)"', fh.read()))
    return sorted(found)


def hooks():
    """Every shim this project registers — a guard nobody documents is one nobody wires."""
    d = os.path.join(ROOT, ".showrunner", "hooks")
    if not os.path.isdir(d):
        return []
    # Files only, and no bytecode: `__pycache__` is a directory Python made, not a hook this
    # project ships, and reporting it as undocumented is the check inventing work.
    return sorted(f for f in os.listdir(d)
                  if os.path.isfile(os.path.join(d, f)) and not f.startswith("."))


def main():
    text = ""
    for d in DOCS:
        try:
            with open(os.path.join(ROOT, d)) as fh:
                text += fh.read()
        except OSError:
            print("COULD NOT READ %s — this report is about the docs it could open, not all of "
                  "them" % d)
    missing, excluded = [], 0
    for kind, names in (("verb", verbs()), ("env", env_vars()), ("hook", hooks())):
        for n in names:
            if n in NOT_FRONT_DOOR:
                excluded += 1
                continue
            if n not in text:
                missing.append("%-5s %s" % (kind, n))

    print("checked %d doc(s) · %d surface(s) excluded with a reason" % (len(DOCS), excluded))
    if missing:
        print("\nNAMED NOWHERE IN THE FRONT DOOR:")
        for m in missing:
            print("  " + m)
    else:
        print("every verb, env var and hook is at least NAMED.")

    print("\nTHIS IS THE FLOOR, NOT THE GOAL. It cannot tell whether the prose is right, still")
    print("describes the code, explains anything, or reads coherently for somebody arriving new.")
    print("Those need the file read WHOLE — %s records when that last happened." % READINGS)
    path = os.path.join(ROOT, READINGS)
    if not os.path.exists(path):
        print("  ...and it does not exist, so no reading has ever been recorded.")
    else:
        with open(path) as fh:
            body = fh.read()
        head = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        for d in DOCS:
            hits = [l for l in body.splitlines() if l.strip().startswith("- ") and d in l]
            last = hits[-1] if hits else None
            if not last:
                print("  %-11s NEVER read end to end on the record" % d)
            elif head and head in last:
                print("  %-11s read whole at THIS commit" % d)
            else:
                print("  %-11s last read whole: %s" % (d, last.strip()[:90]))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
