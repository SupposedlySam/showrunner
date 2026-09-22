"""Is anything able to wake this session back to a GOAL before it starts long work?

THE PROBLEM THIS EXISTS FOR, in the words of the person who kept solving it by hand: "I have to
tell them manually, none of them know." Every agent on this machine starts long unattended work
without binding a mandate, so `game_loop doorbell` has nothing to say -- "no mandate is bound
here, so there is nothing to wake this run FOR" -- and a wake that arrives during that work
returns an agent to a prompt with a hole where the goal goes.

THE INFORMATION WAS NEVER MISSING. showrunner's own SessionStart banner has always printed
`MANDATE: none (Stop gate inert)`, and game_loop's `doorbell` explains the remedy in full the
moment anybody runs it. Agents still did not do it, which makes this a DELIVERY defect rather
than a documentation one: session-start text is read once, before the agent knows whether the
work ahead is long, and by the time it is long the banner is thousands of tokens upstream. The
human succeeded where both documents failed for one reason -- they spoke AT THE MOMENT.

So this is deliberately not more documentation. It fires at the moment a session starts
something long, says the one thing that is actionable then, and stays silent otherwise.

WHAT IT MUST NOT DO, since a nag is how a real signal gets trained into noise:
  - speak when a mandate IS bound (the advice has already been taken)
  - speak when game_loop is absent (showrunner does not require it -- measured: install.sh
    references game_loop only as a shape it copies and one optional integration point)
  - speak for ordinary short commands
  - speak more than once in a session
  - block anything, ever. It is advice at a moment, not a gate with an opinion.
"""

import json
import os
import re
import subprocess
import time

# A SESSION IS TOLD ONCE. The second telling is what turns a true notice into scenery: this repo
# already learned it from the fail-open ledger, where a notice that reached an agent mid-task was
# "reliably skimmed". One at the first long command is the whole budget.
SEEN_NAME = "wake-gate-seen.json"
SEEN_TTL = 7 * 24 * 3600

# CONSERVATIVE ON PURPOSE, and biased toward missing work rather than inventing it. A false
# positive here spends an agent's attention on advice it did not need, which is the failure that
# makes the true positives stop landing. A false negative costs one unattended run -- the status
# quo, which is what this is improving on rather than perfecting.
#
# Anchored on the RUNNER, not on words like "test" or "build" that appear in ordinary prose and
# in paths. `sleep` is included because a deliberate wait is the purest form of "I am about to be
# unreachable for a while".
LONG_PATTERNS = (
    r"\btest/run\.py\b", r"\btest/mutate\.py\b", r"\bpytest\b", r"\bgo test\b",
    r"\bcargo (?:test|build|run)\b", r"\bnpm (?:test|run build|ci)\b", r"\byarn build\b",
    r"\bflutter (?:test|build|run)\b", r"\bdart test\b", r"\bgradlew?\b", r"\bmake\b",
    r"\bdocker (?:build|compose up)\b", r"\bterraform (?:plan|apply)\b",
    r"\bsleep\s+\d{2,}\b", r"--launch\b", r"\bgame_loop\s+verify\b",
)


def long_work(command, background=False):
    """(is_long, why). Never raises.

    `background` is the harness SAYING SO and outranks every pattern below it: a call the caller
    has already declared long-running needs no guessing, and this is the one signal here that is
    a statement rather than an inference.
    """
    if background:
        return True, "the call is backgrounded, which is the caller stating it runs past this turn"
    if not (command or "").strip():
        return False, "no command to judge"
    # A MENTION IS NOT A USE. The first version searched the whole command string, so a commit
    # message containing the word "make" read as a build: reported by balooga-owner and
    # reproduced independently by llm_chat's owner on their own commit, both within a day of
    # release. That is the defect reach.py's gate had already paid for twice (a heredoc body, then
    # a quoted argument) and fixed with `_command_segments`: heredoc bodies dropped, quoted
    # contents emptied, split at command boundaries. Borrowed rather than re-derived, so a third
    # mention-vs-use fix lands in one place and reaches both gates.
    #
    # Matching still runs anywhere WITHIN a segment rather than only at its start, because
    # `--launch` is a flag on a showrunner command and `cd x && pytest` puts the runner second.
    # What it no longer sees is prose: `git commit -m "make it faster"` becomes
    # `git commit -m ""`.
    from .reach import _command_segments
    for seg in _command_segments(command):
        text = " ".join(seg.split())
        for pat in LONG_PATTERNS:
            if re.search(pat, text):
                return True, ("the command matches %s, which this project treats as long work"
                              % pat)
    return False, "nothing about this command says it runs long"


