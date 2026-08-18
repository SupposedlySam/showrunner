"""One session per worktree — the holder is a live process, not a note in a record.

A worktree has an *owner* on paper and no *holder* in fact. `campaign.json` records which
Crawler was spawned into a tree, and nothing consults that at the moment a second session
opens the directory and starts editing. The only thing standing in the way today is that
`worktree.crawler_name` is deterministic, so a re-spawn of the same leaf collides on the path
— which refuses the ORCHESTRATOR'S own duplicate and says nothing to a stranger. A coincidence
of naming is not a mutex.

**Built on `locks.Lock`, not beside it.** The primitive is already right and already carries
the four states that matter — an atomic `mkdir`, a holder that is a live PID, a boot token so
a claim from a previous boot cannot be mistaken for a running one, and `UNREADABLE` refusing
to reclaim what it cannot prove dead. Re-implementing any of that here would produce a second
mutex that drifts from the first, and the failure mode of a drifted mutex is silence.

What this module adds is only what a *tree* needs that a *resource* does not:

* **A name derived from the tree**, so the lock root — one absolute path shared by every
  worktree, validated at config load — holds one entry per tree.
* **`pid_basis`**, because a lease's PID is discovered rather than handed over (WL-01: no PID
  reaches a hook), and a lease whose liveness rests on a weaker fact has to say so.
* **A session id beside the PID.** The PID answers "alive?"; the session id answers "who?".
  Re-entry by the same session is not a hijack, and only the session id can tell them apart.

**What a lease does NOT cover**, because a guard that overstates its reach buys false
confidence: it is a fact that only Claude Code hooks consult. A human in `vim`, a shell
script, a `git checkout` — none of them ask, and the lease does not stop them. And it protects
the *tree*: two sessions in different trees still share the git common dir, the lock root, the
graph and the campaign record. `worktree.audit_shared` enumerates that, and callers should
print it rather than re-derive it.
"""

import os
import re
import shutil

from . import locks
from .util import now, session_pid, short_session, stamp

PREFIX = "worktree:"
INTERACTIVE = "interactive"


def lease_name(tree):
    return "%s%s" % (PREFIX, tree)


def tree_for(cfg, path=None):
    """The managed worktree containing `path`, or None when it is not in one.

    Resolved against `worktree_root` rather than against "is this a linked worktree", because
    a lease covers the trees this orchestrator PLACED and nothing else. A linked worktree
    somebody made by hand is not one it put a Crawler in, and claiming authority over it would
    be a guard inventing its own jurisdiction.
    """
    root = cfg.worktree_root
    if not root:
        return None
    root = os.path.realpath(root)
    target = os.path.realpath(path or os.getcwd())
    if target == root or not target.startswith(root + os.sep):
        return None
    rest = target[len(root) + 1:].split(os.sep)
    return rest[0] if rest and rest[0] else None


