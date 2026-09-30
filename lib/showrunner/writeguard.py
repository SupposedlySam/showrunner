"""Enforce a role's `writes` — on the edit tools AND on Bash (#95).

showrunner published `writes` for a hook of the project's to enforce, and said so in the banner:
"a hook of yours must, and it must cover Bash or a heredoc walks straight past it". Reported: a
campaign-lead's subagent had an Edit in `app/**` refused by the project's guard, made the
identical edit through a Python heredoc in Bash, and committed it. Saying where the hole is did
not close it, so this closes it: a PreToolUse guard over Write, Edit, MultiEdit, NotebookEdit and
Bash, deciding by the SAME resolution `whoami --porcelain` publishes. A subagent inherits its
parent's session id, so it resolves the same role and meets the same guard.

WHAT IT READS IN A BASH COMMAND — targets, not mentions:
  - redirections: `>`, `>>`, `&>`, `>|` and fd-prefixed forms (not `/dev/*`, not `>&2`)
  - `tee`, `sed -i`, `perl -i`, `cp`/`install`/`ln`/`rsync` destinations, `mv` sources and
    destination, `rm`, `rmdir`, `unlink`, `touch`, `truncate`, `mkdir`, `shred`, `dd of=`
  - `sh`/`bash`/`zsh -c "<command>"`, read recursively
  - inline interpreter code — `python3 -c`, `node -e`, `ruby -e`, `perl -e`, or a heredoc fed to
    one — that calls a write function: every LITERAL path it names is treated as a target,
    because which of them it writes cannot be told without running it.

WHAT IT CANNOT SEE, named rather than implied: a path built from a variable, a script file that
writes once run, `git checkout`/`git restore`/`git apply`, and any program not listed above.
"""
import os
import re
import shlex

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash"

_PUNCT = "();<>|&"
_OPERATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", ";;"}
_OPERAND_WRITERS = {"rm", "rmdir", "unlink", "touch", "truncate", "mkdir", "shred", "tee", "mv"}
_DEST_WRITERS = {"cp", "install", "ln", "rsync"}
_SHELLS = {"sh", "bash", "zsh", "dash"}
_INTERPRETERS = {"python", "python3", "python2", "node", "ruby", "perl"}
_PREFIXES = {"sudo", "command", "builtin", "nohup", "time", "env", "exec"}
_FLAG_VALUES = {"truncate": {"-s", "--size", "-r", "--reference"},
                "mkdir": {"-m", "--mode"}, "install": {"-m", "--mode", "-o", "-g"},
                "tee": set(), "sed": {"-e", "-f", "--expression", "--file"},
                "perl": {"-e", "-E", "-M", "-I"}}
_HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1[^\n]*\n(.*?)^[ \t]*\2[ \t]*$", re.S | re.M)
_WRITE_CALL = re.compile(
    r"open\([^)]*,\s*['\"][^'\"]*[wax+]|write_text\(|write_bytes\(|writeFileSync|writeFile\(|"
    r"appendFile|File\.write|File\.open\([^)]*['\"][wa]|shutil\.(?:copy|move|rmtree)|"
    r"os\.(?:remove|unlink|rename|replace|rmdir|makedirs|mkdir)|\.unlink\(|\.rmdir\(|"
    r"\.rename\(|\.replace\(|\.touch\(|\.mkdir\(|print\s+\{?\s*\$?\w*\s*\}?\s*>|unlinkSync|rmSync")
_STRING = re.compile(r"(['\"])((?:(?!\1)[^\\\n]|\\.){1,300})\1")
_PATHLIKE = re.compile(r"^[~.]?[\w.@+-]*(?:/[\w.@+-]+)+/?$|^[\w@+-]+\.[A-Za-z0-9]{1,8}$")


