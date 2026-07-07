#!/usr/bin/env bash
###############################################################################
# hook_precompact_capture.sh — PreCompact hook: CAPTURE RESCUE (spec Item 5).
#
# Claude Code fires PreCompact BEFORE it compacts/summarizes a session. That
# summary discards the raw Chairman utterances, so a durable fact stated this
# session could be lost before the Stop hook's CAPTURE ever runs. This wrapper
# runs the SAME capture path (capture-trigger.py) over the FULL pre-compaction
# transcript so those facts are captured to L0 first.
#
# Authoritative contract (PreCompact):
#   stdin  {session_id, transcript_path, cwd, hook_event_name:"PreCompact",
#           hookSpecificInput:{matcher:"manual|auto"}}
#   transcript_path = the full pre-compaction transcript.
#   NEVER block compaction — ALWAYS exit 0, even on any error.
#
# Reuse, don't reinvent: capture-trigger.py's per-session cooldown de-dups this
# rescue vs the later Stop capture (same session id → the second fire is a
# logged no-op). We invoke it exactly the way the Stop hook does, just with an
# explicit --transcript/--session from the PreCompact payload; we do NOT bypass
# the cooldown. Fire-and-forget.
#
# Any extra args are forwarded to capture-trigger.py — tests pass --dry-run so
# they never hit a real model; the plugin declaration passes none (real capture).
###############################################################################
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hook_guard.sh
. "$SCRIPT_DIR/hook_guard.sh"
sc_hook_optin        # no .company here → silent exit 0 (plugin fires globally)

# Slurp the hook payload (we own stdin — capture-trigger runs in --transcript
# mode, which never reads stdin, so nothing downstream blocks on the pipe).
PAYLOAD="$(cat 2>/dev/null || true)"

# Extract transcript_path + session_id (python: tolerant of any malformed JSON).
{
  read -r TRANSCRIPT || true
  read -r SESSION || true
} < <(printf '%s' "$PAYLOAD" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read() or "{}")
except Exception:
    d = {}
print((d.get("transcript_path") or "").strip())
print((d.get("session_id") or "").strip())
' 2>/dev/null)

_log() {
  local logdir="$SC_COMPANY/ops/logs"
  mkdir -p "$logdir" 2>/dev/null || true
  printf '%s PreCompact capture-rescue %s\n' \
    "$(date +%FT%T 2>/dev/null)" "$1" >> "$logdir/precompact.log" 2>/dev/null || true
}

# No usable transcript → log a line and exit 0 (never fail / never block).
if [ -z "${TRANSCRIPT:-}" ] || [ ! -f "$TRANSCRIPT" ]; then
  _log "no-transcript"
  exit 0
fi
[ -z "${SESSION:-}" ] && SESSION="precompact"

_log "session=$SESSION transcript=$TRANSCRIPT"
# Same invocation as the Stop hook, pointed at THIS transcript + this .company.
# Cooldown inside capture-trigger de-dups vs the Stop capture. Never fail.
python3 "$SCRIPT_DIR/capture-trigger.py" \
  --company "$SC_COMPANY" --transcript "$TRANSCRIPT" --session "$SESSION" "$@" || true
exit 0
