"""Roles as CONSUMER config: showrunner enforces the shape, never the meaning (#40).

The tempting design is a taxonomy — `lead`, `worker`, maybe `reviewer` — baked into the tool. This
repo has already recorded why that is wrong, in `dispatch.chat_path`: an earlier version hardcoded
a vendoring layout and "quietly pinned a public project to an internal tool. A consumer names their
own path or turns chat off; showrunner knows only that a CLI exists." Roles are that, and lane
rules are the precedent — showrunner routes by rules a consumer writes and has no opinion about
what `device` means.

**TWO ACQUISITION MODES ARE THE WHOLE MODEL**, and they replace lead/worker entirely, because that
distinction was never about names — it was about how the seat is obtained:

    claim    a session TAKES an open seat. Exclusive, first-come, with pid+boot liveness so a
             dead holder's seat is reclaimable. The only way a from-scratch session can get a
             role, because nothing existed before it to assign one.
    assign   written by whoever CREATED the session, before it existed, keyed to its worktree. A
             session holding an assignment cannot claim: its role was already decided.

Self-nomination is safe under `claim`, which is not obvious. The hazard was never "a session
believes it leads" — it is TWO sessions believing it with nobody told. Exclusivity, liveness and a
visible roster cover that without any provenance sniffing.

**WHY A CLAIM IS A LOCK AND NOT A NEW MECHANISM.** Exclusive, first-come, held by a live process,
reclaimable when that process is proved dead — that is `locks.Lock`, exactly, including the
UNREADABLE state that refuses to reclaim what it cannot prove dead. Re-implementing it here would
produce a second mutex that drifts from the first, and the failure mode of a drifted mutex is
silence. Claims live under the CAMPAIGN's state dir rather than the repo-wide lock root, because
two campaigns each legitimately want their own lead (#39) — while a physical device stays shared.

**WHERE DEFINITIONS LIVE, AND WHY NOT IN THE REPO.** Measured rather than assumed: a Write to
`.showrunner/config.json` is ALLOWED by the write guard, so roles defined there can be widened by
the very session they constrain — the same defect as a seat file a session can edit. A user-level
path outside every allow_write_root is denied and needs an explicit human authorization to change,
which is the right bar for a policy about what a session may do.

    ~/.config/showrunner/roles.json          authoritative
    .showrunner/config.json  "roles"         a project may ADD a role, never redefine one
    ...either file's "seat_roles"            {seat: role} — a project may map a seat the user
                                             left unmapped, never REMAP one, for the same reason

Note for other setups: `~/.claude` is an allow_write_root on a default install and is therefore
NOT such a path, even though it is denied in this checkout.

**WHAT IS CHECKED AND WHAT IS NOT.** Everything here is shape. `reports_to` must name a role that
exists and must not cycle; `may_create` must name roles that exist; there must be a root; something
must be claimable, or no from-scratch session could ever acquire anything. `notes` is consumer
prose and is announced as explicitly unchecked. Nothing here learns what a role MEANS.
"""

import json
import os

from . import locks
from .util import pid_alive, session_pid, short_session, slug, user_config_dir

# PRECEDENCE HERE IS THE OPPOSITE OF `config.json`'s, AND THAT IS DELIBERATE. Both files live in
# `user_config_dir()` and both are overlaid by the project, so a reader standing at either one
# will assume the other works the same way. It does not:
#
#     roles.json   is PERMISSION.  The user level WINS (see `spec` and `seat_roles` below) —
#                                  project-wins would let a session widen the policy that
#                                  constrains it, which is privilege escalation.
#     config.json  is PREFERENCE.  The PROJECT wins (see config.load) — a repo is the better
#                                  authority on its own lanes, checks and resources.
#
# Stated in both files rather than in one, because a caveat filed where the reader does not
# stand is a caveat they never had.
USER_PATH = os.path.join(user_config_dir(), "roles.json")

FALLBACK = "unassigned"
ACQUIRE = ("claim", "assign")

# The shape of a role, and the whole vocabulary showrunner has for one.
FIELDS = ("acquire", "capacity", "reports_to", "may_create", "writes", "notes")

# The one seat whose assignment showrunner already records. Kept as config rather than code for
# the same reason the roles themselves are: a taxonomy in the tool is a taxonomy its consumers
# have to argue with.
SEAT_ROLES_KEY = "seat_roles"

# The porcelain contract's version, so a guard parsing it can refuse a shape it does not
# know rather than silently misread one. Bump it when a field changes meaning.
PORCELAIN_VERSION = 1


def _read(path):
    """(defs, problem). A missing file is not a problem; an unreadable one is."""
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return {}, "%s could not be read (%s)" % (path, exc)
    if not isinstance(data, dict):
        return {}, "%s is not a JSON object" % path
    roles = data.get("roles") if "roles" in data else data
    if not isinstance(roles, dict):
        return {}, "%s has no 'roles' object" % path
    return roles, None


