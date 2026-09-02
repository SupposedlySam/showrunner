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
    """A per-boot identity, SCHEME-TAGGED so two schemes are never compared as if they were one.

    THE SECONDS ARE NOT CONSTANT. macOS does not store boot time; it recomputes it from the
    current clock minus uptime, so an NTP adjustment moves `kern.boottime`'s `sec` by a second.
    The token is cached for the life of a process (deliberately — see `boot_token`), so two
    processes that cached on opposite sides of one adjustment disagree FOREVER, and every
    cross-process liveness comparison between them is wrong in the one direction this module
    is most careful about: a live holder read as proved dead.

    Reported from a real campaign, with the token observed going BACKWARDS across two readings
    fifteen minutes apart on a machine with two days of uptime. `/proc/stat`'s `btime` on Linux
    is the same shape of value and inherits the same doubt.

    So prefer an identifier the kernel MINTS ONCE PER BOOT and does not recompute:
    `kern.bootsessionuuid` on darwin, `/proc/sys/kernel/random/boot_id` on Linux. The seconds
    remain as a fallback, tagged as such, so a machine without the uuid still gets liveness.
    """
    host = os.uname().nodename
    try:
        if sys.platform == "darwin":
            uuid = subprocess.run(["sysctl", "-n", "kern.bootsessionuuid"],
                                  capture_output=True, text=True, timeout=5).stdout.strip()
            if uuid:
                return "%s:uuid:%s" % (host, uuid)
            out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                                 capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"sec\s*=\s*(\d+)", out)
            if m:
                return "%s:sec:%s" % (host, m.group(1))
        else:
            try:
                with open("/proc/sys/kernel/random/boot_id") as fh:
                    uuid = fh.read().strip()
                if uuid:
                    return "%s:uuid:%s" % (host, uuid)
            except OSError:
                pass
            with open("/proc/stat") as fh:
                for line in fh:
                    if line.startswith("btime "):
                        return "%s:sec:%s" % (host, line.split()[1])
    except Exception:
        pass
    return "%s:unknown" % host


def _boot_parts(token):
    """(host, scheme, value) for a token, understanding the LEGACY untagged form.

    Claims written before the scheme tag existed read `host:<digits>`. Those are the seconds
    scheme; saying so here is what keeps the upgrade from reading every one of them as a
    different boot.
    """
    if not token:
        return None, None, None
    bits = str(token).split(":")
    if len(bits) >= 3 and bits[1] in ("uuid", "sec"):
        return bits[0], bits[1], ":".join(bits[2:])
    if len(bits) == 2:
        return bits[0], ("unknown" if bits[1] == "unknown" else "sec"), bits[1]
    return None, None, None


def same_boot(theirs, ours):
    """True / False / None — and None means CANNOT TELL, never "proved dead".

    ONE COMPARISON, because there were two and they are the same rule. `graph.stale_claims` and
    `locks._live` each implemented "different token means the process cannot still be running",
    which is exactly the shape this repo has had to repair elsewhere: two statements of one
    policy, free to disagree.

    THE UPGRADE ITSELF WOULD OTHERWISE CAUSE THE BUG. Switching darwin from seconds to a boot
    uuid changes the token, so every claim recorded by an older build would suddenly differ from
    every reading by a newer one — mass false STALE, produced by the fix for false STALE. Two
    DIFFERENT SCHEMES are therefore incomparable, not opposed.

    Within the seconds scheme, a ±1s difference is treated as the same boot: the drift being
    repaired is exactly one second of clock adjustment, and the field's own precision is the
    problem. Suggested by the reporter, and cheap enough to keep for the fallback path.
    """
    h1, s1, v1 = _boot_parts(theirs)
    h2, s2, v2 = _boot_parts(ours)
    if not s1 or not s2 or "unknown" in (s1, s2):
        return None
    if h1 != h2:
        return False                      # a different machine is a different boot, knowably
    if s1 != s2:
        return None                       # seconds vs uuid: incomparable, not different
    if s1 == "uuid":
        return v1 == v2
    try:
        return abs(int(v1) - int(v2)) <= 1
    except (TypeError, ValueError):
        return None


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


