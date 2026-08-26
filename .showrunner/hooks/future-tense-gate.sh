#!/usr/bin/env bash
# REFUSE A TURN-END WHOSE LAST PARAGRAPH PROMISES WORK INSTEAD OF DOING IT.
#
# This exists because the rule it enforces was hardened at rung 6 — a memory file delivered into
# context — and then broken within the day BY THE AGENT WHO WROTE IT, with the rule in context.
# "Banned" was an overclaim: a reminder is not a gate. game_loop's own ladder says as much, and
# its own invariant says a rule an agent has to remember is followed only some of the time.
#
# WHY THE ABSTRACT RULE COULD BE IGNORED, which is the thing this fixes: a concept-level rule is
# checked against INTENT, and the intent is always fine. An agent that has just finished a long
# correct piece of work does not experience "next I'll X" as stopping — it experiences it as
# courtesy. So the check has to fire on the TEXT, at the moment the turn ends, where the defect
# actually lives. The reasoning was never the broken part; the writing was.
#
# NARROW BY CONSTRUCTION. It reads the LAST PARAGRAPH only, and only first-person future
# commitments about work. A stated blocker is present tense and must survive — an agent that
# swallows "X is blocked on your authorisation" to satisfy a word-ban has traded a visible stall
# for an invisible one, which is worse than what this prevents.
#
# FAILS OPEN, like every other guard here: no transcript, no payload, nothing parseable — allow,
# and say it was not checked. A turn-end gate that hard-fails on its own plumbing blocks the
# write that would repair it.
set -u

# HEARTBEAT FIRST, BEFORE ANYTHING CAN RETURN. Registration, a clean `bash -n`, and "has fired"
# are all facts about a FILE or about the PAST; none of them is a fact about this turn, which is
# the only thing a gate is for. game_loop's auditor measured their Stop gate as unrun for eight
# hours with four green health checks and one honest timestamp — the timestamp being the only
# artifact that records AN INVOCATION, with a time attached.
#
# A boolean "has it ever fired" would have answered yes, correctly, and been useless. So this
# records a time, and it records it before the payload is read: a mark written after the first
# early return leaves the cheapest paths unprovable, and those are the weak ones.
#
# WHAT IT CANNOT SHOW: that a stale stamp means a specific earlier Stop hook blocked this one.
# It proves the gate did not run. Why is a separate question this file does not answer.
# THE SUITE MUST NOT WRITE THE REPO'S OWN RECORD. The first reading of this heartbeat showed
# 28 stamps per burst for this gate and 13 for its sibling — those were test invocations, not
# turn-ends, because the tests run the hook with no CLAUDE_PROJECT_DIR and it fell back to the
# real checkout. A freshly-run suite then makes every hook look freshly REACHED, which is the
# one thing this file exists to answer. An instrument its own tests can forge measures nothing.
_hb="${SHOWRUNNER_HEARTBEAT:-}"
if [ -z "$_hb" ]; then
  _hb_root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)}"
  [ -n "${_hb_root:-}" ] && [ -d "$_hb_root/.showrunner" ] \
    && _hb="$_hb_root/.showrunner/hook-heartbeat.jsonl"
fi
if [ -n "$_hb" ]; then
  printf '{"hook":"future-tense-gate","ts":%s}\n' "$(date +%s)" >> "$_hb" 2>/dev/null || true
fi

payload="$(cat 2>/dev/null || true)"
[ -n "$payload" ] || exit 0

python3 - "$payload" <<'PYEOF'
import json, os, re, sys

try:
    hook = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)                      # unparseable is UNKNOWN, and unknown allows

path = hook.get("transcript_path") or ""
if not path or not os.path.exists(path):
    print("future-tense gate: ALLOWED WITHOUT BEING CHECKED — no transcript to read.",
          file=sys.stderr)
    sys.exit(0)

last = None
try:
    with open(path, errors="ignore") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            msg = rec.get("message") or {}
            if rec.get("type") == "assistant" and isinstance(msg.get("content"), list):
                text = "".join(c.get("text", "") for c in msg["content"]
                               if c.get("type") == "text")
                if text.strip():
                    last = text
except OSError:
    print("future-tense gate: ALLOWED WITHOUT BEING CHECKED — the transcript could not be read.",
          file=sys.stderr)
    sys.exit(0)

if not last:
    sys.exit(0)

# THE CLOSING PARAGRAPH ONLY. A forward-looking sentence in the middle of an explanation is
# ordinary prose; the defect is specifically a turn that ENDS on a promise.
para = [p for p in last.strip().split("\n\n") if p.strip()]
closing = (para[-1] if para else "").strip()

# Quoted lines are somebody else's words — a report of what another agent said must not trip it.
closing = "\n".join(l for l in closing.splitlines() if not l.lstrip().startswith(">"))

