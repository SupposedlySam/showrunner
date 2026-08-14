"""Pin showrunner's own code at a named git ref, so "what is central running" is a commit.

A central install serves many repos from one copy, which means one bad copy reaches all of
them at once. The mitigation is not "be careful" — it is that the copy is extracted from a
**named git ref** rather than copied from somebody's working tree, and stamps what it
extracted. A working-tree copy answers "what is central running" with a vibe; a pinned one
answers with a SHA that can be checked out, diffed and blamed.

Mirrors game_loop's `self --pin --dest`, which is already running on this machine. Where this
differs it differs for a reason stated at the point of difference, because two tools with the
same job and quietly different rules is the drift this whole feature exists to stop.

**What this does NOT establish.** `VERSION` names what was EXTRACTED, not what is there now:
nothing stops a later edit inside the central directory, and nothing here would notice. A
content hash would close that and is deliberately not built yet — it is a different promise,
and half of it would be worse than none. Stated here rather than in the plan alone, because
the reader who needs it is standing at this function.
"""

import json
import os
import shutil

from .util import Refused, git, now

VERSION_FILE = "VERSION"
PINNED_FILE = "PINNED"
# The two directories that ARE the tool. `.showrunner/` is the consumer's state and config and
# is emphatically not extracted — a central copy carrying one project's config is the misread
# that makes a shared install report on the wrong repo.
PAYLOAD = ("bin", "lib")


def code_root():
    """The directory the RUNNING code lives in. Never the cwd's repo.

    That distinction is the whole point. Under a central install the code lives in
    `~/.claude/showrunner-central` while the CWD is some consumer project, and resolving
    provenance from the cwd would answer with that project's HEAD — a confident, precise,
    completely wrong statement about which showrunner is executing. The question "what code is
    this" is answered by where the code is, and nowhere else.
    """
    # <root>/lib/showrunner/pin.py -> <root>
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def running():
    """What code is executing, and what names it. Returns a dict; never raises.

    THE VERSION STRING ALONE IS A LIE BY OMISSION. `__version__` has read "0.1.0" since the
    first commit and has never been bumped, so `--version` could not distinguish a checkout
    from this morning from an install three weeks stale — establishing that seven consumers
    were out of date needed a file-by-file diff, because the one field whose job is to answer
    that question could not.

    Three provenances, and only two of them can name a commit:

      pinned    extracted by `self --pin` from a git ref. VERSION/PINNED sit beside the code
                and name the exact commit. The strongest answer, and the reason central mode
                can say what every project on the machine is running.
      checkout  the code lives in a git repo — a clone, or this repo working on itself. HEAD
                names it, and `dirty` says whether HEAD still describes what is actually here,
                because uncommitted edits make the SHA an overstatement.
      copy      `install.sh` without --central copies from a working tree. There is NO commit
                that names this code and none can be invented — that is precisely the argument
                for pinning, and it is reported as the absence it is rather than filled in
                with the version literal and left to look like an answer.
    """
    root = code_root()
    info = {"version": None, "source": "copy", "sha": None, "ref": None, "dirty": None,
            "root": root}
    from . import __version__
    info["version"] = __version__

    pinned = read_pin(root)
    if pinned:
        info.update(source="pinned", sha=pinned.get("sha"), ref=pinned.get("ref"))
        # An edited pin is not the commit it claims, and read_pin already knows.
        info["dirty"] = not pinned.get("consistent")
        return info

    # THE CODE ROOT MUST *BE* THE REPO, not merely sit inside one. A plain `install.sh` copy
    # lands at `<consumer>/.showrunner/`, which is inside the CONSUMER's git repo — so asking
    # git for HEAD there answers with the consumer's commit and reports it as showrunner's
    # version. Confident, precise, and about the wrong repository entirely.
    #
    # Caught by running it: a fresh copy reported `checkout 943e2449`, which was the throwaway
    # test project's own seed commit. The guard is exact rather than heuristic — a real
    # checkout's root IS the toplevel; every installed layout is a subdirectory of one.
    rc, top, _ = git(["rev-parse", "--show-toplevel"], cwd=root)
    toplevel = (top or "").strip()
    if rc != 0 or not toplevel or os.path.realpath(toplevel) != os.path.realpath(root):
        return info

    rc, out, _ = git(["rev-parse", "HEAD"], cwd=root)
    sha = (out or "").strip()
    if rc == 0 and sha:
        info.update(source="checkout", sha=sha)
        rc2, dirty, _ = git(["status", "--porcelain"], cwd=root)
        info["dirty"] = bool((dirty or "").strip()) if rc2 == 0 else None
    return info


def describe():
    """One line for `--version`. Says which of the three it is, and never invents a commit."""
    d = running()
    base = "showrunner %s" % d["version"]
    if d["source"] == "pinned":
        return "%s · pinned %s (%s)%s · %s" % (
            base, (d["sha"] or "?")[:12], d["ref"] or "?",
            "  ← EDITED SINCE IT WAS PINNED, so that sha no longer describes this code"
            if d["dirty"] else "", d["root"])
    if d["source"] == "checkout":
        return "%s · checkout %s (%s) · %s" % (
            base, (d["sha"] or "?")[:12],
            "dirty — uncommitted changes, so this sha overstates what is here"
            if d["dirty"] else "clean", d["root"])
    return ("%s · copied from a working tree, so NO commit names this code. `self --pin` is "
            "what makes this answerable. · %s" % (base, d["root"]))


