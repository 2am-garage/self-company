#!/usr/bin/env bash
###############################################################################
# hook_org_lint.sh — PostToolUse org-lint hook (Phase 32, Item 5).
#
# On an Edit/Write under org/employees/**, run schedule_validator.py's R7
# checks (hire-as-data invariants — Phase 32 Item 3) against the touched
# desk and WARN, NEVER BLOCK. Mirrors hook_memory_lint.py's shape (PostToolUse,
# opt-in-guarded, reads stdin JSON for tool_input.file_path) but a "warn" hook
# never emits the PostToolUse block decision — it prints a plain diagnostic to
# stderr (visible, non-blocking) and always exits 0. This is the ACTUAL Claude
# Code hook; hire.sh (Item 2) is the plain command that scaffolds the desk in
# the first place and already blocks/rolls back synchronously at hire time —
# this hook is a defense-in-depth backstop for a desk hand-edited directly.
#
# CONTRACT (PostToolUse): reads stdin JSON {tool_name, tool_input:{file_path}}.
# WARN: print diagnostic lines to stderr, exit 0. Nothing to warn about /
# not an org/employees/ file / a CORE employee's own desk (R1-R6 already
# govern those, unchanged): silent exit 0.
#
# FAIL-OPEN: opt-in guard first (no .company -> exit 0); ANY error -> exit 0
# with no warning. A hook bug must never block legitimate work.
###############################################################################
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hook_guard.sh
. "$SCRIPT_DIR/hook_guard.sh"
sc_hook_optin        # no .company here -> silent exit 0 (plugin fires globally)

INPUT="$(cat)"

command -v python3 >/dev/null 2>&1 || exit 0
VALIDATOR="$SCRIPT_DIR/schedule_validator.py"
[ -f "$VALIDATOR" ] || exit 0

export SC_COMPANY
export SC_HOOK_INPUT="$INPUT"
export SC_VALIDATOR="$VALIDATOR"
export SC_SCRIPT_DIR="$SCRIPT_DIR"

read -r -d '' PYCODE <<'PY' || true
import json
import os
import re
import subprocess
import sys

company = os.environ.get("SC_COMPANY", "")
raw = os.environ.get("SC_HOOK_INPUT", "")

try:
    data = json.loads(raw)
except Exception:
    sys.exit(0)                    # unparseable stdin -> fail open, silent

if not isinstance(data, dict):
    sys.exit(0)
tool_input = data.get("tool_input") or {}
if not isinstance(tool_input, dict):
    sys.exit(0)
file_path = tool_input.get("file_path") or ""
if not isinstance(file_path, str) or not file_path:
    sys.exit(0)

norm = file_path.replace("\\", "/")
m = re.search(r"org/employees/([^/]+)/", norm)
if not m:
    sys.exit(0)                    # not a desk file -> nothing to lint
touched_id = m.group(1)

sys.path.insert(0, os.environ.get("SC_SCRIPT_DIR", ""))
try:
    import employee as emp
except Exception:
    sys.exit(0)

if touched_id in emp.CORE_EMPLOYEES:
    sys.exit(0)                    # core desks: R1-R6 govern them, unchanged

try:
    proc = subprocess.run(
        [sys.executable, os.environ.get("SC_VALIDATOR", ""), "--company", company],
        capture_output=True, text=True, timeout=20,
    )
except Exception:
    sys.exit(0)                    # never block on a validator hiccup

if proc.returncode == 0:
    sys.exit(0)                    # valid -> silent pass

lines = [ln for ln in proc.stdout.splitlines()
         if ln.startswith("R7:") and f"'{touched_id}'" in ln]
if not lines:
    sys.exit(0)                    # this desk isn't the violating one -> silent

print(f"[org-lint] WARNING — org/employees/{touched_id}/ has hire-as-data "
      f"invariant issues (Phase 32 R7), never blocking:", file=sys.stderr)
for ln in lines:
    print(f"[org-lint]   {ln}", file=sys.stderr)
sys.exit(0)
PY

python3 -c "$PYCODE"
exit 0
