"""Provisioning the per-agent harness into a Crawler's worktree.

A harness that resolves its commit gate **per tree** must refuse when the tree being
committed carries no record. That lands on the orchestrator, because `git worktree add`
copies tracked files only: an untracked harness never crosses.

The quiet failure is worse than the loud one. An installer seeds user-owned files only
when absent, so a naive install into a fresh worktree yields the template's rules — a
`verify.yaml` that owes nothing and reports success, default invariants, an emptied write
allowlist. Nothing errors; the party simply plays by two rule sets, and the weaker one is
running unattended in N worktrees.

**showrunner does not decide what "the same harness" means — the harness does.** An earlier
version of this module hardcoded which files were rules and compared them itself. That was
the wrong layer, and it was already drifting: it knew nothing of the harness's notes tier,
so a diverged ledger was invisible to it. The harness now answers both questions itself:

    <harness>/bin/<name> owned --porcelain      # the owned set, each flagged rule or not
    <harness>/bin/<name> worktree --porcelain   # this tree vs its parent
        exit 0  clean          every owned file is byte-identical
        exit 1  drifted        a DETERMINED finding that the trees enforce different things,
                               whether the difference is in the rule files or in the harness's
                               own scripts  -> ABORT the spawn
        exit 2  undetermined   unreadable / not a worktree / no parent harness -> ABORT
        exit 3  notes drifted  ordinary for per-tree notes  -> warn, carry on

Exit 2 never shares a code with anything that was actually compared, so "could not tell"
cannot be mistaken for "clean" — which is the whole reason to ask the harness rather than
guess.

**1 means drifted, not "rules drifted", and that is a correction.** A drifted harness SCRIPT
used to have no entry in the harness's exit map and fell to the default 2, so showrunner
aborted for the right reason under the wrong name — it reported that the tool could not tell,
when the tool had read both files and found them different. The action was already correct
here, which is why nothing broke and why nothing surfaced it either. Anything downstream that
reads 2 as "abort, could not tell" now needs 1 to mean "abort, and we know why".

**What it verifies grew, and this module used to overstate the limit rather than the reach.**
It once appended "not checked: that both trees run the same harness CODE — bin/ is not in the
owned set", which was true when written and is now false: the harness's `worktree` comparison
covers its scripts as well as its owned files, so a drifted `bin/` is caught. The caveat
survived the upgrade and was being printed to every Crawler — a stale claim about the layer
below, which is the failure INV14 exists for and the second time this project has shipped one.

The limit that IS real: the hook-registration file lives outside the harness directory, so the
harness cannot compare it. showrunner refuses a spawn when it would be absent — a different
check, by a different party, and worth naming as such rather than folding into the harness's
verdict.

A harness that does not answer those verbs gets a refusal naming them, not a substitute
comparison invented here — guessing which files are rules is the hardcoded list this module
was rewritten to delete, and it would rot the same way. `harness.require=false` is the escape
hatch for anyone who accepts an unverified Crawler.

Two things showrunner must NOT do, both learned by getting them wrong:

* **Never copy the hook-registration file.** The harness's installer *merges* its hooks into
  it, preserving the project's own statusLine, permissions and unrelated hooks — and warning
  about pre-existing non-harness hooks on the events it manages, because a stray Stop hook
  from an older harness fights it over turn-ends and presents as "the orchestrator is
  mysteriously flaky." A wholesale copy discards the settings and silently drops the warning.
* **Never hand over another session's runtime state.** The exclusion list is read from the
  harness's own `.gitignore`, which is the authoritative declaration.
"""

import fnmatch
import json
import os
import shutil

from .util import run

KNOWN_HARNESS_DIRS = (".game_loop", ".loop")
HOOK_REGISTRATION = ".claude/settings.json"

CLEAN, DRIFTED, UNDETERMINED, NOTES_DRIFTED = 0, 1, 2, 3

# Which verdict wins when a tree carries more than one harness directory. "Could not tell"
# outranks every determined finding, because a determined finding about the files that WERE
# read says nothing about the one that was not — the harness ranks its own branches that way,
# and a consumer that ranked them differently would report the milder half of a mixed tree.
SEVERITY = {CLEAN: 0, NOTES_DRIFTED: 1, DRIFTED: 2, UNDETERMINED: 3}

