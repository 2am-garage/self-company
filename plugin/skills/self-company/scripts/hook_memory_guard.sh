#!/usr/bin/env bash
###############################################################################
# hook_memory_guard.sh — PreToolUse gate (Phase 10, Item 6).
#
# Denies any Bash command that would PHYSICALLY delete or move-away a path under
# .company/memory/ (rm / unlink / shred / rmdir / `mv <memory-path> elsewhere`).
# Physical deletion of memory is the deterministic decay reap's job (Phase 6);
# agents must TOMBSTONE (status: archived) instead. Skill-owned, host-independent
# enforcement of the no-rm rule — defense in depth beside the tar floor.
#
# CONTRACT (PreToolUse): reads stdin JSON {tool_name, tool_input:{command}}.
#   DENY : exit 0, stdout {"hookSpecificOutput":{"hookEventName":"PreToolUse",
#          "permissionDecision":"deny","permissionDecisionReason":"..."}}
#   ALLOW: same shape with "permissionDecision":"allow".
# Exit 0 + deny (NOT exit 2) so the reason surfaces cleanly to the user.
#
# FAIL-OPEN: opt-in guard first (no .company -> silent exit 0); any parse error
# -> allow. A hook bug must NEVER block a legitimate command.
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

# Decision logic lives in python3 (stdlib json + shlex) for robust tokenizing.
read -r -d '' PYCODE <<'PY' || true
import sys, json, shlex

MEM = ".company/memory/"
DELETERS = {"rm", "unlink", "shred", "rmdir"}


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


def is_mem(tok):
    # A path token referencing the memory store (relative or absolute).
    t = tok.strip().strip('"').strip("'")
    return MEM in t or t.rstrip("/").endswith(".company/memory")


REASON = ("physical deletion of memory is the deterministic decay reap's job "
          "(Phase 6) — tombstone instead (status: archived)")

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

# Evaluate each pipeline/chain segment independently so a benign command sharing
# a line with a destructive one is judged on its own tokens.
import re
segments = re.split(r"&&|\|\||[;|&\n]", cmd)
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

    if head in DELETERS:
        if any(is_mem(a) for a in args):
            deny(REASON)
    elif head == "mv":
        # Moving a memory path AWAY = a disguised delete. Deny if a memory path
        # appears as any source (i.e. not solely the final destination arg).
        positional = [a for a in args if not a.startswith("-")]
        sources = positional[:-1] if len(positional) >= 2 else positional
        if any(is_mem(s) for s in sources):
            deny(REASON)

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
