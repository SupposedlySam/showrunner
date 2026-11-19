"""`gc --online`: prove a held tree integrated through the merged PR its branch was pushed to (#116).

THE SHAPE NO LEAF HOLDS. Several leaves push to one PR branch, GitHub squash-merges it and deletes
the branch. main carries the squash, which matches no leaf's bytes; the final head lives only on
GitHub, so `_integrated_via_leaf` has no anchor. In the reporter's campaign that was the normal
shape — four trees HELD as "not in origin/main" whose work had landed.

OPT-IN, because it is the network. Without `--online`, gc makes no call; the most it can say is
the offline hint below. With it, a tree counts as integrated only when ALL THREE hold:
  (a) the PR is merged;
  (b) its merge commit is reachable from the base gc judged against — GitHub saying "merged" is
      a claim about GitHub, and this makes it a fact about this clone;
  (c) the leaf's tip is an ancestor of the PR's head, fetched from `refs/pull/<n>/head` when the
      deleted branch took the local copy with it.

FAIL CLOSED, AND SAY WHICH. No gh, gh failing, a remote that is not GitHub, a head that cannot be
fetched, an ancestry check that fails: each leaves the tree HELD with that reason. "Could not
look" must never read as "not merged" — the first is fixable by the operator, the second is a
fact about the work, and a row that conflates them sends someone to redo landed work.

ONE CALL PER HEAD REF, which is one per PR branch: every leaf that pushed to the same branch
shares it. The ref is the branch's configured upstream (`branch.<b>.merge`) when it has one, and
its own name otherwise. A leaf pushed to a differently named branch WITHOUT an upstream is not
found; it stays HELD, which is the safe side.
"""

import json
import os
import re

from .util import git, run

_GITHUB = re.compile(r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|"
                     r"ssh://git@github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$")


def gh_bin():
    """The gh to run, or None. `SHOWRUNNER_GH`, when set, is AUTHORITATIVE: the suite points it at
    a stub, and a stub path that does not exist must read as gh absent, not fall through to the
    real one on PATH."""
    env = os.environ.get("SHOWRUNNER_GH")
    if env is not None:
        return env if os.access(env, os.X_OK) else None
    from shutil import which
    for c in ("gh", "/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"):
        p = c if os.path.isabs(c) else which(c)
        if p and os.access(p, os.X_OK):
            return p
    return None


def base_remote(cfg, base):
    """The remote `base` belongs to: its first segment when that is a remote, else origin."""
    rc, out, _ = git(["remote"], cwd=cfg.root)
    remotes = (out or "").split() if rc == 0 else []
    head = (base or "").split("/", 1)[0]
    if "/" in (base or "") and head in remotes:
        return head
    return "origin" if "origin" in remotes else None


def github_repo(cfg, remote):
    """(owner, repo, None) or (None, None, why). Read from config, NOT `remote get-url`, which
    applies `insteadOf` and would report a rewritten URL rather than the repo it names."""
    if not remote:
        return None, None, "this clone has no remote to derive a GitHub repo from"
    rc, out, _ = git(["config", "--get", "remote.%s.url" % remote], cwd=cfg.root)
    url = (out or "").strip()
    m = _GITHUB.match(url) if rc == 0 else None
    if not m:
        return None, None, ("remote %s (%s) is not on GitHub, so there is no PR to ask about"
                            % (remote, url or "no URL"))
    return m.group(1), m.group(2), None


def upstream(cfg, branch):
    """(remote, head ref name, gone) for a branch's configured upstream, or (None, None, False).

    `gone` comes from `%(upstream:track)`, which reads `[gone]` only once the remote-tracking ref
    has been PRUNED locally — a fetch with --prune, or fetch.prune set. No network is involved,
    and a clone that never pruned still shows the tracking ref, so `gone` False is not evidence
    the branch exists."""
    rc_r, remote, _ = git(["config", "--get", "branch.%s.remote" % branch], cwd=cfg.root)
    rc_m, merge, _ = git(["config", "--get", "branch.%s.merge" % branch], cwd=cfg.root)
    remote, merge = (remote or "").strip(), (merge or "").strip()
    if rc_r != 0 or rc_m != 0 or not remote or remote == "." or not merge.startswith("refs/heads/"):
        return None, None, False
    rc, out, _ = git(["for-each-ref", "--format=%(upstream:track)", "refs/heads/%s" % branch],
                     cwd=cfg.root)
    return remote, merge[len("refs/heads/"):], rc == 0 and "[gone]" in (out or "")


