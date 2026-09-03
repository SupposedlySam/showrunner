"""showrunner CLI.

Every subcommand that can refuse does so with exit code 2 — the Claude Code PreToolUse
"deny the tool call" code — so `lock guard` and `stop-gate` work unchanged as hooks.
"""

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys

from . import (__version__, brief, campaign, collide, config, dispatch, events, gates,
               reach, graph as G, harness, lanes, lease, locks, pin, roles, worktree)
from .util import (RESOLVED_BASIS, Refused, caller_session, die, eprint, git, now,
                   package_root,
                   session_pid as util_session_pid,
                   rel, resolve_from_caller, run, short_session, slug, stamp)

BOLD, DIM, RED, GRN, YEL, OFF = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    BOLD = DIM = RED = GRN = YEL = OFF = ""


def _cfg(args, required=True, guard=False):
    """`guard=True` lets the resolver fall back to the checkout this code runs from (#74).

    ONLY the guards. A guard is asked about a tool call happening right now and has to answer;
    every other verb is asked a question it may refuse, and refusing beats answering about
    whichever repo the binary happens to live in. Making that anchor global made `ready` from a
    scratch directory quietly report on showrunner's own checkout, which the suite catches.
    """
    return config.load(required=required,
                       fallback=package_root() if guard else None)


def _graph(cfg):
    return G.open_graph(cfg)


# ------------------------------------------------------------------- init
EXAMPLE = {
    "project_name": None,
    "graph": {"backend": "auto", "db": ".showrunner/graph.db", "br_db": None},
    "lock_root": None,
    "resources": [
        {"name": "device", "match": ["\\bdeploy\\b", "\\bflutter(-tizen)? run\\b"],
         "note": "the physical device / deploy target — one consumer at a time"}
    ],
    "lanes": [
        {"name": "device-work", "lane": "serialized", "resource": "device",
         "match": {"labels": ["device", "deploy"], "title": "deploy|device|on-device"}},
        {"name": "pure-logic", "lane": "headless",
         "match": {"labels": ["backend", "test", "docs", "analysis"]}}
    ],
    "default_lane": "serialized",
    "worktree_root": ".worktrees",
    "scratch_root": ".showrunner/scratch",
    "inject": [
        {"path": ".env", "mode": "symlink", "optional": True}
    ],
    "checks": [
        {"name": "tests", "cmd": "echo 'configure me: your test command'"}
    ],
    "collision": {
        "extra_globs": [],
        "always_serialize": ["test/**", "tests/**"]
    },
    "shared_state": [],
    "harness": {
        "provision": "auto",
        "require": True,
        "installer": None,
        "_note": "The per-agent harness is auto-detected. showrunner ASKS it which files are "
                 "rules and whether a worktree matches its parent — it keeps no list of its "
                 "own. Track the harness dir in git (simplest: it then crosses into every "
                 "worktree by itself), or set \"installer\" to the path of its install script "
                 "so each worktree is provisioned with the PARENT's rules. Never let a worktree "
                 "get freshly-seeded rules: an installer seeds user-owned files only when "
                 "absent, so a blank verify.yaml means a commit gate that owes nothing and "
                 "reports success."
    }
}


