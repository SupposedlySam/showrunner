"""Small shared helpers. Standard library only."""

import contextlib
import json
import os
import re
import subprocess
import sys
import time


class Refused(Exception):
    """A gate refused. Carries the exit code the CLI should exit with.

    Exit 2 is the Claude Code PreToolUse "deny the tool call" code, so gates that
    double as hooks raise with code 2.
    """

    def __init__(self, message, code=2, hint=None):
        super().__init__(message)
        self.code = code
        self.hint = hint


def die(message, code=2, hint=None):
    raise Refused(message, code, hint)


def eprint(*args):
    print(*args, file=sys.stderr)


def now():
    return int(time.time())


def boot_token():
    """Identify this boot of this machine.

    A PID alone is not identity: PIDs are reused, and after a reboot a *different*
    process very plausibly holds the number a dead Crawler wrote down. Pairing the PID
    with a boot token makes stale-detection wrong only if a PID is reused within a
    single boot, which is the narrowest form of the bug we can cheaply reach.
    DESIGN.md flags PID reuse as an open question; this is the tightening.

    Falls back to the hostname when no boot time is discoverable, which degrades to
    plain PID semantics rather than to a crash.
    """
    host = os.uname().nodename
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            m = re.search(r"sec\s*=\s*(\d+)", out)
            if m:
                return "%s:%s" % (host, m.group(1))
        else:
            with open("/proc/stat") as fh:
                for line in fh:
                    if line.startswith("btime "):
                        return "%s:%s" % (host, line.split()[1])
    except Exception:
        pass
    return "%s:unknown" % host


def pid_readable(pid):
    """Can this be READ as a pid at all? A different question from whether it is alive.

    `pid_alive` answers False for BOTH "not running" and "not a pid", and only the first
    licenses acting on somebody else's resource — deleting their lock, releasing their claim.
    A partial write by a LIVE holder is indistinguishable from a holder that died unless the
    two are separated here.
    """
    if pid is None or pid == "":
        return False
    try:
        int(pid)
        return True
    except (TypeError, ValueError):
        return False


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        # Exists, owned by somebody else. Alive for our purposes.
        return True


def run(cmd, cwd=None, check=False, timeout=None, env=None):
    """Run a command, returning (rc, stdout, stderr). Never raises on non-zero unless asked."""
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        env=env, shell=isinstance(cmd, str),
    )
    if check and proc.returncode != 0:
        die("command failed (%s): %s\n%s" % (proc.returncode, cmd, proc.stderr.strip()))
    return proc.returncode, proc.stdout, proc.stderr


def git(args, cwd=None, check=False):
    return run(["git"] + list(args), cwd=cwd, check=check)


def repo_root(start=None):
    """The top level of the git work tree containing `start`."""
    rc, out, _ = git(["rev-parse", "--show-toplevel"], cwd=start or os.getcwd())
    if rc != 0:
        return None
    return out.strip()


def main_checkout(start=None):
    """The MAIN checkout, even when called from inside a linked worktree.

    A worktree's `--show-toplevel` is the worktree; its `--git-common-dir` points at the
    main checkout's `.git`. Config, locks and the campaign record must resolve to ONE
    place for every Crawler (INV8), so they key off this rather than off the cwd.
    """
    rc, out, _ = git(["rev-parse", "--git-common-dir"], cwd=start or os.getcwd())
    if rc != 0:
        return repo_root(start)
    common = out.strip()
    if not os.path.isabs(common):
        common = os.path.abspath(os.path.join(start or os.getcwd(), common))
    return os.path.dirname(common.rstrip("/")) or "/"


def slug(text, maxlen=48):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", str(text)).strip("-").lower()
    return (s[:maxlen] or "x").strip("-")


def truthy_env(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


try:
    import fcntl
    _HAVE_FLOCK = True
except ImportError:                                   # pragma: no cover - non-POSIX
    _HAVE_FLOCK = False


@contextlib.contextmanager
def file_lock(path):
    """Exclusive cross-process lock around a state file.

    Deliberately `flock`, not the atomic-mkdir+live-PID primitive in locks.py. Those guard a
    long-running *consumer* of a real single-consumer resource, where the holder is the
    consuming process and a dead holder must be reclaimable. This guards a read-modify-write
    that lasts microseconds, and flock's kernel-backed release-on-death is exactly right for
    that: there is no stale lock to reason about, because there is no window in which a
    holder can die and leave one.

    Degrades to a no-op where flock is unavailable, which is honest rather than silently
    pretending: the caller is told by `concurrency_note()`.
    """
    if not _HAVE_FLOCK:
        yield
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextlib.contextmanager
def try_file_lock(path):
    """Non-blocking exclusive lock. Yields True if taken, False if someone else holds it.

    Used where blocking would look like a hang. An unattended orchestrator that silently
    waits several minutes on another one's merge is indistinguishable from a wedged run,
    and "mysteriously slow" is how a rail gets investigated and then removed.
    """
    if not _HAVE_FLOCK:
        yield True
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    got = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except OSError:
            got = False
        yield got
    finally:
        if got:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def concurrency_note():
    return None if _HAVE_FLOCK else (
        "flock is unavailable on this platform, so showrunner's shared state files are NOT "
        "protected against concurrent orchestrators. Run one orchestrator at a time.")


def atomic_write_json(path, data):
    """Write-then-rename, so a reader never sees a half-written state file.

    ATOMIC AGAINST A READER, NOT DURABLE AGAINST A CRASH — stated because the word invites the
    stronger reading. The contents are fsynced; the RENAME is not, because that would need an
    fsync of the parent directory too. So a power loss between the two can leave the old file.

    That is deliberate rather than unfinished, and the reason is which side the failure lands
    on: a lost rename leaves the previous state intact, while a lost WRITE would leave a
    truncated one. The hazard here is concurrent agents on one machine, and for that the
    ordering above is what matters.

    The temp name carries the PID so two processes never stage through one file. That is
    per-process, not per-thread: two threads would collide on it exactly as two processes
    would without it. showrunner is single-threaded and its shared state sits behind
    `file_lock` regardless — but a threaded caller copying this line gets the original bug
    back wearing a suffix that looks like it solved it.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def rel(path, base):
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path