def _tip(cfg, branch):
    rc, out, _ = git(["rev-parse", "--verify", "--quiet", "refs/heads/%s^{commit}" % branch],
                     cwd=cfg.root)
    return (out or "").strip() if rc == 0 else None


def _have(cfg, sha):
    return git(["cat-file", "-e", "%s^{commit}" % sha], cwd=cfg.root)[0] == 0


def _pulls(gh, owner, repo, ref):
    """(prs, None) or (None, why). One call: closed PRs whose head is `owner:ref`."""
    rc, out, err = run([gh, "api", "repos/%s/%s/pulls?state=closed&per_page=100&head=%s:%s"
                        % (owner, repo, owner, ref)], timeout=60)
    if rc != 0:
        first = ((err or out or "").strip().splitlines() or ["no output"])[0]
        return None, "gh api failed (exit %d: %s)" % (rc, first[:160])
    try:
        prs = json.loads(out or "")
    except ValueError:
        return None, "gh api answered something that is not JSON"
    if not isinstance(prs, list):
        return None, "gh api answered %s, not a list of PRs" % type(prs).__name__
    return prs, None


def prove(cfg, findings, base):
    """Mark each candidate finding PR-proved (`integrated_pr`) or record why not (`pr_why`).

    `findings` are the ones the caller would otherwise hold ONLY for not being integrated; this
    never reconsiders a dirty, live or unreadable tree. A failure that applies to every finding
    (no gh, not GitHub) is recorded on each, so every HELD row carries its own reason.
    """
    findings = [f for f in findings if f.get("branch") and f.get("branch_exists")]
    if not findings:
        return

    def all_why(why):
        for f in findings:
            f["pr_why"] = why

    remote = base_remote(cfg, base)
    owner, repo, why = github_repo(cfg, remote)
    if why:
        return all_why(why)
    gh = gh_bin()
    if not gh:
        return all_why("gh is not installed (or SHOWRUNNER_GH names nothing executable), so "
                       "GitHub could not be asked — not the same as not merged")

    # Group by head ref so leaves sharing one PR branch share one call.
    by_ref = {}
    for f in findings:
        up_remote, up_ref, _gone = upstream(cfg, f["branch"])
        ref = up_ref if up_ref and up_remote == remote else f["branch"]
        by_ref.setdefault(ref, []).append(f)
    fetched = {}
    for ref, group in sorted(by_ref.items()):
        prs, why = _pulls(gh, owner, repo, ref)
        if why:
            for f in group:
                f["pr_why"] = "%s, so whether %s's PR merged is UNKNOWN" % (why, ref)
            continue
        merged = [p for p in prs if isinstance(p, dict) and p.get("merged_at")]
        for f in group:
            f["pr_why"] = _judge(cfg, f, ref, prs, merged, base, remote, fetched)


def _judge(cfg, f, ref, prs, merged, base, remote, fetched):
    """None when a PR proves `f` integrated (and marks it), else the reason it stays HELD."""
    if not prs:
        return "no closed PR has head %s" % ref
    if not merged:
        return "PR %s from %s was closed without merging" % (
            ", ".join("#%s" % p.get("number") for p in prs if isinstance(p, dict)), ref)
    tip = _tip(cfg, f["branch"])
    if not tip:
        return "its tip could not be read, so no PR can be checked against it"
    reasons = []
    for p in merged:
        n, head = p.get("number"), (p.get("head") or {}).get("sha") or ""
        merge = p.get("merge_commit_sha") or ""
        if not merge or git(["merge-base", "--is-ancestor", merge, base], cwd=cfg.root)[0] != 0:
            reasons.append("PR #%s's merge commit %s is not reachable from %s (fetch it?)"
                           % (n, merge[:12] or "?", base))
            continue
        if not head:
            reasons.append("PR #%s names no head commit" % n)
            continue
        if not _have(cfg, head) and n not in fetched:
            fetched[n] = git(["fetch", "--quiet", "--no-tags", remote,
                              "refs/pull/%s/head" % n], cwd=cfg.root)[0] == 0
        if not _have(cfg, head):
            reasons.append("PR #%s's head %s could not be fetched from refs/pull/%s/head"
                           % (n, head[:12], n))
            continue
        rc = git(["merge-base", "--is-ancestor", tip, head], cwd=cfg.root)[0]
        if rc == 0:
            f["integrated_pr"] = {"number": n, "head": head, "merge": merge, "tip": tip}
            return None
        reasons.append("its tip %s is not an ancestor of PR #%s's head %s%s"
                       % (tip[:12], n, head[:12], "" if rc == 1 else " (ancestry check failed)"))
    return "; ".join(reasons)