class Lease:
    """A worktree lease. Thin on purpose — the mutex is `locks.Lock`."""

    def __init__(self, cfg, tree):
        self.cfg = cfg
        self.tree = tree
        self.lock = locks.Lock(cfg.lock_root, lease_name(tree))

    # -- introspection -----------------------------------------------------
    def state(self):
        """(state, holder) straight from the lock. Never reinterpreted here.

        A pass-through, and it must stay one. Any 'helpful' collapsing at this layer — treating
        UNREADABLE as free, say, or aging a HELD lease out — would be this module quietly
        holding a different liveness rule than the one the device lane enforces, and two layers
        disagreeing about the rules silently is the failure BOUNDARY.md exists for.

        The holder that comes back now CARRIES `pid_basis`, which it did not when this was
        written: `Lock.acquire` had a write side with no read side, so a caller taking the
        holder from here got the basis missing and printed "?" in the hijack report — the one
        place the field exists for. Fixed at the lock (d89ab28), which is the right layer; the
        local workaround that reached through `Lock._read` is deleted rather than kept beside
        it, because two ways to get the same field is how they start disagreeing.
        """
        return self.lock.state()

    def holder(self):
        return self.lock.holder()

    def held_by_other(self, session):
        """True only when a DIFFERENT session holds this tree and is alive.

        The narrowest of the four states, and the only one that licenses refusing somebody.
        Re-entry by the same session is not a hijack; a stale lease is not a hijack; an
        unreadable one is not adjudicable from here at all.
        """
        state, h = self.state()
        if state != locks.HELD or not h:
            return False, h
        if session and h.get("session") == session:
            return False, h
        return True, h

    # -- mutation ----------------------------------------------------------
    def acquire(self, session, who=INTERACTIVE, pid=None, basis=None, wait=0):
        """Take the lease for this session. Returns (ok, holder).

        The PID is discovered when not supplied — a dispatched Crawler has one recorded at
        spawn, an interactive session does not, and `session_pid` says which it got.
        """
        if pid is None:
            pid, basis = session_pid()
        if pid is None:
            # No process to point at means no liveness, and a lease with no liveness is a note
            # that outlives whoever wrote it — exactly the campaign-record situation this
            # module exists to replace. Refuse to take it rather than take a hollow one.
            return False, {"why": "no session process could be resolved (basis %r), so this "
                                  "lease would have no liveness at all" % (basis or "unknown")}
        extra = {"pid_basis": basis or "supplied", "tree": self.tree}
        ok = self.lock.acquire(pid, who, session=session, wait=wait, extra=extra)
        return ok, self.holder()

    def release(self, session=None, force=False):
        """Release. Ownership is checked by SESSION, not by pid.

        The pid was discovered by walking an ancestry, so the process releasing may legitimately
        be a different child of the same session than the one that acquired. Keying release on
        the pid would refuse the true owner and hand `--force` to routine use — and an escape
        hatch reached routinely stops being an escape hatch.

        A MISSING SESSION IS NOT A MATCHING ONE. The check used to read `if not force and
        session and ...`, so calling `release()` with no session at all skipped ownership
        entirely and took anybody's lease — the caller who knows least got the most authority,
        which is the wrong way round for the one mutation on a mutex. `force` is now the only
        way past the check, and it has to be said out loud.
        """
        h = self.lock.holder()
        if not h:
            return False, "not held"
        if not force and h.get("session") != session:
            return False, ("held by session %s, not you (%s) — pass force only if you know that "
                           "session is gone" % (h.get("session") or "?", session or "no session"))
        self.lock.release(force=True)
        return True, "released"


REMEDIES = """\
  1. Your own tree, same starting point — the usual answer:
       {sr} worktree fork --from {tree}
     New worktree and branch off the same base commit.

  2. Read-only. Stay, read, do not write. Reads are never guarded.

  3. Take it over — NOT BUILT YET (WL-06). Said plainly rather than printed as though it
     works: a remedy naming a command that does not exist is worse than no remedy, and this
     project has shipped that twice. Until it lands, a holder you know is gone is cleared with
     `{sr} lease status` to confirm the state, then by hand.

  4. Leave."""

# ONE list of remedies, formatted into both the prompt and the refusal. They were written as
# two copies and the second sentence of each disagreed within a day — INV: two layers must
# never disagree about the rules silently, and a remedy list is a rule the reader acts on.
OPTIONS = """\
This worktree is held by another live session.

  holder   {who}  (session {session}, pid {pid}, alive, this boot)
  since    {since}
  basis    {basis}

Your writes here will be DENIED while that holds: `worktree guard` is registered on
PreToolUse and refuses exactly this case. Pick one:

""" + REMEDIES

DENIED = """\
showrunner: DENIED — {what} inside worktree {tree}, which another LIVE session holds.

  holder   {who}  (session {session}, pid {pid}, alive, this boot)
  since    {since}
  basis    {basis}

Two sessions editing one tree lose work silently — the second write wins and neither session
is told. This is the lease refusing, not a permissions error, and retrying will not help.

""" + REMEDIES


