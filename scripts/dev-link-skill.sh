#!/usr/bin/env bash
###############################################################################
# dev-link-skill.sh — DEV-ONLY: make this repo load itself as a Claude Code skill.
#
# Claude Code discovers skills under `.claude/skills/<name>/`. The self-company
# skill's source lives at the REPO ROOT (SKILL.md, scripts/, references/, assets/,
# design/). So that the skill is *active while you develop it in this very repo*,
# we expose the root under `.claude/skills/self-company/` via relative symlinks.
#
# These symlinks are a LOCAL dev convenience and are NOT committed (`.claude/` is
# git-ignored) — committing symlinks is non-portable (Windows / git archive) and
# unusual for a skill repo, whose deliverable is the root content itself. Run this
# once after cloning the dev repo to (re)create them. Idempotent.
#
# Usage: scripts/dev-link-skill.sh
###############################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ ! -f "$REPO_ROOT/.self-company-dev" ]]; then
  echo "[dev-link-skill] no .self-company-dev marker at $REPO_ROOT — this is not the"
  echo "                 skill development repo; nothing to link." >&2
  exit 0
fi

LINK_DIR="$REPO_ROOT/.claude/skills/self-company"
mkdir -p "$LINK_DIR"

# Each entry under .claude/skills/self-company/ points up three levels to the root.
for name in SKILL.md scripts references assets design; do
  target="$REPO_ROOT/$name"
  link="$LINK_DIR/$name"
  if [[ ! -e "$target" ]]; then
    echo "[dev-link-skill] skip $name (not present at root)"
    continue
  fi
  ln -sfn "../../../$name" "$link"
  echo "[dev-link-skill] linked .claude/skills/self-company/$name -> ../../../$name"
done

echo "[dev-link-skill] done. This repo now loads itself as the 'self-company' skill."
