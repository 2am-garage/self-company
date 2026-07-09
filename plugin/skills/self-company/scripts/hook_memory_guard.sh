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
AMBIG_CHARS = set("$`*?[]")


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

# Evaluate each pipeline/chain segment independently, IN ORDER, simulating any
# `cd` so a same-line `cd X && rm Y` resolves Y against the post-cd directory
# (not the directory the whole command started in).
segments = re.split(r"&&|\|\||[;|&\n]", cmd)


def check_targets(paths):
    for a in paths:
        if a.startswith("-"):
            continue                   # a flag (e.g. -rf), never a path arg
        if is_mem_literal(a):
            deny(REASON)
        if cur_cwd is None:
            if not os.path.isabs(a):
                deny(AMBIG_REASON)      # relative + unknown cwd -> fail closed
            resolved = os.path.realpath(a)
        else:
            resolved = resolve(cur_cwd, a)
        if in_store(resolved):
            deny(REASON)


for seg in segments:
    seg = seg.strip()
    if not seg:
        continue
    try:
        toks = shlex.split(seg)
    except ValueError:
        toks = seg.split()
    if not toks:
        continue
    # Skip only LEADING env-assignment tokens (VAR=val) to find the command word;
    # leave the argument list untouched so no path can be silently dropped.
    i = 0
    while i < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]):
        i += 1
    if i >= len(toks):
        continue
    head = toks[i].rsplit("/", 1)[-1]  # basename (/bin/rm -> rm)
    args = toks[i + 1:]

    if head == "cd":
        positional = [a for a in args if not a.startswith("-")]
        target = positional[0] if positional else "~"
        if cur_cwd is None:
            continue  # already ambiguous; stays ambiguous
        if any(ch in target for ch in AMBIG_CHARS):
            cur_cwd = None            # variable/substitution/glob -> unknown
            continue
        cur_cwd = resolve(cur_cwd, os.path.expanduser(target))
        continue

    if head in DELETERS:
        check_targets(args)
    elif head == "find" and "-delete" in args:
        # `find <memory> -delete` is a disguised recursive rm. Deny ONLY when
        # -delete is present so a plain read-only `find` over memory still works.
        check_targets([a for a in args if not a.startswith("-")])
    elif head == "mv":
        # Moving a memory path AWAY = a disguised delete. Deny if a memory path
        # appears as any source (i.e. not solely the final destination arg).
        positional = [a for a in args if not a.startswith("-")]
        sources = positional[:-1] if len(positional) >= 2 else positional
        check_targets(sources)

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
