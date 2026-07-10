#!/usr/bin/env bash
###############################################################################
# fleet-run.sh — self-company HOLDING-COMPANY sweep (Phase 8 driver).
#
# One invocation = what the PARENT's single cron calls. It drives the
# maintenance of every sub-company in the parent's registry
# (<parent>/.company/org/subsidiaries.md) with a smart GLOBAL token budget
# instead of N blind per-sub crons:
#
#   1. ONE auth pre-flight for the whole fleet (the shared single-point-of-
#      failure, checked once). Not logged in => ONE fleet AUTH_FAIL marker, skip
#      ALL agent passes, but still run every sub's deterministic maintenance.
#   2. CHEAP pass, all live subs: each sub's OWN `daily-run.sh <sub> --no-agent`
#      (reinforce->decay->verify->entropy->report — deterministic, cheap, and
#      isolated to that sub's `.company/`). Capture each sub's post-run entropy
#      from its OWN daily log.
#   3. BUDGET-GATED agent pass: a sub qualifies if its entropy ROSE vs its last
#      recorded tick OR its scored-dup backlog exceeds a threshold. Rank
#      qualifiers by (entropy_delta * weight); run the expensive headless agent
#      (the sub's own `daily-run.sh <sub>`) for the top K = FLEET_AGENT_BUDGET.
#      Others: logged budget-deferred. NEVER exceed the budget — hard ceiling.
#   4. COMBINED fleet ledger: append one row per sub to
#      <parent>/.company/ops/reports/fleet-ledger.md; update fleet-state.json.
#
# ISOLATION INVARIANT: the parent orchestrates SCHEDULING + BUDGET only. It
# NEVER reads or writes a sub's memory/personas — it only INVOKES that sub's own
# daily-run.sh (which mutates that sub's `.company/`) and READS that sub's own
# log output. All parent bookkeeping lives under <parent>/.company/ops/.
#
# Usage: fleet-run.sh [PARENT_DIR] [--dry-run] [--now YYYY-MM-DD]
# Env:
#   SELF_COMPANY_FLEET_AGENT_BUDGET   top-K subs that may run the agent (default 3)
#   SELF_COMPANY_FLEET_DUP_THRESHOLD  scored-dup backlog qualify threshold (default 5)
#   SELF_COMPANY_FORCE_AUTH_FAIL=1    force the not-logged-in branch (tests)
#   SELF_COMPANY_SKIP_AUTH_PROBE=1    skip the probe entirely (probe => "unknown")
###############################################################################
set -uo pipefail   # not -e: one sub's failure must never abort the fleet

PARENT_DIR="${SELF_COMPANY_PARENT_DIR:-$PWD}"
DRY_RUN=false
NOW=""
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=true ;;
    --now) NEXT_IS_NOW=1 ;;
    --now=*) NOW="${a#--now=}" ;;
    -*) : ;;                          # ignore unknown flags
    *)
      if [[ "${NEXT_IS_NOW:-0}" == "1" ]]; then NOW="$a"; NEXT_IS_NOW=0
      else PARENT_DIR="$a"; fi
      ;;
  esac
done
[[ -z "$NOW" ]] && NOW="$(date +%F)"

# Phase 28 Item 4b (D1/D6): the claude-spawn scaffolding (CLAUDE_BIN
# resolution, the auth pre-flight probe) + the scripts-dir precedence are the
# ONE shared lib (agent_spawn.sh, same dir) — see its header for why every
# caller keeps this exact bootstrap instead of the lib resolving its own dir.
_SC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=agent_spawn.sh
source "$_SC_LIB_DIR/agent_spawn.sh"

PARENT_COMPANY="$PARENT_DIR/.company"
# Resolve the CANONICAL scripts dir (same precedence as daily-run.sh).
SCRIPTS="$(sc_resolve_scripts_dir "$_SC_LIB_DIR" "$PARENT_COMPANY")"
OPS="$PARENT_COMPANY/ops"
REPORTS="$OPS/reports"
FLEET_LOG="$OPS/logs/fleet-$NOW.md"
AUTH_MARKER="$OPS/fleet-auth-fail.marker"
PARENT_POLICY="$PARENT_COMPANY/org/policy.md"
# FLEET_AGENT_BUDGET resolution (mirrors daily-run.sh's DAILY_RUNS_PER_DAY read):
# the env var is the explicit override (tests/ops); else the PARENT's policy.md
# §7.9 is the source of truth — editing policy.md must actually move the ceiling;
# else the built-in default 3. A missing/malformed policy falls back to 3.
if [[ -n "${SELF_COMPANY_FLEET_AGENT_BUDGET:-}" ]]; then
  BUDGET="$SELF_COMPANY_FLEET_AGENT_BUDGET"
