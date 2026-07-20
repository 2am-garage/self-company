# Changelog

All notable changes to the self-company plugin. Versions track
`plugin/.claude-plugin/plugin.json`. This log starts at 0.1.10 (the last
version before the Phase 19-29 audit series); earlier history lives in the
git log / `references/status.md`.

> Note: the manifest version went **0.1.10 → 0.1.14 in a single bump** (at Phase 29).
> There was never a released 0.1.13 — the "0.1.13 batch" (Phases 24–28, so labelled
> in `status.md`/specs as the intended shared version) shipped folded into 0.1.14.
> The `[0.1.13]` heading below is an internal milestone marker, not a stamped release.

## [0.1.20] — 2026-07-21: Robustness follow-up — tolerant verdict extractor, simpler contract, diagnostic UNRESOLVED

Fixes the gate's first live FALSE-NEGATIVE: on its first real dispatch (the verified-decay task,
`3a4a630`) the gate returned UNRESOLVED even though the work was correct — the real `claude -p`
Gibby's genuine verdict didn't reproduce the strict `@qa-verdict <NONCE> {json}` sentinel closely
enough. This is a FORMAT-robustness fix, not a security change: the nonce stays REQUIRED end to
end, and every prior security test in `tests/test_redblue_gate.py` stays green unmodified.

- `_extract_qa_verdict` (via new `_parse_qa_payload`) now also accepts a bare `pass`/`fail`
  keyword — case-insensitive, tolerant of surrounding whitespace and trailing prose — after the
  correct nonce, alongside the original JSON form (kept for back-compat). A wrong/absent nonce is
  still rejected identically to before.
- `_verdict_contract` now leads with the literal, copy-paste `@qa-verdict <NONCE> pass`/`fail` line
  (the actual nonce substituted in) as the mandatory, only-thing-read last line — simpler for an
  LLM to reproduce verbatim than hand-building JSON. The JSON form is still mentioned as an accepted
  fallback.
- New diagnostic: a nonce-authenticated line whose content isn't a recognized pass/fail form
  (`_qa_verdict_format_miss`, tracked as `Worker.nonce_format_miss`) is classified distinctly from
  no authenticated line at all — never silently a pass. `_unresolved_reason` labels a cap-reached
  cycle `genuine_fail` / `format_miss` / `no_verdict`, surfaced in the supervisor's UNRESOLVED
  stderr message and folded into `company-run.sh`'s ledger verdict cell.
- +37 tests across `tests/test_redblue_gate.py` (tolerant-form acceptance/rejection, format-miss
  classification, diagnostic UNRESOLVED reasons, the new contract text).

## [0.1.19] — 2026-07-18: Phase 33 finalization — verdict nonce + capture timeout (DEFENSE-IN-DEPTH), heuristic removed

Gibby's adversarial pass proved the nonce and timeout **raise the bar but do NOT close**
forgery/DoS against an actively-malicious builder (same-uid sibling processes + live `/proc`).
Shipped as **labeled defense-in-depth**, not a security guarantee; the sound fixes are documented
as future work. What IS genuinely closed: the non-builder heuristic, and the phase's real purpose —
Gibby can no longer silently rubber-stamp.

- Verdict nonce (`_dispatch_redblue` mints `secrets.token_hex(8)` into **Gibby's prompt only**;
  `@qa-verdict <NONCE> {json}`; `_extract_qa_verdict` requires the exact nonce). Parser + fd
  attribution are sound. **But the nonce leaks**: the prompt is a `claude -p` **argv** element,
  readable by any same-uid process via `/proc/<pid>/cmdline` — Gibby lifted it in <1s and forged an
  accepted **enforcement** verdict. Sound fix (future): deliver Gibby's prompt via stdin, not argv.
- Capture timeout on `company-run.sh`'s stderr capture (env `SELF_COMPANY_GATE_CAPTURE_TIMEOUT`,
  default **2400s** — raised from 900s, which was below the legit 3×600s worst case and could
  false-kill a real multi-round gate). Bounds an **accidental** hang; does NOT bound a deliberate
  DoS — a `setsid`-detached worker holding `/proc/<sup>/fd/2` escapes timeout's group-kill. Sound
  fix (future): file-based gate result + `wait` on the supervisor PID.
- Removed the non-builder content-mutation-refusal heuristic (`_looks_like_code_mutation` + regexes
  + the `dispatch()` refusal branch) — genuinely superseded by Phase 34's per-worker
  `--disallowedTools` fence (a non-builder is structurally unable to mutate source), and it had a
  real cost (false-refused legitimate read-only tasks). Builder-presence arming unchanged.
- What the gate reliably provides: Gibby can't silently rubber-stamp (re-loop on a genuine fail,
  UNRESOLVED loud rc≠0, sound parser/pipe-identity). Not closed (documented): a `/proc`-scanning
  malicious builder can forge the enforcement verdict (via the nonce leak), DoS the capture, or
  forge the ledger display — needs stdin delivery + file-based result + OS uid/namespace isolation.
- +30 regression tests across `tests/test_redblue_gate.py`, `tests/test_company_run.py`,
  `tests/test_company_run_plan_parse.py`.

## [0.1.17] — 2026-07-13: Phase 32 hotfix (Gibby's second adversarial pass)

- Fix a deterministic red test the 0.1.16 docs sweep introduced:
  `test_docs_state_eight_registrations` still expected the docs to say "8"
  after they correctly moved to "9" — retargeted to assert NINE.
- Close the discovery-path divergence Gibby found: `supervisor.Member.roster()`
  still used a pre-Phase-32 ad-hoc scan (no charset / no `context.md` / no
  symlink rejection) on the real dispatch path, so a persona-only "ghost" desk
  or a symlinked-persona desk that `discover()`/R7 exclude still got listed and
  inlined into a worker prompt. `discover()` and `roster()` now share ONE
  `employee.is_valid_desk()` predicate; `company-run.sh` also runs the validator
  before dispatch. Defense-in-depth (needs write access to the private store;
  no Layer-B power was reachable) — but the three paths no longer disagree.
- +14 regression tests. Note: 0.1.16 shipped with these two issues (merged on
  Gibby's interim 4-bug report before its full pass landed); this is the same-day
  correction.

## [0.1.18] — 2026-07-15: Phase 34 — per-worker tool restriction

- Dispatched workers are tool-fenced by a code-locked duty→profile table
  (`employee.py` `CORE_TOOL_PROFILES`): **execute** tier (bob/tom/gibby) keeps
  full tools; **restricted** tier (tony/mike/elon/phoebe/july) is spawned with
  `--disallowedTools Bash Write Edit NotebookEdit`; hired/unknown names
  fail-close to restricted. Name-only lookup — a desk's own `context.md` can't
  raise its ceiling.
- The bare-name `--disallowedTools` form removes the tool from the model's
  schema structurally (verified against a real `claude -p`; propagates through
  Task-subagent delegation). No Bash ⇒ the `/proc/<pid>/fd` write vector is
  closed for restricted roles — the foundation Phase 33's gate needs (build
  work can't be silently routed to a non-builder).
- Boundary (documented, deliberate): the fence covers supervisor-dispatched
  workers; `daily-run.sh`/`research-scan.sh`/`fire-trigger.sh` run the same
  personas with full tools for their own `.company/` deliverables. Fence-
  extension to `agent_spawn.sh` is a logged follow-up.
- Phase 33 (verification gate) branch stays unmerged; it returns next on this
  foundation, finalized with a verdict nonce + capture timeout.

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
