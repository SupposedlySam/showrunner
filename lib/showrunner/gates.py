"""The gates: proof-of-done, stop, no-new-failures, and integration provenance.

Lifted out of `prototype/br_gate.sh`, which welded both gates to one machine and one repo
(`$HOME/.cargo/bin`, `$HOME/development/drops/.beads/beads.db`) and parsed JSON with
`sed` and `grep`. The gates themselves were sound in shape and survive; three things
changed (issue #6).

**The DB path and toolchain are config**, not a `$HOME`-relative default.

**`close-gate` records its proof and checks freshness.** Existence is not relevance —
`[ -s "$proof" ]` passes for any non-empty file in the repo, including one with nothing to
do with the leaf; the prototype's own demo closes a throwaway issue by citing
`./device_lane.sh`. That limit cannot be closed by a string check, so the answer is the
honest one: record the proof in the close so a reviewer can judge it later, and refuse an
artifact that predates the claim — an artifact older than the work is evidence about
something else.

**The stop gate parses JSON as JSON.** The `sed`/`grep` version turned into a no-op that
reported success the moment br's field order or whitespace changed — the failure mode
where a gate stops gating and nothing says so.

And the criterion is **no *new* failures versus a recorded baseline**, never "all green".
A repo with pre-existing failures cannot satisfy "all green", so that version of the gate
gets switched off on contact with a real codebase.
"""

import json
import os
import re
import time

from .util import Refused, die, git, now, rel, run

DEFAULT_FAILURE_PATTERN = (
    r"^(?:FAILED|FAIL|ERROR|E\s)\b.*|"
    r"^\s*(?:✗|×)\s+.*|"
    r"^.*\b(?:assertion failed|AssertionError|Traceback \(most recent call last\))\b.*$"
)


# --------------------------------------------------------------- close gate
PREMISE_VERDICTS = ("holds", "partial", "refuted", "unverifiable")


