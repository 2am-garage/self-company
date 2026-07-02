#!/usr/bin/env bash
###############################################################################
# schedule.sh — install/uninstall/status the self-company cron lines.
#
# Ships the scheduling mechanism *inside the skill* (Tom's domain): one command
# installs TWO OS crontab entries — (1) daily-run.sh every 6h (4×/day, internal
# maintenance) and (2) research-scan.sh weekly (Tony's external-improvement
# survey → proposals). Idempotent — re-running install never duplicates lines.
# Local + unattended: runs whenever the machine is on, no cloud, memory never
# leaves the box.
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
MARK_RESEARCH="# self-company-research"
CMD="${1:-status}"
PROJECT_DIR="${2:-${SELF_COMPANY_PROJECT_DIR:-$PWD}}"
PROJECT_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd || echo "$PROJECT_DIR")"
CRON_MIN="${SELF_COMPANY_CRON_MIN:-7}"
CRON_EXPR="$CRON_MIN */6 * * *"          # 4× a day, every 6h, off-minute
# Weekly external research scan (Tony) — Sunday, off-peak. Tunable.
RESEARCH_EXPR="${SELF_COMPANY_RESEARCH_CRON:-23 3 * * 0}"

CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"
CLAUDE_DIR="$(dirname "$CLAUDE_BIN")"
LOGFILE="$PROJECT_DIR/.company/ops/logs/cron.log"

# Resolve the CANONICAL scripts dir (code/data separation). Precedence: plugin root
# -> own dir -> legacy .company/scripts. A1: cron carries an ABSOLUTE snapshot of the
# script path, so a skill/plugin update requires re-running `schedule.sh install`.
if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" && -d "${CLAUDE_PLUGIN_ROOT}/scripts" ]]; then
  SCRIPTS_DIR="${CLAUDE_PLUGIN_ROOT}/scripts"
else
  SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [[ ! -f "$SCRIPTS_DIR/daily-run.sh" && -f "$PROJECT_DIR/.company/scripts/daily-run.sh" ]]; then
  SCRIPTS_DIR="$PROJECT_DIR/.company/scripts"
fi

# cron has a minimal PATH; prepend the claude dir and /usr/bin so python3+claude resolve.
PATH_PREFIX="PATH='$CLAUDE_DIR:/usr/local/bin:/usr/bin:/bin'"
RUNNER="cd '$PROJECT_DIR' && $PATH_PREFIX bash '$SCRIPTS_DIR/daily-run.sh' '$PROJECT_DIR' >> '$LOGFILE' 2>&1"
LINE="$CRON_EXPR $RUNNER $MARK"
RESEARCH_RUNNER="cd '$PROJECT_DIR' && $PATH_PREFIX bash '$SCRIPTS_DIR/research-scan.sh' '$PROJECT_DIR' >> '$LOGFILE' 2>&1"
RESEARCH_LINE="$RESEARCH_EXPR $RESEARCH_RUNNER $MARK_RESEARCH"

current="$(crontab -l 2>/dev/null || true)"
# crontab minus any prior self-company line (daily OR research), blank lines trimmed
without_ours() { printf '%s\n' "$current" | grep -vF "$MARK" | grep -vF "$MARK_RESEARCH" | sed '/^[[:space:]]*$/d'; }

case "$CMD" in
  install)
    if [[ ! -d "$PROJECT_DIR/.company" ]]; then
      echo "[schedule] error: $PROJECT_DIR/.company not found — run init_company.sh first." >&2
      exit 1
    fi
    if [[ ! -f "$SCRIPTS_DIR/daily-run.sh" ]]; then
      echo "[schedule] error: daily-run.sh not found at $SCRIPTS_DIR — check the skill install." >&2
      exit 1
    fi
    mkdir -p "$(dirname "$LOGFILE")"
    { without_ours; printf '%s\n' "$LINE"; printf '%s\n' "$RESEARCH_LINE"; } | crontab -
    echo "[schedule] installed: '$CRON_EXPR' (4×/day) -> daily-run.sh"
    echo "[schedule] installed: '$RESEARCH_EXPR' (weekly) -> research-scan.sh"
    echo "[schedule] project: $PROJECT_DIR"
    echo "[schedule] log:     $LOGFILE"
    ;;
  uninstall)
    if printf '%s\n' "$current" | grep -qE "$(printf '%s|%s' "$MARK" "$MARK_RESEARCH")"; then
      without_ours | crontab -
      echo "[schedule] removed the self-company cron lines (daily + research)"
    else
      echo "[schedule] nothing to remove (not installed)"
    fi
    ;;
  status)
    if printf '%s\n' "$current" | grep -qF "$MARK" || printf '%s\n' "$current" | grep -qF "$MARK_RESEARCH"; then
      echo "[schedule] INSTALLED:"
      printf '%s\n' "$current" | grep -F "$MARK" || echo "  (daily: missing)"
      printf '%s\n' "$current" | grep -F "$MARK_RESEARCH" || echo "  (research: missing)"
    else
      echo "[schedule] not installed"
    fi
    ;;
  *)
    echo "usage: schedule.sh [install|uninstall|status] [PROJECT_DIR]" >&2
    exit 2
    ;;
esac