def enter(cfg, session, path=None, who=None):
    """SessionStart: work out who holds this tree and say so. Never blocks, never denies.

    SessionStart CANNOT block a session — that is a fact about the event, not a decision made
    here — so this is where the *prompt* lives and WL-05 is where the *enforcement* will. The
    two are kept apart on purpose: a prompt that reads like a refusal teaches a reader that
    refusals are advisory, and then the real one gets argued with.

    Returns (verdict, detail). Verdicts: 'not-a-worktree', 'acquired', 'own', 'hijack',
    'reclaimed', 'unreadable', 'no-liveness'.
    """
    from . import events

    tree = tree_for(cfg, path)
    if not tree:
        # Silent. A session in the main checkout is the ordinary case, and an orchestrator that
        # narrates every non-event trains its reader to skim the one that matters.
        return "not-a-worktree", {}

    lease = Lease(cfg, tree)
    # SETTLED, for the same reason `Lock.acquire` uses it: this ACTS on the answer. An
    # UNREADABLE verdict here is a dead end that only a human can clear, and the commonest way
    # to see one is to read a lock another session is halfway through writing — which two
    # Crawlers starting in the same tree produce every time. A transient must not be handed to
    # somebody as a state requiring manual repair.
    state, h = lease.lock.settled_state()

    if state == locks.UNREADABLE:
        # Not adjudicable from here, and deliberately not "reclaim it and carry on". A partial
        # write by a LIVE holder reads exactly like a dead one; only a human can find out which.
        events.emit(cfg, "lease.unreadable", {"tree": tree, "session": session})
        return "unreadable", {"tree": tree, "holder": h or {}}

    if state == locks.HELD:
        if h and h.get("session") == session:
            return "own", {"tree": tree, "holder": h}
        # THE EVENT THIS WHOLE LEAF EXISTS TO PRODUCE. WL-05 may not build anything that
        # refuses until a hijack has actually been observed — no gate without a logged failure —
        # and a verdict printed to a terminal nobody kept is not an observation. The journal is
        # where it becomes one.
        events.emit(cfg, "lease.hijack", {
            "tree": tree,
            "intruder_session": session,
            "holder_session": h.get("session") if h else None,
            "holder_pid": h.get("pid") if h else None,
            "holder": h.get("who") if h else None,
        })
        return "hijack", {"tree": tree, "holder": h or {}}

    got, holder = lease.acquire(session, who=who or INTERACTIVE)
    if got and state == locks.STALE:
        # EMITTED AFTER THE ACQUIRE, not before it. This fired first, so an acquire that then
        # failed left the journal recording a reclaim that never happened — a viewer would show
        # a tree handed from a dead session to a live one while it was in fact still held.
        events.emit(cfg, "lease.reclaimed", {
            "tree": tree, "session": session,
            "dead_session": h.get("session") if h else None,
            "dead_pid": h.get("pid") if h else None,
        })
    if not got:
        # WHY IT FAILED IS RE-DERIVED, NOT INFERRED FROM THE BOOLEAN. `acquire` returns False
        # for two unrelated reasons — no resolvable pid, and losing the atomic mkdir to a
        # session that got there first — and this reported both as "no session process could be
        # resolved… this tree is unprotected". In the race that is the exact opposite of the
        # truth: somebody else holds it, they are live, and the reader was told nothing does.
        # The window is real and narrow — between `state()` reading FREE above and the mkdir
        # here — which is the same fan-out shape two Crawlers starting in one tree produce.
        hijack, other = lease.held_by_other(session)
        if hijack:
            events.emit(cfg, "lease.hijack", {
                "tree": tree,
                "intruder_session": session,
                "holder_session": (other or {}).get("session"),
                "holder_pid": (other or {}).get("pid"),
                "holder": (other or {}).get("who"),
                "raced": True,
            })
            return "hijack", {"tree": tree, "holder": other or {}}
        return "no-liveness", {"tree": tree, "holder": holder or {}}
    verdict = "reclaimed" if state == locks.STALE else "acquired"
    events.emit(cfg, "lease.acquired", {"tree": tree, "session": session,
                                        "basis": (holder or {}).get("pid_basis")})
    return verdict, {"tree": tree, "holder": holder or {}, "previous": h or {}}


# The carve-out, spelled tightly. `showrunner worktree fork` is the FIRST remedy the refusal
# prints, and a guard that denies its own remedy is a guard that gets switched off (INV5) —
# so these verbs pass even inside a tree somebody else holds.
#
# Tight on purpose, in the direction that fails CLOSED. `rm -rf x && showrunner worktree fork`
# is not a fork, and a carve-out that matched it would be a hole shaped exactly like the thing
# being guarded. So anything that could introduce a second command — a separator, a
# substitution, a newline — disqualifies the whole string, and the operator runs the verb on
# its own. Being refused once and retyping it plainly is the cost; the alternative is a
# bypass anybody can find by appending `&&`.
#
# THE FIRST VERSION OF THIS SAID ALL OF THAT AND LISTED ONLY THE SEPARATORS. `;`, `&`, `|`, a
# newline and `$(` were disqualifying; `>` and `<` were not, and both of these were allowed:
#
#     showrunner worktree list <(rm -rf ...)        the substitution runs whatever the outer
#                                                   command does, and bash runs it FIRST
#     showrunner worktree status > <somebody's tree>/src/main.py    truncates it
#
# The carve-out is an UNCONDITIONAL allow checked before any tree is resolved, so either one
# was a write into a tree a live session holds, through the one door the guard leaves open.
# A redirection IS a write — which is the thing being guarded — and a process substitution IS
# a second command, so both belong in a rule whose stated test is "could this introduce a
# second command or a write". Neither is spellable in a real showrunner invocation.
_OWN_VERB = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z_0-9]*=\S*\s+)*"    # leading FOO=bar
                       r"(?:[^\s;|&]*/)?showrunner\s+(?:worktree|lease)\b")