def close_gate(cfg, graph, leaf_id, proof, reason, refuted=False, evidence=None,
               stale_proof_reason=None, premise=None, premise_read=None):
    """Refuse to close unless a real, non-empty artifact is named. Returns (leaf, notes).

    `refuted` is a **first-class successful outcome**, not a failure: a run that correctly
    declines to build something has produced real value, and if the only shapes available
    are done/failed the incentive is to build something (issue #12). It still costs a
    citation — the file checked against — because "the premise does not hold" is itself an
    assertion about external reality.

    The **premise verdict is a required field**, not an optional aside (issue #12). Of 14
    issues in one real run, three had premises that did not survive contact with the
    codebase, and two of those three were volunteered rather than asked for. A Crawler
    that quietly implements a fix for a bug that is not there is indistinguishable from
    one that did the work — same commit, same green tests, same satisfied proof gate —
    because the gate checks that work *happened*, not that it was *needed*. Making the
    verdict a required argument is the difference between a check and a hope.
    """
    leaf = graph.show(leaf_id)

    if refuted and not premise:
        premise = "refuted"
    if premise not in PREMISE_VERDICTS:
        raise Refused(
            "REFUSED to close %s: --premise is required and must be one of %s."
            % (leaf_id, "/".join(PREMISE_VERDICTS)),
            hint="Did the issue's premise survive contact with the code? Say so, and cite the "
                 "file you checked it against:\n"
                 "  --premise holds --premise-read <path you verified against>\n"
                 "A fix for a bug that is not there looks exactly like a fix for one that is.")
    if not premise_read and not (refuted and evidence):
        raise Refused(
            "REFUSED to close %s: --premise-read must name the real file you checked the "
            "premise against." % leaf_id,
            hint="An unsourced premise verdict is the same wish as an unsourced 'done'.")
    if premise_read:
        pr = premise_read if os.path.isabs(premise_read) else os.path.join(cfg.root, premise_read)
        if not os.path.exists(pr):
            raise Refused("REFUSED to close %s: --premise-read names a path that does not "
                          "exist: %s" % (leaf_id, premise_read))
    artifact = evidence if refuted else proof
    label = "--evidence" if refuted else "--proof"

    if not artifact:
        raise Refused(
            "REFUSED to close %s: %s must name a real, non-empty artifact." % (leaf_id, label),
            hint="Name the test / golden / commit / file that proves it. 'Done' in prose is a wish."
            if not refuted else
            "Name the file you checked the premise against. A refutation is an assertion too.")

    path = artifact if os.path.isabs(artifact) else os.path.join(cfg.root, artifact)
    if not os.path.exists(path):
        raise Refused("REFUSED to close %s: %s names a path that does not exist: %s"
                      % (leaf_id, label, artifact))
    if os.path.isfile(path) and os.path.getsize(path) == 0:
        raise Refused("REFUSED to close %s: %s names an empty file: %s" % (leaf_id, label, artifact))

    notes = []
    # Freshness applies to a PROOF and only to a proof. A proof is an artifact the work
    # produced, so one older than the claim is evidence about something else. Evidence for a
    # REFUTATION is the opposite kind of thing: it is the pre-existing source you read to
    # discover the premise does not hold, and it is *supposed* to predate the work. Demanding
    # it be newer would make the honest outcome the hard one to record — which is exactly
    # backwards, since "premise refuted" is the outcome this design wants to be cheap.
    claim_ts = 0 if refuted else (leaf.get("claim_ts") or 0)
    if claim_ts:
        try:
            mtime = int(os.path.getmtime(path))
        except OSError:
            mtime = 0
        if mtime and mtime < claim_ts:
            if not stale_proof_reason:
                raise Refused(
                    "REFUSED to close %s: %s (%s) has not changed since before the leaf was "
                    "claimed (artifact %s, claim %s). An artifact older than the work is "
                    "evidence about something else."
                    % (leaf_id, label, artifact, _ts(mtime), _ts(claim_ts)),
                    hint="If a pre-existing artifact genuinely is the proof (e.g. a test that "
                         "already covered this and now passes for a new reason), say why:\n"
                         "  --stale-proof-reason \"<why this older file proves this work>\"\n"
                         "It is recorded in the close, not waved through.")
            notes.append("proof predates the claim; accepted with reason: %s" % stale_proof_reason)

    outcome = "refuted" if refuted else "closed"
    full_reason = reason or ("premise refuted" if refuted else "done")
    full_reason += " [premise: %s%s]" % (premise, "" if not premise_read else " vs %s" % premise_read)
    if stale_proof_reason:
        full_reason += " [stale-proof accepted: %s]" % stale_proof_reason
    if premise in ("partial", "refuted"):
        notes.append(
            "premise recorded as %r — this is a first-class outcome, not a failure. A run that "
            "correctly declines to build something has produced real value." % premise)

    graph.close(leaf_id, outcome, rel(path, cfg.root), full_reason)

    # INV6: say what the guard does not catch, in the guard itself.
    notes.append(
        "NOT CHECKED: that this artifact is *about* this leaf. Existence and freshness are "
        "checkable; relevance is not. The proof is recorded on the close so a reviewer can "
        "judge it later — that is the boundary, stated rather than papered over.")
    return graph.show(leaf_id), notes


def _ts(epoch):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


