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

from .util import boot_token, die, eprint, now, pid_alive, pid_readable

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

    def holder(self):
        if not os.path.isdir(self.dir):
            return None
        return {
            "pid": self._read("pid"),
            "boot": self._read("boot"),
            "who": self._read("holder") or "?",
            "session": self._read("session"),
            "ts": self._read("ts"),
        }

    def state(self):
        h = self.holder()
        if not h:
            return FREE, None
        if not pid_readable(h.get("pid")):
            return UNREADABLE, h
        return (HELD if self._live(h) else STALE), h

    @staticmethod
    def _live(h):
        """Alive means: the PID responds AND it was recorded during this boot."""
        if h.get("boot") and h["boot"] != boot_token():
            return False
        return pid_alive(h.get("pid"))

    # -- mutation ----------------------------------------------------------
    def _write_owner(self, pid, who, session):
        for key, val in (("pid", pid), ("boot", boot_token()), ("holder", who),
                         ("session", session or ""), ("ts", now())):
            with open(os.path.join(self.dir, key), "w") as fh:
                fh.write("%s\n" % val)

    def acquire(self, pid, who, session=None, wait=0, poll=1.0):
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
                self._write_owner(pid, who, session)
                return True

            state, h = self.state()
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
