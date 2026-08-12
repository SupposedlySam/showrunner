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

import os
import subprocess
import uuid

from . import campaign
from .util import Refused, die, eprint, now, pid_alive, rel

# game_loop's hooks gate writes, commits and deploy verbs regardless of this, and they are the
# actual rails. This only decides whether Claude Code stops to ASK, which no one is present to
# answer under fan-out. `bypassPermissions` is deliberately not the default: it is a wider door
# than the problem needs, and the guard being the real protection is not a reason to open it.
DEFAULT_PERMISSION_MODE = "acceptEdits"


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
            "launched": True}


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
