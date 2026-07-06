# Completion Status

### v1 Completed

- ✅ Org structure (eight agents, responsibility boundaries, relationship diagram)
- ✅ Context engineering spec (each agent's context.md)
- ✅ Addressing protocol + work chain
- ✅ Installation script (idempotent)
- ✅ Language rules + personas

### v2 Memory Pipeline Completed

- ✅ Pipeline execution logic — CAPTURE → ORGANIZE → WRITE → VERIFY (loop until clean) detailed steps in `references/pipeline.md`
- ✅ Decay formula and thresholds — `decay_score = 0.5 ** (age_days / half_life)`, three-tier thresholds, implemented in `scripts/decay.py`
- ✅ Entropy measurement (KPI) — four dimensions (duplication, contradiction, stale, unverified), implemented in `scripts/entropy.py`
- ✅ Memory tiers and promotion — L0/L1/L2 + consolidation rules, see `references/memory-tiers.md`
- ✅ Memory frontmatter schema — nine-column complete definition (id, tier, owner, sources, created, last_reinforced, reinforce_count, decay_score, status)

### v2.5 RAG Deployment Completed

- ✅ RAG infrastructure deployed — offline fastembed backend (`bge-small-en-v1.5`, 384-dim, no daemon) in a project-local `.company/.rag-venv`; scripts re-exec into it and degrade gracefully when it is absent
- ✅ Embedding layer — `scripts/rag_embed.py` (fastembed/ONNX, fully local, no network at query time)
- ✅ Vector store — LanceDB embedded serverless index at `.company/memory/index/`
- ✅ Graceful degradation — stdlib-only fallback everywhere (entropy Jaccard-only pass, reinforce no-op with message)
- ✅ Rebuild/query scripts — `scripts/rag_index.py` and `scripts/rag_query.py` (code/data separation: scripts stay in the skill, no longer copied into `.company/`)
- ✅ Technical reference — see `references/rag.md` for full design and usage

### v3 Daily Loop & Governance Completed (Phases 1-4, 2026-07-03)

- ✅ Scheduling shipped — Stop-hook CAPTURE + 6-hourly cron `daily-run.sh` (`schedule.sh`); auth pre-flight probe with fail-marker escalation and backoff. (The Stop hook is now plugin-native — see the v0.1.2 line below; `install-hook.sh` is no longer the installer.)
- ✅ Report automation — `report.py` + run ledger; daily log parsed into the Chairman report
- ✅ Retire-on-promote + reap — promotion physically moves the file; archived/defunct stubs reaped after the grace window (`decay.py`)
- ✅ Semantic dedup tuned on the live corpus — scored gate `DUP_COSINE` 0.82, review band 0.78-0.82 surfaced but never auto-counted (`entropy.py`)
- ✅ Charter memory class — blessed install seeds excluded from `unverified_rate`; non-blessed self-declared charter flagged as suspicious, never trusted
- ✅ Adjudication ledger — `ops/adjudications.md` distinct verdicts permanently suppress judged pairs from candidates and scores
- ✅ CAPTURE categories + throttle — L2 category tagging (profile/projects/preferences), per-session cooldown, reinforce-not-duplicate output contract
- ✅ Deterministic reinforcement in the daily loop — `reinforce_memory.py --apply` wired before decay so rc bumps feed the same run's promotion pass
- ✅ Scanner parity — entropy treats `defunct` as `archived` on read, matching decay; both scanners agree on the active set
- ✅ Change-management pipeline — spec → dispatch → build ⚔ attack → measure → integrate → closeout, institutionalized in `references/change-management.md`

### v0.1.2 Plugin-native Hooks Completed

- ✅ Hooks are plugin-native — all **7 hooks** are declared once in `hooks/hooks.json` at the plugin root and run via `${CLAUDE_PLUGIN_ROOT}`, so Claude Code loads them on install with no per-repo `install-hook.sh` edit: `Stop` (CAPTURE), `SessionStart` (catch-up push), `UserPromptSubmit` (ask-time memory injection), `PreCompact` (capture-rescue), `PreToolUse` (deny `rm` under `.company/memory`), `PostToolUse` (lint memory writes), `SessionEnd` (verify fresh captures). Each script's first action is a `.company` opt-in guard (silent `exit 0` in non-company repos).
- ✅ `install-hook.sh` deprecated — `install` is a no-op; `uninstall` only cleans legacy `settings.json` entries that would otherwise double-fire against the plugin hooks; `status` reports plugin-native. See `references/operations.md`.

### Still Open / Deferred

- ⏳ Code/Chat entropy — code drift detection, session distillation
- ⏳ NLI/cross-encoder second signal for the cosine [0.74, 0.81] overlap band
- ⏳ Real token accounting (policy §3 currently documented as a runs/day proxy)
- ⏳ RAG index refresh wiring in the daily loop + `rag.md` path cleanup
- ⏳ July (HR) periodic worker-performance review — Chairman decision pending
