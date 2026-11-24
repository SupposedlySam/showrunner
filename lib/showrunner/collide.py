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

from .util import Refused, run

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

# A leaf whose branch is NEVER integrated — an A/B evaluation arm, a spike kept for comparison
# (#107). A collision costs something only where two branches meet at integration; two that never
# meet cannot collide, so the overlap checks pass any pair that includes one, and `integrate`
# refuses to merge one.
NO_INTEGRATE = "no-integrate"


def never_integrated(leaf):
    """Does this leaf carry the no-integrate label?"""
    labels = leaf.labels_list if hasattr(leaf, "labels_list") else (leaf.get("labels") or [])
    if isinstance(labels, str):
        labels = [x.strip() for x in labels.split(",") if x.strip()]
    return NO_INTEGRATE in labels


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


_GLOB_CHARS = re.compile(r"[*?\[]")
# A slash-separated word with no extension: what an author writes for a directory. `_PATHISH`
# needs an extension, so these are never candidates unless backticked, and a blind leaf says so.
_BARE_DIR = re.compile(r"(?<![`\w./\-])([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+)/?(?![\w`/])")


def _resolve(cand, root, files, fileset):
    """Tracked files one candidate names: itself, everything under a directory, or nothing."""
    if cand in fileset:
        return {cand}
    # A directory named in the issue implicates everything under it.
    if os.path.isdir(os.path.join(root, cand)):
        prefix = cand.rstrip("/") + "/"
        return {f for f in files if f.startswith(prefix)}
    return set()


def _path_kinds(leaf, root, files):
    """(declared, prose, unmatched, unbackticked_dirs) — paths by the evidence that named them.

    DECLARED is what `--path` says, globs expanded against the tracked files the way
    `extra_globs` is: the author stating the radius, so it is always firm. PROSE is what the
    body mentions, by `_PATHISH` or a backtick. That is weaker evidence, because a line every
    brief shares names the same file in every brief (#119). UNMATCHED lists each candidate that
    named no tracked file, and UNBACKTICKED_DIRS each existing directory the prose names bare,
    which never became a candidate. Together they are why a blind leaf is blind.
    """
    fileset = set(files)
    text = _text_of(leaf)
    prose_cands = set(_PATHISH.findall(text))
    for m in _BACKTICKED.findall(text):
        prose_cands.add(m.strip())
    declared, prose, unmatched = set(), set(), []
    for p in (leaf.paths_list if hasattr(leaf, "paths_list") else []):
        cand = p.strip()
        if not cand:
            continue
        hits = ({f for f in files if fnmatch.fnmatch(f, cand)} if _GLOB_CHARS.search(cand)
                else _resolve(cand, root, files, fileset))
        declared |= hits
        if not hits:
            unmatched.append({"candidate": cand, "source": "path"})
    named = set()
    for cand in prose_cands:
        cand = cand.strip().strip("`'\"(),;:")
        if not cand:
            continue
        named.add(cand.rstrip("/"))
        hits = _resolve(cand, root, files, fileset)
        prose |= hits
        if not hits:
            unmatched.append({"candidate": cand, "source": "prose"})
    bare_dirs = sorted({d for d in (m.rstrip(".") for m in _BARE_DIR.findall(text))
                        if d not in named and os.path.isdir(os.path.join(root, d))})
    return declared, prose, sorted(unmatched, key=lambda u: (u["source"], u["candidate"])), \
        bare_dirs


def boilerplate_paths(cfg, leaves, files=None):
    """Files that more than half of `leaves` name in PROSE, and at least two of them (#119).

    A path in a line every brief carries ("Follow .claude/builder-rules.md") is a fact about
    the campaign's house style, not about any one leaf's radius. #96 made boilerplate WORDS
    loose; this does the same for boilerplate PATHS, by the only evidence one brief cannot
    show: how many of its siblings say the same thing. `--path` is never counted, because a
    declaration is the author stating the radius, however many leaves share it.

    One leaf alone has nothing to compare against, so nothing is boilerplate and the estimate
    is what it was before.
    """
    files = tracked_files(cfg.root) if files is None else files
    counts = {}
    for leaf in leaves:
        _, prose, _, _ = _path_kinds(leaf, cfg.root, files)
        for f in prose:
            counts[f] = counts.get(f, 0) + 1
    return {f for f, n in counts.items() if n >= 2 and n * 2 > len(leaves)}


