"""Config load + validation.

showrunner is generic; a project's specifics are config, not code. The validation here
is deliberately strict in one direction: **anything whose misconfiguration would make a
guard silently a no-op is a hard refusal, not a warning** (INV8). A lock root that is
worktree-relative, or a worktree root outside the repo, are both configurations that
look like they are working right up until the run that they ruin.

FOUR LAYERS, EACH ONE OVERLAID ON THE ONE ABOVE IT:

    DEFAULTS                                   the tool's own answer
    ~/.config/showrunner/config.json           the USER — set once, applies to every repo
    <repo>/.showrunner/config.json             the PROJECT, tracked and shipped to every clone
    <repo>/.showrunner/config.local.json       THIS MACHINE, untracked

**THE PROJECT BEATS THE USER, WHICH IS THE OPPOSITE OF `roles.json`** — a file that lives in the
same user-level directory. The two are not the same kind of thing and must not share a rule:

    roles.json   is PERMISSION.  User level wins. A project that could redefine a role would be
                                 widening the policy that constrains the session editing it.
    config.json  is PREFERENCE.  The project wins. A repo is the better authority on its own
                                 lanes, checks and resources than a machine-wide default is.

Said again in `roles.py`, on purpose: a reader standing at either file must learn that the rule
is not uniform, and a caveat filed where the reader does not stand is a caveat they never had.

MERGE DEPTH IS UNIFORM ACROSS EVERY LAYER: dicts merge key by key, lists and scalars replace
wholesale. See `deep_merge` for why that is the same rule the shallow merge was protecting, not
a reversal of it. Two overlays with different depths would be two layers disagreeing about the
rules silently (INV12), and the disagreement would surface only in the config hardest to debug.

SOME KEYS ARE REFUSED AT USER LEVEL. A machine-wide value is wrong — not merely unusual — for
anything that names one repo's identity or one repo's state; see MACHINE_SCOPE_REFUSED.
"""

import json
import os

from .util import Refused, caller_tree, die, main_checkout, slug, user_config_dir

CONFIG_NAME = "config.json"
CONFIG_LOCAL_NAME = "config.local.json"
STATE_DIR = ".showrunner"

# The user layer. Resolved through the SAME helper as `roles.USER_PATH` and at the same moment
# (import), so the two files cannot drift about where "user level" is — and so the suite's
# `XDG_CONFIG_HOME` isolation (#46) covers this file for free, subprocess tests included.
USER_PATH = os.path.join(user_config_dir(), CONFIG_NAME)

# ONE LIST, AND IT LIVES HERE. This policy — which files under STATE_DIR are the tool's or a
# run's, and therefore never the consumer's source — had been written out by hand in three
# places: `cmd_init`, install.sh's create-if-absent heredoc, and install.sh's ensure-present
# upgrade loop. It drifted twice. First bin/ and lib/, fixed in install.sh while `init` went on
# writing the old list; then config.local.json, seen-issues.json and hook-heartbeat.jsonl, added
# to install.sh and never to `init`. The consequence is not cosmetic: config.local.json exists
# precisely so machine-specific values stay OUT of the tracked config (see the note at the
# shallow-merge below, which names the real leak that motivated it), so a repo where it is
# neither tracked nor ignored reopens that leak on the next `git add -A`.
#
# It lives in config.py rather than cli.py because this module already owns STATE_DIR and
# CONFIG_LOCAL_NAME — the layer that owns the concept owns the rule about it — and because a
# constant here is importable by the suite, which is what lets a test compare install.sh
# against it instead of comparing one hand-written copy to another.
#
# install.sh keeps its own literal copies deliberately: it must write this file before the
# tool is guaranteed runnable (under --central the binary it places is a shim that exits 1 when
# no central install exists yet), so making the installer shell out to Python here would trade a
# drift bug for an install that produces no ignore file at all. The suite closes the gap
# instead: test/run.py asserts install.sh's lists and this constant agree in BOTH directions, so
# an entry added to either side and not the other fails rather than drifting a third time.
#
# Sections carry their own comment because the reasons differ and a reader of the generated
# file deserves them: bin/ and lib/ are the TOOL, the rest are observations a campaign
# regenerates by running.
STATE_IGNORE_SECTIONS = [
    ("# showrunner runtime state — not source",
     ["graph.db", "graph.db-*", "locks/", "scratch/", "campaigns/", "campaign.json",
      "routing.jsonl",
      "waiting.jsonl", "events.jsonl", "hook-heartbeat.jsonl", "fail-open.jsonl",
      "*.lock", "baseline.json",
      "integration-commit.json"]),
    ("# Machine-specific overrides and per-machine observations. The docs point people here for\n"
     "# absolute paths, and without these lines the files land NEITHER TRACKED NOR IGNORED --\n"
     "# the exact state doctor flags elsewhere, and the state that reopens the leak\n"
     "# config.local.json exists to prevent.",
     [CONFIG_LOCAL_NAME, "seen-issues.json"]),
    ("# The TOOL, not this project — replaced wholesale on upgrade.",
     ["bin/", "lib/"]),
]