def spec(cfg):
    """Merged role definitions. Returns (roles, problems).

    USER LEVEL IS AUTHORITATIVE AND A PROJECT MAY ONLY ADD. A project that could redefine a
    user-level role would be able to widen the policy that constrains it, which is the whole
    reason the definitions left the repo — so a redefinition is reported as a problem and the
    user-level version is kept.
    """
    problems = []
    roles, err = _read(USER_PATH)
    if err:
        problems.append(err)
    roles = dict(roles)

    project = (cfg.get("roles") or {}) if hasattr(cfg, "get") else {}
    if isinstance(project, dict):
        for name, d in project.items():
            if name in roles:
                problems.append(
                    "%r is defined in this project AND at user level; the user-level definition "
                    "wins. A project may ADD a role, never redefine one — otherwise the policy a "
                    "session runs under is editable by that session." % name)
                continue
            roles[name] = d
    return roles, problems


def _read_seat_roles(path):
    """({seat: role}, problem). A missing file or a missing map is not a problem."""
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return {}, "%s could not be read (%s)" % (path, exc)
    if not isinstance(data, dict):
        return {}, None
    m = data.get(SEAT_ROLES_KEY) or {}
    if not isinstance(m, dict):
        return {}, "%s has a %r that is not a JSON object, so no seat resolves through it" % (
            path, SEAT_ROLES_KEY)
    return dict((str(k), str(v)) for k, v in m.items()), None


def seat_roles(cfg):
    """Merged {seat: role}. Returns (map, problems).

    USER LEVEL IS AUTHORITATIVE AND A PROJECT MAY ONLY ADD, exactly as for the definitions. A
    project that could remap its own seat would hand itself any role in the catalog, which is the
    widening the definitions left the repo to prevent.
    """
    m, err = _read_seat_roles(USER_PATH)
    problems = [err] if err else []
    m = dict(m)
    project = (cfg.get(SEAT_ROLES_KEY) or {}) if hasattr(cfg, "get") else {}
    if isinstance(project, dict):
        for where, role in project.items():
            if str(where) in m:
                problems.append(
                    "the seat %r is mapped in this project AND at user level; the user-level "
                    "mapping wins. A project may map a seat the user left unmapped, never remap "
                    "one." % where)
                continue
            m[str(where)] = str(role)
    return m, problems


def validate(roles):
    """Shape findings, as (level, message). Never raises; never judges meaning."""
    out = []
    if not roles:
        return [("ok", "no roles defined — every session resolves to %r, which grants nothing "
                       "and creates nothing" % FALLBACK)]

    for name, d in sorted(roles.items()):
        if not isinstance(d, dict):
            out.append(("error", "role %r is not an object" % name))
            continue
        acq = d.get("acquire")
        if acq not in ACQUIRE:
            out.append(("error", "role %r has acquire=%r; it must be one of %s — that pair IS the "
                                 "model, and a role with neither cannot be obtained at all"
                        % (name, acq, " or ".join(ACQUIRE))))
        cap = d.get("capacity", 1)
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
            out.append(("error", "role %r has capacity=%r; it must be a positive integer"
                        % (name, cap)))
        rt = d.get("reports_to")
        if rt is not None and rt not in roles:
            out.append(("error", "role %r reports_to %r, which is not a defined role — the "
                                 "escalation path would dead-end at a name nobody holds"
                        % (name, rt)))
        for target in d.get("may_create") or []:
            if target not in roles:
                out.append(("error", "role %r may_create %r, which is not a defined role"
                            % (name, target)))
        for key in d:
            if key not in FIELDS:
                out.append(("warn", "role %r carries %r, which showrunner does not check or act "
                                    "on. Only %s mean anything here."
                            % (name, key, ", ".join(FIELDS))))

    roots = [n for n, d in roles.items() if isinstance(d, dict) and not d.get("reports_to")]
    if not roots:
        out.append(("error", "no ROOT role — every role reports to another, so the escalation "
                             "path has no end and `reports_to` cannot answer 'who do I tell'"))

    claimable = [n for n, d in roles.items()
                 if isinstance(d, dict) and d.get("acquire") == "claim"]
    if not claimable:
        out.append(("error", "nothing is claimable — every role must be ASSIGNED, so a session "
                             "started from scratch could never acquire one. Something has to be "
                             "obtainable by a session that nobody created for a purpose"))

    if FALLBACK in roles and (roles[FALLBACK] or {}).get("may_create"):
        out.append(("error", "the fallback role %r may_create — an unresolved session would be "
                             "able to dispatch, which makes the fallback a way AROUND the policy "
                             "rather than the absence of one" % FALLBACK))

    # Cycles: reports_to is an org edge, and an org that reports to itself has no top.
    for start in sorted(roles):
        seen, cur = [], start
        while cur and cur in roles and isinstance(roles[cur], dict):
            if cur in seen:
                out.append(("error", "reports_to CYCLE: %s — escalation would loop forever"
                            % " -> ".join(seen[seen.index(cur):] + [cur])))
                break
            seen.append(cur)
            cur = roles[cur].get("reports_to")

    if not [f for f in out if f[0] == "error"]:
        out.append(("ok", "%d role(s) defined, shape valid: %s"
                    % (len(roles), ", ".join(sorted(roles)))))
    return out