def read_pin(dest):
    """What is pinned at `dest`, or None. The READ SIDE, written with the write side.

    This repo shipped a `Lock.acquire` whose `extra` field had a write side and no read side,
    so the one caller that needed the value got a blank and printed "?" in the report the field
    existed for. A stamp nobody can read back is a comment in a file. `doctor` and the campaign
    record are the callers that will need this (CI-04, CI-05); it is written now, beside the
    thing it reads, rather than later by somebody inferring the format.
    """
    try:
        with open(os.path.join(dest, PINNED_FILE)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("sha"):
        return None
    try:
        with open(os.path.join(dest, VERSION_FILE)) as fh:
            data["version"] = fh.read().strip()
    except OSError:
        data["version"] = None
    # SAID OUT LOUD RATHER THAN RECONCILED SILENTLY. These two files are written together and
    # can only disagree if something edited the directory afterwards — which is exactly the
    # case this module admits it cannot otherwise detect, so it is surfaced where it is visible
    # instead of being papered over by preferring one of them.
    data["consistent"] = data.get("version") == data.get("sha")
    return data


def looks_pinned(dest):
    """Is `dest` a pin we may overwrite? Deliberately narrow: it decides a deletion.

    Answering yes on a directory that merely EXISTS would make a mistyped `--dest` delete a
    home directory. So it demands the marker this module itself writes, and treats everything
    else — including an empty directory or a half-extracted one — as 'not ours'.
    """
    return os.path.isfile(os.path.join(dest, PINNED_FILE)) and \
        os.path.isdir(os.path.join(dest, "lib", "showrunner"))


def extract(cfg, sha, dest):
    """`git archive <sha> bin lib` into dest, through stdlib tarfile — no `tar` dependency."""
    import subprocess
    import tarfile

    proc = subprocess.Popen(["git", "-C", cfg.root, "archive", "--format=tar", sha] +
                            list(PAYLOAD), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            try:
                tf.extractall(dest, filter="data")   # 3.12+: refuse paths that escape dest
            except TypeError:
                tf.extractall(dest)
    finally:
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        if proc.stdout:
            proc.stdout.close()
        if proc.wait() != 0:
            raise RuntimeError(err.strip() or "git archive exited %s" % proc.returncode)

    # THE COMMIT MIGHT NOT CARRY THE TOOL. A ref that predates a rename extracts cleanly and
    # produces a directory that looks installed and cannot run — the dead-path shape this repo
    # has now shipped twice. Checked here, where it can still be cleaned up.
    binary = os.path.join(dest, "bin", "showrunner")
    if not os.path.isfile(binary) or not os.path.isdir(os.path.join(dest, "lib", "showrunner")):
        raise RuntimeError("that commit carries no bin/showrunner and lib/showrunner — it "
                           "predates this layout, or names the wrong repo")


def pin(cfg, ref, dest):
    """Extract the tool at `ref` into `dest` and stamp it. Returns detail. Raises Refused.

    Pinned to a REF, never to a working tree. A working-tree copy cannot answer "what is
    central running" with anything checkable, and under a central install that question is
    asked about every consumer on the machine at once.
    """
    dest = os.path.abspath(os.path.expanduser(dest))

    rc, out, _ = git(["rev-parse", "%s^{commit}" % ref], cwd=cfg.root)
    sha = (out or "").strip()
    if rc != 0 or not sha:
        raise Refused("self --pin: git cannot resolve %r in %s — name a commit, tag or branch "
                      "that exists here." % (ref, cfg.root))

    if os.path.exists(dest):
        if not looks_pinned(dest):
            raise Refused(
                "self --pin: %s exists and is not a pinned checkout — refusing to delete it. "
                "A pin overwrites its destination wholesale, so it will only ever overwrite "
                "something it recognises as its own. Move it aside yourself." % dest)
        shutil.rmtree(dest)
    os.makedirs(dest)

    try:
        extract(cfg, sha, dest)
    except Exception as exc:                        # noqa: BLE001 — see below
        # A HALF-WRITTEN PIN IS WORSE THAN NONE: it is a central directory that exists, looks
        # installed to anything checking for a path, and cannot run. Clean up and say why.
        shutil.rmtree(dest, ignore_errors=True)
        raise Refused("self --pin: could not extract the tool at %s — %s" % (sha[:8], exc))

    bindir = os.path.join(dest, "bin")
    for name in sorted(os.listdir(bindir)):
        path = os.path.join(bindir, name)
        if os.path.isfile(path):
            os.chmod(path, 0o755)

    stamp = {"ref": ref, "sha": sha, "at": now()}
    with open(os.path.join(dest, VERSION_FILE), "w") as fh:
        fh.write(sha + "\n")
    with open(os.path.join(dest, PINNED_FILE), "w") as fh:
        # NO "home" AND NO SOURCE REPO, following game_loop's reasoning rather than inventing a
        # second one: a --dest checkout is meant to serve many consumers, so naming whichever
        # repo happened to run the command is a fact that reads as ownership and is not one.
        json.dump(stamp, fh, indent=2)
        fh.write("\n")

    return {"ref": ref, "sha": sha, "dest": dest, "at": stamp["at"],
            "binary": os.path.join(dest, "bin", "showrunner")}