FALLBACK_RUNTIME = [
    "state.json", "sessions/", "edited.txt", "log.jsonl", "verified.json", "probe/",
    "*.pid", ".state.*.tmp", "notify.json", "limits.json",
]


def spec(cfg):
    raw = dict(cfg.get("harness") or {})
    dirs = raw.get("dirs")
    if dirs is None:
        dirs = [d for d in KNOWN_HARNESS_DIRS if os.path.isdir(os.path.join(cfg.root, d))]
    return {
        "dirs": dirs,
        "provision": raw.get("provision", "auto"),   # auto | off
        "require": raw.get("require", True),
        "installer": raw.get("installer"),           # path to the harness's install script
        "install_args": raw.get("install_args", ["--same-as", "{parent}", "{worktree}"]),
    }


def bin_for(tree, dirname):
    """The harness's project-local binary, by its own convention: .game_loop/bin/game_loop."""
    return os.path.join(tree, dirname, "bin", dirname.lstrip("."))


def _porcelain(binary, verb):
    """Run a harness verb. Returns (exit_code, payload_or_None)."""
    if not os.access(binary, os.X_OK):
        return None, None
    rc, out, _ = run([binary, verb, "--porcelain"], cwd=os.path.dirname(binary), timeout=60)
    try:
        return rc, json.loads(out)
    except (ValueError, TypeError):
        return rc, None


def owned(cfg, dirname):
    """What the harness declares it owns. None when it exposes no such verb."""
    rc, payload = _porcelain(bin_for(cfg.root, dirname), "owned")
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------- runtime state
def runtime_globs(harness_root):
    """What the harness itself declares is runtime, from its own .gitignore."""
    globs = []
    try:
        with open(os.path.join(harness_root, ".gitignore")) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith(("#", "!")):
                    globs.append(line)
    except OSError:
        pass
    return globs or list(FALLBACK_RUNTIME)


def _is_runtime(relpath, globs):
    parts = relpath.split(os.sep)
    for g in globs:
        bare = g.rstrip("/")
        if g.endswith("/") and (bare in parts[:-1] or parts[0] == bare):
            return True
        if fnmatch.fnmatch(relpath, g) or fnmatch.fnmatch(parts[-1], bare):
            return True
        if relpath.startswith(bare + os.sep):
            return True
    return False


def _copy_excluding_runtime(src, dst, globs):
    copied, skipped = [], []
    for dirpath, dirnames, filenames in os.walk(src):
        relroot = os.path.relpath(dirpath, src)
        relroot = "" if relroot == "." else relroot
        for d in list(dirnames):
            rp = os.path.join(relroot, d) if relroot else d
            if _is_runtime(rp + "/", globs) or _is_runtime(rp, globs):
                dirnames.remove(d)
                skipped.append(rp)
        for f in filenames:
            rp = os.path.join(relroot, f) if relroot else f
            if _is_runtime(rp, globs):
                skipped.append(rp)
                continue
            target = os.path.join(dst, rp)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(dirpath, f), target)   # preserves the exec bit
            copied.append(rp)
    return copied, skipped


def tracked_top_levels(cfg):
    rc, out, _ = run(["git", "ls-files"], cwd=cfg.root)
    return {l.split("/")[0] for l in out.splitlines() if l.strip()} if rc == 0 else set()


# ------------------------------------------------------------------- provisioning
def _install(cfg, sp, worktree_path):
    installer = sp["installer"]
    if not installer:
        return None
    path = installer if os.path.isabs(installer) else os.path.join(cfg.root, installer)
    if not os.access(path, os.X_OK):
        return "configured harness installer is not executable: %s" % path
    args = [a.format(parent=cfg.root, worktree=worktree_path) for a in sp["install_args"]]
    rc, out, err = run([path] + args, cwd=os.path.dirname(path), timeout=300)
    if rc != 0:
        return "harness installer failed (%s): %s" % (rc, (err or out).strip()[:500])
    return None


CONTRACT_CODES = (CLEAN, DRIFTED, UNDETERMINED, NOTES_DRIFTED)