def _claims_root(cfg):
    """Where role claims live: the CAMPAIGN's state, not the repo-wide lock root.

    Two campaigns in one checkout each legitimately want their own lead (#39), while a physical
    device stays shared — so these are the one kind of lock that follows the campaign.
    """
    return os.path.join(cfg.state_dir, "roles")


def roster(cfg):
    """Every held role claim, with the state of its holder. Read-only."""
    root = _claims_root(cfg)
    out = []
    if not os.path.isdir(root):
        return out
    for d in sorted(os.listdir(root)):
        if not d.endswith(".lock"):
            continue
        name = d[:-len(".lock")]
        state, holder = locks.Lock(root, name).state()
        out.append({"role": name, "state": state, "holder": holder or {}})
    return out


def _seat_lock(cfg, role, seat):
    return locks.Lock(_claims_root(cfg), "%s#%d" % (slug(role, 40), seat))


def claim(cfg, role, session, pid=None, who=None, seat=0):
    """Take an open seat. Returns (ok, holder). Exclusivity and liveness come from locks.Lock.

    THE PID IS DISCOVERED, NOT HANDED OVER, when the caller does not supply one — because a
    claim keyed to a process that exits when the call returns is DEAD ON ARRIVAL. It reports
    success, and the very next read of the roster says STALE, so `whoami` announces the fallback
    while the claimer was told `claimed: True`. Reported from a real seating attempt, recovered
    only by walking the ancestry by hand to find the long-lived session — which is exactly what
    `util.session_pid` already does, with a `basis` that separates "proved it" from "could not
    tell". This is the same hazard `lock acquire` warns about in its own output; the roles path
    shared the mechanism and not the mitigation.

    A pid that cannot be resolved is REFUSED rather than recorded, following `lease`: a claim
    with no liveness is not a weaker claim, it is a lock nothing can ever reclaim. `pid_basis`
    travels with the holder so a reader sees which fact the liveness rests on.
    """
    basis = "supplied"
    if pid is None:
        pid, basis = session_pid()
        if pid is None:
            return False, {"pid_basis": basis, "role": role,
                           "why": "no session process could be resolved, so this claim would "
                                  "have no liveness at all and was NOT taken"}
    lock = _seat_lock(cfg, role, seat)
    ok = lock.acquire(pid, who or session or "?", session=session,
                      extra={"role": role, "seat": str(seat), "pid_basis": basis})
    return ok, lock.holder()


def release(cfg, role, pid=None, seat=0, force=False):
    """Give a seat back. Returns (ok, holder-before).

    The counterpart `claim` never had, which is why a seat could only be given up by outliving
    it or by deleting a file. The pid is discovered the same way `claim` discovers it, because
    `locks.Lock.release` checks the releaser against the recorded holder and the process calling
    a CLI verb is never the process that was recorded.
    """
    lock = _seat_lock(cfg, role, seat)
    before = lock.holder()
    if pid is None:
        pid = session_pid()[0] or os.getpid()
    return lock.release(pid=pid, force=force), before


# ------------------------------------------------------------------- the seat
# THE SEAT IS DERIVED, NEVER DECLARED (#36). A consumer's prototype kept it in a one-word file
# containing `lead` or `worker`, with two PreToolUse guards gated on `^lead`. The file said
# `worker`, written mid-run, so both guards exited 0 for the remaining 16 hours — a one-word file
# was a global off switch and nothing announced its value. There is no state a session can write
# to become something else here, which is the entire point.
CRAWLER, ORCHESTRATOR, SOLO, UNKNOWN = "crawler", "orchestrator", "solo", "unknown"


def crawler_leaf(cfg):
    """The leaf the campaign record names for THIS worktree, or None. Never raises.

    Its own function because the distinction it draws is load-bearing for policy and not only for
    the announcement: a worktree `spawn` placed is a record showrunner wrote BEFORE the session
    existed, and a worktree somebody added by hand is not. Only the former may resolve to a
    working role -- otherwise `git worktree add` is a way to grant yourself one.
    """
    from . import campaign as _campaign

    tree = getattr(cfg, "tree", None)
    if not tree:
        return None
    here = os.path.basename(tree)
    try:
        for c in (_campaign.load(cfg).get("crawlers") or []):
            if c.get("crawler") == here:
                return c.get("leaf")
    except Exception:                                           # noqa: BLE001
        return None
    return None


