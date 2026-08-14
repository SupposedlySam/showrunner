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

        Deliberately a pass-through. Any 'helpful' collapsing at this layer — treating
        UNREADABLE as free, say, or aging a HELD lease out — would be this module quietly
        holding a different liveness rule than the one the device lane enforces, and two
        layers disagreeing about the rules silently is the failure BOUNDARY.md exists for.
        """
        return self.lock.state()

    def holder(self):
        h = self.lock.holder()
        if h is not None:
            h.setdefault("pid_basis", "")
            h["pid_basis"] = self.lock._read("pid_basis")
        return h

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
