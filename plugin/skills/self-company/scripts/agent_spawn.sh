#!/usr/bin/env bash
###############################################################################
# agent_spawn.sh — Phase 28 Item 4b (D1/D2/D6): the SINGLE shared bash lib for
# the hand-rolled claude-spawn scaffolding that had drifted across five spawn
# scripts (daily-run.sh, company-run.sh, fleet-run.sh, research-scan.sh,
# fire-trigger.sh) plus schedule.sh's cron-line builder.
#
# Verified-live drift this consolidates (audit 2026-07-09, re-verified at HEAD
# 2026-07-10): schedule.sh:120 already has a DIVERGENT CLAUDE_BIN resolver
# (`command -v claude || echo "$HOME/.local/bin/claude"` — no `-x` check) vs
# the other five call sites' 2-line `[[ -z "$CLAUDE_BIN" && -x ... ]]` pattern.
# The drift class is not hypothetical; it is live.
#
# SOURCING CONTRACT: this lib cannot resolve the directory it needs to find
# ITSELF, so every caller keeps a tiny 3-line bootstrap BESIDE its own
# `${BASH_SOURCE[0]}` resolution (the exact shape every caller already had for
# its OWN scripts-dir precedence block):
#
#   _SC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   # shellcheck source=agent_spawn.sh
#   source "$_SC_LIB_DIR/agent_spawn.sh"
#
# Functions never `exit`/`return` non-zero to abort the caller's script — every
# one degrades to an empty/`unknown` result on any failure (the same fail-open
# discipline every consolidated copy already had). Nothing here changes BEHAVIOR
# for a healthy install; it changes WHERE the one correct implementation lives.
###############################################################################

# --- D1: CLAUDE_BIN resolution -----------------------------------------------
# Majority semantics (5 of 6 call sites pre-Phase-28): `command -v claude`,
# else `$HOME/.local/bin/claude` IF EXECUTABLE. Prints the resolved path (may
# be empty — callers gate on `-n "$CLAUDE_BIN"` before invoking it).
sc_resolve_claude_bin() {
  local bin
  bin="$(command -v claude 2>/dev/null || true)"
  if [[ -z "$bin" && -x "$HOME/.local/bin/claude" ]]; then
    bin="$HOME/.local/bin/claude"
  fi
  printf '%s' "$bin"
}

# --- D1: the kill-after timeout probe ----------------------------------------
# `timeout -k GRACE` (GNU coreutils) SIGKILLs a SIGTERM-ignoring child GRACE
# seconds past its budget — without it, a `claude -p` that traps/ignores
# SIGTERM survives past budget as an orphan. `-k` support is probed once (not
# assumed): sets the global array SC_TMO to `(timeout -k KILL_AFTER)` when
# supported, else the plain `(timeout)` (degrades to a bare SIGTERM timeout on
# non-GNU `timeout`, exactly as every pre-Phase-28 copy did).
# Usage: sc_tmo "$KILL_AFTER"; "${SC_TMO[@]}" "$BUDGET_SECONDS" cmd...
sc_tmo() {
  local kill_after="${1:-30}"
  SC_TMO=(timeout)
  if timeout -k 1 1 true 2>/dev/null; then
    SC_TMO=(timeout -k "$kill_after")
  fi
}

# --- D2: the auth pre-flight probe -------------------------------------------
# `claude auth status --json` is a LOCAL credential check (no model call, ~0.2s,
# zero tokens). Env contracts preserved EXACTLY (both pre-Phase-28 copies —
# daily-run.sh and fleet-run.sh — already agreed on these):
#   SELF_COMPANY_FORCE_AUTH_FAIL=1   force the "no" branch (tests)
#   SELF_COMPANY_SKIP_AUTH_PROBE=1   skip the probe entirely -> "unknown"
#   SELF_COMPANY_AUTH_PROBE_TIMEOUT  probe timeout seconds (default 20)
# Requires $CLAUDE_BIN set by the caller (a global, not a parameter — matches
# every pre-Phase-28 copy, which all read a caller-scoped $CLAUDE_BIN). An
# inconclusive result (missing/old CLI, unexpected output, OR $CLAUDE_BIN
# empty) returns "unknown" — a caller must never suppress a working agent on a
# false negative. Echoes: yes | no | unknown.
sc_auth_logged_in() {
  [[ "${SELF_COMPANY_FORCE_AUTH_FAIL:-}" == "1" ]] && { echo no; return; }
  [[ "${SELF_COMPANY_SKIP_AUTH_PROBE:-}" == "1" ]] && { echo unknown; return; }
  [[ -z "${CLAUDE_BIN:-}" ]] && { echo unknown; return; }
  local out
  out="$(timeout "${SELF_COMPANY_AUTH_PROBE_TIMEOUT:-20}" \
         "$CLAUDE_BIN" auth status --json 2>/dev/null)" || true
  if printf '%s' "$out" | grep -q '"loggedIn"[[:space:]]*:[[:space:]]*true'; then
    echo yes
  elif printf '%s' "$out" | grep -q '"loggedIn"[[:space:]]*:[[:space:]]*false'; then
    echo no
  else
    echo unknown
  fi
}