def seat(cfg):
    """Where this session STANDS. Returns (seat, evidence). Never raises.

    Derived from two facts THE CONFIG already carries: the tree it was loaded from against the
    main checkout it resolved to, and whether this repo carries a campaign. UNKNOWN is a real
    answer and is announced as one — an announcer that cannot tell and says nothing is
    indistinguishable from a healthy one, which is exactly how the reported failure went
    unnoticed for a whole run.

    ANSWERED FROM `cfg`, NEVER FROM THE AMBIENT PROCESS. This ran two `git rev-parse` calls
    against `os.getcwd()` while taking a config it used only for the campaign record, so it
    answered about wherever the process happened to stand rather than about the repo it was
    handed. Every caller inside a linked worktree got CRAWLER for a config built elsewhere, the
    `seat_roles` mapping never fired, and five assertions failed in every worktree and nowhere
    else — a defect that presents as "the suite is flaky in worktrees" and is really a function
    disagreeing with its own argument.

    `cfg.root` is NOT the fix and must not be substituted for `cfg.tree`: it resolves through
    `--git-common-dir` and so is the main checkout from every worktree, which would make every
    seat an ORCHESTRATOR. It is one HALF of the comparison; `cfg.tree` is the other.
    """
    from . import campaign as _campaign

    tree = getattr(cfg, "tree", None)
    if not tree:
        return UNKNOWN, ("this config does not record which working tree it was loaded from, so "
                         "neither seat can be derived — said rather than guessed from the "
                         "process's own directory, which is a fact about the caller and not "
                         "about this repo")
    main = os.path.realpath(cfg.root)

    if tree != main:
        leaf = crawler_leaf(cfg)
        return CRAWLER, ("standing in a linked worktree (%s)%s"
                         % (os.path.basename(tree),
                            "; the campaign record names its leaf %s" % leaf if leaf else
                            "; no campaign record names it, so it was not placed by spawn"))

    try:
        crawlers = _campaign.load(cfg).get("crawlers") or []
    except Exception:                                           # noqa: BLE001
        return UNKNOWN, "the campaign record could not be read, so this cannot tell an "\
                        "orchestrator from a solo session"
    if crawlers:
        return ORCHESTRATOR, ("standing in the main checkout of a repo that carries a campaign "
                              "(%d recorded Crawler(s))" % len(crawlers))
    return SOLO, ("standing in the main checkout, and no campaign has been started here — this "
                  "is said rather than left to look like an idle orchestrator")


def enforced_lines(role_def):
    """The ENFORCED block, GENERATED from the role's own fields (#40).

    Announcement prose in one place and enforcement in another is two independent statements of
    one policy, free to disagree — and a session told something no guard enforces has been given
    a rule that is not one. So every line here is derived from a field a guard actually reads.
    `notes` is deliberately NOT included: it is consumer prose and is announced separately as
    unchecked.

    AND THE LABEL HAD TO SPLIT, because that promise was not true of every line (#77). This
    block printed `ENFORCED` over `may NOT write: **` — and showrunner ships NO write guard.
    `writes` appears in exactly three places here: the field list, this printer, and the
    porcelain a CONSUMER'S hook reads. So the strongest thing showrunner can honestly say about
    it is that it published the policy; whether anything enforces it is a fact about the
    consumer's registration, which showrunner does not perform and cannot see.

    A reporter lost half an hour to that: the seat said ENFORCED on both lines, they worked
    normally, and the write guard turned out to be registered for Write|Edit|NotebookEdit and
    not Bash — so every heredoc, `sed -i`, `tee` and redirection went through. Announcing
    enforcement you do not perform is worse than announcing nothing, because it is the sentence
    that stops somebody checking.

    Returns (label, text) pairs. `ENFORCED` means a showrunner guard refuses it; `PUBLISHED`
    means showrunner states it and something of yours has to act on it.
    """
    d = role_def or {}
    out = []
    may = d.get("may_create") or []
    out.append(("ENFORCED", "may dispatch: %s"
                % (", ".join(may) if may else
                   "NOTHING — `spawn --launch` and `dispatch guard` both refuse from this seat")))
    w = d.get("writes")
    # This printed `writes: {'deny': ['**']}` — a Python repr in the block whose entire job is to
    # be the readable, generated statement of what a guard enforces. Rendered per shape rather
    # than assumed: the suite's fixture is a LIST of allowed globs, and a consumer may reasonably
    # hand a mapping. Anything else is printed as-is rather than crashed on, because a renderer
    # that raises takes the whole announcement down and silence is the one unacceptable outcome.
    if isinstance(w, (list, tuple)) and w:
        out.append(("PUBLISHED", "may write: %s" % ", ".join(str(x) for x in w)))
    elif isinstance(w, dict):
        if w.get("deny"):
            out.append(("PUBLISHED", "may NOT write: %s"
                        % ", ".join(str(x) for x in w["deny"])))
        if w.get("allow") and not w.get("deny"):
            out.append(("PUBLISHED", "may write: %s"
                        % ", ".join(str(x) for x in w["allow"])))
    elif w:
        out.append(("PUBLISHED", "writes: %s" % w))
    if d.get("reports_to"):
        out.append(("ENFORCED", "reports to: %s" % d["reports_to"]))
    if d.get("acquire"):
        out.append(("ENFORCED", "acquired by: %s" % d["acquire"]))
    return out


