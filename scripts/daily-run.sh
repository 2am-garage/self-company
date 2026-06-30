#!/usr/bin/env bash
###############################################################################
# daily-run.sh — self-company daily maintenance batch (Tom's scheduled job).
#
# Invoked by cron (every 6h, see schedule.sh) or manually. Two parts:
#   1. DETERMINISTIC core (always, no tokens): decay.py --apply + entropy.py,
#      logged. Decays stale L0, demotes/archives cold L1, surfaces upgrade and
#      dup/contradiction candidates. Safe and fully testable.
#   2. OPTIONAL agent step (default on, bounded): a headless `claude -p` pass for
#      the CONSOLIDATE/VERIFY judgment. Conservative by instruction (annotate +
#      reinforce, never delete). Timeout + graceful degrade + recursion guard so
#      it can never hang the cron or loop into the CAPTURE Stop hook.
#
# Never fails the cron: the deterministic core runs even if the agent is absent.
#
# Usage: daily-run.sh [PROJECT_DIR] [--no-agent] [--dry-run]
###############################################################################
set -uo pipefail   # not -e: a failing optional step must not abort the run

PROJECT_DIR="${SELF_COMPANY_PROJECT_DIR:-$PWD}"
RUN_AGENT=true
DRY_RUN=false
for a in "$@"; do
  case "$a" in
    --no-agent) RUN_AGENT=false ;;
    --dry-run)  DRY_RUN=true; RUN_AGENT=false ;;
    -*)         : ;;                       # ignore unknown flags
    *)          PROJECT_DIR="$a" ;;        # positional = project dir
  esac
done

COMPANY="$PROJECT_DIR/.company"
SCRIPTS="$COMPANY/scripts"
POLICY="$COMPANY/org/policy.md"
MEM="$COMPANY/memory"
LOGDIR="$COMPANY/ops/logs"
DATE="$(date +%F)"
LOG="$LOGDIR/daily-$DATE.md"

if [[ ! -d "$COMPANY" ]]; then
  echo "[daily-run] no .company at $COMPANY — nothing to do"
  exit 0
fi
mkdir -p "$LOGDIR"

ts="$(date +%FT%T)"
printf '\n## Daily run %s%s\n' "$ts" "$($DRY_RUN && echo ' (dry-run)')" >> "$LOG"

# --- 1+2. deterministic core: decay + entropy -----------------------------
# Capture each script's JSON to a temp file, then parse via a heredoc that reads
# the FILES by path. (Piping data INTO a `python3 <<'PY'` heredoc does not work —
# the heredoc itself becomes stdin, so the pipe is ignored.)
DOUT="$(mktemp)"; EOUT="$(mktemp)"; VOUT="$(mktemp)"; SERR="$(mktemp)"
trap 'rm -f "$DOUT" "$EOUT" "$VOUT" "$SERR"' EXIT

if [[ -f "$SCRIPTS/decay.py" ]]; then
  decay_args=(--memory-dir "$MEM" --config "$POLICY")
  $DRY_RUN || decay_args+=(--apply)
  python3 "$SCRIPTS/decay.py" "${decay_args[@]}" >"$DOUT" 2>>"$SERR" || true
fi
# VERIFY: stamp verified_date on memories whose [session#line] sources trace to a
# real transcript line (deterministic provenance gate). Before entropy so the KPI
# reflects the new stamps this round.
if [[ -f "$SCRIPTS/verify_memory.py" ]]; then
  verify_args=(--memory-dir "$MEM" --transcripts-dir "$HOME/.claude/projects")
  $DRY_RUN || verify_args+=(--apply)
  python3 "$SCRIPTS/verify_memory.py" "${verify_args[@]}" >"$VOUT" 2>>"$SERR" || true
fi
[[ -f "$SCRIPTS/entropy.py" ]] && \
  python3 "$SCRIPTS/entropy.py" --memory-dir "$MEM" --config "$POLICY" >"$EOUT" 2>>"$SERR" || true

DRY_FLAG="$($DRY_RUN && echo 1 || echo 0)"
python3 - "$DOUT" "$EOUT" "$VOUT" "$LOG" "$DRY_FLAG" <<'PY' || echo "- deterministic core: log-parse error" >> "$LOG"
import sys, json
dpath, epath, vpath, log, dry = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] == "1"

def load(p):
    try:
        return json.load(open(p))
    except Exception:
        return None

lines = []
d = load(dpath)
if d:
    a = d["actions"]
    lines.append(
        f"- decay{'' if dry else ' --apply'}: scanned {d['scanned']} | "
        f"drop {len(a['drop'])} | demote {len(a['demote'])} | "
        f"archive {len(a['archive'])} | upgrade-candidates {len(a['upgrade_candidates'])}"
    )
    if a["upgrade_candidates"]:
        lines.append("### Upgrade candidates (for next CONSOLIDATE by Tony)")
        for c in a["upgrade_candidates"]:
            lines.append(f"- {c['id']}: {c['from']} -> {c['to']} (rc {c['reinforce_count']})")
else:
    lines.append("- decay: no output (script missing or errored)")

v = load(vpath)
if v:
    lines.append(
        f"- verify{'' if dry else ' --apply'}: newly-verified {len(v['verified'])} | "
        f"already {v['already_verified']} | unverifiable {len(v['unverifiable'])}"
    )
    if v["unverifiable"]:
        lines.append(f"  - unverifiable (sources don't trace): {v['unverifiable'][:8]}")

