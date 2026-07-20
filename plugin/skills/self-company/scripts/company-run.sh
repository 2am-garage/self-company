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
# Phase 28 Item 4b (D1): the claude-spawn scaffolding (CLAUDE_BIN resolution,
# the kill-after timeout probe, the CAPTURE_ACTIVE + `claude -p` wrapper) is
# the ONE shared lib (agent_spawn.sh, same dir) — see its header for why every
# caller keeps this exact bootstrap instead of the lib resolving its own dir.
# shellcheck source=agent_spawn.sh
source "$SCRIPT_DIR/agent_spawn.sh"

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
# Run the CANONICAL scripts: plugin root -> own dir -> legacy .company/scripts
# fallback (Phase 28 Item 4b D6: the shared precedence in agent_spawn.sh).
SCRIPTS_RT="$(sc_resolve_scripts_dir "$SCRIPT_DIR" "$COMPANY" "supervisor.py")"

if [[ -z "$TASK" ]]; then
  echo "usage: company-run.sh \"<task>\" [--demo] [--company DIR]" >&2
  exit 2
fi

REPORTS="$COMPANY/ops/reports"; mkdir -p "$REPORTS"
LEDGER="$REPORTS/company-runs.md"
TS="$(date +%FT%T)"

# --- 1. PLAN (Phoebe) ------------------------------------------------------
LOGDIR="$COMPANY/ops/logs"; mkdir -p "$LOGDIR"
PLAN_LOG="$LOGDIR/company-run-plan-$(date +%F).log"
plan_json=""; plan_partial=false
if ! $DEMO; then
  CLAUDE_BIN="$(sc_resolve_claude_bin)"
  if [[ -n "$CLAUDE_BIN" ]]; then
    roster_raw="$(python3 "$SCRIPTS_RT/supervisor.py" --company "$COMPANY" --list 2>/dev/null)"
    roster="$(printf '%s' "$roster_raw" | python3 -c "
import json, sys
try:
    print(', '.join(json.load(sys.stdin).get('roster', [])))
except Exception:
    print('')
" 2>/dev/null || true)"
    # Phase 29 Item 4 (Bob P1): STATE the real wall-clock budget (the timeout
    # wrapper below receives the SAME 180) and the output contract explicitly
    # (Idea 7's four elements), instead of an implicit "JSON only" aside.
    PLAN_BUDGET_SECONDS=180
    ROLE_LINE="$(python3 "$SCRIPTS_RT/prompt_builder.py" role \
      --name "Phoebe" --role "the self-company execution gateway")"
    BUDGET_LINE="$(python3 "$SCRIPTS_RT/prompt_builder.py" budget --seconds "$PLAN_BUDGET_SECONDS")"
    # Mike 07-20 F2: deliberately NOT passing --summary-cap here either. This
    # is the PLAN step — it dispatches Phoebe as orchestrator (ROLE_LINE
    # above), producing a routing decision Phoebe's own downstream worker
    # dispatches depend on, not a worker's distilled handoff summary. Capping
    # it would truncate the very JSON company-run.sh parses next.
    CONTRACT_LINE="$(python3 "$SCRIPTS_RT/prompt_builder.py" contract \
      --where "your response" \
      --format 'ONLY a single JSON object mapping employee id -> a one-line subtask (e.g. {"bob":"...", "gibby":"verify Bob'"'"'s change"}) — no prose, no markdown fence')"
    BOUNDARY_LINE="$(python3 "$SCRIPTS_RT/prompt_builder.py" boundary \
      --text "use ONLY employee ids from the list above — an id not in that list will be dropped before dispatch")"
    read -r -d '' PPROMPT <<EOF || true
