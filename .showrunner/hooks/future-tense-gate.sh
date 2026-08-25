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

m = COMMIT.search(closing)
if not m:
    sys.exit(0)
if HANDBACK.search(closing) or HANDBACK_TRAILING.search(closing):
    sys.exit(0)                      # waiting on the human is not promising to continue
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
