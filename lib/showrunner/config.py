"""Config load + validation.

showrunner is generic; a project's specifics are config, not code. The validation here
is deliberately strict in one direction: **anything whose misconfiguration would make a
guard silently a no-op is a hard refusal, not a warning** (INV8). A lock root that is
worktree-relative, or a worktree root outside the repo, are both configurations that
look like they are working right up until the run that they ruin.
"""

import json
import os

from .util import Refused, die, main_checkout, slug

CONFIG_NAME = "config.json"
CONFIG_LOCAL_NAME = "config.local.json"
STATE_DIR = ".showrunner"

DEFAULTS = {
    "project_name": None,
    "graph": {"backend": "auto", "db": ".showrunner/graph.db", "br_db": None},
    # Absolute path, shared by every worktree. See validate().
    "lock_root": None,
    "resources": [],
    "lanes": [],
    "default_lane": "serialized",
    "worktree_root": ".worktrees",
    "scratch_root": ".showrunner/scratch",
    "inject": [],
    "checks": [],
    "baseline": ".showrunner/baseline.json",
    "shared_state": [],
    "collision": {"extra_globs": [], "always_serialize": []},
    # dirs/companions default to detection; rule_files are compared byte-for-byte so a
    # Crawler cannot end up running under quietly weaker rules than the orchestrator.
    "harness": {"provision": "auto", "require": True},
}


def _campaign_from_env():
    """The selected campaign, or None. Read at LOAD time — see Config.__init__."""
    return (os.environ.get("SHOWRUNNER_CAMPAIGN") or "").strip() or None


