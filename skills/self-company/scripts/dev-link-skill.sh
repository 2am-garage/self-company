#!/usr/bin/env bash
###############################################################################
# dev-link-skill.sh — DEV-ONLY: make this repo load itself as a Claude Code skill.
#
# Claude Code discovers skills under `.claude/skills/<name>/`. The self-company
# skill's real source now lives at `skills/self-company/` (SKILL.md, scripts/,
# references/, assets/, design/). So that the skill is *active while you develop
# it in this very repo*, we expose that dir under `.claude/skills/self-company`
# via a SINGLE relative symlink.
#
# This symlink is a LOCAL dev convenience and is NOT committed (`.claude/` is
# git-ignored) — committing symlinks is non-portable (Windows / git archive) and
# unusual for a skill repo, whose deliverable is the skills/ content itself. Run
# this once after cloning the dev repo to (re)create it. Idempotent.
#
# Usage: skills/self-company/scripts/dev-link-skill.sh
###############################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# scripts is at REPO/skills/self-company/scripts -> repo root is three levels up.
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

if [[ ! -f "$REPO_ROOT/.self-company-dev" ]]; then
  echo "[dev-link-skill] no .self-company-dev marker at $REPO_ROOT — this is not the"
  echo "                 skill development repo; nothing to link." >&2
  exit 0
fi

SKILLS_DIR="$REPO_ROOT/.claude/skills"
mkdir -p "$SKILLS_DIR"

# ONE symlink: .claude/skills/self-company -> ../../skills/self-company (relative).
# The real skill dir already exists under skills/self-company; dev-link just
# exposes it under .claude/skills/ so this dev repo loads itself as the skill.
link="$SKILLS_DIR/self-company"
rm -rf "$link"                       # clear any prior per-file symlink dir
ln -sfn "../../skills/self-company" "$link"
echo "[dev-link-skill] linked .claude/skills/self-company -> ../../skills/self-company"
echo "[dev-link-skill] done. This repo now loads itself as the 'self-company' skill."
