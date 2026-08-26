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


def source_root():
    """The checkout a pin extracts FROM. The running code's root, or a refusal.

    THE MIRROR OF `code_root`, and it is here because the write side never had one. `pin`
    resolved its source from `cfg.root` — the git root of the CWD — while `running()` argues
    at length that provenance may never be resolved that way. One module, two opposite rules,
    and the printed remedy walked straight into the wrong one: `self --pin <ref> --dest
    $central` is what the shim and the installer tell a reader standing in their OWN project,
    so the source repo was theirs. Two outcomes, both observed:

      it fails    their repo has no `bin`+`lib` to archive — after `pin` has already deleted
                  the machine-wide central install every project on the box dispatches to.
      it SUCCEEDS their repo happens to have those paths, so central is now serving that
                  project's code as showrunner, stamped with a real SHA and exit 0.

    So the source is where this code lives, and it must BE a checkout — the same toplevel
    identity check `running()` makes, for the same reason: an installed copy sits INSIDE the
    consumer's repo, and asking git for a ref there answers about the wrong repository.
    """
    root = code_root()
    rc, top, _ = git(["rev-parse", "--show-toplevel"], cwd=root)
    toplevel = (top or "").strip()
    if rc != 0 or not toplevel or os.path.realpath(toplevel) != os.path.realpath(root):
        raise Refused(
            "self --pin: the running code at %s is not itself a git checkout, so there is no "
            "ref here to pin. A pin extracts from the repository showrunner's own code lives "
            "in — never from the project you are standing in, whose HEAD would be published "
            "as showrunner. Run this from a clone of showrunner." % root)
    return root


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
        info.update(source="pinned", sha=pinned.get("sha"), ref=pinned.get("ref"),
                    unreadable=pinned.get("unreadable"))
        # An edited pin is not the commit it claims, and read_pin already knows. `dirty` is left
        # None when the stamp could not be read: "edited since it was pinned" is a finding, and
        # deriving it from a failed READ would state it on no evidence.
        info["dirty"] = None if pinned.get("unreadable") else not pinned.get("consistent")
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


def staleness(source_repo=None):
    """Is this copy behind the ref it came from? (level, message) or None if not applicable.

    THE GAP THIS CLOSES, reported by a consumer who hit a bug that had been FIXED UPSTREAM three
    commits earlier and spent the evening rediscovering it. `doctor` already warned that a
    copied install is unattributable — but that is a claim about PROVENANCE, and it fires
    identically whether the copy is current or twenty commits behind. It carries no signal about
    whether a known fix is present, so a closed issue and a live one look the same from the
    consumer's side.

    Answers honestly in three ways rather than two, which is the distinction the warning above
    could not make:
      * pinned, ref resolvable  -> how many commits the ref has that this copy does not
      * pinned, ref unreachable -> CANNOT TELL, and says which ref it could not reach
      * not pinned              -> CANNOT TELL, because nothing records what to compare against

    "Cannot tell" is not "up to date". A copy with no pin has no answer available, and printing
    silence there is what made the original bug cost an evening.
    """
    d = running()
    if d["source"] == "checkout":
        # A CHECKOUT IS ITS OWN SOURCE, and git can answer exactly — telling a developer
        # "cannot tell" here would be the check inventing an unknown it does not have. Compared
        # against the tracking ref, which is the only thing that can be ahead of them.
        repo = d.get("root")
        rc, out, _ = git(["rev-list", "--count", "HEAD..@{upstream}"], cwd=repo)
        if rc != 0 or not (out or "").strip().isdigit():
            return ("ok", "this IS a showrunner checkout, so it is its own source — nothing to "
                          "re-vendor. No upstream branch is tracked, so 'behind' has no meaning "
                          "here.")
        behind = int(out.strip())
        if behind:
            return ("warn", "this checkout is %d commit(s) behind its upstream branch — a fix "
                            "you are about to write may already be there." % behind)
        return ("ok", "this IS a showrunner checkout and it is level with its upstream branch.")
    if d["source"] != "pinned" or d.get("unreadable"):
        return ("warn", "whether this copy is BEHIND cannot be told: no readable pin records "
                        "what it came from, so nothing can be compared. A fix landing upstream "
                        "reaches this copy only when somebody re-vendors it, and nothing here "
                        "will say so. `self --pin <ref>` makes it answerable.")
    ref, sha = d.get("ref"), d.get("sha")
    repo = source_repo or d.get("root")
    if not ref or not sha or not repo:
        return ("warn", "pinned, but the stamp does not name both a ref and a commit, so "
                        "staleness cannot be computed.")
    rc, out, _ = git(["rev-list", "--count", "%s..%s" % (sha, ref)], cwd=repo)
    if rc != 0 or not (out or "").strip().isdigit():
        return ("warn", "pinned at %s (%s), and that ref could not be resolved here — CANNOT "
                        "TELL whether it is behind, which is different from being current."
                        % (sha[:12], ref))
    behind = int(out.strip())
    if behind == 0:
        return ("ok", "pinned at %s and level with %s — nothing upstream is missing here."
                      % (sha[:12], ref))
    return ("warn", "this copy is BEHIND: %s carries %d commit(s) it does not have. A bug you "
                    "hit may already be fixed there, and a closed issue upstream is still live "
                    "here until you re-vendor. `self --pin %s` to move."
                    % (ref, behind, ref))