else
  BUDGET="$(python3 -c "import sys; sys.path.insert(0, '$SCRIPTS')
try:
    from policy_config import load_policy_constants as L
    print(int(L('$PARENT_POLICY').get('FLEET_AGENT_BUDGET', 3)))
except Exception:
    print(3)" 2>/dev/null || echo 3)"
fi
DUP_THRESHOLD="${SELF_COMPANY_FLEET_DUP_THRESHOLD:-5}"
[[ "$BUDGET" =~ ^[0-9]+$ ]] || BUDGET=3
[[ "$DUP_THRESHOLD" =~ ^[0-9]+$ ]] || DUP_THRESHOLD=5

if [[ ! -d "$PARENT_COMPANY" ]]; then
  echo "[fleet-run] no parent .company at $PARENT_COMPANY — nothing to do"
  exit 0
fi
mkdir -p "$OPS/logs" "$REPORTS"

ts="$(date +%FT%T)"
{
  printf '\n## Fleet run %s%s (tick %s)\n' "$ts" "$($DRY_RUN && echo ' (dry-run)')" "$NOW"
} >> "$FLEET_LOG"

# --- auth pre-flight (Phase 28 Item 4b D2: shared sc_auth_logged_in — single
# fleet check) --------------------------------------------------------------
CLAUDE_BIN="$(sc_resolve_claude_bin)"
AUTH="$(sc_auth_logged_in)"
AUTH_DOWN=false
if [[ "$AUTH" == "no" ]]; then
  AUTH_DOWN=true
  if ! $DRY_RUN; then
    { printf 'last_ts=%s\n' "$ts"; printf 'reason=fleet-auth\n'; printf 'tick=%s\n' "$NOW"; } > "$AUTH_MARKER"
  fi
  echo "- ESCALATION: fleet auth pre-flight NOT logged in — ALL agent passes skipped this tick; run /login. Deterministic maintenance still applied to every sub. (AUTH_FAIL marker: $AUTH_MARKER)" >> "$FLEET_LOG"
fi

# --- registry scan (dead/dup/disabled reported, never fatal) ----------------
SCAN_JSON="$(python3 "$SCRIPTS/fleet.py" scan --parent "$PARENT_DIR" --json 2>/dev/null || echo '{}')"
# live subs -> "path<TAB>weight" lines
mapfile -t LIVE_LINES < <(python3 "$SCRIPTS/fleet.py" scan --parent "$PARENT_DIR" 2>/dev/null || true)
# surface warnings (dead/dup) into the fleet log
python3 - "$SCAN_JSON" >> "$FLEET_LOG" <<'PY' || true
import sys, json
try:
    d = json.loads(sys.argv[1])
except Exception:
    d = {}
live = d.get("live", []); dead = d.get("dead", []); dis = d.get("disabled", [])
print(f"- registry: {len(live)} live | {len(dead)} dead | {len(dis)} disabled | {len(d.get('duplicates', []))} dup")
for w in d.get("warnings", []):
    print(f"  - {w}")
PY

if [[ "${#LIVE_LINES[@]}" -eq 0 ]]; then
  echo "- no live subs in registry — done" >> "$FLEET_LOG"
  echo "[fleet-run] done ($NOW) — no live subs; see $FLEET_LOG"
  exit 0
fi

# --- 2. cheap deterministic pass, ALL live subs -----------------------------
RESULTS="$(mktemp)"; DROWS="$(mktemp)"; FROWS="$(mktemp)"
trap 'rm -f "$RESULTS" "$DROWS" "$FROWS"' EXIT

