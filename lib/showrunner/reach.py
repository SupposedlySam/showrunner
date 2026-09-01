"""What an agent REACHED FOR, and the mechanism it should have reached for instead.

THE REPORTED FAILURE. An agent several compactions deep had lost which campaign it was on and
what verbs existed, and "wasn't even using it" — it did the work by hand in the main checkout of
a repo carrying a live campaign. An agent that cannot remember a tool does not stop working; it
reaches for what it already knows. `git worktree add`, a raw dispatch, a hand-rolled todo list,
a note written into a memory file. Every one of those is a working action that produces a
plausible result, which is why nothing ever objected.

WHY THIS IS ADVICE AND NEVER A REFUSAL. Each reach named here is legitimate somewhere: people do
create worktrees by hand, and a memory file is the right home for plenty of things. A gate that
blocks a legitimate shape trains its own bypass, and the bypass outlives the lesson — the same
argument `pipeline-status-gate.sh` makes for noticing rather than denying. So this only ever adds
`additionalContext` to a call that proceeds.

WHY IT FIRES AT THE MOMENT OF REACH. SessionStart and PostCompact announce the tool; that
announcement is read once and competes with everything else in the window. The moment an agent
types `git worktree add` is the moment it has a specific intent that a specific verb serves, and
it is the only moment at which naming that verb costs nothing to act on.

THE RULES NAME MECHANISMS, AND THE MECHANISMS ARE CHECKED. A table like this rots the first time
a verb is renamed, and a rule pointing at a verb that no longer exists is worse than no rule: it
teaches that the tool cannot do the thing. So every `verb` a rule names is verified against
`roles.verb_inventory()` — the parser's own list — by the suite, and a rule naming a dead verb
fails the run rather than misleading a reader.

FOREIGN MECHANISMS ARE DETECTED, NEVER ASSUMED. Some advice belongs to game_loop, which is a
separate project this repo consumes and does not modify. A rule carrying `requires` names a path
that must exist before the rule may speak, so a repo without game_loop is never told to run a
command it does not have.
"""
import json
import os
import re

# Each rule: (name, tools, pattern, verb, message).
#   tools   — tool names this can fire on; empty means any.
#   pattern — matched against the payload's searchable text, case-insensitively.
#   verb    — the showrunner verb the message names, or "" when it names none. CHECKED.
#   requires— path that must exist under the repo root for the rule to speak, or "".
RULES = [
    # THE ONE THE REPORT ASKED FOR. A lesson written into a memory file is remembered; a lesson
    # written into an artifact is enforced. This repo's whole posture is that instructions do not
    # survive a compaction and checks do — and a memory file is an instruction with better
    # spelling. It is still the right home for preferences and for facts about a person, so this
    # names the distinction rather than objecting to the write.
    ("memory-write-could-be-hardened",
     ("Write", "Edit", "NotebookEdit"),
     r"(^|/)(memory/|MEMORY\.md|CLAUDE\.md)",
     "",
     ".game_loop/bin/game_loop",
     "You are writing to a memory file. If what you are recording is a LESSON ABOUT HOW TO "
     "WORK — a mistake not to repeat, a check that should have run — a memory is the weakest "
     "form of it: it is remembered, not enforced, and this project's founding rule is that "
     "enforcement lives in tools and artifacts, never in instructions. `game_loop harden "
     "--learning .. --artifact <real path> --mechanism .. --rung <1..6>` binds it to something "
     "that fails loudly, and rung 6 IS 'write it down' — the last resort, not the first. "
     "Preferences, and facts about a person or a project, belong here and need no hardening."),

    # A WORKTREE BY HAND IS THE 42-DISPATCH FAILURE IN ITS OTHER SPELLING. The tree appears, the
    # branch appears, and none of the guarantees do.
    ("worktree-by-hand",
     ("Bash",),
     r"git\s+(-C\s+\S+\s+)?worktree\s+add",
     "spawn",
     "",
     "You are creating a worktree by hand. `showrunner spawn <leaf> --actor <name>` creates the "
     "same tree AND the branch, the lease, the claim a reaper can reclaim, the scratch dir and "
     "the brief — none of which a bare `git worktree add` produces. A tree without a lease is "
     "not protected from a second session writing into it, and a leaf without a claim is "
     "invisible to `showrunner ready`."),

    # A HAND-ROLLED WORK LIST BESIDE A GRAPH THAT ALREADY HOLDS ONE. Two lists of the work is how
    # one of them silently stops being true, and the graph is the one the tool reads.
    ("todo-beside-a-graph",
     ("TodoWrite",),
     r".",
     "ready",
     "",
     "This repo carries a showrunner graph, which is already a work list — one that records "
     "dependencies, claims and liveness, and that `ready` and `plan` read. A private todo list "
     "beside it is a second copy that nothing reconciles: work you close there stays open in "
     "the graph, and work another session claims never appears in yours. `showrunner ready` "
     "lists what is unblocked AND unclaimed; `showrunner add <title>` puts new work where the "
     "rest of the campaign can see it."),

    # A BRANCH FOR PARALLEL WORK, WITHOUT THE REST OF IT.
    ("branch-for-parallel-work",
     ("Bash",),
     r"git\s+(-C\s+\S+\s+)?(checkout\s+-b|switch\s+-c)\s",
     "spawn",
     "",
     "If this branch is for a parallel Crawler, `showrunner spawn` makes the branch inside its "
     "own worktree with a lease and a claim, so two sessions cannot land on it at once. If it "
     "is just a branch for your own work, carry on — this is a notice, not a refusal."),
]


