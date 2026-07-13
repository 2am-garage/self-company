#!/usr/bin/env bash
###############################################################################
# hire.sh — scaffold or fire a data-driven employee desk (Phase 32, Item 2).
#
# Hiring becomes DATA: this is a plain command (not a Claude hook), the
# mechanism behind "hire someone" — Layer B stays code-pinned (the four
# charter singletons: Elon/Phoebe/July/Gibby), a hired desk is `tier:
# worker|manager` data under org/employees/<id>/, discovered by
# employee.discover() (Item 1) and validated by schedule_validator.py's R7
# (Item 3). No new hard powers: dispatch still flows through Phoebe, sign-off
# through Gibby, attack/build duty classes stay exclusive to the code-known
# employees.
#
# Usage:
#   hire.sh <id> --tier worker|manager --role "<title>"
#           [--manager <id>] [--people-lead <id>] [--model <alias|claude-*>]
#           [--company DIR]
#   hire.sh --fire <id> [--company DIR]
#
# Defaults (per the spec):
#   worker  -> manager: phoebe, people_lead: july
#   manager -> manager: elon,   people_lead: july
#
# Refuses (never partially scaffolds): an existing id, any charter/core id, a
# bad-charset id, a bad --tier, an unknown --manager/--people-lead reference,
# an invalid --model (same alias/claude-* contract as employee.py's
# resolved_model — but REFUSE at hire time, never silently degrade).
#
# ATOMIC: after scaffolding persona.md + context.md from the templates/
# new-worker-*.md or new-manager-*.md pair, schedule_validator.py runs against
# the company; a nonzero exit removes the just-scaffolded desk again (leaving
# no partial state) and prints the violation.
#
# --fire <id>: refuses a core id; TOMBSTONES the desk to
# org/employees/.fired/<id>-<date>/ (never deletes — the memory store, if any,
# moves WITH it, untouched). Idempotent: firing an id with no desk is a no-op.
#
# ONE source of truth: the core roster + discovery both come from employee.py
# (CORE_EMPLOYEES / discover()) via a python3 shell-out — never a second
# hardcoded id list here, and never a re-implemented model alias table.
###############################################################################
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="$SCRIPT_DIR/../templates"

usage() {
  cat >&2 <<'EOF'
usage:
  hire.sh <id> --tier worker|manager --role "<title>"
          [--manager <id>] [--people-lead <id>] [--model <alias|claude-*>]
          [--company DIR]
  hire.sh --fire <id> [--company DIR]
EOF
  exit 2
}

err() {
  echo "hire.sh: error: $*" >&2
  exit 1
}

command -v python3 >/dev/null 2>&1 || err "python3 not found"
PY="python3"

# --- ID charset — enforced HERE (defense in depth alongside employee.py's
# discover(), Item 1: a hand-crafted directory that skipped this check would
# still be ignored by discover and flagged by schedule_validator's R7). ------
ID_RE='^[a-z][a-z0-9-]{1,23}$'

COMPANY=".company"
MODE="hire"
ID=""
TIER=""
ROLE=""
MANAGER=""
PEOPLE_LEAD=""
MODEL=""

