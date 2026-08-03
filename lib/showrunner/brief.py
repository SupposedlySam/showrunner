"""The Crawler brief — what an agent is told before it touches anything. Issue #12.

The highest-leverage instruction in a Crawler's brief turned out not to be about the code
at all: *verify the premise against the real source, and if it does not hold, say so
loudly instead of building around it.*

Over one real run of 14 issues, three had premises that did not survive contact with the
codebase — one described a failure that was not live in that repo, one asserted tooling
that did not exist, one named a command from a different harness entirely. That is not a
criticism of the issues: a good bug report is written from the incident, and the incident
happened somewhere. But an orchestrator fans work out to agents who each see one issue
and no others, with no cross-check and nobody watching. A single agent working a queue
serially starts noticing that issue 9 contradicts what it read for issue 3; N isolated
agents each see one issue and cannot notice anything.

The brief is *generated*, not remembered, and the field it demands is enforced at the
close gate rather than requested politely here — a required argument is a check, a
paragraph is a wish.
"""

import os

from . import lanes, worktree
from .util import rel

TEMPLATE = """\
# Crawler brief — {crawler}

## The leaf
**{leaf_id}** — {title}

{body}

## Lane
{lane_line}

{lock_block}
## FIRST, BEFORE YOU WRITE ANYTHING: verify the premise

This issue was written from an incident. The incident may have happened in a different
repo, a different harness, or a version of this code that no longer exists. **Three of
fourteen issues in one real run described something that was not true here.**

Read the actual source this issue is about and decide whether its premise holds. Then say
so — it is a required field when you close, not an optional aside:

    showrunner close {leaf_id} --premise holds|partial|refuted|unverifiable \\
        --premise-read <the file you checked it against> ...

**"Premise refuted" is a successful outcome**, distinct from both done and failed. A run
that correctly declines to build something has produced real value. If you conclude the
premise does not hold, close it as refuted with the evidence and stop — do not build a
smaller version of the thing to have something to show:

    showrunner close {leaf_id} --refuted --evidence <file> --reason "<what is actually true>"

Why this matters more here than in a solo session: a Crawler that quietly implements a
fix for a bug that is not there is **indistinguishable** from one that did the work — same
commit, same green tests, same confident report, and the proof-of-done gate is satisfied
because a real artifact really was produced. The gate checks that work happened, not that
it was needed. And it compounds: the fix lands, the issue closes, and the codebase now
carries machinery defending against a hazard it never had — which the next reader takes
as evidence the hazard exists.

{orchestrator_block}
## Your workspace

| | |
|---|---|
| worktree | `{worktree}` |
| branch | `{branch}` |
| scratch | `{scratch}` |

Your worktree is **inside** the repo on purpose: your own write guard treats everything
outside the project as read-only, so a sibling directory would deny your first edit.

**If a commit is refused because checks are stale, run them IN THIS TREE:**

    cd {worktree_abs} && ./.game_loop/bin/verify

The refusal says "run verify" without saying where, and with several Crawlers running there
is more than one candidate — but only this tree's record can clear this tree's gate. Do not
run it in the main checkout, and do not reach for `--no-verify`.

**Do not chain staging with committing.** Run them as separate calls:

    git add -A
    git commit -m "..."

The gate runs *before* the command body, so `git add -A && git commit -m "..."` can never
pass on a first attempt — and when it is refused, **the `add` did not run either**. The
instinct is then to retry just the commit, which silently commits nothing of what the
message describes. Same reason `verify && git commit` cannot work: your verify line has not
executed when the gate reads the record.

**Put every non-repo file in your own scratch dir above** — commit messages, captured
output, before/after artifacts, fixtures. Not in a shared temp dir. Two Crawlers in a real
run both reached for `commitmsg.txt` in one shared directory; the second noticed the first
one's file only because it happened to list the directory first. Had it not, one Crawler
would have committed the other's commit message onto its own changes: a real commit, a
plausible message, describing work it does not contain, with every gate green. You and
your siblings are the same model solving similar tasks from similar prompts, so you
converge on the same obvious filename far more often than independent actors would.

{shared_block}
## Closing

You cannot close by asserting it. Name a real, non-empty artifact that proves the work —
a passing test, a golden, a committed file — and it must be newer than your claim:

    showrunner close {leaf_id} --proof <path> --premise <verdict> \\
        --premise-read <path> --reason "<what you did>"

Prefer to keep your edits inside the files this leaf is about. Your siblings are working
other leaves right now, and the orchestrator predicted your file sets do not overlap.
"""

SHARED_HEADER = """\
## What your worktree does NOT isolate

A git worktree isolates **tracked files** and nothing else. Before you assume you are
independent of your siblings:

"""

ORCH_HEADER = """\
## What the orchestrator already checked (confirm or refute it — do not trust it)

"""

LOCK_BLOCK = """\
This leaf is serialized behind the single-consumer resource **{resource}**. Do not run the
consuming command directly. Run it through the lock so the lock is held by the process
that actually consumes the resource:

    showrunner lock run {resource} --holder {crawler} -- <your command>

A guard that merely checks the lock is only as good as its verb matcher; the lock is the
guarantee only where the consumer itself takes it.

"""


def build(cfg, leaf, spawn_record, decision=None, orchestrator_findings=None):
    decision = decision or lanes.route(cfg, leaf)

    lock_block = ""
    if decision.get("lane") == lanes.SERIALIZED and decision.get("resource"):
        lock_block = LOCK_BLOCK.format(resource=decision["resource"],
                                       crawler=spawn_record["crawler"])

    shares = spawn_record.get("shares") or worktree.audit_shared(cfg)
    shared_block = ""
    if shares:
        parts = [SHARED_HEADER]
        for item in shares:
            parts.append(
                "- **%s** — %s\n  - consequence: %s\n  - what to do instead: %s\n"
                % (item.get("what"), item.get("why"), item.get("consequence"),
                   item.get("instead")))
        parts.append(
            "\nIf a shared-state gate refuses you, **wait or escalate — never bypass it.** "
            "`--no-verify` starts looking reasonable exactly when you are stuck under a "
            "mandate to finish, and that is the moment it is most wrong.\n")
        shared_block = "".join(parts) + "\n"

    orchestrator_block = ""
    if orchestrator_findings:
        lines = [ORCH_HEADER]
        for f in orchestrator_findings:
            lines.append("- %s\n" % f)
        lines.append(
            "\nThe orchestrator read the above and believes it. **Go verify it yourself before "
            "writing anything.** In a real run, the brief that said exactly this got back an "
            "independent confirmation with line numbers — worth strictly more than either "
            "party's reading alone.\n")
        orchestrator_block = "".join(lines) + "\n"

    lane_line = "%s%s — %s" % (
        decision.get("lane"),
        " (resource: %s)" % decision["resource"] if decision.get("resource") else "",
        decision.get("why"))

    return TEMPLATE.format(
        crawler=spawn_record["crawler"],
        leaf_id=leaf["id"],
        title=leaf.get("title", ""),
        body=(leaf.get("body") or "_(no description on the leaf)_").strip(),
        lane_line=lane_line,
        lock_block=lock_block,
        orchestrator_block=orchestrator_block,
        shared_block=shared_block,
        worktree=rel(spawn_record["worktree"], cfg.root),
        worktree_abs=spawn_record["worktree"],
        branch=spawn_record["branch"],
        scratch=rel(spawn_record["scratch"], cfg.root),
    )


def write(cfg, spawn_record, text):
    path = os.path.join(cfg.abspath(spawn_record["scratch"]), "BRIEF.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    return path
