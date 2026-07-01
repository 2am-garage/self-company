#!/usr/bin/env bash
###############################################################################
# company-run.sh — start a real company work cycle FROM THE SESSION.
#
# The fourth trigger source: the interactive session itself. Instead of Elon
# silently editing every file, he hands a task to the company:
#
#     company-run.sh "improve the trigger ledger to show durations"
#
# Flow (mirrors the self-upgrade loop):
#   1. PLAN   — Phoebe (a headless `claude -p`) breaks the task into a
#               {employee: subtask} assignment plan (JSON). Heuristic fallback if
#               planning is unavailable.
#   2. DISPATCH — supervisor.py spawns the assigned employees as live child
#               processes (real agents), streaming their status.
#   3. LEDGER — the cycle is appended to ops/reports/company-runs.md.
#
# This is how "self-company improves self-company" actually runs. See MISSION.md.
#
# Usage:
#   company-run.sh "<task>" [--demo] [--company DIR]
#     --demo   skip real agents: heuristic plan + supervisor --demo (safe, no LLM)
###############################################################################
set -uo pipefail

PROJECT_DIR="${SELF_COMPANY_PROJECT_DIR:-$PWD}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TASK=""; DEMO=false; COMPANY=""
for a in "$@"; do
  case "$a" in
    --demo) DEMO=true ;;
    --company) : ;;                          # handled below via env if needed
    -*) : ;;
    *) [[ -z "$TASK" ]] && TASK="$a" ;;
  esac
done
# allow --company DIR
prev=""
for a in "$@"; do [[ "$prev" == "--company" ]] && COMPANY="$a"; prev="$a"; done

if [[ -z "$COMPANY" ]]; then
  if [[ -d "$PROJECT_DIR/.company" ]]; then COMPANY="$PROJECT_DIR/.company"
  elif [[ "$(basename "$(dirname "$SCRIPT_DIR")")" == ".company" ]]; then COMPANY="$(dirname "$SCRIPT_DIR")"
  else COMPANY="$PROJECT_DIR/.company"; fi
fi
SCRIPTS_RT="$COMPANY/scripts"; [[ -f "$SCRIPTS_RT/supervisor.py" ]] || SCRIPTS_RT="$SCRIPT_DIR"

if [[ -z "$TASK" ]]; then
  echo "usage: company-run.sh \"<task>\" [--demo] [--company DIR]" >&2
  exit 2
fi

REPORTS="$COMPANY/ops/reports"; mkdir -p "$REPORTS"
LEDGER="$REPORTS/company-runs.md"
TS="$(date +%FT%T)"

# --- 1. PLAN (Phoebe) ------------------------------------------------------
plan_json=""
if ! $DEMO; then
  CLAUDE_BIN="$(command -v claude || true)"
  [[ -z "$CLAUDE_BIN" && -x "$HOME/.local/bin/claude" ]] && CLAUDE_BIN="$HOME/.local/bin/claude"
  if [[ -n "$CLAUDE_BIN" ]]; then
    roster="$(python3 "$SCRIPTS_RT/supervisor.py" --company "$COMPANY" --list 2>/dev/null)"
    read -r -d '' PPROMPT <<EOF || true
You are Phoebe, the self-company execution gateway. Break this task into a MINIMAL
per-employee assignment. Employees available: $roster. Task: "$TASK".
Output ONLY a single JSON object mapping employee id -> a one-line subtask, e.g.
{"bob":"...", "gibby":"verify Bob's change"}. No prose, JSON only.
EOF
    raw="$(SELF_COMPANY_CAPTURE_ACTIVE=1 timeout 180 "$CLAUDE_BIN" -p "$PPROMPT" \
           --model "${SELF_COMPANY_PLAN_MODEL:-claude-sonnet-4-6}" 2>/dev/null || true)"
    plan_json="$(printf '%s' "$raw" | python3 -c "import sys,re,json
t=sys.stdin.read()
m=re.findall(r'\{[^{}]*\}', t, re.S)
for cand in reversed(m):
    try:
        d=json.loads(cand)
        if isinstance(d,dict) and d: print(json.dumps(d)); break
    except Exception: pass" 2>/dev/null || true)"
  fi
fi

# Heuristic fallback / demo plan: Bob does it, Gibby verifies.
if [[ -z "$plan_json" ]]; then
  plan_json="$(python3 -c "import json,sys; print(json.dumps({'bob': sys.argv[1], 'gibby': 'verify the change'}))" "$TASK")"
  planned_by="heuristic"
else
  planned_by="Phoebe"
fi

echo "[company-run] task: $TASK"
echo "[company-run] plan ($planned_by): $plan_json"

# --- 2. DISPATCH (supervisor spawns the assigned employees, live) ----------
if $DEMO; then
  python3 "$SCRIPTS_RT/supervisor.py" --company "$COMPANY" --demo
else
  python3 "$SCRIPTS_RT/supervisor.py" --company "$COMPANY" --dispatch "$plan_json"
fi
rc=$?

# --- 3. LEDGER -------------------------------------------------------------
[[ -f "$LEDGER" ]] || printf '# Company Runs (session-triggered)\n\n_Each row: a company work cycle started from the session. See MISSION.md._\n\n| time | task | planned by | assignments | rc |\n|---|---|---|---|---|\n' > "$LEDGER"
printf '| %s | %s | %s | `%s` | %s |\n' "$TS" "${TASK:0:40}" "$planned_by" "${plan_json:0:60}" "$rc" >> "$LEDGER"

echo "[company-run] done (rc $rc) — logged to ops/reports/company-runs.md"
exit "$rc"
