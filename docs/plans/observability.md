# Plan — a live view over N showrunners, and their game_loops

**Status:** the *producer* half is built and pushed (`f6de1f6`). The server, the app and the
discovery layer are plan only. Nothing about revali, zonai or Flutter below is implemented here.

**Read against:** `~/dev/gents` (`packages/gents_server/routes/loop.controller.dart`,
`docs/architecture.md`, `docs/live-view.md`, `docs/harness-adapters.md`), `~/dev/zonai/README.md`,
and [`central-install.md`](central-install.md) — all on 2026-08-14. The gents citations are the
load-bearing ones: that project already made these mistakes and wrote down which.

---

## What this is for

Three tiers deep — `gents` → `game_loop` → `showrunner` — and the only way to see what a fan-out is
doing is to read files or ask a verb. Morgan needs a window. The shape everyone reaches for is
Flutter DevTools: a process opens a socket, a browser opens on it, the view is live.

The constraint that makes it interesting: **there is no single showrunner.** There are N, in N
repos, possibly on N machines, and any of them may be driving M Crawlers, each with its own
game_loop. A viewer is watching a forest.

---

## The producer half, which is built

`showrunner watch` streams JSON lines: replay the journal, mark `ready`, then follow. Every frame
carries `kind` (or `type`), `seq`, `ts`, `instance`, `pid`.

```
$ showrunner watch --follow
{"kind":"leaf.claimed","seq":2,"leaf":"gh-15","actor":"crawler-a",...}
{"type":"ready","replayed":2,"seq":2,"cursor":"fd4620f33271@2","project":"showrunner"}
{"type":"heartbeat","seq":2,"cursor":"fd4620f33271@2","dropped":0}
```

Four decisions in it that the server should not re-litigate, because each has a corpse:

- **A viewer never reads `.showrunner/` directly.** It asks this verb. The file's name and layout
  are showrunner's business; a consumer that reaches past the verb is the coupling `harness.py`
  deleted a hardcoded rule list to stop having.
- **Replay before follow, always.** A viewer that attaches and sees nothing cannot tell a quiet
  orchestrator from a broken pipe, and quiet is *normal* — twenty minutes of integrating writes no
  events.
