"""Named single-consumer resource locks — the one hard rule showrunner exists to enforce.

Lifted from `prototype/device_lane.sh`, which proved the primitive but hardcoded Drops
verbs, allowed exactly one lock, and defaulted its lock directory to a path relative to
the script's own location. Issue #3.

The primitive is unchanged, because it is right: an **atomic `mkdir`** whose holder is a
**live PID** — the consumer process itself, via `run`. A dead holder is stale and
reclaimable. That is what makes "one at a time" physically true across separate
processes and worktrees, rather than a wish in a prompt.

Three things changed:

* **Named resources.** `device` and `pg-port-5432` are separate locks, so unrelated work
  does not queue behind unrelated work.
* **One absolute lock root** for every worktree, validated at config load. A
  worktree-relative root gives N worktrees N sibling lock directories and the mutex
  silently does nothing — the worst available failure, because it looks like it works.
* **Boot token beside the PID.** PID reuse could otherwise hand the lane to the wrong
  holder; a claim from a previous boot cannot possibly still be running.

**What this does not cover** (INV6 — a guard that overstates its reach buys false
confidence): `guard` is a verb matcher, and a rogue raw command that does not match any
pattern escapes it. Routing and guarding are optimisations; the *lock* is the guarantee,
and it is only a guarantee where the consumer itself takes it (`run`). Belt and
suspenders — the guard catches the honest mistake, `run` catches everything else.
"""

import errno
import os
import re
import shutil
import time

from .util import UNKNOWN_BOOT, boot_token, die, eprint, now, pid_alive, pid_readable

FREE, HELD, STALE = "FREE", "HELD", "STALE"
# A fourth state, and the reason it exists is the whole point of this module. STALE means
# PROVED DEAD — a pid we read, that does not respond, recorded this boot — and `acquire`
# answers it by deleting the lock directory and taking the resource. An unreadable pid used to
# reach that same verdict: `pid_alive` catches ValueError and returns False, so binary or empty
# contents said "dead" when they meant "cannot tell". A partial write by a LIVE holder reads
# exactly like a holder that died, and the consequence is two Crawlers on a single-consumer
# resource with a note in the log saying the lock was reclaimed.
#
# 'could not tell' and 'proved dead' must never be the same answer — the same sentence the
# harness check uses, arriving in the mutex this project exists to enforce.
UNREADABLE = "UNREADABLE"