_CHAINED = re.compile(r"[;&|<>\n]|\$\(|`")


def own_command(command):
    """True when `command` is a showrunner worktree/lease invocation AND NOTHING ELSE.

    Both halves matter. Matching the verb alone would pass any command that mentions it
    anywhere; requiring the whole string to be one simple command is what makes "it starts
    with our binary" mean "it IS our binary".
    """
    if not command or _CHAINED.search(command):
        return False
    return bool(_OWN_VERB.match(command))


def guard(cfg, session, tool=None, tool_input=None, cwd=None, sr=None):
    """PreToolUse verdict: (allow, message, detail). The teeth `enter` deliberately lacks.

    Denies on exactly one condition — `held_by_other` — which is the narrowest of the four
    states and the only one that licenses refusing somebody. FREE, STALE and UNREADABLE all
    ALLOW, and that is not an oversight in any of the three: a free tree has no holder to
    protect, a stale one's holder is proved dead, and an UNREADABLE one cannot be adjudicated
    from here at all. Turning "I cannot tell" into a refusal would wedge a tree on a partial
    write, which is the failure `locks.py` refuses to make and this must not re-make one layer
    up (INV: two layers must never disagree about the rules silently).

    **WHAT IT LOOKS AT, and what that misses.** The tree of every path the payload names, plus
    the tree the session is standing in. So a `Bash` command that names an absolute path into
    somebody's tree from OUTSIDE it is NOT caught — the command string is not parsed for paths,
    deliberately, because a path built from a shell variable is exactly the blindness
    game_loop's own write guard names rather than pretends to cover. The lease stops a session
    that is WORKING in a tree, not every conceivable route into it.

    **An unidentifiable session ALLOWS, loudly.** With no session id there is no way to tell
    the holder from an intruder, and denying would refuse the holder's own writes — the guard
    blocking the work it was taken out for. So it allows and says the guard did not run, on
    the same posture as every other degraded guard here: fail open, never in silence.
    """
    tool_input = tool_input or {}
    command = tool_input.get("command") if (tool or "") == "Bash" else None

    if own_command(command):
        return True, ("allow: showrunner's own worktree/lease verb — the refusal's first "
                      "remedy is `worktree fork`, and a guard that denies its own remedy is "
                      "one that gets switched off"), {"carve_out": "own-verb"}

    targets = [tool_input[k] for k in ("file_path", "notebook_path", "path")
               if isinstance(tool_input.get(k), str) and tool_input.get(k)]
    if cwd:
        targets.append(cwd)

    trees = []
    for target in targets:
        tree = tree_for(cfg, target)
        if tree and tree not in trees:
            trees.append(tree)

    if not trees:
        # The main checkout and everything outside the managed worktree root. Stated as a
        # LIMIT rather than left to look like coverage: `campaign.integrate` serialises the
        # main checkout with its own file lock, and a second answer here would be a rule in
        # two places. A lease covers trees this orchestrator PLACED and nothing else.
        return True, "allow: not inside a managed worktree", {"trees": []}

    if not session:
        return True, ("showrunner: THE WORKTREE GUARD DID NOT RUN — no session id reached it, "
                      "so it cannot tell the holder of %s from an intruder and will not refuse "
                      "the holder's own writes. This tree is UNGUARDED for this call; that is "
                      "said out loud rather than left to look like a pass."
                      % ", ".join(trees)), {"trees": trees, "degraded": "no-session"}

    for tree in trees:
        hijack, h = Lease(cfg, tree).held_by_other(session)
        if not hijack:
            continue
        h = h or {}
        if sr is None:
            from .brief import sr_bin
            sr = sr_bin(cfg)
        what = ("this command runs" if command else
                "this write lands" if targets else "this call acts")
        return False, DENIED.format(
            what=what, tree=tree, sr=sr,
            who=h.get("who") or "?", session=short_session(h.get("session")),
            pid=h.get("pid"), since=stamp(h.get("ts")),
            basis=h.get("pid_basis") or "unrecorded"), {"tree": tree, "holder": h}

    return True, "allow: %s held by nobody else" % ", ".join(trees), {"trees": trees}