# --- D1: the plain CAPTURE_ACTIVE + kill-after + `claude -p` invocation ------
# Used by company-run.sh (foreground, output captured), research-scan.sh
# (foreground, appended to a log file), and fire-trigger.sh (DETACHED via
# `nohup ... &`). Those three shapes differ on foreground-vs-detached and on
# output redirection — both stay the CALLER's concern. This function only
# builds the shared argv (env-wrapped so it stays ONE array `exec`-able,
# `nohup`-able, or backgroundable as the caller needs) into the global array
# SC_SPAWN_CMD; it does NOT execute it.
#
# NOTE (Elon's explicit instruction): daily-run.sh's setsid+watchdog+lock-fd
# spawn topology (its own headless-agent block) does NOT adopt this — that
# topology is P25-hardened and stays exactly as it is; daily-run.sh only
# adopts sc_resolve_claude_bin/sc_auth_logged_in/sc_resolve_scripts_dir from
# this lib.
#
# Args: $1=KILL_AFTER $2=BUDGET_SECONDS $3=CLAUDE_BIN $4=PROMPT $5=MODEL
#       [$6...]=extra `claude` flags (e.g. --output-format stream-json)
# Sets global array SC_SPAWN_CMD. Usage:
#   sc_spawn_capture "$KILL_AFTER" "$BUDGET" "$CLAUDE_BIN" "$PROMPT" "$MODEL"
#   raw="$("${SC_SPAWN_CMD[@]}" 2>/dev/null || true)"                    # foreground capture
#   "${SC_SPAWN_CMD[@]}" >>"$LOG" 2>&1                                   # foreground, logged
#   nohup "${SC_SPAWN_CMD[@]}" >>"$LOG" 2>&1 &                           # detached
sc_spawn_capture() {
  local kill_after="$1" budget="$2" claude_bin="$3" prompt="$4" model="$5"
  shift 5
  sc_tmo "$kill_after"
  SC_SPAWN_CMD=(env SELF_COMPANY_CAPTURE_ACTIVE=1 "${SC_TMO[@]}" "$budget" \
    "$claude_bin" -p "$prompt" --model "$model" "$@")
}

# --- D6: scripts-dir precedence -----------------------------------------------
# Code/data separation: run the CANONICAL scripts, not a stale `.company/scripts`
# copy. Precedence: plugin root -> own dir -> legacy `.company/scripts` (the B1
# safety net for an install where a needed sibling isn't beside the caller but
# an old `.company/scripts` copy still has it).
#
# `own_dir` MUST be the CALLER's own `${BASH_SOURCE[0]}`-resolved directory —
# this lib cannot resolve that for itself (see the module docstring).
# `company_dir` is the caller's ALREADY-RESOLVED `.company` directory (not
# PROJECT_DIR — company-run.sh's own COMPANY resolution has a dev-mode branch
# where it is NOT simply `$PROJECT_DIR/.company`, so the legacy fallback must
# use whatever the caller already decided COMPANY is). `sentinel` is the file
# used to detect "own_dir is missing a needed sibling" (each existing copy
# used a different one: daily-run.sh/fleet-run.sh checked decay.py/
# daily-run.sh's presence, company-run.sh checked supervisor.py) — defaults to
# "daily-run.sh" (present in every real scripts dir).
# Args: $1=own_dir $2=company_dir [$3=sentinel_file]
sc_resolve_scripts_dir() {
  local own_dir="$1" company_dir="$2" sentinel="${3:-daily-run.sh}" scripts
  if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" && -d "${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts" ]]; then
    scripts="${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts"
  else
    scripts="$own_dir"
  fi
  if [[ ! -f "$scripts/$sentinel" && -f "$company_dir/scripts/$sentinel" ]]; then
    scripts="$company_dir/scripts"
  fi
  printf '%s' "$scripts"
}