declare -A WEIGHT_OF
for line in "${LIVE_LINES[@]}"; do
  [[ -z "$line" ]] && continue
  sub="${line%%$'\t'*}"
  weight="${line##*$'\t'}"
  [[ "$weight" =~ ^[0-9]+$ ]] || weight=1
  WEIGHT_OF["$sub"]="$weight"

  if $DRY_RUN; then
    SC_NO_RAG="${SC_NO_RAG:-}" bash "$SCRIPTS/daily-run.sh" "$sub" --dry-run >/dev/null 2>&1 || \
      echo "- WARN cheap pass errored (dry) for $sub — continuing" >> "$FLEET_LOG"
  else
    SC_NO_RAG="${SC_NO_RAG:-}" bash "$SCRIPTS/daily-run.sh" "$sub" --no-agent >/dev/null 2>&1 || \
      echo "- WARN cheap pass errored for $sub — continuing" >> "$FLEET_LOG"
  fi

  # Read this sub's post-run entropy from its OWN log (never its memory).
  ent_line="$(python3 "$SCRIPTS/fleet.py" sub-entropy --sub "$sub" 2>/dev/null || true)"
  if [[ -z "$ent_line" ]]; then
    echo "- WARN no entropy readable for $sub — treating as entropy 0 / backlog 0" >> "$FLEET_LOG"
    ent_line=$'0\t0\t0'
  fi
  entropy="$(printf '%s' "$ent_line" | cut -f1)"
  dupb="$(printf '%s' "$ent_line" | cut -f2)"
  printf '%s\t%s\t%s\t%s\n' "$sub" "$entropy" "$dupb" "$weight" >> "$RESULTS"
done

# --- 3. budget-gated plan (qualify + rank + select top-K) -------------------
python3 "$SCRIPTS/fleet.py" plan --parent "$PARENT_DIR" --results "$RESULTS" \
  --budget "$BUDGET" --dup-threshold "$DUP_THRESHOLD" > "$DROWS" 2>/dev/null || true

echo "- budget: $BUDGET agent slot(s), dup-threshold $DUP_THRESHOLD" >> "$FLEET_LOG"

# Iterate decisions: run the agent for SELECTED subs (auth permitting), record
# the final ledger row per sub.
while IFS=$'\t' read -r sub entropy delta reason selected defer dupb; do
  [[ -z "$sub" ]] && continue
  agent="-"
  verdict="$reason"
  if $AUTH_DOWN; then
    if [[ "$reason" != "stable" ]]; then agent="auth-skip"; fi
    echo "- $sub: entropy $entropy (delta $delta, $reason) — agent $agent" >> "$FLEET_LOG"
  elif [[ "$selected" == "1" ]]; then
    if $DRY_RUN; then
      agent="would-run"
      echo "- $sub: entropy $entropy (delta $delta, $reason) — agent WOULD run (dry)" >> "$FLEET_LOG"
    else
      # Run the sub's OWN daily-run WITH the agent. We already did the ONE fleet
      # auth probe, so tell the sub's daily-run to skip its own probe (avoids N
      # probes) and just attempt the agent.
      SELF_COMPANY_SKIP_AUTH_PROBE=1 SC_NO_RAG="${SC_NO_RAG:-}" \
        bash "$SCRIPTS/daily-run.sh" "$sub" >/dev/null 2>&1 || \
        echo "- WARN agent pass errored for $sub — continuing" >> "$FLEET_LOG"
      agent="ran"
      echo "- $sub: entropy $entropy (delta $delta, $reason) — agent RAN" >> "$FLEET_LOG"
    fi
  elif [[ "$reason" != "stable" ]]; then
    agent="budget-deferred (rank $defer)"
    echo "- $sub: entropy $entropy (delta $delta, $reason) — $agent" >> "$FLEET_LOG"
  else
    echo "- $sub: entropy $entropy (delta $delta, stable) — agent not needed" >> "$FLEET_LOG"
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$sub" "$entropy" "$delta" "$verdict" "$agent" >> "$FROWS"
done < "$DROWS"

# --- 4. combined fleet ledger + state -------------------------------------
if $DRY_RUN; then
  echo "- dry-run: no fleet-ledger append, no fleet-state write" >> "$FLEET_LOG"
else
  python3 "$SCRIPTS/fleet.py" commit --parent "$PARENT_DIR" --rows "$FROWS" --tick "$NOW" 2>/dev/null \
    && echo "- fleet ledger + state updated ($REPORTS/fleet-ledger.md)" >> "$FLEET_LOG" \
    || echo "- WARN failed to write fleet ledger/state" >> "$FLEET_LOG"
fi

echo "[fleet-run] done ($NOW) — see $FLEET_LOG"
exit 0
