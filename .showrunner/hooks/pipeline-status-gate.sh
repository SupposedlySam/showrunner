#!/usr/bin/env bash
# PreToolUse(Bash): NOTICE when `$?` is about to read a pipeline's truncator instead of the
# command being judged.
#
# A pipeline's exit status is its LAST command's. `head` and `tail` essentially always succeed,
# so `cmd 2>&1 | head -3; echo "exit=$?"` reports 0 no matter what `cmd` did. The output still
# looks right, which is why reading it carefully does not help.
#
# MEASURED, and the reason this is a gate rather than a note in a file. Across 3,548 Bash
# commands in one session: 23 genuine instances. Re-running the still-reproducible ones with
# and without the pipe, 4 of 7 reported a WRONG status — `showrunner check` 3 read as 0,
# `showrunner campaign` 2 read as 0, `showrunner waiting` 1 read as 0, `llm_chat owed` 2 read
# as 0. One of those false readings became a bug report filed against another team's tool for
# a defect that did not exist.
#
# The sharpest one is showrunner's own. `check` exits 3 on VOID, and its output says why:
# "distinct from 2 (new failures) so a caller that treats non-zero as 'the code is bad' gets a
# code it did not map rather than a wrong answer it will believe." That distinction was argued
# for, implemented, and documented — and then read as 0 by the author of it, through a pipe.
#
# NOTICES, NEVER DENIES. Sometimes the truncator IS the subject (`grep -c … ; [ $? = 0 ]`), and
# a gate that blocks a legitimate shape trains its own bypass. Naming the hazard at the moment
# of use is the whole job.
set -u
payload=$(cat 2>/dev/null || true)

cmd=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if (d.get("tool_name") or "") != "Bash":
    sys.exit(0)
sys.stdout.write((d.get("tool_input") or {}).get("command") or "")
' 2>/dev/null) || exit 0
[ -n "$cmd" ] || exit 0

verdict=$(printf '%s' "$cmd" | python3 -c '
import re, sys
cmd = sys.stdin.read()
# pipefail / PIPESTATUS mean the author has already handled it.
if re.search(r"pipefail|PIPESTATUS", cmd):
    sys.exit(0)
TRUNCATORS = r"\b(head|tail|grep|wc|cat|sed|cut|uniq|sort)\b"
hits = []
for line in cmd.split("\n"):
    s = line.strip()
    if "|" not in s or "$?" not in s:
        continue
    # A line that is TEXT about the pattern — a heredoc body, a comment — is not a command.
    # Left in because the detector that matched its own source is the same defect one layer up.
    if s.startswith(("#", ">", "*")):
        continue
    segs = re.split(r";|&&|\|\|", s)
    for i, seg in enumerate(segs):
        if "$?" not in seg:
            continue
        subject = seg if "|" in seg else (segs[i - 1] if i else "")
        if "|" not in subject:
            continue
        last = subject.rsplit("|", 1)[1]
        if re.search(TRUNCATORS, last):
            hits.append(subject.strip()[:110])
if hits:
    print("\n".join(dict.fromkeys(hits)))
' 2>/dev/null) || exit 0

[ -n "$verdict" ] || exit 0

printf '%s\n' "$verdict" | python3 -c '
import json, sys
lines = [l for l in sys.stdin.read().split("\n") if l.strip()]
msg = ("⚠ `$?` HERE IS THE TRUNCATOR'"'"'S STATUS, NOT THE COMMAND'"'"'S. A pipeline exits with its "
       "LAST command, and head/tail/grep essentially always succeed — so this reports 0 whatever "
       "the real command did, and the output still looks correct.\n\n  "
       + "\n  ".join(lines)
       + "\n\nCapture it without a pipe (`cmd > /tmp/o 2>&1; rc=$?`), or use `set -o pipefail` / "
         "`${PIPESTATUS[0]}`. Measured on this repo: 4 of 7 reproducible instances reported a "
         "wrong status, and `showrunner check`'"'"'s deliberate exit 3 (VOID — nothing was "
         "compared) read as 0.")
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                         "additionalContext": msg}}))
'
exit 0