def _verify_with_harness(worktree_path, dirname):
    """Ask the harness whether this tree matches its parent. Authoritative when available.

    Returns (None, None) when the harness is not answering this contract at all — a missing
    verb, a usage error, unparseable output. That is a different thing from the harness
    *answering* "undetermined" (exit 2), and conflating the two would let a harness that
    does not implement the verb look like one reporting a real problem. The signal for
    "answering the contract" is a JSON payload, not the exit code alone.
    """
    binary = bin_for(worktree_path, dirname)
    rc, payload = _porcelain(binary, "worktree")
    if rc is None or not isinstance(payload, dict) or rc not in CONTRACT_CODES:
        return None, None
    return rc, payload


def check_tree(cfg, worktree_path):
    """Re-ask the harness whether a worktree still matches its parent. Read-only.

    Spawn-time verification is not enough: a Crawler can edit its own rule files after it
    starts, and a weakened `verify.yaml` means its commit gate stops owing what the
    orchestrator's owes. The whole point of the byte-compare is that the party plays by one
    rule set, and checking only at t=0 verifies that for exactly one instant.

    Returns (status, detail, mis_certified) where status is one of 'clean', 'drifted',
    'notes-drifted', 'undetermined', or None when no harness applies here.

    `mis_certified` is the harness's own `false_clean_before_fix`: this tree is one that a
    harness before game_loop #66 would have reported CLEAN, exit 0. It is retrospective and
    it is not re-derivable — the verb is stateless and compares trees as they are now, so
    once a branch is merged the evidence is gone. Carried out of here rather than left in
    the payload because the tree in front of you being blocked today says nothing about
    whether an earlier version of it already walked through this gate onto trunk.

    Ranked by SEVERITY rather than by a chain of pairwise comparisons. The chain that used to
    live here let a notes-drifted second harness lose to a clean first one, so the milder
    verdict won and the finding it was added to surface never reached a caller.
    """
    names = {CLEAN: "clean", DRIFTED: "drifted",
             NOTES_DRIFTED: "notes-drifted", UNDETERMINED: "undetermined"}
    worst = None
    detail = ""
    mis_certified = False
    for dirname in spec(cfg)["dirs"]:
        rc, payload = _verify_with_harness(worktree_path, dirname)
        if rc is None:
            continue
        # OR'd across harness directories on purpose: any one of them reporting a false clean
        # means the tree as a whole was let through, and the others agreeing proves nothing.
        if (payload or {}).get("false_clean_before_fix") is True:
            mis_certified = True
        if worst is None or SEVERITY[rc] > SEVERITY[worst]:
            worst = rc
            detail = (payload or {}).get("detail", "")
    if worst is None:
        return None, "", False
    return names.get(worst), detail, mis_certified