# ---------------------------------------------------------------- stop gate
def stop_gate(cfg, graph, leaf_id=None, tree=None):
    """Refuse a turn-end while THIS CALLER's claimed LEAF is open. Returns (ok, message).

    Epics are containers — expected to sit in progress while their children are worked —
    so they are excluded. Parked leaves are excluded too: a Crawler parked at a usage
    limit is not abandoned work, it is accounted-for work, and blocking on it would make
    the expected pause look like a fault.

    **SCOPED TO THE CALLER, and it was not.** This asked "is ANY leaf open in this campaign",
    and `spawn` writes it into EVERY Crawler's turn-end triggers — so each Crawler was gated on
    all of its siblings. With N dispatched, N-1 are structurally guaranteed to be refused at
    least once, and a headless Crawler has no next turn in which to act on a refusal, so it
    stops there. Three went inert that way in one afternoon. The refusal then advised the
    Crawler to "finish it through the close gate, or release it" about leaves in two other
    worktrees it cannot reach — advice that, if followed, has one Crawler closing another's work.

    The unscoped answer was wrong for BOTH parties, which is why scoping is the whole fix rather
    than an exemption for Crawlers. For an orchestrator, other agents' leaves being open is not
    a reason to refuse a turn-end at all — it is the definition of waiting on dispatched work,
    and `showrunner waiting` is the verb that reports it. For a Crawler it is somebody else's
    work. One rule covers both: block on what this caller holds.

    IDENTITY, most precise first. `leaf_id` is exact and `spawn` bakes it into the trigger
    command it writes into each worktree. Otherwise the caller's TREE decides: a claim records
    `claim_tree` at spawn, so leaves claimed for a Crawler carry its worktree and leaves an
    orchestrator claimed for itself carry the main checkout. A caller whose tree holds no open
    leaf passes — which is exactly the case that used to fail, since a Crawler that has already
    closed its own leaf still saw every sibling's.

    A claim with NO recorded tree cannot be attributed to anyone. Those are reported and do not
    block: silently blocking every caller on an unattributable leaf is the bug above, and
    silently ignoring it would hide the only evidence that a claim was made outside `spawn`.
    """
    # Symmetrical with `claim`, which records the claiming process's cwd when given no tree.
    # "Where am I standing" has to mean the same thing on both sides of this comparison, or the
    # gate silently matches nothing and reports a clean pass.
    tree = tree or os.getcwd()

    def top(path):
        """The WORKTREE root containing `path` — not the path itself.

        A raw path comparison makes the gate depend on which subdirectory each side happened
        to be in: an agent that claimed from `lib/` and ends its turn from the repo root holds
        the leaf and is told it does not. `git rev-parse --show-toplevel` is per-worktree, so a
        Crawler's worktree and the main checkout still resolve to different roots — which is
        the distinction this whole scoping rests on.
        """
        if not path:
            return None
        rc, out, _ = git(["rev-parse", "--show-toplevel"], cwd=path)
        if rc == 0 and out.strip():
            return os.path.realpath(out.strip())
        return os.path.realpath(path)

    def same_tree(a, b):
        if not a or not b:
            return False
        return top(a) == top(b)

    open_leaves = [x for x in graph.list(status="in_progress") if not x.is_epic]
    parked = [x for x in open_leaves if x.get("parked")]
    claimed = [x for x in open_leaves if not x.get("parked")]

    if leaf_id:
        mine = [x for x in claimed if x["id"] == leaf_id]
        theirs = [x for x in claimed if x["id"] != leaf_id]
        unattributable = []
        basis = "leaf %s, named by the trigger this caller runs" % leaf_id
    else:
        mine = [x for x in claimed if same_tree(x.get("claim_tree"), tree)]
        unattributable = [x for x in claimed if not x.get("claim_tree")]
        theirs = [x for x in claimed if x not in mine and x not in unattributable]
        basis = "claims recorded against %s" % (tree or "(no tree given)")

    lines = []
    if parked:
        lines.append("parked (not blocking): " +
                     ", ".join("%s [%s]" % (x["id"], x.get("park_reason") or "?") for x in parked))
    if theirs:
        lines.append("open but NOT yours (%d): %s" % (
            len(theirs), ", ".join("%s [%s]" % (x["id"], x.get("actor") or "?") for x in theirs)))
    if unattributable:
        lines.append("open with no recorded tree, so nobody can be gated on them (%s) — these "
                     "were claimed outside `spawn`" % ", ".join(x["id"] for x in unattributable))

    if mine:
        detail = "\n".join("  - %s (%s) — %s" % (x["id"], x.get("actor") or "?", x.get("title", ""))
                           for x in mine)
        msg = ("STOP REFUSED: YOUR claimed-but-open leaf work is still in progress:\n%s\n"
               "  Either finish it through the close gate, or release it, or explicitly "
               "checkpoint and hand back.\n  Scope: %s" % (detail, basis))
        if lines:
            msg += "\n  " + "\n  ".join(lines)
        return False, msg
    msg = "stop OK: no claimed-open leaf work of YOURS (%s)" % basis
    if lines:
        msg += "\n  " + "\n  ".join(lines)
    return True, msg


