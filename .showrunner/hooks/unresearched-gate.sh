#!/usr/bin/env bash
# REFUSE A TURN-END WHOSE MESSAGE ADMITS SOMETHING WAS NOT RESEARCHED.
#
# Jonah, 2026-11-09: "I don't ever want to receive a message I have to read when you've mentioned
# that you blatantly haven't researched something." He had just received one: a recommendation
# carrying "I haven't seen a real /swagger.json". An admission like that hands the reader a
# claim to discount and a check to request, when the check was the agent's to run. If it can be
# named, it can be done; if it truly cannot be done without him, that is a BLOCKER, said in the
# present tense as an ask ("needs your go-ahead"), and this lets it through.
#
# THE WHOLE MESSAGE, SENTENCE BY SENTENCE, unlike the future-tense gate's closing paragraph: an
# admission in the middle of a recommendation is the exact case reported.
#
# MEASURED ON THIS REPO'S OWN TRANSCRIPT before shipping: 337 turn-ending messages. The first
# pattern, which included bare "unverified/untested/unchecked", matched 28, a third of them
# describing code ("goes unchecked") or someone else's audit. First person only matched 10: 8
# real admissions ("I never actually read the code", "I haven't measured it, so I didn't
# guess") and 2 false. One was a refusal with its hazard stated ("I haven't run X: it would
# swap the harness under live sessions"); the other was a hypothetical in italics. Both are
# exempted below.
#
# FAILS OPEN, like every guard here: no transcript or no payload allows, and says it was not
# checked. A turn-end gate that hard-fails on its own plumbing blocks the write that repairs it.
set -u

_hb="${SHOWRUNNER_HEARTBEAT:-}"
if [ -z "$_hb" ]; then
  _hb_root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)}"
  [ -n "${_hb_root:-}" ] && [ -d "$_hb_root/.showrunner" ] \
    && _hb="$_hb_root/.showrunner/hook-heartbeat.jsonl"
fi
if [ -n "$_hb" ]; then
  printf '{"hook":"unresearched-gate","ts":%s}\n' "$(date +%s)" >> "$_hb" 2>/dev/null || true
fi

payload="$(cat 2>/dev/null || true)"
[ -n "$payload" ] || exit 0

python3 - "$payload" <<'PYEOF'
import json, os, re, sys

try:
    hook = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)

path = hook.get("transcript_path") or ""
if not path or not os.path.exists(path):
    print("unresearched gate: ALLOWED WITHOUT BEING CHECKED — no transcript to read.",
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
    print("unresearched gate: ALLOWED WITHOUT BEING CHECKED — the transcript could not be read.",
          file=sys.stderr)
    sys.exit(0)

if not last:
    sys.exit(0)

# Somebody else's words are reported, not uttered: quoted lines, code, and short quoted or
# italic spans (a hypothetical like *I didn't look where you put it* is a phrase, not a claim).
text = "\n".join(l for l in last.splitlines() if not l.lstrip().startswith(">"))
text = re.sub(r"```.*?```", "", text, flags=re.S)
text = re.sub(r"[\"“`][^\"”`\n]{0,120}[\"”`]|\*[^*\n]{1,80}\*(?!\*)", '""', text)

ADMIT = re.compile(
    r"\bi\s+(?:have\s+not|haven['’]t|did\s+not|didn['’]t|never|have\s+yet\s+to)\s+"
    r"(?:yet\s+)?(?:actually\s+)?"
    r"(?:checked|check|verified|verify|researched|research|looked|look|read|confirmed|confirm|"
    r"tested|test|seen|measured|measure|run|ran|tried|try|traced|reproduced|probed|opened)\b"
    r"|\b(?:still\s+)?needs?\s+(?:to\s+be\s+)?(?:verified|checked|confirmed|researched)\b"
    r"|\bthe\s+one\s+(?:thing|part|piece)\s+i\s+(?:didn['’]t|did\s+not|haven['’]t)\b"
    r"|\bi(?:['’]m|\s+am)\s+not\s+sure\s+(?:whether|if)\b"
    r"|\bi\s+don['’]t\s+(?:yet\s+)?know\s+(?:whether|if)\b"
    r"|\bwithout\s+having\s+(?:checked|read|verified|run|looked)\b", re.I)

# A decision handed to the human, or a refusal with its hazard stated, is not a research gap.
EXEMPT = re.compile(
    r"yours\s+to\s+decide|your\s+(?:call|go-ahead|ok|approval|decision|authori[sz]ation)"
    r"|decision\s+that['’]s\s+yours|needs\s+your|deliberately|on\s+purpose"
    r"|it\s+would\s+(?:swap|overwrite|break|destroy|wipe|delete|clobber)", re.I)

for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
    m = ADMIT.search(sentence)
    if m and not EXEMPT.search(sentence):
        print(
            "STOP REFUSED — this message admits something was not researched:\n"
            "\n"
            "    %s\n"
            "\n"
            "Jonah does not want to read a message that says it skipped the research. If you can\n"
            "name what was not checked, check it now, then rewrite the message on what you found.\n"
            "If it genuinely cannot be checked without him (it needs his approval, his account,\n"
            "or a decision that is his), say THAT, once, as a present-tense ask.\n"
            "\n"
            "This fires on first-person admissions (\"I haven't checked\", \"I never read\",\n"
            "\"I'm not sure whether\"), not on a description of code that goes unchecked."
            % sentence.strip()[:240], file=sys.stderr)
        sys.exit(2)
sys.exit(0)
PYEOF