# A MANIFEST, NOT A POINTER (#36). Telling a session where to read is the same bet that just
# lost: the reported run had showrunner installed, wired, and a campaign with 38 leaves done, and
# the orchestrator still hand-rolled `git worktree add` 42 times. The load-bearing lines are
# carried HERE, at every session boundary, because that is the only place they are certain to be
# read. It is paid for at every start and every compaction, which is the price of it working.
def verb_inventory():
    """Every top-level verb, DERIVED FROM THE PARSER that defines them.

    A compacted agent that cannot remember what the tool does reaches for what it already
    knows — `git worktree add`, a raw `claude -p`, a hand-rolled todo list — and the tool it
    was told to use in hour one is unused by hour six. Reported exactly that way: an agent
    several compactions deep "wasn't even using it". Naming three verbs, as the seat manifest
    does, answers "how do I dispatch" and not "what else is there".

    NEVER A HAND-WRITTEN LIST. A second enumeration of the verbs is a list that goes stale the
    first time somebody adds one, and a stale inventory is worse than none: it teaches that the
    missing verb does not exist. argparse already holds the only authoritative copy.

    Returns [] rather than raising. whoami's one forbidden outcome is silence, so a failure
    here must cost the inventory line and nothing else.
    """
    try:
        import argparse as _argparse

        from .cli import build_parser
        for action in build_parser()._actions:
            if isinstance(action, _argparse._SubParsersAction):
                return sorted(action.choices)
    except Exception:                                           # noqa: BLE001
        pass
    return []


def campaign_state(cfg):
    """WHAT THE CAMPAIGN IS DOING, counted from the graph rather than named.

    "Which campaign am I on" is not answered by a name. A name is a label an agent can hold
    and still not know whether there is work, whether it is blocked, or whether it is finished
    — and the reported failure was an agent that had lost the campaign entirely across
    compactions. The counts ARE the identity: 12 ready and 3 running is a different situation
    from 0 ready and 41 closed, and an agent that knows which one it is in cannot mistake a
    finished campaign for an idle one.

    Derived every call. A cached summary is a second copy of the graph, free to disagree with
    it, and disagreement here reads as authority because it arrives in the same breath as the
    seat.

    Returns None when the graph cannot be read, and the caller SAYS so — "cannot tell" and
    "nothing to do" are the two answers this repo keeps collapsing.
    """
    try:
        from . import graph as _graph

        g = _graph.open_graph(cfg)
        rows = g.list()
        ready = len(g.ready())
    except Exception:                                           # noqa: BLE001
        return None
    counts = {}
    for row in rows:
        key = (row.get("status") or "?")
        counts[key] = counts.get(key, 0) + 1
    return {"total": len(rows), "ready": ready, "by_status": counts}


_SEAT_MANIFEST = {
    ORCHESTRATOR: [
        "You dispatch through the tool, never around it:",
        "    {sr} spawn <leaf> --actor <name> --launch",
        "A raw `claude -p` gets no worktree, no lease, no claim a reaper can reclaim, no",
        "leaf-scoped stop gate and no room. That path was taken 42 times in one real run and",
        "every guarantee was absent for all 42.",
        "`{sr} ready` is the only discovery surface; `{sr} plan` says what may run together.",
    ],
    CRAWLER: [
        "You work ONE leaf, in THIS tree, and close through the gate:",
        "    {sr} close <leaf> --proof <path> --premise holds|partial|refuted|unverifiable",
        "Verify the premise against the real source FIRST — a refuted premise is a successful",
        "outcome, not a failure. Your report is read by an orchestrator that cannot cheaply",
        "check it, so a confident wrong sentence costs more than a wrong commit.",
    ],
    SOLO: [
        "No campaign has been started in this checkout. Nothing here is orchestrating anything,",
        "and that is stated rather than left to look like an idle orchestrator.",
        "    {sr} add <title>     then     {sr} spawn <leaf> --launch",
    ],
    UNKNOWN: [
        "THE SEAT COULD NOT BE DERIVED, so nothing below is known to apply to you. This is not",
        "a quiet pass: an announcer that cannot tell and says nothing is indistinguishable from",
        "a healthy one, which is how a broken one went unnoticed for a whole run.",
        "    {sr} doctor",
    ],
}


