#!/usr/bin/env bash
###############################################################################
# schedule.sh — install/uninstall/status the self-company daily cron.
#
# Ships the scheduling mechanism *inside the skill* (Tom's domain): one command
# installs an OS crontab entry that runs daily-run.sh every 6 hours (4×/day,
# matching DAILY_RUNS_PER_DAY). Idempotent — re-running install never duplicates
# the line. Local + unattended: runs at the scheduled time whenever the machine
# is on, no cloud, memory never leaves the box.
#
# Usage:
#   schedule.sh install [PROJECT_DIR]   # add/refresh the cron line
#   schedule.sh uninstall               # remove it
#   schedule.sh status                  # show whether it's installed
#
# Tunables (env):
#   SELF_COMPANY_CRON_MIN   minute field (default 7 — an off-peak, non-:00 mark)
#   SELF_COMPANY_PROJECT_DIR project dir (default: arg 2, else $PWD)
###############################################################################
set -uo pipefail

MARK="# self-company-daily"
CMD="${1:-status}"
PROJECT_DIR="${2:-${SELF_COMPANY_PROJECT_DIR:-$PWD}}"
PROJECT_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd || echo "$PROJECT_DIR")"
CRON_MIN="${SELF_COMPANY_CRON_MIN:-7}"
CRON_EXPR="$CRON_MIN */6 * * *"          # 4× a day, every 6h, off-minute

CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"
CLAUDE_DIR="$(dirname "$CLAUDE_BIN")"
LOGFILE="$PROJECT_DIR/.company/ops/logs/cron.log"
# cron has a minimal PATH; prepend the claude dir and /usr/bin so python3+claude resolve.
RUNNER="cd '$PROJECT_DIR' && PATH='$CLAUDE_DIR:/usr/local/bin:/usr/bin:/bin' bash '$PROJECT_DIR/.company/scripts/daily-run.sh' '$PROJECT_DIR' >> '$LOGFILE' 2>&1"
LINE="$CRON_EXPR $RUNNER $MARK"

current="$(crontab -l 2>/dev/null || true)"
# crontab minus any prior self-company line, blank lines trimmed
without_ours() { printf '%s\n' "$current" | grep -vF "$MARK" | sed '/^[[:space:]]*$/d'; }

case "$CMD" in
  install)
    if [[ ! -d "$PROJECT_DIR/.company" ]]; then
      echo "[schedule] error: $PROJECT_DIR/.company not found — run init_company.sh first." >&2
      exit 1
    fi
    if [[ ! -f "$PROJECT_DIR/.company/scripts/daily-run.sh" ]]; then
      echo "[schedule] error: daily-run.sh not in .company/scripts — run init or --sync-scripts." >&2
      exit 1
    fi
    mkdir -p "$(dirname "$LOGFILE")"
    { without_ours; printf '%s\n' "$LINE"; } | crontab -
    echo "[schedule] installed: '$CRON_EXPR' (4×/day) -> daily-run.sh"
    echo "[schedule] project: $PROJECT_DIR"
    echo "[schedule] log:     $LOGFILE"
    ;;
  uninstall)
    if printf '%s\n' "$current" | grep -qF "$MARK"; then
      without_ours | crontab -
      echo "[schedule] removed the self-company cron line"
    else
      echo "[schedule] nothing to remove (not installed)"
    fi
    ;;
  status)
    if printf '%s\n' "$current" | grep -qF "$MARK"; then
      echo "[schedule] INSTALLED:"
      printf '%s\n' "$current" | grep -F "$MARK"
    else
      echo "[schedule] not installed"
    fi
    ;;
  *)
    echo "usage: schedule.sh [install|uninstall|status] [PROJECT_DIR]" >&2
    exit 2
    ;;
esac
