"""Small shared helpers. Standard library only."""

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


def rel(path, base):
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path
