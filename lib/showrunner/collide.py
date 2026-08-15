"""Blast-radius estimation and wave planning. Issue #5.

The graph answers "what is unblocked?" — a question about *dependencies*. It models
nothing about what two agents will *touch*. Two leaves can be mutually unblocked and
still be the same edit, so "prefer non-overlapping file sets" is a preference expressed
in prose, checked by nobody, at exactly the moment it matters.

This estimates each ready leaf's blast radius before dispatch and refuses to fan out two
leaves whose estimates intersect, routing the second into the next wave.

**The estimate does not need to be good, only conservative.** A false collision costs one
wave of latency; a missed one costs a merge conflict in an unattended run with nobody
watching. So a leaf whose radius cannot be estimated at all is treated as colliding with
everything, and said out loud rather than optimistically parallelised.

**Shared surfaces are the expected case, not an anomaly.** Nearly every project has one
file every change touches — a test file, a dispatch table, a config schema. Letting that
force a fully serial run would make the check look like the reason the run is slow, and a
check that looks like an unexplained slowdown invites someone to remove it. Paths matching
`collision.always_serialize` are therefore excluded from the parallel-blocking decision
and recorded as shared surfaces that *integration* must serialise instead (issue #9).
"""

import fnmatch
import os
import re

from .util import run

# Tokens common enough in prose that grepping them would match the whole repo.
_STOPWORDS = {
    "the", "and", "that", "this", "with", "from", "when", "then", "into", "have", "does",
    "issue", "file", "files", "path", "paths", "test", "tests", "code", "line", "lines",
    "should", "would", "could", "which", "there", "their", "because", "every", "never",
    "always", "instead", "rather", "before", "after", "check", "checks", "agent", "agents",
    "work", "run", "runs", "make", "made", "same", "each", "onto", "over", "under", "what",
    "will", "were", "been", "than", "them", "they", "some", "only", "also", "more", "most",
    "case", "cases", "thing", "things", "shape", "state", "value", "values", "true", "false",
}

_PATHISH = re.compile(r"[A-Za-z0-9_.\-/]*[/][A-Za-z0-9_.\-/]*\.[A-Za-z0-9]{1,6}")
_BACKTICKED = re.compile(r"`([^`\n]{2,80})`")
_IDENT = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Z][a-zA-Z0-9]*[a-z][A-Z][a-zA-Z0-9]*)\b")

MAX_GREP_BYTES = 400_000


def tracked_files(root):
    rc, out, _ = run(["git", "ls-files"], cwd=root)
    if rc != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]


# A brief's most useful section is the one that says what NOT to touch, and to a scanner that
# reads prose for filenames it looks exactly like a list of targets. Measured in a consuming
# repo: a leaf whose real blast radius was one markdown file estimated at 9815 paths, because
# its brief responsibly listed every directory it must leave alone. The better the brief, the
# more collision-prone the leaf — the estimator was rewarding vagueness.
_EXCLUSION_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s*.*\b(out of scope|do not touch|non-goals?|not in scope|excluded)\b",
    re.I | re.M)


