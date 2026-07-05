#!/usr/bin/env bash
###############################################################################
# research-scan.sh — WEEKLY external-improvement survey (Mike's research pass).
#
# The daily cron keeps the company healthy internally (Tony's inside role); this
# keeps it LEARNING from the outside. Once a week it runs a bounded headless
# `claude -p` as MIKE, the R&D Researcher: survey the web (WebSearch/WebFetch, if
# the runtime exposes them) AND re-read the current scripts for concrete, cited,
# evidence-backed improvements we don't already do. Mike writes THREE outputs — a
# dated cited BRIEF to ops/research/research-<date>.md (his durable deliverable),
# the mechanism-level items appended to ops/plans/proposals-<date>.md per policy
# §6.3 (Tony/Elon's actionable channel), and a one-line summary to the daily log.
# If the runtime has no web tools, it degrades to an internal audit and says so —
# never fabricates sources, never fails the cron.
#
# Usage: research-scan.sh [PROJECT_DIR]
#   SELF_COMPANY_RESEARCH_MODEL   model for the pass (default claude-sonnet-4-6)
#   SELF_COMPANY_RESEARCH_TIMEOUT seconds (default 900)
###############################################################################
set -uo pipefail

PROJECT_DIR="${SELF_COMPANY_PROJECT_DIR:-${1:-$PWD}}"
COMPANY="$PROJECT_DIR/.company"
if [[ ! -d "$COMPANY" ]]; then
  echo "[research-scan] no .company at $COMPANY — nothing to do"; exit 0
fi
DATE="$(date +%F)"; TS="$(date +%FT%T)"
LOGDIR="$COMPANY/ops/logs"; PLANS="$COMPANY/ops/plans"; RESEARCH="$COMPANY/ops/research"
mkdir -p "$LOGDIR" "$PLANS" "$RESEARCH"
LOG="$LOGDIR/research-$DATE.log"
CRONLOG_LINE="$COMPANY/ops/logs/daily-$DATE.md"

CLAUDE_BIN="$(command -v claude || true)"
[[ -z "$CLAUDE_BIN" && -x "$HOME/.local/bin/claude" ]] && CLAUDE_BIN="$HOME/.local/bin/claude"
if [[ -z "$CLAUDE_BIN" ]]; then
  echo "[research-scan] claude CLI not found — skipped"; exit 0
fi

read -r -d '' PROMPT <<EOF || true
You are MIKE, the self-company R&D Researcher, doing the WEEKLY external research
scan (non-interactive, no human). Working dir: $PROJECT_DIR.

Your job: survey the OUTSIDE world for concrete, cited, evidence-backed improvements
to THIS skill — and report honestly what we already have (Tony measures inside, you
survey outside). First GROUND yourself in our current capabilities so you don't
re-propose them: skim SKILL.md, references/memory-tiers.md, and scripts/{entropy,
decay,reinforce_memory,rag_query,trigger_eval}.py . We already have: tiered markdown
memory (L0/L1/L2), half-life decay, an entropy KPI, a verify-against-source loop,
optional RAG, four triggers, a §5.5 reporting chain, a §6 self-upgrade loop, and
trigger_eval.py. Hard constraint: scripts are pure stdlib + bash, dormant-safe.

Mike's Iron Rules — apply them:
- Every claim carries a SOURCE: title + org + year + URL. An uncited claim is a rumor.
- Applicability-first: every finding maps to one of OUR mechanisms or gaps ("so what,
  for us"). A finding that maps to nothing gets one appendix line, not the body.
- Honest about coverage: explicitly LIST what we ALREADY have that matches or beats
  the external mechanism — do not re-propose it.
- Constraint filter: FLAG anything needing network at runtime, non-stdlib deps, or
  cloud memory services as a VIOLATION — never recommend it.

If WebSearch / WebFetch tools are available, survey current best practices (Anthropic
skill/agent docs & engineering blog; LLM agent-memory literature; multi-agent
orchestration) and CITE real primary URLs. If those tools are NOT available, do an
INTERNAL audit of the current scripts instead and state clearly at the top "(no web
access this run — internal audit only)". NEVER invent sources.

Premise-check every idea against the ACTUAL code before writing it (per §6.3 — do not
propose fixing a problem that doesn't reproduce). Then write THREE outputs:

1. A dated cited BRIEF to .company/ops/research/research-$DATE.md (create/overwrite) —
   your durable deliverable. Structure it as:
   ## Sources — title + org + year + URL for every claim (or "internal audit only")
   ## Findings — ranked by applicability-to-us, each mapped to our mechanism/gap
   ## Already-covered — external mechanisms our system already implements
   ## Constraint check — anything violating offline/privacy or stdlib-only, flagged

2. Append the mechanism-level, actionable items to .company/ops/plans/proposals-$DATE.md
   (create/append) for Tony/Elon, each as (per §6.3):
   **Problem/Gap**, **Proposal** (+ does it fit stdlib?), **Source** (URL or "internal"),
   **Size** (small/big). Prefer 2 well-grounded proposals over 4 thin ones.

3. Append a one-line summary to .company/ops/logs/daily-$DATE.md. Keep it tight.
EOF

printf '\n===== research-scan %s =====\n' "$TS" >> "$LOG"
SELF_COMPANY_CAPTURE_ACTIVE=1 timeout "${SELF_COMPANY_RESEARCH_TIMEOUT:-900}" \
  "$CLAUDE_BIN" -p "$PROMPT" --model "${SELF_COMPANY_RESEARCH_MODEL:-claude-sonnet-4-6}" \
  >>"$LOG" 2>&1
rc=$?
if (( rc == 0 )); then
  echo "- research-scan: ok — brief in ops/research/research-$DATE.md, proposals in ops/plans/proposals-$DATE.md" >> "$CRONLOG_LINE" 2>/dev/null || true
  echo "[research-scan] done ($DATE) — brief in ops/research/research-$DATE.md, proposals in ops/plans/proposals-$DATE.md"
else
  echo "- research-scan: failed (rc $rc)" >> "$CRONLOG_LINE" 2>/dev/null || true
  echo "[research-scan] failed (rc $rc) — see $LOG"
fi
exit 0