def game_loop_bin(root):
    """The project's game_loop, or None. Absent is an ORDINARY answer, not a problem."""
    if not root:
        return None
    path = os.path.join(root, ".game_loop", "bin", "game_loop")
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def armed(root):
    """('armed'|'unarmed'|'absent'|'unknown', detail). Never raises.

    ASKS THE TOOL THAT OWNS THE CONCEPT rather than reading its state file. A mandate lives in
    game_loop's `sessions/<id>/state.json`, and parsing another project's internal shape is how
    two layers start disagreeing silently -- the exact failure this repo keeps finding in itself.
    `doorbell` is game_loop's own published answer to "is there a goal to wake to", and its
    no-mandate branch is a documented, stable sentence.

    UNKNOWN IS NOT ARMED. A `doorbell` that could not be run tells us nothing about whether a
    goal is bound, and recording that as "fine" is the identity-element defect this codebase is
    mostly made of fixes for.
    """
    exe = game_loop_bin(root)
    if not exe:
        return "absent", "no game_loop in this project, so there is no doorbell to arm"
    try:
        out = subprocess.run([exe, "doorbell"], cwd=root, capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return "unknown", "`game_loop doorbell` could not be run (%s)" % exc
    text = (out.stdout or "") + (out.stderr or "")
    if not text.strip():
        return "unknown", "`game_loop doorbell` printed nothing, which answers neither way"
    if "no mandate is bound" in text:
        return "unarmed", "`game_loop doorbell` says there is nothing to wake this run for"
    return "armed", "a mandate is bound, so a wake has a goal to return to"


def _seen_path(root):
    return os.path.join(root, ".showrunner", SEEN_NAME)


def already_told(root, session):
    """Has this session already been told? Never raises; an unreadable ledger means NOT told.

    FAILING TOWARD SPEAKING, which is the opposite of the usual posture here and deliberate: the
    cost of a second notice is one skimmed paragraph, and the cost of a swallowed first one is
    the unattended run this exists to prevent. The asymmetry decides the direction.
    """
    if not session:
        return False
    try:
        with open(_seen_path(root)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return session in (data.get("told") or {})


def record_told(root, session):
    """Remember that this session was told. Best-effort: never let bookkeeping break advice."""
    if not session:
        return
    path = _seen_path(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        told = data.get("told") if isinstance(data.get("told"), dict) else {}
        now = int(time.time())
        # Pruned on write, so a long-lived monorepo checkout does not grow a ledger forever.
        told = {s: t for s, t in told.items()
                if isinstance(t, (int, float)) and now - t <= SEEN_TTL}
        told[session] = now
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"told": told}, fh)
        os.replace(tmp, path)
    except OSError:
        return


def notice_lines(state, detail, why):
    """What to say. A list of lines, or [] when there is nothing worth saying."""
    if state == "armed" or state == "absent":
        return []
    head = ("▸ You are starting long work and NOTHING CAN WAKE YOU BACK TO A GOAL."
            if state == "unarmed" else
            "▸ You are starting long work and whether anything can wake you back to a goal "
            "COULD NOT BE DETERMINED.")
    out = [head, "  %s" % detail, "  Why this fired now: %s." % why, ""]
    if state == "unarmed":
        out += [
            "  Bind one BEFORE the long call, not after — a wake that arrives first returns you",
            "  to a prompt with a hole where the goal goes, and you re-derive the run from",
            "  scratch. That re-derivation is the entire cost this prevents.",
            "",
            '      ./.game_loop/bin/game_loop mandate --set "<what done means>" \\',
            '          --wake-path "<how a signal lands>"',
            "",
            "  Then, as you learn them, the things a woken you must not re-derive:",
            "",
            '      ./.game_loop/bin/game_loop note --recovery "<a remedy you found>"',
            "",
            "  A mandate also arms game_loop's Stop gate, which is inert without one.",
        ]
    else:
        out += [
            "  Could-not-tell is not armed. Run `./.game_loop/bin/game_loop doorbell` yourself",
            "  before the long call and act on what it says.",
        ]
    out += ["", "  Not a refusal — the call is proceeding, and this is said once per session."]
    return out
