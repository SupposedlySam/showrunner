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


def stamp(ts):
    """An epoch second is a number a reader has to go and convert. `since 1786738962` was
    printed to a human deciding whether to take somebody's worktree, which is a decision about
    how long ago something happened.

    HERE rather than in cli.py, because the lease's REFUSAL prints the same field and shipped
    the raw epoch again the first time it ran — the fix had been made one layer away and could
    not be reached. One formatter, so the prompt and the refusal cannot disagree about what a
    timestamp looks like.
    """
    try:
        import datetime
        return datetime.datetime.fromtimestamp(int(ts)).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return str(ts or "?")


def short_session(sid):
    """Abbreviate a session id, and SAY that it is abbreviated.

    `sess-AAAA` came out as `sess-AAA` — eight characters of a nine-character id,
    indistinguishable from the whole thing, in a report whose entire job is telling two
    sessions apart. Then the lease's refusal re-made it with a bare slice, printing
    `session-HOLDER` as `session-HOLD`, which is why this lives beside `stamp` now.
    """
    sid = sid or ""
    if not sid:
        return "?"
    return sid if len(sid) <= 12 else sid[:12] + "…"


def boot_token():
    """Identify this boot of this machine.

    A PID alone is not identity: PIDs are reused, and after a reboot a *different*
    process very plausibly holds the number a dead Crawler wrote down. Pairing the PID
    with a boot token makes stale-detection wrong only if a PID is reused within a
    single boot, which is the narrowest form of the bug we can cheaply reach.
    DESIGN.md flags PID reuse as an open question; this is the tightening.

    Falls back to the hostname when no boot time is discoverable, which degrades to
    plain PID semantics rather than to a crash — see `Lock._live`, which is where that
    degradation has to be honoured, because a token this function could not read must not be
    compared against one it could.

    CACHED FOR THE LIFE OF THE PROCESS, which is not an optimisation: the answer cannot change
    while this process runs (a reboot ends it), and re-deriving it means shelling out to
    `sysctl` on EVERY liveness check — every lock read, every lease check, every guard call.
    Each of those was a chance for a transient failure to answer `unknown` and flip a live
    holder to "proved dead", which is the one verdict this module is most careful about
    everywhere else.
    """
    global _BOOT_TOKEN
    if _BOOT_TOKEN is not None:
        return _BOOT_TOKEN
    _BOOT_TOKEN = _read_boot_token()
    return _BOOT_TOKEN


UNKNOWN_BOOT = ":unknown"
_BOOT_TOKEN = None


def _read_boot_token():
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


SESSION_PROCESS = "claude"
# The one basis that proves a resolved session process. Named because a caller
# checking "did this actually resolve?" must not spell it a second time.
RESOLVED_BASIS = "ancestor-claude"
MAX_ANCESTRY = 12


def session_pid(start=None):
    """The Claude Code process this call sits under. Returns (pid, basis).

    WL-01 settled that no PID reaches a hook. The spec says so verbatim and game_loop — hooks
    on every event, and the consumer that would simply be using such a field if it existed —
    reads eleven payload fields across its guards and never one. So the only way to a live
    process is the ancestry, and a hook is a child of the session that spawned it.

    `basis` is the whole point of the return shape. The match is on process NAME, and here
    `claude` is a native binary whose argv is the bare name — but an install launched through
    `npx` or a node shim presents as `node`, and the walk finds nothing. That case gets the
    immediate parent and a basis that SAYS so, because a lease whose liveness rests on a
    weaker fact must carry that where the reader stands. 'Could not tell' and 'proved it' must
    never be the same answer — the distinction `locks.py` draws between UNREADABLE and STALE,
    arriving one layer up.

    What this does NOT establish: that the process found is *this* session rather than another
    `claude` in the same ancestry. Nothing observed suggests that shape and nothing here would
    detect it, which is why a lease records the session id beside the pid — the pid answers
    "alive?", the session id answers "who?", and neither is asked to do the other's job.
    """
    pid = int(start or os.getpid())
    first_parent = None
    for _ in range(MAX_ANCESTRY):
        rc, out, _ = run(["ps", "-o", "ppid=,comm=", "-p", str(pid)])
        if rc != 0 or not out.strip():
            break
        parts = out.strip().split(None, 1)
        if len(parts) != 2:
            break
        ppid, comm = parts[0].strip(), os.path.basename(parts[1].strip())
        if comm.startswith(SESSION_PROCESS):
            return pid, RESOLVED_BASIS
        if first_parent is None:
            first_parent = ppid
        if ppid in ("0", "1"):
            break
        pid = int(ppid)
    # Deliberately the caller's own parent and not the last pid the walk reached: the walk
    # stopping early means it learned nothing, and the highest ancestor it happened to touch
    # is init or a terminal, which is alive forever and would make the lease immortal.
    if first_parent and first_parent not in ("0", "1"):
        return int(first_parent), "ppid-fallback"
    return None, "unresolved"


def git_common_dir(path):
    """The shared git dir behind `path`, absolute, or None. Two worktrees of one repo agree here."""
    rc, out, _ = run(["git", "rev-parse", "--git-common-dir"], cwd=path)
    out = (out or "").strip()
    if rc != 0 or not out:
        return None
    return os.path.realpath(out if os.path.isabs(out) else os.path.join(path, out))


def caller_tree(cwd=None):
    """The worktree the caller is STANDING IN, or None. Never raises."""
    rc, top, _ = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd or os.getcwd())
    top = (top or "").strip()
    return os.path.realpath(top) if rc == 0 and top else None


