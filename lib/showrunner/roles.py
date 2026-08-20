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
from .util import session_pid, slug

USER_PATH = os.path.join(
    os.path.expanduser(os.environ.get("XDG_CONFIG_HOME") or "~/.config"),
    "showrunner", "roles.json")

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
    from .util import run
    from . import campaign as _campaign

    rc, top, _ = run(["git", "rev-parse", "--show-toplevel"], cwd=os.getcwd())
    if rc != 0 or not (top or "").strip():
        return None
    here = os.path.basename(os.path.realpath(top.strip()))
    try:
        for c in (_campaign.load(cfg).get("crawlers") or []):
            if c.get("crawler") == here:
                return c.get("leaf")
    except Exception:                                           # noqa: BLE001
        return None
    return None


def seat(cfg):
    """Where this session STANDS. Returns (seat, evidence). Never raises.

    Derived from two facts showrunner already has: whether the cwd is a linked worktree, and
    whether this repo carries a campaign. UNKNOWN is a real answer and is announced as one — an
    announcer that cannot tell and says nothing is indistinguishable from a healthy one, which is
    exactly how the reported failure went unnoticed for a whole run.
    """
    from .util import run
    from . import campaign as _campaign

    rc, common, _ = run(["git", "rev-parse", "--git-common-dir"], cwd=os.getcwd())
    rc2, top, _ = run(["git", "rev-parse", "--show-toplevel"], cwd=os.getcwd())
    if rc != 0 or rc2 != 0:
        return UNKNOWN, "not inside a git repository, so neither seat can be derived"
    common, top = (common or "").strip(), (top or "").strip()
    if not common or not top:
        return UNKNOWN, "git answered neither --git-common-dir nor --show-toplevel"
    main = os.path.realpath(os.path.dirname(
        common if os.path.isabs(common) else os.path.join(os.getcwd(), common)))

    if os.path.realpath(top) != main:
        leaf = crawler_leaf(cfg)
        return CRAWLER, ("standing in a linked worktree (%s)%s"
                         % (os.path.basename(top),
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
    """
    d = role_def or {}
    out = []
    may = d.get("may_create") or []
    out.append("may dispatch: %s" % (", ".join(may) if may else
                                     "NOTHING — `dispatch guard` refuses a raw `claude -p` from "
                                     "this seat"))
    if d.get("writes"):
        out.append("writes: %s" % d["writes"])
    if d.get("reports_to"):
        out.append("reports to: %s" % d["reports_to"])
    if d.get("acquire"):
        out.append("acquired by: %s" % d["acquire"])
    return out


# A MANIFEST, NOT A POINTER (#36). Telling a session where to read is the same bet that just
# lost: the reported run had showrunner installed, wired, and a campaign with 38 leaves done, and
# the orchestrator still hand-rolled `git worktree add` 42 times. The load-bearing lines are
# carried HERE, at every session boundary, because that is the only place they are certain to be
# read. It is paid for at every start and every compaction, which is the price of it working.
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
    r = resolution(cfg, session)
    where = r["seat"]

    out = ["showrunner: you are the %s here." % where.upper(), "  %s." % r["evidence"]]
    if r["campaign"]:
        out.append("  campaign: %s" % r["campaign"])

    for line in _SEAT_MANIFEST.get(where, []):
        out.append("  " + line.replace("{sr}", sr))

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
        for line in enforced_lines(r["policy"]):
            out.append("    ENFORCED  " + line)
        if r["notes"]:
            out.append("    note      %s" % r["notes"])
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
        if role in defs:
            if where != CRAWLER:
                return role, "mapped from the %s seat" % where
            leaf = crawler_leaf(cfg)
            if leaf:
                return role, ("assigned by the campaign record, which names this worktree's "
                              "leaf %s" % leaf)
    return FALLBACK, "fallback — nothing assigned or claimed this session"
