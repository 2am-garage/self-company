#!/usr/bin/env bash
###############################################################################
# hook_memory_guard.sh — PreToolUse gate (Phase 10, Item 6; hardened Phase 22,
# Phase 26 Item 3).
#
# Denies any Bash command that would PHYSICALLY delete or move-away a path under
# .company/memory/ — OR the .company store ROOT (rm -rf .company wipes memory too)
# — via rm / unlink / shred / rmdir / truncate / `find <mem> -delete` / `mv
# <memory-path> elsewhere`.
# Physical deletion of memory is the deterministic decay reap's job (Phase 6);
# agents must TOMBSTONE (status: archived) instead. Skill-owned, host-independent
# enforcement of the no-rm rule — defense in depth beside the tar floor.
#
# Phase 26 Item 3 — close the `cd` bypass. The Phase-22 guard matched only the
# command's own literal TOKENS ("does this string look like .company/memory"),
# so `cd .company/memory && rm -rf L0-working/*` sailed straight through: the
# rm's own argument never mentions ".company" at all. RESOLVE, DON'T TRACK: we
# read the hook input's `cwd` (the harness's real, persisted working directory
# at the moment this command is about to run) and realpath-resolve every
# deleter path argument against it — following symlinks and `..` traversal —
# rather than pattern-matching literal text. A single command that itself
# contains a `cd` (e.g. `cd .company && rm -rf memory`) is handled by
# simulating that `cd` across the command's own segments, in order, starting
# from the real hook cwd. If a `cd` target can't be resolved with confidence
# (a shell variable, command substitution, or glob) we don't know the
# effective cwd for anything after it — FAIL CLOSED for any later RELATIVE
# deleter argument (a legitimate command can always be re-run with an absolute
# path outside the store; this guard is deleters-only, so the false-positive
# cost is near zero). The Phase-22 literal-text check is KEPT as a fast,
# resolution-independent backstop (it also catches, deliberately broadly, ANY
# absolute path that merely *looks* like a `.company` root, even one outside
# THIS project — the original conservative choice) — either check tripping is
# enough to deny.
#
# GIB re-attack (MUST-FIX 2) — cd-equivalents & wrapper structure. The naive
# raw-text segment splitter tore `bash -c "…"` and `( … )` apart before
# tokenizing, and only the literal token `cd` was treated as a directory
# change, so `pushd` / `env -C` / subshell / `bash -c` walked a deleter into
# the store undetected. The parser now (1) tokenizes QUOTE- and PAREN-aware
# (shlex punctuation_chars) so quoted scripts stay intact and control
# operators/subshells are real boundaries; (2) treats `pushd` and `env -C` /
# `--chdir` as cd-equivalents and scopes subshell cd with a stack; and (3)
# fail-closes any deleter it cannot fully resolve — a nested `bash -c` / `eval`
# / `xargs` whose script invokes a deleter, or a wholly-unparseable
# (unbalanced-quote) command that still mentions a deleter — is DENIED rather
# than silently allowed.
#
# CONTRACT (PreToolUse): reads stdin JSON {tool_name, tool_input:{command}, cwd}.
#   DENY : exit 0, stdout {"hookSpecificOutput":{"hookEventName":"PreToolUse",
#          "permissionDecision":"deny","permissionDecisionReason":"..."}}
#   ALLOW: same shape with "permissionDecision":"allow".
# Exit 0 + deny (NOT exit 2) so the reason surfaces cleanly to the user.
#
# FAIL-OPEN: opt-in guard first (no .company -> silent exit 0); any parse error
# -> allow. A hook bug must NEVER block a legitimate command. (The Item-3
# cd-ambiguity rule above is fail-CLOSED, but only within the narrow deleter
# scope this hook already governs — it never blocks a non-deleting command.)
###############################################################################
set -uo pipefail

