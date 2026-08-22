"""Start a Crawler as a REAL Claude Code session, with its own hooks. Issue #15.

Until now showrunner prepared the ground and stopped: a worktree, a branch, a scratch dir,
a brief, a lock, a claim — and then a human started the agent by hand. The README said it
"sends a party of Crawlers into separate rooms in parallel", and the documented workflow ran
`plan → route → spawn → integrate` with no step that starts anything. A reader concluded
spawn launched them, which is the correct reading of what was written and not of what ran.

**A subagent was the obvious shortcut and is the wrong one.** A Task subagent shares the
parent's process and its hooks; it is not a session. game_loop's rails are Claude Code hooks
registered in `<project>/.claude/settings.json`, its usage park and watchdog key on a session,
and the model it observes is read from a session transcript. A Crawler that is not a session
has none of that: no commit gate of its own, no Stop gate, no `model.json`, and nothing to
reap when it dies. The whole point of a Crawler is that it survives and polices itself while
nobody is watching, so it has to be the thing the harness was built for.

So each Crawler is `claude -p` in its own worktree, which carries the hooks because
`.claude/settings.json` is TRACKED and `git worktree add` copies tracked files.

THE ORDER HERE IS LOAD-BEARING, and it is the opposite of the obvious one. The session id is
generated and RECORDED BEFORE the process is launched, not read back from it afterwards.
Launching first and recording second leaves a window where a real agent is running in a real
worktree and no record names it: it cannot be reaped, its claim cannot be reclaimed, and the
only evidence it exists is a process nobody is looking for. Recording first can at worst leave
a record of something that never started, which `reconcile` already reports and `reap` already
clears. One failure is invisible and the other is loud, so the order is chosen for which
failure you get, not for which is tidier.
"""

import json
import os
import re
import subprocess
import uuid

from . import campaign, locks
from .util import Refused, boot_token, die, eprint, now, pid_alive, rel, short_session

# MEASURED, AND THE OPPOSITE OF WHAT I FIRST REASONED. This was `acceptEdits`, chosen because
# bypassPermissions "is a wider door than the problem needs". The prediction was wrong: under
# acceptEdits a launched Crawler can edit files and do NOTHING else — every bash call is refused
# for want of a human who is not there, so it cannot run its harness, cannot commit, and cannot
# close its own leaf. The narrow door did not narrow the blast radius; it removed the work.
#
# The reasoning that survives is the second half: game_loop's PreToolUse hooks gate writes,
# commits and deploy verbs REGARDLESS of this setting, and they are the actual rails. This
# only decides whether Claude Code stops to ask a question nobody is present to answer.
# Configurable via dispatch.permission_mode for anyone who wants the prompt back.
DEFAULT_PERMISSION_MODE = "bypassPermissions"


def dispatch_config(cfg):
    return cfg.data.get("dispatch") or {}


def resolve_model(cfg, decision):
    """The model this leaf's routing calls for, or None to inherit whatever claude defaults to.

    Three sources, most specific first, and each one matched EXACTLY. An earlier version fell
    back to "the first rule carrying this lane", which quietly resolves to a rule that did not
    fire — two rules can share a lane, and the wrong model is not an error anywhere downstream,
    it is just a bill. Routing already records WHICH rule matched, so use it.

    Declared here and never enforced: showrunner records what it ASKED for, game_loop observes
    what actually ran, and `reconcile` compares them. Nothing switches a running session.
    """
    dcfg = dispatch_config(cfg)
    rule_name = decision.get("rule")
    for rule in cfg.data.get("lanes") or []:
        if rule.get("name") and rule.get("name") == rule_name and rule.get("model"):
            return rule["model"]
    by_lane = dcfg.get("models_by_lane") or {}
    if decision.get("lane") in by_lane:
        return by_lane[decision["lane"]]
    return dcfg.get("default_model") or None


def channel_for(cfg, record):
    dcfg = dispatch_config(cfg).get("chat") or {}
    if not dcfg.get("enabled"):
        return None
    prefix = dcfg.get("channel_prefix") or cfg.data.get("project_name") or "showrunner"
    return "%s_%s" % (prefix, record["crawler"])