GUARD_SHIM = os.path.join(".showrunner", "hooks", "worktree-guard.sh")
GUARD_HOOKS_DIR = os.path.join(".showrunner", "hooks")
GUARD_TOOLS = ("Write", "Edit", "NotebookEdit", "Bash")


def provision_hooks(cfg, worktree_path):
    """Copy showrunner's own hook shims into a fresh worktree. Returns [] or [action].

    THE ONLY REMEDY THIS OFFERED WAS ONE SOME INSTALLS CANNOT TAKE (#31). `git worktree add`
    carries TRACKED files only, so an untracked shim is present in the main checkout and absent
    in every worktree — the one place the guard exists to run — and `doctor` said so and told
    the reader to commit it. In a shared team repo where `.showrunner/` is in
    `.git/info/exclude` on purpose, committing it is exactly the thing that must not happen.
    The diagnosis was right and the remedy was unavailable.

    SHOWRUNNER HAD ALREADY SOLVED THIS SHAPE — for the other harness. `harness.provision` copies
    game_loop into each worktree for precisely this reason, and it is the arrangement
    showrunner's own docs tell a consumer to configure. Its own hooks were not covered by it,
    which is one mechanism with two answers depending on whose files it is.

    So the shims are provisioned, not tracked. Tracking still works and is still the simpler
    thing when a repo allows it: a worktree that already carries the file is left alone, and
    this reports nothing.
    """
    from . import worktree as W

    src = os.path.join(cfg.root, GUARD_HOOKS_DIR)
    if not os.path.isdir(src):
        return []
    dst = os.path.join(worktree_path, GUARD_HOOKS_DIR)
    copied = []
    for name in sorted(os.listdir(src)):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if not os.path.isfile(s) or os.path.exists(d):
            # Already there means git carried it — the file is TRACKED, which is the other
            # supported arrangement and needs nothing from here. Overwriting it with the working
            # copy would also make a spawned tree differ from a hand-made one for no stated
            # reason.
            continue
        os.makedirs(dst, exist_ok=True)
        shutil.copy2(s, d)
        os.chmod(d, 0o755)   # the mode is the difference between a guard and an inert file
        copied.append(name)
    if not copied:
        return []

    # AND THEN VERIFY WHAT WE JUST CREATED, which is this repo's own rule arriving on its own
    # code: a file that is neither tracked nor ignored is the one combination with no visible
    # state, and the next `git add -A` commits it. Caught by the suite immediately — the copied
    # shim landed on a Crawler's branch and then blocked the merge into a main checkout holding
    # the same path untracked. Provisioning a guard must not hand the Crawler a file that
    # wedges its own integration.
    rel_paths = [os.path.join(GUARD_HOOKS_DIR, n) for n in copied]
    check = W.unignored(worktree_path, rel_paths)
    if not check.stageable:
        return ["%s provisioned into the worktree (%s) — `git worktree add` carries tracked "
                "files only, so an install that is deliberately untracked would otherwise leave "
                "the guard absent in the one place it runs"
                % (GUARD_HOOKS_DIR, ", ".join(copied))]

    for rel_path in rel_paths:
        p = os.path.join(worktree_path, rel_path)
        if os.path.exists(p):
            os.remove(p)
    return ["NOT PROVISIONED: %s is neither tracked by git nor ignored, so copying it into the "
            "worktree would hand the Crawler a file its own `git add -A` commits onto its "
            "branch — which then collides with the same untracked path in the main checkout and "
            "wedges the merge. The copy was removed. Fix it at the source: TRACK the hooks (they "
            "then cross by themselves and this does nothing), or IGNORE them (and this "
            "provisions them). Neither-nor is the only state with no answer."
            % ", ".join(sorted(check.stageable))]