# ------------------------------------------------------- checks / baseline
def _checks(cfg):
    out = []
    for i, entry in enumerate(cfg.get("checks") or []):
        if isinstance(entry, str):
            entry = {"cmd": entry}
        out.append({
            "name": entry.get("name") or "check-%d" % (i + 1),
            "cmd": entry.get("cmd"),
            "failure_pattern": entry.get("failure_pattern") or DEFAULT_FAILURE_PATTERN,
        })
    return [c for c in out if c["cmd"]]


def _signatures(text, pattern):
    try:
        rx = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        die("invalid failure_pattern %r: %s" % (pattern, exc), code=2)
    sigs = set()
    for line in text.splitlines():
        if rx.match(line.strip()) or rx.match(line):
            sigs.add(re.sub(r"\s+", " ", line.strip())[:200])
    return sigs


def run_checks(cfg, cwd=None):
    """Run every configured check. Returns a result dict (JSON-serializable)."""
    results = []
    for chk in _checks(cfg):
        rc, out, err = run(chk["cmd"], cwd=cwd or cfg.root, timeout=3600)
        sigs = _signatures(out + "\n" + err, chk["failure_pattern"])
        results.append({
            "name": chk["name"],
            "cmd": chk["cmd"],
            "rc": rc,
            "failures": sorted(sigs),
            # A check that failed while producing no recognisable failure lines can only be
            # compared at exit-code granularity. Say so; do not let reduced resolution read
            # as a clean comparison.
            "resolution": "signatures" if sigs else ("exit-code-only" if rc != 0 else "clean"),
        })
    return {"ts": now(), "checks": results}


