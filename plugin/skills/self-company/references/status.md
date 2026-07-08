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

### v0.1.10 Migration-script cleanup

- ✅ **Removed two one-shot migration scripts** — `scripts/backfill_rc.py` (Phase-5 one-time rc recompute) and `scripts/migrate_charter_seeds.py` (Phase-4 one-time charter-seed move to L2-cold/profile/) are dead for new installs and had no runtime caller (no cron/hook/daily-run/init invocation). Deleted along with their tests (`test_backfill.py`; the `TestCharterMigration` cases and the `backfill_rc` import-smoke entry). Stale comment/warning references in `frontmatter.py`, `decay.py`, and `charter_ids.py` swept — no logic changed. The charter-guard still refuses to reap/demote a blessed seed found below L2 (now advises "move it to L2-cold/profile/" without naming a script). SKILL.md gained a one-line per-employee memory note.

### Shared Company Memory at Dispatch (Phase 18c)

- ✅ **Phase 18c — SHARED company-memory READ wired INTO dispatch** — a dispatched worker previously read only its OWN per-employee store; the SHARED memory (about the Chairman) was read back only at ASK time by the `UserPromptSubmit` hook, so an autonomous/cron/trigger-dispatched planner missed the Chairman's standing directives. A new `shared_memory_read` capability (data-driven like `memory: rag|flat` — a default table with **elon on, everyone else off**, plus a `context.md shared_memory_read: on|off` override) gates a shared read at dispatch. `Employee.recall_shared(query)` queries the SHARED index (`memory/index`) through the SAME `rag_query.py` + SAME cosine gate (`SELF_COMPANY_INJECT_RAG_MIN_SCORE`) the hook uses, re-validating hits against the live files (skip tombstoned/deleted). `Employee.dispatch_context(query)` returns the own-store **"Relevant past experience"** block + a SEPARATE shared **"Relevant company memory"** block, deduped (own wins) and sharing one budget; `supervisor.py` (`Member.real_command`) injects it. **Double-injection guard:** a spawned `claude -p` worker also fires the `UserPromptSubmit` hook (confirmed), so for a `shared_memory_read` worker the dispatcher sets `SC_NO_MEMORY_INJECT=1` and `hook_memory_inject` no-ops — the explicit dispatch injection is the single source; non-shared workers are unaffected. Coverage: `supervisor.py` is the only Elon headless-dispatch path (via `company-run.sh`); `daily-run.sh` (Tony), `fire-trigger.sh` (Phoebe), `fleet-run.sh` (per-sub daily-run) and `elon_survey.py` (deterministic) dispatch no `shared_memory_read` employee. Degrades to no block on flag-off / no-venv / empty-index / timeout / zero-hit; never raises, never blocks.

### v0.1.9 Per-Employee Memory Mode Completed (Phase 18b)

- ✅ **Phase 18b — per-employee memory MODE (rag/flat) + recall-at-dispatch** — not every employee needs semantic recall (Chairman): analysts/planners get RAG, executors keep their existing "flat" memory (log.md, and for Gibby the red/blue ledger — already his superior structured/deterministic memory). A per-employee `memory: rag|flat` field on the Employee model (config-driven from `context.md`, with a default table: **flat** = bob/gibby/tom, **rag** = tony/mike/elon/phoebe/july) gates everything: a flat employee's `remember()` is a no-op, `recall()`/`recall_context()` return empty, and daily-run builds NO index for them (lighter — fewer refreshes). The Phase-18 recall-at-dispatch follow-up is now wired end-to-end via `Employee.recall_context(query)` — the live dispatcher `supervisor.py` (`Member.real_command()`) bridges to the data model and prepends the budget-capped, injection-safe "Relevant past experience: …" block before dispatching a rag worker ("" for flat / no-venv / no-hit, so it never blocks or fails a dispatch). Config overrides the table both ways; case-insensitive; never raises; R1–R6 byte-identical.

### v0.1.8 Per-Employee RAG Memory Completed (Phase 18)