def reseat_after_reload(cfg, session):
    """A seat whose SESSION still matches but whose PID is gone: rebind it. Returns a note or "".

    THE PROBLEM, reported by an operator. Reloading a VS Code window loses the seat every time.
    Nothing is broken — a seat's liveness is a pid discovered by walking the ancestry, the
    reload restarts the extension host under a NEW pid, the old one is dead, so `locks` correctly
    reports STALE and the resolver skips it. Correct, and useless: the same logical session comes
    back, cannot see its own seat, and has to re-claim by hand every time.

    THE DISCRIMINATOR IS THE SESSION ID, and it works because the two facts age differently —
    measured on this machine: the Claude session id is unchanged across a window reload while the
    pid is not. So "same session, dead pid" is exactly a reload, and it is safe to rebind: the
    only party that could hold that seat is the session asking for it.

    THREE ANSWERS, NOT TWO, and the third is why this cannot be a blind swap:

      old pid DEAD      rebind, and SAY a reload happened — the caller may owe setup it did
                        before, and a silent re-seat is indistinguishable from never losing it.
      old pid ALIVE     REFUSE. Two live processes carrying one session id is what
                        `claude --resume` produces, and taking the seat from one of them is
                        acting on the reading this repo never permits — that a resource whose
                        owner is demonstrably running can be reassigned. Reported, never taken.
      session EMPTY     refuse, on either side. A seat claimed without a session id records "",
                        and matching "" to "" would let any session with no id inherit any
                        unidentified seat. The absent value must not be a key.

    Best-effort and never raises: this runs inside the announcement path, whose one forbidden
    outcome is silence.
    """
    if not session:
        return ""
    try:
        for entry in roster(cfg):
            holder = entry.get("holder") or {}
            if (holder.get("session") or "") != session:
                continue
            recorded = str(holder.get("pid") or "")
            mine, basis = session_pid()
            if not mine or basis != "ancestor-claude" or recorded == str(mine):
                return ""
            if pid_alive(recorded):
                # NOT REBOUND, and the message must not imply you were denied the ROLE — you
                # were not. `resolved_role` matches on the session id, so both processes
                # resolve to this seat; what does not happen is the recorded pid moving. The
                # first wording said "the seat was NOT taken" and the very next line announced
                # the role, which is two true statements arranged to read as a contradiction.
                return ("NOTE: seat %s records pid %s, which is STILL ALIVE, and you are a "
                        "different process under the same session id — which is what "
                        "`claude --resume` produces. You BOTH resolve to this role, because the "
                        "session is the unit of identity here. The recorded pid was left alone: "
                        "a seat whose holder is demonstrably running is not reassignable, and a "
                        "reload is the case this rebinds for. If two live processes acting as "
                        "%s is not what you want, release it from one of them."
                        % (entry.get("role"), recorded, holder.get("role") or entry.get("role")))
            role = holder.get("role") or entry.get("role")
            ok, _ = claim(cfg, role, session, who=holder.get("who"),
                          seat=int(holder.get("seat") or 0))
            if ok:
                return ("RE-SEATED after a reload: %s was held by pid %s, which is gone, and "
                        "this session's id is the same — so the seat came back to you rather "
                        "than needing a re-claim. If you did setup when you first took it, do "
                        "it again: the process changed even though the session did not."
                        % (role, recorded))
            return ("seat %s could not be re-taken after a reload (its pid %s is gone). "
                    "Claim it by hand." % (role, recorded))
    except Exception:                                           # noqa: BLE001
        return ""
    return ""


