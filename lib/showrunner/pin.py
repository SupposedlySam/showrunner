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
