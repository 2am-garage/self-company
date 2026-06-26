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
DOUT="$(mktemp)"; EOUT="$(mktemp)"
trap 'rm -f "$DOUT" "$EOUT"' EXIT

if [[ -f "$SCRIPTS/decay.py" ]]; then
  decay_args=(--memory-dir "$MEM" --config "$POLICY")
  $DRY_RUN || decay_args+=(--apply)
  python3 "$SCRIPTS/decay.py" "${decay_args[@]}" >"$DOUT" 2>/dev/null || true
fi
[[ -f "$SCRIPTS/entropy.py" ]] && \
  python3 "$SCRIPTS/entropy.py" --memory-dir "$MEM" --config "$POLICY" >"$EOUT" 2>/dev/null || true

DRY_FLAG="$($DRY_RUN && echo 1 || echo 0)"
python3 - "$DOUT" "$EOUT" "$LOG" "$DRY_FLAG" <<'PY' || echo "- deterministic core: log-parse error" >> "$LOG"
import sys, json
dpath, epath, log, dry = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"

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

# --- 3. optional headless agent (CONSOLIDATE / VERIFY judgment) -------------
if $RUN_AGENT; then
  CLAUDE_BIN="$(command -v claude || true)"
  [[ -z "$CLAUDE_BIN" && -x "$HOME/.local/bin/claude" ]] && CLAUDE_BIN="$HOME/.local/bin/claude"
  if [[ -n "$CLAUDE_BIN" ]]; then
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
    # Recursion guard so this agent's own Stop hook (CAPTURE) no-ops; hard timeout.
    if SELF_COMPANY_CAPTURE_ACTIVE=1 timeout "${SELF_COMPANY_DAILY_TIMEOUT:-600}" \
         "$CLAUDE_BIN" -p "$PROMPT" --model "${SELF_COMPANY_DAILY_MODEL:-claude-sonnet-4-6}" \
         >/dev/null 2>&1; then
      echo "- agent (consolidate/verify): ok" >> "$LOG"
    else
      echo "- agent: skipped/failed (rc $?) — deterministic maintenance still applied" >> "$LOG"
    fi
  else
    echo "- agent: claude CLI not found — skipped (deterministic maintenance applied)" >> "$LOG"
  fi
fi

echo "[daily-run] done ($DATE) — see $LOG"
exit 0