def state_ignore_entries():
    """Every path the tool claims under STATE_DIR, flat and in written order."""
    return [e for _, entries in STATE_IGNORE_SECTIONS for e in entries]


def state_ignore_text():
    """The .showrunner/.gitignore body `init` writes, comments and all."""
    return "".join("%s%s\n%s\n" % ("" if i == 0 else "\n", comment, "\n".join(entries))
                   for i, (comment, entries) in enumerate(STATE_IGNORE_SECTIONS))

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
    def __init__(self, data, root, path, campaign=None, tree=None, user_path=None):
        self.data = data
        self.root = root          # the MAIN checkout, absolute
        self.path = path          # where config.json was read from (may not exist)
        # The user-level file that CONTRIBUTED to `data`, or None when there was none. A merged
        # dict cannot be asked which layer a value came from, so the one question a confused
        # reader actually has — "is a file outside this repo affecting me?" — has to be answered
        # by recording it here and printing it (see `doctor`). None means no such file existed,
        # which is the case every repo that will never have one stays in.
        self.user_path = user_path
        # WHERE THIS CONFIG WAS LOADED FROM — the `--show-toplevel` of the directory `load()`
        # was called against, realpath'd, or None when nobody recorded one.
        #
        # `root` cannot answer this and never could: it resolves through `--git-common-dir`, so
        # it is the MAIN checkout from every linked worktree by design (INV8) — that is what
        # makes locks, config and the campaign record agree across Crawlers. Which leaves
        # nothing on a Config that says which TREE the caller is standing in, and the one
        # function that needed it (`roles.seat`) reached past its argument to `os.getcwd()`
        # instead. A function handed a config and answering about the ambient process is two
        # callers free to disagree, and a true answer to the wrong question is the most
        # convincing kind of wrong: inside a worktree it reported CRAWLER for a config built
        # for a repo somewhere else entirely.
        #
        # None is a real answer and is announced as one. A construction path that does not set
        # this makes `seat` say UNKNOWN, which is honest; quietly falling back to the process
        # cwd would restore exactly the defect this field exists to remove.
        self.tree = os.path.realpath(tree) if tree else None
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

        for label, raw in path_shaped(self.get):
            problem = path_problem(label, raw)
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


def path_shaped(get):
    """Every configured value that names a path, as (label, raw) pairs.

    ONE LIST, so `validate` and the user-layer check below cannot end up disagreeing about
    what is path-shaped — a key checked in one place and not the other is a rule with a hole
    in exactly the layer nobody was looking at. `get` is a `Config.get` or a plain dict's
    `.get`, which have the same signature; the caller decides whether DEFAULTS are behind it.
    """
    pairs = [("lock_root", get("lock_root")),
             ("worktree_root", get("worktree_root")),
             ("scratch_root", get("scratch_root")),
             ("baseline", get("baseline")),
             ("graph.db", (get("graph") or {}).get("db")),
             ("graph.br_db", (get("graph") or {}).get("br_db")),
             ("harness.installer", (get("harness") or {}).get("installer"))]
    for entry in get("inject") or []:
        raw = entry if isinstance(entry, str) else (entry or {}).get("path")
        pairs.append(("inject path %r" % raw, raw))
    return pairs


# WHAT A MACHINE-WIDE FILE MAY NOT SAY, and why each one is a refusal rather than a warning.
# Every entry here names something that belongs to ONE repo or ONE campaign; set once for the
# machine it is not a preference applied broadly, it is the same wrong answer everywhere.
MACHINE_SCOPE_REFUSED = (
    ("project_name",
     "it feeds dispatch.chat.channel_prefix and the orchestrator identity, so every repo on "
     "this machine would open its Crawler rooms under the same prefix. That collision has "
     "already been measured here with two campaigns in one checkout: rooms crossed, another "
     "campaign's messages received, and `owed` debt accrued for questions never seen"),
    ("lock_root",
     "a lock names a single-consumer resource, and one absolute root shared by UNRELATED repos "
     "makes them serialize against each other — while a per-repo default keeps the trees of one "
     "repo sharing one. A machine-wide value is a mutex that is quietly the wrong mutex"),
    ("graph.db",
     "the leaf graph is one campaign's state; there is nothing for it to mean machine-wide"),
    ("baseline",
     "the baseline is one campaign's observation of one repo; there is nothing for it to mean "
     "machine-wide"),
)