$ROLE_LINE
$BUDGET_LINE
Break this task into a MINIMAL per-employee assignment.
Employees available: $roster.
Task: "$TASK"
$CONTRACT_LINE
$BOUNDARY_LINE
EOF
    # Item 1 (TOM-2): hard-kill grace on Phoebe's planning spawn too — a claude
    # that ignores SIGTERM is SIGKILLed <grace>s past budget, no orphan. `-k` is
    # GNU coreutils; degrade to a plain SIGTERM timeout where unsupported.
    KILL_AFTER="${SELF_COMPANY_TIMEOUT_KILL_AFTER:-30}"
    # Phase 29 Item 2: default model resolves through schedule_config's ONE
    # source-of-truth constant (DEFAULT_AGENT_MODEL) instead of a second
    # hardcoded literal here — env override still wins.
    _default_model="$(python3 "$SCRIPTS_RT/schedule_config.py" --company "$COMPANY" \
      --agent model 2>/dev/null || true)"
    [[ -n "$_default_model" ]] || _default_model="claude-sonnet-5"
    # Phase 29 fold-in H3: --output-format json gives ONE parseable envelope
    # (the model's reply lives in .result) instead of scraping raw stdout —
    # see the roster-validation parser below.
    sc_spawn_capture "$KILL_AFTER" "$PLAN_BUDGET_SECONDS" "$CLAUDE_BIN" "$PPROMPT" \
      "${SELF_COMPANY_PLAN_MODEL:-$_default_model}" --output-format json
    raw="$("${SC_SPAWN_CMD[@]}" 2>>"$PLAN_LOG" || true)"
    # H3: parse the JSON envelope -> .result -> the plan object inside it,
    # DROPPING any key that isn't a real roster employee id (a hallucinated
    # {"alice": "..."} must never silently dispatch nobody and ledger rc 0 —
    # every path below is logged, never swallowed by `2>/dev/null || true`).
    parsed="$(printf '%s' "$raw" | python3 -c "
import json, re, sys

roster = [e.strip() for e in sys.argv[1].split(',') if e.strip()]
raw = sys.stdin.read()
out = {'plan': {}, 'dropped': [], 'parse_error': None}


def emit():
    print(json.dumps(out))


try:
    envelope = json.loads(raw)
except Exception as e:
    out['parse_error'] = f'envelope not JSON: {e}'
    emit()
    sys.exit(0)
if not isinstance(envelope, dict):
    out['parse_error'] = 'envelope not a JSON object'
    emit()
    sys.exit(0)
if envelope.get('is_error'):
    out['parse_error'] = f\"claude reported an error (subtype={envelope.get('subtype')})\"
    emit()
    sys.exit(0)
result_text = envelope.get('result')
if not isinstance(result_text, str) or not result_text.strip():
    out['parse_error'] = 'no .result text in envelope'
    emit()
    sys.exit(0)
candidates = re.findall(r'\{[^{}]*\}', result_text, re.S)
plan = None
for cand in reversed(candidates):
    try:
        d = json.loads(cand)
        if isinstance(d, dict) and d:
            plan = d
            break
    except Exception:
        continue
if plan is None:
    out['parse_error'] = 'no parseable JSON object found in .result'
    emit()
    sys.exit(0)
valid, dropped = {}, []
for k, v in plan.items():
    if k in roster:
        valid[k] = v
    else:
        dropped.append(k)
out['plan'] = valid
out['dropped'] = dropped
emit()
" "$roster" 2>>"$PLAN_LOG" || echo '{"plan": {}, "dropped": [], "parse_error": "parser crashed"}')"

    parse_error="$(printf '%s' "$parsed" | python3 -c "import json,sys; print(json.load(sys.stdin).get('parse_error') or '')" 2>/dev/null || true)"
    dropped="$(printf '%s' "$parsed" | python3 -c "import json,sys; print(', '.join(json.load(sys.stdin).get('dropped') or []))" 2>/dev/null || true)"
    plan_json="$(printf '%s' "$parsed" | python3 -c "
import json, sys
d = json.load(sys.stdin).get('plan') or {}
print(json.dumps(d) if d else '')
" 2>/dev/null || true)"

    if [[ -n "$parse_error" ]]; then
      echo "$(date +%FT%T) plan parse failed: $parse_error (raw: ${raw:0:200})" >> "$PLAN_LOG"
    fi
    if [[ -n "$dropped" ]]; then
      echo "$(date +%FT%T) plan dropped unknown employee id(s): $dropped" >> "$PLAN_LOG"
      plan_partial=true
    fi
  fi
fi

# Heuristic fallback / demo plan: Bob does it, Gibby verifies. Also the
# recovery path when Phoebe's plan parsed to NOTHING usable (garbage output,
# an error envelope, or every key hallucinated) — H3: this must never
# silently ledger an empty/no-op plan as a clean success.
if [[ -z "$plan_json" ]]; then
  plan_json="$(python3 -c "import json,sys; print(json.dumps({'bob': sys.argv[1], 'gibby': 'verify the change'}))" "$TASK")"
  if ! $DEMO && [[ -n "${parse_error:-}${dropped:-}" ]]; then
    planned_by="heuristic-after-invalid-plan"
  else
    planned_by="heuristic"
  fi
elif $plan_partial; then
  planned_by="Phoebe (plan:partial)"
