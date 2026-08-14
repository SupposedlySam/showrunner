#!/usr/bin/env bash
# The PreToolUse entrypoint for the worktree lease guard.
#
# TRACKED ON PURPOSE, AND MACHINE-AGNOSTIC ON PURPOSE — those two facts are the whole reason
# this file exists rather than a hook command in .claude/settings.json naming the binary
# directly. `.claude/settings.json` is tracked, so it crosses into every worktree, but the
# COMMAND it names has to resolve on the other side, and both obvious spellings fail there:
#
#   "$CLAUDE_PROJECT_DIR"/.showrunner/bin/showrunner
#       In a worktree, CLAUDE_PROJECT_DIR is the WORKTREE (verified, WL-01), and .showrunner/
#       is runtime state that `git worktree add` does not carry — it copies TRACKED files
#       only. Dead on arrival in exactly the place the guard is for.
#
#   an absolute path to this machine's checkout, baked into the tracked file
#       Resolves here and is wrong in every other clone. (Not spelled out even as an example:
#       this repo is public and a tracked file carrying somebody's home directory is a rule a
#       stranger inherits — the suite scans for exactly that, and caught this comment.)
#
# So the tracked thing is this shim, which resolves the MAIN checkout at run time the same way
# util.main_checkout does — via --git-common-dir, whose value is the main checkout's .git even
# when called from inside a linked worktree.
#
# FAIL OPEN, NEVER IN SILENCE. When no binary can be found this ALLOWS the tool call and says
# so. A PreToolUse hook that exits non-zero blocks every Write/Edit/Bash including the one
# that would repair it, so hard-failing on our own plumbing would lock the repo against its
# own fix. But allowing without a word is indistinguishable from a guard that ran and was
# content, which is how a rail goes quiet exactly where it is blind — so the allow announces
# itself, `showrunner doctor` reports the absence as an error, and `worktree enter` says one
# line into agent context when the guard is inert.
#
# Keep this file trivially correct. All real logic lives in `showrunner worktree guard`.
set -u

notice() {
  # additionalContext is what actually reaches the agent on an allow. Fixed strings only —
  # this is the piece that must never itself be the thing that breaks.
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}\n' "$1"
  exit 0
}

common="$(git rev-parse --git-common-dir 2>/dev/null)" || common=""
if [ -z "$common" ]; then
  notice "⚠ THE WORKTREE GUARD DID NOT RUN — git could not resolve a repository here, so this tool call was ALLOWED WITHOUT BEING CHECKED. A worktree held by another live session is NOT protected. Check: showrunner doctor"
fi

# --git-common-dir answers relatively (\".git\") when the cwd is the repo root, so resolve it
# against the cwd before taking its parent. Skipping this made the root's parent the repo.
case "$common" in
  /*) ;;
   *) common="$PWD/$common" ;;
esac
root="$(cd "$(dirname "$common")" 2>/dev/null && pwd)" || root=""

# THE SAME ORDER brief.sr_bin resolves in, and deliberately not a second resolver: the
# installed copy wins because it is the one a consumer has, and bin/showrunner is the fallback
# for the repo that IS showrunner and never runs its own installer. Two resolvers that
# disagree about which binary is real is a failure nobody would see until they disagreed.
for candidate in "$root/.showrunner/bin/showrunner" "$root/bin/showrunner"; do
  if [ -x "$candidate" ]; then
    exec "$candidate" worktree guard
  fi
done

notice "⚠ THE WORKTREE GUARD DID NOT RUN — no showrunner binary was found (looked for .showrunner/bin/showrunner and bin/showrunner under the main checkout), so this tool call was ALLOWED WITHOUT BEING CHECKED. A worktree held by another live session is NOT protected. Check: showrunner doctor"