def blind_reasons(est):
    """Why a non-estimable estimate matched nothing, one line per cause, from its data."""
    out = []
    for u in est.get("unmatched") or []:
        if u["source"] == "path":
            out.append("declared path %s matched no tracked file" % u["candidate"])
        else:
            out.append("the brief names %s, which is not a tracked file or directory"
                       % u["candidate"])
    for d in est.get("unbackticked_dirs") or []:
        out.append("the brief names directory %s in prose; a directory in prose counts only "
                   "when it is backticked" % d)
    if not out:
        out.append("it declares no --path and its brief names no path or findable symbol")
    return out


def _symbols(leaf):
    firm, loose = _symbol_kinds(leaf)
    return firm | loose


def _symbol_kinds(leaf):
    """(backticked, bare) — symbols the brief MARKED as code, and identifiers found in its prose.

    Kept apart because they are different evidence. A backticked symbol is the author pointing
    at code. A bare identifier is any snake_case or camelCase word, and briefs in one campaign
    share boilerplate full of them — a house-rules pointer, a check command, a guardrail name —
    so every leaf's bare set greps the same files and every pair "collides" (#96).
    """
    text = _text_of(leaf)
    firm, loose = set(), set()
    for m in _BACKTICKED.findall(text):
        token = m.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]{2,60}", token) and "/" not in token:
            firm.add(token)
    for m in _IDENT.findall(text):
        if m.lower() not in _STOPWORDS and len(m) >= 5:
            loose.add(m)
    firm = {s for s in firm if s.lower() not in _STOPWORDS}
    return firm, {s for s in loose if s.lower() not in _STOPWORDS} - firm


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


def estimate(cfg, leaf, files=None, boilerplate=None):
    """Return {'paths': set, 'shared': set, 'symbols': set, 'basis': str}.

    `boilerplate` is `boilerplate_paths` over the leaves being compared. One leaf cannot see
    its siblings, so the caller that holds them all passes it in.
    """
    root = cfg.root
    files = tracked_files(root) if files is None else files
    named, prose, unmatched, bare_dirs = _path_kinds(leaf, root, files)
    house = (prose - named) & (boilerplate or set())
    declared = named | prose
    marked, bare = _symbol_kinds(leaf)
    symbols = marked | bare
    grepped_marked = _grep_symbols(root, files, marked) if marked else set()
    grepped_bare = _grep_symbols(root, files, bare) if bare else set()
    grepped = grepped_marked | grepped_bare
    extra = set()
    for glob in (cfg.get("collision") or {}).get("extra_globs") or []:
        extra.update(f for f in files if fnmatch.fnmatch(f, glob))

    all_paths = declared | grepped | extra
    shared_globs = (cfg.get("collision") or {}).get("always_serialize") or []
    shared = {f for f in all_paths if any(fnmatch.fnmatch(f, g) for g in shared_globs)}
    # WHAT MAY BLOCK (#96). Firm evidence — a path the brief names or declares, a file carrying a
    # symbol it backticked, a configured glob — blocks. Files reached ONLY through a bare prose
    # identifier are reported, not blocking, whenever the leaf has anything firm: shared
    # boilerplate made every pair of briefs "collide" on files neither touches, and a refusal
    # that fires on every pair trains `--despite-live` into a reflex, which is exactly when a
    # real collision gets waved through. A leaf with NOTHING firm still blocks on its bare
    # matches, because they are all the estimate it has, and an unknown radius is not a free one.
    # A path most briefs name in prose is the same kind of evidence (#119): loose, unless the leaf
    # names it some firmer way too. `--path` is never loose.
    firm = (declared - house) | grepped_marked | extra
    blocking = (firm if firm else grepped_bare | house) - shared

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
        "blocking": blocking,
        "shared": shared,
        "symbols": symbols,
        "basis": basis,
        "estimable": bool(all_paths),
        # Data, not prose, so a caller can say WHY a blind leaf is blind (#119).
        "unmatched": unmatched,
        "unbackticked_dirs": bare_dirs,
    }


