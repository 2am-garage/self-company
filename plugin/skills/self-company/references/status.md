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
- ✅ **Phase 13 Stage A — RAG index wired into the daily core** — `daily-run.sh` refreshes the LanceDB index incrementally each run (after reinforce+decay+verify+entropy, L1/L2 only, Tony-owned, idempotent via content_hash), and surfaces a deps-free "activate RAG" candidate when active L1+L2 ≥ 50 while the venv is uninstalled. Absent/broken venv → one logged skip line; the core always completes. Dead Ollama vestiges removed from `rag_index.py`; `rag.md` rewritten to the wired reality. (Stage B — semantic ask-time injection — shipped in v0.1.5, see below.)

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

### v3.x Trustworthy Memory → Multi-Company → R&D Completed (Phases 5-9, 11)

- ✅ **Phase 5 — Trustworthy memory & durability floor** — Mike (R&D Researcher) added to the skeleton and Bob retitled **Build Engineer**, so the org is now **8 employees**; distinct-session reinforce semantics; **soft-delete tombstones** (`invalid_at` + grace window) instead of an unsupervised `rm`; **offline-gap damper** so a long unattended gap doesn't over-decay the corpus on the first tick back; pre-mutation `memory/` snapshots to `ops/backups/` (`BACKUP_KEEP`, policy §7.8) as the durability floor before any `--apply` (`decay.py`, `daily-run.sh`).
- ✅ **Phase 6 — Tombstone convergence** — `archived`/`defunct`/`absorbed` unified in the single-source `scripts/tombstone.py`, imported by every scanner; the agent **tombstones** (`status: absorbed` + `invalid_at`) and the deterministic decay reap physically removes past grace — no destructive permission granted to an unsupervised agent. See `references/memory-tiers.md` §9.
- ✅ **Phase 7 — Multi-company scheduling** — `schedule.sh` manages the crontab as a **namespaced keyed set** (`sha1(PROJECT_DIR)[:12]` key), auto-staggers each project's minute, migrates legacy un-namespaced lines, and adds `list` / `prune` / scoped `uninstall`, so many companies coexist in one crontab. See `references/operations.md`.
- ✅ **Phase 8 — Holding company (fleet)** — `schedule.sh install-fleet <parent>` installs one parent cron running `fleet-run.sh` over `<parent>/.company/org/subsidiaries.md` on a shared **`FLEET_AGENT_BUDGET`** (policy §7.9); the parent orchestrates SCHEDULING + BUDGET only and never reads/writes a sub's `.company/` except by invoking that sub's own `daily-run.sh`.
- ✅ **Phase 9 — Mike online (R&D split)** — the weekly `research-scan.sh` runs headless **as Mike**, surveying the external world (literature / ecosystem / comparable harnesses) and writing **cited, applicability-ranked briefs** to `ops/research/` (plus improvement proposals to `ops/plans/`). Division of labor: **Tony measures inside, Mike surveys outside**.
- ✅ **Phase 11 — Frontmatter consolidation** — the fragile markdown-frontmatter parse/serialize/tokenize seam, previously open-coded across ten scanners (with a real `entropy.py` divergence bug), is now single-sourced in `scripts/frontmatter.py`, imported the same best-effort + verbatim-fallback way as `tombstone.py` / `charter_ids.py`.

### v0.1.5 Connect RAG + Skill Lightening Completed (Phases 13-14)