class Config:
    def __init__(self, data, root, path, campaign=None):
        self.data = data
        self.root = root          # the MAIN checkout, absolute
        self.path = path          # where config.json was read from (may not exist)
        # CAPTURED ONCE, NOT READ ON EVERY ACCESS. The first version resolved the campaign
        # lazily from the environment inside each path property, so a loaded Config changed its
        # answers when os.environ moved — two configs loaded for two campaigns both reported the
        # second one's paths. A config object has to be a stable answer about ONE campaign, or
        # nothing downstream can hold one and trust it.
        self._campaign = campaign if campaign is not None else _campaign_from_env()

    # -- accessors ---------------------------------------------------------
    def get(self, key, default=None):
        return self.data.get(key, DEFAULTS.get(key, default))

    @property
    def project_name(self):
        return self.get("project_name") or os.path.basename(self.root)

    @property
    def campaign(self):
        """Which campaign this process is speaking for, or None for the repo-wide one.

        A CAMPAIGN IS SMALLER THAN A REPO (#39). The natural size of a body of work is often a
        story, and the handoff a showrunner charges — worktrees, briefs, rooms, integration — is
        only paid for by the parallelism it buys. So several campaigns in one checkout is the
        ORDINARY case, not a monorepo edge, and scoping everything per git root was wrong by
        default rather than wrong in an unusual layout.

        Read from the environment because the alternative is worse in a way this project has
        already recorded: a flag would have to be threaded through every verb, and any verb that
        forgot it would silently answer about a different campaign. An env var is inherited by a
        subtree, which is exactly right HERE — a Crawler dispatched into a campaign belongs to it,
        and its children do too. (That same inheritance is what makes an env var wrong for a
        per-generation fact like a role: see #38.)

        None is not a campaign named "": an empty or whitespace value means unset, and the
        repo-wide layout is used unchanged.
        """
        return self._campaign

    @property
    def state_dir(self):
        """Where this campaign's state lives. `.showrunner/` when there is only one.

        NAMED CAMPAIGNS NEST rather than sitting beside the default, so an existing checkout is
        untouched: with no campaign selected every path below resolves byte-identically to what
        it did before this existed, which is the property that makes adopting it safe.
        """
        base = os.path.join(self.root, STATE_DIR)
        c = self.campaign
        return os.path.join(base, "campaigns", slug(c, 60)) if c else base

    @property
    def graph_db(self):
        g = self.get("graph") or {}
        return self.abspath(g.get("db") or DEFAULTS["graph"]["db"])

    @property
    def graph_backend(self):
        return (self.get("graph") or {}).get("backend") or "auto"

    @property
    def br_db(self):
        v = (self.get("graph") or {}).get("br_db")
        return os.path.expanduser(v) if v else None

    @property
    def lock_root(self):
        """The ONE thing that must not follow a campaign (#39).

        A lock names a PHYSICAL single-consumer resource — a device, a bound port, a deploy
        target. Those are shared by the machine, not by a body of work, so two campaigns in one
        checkout both flashing the same TV must serialize against EACH OTHER. Scoping locks per
        campaign would give each its own lock root and let both hold "the device" at once: a
        mutex that is quietly a no-op, which config.validate already refuses in its other form
        (a worktree-relative lock root) as the worst available failure, because it looks like it
        works.

        So this resolves against the REPO, deliberately bypassing the campaign re-rooting in
        `abspath`. Caught by looking at the output rather than by reasoning: the first version
        of the campaign selector moved it, and moving it is the one change here that could lose
        somebody's hardware.
        """
        v = self.get("lock_root")
        if v:
            # Resolve a relative entry against the REPO ROOT, like every other configured
            # path here — never against the process cwd. `os.path.abspath` resolves against
            # cwd, which made the lock root differ per caller while still passing an
            # `isabs()` check, because abspath makes anything absolute.
            v = os.path.expanduser(v)
            return v if os.path.isabs(v) else os.path.join(self.root, v)
        # Default: inside the MAIN checkout's state dir — the REPO-WIDE one, never the
        # campaign's. Absolute by construction, and identical from every linked worktree
        # because the root derives from --git-common-dir, not from the cwd.
        return os.path.join(self.root, STATE_DIR, "locks")

    @property
    def worktree_root(self):
        return self.abspath(self.get("worktree_root"))

    @property
    def scratch_root(self):
        return self.abspath(self.get("scratch_root"))

    @property
    def baseline_path(self):
        return self.abspath(self.get("baseline"))

    def abspath(self, p):
        """A configured path, resolved against the repo root — or against the CAMPAIGN.

        A value written as `.showrunner/graph.db` names state, and state belongs to a campaign.
        So under a selected campaign such a path is re-rooted at `state_dir` and follows it,
        while anything else — `.worktrees`, an absolute path, a path outside `.showrunner/` — is
        resolved against the root exactly as before. That keeps ONE rule for a reader ("state
        paths follow the campaign") without a second config key to keep in sync.
        """
        if not p:
            return None
        p = os.path.expanduser(p)
        if os.path.isabs(p):
            return p
        prefix = STATE_DIR + os.sep
        if self.campaign and p.startswith(prefix):
            return os.path.join(self.state_dir, p[len(prefix):])
        return os.path.join(self.root, p)

    def resource(self, name):
        for r in self.get("resources") or []:
            if r.get("name") == name:
                return r
        return None

    def to_json(self):
        return json.dumps(self.data, indent=2, sort_keys=True)

    # -- validation --------------------------------------------------------
    def validate(self):
        """Return a list of (level, message) findings. level in {'error','warn','ok'}."""
        out = []
        root = os.path.realpath(self.root)

        # There is deliberately NO "lock_root must be absolute" branch here any more. It used
        # to test `isabs()` on a value that had already been through `abspath()`, which makes
        # any string absolute — a predicate with no failing input, sitting in the validator
        # written to prevent exactly this failure, returning an empty error list that reads as
        # "validated". A check that cannot fail is not a weak check; it was never a check.
        # The real hazards are covered by things that CAN fail: path_problem() below refuses an
        # unexpanded variable, and lock_root now resolves against the repo root rather than the
        # caller's cwd, so absoluteness is a property of the resolution and not of the input.
        lock_root = os.path.realpath(self.lock_root) if self.lock_root else None
        # The failure this exists to prevent: N worktrees, N sibling lock dirs, a mutex that
        # silently does nothing. If the lock root sits under a *linked* worktree it is per-tree
        # and therefore not a mutex at all.
        wt_root = os.path.realpath(self.worktree_root) if self.worktree_root else None
        if lock_root and wt_root and (lock_root == wt_root
                                      or lock_root.startswith(wt_root + os.sep)):
            out.append((
                "error",
                "lock_root (%s) is inside worktree_root (%s) — every Crawler would get its "
                "own lock and the mutex would silently be a no-op." % (lock_root, wt_root),
            ))
        elif lock_root:
            out.append(("ok", "lock_root is one absolute shared path: %s" % lock_root))

        # Worktrees must live INSIDE the repo, or each Crawler's own game_loop
        # write-guard (INV3: everything outside this repo is READ-ONLY) denies its very
        # first edit. See issue #4.
        wt = os.path.realpath(self.worktree_root) if self.worktree_root else None
        if not wt:
            out.append(("error", "worktree_root is unset"))
        elif not (wt == root or wt.startswith(root + os.sep)):
            out.append((
                "error",
                "worktree_root (%s) is outside the repo (%s). Each Crawler runs a per-agent "
                "harness whose write guard denies writes outside CLAUDE_PROJECT_DIR, so every "
                "Crawler would be denied its first edit. Put worktrees inside the repo "
                "(e.g. .worktrees/) and gitignore them." % (wt, root),
            ))
        elif wt == root:
            out.append(("error", "worktree_root must not be the repo root itself"))
        else:
            out.append(("ok", "worktree_root is inside the repo: %s" % wt))

        for r in self.get("resources") or []:
            if not r.get("name"):
                out.append(("error", "a resource has no name: %r" % r))
            if not r.get("match"):
                out.append(("warn", "resource %r has no match patterns — nothing will route to it"
                            % r.get("name")))

        known = {r.get("name") for r in self.get("resources") or []}
        for lane in self.get("lanes") or []:
            if lane.get("lane") == "serialized":
                res = lane.get("resource")
                if not res:
                    out.append(("error", "serialized lane rule %r names no resource" % lane.get("name")))
                elif res not in known:
                    out.append(("error", "lane rule %r references unknown resource %r"
                                % (lane.get("name"), res)))

        if self.get("default_lane") not in ("serialized", "headless"):
            out.append(("error", "default_lane must be 'serialized' or 'headless'"))
        elif self.get("default_lane") != "serialized":
            # Routing a serialized leaf into the headless lane collides on a
            # single-consumer resource; the reverse merely runs slower. Not comparable.
            out.append(("warn",
                        "default_lane is 'headless' — unclassified work will run in parallel. "
                        "The costs are not symmetric: a wrong headless route collides on a "
                        "single-consumer resource, a wrong serialized route is just slower."))

        for label, raw in (("lock_root", self.get("lock_root")),
                           ("worktree_root", self.get("worktree_root")),
                           ("scratch_root", self.get("scratch_root")),
                           ("baseline", self.get("baseline")),
                           ("graph.db", (self.get("graph") or {}).get("db")),
                           ("graph.br_db", (self.get("graph") or {}).get("br_db")),
                           ("harness.installer", (self.get("harness") or {}).get("installer"))):
            problem = path_problem(label, raw)
            if problem:
                out.append(("error", problem))
        for entry in self.get("inject") or []:
            raw = entry if isinstance(entry, str) else (entry or {}).get("path")
            problem = path_problem("inject path %r" % raw, raw)
            if problem:
                out.append(("error", problem))

        if not self.get("checks"):
            out.append(("warn",
                        "no checks configured — integration cannot tell a merged trunk that "
                        "still works from one that does not (issue #9)."))
        return out

    def require_valid(self):
        findings = self.validate()
        errors = [m for lvl, m in findings if lvl == "error"]
        if errors:
            raise Refused(
                "config is not safe to run with:\n  - " + "\n  - ".join(errors),
                code=2,
                hint="run `showrunner doctor` for the full report; fix %s" % self.path,
            )
        return findings