_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?\n.*?^\1$", re.S | re.M)


def _strip_heredocs(command):
    """Drop heredoc BODIES before matching. A mention is not a use.

    Measured the hour this shipped: writing a test whose source contained `git worktree add`
    inside a heredoc made this fire on the act of writing the test. The body of a heredoc is
    text being handed to another program — a file being written, a message, a script — and text
    ABOUT a command is not somebody running it. `pipeline-status-gate.sh` documents the same
    distinction for the same reason, having first matched its own source.

    A false positive is cheap here because this only ever advises. It is not free: an advisory
    channel that fires on documents about git becomes one a reader skims, and it is depended on
    precisely for the turns where the reader is not paying close attention.
    """
    return _HEREDOC.sub("", command)


def _searchable(tool, tool_input):
    """The text a rule matches against — the fields an intent actually shows up in.

    NOT `json.dumps(payload)`. Dumping the whole object makes every rule match on any field that
    happens to contain the word, including the file CONTENT of a write — so a rule about writing
    to a memory file would fire on a commit message that merely mentions memory. Matching named
    fields keeps a rule about a path a rule about a path.
    """
    if not isinstance(tool_input, dict):
        return ""
    fields = ("command", "file_path", "notebook_path", "path")
    parts = [_strip_heredocs(str(tool_input.get(f) or "")) for f in fields]
    if tool == "TodoWrite":
        # The intent IS the call; there is no path or command to read.
        parts.append("todowrite")
    return "\n".join(p for p in parts if p)


def advise(tool, tool_input, root=None):
    """Every rule that fires, as a list of (name, message). Empty when none do.

    Returns a LIST rather than the first hit: two reaches in one command is an ordinary shape
    (`git checkout -b x && git worktree add ..`), and reporting one of them silently drops the
    other. The caller decides how many to render.
    """
    text = _searchable(tool, tool_input)
    if not text:
        return []
    out = []
    for name, tools, pattern, _verb, requires, message in RULES:
        if tools and tool not in tools:
            continue
        if requires and not (root and os.path.exists(os.path.join(root, requires))):
            continue
        if re.search(pattern, text, re.I | re.M):
            out.append((name, message))
    return out


def render(hits, sr=None):
    """The additionalContext string for a set of hits, or "" for none."""
    if not hits:
        return ""
    lines = []
    for _name, message in hits:
        lines.append("▸ " + (message.replace("showrunner ", "%s " % sr) if sr else message))
    return ("showrunner: you reached for something this project has a mechanism for. Not a "
            "refusal — the call is proceeding.\n\n" + "\n\n".join(lines))


def hook_payload(stream):
    """The PreToolUse payload, or ({}, reason). Never raises — see the guards' posture."""
    try:
        raw = stream.read()
    except Exception as exc:                                    # noqa: BLE001
        return {}, "stdin could not be read (%s)" % exc
    if not (raw or "").strip():
        return {}, "the payload was empty"
    try:
        return json.loads(raw), None
    except ValueError as exc:
        return {}, "the payload was not JSON (%s)" % exc