- ✅ **Phase 13 — Connect RAG across the tiers** — the built-but-unwired LanceDB index is now live (Chairman: connect RAG, don't delete it). **Stage A** wires an incremental index refresh into `daily-run.sh` (fresh L1/L2, idempotent, never fails the core). **Stage B** is the killer app: `hook_memory_inject.py` now selects ask-time memories by MEANING, not just keyword overlap — it queries `rag_query.py` (project venv, ~7s timeout) and injects the semantically-relevant memories, re-validated against the live candidate set. Semantic-first with the keyword path as the guaranteed-fast floor and the no-venv/timeout degrade (byte-for-byte identical when RAG is unavailable); never injects a stale/tombstoned/out-of-scope memory; the 30s hook budget is never approached. (B.2 semantic dedup-at-capture deferred — the daily reinforce batch already absorbs transient dups, and a false-positive dedup on a write path risks silent loss.)
- ✅ **Phase 14 — Skill lightening (same function, less weight)** — a 4-employee audit drove a set of behavior-neutral cuts: deleted the orphan `ENTROPY_USAGE.md` and `decay.py`'s unused `_parse_errors`; removed the dead `install-hook.sh install` no-op (kept `uninstall`/`status`); **collapsed ~10 scripts' verbatim import-fallbacks to hard imports (−377 lines)**, killing a drift risk while proven byte-identical; slimmed `SKILL.md` 324→274 (pointer-ize Operations, de-dup the reference catalog) for a per-session token win; marked the holding-company fleet layer explicitly optional (doc-only). Persona boilerplate single-sourcing deferred (isolated workers load only their persona). Bucket 0 (ship-exclude tests) needs a `source: "./plugin"` repo restructure — split out to its own Phase 15.

### v0.1.4 Configurable Schedule Completed (Phase 12)

- ✅ **Phase 12 — Configurable per-employee schedule & duties (invariant-guarded)** — per-company `org/schedule.yaml` makes each employee's tick, `cadence`, `duties`, `budget`, and `enabled` adjustable as DATA (**Layer A**); a missing file reproduces today's defaults byte-for-byte. The friendly cadence grammar (`every Nh` / `hourly` / `weekdays-9-17` / raw cron) and per-employee sub-cadences are translated + duty-gated at runtime by `scripts/schedule_config.py` (one tick, fail-open gating). **Layer B** — who is attacker vs builder, the sign-off gate, dispatch topology — stays in code and is machine-enforced: `scripts/schedule_validator.py` (rules **R1–R6**) REFUSES any config that would break the red/blue competition and falls back to defaults. A `SessionStart` guard (`scripts/hook_schedule_guard.sh`) syncs a tick change into the crontab; `ops/schedule/roster.md` is generated, no longer hand-maintained. Cron exprs are charset+semantic-validated so a malformed/injection-shaped cadence never reaches the crontab. See `references/operations.md` + `references/red-blue-protocol.md`.

### v0.1.2 Plugin-native Hooks Completed (Phase 10)

- ✅ Hooks are plugin-native — all **7 hooks** are declared once in `hooks/hooks.json` at the plugin root and run via `${CLAUDE_PLUGIN_ROOT}`, so Claude Code loads them on install with no per-repo `install-hook.sh` edit: `Stop` (CAPTURE), `SessionStart` (catch-up push), `UserPromptSubmit` (ask-time memory injection), `PreCompact` (capture-rescue), `PreToolUse` (deny `rm` under `.company/memory`), `PostToolUse` (lint memory writes), `SessionEnd` (verify fresh captures). Each script's first action is a `.company` opt-in guard (silent `exit 0` in non-company repos).
- ✅ `install-hook.sh` is a legacy-cleaner only — nothing to install (hooks are plugin-native; the old `install` no-op command was removed in Phase 14); `uninstall` cleans legacy `settings.json` entries that would otherwise double-fire against the plugin hooks; `status` reports plugin-native. See `references/operations.md`.

### Still Open / Deferred

- ⏳ Code/Chat entropy — code drift detection, session distillation
- ⏳ NLI/cross-encoder second signal for the cosine [0.74, 0.81] overlap band
- ⏳ Real token accounting (policy §3 currently documented as a runs/day proxy)
- ⏳ Phase 15 — ship-exclude `tests/`+`evals/` from the installed bundle via a `source: "./plugin"` repo restructure (no plugin-native exclude exists; own carefully-verified phase)
- ⏳ RAG B.2 — semantic dedup-at-capture (deferred: daily reinforce already absorbs transient dups; false-positive on a write path risks silent loss)
- ⏳ Persona boilerplate single-sourcing (deferred: isolated workers load only their persona)
- ⏳ July (HR) periodic worker-performance review — Chairman decision pending
