# Changelog

All notable changes to the self-company plugin. Versions track
`plugin/.claude-plugin/plugin.json`. This log starts at 0.1.10 (the last
version before the Phase 19-29 audit series); earlier history lives in the
git log / `references/status.md`.

> Note: the manifest version went **0.1.10 → 0.1.14 in a single bump** (at Phase 29).
> There was never a released 0.1.13 — the "0.1.13 batch" (Phases 24–28, so labelled
> in `status.md`/specs as the intended shared version) shipped folded into 0.1.14.
> The `[0.1.13]` heading below is an internal milestone marker, not a stamped release.

## [0.1.16] — 2026-07-13: Phase 32 — hire-as-data (worker & manager tiers)

- `hire.sh <id> --tier worker|manager` scaffolds an `org/employees/<id>/` desk;
  `Employee.discover()` = core 8 ∪ valid desks on disk, dispatched with no code
  edit. Zero hired desks reproduce today's output byte-for-byte. `--fire`
  tombstones to `org/employees/.fired/` (never deletes).
- Charter singletons stay code-pinned: a hire can be a worker or a department
  **manager** but cannot replace/claim Elon (CEO), Phoebe (gateway), July (HR),
  or Gibby (QA sign-off). New validator **R7**: `tier: worker|manager` only, no
  attack/build duty (R1 survives), no charter-role claim (normalized match),
  manager graph acyclic and rooted at Elon. Core 8 keep R1–R6 unchanged.
- Hardening: `discover()` rejects bad-charset/dotfile/symlinked desks; `hire.sh`
  is atomic and fail-closed (missing validator → refuse); new 9th hook
  `hook_org_lint.sh` (PostToolUse, warn-only) lints hand-edits under
  `org/employees/**`. Red/blue: Gibby found 4 gaps (incl. `.fired` poisoning the
  validator on first fire), all fixed with +13 regression tests before merge.

## [0.1.15] — 2026-07-12: first features shipped by the daily research loop

- Real token accounting: `supervisor.py` captures usage/cost from the `result`
  event it already parsed (only `is_error` before) into a running daily total
  at `.company/ops/.token-usage`; `daily-run.sh`'s budget-degradation check
  reads the real total alongside the `DAILY_RUNS_PER_DAY` proxy.
- `entropy.py` O(n) sources-array dup-candidate pre-filter (advisory-only JSON
  candidates; never scores, never auto-merges) — catches same-source duplicate
  pairs the cosine scan misses.
- Process note: both items came from the new daily loop (Mike 08:00 research →
  Elon review → dispatch → red/blue → merge); headless workers gained
  Edit/Write + WebSearch/WebFetch permissions on 2026-07-11.
- (2026-07-13, same loop) Opt-in ~1,000–2,000-token soft cap on worker return
  summaries: `output_contract(summary_cap=True)` at supervisor's worker-dispatch
  site only; trigger contracts stay capless (Phase 21 "seconds, never tokens"
  invariant preserved). `pipeline.md` handoff spec cites the Anthropic source.

## [0.1.14] — Phase 29: prompt/harness engineering & the employee model table

- Per-employee model table wired end to end (`Employee.resolved_model`): each
  employee's `model:` in `context.md` is Layer-A adjustable (unset → silent
  default, invalid → default + warning, never hard-fails a dispatch);
  defaults bob/gibby/tom = haiku, tony/mike/july = sonnet, phoebe pinned to
  `claude-sonnet-4-6`, elon = fable; system default bumped to `claude-sonnet-5`.
- `@status` restored for real dispatched agents via `--output-format
  stream-json` (was silently dead in plain-text mode); env escape hatch to
  revert.
- Shared `prompt_builder.py`: a stated wall-clock budget + nonce-fenced data
  block used by every dispatch prompt (fire-trigger, research-scan,
  supervisor, company-run); persona body inlined into worker prompts.
- Injection char budgets raised (600→1800 / 900→2700) with the Phase-24
  reranker gate intact.

## [0.1.13] — Phases 19-28 (batched version bump)

### Phase 28 — pipeline efficiency & overlap consolidation
- `schedule_config.py --plan-tick`: one JSON call replaces ~13 per-tick
  `schedule_config` spawns.
- `corpus.py`: one `load_memories()` consolidates 6 corpus loaders
  (byte-identical `entropy.json` proof) and fixes a frontmatter
  body-extraction truncation bug on `---`-containing values.
