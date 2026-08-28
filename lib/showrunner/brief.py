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

from . import dispatch, lanes, worktree
from .util import rel

TEMPLATE = """\
# Crawler brief — {crawler}

## The leaf
**{leaf_id}** — {title}

{body}

## Lane
{lane_line}
{chat_block}

{lock_block}
## FIRST, BEFORE YOU WRITE ANYTHING: verify the premise

This issue was written from an incident. The incident may have happened in a different
repo, a different harness, or a version of this code that no longer exists. **Three of
fourteen issues in one real run described something that was not true here.**

Read the actual source this issue is about and decide whether its premise holds. Then say
so — it is a required field when you close, not an optional aside:

    {sr} close {leaf_id} --premise holds|partial|refuted|unverifiable \\
        --premise-read <the file you checked it against> ...

**"Premise refuted" is a successful outcome**, distinct from both done and failed. A run
that correctly declines to build something has produced real value. If you conclude the
premise does not hold, close it as refuted with the evidence and stop — do not build a
smaller version of the thing to have something to show:

    {sr} close {leaf_id} --refuted --evidence <file> --reason "<what is actually true>"

## AND SEPARATELY: does anything REACH the code?

A true premise attached to dead code is void, and the premise check cannot catch it — that
one asks *"is this true of the code?"*, this asks *"does anything get here?"*, and the answer
lives in files this brief did not point you at.

Two real cases, one run, one hour apart. A UI preset's colour matrix was genuinely malformed
— measured, +35 luminance, every word correct — and the preset is not in the picker, because
an allowlist in a different file dropped it five months earlier in a commit that says so.
And a server branch picking 2 of 4 items was a real defect, gated on `count <= 4`, where the
only client in the repo sends 40. Both briefs were accurate. Both were void. Neither Crawler
could have discovered it from the file it was handed.

**Name the entry point and the caller before you build.** If the orchestrator filled in the
reachability section below, check it rather than trusting it. If it is empty, that is your
first task and it is usually one grep: who supplies the enum value, the parameter or the flag
this code is gated on?

**Where a UI is involved, walk the UI.** A rendered list, menu or picker is enumerated
somewhere the source read will not show you. In the case above, the source supported the brief
and scrolling the on-device list to the end refuted it in under a minute.

**"Unreachable" is a third close outcome**, not a kind of refuted:

    {sr} close {leaf_id} --unreachable --evidence <the allowlist / caller / enumeration> \
        --premise holds --reason "<what nothing reaches, and how you know>"

Recording it as `done` claims a user-visible change that does not exist; recording it as
`refuted` says your analysis was wrong when it was right. Both lose the finding, and `done`
is the one you will reach for, because you will have a real commit and real tests in hand.

Why this matters more here than in a solo session: a Crawler that quietly implements a
fix for a bug that is not there is **indistinguishable** from one that did the work — same
commit, same green tests, same confident report, and the proof-of-done gate is satisfied
because a real artifact really was produced. The gate checks that work happened, not that
it was needed. And it compounds: the fix lands, the issue closes, and the codebase now
carries machinery defending against a hazard it never had — which the next reader takes
as evidence the hazard exists.

## What your REPORT is allowed to claim

Your report is read by an orchestrator that cannot cheaply check it. It will dispatch the next
leaf, tell a sibling what already landed, or close this one on the strength of your sentences.
So a confident wrong sentence here is more expensive than a wrong commit — the commit is caught
by a gate, the sentence is acted on.

Four failures, each of which has actually happened in a real run:

**A claim about code you just spent an hour in is the most convincing kind of wrong.** Recency
feels like knowledge, so you skip the read that would take seconds. Before writing "X now does
Y", grep for Y. This is the single cheapest check in this document.

**Existing is not working.** A file that is present, an option that is accepted, a command that
exits 0 — none of them establish the thing runs. Assert the OUTPUT, not the artifact. A binary
placed without its library is executable and dies on every invocation, and both look identical
to a check that asks whether the path exists.

**A failed read is not a fact about the world.** Code that catches an error and returns what it
has reports "nothing there", which is indistinguishable from there genuinely being nothing. If
you could not look, say *could not look* — never fold it into the answer. Same for a search that
matched nothing: "no results" and "the search was wrong" are one observation.

**Surviving by elimination is not support.** If two of three explanations died and you are
reporting the third, say that is what happened. An explanation that is merely the last one
standing has no evidence for it, and written up without that qualifier it becomes a fact the
next reader inherits.

If you refute something you already told the orchestrator, send the correction as a NEW message
rather than editing the old one — somebody may have already acted on it.

{orchestrator_block}
## Your workspace

| | |
|---|---|
| worktree | `{worktree}` |
| branch | `{branch}` |
| scratch | `{scratch}` |

Your worktree is **inside** the repo on purpose: your own write guard treats everything
outside the project as read-only, so a sibling directory would deny your first edit.

**And that is exactly why a parent-walking resolver finds the WRONG tree.** `npx`, a bare
binary on `PATH`, `python -m` against a parent venv, `bundle exec` — each walks UP until it
finds a `node_modules`, a venv, a Gemfile. Your worktree sits inside the repo root, so the
first one they find belongs to the PRIMARY checkout, not to you.

**Invoke project binaries by explicit path from this tree.** `./node_modules/.bin/mocha`,
never `npx mocha`.

This is here rather than in a doc because the failure is shaped to waste your time. Observed:
`npx mocha` inside a worktree failed with `Cannot find package 'ts-node'`, naming a path under
the primary checkout — in a project that uses `tsx` and has never depended on `ts-node`. The
error names a package the repo does not use, so it reads as a broken install, and the natural
next move (reinstalling dependencies inside the worktree) is slow and can "fix" it in a way
that hides the cause.

**So: if a tool reports a dependency this project does not use, suspect the resolver picked up
the parent checkout before you suspect the install.** Read the paths in the error — they name
which tree actually answered, and that is the fastest thing in the message.

**If a commit is refused because checks are stale, run them IN THIS TREE:**

    cd {worktree_abs} && ./.game_loop/bin/verify

The refusal says "run verify" without saying where, and with several Crawlers running there
is more than one candidate — but only this tree's record can clear this tree's gate. Do not
run it in the main checkout, and do not reach for `--no-verify`.

**Expect `verify` to take minutes, not seconds**, and run it as its own earlier step rather
than in front of the commit you want. It re-runs the suite once per stale pattern, so a change
touching several gated files costs several suite runs. It has not hung and it does not need
interrupting; a verb that quietly became slow is easy to mistake for a wedged session, which
is the mistake to avoid here.

**Do not chain staging with committing.** Run them as separate calls:

    git add -A
    git commit -m "..."

The gate runs *before* the command body, so `git add -A && git commit -m "..."` can never
pass on a first attempt — and when it is refused, **the `add` did not run either**. The
instinct is then to retry just the commit, which silently commits nothing of what the
message describes. Same reason `verify && git commit` cannot work: your verify line has not
executed when the gate reads the record.

**Commit from the tree you are already in — never `cd` to it through a shell variable.**
The gate reads the command line to work out which tree a commit lands in. A path it cannot
resolve is now DENIED outright — resolving it would mean executing it, which the guard must
never do, so it fails closed and says so. Commit from the tree you are in, or name the path
literally; either is one line.

This used to pass *silently* instead, which is why the habit is worth breaking rather than
working around: measured on the same tree with the same failing checks, a literal path was
correctly denied and a variable-built path sailed through with no output distinguishing
"checked and fine" from "never looked". Orchestration reaches worktrees through variables by
default, so that silence was the normal shape under fan-out, not an edge case. It is fixed
upstream, and a tree carrying an older harness still has the quiet version.

**Put every non-repo file in your own scratch dir above, by the ABSOLUTE path given** — commit
messages, captured output, before/after artifacts, fixtures. Never at a path relative to this
worktree: your tree is deleted once your work is integrated and everything inside it goes too,
including the artifact you cite as `--proof`. The scratch dir named above is in the main
checkout and survives. Not a shared temp dir either. Two Crawlers in a real
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

    {sr} close {leaf_id} --proof <path> --premise <verdict> \\
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

CHAT_BLOCK = """\
## You are reachable

