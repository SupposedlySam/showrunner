"""showrunner CLI.

Every subcommand that can refuse does so with exit code 2 — the Claude Code PreToolUse
"deny the tool call" code — so `lock guard` and `stop-gate` work unchanged as hooks.
"""

import argparse
import json
import os
import sys

from . import __version__, brief, campaign, collide, config, gates, graph as G, lanes, locks, worktree
from .util import Refused, die, eprint, rel, run, slug

BOLD, DIM, RED, GRN, YEL, OFF = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    BOLD = DIM = RED = GRN = YEL = OFF = ""


def _cfg(args, required=True):
    cfg = config.load(required=required)
    return cfg


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
    "shared_state": []
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
            fh.write("# showrunner runtime state — not source\n"
                     "graph.db\ngraph.db-*\nlocks/\nscratch/\ncampaign.json\n"
                     "routing.jsonl\nbaseline.json\nintegration-commit.json\n")
    worktree.ensure_root(cfg)
    print("wrote %s" % rel(path, cfg.root))
    print("wrote %s" % rel(gi, cfg.root))
    print("created %s (gitignored — Crawler worktrees live INSIDE the repo, see issue #4)"
          % rel(cfg.worktree_root, cfg.root))
    print("\nNext: edit the config (resources, lanes, checks, inject), then `showrunner doctor`.")
    return 0


# ----------------------------------------------------------------- doctor
def cmd_doctor(args):
    cfg = _cfg(args, required=False)
    print("%sshowrunner %s%s  ·  repo %s" % (BOLD, __version__, OFF, cfg.root))
    print("config: %s%s" % (rel(cfg.path, cfg.root),
                            "" if os.path.exists(cfg.path) else "  (MISSING — run `showrunner init`)"))
    findings = cfg.validate()
    bad = 0
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

    gap = worktree.harness_gap(cfg)
    if gap:
        print("  %s %s" % (YEL + "warn " + OFF, gap))

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
    print(leaf_id)
    return 0


def cmd_dep(args):
    cfg = _cfg(args)
    _graph(cfg).dep(args.child, args.parent)
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
    cfg = _cfg(args)
    leaf = _graph(cfg).show(args.id)
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
    leaf = _graph(cfg).claim(args.id, args.actor, pid=args.pid, tree=args.tree, session=args.session)
    print("claimed %s as %s (pid %s)" % (leaf["id"], leaf.get("actor"), leaf.get("claim_pid")))
    return 0


def cmd_release(args):
    cfg = _cfg(args)
    _graph(cfg).release(args.id, args.reason or "released")
    print("released %s back to ready" % args.id)
    return 0


def cmd_park(args):
    cfg = _cfg(args)
    _graph(cfg).park(args.id, args.reason)
    print("parked %s — the claim survives; a Crawler at a usage limit is not dead." % args.id)
    return 0


def cmd_unpark(args):
    cfg = _cfg(args)
    _graph(cfg).unpark(args.id)
    print("unparked %s" % args.id)
    return 0