# --- Opt-in guard: inert in any repo without a self-company .company/ -----------
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
if [[ ! -d "$PROJECT_DIR/.company" ]]; then
  exit 0
fi

# Slurp the hook stdin (kept in a var so we can hand it to python3 explicitly and
# avoid depending on jq for JSON parsing).
INPUT="$(cat)"

# Decision logic lives in python3 (stdlib json + shlex + os.path) for robust
# tokenizing and path resolution. PROJECT_DIR is passed via env (the heredoc is
# single-quoted, so no bash interpolation/injection risk).
export SC_PROJECT_DIR="$PROJECT_DIR"

read -r -d '' PYCODE <<'PY' || true
import sys, json, shlex, os, re

PROJECT_DIR = os.environ.get("SC_PROJECT_DIR", "")
STORE = os.path.realpath(os.path.join(PROJECT_DIR, ".company")) if PROJECT_DIR else ""
MEM_ROOT = os.path.join(STORE, "memory") if STORE else ""
MEM_SUBSTR = ".company/memory/"

DELETERS = {"rm", "unlink", "shred", "rmdir", "truncate"}
CD_CMDS = {"cd", "pushd"}                       # GIB re-attack: pushd is a cd too
SHELL_INTERPRETERS = {"bash", "sh", "dash", "zsh", "ksh", "ash", "fish"}
NESTED_WRAPPERS = {"eval", "xargs"}             # run another command we can't resolve
AMBIG_CHARS = set("$`*?[]")
PUNCT = set("();<>|&")                          # shell control punctuation


def allow():
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "no memory-destructive operation detected",
    }}))
    sys.exit(0)


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def is_mem_literal(tok):
    # Phase 22 backstop: a path token that TEXTUALLY looks like the memory
    # store (relative or absolute), independent of any cwd resolution.
    # Deliberately broad: ANY absolute path ending in "/.company" is treated
    # as a store root (even one that isn't literally this project's, since
    # deleting a whole .company store wholesale is dangerous in general).
    t = tok.strip().strip('"').strip("'").rstrip("/")
    if MEM_SUBSTR in tok or t.endswith(".company/memory"):
        return True
    return t == ".company" or t.endswith("/.company")


def resolve(base, arg):
    p = arg if os.path.isabs(arg) else os.path.join(base or "/", arg)
    return os.path.realpath(p)


def in_store(path):
    # path is already realpath'd. Scope matches the original guard exactly:
    # the store ROOT itself, or anything AT/UNDER the memory subtree — not
    # every other subdir of .company (e.g. .company/ops is out of scope).
    if not STORE:
        return False
    return path == STORE or path == MEM_ROOT or path.startswith(MEM_ROOT + os.sep)


REASON = ("physical deletion of memory is the deterministic decay reap's job "
          "(Phase 6) — tombstone instead (status: archived)")
AMBIG_REASON = (
    "a `cd` earlier in this command lands somewhere this guard can't resolve "
    "(a variable/substitution/glob target), so a RELATIVE deletion target "
    "after it can't be proven safe — fail-closed. Re-run with an absolute "
    "path outside .company, or split the cd and the delete into separate "
    "steps")
STRUCT_REASON = (
    "a deleter is wrapped in structure this narrow guard can't fully parse "
    "(nested shell -c / eval / xargs / subshell / unbalanced quoting), so its "
    "target can't be proven to lie outside the memory store — fail-closed "
    "(GIB re-attack). Re-run the deletion directly with an absolute path "
    "outside .company")


def _basename(t):
    return t.rsplit("/", 1)[-1]


def _ambiguous(t):
    return any(ch in t for ch in AMBIG_CHARS)


def tok_is_deleter(a):
    b = _basename(a)
    return b in DELETERS or b in ("find", "mv")


