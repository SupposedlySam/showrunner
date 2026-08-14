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

from . import locks
from .util import now, session_pid

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
        """
        h = self.lock.holder()
        if not h:
            return False, "not held"
        if not force and session and h.get("session") != session:
            return False, ("held by session %s, not you — pass force only if you know that "
                           "session is gone" % (h.get("session") or "?"))
        self.lock.release(force=True)
        return True, "released"


OPTIONS = """\
This worktree is held by another live session.

  holder   {who}  (session {session}, pid {pid}, alive, this boot)
  since    {since}
  basis    {basis}

Nothing is stopping your writes yet — the guard that will is WL-05, and it is deliberately
not built until a real hijack has been observed rather than imagined. Treat this as the
warning it is:

  1. Your own tree, same starting point — the usual answer:
       {sr} worktree fork --from {tree}
     New worktree and branch off the same base commit.

  2. Read-only. Stay, read, do not write. Nothing further needed.

  3. Take it over — NOT BUILT YET (WL-06). Said plainly rather than printed as though it
     works: a remedy naming a command that does not exist is worse than no remedy, and this
     project has shipped that twice. Until it lands, a holder you know is gone is cleared with
     `{sr} lease status` to confirm the state, then by hand.

  4. Leave."""


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
    state, h = lease.state()

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

    if state == locks.STALE:
        events.emit(cfg, "lease.reclaimed", {
            "tree": tree, "session": session,
            "dead_session": h.get("session") if h else None,
            "dead_pid": h.get("pid") if h else None,
        })

    got, holder = lease.acquire(session, who=who or INTERACTIVE)
    if not got:
        return "no-liveness", {"tree": tree, "holder": holder or {}}
    verdict = "reclaimed" if state == locks.STALE else "acquired"
    events.emit(cfg, "lease.acquired", {"tree": tree, "session": session,
                                        "basis": (holder or {}).get("pid_basis")})
    return verdict, {"tree": tree, "holder": holder or {}, "previous": h or {}}


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

    name = name or slug("%s-fork-%s" % (tree, (session or "x")[:8]), 60)
    from . import worktree as W
    path = W.create(cfg, name, "showrunner/%s" % name, base)
    injected, problems = W.inject(cfg, path)
    provisioned, hp, hw = harness.provision(cfg, path)
    if hp and harness.spec(cfg)["require"]:
        W.remove(cfg, name, force=True)
        git(["branch", "-D", "showrunner/%s" % name], cwd=cfg.root)
        raise Refused("fork aborted — the new tree's harness is not the project's:\n  - %s"
                      % "\n  - ".join(hp))

    lease = Lease(cfg, name)
    lease.acquire(session, who=INTERACTIVE)
    from . import events
    events.emit(cfg, "lease.forked", {"from": tree, "tree": name, "session": session,
                                      "base": base})
    return path, {"tree": name, "base": base, "from": tree, "injected": injected,
                  "provisioned": provisioned, "warnings": hw,
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
