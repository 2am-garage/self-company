#!/usr/bin/env bash
###############################################################################
# hook_schedule_guard.sh — SessionStart guard for org/schedule.yaml (Phase 12, I4).
#
# Plugin hooks fire in EVERY repo the Chairman opens, so this no-ops unless THIS
# project is a company (.company/ present) AND is ALREADY scheduled. Then:
#
#   1. VALIDATE (Layer B) — ONLY when a schedule.yaml exists. Run
#      schedule_validator.py. An invalid config is NON-BLOCKING: print the
#      violations as a warning and exit 0. daily-run.sh / schedule.sh fall back to
#      defaults on their own — we must never fail the session and never install a
#      mis-configured tick. A company on the DEFAULT schedule (NO schedule.yaml)
#      skips this step (nothing to validate) but still runs the SYNC below —
#      Phase 12b: the scripts-dir self-heal must reach default-schedule companies
#      too, since a plugin move breaks their cron path identically (this is exactly
#      how self-company's own cron silently broke on a plugin restructure).
#   2. SYNC. Compute a signature of the desired daily + research cron (minute-
#      AGNOSTIC) PLUS the canonical scripts dir, and compare it to
#      .company/ops/schedule/.installed-tick. If the TICK, research cadence, OR the
#      scripts dir changed, re-run `schedule.sh install` so the edit reaches the
#      live crontab without the Chairman remembering to re-install, then refresh
#      the marker. Per-employee SUB-cadence edits do NOT change the signature => no
#      re-install (Phase-7 A1: the crontab carries an absolute snapshot; only the
#      tick + script path need syncing, gating is resolved at runtime in daily-run).
#      Phase 12b: folding the scripts dir in makes a PLUGIN UPDATE/MOVE self-heal
#      the cron (the update swaps the script files but leaves the stale absolute
#      path in the crontab) — the same "signature changed -> re-install" path fires.
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

command -v python3 >/dev/null 2>&1 || exit 0
[ -f "$CONFIG_PY" ] || exit 0

# 1. Validate (Layer B) — ONLY when a schedule.yaml EXISTS. Phase 12b gap fix: a
#    company on the DEFAULT schedule (no org/schedule.yaml) has nothing to
#    validate, but it STILL needs the scripts-dir self-heal below — a plugin
#    update leaves its cron pointing at a stale absolute path exactly the same as
#    a configured company (this is how self-company's own cron silently broke).
#    So validation is gated on $CFG; the SYNC that follows is NOT — it runs for
#    ANY already-installed project. A rejected config is non-blocking: warn, leave
#    the tick on its current (default / last-valid) value, and DO NOT sync (a bad
#    yaml must never install a tick — the scripts-dir heal waits for a valid one).
if [ -f "$CFG" ] && [ -f "$VALIDATOR_PY" ]; then
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
# Phase 12b — cron self-heal on plugin update. The crontab carries an ABSOLUTE
# snapshot of the scripts dir (schedule.sh A1); a plugin update/move swaps the
# files but leaves the cron pointing at the stale path (hooks reload, cron does
# not). Design (a): fold the CANONICAL scripts dir into the signature, so a path
# change trips the SAME "signature changed -> re-install" path that a tick change
# does — one mechanism, no new branch. Ground-truth + single-source: we ASK
# schedule.sh which dir it would embed now (`scripts-dir`, honoring
# CLAUDE_PLUGIN_ROOT) instead of re-deriving that resolution here (no drift). An
# older 2-field marker simply differs from this 3-field signature => exactly one
# self-heal re-install, then it converges (no churn). A failed query yields an
# empty field, which is also self-consistent after one install (no churn).
SCRIPTS_NOW="$(bash "$SCHEDULE_SH" scripts-dir "$PROJECT_DIR" 2>/dev/null)" || SCRIPTS_NOW=""
DESIRED="$DAILY_SIG|$RESEARCH_SIG|$SCRIPTS_NOW"

# C3 (GIB-S3): the compare-read + install + marker-write below is a crontab
# read-modify-write. Two concurrent SessionStarts (two windows opened at once)
# could interleave schedule.sh install's RMW and clobber each other's line / a
# neighbour. The critical section reads the marker AFTER acquiring the lock, so a
# guard that just synced is seen and we no-op instead of racing. Idempotent: an
# install replaces only THIS project's two lines with the current tick AND the
# current scripts dir — so both a tick edit and a plugin update heal.
_guard_sync() {
  local CURRENT=""
  [ -f "$MARKER" ] && CURRENT="$(cat "$MARKER" 2>/dev/null || true)"
  if [ "$DESIRED" != "$CURRENT" ] && [ -f "$SCHEDULE_SH" ]; then
    if bash "$SCHEDULE_SH" install "$PROJECT_DIR" >/dev/null 2>&1; then
      mkdir -p "$MARKER_DIR" 2>/dev/null || true
      printf '%s\n' "$DESIRED" > "$MARKER" 2>/dev/null || true
      echo "[schedule-guard] schedule signature changed (tick/research/scripts path) -> re-installed cron for $PROJECT_DIR" >&2
    fi
  fi
}

# Serialize the RMW with a subshell-scoped flock (fd 9) — held for the life of the
# _guard_sync call, released when the subshell exits. Blocking with a short cap so
# SessionStart never hangs. flock absent (SELF_COMPANY_NO_FLOCK=1 or no util-linux)
# => run unlocked; convergence still holds (install is idempotent). The subshell
# form (vs `exec {fd}>`) avoids clobbering the script's own stderr and the
# non-interactive exec-redirect-failure exit trap.
mkdir -p "$MARKER_DIR" 2>/dev/null || true
if command -v flock >/dev/null 2>&1 && [ -z "${SELF_COMPANY_NO_FLOCK:-}" ]; then
  ( flock -w "${SELF_COMPANY_GUARD_LOCK_WAIT:-10}" 9 2>/dev/null || true
    _guard_sync ) 9>"$MARKER_DIR/.guard.lock"
else
  _guard_sync
fi
exit 0
