#!/usr/bin/env bash
###############################################################################
# hook_schedule_guard.sh — SessionStart guard for org/schedule.yaml (Phase 12, I4).
#
# Plugin hooks fire in EVERY repo the Chairman opens, so this no-ops unless THIS
# project is a company (.company/ present). Then, ONLY if a schedule.yaml exists:
#
#   1. VALIDATE (Layer B). Run schedule_validator.py. An invalid config is
#      NON-BLOCKING: print the violations as a warning and exit 0. daily-run.sh /
#      schedule.sh fall back to defaults on their own — we must never fail the
#      session and never install a mis-configured tick.
#   2. SYNC the tick. Compute a minute-AGNOSTIC signature of the desired daily +
#      research cron from the config and compare it to
#      .company/ops/schedule/.installed-tick. If the TICK (or research cadence)
#      changed, re-run `schedule.sh install` so the edit reaches the live crontab
#      without the Chairman remembering to re-install, then refresh the marker.
#      Per-employee SUB-cadence edits do NOT change the signature => no re-install
#      (Phase-7 A1: the crontab carries an absolute tick snapshot; only the tick
#      needs syncing, gating is resolved at runtime in daily-run.sh).
#
# Only syncs a project that is ALREADY scheduled — SessionStart never silently
# installs cron lines for a company the Chairman hasn't opted in. Honors
# SELF_COMPANY_CRONTAB_FILE / SELF_COMPANY_CRONTAB_CMD end-to-end (schedule.sh
# routes all crontab I/O through them), so dev/test never touch the real crontab;
# if there is no crontab backend at all, the sync is skipped silently.
#
# ALWAYS exits 0: SessionStart must never block startup.
###############################################################################
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hook_guard.sh
. "$SCRIPT_DIR/hook_guard.sh"
sc_hook_optin        # no .company here → silent exit 0 (plugin fires globally)

# Drain stdin so the host never blocks on the pipe — we don't need the payload.
cat >/dev/null 2>&1 || true

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
COMPANY="$SC_COMPANY"
CFG="$COMPANY/org/schedule.yaml"
CONFIG_PY="$SCRIPT_DIR/schedule_config.py"
VALIDATOR_PY="$SCRIPT_DIR/schedule_validator.py"
SCHEDULE_SH="$SCRIPT_DIR/schedule.sh"

# No config => nothing to validate or sync (defaults govern — today's behaviour).
[ -f "$CFG" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
[ -f "$CONFIG_PY" ] || exit 0

# 1. Validate (Layer B). A rejected config is non-blocking: warn, leave the tick
#    on its current (default / last-valid) value. Never install a bad tick.
if [ -f "$VALIDATOR_PY" ]; then
  if ! violations="$(python3 "$VALIDATOR_PY" --company "$COMPANY" 2>/dev/null)"; then
    echo "[schedule-guard] org/schedule.yaml REJECTED — falling back to defaults; not syncing the crontab:" >&2
    printf '%s\n' "$violations" | sed 's/^/[schedule-guard]   /' >&2
    exit 0
  fi
fi

# 2. Sync. Skip silently if there is no crontab backend at all (no fake file seam
#    AND no real crontab binary) — nothing to install into.
if [ -z "${SELF_COMPANY_CRONTAB_FILE:-}" ] \
   && ! command -v "${SELF_COMPANY_CRONTAB_CMD:-crontab}" >/dev/null 2>&1; then
  exit 0
fi

# Only keep an ALREADY-scheduled project in sync (never auto-install a company the
# Chairman hasn't opted in). schedule.sh status prints INSTALLED when a line
# exists. Capture into a var (not a pipe): under `pipefail`, `grep -q` closing the
# pipe early would make the status producer look like it failed (SIGPIPE).
_status="$(bash "$SCHEDULE_SH" status "$PROJECT_DIR" 2>/dev/null)"
case "$_status" in
  *INSTALLED*) ;;              # scheduled -> keep it in sync
  *) exit 0 ;;                 # not scheduled -> leave it alone
esac

MARKER_DIR="$COMPANY/ops/schedule"
MARKER="$MARKER_DIR/.installed-tick"
# Minute-agnostic signature: cadence-derived daily + research exprs with a
# placeholder minute. Sub-cadence (per-employee) edits do not affect these.
DAILY_SIG="$(python3 "$CONFIG_PY" --company "$COMPANY" --cron daily --minute M 2>/dev/null)" || DAILY_SIG=""
RESEARCH_SIG="$(python3 "$CONFIG_PY" --company "$COMPANY" --cron research --minute M 2>/dev/null)" || RESEARCH_SIG=""
DESIRED="$DAILY_SIG|$RESEARCH_SIG"

CURRENT=""
[ -f "$MARKER" ] && CURRENT="$(cat "$MARKER" 2>/dev/null || true)"

if [ "$DESIRED" != "$CURRENT" ] && [ -f "$SCHEDULE_SH" ]; then
  # Idempotent: install replaces only THIS project's two lines with the config tick.
  if bash "$SCHEDULE_SH" install "$PROJECT_DIR" >/dev/null 2>&1; then
    mkdir -p "$MARKER_DIR" 2>/dev/null || true
    printf '%s\n' "$DESIRED" > "$MARKER" 2>/dev/null || true
    echo "[schedule-guard] tick changed -> re-installed cron for $PROJECT_DIR" >&2
  fi
fi
exit 0