The orchestrator opened a channel for you and wired delivery into this worktree. Join it
once, as yourself, before you start — **run it by this exact path**:

    {chat_cli} join {channel} --as {crawler}

Messages arrive in your context automatically; you do not poll. INBOUND and OUTBOUND are wired
differently and only one of them is automatic: delivery hooks carry absolute paths, so you
RECEIVE without doing anything, while sending needs the binary — and it is generally NOT on
your PATH. A Crawler that reported this had received messages all session and could not answer
one, which from the orchestrator's side is indistinguishable from a Crawler thinking hard.

**Do not post a start notice.** The orchestrator dispatched you seconds ago and already knows
what you are doing — it wrote this brief. Under a turn-end gate that blocks on unanswered
messages, an announcement is indistinguishable from a question, so a wave of N Crawlers each
saying hello costs N blocked turn-ends on the one session whose attention is not parallel.

**Post when you have something the orchestrator can act on.** You are one of several Crawlers
who cannot see each other, working from an issue that may describe a different repo. Say it
when the premise looks refuted, when two leaves seem to want the same file, when you are
blocked, or when you are about to do something wide and irreversible — then keep working on
what is unambiguous. Post your verdict when you close. The orchestrator is reading. A question
costs a sentence; the wrong guess costs a merge.

**This is how you ask instead of guessing** — and saying nothing else is how the asking stays
cheap enough to be worth answering.

