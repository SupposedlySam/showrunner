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
# NOTE ON DEAD KEYS: `limitgate`, `statusline` and `sessionstart` were excused here as
# "harness-facing" and are not showrunner verbs at all -- the CLI has never had them. They
# excused nothing, and `vacuous()` below now fails the suite on that shape rather than leaving it
# to be noticed. A vacuous exemption is not dead code: dead code does nothing, while this is a
# standing authorisation, redeemed by whoever adds a matching surface next -- nobody's decision,
# at no particular moment, with no output.
# Variables showrunner HONOURS but never defines. They are not exclusions and must not sit in
# NOT_FRONT_DOOR: the derivation only ever yields SHOWRUNNER_* names, so a key here could never
# match one, and an entry that can never match is indistinguishable from a decision somebody
# made. Kept as prose because the reasoning is worth having, not as a suppression.
HONOURED_NOT_OURS = {
    "GAME_LOOP_REPO": "the harness's own variable, read here to cooperate with it; it belongs to "
                      "game_loop's docs and naming it here would be this project documenting "
                      "somebody else's surface",
    "NO_COLOR": "a standard variable this honours rather than defines",
    "XDG_CONFIG_HOME": "a standard variable this honours rather than defines",
}

NOT_FRONT_DOOR = {
    "stop-gate": "a hook verb, not something a reader invokes; documented where the gate is",
    "integration-commit": "the second half of `integrate`, described with it",
    "SHOWRUNNER_STATE": "test seam for the issue waker, not a consumer knob",
    "SHOWRUNNER_SESSION": "set by the harness for a Crawler, never by a reader",
    "SHOWRUNNER_BIN": "an override for the probe's own resolution, not consumer surface",
    "SHOWRUNNER_CENTRAL": "central mode, documented in its own section by name",
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


# Everything a CONSUMER receives, not just the library. This used to walk lib/**.py alone, so
# every variable read by install.sh or by a hook shim was invisible -- and three of them sat in
# NOT_FRONT_DOOR by name, which made the blind spot read as a decision somebody had made. Removing
# those names as "vacuous" would have left the blindness and lost the only trace of it. An
# exclusion that names something the check cannot see is the shape to watch for.
_ENV_SOURCES = (("lib", (".py",)), (".showrunner/hooks", (".sh", ".py")), (".", (".sh",)))
_ENV_PATTERNS = (
    r'environ(?:\.get)?[\(\[]"([A-Z][A-Z0-9_]+)"',   # python
    r'\$\{?([A-Z][A-Z0-9_]{3,})\}?',                  # shell reads
)


def env_vars():
    """Every SHOWRUNNER_* variable this project reads, wherever it reads it."""
    found = set()
    for rel, exts in _ENV_SOURCES:
        base_dir = os.path.join(ROOT, rel)
        if not os.path.isdir(base_dir):
            continue
        walk = os.walk(base_dir) if rel != "." else [(base_dir, [], os.listdir(base_dir))]
        for base, _, files in walk:
            if os.sep + ".git" in base:
                continue
            for f in files:
                if not f.endswith(exts):
                    continue
                try:
                    with open(os.path.join(base, f), errors="ignore") as fh:
                        blob = fh.read()
                except OSError:
                    continue
                for pat in _ENV_PATTERNS:
                    found |= set(re.findall(pat, blob))
    # Only this project's own knobs. A shell scan otherwise reports PATH, HOME and every caps
    # word in a heredoc, and a check that cries wolf is one whose output stops being read.
    return sorted(v for v in found if v.startswith("SHOWRUNNER_"))


def hooks():
    """Every shim this project registers — a guard nobody documents is one nobody wires."""
    d = os.path.join(ROOT, ".showrunner", "hooks")
    if not os.path.isdir(d):
        return []
    # Files only, and no bytecode: `__pycache__` is a directory Python made, not a hook this
    # project ships, and reporting it as undocumented is the check inventing work.
    return sorted(f for f in os.listdir(d)
                  if os.path.isfile(os.path.join(d, f)) and not f.startswith("."))



def vacuous():
    """Exclusion keys naming nothing this project actually has."""
    return sorted(set(NOT_FRONT_DOOR) - (set(verbs()) | set(env_vars()) | set(hooks())))


def unnamed():
    """(missing, excluded, unreadable) — the finding, separated from the reporting.

    Exists so test/run.py can ASSERT on this rather than shelling out for an exit code. A check
    that only a human runs by hand has never run: this module shipped tracked, reachable and
    invoked by nothing, while I claimed in public that the ordinary suite ran it. A doc file that
    could not be opened is returned as its own value and never folded into "nothing missing" --
    a failed READ must not be stored as the fact "no surface is undocumented".
    """
    text, unreadable = "", []
    for d in DOCS:
        try:
            with open(os.path.join(ROOT, d)) as fh:
                text += fh.read()
        except OSError:
            unreadable.append(d)
    missing, excluded = [], 0
    for kind, names in (("verb", verbs()), ("env", env_vars()), ("hook", hooks())):
        for n in names:
            if n in NOT_FRONT_DOOR:
                excluded += 1
                continue
            if n not in text:
                missing.append("%-5s %s" % (kind, n))
    return missing, excluded, unreadable


def main():
    missing, excluded, unreadable = unnamed()
    for d in unreadable:
        print("COULD NOT READ %s — this report is about the docs it could open, not all of "
              "them" % d)

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
            # Anchor on the entry's own prefix, never a substring anywhere in the line: one
            # entry mentioned the OTHER file while describing a shared defect, and a plain
            # `d in l` reported README's reading as llms.txt's. A ledger that answers for the
            # wrong file is worse than one that answers "never" -- it retires the question.
            # Struck entries (leading ~~) are corrections, not readings, and never count.
            hits = [l for l in body.splitlines()
                    if l.strip().startswith("- " + d + " ")]
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