def plan_waves(cfg, leaves, files=None, population=None):
    """Greedy wave assignment. Returns (waves, estimates, notes).

    `waves` is a list of lists of leaf ids; every leaf in one wave is pairwise
    non-overlapping on its *exclusive* paths. Non-estimable leaves get a wave to
    themselves — an unknown radius cannot be shown disjoint from anything.

    `population` is the set of briefs boilerplate is counted over (`boilerplate_paths`); it
    defaults to `leaves`. A wider one, such as every leaf of the campaign, tells a house line
    from a file two leaves really share better than the handful being placed can.
    """
    files = tracked_files(cfg.root) if files is None else files
    house = boilerplate_paths(cfg, leaves if population is None else population, files)
    estimates = {leaf["id"]: estimate(cfg, leaf, files, house) for leaf in leaves}
    notes = []
    waves, wave_paths = [], []
    solo = []

    for leaf in leaves:
        est = estimates[leaf["id"]]
        if never_integrated(leaf):
            # Never merged, so it cannot collide with anything: it joins the first wave and
            # occupies nothing there.
            if not waves:
                waves.append([])
                wave_paths.append(set())
            waves[0].append(leaf["id"])
            continue
        if not est["estimable"]:
            solo.append(leaf["id"])
            notes.append(
                "%s cannot be parallelised: %s (%s). Treating an unknown blast radius as "
                "colliding with everything — a false collision costs one wave, a missed "
                "one costs a merge conflict nobody is watching."
                % (leaf["id"], est["basis"], "; ".join(blind_reasons(est))))
            continue
        placed = False
        for i, taken in enumerate(wave_paths):
            clash = taken & est["blocking"]
            if not clash:
                waves[i].append(leaf["id"])
                taken |= est["blocking"]
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
            wave_paths.append(set(est["blocking"]))

    for leaf_id in solo:
        waves.append([leaf_id])
        wave_paths.append(set())

    shared_all = sorted({p for e in estimates.values() for p in e["shared"]})
    if shared_all:
        notes.append(
            "shared surface(s) excluded from the parallel decision and owed to serialised "
            "integration instead: %s" % ", ".join(shared_all[:8]))
    return waves, estimates, notes


# ------------------------------------------------- live claims (issue #71)
# `plan_waves` reads `graph.ready()`, which is unblocked AND UNCLAIMED. A leaf another Crawler
# is working right now is not merely absent from the output — it is absent from the INPUT, so
# its paths are never considered occupied. `overlap` cannot cover the gap either: it measures
# committed diffs, and a Crawler twenty minutes in with nothing committed has no commits on its
# branch, so it is not an in-flight branch by that definition and contributes nothing.
#
# Measured twice, on two consecutive campaigns of this repo. Both times `plan` recommended a
# wave-1 leaf whose declared paths overlapped a LIVE Crawler's on two files, `overlap` said
# "0 in-flight branch(es)", and the collision was avoided by hand. The second time the branch
# EXISTED — created at spawn — and still counted as zero, because it counts branches with
# commits. So the invisible window is not "before the branch exists", it is "before the first
# commit", which for these Crawlers was most of their run.
#
# This closes the window from the one fact that exists the whole time: the CLAIM. A claimed,
# live leaf gets the same blast-radius estimate `plan_waves` already computes for a ready one,
# and that estimate needs no commit, no diff and no branch.