COMMIT = re.compile(
    r"(?:^|[.;:!?]\s+|\b)(?:"
    r"next(?:,)?\s+(?:i['’]ll|i\s+will|i\s+am\s+going\s+to)"
    r"|then\s+i['’]ll|then\s+i\s+will"
    r"|after\s+that(?:,)?\s+i"
    r"|i['’]ll\s+(?:take|pay|start|do|tackle|pick|fix|work|handle|move|continue)"
    r"|i\s+will\s+(?:take|pay|start|do|tackle|pick|fix|work|handle|move|continue)"
    r"|moving\s+on\s+to"
    r"|next\s+up"
    # PRESENT CONTINUOUS USED AS FUTURE, and periphrastic future. The list started at `I'll`
    # and missed the whole class: "which I'm answering next", "I'm taking #63 next", "I am
    # going to pay those debts". Caught by the chat-debt waker instead of by this gate, on a
    # turn I ended with exactly that sentence — the gate that exists to catch the shape, blind
    # to three of its forms.
    #
    # `next` is required for the gerund arm, deliberately: "I'm reporting it here" is present
    # tense and true, while "I'm reporting it next" is a promise. Without that anchor the arm
    # would refuse every sentence describing what the turn just did.
    #
    # BOUNDED TO ONE CLAUSE, not just one sentence. `[^.]{0,40}` let the gerund reach across a
    # comma into an unrelated clause: "I'm publishing the fix now, and #64 is next" was refused,
    # and it is two true statements — present-tense work, plus a fact about the queue. Forty
    # characters is plenty of room for that, so sentence-splitting would not have caught it.
    # Clause punctuation and the coordinators are the actual boundary.
    r"|i(?:'|’)?m\s+\w+ing\b(?:(?!\band\b|\bbut\b)[^.,;:—])" + r"{0,40}\bnext\b"
    r"|i\s+am\s+\w+ing\b(?:(?!\band\b|\bbut\b)[^.,;:—])" + r"{0,40}\bnext\b"
    r"|i(?:'|’)?m\s+going\s+to\b"
    r"|i\s+am\s+going\s+to\b"
    r")", re.I)

# MEASURED ON 1,648 REAL CLOSINGS FROM THIS PROJECT'S OWN TRANSCRIPT, not on fixtures the
# author wrote. 16 matched; 11 were real promises and FIVE WERE FALSE BLOCKS — a 31% false-block
# rate among the turns this gate judges, which is the number that matters rather than the 1%
# against all closings. Prompted by another agent reporting ~25% on their own gate, found only
# after shipping, on four fixtures that looked clean.
#
# HANDBACKS ARE THE EXPENSIVE ONES and were four of the five. "Say which and I'll do it", "Say
# the word on scope and I'll start" — the agent is CORRECTLY waiting on a decision that is the
# human's to make. Refusing those forces work to continue when it should be asking, which is the
# exact failure the sibling rule exists to prevent. A gate that turns a correct handback into
# forced motion is worse than the promise it catches.
HANDBACK = re.compile(
    r"(say\s+(?:the\s+word|which)|tell\s+me|let\s+me\s+know|if\s+you|once\s+you"
    r"|unless\s+you|your\s+call|which\s+would\s+you)[^.]{0,80}\bi['’]ll", re.I)

# THE CONDITIONAL CAN TRAIL THE VERB TOO. "I'll work those, unless you'd rather I did X" is the
# same handback with the clause on the other side, and the first version only looked in front —
# so it refused two real closings in the corpus that were correctly waiting on a decision.
HANDBACK_TRAILING = re.compile(r"\bi['’]ll\b[^.]{0,90}?\b(unless|if\s+you|say\s+which"
                               r"|your\s+call|tell\s+me|let\s+me\s+know)\b", re.I)

# A phrase INSIDE quotes is being reported, not made — a retro that quotes its own violation
# reads exactly like committing it. Same class as the >-quoted lines stripped above, arriving
# inline. The fifth false block was precisely this: a turn correcting itself for the sentence.
QUOTED_INLINE = re.compile(r"[\"“'‘*_].{0,60}?(next\s+i['’]ll|then\s+i['’]ll"
                           r"|i['’]ll\s+(?:take|pay|start|do|pick|work|fix|move))", re.I)

# A NEGATED COMMITMENT IS A HANDBACK BY GRAMMAR RATHER THAN BY VERB. "Two open questions,
# neither of which I'll act on unilaterally" is the agent saying it is STOPPING ON PURPOSE —
# the exact behaviour this gate exists to protect — and the future form is what carries it.
# No verb list reaches it, because the verb is the same one a real promise uses. `I'll not`
# and `I won't` never matched COMMIT at all, so only the neither/nor form leaked, and it
# leaked as a refusal to act being punished for saying so.
#
# Reported by game_loop's auditor from a 25-concession corpus. My first check said my gate was
# already safe; it was safe only because the example verb was missing from the list. Swap in
# `fix` or `take` and it refuses. The refutation was of my probe, not of their finding.
NEGATED = re.compile(r"\b(neither|nor|none)\b[^.]{0,40}?\bi['’]ll\b", re.I)

m = COMMIT.search(closing)
if not m:
    sys.exit(0)
if HANDBACK.search(closing) or HANDBACK_TRAILING.search(closing):
    sys.exit(0)                      # waiting on the human is not promising to continue
if NEGATED.search(closing):
    sys.exit(0)                      # declining to act is the opposite of promising to
if QUOTED_INLINE.search(closing):
    sys.exit(0)                      # reporting the phrase is not uttering it

print(
    "STOP REFUSED — your last paragraph PROMISES work instead of doing it.\n"
    "\n"
    "    %s\n"
    "\n"
    "If you can name the next action, you have already established it is identified and\n"
    "unblocked — which is the condition for DOING it, not for announcing it. A long turn that\n"
    "chains fix, verify, commit, push, next item is the expected shape here, not an overreach.\n"
    "\n"
    "Do the thing you just described, then end the turn on what is FINISHED.\n"
    "\n"
    "This fires on the WORD, not on your intent, and deliberately: the abstract version of this\n"
    "rule was hardened and then broken the same day by the agent who wrote it, because a\n"
    "forward pointer reads as courtesy rather than as a stop. It does not fire on a stated\n"
    "BLOCKER — that is present tense, and saying it once is correct."
    % m.group(0).strip(), file=sys.stderr)
sys.exit(2)
PYEOF
