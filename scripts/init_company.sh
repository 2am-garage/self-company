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

# Copy the deterministic Python scripts into .company/scripts/ so they travel
# with the company folder and stay runnable after the skill source is edited.
# Shared by full init and --sync-scripts so the two can never drift.
copy_scripts() {
  mkdir -p "${TARGET_DIR}/scripts"

  # Core maintenance scripts + the shared policy_config module they import; fail if missing.
  if cp "${SCRIPT_DIR}/decay.py" "${SCRIPT_DIR}/entropy.py" "${SCRIPT_DIR}/policy_config.py" "${TARGET_DIR}/scripts/"; then
    chmod +x "${TARGET_DIR}/scripts/decay.py" "${TARGET_DIR}/scripts/entropy.py" 2>/dev/null || true
    log_success "Copied decay.py / entropy.py / policy_config.py into ${TARGET_DIR}/scripts/"
  else
    log_error "Failed to copy decay.py / entropy.py / policy_config.py into ${TARGET_DIR}/scripts/"
    exit 1
  fi

  # Catch-up notifier (Option B): summarises unattended daily runs since last seen.
  if [[ -f "${SCRIPT_DIR}/notify-status.py" ]]; then
    cp "${SCRIPT_DIR}/notify-status.py" "${TARGET_DIR}/scripts/" \
      && chmod +x "${TARGET_DIR}/scripts/notify-status.py" 2>/dev/null || true
    log_success "Copied notify-status.py into ${TARGET_DIR}/scripts/"
  fi

  # CAPTURE hook entrypoint if present (used by the Stop-hook memory capture).
  if [[ -f "${SCRIPT_DIR}/capture-trigger.py" ]]; then
    cp "${SCRIPT_DIR}/capture-trigger.py" "${TARGET_DIR}/scripts/" \
      && chmod +x "${TARGET_DIR}/scripts/capture-trigger.py" 2>/dev/null || true
    log_success "Copied capture-trigger.py into ${TARGET_DIR}/scripts/"
  fi

  # Daily-run + scheduler + hook installer + RAG setup (Tom's automation) if present.
  for s in daily-run.sh schedule.sh install-hook.sh rag_setup.sh; do
    if [[ -f "${SCRIPT_DIR}/${s}" ]]; then
      cp "${SCRIPT_DIR}/${s}" "${TARGET_DIR}/scripts/" \
        && chmod +x "${TARGET_DIR}/scripts/${s}" 2>/dev/null || true
      log_success "Copied ${s} into ${TARGET_DIR}/scripts/"
    fi
  done

  # RAG scripts (index/query + shared fastembed backend) if available.
  if [[ -f "${SCRIPT_DIR}/rag_index.py" ]] && [[ -f "${SCRIPT_DIR}/rag_query.py" ]]; then
    if cp "${SCRIPT_DIR}/rag_index.py" "${SCRIPT_DIR}/rag_query.py" "${SCRIPT_DIR}/rag_embed.py" "${TARGET_DIR}/scripts/"; then
      chmod +x "${TARGET_DIR}/scripts/rag_index.py" "${TARGET_DIR}/scripts/rag_query.py" 2>/dev/null || true
      log_success "Copied rag_index.py / rag_query.py / rag_embed.py into ${TARGET_DIR}/scripts/"
    else
      log_error "Failed to copy rag_index.py / rag_query.py into ${TARGET_DIR}/scripts/"
      exit 1
    fi
  else
    log_info "RAG scripts (rag_index.py, rag_query.py) not yet built — skipping"
  fi
}

# Re-copy ONLY the scripts into an existing .company/ — a hot-sync to run after
# editing the skill's scripts/*.py. Leaves memory/org/ops/reports untouched.
sync_scripts() {
  if [[ ! -d "${TARGET_DIR}" ]]; then
    log_error "'${TARGET_DIR}' not found — run init first (without --sync-scripts)."
    exit 1
  fi
  log_info "Syncing scripts into existing ${TARGET_DIR}/scripts/ (memory/org untouched)"
  copy_scripts
  log_success "Scripts synced. Memory and config were not modified."
  exit 0
}

################################################################################
# Main
################################################################################

main() {
  # --sync-scripts: only refresh .company/scripts/ from the skill source; -h help.
  for arg in "$@"; do
    case "$arg" in
      --sync-scripts) sync_scripts ;;
      -h|--help)
        echo "Usage: init_company.sh [--sync-scripts]"
        echo "  (no args)        initialize ./.company/ from the template"
        echo "  --sync-scripts   re-copy scripts/*.py into an existing ./.company/scripts/"
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

  # Copy the deterministic Python scripts into .company/scripts/ so they travel
  # with the company folder. The pipeline/triggers docs reference
  # `.company/scripts/decay.py`, `.company/scripts/entropy.py`, `.company/scripts/rag_index.py`,
  # and `.company/scripts/rag_query.py` — they must actually exist there after install.
  copy_scripts

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