# ------------------------------------------------------- the session transcript
# THE ONE SIGNAL THAT SEPARATES "WORKING" FROM "WEDGED" (#69). Every other liveness fact
# showrunner holds is about a PROCESS, and a process parked at a prompt is byte-identical to
# one mid-computation: `ps` reports `Ss` for both, and `%CPU` is a lifetime average, so a
# session that did five minutes of work and then stalled for fifty still reports a healthy
# number. Measured in the incident that filed this: 0.13s of CPU accrued over 56 minutes, and
# every showrunner-visible signal said the Crawler was working.
#
# `heartbeat_ts` is not the missing signal either, and is worth naming so nobody reaches for
# it: `Graph.heartbeat()` has no callers and nothing reads the column, so the field records the
# last STATE CHANGE, not the last sign of life. Starting to call it would still report nothing.
#
# The transcript is the only artefact that moves when the AGENT moves, and it needs no new
# plumbing: a claim already carries `claim_tree` and `claim_session`, and the host's path is
# derivable from exactly those two.


def projects_root():
    """Where the host keeps session transcripts.

    `CLAUDE_CONFIG_DIR` wins because the host honours it; `~/.claude` is the default. Read at
    CALL time rather than import time so a test can point it somewhere and so a session that
    exports it mid-run is not answered from a stale cache.
    """
    base = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    return os.path.join(base or os.path.join(os.path.expanduser("~"), ".claude"), "projects")


# EVERY non-alphanumeric byte, not just the separator. The issue says "separators replaced by
# dashes" and a premise check on this repo added "the dot is replaced too" -- both are true and
# both are incomplete. Measured against this machine's own projects directory: the checkout
# `.../programs/llm_chat` resolves to `-Users-...-programs-llm-chat`, so the UNDERSCORE is
# mangled as well, and no directory there contains a byte outside [A-Za-z0-9-]. A resolver that
# replaced only `/` and `.` would miss every project with an underscore in its path and report
# "no transcript" for a session that has one.
_TRANSCRIPT_SLUG = re.compile(r"[^A-Za-z0-9]")


def transcript_path(tree, session):
    """The transcript file a session working in `tree` should be writing, or None.

    A DERIVATION AGAINST SOMEBODY ELSE'S LAYOUT, and every caller is told so rather than left
    to assume. showrunner does not own this path and is not told it; the rule is inferred from
    the directory names the host produces. So an absent file means "not where we looked", which
    is a fact about the derivation as much as about the session -- see `transcript_activity`,
    which refuses to collapse the two.

    The mangling is also LOSSY: `llm_chat`, `llm-chat` and `llm.chat` all slug to the same
    directory. The session id in the filename is what keeps the answer unambiguous, so a
    collision costs nothing here -- but a caller that ever derives a DIRECTORY alone and reads
    whatever sessions are in it would be reading a neighbour's.
    """
    if not tree or not session:
        return None
    tree = os.path.abspath(os.path.expanduser(str(tree)))
    return os.path.join(projects_root(), _TRANSCRIPT_SLUG.sub("-", tree), "%s.jsonl" % session)


def transcript_activity(tree, session):
    """When this session last wrote anything: {"path", "idle", "mtime", "why"}.

    `idle` is seconds since the transcript last changed, and it is **None whenever the file
    could not be read** -- with `why` naming which failure it was. A FAILED READ IS NOT A FACT
    ABOUT THE WORLD: "the session has been silent for an hour" and "the host stores transcripts
    somewhere this derivation does not reach" are indistinguishable to `stat`, and only the
    first says anything about the agent. Folding a missing file into a large idle time would
    manufacture a stalled verdict for every consumer whose host does not match the rule above
    -- a false positive on healthy runs, which is how a report stops being read.

    Nothing here decides anything. It reports a measurement; the threshold lives with the
    caller that has to choose when to speak.
    """
    path = transcript_path(tree, session)
    if not path:
        missing = "worktree" if not tree else "session id"
        return {"path": None, "idle": None, "mtime": None,
                "why": "the claim carries no %s, so no transcript can be derived for it"
                       % missing}
    try:
        mtime = int(os.path.getmtime(path))
    except OSError as exc:
        return {"path": path, "idle": None, "mtime": None,
                "why": "could not read %s (%s) -- NOT evidence that the session is silent, "
                       "only that nothing was readable where showrunner looked"
                       % (path, exc.__class__.__name__)}
    return {"path": path, "idle": max(0, now() - mtime), "mtime": mtime, "why": ""}


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