def resolution(cfg, session=None):
    """Everything a guard needs in order to enforce, as DATA. Returns a dict.

    THE ANNOUNCEMENT AND THE POLICY COME FROM HERE, both of them. `whoami` renders this and
    `whoami --porcelain` prints it, so the prose cannot drift from the answer a guard acts on.

    That is not hypothetical tidiness. `whoami` emitted prose and nothing else, so a hook author
    who needed the resolved role had no way to ask for it and reimplemented the resolver. A
    consumer's write guard carried such a copy; when a seat learned to resolve through
    `seat_roles` the copy did not, so the announcement said one role while the guard enforced the
    deny-everything fallback, and a session was correctly unable to edit the file it had been
    dispatched to edit. Two statements of one policy, free to disagree — which is the failure
    `enforced_lines` exists to prevent INSIDE the announcement, arriving from outside it.

    `enforced` is the field to branch on, and it is false whenever showrunner is enforcing
    nothing — no definitions, or definitions it could not read. A guard must not read a null
    `role` as "no restriction"; that is the fail-open reading, and `problems` says which it is.
    """
    where, evidence = seat(cfg)
    defs, problems = spec(cfg)
    role, how = (None, None)
    if defs and not problems:
        role, how = _resolved(cfg, session, defs)
    d = (defs.get(role) or {}) if role else {}
    return {
        "version": PORCELAIN_VERSION,
        "seat": where,
        "evidence": evidence,
        "campaign": cfg.campaign,
        "role": role,
        "how": how,
        "enforced": bool(role),
        "policy": {"writes": d.get("writes"), "may_create": d.get("may_create") or [],
                   "reports_to": d.get("reports_to"), "acquire": d.get("acquire")},
        "notes": d.get("notes"),
        "problems": list(problems),
        "ignored_seat_mappings": seat_roles(cfg)[1],
    }


def whoami(cfg, session=None):
    """What this session is, what it may do, and what it may not. Returns a list of lines.

    Printed at SessionStart AND PostCompact, because a compaction is where the last one was lost:
    every compaction refreshed the harness that owned those seams and eroded the tool that did
    not. A rule that survives only until the next compaction is a rule for the first hour.
    """
    from .brief import sr_bin

    sr = sr_bin(cfg)
    # BEFORE RESOLVING, because a reload's whole symptom is that resolution answers `fallback`
    # while the seat is sitting there with a dead pid. Doing it here means the seat is back
    # before the announcement describes it, so the session is not told it is unassigned and then
    # separately told it was re-seated.
    #
    # THE ANNOUNCEMENT IS THE RIGHT CHANNEL and not a convenience: SessionStart and PostCompact
    # are the two moments a reloaded session is guaranteed to read, which is the same argument
    # that put `whoami` on both seams. A re-seat that only `doctor` mentioned would be a notice
    # in a channel nobody reads on the turn it matters — the #71 shape.
    reseat = reseat_after_reload(cfg, session)
    r = resolution(cfg, session)
    where = r["seat"]

    out = ["showrunner: you are the %s here." % where.upper(), "  %s." % r["evidence"]]
    if reseat:
        out.append("  %s" % reseat)
    if r["campaign"]:
        out.append("  campaign: %s" % r["campaign"])

    # WHAT THE CAMPAIGN IS DOING, before the seat manifest. The reported failure is an agent
    # several compactions deep that had lost which campaign it was on and stopped using the
    # tool at all. The seat lines answer "how do I dispatch"; they never answered "is there
    # anything to dispatch", and an agent that cannot tell a finished campaign from an idle one
    # has no reason to consult the tool again.
    state = campaign_state(cfg)
    if state is None:
        out.append("  campaign work: THE GRAPH COULD NOT BE READ, so this cannot tell you "
                   "whether there is work — which is not the same as there being none.")
    elif state["total"]:
        parts = ", ".join("%d %s" % (n, k)
                          for k, n in sorted(state["by_status"].items(), key=lambda kv: -kv[1]))
        out.append("  campaign work: %d leaf/leaves — %s; %d READY to dispatch now."
                   % (state["total"], parts, state["ready"]))
        if not state["ready"]:
            out.append("  Nothing is ready: either the graph is finished or every ready leaf is "
                       "claimed. `%s ready` and `%s status` say which." % (sr, sr))

    for line in _SEAT_MANIFEST.get(where, []):
        out.append("  " + line.replace("{sr}", sr))

    # THE REST OF THE TOOL, so a compacted agent reaches for it instead of for what it already
    # knows. Derived from the parser; see verb_inventory.
    verbs = verb_inventory()
    if verbs:
        line, wrapped = "", []
        for verb in verbs:
            if len(line) + len(verb) + 1 > 84:
                wrapped.append(line)
                line = ""
            line += (" " if line else "") + verb
        wrapped.append(line)
        out.append("  all %d verbs (`%s <verb> --help` for any of them):" % (len(verbs), sr))
        for chunk in wrapped:
            out.append("    " + chunk)

    if r["problems"]:
        out.append("  ROLE DEFINITIONS UNREADABLE: %s" % r["problems"][0])
        out.append("  Nothing below is enforced, and that is said rather than left blank.")
    elif r["enforced"]:
        out.append("  role: %s (%s)" % (r["role"], r["how"]))
        # A MAPPING THAT WAS DROPPED is announced next to the role it did not produce. Silently
        # falling back is how a session ends up told it is `unassigned` while a file it cannot
        # see says otherwise, with nothing connecting the two.
        for msg in r["ignored_seat_mappings"]:
            out.append("  SEAT MAPPING IGNORED: %s" % msg)
        for label, line in enforced_lines(r["policy"]):
            out.append("    %-9s %s" % (label, line))
        if any(label == "PUBLISHED" for label, _ in enforced_lines(r["policy"])):
            # SAID EVERY TIME, beside the line it qualifies. showrunner has no write guard: it
            # publishes `writes` and a hook of YOURS enforces it. The reporter's was registered
            # for Write|Edit|NotebookEdit and not Bash, so every heredoc, `sed -i`, `tee` and
            # `>` redirection went through while this block said ENFORCED.
            out.append("    PUBLISHED means showrunner STATES it and does not enforce it — a "
                       "hook of yours must,")
            out.append("    and it must cover Bash or a heredoc walks straight past it. "
                       "`showrunner doctor` checks.")
        if r["notes"]:
            # `%s` on a list prints a Python repr on one line, so a multi-line note arrives as an
            # unreadable wall — and an announcement nobody can read is one that did not happen.
            # A string stays one line; a list becomes lines.
            out.append("    note")
            for line in ([r["notes"]] if isinstance(r["notes"], str) else r["notes"]):
                out.append("      " + str(line))
            out.append("    ...which is prose your project wrote. Nothing checks it.")
    else:
        out.append("  no roles are defined, so no dispatch policy is enforced for any seat.")
    return out


