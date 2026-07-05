#!/usr/bin/env bash
###############################################################################
# schedule.sh — install/uninstall/status/list/prune the self-company cron lines.
#
# Ships the scheduling mechanism *inside the skill* (Tom's domain). Each company
# (project) installs TWO OS crontab entries — (1) daily-run.sh every 6h (4×/day,
# internal maintenance) and (2) research-scan.sh weekly (Tony's external survey).
#
# MULTI-COMPANY (Phase 7): the crontab is treated as a KEYED SET of companies —
# one entry pair per project, every operation scoped by a stable project key
# derived from the resolved absolute PROJECT_DIR. Installing company B never
# evicts company A. This is a general mechanism (N companies as data), not a
# per-repo special case: the same generalization tombstone.py/charter_ids.py use.
#
# Ownership marks are namespaced:
#   # self-company-daily    project=<key> path=<PROJECT_DIR>
#   # self-company-research project=<key> path=<PROJECT_DIR>
# where <key> = first 12 hex of sha1(PROJECT_DIR). Legacy un-namespaced lines
# (no project=) are migrated to the namespaced form on the next install/uninstall
# for their embedded path — never orphaned, never duplicated.
#
# AUTO-STAGGER: the default daily minute is sha1(path) % 60 (and the weekly
# research minute a second, independent slice), so N companies land on different
# minutes across the hour instead of stacking on one. SELF_COMPANY_CRON_MIN still
# overrides explicitly (accept the special case via config, don't hardcode it).
#
# Idempotent — re-running install replaces only THIS project's two lines.
# Local + unattended: runs whenever the machine is on, no cloud, memory never
# leaves the box.
#
# Usage:
#   schedule.sh install   [PROJECT_DIR]   # add/refresh this project's cron lines
#   schedule.sh uninstall [PROJECT_DIR]   # remove only this project's lines
#   schedule.sh status    [PROJECT_DIR]   # single-project view (back-compat)
#   schedule.sh status --all              # fleet view (alias of list)
#   schedule.sh list                      # fleet table: all companies + orphans
#   schedule.sh prune                     # remove only orphan/dead-path lines
#
# Tunables (env):
#   SELF_COMPANY_CRON_MIN      explicit minute override (else auto-staggered)
#   SELF_COMPANY_RESEARCH_CRON full weekly cron expr override (5 fields)
#   SELF_COMPANY_PROJECT_DIR   project dir (default: arg 2, else $PWD)
#   SELF_COMPANY_CRONTAB_FILE  test/seam: read+write this file instead of the
#                              real user crontab (general injectable backend)
#   SELF_COMPANY_CRONTAB_CMD   crontab binary to shell out to (default: crontab)
###############################################################################
set -uo pipefail

MARK_DAILY="# self-company-daily"
MARK_RESEARCH="# self-company-research"
CMD="${1:-status}"
PROJECT_DIR="${2:-${SELF_COMPANY_PROJECT_DIR:-$PWD}}"
# For fleet-wide commands PROJECT_DIR may be a flag ("--all") — don't resolve it.
if [[ "$PROJECT_DIR" != "--all" ]]; then
  PROJECT_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd || echo "$PROJECT_DIR")"
fi

# --- C1 backend seam ---------------------------------------------------------
# Route every crontab read/write through these two helpers. When
# SELF_COMPANY_CRONTAB_FILE is set, use that file (tests point it at a temp file);
# otherwise shell out to the real crontab binary. General mechanism, not a hack.
CRONTAB_CMD="${SELF_COMPANY_CRONTAB_CMD:-crontab}"
_cron_read() {
  if [[ -n "${SELF_COMPANY_CRONTAB_FILE:-}" ]]; then
    [[ -f "$SELF_COMPANY_CRONTAB_FILE" ]] && cat "$SELF_COMPANY_CRONTAB_FILE" || true
  else
    "$CRONTAB_CMD" -l 2>/dev/null || true
  fi
}
_cron_write() {  # stdin = the full new crontab body
  if [[ -n "${SELF_COMPANY_CRONTAB_FILE:-}" ]]; then
    cat > "$SELF_COMPANY_CRONTAB_FILE"
  else
    "$CRONTAB_CMD" -
  fi
}
_put() {  # $1 = content; normalize trailing newline, allow an empty crontab
  if [[ -n "$1" ]]; then printf '%s\n' "$1" | _cron_write; else printf '' | _cron_write; fi
}