def describe():
    """One line for `--version`. Says which of the three it is, and never invents a commit."""
    d = running()
    base = "showrunner %s" % d["version"]
    if d["source"] == "pinned":
        if d.get("unreadable"):
            # Neither "pinned at X" nor "no commit names this" — both would be inventions. The
            # directory IS a pin; what cannot be read is which commit, and that is the answer.
            return ("%s · pinned, STAMP UNREADABLE (%s) — this directory was pinned and the "
                    "file naming the commit cannot be read, so which commit is unknown rather "
                    "than absent. Re-pin it. · %s" % (base, d["unreadable"], d["root"]))
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
    # MISSING AND UNREADABLE ARE DIFFERENT ANSWERS, and collapsing them here produced a
    # positive claim about provenance out of a caught exception: a genuinely pinned directory
    # whose stamp was truncated fell through to `source="copy"` and `--version` said "copied
    # from a working tree, so NO commit names this code" — with VERSION sitting beside it
    # naming the commit. That is this module's own stated anti-pattern (report an absence as
    # the absence it is, rather than filling it in and leaving it to look like an answer)
    # applied to the wrong absence.
    if not os.path.exists(os.path.join(dest, PINNED_FILE)):
        return None
    try:
        with open(os.path.join(dest, PINNED_FILE)) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"unreadable": str(exc), "sha": None, "ref": None,
                "version": None, "consistent": False}
    if not isinstance(data, dict) or not data.get("sha"):
        return {"unreadable": "%s carries no sha" % PINNED_FILE, "sha": None, "ref": None,
                "version": None, "consistent": False}
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


def extract(source, sha, dest):
    """`git archive <sha> bin lib` from `source` into dest, through stdlib tarfile.

    `source` is a showrunner checkout — see `source_root`. It is a parameter rather than a
    config field because the config describes the CONSUMER's project, and the consumer's
    project has no say in what code gets published as showrunner.
    """
    import subprocess
    import tarfile

    proc = subprocess.Popen(["git", "-C", source, "archive", "--format=tar", sha] +
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


def pin(ref, dest, source=None):
    """Extract the tool at `ref` into `dest` and stamp it. Returns detail. Raises Refused.

    Pinned to a REF, never to a working tree. A working-tree copy cannot answer "what is
    central running" with anything checkable, and under a central install that question is
    asked about every consumer on the machine at once.

    NO CONFIG. It took one deliberately: the consumer's `Config`, whose `root` then chose the
    repository to publish as showrunner. `source` defaults to `source_root()` — where this
    code lives — and is a parameter only so a test can point it somewhere synthetic.

    THE DESTINATION IS NOT TOUCHED UNTIL A REPLACEMENT EXISTS. Everything lands in a staging
    directory beside it and is renamed into place at the end. The previous order deleted
    `dest` first and validated after, so the failure path — a ref that carries no payload —
    left the machine with no central install at all and a recovery message naming the command
    that had just removed it. A failed pin now leaves whatever was already there running.
    """
    dest = os.path.abspath(os.path.expanduser(dest))
    source = source or source_root()

    rc, out, _ = git(["rev-parse", "%s^{commit}" % ref], cwd=source)
    sha = (out or "").strip()
    if rc != 0 or not sha:
        raise Refused("self --pin: git cannot resolve %r in %s — name a commit, tag or branch "
                      "that exists in the showrunner checkout." % (ref, source))

    if os.path.exists(dest) and not looks_pinned(dest):
        raise Refused(
            "self --pin: %s exists and is not a pinned checkout — refusing to delete it. "
            "A pin overwrites its destination wholesale, so it will only ever overwrite "
            "something it recognises as its own. Move it aside yourself." % dest)

    staging = "%s.pinning.%d" % (dest, os.getpid())
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)

    try:
        extract(source, sha, staging)

        bindir = os.path.join(staging, "bin")
        for name in sorted(os.listdir(bindir)):
            path = os.path.join(bindir, name)
            if os.path.isfile(path):
                os.chmod(path, 0o755)

        stamp = {"ref": ref, "sha": sha, "at": now()}
        with open(os.path.join(staging, VERSION_FILE), "w") as fh:
            fh.write(sha + "\n")
        with open(os.path.join(staging, PINNED_FILE), "w") as fh:
            # NO "home" AND NO SOURCE REPO, following game_loop's reasoning rather than
            # inventing a second one: a --dest checkout is meant to serve many consumers, so
            # naming whichever repo happened to run the command is a fact that reads as
            # ownership and is not one.
            json.dump(stamp, fh, indent=2)
            fh.write("\n")
    except Exception as exc:                        # noqa: BLE001 — see below
        # A HALF-WRITTEN PIN IS WORSE THAN NONE: it is a central directory that exists, looks
        # installed to anything checking for a path, and cannot run. Clean up and say why —
        # and `dest` has not been touched, so what was there is still what is running.
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, Refused):
            raise
        raise Refused("self --pin: could not extract the tool at %s — %s. %s is UNCHANGED."
                      % (sha[:8], exc, dest))

    # The one unavoidable window, and it is between two local renames rather than around a
    # network fetch and a validation. `dest` is known to be one of ours by the check above.
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.rename(staging, dest)

    return {"ref": ref, "sha": sha, "dest": dest, "at": stamp["at"], "source": source,
            "binary": os.path.join(dest, "bin", "showrunner")}