def provision(cfg, worktree_path):
    """Make the worktree carry the SAME harness. Returns (actions, problems, warnings)."""
    sp = spec(cfg)
    actions, problems, warnings = [], [], []
    if sp["provision"] == "off" or not sp["dirs"]:
        return actions, problems, warnings

    tracked = tracked_top_levels(cfg)

    for dirname in sp["dirs"]:
        src = os.path.join(cfg.root, dirname)
        dst = os.path.join(worktree_path, dirname)
        if not os.path.isdir(src):
            problems.append("configured harness dir %s does not exist in the main checkout" % dirname)
            continue

        installed = False
        if not os.path.isdir(dst):
            err = _install(cfg, sp, worktree_path)
            if err is None and sp["installer"]:
                actions.append("%s installed by the harness's own installer (which MERGES hook "
                               "registration rather than overwriting it)" % dirname)
                installed = True
            elif err:
                problems.append(err)
                continue
            else:
                copied, skipped = _copy_excluding_runtime(src, dst, runtime_globs(src))
                actions.append("%s copied (%d file(s); %d runtime path(s) excluded per the "
                               "harness's own .gitignore)" % (dirname, len(copied), len(skipped)))
        elif dirname in tracked:
            actions.append("%s is tracked by git and crossed with the worktree" % dirname)
        else:
            actions.append("%s already present in the worktree — left alone" % dirname)

        if dirname not in tracked:
            from .worktree import unignored
            if unignored(worktree_path, [dirname]).stageable:
                problems.append(
                    "%s is neither tracked nor ignored, so `git add -A` in the worktree would "
                    "commit the whole harness onto the Crawler's branch. Either track it (which "
                    "also makes it cross into every worktree by itself) or add it to the repo's "
                    ".gitignore. showrunner will not write git's shared exclude file — it is not "
                    "per-worktree, so that would change the main checkout's ignores too."
                    % dirname)

        # Authoritative check: the harness compares its own trees.
        rc, payload = _verify_with_harness(worktree_path, dirname)
        if rc is None:
            # Not answering the contract. showrunner does NOT substitute a comparison of
            # its own here: guessing which files are rules is exactly the hardcoded list
            # this module was rewritten to delete, and it would rot the same way.
            problems.append(
                "%s does not answer `%s worktree --porcelain` (exit 0 clean / 1 drifted / "
                "2 undetermined / 3 notes drifted). showrunner will not guess which of its files "
                "are rules — that list belongs to the harness and drifts silently anywhere else.\n"
                "See the harness's embedding contract, or set harness.require=false to accept a "
                "Crawler whose rules are unverified."
                % (dirname, os.path.basename(bin_for(cfg.root, dirname))))
            continue
        detail = (payload or {}).get("detail", "")
        if rc == CLEAN:
            actions.append("%s verified by the harness itself: %s. NOT checked by it: the "
                           "hook-registration file, which lives outside the harness directory "
                           "— showrunner refuses a spawn without one, which is a different "
                           "check by a different party." % (dirname, detail))
        elif rc == DRIFTED:
            problems.append(
                "%s: %s\nThe Crawler would enforce different things than the orchestrator, and "
                "nothing downstream would report it." % (dirname, detail))
        elif rc == NOTES_DRIFTED:
            warnings.append("%s: %s" % (dirname, detail))
        else:
            problems.append(
                "%s: the harness could not determine whether this tree matches (%s). Refusing "
                "rather than reading it as clean — 'could not tell' and 'matched' must never be "
                "the same answer." % (dirname, detail or "exit %s" % rc))
        if (payload or {}).get("false_clean_before_fix") is True:
            problems.append(
                "%s: and a harness before game_loop #66 would have called this exact tree CLEAN. "
                "Nothing re-derives that after the fact — the verb compares trees as they are "
                "now — so if this branch has been integrated before, that merge was certified "
                "by a gate answering about files it never opened." % dirname)

        if not installed and not _hooks_present(worktree_path):
            problems.append(
                "%s is present but %s is not, so NONE of its hooks are registered in the "
                "Crawler's project — showrunner would be promising a guarded agent and "
                "delivering an unguarded one.\nDo not copy that file: the harness's installer "
                "MERGES its hooks, preserving the project's own settings and warning about "
                "pre-existing non-harness hooks on the events it manages. Set "
                "harness.installer in .showrunner/config.json, or track %s in git so it "
                "crosses with the worktree." % (dirname, HOOK_REGISTRATION, HOOK_REGISTRATION))

    return actions, problems, warnings


def _hooks_present(worktree_path):
    return os.path.exists(os.path.join(worktree_path, HOOK_REGISTRATION))


def waiting_probe(cfg, dirname):
    """Is the harness's idle watchdog wired to an answer about dispatched work? (issue #23)

    ASKED, NEVER ASSUMED. showrunner does not name the harness's config key, does not write its
    file, and does not know where it keeps this — it asks a verb and reports what comes back.
    The tempting version writes `watchdog.waiting_probe` into the harness's config, and that is
    `DEFAULT_RULE_FILES` again: a key belonging to the layer below, hardcoded here, rotting the
    same way. The harness's own answer carries the path to paste it into.

    Nor is there an `--arm` verb to call, and the reason is the harness's rather than mine: a
    verb is callable by the session being WATCHED, and it cannot tell an orchestrator from the
    Crawler it dispatched. A probe of `true` always exits 0, which reads as always waiting,
    which is the watchdog disarmed by the thing it watches. So the arming stays a human's, once
    per install, and showrunner's whole job here is to make sure nobody discovers the gap by
    being paged for a run that was behaving perfectly.

    Returns (state, detail) — 'armed', 'unarmed', 'failing', or None when the harness answers
    no such verb. `failing` matters as much as `unarmed`: the probe contract's third state is
    "could not answer", which still rings AND reports failing, so a relative path or a missing
    executable bit presents as a broken watchdog rather than as the config error it is.
    """
    rc, payload = _porcelain(bin_for(cfg.root, dirname), "watchdog")
    if not isinstance(payload, dict):
        return None, ""
    if payload.get("failing"):
        return "failing", str(payload.get("command") or "")
    if payload.get("configured"):
        return "armed", str(payload.get("command") or "")
    return "unarmed", str(payload.get("set_it_by") or "")