class Lock:
    def __init__(self, root, name):
        self.name = name
        self.root = root
        self.dir = os.path.join(root, "%s.lock" % name)

    # -- introspection -----------------------------------------------------
    def _read(self, key):
        try:
            with open(os.path.join(self.dir, key)) as fh:
                return fh.read().strip()
        except OSError:
            return ""

    # The five this module OWNS and computes liveness from. Everything else in the directory
    # was put there by a caller through `acquire(extra=...)` and is returned untouched.
    _OWN = ("pid", "boot", "holder", "session", "ts")

    def holder(self):
        """Who holds it, including whatever the caller recorded alongside.

        EXTRAS ARE READ BACK HERE, and that is a fix rather than a feature. `acquire` grew an
        `extra` parameter so a caller could record why its pid means what it does — the worktree
        lease needs it, since its pid comes from walking a hook's ancestry and can rest on a
        weaker fact. But only the WRITE side was added: this returned the five fixed fields, so
        the lease had to reach through `Lock._read` to get its own value back.

        An interface that lets a caller write a field and not read it forces exactly that, and
        reaching past an interface into another module's privates is the coupling this project
        deleted `DEFAULT_RULE_FILES` to end. The asymmetry was the defect; the reach was the
        symptom.

        Extras are returned and never interpreted. The four states are still computed from `pid`
        and `boot` alone, so no caller can widen or weaken the liveness rule by passing a field.
        """
        if not os.path.isdir(self.dir):
            return None
        h = {
            "pid": self._read("pid"),
            "boot": self._read("boot"),
            "who": self._read("holder") or "?",
            "session": self._read("session"),
            "ts": self._read("ts"),
        }
        try:
            for name in sorted(os.listdir(self.dir)):
                if name not in self._OWN and os.path.isfile(os.path.join(self.dir, name)):
                    h[name] = self._read(name)
        except OSError:
            pass
        return h

    def state(self):
        h = self.holder()
        if not h:
            return FREE, None
        if not pid_readable(h.get("pid")):
            return UNREADABLE, h
        return (HELD if self._live(h) else STALE), h

    def settled_state(self, grace=1.0, poll=0.05):
        """`state()`, but UNREADABLE has to still be true a moment later to count.

        THE WRITE IS NOT ATOMIC AND NEVER WAS: `acquire` creates the lock directory and then
        writes `pid` as a separate file, so for a few hundred microseconds a concurrent reader
        sees a directory with no pid in it — which `state()` correctly calls UNREADABLE, and
        which every adjudicating caller correctly treats as "a human has to clear this". Two
        sessions starting in the same tree is the ordinary fan-out shape, and it could produce a
        hard refusal, exit 2, over a lock that was valid a millisecond later.

        UNREADABLE means "cannot tell", and the honest way to answer a question you cannot tell
        yet is to look again — not to promote the transient case (someone is mid-acquire) or to
        demote the real one (a torn write by a process that died). A torn write does not repair
        itself, so anything still unreadable after the grace is the state the refusal was
        written for and gets it, unchanged.

        Only for callers that ACT on the answer. Reporting readers keep `state()`: a report that
        blocks for a second per lock is a report nobody runs.
        """
        state, h = self.state()
        if state != UNREADABLE:
            return state, h
        deadline = time.time() + max(0, grace)
        while time.time() < deadline:
            time.sleep(poll)
            state, h = self.state()
            if state != UNREADABLE:
                return state, h
        return UNREADABLE, h

    @staticmethod
    def _live(h):
        """Alive means: the PID responds AND it was recorded during this boot.

        A BOOT TOKEN NOBODY COULD READ IS NOT A DIFFERENT BOOT. `boot_token` degrades to
        `<host>:unknown` when the boot time is not discoverable — a `sysctl` that failed, a
        container with no `/proc/stat` — and comparing that against a real recorded token makes
        every holder on the machine read as "recorded during a previous boot", i.e. PROVED DEAD.
        A live session's lease would then be reclaimed out from under it by the next `acquire`,
        which is the single worst outcome this module has, produced by a transient failure to
        read an unrelated number.

        So an unknown token on either side drops back to plain PID semantics — exactly what
        `boot_token`'s docstring says the fallback degrades to, honoured here rather than only
        promised there. That is weaker (a PID reused across a reboot could read as alive) and it
        is the right direction: this module's whole posture is that 'could not tell' must never
        become 'proved dead'.
        """
        theirs, ours = h.get("boot"), boot_token()
        comparable = (theirs and not theirs.endswith(UNKNOWN_BOOT)
                      and not ours.endswith(UNKNOWN_BOOT))
        if comparable and theirs != ours:
            return False
        return pid_alive(h.get("pid"))

    # -- mutation ----------------------------------------------------------
    def _write_owner(self, pid, who, session, extra=None):
        fields = [("pid", pid), ("boot", boot_token()), ("holder", who),
                  ("session", session or ""), ("ts", now())]
        # `extra` exists so a CALLER can record why its pid means what it does, without this
        # module growing a vocabulary for every kind of holder. The worktree lease needs it:
        # its pid comes from walking a hook's ancestry, which can fail, and a lease whose
        # liveness rests on a weaker fact has to say so where the reader stands. Written LAST
        # and never read by anything here — the four states above are computed from pid and
        # boot alone, so a caller cannot widen or weaken the liveness rule by passing a field.
        for key, val in list(extra.items() if extra else []):
            if key not in dict(fields):
                fields.append((key, val))
        for key, val in fields:
            with open(os.path.join(self.dir, key), "w") as fh:
                fh.write("%s\n" % val)

    def acquire(self, pid, who, session=None, wait=0, poll=1.0, extra=None):
        """Take the lock, or return False. `wait` seconds of polling is NOT fair —
        there is no queue, so a starved waiter can lose repeatedly. Said out loud
        because a fairness property nobody stated is one somebody will assume."""
        deadline = time.time() + max(0, wait)
        while True:
            os.makedirs(self.root, exist_ok=True)
            try:
                os.mkdir(self.dir)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
            else:
                self._write_owner(pid, who, session, extra)
                return True

            # SETTLED, because this caller ACTS on the answer — it reclaims, or it refuses a
            # human out to a manual repair. The unsettled read makes another session's own
            # acquire, mid-write, look like corruption.
            state, h = self.settled_state()
            if state == STALE:
                eprint("note: reclaiming stale lock %r (holder pid %s, boot %s — not alive)"
                       % (self.name, h.get("pid"), h.get("boot")))
                shutil.rmtree(self.dir, ignore_errors=True)
                continue

            if state == UNREADABLE:
                # Refuse rather than reclaim, and refuse rather than poll: waiting would spin
                # to the deadline and then return False, which is indistinguishable from a
                # busy resource and leaves the corruption in place for the next caller to
                # rediscover. A human clears this, because only a human can find out whether
                # the holder is still running.
                die("lock %r holds an UNREADABLE pid (%r) — it cannot be proved dead, so it "
                    "will not be reclaimed. A partial write by a LIVE holder looks exactly "
                    "like this. Find out whether %s is still running, then "
                    "`showrunner lock release %s --force` if it is not."
                    % (self.name, (h.get("pid") or "")[:40], h.get("who") or "the holder",
                       self.name), code=2)

            if time.time() >= deadline:
                return False
            time.sleep(poll)

    def release(self, pid=None, force=False):
        h = self.holder()
        if not h:
            return False
        if not force and str(h.get("pid")) != str(pid):
            die("not the lock owner (holder pid %s, you are %s); pass --force only if you "
                "know the holder is gone" % (h.get("pid"), pid), code=2)
        shutil.rmtree(self.dir, ignore_errors=True)
        return True