def guard_health(cfg):
    """Is the guard actually WIRED? Returns [(level, message)] for `doctor`.

    THE CHECK ONE LEVEL OUT FROM THE ONE THAT ALREADY EXISTS. `doctor` errors when the binary
    every brief names is missing; this asks the same question about the guard — not "does the
    verb work" but "would it ever run". A guard verb nobody registers has never once run, and
    that was true of `lock guard` for the whole life of this repo: it exists, it is correct,
    and `.claude/settings.json` names three game_loop hooks and none of ours.

    Nothing here is a runtime dependency. The guard fails OPEN and says so; this verb is where
    the loudness lives instead, because a PreToolUse hook cannot carry it without blocking the
    repair.
    """
    from .util import git

    out = []
    shim = os.path.join(cfg.root, GUARD_SHIM)
    if not os.path.exists(shim):
        out.append(("error", "the worktree guard's shim is MISSING (%s). Nothing is registered "
                             "to deny a write into a tree another session holds." % GUARD_SHIM))
    elif not os.access(shim, os.X_OK):
        out.append(("error", "%s exists but is not executable, so the hook cannot run it — the "
                             "guard is inert. `chmod +x %s`" % (GUARD_SHIM, GUARD_SHIM)))
    else:
        out.append(("ok", "worktree guard shim present and executable: %s" % GUARD_SHIM))

    # THE CROSSING, WHICH IS THE WHOLE POINT OF THE SHIM AND IS NOT IMPLIED BY ITS EXISTENCE.
    # `git worktree add` copies files from HEAD, not from the working tree — so a shim that is
    # untracked, or tracked but uncommitted, is present HERE and absent in every worktree made
    # from now on, which is precisely where the guard is for. Observed while building WL-05:
    # the first probe worktree came up with no shim in it at all. Same failure the harness
    # payload has, checked in the same place, for the same reason.
    if os.path.exists(shim):
        rc, tracked, _ = git(["ls-files", "--error-unmatch", GUARD_SHIM], cwd=cfg.root)
        if rc != 0 or not (tracked or "").strip():
            # WARN, NOT ERROR, and the severity is copied rather than chosen: `doctor` already
            # reports "the harness payload is upgraded but NOT COMMITTED" as a warning, and
            # that is the identical failure — a worktree gets HEAD's copy, so an uncommitted
            # file is absent in every tree made from now on. Two checks about one mechanism
            # answering at two severities is the two-layers-disagree failure in miniature.
            # It is also the state EVERY fresh install passes through: install.sh places this
            # file and tells the consumer to commit it, so erroring here would make a correct
            # install's first `doctor` red for something it had just been told to do.
            out.append(("warn", "%s is not tracked by git yet. `git worktree add` copies "
                                "tracked files only, so until it is committed the guard is "
                                "present here and ABSENT in every worktree — the one place it "
                                "exists to run. `git add %s`" % (GUARD_SHIM, GUARD_SHIM)))
        else:
            rc, pending, _ = git(["diff", "HEAD", "--name-only", "--", GUARD_SHIM], cwd=cfg.root)
            if rc == 0 and (pending or "").strip():
                out.append(("warn", "%s is tracked but its committed copy DIFFERS from the "
                                    "working one. A new worktree gets HEAD's version, so the "
                                    "guard that crosses is not the guard you are reading."
                                    % GUARD_SHIM))

    # THE REGISTRATION. Absence is an error, not a warning: the guard is exactly as present as
    # this entry, and the remedy is printed as literal JSON rather than as a command, because a
    # remedy naming a command that does not exist is worse than no remedy and no verb writes
    # this file today.
    settings = os.path.join(cfg.root, ".claude", "settings.json")
    registered, matcher = _guard_registration(settings)
    # A COMMAND, not JSON to paste. This printed the raw hook entry for a reader to copy, which
    # is a remedy that asks somebody to hand-edit the file the guard depends on — and `register`
    # exists precisely so they do not have to. INV: a printed remedy is a claim that a command
    # exists, and now one does.
    fix = "%s worktree register" % _sr(cfg)
    if registered is None:
        out.append(("error", "no %s, so nothing registers the worktree guard and it will never "
                             "run. Fix: `%s`" % (rel_or(settings, cfg.root), fix)))
    elif not registered:
        out.append(("error", "%s registers no worktree-guard hook. The verb exists and has "
                             "never once run — which is exactly what was true of `lock guard` "
                             "for this repo's whole life. Fix: `%s`"
                             % (rel_or(settings, cfg.root), fix)))
    else:
        missing = [t for t in GUARD_TOOLS if t not in (matcher or "")]
        if missing:
            out.append(("warn", "the worktree guard is registered but its matcher (%r) does not "
                                "cover %s — writes through those tools are UNGUARDED."
                                % (matcher, ", ".join(missing))))
        else:
            out.append(("ok", "worktree guard registered on PreToolUse (%s)" % matcher))
    return out