def follow_up(cfg):
    """When did anything last re-check this campaign, and is another re-check coming?

    THE QUESTION A CRAWLER REPORT LEAVES UNANSWERED. `reconcile` says what is true at the
    instant it runs, and a reader cannot tell a report taken thirty seconds ago from one taken
    yesterday — nor whether anything will look again if they walk away. Both halves are facts
    about the HARNESS's idle watchdog, which is the only thing here that re-checks anything.

    ASKED, NEVER ASSUMED, for the reason `waiting_probe` gives directly above: the interval and
    the schedule belong to the layer below, and hardcoding its config key here is
    `DEFAULT_RULE_FILES` again — a key of somebody else's that rots silently. So this reads only
    what the harness's own porcelain answers.

    Returns a dict, always. Keys:
      harness    the harness dir that answered, or None if none did
      last       epoch seconds of the last re-check, or None (armed but never run, or unarmed)
      waiting    that re-check's verdict, or None
      scheduled  True only when a probe is armed AND answering
      why        why not, in the reader's terms, when scheduled is False

    **`interval` is deliberately absent, and that is a finding rather than an omission.** The
    harness's `watchdog --porcelain` payload carries `configured`, `command`, `armed_in`,
    `last`, `failing` and `set_it_by` — and no period. So "the next follow-up is at T+n" cannot
    be computed from anything this layer is entitled to read, and printing one would be a
    promise about an event nothing here causes. Callers must say "when it next goes idle", not
    a time. Closing this needs the harness to publish its interval; it is a cross-repo ask, not
    a local fix, and inventing the number locally is the failure this docstring exists to stop.
    """
    out = {"harness": None, "last": None, "waiting": None, "scheduled": False,
           "why": "no harness is present, so nothing re-checks this campaign on its own"}
    for dirname in spec(cfg)["dirs"]:
        rc, payload = _porcelain(bin_for(cfg.root, dirname), "watchdog")
        if not isinstance(payload, dict):
            continue
        out["harness"] = dirname
        last = payload.get("last")
        if isinstance(last, dict):
            out["last"] = last.get("at")
            out["waiting"] = last.get("waiting")
        if payload.get("failing"):
            out["why"] = ("%s's waiting probe is CONFIGURED AND FAILING, so its re-checks "
                          "report a broken watchdog rather than an answer about your Crawlers"
                          % dirname)
        elif payload.get("configured"):
            out["scheduled"] = True
            out["why"] = ""
        else:
            # THE ORDINARY STATE, and the one worth saying out loud: the watchdog exists, and
            # nothing has told it how to ask about dispatched work. A report that stayed silent
            # here would let "nothing is scheduled" look exactly like "something is".
            out["why"] = ("%s's idle watchdog has no waiting probe, so nothing re-checks this "
                          "campaign — arm it in %s" % (dirname, payload.get("set_it_by")
                                                       or "the harness's local config"))
        return out
    return out