- `rag_index.py --pair`: batches the company + every rag-employee index
  refresh into ONE process (up to ~8 fastembed loads → ≤1).
- `agent_spawn.sh`: consolidates CLAUDE_BIN/kill-after/capture-spawn/auth/
  scripts-dir resolution shared by every spawn site.

### Phase 27 — observability & run-health
- Machine-readable per-run `daily-<date>.jsonl` (unique `run_id`) is now the
  source of truth, read by one shared `daily_log.py`; retires 4 drifting
  regex parsers and the old mtime in-flight heuristic.
- Staleness alarm (dark past 2× the installed cadence, silent when no cron is
  installed); lock-skip streaks now escalate instead of masquerading as
  healthy.
- `timeout -k 900s` on every bare deterministic-core step; `ops/logs`
  age-pruning with a 30-day parse window.

### Phase 26 — trigger-security & guard follow-ups
- `fire-trigger.sh` commits state only AFTER stage-2 validation + a
  commit-time guard re-check under flock — closes the daily-cap DoS in
  Phase 21's own threat model.
- `require_confirm` is now an honest HOLD regardless of `source_trust` (was
  silently skipped for trusted triggers); dead `.pending.json` / phantom
  `--confirm-override` removed.
- `hook_memory_guard` fail-closed hardening: recursive transparent-prefix and
  nested-shell stripping before the delete-command check.

### Phase 25 — durability & data-safety
- A free-space preflight + tar-snapshot-failure abort (`CORE_ABORT` marker)
  guards every mutating core run — a disk-full tick can no longer silently
  zero the corpus.
- One shared `frontmatter._atomic_write` (temp + `os.replace`) for every
  memory writer; a kill mid-write never truncates or loses a memory.
- The daily lock is held under `setsid` with process-group kill + a
  stale-lock tripwire, so a surviving grandchild can't wedge the scheduler.

### Phase 24 — RAG quality overhaul
- Swapped to a multilingual embedding model
  (`paraphrase-multilingual-MiniLM-L12-v2`) fixing wrong-language retrieval
  for the Chairman's Chinese-language default (ZH hit@1 0.375→0.875).
- Added a model/dim/lib index stamp that refuses cross-space cosine and
  auto-rebuilds on mismatch; retuned the injection floor 0.30→0.40 with an
  IDF-gated keyword-path fallback.
- Native hybrid retrieval (BM25 + vector, fused via RRF) and a cross-encoder
  reranker (precision layer only, never load-bearing).
- `Employee.remember()` wired into the 5 rag-mode personas (per-employee
  memory capture had zero call sites before this).

### Phase 22 — quality consolidation & doc truth
- `rag_venv.py` single-sources the 4 duplicated re-exec-into-venv copies;
  `schedule.sh` prepends python3's own dir to the cron PATH so a pyenv/conda
  install no longer silently no-ops the deterministic core.
- `hook_memory_guard` blocks `rm -rf .company` (store root) plus
  find-delete/truncate.
- Docs corrected to the real hook count ("8 registrations across 7 events").

### Phase 21 — trigger-engine robustness & injection guardrail
- `trigger_engine.decide()` coerces malformed config/state to safe defaults —
  a bad config or corrupt state HOLDS fail-closed instead of crash-and-wedge.
- `decide_and_record()` made atomic under flock + temp/`os.replace`.
- External-payload prompt-injection guardrail: a deterministic, tool-less
  parse stage distills the payload into a schema-validated intent before any
  action runs; `source_trust` routing + `require_confirm`.

### Phase 20 — memory & RAG integrity
- Fixed the RAG incremental cache to invalidate on path/tier change, so an
  L1→L2-promoted memory stays retrievable via semantic recall.
- Tombstones are absorbed (`status: absorbed` + `invalid_at`) instead of
  hard-unlinked, so they stay recoverable within the grace window.

### Phase 19 — runtime reliability & concurrency hardening
- `timeout -k <grace>` on every agent spawn so a SIGTERM-trapping `claude` is
  SIGKILLed past budget instead of orphaning.
- `flock` on `.company/ops/.daily.lock` serializes the memory-mutating core;
  cron skips on overlap, a manual run blocks and waits.

## [0.1.10] — baseline before the Phase 19-29 audit series

Prior history (Phases 1-18c: plugin packaging, per-company scheduling, the
holding-company fleet layer, per-employee memory, shared company-memory read
at dispatch) is not repeated here — see the git log and
`references/status.md` for the full record.
