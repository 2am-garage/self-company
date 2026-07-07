#!/usr/bin/env bash
###############################################################################
# hook_sessionend_verify.sh — SessionEnd hook: verify fresh captures (Item 8).
#
# On session end, run the deterministic VERIFY pass (verify_memory.py --apply)
# so THIS session's brand-new L0 captures get source-stamped (verified_date /
# verified_by) BEFORE the next SessionStart report — instead of waiting up to
# 6h for the cron verify. Cheap, deterministic, source-existence only.
#
# Authoritative contract (SessionEnd):
#   stdin  {session_id, transcript_path, cwd, hook_event_name:"SessionEnd",
#           hookSpecificInput:{matcher:"clear|logout|normal|error"}}
#   Side-effect ONLY — SessionEnd cannot emit decisions. ALWAYS exit 0: never
#   fail the session end. A missing verify script or memory dir is a no-op.
#
# Any extra args are forwarded to verify_memory.py (tests point --memory-dir at
# a scratch corpus + --transcripts-dir/--now for determinism); the plugin
# declaration passes none (verifies the live store).
###############################################################################
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hook_guard.sh
. "$SCRIPT_DIR/hook_guard.sh"
sc_hook_optin        # no .company here → silent exit 0 (plugin fires globally)

# Drain stdin so the host never blocks on the pipe — we don't need the payload.
cat >/dev/null 2>&1 || true

VERIFY="$SCRIPT_DIR/verify_memory.py"
MEMDIR="$SC_COMPANY/memory"

# Guard: a missing verify script or memory dir is a silent no-op (never fail).
if [ ! -f "$VERIFY" ] || [ ! -d "$MEMDIR" ]; then
  exit 0
fi

LOGDIR="$SC_COMPANY/ops/logs"
mkdir -p "$LOGDIR" 2>/dev/null || true

# Deterministic verify pass over this project's memory. tee so the report is
# both observable (stdout / tests) and durably logged. Never fail the session.
python3 "$VERIFY" --apply --memory-dir "$MEMDIR" "$@" 2>&1 \
  | tee -a "$LOGDIR/sessionend-verify.log" || true
exit 0