- ✅ **Phase 18 — per-employee RAG memory ("workers grow with the project")** — each employee gets their OWN isolated RAG memory store, built on the Employee model's `memory_dir` seam and reusing the Phase-13 RAG stack (no new embedding/index/query machinery). `Employee.remember(text, tags, source)` writes a structured, content-hash-deduped memory file to `org/employees/<name>/memory/`; `Employee.recall(query)` shells `rag_query.py` against that employee's **own** index and returns their semantically-relevant past experience (no-venv/empty/timeout → `[]`, never raises/blocks). Capture is explicit + structured (a closing "record one reusable lesson" step in the worker personas), NOT auto-indexed logs; the store is FLAT (no per-employee tiers/decay/entropy — the shared company memory keeps the anti-entropy machinery). Eight physically-separate indexes, refreshed incrementally in daily-run under Tony's existing `rag_index` duty (no new Layer-B step). **Isolation proven** (a query for one worker never returns another's or the company's memory — realpath containment backstop survives symlink/traversal); the `tier: L2` reuse constant does NOT contaminate the company pipeline (`.company/memory` and `org/employees/*/memory` are disjoint subtrees). Dispatch-time recall-injection is now wired at the concrete call site (`supervisor.py` `Member.real_command()` prepends `recall_context()`'s block before dispatching a rag worker); see the v0.1.9 entry. R1–R6 byte-identical (employee.py additive only).

### v0.1.7 Employee Model + Capability Steward Completed (Phases 16-17)

- ✅ **Phase 16 — the `Employee` model** — the foundation (Chairman: "build our fundamental well first"). ONE data-driven `Employee` class, eight instances (no per-employee subclass); `Employee.load(name, company)` exposes identity, the least-privilege capability slice, execution knobs, and desk paths, plus `allows_duty`/`owns_step`/`should_run`/`log`/`capabilities`/`roster`. The authoritative red/blue role topology (`EMPLOYEES`, `ALLOWED_DUTIES`, attack/build/verify classes, `STEP_OWNER`) moved into `scripts/employee.py` as its single home; `schedule_config.py` + `schedule_validator.py` import it from there (R1–R6 proven byte-identical). Pure stdlib; never raises.
- ✅ **Phase 17 — July the capability steward (PROPOSE-ONLY)** — July gains a load-bearing, scheduled job built ON the Employee model. The capability profile extends to four functional dimensions (`tools`/`mcp`/`skills`/`plugins`) via the reserved `capabilities()` seam. `scripts/july_audit.py` (deterministic, `july_audit` step owned by July, weekly-recommended) detects available capabilities from the environment (MCP config / skills dirs / plugins — absent/empty/malformed source ⇒ "unknown", never a crash), diffs vs each worker's declared profile, and surfaces **STALE / CAPABILITY GAP / OVER-GRANT** all as **PROPOSALS** to `ops/plans/capability-audit-<date>.md` (Elon→Phoebe→Tom apply any approved edit). It **never edits a `context.md`**: the hard lesson (Gibby's D1+D2) is that filesystem availability can't be ground truth — a bundled skill like `deep-research` isn't enumerable, so any auto-removal is unsafe; dropping auto-mutation deletes the whole class. Managers never audited; Gibby/Bob-pair items marked human-review; red/blue duty assignment untouched (still `schedule_validator`'s R1–R6). Never fails the run.

### v0.1.6 Plugin-subdir Restructure Completed (Phase 15)

- ✅ **Phase 15 — ship-exclude `tests/`+`evals/` via a `plugin/` subdir** — Claude Code plugin packaging has no ship-ignore (the whole `source` dir installs), so the plugin content (`skills/`, `hooks/`, `plugin.json`) moved into `plugin/` and the marketplace `source` became `./plugin`; `tests/` + `evals/` stay at the repo root and no longer download to users. All via `git mv` (history preserved). Scripts/hooks resolve unchanged — they locate code via `${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/…` and `CLAUDE_PLUGIN_ROOT` now points at `plugin/`, whose internal layout mirrors the old one. Only repo-root-relative references changed (`tests/_helpers` SCRIPTS_DIR, a few test path literals, the CI `bash -n` path, `dev-link-skill.sh`, docs). Existing installs self-heal their absolute cron path to `…/plugin/skills/…` via the Phase-12b SessionStart guard.

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
- ⏳ RAG B.2 — semantic dedup-at-capture (deferred: daily reinforce already absorbs transient dups; false-positive on a write path risks silent loss)
- ⏳ Persona boilerplate single-sourcing (deferred: isolated workers load only their persona)
- ✅ July (HR) now has an active scheduled job — the **capability steward** audit (Phase 17) closes the long-open "July has no clear active job" gap. (A quantified worker-*performance* review remains a separate, still-pending track.)