def record_baseline(cfg, cwd=None):
    data = run_checks(cfg, cwd)
    os.makedirs(os.path.dirname(cfg.baseline_path), exist_ok=True)
    with open(cfg.baseline_path, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    return data


def load_baseline(cfg):
    if not os.path.exists(cfg.baseline_path):
        return None
    with open(cfg.baseline_path) as fh:
        return json.load(fh)


def compare_to_baseline(cfg, current, baseline):
    """Return (ok, report). The criterion is NO NEW FAILURES, never 'all green'."""
    if baseline is None:
        return None, ["no baseline recorded — run `showrunner baseline` on a known-good tree "
                      "first. Without one, 'no new failures' has nothing to be new *against*, "
                      "and 'all green' is a criterion a real codebase cannot satisfy."]
    base = {c["name"]: c for c in baseline.get("checks", [])}
    report, ok = [], True
    for cur in current.get("checks", []):
        b = base.get(cur["name"])
        if not b:
            report.append("%s: NEW CHECK (no baseline entry) — treating any failure as new" % cur["name"])
            if cur["rc"] != 0:
                ok = False
                report.append("  rc=%s with %d failure line(s)" % (cur["rc"], len(cur["failures"])))
            continue
        new = sorted(set(cur["failures"]) - set(b["failures"]))
        fixed = sorted(set(b["failures"]) - set(cur["failures"]))
        if new:
            ok = False
            report.append("%s: %d NEW failure(s):" % (cur["name"], len(new)))
            report += ["    %s" % s for s in new[:20]]
        elif cur["rc"] != 0 and b["rc"] == 0:
            ok = False
            report.append("%s: rc %s (baseline 0) with no parseable failure lines — comparison "
                          "degraded to exit-code granularity, treating as a new failure."
                          % (cur["name"], cur["rc"]))
        else:
            report.append("%s: no new failures (rc %s, baseline rc %s)" % (cur["name"], cur["rc"], b["rc"]))
        if fixed:
            report.append("  (%d baseline failure(s) no longer reproduce)" % len(fixed))
        if cur["resolution"] == "exit-code-only":
            report.append("  NOTE: %s produced no recognisable failure lines; set "
                          "\"failure_pattern\" for this check or the comparison stays coarse."
                          % cur["name"])
    return ok, report


# --------------------------------------- integration commit provenance (#14)
DECLARATION = "integration-commit.json"


def crawler_touched(cfg, branch, base):
    """Files a Crawler's branch actually changed, versus the merge base."""
    rc, out, _ = git(["merge-base", base, branch], cwd=cfg.root)
    if rc != 0:
        die("cannot find a merge base between %s and %s" % (base, branch), code=2)
    mb = out.strip()
    rc, out, _ = git(["diff", "--name-only", "%s..%s" % (mb, branch)], cwd=cfg.root)
    if rc != 0:
        die("cannot diff %s..%s" % (mb, branch), code=2)
    return {l for l in out.splitlines() if l.strip()}


def staged_files(cfg):
    rc, out, _ = git(["diff", "--cached", "--name-only"], cwd=cfg.root)
    return {l for l in out.splitlines() if l.strip()} if rc == 0 else set()


def declare_integration(cfg, entries, base="HEAD"):
    """Declare an integration commit to the harness underneath. Issue #14.

    A provenance check that compares a commit's staged files against the set *this
    session* edited fires on every integration commit an orchestrator makes, and it is
    correct by its own definition: the integrating session never edits those files —
    Crawlers wrote them, in worktrees, and `git merge` brought them in.

    Silence is the wrong fix. A warning that fires every time is one people learn to
    scroll past, and the moment it becomes background noise it stops working for the case
    it was built for. The honest version for an orchestrator is a **different question**:
    *does the staged set match the union of what the merged Crawlers edited?* That is
    answerable, strictly more useful, and it catches the real orchestration failure — a
    file appearing in an integration commit that no Crawler ever touched.

    `entries` is a list of {"crawler", "branch"}.
    """
    union, per_crawler = set(), {}
    for e in entries:
        touched = crawler_touched(cfg, e["branch"], base)
        per_crawler[e.get("crawler") or e["branch"]] = sorted(touched)
        union |= touched

    staged = staged_files(cfg)
    unexplained = sorted(staged - union)
    missing = sorted(union - staged)

    decl = {
        "kind": "integration",
        "ts": now(),
        "base": base,
        "crawlers": per_crawler,
        "expected_files": sorted(union),
        "staged_files": sorted(staged),
        "unexplained": unexplained,
        "not_staged": missing,
        "ok": not unexplained,
    }
    path = os.path.join(cfg.state_dir, DECLARATION)
    os.makedirs(cfg.state_dir, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(decl, fh, indent=2, sort_keys=True)
    decl["path"] = path
    return decl


def trailer(decl):
    """A commit trailer so the provenance is in git, not only in a sidecar file."""
    names = ", ".join(sorted(decl.get("crawlers") or {}))
    return "Showrunner-Integration: %s" % (names or "none")


def attribution(cfg, entries, harness_bin=None):
    """The harness command that declares this commit's provenance, plus when NOT to run it.

    Two rules, both found by running the verb rather than reading it, and both cheap to get
    wrong in a way that wastes a single-use declaration:

    1. **A clean merge never invokes the gate.** The commit gate matches `git commit …`; a
       clean `git merge` auto-commits without one. So declare only on the path where you
       commit yourself — a conflict resolution, or a `--no-commit` merge you finish by hand.
       Declaring for a clean merge burns the declaration on a commit that was never going to
       be checked, and the *next* commit — the one that needed it — goes bare.
    2. **Declare after the branch has its commit.** Attribution is recomputed from
       `merge-base HEAD <ref>..<ref>`, so declaring before the branch's work is committed
       resolves to zero files: a correct answer, and a useless one.
    """
    refs = [e["branch"] for e in entries if e.get("branch")]
    if not refs:
        return None
    binary = harness_bin or _harness_bin(cfg)
    if not binary:
        return None
    cmd = "%s attribute %s --reason %s" % (
        binary,
        " ".join("--merge %s" % r for r in refs),
        '"integration commit: work produced by %s"' % ", ".join(
            sorted({e.get("crawler") or e["branch"] for e in entries})))
    return {
        "command": cmd,
        "refs": refs,
        "when": "ONLY if you are running `git commit` yourself (a conflict resolution, or a "
                "--no-commit merge). A clean `git merge` auto-commits and never invokes the "
                "gate, so a declaration spent there is wasted and the next commit goes bare.",
        "order": "Run it AFTER the branch has its commit — attribution is recomputed from the "
                 "ref, so declaring early resolves to zero files.",
    }


def _harness_bin(cfg):
    from . import harness
    for d in harness.spec(cfg)["dirs"]:
        b = harness.bin_for(cfg.root, d)
        if os.access(b, os.X_OK):
            return os.path.join(".", os.path.relpath(b, cfg.root))
    return None