# --- stable project key + auto-stagger ---------------------------------------
_sha1hex() {  # $1 = string -> hex digest on stdout (guards missing sha1sum)
  local out
  if command -v sha1sum >/dev/null 2>&1; then
    out="$(printf '%s' "$1" | sha1sum)"
  elif command -v shasum >/dev/null 2>&1; then
    out="$(printf '%s' "$1" | shasum -a 1)"
  else
    out="$(printf '%s' "$1" | cksum)"  # last-resort deterministic fallback
  fi
  printf '%s' "${out%% *}"
}

HASH="$(_sha1hex "$PROJECT_DIR")"
PROJ_KEY="${HASH:0:12}"
_h1="${HASH:0:8}"; [[ "$_h1" =~ ^[0-9a-fA-F]+$ ]] || _h1=0
_h2="${HASH:8:8}"; [[ "$_h2" =~ ^[0-9a-fA-F]+$ ]] || _h2=0
DEFAULT_MIN=$(( 0x$_h1 % 60 ))
DEFAULT_RESEARCH_MIN=$(( 0x$_h2 % 60 ))

CRON_MIN="${SELF_COMPANY_CRON_MIN:-$DEFAULT_MIN}"
CRON_EXPR="$CRON_MIN */6 * * *"          # 4× a day, every 6h, staggered minute
# Weekly external research scan (Tony) — Sunday, off-peak, staggered minute.
RESEARCH_EXPR="${SELF_COMPANY_RESEARCH_CRON:-$DEFAULT_RESEARCH_MIN 3 * * 0}"

CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"
CLAUDE_DIR="$(dirname "$CLAUDE_BIN")"
LOGFILE="$PROJECT_DIR/.company/ops/logs/cron.log"

# Resolve the CANONICAL scripts dir (code/data separation). Precedence: plugin root
# -> own dir -> legacy .company/scripts. A1: cron carries an ABSOLUTE snapshot of the
# script path, so a skill/plugin update requires re-running `schedule.sh install`.
if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" && -d "${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts" ]]; then
  SCRIPTS_DIR="${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts"
else
  SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [[ ! -f "$SCRIPTS_DIR/daily-run.sh" && -f "$PROJECT_DIR/.company/scripts/daily-run.sh" ]]; then
  SCRIPTS_DIR="$PROJECT_DIR/.company/scripts"
fi

# cron has a minimal PATH; prepend the claude dir and /usr/bin so python3+claude resolve.
PATH_PREFIX="PATH='$CLAUDE_DIR:/usr/local/bin:/usr/bin:/bin'"
MARK="$MARK_DAILY project=$PROJ_KEY path=$PROJECT_DIR"
MARK_RES="$MARK_RESEARCH project=$PROJ_KEY path=$PROJECT_DIR"
RUNNER="cd '$PROJECT_DIR' && $PATH_PREFIX bash '$SCRIPTS_DIR/daily-run.sh' '$PROJECT_DIR' >> '$LOGFILE' 2>&1"
LINE="$CRON_EXPR $RUNNER $MARK"
RESEARCH_RUNNER="cd '$PROJECT_DIR' && $PATH_PREFIX bash '$SCRIPTS_DIR/research-scan.sh' '$PROJECT_DIR' >> '$LOGFILE' 2>&1"
RESEARCH_LINE="$RESEARCH_EXPR $RESEARCH_RUNNER $MARK_RES"

# --- project-scoped crontab filters ------------------------------------------
# Remove ONLY the current project's self-company lines (namespaced by key, OR a
# legacy un-namespaced line whose embedded 'cd <path>' matches this project) and
# trim blank lines. Every other line — including other companies' — is preserved.
_without_project() {
  awk -v key="$PROJ_KEY" -v pdir="$PROJECT_DIR" '
    {
      is_sc = ($0 ~ /# self-company-(daily|research)/)
      if (is_sc && index($0, "project=" key)) next                        # ours (namespaced)
      if (is_sc && $0 !~ /project=/ && index($0, "\047" pdir "\047")) next # ours (legacy path)
      if ($0 ~ /^[[:space:]]*$/) next                                      # trim blanks
      print
    }'
}

# Emit only the current project's self-company lines (for the single-project view).
_our_lines() {
  awk -v key="$PROJ_KEY" -v pdir="$PROJECT_DIR" '
    {
      if ($0 !~ /# self-company-(daily|research)/) next
      if (index($0, "project=" key)) { print; next }
      if ($0 !~ /project=/ && index($0, "\047" pdir "\047")) { print }
    }'
}

# Parse ALL self-company lines into TSV records: type<TAB>path<TAB>minute<TAB>expr
_parse_records() {
  awk '
    function get_path(line,   p) {
      if (match(line, /path=/))               return substr(line, RSTART+5)
      if (match(line, /cd \047[^\047]*\047/)) return substr(line, RSTART+4, RLENGTH-5)
      return "?"
    }
    /# self-company-daily/ {
      split($0,a,/[ \t]+/)
      print "daily\t" get_path($0) "\t" a[1] "\t" a[1] " " a[2] " " a[3] " " a[4] " " a[5]; next
    }
    /# self-company-research/ {
      split($0,a,/[ \t]+/)
      print "research\t" get_path($0) "\t" a[1] "\t" a[1] " " a[2] " " a[3] " " a[4] " " a[5]
    }'
}

# Extract the project path from a single crontab line (namespaced or legacy).
_line_path() {
  local line="$1"
  if [[ "$line" == *path=* ]]; then
    printf '%s' "${line##*path=}"
  else
    local re="cd '([^']*)'"
    [[ "$line" =~ $re ]] && printf '%s' "${BASH_REMATCH[1]}"
  fi
}

# Drop self-company lines whose project .company/ dir is gone (orphans); keep all
# else. Never removes a line with a live .company/ or a non-self-company line.
_prune_filter() {
  local sc_re='# self-company-(daily|research)'
  local line p
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ $sc_re ]]; then
      p="$(_line_path "$line")"
      if [[ -n "$p" && -d "$p/.company" ]]; then
        printf '%s\n' "$line"
      fi
      # else: orphan / dead path -> drop
    else
      [[ -n "$line" ]] && printf '%s\n' "$line"
    fi
  done
}