[[ $# -ge 1 ]] || usage

if [[ "$1" == "--fire" ]]; then
  MODE="fire"
  shift
  [[ $# -ge 1 ]] || err "--fire requires an employee id"
  ID="$1"; shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --company) [[ $# -ge 2 ]] || err "--company requires a value"; COMPANY="$2"; shift 2 ;;
      -h|--help) usage ;;
      *) err "unknown argument: $1" ;;
    esac
  done
else
  case "$1" in
    -h|--help) usage ;;
    -*) err "expected an employee id as the first argument, got '$1'" ;;
  esac
  ID="$1"; shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --tier)        [[ $# -ge 2 ]] || err "--tier requires a value"; TIER="$2"; shift 2 ;;
      --role)        [[ $# -ge 2 ]] || err "--role requires a value"; ROLE="$2"; shift 2 ;;
      --manager)     [[ $# -ge 2 ]] || err "--manager requires a value"; MANAGER="$2"; shift 2 ;;
      --people-lead) [[ $# -ge 2 ]] || err "--people-lead requires a value"; PEOPLE_LEAD="$2"; shift 2 ;;
      --model)       [[ $# -ge 2 ]] || err "--model requires a value"; MODEL="$2"; shift 2 ;;
      --company)     [[ $# -ge 2 ]] || err "--company requires a value"; COMPANY="$2"; shift 2 ;;
      -h|--help) usage ;;
      *) err "unknown argument: $1" ;;
    esac
  done
fi

[[ -n "$ID" ]] || err "employee id is required"

# --- opt-in: this must already be a real self-company store (never created
# implicitly by hire.sh — run init_company.sh first). ------------------------
[[ -d "$COMPANY/org/employees" ]] || \
  err "'$COMPANY' is not a self-company store (no org/employees/ under it) — pass --company DIR, or run init_company.sh first"

# ONE source of truth for the core roster + the discovered roster —
# employee.py, never a second hardcoded list here. User-controlled values
# (COMPANY, below MODEL) are ALWAYS passed via environment variables, never
# spliced into the python -c source text — a value containing a quote/
# backslash must never be able to break out of a string literal into
# executable code (the same argv-smuggle-proof discipline employee.py's
# resolved_model itself enforces at the --model argv boundary).
export SC_HIRE_SCRIPT_DIR="$SCRIPT_DIR"
export SC_HIRE_COMPANY="$COMPANY"

CORE_IDS="$("$PY" -c "
import os, sys
sys.path.insert(0, os.environ['SC_HIRE_SCRIPT_DIR'])
from employee import CORE_EMPLOYEES
print(' '.join(CORE_EMPLOYEES))
")" || err "could not read the core employee roster (employee.py import failed)"

DISCOVERED_IDS="$("$PY" -c "
import os, sys
sys.path.insert(0, os.environ['SC_HIRE_SCRIPT_DIR'])
from employee import discover
print(' '.join(discover(os.environ['SC_HIRE_COMPANY'])))
")" || err "could not discover the current employee roster (employee.py import failed)"

_is_core() {
  local id="$1" c
  for c in $CORE_IDS; do [[ "$id" == "$c" ]] && return 0; done
  return 1
}

_is_known() {
  local id="$1" c
  for c in $DISCOVERED_IDS; do [[ "$id" == "$c" ]] && return 0; done
  return 1
}

# ============================================================== --fire ======
if [[ "$MODE" == "fire" ]]; then
  [[ "$ID" =~ $ID_RE ]] || err "invalid employee id '$ID' — must match $ID_RE"
  _is_core "$ID" && err "refuse to fire a charter/core employee '$ID' — the four charter singletons (Elon/Phoebe/July/Gibby) plus Tony/Bob/Mike/Tom stay code-pinned"

  DESK="$COMPANY/org/employees/$ID"
  if [[ ! -d "$DESK" ]]; then
    echo "hire.sh: '$ID' has no desk under $COMPANY/org/employees/ — nothing to fire (idempotent)"
    exit 0
  fi

  TOMB_DIR="$COMPANY/org/employees/.fired"
  mkdir -p "$TOMB_DIR"
  STAMP="$(date +%Y-%m-%d)"
  DEST="$TOMB_DIR/${ID}-${STAMP}"
  N=2
  while [[ -e "$DEST" ]]; do
    DEST="$TOMB_DIR/${ID}-${STAMP}-${N}"
    N=$((N + 1))
  done

  mv "$DESK" "$DEST"
  echo "hire.sh: fired '$ID' — desk tombstoned to $DEST (never deleted; memory store, if any, moved with it untouched)"
  exit 0
fi

# ============================================================== hire ========
[[ "$ID" =~ $ID_RE ]] || err "invalid employee id '$ID' — must match $ID_RE"
_is_core "$ID" && err "'$ID' is a charter/core employee id — cannot re-hire"
[[ -d "$COMPANY/org/employees/$ID" ]] && err "employee '$ID' already exists"

case "$TIER" in
  worker|manager) ;;
  "") err "--tier worker|manager is required" ;;
  *)  err "invalid --tier '$TIER' — must be worker or manager" ;;
esac

[[ -n "$ROLE" ]] || err '--role "<title>" is required'

if [[ -z "$MANAGER" ]]; then
  if [[ "$TIER" == "worker" ]]; then MANAGER="phoebe"; else MANAGER="elon"; fi
fi
[[ -z "$PEOPLE_LEAD" ]] && PEOPLE_LEAD="july"

_is_known "$MANAGER" || err "unknown --manager reference '$MANAGER' — not an existing employee"
_is_known "$PEOPLE_LEAD" || err "unknown --people-lead reference '$PEOPLE_LEAD' — not an existing employee"

if [[ -n "$MODEL" ]]; then
  # Reuse employee.py's OWN resolved_model — the single source of truth for
  # the alias/claude-* contract (Phase 29) — never a second alias table here.
  # At hire time this is a REFUSAL (exit nonzero on a warning), not the
  # dispatch-time degrade-and-warn. MODEL is attacker-shaped input (that is
  # the whole point of this check) — it travels via an environment variable,
  # NEVER spliced into the python source text, so a quote/backslash/`$()` in
  # it can't break out of a string literal into executable code.
  export SC_HIRE_MODEL="$MODEL"
  if ! "$PY" -c "
import os, sys
sys.path.insert(0, os.environ['SC_HIRE_SCRIPT_DIR'])
from employee import Employee
e = Employee('x', '/nonexistent', fm={'model': os.environ['SC_HIRE_MODEL']})
_, warning = e.resolved_model('placeholder-default')
sys.exit(1 if warning else 0)
" >/dev/null 2>&1; then
    err "invalid --model '$MODEL' — not a recognized alias (haiku/sonnet/opus/fable) or a valid claude-* id"
  fi
fi

TEMPLATE_CTX="$TEMPLATES_DIR/new-${TIER}-context.md"
TEMPLATE_PERSONA="$TEMPLATES_DIR/new-${TIER}-persona.md"
[[ -f "$TEMPLATE_CTX" ]] || err "missing template: $TEMPLATE_CTX"
[[ -f "$TEMPLATE_PERSONA" ]] || err "missing template: $TEMPLATE_PERSONA"

# Title-case the id ("sam-jr" -> "Sam Jr") for the default display name —
# bash parameter expansion only (no GNU-sed \U dependency).
_titlecase() {
  local out="" part
  IFS='-' read -ra parts <<< "$1"
  for part in "${parts[@]}"; do
    out="$out${part^} "
  done
  printf '%s' "${out% }"
}
DISPLAY_NAME="$(_titlecase "$ID")"
DATE="$(date +%Y-%m-%d)"

# Escape backslash, ampersand, and the sed delimiter '|' in every value that
# gets spliced into a `sed s|TOKEN|VALUE|g` replacement below — a role title
# or id containing any of those must never corrupt the substitution.
_esc() { printf '%s' "$1" | sed -e 's/[\&|]/\\&/g'; }

E_ID="$(_esc "$ID")"
E_DISPLAY="$(_esc "$DISPLAY_NAME")"
E_ROLE="$(_esc "$ROLE")"
E_MANAGER="$(_esc "$MANAGER")"
E_PEOPLE_LEAD="$(_esc "$PEOPLE_LEAD")"
E_MODEL="$(_esc "$MODEL")"
E_DATE="$(_esc "$DATE")"

_render() {
  sed -e "s|@@ID@@|$E_ID|g" \
      -e "s|@@DISPLAY_NAME@@|$E_DISPLAY|g" \
      -e "s|@@ROLE@@|$E_ROLE|g" \
      -e "s|@@MANAGER@@|$E_MANAGER|g" \
      -e "s|@@PEOPLE_LEAD@@|$E_PEOPLE_LEAD|g" \
      -e "s|@@MODEL@@|$E_MODEL|g" \
      -e "s|@@DATE@@|$E_DATE|g" \
      "$1"
}

DESK="$COMPANY/org/employees/$ID"
mkdir -p "$DESK"
_render "$TEMPLATE_CTX" > "$DESK/context.md"
_render "$TEMPLATE_PERSONA" > "$DESK/persona.md"
: > "$DESK/log.md"
: > "$DESK/scratchpad.md"

# --- atomic: a validator failure removes the just-scaffolded desk again ----
VALIDATOR="$SCRIPT_DIR/schedule_validator.py"
if [[ -f "$VALIDATOR" ]]; then
  if ! OUT="$("$PY" "$VALIDATOR" --company "$COMPANY" 2>&1)"; then
    rm -rf "$DESK"
    err "scaffolded desk failed validation — removed (hire is atomic):
$OUT"
  fi
fi

echo "hire.sh: hired '$ID' ($TIER) — desk at $DESK"
echo "  role: $ROLE"
echo "  manager: $MANAGER   people_lead: $PEOPLE_LEAD"
exit 0
