# showrunner

**A multi-agent orchestrator for Claude Code — the one who runs the whole crawl.**

If [`game_loop`](https://github.com/SupposedlySam/game_loop) keeps a single Crawler alive, honest,
and safe through one unattended session, `showrunner` runs the *show*: it decomposes the quest,
sends a party of Crawlers into separate rooms in parallel, enforces the party-wide rules that no
single Crawler can see, and keeps the campaign coherent across sessions.

> Status: **implemented and self-hosting.** The orchestration loop is real code
> ([`lib/showrunner/`](lib/showrunner/)), it installs in one line with no packages, and it runs
> against its own issue list — see [Dogfooding](#dogfooding-showrunner-on-its-own-issues).
> `python3 test/run.py` → **no setup beyond Python 3 and git.** It prints its own count.

## Requirements

- **Python 3.7+** (standard library only — nothing to install)
- **git**
- **[Claude Code](https://claude.com/claude-code)** for the Crawlers themselves

`br` and `tmux` are **optional**. Everything showrunner guarantees works without them.

## Install

```bash
git clone https://github.com/SupposedlySam/showrunner.git
cd showrunner
./install.sh /path/to/your/project
```

Then, in your project:

```bash
$EDITOR .showrunner/config.json      # resources, lanes, inject, checks
./.showrunner/bin/showrunner doctor  # refuses configs that would degrade silently
./.showrunner/bin/showrunner baseline
```

`showrunner` is the project-local binary `./.showrunner/bin/showrunner` — not a global command.

The installer touches **only the target repo**, with one exception it asks about: `--skills` links
the Claude Code skills (`showrunner`, `sr-status`, `sr-doctor`, `sr-install`) into
`~/.claude/skills`, which is the one thing written outside your project. Given neither flag it
asks, and only when there is a terminal to ask at; `--no-skills` never touches `~/.claude` and
does not ask.

### Installing for yourself, not for the team

That install is **shared**, on purpose: the payload and the gates are committed, and everyone who
clones the repo gets both. If you want showrunner for yourself only:

```bash
./install.sh --local /path/to/your/project
```

Hooks go into the untracked `.claude/settings.local.json` instead of the source-controlled
`.claude/settings.json`, and what the install writes is excluded in `.git/info/exclude`. A
`--local` install leaves `git status` **clean** — that is the whole promise, and it is why the
exclusion is not appended to `.gitignore`: that file is source-controlled, so ignoring the payload
there would make a private decision arrive as a tracked diff on everybody else's next pull.

Two things worth knowing before you use it:

- **`spawn` carries the untracked registration into each Crawler tree.** `git worktree add` copies
  tracked files only, so without that a `--local` install would produce worktrees with no
  `.claude` at all — no worktree guard, no dispatch guard, no seat announcement, no reach gate —
  while the main checkout reported every one of them registered and healthy.
- **Do not end up in both layers.** Registration is idempotent within the file it writes and
  cannot see the other one, so a shared install followed by a local one leaves every guard firing
  twice. `showrunner doctor` names any hook registered in both; which copy to remove depends on
  whose file it is, so the tool reports it and does not choose.

## The cast

| Piece | Role |
|-------|------|
| **`showrunner`** (this repo) | The orchestrator. Owns the work-graph, routes work to lanes, holds party-wide locks, spawns + monitors Crawlers, integrates their results. |
| **`game_loop`** (optional) | Per-Crawler survival rails — autonomy + safety + honesty, enforced by Claude Code hooks. Independently useful for solo runs. |
| **`br`** (optional) | An existing beads work-graph. showrunner ships its own; if you already run `br`, it defers to yours. |
| **Crawler** | One Claude Code agent in its own git worktree, with its own scratch dir and its own brief. |

Dependency arrow points one way: **`showrunner` → `game_loop` + `br`.** Neither knows showrunner exists.

## Why it's a separate thing from game_loop

Two different axes:

- **`game_loop` is vertical** — depth and integrity *within one session*. Its power is its narrowness
  ("enforcement lives in tools, never instructions"; "name a real file"). Bloating it with a tracker
  or an orchestrator would wreck that.
- **`showrunner` is horizontal** — breadth *across work and agents*: a dependency graph, parallel
  lanes, shared locks, resume. That's a different job, so it's a different repo.

## The one hard rule it exists to enforce

Parallelism has a serialization point that no single Crawler can enforce, because none of them can see
the others: **shared, single-consumer resources** (a device, a deploy target, a bound port). showrunner
owns the **cross-process lock** that makes "only one Crawler in the boss room at a time" *physically
true*, not a wish in a prompt.

```bash
showrunner lock run device --holder crawler-a -- ./deploy.sh
```

The holder is a **live PID** — the consuming process itself. A dead holder is stale and reclaimable; a
holder recorded on a previous boot cannot possibly still be running. Named resources are independent,
so unrelated work never queues behind unrelated work, and the lock root is **one absolute path shared
by every worktree**, validated at config load. A worktree-relative lock root is refused outright,
because N worktrees would get N sibling lock directories and the mutex would silently do nothing —
which is the worst available failure, since it looks like it is working.

> **What the lock does not cover**, stated in the guard itself: `lock guard` is a verb matcher, and a
> rogue raw command that matches no pattern escapes it. Routing and guarding are optimisations; the
> lock is the guarantee only where the *consumer* takes it. Belt and suspenders.

## The loop

```
showrunner ready                # the only work-discovery entrypoint: unblocked, UNCLAIMED leaves
  └─ showrunner plan            # group them into waves whose file sets do not overlap
  └─ for each leaf in a wave:
       showrunner spawn <leaf> --launch
                                # worktree INSIDE the repo + private scratch + injected secrets
                                #   + a brief that demands the premise be verified first,
                                #   then STARTS a real session in it on the lane's model.
                                #   Omit --launch to prepare the room and start it yourself.
       Crawler works under game_loop (kept alive, honest, safe)
       showrunner close <leaf> --proof <real artifact> --premise <verdict> --premise-read <file>
  └─ showrunner integrate       # serial merge, checks re-run on each MERGED result
  └─ showrunner reap            # reclaim what dead Crawlers left claimed
     repeat until ready is dry
```

The mechanics that carry the weight, each with the failure it exists to prevent.

**Proof-of-done gate.** A leaf cannot close unless it cites a real, non-empty artifact that is *newer
than the claim* — an artifact older than the work is evidence about something else. The proof is
recorded on the close, because existence is checkable and *relevance is not*, and the gate says so.

**Premise verification.** `--premise` is a required argument, not an aside, because **an issue's
premise does not always survive contact with the codebase**. A Crawler that
fixes a bug that is not there is indistinguishable from one that did the work — same commit, same
green tests, same satisfied gate — because the gate checks that work *happened*, not that it was
*needed*. `--refuted` is a first-class successful outcome, distinct from done and from failed.

**Dependency-gated fan-out.** Blocked work is hidden, so a shared prerequisite gates everything behind
it; the moment it closes, its dependents become ready and fan out together.

**Each Crawler is a real session, not a subagent.** `showrunner spawn <leaf> --launch` starts
`claude -p` inside the worktree, on the model that leaf's lane declares. A subagent would be the
easier thing to build and the wrong one: game_loop's rails are Claude Code hooks registered per
project, its usage park and watchdog key on a session, and the model it observes is read from a
session transcript. A Crawler that is not a session has no commit gate of its own, no Stop gate,
and nothing to reap when it dies — which is the entire point of sending one somewhere unattended.
The worktree carries the hooks because `.claude/settings.json` is tracked and `git worktree add`
copies tracked files.

Without `--launch`, spawn prepares the room and stops, and **nothing starts until you start the
agent yourself** — `plan → route → spawn → integrate` without `--launch` has no step that runs
anything.

**A finished Crawler spins itself down.** Closing a leaf marks its Crawler finished and
closes its chat room immediately — both safe, because the leaf is already closed. The
*process* is deliberately left alone at that moment: a Crawler closes its own leaf from inside
its own session, so it is mid-call right then, and terminating it would truncate the work it
just certified. `showrunner reap` takes a process that is still alive well after its leaf
closed (SIGTERM, never SIGKILL), and closes rooms belonging to Crawlers that died without
closing anything. **It signals only a process it can prove is the Crawler's**: pids are recycled
within one boot, so a launch records the process's start time, a live pid whose process started at
a different moment belongs to someone else, and "cannot tell" never licenses a signal. Under repeated fan-out those two leaks — a stacking process and a room per
dead Crawler — are what fill a machine and make a channel list unreadable.

**A Crawler can also stop without dying, and that is the harder one.** Its turn-end gate refuses
while its leaf is still open — correct, and the reason it does not exit with work unfinished —
but a headless session has nothing to deliver "go back to work" to itself. It stays alive and
inert, and every signal reads healthy: a live pid, an open leaf, a report already on disk, `reap`
correctly proposing nothing. `showrunner reconcile` reports those as **BLOCKED**, ranked above
LIVE precisely because they *are* live. Such a Crawler wakes, reports and closes correctly the
moment a message reaches it.

**Talking to a running Crawler is unbundled — and under `--launch`, not optional.** Clone a chat
tool and point showrunner at it; nothing is vendored and no package manager is assumed:

```bash
git clone https://github.com/SupposedlySam/llm_chat.git ../llm_chat
```

```jsonc
// ~/.config/showrunner/config.json — one chat tool serves every repo, so this belongs at
// USER level, not per-project. See the four layers below. ABSOLUTE paths here: a relative
// one resolves against whichever repo is asking, which is not one place.
"dispatch": {
  "chat": {
    "enabled": true,
    "cli":       "/abs/path/to/llm_chat/bin/llm_chat",
    "installer": "/abs/path/to/llm_chat/install.sh"
  }
}
```

`spawn --launch` then opens a room per Crawler, installs the tool into its worktree, **joins the
Crawler to that room on its behalf**, and tells it in its brief to ask rather than guess.

**The join is not left to the Crawler**, because a Crawler goes straight to the work — which is
what the brief's own task section optimises for — and an unjoined Crawler receives nothing: every
correction reads `nobody else is in this room yet` and sits unread until after it closes. And
since `showrunner edit` refuses to rewrite a brief already in somebody's hands, the room is the
only way to correct a running Crawler.

Membership is keyed to the Crawler's session, so `spawn` generates that id before it opens the
room, joins from inside the Crawler's own worktree, and then **verifies the membership actually
got recorded** rather than trusting the exit code. If the join does not happen the brief says so
and tells the Crawler to join — a Crawler wrongly told it is already reachable will not join, and
will read the silence as the orchestrator having nothing to say.

**Leaving `chat` out does not leave dispatch working the same minus the conversation.** A Crawler
whose turn-end is refused by the stop gate stays alive and inert until something external prompts
it, and the room is the only thing that can. So with `chat` disabled, "inert until rung"
becomes "inert": `waiting` reports the Crawler BLOCKED (exit 3), and nothing showrunner owns can
deliver the message it needs — `reap` correctly proposes nothing, because it is alive. Chat is load-bearing for **correctness** under
`--launch`, not a convenience. Without it, prefer `spawn` without `--launch` and drive the
Crawlers yourself.

**A watchdog probe has a cost budget, and blowing it disarms the watchdog.** `waiting` is the
command a consumer's idle watchdog runs, under a fixed timeout — and game_loop reads a timeout as
"the probe did not run at all", reports a broken watchdog, and then stops scheduling re-checks. A
slow probe does not degrade that mechanism, it switches it off — and Crawlers that die in the
silence are found only when a human goes looking.

Branch questions come from one `for-each-ref` per pass rather than a `rev-parse` per branch, and
`waiting` asks for a shallow pass, because it reads alive/parked/blocked and never touches merged,
empty, uncommitted, harness or model. Its cost is bounded by how many Crawlers are *alive*, not by
how many the campaign has recorded. The suite asserts that shape rather than a wall-clock time: a
timing assertion measures the machine, and a machine can be slow for a reason no code change
fixes — security software intercepting every process spawn, for instance.

**A worktree is reclaimed when its work lands.** `spawn` makes one tree per leaf, and `reap` only
handles claims and locks whose owners are dead, so without reclamation trees accumulate — and the
cost is not only disk: every tree is another copy of the repo for antivirus and indexers to
rescan continuously, on a machine that looks idle.

Every brief promises it: Crawlers are told their tree is removed once the work integrates, and
that sentence is the justification for the whole scratch-dir discipline. A tree left behind would
leave the rule standing on an argument that does not hold.

`integrate` reclaims a tree at the moment it becomes provably redundant — the branch is
merged, so every commit survives and `spawn` can recreate the tree. `showrunner gc` does the same
retroactively and is **dry-run by default**. Three conditions are all required, and `unknown` is
not one of them: merged, clean, and not alive. `reconcile` answers clean/dirty/**unknown**, and a
failed read must never license a delete, with somebody's only copy of their work on the other
side of it. Everything held back is printed with its reason, and `doctor` reports how many trees
exist, because that count is not a number anyone discovers on purpose.

**What a compacted agent gets back.** An agent several compactions deep can lose which campaign
it is on and what verbs exist, and stop using the tool at all — doing the work by hand in a repo
carrying a live campaign. `whoami`, which runs on SessionStart **and PostCompact**, carries the
campaign's *state* rather than the bare fact that one exists: how many leaves, how they split by
status, and how many are READY right now. It prints every verb, derived from the argparse parser
rather than a hand-written list that would go stale, because naming only the dispatch verbs
answers "how do I dispatch" and nothing else.

**And `reach` names the mechanism at the moment of reach.** An agent that cannot remember a tool
does not stop working; it reaches for what it knows — `git worktree add`, a private todo list, a
note in a memory file — and each of those produces a plausible result, which is why nothing ever
objected. A PreToolUse hook reads the payload and, when it has something specific, says which
verb serves that intent. It is advice and never a refusal, because every reach it names is
legitimate somewhere and a gate that blocks a legitimate shape trains its own bypass; it is
silent otherwise, because a notice on every call is an alarm that is always on. Rules naming
game_loop's `harden` stay quiet in repos without game_loop, and every verb a rule names is
verified against the parser by the suite.

**And `wake-gate` says, once, that long work is about to start with nothing able to wake you back
to a goal.** With no mandate bound, `game_loop doorbell` answers "there is nothing to wake this
run FOR", so a wake arriving mid-run drops an agent into a prompt with a hole where the goal goes
and the run gets re-derived from scratch.

None of that information is missing elsewhere: the SessionStart banner prints `MANDATE: none
(Stop gate inert)` and `doorbell` explains the fix to anyone who runs it. Agents start unattended
runs unarmed anyway. That makes it a **delivery** problem rather than a documentation one —
session-start text is read once, before the agent knows the work ahead is long, and by then the
banner is far upstream. What works is speaking at the moment, so this speaks at the moment.

Bash only, since long unattended work is a process and an `Edit` is never what makes a session
unreachable for twenty minutes. Silent when a mandate is bound, when game_loop is absent, for
ordinary commands, and after the first telling in a session. Never refuses. A backgrounded call
counts because the caller *said* it outlives the turn; the patterns behind that are anchored on
runners rather than words like "test" that appear in ordinary prose, because a false positive
spends the attention the true ones depend on. Heredoc bodies and quoted text are ignored, so a
commit message that mentions `make` is prose, not a build. And `armed` reports **armed, unarmed, absent or
unknown** separately — a doorbell that could not be run says nothing about whether a goal is
bound, so it is never filed under "fine".

**A Crawler's tree can be sparse, and the cone can narrow the work but never the rails.** On a
monorepo, a campaign of full-tree worktrees can fill a disk. `showrunner spawn <leaf> --sparse app audio`,
or `sparse_by_label` in config, writes only those directories plus root files — before any file
lands, not afterwards. Every cone also carries `.claude/` and each tracked directory a registered
hook points into, derived from both settings layers; the tree is checked for them after checkout
and a gap refuses the spawn, because a hook whose file is missing fails open and silently. And for
**every** spawn, sparse or not, each registered hook must find its directory in the finished tree
— tracked or untracked, which covers a `--local` install of any tool — or the spawn is refused.
Declared paths outside the cone are warned at spawn and named in the brief. An `inject` entry
outside the cone is skipped and named in the spawn report: nothing in that tree can use it, and
the `.gitignore` covering it usually sits beside it, outside the cone too. The main checkout
stays full: git adds `extensions.worktreeConfig = true` to **that repository's own `.git/config`**
to keep the sparse setting per-worktree. That file is local and never committed, and your global
`~/.gitconfig` is not touched.

**A guard finds its project from its own location before it gives up.** `cwd` and
`CLAUDE_PROJECT_DIR` alone would let a tool call from a scratch directory with no harness variable
— including a raw `claude -p` — go unchecked while the hook answering it sits inside the project.
The fallback is a parameter only the guard verbs pass,
because a guard must answer about a call happening now while every other verb may refuse; making it
global would turn `ready` outside a repo into a quiet answer about showrunner's own checkout. A shim
genuinely outside any repo still fails open and still says so.

**A guard that fails open is COUNTED, not just announced.** When a guard cannot do its check —
no repo, unreadable config — it allows the call and prints a notice saying it did not check. That
notice alone is quiet: it arrives beside a *successful* tool result, which is the channel an agent
mid-task skims. Rewording it louder would treat a delivery problem as a copywriting problem. Every
fail-open appends to `.showrunner/fail-open.jsonl`, and
`doctor` reports how many calls went unchecked — a count is the fact a per-call banner cannot
carry, and `doctor` is read by somebody who has stopped to look. Both entrypoints record through
the same funnel, and an unparseable ledger reports UNKNOWN rather than none.

**The launch binary is configurable, and `doctor` resolves it.** `dispatch.claude_bin` defaults
to `claude` on PATH. On a machine whose only `claude` is bundled inside an editor extension —
not on PATH, no standalone install — every `spawn --launch` fails and the whole parallel lane is
unavailable, and a spawn discovers that only after it has created a worktree, a branch and a
claim. An unresolvable binary is therefore an ERROR from `doctor`, which is what that verb is for.

**A launch that fails parks the leaf rather than stranding it.** `spawn` records first and
starts second, which is the right order, and so it needs a compensating action. A failed start
would otherwise leave the leaf `in_progress`, claimed by the invoking shell's pid — gone seconds
later — so it would be out of `ready` and invisible to the only discovery surface. It is parked
with the launch error as its reason: it survives `reap`, stays visible, and its worktree is kept,
because that tree may hold the only copy of real work. Deliberately not a rollback — and
deliberately not a steer to `reap`, which can propose closing chat rooms belonging to another
agent's Crawlers, sweeping far wider than the failure.

**Config is four layers, and only the middle one ships.** Each is overlaid on the one above:

| | |
|---|---|
| built-in defaults | the tool's own answer |
| `~/.config/showrunner/config.json` | **the user** — set once, applies to every repo on this machine |
| `.showrunner/config.json` | the project, tracked and shipped to every clone |
| `.showrunner/config.local.json` | **this machine**, untracked — an absolute path only you have |

Dicts merge key by key; **lists and scalars replace wholesale**, at every layer. So a
`dispatch.chat` you configured once at user level survives a project that sets only
`dispatch.default_model`, while a project's `lanes` replaces the user's entirely — half a lane
is a configuration nobody wrote. An empty value is a value: a project writing `"checks": []`
overrides a user-level list rather than inheriting it. `showrunner doctor` prints which
user-level file, if any, was merged, because a merged config cannot be asked where a value came
from.

**`doctor` also reports, per leaf key, which layer's value won and which was shadowed** —
naming the two files, and marking a shadowed *user-level* value distinctly rather than burying
it among `ok` lines. The limit: it reports at **leaf-value** granularity, not dict granularity.
A top-level key set in two layers with disjoint sub-keys — `dispatch.chat` at user level,
`dispatch.default_model` at project level, the exact pair above — is a merge, not a shadow, and
produces no line at all; only a dotted path whose own scalar-or-list value was actually replaced
by a lower layer counts as shadowed. It says nothing about a value that only ever existed at one
layer, and nothing about DEFAULTS, which is the tool's own answer rather than a file.

**The project beats the user — which is the opposite of `roles.json`,** the other file in that
same directory. The two are different kinds of thing: `roles.json` is *permission*, so it comes
from user level **only** — a project's `roles` and `seat_roles` are reported and ignored, because
a project that could add a role could grant the session editing it any seat; `config.json`
is *preference*, so the project wins, because a repo is the better authority on its own lanes,
checks and resources.

**Some keys are refused at user level**, loudly, naming the file: `project_name` (it feeds the
chat channel prefix and orchestrator identity — machine-wide, every repo would open rooms under
one prefix), `lock_root` (one absolute root shared by
unrelated repos makes them serialize against each other — a mutex that is quietly the wrong
one), and `graph.db` / `baseline`, which are one campaign's state and mean nothing machine-wide.

**The model is declared, observed, and compared — never enforced.** A lane names a model,
`spawn --launch` passes it, game_loop records what actually ran, and `showrunner reconcile`
reports a mismatch or a mid-run fallback. Nothing blocks: an Opus-priced Crawler doing Sonnet work
produces perfectly good output, which is exactly why nothing else notices and why it can run for a
week. A missing observation reads as UNKNOWN rather than as agreement.

**Collision prediction.** The graph answers "what is unblocked?" — a question about *dependencies*. It
models nothing about what two agents will *touch*. `showrunner plan` estimates each leaf's blast
radius and refuses to fan out two leaves whose estimates intersect. The estimate does not need to be
good, only conservative: a false collision costs one wave of latency, a missed one costs a merge
conflict in an unattended run with nobody watching. Shared surfaces (the one test file every change
touches) are configured as such and owed to serialized integration instead of blocking parallelism.

**...and it counts what is already running.** `plan` groups `ready` work, which is unblocked *and
unclaimed*, so a Crawler working right now is absent from the **input** and its files were never
considered occupied; `overlap` measures committed diffs, so a Crawler twenty minutes in with nothing
committed is not an in-flight branch by that definition — and a branch existing is not enough, since
it counts branches with commits. Between them, the whole working life of a Crawler up to its first
commit would be invisible. So `plan` reports live claims **beside** its waves (the grouping itself
ignores them, because "how would I group this if nothing were running" is a real question before a
campaign starts), and `showrunner spawn` refuses a leaf whose estimate collides with a live claim.
The refusal is overridden by naming what it overrides — `--despite-live <leaf>`, which must name
every colliding leaf — because a guard answered by a reflexive `--force` teaches every later session
to bypass it. It is an **estimate**, from declared paths and grepped symbols, and says so wherever it
is printed: `overlap` measures, this guesses about work that has produced nothing measurable yet.

**A base that is missing work the leaf depends on is REFUSED, not reported.** `spawn` cuts from
the primary checkout's HEAD unless told otherwise, and that default is invisible and
context-dependent: the identical command is right or wrong depending on where an unrelated checkout
happens to be pointing. Printing the base is not enough — a printed line arrives *after* the
worktree, branch, brief and claim exist, and under `--launch`, after the Crawler is already
running.

The failure is silent and plausible, which is what earns a refusal here. The worktree exists, the
branch exists, the code compiles, and every file the brief names is present — just older. A Crawler
that does not think to run `git log -1` finds the function it was sent to fix, finds it does not have
the problem described, and reports **premise refuted** with real evidence: every word true of the
tree it was given and false of the tree under review. That is the most expensive wrong answer this
tool can produce, because refuted is a legitimate close and reads as a successful run.

So `spawn` exits 3 before creating anything when a dependency's branch is definitely not an ancestor
of the base, overridden by `--despite-base <leaf>` naming which one — the same rule as
`--despite-live`. A dependency that *cannot* be checked stays a warning: refusing there would block
work on the strength of not having looked.

**The second arm needs no graph edge**, because the base a leaf depends on may be named only in
the *brief's prose*, which showrunner cannot read and must not pretend to. An **implicit** base is
refused whenever the checkout is not standing on the default branch: defaulting to HEAD "is
defensible for a leaf off `main`; it is wrong the moment a campaign has more than one branch in
flight." `--base HEAD` is the confirmation: the same commit the default would have used, differing
only in that somebody typed it — and showrunner tells the two apart, because a guard asking for a
decision has to be able to see one being made. A repo with no `origin/HEAD`, `main` or `master`,
or a detached HEAD, warns and allows: cannot-tell must not refuse. `showrunner show <leaf>` reports
`crawler_base` — what was asked for, the resolved sha, the branch.

**A window reload does not cost the seat.** Reloading a VS Code window restarts the extension
host under a new pid, so the recorded holder is dead, the lock correctly reports STALE, and the
resolver correctly skips it — every step right and the outcome useless, because the same logical
session comes back and cannot see its own seat, and would re-claim on every reload.

The two facts age differently, which is what makes it decidable: the Claude session id is
unchanged across a reload while the pid is not. So a seat whose **session matches** and whose
**pid is gone** is rebound, and the announcement SAYS a reload happened — a silent re-seat is
indistinguishable from never having lost it, and the caller may owe setup it did the first time.

Three answers, not two. A pid that is **still alive** is never displaced: two live processes under
one session id is what `claude --resume` produces, and both resolve to the seat because the
session is the unit of identity — what does not happen is the pid moving. An **empty** session id
matches nothing on either side, or any unidentified session would inherit any unidentified seat —
and that holds everywhere a session is compared (seats, lease ownership, releases, mapped seats),
through one function. A
**different** session inherits nothing however dead the pid. The id itself is discovered from the
environment rather than demanded as a flag, because a mechanism nobody should have to think about
must not require knowing about it.

**Claims carry liveness.** A claim records the owning PID and a boot token. Without that, a Crawler
that dies leaves its leaf claimed forever — `ready` means unblocked *and unclaimed*, so the work
silently leaves the queue, `ready` goes dry, and the loop terminates **reporting success** on work
nobody did. `showrunner reap` reclaims loudly and is dry-run by default; an abandoned worktree may
hold the only copy of real work, so it is surfaced, never deleted. A Crawler *parked* at a usage limit
is not dead and its claim survives.

**Integration means green on the merged result.** Green on a branch is evidence about a trunk that no
longer exists once the second branch lands — two Crawlers adding an entry to the same dispatch table
touch different lines and still break the trunk. Merges are serial, checks re-run after each, and the
run stops and rewinds on the first failure rather than stacking onto a broken trunk. The criterion is
**no *new* failures versus a recorded baseline**, never "all green": a repo with pre-existing failures
cannot satisfy "all green", so that version of the gate gets switched off on contact with reality.

**showrunner greets its own sessions.** A guard that reads only showrunner's own state sees a
session that never registered as holding no lease and being no Crawler, and correctly exits 0
while it works — so a repo can have showrunner installed, wired and carrying a campaign while every
worker session goes around it. `showrunner whoami` fires on **SessionStart and PostCompact** and announces what this
session IS. The second seam is the point: a rule that survives only until the next compaction is a
rule for the first hour.

The seat is **derived, never declared** — a linked worktree is a CRAWLER, the main checkout of a
repo carrying a campaign is the ORCHESTRATOR, no campaign is SOLO, and UNKNOWN is a real answer
announced as one. A seat kept in a one-word file is a claim that goes stale: a file that says
`worker`, written mid-run, lets every guard that reads it exit 0 for the rest of the run.

**The cheap dispatch path has a gate on it.** `spawn --launch` is the correct way to start a
Crawler; the competing path is one Bash line — a raw headless `claude` — which gets no worktree,
no lease, no claim a reaper can reclaim, no leaf-scoped stop gate and no room. `dispatch guard`
refuses it from a session whose role may not create one. Registered on **Bash**, which is the
mechanism actually used: a matcher on `Agent` guards only the in-process subagent tool and sees
none of the real dispatches.

**A hook is only as present as its registration — and an untracked registration does not cross
into a worktree.** `git worktree add` copies tracked files only, which is why the shim must be
either tracked or provisioned. The same is true of the settings file that *registers* it: without
it, a `--local` install would produce worktrees with no `.claude` directory at all, so every hook
showrunner owns would be absent from every Crawler while the main checkout reported them all
registered and healthy. Provisioning the shim files is not enough; a provisioned shim nothing
registers never runs. `spawn` merges showrunner's own entries into the tree — merged, not copied,
because the file also carries your statusLine, permissions and unrelated hooks.

The ordering is the other half: Claude Code reads settings
at **startup**, so a hook registered after `spawn --launch` returns is not read by the Crawler
that spawn just started. If a guard of yours must be live for a Crawler's first tool call, it has
to be registered before the spawn — and in the tracked layer if you want git to carry it for you.

**A line that says ENFORCED has to be one showrunner refuses.** Not every policy line is:
showrunner publishes `writes` and ships no write guard at all, and a write guard registered for
`Write|Edit|NotebookEdit` and not `Bash` is walked past by a heredoc. Announcing enforcement you
do not perform is worse than announcing nothing: it is the sentence that stops somebody checking.

`may_create` is enforced at **both** paths — the sanctioned `spawn --launch` as well as the raw
`claude -p` the announcement steers you away from, from one shared function so the two cannot
disagree. `writes` is labelled **PUBLISHED**, and `doctor` reports whether any PreToolUse hook
matches Bash when a role declares one; it will not say *which* hook enforces it, because
attributing another tool's job would be a guess.

**Roles are yours; showrunner checks the shape.** It never learns what a role *means* — the way
lane rules already work. It knows two acquisition modes, `claim` (a session takes an open seat,
exclusive, with pid+boot liveness) and `assign` (written by whoever created the session), and it
refuses a configuration that cannot resolve: a dangling `reports_to`, a cycle, an org with no
root, nothing claimable at all, or a fallback role that may create something. Definitions live at
a user-level path, because an in-repo config is writable by the very session it constrains.

**A seat with no role is a guard that gets routed around.** An unmapped Crawler resolves to the
fallback and runs under the fallback's policy *inside the worktree `spawn` has just made for it* —
with a deny-everything fallback, a leaf finishes only by routing its evidence around the write
guard with shell redirection. `seat_roles` maps a derived seat onto
one of your roles, `{"seat_roles": {"crawler": "worker", "solo": "worker"}}`, and the campaign
record is the assignment being read back: `spawn` names the tree's leaf before the session exists.
The keys are the derived seats — `crawler`, `orchestrator`, `solo` — and **`solo` is how an
operator says a session is not doing campaign work**: in a checkout that carries a campaign,
`showrunner campaign use <a-new-name>` moves the seat to `solo`, and a `solo` mapping gives it a
writable role without touching the campaign's own seats. Permission is **user level ONLY**; a
project's `roles` and `seat_roles` are reported and ignored, because mapping a seat the user left
*unmapped*, or defining a claimable role in the repo and claiming it, would hand a session any
role in the catalog — and an operator who has mapped nothing has not consented to anything. Only a worktree the record NAMES resolves, so `git
worktree add` grants nothing, and **`orchestrator` ships unmapped on purpose**: standing in the
main checkout is a location, not a record, and authority by location is the failure this seam
exists to prevent. `doctor` refuses a seat mapped at a role nothing defines — that seat resolves to the
fallback, so one typo buys the whole write denial back and nothing else would have said so.

**Both acquisition modes need a path, or every session gets the fallback whatever its roles
say.** `assign` is read through `seat_roles`; `claim` is written by `role claim` — not the `claim`
verb, which claims a *leaf*.

```
showrunner role claim campaign-lead --who agent-a   # a role declaring acquire=claim
showrunner role roster                              # every seat, its state, its liveness basis
showrunner role release campaign-lead
```

A role declaring `assign` is refused here — its meaning is that whoever created the session
decided, so claiming it would be self-nomination into a seat the model says cannot be
self-nominated; `seat_roles` is how that one is obtained. **A claim's pid is discovered, not handed
over:** liveness is a pid plus a boot token, so a seat keyed to the short-lived process that made
the call reports success and reads STALE the instant that call returns, and `whoami` announces the
fallback again. `lock acquire` warns about exactly this. A pid that cannot be resolved is refused rather than recorded, because a claim
with no liveness is not a weaker claim — it is a lock nothing can ever reclaim.

**A guard cannot consume prose.** A hook author who needs the resolved role and can only read
prose reimplements the resolver — and a copy drifts: a copy that does not resolve through
`seat_roles` enforces the deny-everything fallback while the announcement names another role. `showrunner whoami --porcelain` emits
the seat, the resolved role, how it resolved, the `writes`/`may_create`/`reports_to` a guard
enforces, and a Crawler's `scratch` path — found by session when asked from outside its tree, so a
guard judging a write by the target's tree can still allow it. `whoami` renders that same dict, so the two cannot disagree. Branch on `enforced`; a null
`role` is not "no restriction", and the porcelain exits non-zero if it could not resolve so a
parser can fail closed.

**A campaign is smaller than a repo.** The natural unit of a body of work is often a story, and
the handoff a showrunner charges is only paid for by the parallelism it buys — so several
campaigns in one checkout is the ordinary case. `SHOWRUNNER_CAMPAIGN` scopes graph, record, events
and scratch. **Locks deliberately do not follow it:** a lock names a physical resource shared by
the machine, so two campaigns flashing the same TV must serialize against each other.

**Many agents in one repo: `showrunner campaign use <name>`.** It binds THIS session, so each
agent resolves its own campaign — including from its hooks, which is the part nothing else could
do. Nothing shared is written, so two agents in one monorepo can each hold `campaign-lead` in
their own campaign without colliding.

```bash
showrunner campaign use my-campaign   # bind this session
showrunner campaign show              # what this session resolves, and what named it
showrunner campaign use --repo-wide   # bind the unnamed repo-wide campaign
showrunner campaign clear             # unbind (falls back to the checkout default, which is
                                      # NOT the same as --repo-wide)
```

Resolution is **explicit argument > environment > this session's binding > checkout config**. A
Crawler dispatched with `SHOWRUNNER_CAMPAIGN` set therefore keeps the campaign it was dispatched
into, whatever anybody binds afterwards.

**`spawn --launch` binds each Crawler's own session to the campaign that placed it**, so the
Crawler resolves the campaign whose record names its worktree rather than the repo-wide one, which
would deny it every write inside its own tree.

**`campaign` in `.showrunner/config.local.json` is the checkout's default**, for a checkout that
works one named campaign. Hooks are spawned with the *session's* environment, not the shell where
you typed `export`, so the environment alone cannot name a campaign for them — `campaign use`
(per session) or this default (per checkout) can. The environment still wins where it is set.

```json
{ "campaign": "my-campaign" }
```

**A run that could not measure anything is not a degraded comparison.** `check` does not let
reduced resolution read as a clean comparison, nor *no* resolution read as reduced. A suite that could not reach the world did not measure anything, and its failure count
carries no information — so a VOID run exits **3**, distinct from 2, because "your code broke" and
"nothing was measured" must not be the same number.

**showrunner runs a pinned copy of itself.** It develops itself, so its guards run the very code
being edited — and one syntax error under `lib/showrunner/` kills every verb at import, which would
leave the worktree guard exiting 1 with empty stdout: neither a refusal nor an announcement, so editing
the tool would silently disarm it. The hooks resolve a gitignored `.showrunner_self` pin first, so the
plumbing runs code a mid-edit cannot break, and `doctor` says how far behind that pin has drifted.

## Lanes

| Lane | Runs | Parallel? |
|------|------|-----------|
| **Headless** | tests, backend, pure logic, analyze | ✅ one worktree Crawler per ready leaf, in collision-free waves |
| **Serialized** | anything touching a single-consumer resource | ❌ one at a time, behind the named lock |
| **Orchestration** | showrunner itself: graph, routing, review gates, integration | single |

Routing is config, and **conservative by default**: an unmatched leaf serializes and says so out loud.
The costs are not symmetric — a wrongly-headless leaf collides on a single-consumer resource hours in
with nobody watching; a wrongly-serialized leaf just runs slower. Every decision is logged with the
rule that produced it, so a wrong route is diagnosable after the fact rather than re-derived from
behaviour.

## What a Crawler actually gets

`showrunner spawn` creates all of it and refuses if any of it is unsafe:

- **A worktree inside the repo** (`.worktrees/<crawler>/`, gitignored by showrunner itself). The
  sibling-directory instinct is wrong: a Crawler's own write guard treats everything outside the
  project as read-only, so a sibling worktree is a workspace it is structurally forbidden to work in.
  Inside also means each Crawler gets its own copy of the harness — **a Crawler editing the guard can
  only brick itself**, not the whole party.
- **Optionally, a sparse tree** — only the directories its leaf needs (`--sparse`, or
  `sparse_by_label`), with `.claude/` and every hook directory always included.
- **Its own scratch directory**, in the **main checkout** rather than the worktree, because it must
  outlive the tree: `gc` treats a dead Crawler's scratch as possibly the only copy of real work. A
  write guard that judges by path may therefore refuse it, correctly; a long close reason goes on
  stdin instead (`--reason-file -`), so no file needs writing at all. Two Crawlers reaching for
  `commitmsg.txt` in one shared temp dir means one can commit the other's commit message onto its
  own changes — a real commit, a plausible message, describing work it does not contain, every gate
  green. Crawlers are the same model solving similar tasks from similar
  prompts, so they converge on the same obvious filename far more often than independent actors would.
- **The gitignored files the build actually needs**, from an explicit configured list — symlinked
  where possible, added to the worktree's exclude file so `git add -A` cannot stage them, and verified
  after injection. A missing declared path **aborts the spawn**, because a Crawler that cannot reach a
  service will write the service up as broken in the same confident tone as a real finding.
- **An audit of what it still shares.** A worktree isolates tracked files and *nothing else* — not the
  harness's state directory, not lock paths, not caches, not anything resolved from an absolute path
  or from a hook's own script location. The brief names the shared-state case and says what to do
  instead, because `--no-verify` starts looking reasonable exactly when an agent is stuck under a
  mandate to finish.

## Integration commits and provenance

A provenance check that compares a commit's staged files against the set *this session* edited fires
on **every** integration commit an orchestrator makes, correctly by its own definition: the
integrating session never edits those files — Crawlers wrote them, in worktrees, and `git merge`
brought them in. Silence is the wrong fix; a warning that fires every time is one people learn to
scroll past, and then it stops working for the case it was built for.

```bash
showrunner integration-commit --crawler crawler-a --crawler crawler-b
```

The honest version is a **different question**: *does the staged set match the union of what the
merged Crawlers edited?* That is answerable, strictly more useful, and it catches the real
orchestration failure — a file appearing in an integration commit that **no Crawler ever touched**.

## Dogfooding: showrunner on its own issues

showrunner's own issues run through showrunner's own graph, and its hooks guard the sessions that
edit it (through the pinned copy above). Running it is what surfaces the second-order defects — a
fix that turns a loud failure into a silent one only shows up once the fix is running — so
`--launch` against a real repo is part of how this tool is tested, not an afterthought.

## Verifying it

```bash
python3 test/run.py            # CORE assertions — Python 3 + git, nothing else
python3 test/mutate.py         # which producers anything would NOTICE if they broke
python3 test/corpus.py         # the turn-end gates, measured against a real transcript
bash prototype/demo.sh         # the original shell POC; it prints its own pass/skip counts
```

`test/run.py` exits **3** — not 1 — when it did not run every group it defines. A suite that
skipped groups silently prints the same green line as one that ran them all, so "nothing ran"
and "something failed" are deliberately different codes.

The CORE half runs green on a clean clone with **zero setup**. Assertions that genuinely need external
tooling (`br`, `tmux`) **skip loudly, naming the missing dependency**, rather than failing obscurely —
a repo claiming a passing suite should ship one a stranger can run.

## Prototype

[`prototype/`](prototype/) holds the original shell proof-of-concept, kept for the record. The
primitives it proved now live in [`lib/showrunner/`](lib/showrunner/); see
[`prototype/README.md`](prototype/README.md) for what changed in the lift and why.

## Generic, not project-specific

Like `game_loop`, showrunner is generic; a project's specifics (which resources are single-consumer,
which verbs are "serialized," the graph location, the owed checks) are **config, not code** — see
[`.showrunner/config.json`](.showrunner/config.json) for this repo's own.

## Watching a campaign while it runs

`status` and `reconcile` answer *what is true now*. Neither answers *what just happened*, and a
viewer that polls a snapshot and diffs it invents transitions it never saw and misses any pair
that cancels out between polls. So every state change is appended to a journal, and one verb
streams it:

```bash
showrunner snapshot            # the whole world in one call and one instant
showrunner watch --follow      # then the deltas. One JSON object per line.
```

The two are a pair. An event saying `leaf.closed` is a **delta against a picture**, so a viewer
needs the picture first — and building it from `status` + `reconcile` + `waiting` + `plan` costs
four round trips and returns a composite of instants that never co-existed. They join on a
`cursor`: `snapshot`'s names the last event it could have seen, so `watch --since <that>` gives
exactly what is not already reflected, with no overlap and no gap.

```jsonc
{"kind":"leaf.claimed","seq":2,"leaf":"gh-15","actor":"crawler-a","instance":"/path/to/repo"}
{"type":"ready","replayed":2,"seq":2,"cursor":"fd4620f33271@2","project":"showrunner"}
{"type":"heartbeat","seq":2,"dropped":0,"unparseable":0}
```

Three properties, each with a reason:

- **A viewer asks the verb; it never reads `.showrunner/`.** The journal's name and layout are
  showrunner's business, and a consumer that reaches past the verb couples itself to a layout
  that is free to change.
- **Replay comes before follow.** Attaching to a running campaign is never a blank screen — and a
  blank screen cannot be told apart from a broken pipe.
- **A heartbeat, because the journal is sparse.** An orchestrator can integrate for twenty minutes
  without writing one event. A view built on the journal alone freezes exactly when the work is
  hardest, which is when someone is most likely watching it. The heartbeat also carries the count
  of events that could **not** be written and lines that could not be parsed, so "nothing is
  happening" and "I have not been able to see" stay different answers. A journal that cannot be
  read at all is a **refusal**, not an empty replay.
- **Refusals are events too.** `lock.refused` is what lets a view draw a *queue*; with only
  `lock.acquired` a contended resource and an idle one are the same picture — and the
  serialization point is the thing no single Crawler can observe, which is why showrunner owns it
  at all. `lock.reclaimed` stays distinct from `lock.released`: a reaper taking a resource off a
  dead holder is not the holder handing it back, and only one of them says something went wrong.

The cursor names the instance that minted it, and `--since` refuses one from a different
showrunner: a sequence number counts within one journal, and several showrunners in several repos
is the ordinary case. Design notes, and what a server built on this must not do with that cursor,
are in [`docs/plans/observability.md`](docs/plans/observability.md).

## Running more than one orchestrator

A graph that survives sessions is a graph more than one agent will open — several Claude Code
sessions driving one build is a supported shape, not an accident. The state showrunner shares
between them is protected:

| Shared state | The race | Protection |
|---|---|---|
| a leaf claim | check-then-write lets concurrent claims win the same leaf | one conditional `UPDATE` — exactly one winner (`python3 test/run.py`, group *More than one orchestrator may share this state*) |
| the campaign record | read-modify-write loses concurrent spawns | `flock` + write-then-rename — every spawn survives |
| the main checkout | two `integrate` runs rewinding each other | exclusive, and it **refuses** rather than queueing |

Take work with the primitive built for it, not by reading `ready` and claiming the first entry —
`ready` hands the same list to everyone who asks:

```bash
showrunner claim --next --actor crawler-a     # atomically take ANY free leaf; exit 1 when dry
```

Losing a race there is not an error: it means a sibling got there first, which is the system
working. Concurrent orchestrators against as many leaves each claim a distinct leaf and none of
them fails — asserted in the suite.

Two things stay deliberately single: **integration** (it merges, runs checks, and rewinds with
`git reset --hard`, so two at once would rewind each other's work) and any **single-consumer
resource** you have configured. Both refuse loudly instead of waiting silently, because a
multi-minute silent wait is indistinguishable from a hang.

`showrunner waiting` answers in three codes: **0** waiting, **1** not waiting, **3** a Crawler is
BLOCKED. That is the recomputable fact an idle watchdog needs, since it cannot see a subagent. A
Crawler refused at a turn-end is live and is deliberately counted as NEITHER waiting nor parked:
it is doing nothing, only a message restarts it, and calling that "waiting" would silence the
watchdog on the one run that needs it.

BLOCKED has its own exit code so that the case a gate exists for never produces the same number as
an ordinary quiet campaign. Build on `--porcelain`: a verb whose finding, verdict and
status live on three channels gets integrated against incorrectly, and `waiting || exit 0` still
swallows the blocked case, because that idiom collapses every non-zero code.

**Arming that watchdog is a manual step, once per install.** Point it at
`.showrunner/hooks/waiting-probe.sh`, **not** at `showrunner waiting` directly — the probe maps
the three codes above onto the two-plus-unknown contract a watchdog expects, so a BLOCKED Crawler
rings rather than being reported as a broken probe, and it never answers "waiting" from inside a
worktree, where a Crawler would otherwise silence its own watchdog with a sibling's liveness.
`showrunner doctor` names the file and says whether it is armed.

It cannot arm it for you: a probe an agent can set is a watchdog an agent can switch off, and one
that always exits 0 reads as "always waiting". Until it is armed, an orchestrator that has
correctly dispatched a full wave looks exactly like one that fell asleep, gets rung, and
eventually pages you for a run that was behaving perfectly.

## What a Crawler's harness gets

A harness that resolves its commit gate **per tree** — the correct design, since what a change
owes is a fact about a tree — must refuse when the tree being committed carries no record. That
lands on the orchestrator, because `git worktree add` copies **tracked files only**: an untracked
harness never crosses, and the Crawler is denied its first commit.

The loud failure is the easy one. The quiet one is why `showrunner spawn` provisions the harness
itself rather than running its installer: an installer seeds user-owned files **only if absent**,
so a fresh install in a worktree yields a blank `verify.yaml` — **a commit gate that owes nothing
and reports success** — plus default invariants and default write roots. Nothing errors. The party
simply plays by two rule sets, and the weaker one is the one running unattended in N worktrees.

So showrunner prefers the harness's **own installer** when one is configured, because that is the
thing that knows how to provision a tree from a parent rather than from blank templates. Failing
that it copies the harness minus whatever it declares as runtime state — read from the harness's
*own* ignore file, because session state belongs to a session and must never be handed to a
Crawler.

It **never copies the hook-registration file**, because the installer *merges* its hooks, preserving the project's own statusLine, permissions and unrelated
hooks, and warning about a pre-existing non-harness hook on an event it manages — a stray Stop
hook from an older harness fights it over turn-ends and presents as "the orchestrator is
mysteriously flaky". A wholesale copy discards the settings and silently drops the warning.

And showrunner does **not** compare the rule files itself. It asks the harness, which answers
about its own trees: which files are rules, which are notes, and whether this tree matches its
parent. A list hardcoded here would drift — one that knows nothing of the harness's notes tier
cannot see a diverged ledger. A verdict of drifted or undetermined aborts the spawn; "could not
tell" and "matched" are never the same answer.

## Docs

- [`docs/DESIGN.md`](docs/DESIGN.md) — the design notes and what remains open.
- [`docs/BOUNDARY.md`](docs/BOUNDARY.md) — who owns what across showrunner and `game_loop`, the
  standing direction for cross-repo fixes, and what showrunner currently assumes about the layer
  below (with the line numbers it was verified against).
- [`llms.txt`](llms.txt) — the operational brief, if you are an agent.

Design records for things that are **built**, each stating which steps are not:

- [`docs/plans/worktree-lease.md`](docs/plans/worktree-lease.md) — one session per worktree,
  enforced by a lease and a PreToolUse guard rather than described by the campaign record. The
  guard denies, from a tracked shim, registered, with `doctor` checking all three. `worktree
  takeover` is the step that is not built.
- [`docs/plans/central-install.md`](docs/plans/central-install.md) — opt-in `install.sh --central`:
  one machine-wide copy of the code, every project keeping its own config. Works end to end and
  is reversible; `doctor`'s central checks and the campaign record's central SHA are not built,
  so a mid-campaign `self --pin` is invisible to a running campaign.