def chat_path(cfg, key):
    """Where the chat tool lives, from config — never a vendored path baked into the source.

    An earlier version hardcoded a private vendoring layout, which made this repo unusable to
    anyone who does not share it and quietly pinned a public project to an internal tool. A
    consumer names their own path or turns chat off; showrunner knows only that a CLI exists.
    """
    raw = (dispatch_config(cfg).get("chat") or {}).get(key)
    if not raw:
        return None
    return raw if os.path.isabs(raw) else os.path.join(cfg.root, raw)


def provision_chat(cfg, record, channel):
    """Install llm_chat into the Crawler's worktree so it can be talked to while it works.

    Its hooks live in `.claude/settings.local.json`, which is deliberately UNTRACKED because
    the command is an absolute path to this machine's checkout — so unlike game_loop's, they
    do not cross into a worktree and each one needs its own install. That is llm_chat being
    right about machine-specific paths, not a gap.

    Returns (ok, detail). A failure here is reported and does NOT abort the dispatch: a Crawler
    that cannot be chatted with is degraded, not broken, and refusing to start real work over a
    missing convenience would be the guard doing more damage than the thing it guards.
    """
    installer = chat_path(cfg, "installer")
    if not installer or not os.path.exists(installer):
        return False, ("no chat installer configured — set dispatch.chat.installer to the path "
                       "of your chat tool's install script, or dispatch.chat.enabled=false")
    wt = cfg.abspath(record["worktree"])
    p = subprocess.run([installer, wt], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        return False, (p.stderr or p.stdout).strip()[:200]
    # Verify rather than trust the exit code: the thing that matters is the hook file existing
    # in the tree the Crawler will run in, which is a fact we can read.
    local = os.path.join(wt, ".claude", "settings.local.json")
    if not os.path.exists(local):
        return False, "installer reported success but wrote no .claude/settings.local.json"

    # AND OPEN THE ROOM. Installing the tooling and naming a channel is not the same as the
    # channel existing — the first version of this did exactly that, printed the channel name
    # in the dispatch report, and left the Crawler unreachable in a room nobody had created.
    # A name is not a room, and reporting one as if it were is the failure this whole repo
    # keeps finding: a claim about a thing that was never made.
    cli = chat_path(cfg, "cli")
    if not cli or not os.path.exists(cli):
        return False, "no chat CLI configured — set dispatch.chat.cli"
    topic = "Crawler %s — leaf %s. The orchestrator reads here." % (record["crawler"],
                                                                   record.get("leaf", "?"))
    p = subprocess.run([cli, "open", channel, "--as", "orchestrator", "--topic", topic],
                       capture_output=True, text=True, timeout=60, cwd=cfg.root)
    out = (p.stdout + p.stderr).lower()
    if p.returncode != 0 and "exists" not in out and "already" not in out:
        return False, "channel not opened: %s" % (p.stderr or p.stdout).strip()[:160]
    return True, channel


def wire_stop_gate(cfg, record):
    """Give the Crawler's own tree a turn-end trigger that runs showrunner's stop gate.

    `stop-gate` refuses a turn-end while this Crawler's leaf is still claimed and open, and it
    has existed since the beginning — presented in llms.txt under "The gates, as hooks", which
    is a claim about wiring that had no path into a Crawler at all. The gate was real and
    unreachable, so a launched Crawler could end its session with its leaf open and nothing
    would stop it. A gate nobody can wire is documentation.

    Written into the WORKTREE's harness triggers, merged rather than replaced, because the
    harness's trigger file is its own and a project may already have one. Reports rather than
    raises: a Crawler that starts without a turn-end gate is degraded, not broken, and refusing
    to dispatch over it would be the guard doing more damage than the gap.
    """
    wt = cfg.abspath(record["worktree"])
    tf = os.path.join(wt, ".game_loop", "triggers.json")
    if not os.path.isdir(os.path.dirname(tf)):
        return False, "no harness dir in the worktree — nothing to wire a turn-end gate into"
    from .brief import sr_bin
    sr = sr_bin(cfg)   # resolved, not assumed — the same dead-path bug lived here too
    # NO timeout_sec, deliberately. This ran with an explicit 30, and the harness honours an
    # explicit value uncapped — so showrunner was overriding a 10s default the layer below had
    # chosen for a reason it stated: this trigger runs on EVERY turn-end, and a timeout there
    # fails open, which is a turn ending unchecked rather than a refusal. How long a turn-end
    # may be held is the harness's concept and its budget to spend. Omitting the key means a
    # retune there reaches every Crawler here without anyone remembering to follow it.
    # --leaf IS THE WHOLE POINT OF WRITING IT PER TREE. Without it the gate asks "is any leaf
    # open in this campaign" and every Crawler in a wave is gated on every sibling, so with N
    # dispatched, N-1 are structurally guaranteed to be refused — and a headless Crawler has no
    # next turn in which to act on the refusal. This file already writes an absolute path here;
    # naming the leaf costs nothing and is the exact answer to "whose turn-end is this".
    entry = {"name": "showrunner-stop-gate",
             "command": "%s stop-gate --leaf %s" % (sr, record["leaf"])}
    try:
        data = {}
        if os.path.exists(tf):
            with open(tf) as fh:
                data = json.load(fh) or {}
        stops = [t for t in (data.get("stop") or []) if t.get("name") != entry["name"]]
        data["stop"] = stops + [entry]
        with open(tf, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
    except (OSError, ValueError) as exc:
        return False, "could not wire the turn-end gate: %s" % exc
    return True, entry["command"]


def build_command(cfg, record, model, session_id, prompt):
    """The argv for one Crawler. Separated from launching so it can be asserted without
    starting a real session, which costs money and a worktree."""
    dcfg = dispatch_config(cfg)
    cmd = ["claude", "-p", prompt,
           "--session-id", session_id,
           "--permission-mode", dcfg.get("permission_mode") or DEFAULT_PERMISSION_MODE,
           "-n", record["crawler"]]
    if model:
        cmd += ["--model", model]
    for extra in dcfg.get("claude_args") or []:
        cmd.append(extra)
    return cmd


def new_session_id():
    """Generated by the CALLER, before the claim is taken.

    The claim, the campaign record and the process must all name the same session or the
    orchestrator has two ideas of who is doing the work. Claude Code accepts `--session-id`,
    so this is knowable up front rather than read back from a process that may already have
    died — which is the only version of this that has no window in it.
    """
    return str(uuid.uuid4())


def launch(cfg, record, decision, brief, session_id, dry_run=False):
    """Record first, then start. See the module docstring for why that order is not cosmetic."""
    wt = cfg.abspath(record["worktree"])
    if not os.path.isdir(wt):
        raise Refused("worktree %s does not exist — spawn it before dispatching" % wt)

    model = resolve_model(cfg, decision)
    channel = channel_for(cfg, record)
    cmd = build_command(cfg, record, model, session_id, brief)

    if dry_run:
        return {"session": session_id, "model": model, "channel": channel,
                "cmd": cmd, "launched": False, "why": "dry run"}

    # The record names a session that does not exist yet. That is the intended window: a
    # record with no process is visible to reconcile; a process with no record is not.
    campaign.set_state(cfg, record["crawler"], "dispatching",
                       session=session_id, model_declared=model, channel=channel)

    gate_ok, gate_detail = wire_stop_gate(cfg, record)

    chat_ok, chat_detail = (False, "disabled")
    if channel:
        chat_ok, chat_detail = provision_chat(cfg, record, channel)

    log = os.path.join(cfg.abspath(record["scratch"]), "session.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    try:
        with open(log, "ab") as fh:
            proc = subprocess.Popen(cmd, cwd=wt, stdout=fh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        campaign.set_state(cfg, record["crawler"], "dispatch-failed", error=str(exc))
        raise Refused("could not start a session for %s: %s" % (record["crawler"], exc))

    campaign.set_state(cfg, record["crawler"], "running", pid=proc.pid, dispatched_at=now())
    return {"session": session_id, "model": model, "channel": channel if chat_ok else None,
            "chat": chat_detail, "cmd": cmd, "pid": proc.pid, "log": rel(log, cfg.root),
            "stop_gate": gate_detail if gate_ok else None, "launched": True}


def observed_models(cfg, entry):
    """What game_loop saw this session actually run on, read from the CRAWLER's checkout.

    Returns None when there is nothing to read, and the caller must treat that as UNKNOWN
    rather than as agreement — game_loop warned about exactly this: `changed: false` means the
    model did not move, never that it matched what was asked for, and an absent file means
    nobody looked. An absence here is not evidence of a match.
    """
    session = entry.get("session")
    if not session:
        return None
    path = os.path.join(cfg.abspath(entry["worktree"]), ".game_loop", "sessions", session,
                        "model.json")
    try:
        with open(path) as fh:
            import json
            return json.load(fh)
    except (OSError, ValueError):
        return None


def models_agree(declared, observed):
    """Does an ALIAS name the model that actually ran?

    `--model` takes either an alias ("sonnet") or a full id ("claude-sonnet-5"); the transcript
    only ever records the full id. Comparing the two as strings reports every correctly
    dispatched Crawler as a mismatch — a false positive, which is the expensive direction: a
    real ghost costs somebody four seconds, and a check that cries wolf on correct runs is one
    nobody reads by Thursday. Found by the test rather than in production, but only because the
    fixture used an alias, which is what anyone would actually type.
    """
    if not declared or not observed:
        return False
    if declared == observed:
        return True
    return observed.startswith("claude-%s-" % declared)


# How long a finished Crawler's process is left alone before it counts as LINGERING. A
# Crawler closes its own leaf from inside its own session, so at the instant the leaf closes
# the process is still mid-call: writing its last commit, flushing its transcript, running the
# Stop gate. Terminating there truncates the work it just certified. The grace window is the
# difference between "spun down" and "killed while finishing".
# WHY AN INVENTED NUMBER IS RIGHT HERE, when it is wrong for freshness. `doctor` refuses to
# pick a "too old" tolerance for the waiting journal and asserts a RELATION instead — has the
# probe answered since the thing that would have changed its answer — because that number would
# have produced a VERDICT about an event showrunner does not schedule.
#
# This one produces no verdict. It budgets a RISK, and the costs are asymmetric and known: too
# short truncates work a Crawler has already certified; too long leaves a dead process for one
# more sweep. There is no relation to assert, because the question is not "has anything happened
# since" — a process mid-flush between two writes has, by that measure, done nothing.
#
# So the rule is not "never invent a number". It is: an invented number that yields a FACT is a
# fabrication; one that bounds a DESTRUCTIVE action toward caution is a budget, and a budget is
# defensible as long as the direction of error is stated. Stated: this errs toward leaving a
# process alone.
LINGER_GRACE_SECONDS = 120

ERROR_MARKERS = ("Execution error", "API Error", "Invalid API key", "rate limit")


def close_channel(cfg, entry):
    """Close the Crawler's room. Safe at any point, so it happens as soon as the leaf closes.

    One room per Crawler is the right shape while it works and a leak the moment it stops:
    under fan-out the rooms accumulate one per leaf forever, and a list of dead rooms is how
    a channel list stops being readable. Leaving as the only member closes it.

    Idempotent by construction — a room already closed, or never opened, is success. Returns
    (ok, detail) and never raises: spin-down must not be the thing that fails a close.
    """
    channel = entry.get("channel")
    if not channel:
        return True, "no channel"
    cli = chat_path(cfg, "cli")
    if not cli or not os.path.exists(cli):
        return False, "no chat CLI configured — room %s left open" % channel
    try:
        p = subprocess.run([cli, "leave", channel, "--as", "orchestrator"],
                           capture_output=True, text=True, timeout=60, cwd=cfg.root)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "could not close %s: %s" % (channel, exc)
    out = (p.stdout + p.stderr).lower()
    if p.returncode == 0 or "closed" in out or "no such" in out or "not a member" in out:
        return True, "closed %s" % channel
    return False, (p.stderr or p.stdout).strip()[:160]


def lingering(entry, grace=LINGER_GRACE_SECONDS):
    """Is this Crawler's process still running well after its work ended?

    Two facts, both required. The leaf being finished is what makes stopping SAFE — the work
    is done by definition, so nothing is lost. The grace window is what makes it CORRECT,
    because the moment of closing is the moment the process is busiest.

    Returns None when it is not lingering, so the caller cannot mistake "no opinion" for
    "safe to kill" — an absence here would otherwise read as permission.
    """
    # A PID IS ONLY MEANINGFUL INSIDE THE BOOT THAT ISSUED IT. `pid_alive` in a later boot
    # answers "is some process 36042 alive", not "is my Crawler alive" — and the OS will have
    # reused that number for something that has nothing to do with this campaign. Every other
    # liveness question here already scopes by boot token (`campaign.live`); this one did not,
    # and it is the only one that ACTS, so the consequence of getting it wrong is a SIGTERM
    # sent to a stranger's process rather than a wrong answer on a report.
    #
    # Note which direction the error runs. Resolving an identifier in the wrong namespace
    # usually yields a false NEGATIVE — absent, so you conclude nothing happened. Here it
    # yields a false POSITIVE, and a false positive that is wired to an action is the shape
    # worth being frightened of.
    if entry.get("boot") and entry["boot"] != boot_token():
        return None
    pid = entry.get("pid")
    if not pid or not pid_alive(pid):
        return None
    finished = entry.get("finished_at")
    if not finished:
        return None
    try:
        age = now() - float(finished)
    except (TypeError, ValueError):
        return None
    if age < grace:
        return None
    return {"pid": pid, "seconds_since_finished": int(age)}


def session_health(cfg, entry):
    """What the session's own output says, because a live PID is not a working agent.

    Measured, not anticipated: a dispatched Crawler printed `Execution error`, stopped doing
    anything, and kept its process. `reconcile` reported it as `running / alive` — correct
    about the PID and useless about the work. Liveness is deliberately a recomputable fact
    (INV: a heuristic there silences a watchdog that exists to catch a genuinely wedged run),
    so this does not replace it. It reports a SECOND fact beside it, and lets the human tell
    "working" from "sitting on an error" instead of inferring one from the other.

    Returns None when there is no log to read — an absence, reported as one, never as health.
    """
    scratch = entry.get("scratch")
    if not scratch:
        return None
    log = os.path.join(cfg.abspath(scratch), "session.log")
    try:
        size = os.path.getsize(log)
        with open(log, errors="ignore") as fh:
            text = fh.read()[-4000:]
    except OSError:
        return None
    hits = [m for m in ERROR_MARKERS if m.lower() in text.lower()]
    return {"log": rel(log, cfg.root), "bytes": size, "errors": hits,
            "verdict": "errored" if hits else ("quiet" if size == 0 else "producing")}


def model_finding(cfg, entry):
    """Compare what was ASKED for against what RAN. Advisory, never blocking.

    A model mismatch is a COST failure rather than a correctness one: an Opus-priced Crawler
    doing Sonnet work produces perfectly good output, which is precisely why nothing notices
    and why it can run for a week. So this is loud and cheap and never refuses anything.
    """
    declared = entry.get("model_declared")
    obs = observed_models(cfg, entry)
    if obs is None:
        return {"verdict": "unknown", "declared": declared, "observed": None,
                "why": "no model.json for this session — nobody looked, which is not a match"}
    models = obs.get("models") or []
    if obs.get("changed"):
        return {"verdict": "changed-mid-run", "declared": declared, "observed": models,
                "why": "the model changed under the run: %s. A fallback degrades silently and "
                       "the output still looks fine" % " → ".join(models)}
    if declared and models and not models_agree(declared, models[0]):
        return {"verdict": "mismatch", "declared": declared, "observed": models,
                "why": "dispatched as %s, ran as %s" % (declared, models[0])}
    return {"verdict": "match" if declared else "undeclared",
            "declared": declared, "observed": models}


# ---------------------------------------------------------------- dispatch guard
# THE CHEAP PATH HAS NO GATE ON IT (#37). `spawn --launch` is the correct way to start a Crawler;
# the competing path is ONE Bash call —
#
#     GAME_LOOP_SESSION=lane-x nohup claude -p --permission-mode bypassPermissions \
#       --model sonnet "$(cat BRIEF.md)" < /dev/null > /tmp/lane-x.out 2>&1 &
#
# — which was used 42 consecutive times in one real run. It is strictly worse in every way
# showrunner exists to fix: no worktree, no lease, no claim a reaper can reclaim, no leaf-scoped
# stop gate, no room. And it is one line, available immediately, with nothing in the way.
#
# THE PROTOTYPE THAT MISSED IT IS THE LESSON. A consumer's guard registered its PreToolUse matcher
# on `Agent`, so it guarded the in-process subagent tool while every real dispatch went out through
# `Bash`. The guard built specifically to stop flat dispatch was blind to the mechanism actually
# used, 42 times, and reported nothing. A guard matched on the wrong tool is indistinguishable
# from a world with nothing to guard.
_CLAUDE_CALL = re.compile(r"(?:^|[;&|]|\s)(?:[^\s;|&]*/)?claude\s", re.I)
_PRINT_FLAG = re.compile(r"(?:^|\s)(?:-p|--print)(?:\s|=|$)")


def dispatch_guard(cfg, session=None, tool=None, tool_input=None):
    """PreToolUse verdict for a raw `claude -p` dispatch: (allow, message, detail).

    DENIES ON EXACTLY ONE CONDITION — a Bash command that starts a headless `claude` and does not
    go through showrunner, made by a session whose ROLE may not create anything. Every other
    state allows, and none of them is an oversight:

      no roles configured   showrunner has no policy to enforce. It never learns what a role
                            MEANS (#40), and inventing one here would be exactly that: a consumer
                            who has not written roles would find their own dispatches refused by
                            a rule nobody wrote.
      the role may create   the policy permits it; this is not a ban on `claude`, it is a check
                            that the session is allowed to dispatch at all.
      it mentions showrunner  `spawn --launch` builds its own command line, so the tool's own
                            dispatch must pass — a guard that denies its own remedy is one that
                            gets switched off (INV5).
      anything unknown      unparseable payload, no session, no roles file, a bad regex. A
                            PreToolUse that hard-fails on its own plumbing blocks the write that
                            would repair it, so it allows AND SAYS the call went unchecked.

    WHAT IT CANNOT SEE, stated rather than implied: a command built from a shell variable, a
    wrapper script that calls `claude` internally, or a dispatch through any tool that is not
    Bash. It catches the honest one-liner — which is the one that was used 42 times — and the
    LEASE is what protects a tree from whatever gets in anyway.
    """
    tool_input = tool_input or {}
    if (tool or "") != "Bash":
        return True, "", {"checked": False, "why": "not a Bash call"}
    command = tool_input.get("command") or ""
    if not command:
        return True, "", {"checked": False, "why": "no command"}

    if not (_CLAUDE_CALL.search(command) and _PRINT_FLAG.search(command)):
        return True, "", {"checked": True, "why": "not a headless claude dispatch"}
    if "showrunner" in command:
        return True, "", {"checked": True, "why": "routed through showrunner"}

    from . import roles as _roles
    defs, problems = _roles.spec(cfg)
    if problems:
        return True, ("showrunner: DISPATCH WENT UNCHECKED — the role definitions could not be "
                      "read (%s), so this raw `claude -p` was ALLOWED without being checked "
                      "against any policy." % problems[0]), {"checked": False}
    if not defs:
        return True, "", {"checked": False, "why": "no roles configured — no policy to enforce"}

    role, seat = resolved_role(cfg, session, defs)
    if (defs.get(role) or {}).get("may_create"):
        return True, "", {"checked": True, "role": role, "why": "role may create"}

    return False, DISPATCH_DENIED.format(
        role=role, session=short_session(session) if session else "?",
        seat=seat or "unresolved"), {"checked": True, "role": role}


def resolved_role(cfg, session, defs=None):
    """(role, how) for this session. Assignment first, then a held claim, then the fallback.

    ASSIGNMENT WINS, because a session holding one had its role decided before it existed and
    must not be able to claim a different one (#40). Nothing writes assignments yet — `spawn`
    does not record a role — so today this resolves through claims and the fallback, and saying
    that plainly is better than implying a path that has no writer.
    """
    from . import roles as _roles
    defs = defs if defs is not None else _roles.spec(cfg)[0]
    for entry in _roles.roster(cfg):
        holder = entry.get("holder") or {}
        if entry.get("state") == locks.HELD and holder.get("session") == session:
            return holder.get("role") or entry["role"], "claimed"
    return _roles.FALLBACK, "fallback"


DISPATCH_DENIED = """\
showrunner: DENIED — a raw `claude -p` dispatch, from a session whose role may not create one.

  role     {role}  ({seat})
  session  {session}

That one line skips everything showrunner exists to provide: no worktree, so two agents edit one
tree; no lease, so nothing refuses the second; no claim, so a reaper cannot tell the work was
abandoned; no leaf-scoped stop gate; and no room, so nobody can reach it. In one real run this
path was taken 42 times and every guarantee was absent for all 42.

  Dispatch through the tool instead:
      showrunner spawn <leaf> --actor <name> --launch

  Or, if this session SHOULD be able to dispatch, give its role a `may_create` naming what it
  may start. Roles are yours to define — showrunner checks the shape and never the meaning."""