def caller_session():
    """This session's id, DISCOVERED rather than demanded. "" when nothing names one.

    A seat's re-seat-after-reload keys on the session id, and a seat claimed without one records
    `""` — which that feature deliberately refuses to match, because treating absent-as-equal
    would let any unidentified session inherit any unidentified seat. So a `role claim` run the
    obvious way produced a seat the feature could never rebind, and the operator had to know to
    pass `--session` for a mechanism whose whole purpose is that they should not have to think
    about it. Documenting that would have been documenting a footgun.

    ORDER IS EXPLICIT-FIRST. `SHOWRUNNER_SESSION` is showrunner's own override and stays ahead of
    the harness's variable, so a caller that sets it deliberately is never second-guessed.
    """
    return (os.environ.get("SHOWRUNNER_SESSION")
            or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CLAUDE_SESSION_ID")
            or "")


def package_root():
    """The checkout this code is running out of. ONLY the guards may anchor to it (#74).

    A guard must answer about a call happening right now; every other verb may — and must —
    refuse rather than guess which repo it meant. Kept here beside the resolver so the two
    facts sit together, rather than in whichever caller needed it first.
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main_checkout(start=None, fallback=None):
    """The MAIN checkout, even when called from inside a linked worktree.

    A worktree's `--show-toplevel` is the worktree; its `--git-common-dir` points at the
    main checkout's `.git`. Config, locks and the campaign record must resolve to ONE
    place for every Crawler (INV8), so they key off this rather than off the cwd.
    """
    for anchor in _root_anchors(start, fallback):
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


def _root_anchors(start, fallback=None):
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

    `fallback` IS LAST, AND ONLY THE GUARDS PASS ONE (#74). A guard is asked about a call that
    is happening right now and must answer something; every other verb is asked a question it
    is allowed to refuse, and MUST refuse rather than guess — `showrunner ready` run from a
    scratch directory has to say it cannot find a repo, never quietly answer about whichever
    checkout the binary happens to live in. Making this anchor global did exactly that, and the
    suite caught it: "the fallback did not turn 'cannot resolve' into a guess" is an assertion.

    So the anchor is a PARAMETER, not a fourth default. It is last, so it speaks only after
    every anchor that actually knows has declined, and under a central install the package root
    simply is not a git repo, which leaves the fail-open exactly as it was.
    """
    seen, out = set(), []
    for cand in (start, os.getcwd(), os.environ.get("CLAUDE_PROJECT_DIR"), fallback):
        if cand and cand not in seen and os.path.isdir(cand):
            seen.add(cand)
            out.append(cand)
    return out


def user_config_dir():
    """The ONE user-level directory showrunner keeps anything in. `~/.config/showrunner`.

    TWO MODULES ANSWER THE SAME QUESTION HERE, so neither may compute it. `roles.py` reads
    `roles.json` from user level and `config.py` reads `config.json` from user level; two
    expressions of "where is user level" is two layers free to disagree silently (INV12), and
    the disagreement would surface as a file the user wrote and the tool never read — which
    looks exactly like a setting that did not take effect.

    `XDG_CONFIG_HOME` is the override, and there is deliberately no second env var for it. The
    suite sets it to a temp dir before importing anything so no test reads the developer's real
    `~/.config` (#46); a showrunner-specific variable would have to be threaded into every
    subprocess test separately to buy the same isolation.
    """
    return os.path.join(
        os.path.expanduser(os.environ.get("XDG_CONFIG_HOME") or "~/.config"), "showrunner")


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