# ----------------------------------------------------------------------------- policy
def _glob_re(pattern):
    """gitignore-flavoured: `**` spans directories, `*` stays in one, and a pattern with no `/`
    matches a NAME at any depth (`*.md` is every markdown file, not only the root's)."""
    p = pattern.strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    anchored = "/" in p.rstrip("/")
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif p.startswith("**", i):
            out, i = out + ".*", i + 2
        elif p[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif p[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(p[i]), i + 1
    out = out.rstrip("/") if out.endswith("/") else out
    return re.compile((out if anchored else r"(?:.*/)?" + out) + r"\Z")


def matches(pattern, rel):
    """Does `pattern` cover `rel` — the path itself, or a directory it sits inside?"""
    rx = _glob_re(pattern)
    parts = rel.strip("/").split("/")
    return any(rx.match("/".join(parts[:n])) for n in range(len(parts), 0, -1))


def permits(writes, rel):
    """(allowed, why) for one tree-relative path under a role's `writes`.

    A LIST is the globs the role MAY write; a mapping may carry `allow` and/or `deny`, and deny
    wins. Any other shape is not enforced — a policy this cannot read is one it must not invent.
    """
    if not writes:
        return True, None
    if isinstance(writes, (list, tuple)):
        hit = next((g for g in writes if matches(str(g), rel)), None)
        return (True, None) if hit else (False, "not in may-write %s" % ", ".join(map(str, writes)))
    if isinstance(writes, dict):
        deny = next((g for g in writes.get("deny") or [] if matches(str(g), rel)), None)
        if deny:
            return False, "matches may-NOT-write %s" % deny
        allow = writes.get("allow")
        if allow and not any(matches(str(g), rel) for g in allow):
            return False, "not in may-write %s" % ", ".join(map(str, allow))
    return True, None


# ----------------------------------------------------------------------------- parsing
def _tokens(text):
    lex = shlex.shlex(text, posix=True, punctuation_chars=_PUNCT)
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        return list(lex)
    except ValueError:
        return None


def _is_redirect(tok):
    return bool(tok) and set(tok) <= set("<>&|") and ">" in tok


def _operands(argv, verb):
    values = _FLAG_VALUES.get(verb, set())
    out, skip = [], False
    for a in argv[1:]:
        if skip:
            skip = False
            continue
        if a in values:
            skip = True
            continue
        if a.startswith("-") and a != "-":
            continue
        out.append(a)
    return out


def _inline_paths(code):
    """Every literal path an inline program names, IF it calls something that writes."""
    if not code or not _WRITE_CALL.search(code):
        return []
    return [m.group(2) for m in _STRING.finditer(code) if _PATHLIKE.match(m.group(2))]


def _argv_targets(argv, bodies, depth):
    while argv and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]) or argv[0] in _PREFIXES):
        argv = argv[1:]
    if not argv:
        return []
    verb = os.path.basename(argv[0])
    if verb in _OPERAND_WRITERS:
        return _operands(argv, verb)
    if verb in _DEST_WRITERS:
        ops = _operands(argv, verb)
        return ops[-1:] if len(ops) >= 2 else []
    if verb == "dd":
        return [a[3:] for a in argv[1:] if a.startswith("of=")]
    if verb == "sed" and any(a == "-i" or a.startswith("-i") or a.startswith("--in-place")
                             for a in argv[1:]):
        ops = _operands(argv, "sed")
        scripted = any(a in ("-e", "-f", "--expression", "--file") for a in argv[1:])
        return ops if scripted else ops[1:]
    if verb in _SHELLS and "-c" in argv[1:]:
        i = argv.index("-c")
        return targets_of(argv[i + 1], depth + 1)[0] if i + 1 < len(argv) and depth < 3 else []
    if verb in _INTERPRETERS:
        if verb == "perl" and any(re.match(r"^-\w*i", a) for a in argv[1:]):
            return [a for a in _operands(argv, "perl")]
        code = None
        for flag in ("-c", "-e", "-E"):
            if flag in argv[1:]:
                i = argv.index(flag)
                code = argv[i + 1] if i + 1 < len(argv) else None
                break
        if code is None and bodies:
            code = bodies.pop(0)
        return _inline_paths(code)
    return []