"""

NO_CHAT_BLOCK = """\
## You are NOT reachable — nobody is listening

The orchestrator tried to open a channel for you, and it did not open:

    {reason}

**There is no room.** Do not try to join one — the join fails, and working out whether that
was your fault costs you a turn. Nothing you post reaches anyone and no answer is coming, so
silence here is not the orchestrator thinking: it is nobody.

**Write it down instead of asking it.** The question you would have posted goes in your
scratch dir, which outlives this worktree:

    {scratch}/QUESTIONS.md

Then decide it yourself and keep working. Say in your close-reason which way you went, what
that rests on, and what would have changed it. That is the orchestrator's copy of the
conversation you could not have, and it is worth more than a guess reported as a finding.

A Crawler that knows it cannot reach anyone can do this. One that has been told a room exists
waits for an answer, or posts into nothing and reads the silence back as agreement.

"""

LOCK_BLOCK = """\
This leaf is serialized behind the single-consumer resource **{resource}**. Do not run the
consuming command directly. Run it through the lock so the lock is held by the process
that actually consumes the resource:

    {sr} lock run {resource} --holder {crawler} -- <your command>

A guard that merely checks the lock is only as good as its verb matcher; the lock is the
guarantee only where the consumer itself takes it.

"""


def sr_bin(cfg):
    """The showrunner binary, named ABSOLUTELY, because the brief is read inside a worktree.

    `showrunner` is not a global command — it is `.showrunner/bin/showrunner`, and
    `.showrunner/` is runtime state that `git worktree add` does not carry, since that copies
    TRACKED files only. So a bare command in the brief names something that cannot resolve
    from where the Crawler actually stands, and the whole proof-of-done design routes through
    it. Nothing needs copying: `config.load` resolves the main checkout from --git-common-dir,
    so one binary serves every worktree, one graph, one campaign.

    RESOLVED AGAINST THE FILESYSTEM, because naming it absolutely is only half the job and the
    other half was missing here for a week. `install.sh` places that copy; a DEVELOPMENT
    checkout — this repo working on itself — never runs its own installer, so the path was
    absolute, canonical, and dead. Every Crawler launched from here was told to run a binary
    that did not exist, in the highest-traffic remedy text this project ships. The check that
    was supposed to catch this reads every `showrunner <verb>` and asks whether the VERB is
    real; nothing asked whether the thing being invoked was.

    The installed copy wins when both are present — it is the one a consumer has, and the one
    `git worktree add` correctly does not carry. `bin/showrunner` is the fallback for a repo
    that IS showrunner. When neither exists the canonical path is returned anyway, so the
    message still names the right place, and `doctor` says it is missing.

    AND A SELF-VENDORED PIN WINS OVER BOTH, for the one repo where the tool and the work are the
    same files. showrunner develops itself, so its guards, hooks and briefs run the very code
    being edited — and ONE syntax error under `lib/showrunner/` kills every verb at import, which
    was measured to leave the worktree guard exiting 1 with empty stdout: neither a refusal nor
    an announcement. Editing this tool silently disarmed its own guard.

    `.showrunner_self/` is a gitignored copy pinned at a commit (`self --pin --dest
    .showrunner_self`), so the plumbing runs code a mid-edit cannot break while the working tree
    is free to be broken. Borrowed wholesale from game_loop, which solves the same problem with
    `.game_loop_self/` and the same ordered fallback — and the fallback is the load-bearing part:
    a fresh clone has no pin and simply uses the source, so nothing has to be installed for the
    repo to work.

    STATE IS NOT AFFECTED, and that is why this needs no equivalent of game_loop's
    GAME_LOOP_HOME: `config.load` already resolves the project from the cwd's git root, so a
    binary anywhere reads THIS repo's `.showrunner/`. Only the CODE moves.
    """
    for path in (os.path.join(cfg.root, ".showrunner_self", "bin", "showrunner"),
                 os.path.join(cfg.root, ".showrunner", "bin", "showrunner"),
                 os.path.join(cfg.root, "bin", "showrunner")):
        if os.access(path, os.X_OK):
            return path
    return os.path.join(cfg.root, ".showrunner", "bin", "showrunner")


def build(cfg, leaf, spawn_record, decision=None, orchestrator_findings=None,
          chat=None):
    """`chat` is the PROVISIONING RESULT — `(channel, opened, detail)` — never a bare name.

    IT USED TO BE THE NAME, and a name is not a room. `dispatch.channel_for` hands back a
    channel whenever `chat.enabled` is true, whether or not anything was ever opened, and this
    function rendered "the orchestrator opened a channel for you" from it as a statement of
    fact — in the same `spawn` whose dispatch report said `chat not wired`. The Crawler acts on
    the brief, not on the report, so four of them in one campaign were handed a claim about a
    thing that was never made plus a join command that could not succeed.

    Taking the result rather than the name is what makes that unrepresentable: there is no
    longer a value you can pass that asserts a room without also asserting it opened.
    """
    decision = decision or lanes.route(cfg, leaf)

    lock_block = ""
    if decision.get("lane") == lanes.SERIALIZED and decision.get("resource"):
        lock_block = LOCK_BLOCK.format(sr=sr_bin(cfg), resource=decision["resource"],
                                       crawler=spawn_record["crawler"])

    chat_block = ""
    if isinstance(chat, str):
        # A caller passing the old bare name is asserting a room it has not checked. Refuse
        # loudly rather than render it: the whole point of the parameter change is that the
        # lying form cannot be expressed, and a string that unpacks by accident would put it
        # back.
        raise TypeError(
            "brief.build takes the provisioning RESULT (channel, opened, detail), not a "
            "channel name — a name is not a room. Use dispatch.open_channel(cfg, record).")
    if chat is not None:
        channel, opened, detail = chat
        if channel and opened:
            # THE RESOLVED ABSOLUTE PATH, never the bare word. `llm_chat` is not on PATH for a
            # consumer who vendors it, and the bare name shipped in every brief: inbound worked
            # (the delivery hooks carry absolute paths), outbound did not, and the half that
            # worked is the half that hid the other. showrunner already has this value and
            # `doctor` already resolves it -- the same argument as `--version` answering from
            # where the code lives: the tool knows, so the tool should say.
            _cli = dispatch.chat_path(cfg, "cli") or "llm_chat"
            chat_block = CHAT_BLOCK.format(channel=channel, crawler=spawn_record["crawler"],
                                           chat_cli=_cli)
        elif channel:
            # SAY IT, DO NOT OMIT IT. Silence here is indistinguishable from chat never having
            # been configured, and the two call for opposite behaviour: with chat off the
            # Crawler was never going to ask anyone anything, while here a room was meant to
            # exist and does not, so every question it would have posted now has to be decided
            # alone and recorded. A Crawler cannot route around a channel it does not know was
            # supposed to be there.
            chat_block = NO_CHAT_BLOCK.format(
                reason=detail or "no reason reported",
                scratch=cfg.abspath(spawn_record["scratch"]))

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
        sr=sr_bin(cfg), chat_block=chat_block, lane_line=lane_line,
        lock_block=lock_block,
        orchestrator_block=orchestrator_block,
        shared_block=shared_block,
        # Absolute for the same reason as scratch, and caught by the same rule the moment it
        # was written: this row is the reader's answer to "where am I", and a relative path
        # there resolves against the tree they are already standing in — naming a directory
        # inside their own worktree that does not exist. It was only ever informational, which
        # is exactly why nobody noticed; the actionable copy below was always absolute.
        worktree=spawn_record["worktree"],
        worktree_abs=spawn_record["worktree"],
        branch=spawn_record["branch"],
        # ABSOLUTE, for exactly the reason `sr_bin` is (#15) — the brief is READ inside a
        # worktree, so a relative path resolves against the Crawler's cwd and not the main
        # checkout. It produced a real scratch directory inside the worktree, and
        # `git worktree remove` destroyed everything in it: the report, the captured evidence,
        # and the artifact cited as `--proof`. A true claim then reads as false, because the
        # file the close gate recorded is gone from the only place anyone looks for it.
        #
        # WORSE, IT ONLY APPEARS AFTER #15 IS ADOPTED. Consumers used to symlink the state dir
        # into the worktree to reach the binary, which incidentally made this relative path
        # resolve to the shared directory. #15 replaced that workaround with an absolute path —
        # correctly — and this stayed relative, so the fix removed what had been masking it.
        scratch=cfg.abspath(spawn_record["scratch"]),
    )


def write(cfg, spawn_record, text):
    path = os.path.join(cfg.abspath(spawn_record["scratch"]), "BRIEF.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    return path
