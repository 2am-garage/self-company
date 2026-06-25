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

################################################################################
# Main
################################################################################

main() {
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

  # Copy the deterministic Python scripts into .company/scripts/ so they travel
  # with the company folder. The pipeline/triggers docs reference
  # `.company/scripts/decay.py`, `.company/scripts/entropy.py`, `.company/scripts/rag_index.py`,
  # and `.company/scripts/rag_query.py` — they must actually exist there after install.
  mkdir -p "${TARGET_DIR}/scripts"

  # Copy core maintenance scripts (decay, entropy) plus the shared policy_config
  # module they import for reading tunable constants from policy.md; fail if missing.
  if cp "${SCRIPT_DIR}/decay.py" "${SCRIPT_DIR}/entropy.py" "${SCRIPT_DIR}/policy_config.py" "${TARGET_DIR}/scripts/"; then
    chmod +x "${TARGET_DIR}/scripts/decay.py" "${TARGET_DIR}/scripts/entropy.py" 2>/dev/null || true
    log_success "Copied decay.py / entropy.py / policy_config.py into ${TARGET_DIR}/scripts/"
  else
    log_error "Failed to copy decay.py / entropy.py / policy_config.py into ${TARGET_DIR}/scripts/"
    exit 1
  fi

  # Copy RAG scripts (rag_index.py, rag_query.py) if available; skip if not yet built.
  if [[ -f "${SCRIPT_DIR}/rag_index.py" ]] && [[ -f "${SCRIPT_DIR}/rag_query.py" ]]; then
    if cp "${SCRIPT_DIR}/rag_index.py" "${SCRIPT_DIR}/rag_query.py" "${TARGET_DIR}/scripts/"; then
      chmod +x "${TARGET_DIR}/scripts/rag_index.py" "${TARGET_DIR}/scripts/rag_query.py" 2>/dev/null || true
      log_success "Copied rag_index.py / rag_query.py into ${TARGET_DIR}/scripts/"
    else
      log_error "Failed to copy rag_index.py / rag_query.py into ${TARGET_DIR}/scripts/"
      exit 1
    fi
  else
    log_info "RAG scripts (rag_index.py, rag_query.py) not yet built — skipping (will be available once RAG is fully deployed)"
  fi

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