class LockSet:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = cfg.lock_root
        self.resources = cfg.get("resources") or []

    def names(self):
        return [r["name"] for r in self.resources if r.get("name")]

    def lock(self, name):
        if not self.cfg.resource(name):
            die("no resource named %r in config (known: %s)"
                % (name, ", ".join(self.names()) or "<none>"), code=2)
        return Lock(self.root, name)

    def on_disk(self):
        """Every lock directory under the root, configured or not.

        A root that CANNOT BE LISTED answers [] — the same answer as a machine holding no locks
        — so the failure is recorded on the instance instead of being inferred from an empty
        list. `reap` reads this to find the locks a dead Crawler left behind, and its own
        comment says that loop exists to end the "silently absorbed" case; an unreadable root
        silently absorbing every stale lock is that failure wearing the loop's own clothes.

        [] is still the return, because every caller wants a list and a raise here would take
        out `reap` entirely — losing the other actions it had already found.
        """
        self.on_disk_error = None
        try:
            return sorted(d[:-len(".lock")] for d in os.listdir(self.root)
                          if d.endswith(".lock"))
        except OSError as e:
            # A root that does not EXIST is not a failure to look — no lock has ever been taken
            # here, and that is an answer. Only a root that is there and cannot be listed is a
            # blindness worth reporting. Recording both identically would make every fresh repo
            # cry wolf, and a check that cries wolf stops being read.
            if os.path.isdir(self.root):
                self.on_disk_error = str(e)
            return []

    def existing(self, name):
        """A lock to RELEASE. Configured, or merely present — releasing is the remedial path.

        `lock()` resolves configured resources only, which is right for taking a lock: a typo
        must not mint a new one. It is wrong for giving one back, and the gap was reachable
        through a printed remedy. The UNREADABLE refusal says

            showrunner lock release <name> --force

        and the worktree lease names its locks `worktree:<tree>`, which is not a configured
        resource and never will be — so the one escape hatch offered to a human staring at a
        wedged lock answered "no resource named 'worktree:victim' in config" and exited 2.

        A lock that physically exists can be released by name. Refusing to name something that
        is really there is refusing to repair a real state.
        """
        if self.cfg.resource(name) or os.path.isdir(os.path.join(self.root, "%s.lock" % name)):
            return Lock(self.root, name)
        known = sorted(set(self.names()) | set(self.on_disk()))
        die("no lock named %r — nothing configured under that name and nothing held under it "
            "on disk (known: %s)" % (name, ", ".join(known) or "<none>"), code=2)

    def matching(self, command):
        """Every configured resource whose match patterns fire on this command line."""
        hits = []
        for res in self.resources:
            for pat in res.get("match") or []:
                try:
                    if re.search(pat, command, re.IGNORECASE):
                        hits.append((res["name"], pat))
                        break
                except re.error as exc:
                    die("resource %r has an invalid match pattern %r: %s"
                        % (res.get("name"), pat, exc), code=2)
        return hits

    def guard(self, command, session=None):
        """PreToolUse shape: return (allow: bool, message: str).

        Allows when the holder is *us* — a guard that blocks its own holder from doing
        the very work it acquired the lock for is a guard that gets switched off (INV5).
        """
        hits = self.matching(command)
        if not hits:
            return True, "allow (matches no single-consumer resource): %s" % command
        for name, pat in hits:
            state, h = Lock(self.root, name).state()
            if state != HELD:
                continue
            if session and h.get("session") == session:
                continue  # we hold it
            return False, (
                "BLOCKED: %r is held by pid %s (%s). Matched pattern %r.\n"
                "  Command: %s\n"
                "  This is a single-consumer resource: one Crawler in the boss room at a time.\n"
                "  Wait, or run it through: `showrunner lock run %s --holder <who> -- <cmd>`"
                % (name, h.get("pid"), h.get("who"), pat, command, name))
        return True, "allow (%s free): %s" % (", ".join(n for n, _ in hits), command)