def cmd_init(args):
    cfg = config.load(required=False)
    if os.path.exists(cfg.path) and not args.force:
        die("%s already exists (use --force to overwrite)" % rel(cfg.path, cfg.root), code=2)
    data = dict(EXAMPLE)
    data["project_name"] = os.path.basename(cfg.root)
    cfg.data = data
    path = config.write(cfg)
    os.makedirs(cfg.state_dir, exist_ok=True)
    gi = os.path.join(cfg.state_dir, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as fh:
            # THE LIST IS NOT WRITTEN HERE ANY MORE. It was, and it drifted twice: bin/ and
            # lib/ ARE THE TOOL, not this project (CI-01 measured a fresh install staging 31
            # paths on the consumer's next `git add -A`), and that was fixed in install.sh
            # while this copy went on writing the old list. Then config.local.json arrived in
            # install.sh and never here, so an `init`-created repo left the machine-specific
            # overlay neither tracked nor ignored — reopening the leak the overlay exists to
            # prevent. Adding the missing entries to a second hand list is the bug, not the
            # fix, so the policy now has exactly one home: config.STATE_IGNORE_SECTIONS, which
            # a test compares install.sh against in both directions.
            fh.write(config.state_ignore_text())
    worktree.ensure_root(cfg)
    # PLACE THE BINARY EVERY BRIEF WILL NAME. install.sh copies it and then calls init, so this
    # was only ever missing for someone who ran init directly — and what they got was a repo
    # that reported ready while every brief it produced named a path that did not exist. The
    # remedy `showrunner init` has to leave the person unstuck, not merely exit 0.
    # AND THE LIBRARY BESIDE IT. The binary is a launcher that resolves `lib/showrunner` next to
    # itself and refuses without it, so copying it alone produced a file that existed, was
    # executable, and died on every invocation — the second time this exact shape has shipped
    # here. `install.sh` always copied both; `init` copied one and reported success, which is
    # how "placed the path every brief names" became true and useless in the same sentence.
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src = os.path.join(here, "bin", "showrunner")
    src_lib = os.path.join(here, "lib", "showrunner")
    dst = os.path.join(cfg.state_dir, "bin", "showrunner")
    dst_lib = os.path.join(cfg.state_dir, "lib", "showrunner")
    if os.access(src, os.R_OK) and os.path.isdir(src_lib) and not os.access(dst, os.X_OK) \
            and os.path.realpath(src) != os.path.realpath(dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.makedirs(os.path.dirname(dst_lib), exist_ok=True)
        if os.path.isdir(dst_lib):
            shutil.rmtree(dst_lib)
        shutil.copytree(src_lib, dst_lib)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        print("placed %s and its library (the path every Crawler brief names)"
              % rel(dst, cfg.root))
    # AND THE GUARD'S SHIM, for the same reason and with the same history. `doctor` reports a
    # missing shim as an error, so an `init` that placed everything except it would hand the
    # person a repo that reports a fault they did not cause and cannot fix by reading the
    # message. install.sh copies it too; both real entry points place it or neither should.
    shim_src = os.path.join(here, lease.GUARD_SHIM)
    shim_dst = os.path.join(cfg.root, lease.GUARD_SHIM)
    if os.access(shim_src, os.R_OK) and not os.path.exists(shim_dst) \
            and os.path.realpath(shim_src) != os.path.realpath(shim_dst):
        os.makedirs(os.path.dirname(shim_dst), exist_ok=True)
        shutil.copy2(shim_src, shim_dst)
        os.chmod(shim_dst, 0o755)
        print("placed %s — COMMIT IT: `git worktree add` copies tracked files only, so an "
              "uncommitted shim is absent in every worktree" % lease.GUARD_SHIM)

    # --local HERE TOO, because `init` runs FIRST. Registering `--local` afterwards still left
    # one showrunner hook in the tracked settings.json — measured, in a reproduction of the
    # arrangement a consumer reported — so the verb that cleans up cannot undo the verb that
    # ran before it. A repo keeping showrunner out of its history has to be able to say so at
    # the point it is initialised.
    changed, note = lease.register_guard(cfg, bool(getattr(args, "local", False)))
    if note:
        print(note if changed else "  ⚠ %s" % note)

    print("wrote %s" % rel(path, cfg.root))
    print("wrote %s" % rel(gi, cfg.root))
    print("created %s (gitignored — Crawler worktrees live INSIDE the repo, see issue #4)"
          % rel(cfg.worktree_root, cfg.root))
    print("\nNext: edit the config (resources, lanes, checks, inject), then `showrunner doctor`.")
    return 0


def _flatten_leaves(data, prefix=""):
    """Dotted-path -> value for every LEAF in `data`. A non-empty dict recurses; anything
    else — a scalar, a list, or an empty dict — is a leaf, because an empty value is still a
    value (see config.deep_merge). Flattening at leaf granularity, not top-level-key
    granularity, is what keeps a dict like `dispatch` — set in two layers with disjoint
    sub-keys — from being misreported as shadowed when it is actually a merge."""
    out = {}
    for k, v in (data or {}).items():
        path = "%s.%s" % (prefix, k) if prefix else k
        if isinstance(v, dict) and v:
            out.update(_flatten_leaves(v, path))
        else:
            out[path] = v
    return out


def _config_layer_shadows(cfg):
    """Per-leaf-key provenance across the user/project/local layers.

    Returns (key, shadowed_layer, shadowed_path, winning_layer, winning_path) for every leaf
    whose value is set in more than one of those three FILES — DEFAULTS is the tool's own
    answer, not a file, and is not reported here. This re-reads the layer files directly
    rather than threading provenance through `Config`/`deep_merge`: that channel would have to
    be carried by every consumer of a config object for the benefit of this one report.
    """
    layers = []
    if cfg.user_path:
        layers.append(("user", cfg.user_path, config.read_config_file(cfg.user_path) or {}))
    if os.path.exists(cfg.path):
        layers.append(("project", cfg.path, config.read_config_file(cfg.path) or {}))
    local_path = os.path.join(cfg.root, config.STATE_DIR, config.CONFIG_LOCAL_NAME)
    if os.path.exists(local_path):
        layers.append(("local", local_path, config.read_config_file(local_path) or {}))

    flattened = [(name, path, _flatten_leaves(data)) for name, path, data in layers]
    keys = set()
    for _, _, flat in flattened:
        keys.update(flat)

    shadows = []
    for key in sorted(keys):
        setters = [(name, path) for name, path, flat in flattened if key in flat]
        if len(setters) < 2:
            continue
        winning_name, winning_path = setters[-1]
        for shadowed_name, shadowed_path in setters[:-1]:
            shadows.append((key, shadowed_name, shadowed_path, winning_name, winning_path))
    return shadows


# ----------------------------------------------------------------- doctor
def cmd_doctor(args):
    cfg = _cfg(args, required=False)
    # THE CODE AND THE PROJECT ARE TWO DIFFERENT THINGS, and under a central install they are
    # in two different places. Printing only the repo invited the reading that the tool came
    # from it, which is exactly wrong for every centrally-wired consumer.
    print("%s%s%s" % (BOLD, pin.describe(), OFF))
    print("repo: %s" % cfg.root)
    print("config: %s%s" % (rel(cfg.path, cfg.root),
                            "" if os.path.exists(cfg.path) else "  (MISSING — run `showrunner init`)"))
    # A FILE OUTSIDE THIS REPO IS AFFECTING THIS REPO, so say so unprompted. Every check below
    # runs against the MERGED config, and a merged dict cannot be asked where a value came
    # from; without this line a setting nobody can find in `.showrunner/` reads as the tool
    # inventing it. Printed in both directions on purpose — "none" is the state of every repo
    # that will never have one, and it is what makes the other line mean something.
    print("user config: %s" % (("%s  (merged BENEATH this repo's — the project wins)"
                                % cfg.user_path) if cfg.user_path
                               else "none (%s)" % config.USER_PATH))
    # WHICH VALUE WON, PER KEY — "user config: <path>" above says a file is in play, not
    # whether anything it set actually survived the merge. A shadowed USER-level value is the
    # quiet failure this exists for, so it is marked `warn`, not buried among `note`s.
    for key, shadowed_name, shadowed_path, winning_name, winning_path in _config_layer_shadows(cfg):
        winning_display = winning_path if winning_name == "user" else rel(winning_path, cfg.root)
        shadowed_display = shadowed_path if shadowed_name == "user" else rel(shadowed_path, cfg.root)
        if shadowed_name == "user":
            print("  %s %s: %s wins, shadowing the user-level value in %s"
                  % (YEL + "warn " + OFF, key, winning_display, shadowed_display))
        else:
            print("  %s %s: %s wins, shadowing the %s value in %s"
                  % (DIM + "note " + OFF, key, winning_display, shadowed_name, shadowed_display))
    findings = cfg.validate()
    bad = 0
    if not os.path.exists(cfg.path):
        # EXIT 2, not a decorated header line. doctor validated the DEFAULTS and reported every
        # check passing, in a repo nobody had configured — a true answer to a question the
        # person asking had not asked. The remedy was printed and the exit code said go, so
        # anything gating on this verb (a CI step, an agent reading `$?`) read an uninitialised
        # repo as a healthy one. Running pre-init is still supported and still prints the whole
        # report; what it no longer does is call that success.
        findings = [("error", "no config at %s — every check below ran against DEFAULTS, not "
                              "against anything you chose. Run `showrunner init`."
                              % rel(cfg.path, cfg.root))] + list(findings)
    for level, msg in findings:
        mark = {"error": RED + "ERROR" + OFF, "warn": YEL + "warn " + OFF, "ok": GRN + "ok   " + OFF}[level]
        print("  %s %s" % (mark, msg))
        bad += level == "error"

    backend = "?"
    try:
        g = _graph(cfg)
        backend = g.name
        print("  %s graph backend: %s (%s)" % (GRN + "ok   " + OFF, backend,
                                               cfg.graph_db if backend == "vendored" else cfg.br_db or "br"))
    except Refused as exc:
        print("  %s graph backend: %s" % (RED + "ERROR" + OFF, exc))
        bad += 1

    if backend == "br":
        # Honesty about the boundary: this adapter has not been exercised against a real
        # br install on this machine, and saying so is cheaper than a surprise mid-run.
        print("  %s the br adapter parses `br --json` output strictly and REFUSES on an "
              "unrecognised shape rather than reporting an empty graph. If br changes its "
              "output you will get a loud error, not a silent no-op." % (YEL + "note " + OFF))

    # ROUTING, READ BACK. Every lane decision has been appended to routing.jsonl since that
    # module was written, and nothing ever opened it — so "NO RULE MATCHED: an unmatched leaf is
    # a missing rule, not a neutral outcome" printed once at spawn and then accumulated where
    # nobody looks. A repo whose leaves keep defaulting has a configuration gap that is invisible
    # exactly because the default is safe: unclassified work SERIALIZES, so it is slow rather
    # than broken, and slow has no error message.
    missed, seen = lanes.unmatched(cfg)
    if seen is None:
        print("  %s no routing decisions recorded yet — nothing has been routed here, so this "
              "says nothing about whether your lane rules are right"
              % (YEL + "note " + OFF))
    elif missed:
        print("  %s %d of the last %d routing decision(s) MATCHED NO RULE and defaulted. An "
              "unmatched leaf is a missing lane rule, not a neutral outcome — the default "
              "serializes, so the cost is a campaign that is quietly slower than it should be."
              % (YEL + "warn " + OFF, missed, seen))
    else:
        print("  %s all %d recorded routing decision(s) matched a lane rule"
              % (GRN + "ok   " + OFF, seen))

    # WHERE THE HARNESS COMES FROM (#41). An installer inside somebody's working tree provisions
    # every Crawler from whatever is uncommitted there at that moment — a per-machine, per-minute
    # artifact deciding what the whole party is guarded by, and nothing else would say so.
    prov = harness.installer_provenance(cfg)
    if prov:
        level, msg = prov
        print("  %s %s" % ({"error": RED + "ERROR" + OFF, "warn": YEL + "warn " + OFF,
                            "ok": GRN + "ok   " + OFF}[level], msg))
        bad += level == "error"

    # ROLES, SHAPE ONLY (#40). showrunner never learns what a role MEANS — it checks that the
    # org can be resolved: escalation ends somewhere, nothing dangles, something is obtainable by
    # a session nobody created for a purpose. `notes` is consumer prose and says so.
    role_defs, role_problems = roles.spec(cfg)
    for msg in role_problems:
        print("  %s %s" % (YEL + "warn " + OFF, msg))
    # A SEAT MAPPED AT A ROLE NOBODY DEFINED resolves to the fallback, which is indistinguishable
    # from having written no mapping at all. One typo would otherwise buy back the whole bug this
    # map exists to fix, and buy it back silently.
    seat_map, seat_problems = roles.seat_roles(cfg)
    for msg in seat_problems:
        print("  %s %s" % (YEL + "warn " + OFF, msg))
    for where, role in sorted(seat_map.items()):
        if role_defs and role not in role_defs:
            print("  %s seat_roles maps the %s seat to %r, which no role defines — that seat "
                  "resolves to %s and nothing else would have said so"
                  % (RED + "ERROR" + OFF, where, role, roles.FALLBACK))
            bad += 1
    for level, msg in roles.validate(role_defs):
        mark = {"error": RED + "ERROR" + OFF, "warn": YEL + "warn " + OFF,
                "ok": GRN + "ok   " + OFF}[level]
        print("  %s %s" % (mark, msg))
        bad += level == "error"
    held = [r for r in roles.roster(cfg) if r["state"] == locks.HELD]
    if held:
        print("  %s %d role seat(s) held: %s" % (GRN + "ok   " + OFF, len(held),
              ", ".join("%s by %s" % (r["role"], (r["holder"].get("who") or "?")) for r in held)))

    ls = locks.LockSet(cfg)
    if ls.names():
        print("  %s resources: %s" % (GRN + "ok   " + OFF, ", ".join(ls.names())))
        for name in ls.names():
            state, h = locks.Lock(ls.root, name).state()
            if state != locks.FREE:
                print("        %s: %s (pid %s, %s)" % (name, state, h.get("pid"), h.get("who")))
    else:
        print("  %s no resources configured — nothing is serialized, so `default_lane: "
              "serialized` has no lock to take." % (YEL + "warn " + OFF))

    for line in harness.report(cfg):
        print("  %s %s" % (GRN + "ok   " + OFF, line))
    gap = worktree.harness_gap(cfg)
    if gap and not harness.spec(cfg)["dirs"]:
        print("  %s %s" % (YEL + "warn " + OFF, gap))

    # THE UPGRADE WINDOW. Between upgrading the harness and committing it, the orchestrator runs
    # scripts a Crawler cannot have: `git worktree add` copies TRACKED files, so a new worktree
    # gets HEAD's payload, not the working tree's. Spawn already refuses — correctly, and loudly
    # — but it refuses one Crawler at a time, in the middle of a fan-out, which is the worst
    # moment to discover a condition that has been true since the upgrade. It is knowable now.
    if os.path.isdir(os.path.join(cfg.root, ".game_loop")):
        rc, out, _ = run(["git", "diff", "HEAD", "--name-only", "--", ".game_loop/bin/"],
                         cwd=cfg.root)
        pending = [x for x in (out or "").split() if x]
        if rc == 0 and pending:
            print("  %s the harness payload is upgraded but NOT COMMITTED (%s). A worktree gets "
                  "HEAD's copy, so every spawn will refuse until this is committed — commit "
                  "first, then fan out." % (YEL + "warn " + OFF, ", ".join(pending[:3])))

    # IS THIS COPY BEHIND WHAT IT CAME FROM? `doctor` already warned that a copied install is
    # unattributable — a claim about PROVENANCE, which fires identically whether the copy is
    # current or twenty commits stale. A consumer hit a bug that had been fixed upstream three
    # commits earlier and spent an evening rediscovering it, because a closed issue and a live
    # one look the same from inside a vendored tree.
    _st = pin.staleness()
    if _st:
        _lvl, _msg = _st
        print("  %s %s" % ({"ok": GRN + "ok   " + OFF, "warn": YEL + "warn " + OFF}[_lvl], _msg))

    # THE LOCK ROOT'S READABILITY. `on_disk` answers [] for a root it cannot list, which is the
    # same answer as a machine holding no locks — and `reap` reads it to find what a dead
    # Crawler left behind, so an unreadable root turns every stale lock into "nothing to do".
    _lsx = locks.LockSet(cfg)
    _lsx.on_disk()
    if getattr(_lsx, "on_disk_error", None):
        print("  %s the lock root CANNOT BE LISTED (%s): %s. `reap` reads it to find locks a "
              "dead Crawler left, so every stale lock currently reads as nothing to do."
              % (RED + "ERROR" + OFF, rel(_lsx.root, cfg.root), _lsx.on_disk_error))
        bad += 1

    # THE WAITING JOURNAL'S WRITABILITY, checked here because this is the only surface a human
    # runs on purpose. `waiting` reports a failed journal write in its porcelain, which is the
    # right channel for the probe that could not act on it — and a channel with no proactive
    # consumer is a file somebody has to remember to open. This is the consumer.
    #
    # It matters because that journal is the evidence a consumer needs before adopting the
    # watchdog at all: a write that stops silently makes "the gate never fired" and "the record
    # could not be written" the same reading, and the second one argues FOR adoption.
    _wj = os.path.join(cfg.state_dir, "waiting.jsonl")
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        with open(_wj, "a"):
            pass
        # AND WHEN IT LAST ANSWERED. Registration and FIRING are two claims, and everything
        # above reports the first. The Stop trigger reaches `waiting --porcelain` on every
        # turn-end it examines, and that appends a line here — so this file's newest timestamp
        # is the only evidence in the repo that the wiring does anything, as opposed to being
        # correctly wired to something that never runs.
        _n, _last = 0, None
        try:
            with open(_wj) as _fh:
                for _ln in _fh:
                    if _ln.strip():
                        _n += 1
                        _last = _ln
        except OSError:
            _last = None
        if _last:
            try:
                _age = int(now()) - int(json.loads(_last).get("ts") or 0)
                _ago = ("%dm" % (_age // 60)) if _age < 7200 else ("%dh" % (_age // 3600))
            except (ValueError, TypeError):
                _ago = "?"
            print("  %s the waiting journal has %d entr(ies), newest %s ago (%s) — evidence the "
                  "wiring RUNS, not just that it is registered. It cannot tell a Stop trigger "
                  "firing from somebody running `waiting` by hand."
                  % (GRN + "ok   " + OFF, _n, _ago, rel(_wj, cfg.root)))
            # FRESHNESS AS A RELATION, NOT A DURATION. "Is this too old" needs a tolerance
            # somebody invents about an event showrunner does not schedule — the same defect as
            # printing a clock time for a trigger. The answerable question is whether the probe
            # has answered SINCE the thing that would have changed its answer, and the campaign
            # journal already timestamps exactly those things. With no such event, this says it
            # cannot tell rather than picking a default.
            try:
                _last_ts = int(json.loads(_last).get("ts") or 0)
            except (ValueError, TypeError):
                _last_ts = 0
            _ev_ts, _ev_kind = 0, None
            try:
                with open(events.path_for(cfg)) as _efh:
                    for _el in _efh:
                        if not _el.strip():
                            continue
                        try:
                            _e = json.loads(_el)
                        except ValueError:
                            continue
                        if int(_e.get("ts") or 0) >= _ev_ts:
                            _ev_ts, _ev_kind = int(_e.get("ts") or 0), _e.get("kind")
            except OSError:
                pass
            if not _ev_ts:
                print("       (no campaign event to compare against, so freshness is UNKNOWN "
                      "rather than assumed — nothing has happened that would change the answer)")
            elif _last_ts < _ev_ts:
                print("  %s the journal's newest entry PREDATES the last campaign event (%s). "
                      "The wiring has not answered since something changed, which is what makes "
                      "an old entry wrong — not its age."
                      % (YEL + "warn " + OFF, _ev_kind))
        else:
            print("  %s the waiting journal is writable but EMPTY (%s) — nothing has answered "
                  "yet. Registered and never fired looks exactly like registered and content, "
                  "and this is the only place the difference shows."
                  % (YEL + "warn " + OFF, rel(_wj, cfg.root)))
    except OSError as _e:
        print("  %s the waiting journal CANNOT BE WRITTEN (%s): %s. `waiting` keeps answering; "
              "what stops is the record of what it answered, and an empty journal reads as a "
              "watchdog that never fired." % (RED + "ERROR" + OFF, rel(_wj, cfg.root), _e))
        bad += 1

    # DO THE HOOKS PARSE? The one failure that blocks its own repair (reported by a consumer).
    # These are PreToolUse hooks: bash failing to PARSE one refuses Bash, Edit and Write alike,
    # so no tool can fix the file doing the refusing. Measured here the hard way — an unclosed
    # `if` in worktree-guard.sh ended with a human running `git checkout` by hand. Every
    # fail-open path in those scripts is downstream of parsing and none of them get to run.
    #
    # AND IT TRAVELS FURTHEST. These hooks run inside Crawler worktrees, so an unparseable one
    # arrives everywhere the campaign reaches.
    #
    # IN THE TOOL RATHER THAN IN A RULE FILE, which is the reporter's argument and the better
    # half of it: a check shipped as text in each consumer's verify.yaml is a check you cannot
    # fix for anybody — theirs had a quoting bug that made it unable to pass, and an upgrade did
    # not rewrite it because it was their copy. A check inside `doctor` upgrades. They also
    # could not patch their own copy: that file is project policy and their write guard refused,
    # correctly.
    _hookdir = os.path.join(cfg.root, ".showrunner", "hooks")
    _bad, _seen = [], 0
    for _name in sorted(os.listdir(_hookdir)) if os.path.isdir(_hookdir) else []:
        _hp = os.path.join(_hookdir, _name)
        if not os.path.isfile(_hp):
            continue
        try:
            with open(_hp) as _fh:
                _first = _fh.readline()
        except OSError as _exc:
            _bad.append((_name, "could not be read (%s)" % _exc))
            continue
        _seen += 1
        # THE SHEBANG DECIDES THE PARSER, not the extension: a hook without a suffix would
        # otherwise be guessed at, and guessing wrong reports a healthy file as broken.
        if _name.endswith(".py") or "python" in _first:
            try:
                with open(_hp) as _fh:
                    ast.parse(_fh.read())
            except (SyntaxError, ValueError) as _exc:
                _bad.append((_name, str(_exc)))
        else:
            _pr = subprocess.run(["bash", "-n", _hp], capture_output=True, text=True)
            if _pr.returncode != 0:
                # THE LAST LINE, AS A STRING. `splitlines()[-1:]` is a LIST, and `%s` on it
                # printed a Python repr into the one message whose job is to be actionable —
                # the same repr-not-rendering defect `enforced_lines` had, in the reporter it
                # would be read from.
                _lines = (_pr.stderr or "").strip().splitlines()
                _bad.append((_name, _lines[-1].strip() if _lines else "bash -n exited non-zero"))
    if _bad:
        # `bad` IS THE VARIABLE THIS FUNCTION RETURNS ON — `return 2 if bad else 0`. My first
        # version assigned `rc = 2` here, which is a local nothing reads: doctor has no `rc`.
        # It printed the ERROR and exited 0, so the check reported a lockout-class failure and
        # told the caller everything was fine. Caught by asserting on the exit code rather than
        # on the text, which is the only reason it did not ship that way.
        bad = True
        for _name, _why in _bad:
            print("  %s hook %s DOES NOT PARSE: %s" % (RED + "ERROR" + OFF, _name, _why))
        print("        A PreToolUse hook that cannot parse refuses Bash, Edit AND Write, so "
              "nothing can repair the file doing the refusing. Fix it from outside the session "
              "(`git checkout` it, or edit it in another editor), then `bash -n` it.")
    elif _seen:
        print("  %s all %d hook file(s) under .showrunner/hooks parse — the one failure that "
              "blocks its own repair" % (GRN + "ok   " + OFF, _seen))

    # A `writes` POLICY WITH NOTHING ON BASH TO ENFORCE IT (#77). showrunner publishes `writes`
    # and does not enforce it — no write guard ships here — so the only thing it can honestly
    # check is whether a reader exists at all. The reported install had one registered for
    # Write|Edit|NotebookEdit and NOT Bash, while `whoami` said ENFORCED, so every heredoc,
    # `sed -i`, `tee`, `cp` and `>` redirection went through unrefused for half an hour.
    #
    # THIS IS THE WORST GAP TO HAVE, because of where agents are pushed: an operating
    # instruction to "make file changes with sed, heredocs, or short scripts rather than the
    # Write tool" routes the default authoring path around a Write-only matcher. The agent
    # following its instructions is unguarded and the one ignoring them is caught.
    #
    # It CANNOT say which hook enforces `writes` — attribution would be a guess about somebody
    # else's tool. It says whether ANY PreToolUse hook sees Bash, which is checkable and is the
    # question that was answered wrongly.
    try:
        _pol = roles.resolution(cfg, os.environ.get("SHOWRUNNER_SESSION") or "")
        _declares_writes = bool((_pol.get("policy") or {}).get("writes"))
    except Exception:                                            # noqa: BLE001
        _declares_writes = False
    if _declares_writes:
        _bash_seen, _read_any = False, False
        for _sf in lease.settings_candidates(cfg.root):
            try:
                with open(_sf) as _fh:
                    _data = json.load(_fh)
            except (OSError, ValueError):
                continue
            _read_any = True
            for _entry in (_data.get("hooks") or {}).get("PreToolUse", []) or []:
                if "Bash" in (_entry.get("matcher") or ""):
                    _bash_seen = True
        if not _read_any:
            print("  %s a role here declares `writes`, and no settings file could be read, so "
                  "whether anything enforces it is UNKNOWN — not none"
                  % (YEL + "warn " + OFF))
        elif not _bash_seen:
            print("  %s a role here declares `writes` and NO PreToolUse hook matches Bash. "
                  "showrunner publishes that policy and does not enforce it; a hook of yours "
                  "must, and one registered only for Write|Edit|NotebookEdit is walked past by "
                  "every heredoc, `sed -i`, `tee` and `>` redirection. Add Bash to its matcher."
                  % (RED + "ERROR" + OFF))
        else:
            print("  %s a role declares `writes`, and a PreToolUse hook does match Bash — "
                  "showrunner cannot tell WHICH hook enforces it, only that a reader exists"
                  % (GRN + "ok   " + OFF))

    # HOW MANY WORKTREES EXIST, because 178 is not a number anybody discovers on purpose (#75).
    # The reported checkout had 178 trees and 133 GB with ONE live Crawler, and what hurt was
    # not disk: an AV suite rescanning a duplicated monorepo at ~64% CPU, on a machine reported
    # as "running slow" while almost nothing was running. A count beside the campaign would have
    # caught it weeks earlier, which is the operator's own reading of it.
    try:
        _trees = [d for d in os.listdir(cfg.worktree_root)
                  if os.path.isdir(os.path.join(cfg.worktree_root, d))]
    except OSError:
        _trees = None
    if _trees is None:
        print("  %s the worktree root (%s) could not be listed, so how many trees exist is "
              "UNKNOWN — not none" % (YEL + "warn " + OFF, rel(cfg.worktree_root, cfg.root)))
    elif _trees:
        _bytes = campaign.tree_bytes(cfg.worktree_root)
        print("  %s %d worktree(s) under %s%s. `showrunner gc` reports which are merged AND "
              "clean, and removes them with --apply; the branch and its commits survive."
              % (GRN + "ok   " + OFF if len(_trees) < 12 else YEL + "warn " + OFF,
                 len(_trees), rel(cfg.worktree_root, cfg.root),
                 "" if _bytes is None else " (%s)" % _human(_bytes)))

    # HOW MANY CALLS WENT UNCHECKED, because until now nothing downstream ever asked. A guard
    # that fails open says so — correctly — as hook output beside a SUCCESSFUL tool result, and
    # an agent concentrating on something else skims it. The reporter said exactly that about
    # their own reading, and they were the person who had just filed the guard issue.
    #
    # That satisfies "a degraded guard must fail loud" in letter only: the guard is quiet in the
    # sense that matters, which is whether the behaviour of the thing being warned changes. A
    # louder wording would treat a delivery problem as a copywriting problem. A COUNT read by
    # somebody who has stopped to look is a different fact from a banner skimmed mid-task.
    _fo = os.path.join(cfg.state_dir, "fail-open.jsonl")
    try:
        with open(_fo) as _fh:
            _rows = [json.loads(l) for l in _fh if l.strip()]
    except OSError:
        _rows = []
    except ValueError:
        _rows = None
    if _rows is None:
        print("  %s the fail-open ledger (%s) cannot be parsed, so how many calls went "
              "unchecked is UNKNOWN — which is not the same as none."
              % (YEL + "warn " + OFF, rel(_fo, cfg.root)))
    elif _rows:
        _last = max(int(r.get("ts") or 0) for r in _rows)
        _age = int(now()) - _last
        _ago = ("%dm" % (_age // 60)) if _age < 7200 else ("%dh" % (_age // 3600))
        print("  %s %d tool call(s) were ALLOWED WITHOUT BEING CHECKED, most recently %s ago. "
              "Each printed a notice beside a successful result, which is the channel an agent "
              "mid-task skims. Read them: %s — then fix what degraded and delete the file."
              % (YEL + "warn " + OFF, len(_rows), _ago, rel(_fo, cfg.root)))
    else:
        print("  %s no guard has failed open here — the ledger is empty, which is a different "
              "answer from it being unreadable" % (GRN + "ok   " + OFF))

    # WHICH STOP HOOKS ACTUALLY RAN, compared against EACH OTHER. Registration is a fact about
    # a file, a clean parse is a fact about source, and "has fired" is a fact about the past.
    # None of them is a fact about the last turn, which is the only thing a Stop gate is for.
    #
    # Prompted next door by a report of a Stop gate unrun for eight hours behind four green
    # checks — parses, registered, can write, doctor says fired. THE REPORT WAS RETRACTED: the
    # session had been idle, zero completed turn-ends in the window, so the old stamp was what
    # a healthy gate produces. What survives is the part that was never in dispute — those four
    # checks are facts about a FILE and about the PAST, and none is a fact about this turn.
    #
    # THE RELATION IS BETWEEN THE HOOKS, not against a tolerance somebody invents. The NEWEST
    # stamp across all of them is a proxy for the last turn-end that reached anything; a hook
    # whose own newest stamp sits far behind that was registered and not reached. That question
    # is answerable without knowing when turns happen, which showrunner does not schedule.
    _hb = os.path.join(cfg.root, ".showrunner", "hook-heartbeat.jsonl")
    _stamps = {}
    try:
        with open(_hb) as _fh:
            for _l in _fh:
                try:
                    _r = json.loads(_l)
                except ValueError:
                    continue
                _k, _t = _r.get("hook"), int(_r.get("ts") or 0)
                if _k and _t > _stamps.get(_k, 0):
                    _stamps[_k] = _t
    except OSError:
        pass

    # DERIVED FROM THE REGISTRATION, never listed here — a list in this file goes stale the day
    # a hook is added, which is the failure it would be pretending to catch.
    _expected = set()
    for _sf in ("settings.json", "settings.local.json"):
        try:
            with open(os.path.join(cfg.root, ".claude", _sf)) as _fh:
                _sd = json.load(_fh)
        except (OSError, ValueError):
            continue
        for _grp in (_sd.get("hooks") or {}).get("Stop", []):
            for _h in _grp.get("hooks", []):
                _c = _h.get("command") or ""
                if ".showrunner/hooks/" in _c:
                    _expected.add(os.path.splitext(os.path.basename(_c.strip('"')))[0])

    if not _expected:
        print("  %s no Stop hook of showrunner's own is registered, so there is nothing here to "
              "have run or not run." % (GRN + "ok   " + OFF))
    else:
        _newest = max(_stamps.values()) if _stamps else 0
        for _name in sorted(_expected):
            _t = _stamps.get(_name, 0)
            if not _t:
                # CANNOT TELL, said out loud. A hook that records no stamp and a hook never
                # reached both produce no line, and this is not able to separate them.
                print("  %s Stop hook `%s` has NEVER stamped an invocation. That is either a "
                      "hook that does not record one or a hook nothing has reached — this "
                      "cannot tell which, and both look like a healthy quiet gate everywhere "
                      "else." % (YEL + "warn " + OFF, _name))
                continue
            _behind = _newest - _t
            _age = int(now()) - _t
            _ago = ("%dm" % (_age // 60)) if _age < 7200 else ("%dh" % (_age // 3600))
            if _behind >= 900:
                print("  %s Stop hook `%s` last ran %s ago, %dm BEHIND the newest Stop hook "
                      "invocation. Turn-ends have reached other hooks and not this one — a "
                      "gate that is not reached is indistinguishable from a gate with nothing "
                      "to say. This does not establish WHY; an earlier blocking hook in the "
                      "Stop array is one candidate."
                      % (YEL + "warn " + OFF, _name, _ago, _behind // 60))
            else:
                print("  %s Stop hook `%s` stamped an invocation %s ago, in step with the "
                      "others — evidence it RAN, not that it is registered."
                      % (GRN + "ok   " + OFF, _name, _ago))

    # THE WORKTREE GUARD'S WIRING. Not "does the verb work" — the suite answers that — but
    # "would it ever run". The guard fails OPEN by design, so nothing at runtime can be loud
    # about its own absence without blocking the repair it needs; this is where that loudness
    # lives instead. An unregistered guard is an ERROR because it is indistinguishable, from
    # every other angle, from a guard that ran and was content.
    for level, msg in lease.guard_health(cfg):
        mark = {"error": RED + "ERROR" + OFF, "warn": YEL + "warn " + OFF,
                "ok": GRN + "ok   " + OFF}[level]
        print("  %s %s" % (mark, msg))
        bad += level == "error"

    # THE RECIPROCAL HALF: showrunner's config names paths into a NEIGHBOUR's tree, and only
    # this repo can see whether they still resolve. A neighbour who moves or uninstalls their
    # tool cannot fail our suite — they do not know we point at them — so the failure would
    # otherwise surface one Crawler at a time, as a soft warning, mid-fan-out. Configured-and-
    # missing and never-configured are different states and used to print the same thing.
    chat_cfg = (dispatch.dispatch_config(cfg).get("chat") or {})
    if chat_cfg.get("enabled"):
        for key in ("cli", "installer"):
            path = dispatch.chat_path(cfg, key)
            if not path:
                print("  %s chat is enabled but dispatch.chat.%s is unset — Crawlers will spawn "
                      "unreachable" % (YEL + "warn " + OFF, key))
            elif not os.path.exists(path):
                print("  %s dispatch.chat.%s points at %s, which does not exist. A neighbour "
                      "moving their checkout cannot fail our suite, so this is the only place "
                      "it shows." % (RED + "ERROR" + OFF, key, path))
                bad += 1
            else:
                print("  %s chat %s resolves: %s" % (GRN + "ok   " + OFF, key, path))

    # THE LAUNCH BINARY, resolved rather than assumed. `doctor` refuses configurations that
    # degrade silently, and an unresolvable `claude` is one: EVERY `spawn --launch` fails, the
    # whole parallel lane is unavailable, and the first sign is a failed spawn that has already
    # created a worktree, a branch, a scratch dir and a CLAIM.
    #
    # Reported by a consumer whose only `claude` lives inside the editor extension — not on
    # PATH, no standalone install, no package-manager formula. Nothing here caught it.
    _cb = dispatch.claude_bin(cfg)
    _cb_resolved = _cb if os.path.sep in _cb else shutil.which(_cb)
    if _cb_resolved and os.path.sep in _cb and not os.access(_cb_resolved, os.X_OK):
        print("  %s dispatch.claude_bin points at %s, which is not executable. Every "
              "`spawn --launch` will fail AFTER creating a worktree, a branch and a claim."
              % (RED + "ERROR" + OFF, _cb))
        bad += 1
    elif not _cb_resolved:
        print("  %s dispatch.claude_bin %r does not resolve — not on PATH and not an absolute "
              "path. `spawn --launch` cannot start anything; `spawn` without it still prepares "
              "the room. Set `dispatch.claude_bin` in .showrunner/config.local.json to the "
              "absolute path (an editor-bundled binary is the common case, and its directory "
              "carries a version that changes on update)."
              % (RED + "ERROR" + OFF, _cb))
        bad += 1
    else:
        print("  %s dispatch.claude_bin resolves: %s" % (GRN + "ok   " + OFF, _cb_resolved))

    # #22 — knowable from the config with nothing running, so it is worth saying here rather
    # than one Crawler at a time in the middle of a fan-out.
    for path, dirname in harness.inject_conflicts(cfg):
        print("  %s inject %r is INSIDE the harness directory %s. Injecting it creates that "
              "directory before the harness is provisioned, after which provisioning leaves it "
              "alone and the harness cannot answer `worktree --porcelain` — so every spawn "
              "aborts blaming the harness's embedding contract for a config conflict.\n"
              "        The harness owns what crosses into a worktree: set harness.installer, or "
              "track %s in git."
              % (RED + "ERROR" + OFF, path, dirname, dirname))
        bad += 1

    # THE WATCHDOG THAT CANNOT SEE A SUBAGENT (issue #23). `showrunner waiting` was built for
    # exactly one consumer and nothing connects them, so the default state of a two-layer
    # install is: the guard exists, the answer exists, and they are not talking. The failure is
    # silent in the direction that trains people to ignore alarms — an orchestrator that has
    # correctly dispatched a full wave gets rung, then pages the human, and the operator raises
    # the idle threshold until the genuinely wedged run is invisible too.
    for dirname in harness.spec(cfg)["dirs"]:
        state, detail = harness.waiting_probe(cfg, dirname)
        if state is None:
            continue
        if state == "armed":
            print("  %s %s's idle watchdog is wired to an answer about dispatched work: %s"
                  % (GRN + "ok   " + OFF, dirname, detail))
        elif state == "failing":
            print("  %s %s's waiting probe is CONFIGURED AND FAILING (%s). 'could not answer' "
                  "rings and reports failing, so this reads as a broken watchdog rather than "
                  "as the config error it is — check the path is absolute and executable."
                  % (RED + "ERROR" + OFF, dirname, detail or "no command"))
            bad += 1
        else:
            print("  %s %s's idle watchdog has NO waiting probe, so a fanned-out orchestrator "
                  "waiting on Crawlers it cannot hurry looks identical to one that fell asleep "
                  "— it gets rung, and at the ring cap it pages a human for a healthy run.\n"
                  "        A human arms this once per install, deliberately: a verb showrunner "
                  "could call would be callable by the sessions being watched, and a probe of "
                  "`true` is the watchdog switched off by the thing it watches.\n"
                  "        Set %s to:\n            %s waiting"
                  % (YEL + "warn " + OFF, dirname, detail or "the harness's waiting-probe key",
                     brief.sr_bin(cfg)))

    # THE BINARY EVERY BRIEF NAMES. A Crawler's proof-of-done, its close, its stop-gate trigger
    # all route through one absolute path, and this repo spent a week naming one that did not
    # exist — `install.sh` places it and a development checkout never runs its own installer.
    # Absolute and canonical and dead reads exactly like absolute and correct.
    sr = brief.sr_bin(cfg)
    if os.access(sr, os.X_OK):
        print("  %s the binary every brief names resolves: %s" % (GRN + "ok   " + OFF, rel(sr, cfg.root)))
        # WHICH COPY, AND HOW OLD. A self-vendored pin is what lets this repo edit the tool its
        # own guards run — but a pin nobody refreshes guards with rules from whenever it was
        # taken, and that is invisible: the guard answers normally, it just answers an older
        # question. Same "configured but inert" class as a hook that is registered and dead.
        if os.path.realpath(sr).startswith(os.path.realpath(
                os.path.join(cfg.root, ".showrunner_self")) + os.sep):
            # THE REMEDY MUST NAME A BINARY THAT CAN ACTUALLY RE-PIN, and the resolved one
            # cannot: `self --pin` extracts from the checkout the RUNNING code lives in, and the
            # pinned copy is not a checkout — it refuses, correctly. Printing `sr` here named the
            # pin re-pinning itself, which is a remedy that exits 2. `lease.REMEDIES` says this
            # project has shipped a dead remedy twice; this would have been the fourth.
            repin = os.path.join(cfg.root, "bin", "showrunner")
            repin = rel(repin, cfg.root) if os.access(repin, os.X_OK) else (
                "<from a showrunner checkout> bin/showrunner")
            # ASKED OF THE ONE OWNER, not computed again here. This block and the session
            # announcement answer the same question, and two copies of one rule is how they drift —
            # the failure this file spends most of its comments removing.
            state = pin.self_pin_state(cfg, sr)
            if state is None:
                pass
            elif state[0] == "ok":
                print("  %s   ...%s" % (GRN + "ok   " + OFF, state[1]))
            else:
                print("  %s   ...%s" % (YEL + "warn " + OFF, state[1]))
    else:
        print("  %s %s does not exist or is not executable, and every Crawler brief tells its "
              "agent to run it. Run install.sh, or work from a checkout that carries "
              "bin/showrunner." % (RED + "ERROR" + OFF, sr))
        bad += 1

    if gates.load_baseline(cfg) is None:
        print("  %s no baseline recorded — `showrunner baseline` on a known-good tree, or "
              "integration cannot tell a new failure from a pre-existing one." % (YEL + "warn " + OFF))
    return 2 if bad else 0


# ----------------------------------------------------------------- status
def cmd_status(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    ready = g.ready()
    in_prog = [x for x in g.list(status=G.IN_PROGRESS) if not x.is_epic]
    print("%s%s%s — graph %s" % (BOLD, cfg.project_name, OFF, g.name))
    print("  ready: %d   in progress: %d   done: %d   refuted: %d"
          % (len(ready), len(in_prog), len(g.list(status=G.CLOSED)), len(g.list(status=G.REFUTED))))
    for leaf in ready[:20]:
        d = lanes.route(cfg, leaf)
        print("  %sready%s  %-16s %-9s %s" % (GRN, OFF, leaf["id"], d["lane"], leaf.get("title", "")[:70]))
    for leaf in in_prog:
        tag = "parked" if leaf.get("parked") else "claimed"
        print("  %s%s%s %-16s %-9s %s" % (YEL, tag, OFF, leaf["id"], leaf.get("actor") or "?",
                                          leaf.get("title", "")[:70]))
    ls = locks.LockSet(cfg)
    for name in ls.names():
        state, h = locks.Lock(ls.root, name).state()
        if state != locks.FREE:
            print("  lock %-10s %s by pid %s (%s)" % (name, state, h.get("pid"), h.get("who")))
    try:
        stale = g.stale_claims()
    except Refused:
        stale = []
    if stale:
        print("  %s%d stale claim(s)%s — run `showrunner reap`" % (RED, len(stale), OFF))
    # #69 — THE THIRD VERDICT, next to the other two and pointedly NOT offering reap. Every
    # signal status had was about a process, and a session parked at a prompt is indis-
    # tinguishable from one mid-computation by any of them: one Crawler produced nothing for
    # 55 minutes while `ps`, `%CPU` and `heartbeat_ts` all read healthy, and a human caught it
    # by asking why goldens were taking 74 minutes. The remedy printed here is to PROMPT the
    # session, never to reclaim it — a stalled Crawler's process is the only thing holding its
    # uncommitted work, so the reap this line does not suggest is the destructive one.
    try:
        stalled = g.stalled_claims()
    except Refused:
        stalled = []
    for leaf, why in stalled:
        print("  %sstalled%s %-16s %-9s %s" % (RED, OFF, leaf["id"],
                                               leaf.get("actor") or "?", why))
    if stalled:
        eprint("  A stalled Crawler is ALIVE, so do NOT reap it — its process may hold the "
               "only copy of uncommitted work. Prompt the session; reclaim only if its tree "
               "turns out to be safe.")
    # #29 — SURFACED WHERE SOMEBODY IS ALREADY LOOKING. The detection existed and was reachable
    # only through `reap`, a verb somebody has to decide to run — and a lingering process is
    # invisible by construction, so nothing prompts that decision. Two of them polled for four
    # hours past their own closes and exhausted a shared rate limit, taking down a turn-end gate
    # for every other agent.
    ling = campaign.lingering_crawlers(cfg)
    if ling:
        print("  %s%d crawler process(es) outlived their leaf%s — run `showrunner reap`"
              % (RED, len(ling), OFF))
        for item in ling[:5]:
            print("        %s (leaf %s, pid %s) — %s"
                  % (item["crawler"], item["leaf"], item["pid"], item["why"]))
        eprint("  A finished session does not idle: it keeps polling whatever it was told to "
               "poll, and that is a SHARED cost — the run that notices is usually not the run "
               "that pays.")
    return 0


# ------------------------------------------------------------------ graph
def cmd_add(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    body = args.body or ""
    if args.body_file:
        with open(args.body_file) as fh:
            body = fh.read()
    leaf_id = g.add(args.title, body=body, kind=args.kind, leaf_id=args.id,
                    labels=args.label or [], paths=args.path or [])
    for parent in args.after or []:
        g.dep(leaf_id, parent)
    events.emit(cfg, "leaf.added", {"leaf": leaf_id, "title": args.title, "leaf_kind": args.kind,
                "labels": args.label or [], "after": args.after or []})
    # AT ADD TIME, not only at `plan`. `plan` already refuses to be neutral about an unmatched
    # leaf, and that is the only reason a consumer caught a pure-software leaf queued against a
    # device lane. But `plan` may never run before `spawn`, and `add` is where the human still
    # has the context to fix it -- a label typo discovered at plan time is a leaf you have
    # already reasoned about twice.
    _d = lanes.route(cfg, g.show(leaf_id))
    if not _d.get("matched"):
        eprint("note: %s matches no lane rule, so it defaults to %s%s. An unmatched leaf is a "
               "missing rule, not a neutral outcome. Fix it in place with "
               "`showrunner edit %s --label <label>` — no need to close and re-create."
               % (leaf_id, _d.get("lane"),
                  (", which owns resource %r — this leaf will queue for it" % _d.get("resource"))
                  if _d.get("resource") else "", leaf_id))
    print(leaf_id)
    return 0


def cmd_dep(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    if getattr(args, "remove", False):
        # A WRONG EDGE HAD NO WAY BACK. `edit` exists because "the body IS the brief, so a wrong
        # one dispatches a wrong task, and before this verb there was no way back" — every clause
        # of that applies to an edge, which does something worse than mis-describe work: it HIDES
        # it. `ready` means unblocked, so a false parent keeps a leaf out of the discovery surface
        # entirely. The only routes left were editing the graph database by hand or closing a leaf
        # that is not done, which spends the proof-of-done gate on a decision nobody made.
        if not g.undep(args.child, args.parent):
            # NOT AN ERROR, AND NOT A SHRUG. The end state the caller asked for is the end state
            # they have. But reporting "removed" would tell them their graph changed when it did
            # not, and somebody who mistyped an id would walk away believing a leaf was freed.
            print("no such dependency: %s was not blocked by %s — nothing removed"
                  % (args.child, args.parent))
            return 0
        print("%s is no longer blocked by %s" % (args.child, args.parent))
        return 0
    g.dep(args.child, args.parent)
    print("%s is blocked by %s" % (args.child, args.parent))
    return 0


def cmd_list(args):
    cfg = _cfg(args)
    leaves = _graph(cfg).list(status=args.status)
    if args.json:
        print(json.dumps(leaves, indent=2, sort_keys=True))
        return 0
    for leaf in leaves:
        print("%-16s %-12s %-8s %s" % (leaf["id"], leaf["status"], leaf["kind"], leaf.get("title", "")))
    return 0


def cmd_show(args):
    """The leaf, plus WHAT ITS TREE WAS ACTUALLY CUT FROM if one was spawned (#73).

    `spawn` has recorded `base`/`base_sha` since #33 and nothing ever showed them back. They
    were load-bearing but invisible: `campaign.is_empty` and `lease.base_sha_of` both read the
    recorded sha, so the fact existed and no operator surface answered "where is this tree
    cut from" — the one question a Crawler on a wrong tree needs asked about it. The Crawler
    that caught the reported case ran `git merge-base --is-ancestor` by hand.

    UNDER ITS OWN KEY, not merged into the leaf. The leaf is a graph record and consumers
    round-trip it; `crawler_base` is a different fact from a different store, and flattening
    them would let a stale campaign entry read as graph state.
    """
    cfg = _cfg(args)
    leaf = _graph(cfg).show(args.id)
    entry = next((c for c in (campaign.load(cfg).get("crawlers") or [])
                  if c.get("leaf") == args.id), None)
    if entry:
        leaf = dict(leaf)
        leaf["crawler_base"] = {"asked_for": entry.get("base"),
                                "sha": entry.get("base_sha"),
                                "branch": entry.get("branch"),
                                "crawler": entry.get("crawler")}
    print(json.dumps(leaf, indent=2, sort_keys=True))
    return 0


def cmd_ready(args):
    cfg = _cfg(args)
    ready = _graph(cfg).ready()
    if args.json:
        print(json.dumps(ready, indent=2, sort_keys=True))
        return 0
    for leaf in ready:
        print("%-16s %s" % (leaf["id"], leaf.get("title", "")))
    return 0


def cmd_claim(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    if args.next:
        leaf = g.claim_next(args.actor, pid=args.pid, tree=args.tree, session=args.session,
                            prefer=[args.id] if args.id else None)
        if not leaf:
            print("no ready leaf available — either the graph is dry or siblings hold them all")
            return 1
    else:
        if not args.id:
            die("name a leaf, or use --next to take whichever one is free", code=64)
        leaf = g.claim(args.id, args.actor, pid=args.pid, tree=args.tree, session=args.session)
    events.emit(cfg, "leaf.claimed", {"leaf": leaf["id"], "actor": leaf.get("actor"),
                "claim_pid": leaf.get("claim_pid"), "tree": leaf.get("claim_tree"),
                "how": "next" if args.next else "named"})
    print("claimed %s as %s (pid %s)" % (leaf["id"], leaf.get("actor"), leaf.get("claim_pid")))
    return 0


def cmd_release(args):
    cfg = _cfg(args)
    _graph(cfg).release(args.id, args.reason or "released")
    events.emit(cfg, "leaf.released", {"leaf": args.id, "reason": args.reason or "released"})
    print("released %s back to ready" % args.id)
    return 0


def cmd_park(args):
    cfg = _cfg(args)
    _graph(cfg).park(args.id, args.reason)
    events.emit(cfg, "leaf.parked", {"leaf": args.id, "reason": args.reason})
    print("parked %s — the claim survives; a Crawler at a usage limit is not dead." % args.id)
    return 0


def cmd_unpark(args):
    cfg = _cfg(args)
    _graph(cfg).unpark(args.id)
    events.emit(cfg, "leaf.unparked", {"leaf": args.id})
    print("unparked %s" % args.id)
    return 0


def cmd_close(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    leaf, notes = gates.close_gate(
        cfg, g, args.id, args.proof, args.reason, refuted=args.refuted,
        evidence=args.evidence, stale_proof_reason=args.stale_proof_reason,
        premise=args.premise, premise_read=args.premise_read,
        unreachable=args.unreachable)
    _outcome = ("refuted" if args.refuted else
                ("unreachable" if args.unreachable else "closed"))
    events.emit(cfg, "leaf.closed", {"leaf": leaf["id"], "outcome": _outcome,
                "proof": leaf.get("proof"), "premise": args.premise, "actor": leaf.get("actor")})
    print("%s %s (%s)" % ("REFUTED" if args.refuted else "closed", leaf["id"], leaf.get("proof")))
    # Spin the Crawler down as soon as its leaf closes: mark it finished and close its room.
    # The process is left alone here on purpose — it is mid-call at this exact moment, having
    # just run this very command. `reap` takes it if it is still alive after the grace window.
    for done in campaign.finish(cfg, leaf["id"]):
        print("  %sspun down %s — %s%s" % (DIM, done["crawler"], done["channel"], OFF))
    for n in notes:
        print("  %s%s%s" % (DIM, n, OFF))
    return 0


def cmd_stop_gate(args):
    cfg = _cfg(args)
    # WHOSE turn-end is this? --leaf is exact and spawn bakes it into each Crawler's trigger.
    # Falling back to the tree: GAME_LOOP_REPO is set by the harness that invokes this trigger
    # and names the tree it is running in, which beats cwd because a trigger's working directory
    # belongs to whoever runs it. cwd last, for a hand-wired setup with no harness.
    tree = args.tree or os.environ.get("GAME_LOOP_REPO") or os.getcwd()
    ok, msg = gates.stop_gate(cfg, _graph(cfg), leaf_id=args.leaf, tree=tree)
    (print if ok else eprint)(msg)
    return 0 if ok else 2


# ------------------------------------------------------------------- lock
def cmd_lock_status(args):
    cfg = _cfg(args)
    ls = locks.LockSet(cfg)
    names = [args.resource] if args.resource else ls.names()
    if not names:
        print("no resources configured")
        return 0
    for name in names:
        state, h = ls.lock(name).state()
        if state == locks.FREE:
            print("%-14s FREE" % name)
        else:
            print("%-14s %s by pid %s (%s) since %s" % (name, state, h.get("pid"), h.get("who"), h.get("ts")))
    return 0


def _rebind_claim_to_this_session(cfg, tree, verdict):
    """Point the leaf claimed for THIS worktree at the process now working in it.

    Best-effort and silent on failure: `enter` is a SessionStart hook whose one job is to not
    break a session's startup, and a claim that stays unprovable is surfaced by `stale_claims`
    rather than released. Doing nothing here is the old behaviour, which is safe; raising is not.

    Only for a tree the CAMPAIGN RECORD names, so `git worktree add` cannot rebind somebody
    else's claim by entering a directory — the same rule `seat_roles` follows for exactly the
    same reason.
    """
    if verdict not in ("acquired", "own") or not tree:
        return
    try:
        entry = next((c for c in (campaign.load(cfg).get("crawlers") or [])
                      if os.path.realpath(cfg.abspath(c.get("worktree") or "")) ==
                      os.path.realpath(tree)), None)
        if not entry or not entry.get("leaf"):
            return
        pid, basis = util_session_pid()
        if basis != "ancestor-claude":
            return
        g = _graph(cfg)
        leaf = g.show(entry["leaf"])
        if (leaf or {}).get("status") != "in_progress":
            return
        if leaf.get("claim_pid") == pid:
            return
        g.rebind_claim(entry["leaf"], pid)
    except Exception:                                            # noqa: BLE001
        return


def cmd_worktree_enter(args):
    """SessionStart hook shape. ALWAYS exits 0 — see below.

    Exit 0 unconditionally, including on a detected hijack, and that is not timidity. A
    SessionStart hook cannot deny a session; a non-zero exit here would abort the session's
    startup over a condition this leaf explicitly does not yet enforce, which is a gate built
    before its failure was observed. The teeth are WL-05, on PreToolUse, where a refusal
    actually maps onto the event.
    """
    cfg = _cfg(args)
    session = args.session or caller_session()
    verdict, detail = lease.enter(cfg, session, path=args.path, who=args.holder)
    tree = detail.get("tree")
    h = detail.get("holder") or {}

    if verdict == "not-a-worktree":
        return 0
    # THE SESSION HAS ARRIVED, so point the leaf's claim at it. `spawn` takes the claim before
    # any session exists — it has to — so until the real process is known the claim names
    # nothing that can be proved alive. `--launch` rebinds the moment it has a pid; the path
    # that prepares a tree for you to start yourself had no such moment until this one. Entering
    # the tree IS that moment, and it is the same remedy at the same instant of knowing.
    _rebind_claim_to_this_session(cfg, tree, verdict)

    if verdict == "own":
        print("showrunner: you already hold worktree %s" % tree)
        return 0
    if verdict == "acquired":
        print("showrunner: worktree %s is yours (liveness basis: %s)"
              % (tree, h.get("pid_basis") or "?"))
    elif verdict == "reclaimed":
        prev = detail.get("previous") or {}
        print("showrunner: worktree %s was held by a session that is NOT alive (pid %s, "
              "session %s) — reclaimed.\n  Its work may still be in the tree. Nothing here "
              "deleted anything." % (tree, prev.get("pid"), (prev.get("session") or "?")[:8]))
    elif verdict == "unreadable":
        # THE REMEDIES COME FROM `lease.REMEDIES`, and this line is why that constant exists.
        # It used to hand-roll its own and print `worktree takeover`, which is a command this
        # repo has never had — in the one branch reserved for the state only a human can
        # resolve. REMEDIES says so in as many words ("NOT BUILT YET (WL-06)… a remedy naming a
        # command that does not exist is worse than no remedy, and this project has shipped
        # that twice"), and then a second copy of the rule shipped it a third time.
        print("showrunner: worktree %s holds an UNREADABLE pid (%r). It cannot be proved dead, "
              "so it will not be reclaimed — a partial write by a LIVE holder looks exactly "
              "like this.\n  Find out whether %s is still running. Meanwhile:\n"
              % (tree, (h.get("pid") or "")[:40], h.get("who") or "the holder"))
        print(lease.REMEDIES.format(sr=brief.sr_bin(cfg), tree=tree))
        return 0
    elif verdict == "no-liveness":
        print("showrunner: no session process could be resolved, so a lease here would have no "
              "liveness at all and was NOT taken (%s).\n  This tree is unprotected; that is "
              "said out loud rather than left to look like success." % h.get("why", ""))
        return 0
    elif verdict == "hijack":
        print(lease.OPTIONS.format(
            who=h.get("who") or "?", session=short_session(h.get("session")),
            pid=h.get("pid"), since=stamp(h.get("ts")),
            basis=h.get("pid_basis") or "unrecorded",
            sr=brief.sr_bin(cfg), tree=tree))

    # THE GUARD'S OWN ABSENCE, said where the reader is standing. The guard fails OPEN, so it
    # cannot announce that it is missing — it is not running. `doctor` reports it, but a
    # Crawler entering a tree never runs `doctor`, and a caveat filed where the reader does not
    # stand is a caveat they never had. One line, only when something is actually wrong.
    # EVERY non-ok finding, not only the errors. An untracked shim is a WARNING in `doctor` —
    # correctly, since the main checkout is still guarded — but from inside a WORKTREE it is
    # total absence, and this is the one reader standing in a worktree.
    inert = [m for level, m in lease.guard_health(cfg) if level != "ok"]
    if inert:
        print("\n  %sThe write guard for this tree is NOT ACTIVE:%s" % (YEL, OFF))
        for msg in inert:
            print("    - %s" % msg.split("\n")[0])
        print("    Your writes here are not checked against the lease. Nothing below denies.")

    # WHAT A WORKTREE DOES NOT ISOLATE, printed from the audit rather than restated here.
    # Restating it is how the two copies drift, and worktree.audit_shared is the one that gets
    # maintained because spawn already depends on it.
    shares = worktree.audit_shared(cfg)
    if shares and verdict in ("acquired", "reclaimed", "hijack"):
        print("\n  A lease covers this TREE. Still shared with every sibling:")
        for item in shares:
            print("    - %s" % item["what"])
    return 0


def cmd_worktree_fork(args):
    """Your own tree, from the same commit the held one started at. Exit 2 on refusal."""
    cfg = _cfg(args)
    session = args.session or caller_session()
    try:
        path, d = lease.fork(cfg, getattr(args, "from"), session, base=args.base, name=args.name)
    except Refused as exc:
        die(str(exc), code=2)
    print("forked %s -> %s" % (d["from"], d["tree"]))
    # Resolved, and described as what it IS rather than as what it usually is. This said
    # "the commit X started at, not HEAD" unconditionally, which is false the moment somebody
    # passes --base HEAD — the line then argued with itself in the same breath.
    print("  base    %s  (%s)" % (
        d["base"][:12],
        "explicit --base" if args.base else "the commit %s started at" % d["from"]))
    print("  path    %s" % rel(path, cfg.root))
    # REPORTED FROM THE ACQUIRE, not asserted. This printed "held by you" whatever happened,
    # including when the acquire had failed — the fresh tree the reader was just moved to was
    # UNGUARDED and they had been told the opposite, on the recovery path, by the remedy the
    # refusal recommends first.
    if d.get("leased"):
        print("  lease   held by you")
    else:
        # THE REASON IS READ BACK, not restated. `fork` records the failed acquire's holder in
        # `lease_holder`, whose `why` already says what went wrong — and this branch used to
        # hardcode "no session process could be resolved", which is only ONE of the reasons
        # `acquire` can fail. A field written and never read is a detector reporting to nobody,
        # and a duplicate of its content beside it is how the two start disagreeing: the day
        # acquire fails for a different reason, the record is right and the line is wrong.
        why = (d.get("lease_holder") or {}).get("why") or "the acquire returned no reason"
        print("  %slease   NOT TAKEN — this tree is UNGUARDED%s" % (YEL, OFF))
        print("          The fork itself worked and the tree is yours to use; what failed is "
              "the lease,\n          so nothing will refuse a second session that walks into "
              "it.\n          Reason: %s (session %r)." % (why, session or ""))
    for line in d.get("injected") or []:
        print("  inject  %s" % line)
    for line in d.get("warnings") or []:
        print("  note    %s" % line)
    return 0


class _LazyVersion(argparse.Action):
    """`--version`, resolved only when ASKED.

    argparse's built-in version action takes a finished string, which would mean resolving
    provenance while BUILDING the parser — on every invocation. `worktree guard` runs from a
    PreToolUse hook on every Write, Edit and Bash in the repo, so that would put a `git
    rev-parse` subprocess in front of every tool call in every session, to answer a question
    nobody asked. Lazy here is not a micro-optimisation; it is the difference between a version
    string and a tax.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        print(pin.describe())
        parser.exit()


def cmd_self(args):
    """`showrunner self` — pin the tool's own code at a git ref, for a central install.

    NOTHING CONSUMES THIS YET, and that is said plainly rather than left for a reader to infer
    from the absence of callers: `install.sh --central` and the dispatcher shim are CI-03, and
    a pin with no consumer is a populated directory, not a working central install.
    """
    # NO CONFIG IS LOADED HERE, and its absence is the fix rather than an oversight. Both halves
    # of this verb are about a DIRECTORY and a checkout of showrunner's own code; neither is
    # about the consumer project you happen to be standing in. Passing that project's config to
    # `pin` is what made `self --pin` archive from the wrong repository.
    if not args.pin:
        # THE READ SIDE, reachable on its own. A stamp that can only be written by the command
        # that writes it is one nobody checks between upgrades.
        if not args.dest:
            die("self: --dest names the pinned checkout to report on, or pass --pin <ref> "
                "--dest <path> to create one.", code=64)
        found = pin.read_pin(os.path.abspath(os.path.expanduser(args.dest)))
        if not found:
            print("no pinned checkout at %s (no %s)" % (args.dest, pin.PINNED_FILE))
            return 1
        if found.get("unreadable"):
            # A PIN THAT IS THERE AND CANNOT BE READ, reported as that. It used to reach the
            # branch above and print "no pinned checkout", which is a different fact and the
            # one that sends a reader looking for a directory that is sitting right there.
            print("%s%s exists at %s and CANNOT BE READ (%s) — this is a pin whose commit is "
                  "unknown, not the absence of one. Re-pin it.%s"
                  % (RED, pin.PINNED_FILE, args.dest, found["unreadable"], OFF))
            return 2
        print("pinned  %s (%s)" % (found["sha"][:12], found.get("ref")))
        print("  at    %s" % _stamp_or(found.get("at")))
        if not found.get("consistent"):
            # Only reachable if something edited the directory after the pin, which is the one
            # thing this module admits it cannot otherwise see. Loud, not reconciled.
            print("  %sVERSION (%s) and %s (%s) DISAGREE — this directory was modified after "
                  "it was pinned, so neither names what is actually here.%s"
                  % (RED, found.get("version"), pin.PINNED_FILE, found["sha"][:12], OFF))
            return 2
        return 0

    if not args.dest:
        die("self --pin: --dest is required. The only consumer of a pin today is a machine-wide "
            "central install, which is somewhere you name; there is no default that would be "
            "right for it.", code=64)
    d = pin.pin(args.pin, args.dest)
    print("pinned %s (%s) → %s" % (d["ref"], d["sha"][:12], d["dest"]))
    print("  from  %s — the checkout this code is running out of, never the project you are "
          "standing in" % d["source"])
    print("  stamped %s and %s, so 'what is central running' answers with a commit rather than "
          "with whoever last copied a working tree" % (pin.VERSION_FILE, pin.PINNED_FILE))
    print("  NOTHING POINTS AT IT YET — wiring consumer repos to a central copy is CI-03 "
          "(`install.sh --central`). This populated a directory; it did not switch anything over.")
    return 0


def _stamp_or(ts):
    return stamp(ts) if ts else "?"


# THE SENTENCE THE SHIMS ALSO SAY, so the two entrypoints can be compared BY THEIR WORDS.
#
# Fixing #56 cost a detector. That divergence — the shim carrying the CLAUDE_PROJECT_DIR
# fallback while the CLI did not — was caught because the message text was the tell: the
# pre-fix sentence in the CLI output was visible evidence the fallback had not reached it,
# before any assertion existed to compare the two.
#
# Afterwards they differed by DEFAULT. The shim named both anchors it tried; the CLI wrapped
# everything as `it raised <Refused>`. Behaviour agreed, so nothing was broken — and a future
# divergence would have produced two different messages that looked exactly like today's two
# different messages. A behavioural assertion catches what it was written to compare; the text
# was what caught the case nobody had thought to compare yet.
NO_REPO_FAIL_OPEN = ("neither the working directory nor CLAUDE_PROJECT_DIR resolves to a git "
                     "repository, so this call was ALLOWED WITHOUT BEING CHECKED")


def _fail_open_text(which, exc, consequence):
    """One fail-open sentence for both CLI guards, matching the shims for the case that happens.

    ANY OTHER EXCEPTION KEEPS ITS TYPE AND MESSAGE. An unexpected failure is different
    information, and flattening it would trade one lost detector for another.
    """
    head = "⚠ THE %s GUARD DID NOT RUN — " % which
    if "not inside a git repository" in str(exc):
        return "%s%s. %s Check: `showrunner doctor`" % (head, NO_REPO_FAIL_OPEN, consequence)
    return ("%sit raised %s: %s. This tool call was ALLOWED WITHOUT BEING CHECKED. %s "
            "Repair it, do not work around it: `showrunner doctor`."
            % (head, type(exc).__name__, exc, consequence))


def cmd_worktree_register(args):
    """Put the guard's PreToolUse entry in .claude/settings.json. Idempotent.

    A VERB RATHER THAN A PARAGRAPH, and the reason is a bug this repo shipped and then found
    within the hour: `init` registered the guard, and `init` only runs when there is no config
    — so every ALREADY-INSTALLED consumer got the shim file on upgrade and no registration, and
    would have been handed a `doctor` error for a gap the installer created. That is exactly the
    "fires once per repo, leaving the population that has the hole" shape `install.sh` already
    carries a comment about for the ignore rules.

    So the installer calls this on EVERY run, and `doctor`'s remedy can name a command that
    exists instead of printing JSON to paste by hand.
    """
    cfg = _cfg(args)
    # --local WRITES THE UNTRACKED LAYER, for a repo that keeps showrunner out of its history.
    # `doctor` endorses that arrangement and its remedy used to name the tracked file, which
    # would have committed five showrunner hooks into a file shared with other developers.
    local = bool(getattr(args, "local", False))
    settings = rel(lease.settings_target(cfg.root, local), cfg.root)
    # EVERY HOOK SHOWRUNNER OWNS, from one verb. They are registered together because they are
    # installed together and a reader has no way to know how many there are — and one existing
    # but unregistered is precisely the state this verb was built to end.
    #
    # (This said "BOTH OF SHOWRUNNER'S HOOKS" over a list of four, which is the ungated count in
    # prose this repo keeps correcting. No number now: the list is the claim.)
    #
    # `reach` JOINED THE LIST AFTER A CONSUMER MEASURED ITS ABSENCE. It shipped wired by hand,
    # on the reasoning that advice should be opt-in — and a consumer then hand-rolled a worktree
    # wrapper, a dispatch script and a layer guard in one session, with zero references to it in
    # either settings layer, before piping a payload in by hand and getting the sentence they
    # had needed hours earlier. A verb whose job is to say "the tool already does this" is
    # useless to anyone who does not already know it exists.
    rc = 0
    for register_fn, what in ((lease.register_guard, "worktree guard"),
                              (lease.register_stop_trigger, "inert-Crawler stop trigger"),
                              (lease.register_whoami,
                               "seat announcement (SessionStart+PostCompact)"),
                              (lease.register_dispatch_guard,
                               "dispatch guard (PreToolUse on Bash)"),
                              (lease.register_reach,
                               "reach gate (PreToolUse; advice, never refuses)")):
        register = (lambda c, f=register_fn: f(c, local))
        changed, note = register(cfg)
        if changed:
            print(note)
        elif note:
            eprint(note)
            rc = 2
        else:
            print("the %s is already registered in %s" % (what, settings))
    return rc


def _hook_payload():
    """The PreToolUse JSON on stdin. Returns (payload, problem).

    Never raises and never blocks: `problem` carries what went wrong so the CALLER can decide,
    and the caller's only safe decision is to allow. A guard that dies reading its own input
    exits non-zero, and a non-zero PreToolUse blocks every write including the one that would
    repair it.
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}, "no stdin (not running as a hook)"
        raw = sys.stdin.read()
    except (OSError, ValueError) as exc:
        return {}, "stdin could not be read (%s)" % exc
    if not (raw or "").strip():
        return {}, "empty stdin"
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return {}, "stdin was not JSON (%s)" % exc
    return (payload, None) if isinstance(payload, dict) else ({}, "stdin JSON was not an object")


def _allow_loudly(notice):
    """Exit 0, and SAY the guard did not run — the posture, and never the silent half of it.

    Allowing without a word is indistinguishable from a guard that ran and was content, which
    is how a rail goes quiet exactly where it is blind. The structured form is used rather than
    a bare print because `additionalContext` is what actually reaches the agent on an allow —
    this is the shape `.game_loop/bin/guard-writes.sh` uses for the same purpose, in this repo,
    on every tool call, which makes it the mechanism with observed evidence behind it.
    """
    _record_fail_open(notice)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "additionalContext": notice}}))
    return 0


def _record_fail_open(notice):
    """Append the fail-open to a durable ledger, so something downstream can ASK.

    THE NOTICE IS TECHNICALLY EMITTED AND RELIABLY UNREAD. It arrives as hook output beside a
    successful tool result, and an agent concentrating on something else skims it — the
    reporter said so about their own reading, and they were the person who had just filed the
    guard issue. Nothing downstream ever asked whether it was consumed, which makes the failure
    silent by construction and satisfies "fail loud" in letter only.

    A louder wording would treat a delivery problem as a copywriting problem. A COUNT is a
    different fact from a banner: N unchecked calls in a session is not something a reader can
    skim past in the same way, and `doctor` is read by somebody who has stopped to look rather
    than by somebody mid-task.

    Best-effort by construction: a ledger that cannot be written must never turn a fail-open
    into a hard failure, which would block the write that repairs the guard.
    """
    try:
        root = _fail_open_root()
        if not root:
            return
        with open(os.path.join(root, ".showrunner", "fail-open.jsonl"), "a") as fh:
            fh.write(json.dumps({"ts": now(), "notice": notice[:300]}) + "\n")
    except Exception:                                            # noqa: BLE001
        pass


def _fail_open_root():
    """Find a `.showrunner` WITHOUT loading config, because config is why we are here.

    The first version keyed the ledger off `config.load().state_dir` — which is unavailable in
    precisely the case that makes a guard fail open. It would have recorded the fail-opens that
    least needed recording and dropped every one caused by an unreadable config: a ledger whose
    coverage is the complement of its purpose. Found by asking what `_cfg` raising means for
    the line that runs afterward, and confirmed by running it from a no-repo directory.

    The anchors are the shims' anchors, in the shims' order — two entrypoints for one guard is
    this repo's standing hazard, and a ledger they disagree about is the same defect wearing a
    different hat.
    """
    seen = set()
    for anchor in (os.environ.get("CLAUDE_PROJECT_DIR"), os.getcwd(),
                   os.path.dirname(os.path.dirname(os.path.dirname(
                       os.path.abspath(__file__))))):
        if not anchor or anchor in seen:
            continue
        seen.add(anchor)
        here = os.path.abspath(anchor)
        while True:
            if os.path.isdir(os.path.join(here, ".showrunner")):
                return here
            parent = os.path.dirname(here)
            if parent == here:
                break
            here = parent
    return None


def cmd_whoami(args):
    """SessionStart / PostCompact: say what this session IS, and what it may not do (#36).

    THE OUTPUT IS THE ENTIRE MECHANISM. A SessionStart hook cannot block, so there is nothing
    else this can do — which means silence is the one unacceptable outcome. It prints on every
    path, including the ones where it could not work out the answer, because an announcer that
    cannot tell and says nothing is indistinguishable from a healthy one.

    ON BOTH SEAMS, and the second is the one that matters. A consumer had showrunner installed,
    wired, and a campaign with 38 leaves done, and its orchestrator still hand-rolled worktree
    isolation 42 times — because nothing fired at a session boundary, so every compaction
    refreshed the harness that owned those seams and eroded the tool that did not.
    """
    session = args.session or caller_session()
    try:
        cfg = _cfg(args, required=False)
        # ONE RESOLVER, TWO RENDERINGS. The porcelain is not a second answer computed a second
        # way — `roles.whoami` renders the same dict this prints, so a guard reading the JSON and
        # a human reading the prose cannot be told different things.
        if getattr(args, "porcelain", False):
            print(json.dumps(roles.resolution(cfg, session), indent=2, sort_keys=True))
            return 0
        lines = roles.whoami(cfg, session)
    except Exception as exc:                                    # noqa: BLE001 — see docstring
        if getattr(args, "porcelain", False):
            # A PARSER MUST NOT READ A CRASH AS A PERMISSIVE ANSWER. The prose path prints and
            # exits 0 because a SessionStart hook cannot block; a guard consuming this has to be
            # able to fail closed, so this reports the failure in-band AND exits non-zero.
            print(json.dumps({"version": roles.PORCELAIN_VERSION, "enforced": False,
                              "role": None, "problems": ["%s: %s" % (type(exc).__name__, exc)]},
                             indent=2, sort_keys=True))
            return 1
        print("showrunner: COULD NOT SAY WHAT THIS SESSION IS — %s: %s. That is printed rather "
              "than swallowed: an announcer that fails quietly is indistinguishable from one "
              "that had nothing to say." % (type(exc).__name__, exc))
        return 0
    for line in lines:
        print(line)
    return 0


# ------------------------------------------------------- role seats (claim/release/roster)
# BOTH ACQUISITION MODES WERE UNREACHABLE FROM THE CLI. `assign` had no reader until a seat could
# resolve through `seat_roles`; `claim` had no writer at all — `roles.claim` existed as a library
# function nothing called, and the `claim` verb claims a LEAF. So on a stock install every
# session resolved to the fallback whatever its roles said, and the only way to seat anything was
# to import the library and call it from Python.
def cmd_role_claim(args):
    cfg = _cfg(args)
    defs, problems = roles.spec(cfg)
    for msg in problems:
        eprint("note: %s" % msg)
    if not defs:
        die("no roles are defined, so there is no seat to claim. Define them at %s"
            % roles.USER_PATH, code=2)
    if args.role not in defs:
        die("no role named %r is defined (known: %s)" % (args.role, ", ".join(sorted(defs))),
            code=2)
    acquire = (defs[args.role] or {}).get("acquire")
    if acquire and acquire != "claim":
        # A ROLE THAT SAYS `assign` MUST NOT BE CLAIMABLE. Its whole meaning is that whoever
        # created the session decided; letting a session take it here would be self-nomination
        # into a seat the model says it cannot nominate itself for.
        die("role %r declares acquire=%r, so it cannot be claimed — it is assigned by whoever "
            "creates the session, through `seat_roles`" % (args.role, acquire), code=2)

    session = args.session or caller_session()
    ok, holder = roles.claim(cfg, args.role, session, pid=args.pid, who=args.who, seat=args.seat)
    if not ok and holder.get("pid_basis") == "unresolved":
        print("showrunner: %s.\n  Nothing was claimed; that is said out loud rather than left to "
              "look like success." % holder.get("why"))
        return 1
    if not ok:
        eprint("BLOCKED: seat %s#%d is held by pid %s (%s)"
               % (args.role, args.seat, holder.get("pid"), holder.get("who")))
        return 1
    print("CLAIMED %s#%d (pid %s — liveness basis: %s)"
          % (args.role, args.seat, holder.get("pid"), holder.get("pid_basis") or "?"))
    if holder.get("pid_basis") != RESOLVED_BASIS:
        # DEAD ON ARRIVAL IS THE WORST OUTCOME, so it is named at the moment of claiming rather
        # than discovered later as a fallback role. `lock acquire` prints the same warning for
        # the same reason; this path shared its mechanism and not its mitigation.
        eprint("NOTE: liveness rests on %r, not on a resolved session process. If that pid exits, "
               "this seat reads STALE and `whoami` will announce the fallback again — check with "
               "`%s role roster`." % (holder.get("pid_basis") or "?", brief.sr_bin(cfg)))
    return 0


def cmd_role_release(args):
    cfg = _cfg(args)
    ok, before = roles.release(cfg, args.role, pid=args.pid, seat=args.seat, force=args.force)
    if not ok:
        eprint("nothing to release: seat %s#%d is not held" % (args.role, args.seat))
        return 1
    print("RELEASED %s#%d (was pid %s — %s)"
          % (args.role, args.seat, before.get("pid"), before.get("who")))
    return 0


def cmd_role_roster(args):
    """Every seat and the state of its holder — the read that made a dead claim diagnosable.

    A claim keyed to a process that had already exited reported success and then read STALE, and
    the only way to SEE that was to call `roles.roster` from Python. A state nothing surfaces is
    a state nobody debugs.
    """
    cfg = _cfg(args)
    entries = roles.roster(cfg)
    if args.json:
        print(json.dumps(entries, indent=2, sort_keys=True))
        return 0
    if not entries:
        print("no role seats have been claimed in this campaign.")
        return 0
    for e in entries:
        h = e.get("holder") or {}
        print("%-24s %-6s pid %s (%s) basis %s" % (e["role"], e["state"], h.get("pid") or "-",
                                                   h.get("who") or "-",
                                                   h.get("pid_basis") or "-"))
    stale = [e["role"] for e in entries if e["state"] == locks.STALE]
    if stale:
        eprint("NOTE: %d seat(s) read STALE — the holder is proved dead, so `_resolved` skips "
               "them and those sessions announce the fallback: %s" % (len(stale), ", ".join(stale)))
    return 0


def cmd_dispatch_guard(args):
    """PreToolUse on Bash: refuse a raw `claude -p` from a session whose role may not create (#37).

    ON BASH, WHICH IS THE MECHANISM ACTUALLY USED. A consumer's prototype registered this on
    `Agent` and guarded the in-process subagent tool while every real dispatch went out through
    Bash — 42 consecutive times, reporting nothing. A guard matched on the wrong tool is
    indistinguishable from a world with nothing to guard, which is why the matcher is part of
    the fix and not a detail of it.

    Same fail-open posture as `worktree guard`, for the same reason: this runs before every Bash
    call in the repo, so a bug here that exited non-zero would lock the repo against its own
    repair. Every unknown allows AND SAYS the call went unchecked.
    """
    # THE PAYLOAD IS ONLY READ WHEN THERE IS ONE TO READ. `_hook_payload` blocks on stdin, so
    # calling it unconditionally made `--command` — the flag that exists to test the rule
    # WITHOUT constructing a hook event — hang forever waiting for a payload nobody was going to
    # send. Found by running it.
    payload, problem = ({}, None) if args.command is not None else _hook_payload()
    if problem:
        return _allow_loudly(
            "⚠ THE DISPATCH GUARD DID NOT RUN — it could not read its PreToolUse payload (%s), "
            "so this tool call was ALLOWED WITHOUT BEING CHECKED. A raw `claude -p` would skip "
            "the worktree, the lease, the claim and the room." % problem)
    try:
        cfg = _cfg(args, guard=True)
        session = (args.session or payload.get("session_id")
                   or os.environ.get("SHOWRUNNER_SESSION") or "")
        tool = payload.get("tool_name") or ""
        tool_input = payload.get("tool_input") or {}
        if args.command is not None:
            tool, tool_input = "Bash", {"command": args.command}
        allow, message, detail = dispatch.dispatch_guard(cfg, session, tool=tool,
                                                         tool_input=tool_input)
    except Exception as exc:                                    # noqa: BLE001 — see docstring
        return _allow_loudly(_fail_open_text(
            "DISPATCH", exc,
            "A raw `claude -p` would skip the worktree, the lease, the claim and the room."))

    if allow:
        if message:
            return _allow_loudly(message)
        print(message or "allow: %s" % (detail.get("why") or "not a dispatch"))
        return 0
    eprint(message)
    return 2


def _human(n):
    """Bytes, or '?' — never 0 for a size that could not be measured."""
    if n is None:
        return "?"
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return "%.0f%s" % (n, unit)
        n /= 1024.0
    return "%.0fG" % n


def _report_reclaim(take, held, applied):
    """What was (or would be) reclaimed, and everything held back WITH ITS REASON."""
    total = sum(r["bytes"] or 0 for r in take)
    unknown_size = any(r["bytes"] is None for r in take)
    for r in held:
        eprint("  %sHELD%s %-22s %s" % (YEL, OFF, r["crawler"] or "?", r["why"]))
    for r in take:
        print("  %s %-22s %s (%s)" % ("removed" if applied else "would remove",
                                      r["crawler"] or "?", r["worktree"], _human(r["bytes"])))
    if take:
        print("  %s %d tree(s), %s%s" % ("reclaimed" if applied else "would reclaim",
                                         len(take), _human(total),
                                         " (at least — one tree could not be measured)"
                                         if unknown_size else ""))
    elif not held:
        print("  no worktrees to reclaim, and none held back — nothing was found to look at")


def cmd_gc(args):
    """Remove worktrees whose branch is merged and whose tree is clean (#75).

    DRY RUN BY DEFAULT, like `reap`, because this deletes directories. `--apply` performs it.

    The branch and every commit on it survive: that is what makes a merged, clean tree
    redundant rather than valuable, and `spawn` can recreate it. A tree that is dirty, or whose
    state could not be read, or whose branch is not merged, is REPORTED and kept — the same
    discrimination `reap` already makes, applied to the trees of leaves that succeeded.
    """
    cfg = _cfg(args)
    take, held = campaign.reclaimable(cfg, _graph(cfg), base=args.base)
    print("%sWorktree reclaim%s%s" % (BOLD, OFF, "" if args.apply else " (dry run — use --apply)"))
    removed = []
    if args.apply:
        for r in take:
            try:
                worktree.remove(cfg, r["crawler"])
                removed.append(r)
            except SystemExit:
                raise
            except Exception as exc:                            # noqa: BLE001
                eprint("  %sFAILED%s %s: %s" % (RED, OFF, r["crawler"], exc))
        _report_reclaim(removed, held, True)
    else:
        _report_reclaim(take, held, False)
    return 0


def cmd_reach(args):
    """PreToolUse: name the mechanism for what this call reached for. NEVER refuses.

    Always exits 0. This fires on Write, Edit and Bash — the same breadth as the write guard —
    so a bug here that exited non-zero would lock the repo against its own repair (INV5), and
    unlike the lease guard it protects nothing by blocking. Its entire product is context.

    SILENT WHEN NOTHING MATCHES, and that is the one place silence is right: this speaks only
    when it has a specific verb for a specific intent. A notice on every call is an alarm that
    is always on, which is the failure #71 was about — and the reader would learn to skim
    exactly the channel this depends on.
    """
    payload, problem = reach.hook_payload(sys.stdin)
    if problem:
        # NOT a fail-open notice. A guard that could not check has to say so because something
        # went unprotected; this one protects nothing, so an unreadable payload costs advice and
        # announcing it would spend the reader's attention to report that nothing happened.
        return 0
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    try:
        cfg = _cfg(args, required=False, guard=True)
        root, sr = cfg.root, brief.sr_bin(cfg)
    except Exception:                                           # noqa: BLE001
        root, sr = None, None
    hits = reach.advise(tool, tool_input, root)
    text = reach.render(hits, sr)
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": text}}))
    return 0


def cmd_worktree_guard(args):
    """PreToolUse: refuse a write into a tree another LIVE session holds. Exit 2 denies.

    THE TEETH. `worktree enter` detects a hijack and prints; this is the half that refuses,
    and it is deliberately the last thing built — WL-03 produced two real `lease.hijack`
    events in the journal first, because no gate is built without a logged, observed failure.

    **Exit 2 rather than the structured `permissionDecision: "deny"` form.** Both are valid
    hook contracts; this one keeps ONE convention inside showrunner, beside `lock guard` which
    already exits 2, rather than having two of our own guards answer the same event in two
    shapes. The structured form is still used for the fail-open notice, where it is the only
    thing that reaches the agent.

    **Every failure path here allows.** Unreadable payload, missing config, an exception from
    anywhere below — all exit 0 with a notice. This verb runs before every Write, Edit and
    Bash in the repo, so a bug in it that exited non-zero would lock the repo against its own
    repair (INV5). `doctor` carries the loudness that this cannot.
    """
    payload, problem = _hook_payload()
    if problem and not (args.session or args.path or args.command):
        return _allow_loudly(
            "⚠ THE WORKTREE GUARD DID NOT RUN — it could not read its PreToolUse payload (%s), "
            "so this tool call was ALLOWED WITHOUT BEING CHECKED. A worktree held by another "
            "live session is NOT protected right now. Check `showrunner doctor`." % problem)

    try:
        cfg = _cfg(args, guard=True)
        session = (args.session or payload.get("session_id")
                   or os.environ.get("SHOWRUNNER_SESSION") or "")
        tool = args.tool or payload.get("tool_name") or ""
        tool_input = payload.get("tool_input") or {}
        if args.command is not None:
            tool, tool_input = "Bash", {"command": args.command}
        if args.path:
            tool_input = dict(tool_input, file_path=args.path)
        cwd = payload.get("cwd") or os.getcwd()
        allow, message, detail = lease.guard(cfg, session, tool=tool, tool_input=tool_input,
                                             cwd=cwd)
    except Exception as exc:                                    # noqa: BLE001 — see docstring
        return _allow_loudly(_fail_open_text(
            "WORKTREE", exc,
            "A worktree held by another live session is NOT protected right now."))

    if allow:
        if detail.get("degraded"):
            return _allow_loudly(message)
        print(message)
        return 0

    # LOGGED. The refusal is the event a later reader needs — that the gate fired at all, and
    # against whom. A denial visible only in one session's scrollback is not an observation.
    try:
        events.emit(cfg, "lease.denied", {
            "tree": detail.get("tree"), "intruder_session": session, "tool": tool,
            "holder_session": (detail.get("holder") or {}).get("session"),
            "holder_pid": (detail.get("holder") or {}).get("pid"),
            "holder": (detail.get("holder") or {}).get("who")})
    except Exception:                                           # noqa: BLE001
        # A journal that will not accept a line must not turn a correct refusal into a crash,
        # and must not turn it into an allow either. The denial stands; the record is what is
        # lost, and the message below is still on the channel the agent reads.
        eprint("showrunner: (the denial below could not be journalled)")
    eprint(message)
    return 2


def cmd_lease_status(args):
    """What holds each worktree. Read-only, and it prints the BASIS of every liveness claim.

    A lease's pid is discovered by walking a hook's ancestry rather than handed over (WL-01),
    and that walk can land on a weaker fact. Printing the state without the basis would show
    HELD in the same shape whether the process was identified or merely guessed at, which is
    the reader forming a belief the data does not support.
    """
    cfg = _cfg(args)
    rows = lease.status(cfg, args.tree)
    if not rows:
        print("no worktree leases (nothing has entered a managed worktree, or none exist)")
        return 0
    for r in rows:
        h = r["holder"] or {}
        if r["state"] == locks.FREE:
            print("%-28s FREE" % r["tree"])
            continue
        basis = r.get("pid_basis") or "?"
        print("%-28s %s by pid %s (%s) session %s since %s"
              % (r["tree"], r["state"], h.get("pid"), h.get("who") or "?",
                 (h.get("session") or "?")[:8], h.get("ts")))
        print("%-28s   liveness basis: %s%s"
              % ("", basis,
                 "  ← the session process was NOT identified; this pid is its parent, so a "
                 "dead session may still read as HELD" if basis == "ppid-fallback" else ""))
        if not r["exists"]:
            print("%-28s   NOTE: %s no longer exists on disk, but the lease does — a lease "
                  "outliving its tree is stale state, not a holder" % ("", r["path"]))
    return 0


def cmd_lock_acquire(args):
    cfg = _cfg(args)
    lock = locks.LockSet(cfg).lock(args.resource)
    ok = lock.acquire(os.getpid(), args.holder, session=args.session, wait=args.wait)
    if not ok:
        _, h = lock.state()
        events.emit(cfg, "lock.refused", {"resource": args.resource, "who": args.holder,
                                          "held_by": h.get("who"), "held_pid": h.get("pid"),
                                          "path": "acquire"})
        eprint("BLOCKED: %r held by pid %s (%s)" % (args.resource, h.get("pid"), h.get("who")))
        return 2
    events.emit(cfg, "lock.acquired", {"resource": args.resource, "who": args.holder,
                                       "lock_pid": os.getpid(), "path": "acquire"})
    print("ACQUIRED %s (pid %s — %s)" % (args.resource, os.getpid(), args.holder))
    eprint("NOTE: the holder recorded is THIS shell, which exits immediately. `lock run` is the "
           "authoritative path — there the holder is the consumer process itself.")
    return 0


def cmd_lock_release(args):
    cfg = _cfg(args)
    # `existing`, not `lock`: this is the remedy the UNREADABLE refusal prints, and the locks a
    # human most needs to clear — `worktree:<tree>` leases — are not configured resources.
    lock = locks.LockSet(cfg).existing(args.resource)
    if lock.release(pid=args.pid or os.getpid(), force=args.force):
        events.emit(cfg, "lock.released", {"resource": args.resource, "forced": bool(args.force),
                                           "path": "release"})
        print("released %s" % args.resource)
        return 0
    print("%s was not held" % args.resource)
    return 0


def cmd_lock_guard(args):
    cfg = _cfg(args)
    command = " ".join(args.command)
    allow, msg = locks.LockSet(cfg).guard(command, session=args.session or os.environ.get("SHOWRUNNER_CRAWLER"))
    if allow:
        print(msg)
        return 0
    eprint(msg)
    return 2


def cmd_lock_run(args):
    cfg = _cfg(args)
    lock = locks.LockSet(cfg).lock(args.resource)
    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        die("nothing to run — usage: `showrunner lock run <resource> --holder <who> -- <cmd...>`",
            code=64)
    if not lock.acquire(os.getpid(), args.holder, session=args.session, wait=args.wait):
        _, h = lock.state()
        events.emit(cfg, "lock.refused", {"resource": args.resource, "who": args.holder,
                                          "held_by": h.get("who"), "held_pid": h.get("pid"),
                                          "path": "run", "waited": args.wait})
        eprint("BLOCKED: %r held by pid %s (%s). One consumer at a time."
               % (args.resource, h.get("pid"), h.get("who")))
        return 2
    events.emit(cfg, "lock.acquired", {"resource": args.resource, "who": args.holder,
                                       "lock_pid": os.getpid(), "path": "run"})
    rc = None
    try:
        import subprocess
        rc = subprocess.call(cmd, cwd=cfg.root)
        return rc
    finally:
        lock.release(pid=os.getpid(), force=True)
        # In the FINALLY, so a consumer killed mid-command still reports the release that
        # actually happened. A view that shows a resource held by a process that died is the
        # stale-lock confusion this whole module exists to prevent, one layer out.
        events.emit(cfg, "lock.released", {"resource": args.resource, "who": args.holder,
                                           "path": "run", "exit": rc})


# ------------------------------------------------------------- route/plan
def cmd_route(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    leaves = [g.show(i) for i in args.id] if args.id else g.ready()
    decisions = [lanes.route(cfg, leaf) for leaf in leaves]
    path = lanes.log(cfg, decisions)
    for d in decisions:
        colour = "" if d["matched"] else YEL
        print("%s%s%s" % (colour, d.line(), OFF))
    print("%slogged to %s%s" % (DIM, rel(path, cfg.root), OFF))
    unmatched = [d for d in decisions if not d["matched"]]
    if unmatched:
        print("%s%d leaf/leaves matched no lane rule and defaulted. That is a missing rule, not a "
              "neutral outcome.%s" % (YEL, len(unmatched), OFF))
    return 0


def cmd_plan(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    ready = g.ready()
    if not ready:
        print("no ready work — `showrunner ready` is dry")
        return 0
    decisions = {leaf["id"]: lanes.route(cfg, leaf) for leaf in ready}
    headless = [l for l in ready if decisions[l["id"]]["lane"] == lanes.HEADLESS]
    serialized = [l for l in ready if decisions[l["id"]]["lane"] != lanes.HEADLESS]

    waves, estimates, notes = collide.plan_waves(cfg, headless)
    print("%sheadless lane%s — %d ready leaf/leaves in %d wave(s)" % (BOLD, OFF, len(headless), len(waves)))
    for i, wave in enumerate(waves, 1):
        print("  wave %d: %s" % (i, ", ".join(wave)))
        for leaf_id in wave:
            est = estimates[leaf_id]
            print("    %s%-16s %s%s" % (DIM, leaf_id, est["basis"], OFF))
    for note in notes:
        print("  %s%s%s" % (YEL, note, OFF))

    if serialized:
        print("%sserialized lane%s — %d leaf/leaves, one at a time:" % (BOLD, OFF, len(serialized)))
        for leaf in serialized:
            d = decisions[leaf["id"]]
            print("  %-16s resource=%-10s %s" % (leaf["id"], d.get("resource") or "?", d["why"]))

    # WHAT IS ALREADY RUNNING (#71). The waves above are computed from `ready`, which is
    # unblocked AND UNCLAIMED — so a Crawler working right now is absent from the INPUT and its
    # files are never considered occupied. That is the correct answer to "how would I group this
    # work if nothing were running", and it is a legitimate question BEFORE a campaign starts,
    # so the grouping is left exactly as it was. What was missing is the other question, the one
    # an orchestrator mid-campaign is actually asking, and it is appended rather than folded in.
    #
    # Nothing is printed when nothing is live, so `plan` on a quiet campaign is unchanged.
    live, caveat = collide.live_claims(g)
    conflicts = {}
    if live:
        files = collide.tracked_files(cfg.root)
        for leaf in headless:
            found = collide.live_conflicts(cfg, leaf, live, files)
            if found:
                conflicts[leaf["id"]] = found
    if live:
        print("%sLIVE CLAIMS%s — %d claimed leaf/leaves are being worked NOW, and the waves "
              "above do not account for them:" % (BOLD, OFF, len(live)))
        for leaf in live:
            print("  %s%-16s claimed by %s%s%s" % (DIM, leaf["id"], leaf.get("actor") or "?",
                                                   " (PARKED)" if leaf.get("parked") else "", OFF))
        if caveat:
            eprint("  %s%s%s" % (YEL, caveat, OFF))
    for leaf_id, found in sorted(conflicts.items()):
        for c in found:
            if c["blocks"]:
                print("  %sCOLLIDES%s %s  <->  %s (live)" % (RED, OFF, leaf_id, c["leaf"]))
            else:
                print("  %sshared surface only%s %s <-> %s (live)"
                      % (DIM, OFF, leaf_id, c["leaf"]))
            for f in c["files"][:8]:
                print("      both estimate %s" % f)
            if c["blind"]:
                print("      one of the two has NO estimable blast radius — treated as "
                      "colliding with everything")
            for f in c["shared"][:8]:
                print("      %sshared surface, owed to serialised integration, not blocking: "
                      "%s%s" % (DIM, f, OFF))
    if conflicts:
        eprint("%sThis is an ESTIMATE from declared paths and grepped symbols, not a "
               "measurement.%s `overlap` measures, and it cannot see a live Crawler that has "
               "not committed yet — a branch existing is not enough, it counts branches with "
               "commits. `spawn` refuses these unless you name the live leaf with "
               "--despite-live." % (YEL, OFF))

    if args.json:
        print(json.dumps({
            "waves": waves,
            "serialized": [l["id"] for l in serialized],
            "estimates": {k: {"paths": sorted(v["paths"]), "shared": sorted(v["shared"]),
                              "basis": v["basis"]} for k, v in estimates.items()},
            "notes": notes,
            # Additive: a reader that never looks at these gets exactly the plan it got before.
            "live": [l["id"] for l in live],
            "live_caveat": caveat,
            "live_conflicts": conflicts,
            "estimated": True,
        }, indent=2, sort_keys=True))
    return 0


# ------------------------------------------------------------------ spawn
def _print_base(report):
    """The one input that silently decides whether a spawn is correct (#33).

    `spawn` cuts from the PRIMARY checkout's HEAD, and that default is invisible and
    context-dependent: the identical command is right or wrong depending on where an unrelated
    checkout happens to be pointing. Everything else about a dispatch was printed — lane, model,
    the full argv — except this. One line would have caught the reported failure, because `main`
    was obviously wrong to the operator the moment they saw it.
    """
    origin = "explicit --base" if report.get("explicit") else "primary checkout HEAD"
    print("  base     %s @ %s (%s)" % (report.get("branch") or "?",
                                       (report.get("sha") or "?")[:12], origin))
    for dep, dep_branch in report.get("missing") or []:
        eprint("  %sBASE IS MISSING A DEPENDENCY: %s (%s) is NOT an ancestor of this base.%s"
               % (RED, dep, dep_branch, OFF))
        eprint("    The Crawler will come up without that work in its history. A brief that "
               "branches on\n    whether a prerequisite landed will then take the smaller path, "
               "correctly, and report a\n    complete honest outcome that is half the item — "
               "with every gate green.")
        eprint("    Pass --base %s (or a ref that contains it) if this leaf builds on it."
               % dep_branch)
    for why in report.get("unknown") or []:
        eprint("  %sBASE NOT CHECKED against one dependency: %s%s" % (YEL, why, OFF))
        eprint("    Not 'nothing is missing' — this could not look. Confirm the base yourself.")
    if report.get("present") and not (report.get("missing") or report.get("unknown")):
        print("  base dep %s in history" % ", ".join(
            d for d, _ in report["present"]))


def _live_collision_check(cfg, g, leaf, despite, rehearsing=False):
    """Refuse to open a room inside files a LIVE Crawler is already in (#71).

    SPAWN IS THE PLACE THIS HAS TO LIVE. `plan` reports the same finding one screen earlier,
    and an orchestrator that reads `plan` carefully was never the failure mode — the failure
    mode is one that does not read it, or reads it and has forgotten by the third wave. This
    is the one verb that cannot be skipped by a careless caller, because it is what actually
    creates the tree.

    THE OVERRIDE NAMES WHAT IT OVERRIDES. `--despite-live <leaf-id>`, repeatable, and it must
    cover every colliding leaf or the refusal stands. A bare `--force` is answered reflexively
    within a week and teaches every later session that the guard is a speed bump; naming the
    live Crawler you are choosing to collide with costs a read of the refusal, which is the
    read the guard exists to force. Naming a leaf that is not colliding is refused too — an
    override copied from a previous command is not a decision.

    Shared surfaces do not block, for the reason `plan_waves` gives: `always_serialize` names
    the file every change touches, so blocking on it would serialise the campaign behind
    whichever Crawler is running and the guard would look like the reason the run is slow.
    """
    live, caveat = collide.live_claims(g)
    if not live:
        return []
    found = collide.live_conflicts(cfg, leaf, live)
    blocking = [c for c in found if c["blocks"]]
    for c in found:
        if not c["blocks"]:
            eprint("  %sshared surface with live %s (owed to serialised integration, not "
                   "blocking): %s%s" % (DIM, c["leaf"], ", ".join(c["shared"][:6]), OFF))
    if not blocking:
        return found
    named = set(despite)
    unmatched = named - {c["leaf"] for c in blocking}
    uncovered = [c for c in blocking if c["leaf"] not in named]
    lines = []
    for c in blocking:
        who = c.get("actor") or "?"
        lines.append("  %s (live, claimed by %s%s)%s"
                     % (c["leaf"], who, ", PARKED" if c["parked"] else "",
                        " — NO estimable blast radius on one side, so it is treated as "
                        "colliding with everything" if c["blind"] else ""))
        for f in c["files"][:8]:
            lines.append("      both estimate %s" % f)
    detail = "\n".join(lines)
    if unmatched:
        die("--despite-live named %s, which is not colliding with %s right now.\n"
            "An override copied from an earlier command is not a decision. Colliding live "
            "leaves are:\n%s"
            % (", ".join(sorted(unmatched)), leaf["id"],
               detail or "  (none)"), code=2)
    if uncovered:
        cmd = " ".join("--despite-live %s" % c["leaf"] for c in uncovered)
        msg = ("%s overlaps work a LIVE Crawler is doing right now:\n%s\n"
               "%s"
               "This is an ESTIMATE from declared paths and grepped symbols, not a "
               "measurement — `overlap` measures, and it CANNOT see this: a Crawler with no "
               "commits has no in-flight branch by that definition, even when its branch "
               "exists.\n"
               "Wait for it to close, or accept the collision deliberately by naming it:\n"
               "    showrunner spawn %s %s"
               % (leaf["id"], detail,
                  ("  %s\n" % caveat) if caveat else "",
                  leaf["id"], cmd))
        if rehearsing:
            # The rehearsal must show what the real spawn would refuse, and still create
            # nothing. Refusing here would make --dry-run unable to preview its own refusal.
            eprint("%sWOULD REFUSE: %s%s" % (RED, msg, OFF))
            return found
        die(msg, code=3)
    print("%sACCEPTED A LIVE COLLISION on purpose (--despite-live %s):%s\n%s"
          % (YEL, ", ".join(sorted(named)), OFF, detail))
    return found


def _implicit_base_check(cfg, leaf, report, rehearsing=False):
    """REFUSE an IMPLICIT base when the checkout is not standing on the default branch (#73).

    The dependency check below fires on a MEASUREMENT — a declared `dep` edge whose branch is
    not an ancestor. The reported failure does not require one: four Crawlers were dispatched
    with the base named in the BRIEF\'s prose, which showrunner cannot read and must not
    pretend to. If those leaves carried no dep edge, every check in this file stays silent and
    the trees are still wrong.

    THE RULE IS THE REPORTER\'S OWN: defaulting to the primary checkout\'s HEAD "is defensible
    for a leaf off `main`; it is wrong the moment a campaign has more than one branch in
    flight." So the default stands where it is defensible and is refused where it is not, and
    the fix for a Crawler that genuinely should cut from the current branch is to SAY so.

    `--base HEAD` is the confirmation, not a bypass flag. It is the same commit the default
    would have used; what differs is that somebody typed it. That is the whole cost, and it is
    paid once per spawn rather than once per campaign, because the checkout can move between
    two spawns in the same run — which is exactly how #33 happened.

    CANNOT-TELL DOES NOT REFUSE. `default_branch` answers None for a repo with no origin/HEAD
    and no main or master, and a detached HEAD has no branch name to compare. Both warn and
    allow: blocking every spawn in an unusual repo would be a guard that is wrong by default,
    and the one thing worse than a silent wrong base is a tool nobody can run.
    """
    if report.get("explicit"):
        return
    default = worktree.default_branch(cfg)
    rc, head, _ = git(["symbolic-ref", "--short", "--quiet", "HEAD"], cwd=cfg.root)
    head = head.strip() if rc == 0 else ""
    if not default or not head:
        eprint("  %sBASE NOT CHECKED against the default branch: %s. Confirm it yourself.%s"
               % (YEL, "this repo has no origin/HEAD, main or master" if not default
                  else "the checkout is on a detached HEAD", OFF))
        return
    if head == default:
        return
    msg = ("%s would be cut from the primary checkout\'s HEAD, and this checkout is on %s, "
           "not %s.\n"
           "  base %s @ %s\n"
           "You did not name a base, so this is wherever the checkout happens to be pointing — "
           "which is right for a leaf off %s and wrong the moment a campaign has more than one "
           "branch in flight. The Crawler comes up on a tree whose files are all present and "
           "all older, finds the problem it was sent to fix is not there, and can close as "
           "PREMISE REFUTED with evidence that is true of the tree it was given.\n"
           "Name the base:\n"
           "    showrunner spawn %s --base <ref>\n"
           "or confirm the current checkout is what you mean:\n"
           "    showrunner spawn %s --base HEAD"
           % (leaf["id"], head, default, report.get("branch") or "?",
              (report.get("sha") or "?")[:12], default, leaf["id"], leaf["id"]))
    if rehearsing:
        eprint("%sWOULD REFUSE: %s%s" % (RED, msg, OFF))
        return
    die(msg, code=3)


def _base_dependency_check(leaf, report, despite, rehearsing=False):
    """REFUSE a base that definitely lacks work this leaf declares it needs (#73).

    #33 built the detection and printed it. #73 is the same failure again — four Crawlers, one
    run, every tree cut from an unrelated branch the primary checkout happened to be on — which
    is the evidence that a printed line was not enough. It printed AFTER the worktree, the
    branch, the brief and the claim existed, and with `--launch`, after the Crawler was already
    running. One Crawler caught it by hand and held; the rest had no reason to look.

    WHY THIS IS WORTH A REFUSAL AND MOST THINGS ARE NOT. The wrong tree is silent and plausible:
    the worktree exists, the branch exists, the code compiles, and every file the brief names is
    present, just older. A Crawler that does not think to run `git log -1` finds the function it
    was sent to fix, finds it does not have the problem described, and reports PREMISE REFUTED
    with real evidence — `grep` returning zero matches, every word of it true of the tree it was
    given and false of the tree under review. That is the most expensive wrong answer this tool
    can produce, because refuted is a legitimate close and reads as a successful run.

    ONLY ON `missing`, never on `unknown`. Missing is a measurement: the dependency has a branch
    and it is not an ancestor of this base. Unknown means the graph could not be read — a
    backend that cannot list dependencies, or a dependency never spawned — and refusing there
    would block work on the strength of not having looked. It stays a warning, which is the same
    split `_print_base` already makes.

    THE OVERRIDE NAMES WHAT IT OVERRIDES, exactly as `--despite-live` does and for the reason
    given there: a bare `--force` is answered reflexively and teaches the next session that the
    guard is a speed bump. Naming a dependency that is NOT missing is refused too, because an
    override copied from an earlier command is not a decision.
    """
    missing = report.get("missing") or []
    named = set(despite or [])
    unmatched = named - {d for d, _ in missing}
    if unmatched:
        die("--despite-base named %s, which is not missing from this base.\n"
            "An override copied from an earlier command is not a decision. Missing here: %s"
            % (", ".join(sorted(unmatched)),
               ", ".join(d for d, _ in missing) or "(nothing)"), code=2)
    uncovered = [(d, b) for d, b in missing if d not in named]
    if not uncovered:
        if named:
            print("%sACCEPTED A BASE MISSING A DEPENDENCY on purpose (--despite-base %s)%s"
                  % (YEL, ", ".join(sorted(named)), OFF))
        return
    detail = "\n".join("  %s (%s) is NOT an ancestor of %s @ %s"
                       % (d, b, report.get("branch") or "?", (report.get("sha") or "?")[:12])
                       for d, b in uncovered)
    origin = ("the base you named" if report.get("explicit")
              else "the PRIMARY CHECKOUT'S HEAD, which is where an unrelated checkout happens "
                   "to be pointing — you did not name a base")
    msg = ("%s would be cut from a base that is missing work it depends on:\n%s\n"
           "  That base is %s.\n"
           "The Crawler comes up without that work in history, finds the prerequisite absent, "
           "and can correctly report a complete honest outcome that is half the item — with "
           "every gate green.\n"
           "Cut from a ref that contains it:\n"
           "    showrunner spawn %s --base %s\n"
           "or accept it deliberately by naming what you are overriding:\n"
           "    showrunner spawn %s %s"
           % (leaf["id"], detail, origin, leaf["id"], uncovered[0][1], leaf["id"],
              " ".join("--despite-base %s" % d for d, _ in uncovered)))
    if rehearsing:
        # The rehearsal must preview the refusal and still create nothing, for the reason
        # `_live_collision_check` gives: a dry run that shows a clean dispatch and then a real
        # spawn that refuses is worse than no rehearsal.
        eprint("%sWOULD REFUSE: %s%s" % (RED, msg, OFF))
        return
    die(msg, code=3)


def cmd_spawn(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    leaf = g.show(args.id)
    decision = lanes.route(cfg, leaf)
    lanes.log(cfg, [decision])

    if not decision["matched"]:
        eprint("%sNOTE: %s%s" % (YEL, decision["why"], OFF))

    # A REHEARSAL THAT BUILDS THE ROOM POISONS THE RUN IT REHEARSED. `--dry-run` documented
    # itself as "see the command and start nothing", and started no SESSION while still
    # creating the worktree, the branch, the scratch dir and the claim — so the honest first
    # move (rehearse, read the argv, then launch) left the room already built and the real
    # spawn refused it. The preview now previews.
    if getattr(args, "dry_run", False) and not getattr(args, "launch", False):
        die("--dry-run applies to --launch; it shows the command a launch WOULD run.\n"
            "Without --launch there is nothing to rehearse.", code=64)

    # AFTER the argument check and BEFORE anything is created, in both the real path and the
    # rehearsal. A dry run must be able to preview the refusal it would hit — a rehearsal that
    # shows a clean dispatch and then a real spawn that refuses is worse than no rehearsal.
    _live_collision_check(cfg, g, leaf, getattr(args, "despite_live", None) or [],
                          rehearsing=bool(getattr(args, "dry_run", False)))
    # COMPUTED ONCE, HERE, so the refusal and the rehearsal read the SAME report. It used to be
    # computed twice — once inside the dry-run branch and once after it — which is two callers
    # of one check, free to drift apart, and the drift would be invisible because both look
    # right in isolation.
    base_seen = worktree.base_report(cfg, g, leaf, args.base or "HEAD",
                                     explicit=args.base is not None)
    # MAY THIS SEAT DISPATCH AT ALL (#77)? Asked at the SANCTIONED path, not only at the raw one.
    # `dispatch guard` watches `claude -p` — which `whoami` tells you not to use — while
    # `spawn --launch`, which it tells you to use two lines earlier, never asked. A seat
    # announcing "may dispatch: NOTHING" launched two Crawlers, and the operator found out half
    # an hour later when an unrelated Write was refused. A restriction enforced only on the
    # discouraged path restricts nothing.
    #
    # ONLY WHEN IT ACTUALLY DISPATCHES. Without `--launch`, spawn prepares a room and starts no
    # session, and `may_create` names what a role may START. Refusing the preparation too would
    # be a wider rule than the field says, invented here rather than declared by the operator.
    if getattr(args, "launch", False):
        # DISCOVERED, like every other session lookup here. This one was missed when the
        # rest were converted — it reads `SHOWRUNNER_SESSION` only, so the refusal it
        # prints said `session ?` for a session that is perfectly identifiable. A seat
        # check that cannot name the session it is refusing is harder to act on, and the
        # inconsistency is the kind that survives because four call sites looked right.
        _me = args.session or caller_session()
        _ok, _role, _seat, _ = dispatch.may_dispatch(cfg, _me)
        if not _ok:
            msg = ("%s would START a session, and this seat may not.\n"
                   "  role     %s (%s)\n"
                   "  session  %s\n"
                   "A seat announcing `may dispatch: NOTHING` was refusing only a raw "
                   "`claude -p`, so the path this tool tells you to use went unchecked.\n"
                   "Take a seat that may dispatch:\n"
                   "    showrunner role claim <role> --who <you>\n"
                   "or give this role a `may_create` naming what it may start. Roles are yours "
                   "to define — showrunner checks the shape, never the meaning.\n"
                   "`showrunner spawn %s` without --launch prepares the room and starts nothing."
                   % (leaf["id"], _role, _seat or "unresolved",
                      short_session(_me) if _me else "?", leaf["id"]))
            if getattr(args, "dry_run", False):
                eprint("%sWOULD REFUSE: %s%s" % (RED, msg, OFF))
            else:
                die(msg, code=3)

    _implicit_base_check(cfg, leaf, base_seen,
                         rehearsing=bool(getattr(args, "dry_run", False)))
    _base_dependency_check(leaf, base_seen, getattr(args, "despite_base", None) or [],
                           rehearsing=bool(getattr(args, "dry_run", False)))
    if getattr(args, "dry_run", False):
        session = args.session or dispatch.new_session_id()
        model = dispatch.resolve_model(cfg, decision)
        cmd = dispatch.build_command(cfg, {"crawler": worktree.crawler_name(leaf["id"], args.actor)},
                                     model, session, "<brief>")
        print("%sDispatch (dry run — NOTHING created: no worktree, branch, scratch, brief or "
              "claim)%s" % (BOLD, OFF))
        print("  lane     %s" % decision["lane"])
        # IN THE REHEARSAL TOO, and this is the half that matters: the dry run existed to show
        # the operator what a launch would do, and it showed everything except the input that
        # decides correctness.
        _print_base(base_seen)
        print("  model    %s" % (model or "(inherited)"))
        print("  command  %s" % " ".join(cmd))
        return 0

    record = worktree.spawn(cfg, leaf, actor=args.actor, base=args.base or "HEAD",
                            branch=args.branch)
    # THE ROOM IS OPENED HERE, BEFORE THE BRIEF, and that ordering is the whole fix. The
    # channel still has to be named before the brief is written — a room the agent is never
    # told about is one it never joins, indistinguishable from one that was never opened — but
    # naming was all this used to do. `channel_for` returns a name whenever chat is enabled,
    # provisioning ran later inside `launch`, and the brief was authored from the name: four
    # Crawlers in one campaign read "the orchestrator opened a channel for you" out of a spawn
    # whose own report said `chat not wired: channel not opened`.
    #
    # THE BRIEF IS AUTHORED ONCE, which is why provisioning moved rather than the rendering.
    # `brief.write` puts this text on disk at BRIEF.md and `dispatch.launch` hands the SAME
    # string to the process as its prompt, so letting `launch` patch the block in afterwards
    # would mean two writers of one file and a window in which the version a Crawler can read
    # is the lying one — a window a failed launch leaves behind permanently, since a parked
    # leaf keeps its worktree. Opening first costs a reorder; there is nothing to patch.
    # Generated here, before the claim, so the claim, the campaign record and the process all
    # name one session. Reading it back from a launched process would leave a window in which
    # the claim names nothing and a live agent cannot be reaped.
    #
    # AND NOW BEFORE THE ROOM, TOO (#78). llm_chat keys room membership to the SESSION, so
    # joining the Crawler to its own room on its behalf is only possible once we know which
    # session it will be. This used to be generated after the brief was written, which put the
    # one fact the join needs on the wrong side of the join — the reason the asymmetry could
    # not simply have been fixed in place.
    session = args.session
    if getattr(args, "launch", False) and not session:
        session = dispatch.new_session_id()

    chat = (dispatch.open_channel(cfg, record, session=session)
            if getattr(args, "launch", False) else None)
    text = brief.build(cfg, leaf, record, decision,
                       orchestrator_findings=args.finding or None,
                       chat=chat)
    brief_path = brief.write(cfg, record, text)

    entry = campaign.record_spawn(cfg, record, pid=args.pid, session=session)

    if not args.no_claim:
        g.claim(leaf["id"], args.actor, pid=args.pid, tree=record["worktree"], session=session)

    print("%sCrawler %s%s" % (BOLD, record["crawler"], OFF))
    print("  leaf     %s — %s" % (leaf["id"], leaf.get("title", "")))
    print("  lane     %s%s" % (decision["lane"],
                               " (resource %s)" % decision["resource"] if decision.get("resource") else ""))
    print("  worktree %s" % rel(record["worktree"], cfg.root))
    print("  branch   %s" % record["branch"])
    _print_base(base_seen)
    print("  scratch  %s" % rel(record["scratch"], cfg.root))
    for line in record["injected"]:
        print("  inject   %s" % line)
    for line in record.get("provisioned") or []:
        print("  harness  %s" % line)
    print("  brief    %s" % rel(brief_path, cfg.root))
    print("\n%sShares with siblings (a worktree isolates tracked files and nothing else):%s" % (BOLD, OFF))
    for item in record["shares"]:
        print("  - %s" % item["what"])
    if record.get("harness_gap"):
        eprint("\n%sHARNESS GAP: %s%s" % (YEL, record["harness_gap"], OFF))

    if getattr(args, "launch", False) or getattr(args, "dry_run", False):
        if record.get("harness_gap") and not args.dry_run:
            die("refusing to start a session in a tree with a harness gap: %s\n"
                "A Crawler without its own rails cannot gate its own commits, and under fan-out "
                "nobody is watching it." % record["harness_gap"], code=2)
        # A FAILED LAUNCH USED TO LEAVE THE CAMPAIGN MUTATED AND THE LEAF INVISIBLE. The
        # ordering is right — record first, then start — but nothing compensated when the start
        # failed. The leaf stayed `in_progress`, claimed by the invoking shell's pid, which is
        # dead seconds later: out of `ready`, so invisible to the only discovery surface, and
        # reachable only by a hand-written cleanup.
        #
        # PARK, DO NOT ROLL BACK. Reported with the reason: the tool's own advice was `reap`,
        # and on a real campaign `reap` proposed releasing the leaf AND closing a dozen chat
        # rooms belonging to another agent's Crawlers, because rooms with dead owners are swept
        # in the same pass. Following the printed remedy would have been destructive well
        # outside the failure. A park survives the reaper, keeps the worktree (which may hold
        # the only copy of real work — this repo surfaces, never deletes), and carries the
        # launch error as its reason so the next reader sees WHY rather than a bare stall.
        try:
            out = dispatch.launch(cfg, record, decision, text, session,
                                  dry_run=bool(getattr(args, "dry_run", False)),
                                  chat=chat)
        except Refused as exc:
            if not args.no_claim:
                try:
                    g.park(leaf["id"], "launch failed: %s" % exc)
                    eprint("%sPARKED %s%s — the claim survives `reap` and the leaf stays "
                           "visible. Its worktree is intact and is NOT removed: it may hold "
                           "work. `showrunner unpark %s` when the launch problem is fixed."
                           % (YEL, leaf["id"], OFF, leaf["id"]))
                except Exception as park_exc:  # noqa: BLE001
                    eprint("%sCOULD NOT PARK %s after the failed launch: %s%s\n"
                           "  The leaf is in_progress with a claim on a pid that is already "
                           "gone. `showrunner release %s` is the targeted fix; do NOT reach "
                           "for `reap`, which sweeps every dead owner in the campaign."
                           % (RED, leaf["id"], park_exc, OFF, leaf["id"]))
            raise
        # THE CLAIM'S LIVENESS MUST NAME THE SESSION, NOT THE SHELL. The claim is taken before
        # the process exists, so until now it recorded whichever shell ran `spawn` — which is
        # gone seconds later, making `reap --apply` release a leaf whose Crawler is still
        # working. Rebind it the moment the real pid is known.
        if out.get("launched") and out.get("pid") and not args.no_claim:
            g.rebind_claim(leaf["id"], out["pid"])
        print("\n%sDispatch%s" % (BOLD, OFF))
        print("  model    %s" % (out["model"] or "(inherited — no lane model declared)"))
        print("  session  %s" % out["session"])
        if out.get("channel"):
            print("  chat     %s" % out["channel"])
        elif out.get("chat"):
            eprint("  %schat     not wired: %s%s" % (YEL, out["chat"], OFF))
        if out["launched"]:
            print("  pid      %s" % out["pid"])
            print("  log      %s" % out["log"])
        else:
            print("  command  %s" % " ".join(out["cmd"][:2] + ["<brief>"] + out["cmd"][3:]))
            print("  (dry run — nothing started)")
    return 0


def cmd_edit(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    body = args.body
    if args.body_file:
        with open(args.body_file) as fh:
            body = fh.read()
    leaf = g.edit(args.id, title=args.title, body=body,
                  paths=(args.path.split(",") if args.path else None),
                  labels=(args.label.split(",") if args.label else None),
                  add_labels=(args.add_label or []), remove_labels=(args.remove_label or []))
    print("edited %s" % leaf["id"])
    print("  title %s" % leaf.get("title"))
    print("  body  %d chars" % len(leaf.get("body") or ""))
    print("  labels %s" % (", ".join(leaf.labels_list) or "(none)"))
    # Labels pick the LANE, so say what this leaf now matches rather than leaving the caller to
    # run `plan` to find out. An unmatched leaf falls to the default lane, which may own an
    # exclusive resource it never needed.
    _d = lanes.route(cfg, leaf)
    print("  lane   %s — %s" % (_d.get("lane"), _d.get("why")))
    if not _d.get("matched"):
        eprint("note: no lane rule matches this leaf, so it defaults to %s%s. An unmatched leaf "
               "is a missing rule, not a neutral outcome — and if that lane owns a resource, the "
               "leaf now queues for it."
               % (_d.get("lane"), (" (resource %s)" % _d.get("resource"))
                  if _d.get("resource") else ""))
    return 0


def cmd_brief(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    leaf = g.show(args.id)
    data = campaign.load(cfg)
    entry = next((c for c in data.get("crawlers", []) if c.get("leaf") == leaf["id"]), None)
    if not entry:
        die("no Crawler spawned for %s yet — run `showrunner spawn %s`" % (leaf["id"], leaf["id"]),
            code=2)
    entry = dict(entry)
    entry["worktree"] = cfg.abspath(entry["worktree"])
    entry["scratch"] = cfg.abspath(entry["scratch"])
    entry["shares"] = worktree.audit_shared(cfg)
    print(brief.build(cfg, leaf, entry, lanes.route(cfg, leaf),
                      orchestrator_findings=args.finding or None))
    return 0


# --------------------------------------------------- lifecycle / integrate
def cmd_reap(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    actions, warnings = campaign.reap(cfg, g, base=args.base, apply=args.apply)
    for w in warnings:
        print("%swarn%s %s" % (YEL, OFF, w))
    if not actions:
        print("nothing to reap — every claim and lock has a live owner")
        return 0
    for a in actions:
        target = a.get("leaf") or a.get("resource") or a.get("crawler")
        print("%s%-8s%s %-24s %s" % (RED, a["kind"], OFF, target, a["why"]))
        print("         → %s" % a["action"])
    if not args.apply:
        print("\n%sdry run — re-run with --apply to reclaim.%s" % (DIM, OFF))
        print("Reclaim is deliberately loud: a leaf that was claimed and abandoned is evidence "
              "about the work or the harness, and a run that quietly re-queues it three times "
              "has learned nothing.")
    return 0


def _print_follow_up(cfg):
    """How fresh this report is, and whether anything will look again.

    A crawler report says what is true at the instant it runs, and nothing on it distinguished
    a reading taken thirty seconds ago from one taken yesterday — nor told the reader whether
    walking away was safe. Both belong at the TOP, before the verdicts, because they say how
    much the verdicts below are worth.
    """
    f = harness.follow_up(cfg)
    print("%-14s %s" % ("checked", stamp(now())))
    if f["last"]:
        verdict = "" if f["waiting"] is None else (
            " · it found this orchestrator %s" % ("WAITING on dispatched work"
                                                  if f["waiting"] else "not waiting"))
        print("%-14s %s%s" % ("last re-check", stamp(f["last"]), verdict))
    if f["scheduled"]:
        # NO TIME, ON PURPOSE. The harness's watchdog fires on IDLE, and its porcelain answer
        # carries no interval — so a "next follow-up at HH:MM" here would be a number this
        # layer invented about an event it does not schedule. Naming the trigger is the true
        # statement; naming a time would be the convincing wrong one.
        print("%-14s when this orchestrator next goes idle, via %s's watchdog (it does not "
              "publish an interval, so this is a trigger, not a time)"
              % ("next re-check", f["harness"]))
    else:
        print("%s%-14s NONE SCHEDULED — %s%s" % (YEL, "next re-check", f["why"], OFF))


def cmd_reconcile(args):
    cfg = _cfg(args)
    findings = campaign.reconcile(cfg, _graph(cfg), base=args.base)
    if args.json:
        print(json.dumps(findings, indent=2, sort_keys=True))
        return 0
    _print_follow_up(cfg)
    if not findings:
        print("no Crawlers on record")
        return 0
    # IDLE DRIFT IS SUMMARISED, NOT ENUMERATED (#66). Measured in one real campaign: 48 trees
    # carrying a harness, 42 drifted, ZERO live. Forty-two identical lines is how the one that
    # matters gets scrolled past — and the reader who learns to skim this block skims it on the
    # night a LIVE tree drifts.
    #
    # The line stays present as a count, because dropping it entirely would make "no idle drift"
    # and "idle drift not reported" the same output, which is the collapse this repo spends its
    # time removing.
    idle_drift = [f for f in findings if f["verdict"].startswith("harness drifted (idle)")]
    if idle_drift:
        findings = [f for f in findings if f not in idle_drift]
        print("%s%d idle tree(s) are harness-drifted%s — no live holder, so no gate of theirs can "
              "refuse anybody. Harmless until resumed; re-provision then. (%s)"
              % (DIM, len(idle_drift), OFF,
                 ", ".join(sorted(f["crawler"] for f in idle_drift)[:4])
                 + (" …" if len(idle_drift) > 4 else "")))

    for f in findings:
        # `MERGED — ` WITH ITS DASH, not the bare prefix. "MERGED, BUT THE TREE IS NOT CLEAN"
        # also starts with MERGED, and colouring it green would restore in the terminal exactly
        # the reassurance the sentence was written to withdraw.
        colour = RED if f["verdict"].startswith(("ABANDONED", "NEVER COMMITTED")) else (
            GRN if f["verdict"].startswith(("MERGED — ", "LIVE")) else YEL)
        print("%s%-28s%s %s" % (colour, f["crawler"], OFF, f["verdict"]))
        print("    leaf %s (%s) · branch %s%s" % (
            f["leaf"], f["leaf_status"], f["branch"], "" if f["branch_exists"] else " [gone]"))
        if f.get("uncommitted_unknown"):
            print("    %sCOULD NOT READ %s — git failed there, so whether it holds uncommitted "
                  "work is UNKNOWN%s" % (YEL, f["worktree"], OFF))
        elif f["uncommitted"]:
            print("    %d uncommitted change(s) in %s — inspect before deleting anything"
                  % (len(f["uncommitted"]), f["worktree"]))
        if f["scratch_files"]:
            print("    scratch holds %d file(s): %s" % (len(f["scratch_files"]),
                                                        ", ".join(f["scratch_files"][:5])))
        # THE SECOND FACT, BESIDE LIVENESS — and until now nothing printed it. `reconcile` has
        # computed `session_health` since #69 and NO caller read it: not this printer, not
        # `waiting`, not `status`. A detector built because "a live PID is not a working agent"
        # was rendering its verdict to nobody, directly beneath a green `LIVE — do not disturb`.
        #
        # ERRORED AND QUIET ARE THE WHOLE POINT, so they are the ones that print. `producing` is
        # left silent deliberately: a line on every healthy Crawler is how the one that matters
        # gets scrolled past, which is the same argument the idle-drift summary above makes.
        _h = f.get("session_health") or {}
        if _h.get("verdict") == "errored":
            print("    %sSITTING ON AN ERROR%s — its log carries %s. The pid is alive and the "
                  "work is not moving; read %s" % (RED, OFF, ", ".join(_h.get("errors") or []),
                                                   _h.get("log")))
        elif _h.get("verdict") == "quiet":
            print("    %sPRODUCED NOTHING%s — %s is empty. A dispatched session that has written "
                  "no output is not the same as one working quietly, and `LIVE` above is only "
                  "about the pid." % (YEL, OFF, _h.get("log")))
        if f.get("harness_mis_certified"):
            # Retrospective and unrecoverable elsewhere: the harness verb is stateless, so once
            # a branch is merged nothing can be asked about the tree it was merged from.
            eprint("    %sMIS-CERTIFIED: a harness before game_loop #66 called this tree clean, "
                   "exit 0. Check whether this branch was ever integrated.%s" % (YEL, OFF))
    return 0


def cmd_amend(args):
    """Correct the verdict on a leaf that is already closed.

    A correction is an assertion, so it owes evidence exactly as the close did — an amended
    verdict with nothing behind it is the same wish as an unsourced 'done', and this verb exists
    precisely because the FIRST verdict was confident and wrong.
    """
    cfg = _cfg(args)
    g = _graph(cfg)
    # SAME JOIN, SAME DEFECT as the close gate had: a session standing in a worktree supplies
    # this path, so resolving it against the main checkout reads a different file of the same name.
    path, ev_root = resolve_from_caller(cfg, args.evidence)
    if not os.path.exists(path):
        die("--evidence names a path that does not exist: %s%s"
            % (args.evidence, "" if not ev_root else " (resolved against %s)" % ev_root), code=2)
    if os.path.isfile(path) and os.path.getsize(path) == 0:
        die("--evidence names an empty file: %s" % args.evidence, code=2)
    before = g.show(args.id)
    leaf = g.amend(args.id, args.premise, args.reason, rel(path, cfg.root))
    events.emit(cfg, "leaf.amended", {"leaf": args.id, "premise": args.premise,
                                      "was_outcome": before.get("outcome"),
                                      "outcome": leaf.get("outcome"),
                                      "evidence": rel(path, cfg.root)})
    print("amended %s — premise is now %r (outcome %s)"
          % (args.id, args.premise, leaf.get("outcome")))
    print("  the original close and its proof are kept; the correction is appended beneath them")
    if args.premise in ("holds", "partial"):
        eprint("\nNOTE: this corrected the RECORD and queued nothing. A verdict moving off "
               "`refuted` means real work was missed — give it its own leaf with `showrunner "
               "add`, or it exists only in this reason string.")
    return 0


def cmd_overlap(args):
    """What in-flight branches have actually changed in common (#30)."""
    cfg = _cfg(args)
    branches = args.branches
    if not branches:
        # Default to every branch the campaign knows and has not integrated: the question is
        # almost always "what is in flight right now", and making somebody type them is how a
        # check gets skipped on the day it matters.
        data = campaign.load(cfg)
        branches = [c["branch"] for c in data.get("crawlers", [])
                    if c.get("branch") and campaign.branch_exists(cfg, c["branch"])
                    and not campaign.is_merged(cfg, c["branch"], args.base)]
    if len(branches) < 2:
        print("nothing to compare — %d in-flight branch(es). This is a real answer, not an "
              "empty one: with fewer than two branches there is no cross-branch overlap to "
              "have." % len(branches))
        return 0
    result = collide.overlap(cfg, branches, base=args.base)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if result["unresolvable"]:
        eprint("%sCOULD NOT RESOLVE%s: %s — no merge-base against %s. These were NOT compared, "
               "which is different from having no overlap."
               % (RED, OFF, ", ".join(result["unresolvable"]), result["base"]))
    if not result["overlaps"]:
        print("no overlap across %d branch(es) against %s"
              % (len(result["branches"]), result["base"]))
        return 0
    for ov in result["overlaps"]:
        print("%sOVERLAP%s  %s  <->  %s" % (YEL, OFF, ov["a"], ov["b"]))
        for f in ov["files"][:20]:
            print("    both edit %s" % f)
        for f in ov["add_add"]:
            print("    %sADD/ADD%s  %s  — both CREATE it; git cannot merge this"
                  % (RED, OFF, f))
    eprint("\nFound before dispatch this is a one-line brief change (\"extend that file, do "
           "not create it\"). Found at merge time it is a hand-reconciliation.")
    return 2 if any(o["add_add"] for o in result["overlaps"]) else 0


def cmd_snapshot(args):
    """The world as it is, in one call. JSON on stdout.

    A viewer attaching to a campaign needs the current state before the stream means anything —
    an event saying `leaf.closed` is not a picture, it is a delta against one. Today that picture
    costs four calls (`status`, `reconcile --json`, `waiting --porcelain`, `plan --json`), each
    of which opens the graph and re-reads the record separately, so a fan-out landing between
    them hands the viewer a composite of two different instants that never existed.

    WHAT THIS DOES NOT PROMISE. It is not a transaction. Nothing here freezes the graph, and a
    Crawler can close a leaf while this is being assembled — so the parts can still disagree by
    milliseconds. What it removes is the four-round-trip window and the reader's belief that
    they were looking at one moment. The `cursor` is the honest join: it names the last event
    this snapshot could have seen, so a viewer can `watch --since` that and know exactly which
    events are already reflected here rather than guessing an overlap.
    """
    cfg = _cfg(args)
    g = _graph(cfg)
    ready = g.ready()
    in_prog = [x for x in g.list(status=G.IN_PROGRESS) if not x.is_epic]
    findings = campaign.reconcile(cfg, g, base=args.base)
    is_waiting, waiting_detail = campaign.waiting(cfg, g, base=args.base)
    _, _, unreadable = events.read(cfg, limit=1)
    backlog, _, _ = events.read(cfg)
    seq = backlog[-1]["seq"] if backlog else 0

    ls = locks.LockSet(cfg)
    resources = []
    for name in ls.names():
        state, h = locks.Lock(ls.root, name).state()
        resources.append({"resource": name, "state": state,
                          "holder": (h or {}).get("who"), "pid": (h or {}).get("pid")})

    snap = {
        "project": cfg.project_name,
        "instance": events.instance_id(cfg),
        "cursor": events.cursor(cfg, seq),
        "at": now(),
        # A journal this cannot read is NOT an idle campaign, and a snapshot that omitted the
        # distinction would be the exact failure `watch` refuses over.
        "journal_unreadable": unreadable,
        # HOW FRESH THIS IS, AND WHETHER ANYTHING WILL LOOK AGAIN. `at` above says when the
        # snapshot was taken; a viewer also needs to know whether walking away is safe, and
        # nothing here answered that. Carries no interval on purpose — see harness.follow_up.
        "follow_up": harness.follow_up(cfg),
        "ready": [{"id": x["id"], "title": x.get("title", ""),
                   "lane": lanes.route(cfg, x)["lane"]} for x in ready],
        "in_progress": [{"id": x["id"], "actor": x.get("actor"),
                         "parked": bool(x.get("parked"))} for x in in_prog],
        "crawlers": [{"crawler": f["crawler"], "leaf": f["leaf"], "branch": f["branch"],
                      "verdict": f["verdict"], "alive": f["alive"], "blocked": f["blocked"],
                      "harness": f["harness"]} for f in findings],
        "resources": resources,
        # Same finding, machine-readable. A viewer showing a quiet campaign over two sessions
        # still polling is the exact picture #29 describes.
        "lingering": campaign.lingering_crawlers(cfg),
        "waiting": {"waiting": is_waiting,
                    "live": len(waiting_detail["live_crawlers"]),
                    "parked": len(waiting_detail["parked_crawlers"]),
                    "blocked": len(waiting_detail.get("blocked_crawlers") or [])},
    }
    print(json.dumps(snap, indent=2, sort_keys=True, default=str))
    return 0


def cmd_watch(args):
    """Replay the event journal, then follow it. One JSON object per line, on stdout.

    THE READ SIDE OF THE BOUNDARY. A viewer must never tail `.showrunner/events.jsonl` itself:
    that file's name, location and rotation are showrunner's private business, and a consumer
    that reaches past this verb is the coupling `harness.py` deleted a hardcoded rule list to
    stop having. It asks; showrunner answers.

    REPLAY FIRST, ALWAYS. A viewer that attaches to a running campaign and sees nothing until
    the next transition cannot tell a quiet orchestrator from a broken pipe — and the quiet one
    is normal, since orchestration is bursty by nature. `--since <seq>` is how a reconnecting
    viewer asks for only what it missed, which is the same question with a different starting
    point rather than a different mode.

    EVERY FRAME CARRIES ITS OWN TYPE, including the ones that are not events: `ready` marks the
    end of replay, `heartbeat` proves the stream is alive during a quiet stretch, and `bye`
    marks a clean end. A stream that simply stops is indistinguishable from one that died, so
    the absence of `bye` is the signal — never end-of-file.
    """
    cfg = _cfg(args)
    # A CURSOR NAMES ITS INSTANCE; a bare integer does not. Several showrunners in several places
    # is the shape this whole surface exists for, and `seq` counts within one journal — so a
    # viewer holding two streams could resume one from the other's position, with both values
    # integers, the comparison succeeding, and no symptom anywhere.
    since, err = events.parse_cursor(cfg, args.since)
    if err:
        die(err, code=2)
    backlog, bad, unreadable = events.read(cfg, since_seq=since, limit=args.limit)
    if unreadable:
        # NOT an empty replay. "I cannot see this campaign" and "this campaign has done nothing"
        # are different answers, and only one of them is safe to render as an idle dashboard.
        die("the event journal exists and could not be read (%s). Refusing to stream a clean "
            "empty replay over it: a viewer cannot tell that from a campaign that has done "
            "nothing, and would show an orchestrator mid-fan-out as idle."
            % events.path_for(cfg), code=2)
    for ev in backlog:
        print(json.dumps(ev, sort_keys=True), flush=True)
    seq = backlog[-1]["seq"] if backlog else (since or 0)
    # A torn or corrupt line is REPORTED rather than skipped: a viewer that silently drops them
    # shows a campaign with holes in it and calls that the truth.
    print(json.dumps({"type": "ready", "replayed": len(backlog), "unparseable": bad,
                      "seq": seq, "cursor": events.cursor(cfg, seq),
                      "instance": events.instance_id(cfg),
                      "project": cfg.project_name}, sort_keys=True), flush=True)
    if not args.follow:
        return 0

    import time
    last_beat = 0.0
    try:
        while True:
            fresh, bad, unreadable = events.read(cfg, since_seq=seq)
            for ev in fresh:
                print(json.dumps(ev, sort_keys=True), flush=True)
                seq = max(seq, ev.get("seq", seq))
            nowt = time.time()
            if nowt - last_beat >= args.heartbeat:
                last_beat = nowt
                # The journal is SPARSE by design — it records decisions, and an orchestrator can
                # spend twenty minutes integrating without making one. A view built on the journal
                # alone freezes exactly when the work is hardest, which is when somebody is most
                # likely to be watching it. The heartbeat carries the cheap live counters so the
                # picture keeps moving without inventing events that did not happen.
                # `unreadable` rides every heartbeat rather than only the attach: a journal
                # can become unreadable long after a viewer connected, and the stream would
                # otherwise go quiet in exactly the way a finished campaign does.
                print(json.dumps({"type": "heartbeat", "ts": int(nowt), "seq": seq,
                                  "cursor": events.cursor(cfg, seq),
                                  "dropped": events.dropped(),
                                  "unreadable": unreadable,
                                  "unparseable": bad}, sort_keys=True), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(json.dumps({"type": "bye", "seq": seq, "reason": "interrupted"}, sort_keys=True),
              flush=True)
        return 0


def cmd_waiting(args):
    cfg = _cfg(args)
    is_waiting, detail = campaign.waiting(cfg, _graph(cfg), base=args.base)
    if args.porcelain:
        # The porcelain branch returns through the same three-code path below, so a structured
        # consumer and a prose one cannot disagree about the verdict.
        print(json.dumps(detail, indent=2, sort_keys=True))
    else:
        if is_waiting:
            print("WAITING on %d live and %d parked Crawler(s):"
                  % (len(detail["live_crawlers"]), len(detail["parked_crawlers"])))
            for c in detail["live_crawlers"]:
                print("  live   %s (%s)" % (c["crawler"], c["leaf"]))
            for c in detail["parked_crawlers"]:
                print("  parked %s (%s) — %s" % (c["crawler"], c["leaf"], c["why"]))
        else:
            print("not waiting — no dispatched work has a live owner or an explicit park")
        # PRINTED ON BOTH BRANCHES, because a blocked Crawler is the reason this can say NOT
        # waiting while Crawlers are alive, and a verdict whose cause is invisible is one the
        # reader argues with. These are the sessions somebody has to go and prompt.
        for c in detail.get("blocked_crawlers") or []:
            # ON STDOUT AS WELL AS STDERR, because this line is the FINDING and the finding must
            # not depend on which stream a caller happened to capture. It stays on stderr too:
            # a human reading a terminal sees it in the same place as before, and the colour is
            # only worth carrying there.
            print("BLOCKED %s (%s) — %s. Alive and doing nothing; it needs a message, not time."
                  % (c["crawler"], c["leaf"], c["why"]))
            eprint("  %sBLOCKED%s %s (%s) — %s. Alive and doing nothing; it needs a message, "
                   "not time." % (YEL, OFF, c["crawler"], c["leaf"], c["why"]))

    # THREE STATES, NOT TWO (#35). This returned 0 for waiting and 1 for everything else, so a
    # blocked Crawler — counted as neither waiting nor parked — produced the SAME code as an
    # ordinary quiet campaign. A consumer writing `waiting || exit 0`, which is the natural way
    # to mean "if it cannot tell, do not act", then allowed in exactly the case it was built for.
    # That happened: a real stop gate, written against this verb, never fired once.
    #
    # 3 is chosen so the wrong reading becomes LOUD rather than staying quiet: a caller that
    # treats non-zero as "no" now gets a different number for the case it must not miss, and one
    # that only knew 0/1 sees an unexpected code instead of a false negative.
    if detail.get("blocked_crawlers"):
        return 3
    return 0 if is_waiting else 1


def cmd_baseline(args):
    cfg = _cfg(args)
    if not (cfg.get("checks") or []):
        die("no checks configured — a baseline of nothing proves nothing", code=2)
    stub = gates.unconfigured_checks(cfg)
    if stub:
        die("check(s) %s cannot fail, so a baseline of them proves nothing — the same refusal "
            "as no checks at all, and for the same reason. `init` ships a placeholder command; "
            "until it is replaced, `integrate` re-runs it after every merge and reports passing "
            "while measuring nothing. Point it at your real test command in .showrunner/"
            "config.json." % ", ".join(stub), code=2)
    data = gates.record_baseline(cfg)
    print("recorded baseline at %s" % rel(cfg.baseline_path, cfg.root))
    for c in data["checks"]:
        print("  %-16s rc=%-3s %d failure line(s) [%s]" % (c["name"], c["rc"], len(c["failures"]),
                                                           c["resolution"]))
    print("%sThe criterion from here is NO NEW FAILURES versus this, never 'all green' — a repo "
          "with pre-existing failures cannot satisfy 'all green', so that gate gets switched off "
          "on contact with a real codebase.%s" % (DIM, OFF))
    return 0


def cmd_check(args):
    cfg = _cfg(args)
    current = gates.run_checks(cfg)

    # VALIDITY IS A PRECONDITION, NOT A FINDING (#41). A run that could not reach the world did
    # not measure anything, so its failure count carries no information — and comparing it would
    # produce a confident, detailed verdict about code that was never exercised. Exit 3, its own
    # code, because folding it into 2 ("new failures") is exactly the substitution this gate
    # exists to refuse: the same number for "your code broke" and "nothing was measured".
    valid, void_report = gates.validity(cfg, current)
    if not valid:
        for line in void_report:
            eprint(line)
        eprint("VOID — nothing was compared. Exit 3, distinct from 2 (new failures) so a caller "
               "that treats non-zero as 'the code is bad' gets a code it did not map rather than "
               "a wrong answer it will believe.")
        return 3

    ok, report = gates.compare_to_baseline(cfg, current, gates.load_baseline(cfg))
    for line in report:
        print(line)
    if ok is None:
        return 1
    return 0 if ok else 2


def cmd_integrate(args):
    cfg = _cfg(args)
    results, ok = campaign.integrate(cfg, _graph(cfg), base=args.base, only=args.only or None,
                                     dry_run=args.dry_run)
    if not results:
        print("nothing to integrate — no closed Crawler branch is unmerged")
        return 0
    for r in results:
        colour = GRN if r["status"] in ("integrated", "would-merge") else RED
        print("%s%-12s%s %s (%s)" % (colour, r["status"], OFF, r["branch"], r["crawler"]))
        for line in r.get("report") or []:
            print("    %s" % line)
        if r.get("detail"):
            print("    %s" % r["detail"].replace("\n", "\n    "))
        if r.get("note"):
            print("    %s%s%s" % (YEL, r["note"], OFF))
    if not ok:
        eprint("\nSTOPPED on the first failure rather than stacking branches onto a broken trunk. "
               "A branch is integrated only when the checks pass on the MERGED result.")
        return 2
    integrated = [r for r in results if r["status"] == "integrated"]

    # THE TREE GOES WHEN THE WORK LANDS (#75), which is the moment it becomes provably
    # redundant: the branch is merged, so every commit survives, and `spawn` can recreate the
    # tree from it. Nothing removed one before — one reported checkout carried 178 trees and
    # 133 GB, of which ONE belonged to a live Crawler, and the cost that hurt was an AV suite
    # rescanning a duplicated monorepo at ~64% CPU on a machine reported as "running slow".
    #
    # AND EVERY BRIEF ALREADY PROMISED IT. brief.py tells each Crawler its tree is deleted once
    # the work integrates — the justification for the whole scratch-dir discipline. Leaving the
    # trees made that sentence false, so the rule survived on an argument that did not hold.
    #
    # Dirty, unknown and unmerged trees are held back and REPORTED, never removed; `reclaimable`
    # owns that discrimination. `--keep-trees` skips this entirely.
    if integrated and not getattr(args, "keep_trees", False):
        take, held = campaign.reclaimable(cfg, _graph(cfg), base=args.base)
        if take or held:
            print("\n%sWorktrees%s" % (BOLD, OFF))
            removed = []
            for r in take:
                try:
                    worktree.remove(cfg, r["crawler"])
                    removed.append(r)
                except SystemExit:
                    raise
                except Exception as exc:                        # noqa: BLE001
                    eprint("  %sFAILED%s %s: %s" % (RED, OFF, r["crawler"], exc))
            _report_reclaim(removed, held, True)

    if integrated:
        print("%sThese merges auto-committed, so no provenance declaration was needed: the "
              "harness's commit gate matches `git commit`, which a clean merge never runs. "
              "Declare only when you commit a resolution yourself.%s" % (DIM, OFF))
        proofs = [r["merged_proof"] for r in integrated if r.get("merged_proof")]
        if proofs:
            print("\n%sChecks on the MERGED result, written out so they can be cited:%s" % (BOLD, OFF))
            # SAY WHETHER THE EVIDENCE WILL TRAVEL. The integration record now names the
            # artifact, which closes the "which file proves this leaf" question for anyone
            # standing where the files are. It does not make the file survive a clone: consumers
            # gitignore these — reasonably, they are large and local — and then the record
            # arrives on another machine reading as a completed, proved leaf with nothing behind
            # it. Silent in the direction that matters, so it is said at the moment it is true.
            for p_ in proofs:
                tracked = campaign._is_tracked(cfg, p_)
                mark = ("" if tracked else
                        "   — LOCAL ONLY: git does not carry this, so the record will outlive it "
                        "on any other machine" if tracked is False else
                        "   — cannot tell whether git carries this")
                print("  %s%s" % (rel(p_, cfg.root), mark))
            print("%sA fix proved on a branch does NOT transfer: the harness scopes a proved fix "
                  "to the session that proved it, so a Crawler's proof cannot satisfy your "
                  "handback — by design. Branch-green is not trunk-green. If a leaf claimed a "
                  "fix, exercise that fix's own consumer against the merged trunk and prove it "
                  "here.%s" % (DIM, OFF))
    return 0


def cmd_integration_commit(args):
    cfg = _cfg(args)
    data = campaign.load(cfg)
    entries = []
    for name in args.crawler or []:
        entry = next((c for c in data.get("crawlers", [])
                      if c.get("crawler") == name or c.get("leaf") == name), None)
        if not entry:
            die("no Crawler on record named %r" % name, code=2)
        entries.append({"crawler": entry["crawler"], "branch": entry["branch"]})
    for br in args.branch or []:
        entries.append({"crawler": br, "branch": br})
    if not entries:
        die("name at least one --crawler or --branch whose work this commit lands", code=2)

    decl = gates.declare_integration(cfg, entries, base=args.base)
    print("declared %s" % rel(decl["path"], cfg.root))
    print("  crawlers: %s" % ", ".join(sorted(decl["crawlers"])))
    print("  expected %d file(s) from the merged Crawlers; %d staged"
          % (len(decl["expected_files"]), len(decl["staged_files"])))
    if decl["unexplained"]:
        eprint("%sUNEXPLAINED: %d staged file(s) no named Crawler ever touched:%s"
               % (RED, len(decl["unexplained"]), OFF))
        for p in decl["unexplained"][:20]:
            eprint("  %s" % p)
        eprint("This is the real orchestration failure the single-agent provenance check cannot "
               "see: not 'you didn't edit these' (you never do), but 'nobody did'.")
        return 2
    print("  %sok — every staged file is accounted for by a named Crawler%s" % (GRN, OFF))
    print("  trailer: %s" % gates.trailer(decl))

    att = gates.attribution(cfg, entries)
    if att:
        print("\n%sDeclare it to the harness so its provenance check asks the better question:%s"
              % (BOLD, OFF))
        print("  %s" % att["command"])
        print("  %sWHEN: %s%s" % (DIM, att["when"], OFF))
        print("  %sORDER: %s%s" % (DIM, att["order"], OFF))
    return 0


# ------------------------------------------------------------------ parser
def build_parser():
    p = argparse.ArgumentParser(prog="showrunner", description=__doc__)
    p.add_argument("--version", action=_LazyVersion, nargs=0,
                   help="show the version AND what commit names this code")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", help="write .showrunner/config.json and the worktree root")
    s.add_argument("--force", action="store_true")
    s.add_argument("--local", action="store_true",
                   help="register hooks in .claude/settings.local.json (UNTRACKED) rather than "
                        "settings.json — for a repo that keeps showrunner out of its history")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("doctor", help="validate config; refuses configurations that degrade silently")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("status", help="the campaign at a glance")
    s.set_defaults(func=cmd_status)

    # graph
    s = sub.add_parser("add", help="add a leaf")
    s.add_argument("title")
    s.add_argument("--id")
    s.add_argument("--body")
    s.add_argument("--body-file")
    s.add_argument("--kind", default="task", choices=["task", "epic"])
    s.add_argument("--label", action="append")
    s.add_argument("--path", action="append")
    s.add_argument("--after", action="append", help="leaf id that must close first")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("dep", help="declare that CHILD is blocked by PARENT")
    s.add_argument("child")
    s.add_argument("parent")
    s.add_argument("--remove", action="store_true",
                   help="remove the edge instead of adding it. An edge added on a reason that "
                        "does not survive scrutiny had no way back: `edit` takes title, body, "
                        "paths and labels but not dependencies, and a wrong parent HIDES the "
                        "leaf, because `ready` means unblocked.")
    s.set_defaults(func=cmd_dep)

    s = sub.add_parser("list", help="list leaves")
    s.add_argument("--status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("edit", help="correct a leaf's title, body or paths (the body IS the brief)")
    s.add_argument("id")
    s.add_argument("--title")
    s.add_argument("--body")
    s.add_argument("--body-file")
    s.add_argument("--path")
    s.add_argument("--label", help="REPLACE the label set (comma-separated). Labels pick the "
                                   "lane, so a typo queues the leaf against the default lane's "
                                   "resource")
    s.add_argument("--add-label", action="append", help="add one label, keeping the rest")
    s.add_argument("--remove-label", action="append",
                   help="remove one label; REFUSES if the leaf does not carry it")
    s.set_defaults(func=cmd_edit)

    s = sub.add_parser("show", help="show one leaf")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("ready", help="unblocked, unclaimed work — the only discovery entrypoint")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_ready)

    s = sub.add_parser("claim", help="claim a leaf (records owner liveness)")
    s.add_argument("id", nargs="?")
    s.add_argument("--next", action="store_true",
                   help="atomically take ANY ready leaf; exit 1 when none is free. Safe for "
                        "several orchestrators at once — losing a race means a sibling got "
                        "there first, which is the system working")
    s.add_argument("--actor", default="crawler")
    s.add_argument("--pid", type=int)
    s.add_argument("--tree")
    s.add_argument("--session")
    s.set_defaults(func=cmd_claim)

    s = sub.add_parser("release", help="release a claim back to ready")
    s.add_argument("id")
    s.add_argument("--reason")
    s.set_defaults(func=cmd_release)

    s = sub.add_parser("park", help="pause a claim (usage limit) — it survives the reaper")
    s.add_argument("id")
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_park)

    s = sub.add_parser("unpark", help="resume a parked claim under a new owner")
    s.add_argument("id")
    s.set_defaults(func=cmd_unpark)

    s = sub.add_parser("close", help="the proof-of-done gate")
    s.add_argument("id")
    s.add_argument("--proof")
    s.add_argument("--reason")
    s.add_argument("--refuted", action="store_true", help="the premise did not hold (a SUCCESS)")
    s.add_argument("--unreachable", action="store_true",
                   help="the premise HELD and nothing reaches the code — a third outcome, not a "
                        "kind of refuted. Needs --evidence naming the allowlist, caller or "
                        "enumeration that shows the gap")
    s.add_argument("--evidence", help="the file that refutes the premise")
    s.add_argument("--premise", choices=list(gates.PREMISE_VERDICTS))
    s.add_argument("--premise-read", help="the real file you checked the premise against")
    s.add_argument("--stale-proof-reason", help="why an artifact older than the claim is the proof")
    s.set_defaults(func=cmd_close)

    s = sub.add_parser("stop-gate", help="refuse a turn-end while YOUR claimed leaf is open")
    s.add_argument("--leaf", help="the leaf this caller holds; exact, and what spawn bakes in")
    s.add_argument("--tree", help="the caller's tree, matched against claim_tree "
                                  "(default: $GAME_LOOP_REPO, then cwd)")
    s.set_defaults(func=cmd_stop_gate)

    # locks
    s = sub.add_parser("lease", help="worktree leases — one session per tree")
    esub = s.add_subparsers(dest="leasecmd")
    t = esub.add_parser("status", help="what holds each worktree, and on what evidence")
    t.add_argument("tree", nargs="?")
    t.set_defaults(func=cmd_lease_status)

    s = sub.add_parser("worktree", help="the tree a session is standing in")
    wsub = s.add_subparsers(dest="worktreecmd")
    t = wsub.add_parser("enter", help="SessionStart: take this tree's lease, or report who holds it")
    t.add_argument("--session", help="the Claude Code session id (hooks supply it on stdin)")
    t.add_argument("--path", help="the tree to consider; defaults to the cwd")
    t.add_argument("--holder", help="a name for the holder; defaults to 'interactive'")
    t.set_defaults(func=cmd_worktree_enter)

    t = wsub.add_parser("fork", help="your own tree, off the same base as a held one")
    t.add_argument("--from", required=True, help="the held worktree to fork from")
    t.add_argument("--session", help="the Claude Code session id")
    t.add_argument("--base", help="override the base commit; refuses rather than guess one")
    t.add_argument("--name", help="a name for the new tree")
    t.set_defaults(func=cmd_worktree_fork)

    t = wsub.add_parser("guard", help="PreToolUse: deny a write into a tree another live "
                                      "session holds. Exit 2 denies")
    t.add_argument("--session", help="override the session id the payload carries")
    t.add_argument("--path", help="the path being written; defaults to the payload's")
    t.add_argument("--tool", help="the tool name; defaults to the payload's")
    t.add_argument("--command", help="a Bash command to judge, instead of reading stdin")
    t.set_defaults(func=cmd_worktree_guard)

    t = wsub.add_parser("register", help="register the hooks this tool owns in "
                                         ".claude/settings.json "
                                         "(idempotent). --local writes settings.local.json "
                                         "instead, for a repo that keeps showrunner untracked")
    t.add_argument("--local", action="store_true",
                   help="write .claude/settings.local.json (UNTRACKED) rather than "
                        "settings.json — the arrangement for a repo that keeps showrunner out "
                        "of its history, which doctor endorses and whose remedy used to write "
                        "the tracked file")
    t.set_defaults(func=cmd_worktree_register)

    s = sub.add_parser("self", help="pin showrunner's own code at a git ref, for a central "
                                    "install. exit 1 no pin there, 2 the pin cannot be trusted",
                       description=(
                           "EXIT CODES ARE THE ANSWER, and they are semantic:\n"
                           "  0  a pin is there and names the commit it holds\n"
                           "  1  NO pin at that path — the absence of one\n"
                           "  2  a pin IS there and cannot be trusted: its stamp is unreadable, "
                           "or VERSION\n     and the stamp DISAGREE, meaning the directory was "
                           "modified after pinning\n"
                           "\n"
                           "1 and 2 are different findings and must not be collapsed. 1 says "
                           "nothing is\ninstalled. 2 says something IS installed and no commit "
                           "describes it — which is the\nworse case, because the code is running "
                           "and its provenance is a guess.\n"
                           "\n"
                           "`self --dest X || install` is WRONG: it treats an untrustworthy pin "
                           "the same as a\nmissing one, and reinstalls over the evidence."),
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("--pin", help="the git ref to extract: a commit, tag or branch")
    s.add_argument("--dest", help="where the pinned checkout lands, or which one to report on")
    s.set_defaults(func=cmd_self)

    s = sub.add_parser("lock", help="single-consumer resource locks")
    lsub = s.add_subparsers(dest="lockcmd")

    t = lsub.add_parser("status")
    t.add_argument("resource", nargs="?")
    t.set_defaults(func=cmd_lock_status)

    t = lsub.add_parser("acquire")
    t.add_argument("resource")
    t.add_argument("--holder", default="manual")
    t.add_argument("--session")
    t.add_argument("--wait", type=float, default=0)
    t.set_defaults(func=cmd_lock_acquire)

    t = lsub.add_parser("release")
    t.add_argument("resource")
    t.add_argument("--pid", type=int)
    t.add_argument("--force", action="store_true")
    t.set_defaults(func=cmd_lock_release)

    t = lsub.add_parser("guard", help="PreToolUse shape: exit 2 blocks the tool call")
    t.add_argument("--session")
    t.add_argument("command", nargs=argparse.REMAINDER)
    t.set_defaults(func=cmd_lock_guard)

    t = lsub.add_parser("run", help="authoritative: acquire, exec, release — holder is the consumer")
    t.add_argument("resource")
    t.add_argument("--holder", default="run")
    t.add_argument("--session")
    t.add_argument("--wait", type=float, default=0)
    # nargs="*" AND NOT REMAINDER, and the `--` is split off before argparse ever sees it — see
    # `_split_trailing_command`. REMAINDER takes everything after the first positional, so
    # `lock run device --holder crawler-a -- ./deploy.sh` — the form documented in the README,
    # for the one hard rule this project exists to enforce — put `--holder` into the COMMAND,
    # left holder at its DEFAULT, and then tried to execute `--holder` as a program.
    #
    # "*" alone does not fix that, and the comment that stood here said it did. Argparse assigns
    # positionals in a single pass, so once an optional intervenes the trailing words cannot
    # reach `command` and fall out as unrecognized arguments — the same documented form, exiting
    # 2 with a top-level usage dump instead. Measured, both ways round:
    #
    #     "*"        device --holder X -- echo hi  -> holder='X'   command=[]  EXTRA=['--','echo','hi']
    #     REMAINDER  device --holder X -- echo hi  -> holder='run' command=['--holder','X','--',...]
    #
    # THE LIMIT WAS LIFTED, and what replaced it is narrower than the limit was. The bare form
    # `lock run device --holder X echo hi` now RUNS, because `_split_trailing_command` finds where
    # the command starts and cuts there — argparse still sees exactly one shape, the one above.
    #
    # What is REFUSED is now an option-like token argparse does not recognise, and the distinction
    # sits there rather than at "no `--`" because that is where the actual hazard is. Measured on
    # `parse_known_args`, the mechanism the old limit named and rejected:
    #
    #     device --holder X echo hi   -> holder='X'   cmd=[]  EXTRA=['echo','hi']
    #     device --hodler X echo hi   -> holder='run' cmd=[]  EXTRA=['--hodler','X','echo','hi']
    #
    # The second line IS the defect this argument already produced once: a typo becomes a lock
    # recorded against the DEFAULT holder, silently. So unrecognized option-like tokens are never
    # swallowed — they are left in the argv and argparse rejects them, loudly, as it always did.
    # Trailing POSITIONAL words are what the bare form was ever asking for, and those are separable
    # from flags; a word after the command's program name is the command's, a stray flag is not.
    t.add_argument("command", nargs="*")
    t.set_defaults(func=cmd_lock_run)

    # routing / planning
    s = sub.add_parser("route", help="show the lane each leaf routes to, and the rule that decided")
    s.add_argument("id", nargs="*")
    s.set_defaults(func=cmd_route)

    s = sub.add_parser("plan", help="group ready work into non-overlapping waves before fan-out")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_plan)

    # crawlers
    s = sub.add_parser("spawn", help="create a Crawler's worktree, scratch, injection and brief")
    s.add_argument("id")
    s.add_argument("--actor", default="crawler")
    # DEFAULT None, NOT "HEAD". `--base HEAD` is a confirmation that the checkout is what the
    # operator means, and with a "HEAD" default it was byte-identical to not passing anything —
    # so the guard asking for a decision could not see one being made.
    s.add_argument("--base", help="the ref to cut from; defaults to the primary checkout's HEAD, "
                                  "which is refused off the default branch unless named")
    s.add_argument("--branch")
    s.add_argument("--pid", type=int)
    s.add_argument("--session")
    s.add_argument("--no-claim", action="store_true")
    s.add_argument("--launch", action="store_true",
                   help="start a REAL Claude Code session in the worktree, with its own hooks")
    s.add_argument("--dry-run", action="store_true",
                   help="with --launch: show what would start, and start nothing")
    s.add_argument("--finding", action="append",
                   help="something you already checked; the Crawler is asked to confirm or refute it")
    s.add_argument("--despite-base", action="append", metavar="LEAF",
                   help="accept a base missing this dependency, naming which one (#73)")
    s.add_argument("--despite-live", action="append", metavar="LEAF",
                   help="accept a collision with a NAMED live leaf. Repeatable, and it must "
                        "name every colliding leaf — an override that does not say what it is "
                        "overriding is answered reflexively")
    s.set_defaults(func=cmd_spawn)

    s = sub.add_parser("brief", help="print a Crawler's brief")
    s.add_argument("id")
    s.add_argument("--finding", action="append")
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("reap", help="reclaim claims and locks whose owners are dead")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--base", default="HEAD")
    s.set_defaults(func=cmd_reap)

    s = sub.add_parser("reconcile", help="on resume: which branch is merged, abandoned, or live")
    s.add_argument("--base", default="HEAD")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_reconcile)

    s = sub.add_parser("amend", help="correct the VERDICT on a leaf that is already closed — "
                                     "the inverse of `edit`, which refuses a closed leaf")
    s.add_argument("id")
    s.add_argument("--premise", required=True,
                   help="the corrected verdict: holds|partial|refuted|unverifiable")
    s.add_argument("--reason", required=True, help="what changed your conclusion")
    s.add_argument("--evidence", required=True,
                   help="the real file behind the correction — a correction is an assertion too")
    s.set_defaults(func=cmd_amend)

    s = sub.add_parser("overlap", help="what in-flight branches have ACTUALLY changed in common "
                                       "— measured from diffs, where `plan` estimates")
    s.add_argument("branches", nargs="*")
    s.add_argument("--base", default="HEAD")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_overlap)

    s = sub.add_parser("snapshot", help="the whole campaign in ONE call and one instant — what "
                                        "a viewer needs before a stream of deltas means anything")
    s.add_argument("--base", default="HEAD")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("watch", help="stream this campaign's events as JSON lines: replay, then "
                                     "follow. The read side of the observability boundary — a "
                                     "viewer asks this rather than tailing showrunner's files")
    s.add_argument("--follow", "-f", action="store_true", help="keep streaming after the replay")
    s.add_argument("--since", help="resume from a cursor (as handed back in `ready`) or a bare seq. A cursor from a DIFFERENT showrunner is refused, not silently used")
    s.add_argument("--limit", type=int, help="cap the replay to the last N events")
    s.add_argument("--interval", type=float, default=0.5, help="poll seconds while following")
    s.add_argument("--heartbeat", type=float, default=5.0,
                   help="seconds between heartbeat frames; the journal is sparse and a view "
                        "built on it alone freezes during long quiet work")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("whoami",
                       help="what this session IS — the seat is DERIVED from where it stands, "
                            "never declared, so there is no file a session can write to become "
                            "something else. Register on SessionStart AND PostCompact")
    s.add_argument("--session")
    s.add_argument("--porcelain", action="store_true",
                   help="the SAME resolution as data: seat, role, how, and the writes/"
                        "may_create/reports_to a guard has to enforce. Build a hook against "
                        "this instead of keeping a copy of the resolver — a copy is a second "
                        "statement of one policy and it will disagree. Branch on `enforced`; "
                        "exits non-zero if it could not resolve, so a parser can fail closed")
    s.set_defaults(func=cmd_whoami)

    s = sub.add_parser("role", help="role SEATS — claim one, give it back, or see who holds what. "
                                    "`claim` claims a LEAF; this claims a role")
    rsub = s.add_subparsers(dest="rolecmd")

    t = rsub.add_parser("claim", help="take an open seat (only a role declaring acquire=claim)")
    t.add_argument("role")
    t.add_argument("--seat", type=int, default=0)
    t.add_argument("--who")
    t.add_argument("--session")
    t.add_argument("--pid", type=int,
                   help="override the discovered session pid. The default DISCOVERS the "
                        "long-lived session process, because a seat keyed to a process that "
                        "exits when this call returns reports success and reads STALE forever "
                        "after")
    t.set_defaults(func=cmd_role_claim)

    t = rsub.add_parser("release", help="give a seat back")
    t.add_argument("role")
    t.add_argument("--seat", type=int, default=0)
    t.add_argument("--pid", type=int)
    t.add_argument("--force", action="store_true")
    t.set_defaults(func=cmd_role_release)

    t = rsub.add_parser("roster", help="every seat, its state, and the liveness basis of its "
                                       "holder — STALE is where a dead claim becomes visible")
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_role_roster)

    s = sub.add_parser("dispatch",
                       help="the dispatch seam. `dispatch guard` is a PreToolUse hook ON BASH "
                            "that refuses a raw `claude -p` from a session whose role may not "
                            "create one — the cheap path that skips the worktree, the lease, "
                            "the claim and the room")
    dsub = s.add_subparsers(dest="dispatchcmd", required=True)
    d = dsub.add_parser("guard", help="PreToolUse (Bash): refuse a raw headless dispatch. Exit 2 "
                                      "denies; every unknown allows and says it went unchecked")
    d.add_argument("--session")
    d.add_argument("--command", default=None,
                   help="check this command instead of reading a hook payload — for testing the "
                        "rule without constructing a PreToolUse event")
    d.set_defaults(func=cmd_dispatch_guard)

    s = sub.add_parser("waiting",
                       help="is this orchestrator legitimately waiting? exit 0 waiting, 1 not "
                            "waiting, 3 a Crawler is BLOCKED (alive and inert — needs a message, "
                            "not time). Build against --porcelain: three exit codes and two "
                            "streams are easy to combine wrongly, and every wrong way is quiet",
                       description=(
                           "EXIT CODES ARE THE ANSWER, and they are semantic:\n"
                           "  0  waiting on live dispatched work\n"
                           "  1  NOT waiting — nothing outstanding\n"
                           "  3  a Crawler is BLOCKED: alive and inert, needs a message not time\n"
                           "\n"
                           "3 is separate from 1 because it used to share it, and the case this "
                           "verb exists for\nproduced the same number as an ordinary quiet "
                           "campaign — a real stop gate written\nagainst it never fired once.\n"
                           "\n"
                           "BLOCKED prints on BOTH streams; the ordinary verdict is stdout only.\n"
                           "`waiting || exit 0` is WRONG and fails quiet: it collapses every "
                           "non-zero code,\nso the blocked case reads as 'nothing to wait for'. "
                           "Build on --porcelain."),
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("--base", default="HEAD")
    s.add_argument("--porcelain", action="store_true",
                   help="JSON on stdout — THE CONTRACT. The prose form splits its verdict across "
                        "stdout and its BLOCKED finding across both streams; build on this one")
    s.set_defaults(func=cmd_waiting)

    s = sub.add_parser("gc", help="remove worktrees whose branch is merged and whose tree is "
                                  "clean; dry-run by default")
    s.add_argument("--base", default="HEAD")
    s.add_argument("--apply", action="store_true", help="actually remove them")
    s.set_defaults(func=cmd_gc)

    s = sub.add_parser("reach", help="PreToolUse: name the mechanism for what a call reached "
                                     "for; advice only, never refuses")
    s.set_defaults(func=cmd_reach)

    s = sub.add_parser("baseline", help="record the current check results as the comparison point")
    s.set_defaults(func=cmd_baseline)

    s = sub.add_parser("check", help="run checks and compare: NO NEW FAILURES, not 'all green'. "
                                     "exit 0 clean, 1 no baseline, 2 NEW failures, 3 VOID",
                       description=(
                           "EXIT CODES ARE THE ANSWER, and they are semantic:\n"
                           "  0  no NEW failures against the baseline\n"
                           "  1  no baseline recorded — nothing to compare, so nothing measured\n"
                           "  2  NEW failures\n"
                           "  3  VOID: the run could not reach the world, so it measured nothing\n"
                           "\n"
                           "3 is separate from 2 deliberately. A run that could not reach the "
                           "world did not\nmeasure anything, and its failure count carries no "
                           "information — which is strictly\nWORSE than a degraded comparison, "
                           "because a degraded comparison is still about the\ncode. A caller "
                           "treating non-zero as 'the code is bad' gets a code it did not map\n"
                           "rather than a wrong answer it will believe.\n"
                           "\n"
                           "`check || <fail>` is WRONG: it collapses 1 and 3 — nothing measured — "
                           "into 2,\nwhich says the code is broken. VALID means 'no evidence the "
                           "world was unreachable',\nnever 'the environment was sound'."),
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("integrate", help="merge Crawler branches serially, checks after each")
    s.add_argument("--base")
    s.add_argument("--only", action="append")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--keep-trees", action="store_true",
                   help="do not reclaim merged, clean worktrees after integrating")
    s.set_defaults(func=cmd_integrate)

    s = sub.add_parser("integration-commit",
                       help="declare a merge commit's provenance: does the staged set match the "
                            "union of what the merged Crawlers edited?")
    s.add_argument("--crawler", action="append")
    s.add_argument("--branch", action="append")
    s.add_argument("--base", default="HEAD")
    s.set_defaults(func=cmd_integration_commit)

    return p


# PROSE THAT SURVIVES THE RUN NEEDS A WAY IN THAT THE SHELL CANNOT EDIT. A backticked word in a
# double-quoted argument is executed and removed before the program sees it; the command reports
# success and the loss is invisible on both sides. That happened to a release note in a sibling
# tool and the damaged text is permanent, because published state should not be rewritten to
# repair a word.
#
# These are the fields whose content OUTLIVES the command: a close reason is a decision's only
# human explanation and is read back off the record months later. `--body` already had a twin;
# nothing else did, and nothing bounded any of them.
#
# DERIVED, NOT ENUMERATED. The twins are generated by walking the parser, so an option added
# later gets one without anybody remembering — the same move game_loop used, and the reason a
# reader grepping for a specific twin name finds nothing and concludes wrongly that none exists.
PROSE_OPTS = ("reason", "title", "who", "stale-proof-reason", "note")

# Long prose on a command line is the shape that invites a heredoc, and a heredoc is where
# backticks live. Bounded so the refusal arrives before the damage rather than after it.
PROSE_MAX = 400


def _add_prose_twins(parser):
    """Give every prose option a `--<name>-file` sibling, wherever it appears."""
    seen = []
    for action in getattr(parser, "_subparsers", None) and parser._subparsers._group_actions or []:
        for name, sub in getattr(action, "choices", {}).items():
            have = {o for a in sub._actions for o in a.option_strings}
            for opt in PROSE_OPTS:
                flag = "--%s" % opt
                if flag in have and (flag + "-file") not in have:
                    sub.add_argument(flag + "-file", metavar="PATH",
                                     help="read %s from this file instead — no shell touches it, "
                                          "and prose over %d chars must come this way"
                                          % (flag, PROSE_MAX))
                    seen.append("%s %s" % (name, flag))
    return seen


def _resolve_prose(parser, args):
    """Fold `--x-file` into `--x`, and refuse prose too long to have survived the shell."""
    for opt in PROSE_OPTS:
        dest = opt.replace("-", "_")
        fdest = dest + "_file"
        path = getattr(args, fdest, None)
        if path:
            if getattr(args, dest, None):
                parser.error("--%s and --%s-file are two answers to one question; pass one"
                             % (opt, opt))
            try:
                with open(path) as fh:
                    setattr(args, dest, fh.read().strip())
            except OSError as exc:
                parser.error("--%s-file could not be read: %s" % (opt, exc))
        val = getattr(args, dest, None)
        if isinstance(val, str) and len(val) > PROSE_MAX and not path:
            parser.error("--%s is %d chars, over the %d-char limit for prose on a command line. "
                         "Use --%s-file: a long argument is where backticks and $(...) hide, and "
                         "the shell removes them before this program sees them — the command "
                         "then reports success and neither side can see what was lost."
                         % (opt, len(val), PROSE_MAX, opt))


def _trailing_command_node(parser, argv):
    """Resolve (node, index) for the subcommand `argv` names, if it takes a trailing command.

    Asked BEFORE parsing, and asked of the parser tree rather than assumed, because the answer
    decides whether `--` is stripped at all. Stripping it unconditionally would break every other
    subcommand's use of the POSIX separator — `showrunner add -- --starts-with-a-dash` would lose
    the one thing keeping its title from being read as a flag.

    `index` is where that subcommand's OWN arguments begin, which the bare-form split below needs
    and the `--` split does not. Returns (None, None) when the verb declares no `command`.

    Walks only tokens that ARE subparser choices, so a flag's value can never be mistaken for a
    subcommand name. Reads argparse's private structure; if that ever changes shape this returns
    (None, None) and `lock run`'s own assertions fail loudly, rather than the separator quietly
    going back to being dropped.
    """
    node, i = parser, 0
    for tok in argv:
        subs = next((a for a in node._actions
                     if isinstance(a, argparse._SubParsersAction)), None)
        if subs is None:
            break
        if tok in subs.choices:
            node = subs.choices[tok]
        i += 1
    if not any(a.dest == "command" for a in node._actions):
        return None, None
    return node, i


def _bare_command_start(node, argv, i):
    """Index in `argv` where the command words begin WITHOUT a `--`, or None if undecidable.

    This is the whole bare form: rather than teaching argparse a second way to reach `command`,
    find the split point ourselves and hand argparse an argv that ends before it — so the bare
    form and the `--` form converge on the ONE path, and anything this scan cannot decide falls
    through to argparse unchanged and is refused there.

    Fails CLOSED on purpose, at every branch that is not certain: an option shape it does not
    model, a positional it cannot count, an option-like token it does not recognise. The last of
    those is the point — `--hodler crawler-a` is not swallowed into the command here, it is left
    in the argv for argparse to reject as an unrecognized argument, which is where the loud
    failure this whole argument exists to preserve comes from.
    """
    opts = {}
    for a in node._actions:
        if a.option_strings:
            n = 1 if a.nargs is None else a.nargs
            if not isinstance(n, int):
                return None            # '?', '*', '+' on an OPTION — not modelled, so not guessed
            for s in a.option_strings:
                opts[s] = n
    slots = 0
    for a in node._actions:
        if a.option_strings or a.dest == "command":
            continue
        if a.nargs is None:
            slots += 1
        elif a.nargs == "?":
            slots += 1                 # may consume 0; over-counting only DELAYS the split point
        else:
            return None                # a variadic non-command positional has no split point
    j = i
    while j < len(argv):
        tok = argv[j]
        if tok.startswith("-") and tok != "-":
            n = opts.get(tok.split("=", 1)[0])
            if n is None:
                return None            # unrecognized: leave it for argparse to refuse
            j += 1 + (0 if "=" in tok else n)
            continue
        if slots:
            slots -= 1
            j += 1
            continue
        return j
    return None                        # no command words at all — cmd_lock_run says so better


def _split_trailing_command(parser, argv):
    """Take the command words away from argparse, for the verbs that exec.

    Returns (argv_for_argparse, trailing_or_None). Argparse cannot parse
    `lock run <resource> --holder X -- <cmd...>` in one pass under any nargs — see the note on
    `lock run`'s `command` argument for the two measured failures — so the separator is honoured
    here and the words after it are handed back to the `command` positional after parsing.

    Without a `--`, the same is done at the split point `_bare_command_start` finds. From that
    point on every token is the command's, INCLUDING flags — `lock run device ./deploy.sh -v`
    gives `-v` to `./deploy.sh`, because a word that arrives after the program name is the
    program's in every shell. The one exception is a flag that is also one of THIS verb's own:
    `lock run device echo hi --holder X` would otherwise run under the default holder while
    looking like it named one, which is the silent mislabelled lock this parser has already
    shipped once. That refuses, and names the `--` that expresses it unambiguously.
    """
    node, i = _trailing_command_node(parser, argv)
    if node is None:
        return argv, None
    if "--" in argv:
        cut = argv.index("--")
        return argv[:cut], argv[cut + 1:]
    if not any(a.dest == "command" and a.nargs == "*" for a in node._actions):
        return argv, None              # REMAINDER already reaches `command` bare; leave it alone
    cut = _bare_command_start(node, argv, i)
    if cut is None:
        return argv, None
    own = {s for a in node._actions for s in a.option_strings}
    stolen = [t for t in argv[cut:] if t.split("=", 1)[0] in own]
    if stolen:
        parser.error("%s after the command words is ambiguous: it would be read as the command's, "
                     "leaving this lock recorded under the DEFAULT holder while looking like it "
                     "named one. Put it before the command, or use `--` to mark where the command "
                     "starts." % stolen[0])
    return argv[:cut], argv[cut:]


def main(argv=None):
    parser = build_parser()
    _add_prose_twins(parser)
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, trailing = _split_trailing_command(parser, argv)
    args = parser.parse_args(argv)
    if trailing is not None:
        args.command = list(getattr(args, "command", None) or []) + trailing
    _resolve_prose(parser, args)
    if not getattr(args, "func", None):
        parser.print_help()
        return 64
    try:
        return args.func(args)
    except Refused as exc:
        # A MARKER ON THE CHANNEL A FILTER READS. The refusal itself belongs on stderr and
        # stays there — but every consumer of this CLI is an agent, this repo's own brief
        # tells them to run `showrunner close ...`, and agents pipe stdout aggressively to
        # protect context. A refusal that appears only on stderr is, to `... | grep -i done`,
        # indistinguishable from a command that did nothing and succeeded.
        #
        # One line, so the filter cannot swallow the fact that something happened. Not the
        # message: duplicating it would double every refusal for the humans reading both
        # streams, and the point is to stop silence, not to repeat the explanation.
        print("showrunner: REFUSED (exit %d) — reason on stderr" % exc.code)
        eprint("%s%s%s" % (RED, exc, OFF))
        if exc.hint:
            eprint("  %s" % exc.hint.replace("\n", "\n  "))
        return exc.code
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