def register_guard(cfg):
    """Add the guard's PreToolUse entry to .claude/settings.json. Returns (changed, message).

    SHIPS ITS OWN REGISTRATION, because the alternative is documented: `lock guard` has existed
    and been correct for this repo's whole life and has never once run, since nothing ever
    registered it. A guard verb whose registration is left as an instruction inherits that.

    Additive and idempotent. Every other key, and every other hook, is preserved — the file
    belongs to Claude Code and to whoever else registered a hook in it, and showrunner is a
    fourth entry beside them, not the owner. An unparseable file is REPORTED and left exactly
    as it is: silently rewriting a file we could not read is how somebody's hooks disappear.
    """
    import json
    from .util import atomic_write_json, file_lock

    path = os.path.join(cfg.root, ".claude", "settings.json")
    entry = {"matcher": "|".join(GUARD_TOOLS),
             "hooks": [{"type": "command",
                        "command": "\"$CLAUDE_PROJECT_DIR\"/" + GUARD_SHIM,
                        "timeout": 10,
                        "statusMessage": "showrunner: is this tree held by another session?"}]}

    # ONE LOCK AROUND THE WHOLE READ-MODIFY-WRITE, not just the write. Two installs racing —
    # and `install.sh` runs this on every invocation — could otherwise both read a file with no
    # entry and both append one, or the second could overwrite whatever the first added.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with file_lock(path + ".sr-lock"):
        return _register_guard_locked(cfg, path, entry, json, atomic_write_json)


def _register_guard_locked(cfg, path, entry, json, atomic_write_json):
    data = {}
    if os.path.exists(path):
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            return False, ("%s could not be read (%s) and was NOT modified — add the worktree "
                           "guard's PreToolUse entry by hand, or `doctor` will keep reporting "
                           "it missing." % (rel_or(path, cfg.root), exc))
        if not isinstance(data, dict):
            return False, ("%s is not a JSON object and was NOT modified."
                           % rel_or(path, cfg.root))

    registered, _ = _guard_registration(path)
    if registered:
        return False, ""

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return False, ("%s has a non-object \"hooks\" key and was NOT modified."
                       % rel_or(path, cfg.root))
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        return False, ("%s has a non-list \"hooks.PreToolUse\" and was NOT modified."
                       % rel_or(path, cfg.root))
    pre.append(entry)

    # WRITE-THEN-RENAME, through the helper that already exists for this. A plain `open(w)` +
    # `json.dump` truncates the file first, so a crash, a full disk, or a second writer between
    # the truncate and the flush loses somebody's entire hook configuration — the exact outcome
    # this function's own docstring argues against when it refuses to rewrite a file it could
    # not read. `install.sh` calls this on EVERY run and `init` calls it too, so the window is
    # not rare. `.claude/settings.json` belongs to Claude Code and to whoever else registered a
    # hook in it; it is not ours to lose.
    atomic_write_json(path, data)
    return True, ("registered the worktree guard in %s (PreToolUse on %s)"
                  % (rel_or(path, cfg.root), "|".join(GUARD_TOOLS)))


def _sr(cfg):
    """The binary, named absolutely — a remedy is read from wherever the reader is standing."""
    from .brief import sr_bin
    return sr_bin(cfg)