def cmd_close(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    leaf, notes = gates.close_gate(
        cfg, g, args.id, args.proof, args.reason, refuted=args.refuted,
        evidence=args.evidence, stale_proof_reason=args.stale_proof_reason,
        premise=args.premise, premise_read=args.premise_read)
    print("%s %s (%s)" % ("REFUTED" if args.refuted else "closed", leaf["id"], leaf.get("proof")))
    for n in notes:
        print("  %s%s%s" % (DIM, n, OFF))
    return 0


def cmd_stop_gate(args):
    cfg = _cfg(args)
    ok, msg = gates.stop_gate(cfg, _graph(cfg))
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


def cmd_lock_acquire(args):
    cfg = _cfg(args)
    lock = locks.LockSet(cfg).lock(args.resource)
    ok = lock.acquire(os.getpid(), args.holder, session=args.session, wait=args.wait)
    if not ok:
        _, h = lock.state()
        eprint("BLOCKED: %r held by pid %s (%s)" % (args.resource, h.get("pid"), h.get("who")))
        return 2
    print("ACQUIRED %s (pid %s — %s)" % (args.resource, os.getpid(), args.holder))
    eprint("NOTE: the holder recorded is THIS shell, which exits immediately. `lock run` is the "
           "authoritative path — there the holder is the consumer process itself.")
    return 0


def cmd_lock_release(args):
    cfg = _cfg(args)
    lock = locks.LockSet(cfg).lock(args.resource)
    if lock.release(pid=args.pid or os.getpid(), force=args.force):
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
        die("nothing to run — usage: showrunner lock run <resource> --holder <who> -- <cmd...>", code=64)
    if not lock.acquire(os.getpid(), args.holder, session=args.session, wait=args.wait):
        _, h = lock.state()
        eprint("BLOCKED: %r held by pid %s (%s). One consumer at a time."
               % (args.resource, h.get("pid"), h.get("who")))
        return 2
    try:
        import subprocess
        return subprocess.call(cmd, cwd=cfg.root)
    finally:
        lock.release(pid=os.getpid(), force=True)


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
    if args.json:
        print(json.dumps({
            "waves": waves,
            "serialized": [l["id"] for l in serialized],
            "estimates": {k: {"paths": sorted(v["paths"]), "shared": sorted(v["shared"]),
                              "basis": v["basis"]} for k, v in estimates.items()},
            "notes": notes,
        }, indent=2, sort_keys=True))
    return 0


# ------------------------------------------------------------------ spawn
def cmd_spawn(args):
    cfg = _cfg(args)
    g = _graph(cfg)
    leaf = g.show(args.id)
    decision = lanes.route(cfg, leaf)
    lanes.log(cfg, [decision])

    if not decision["matched"]:
        eprint("%sNOTE: %s%s" % (YEL, decision["why"], OFF))

    record = worktree.spawn(cfg, leaf, actor=args.actor, base=args.base, branch=args.branch)
    text = brief.build(cfg, leaf, record, decision,
                       orchestrator_findings=args.finding or None)
    brief_path = brief.write(cfg, record, text)
    entry = campaign.record_spawn(cfg, record, pid=args.pid, session=args.session)

    if not args.no_claim:
        g.claim(leaf["id"], args.actor, pid=args.pid, tree=record["worktree"], session=args.session)

    print("%sCrawler %s%s" % (BOLD, record["crawler"], OFF))
    print("  leaf     %s — %s" % (leaf["id"], leaf.get("title", "")))
    print("  lane     %s%s" % (decision["lane"],
                               " (resource %s)" % decision["resource"] if decision.get("resource") else ""))
    print("  worktree %s" % rel(record["worktree"], cfg.root))
    print("  branch   %s" % record["branch"])
    print("  scratch  %s" % rel(record["scratch"], cfg.root))
    for line in record["injected"]:
        print("  inject   %s" % line)
    print("  brief    %s" % rel(brief_path, cfg.root))
    print("\n%sShares with siblings (a worktree isolates tracked files and nothing else):%s" % (BOLD, OFF))
    for item in record["shares"]:
        print("  - %s" % item["what"])
    if record.get("harness_gap"):
        eprint("\n%sHARNESS GAP: %s%s" % (YEL, record["harness_gap"], OFF))
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


def cmd_reconcile(args):
    cfg = _cfg(args)
    findings = campaign.reconcile(cfg, _graph(cfg), base=args.base)
    if not findings:
        print("no Crawlers on record")
        return 0
    if args.json:
        print(json.dumps(findings, indent=2, sort_keys=True))
        return 0
    for f in findings:
        colour = RED if f["verdict"].startswith("ABANDONED") else (
            GRN if f["verdict"].startswith(("MERGED", "LIVE")) else YEL)
        print("%s%-28s%s %s" % (colour, f["crawler"], OFF, f["verdict"]))
        print("    leaf %s (%s) · branch %s%s" % (
            f["leaf"], f["leaf_status"], f["branch"], "" if f["branch_exists"] else " [gone]"))
        if f["uncommitted"]:
            print("    %d uncommitted change(s) in %s — inspect before deleting anything"
                  % (len(f["uncommitted"]), f["worktree"]))
        if f["scratch_files"]:
            print("    scratch holds %d file(s): %s" % (len(f["scratch_files"]),
                                                        ", ".join(f["scratch_files"][:5])))
    return 0


def cmd_baseline(args):
    cfg = _cfg(args)
    if not (cfg.get("checks") or []):
        die("no checks configured — a baseline of nothing proves nothing", code=2)
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
    return 0


# ------------------------------------------------------------------ parser
def build_parser():
    p = argparse.ArgumentParser(prog="showrunner", description=__doc__)
    p.add_argument("--version", action="version", version="showrunner %s" % __version__)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", help="write .showrunner/config.json and the worktree root")
    s.add_argument("--force", action="store_true")
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
    s.set_defaults(func=cmd_dep)

    s = sub.add_parser("list", help="list leaves")
    s.add_argument("--status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="show one leaf")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("ready", help="unblocked, unclaimed work — the only discovery entrypoint")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_ready)

    s = sub.add_parser("claim", help="claim a leaf (records owner liveness)")
    s.add_argument("id")
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
    s.add_argument("--evidence", help="the file that refutes the premise")
    s.add_argument("--premise", choices=list(gates.PREMISE_VERDICTS))
    s.add_argument("--premise-read", help="the real file you checked the premise against")
    s.add_argument("--stale-proof-reason", help="why an artifact older than the claim is the proof")
    s.set_defaults(func=cmd_close)

    s = sub.add_parser("stop-gate", help="refuse a turn-end while a claimed leaf is open")
    s.set_defaults(func=cmd_stop_gate)

    # locks
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
    t.add_argument("command", nargs=argparse.REMAINDER)
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
    s.add_argument("--base", default="HEAD")
    s.add_argument("--branch")
    s.add_argument("--pid", type=int)
    s.add_argument("--session")
    s.add_argument("--no-claim", action="store_true")
    s.add_argument("--finding", action="append",
                   help="something you already checked; the Crawler is asked to confirm or refute it")
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

    s = sub.add_parser("baseline", help="record the current check results as the comparison point")
    s.set_defaults(func=cmd_baseline)

    s = sub.add_parser("check", help="run checks and compare: NO NEW FAILURES, not 'all green'")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("integrate", help="merge Crawler branches serially, checks after each")
    s.add_argument("--base")
    s.add_argument("--only", action="append")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_integrate)

    s = sub.add_parser("integration-commit",
                       help="declare a merge commit's provenance: does the staged set match the "
                            "union of what the merged Crawlers edited?")
    s.add_argument("--crawler", action="append")
    s.add_argument("--branch", action="append")
    s.add_argument("--base", default="HEAD")
    s.set_defaults(func=cmd_integration_commit)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 64
    try:
        return args.func(args)
    except Refused as exc:
        eprint("%s%s%s" % (RED, exc, OFF))
        if exc.hint:
            eprint("  %s" % exc.hint.replace("\n", "\n  "))
        return exc.code
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
