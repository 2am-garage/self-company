#!/usr/bin/env bash
###############################################################################
# skeleton_guard.sh — may the skill modify its OWN skeleton here?
#
# Exit 0 (allowed) when this is the skill's development repo (a `.self-company-dev`
# marker at the working-tree root) OR the Chairman explicitly overrides
# (SELF_COMPANY_ALLOW_SKELETON=1). Exit 1 (locked) otherwise — in usage mode the
# company operates only within .company/ and never edits SKILL.md/scripts/personas.
#
# Agents/scripts MUST consult this before editing any skill-source file. See
# SKILL.md "Governance: Skeleton Immutability".
#
# Usage: skeleton_guard.sh [PROJECT_DIR]   (default: $PWD)
###############################################################################
set -uo pipefail

ROOT="${1:-${SELF_COMPANY_PROJECT_DIR:-$PWD}}"

if [[ "${SELF_COMPANY_ALLOW_SKELETON:-}" == "1" ]]; then
  echo "allowed: Chairman override (SELF_COMPANY_ALLOW_SKELETON=1)"
  exit 0
fi
if [[ -f "$ROOT/.self-company-dev" ]]; then
  echo "allowed: development repo (.self-company-dev present)"
  exit 0
fi
echo "locked: usage mode — the skill skeleton is immutable here. Operate only" >&2
echo "within .company/. To change skill source, run in the dev repo (.self-company-dev)" >&2
echo "or set SELF_COMPANY_ALLOW_SKELETON=1 on explicit Chairman order." >&2
exit 1
