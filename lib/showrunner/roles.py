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

    ~/.config/showrunner/roles.json     authoritative
    .showrunner/config.json  "roles"    a project may ADD a role, never redefine one

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
from .util import slug

USER_PATH = os.path.join(
    os.path.expanduser(os.environ.get("XDG_CONFIG_HOME") or "~/.config"),
    "showrunner", "roles.json")

FALLBACK = "unassigned"
ACQUIRE = ("claim", "assign")

# The shape of a role, and the whole vocabulary showrunner has for one.
FIELDS = ("acquire", "capacity", "reports_to", "may_create", "writes", "notes")


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


def claim(cfg, role, session, pid, who=None, seat=0):
    """Take an open seat. Returns (ok, holder). Exclusivity and liveness come from locks.Lock."""
    name = "%s#%d" % (slug(role, 40), seat)
    lock = locks.Lock(_claims_root(cfg), name)
    ok = lock.acquire(pid, who or session or "?", session=session,
                      extra={"role": role, "seat": str(seat)})
    return ok, lock.holder()