def _resolved(cfg, session, defs):
    """(role, how) — a held claim, else a seat the campaign record vouches for, else the fallback.

    `assign` never had a writer (#40), so every Crawler resolved to the fallback and ran under the
    fallback's policy INSIDE the worktree spawn had just made for it. With a deny-everything
    fallback that is not a safe default, it is a broken tool: an audit leaf finished only by
    routing its evidence around the guard with shell redirection, and a leaf that had to edit code
    would have been stopped outright. A guard whose reward for holding is a workaround teaches
    every later session to route around it.

    The campaign record IS the assignment. `spawn` writes the tree's leaf into it before the
    session exists, keyed to its worktree — which is what `assign` was specified to mean. So the
    seat is not a second source of truth being invented here; it is the one showrunner already
    kept, finally read.

    Deliberately NOT symmetric: a seat resolves only through a mapping the user wrote, and
    mapping `orchestrator` is left to them precisely because standing in the main checkout is a
    location, not a record. Authority by location is what put a `lead` in every session that
    happened to be in the right directory.
    """
    for entry in roster(cfg):
        holder = entry.get("holder") or {}
        if entry.get("state") == locks.HELD and holder.get("session") == session:
            return holder.get("role") or entry["role"], "claimed"

    mapped, _problems = seat_roles(cfg)
    if mapped:
        where, _why = seat(cfg)
        role = mapped.get(where)
        # A SEAT SOMEBODY ELSE IS SITTING IN IS NOT YOURS TO BE MAPPED INTO (#64). The mapping
        # answers "what does a session in this position do"; it cannot answer "is that position
        # already occupied", and a role at capacity is occupied. Without this, a second session
        # standing in the same main checkout was told it was the campaign-lead of a campaign
        # another live process was leading, and told what it might dispatch there.
        #
        # That is the failure the docstring above already names — authority by LOCATION, which
        # put a lead in every session that happened to be in the right directory. The mapping
        # was meant to be the user's deliberate answer to it and instead reintroduced it,
        # because nothing checked the holder.
        #
        # Capacity is respected rather than assumed to be 1: a role with room left can still be
        # mapped into, since there is a seat for this session to take. Only a FULL role is
        # refused, and the refusal names who holds it — "held by pid N, not you" is both true
        # and more useful than a bare fallback.
        held_elsewhere = [e for e in roster(cfg)
                          if e.get("state") == locks.HELD
                          and (e.get("role") or "").rsplit("#", 1)[0] == slug(role or "", 40)
                          and (e.get("holder") or {}).get("session") != session]
        cap = int(((defs.get(role) or {}).get("capacity") or 1)) if role in defs else 1
        if role in defs and len(held_elsewhere) >= cap:
            h = (held_elsewhere[0].get("holder") or {})
            return FALLBACK, ("%s maps to this seat, but every %s seat is HELD by somebody else "
                              "(pid %s, session %s) — so this session is the fallback, not the "
                              "lead. `showrunner role roster` names the holders."
                              % (where, role, h.get("pid") or "?",
                                 short_session(h.get("session")) or "?"))
        if role in defs:
            if where != CRAWLER:
                return role, "mapped from the %s seat" % where
            leaf = crawler_leaf(cfg)
            if leaf:
                return role, ("assigned by the campaign record, which names this worktree's "
                              "leaf %s" % leaf)
    return FALLBACK, "fallback — nothing assigned or claimed this session"