def overlap(cfg, branches, base=None):
    """What in-flight branches have ACTUALLY changed in common. Measured, not estimated. (#30)

    `plan_waves` estimates each ready leaf's blast radius and refuses to fan out two whose
    estimates intersect. That is the right check and it is scoped to one wave of ready leaves,
    using guesses, before dispatch — this module has no notion of a branch at all. So it cannot
    see what leaves in EARLIER waves, or another story's branch, have already changed.

    Two branches that were each internally collision-free shared six files and two ADD/ADDs —
    found at merge time, when the repair is a hand-reconciliation, rather than at dispatch when
    it was a one-line brief change ("extend that file, do not create it").

    ESTIMATES CANNOT COVER THIS AND DIFFS CAN, and they are complementary rather than competing.
    A blast radius is necessarily conservative guesswork about the future. Once work has landed
    on a branch its file set is no longer a guess: `git diff --name-only <merge-base>..<branch>`
    is exact, costs one call, and needs no heuristics. Estimate forward within a wave; measure
    backward against what already exists.

    ADD/ADD IS CALLED OUT SEPARATELY because it is the one git cannot auto-resolve at all. A
    shared edit usually merges; two branches each CREATING the same path always stops, and it is
    the case a reader most needs to see before dispatch rather than after.
    """
    base = base or cfg.get("integration_base") or "HEAD"
    per_branch, missing = {}, []
    for br in branches:
        rc, mb, _ = run(["git", "merge-base", base, br], cwd=cfg.root)
        if rc != 0 or not mb.strip():
            missing.append(br)
            continue
        # --diff-filter=A separates "created here" from "edited here", which is the whole
        # reason ADD/ADD can be reported rather than buried among ordinary shared edits.
        rc, out, _ = run(["git", "diff", "--name-only", "%s..%s" % (mb.strip(), br)], cwd=cfg.root)
        rc2, adds, _ = run(["git", "diff", "--name-only", "--diff-filter=A",
                            "%s..%s" % (mb.strip(), br)], cwd=cfg.root)
        per_branch[br] = {
            "files": {l.strip() for l in out.splitlines() if l.strip()} if rc == 0 else set(),
            "added": {l.strip() for l in adds.splitlines() if l.strip()} if rc2 == 0 else set(),
        }

    pairs = []
    names = sorted(per_branch)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = per_branch[a]["files"] & per_branch[b]["files"]
            if not shared:
                continue
            add_add = sorted(per_branch[a]["added"] & per_branch[b]["added"])
            pairs.append({"a": a, "b": b, "files": sorted(shared), "add_add": add_add})
    return {
        "base": base,
        "branches": {k: {"files": sorted(v["files"]), "added": sorted(v["added"])}
                     for k, v in per_branch.items()},
        "overlaps": pairs,
        # A branch that could not be resolved is NOT a branch with no overlap. Reported, because
        # the reassuring answer and the unanswerable one are the same empty list otherwise.
        "unresolvable": sorted(missing),
    }


def _text_of(leaf):
    """Title plus body, with any explicitly out-of-scope SECTION removed.

    Cut at the heading and resume at the next heading of the same or higher level, so a brief
    that names its exclusions is not punished for saying so. Only whole sections are dropped:
    an inline "do not touch x" mid-paragraph still contributes, because guessing at sentence
    scope in prose is how a scanner starts being wrong in the other direction.
    """
    body = str(leaf.get("body") or "")
    out, i = [], 0
    for m in _EXCLUSION_HEADING.finditer(body):
        out.append(body[i:m.start()])
        level = len(m.group(0)) - len(m.group(0).lstrip("# \t"))
        nxt = re.compile(r"^\s{0,3}#{1,%d}\s" % max(1, body[m.start():m.end()].count("#")),
                         re.M).search(body, m.end())
        i = nxt.start() if nxt else len(body)
    out.append(body[i:])
    return "\n".join([str(leaf.get("title") or ""), "".join(out)])


def _declared_paths(leaf, root, files):
    """Paths the leaf names outright, kept only when they exist in the repo."""
    found = set()
    fileset = set(files)
    text = _text_of(leaf)
    candidates = set(_PATHISH.findall(text))
    for m in _BACKTICKED.findall(text):
        candidates.add(m.strip())
    for p in (leaf.paths_list if hasattr(leaf, "paths_list") else []):
        candidates.add(p)
    for cand in candidates:
        cand = cand.strip().strip("`'\"(),;:")
        if not cand:
            continue
        if cand in fileset:
            found.add(cand)
            continue
        # A directory named in the issue implicates everything under it.
        if os.path.isdir(os.path.join(root, cand)):
            prefix = cand.rstrip("/") + "/"
            found.update(f for f in files if f.startswith(prefix))
    return found


def _symbols(leaf):
    text = _text_of(leaf)
    syms = set()
    for m in _BACKTICKED.findall(text):
        token = m.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]{2,60}", token) and "/" not in token:
            syms.add(token)
    for m in _IDENT.findall(text):
        if m.lower() not in _STOPWORDS and len(m) >= 5:
            syms.add(m)
    return {s for s in syms if s.lower() not in _STOPWORDS}