def script_has_deleter(s):
    # Best-effort: does an inline script string invoke a deleter? Used to
    # fail-closed on `bash -c "…rm…"` / `eval "…"` where we won't recursively
    # simulate the nested cwd. shlex-tokenize when we can, else scan words.
    if not isinstance(s, str):
        return False
    try:
        toks = shlex.split(s)
    except ValueError:
        toks = re.findall(r"[A-Za-z0-9_./-]+", s)
    return any(tok_is_deleter(t) or _basename(t) in NESTED_WRAPPERS for t in toks)


def text_has_hard_deleter(s):
    # For a wholly-unparseable command (unbalanced quotes): is a hard deleter
    # even present? If so, fail-closed; if not, there is nothing to guard.
    words = re.findall(r"[A-Za-z0-9_./-]+", s or "")
    return any(_basename(w) in DELETERS for w in words)

try:
    data = json.load(sys.stdin)
except Exception:
    allow()  # unparseable stdin -> never block

# Matcher is Bash(rm *); if some other tool slips through, don't touch it.
if not isinstance(data, dict) or data.get("tool_name") != "Bash":
    allow()

cmd = ((data.get("tool_input") or {}) if isinstance(data.get("tool_input"), dict) else {}).get("command") or ""
if not isinstance(cmd, str) or not cmd.strip():
    allow()

# Item 3: the harness's own persisted cwd at the moment this command is about
# to run — the ground truth a `cd` in a PRIOR, separate tool call already
# updated. Falls back to PROJECT_DIR (the pre-Phase-26 assumption) if absent.
hook_cwd = data.get("cwd")
if not isinstance(hook_cwd, str) or not hook_cwd.strip():
    hook_cwd = PROJECT_DIR
if hook_cwd and not os.path.isabs(hook_cwd):
    hook_cwd = os.path.join(PROJECT_DIR or "/", hook_cwd)
cur_cwd = os.path.realpath(hook_cwd) if hook_cwd else None

# GIB re-attack (MUST-FIX 2): tokenize QUOTE- and PAREN-aware so a deleter
# hidden inside `bash -c "…"`, a subshell `( … )`, or `env -C …` can't be torn
# apart at raw-text level before we ever see it. shlex(posix, punctuation_chars)
# keeps quoted strings intact as single tokens AND surfaces the control
# operators; unbalanced quoting is unparseable -> fail-closed if a deleter is
# even present.
try:
    _lx = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    _lx.whitespace_split = True
    raw_tokens = list(_lx)
except ValueError:
    if text_has_hard_deleter(cmd):
        deny(STRUCT_REASON)            # can't parse + a deleter is present
    allow()


def _normalize(tokens):
    # A run of punctuation chars may lex as one token (e.g. ');'). Split it into
    # atoms: '(' and ')' individually (subshell scoping), every other operator
    # (&& || | & ; < >) collapsed to a single ';' separator — all we need is
    # "flush the current simple command here".
    out = []
    for tok in tokens:
        if tok and all(c in PUNCT for c in tok):
            pending = False
            for ch in tok:
                if ch in "()":
                    if pending:
                        out.append(";"); pending = False
                    out.append(ch)
                else:
                    pending = True
            if pending:
                out.append(";")
        else:
            out.append(tok)
    return out


toks_norm = _normalize(raw_tokens)

# CWD[0] is the simulated cwd; a stack scopes subshell `( … )` cd so it doesn't
# leak out (avoids false positives after the subshell closes).
CWD = [cur_cwd]
cwd_stack = []


def check_targets(paths, cwd):
    for a in paths:
        if a.startswith("-"):
            continue                   # a flag (e.g. -rf), never a path arg
        if is_mem_literal(a):
            deny(REASON)
        if cwd is None:
            if not os.path.isabs(a):
                deny(AMBIG_REASON)      # relative + unknown cwd -> fail closed
            resolved = os.path.realpath(a)
        else:
            resolved = resolve(cwd, a)
        if in_store(resolved):
            deny(REASON)