else
  planned_by="Phoebe"
fi

echo "[company-run] task: $TASK"
echo "[company-run] plan ($planned_by): $plan_json"

# --- 2. VALIDATE before dispatch (Phase 32 hotfix Finding 2, defense-in-depth) -
# The supervisor's roster already shares discover()'s strict per-desk predicate,
# so a ghost/symlinked/bad-charset desk is never LISTED. This gate is the
# belt-and-braces companion: run the full Layer-B validator (R1-R7) over the
# store and REFUSE to dispatch on a violation (exit 3), rather than sending
# workers into a company whose org/employees/ carries a flagged desk. Best-
# effort: if python3 or the validator is unavailable we do NOT block (the
# roster-level strictness still holds); only an actual validator VIOLATION
# stops the run.
VALIDATOR_RT="$SCRIPTS_RT/schedule_validator.py"
if command -v python3 >/dev/null 2>&1 && [[ -f "$VALIDATOR_RT" ]]; then
  if ! vout="$(python3 "$VALIDATOR_RT" --company "$COMPANY" 2>&1)"; then
    echo "[company-run] REFUSING to dispatch — org/schedule.yaml or a desk fails Layer-B validation:" >&2
    printf '%s\n' "$vout" | sed 's/^/[company-run]   /' >&2
    exit 3
  fi
fi

# --- 3. DISPATCH (supervisor spawns the assigned employees, live) ----------
# Phase 33 FIX 2 (Finding 3): the gate's rounds/verdict come from the
# supervisor's OWN stderr (`@redblue-gate {json}`), NOT a shared-fs marker a
# Bob worker could overwrite to forge a "clean" ledger cell. We capture the
# supervisor's stderr via an anonymous pipe (command substitution) while its
# stdout — the live tree — passes straight through to the terminal (fd 3
# trick). A worker's own stderr is merged into ITS stdout pipe (consumed by
# the supervisor), so no worker can write the supervisor's fd 2: the captured
# stream is parent-trusted, and there is no filesystem path to target.
#
# Finalization pass (closes the DoS Gibby found): the CAPTURE ITSELF has no
# bound. A dispatched worker still has full Bash (Phase 34's execute tier —
# bob/gibby/tom, by necessity) and can open `/proc/<supervisor-pid>/fd/2` and
# hold it open; that grabs a SECOND reference to the write end of THIS pipe,
# so even once the supervisor process fully exits, this `$(...)` command
# substitution never sees EOF — it hangs forever, wedging the whole session
# (not merely the one worker; the supervisor's own in-process per-worker
# budget is irrelevant here, since it's the CAPTURE PIPE, not the worker,
# that's stuck). `timeout` wraps the whole capture so it is bounded no matter
# WHY it fails to return — env-tunable via SELF_COMPANY_GATE_CAPTURE_TIMEOUT
# (default 900s, comfortably above one full multi-round gate cycle at the
# supervisor's own default per-worker budget so a legitimately slow gate is
# never cut short) with a SIGKILL grace (SELF_COMPANY_TIMEOUT_KILL_AFTER,
# default 30s, the same knob the planning spawn above already uses) via
# agent_spawn.sh's shared `sc_tmo` — one lever, not a second hand-rolled
# timeout wrapper. If it fires we do NOT fall through to a "clean"/placeholder
# ledger row: no `@redblue-gate` line was ever captured, so `gate_line` stays
# empty and `gate_capture_timed_out` is set — the ledger step below records
# this EXPLICITLY as verdict `unresolved` (never silently "-", which would
# read as "no gate armed" rather than "gate outcome unknown, capture killed"),
# and `rc` (124/137 from `timeout`) stays the script's own non-zero exit —
# loud, exactly like the supervisor's own cap-without-pass path.
# Default 2400s: must exceed a LEGIT worst-case gate cycle so a real multi-round
# run isn't false-killed — rounds (default 3) x per-worker dispatch budget
# (default 600s) = 1800s, plus margin. (Gibby 2026-07-18: the old 900s was below
# 1800s and would UNRESOLVED-kill a legit 3-round gate.) NOTE: this bounds an
# ACCIDENTAL hang only; a setsid-detached worker holding the supervisor's fd
# escapes timeout's process-group kill — see red-blue-protocol.md's honest limit.
GATE_CAPTURE_TIMEOUT="${SELF_COMPANY_GATE_CAPTURE_TIMEOUT:-2400}"
GATE_CAPTURE_KILL_AFTER="${SELF_COMPANY_TIMEOUT_KILL_AFTER:-30}"
gate_line=""
gate_capture_timed_out=false
if $DEMO; then
  python3 "$SCRIPTS_RT/supervisor.py" --company "$COMPANY" --demo
  rc=$?
