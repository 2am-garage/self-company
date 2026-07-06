#!/usr/bin/env bash
###############################################################################
# hook_guard.sh — the ONE shared opt-in guard for every self-company plugin hook.
#
# Plugin hooks fire GLOBALLY — in EVERY repo the Chairman opens, company or not.
# So each hook script's FIRST action must be to no-op unless THIS project
# actually has a `.company/` store. Factoring that check into one sourceable
# helper is what keeps the 7 hooks inert off-company AND stops the guard from
# drifting per-hook (spec Phase 10, Item 2).
#
# Usage (source it, then call sc_hook_optin as the hook's first real line):
#
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   . "$SCRIPT_DIR/hook_guard.sh"
#   sc_hook_optin        # silent `exit 0` if no .company here; else continues
#
# On success it also sets $SC_COMPANY to the resolved `.company` dir so the
# caller doesn't re-derive it. Deliberately tiny — no logic beyond the marker
# check. Python hooks (B1/B2) do the equivalent inline check; this is the bash
# side of the same contract.
###############################################################################

# sc_hook_optin: no-op-and-exit-0 unless $CLAUDE_PROJECT_DIR (or $PWD) has a
# .company marker. Mirrors the spec's one-liner contract exactly.
sc_hook_optin() {
  local base="${CLAUDE_PROJECT_DIR:-$PWD}"
  [ -d "$base/.company" ] || exit 0
  SC_COMPANY="$base/.company"
}