def targets_of(command, depth=0):
    """(targets, unparsed) — the paths a Bash command would write, as written in it."""
    bodies = [m.group(3) for m in _HEREDOC.finditer(command or "")]
    text = _HEREDOC.sub(lambda m: m.group(0).split("\n", 1)[0] + "\n", command or "")
    toks = _tokens(text.replace("\n", " ; "))
    if toks is None:
        return [], True
    out, argv, i = [], [], 0
    def flush():
        # a heredoc feeds the command it is attached to; one fed to a non-interpreter is
        # content (`cat > f <<EOF`), so its body is consumed without being read.
        if argv:
            fed = "<<" in argv
            clean = [a for a in argv if a != "<<"]
            if fed and os.path.basename(clean[0]) not in _INTERPRETERS and bodies:
                bodies.pop(0)
            out.extend(_argv_targets(clean, bodies, depth))
    while i < len(toks):
        t = toks[i]
        if t in _OPERATORS:
            flush()
            argv = []
        elif t in ("<<", "<<-"):
            argv.append("<<")
            i += 1                      # the delimiter word
        elif t == "<<<":
            i += 1
        elif _is_redirect(t):
            if argv and argv[-1].isdigit():
                argv.pop()              # `2>` — the fd, not an operand
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            i += 1
            if not (t.endswith("&") and (nxt.isdigit() or nxt == "-")) and \
                    not nxt.startswith("/dev/") and nxt not in _OPERATORS:
                out.append(nxt)
        elif t.startswith("<") and set(t) <= set("<&"):
            i += 1                      # an input redirect's source
        else:
            argv.append(t)
        i += 1
    flush()
    return [t for t in out if t], False


# ----------------------------------------------------------------------------- decision
def _trees(cfg):
    from .util import git
    rc, out, _ = git(["worktree", "list", "--porcelain"], cwd=cfg.root)
    roots = [l[len("worktree "):] for l in out.splitlines() if l.startswith("worktree ")] \
        if rc == 0 else []
    roots = [os.path.realpath(r) for r in roots] or [os.path.realpath(cfg.root)]
    return sorted(set(roots), key=len, reverse=True)


def _resolve(path, base):
    path = os.path.expanduser(path)
    full = os.path.normpath(os.path.join(base, path))
    head, tail = full, []
    while head and not os.path.exists(head) and head != os.path.dirname(head):
        head, name = os.path.split(head)
        tail.insert(0, name)
    return os.path.join(os.path.realpath(head), *tail) if tail else os.path.realpath(head)


def verdict(cfg, session, tool, tool_input, cwd=None):
    """(allowed, message). Never raises on a payload it cannot parse — it allows and says so."""
    from .roles import resolution
    r = resolution(cfg, session)
    writes = (r.get("policy") or {}).get("writes")
    if not r.get("enforced") or not writes:
        return True, ""
    tool_input = tool_input or {}
    base = cwd or os.getcwd()
    if tool in WRITE_TOOLS:
        raw = [tool_input.get(k) for k in ("file_path", "notebook_path")
               if isinstance(tool_input.get(k), str)]
        unparsed = False
    elif tool == "Bash":
        raw, unparsed = targets_of(tool_input.get("command") or "")
    else:
        return True, ""
    scratch = os.path.realpath(r["scratch"]) if r.get("scratch") else None
    trees = _trees(cfg)
    refused = []
    for operand in raw:
        path = _resolve(operand, base)
        if scratch and (path == scratch or path.startswith(scratch + os.sep)):
            continue
        root = next((t for t in trees if path == t or path.startswith(t + os.sep)), None)
        if not root:
            continue                    # outside this repo: not a role's business
        rel = os.path.relpath(path, root)
        if rel == "." or rel == ".git" or rel.startswith(".git" + os.sep):
            continue
        ok, why = permits(writes, rel)
        if not ok:
            refused.append((operand, rel, why))
    if refused:
        inline = tool == "Bash" and any(
            os.path.basename(a) in _INTERPRETERS for a in (tool_input.get("command") or "").split())
        return False, (
            "BLOCKED: role %s may not write:\n%s\n"
            "The same rule refuses an Edit and the Bash route around it — a heredoc, `sed -i`, "
            "`tee` or a redirection is the same write.%s\n"
            "If this role SHOULD write there, its `writes` in ~/.config/showrunner/roles.json is "
            "where that changes, and that is the operator's decision, not this session's. If the "
            "change belongs to another role, hand it to that role."
            % (r.get("role"), "\n".join("  %s  (%s — %s)" % (o, rel, why)
                                        for o, rel, why in refused),
               ("\nThe inline program names that path and calls something that writes, and which "
                "path it writes cannot be told without running it — if it only READS that path, "
                "read it in a separate command.") if inline else ""))
    if unparsed:
        return True, ("showrunner write guard: this command could not be tokenized (an unbalanced "
                      "quote?), so its write targets were NOT checked against role %s."
                      % r.get("role"))
    return True, ""
