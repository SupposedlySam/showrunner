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
# WHAT `install.sh` RECORDS ABOUT A COPY, beside the copy. A sibling of PINNED and deliberately
# not PINNED itself: `looks_pinned` reads PINNED + lib/showrunner as "a pin this verb may delete
# wholesale", and a copy install has lib/showrunner — so stamping PINNED into a consumer's
# `.showrunner/` would hand `self --pin --dest` permission to rm -rf their config and campaigns.
INSTALL_STAMP_FILE = "INSTALLED"
# What install.sh copies out of the source tree. A dirty README does not change the code a copy
# carries, so dirtiness is asked of these paths only — "dirty" means the copied code is not the
# commit it names, which is the one thing the flag exists to say.
COPIED_PATHS = ("bin/showrunner", "lib/showrunner", ".showrunner/hooks")
# The moving tag that marks the newest release on the public repo. Moved by the maintainer at
# each release; `stable-<sha8>` marks each one immutably. Nothing here creates or moves either.
RELEASE_TAG = "stable"
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

    Five provenances, and only four of them can name a commit:

      pinned    extracted by `self --pin` from a git ref. VERSION/PINNED sit beside the code
                and name the exact commit. The strongest answer, and the reason central mode
                can say what every project on the machine is running.
      checkout  the code lives in a git repo — a clone, or this repo working on itself. HEAD
                names it, and `dirty` says whether HEAD still describes what is actually here,
                because uncommitted edits make the SHA an overstatement.
      copy      `install.sh` copied it from a git checkout and stamped INSTALLED with the
                commit git resolved there, and whether the copied paths were dirty. A dirty
                copy names its BASE commit and says dirty; it is never that commit.
      declared  `install.sh` copied it from a tree with no `.git`, and whoever ran it passed
                `--source-commit`. Recorded and reported as a claim nobody verified.
      unknown   a copy with no stamp: no commit names this code and none can be invented. It
                is reported as the absence it is rather than filled in with the version
                literal and left to look like an answer.
    """
    root = code_root()
    info = {"version": None, "source": "unknown", "sha": None, "ref": None, "dirty": None,
            "root": root}
    from . import __version__
    info["version"] = __version__

    pinned = read_pin(root)
    if pinned:
        info.update(source="pinned", sha=pinned.get("sha"), ref=pinned.get("ref"),
                    pin_source=pinned.get("source"), track=pinned.get("track"),
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
    if not _is_toplevel(root):
        stamp = read_install_stamp(root)
        if stamp and stamp.get("unreadable"):
            # Still unknown — but said as "the stamp is there and cannot be read", which sends
            # a reader to the file rather than to a re-install they may not need.
            info["unreadable"] = stamp["unreadable"]
        elif stamp:
            info.update(source=stamp["kind"], sha=stamp["sha"], dirty=stamp.get("dirty"),
                        refs={"tags": list(stamp.get("tags") or []),
                              "branch": stamp.get("branch")},
                        origin=stamp.get("origin"), cut_from=stamp.get("path"),
                        at=stamp.get("at"))
        return info

    rc, out, _ = git(["rev-parse", "HEAD"], cwd=root)
    sha = (out or "").strip()
    if rc == 0 and sha:
        info.update(source="checkout", sha=sha)
        rc2, dirty, _ = git(["status", "--porcelain"], cwd=root)
        info["dirty"] = bool((dirty or "").strip()) if rc2 == 0 else None
        info["refs"] = _refs_at(root, sha)
        info["origin"] = _origin(root)
    return info


def _is_toplevel(root):
    """Is `root` itself a git checkout's top level — not merely somewhere inside one?

    An installed copy sits INSIDE the consumer's repo, and a packager's extracted tree can sit
    inside the packager's. Asking git there answers about the wrong repository, so only the
    exact toplevel counts.
    """
    rc, top, _ = git(["rev-parse", "--show-toplevel"], cwd=root)
    toplevel = (top or "").strip()
    return rc == 0 and bool(toplevel) and os.path.realpath(toplevel) == os.path.realpath(root)


def _strip_credentials(url):
    """A remote URL with any user:token removed. The stamp is a file people paste.

    Only URL-shaped remotes carry credentials in the authority (`https://u:tok@host/x`); an
    scp-style `git@host:x` names a login, not a secret, and is kept as written.
    """
    url = (url or "").strip()
    if "://" not in url:
        return url or None
    scheme, rest = url.split("://", 1)
    authority, sep, path = rest.partition("/")
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    return "%s://%s%s%s" % (scheme, authority, sep, path)


def _origin(repo):
    rc, out, _ = git(["remote", "get-url", "origin"], cwd=repo)
    return _strip_credentials(out) if rc == 0 else None


def _refs_at(repo, sha):
    """The tags pointing at `sha`, and the branch checked out when it is that commit."""
    rc, out, _ = git(["tag", "--points-at", sha], cwd=repo)
    tags = sorted(t for t in (out or "").split() if t) if rc == 0 else []
    branch = None
    rc, br, _ = git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=repo)
    if rc == 0 and (br or "").strip():
        rc2, head, _ = git(["rev-parse", "HEAD"], cwd=repo)
        if rc2 == 0 and (head or "").strip() == sha:
            branch = br.strip()
    return {"tags": tags, "branch": branch}


def _payload_dirty(source):
    """Does the source differ from HEAD in what install.sh copies? None when git cannot say.

    Untracked files count: `cp -R lib/showrunner` copies them too, so a new module nobody
    committed is in the copy and not in the commit. Bytecode does not: running the source
    binary (which install.sh does just before this) writes `__pycache__` into a tree that may
    not ignore it, and a cache is not a change to the code.
    """
    rc, out, _ = git(["status", "--porcelain", "--"] + list(COPIED_PATHS) +
                     [":(exclude,glob)**/__pycache__/**"], cwd=source)
    return bool((out or "").strip()) if rc == 0 else None


def _stamp_kind(verified):
    """'copy' when git resolved the commit in the source, 'declared' when a person named it.

    The distinction is the whole value of the stamp: SRCode will compare this sha against the
    release, and an unverified claim reported as verified is a confident wrong answer.
    """
    return "copy" if verified else "declared"


def _looks_like_sha(value):
    v = (value or "").strip().lower()
    return 7 <= len(v) <= 64 and all(c in "0123456789abcdef" for c in v)


def install_stamp(source, declared=None):
    """What `install.sh` should record about a copy cut from `source`. Never raises.

    Returns the stamp dict, or None when nothing names the code — no checkout at `source` and
    no declared commit — so the install stays honestly "unknown" instead of guessing.
    A checkout WINS over a declaration: git's answer is a fact, the flag is a claim, and when
    they disagree the stamp carries both so the disagreement is visible rather than resolved.
    """
    source = os.path.realpath(source)
    declared = (declared or "").strip().lower() or None
    stamp = None
    if _is_toplevel(source):
        rc, out, _ = git(["rev-parse", "HEAD"], cwd=source)
        sha = (out or "").strip()
        if rc == 0 and sha:
            refs = _refs_at(source, sha)
            stamp = {"kind": _stamp_kind(True), "sha": sha, "dirty": _payload_dirty(source),
                     "tags": refs["tags"], "branch": refs["branch"],
                     "origin": _origin(source), "path": source}
            if declared and not sha.startswith(declared):
                stamp["declared_mismatch"] = declared
    if stamp is None and declared:
        stamp = {"kind": _stamp_kind(False), "sha": declared, "dirty": None, "tags": [],
                 "branch": None, "origin": None, "path": source}
    if stamp is not None:
        stamp["at"] = now()
    return stamp


def write_install_stamp(dest, stamp):
    """Replace the stamp at `dest` — or remove it when `stamp` is None. Re-installs replace."""
    path = os.path.join(dest, INSTALL_STAMP_FILE)
    if stamp is None:
        if os.path.exists(path):
            os.remove(path)
        return None
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as fh:
        json.dump(stamp, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def read_install_stamp(root):
    """The INSTALLED stamp at `root`, None when absent, {"unreadable": ...} when broken.

    Missing and unreadable are different answers, for the reason `read_pin` gives.
    """
    path = os.path.join(root, INSTALL_STAMP_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"unreadable": str(exc)}
    if not isinstance(data, dict) or data.get("kind") not in ("copy", "declared") or \
            not _looks_like_sha(data.get("sha")):
        return {"unreadable": "%s names no kind and sha" % INSTALL_STAMP_FILE}
    return data


def as_json(info=None):
    """The `--version --json` object. ONE SCHEMA, pinned by a test, because SRCode parses it.

    Keys, always all six: version, sha (null when unknown), provenance (pinned | checkout |
    copy | declared | unknown), dirty (true/false, null when not known), source (the remote
    URL, else the path the code was cut from or lives at), refs ({"tags": [...], "branch":
    str|null}, or null when nothing recorded them).
    """
    d = info or running()
    kind = d.get("source")
    if kind == "pinned" and d.get("unreadable"):
        kind = "unknown"
    source = d.get("origin") or d.get("cut_from") or d.get("pin_source") or d.get("root")
    refs = d.get("refs")
    if kind == "pinned" and refs is None and d.get("sha") and d.get("pin_source") and \
            os.path.isdir(d["pin_source"]):
        # A pin records the ref it was ASKED for, which may be a tag or a branch — guessing
        # which would be inventing a ref. The clone it was cut from can say which tags point
        # at the sha; with no clone to ask, refs stays null ("not recorded"), never [].
        rc, out, _ = git(["tag", "--points-at", d["sha"]], cwd=d["pin_source"])
        if rc == 0:
            refs = {"tags": sorted(t for t in out.split() if t), "branch": d.get("track")}
    return {"version": d.get("version"), "sha": d.get("sha") if kind != "unknown" else None,
            "provenance": kind, "dirty": d.get("dirty"), "source": source, "refs": refs}


def _is_bare_sha(ref, sha):
    """Was the pin named by a commit hash rather than a branch or tag?"""
    r = (ref or "").lower()
    return len(r) >= 7 and all(c in "0123456789abcdef" for c in r) and \
        (sha or "").lower().startswith(r)


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
    if d["source"] in ("copy", "declared") and d.get("sha"):
        # THE STAMP NAMES A COMMIT, so "nothing records what it came from" would be false. What
        # it does not record is a ref to compare against without the network, so say that, and
        # name the one-line check that answers it.
        return ("warn", "this copy names %s%s, but whether it is BEHIND cannot be told here: "
                        "nothing local records the latest release. `git ls-remote --tags "
                        "<showrunner remote> refs/tags/%s` names it; if that sha differs, "
                        "upgrade." % (d["sha"][:12], " (declared, unverified)"
                                      if d["source"] == "declared" else "", RELEASE_TAG))
    if d["source"] != "pinned" or d.get("unreadable"):
        return ("warn", "whether this copy is BEHIND cannot be told: no readable pin records "
                        "what it came from, so nothing can be compared. A fix landing upstream "
                        "reaches this copy only when somebody re-vendors it, and nothing here "
                        "will say so. `self --pin <ref>` makes it answerable.")
    ref, sha = d.get("ref"), d.get("sha")
    repo = source_repo or d.get("pin_source") or d.get("root")
    if not ref or not sha or not repo:
        return ("warn", "pinned, but the stamp does not name both a ref and a commit, so "
                        "staleness cannot be computed.")
    if _is_bare_sha(ref, sha):
        if not d.get("track"):
            return ("warn", "pinned to the bare commit %s, which is level with itself by "
                            "definition — no branch was recorded to compare against, so CANNOT "
                            "TELL whether it is behind. `self --pin <branch>` makes it "
                            "answerable." % sha[:12])
        ref = d["track"]
    rc, out, _ = git(["rev-list", "--count", "%s..%s" % (sha, ref)], cwd=repo)
    if rc != 0 or not (out or "").strip().isdigit():
        why = ("the stamp names no source clone, so git was asked in the pinned copy, which is "
               "not a repository; re-pin with this version to record one"
               if not (source_repo or d.get("pin_source")) else
               "git could not resolve it in %s" % repo)
        return ("warn", "pinned at %s (%s), and that ref could not be resolved — %s. CANNOT "
                        "TELL whether it is behind, which is different from being current."
                        % (sha[:12], ref, why))
    behind = int(out.strip())
    if behind == 0:
        return ("ok", "pinned at %s and level with %s — nothing upstream is missing here."
                      % (sha[:12], ref))
    return ("warn", "this copy is BEHIND: %s carries %d commit(s) it does not have. A bug you "
                    "hit may already be fixed there, and a closed issue upstream is still live "
                    "here until you re-vendor. `self --pin %s` to move."
                    % (ref, behind, ref))


def self_pin_state(cfg, sr):
    """Is the SELF-VENDORED pin the hooks run behind this checkout's HEAD?

    A DIFFERENT QUESTION FROM `staleness`, and the confusion between them is why this went
    unowned. `staleness` asks whether the running COPY is behind the repo it was vendored from —
    a consumer's question. This asks whether `.showrunner_self`, the copy every hook in THIS
    checkout executes, is behind the working tree its developer is editing.

    Returns (level, message) or None when no self-pin is in force — None meaning "this does not
    apply here", never "it is fine".

    IT COST SOMETHING TWICE IN ONE DAY, which is what makes it worth a function rather than a
    line inside `doctor`:
      * a consumer's `reach` gate went silent for every call, because the pin predated the
        `reach` verb;
      * after a window reload, the seat announcement printed wording replaced in #77, because
        the pin was sixteen commits behind while the checkout read as current.
    `doctor` said so accurately both times. Nobody had run `doctor`.
    """
    if not sr:
        return None
    root = cfg.root
    vendored = os.path.join(root, ".showrunner_self")
    try:
        inside = os.path.realpath(sr).startswith(os.path.realpath(vendored) + os.sep)
    except OSError:
        return None
    if not inside:
        return None
    stamp = read_pin(vendored) or {}
    sha = stamp.get("sha")
    if stamp.get("unreadable") or not sha:
        return ("warn", "the plumbing runs .showrunner_self and its stamp is UNREADABLE, so the "
                        "rules your hooks enforce cannot be named. `self --pin HEAD --dest "
                        ".showrunner_self`")
    rc, head, _ = git(["rev-parse", "HEAD"], cwd=root)
    head = (head or "").strip() if rc == 0 else ""
    if not head:
        # THE UNDEFINED CASE IS THE LOUD ONE. Every branch here needs HEAD; without it the
        # original inline version printed nothing at all, and a pin of unknown age reported as
        # silence reads exactly like a current one.
        return ("warn", "from the self-vendored pin at %s, and this checkout WOULD NOT SAY WHAT "
                        "HEAD IS, so whether your guards are current CANNOT BE "
                        "DETERMINED. That is not the same as up to date. Refresh blind, or fix "
                        "git here: `bin/showrunner self --pin HEAD --dest .showrunner_self`"
                        % sha[:12])
    if sha == head:
        return ("ok", "the plumbing runs .showrunner_self at %s, which IS this checkout's HEAD."
                      % sha[:12])
    rc_c, behind, _ = git(["rev-list", "--count", "%s..HEAD" % sha], cwd=root)
    n = (behind or "").strip() if rc_c == 0 else "?"
    return ("warn", "your hooks are running .showrunner_self pinned at %s, %s commit(s) BEHIND "
                    "HEAD — they are enforcing the rules as of that commit, not the ones you "
                    "are editing. Deliberate while you work, stale if you forget. Refresh: "
                    "`bin/showrunner self --pin HEAD --dest .showrunner_self`" % (sha[:12], n))


def describe():
    """One line for `--version`. Says which provenance it is, and never invents a commit."""
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
    if d["source"] == "copy":
        return "%s · copied from %s (%s) · %s" % (
            base, (d["sha"] or "?")[:12],
            "dirty — the copied code had uncommitted changes, so it is NOT that commit; that "
            "is its base" if d["dirty"] else
            "clean" if d["dirty"] is False else "dirtiness unknown",
            d.get("origin") or d.get("cut_from") or d["root"])
    if d["source"] == "declared":
        return ("%s · declared %s — named with --source-commit by whoever installed it, from a "
                "tree with no .git, so nothing verified it · %s"
                % (base, (d["sha"] or "?")[:12], d["root"]))
    if d.get("unreadable"):
        return ("%s · copied, and its %s stamp CANNOT BE READ (%s), so which commit is unknown "
                "rather than absent. Re-run install.sh. · %s"
                % (base, INSTALL_STAMP_FILE, d["unreadable"], d["root"]))
    return ("%s · copied from a working tree, so NO commit names this code. Re-install from a "
            "git clone, or `self --pin`, to make this answerable. · %s" % (base, d["root"]))


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

    # WHAT WAS HERE, read before it is replaced: an update to a copy repos already run and a
    # first populate are different events, and only this side can tell them apart afterwards.
    replaced = (read_pin(dest) or {}).get("sha") if os.path.exists(dest) else None

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

        # THE CLONE IT WAS CUT FROM, so staleness can ask git there (#112). The pinned copy is
        # not a repository, so without this every central consumer read CANNOT TELL for a ref
        # that resolved fine in the clone that made the pin.
        stamp = {"ref": ref, "sha": sha, "at": now(), "source": os.path.realpath(source)}
        if _is_bare_sha(ref, sha):
            # A BARE COMMIT IS LEVEL WITH ITSELF by definition, so "behind" needs a branch to
            # mean anything: the one checked out in the source, when it contains the commit.
            rc, br, _ = git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=source)
            br = (br or "").strip()
            if rc == 0 and br and git(["merge-base", "--is-ancestor", sha, br],
                                      cwd=source)[0] == 0:
                stamp["track"] = br
        with open(os.path.join(staging, VERSION_FILE), "w") as fh:
            fh.write(sha + "\n")
        with open(os.path.join(staging, PINNED_FILE), "w") as fh:
            # NO "home", following game_loop's reasoning rather than inventing a second one: a
            # --dest checkout is meant to serve many consumers, so naming whichever consumer
            # happened to run the command is a fact that reads as ownership and is not one.
            # "source" is different: it is the showrunner clone, the only place git can answer.
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
            "binary": os.path.join(dest, "bin", "showrunner"), "replaced": replaced}