def stop_gate(cfg, worktree_path, session):
    """Is this Crawler INERT at a refused turn-end rather than working? (issue #24)

    The failure this exists for is one showrunner helped build. Before its turn-end gate was
    wired, a Crawler that could not finish exited with its leaf open — loud, and caught by one
    poll of process liveness. After, it stays alive and inert, and every signal reads healthy:
    a live pid, an open leaf, `waiting` exiting 0, the watchdog quiet, `reap` correctly
    proposing nothing, and the report already on disk. One sat 44 minutes that way and then
    woke, reported and closed correctly the moment a chat message reached it.

    So this is a false "waiting" — the exact failure `campaign.waiting` exists to prevent,
    arriving through a door neither layer was watching.

    ASKED, PER SESSION. The harness records the block in its own state and showrunner assigns
    the session id at spawn, so the question is answerable from outside without reading another
    layer's state file. GAME_LOOP_SESSION selects which session is reported; without it the
    harness answers about whichever session the environment implies, which under an orchestrator
    is showrunner's own and not the Crawler's.

    Returns (blocked, detail) — or (None, "") when the harness does not answer this contract,
    which is NOT the same as "not blocked" and must never be read as reassurance. An older
    harness has no seam here and a consumer that read its silence as healthy would be making
    the same mistake one layer up.

    WHAT THIS DOES NOT TELL YOU: whether the inert time is the block. The harness reports that
    a turn-end was refused and when; it cannot report that nothing has happened since, and the
    session that experienced it could not tell from inside either. `blocked` plus a stale
    transcript is evidence; `blocked` alone is a fact about the past.
    """
    if not session:
        return None, ""
    for dirname in spec(cfg)["dirs"]:
        binary = bin_for(worktree_path, dirname)
        if not os.access(binary, os.X_OK):
            continue
        env = dict(os.environ, GAME_LOOP_SESSION=session)
        rc, out, _ = run([binary, "watchdog", "--porcelain"], cwd=os.path.dirname(binary),
                         timeout=60, env=env)
        try:
            seam = (json.loads(out) or {}).get("stop_gate")
        except (ValueError, TypeError):
            continue
        if not isinstance(seam, dict) or "blocked" not in seam:
            continue
        if not seam.get("blocked"):
            return False, ""
        att = seam.get("attachments") or {}
        who = ", ".join(sorted(att)) or "an unnamed attachment"
        # `limit` bounds REPEATED blocking by one attachment. It does not bound a single block:
        # a session that never attempts another turn-end never increments again, so the
        # stand-down is never reached. The harness says so in `bound_covers`; repeated here
        # because a reader who sees "1 of 3" will otherwise assume something is counting down.
        return True, ("refused at turn-end by %s (%d block(s) total; the harness's limit of %s "
                      "bounds REPEATED blocking by one attachment and does not bound this — a "
                      "session that never tries another turn-end never increments again)"
                      % (who, seam.get("blocks_total") or 0, seam.get("limit")))
    return None, ""


def owns_path(cfg, path):
    """The harness directory containing `path`, or None. THE rule, in one place.

    Both callers need it — `worktree.inject` refuses an entry as it materialises, and `doctor`
    reports the same conflict from the config with nothing running. Written twice it would be
    two rules that agree today, and the failure mode of two rules that drift is that the
    quieter one wins: doctor stays silent while spawn keeps aborting, or the reverse.
    """
    if not path:
        return None
    for d in spec(cfg)["dirs"]:
        if path == d or path.startswith(d.rstrip("/") + os.sep):
            return d
    return None


def inject_conflicts(cfg):
    """Configured inject paths that fall inside a harness directory (#22). Doctor asks.

    `spawn` refuses these where they happen, which is correct and late: it fires one Crawler at
    a time in the middle of a fan-out. The conflict is knowable from the config alone, with
    nothing running, so this is the same rule at the moment it costs nothing.
    """
    out = []
    for entry in (cfg.get("inject") or []):
        path = entry.get("path") if isinstance(entry, dict) else entry
        owner = owns_path(cfg, path)
        if owner:
            out.append((path, owner))
    return out


def report(cfg):
    """Doctor-facing summary of what a Crawler would get."""
    sp = spec(cfg)
    if sp["provision"] == "off":
        return ["harness provisioning is OFF — a Crawler's worktree carries whatever git brings "
                "across, and its commit gate may owe nothing."]
    if not sp["dirs"]:
        return []
    tracked = tracked_top_levels(cfg)
    lines = []
    for dirname in sp["dirs"]:
        info = owned(cfg, dirname)
        if info:
            lines.append("harness %s declares its own owned set: %d rule file(s) (%s), %d notes "
                         "file(s) (%s) — showrunner keeps no list of its own"
                         % (dirname, len(info.get("rule_files") or []),
                            ", ".join(info.get("rule_files") or []) or "none",
                            len(info.get("notes_files") or []),
                            ", ".join(info.get("notes_files") or []) or "none"))
        else:
            lines.append("harness %s exposes no `owned` verb — falling back to 'everything not "
                         "declared runtime must match', which is conservative and noisier"
                         % dirname)
        if dirname in tracked:
            lines.append("  %s is tracked by git, so it and %s cross with every worktree"
                         % (dirname, HOOK_REGISTRATION))
        elif sp["installer"]:
            lines.append("  %s is untracked; the configured installer provisions it per worktree"
                         % dirname)
        else:
            lines.append("  %s is untracked and no harness.installer is configured — spawn will "
                         "refuse rather than hand over a Crawler with no registered hooks"
                         % dirname)
    return lines
