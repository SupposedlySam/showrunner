"""Config load + validation.

showrunner is generic; a project's specifics are config, not code. The validation here
is deliberately strict in one direction: **anything whose misconfiguration would make a
guard silently a no-op is a hard refusal, not a warning** (INV8). A lock root that is
worktree-relative, or a worktree root outside the repo, are both configurations that
look like they are working right up until the run that they ruin.
"""

import json
import os

from .util import Refused, die, main_checkout

CONFIG_NAME = "config.json"
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


class Config:
    def __init__(self, data, root, path):
        self.data = data
        self.root = root          # the MAIN checkout, absolute
        self.path = path          # where config.json was read from (may not exist)

    # -- accessors ---------------------------------------------------------
    def get(self, key, default=None):
        return self.data.get(key, DEFAULTS.get(key, default))

    @property
    def project_name(self):
        return self.get("project_name") or os.path.basename(self.root)

    @property
    def state_dir(self):
        return os.path.join(self.root, STATE_DIR)

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
        v = self.get("lock_root")
        if v:
            # Resolve a relative entry against the REPO ROOT, like every other configured
            # path here — never against the process cwd. `os.path.abspath` resolves against
            # cwd, which made the lock root differ per caller while still passing an
            # `isabs()` check, because abspath makes anything absolute.
            return self.abspath(v)
        # Default: inside the MAIN checkout's state dir. Absolute by construction, and
        # identical from every linked worktree because state_dir derives from
        # --git-common-dir, not from the cwd.
        return os.path.join(self.state_dir, "locks")

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
        if not p:
            return None
        p = os.path.expanduser(p)
        return p if os.path.isabs(p) else os.path.join(self.root, p)

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

        lock_root = os.path.realpath(self.lock_root) if self.lock_root else None
        if not lock_root or not os.path.isabs(lock_root):
            out.append(("error", "lock_root must be an absolute path; got %r" % self.lock_root))
        else:
            # The failure this exists to prevent: N worktrees, N sibling lock dirs, a
            # mutex that silently does nothing. If the lock root sits under a *linked*
            # worktree it is per-tree and therefore not a mutex at all.
            wt_root = os.path.realpath(self.worktree_root) if self.worktree_root else None
            if wt_root and (lock_root == wt_root or lock_root.startswith(wt_root + os.sep)):
                out.append((
                    "error",
                    "lock_root (%s) is inside worktree_root (%s) — every Crawler would get its "
                    "own lock and the mutex would silently be a no-op." % (lock_root, wt_root),
                ))
            else:
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


def load(start=None, required=False):
    root = find_root(start)
    path = os.path.join(root, STATE_DIR, CONFIG_NAME)
    if not os.path.exists(path):
        if required:
            die("no %s — run `showrunner init` first" % path, code=2)
        return Config(dict(DEFAULTS), root, path)
    with open(path) as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            die("%s is not valid JSON: %s" % (path, exc), code=2)
    merged = dict(DEFAULTS)
    merged.update(data)
    return Config(merged, root, path)


def write(cfg):
    os.makedirs(os.path.dirname(cfg.path), exist_ok=True)
    with open(cfg.path, "w") as fh:
        fh.write(cfg.to_json() + "\n")
    return cfg.path