else
  sc_tmo "$GATE_CAPTURE_KILL_AFTER"
  exec 3>&1
  gate_err="$("${SC_TMO[@]}" "$GATE_CAPTURE_TIMEOUT" \
    python3 "$SCRIPTS_RT/supervisor.py" --company "$COMPANY" \
    --dispatch "$plan_json" 2>&1 1>&3)"
  rc=$?
  exec 3>&-
  if [[ "$rc" -eq 124 || "$rc" -eq 137 ]]; then
    gate_capture_timed_out=true
    echo "[company-run] GATE CAPTURE TIMEOUT after ${GATE_CAPTURE_TIMEOUT}s — the" \
         "supervisor's stderr never reached EOF (a worker may be holding its pipe" \
         "open via /proc). Treating this cycle as UNRESOLVED — never clean." >&2
  else
    # Replay the supervisor's diagnostics to the terminal (its loud UNRESOLVED /
    # auto-arm messages), minus the machine `@redblue-gate` sentinel.
    printf '%s\n' "$gate_err" | grep -v '^@redblue-gate ' >&2 || true
    gate_line="$(printf '%s\n' "$gate_err" | grep '^@redblue-gate ' | tail -1 \
      | sed 's/^@redblue-gate //')"
  fi
fi

# --- 4. LEDGER -------------------------------------------------------------
# Store the FULL assignment JSON (no truncation) so org-status.py can attribute
# EVERY assigned employee, not just the first. Sanitize '|' -> '/' so a subtask
# string can't break the markdown table (JSON itself has none). Task stays short.
assign_cell="${plan_json//|//}"          # sanitize pipes; keep the FULL json
task_short="${TASK:0:40}"; task_cell="${task_short//|//}"
# Phase 33: rounds used + final verdict, parsed from the trusted supervisor
# stderr sentinel. "-"/"-" for a lone-worker (non-builder) dispatch or a --demo
# run — the gate never arms there, so nothing is emitted and this stays exactly
# a pre-Phase-33 ledger row except for the two trailing columns.
#
# Finalization pass: a capture-timeout is EXPLICITLY "timeout"/"unresolved" —
# never the "-"/"-" placeholder (which means "gate never armed"; this cycle's
# gate DID arm, its outcome is simply unknown because the capture was killed)
# and never a value borrowed from a stale $gate_line (there isn't one).
#
# Item 3 (2026-07-21 robustness follow-up): an "unresolved" verdict now
# carries a `reason` from the supervisor's own `_unresolved_reason` —
# 'genuine_fail' (Gibby really failed it) vs 'format_miss' (an authenticated
# but unparseable verdict line — likely a sentinel-format miss, not a real
# fail) vs 'no_verdict' (no authenticated line at all). Folded into the SAME
# verdict cell as "unresolved (reason)" rather than a new table column, so an
# existing local ledger file (its header written once, on first run) never
# goes out of sync with a later row that has one more column than the header
# it already committed to.
gate_rounds="-"; gate_verdict="-"
if $gate_capture_timed_out; then
  gate_rounds="timeout"; gate_verdict="unresolved (capture_timeout)"
elif [[ -n "$gate_line" ]]; then
  gate_rounds="$(printf '%s' "$gate_line" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('rounds', '-'))
except Exception:
    print('-')
" 2>/dev/null || echo '-')"
  gate_verdict="$(printf '%s' "$gate_line" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    v = d.get('verdict', '-')
    r = d.get('reason')
    print(f'{v} ({r})' if v == 'unresolved' and r else v)
except Exception:
    print('-')
" 2>/dev/null || echo '-')"
fi
[[ -f "$LEDGER" ]] || printf '# Company Runs (session-triggered)\n\n_Each row: a company work cycle started from the session. See MISSION.md._\n\n| time | task | planned by | assignments | rc | rounds | verdict |\n|---|---|---|---|---|---|---|\n' > "$LEDGER"
printf '| %s | %s | %s | `%s` | %s | %s | %s |\n' "$TS" "$task_cell" "$planned_by" "$assign_cell" "$rc" "$gate_rounds" "$gate_verdict" >> "$LEDGER"

echo "[company-run] done (rc $rc) — logged to ops/reports/company-runs.md"
exit "$rc"