UNEXPANDED = "$"


def path_problem(label, raw):
    """A configured path that will not mean what its author thinks. Returns a message or None.

    `os.path.expanduser` handles a leading `~` and NOTHING else — `$HOME/x` comes back
    verbatim. So the most obvious portable-looking entry is silently a literal string that
    resolves against the process cwd, and the caller is left holding a belief the config does
    not support. That is invisible in the worst direction here: for a lock root it means a
    different directory per caller, which is a mutex that is quietly a no-op (INV8) — and it
    survived an `isabs()` check, because `abspath` makes anything absolute.
    """
    if not raw or not isinstance(raw, str):
        return None
    if UNEXPANDED in raw:
        return ("%s contains %r, which is NOT expanded: only a leading `~` is. The entry stays "
                "a literal string and resolves against whatever directory the caller happens "
                "to be in. Write `~/...` or a real absolute path." % (label, UNEXPANDED))
    return None


def find_root(start=None):
    root = main_checkout(start)
    if not root:
        die("not inside a git repository — showrunner orchestrates a repo", code=2)
    return root


def load(start=None, required=False, campaign=None):
    root = find_root(start)
    path = os.path.join(root, STATE_DIR, CONFIG_NAME)
    if not os.path.exists(path):
        if required:
            die("no %s — run `showrunner init` first" % path, code=2)
        return Config(dict(DEFAULTS), root, path, campaign=campaign)
    with open(path) as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            die("%s is not valid JSON: %s" % (path, exc), code=2)
    merged = dict(DEFAULTS)
    merged.update(data)

    # A LOCAL, UNTRACKED OVERLAY, and the absence of one is what caused a real leak. Some
    # settings are facts about THIS machine — where a chat tool is installed, an absolute
    # path only you have — and the tracked config is the wrong home for them: it ships to
    # every clone, so a stranger inherits a path that does not exist and a dependency they
    # never chose. Without somewhere else to put them they end up tracked anyway, which is
    # exactly how internal tooling reached a public repo here.
    #
    # Shallow merge by top-level key, deliberately: a deep merge lets a local file silently
    # half-override a rule (half a lane, half a resource) and produce a configuration nobody
    # wrote. Replacing a whole key is a change you can see.
    local_path = os.path.join(root, STATE_DIR, CONFIG_LOCAL_NAME)
    if os.path.exists(local_path):
        with open(local_path) as fh:
            try:
                local = json.load(fh)
            except json.JSONDecodeError as exc:
                die("%s is not valid JSON: %s" % (local_path, exc), code=2)
        if not isinstance(local, dict):
            die("%s must be a JSON object" % local_path, code=2)
        merged.update(local)
    return Config(merged, root, path, campaign=campaign)


def write(cfg):
    os.makedirs(os.path.dirname(cfg.path), exist_ok=True)
    with open(cfg.path, "w") as fh:
        fh.write(cfg.to_json() + "\n")
    return cfg.path