def rel_or(path, root):
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def _guard_registration(settings_path):
    """(registered, matcher) for the guard's PreToolUse entry.

    `registered` is None when the settings file is absent or unreadable — a state kept
    distinct from False, because "nobody configured hooks here" and "hooks are configured and
    ours is not among them" are different problems with different remedies.
    """
    import json
    try:
        with open(settings_path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    for entry in (data.get("hooks") or {}).get("PreToolUse") or []:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if isinstance(hook, dict) and "worktree-guard" in str(hook.get("command") or ""):
                return True, entry.get("matcher") or ""
    return False, None


def base_sha_of(cfg, tree):
    """The commit the held tree started from, read from the campaign record.

    Recorded at spawn precisely because git CANNOT reconstruct it afterwards: a fully-merged
    branch and one that never received a commit both have the base as their merge-base. So a
    fork that guessed with `merge-base` would silently start from the wrong place whenever the
    held tree had already merged, and produce a tree that looks right.

    Returns None when unknown, and callers must treat that as "ask", never as "use HEAD".
    """
    from . import campaign
    for entry in campaign.load(cfg).get("crawlers", []):
        if os.path.basename(str(entry.get("worktree") or "")) == tree:
            return entry.get("base_sha")
    return None


def fork(cfg, tree, session, base=None, name=None):
    """A tree of your own, from the same commit the held one started at.

    The answer the hijack prompt offers first, and the reason it is a VERB rather than a
    paragraph: told to "just make another worktree", a reader picks HEAD, which is not where
    the held tree started and is the one detail that makes the two trees incomparable.

    Returns (path, detail). Raises Refused on anything it will not guess at.
    """
    from . import harness
    from .util import Refused, git, slug

    src = tree_for(cfg, os.path.join(cfg.worktree_root, tree))
    if not src:
        raise Refused("%r is not a worktree under %s" % (tree, cfg.worktree_root))
    if base is None:
        base = base_sha_of(cfg, tree)
    if not base:
        raise Refused(
            "no recorded base commit for %r, and this will not guess one. git cannot tell a "
            "merged branch from an empty one after the fact, so a guessed base is wrong exactly "
            "when the held tree has already merged — silently, and the fork would look fine. "
            "Pass --base <commit> if you know it." % tree)

    # RESOLVE TO A SHA BEFORE CREATING ANYTHING, for the reason `spawn` does it: afterwards git
    # cannot tell a fully-merged branch from one that never received a commit, so the symbolic
    # ref is not recoverable as the thing it meant at this instant. A caller passing `--base
    # HEAD` also made the report say `base HEAD ... not HEAD`, which is a sentence that argues
    # with itself — observed on the first real fork.
    rc, resolved, _ = git(["rev-parse", "%s^{commit}" % base], cwd=cfg.root)
    if rc != 0 or not resolved.strip():
        raise Refused("git cannot resolve base %r in %s" % (base, cfg.root))
    base = resolved.strip()

    # THE NAME IS DERIVED FROM A PREFIX, so two sessions whose ids share their first 8
    # characters derive the same one — and the second then failed with "worktree path already
    # exists", which blames the path and sends the reader looking at the filesystem instead of
    # at the collision. Real session UUIDs make this rare, not impossible, and `--name` is the
    # answer either way; the refusal now says so.
    derived = name is None
    name = name or slug("%s-fork-%s" % (tree, (session or "x")[:8]), 60)
    if derived and os.path.exists(os.path.join(cfg.worktree_root, name)):
        raise Refused(
            "fork: %r already exists. The name is derived from the tree and the first 8 "
            "characters of your session id, so another session whose id starts the same way "
            "already forked this tree — this is a name collision, not a leftover directory. "
            "Pass --name <something> to choose your own." % name)
    from . import worktree as W
    path = W.create(cfg, name, "showrunner/%s" % name, base)
    injected, problems = W.inject(cfg, path)
    provisioned, hp, hw = harness.provision(cfg, path)
    if hp and harness.spec(cfg)["require"]:
        W.remove(cfg, name, force=True)
        git(["branch", "-D", "showrunner/%s" % name], cwd=cfg.root)
        raise Refused("fork aborted — the new tree's harness is not the project's:\n  - %s"
                      % "\n  - ".join(hp))

    # THE RESULT OF THE ACQUIRE IS THE ANSWER, not a side effect. This discarded it and the CLI
    # printed "lease held by you" unconditionally — so in the state where `enter` itself refuses
    # ("no session process could be resolved, this tree is unprotected"), `fork` moved the user
    # to a fresh tree, told them it was leased, and left it FREE. That lands on the recovery
    # path: fork is the FIRST remedy the hijack refusal offers, so the failure is reserved for
    # someone already being told their tree was taken.
    lease = Lease(cfg, name)
    got, holder = lease.acquire(session, who=INTERACTIVE)
    from . import events
    events.emit(cfg, "lease.forked", {"from": tree, "tree": name, "session": session,
                                      "base": base, "leased": bool(got)})
    return path, {"tree": name, "base": base, "from": tree, "injected": injected,
                  "provisioned": provisioned, "warnings": hw, "leased": bool(got),
                  "lease_holder": holder or {},
                  "problems": [] if not hp else hp}


def status(cfg, tree=None):
    """Every lease under this repo's lock root, or one. Read-only."""
    root = cfg.lock_root
    names = []
    if tree:
        names = [lease_name(tree)]
    elif os.path.isdir(root):
        names = sorted(d[:-len(".lock")] for d in os.listdir(root)
                       if d.startswith(PREFIX) and d.endswith(".lock"))
    out = []
    for name in names:
        t = name[len(PREFIX):]
        lease = Lease(cfg, t)
        state, h = lease.state()
        out.append({
            "tree": t,
            "state": state,
            "holder": h or {},
            "pid_basis": (lease.holder() or {}).get("pid_basis") if h else "",
            "path": os.path.join(cfg.worktree_root, t),
            "exists": os.path.isdir(os.path.join(cfg.worktree_root, t)),
            "checked": now(),
        })
    return out