def _dotted(data, key):
    """(present, value) for a dotted key, so a nested entry can be refused by name."""
    cur = data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def deep_merge(base, over):
    """`over` laid on `base`: dicts merge key by key, lists and scalars REPLACE wholesale.

    THE RULE THE OLD SHALLOW MERGE WAS PROTECTING SURVIVES INTACT, which is why this is not a
    reversal of it. That comment refused a deep merge because "a deep merge lets a local file
    silently half-override a rule (half a lane, half a resource) and produce a configuration
    nobody wrote" — and the two things it names are LISTS. Lists still replace wholesale, so a
    half-written lane is still impossible; what changes is that `dispatch.chat` set at one layer
    now survives a layer below setting only `dispatch.default_model`, instead of vanishing.

    Applied at EVERY layer, deliberately. A user layer that deep-merged and a local layer that
    did not would be two overlays disagreeing about the rules, discoverable only by whoever is
    already debugging the config.
    """
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def read_config_file(path):
    """The JSON object at `path`, or None when there is no file. Refuses anything else.

    A missing file is a real answer and the common one. A file that exists and cannot be parsed
    is NOT "nothing there": returning {} for it would silently run the config the author thought
    they had replaced.
    """
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            die("%s is not valid JSON: %s" % (path, exc), code=2)
    if not isinstance(data, dict):
        die("%s must be a JSON object" % path, code=2)
    return data


def user_layer(path=None):
    """The user-level config, refused if it says anything only a repo may say. {} when absent."""
    path = path or USER_PATH
    data = read_config_file(path)
    if data is None:
        return {}
    for key, why in MACHINE_SCOPE_REFUSED:
        present, value = _dotted(data, key)
        if present and value is not None:
            die("%s sets %r (%r), which is not allowed in a machine-wide config: %s.\n"
                "Move it to the repo's .showrunner/%s." % (path, key, value, why, CONFIG_NAME),
                code=2)
    # Path-shaped values get the SAME rule as everywhere else — `path_problem`, not a second
    # one — but named against the file they came from, since a user file is the layer whose
    # author is least likely to be looking at the repo doctor reports on.
    for label, raw in path_shaped(data.get):
        problem = path_problem("%s: %s" % (path, label), raw)
        if problem:
            die(problem, code=2)
    return data


def find_root(start=None, fallback=None):
    root = main_checkout(start, fallback)
    if not root:
        die("not inside a git repository — showrunner orchestrates a repo", code=2)
    return root


def load(start=None, required=False, campaign=None, fallback=None):
    root = find_root(start, fallback)
    # BOTH DERIVED FROM THE SAME START DIR, which is what makes their difference meaningful:
    # `root` walks to the main checkout, `tree` stays where the caller stands, so `root != tree`
    # is precisely "this caller is in a linked worktree" with no second git call at the point of
    # use. Recorded at LOAD time for the same reason `campaign` is: a config object has to be a
    # stable answer about ONE place, or nothing downstream can hold one and trust it.
    tree = caller_tree(start)
    path = os.path.join(root, STATE_DIR, CONFIG_NAME)

    # THE USER LAYER SITS BENEATH THE PROJECT'S, so a setting made once applies to every repo
    # and any repo can still override it. Read even when this repo has no config of its own:
    # the whole point is that it does not need one. `user_layer` refuses the keys a machine-wide
    # file may not set (MACHINE_SCOPE_REFUSED) before any of it reaches a Config.
    user_path = USER_PATH if os.path.exists(USER_PATH) else None
    merged = deep_merge(DEFAULTS, user_layer())

    if not os.path.exists(path):
        if required:
            die("no %s — run `showrunner init` first" % path, code=2)
        return Config(merged, root, path, campaign=campaign, tree=tree, user_path=user_path)
    merged = deep_merge(merged, read_config_file(path))

    # A LOCAL, UNTRACKED OVERLAY, and the absence of one is what caused a real leak. Some
    # settings are facts about THIS machine — where a chat tool is installed, an absolute
    # path only you have — and the tracked config is the wrong home for them: it ships to
    # every clone, so a stranger inherits a path that does not exist and a dependency they
    # never chose. Without somewhere else to put them they end up tracked anyway, which is
    # exactly how internal tooling reached a public repo here.
    #
    # It is the LAST word, above the project and the user both, and it merges by the one rule
    # every layer here uses — see `deep_merge`, which explains why the half-a-lane hazard the
    # original shallow merge was written against is still impossible.
    local = read_config_file(os.path.join(root, STATE_DIR, CONFIG_LOCAL_NAME))
    if local is not None:
        merged = deep_merge(merged, local)
    return Config(merged, root, path, campaign=campaign, tree=tree, user_path=user_path)


def write(cfg):
    os.makedirs(os.path.dirname(cfg.path), exist_ok=True)
    with open(cfg.path, "w") as fh:
        fh.write(cfg.to_json() + "\n")
    return cfg.path