def resolve_from_caller(cfg, given, cwd=None):
    """(path, root) for a path a SESSION supplied. Never raises.

    A RELATIVE PATH BELONGS TO THE TREE THE CALLER IS STANDING IN, not to the main checkout.
    Joining it against `cfg.root` reads a DIFFERENT FILE THAT HAPPENS TO SHARE THE PATH, and the
    proof gate turned that into its worst possible verdict: an agent working a leaf passed a
    relative path to a test it had just written in its own worktree, the join found the main
    checkout's stale copy of the same path, and the close was refused as proof that predates the
    claim. The proof was real and fresh. The gate read another file and manufactured exactly the
    verdict it exists to catch, which gives the agent no reason to suspect path resolution.

    Absolute paths were unaffected, so the bug was invisible to anyone who passed those and
    reliably fatal to anyone who typed the relative path that is natural when you are standing in
    the directory.

    From the main checkout the caller's tree IS `cfg.root`, so nothing changes there. `root` comes
    back so a refusal can NAME where it looked — the fix for the confusion is not only resolving
    correctly, it is saying which tree the answer came from.
    """
    if os.path.isabs(given):
        return given, None
    root = caller_tree(cwd)
    # ...BUT ONLY A TREE OF *THIS* REPO. A cwd in some unrelated checkout is not "the tree the
    # closer is standing in" in any sense the campaign knows about, and silently resolving a
    # proof path into a stranger repo would be the same class of bug pointed somewhere new. When
    # the cwd is not ours the answer falls back to `cfg.root`, which is the behaviour that was
    # always correct for that case.
    if root and git_common_dir(root) != git_common_dir(cfg.root):
        root = None
    root = root or cfg.root
    return os.path.join(root, given), root


def run(cmd, cwd=None, check=False, timeout=None, env=None):
    """Run a command, returning (rc, stdout, stderr). Never raises on non-zero unless asked.

    A MISSING `cwd` IS NOT A NON-ZERO EXIT, and it used to escape as an exception. Python raises
    from `subprocess.run` when it cannot enter the working directory — the process never starts,
    so there is no exit code to return — and every caller here is written against exit codes and
    catches nothing. One of them is `stop_gate`, which resolves a claim's RECORDED tree, and a
    recorded tree outlives the directory: a worktree removed after its claim leaves a path that
    was true when written. So a Claude Code hook crashed with a traceback instead of answering,
    and the harness's fail-open kept the session moving while the gate said nothing.

    Converted here rather than at each call site because this is the single point processes are
    spawned, which is the only place that fixes it once. Reported as rc 127 — "command not
    executable" is the closest true thing, and it is non-zero, so a caller checking `rc != 0`
    behaves correctly without knowing this case exists.
    """
    if cwd is not None and not os.path.isdir(cwd):
        return 127, "", "working directory does not exist: %s" % cwd
    # THE SAME ARGUMENT, TWO MORE WAYS THE PROCESS NEVER STARTS. A binary that is not on PATH
    # raises FileNotFoundError and a `timeout=` that expires raises TimeoutExpired — neither is
    # an exit code either, and the callers that catch nothing include `pin.running()`, whose
    # docstring promises "never raises" and which backs `--version` and the statusline. On a box
    # without git that was a traceback from `showrunner --version`. Fixed at the single spawn
    # point rather than at each call site, for the reason above.
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env=env, shell=isinstance(cmd, str),
        )
    except FileNotFoundError as exc:
        return 127, "", "command not found: %s" % (exc.filename or cmd)
    except subprocess.TimeoutExpired:
        # 124, which is what `timeout(1)` returns, so a caller reading the code gets the same
        # answer it would from the shell. Distinct from 127 on purpose: "it never started" and
        # "it started and would not finish" are different problems.
        return 124, "", "timed out after %ss: %s" % (timeout, cmd)
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
    for anchor in _root_anchors(start):
        rc, out, _ = git(["rev-parse", "--git-common-dir"], cwd=anchor)
        if rc != 0:
            found = repo_root(anchor)
            if found:
                return found
            continue
        common = out.strip()
        if not os.path.isabs(common):
            # ANCHORED ON WHAT PRODUCED IT, never on $PWD. A relative --git-common-dir is
            # relative to the directory git was RUN in; joining it against the process cwd
            # would resolve the fallback answer against the very location that could not
            # answer. Flagged by the reporter before it could bite.
            common = os.path.abspath(os.path.join(anchor, common))
        return os.path.dirname(common.rstrip("/")) or "/"
    return None


def _root_anchors(start):
    """Where to look for the repo, in order: the caller's choice, then the HARNESS.

    CWD IS NOT A PROXY FOR WHAT A GUARD PROTECTS. Resolving only from cwd made showrunner's
    Python entrypoints strongest exactly where they are least needed — already inside the repo
    — and absent exactly where a stray absolute path is most likely, which is a scratch
    directory. Working from a scratchpad is the ordinary shape of orchestration.

    The shell shims gained this fallback and the PYTHON PATH DID NOT, so
    `.showrunner/hooks/dispatch-guard.sh` evaluated the call while
    `showrunner dispatch guard` — a separately-registerable entrypoint to the SAME guard —
    failed open beside it, with `CLAUDE_PROJECT_DIR` set and valid. A consumer had both
    registered: the fixed shim's silence was drowned out by the unfixed CLI's warning, so the
    session read as guarded-but-noisy rather than partly unguarded.

    Fixed in the ONE resolver every entrypoint shares, so the two cannot disagree again — the
    repair the reporter asked for, rather than a second copy of the fallback.
    """
    seen, out = set(), []
    for cand in (start, os.getcwd(), os.environ.get("CLAUDE_PROJECT_DIR")):
        if cand and cand not in seen and os.path.isdir(cand):
            seen.add(cand)
            out.append(cand)
    return out


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