def live_claims(graph):
    """(leaves, caveat) — claimed leaves whose owner may still be running.

    THE GRAPH OWNS CLAIM LIVENESS and this must not become a second statement of it. #68 (a
    live claim read as dead) and #69 (a dead session read as live) were both fixed in
    `stale_claims`/`stalled_claims`; re-deriving the rule here is how two layers start
    disagreeing silently. So this is the COMPLEMENT of `stale_claims` over in-progress leaves:
    anything the graph cannot prove abandoned is treated as still occupying its paths.

    That direction is deliberate and matches `plan_waves`: an unknown radius collides with
    everything, because a false collision costs one wave and a missed one costs a merge
    conflict nobody is watching.

    A BACKEND THAT CANNOT ANSWER SAYS SO. `BrGraph.stale_claims` refuses rather than returning
    an empty list, and folding that refusal into "nothing is live" would rebuild the exact
    blindness this exists to close. The refusal becomes a caveat carried beside the leaves, and
    every in-progress leaf counts as occupied until something can adjudicate it.
    """
    from .graph import IN_PROGRESS
    claimed = [l for l in graph.list(status=IN_PROGRESS) if not l.is_epic]
    try:
        stale = {leaf["id"] for leaf, _ in graph.stale_claims()}
    except Refused as exc:
        return claimed, ("owner liveness could NOT be adjudicated (%s) — every claimed leaf is "
                         "counted as occupied, which is conservative, not measured" % exc)
    return [l for l in claimed if l["id"] not in stale], None


def live_conflicts(cfg, leaf, live, files=None, population=None):
    """Would starting `leaf` now put it in files a LIVE Crawler is already in?

    Returns a list of findings, one per live claim that collides, each with the exclusive
    paths that BLOCK and the shared surfaces that do not.

    Scoped to `exclusive` paths for the same reason `plan_waves` is: paths matching
    `collision.always_serialize` are the file every change touches, and letting one of them
    block would serialise the whole campaign behind whichever Crawler happens to be running —
    a check that looks like an unexplained slowdown is one somebody removes. They are reported
    beside the decision and owed to serialised integration instead (#9).

    THIS IS AN ESTIMATE, from declared paths and grepped symbols, and every caller must print
    it as one. `overlap` measures; this guesses, and it guesses about work that has produced
    nothing measurable yet.

    `population` is what boilerplate paths are counted over, as in `plan_waves`; it defaults
    to this leaf and the live ones.
    """
    files = tracked_files(cfg.root) if files is None else files
    if never_integrated(leaf):
        return []                       # its branch never meets anything at integration
    house = boilerplate_paths(cfg, [leaf] + list(live) if population is None else population,
                              files)
    mine = estimate(cfg, leaf, files, house)
    out = []
    for other in live:
        if other["id"] == leaf["id"] or never_integrated(other):
            continue
        theirs = estimate(cfg, other, files, house)
        clash = mine["blocking"] & theirs["blocking"]
        # Overlap on prose-only evidence: printed beside the decision, never a refusal (#96).
        loose = (mine["exclusive"] & theirs["exclusive"]) - clash
        shared = mine["shared"] & theirs["shared"]
        # A leaf whose radius cannot be estimated at all is treated as colliding with
        # everything here too — the same rule `plan_waves` applies, for the same reason.
        blind = not mine["estimable"] or not theirs["estimable"]
        if not clash and not shared and not blind and not loose:
            continue
        out.append({
            "leaf": other["id"],
            "title": other.get("title") or "",
            "actor": other.get("actor"),
            "session": other.get("claim_session"),
            "pid": other.get("claim_pid"),
            "parked": bool(other.get("parked")),
            "files": sorted(clash),
            "loose": sorted(loose),
            "shared": sorted(shared),
            "basis": theirs["basis"],
            "blind": blind,
            # Which side is blind, and why, from the estimate's data (#119).
            "blind_reasons": {e["leaf"]: blind_reasons(e) for e in (mine, theirs)
                              if not e["estimable"]},
            # SHARED-ONLY IS NOT A BLOCK. Recorded so a caller can print the surface without
            # refusing over it, which is what keeps the refusal worth obeying.
            "blocks": bool(clash) or blind,
        })
    return out
