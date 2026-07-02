#!/usr/bin/env bash

################################################################################
# init_company.sh
#
# Initialize a self-company skeleton in the current repository.
#
# Usage:
#   ./init_company.sh
#
# Description:
#   Copies the company template from assets/company-template/ to ./.company/
#   in the current working directory (where the script is invoked).
#
#   - .company/ is HIDDEN and kept PRIVATE: the script ensures the repo's
#     .gitignore excludes it, so company memory is never committed/pushed.
#   - If ./.company/ already exists, aborts with warning.
#   - Template location is resolved relative to this script's directory.
#   - Creates full directory structure and seeded config/log files.
#   - Fails fast on errors (set -euo pipefail).
#
# Output:
#   Prints the initialized structure on success.
#
################################################################################

set -euo pipefail

# Color codes for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m' # No Color

# Script location
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TEMPLATE_DIR="${SCRIPT_DIR}/../assets/company-template"

# Target location (current working directory) — hidden + private
readonly TARGET_DIR="./.company"

################################################################################
# Helper Functions
################################################################################

log_error() {
  echo -e "${RED}[ERROR]${NC} $*" >&2
}

log_warn() {
  echo -e "${YELLOW}[WARN]${NC} $*" >&2
}

log_info() {
  echo -e "${GREEN}[INFO]${NC} $*"
}

log_success() {
  echo -e "${GREEN}[SUCCESS]${NC} $*"
}

# Ensure the repo's .gitignore excludes the company folder (idempotent).
ensure_gitignore() {
  local pattern=".company/"
  local gitignore=".gitignore"

  # Already ignored? (covers existing patterns like .company or .company/)
  if [[ -f "${gitignore}" ]] && grep -qxE '\.company/?' "${gitignore}"; then
    log_info ".gitignore already excludes ${pattern}"
    return 0
  fi

  {
    [[ -f "${gitignore}" ]] && echo ""
    echo "# self-company private memory — never commit (Chairman privacy)"
    echo "${pattern}"
  } >> "${gitignore}"
  log_success "Added '${pattern}' to ${gitignore} — company memory stays private"
}

# Phase 1b (code/data separation): scripts are NO LONGER copied into
# .company/scripts/. The runtime self-resolves the CANONICAL scripts from the
# skill/plugin, so a skill update takes effect immediately (no P3 drift). This is
# now a DEPRECATION NO-OP kept so old muscle-memory / docs referencing
# `--sync-scripts` don't error.
sync_scripts() {
  log_info "scripts now run from the skill; nothing to sync"
  log_info "(code/data separation: .company/ is data only — the runtime resolves scripts from the skill/plugin)"
  exit 0
}

################################################################################
# Main
################################################################################

main() {
  # --sync-scripts: deprecated no-op (scripts now run from the skill, not copied); -h help.
  for arg in "$@"; do
    case "$arg" in
      --sync-scripts) sync_scripts ;;
      -h|--help)
        echo "Usage: init_company.sh [--sync-scripts]"
        echo "  (no args)        initialize ./.company/ (data only) from the template"
        echo "  --sync-scripts   DEPRECATED no-op — scripts run from the skill, nothing to sync"
        exit 0 ;;
    esac
  done

  # Check template exists
  if [[ ! -d "${TEMPLATE_DIR}" ]]; then
    log_error "Template directory not found: ${TEMPLATE_DIR}"
    exit 1
  fi

  # Check if company directory already exists AND holds anything. A non-empty
  # .company/ is a real install — never clobber it. An empty .company/ (e.g. a
  # stale placeholder directory) is safe to initialize in place.
  if [[ -d "${TARGET_DIR}" ]]; then
    if [[ -n "$(ls -A "${TARGET_DIR}" 2>/dev/null)" ]]; then
      log_warn "Directory '${TARGET_DIR}' already exists and is not empty."
      log_warn "Skipping initialization to avoid overwriting existing configuration."
      log_warn "Remove '${TARGET_DIR}' manually if you want to reinitialize."
      exit 0
    fi
    log_info "Found an empty '${TARGET_DIR}' — initializing in place."
    rmdir "${TARGET_DIR}"
  fi

  log_info "Initializing self-company in: $(pwd)"
  log_info "Copying template from: ${TEMPLATE_DIR}"

  # Copy entire template tree
  if cp -r "${TEMPLATE_DIR}" "${TARGET_DIR}"; then
    log_success "Template copied successfully"
  else
    log_error "Failed to copy template directory"
    exit 1
  fi

  # Code/data separation (Phase 1b): scripts are NO LONGER copied into
  # .company/scripts/. The runtime (daily-run.sh, schedule.sh, hooks, company-run.sh)
  # self-resolves and runs the CANONICAL scripts from the skill/plugin. .company/ is
  # DATA only (memory/org/ops). This kills the P3 drift where a stale copy shadowed
  # the updated skill script.

  # Privacy: ensure git never tracks the company folder.
  # Company memory is personal — it must not be committed or pushed.
  ensure_gitignore

  # Print resulting structure
  echo
  log_info "Company structure initialized:"
  echo
  tree -L 3 "${TARGET_DIR}" 2>/dev/null || find "${TARGET_DIR}" -type d | sort | sed 's|[^/]*/| |g'

  echo
  log_success "Done! Self-company initialized in ${TARGET_DIR}/"
  echo
  echo "Next steps:"
  echo "  1. Read the company charter: .company/org/policy.md"
  echo "  2. Review the trigger matrix: .company/org/triggers.md"
  echo "  3. Talk to Elon — no prefix = default to CEO, he'll set direction or dispatch tasks"
  echo "  4. Name a worker: (Tom) I need you... or (Bob) this file..."
  echo
  echo "  🔒 .company/ added to .gitignore — company memory is private, won't be git pushed."
  echo
}

main "$@"