e = load(epath)
if e:
    dim, det = e["dimensions"], e["details"]
    lines.append(
        f"- entropy {e['entropy']} (dup {dim['dup_rate']} | contra {dim['contradiction_score']} | "
        f"stale {dim['stale_rate']} | unverified {dim['unverified_rate']}) over {e['total_memories']} memories"
    )
    if det["contradiction_pairs"]:
        lines.append(f"  - contradiction candidates: {det['contradiction_pairs']}")
    if det["duplicate_pairs"]:
        lines.append(f"  - duplicate candidates: {det['duplicate_pairs']}")

with open(log, "a") as f:
    f.write("\n".join(lines) + "\n")
PY

# Elon's daily survey: a prioritized TODO from current metrics (read-only,
# deterministic — keeps the CEO load-bearing every day). Writes ops/plans/todo-<date>.md.
if [[ -f "$SCRIPTS/elon_survey.py" ]]; then
  python3 "$SCRIPTS/elon_survey.py" --company "$COMPANY" 2>>"$SERR" \
    | python3 -c "import sys, json
try:
    d = json.load(sys.stdin)
    print(f\"- elon survey: {d.get('todos','?')} todo(s) -> ops/plans/todo-${DATE}.md\")
except Exception:
    print('- elon survey: no output')" >> "$LOG" || true
fi

# Scheduled-work ledger (autoresearch-style): regenerate ops/reports/ledger.md so
# the Chairman wakes up to a one-row-per-run report — entropy headline, keep/flat/
# skip/fail verdict, one-line description. Read-only over the logs; deterministic.
if [[ -f "$SCRIPTS/report.py" ]]; then
  python3 "$SCRIPTS/report.py" --company "$COMPANY" --write >/dev/null 2>>"$SERR" \
    && echo "- ledger: refreshed ops/reports/ledger.{md,tsv}" >> "$LOG" || true
fi

# C2: surface any script warnings/errors instead of swallowing them (e.g. the
# policy-provenance [WARN] that P3 added, or a real crash).
if [[ -s "$SERR" ]]; then
  {
    echo "- script warnings/errors:"
    sed 's/^/    /' "$SERR" | head -20
  } >> "$LOG"
fi

# --- 3. optional headless agent (CONSOLIDATE / VERIFY judgment) -------------
if $RUN_AGENT; then
  # B1 token-breaker proxy: cap headless-agent runs per day (a proxy for the
  # policy §3 daily token ceiling — the agent step is the only token spend). Past
  # the cap, degrade to deterministic-only so unattended runs can't overspend.
  CAP="$(python3 -c "import sys; sys.path.insert(0, '$SCRIPTS')
try:
    from policy_config import load_policy_constants as L
    print(int(L('$POLICY').get('DAILY_RUNS_PER_DAY', 4)))
except Exception:
    print(4)" 2>/dev/null || echo 4)"
  COUNTER="$LOGDIR/.agent_runs_$DATE"
  RUNS="$(cat "$COUNTER" 2>/dev/null || echo 0)"
  [[ "$RUNS" =~ ^[0-9]+$ ]] || RUNS=0
  CLAUDE_BIN="$(command -v claude || true)"
  [[ -z "$CLAUDE_BIN" && -x "$HOME/.local/bin/claude" ]] && CLAUDE_BIN="$HOME/.local/bin/claude"
  if (( RUNS >= CAP )); then
    echo "- agent: skipped — daily agent-run cap reached ($RUNS/$CAP, token breaker)" >> "$LOG"
  elif [[ -n "$CLAUDE_BIN" ]]; then
    AGENT_LOG="$LOGDIR/agent-$DATE.log"
    read -r -d '' PROMPT <<EOF || true
You are the self-company DAILY maintenance agent running non-interactively (no human).
Working dir: $PROJECT_DIR . Memory lives in .company/memory (L0-working, L1-warm, L2-cold).
Do a CONSERVATIVE consolidation pass per references/pipeline.md and references/memory-tiers.md:
- Read L0-working memories. Where two L0 entries are clearly the same observation,
  reinforce (merge sources, reinforce_count++, last_reinforced=today) into one and
  remove the exact duplicate.
- Promote an L0 memory to L1-warm only if reinforce_count>=2; L1 to L2-cold only if >=4.
- Do NOT invent memories, do NOT delete anything that is not an exact duplicate, do NOT
  touch L2 except to add a contradiction note. Keep frontmatter valid (policy.md §4.2).
- Append a short summary of what you changed to .company/ops/logs/daily-$DATE.md.
Be quick and conservative. If unsure, leave it and just note it in the log.
EOF
    # A3: capture the agent's stdout/stderr to an audit log (not /dev/null).
    # Recursion guard so this agent's own Stop hook (CAPTURE) no-ops; hard timeout.
    printf '\n===== agent run %s =====\n' "$ts" >> "$AGENT_LOG"
    SELF_COMPANY_CAPTURE_ACTIVE=1 timeout "${SELF_COMPANY_DAILY_TIMEOUT:-600}" \
         "$CLAUDE_BIN" -p "$PROMPT" --model "${SELF_COMPANY_DAILY_MODEL:-claude-sonnet-4-6}" \
         >>"$AGENT_LOG" 2>&1
    rc=$?
    echo "$((RUNS + 1))" > "$COUNTER"   # B1: count the run (it spent tokens) toward the cap
    if (( rc == 0 )); then
      echo "- agent (consolidate/verify): ok [run $((RUNS + 1))/$CAP; stdout in agent-$DATE.log]" >> "$LOG"
    else
      echo "- agent: failed (rc $rc) [run $((RUNS + 1))/$CAP] — deterministic maintenance still applied" >> "$LOG"
    fi
  else
    echo "- agent: claude CLI not found — skipped (deterministic maintenance applied)" >> "$LOG"
  fi
fi

echo "[daily-run] done ($DATE) — see $LOG"
exit 0