- **A heartbeat, because the journal is sparse.** gents shipped a dashboard over a sparse journal
  and it froze during long build stretches, which is exactly when someone is watching
  (`docs/live-view.md`, and the user's own words quoted there).
- **Cursors name their instance.** `seq` counts within one journal. A bare integer crossing an
  instance boundary is a confident answer about a different campaign — both sides integers, the
  comparison succeeds, no symptom. `--since` refuses a cursor minted elsewhere.

### What it does NOT yet emit

Honest gaps, in the order a live view will miss them:

| Missing | Why it matters to a view |
|---|---|
| `lock.acquired` / `lock.released` | The serialization point is the one thing no Crawler can see. A view that cannot show the queue cannot show the most important thing showrunner does. |
| `integrate.*` (merged, refused, rewound) | The riskiest verb, and currently invisible between "started" and "finished". |
| `crawler.blocked` | `reconcile` computes it live (#24) but nothing records the transition, so a view can only poll for it. |
| A snapshot verb | A viewer needs "the world as it is" on attach. Today that is 4 calls (`status`, `reconcile --json`, `waiting --porcelain`, `plan --json`). One composed call is one round trip and one consistent instant. |
| Per-Crawler game_loop state | showrunner asks its harness per tree (`harness.stop_gate`, `waiting_probe`). None of that reaches the journal. |

I would build `lock.*` and the snapshot verb first; they are the two a view is unusable without.

---

## Discovery: the part that is genuinely new

A UI over one repo is `?project=<path>`, as gents does it. A UI over *N* needs to answer "which
showrunners exist, and where" — and nothing today can.

**The central install is the natural home for this, and that is a reason to wait for it rather than
route around it.** [`central-install.md`](central-install.md) establishes the split this needs:
code is `__file__`-relative, state is git-relative. One central binary, N state directories. A
registry belongs next to the code, keyed by state directory.

Proposed, once central lands:

```
~/.claude/showrunner-central/instances.json
  { "<realpath of repo>": { "project": "...", "last_seen": <ts>, "version": "<sha>" } }
```

Written by any showrunner invocation, best-effort, same posture as the event journal: it must never
fail the work, and its silence must be visible. **A registry entry is a claim that a repo existed,
never that it is running** — the distinction the campaign record already draws between a recorded
PID and a live one, and the one a viewer will get wrong first.

Two open questions I do not want to answer alone:

1. **Machine scope.** `instances.json` is per-machine. Several machines means either the app talks
   to N servers or a server aggregates. gents' server is un-sandboxed and local by design; I would
   start local and treat cross-machine as a later, deliberate step rather than a config option.
2. **Registration without central.** A per-project install has no shared directory. `~/.showrunner/`
   would work and is a *second* machine-wide location, which is exactly the kind of thing that
   drifts from the first. Prefer central being the answer.

---

## Transport, and where the seam sits

gents' layering is the one to copy, and its correction is the valuable part:

> The heartbeat's first cut had the *app* read the session transcript to detect activity. The macOS
> app sandbox blocked it, **and that block was correct**: the app should not reach into a harness's
> private files.

Same rule one tier up. The app must not read `.showrunner/`; the server must not either. The server
runs `showrunner watch` and pushes frames.

```
showrunner (N repos)  ──`showrunner watch --follow`──▶  revali server  ──WebSocket──▶  Flutter app
      │                                                      │
      └── game_loop per Crawler ── asked via showrunner's verbs, never read directly
```

- **revali** for the socket: `@WebSocket.mode(WebSocketMode.sendOnly, triggerOnConnect: true)` is
  the shape gents verified against the pinned version — we push, the client never talks back, and a
  client attaching mid-session gets the backlog. That maps exactly onto replay-then-follow.
- **zonai** for anything durable. Its live query streams (`client.db.listen`, "do not poll") are the
  right answer if we want history, cross-instance queries, or a view that survives a server
  restart. A first cut does not need it; the moment we want "show me yesterday's campaign" it does.
- **Frames stay JSON strings across the socket.** gents' note is specific: a controller returning a
  weakly typed map generates a useless client. Hand-write the facade that parses back into types.

**One gents lesson to carry verbatim:** `revali_client_gen` appends `.handleError((_) {})` to every
generated stream call, so a swallowed error completes the stream normally and a crashed watcher is
indistinguishable from a clean finish. Their answer — every payload carries its own terminal state,
and the client never infers completion from end-of-stream — is why `watch` emits `bye` and why the
absence of it is the signal.

---

## What waits on Morgan, and what does not

**Waits:**
- Discovery (needs central's directory to exist).
- Anything assuming one code copy — `version` in a registry entry is meaningful only once
  `self --pin` stamps a SHA.

**Does not wait** — and I would build in this order:
1. `lock.*` and `integrate.*` events. Pure showrunner, no interaction.
2. A composed `snapshot --json`. Same.
3. A revali route that shells `showrunner watch` for one `?project=`. Single-instance, proves the
   transport in the real stack, and is exactly what the throwaway Python probe already proved is
   possible.
4. Discovery, once central lands.

---

## The objection I raised, and why it was wrong

This section said a central shim's fail-open posture was unacceptable for showrunner, because its
clones are worktrees full of unattended agents rather than a developer on another machine.
[`central-install.md`](central-install.md) had already answered it as **Objection C**, and the
answer is right:

> `locks.py` says why in source rather than in argument: *routing and guarding are optimisations;
> the lock is the guarantee, and only where the consumer itself takes it (`run`).* `lock run` is not
> a hook, so it fails **loud**. A missing central install costs the optimisation and keeps the
> guarantee.

That is quoted from showrunner's own source and repeated in this repo's README, which is where I
should have checked before raising it. INV14 says verify premises about our own repos too; I applied
it to game_loop all week and not to my own objection about my own lock.

The same reasoning covers `stop-gate`: fail-open there degrades to a Crawler ending its turn with
its leaf open, which is the pre-#21 state — and that state is *loud*, caught by `reap` and
`reconcile`. A fallback that lands in a condition the tool already detects is not a silent failure.

**What survives is one line, not a design change.** game_loop's own write guard fails open and
*says so* in the hook's `additionalContext` — "this tool call was ALLOWED WITHOUT BEING CHECKED" —
and central-install.md already gives that treatment to `worktree enter`. `lock guard` and
`stop-gate` should get it too: exit 0, and tell the agent the guard did not run, so the one reading
it knows to take the lock itself with `lock run`. Cheap, matches the precedent on both sides of the
boundary, and it is the difference between an optimisation being off and an optimisation being off
invisibly.

---

## Risks, stated as things that will happen rather than might

- **A view that shows a wedged run as healthy.** Every failure this project has shipped is that
  shape. The view must distinguish *observed idle* from *not observed* — which is why `watch` counts
  dropped events and unparseable lines and puts both on the heartbeat rather than swallowing them.
- **Capabilities drift.** gents' rule: *the UI shows what is actually enforced versus merely
  watched*, because the worst outcome is a gate everyone believes in that silently is not there. A
  showrunner with no waiting probe armed, or `harness.require=false`, is watched and not enforced,
  and the view owes that distinction.
- **The journal grows without bound.** No rotation today. A long-lived campaign will make replay
  slow before it makes anything fail, and `--limit` is a workaround rather than an answer.