do_list() {
  local recs; recs="$(_cron_read | _parse_records)"
  if [[ -z "$recs" ]]; then
    echo "[schedule] no self-company companies scheduled"
    return 0
  fi
  printf '%-48s %-12s %-9s %s\n' "PROJECT PATH" "DAILY(min)" "RESEARCH" "STATUS"
  local paths p dmin research status
  paths="$(printf '%s\n' "$recs" | awk -F'\t' 'NF>=2{print $2}' | sort -u)"
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    dmin="$(printf '%s\n' "$recs" | awk -F'\t' -v p="$p" '$1=="daily" && $2==p {print $3; exit}')"
    [[ -z "$dmin" ]] && dmin="-"
    if printf '%s\n' "$recs" | awk -F'\t' -v p="$p" '$1=="research" && $2==p{f=1} END{exit !f}'; then
      research="yes"
    else
      research="no"
    fi
    if [[ -d "$p/.company" ]]; then status="ok"; else status="ORPHAN"; fi
    printf '%-48s %-12s %-9s %s\n' "$p" "$dmin" "$research" "$status"
  done <<< "$paths"
}

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
    base="$(_cron_read | _without_project)"   # everything except THIS project (legacy migrated out)
    { [[ -n "$base" ]] && printf '%s\n' "$base"
      printf '%s\n%s\n' "$LINE" "$RESEARCH_LINE"; } | _cron_write
    echo "[schedule] installed: '$CRON_EXPR' (4×/day) -> daily-run.sh"
    echo "[schedule] installed: '$RESEARCH_EXPR' (weekly) -> research-scan.sh"
    echo "[schedule] project: $PROJECT_DIR"
    echo "[schedule] key:     $PROJ_KEY"
    echo "[schedule] log:     $LOGFILE"
    ;;
  uninstall)
    if _cron_read | _our_lines | grep -q .; then
      _put "$(_cron_read | _without_project)"
      echo "[schedule] removed self-company cron lines for: $PROJECT_DIR"
    else
      echo "[schedule] nothing to remove (not installed for $PROJECT_DIR)"
    fi
    ;;
  list)
    do_list
    ;;
  prune)
    before="$(_cron_read)"
    n_before="$(printf '%s\n' "$before" | grep -cE '# self-company-(daily|research)' || true)"
    after="$(printf '%s\n' "$before" | _prune_filter)"
    _put "$after"
    n_after="$(printf '%s\n' "$after" | grep -cE '# self-company-(daily|research)' || true)"
    removed=$(( n_before - n_after ))
    echo "[schedule] prune: removed $removed orphan line(s); $n_after self-company line(s) remain"
    ;;
  status)
    if [[ "${2:-}" == "--all" ]]; then
      do_list
    elif _cron_read | _our_lines | grep -q .; then
      echo "[schedule] INSTALLED ($PROJECT_DIR, key=$PROJ_KEY):"
      ours="$(_cron_read | _our_lines)"
      printf '%s\n' "$ours" | grep -F "$MARK_DAILY" || echo "  (daily: missing)"
      printf '%s\n' "$ours" | grep -F "$MARK_RESEARCH" || echo "  (research: missing)"
    else
      echo "[schedule] not installed"
    fi
    ;;
  *)
    echo "usage: schedule.sh [install|uninstall|status|list|prune] [PROJECT_DIR|--all]" >&2
    exit 2
    ;;
esac