def _grep_symbols(root, files, symbols, limit_files=4000):
    """Files mentioning any of the symbols. Cheap, conservative, and bounded."""
    if not symbols:
        return set()
    pattern = re.compile("|".join(re.escape(s) for s in sorted(symbols)))
    hits = set()
    for rel in files[:limit_files]:
        path = os.path.join(root, rel)
        try:
            if os.path.getsize(path) > MAX_GREP_BYTES:
                continue
            with open(path, "r", errors="ignore") as fh:
                if pattern.search(fh.read()):
                    hits.add(rel)
        except OSError:
            continue
    return hits


def estimate(cfg, leaf, files=None):
    """Return {'paths': set, 'shared': set, 'symbols': set, 'basis': str}."""
    root = cfg.root
    files = tracked_files(root) if files is None else files
    declared = _declared_paths(leaf, root, files)
    symbols = _symbols(leaf)
    grepped = _grep_symbols(root, files, symbols) if symbols else set()
    extra = set()
    for glob in (cfg.get("collision") or {}).get("extra_globs") or []:
        extra.update(f for f in files if fnmatch.fnmatch(f, glob))

    all_paths = declared | grepped | extra
    shared_globs = (cfg.get("collision") or {}).get("always_serialize") or []
    shared = {f for f in all_paths if any(fnmatch.fnmatch(f, g) for g in shared_globs)}

    if declared and grepped:
        basis = "%d path(s) named in the issue + %d file(s) mentioning %d symbol(s)" % (
            len(declared), len(grepped), len(symbols))
    elif declared:
        basis = "%d path(s) named in the issue" % len(declared)
    elif grepped:
        basis = "%d file(s) mentioning %d symbol(s)" % (len(grepped), len(symbols))
    else:
        basis = "NOTHING ESTIMABLE — the issue names no real path and no findable symbol"

    return {
        "leaf": leaf["id"],
        "paths": all_paths,
        "exclusive": all_paths - shared,
        "shared": shared,
        "symbols": symbols,
        "basis": basis,
        "estimable": bool(all_paths),
    }


def plan_waves(cfg, leaves, files=None):
    """Greedy wave assignment. Returns (waves, estimates, notes).

    `waves` is a list of lists of leaf ids; every leaf in one wave is pairwise
    non-overlapping on its *exclusive* paths. Non-estimable leaves get a wave to
    themselves — an unknown radius cannot be shown disjoint from anything.
    """
    files = tracked_files(cfg.root) if files is None else files
    estimates = {leaf["id"]: estimate(cfg, leaf, files) for leaf in leaves}
    notes = []
    waves, wave_paths = [], []
    solo = []

    for leaf in leaves:
        est = estimates[leaf["id"]]
        if not est["estimable"]:
            solo.append(leaf["id"])
            notes.append(
                "%s cannot be parallelised: %s. Treating an unknown blast radius as "
                "colliding with everything — a false collision costs one wave, a missed "
                "one costs a merge conflict nobody is watching."
                % (leaf["id"], est["basis"]))
            continue
        placed = False
        for i, taken in enumerate(wave_paths):
            clash = taken & est["exclusive"]
            if not clash:
                waves[i].append(leaf["id"])
                taken |= est["exclusive"]
                placed = True
                break
            if i == 0:
                sample = ", ".join(sorted(clash)[:3])
                notes.append(
                    "%s held back from wave 1: its estimated files overlap work already in "
                    "that wave (%s%s)." % (leaf["id"], sample,
                                           ", …" if len(clash) > 3 else ""))
        if not placed:
            waves.append([leaf["id"]])
            wave_paths.append(set(est["exclusive"]))

    for leaf_id in solo:
        waves.append([leaf_id])
        wave_paths.append(set())

    shared_all = sorted({p for e in estimates.values() for p in e["shared"]})
    if shared_all:
        notes.append(
            "shared surface(s) excluded from the parallel decision and owed to serialised "
            "integration instead: %s" % ", ".join(shared_all[:8]))
    return waves, estimates, notes