def _cd_target(args):
    positional = [a for a in args if not a.startswith("-")]
    return positional[0] if positional else "~"


def _apply_cd(cwd, target):
    if cwd is None:
        return None                    # already ambiguous; stays ambiguous
    if _ambiguous(target):
        return None                    # variable/substitution/glob -> unknown
    return resolve(cwd, os.path.expanduser(target))


def _parse_env(rest):
    # `env [-i] [-u VAR] [VAR=val…] [-C DIR | --chdir DIR|=DIR] CMD ARGS…`
    # -> (cd_target_or_None, inner_command_tokens). The chdir is NON-persistent
    # (only the wrapped command sees it).
    cd_target = None
    j = 0
    while j < len(rest):
        t = rest[j]
        if t in ("-C", "--chdir"):
            if j + 1 < len(rest):
                cd_target = rest[j + 1]; j += 2; continue
            j += 1; continue
        if t.startswith("--chdir="):
            cd_target = t.split("=", 1)[1]; j += 1; continue
        if t == "-u":
            j += 2; continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            j += 1; continue           # VAR=val assignment
        if t.startswith("-"):
            j += 1; continue           # -i and other flags
        break                          # first bare word = the wrapped command
    return cd_target, rest[j:]


def check_command(toks, cwd):
    """Analyze one simple command. Returns the (persistently) updated cwd; may
    deny() and never return."""
    i = 0
    while i < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]):
        i += 1                         # skip leading VAR=val
    if i >= len(toks):
        return cwd
    head = _basename(toks[i])
    args = toks[i + 1:]

    if head in CD_CMDS:
        return _apply_cd(cwd, _cd_target(args))

    if head == "env":
        cd_t, inner = _parse_env(args)
        eff = cwd
        if cd_t is not None:
            eff = _apply_cd(cwd, cd_t)
        check_command(inner, eff)      # non-persistent chdir -> discard result
        return cwd

    if head in SHELL_INTERPRETERS:
        # A nested shell (`bash -c "cd … && rm …"`) we won't recursively
        # simulate — if its script invokes a deleter, fail-closed.
        if any(script_has_deleter(a) for a in args):
            deny(STRUCT_REASON)
        return cwd

    if head in NESTED_WRAPPERS:        # eval / xargs run a command we can't resolve
        if any(tok_is_deleter(a) for a in args) or \
                any(script_has_deleter(a) for a in args):
            deny(STRUCT_REASON)
        return cwd

    if head in DELETERS:
        check_targets(args, cwd)
    elif head == "find" and "-delete" in args:
        # `find <memory> -delete` is a disguised recursive rm. Deny ONLY when
        # -delete is present so a plain read-only `find` over memory still works.
        check_targets([a for a in args if not a.startswith("-")], cwd)
    elif head == "mv":
        # Moving a memory path AWAY = a disguised delete. Deny if a memory path
        # appears as any source (i.e. not solely the final destination arg).
        positional = [a for a in args if not a.startswith("-")]
        sources = positional[:-1] if len(positional) >= 2 else positional
        check_targets(sources, cwd)
    return cwd


simple = []


def _flush():
    if simple:
        CWD[0] = check_command(list(simple), CWD[0])
    simple.clear()


for tok in toks_norm:
    if tok == "(":
        _flush()
        cwd_stack.append(CWD[0])       # enter subshell — scope its cd
    elif tok == ")":
        _flush()
        if cwd_stack:
            CWD[0] = cwd_stack.pop()    # restore: subshell cd doesn't leak out
    elif tok == ";":
        _flush()
    else:
        simple.append(tok)
_flush()

allow()
PY

printf '%s' "$INPUT" | python3 -c "$PYCODE"
# Any failure to even launch python3 -> fail open (allow implicitly).
rc=$?
if [[ $rc -ne 0 ]]; then
  # python3 unavailable or crashed: emit an explicit allow, never block.
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"guard fell open"}}'
fi
exit 0
