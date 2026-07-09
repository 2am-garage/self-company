# Operations reference

How to run and wire the company's day-to-day operations. Four areas:

1. **[Triggers](#triggers--three-ways-the-company-starts-working)** — the four ways the company starts working (call / clock / event / session), session vs headless dispatch, the §5.5 chain, the event-driven `fire-trigger.sh` flow, poll adapter.
2. **[Session Catch-Up Notification](#session-catch-up-notification-chairman-opt-in-option-b)** — the `SessionStart` hook, `notify-status.py --emit-hook`, push-only rule, manual fallback.
3. **[Scheduled-Work Ledger](#scheduled-work-ledger-autoresearch-style-report)** — `report.py`, `ops/reports/ledger.md`, the entropy-headline table, verdict vocabulary.
4. **[On-demand views](#on-demand-views-chairman-asks--render-inline)** — the "Chairman says → run → shows" table, `org-status.py` snapshot, `supervisor.py` live harness.

---

## Repository layout — what ships vs. what stays home

Since **Phase 15** the shippable plugin lives entirely under a `plugin/` subdirectory,
and the marketplace `source` points at `./plugin`. So `CLAUDE_PLUGIN_ROOT` resolves to
that `plugin/` dir, under which the internal structure is unchanged
(`skills/self-company/…`, `hooks/hooks.json`) — every script's
`${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/<script>` path and the `hooks.json`
declarations needed **no** change.

```
repo-root/
  .claude-plugin/marketplace.json   # source: "./plugin"  (stays at repo root)
  plugin/                           # == CLAUDE_PLUGIN_ROOT when installed
    .claude-plugin/plugin.json
    hooks/hooks.json
    skills/self-company/…           # SKILL.md, scripts/, references/, assets/, design/
  tests/                            # dev-only — NOT shipped to users
  evals/                            # dev-only — NOT shipped to users
  .github/, README.md, MISSION.md   # repo scaffolding — NOT shipped
```

Only the `plugin/` subtree is delivered on install, so **`tests/` and `evals/` no longer
ship to users** — they were previously siblings of `skills/` under the packaged root and
rode along with every install. The test suite still runs from the repo (its
`REPO_ROOT/plugin/skills/self-company/…` joins locate the moved scripts); it is simply
excluded from the plugin payload.

---

## Triggers — three ways the company starts working

| # | Trigger | Mechanism | Fired by |
|---|---|---|---|
| 1 | Chairman calls | conversation | the Chairman |
| 2 | Clock | cron → `daily-run.sh` (default every 6h; per-company via `org/schedule.yaml`) | time |
| 3 | **Event** | **`fire-trigger.sh <name> <payload>`** (push) | any external program / user-defined |
| 4 | **Session** | **`company-run.sh "<task>"`** | the interactive session (Elon hands work to the company) |

**Trigger #4 (session).** Per MISSION.md, this repo is run by the self-company to
improve the self-company. Rather than Elon silently editing every file, the
session hands a task to the company.

There are two dispatch paths — pick by origin:

**Session dispatch (default when the Chairman is present) — real, visible agents.**
When a cycle is triggered from an interactive session, dispatch employees as REAL
subagents via the **harness Agent tool** (not a script). These are genuinely
separate agents AND the Claude app renders them live. Follow the chain and the
reporting rule (policy.md §5.5):

1. **Phoebe** (Agent) plans a `{employee: subtask}` assignment.
2. **Assigned workers** (Agent, e.g. Bob) implement. Use `isolation: worktree`
   when workers edit code in parallel, to avoid conflicts; sequential single-owner
   edits need no worktree.
3. **Gibby** (Agent) verifies — reads the diff, runs the suite, sanity-runs the tool.
4. Workers and Gibby **report to Phoebe**; Phoebe aggregates and **reports to Elon**.
5. **Elon** resolves small tasks with Phoebe; escalates big ones to the Chairman.

> A bash script cannot call the Agent tool — app-visible subagents are inherently
> spawned by the main session. So this path is driven by the session agent (Elon),
> documented here as the standard operating procedure, not by `company-run.sh`.

**Headless dispatch (cron / external trigger / no session) — portable, text-only.**
`company-run.sh "<task>"` has Phoebe plan, then `supervisor.py` spawns the assigned
employees as live child processes (`claude -p`); the cycle is logged to
`ops/reports/company-runs.md`. Not app-visible (native widgets belong to the host),
but fully modular. `--demo` runs the whole flow with no LLM.

**Trigger #3 (event-driven)** is push-first: the company is dormant until an
external producer (a training run, trading bot, CI job, …) fires it — no polling,
no daemon. Triggers are **user-defined**, declarative, one file per trigger under
`org/triggers/<name>.yaml` (flat `key: value`; see `org/triggers/README.md`). The
engine is never edited:

```
your program ── fire-trigger.sh training-done '{"val_bpb":0.98}' ──┐
                                                                    ▼
   trigger_engine.py --decide (READ-ONLY): condition → cooldown/dedupe/cap
                                                                    │ fire=true
                        untrusted: tool-less STAGE-1 parse → schema validation
                                                                    │ ok, not require_confirm-held
                            trigger_engine.py --commit: re-check the SAME
                             guards under the state flock, THEN record()
                                                                    │ still fires
                                          detached, bounded `claude -p` → Phoebe
```

**State only commits after validation (Phase 26 Item 1).** A schema-rejected
or `require_confirm`-parked payload consumes NOTHING — no cap slot, no
cooldown/dedupe update — so a malformed or untrusted producer can never burn
the daily budget by sending garbage (the DoS the privilege-separation split
below exists to prevent). The `--commit` re-check happens **inside the same
state flock** `decide_and_record()` already used, so a race that claims the
slot during the gap converts a would-be fire into a clean HOLD instead of an
over-fire. `require_confirm` today is a deterministic HOLD-for-manual, not an
approval queue — see `org/triggers/README.md`.

Decision is deterministic and testable (`trigger_engine.py`); orchestration —
the bounded, recursion-guarded, detached agent — lives in `fire-trigger.sh`, the
same split as `daily-run.sh`. Every call (fired or held) is appended to
`ops/reports/triggers.md`. For sources that *cannot* call us, an optional cron
**poll adapter** can check them and call the same entry point — push primary,
poll only as a fallback.

### Multi-company scheduling (`schedule.sh`) — the crontab as a keyed set

`schedule.sh` owns Trigger #2 (the clock). One deployment installs **two** OS
crontab lines per project: `daily-run.sh` (default every 6h) and `research-scan.sh`
weekly — both cadences are per-company overridable via `org/schedule.yaml` (see
[Per-company schedule & duties](#per-company-schedule--duties-orgscheduleyaml--config-not-hardcode) below).
The two lines mirror the role split — the 6-hourly line is Tony's *internal*
maintenance; the weekly line is **Mike's *external* research pass** (R&D
Researcher): a bounded headless `claude -p` that surveys the outside world and
writes a dated, cited BRIEF to `ops/research/research-<date>.md`, then feeds the
mechanism-level items to `ops/plans/proposals-<date>.md` for Tony/Elon.
The Chairman deploys the skill to several repos, so the scheduler treats the
crontab as a **keyed set of companies** — one entry pair per project, every
operation scoped by a stable per-project key (`sha1(abs PROJECT_DIR)[:12]`).
Installing company B never evicts company A. This is a general mechanism (N
companies as data), the same generalization `tombstone.py`/`charter_ids.py` use —
not a per-repo special case.

- **Namespaced ownership.** Each managed line is tagged
  `# self-company-daily project=<key> path=<PROJECT_DIR>` (and `-research`
  likewise). install/uninstall filter on `project=<key>`, so they touch only the
  current project; other companies and any non-self-company crontab line are left
  byte-untouched. Idempotent per project (re-install replaces just its two lines).
  A legacy un-namespaced line (`# self-company-daily`, no `project=`) whose
  embedded `cd '<path>'` matches the project is migrated to the namespaced form on
  the next install/uninstall — never orphaned, never duplicated.
- **Auto-stagger.** The default daily minute is `sha1(path) % 60` (the weekly
  research minute a second, independent hash slice), so N companies land on
  different minutes across the hour instead of stacking on `:07` (concurrent
  headless `claude -p`, token bursts). `SELF_COMPANY_CRON_MIN` still overrides
  explicitly — accept the special case via config, don't hardcode it.
- **Fleet commands.**
  - `list` (or `status --all`) — table of every scheduled company: path, daily
    minute, research present, and **ORPHAN** if that project's `.company/` is gone.
  - `status [PROJECT]` — single-project view (back-compat).
  - `install [PROJECT]` / `uninstall [PROJECT]` — scoped add / remove.
  - `prune` — remove only orphan/dead-path lines; never a live one.
- **Testability seam (C1).** All crontab I/O routes through `_cron_read` /
  `_cron_write`. Set `SELF_COMPANY_CRONTAB_FILE` to read+write a file instead of
  the real `crontab` binary (`SELF_COMPANY_CRONTAB_CMD` overrides the binary) — a
  general injectable backend used by `tests/test_schedule.py` so the suite never
  touches the user's real crontab.

### Per-company schedule & duties (`org/schedule.yaml`) — config, not hardcode

The `*/6` tick, weekly research, and the daily duty pipeline are **defaults**, not
fixed: a company declares its OWN schedule and per-employee duties as DATA in
`org/schedule.yaml` (per company, git-ignored like the rest of `.company/`; a
commented, absent-equivalent template ships on init). The reader
`schedule_config.py` is the single source of truth; same safe flat-YAML discipline
as `trigger_engine.py` (PyYAML optional, stdlib fallback, no hard dep). **Every key
is optional — a missing or empty `schedule.yaml` reproduces today's behaviour
byte-for-byte.** The design has **two layers**:

- **Layer A (knobs — freely adjustable).** Company **`cadence`** = the cron tick
  (how often `daily-run.sh` fires); **`research: { enabled, cadence }`** = Mike's
  weekly pass on/off + when; **`agent: { model, timeout, daily_cap }`** = the daily
  headless-agent knobs (env `SELF_COMPANY_DAILY_*` still wins). Per-employee blocks
  (`tony`/`gibby`/`bob`/`mike`/`elon`/`tom`/…) take **`cadence`** (a sub-cadence
  relative to the tick), **`duties`** (which of that employee's OWN duties run),
  **`budget`**, and **`enabled`**.
- **Layer B (structural invariants — NOT configurable).** Who is attacker vs
  builder, the 3-consecutive sign-off gate, ledger-first/defenses-only-grow, and
  the dispatch topology stay in code. There is deliberately **no `role:`/`tier:`/
  `attacks:` field** — config picks *which* of an employee's fixed duties run, it
  can never *reassign* a role. See below and `references/red-blue-protocol.md`.

**Cadence grammar** (company tick and per-employee alike, translated to a 5-field
cron by `schedule_config.py`): `every Nh` (1–23) · `hourly` · `weekdays-<start>-<end>`
(hours, Mon–Fri) · a raw 5-field cron expression. Research cadence adds
`weekly-<dow>-<hh>` (dow `sun`..`sat` or `0`..`6`). Per-employee sub-cadences:
`every-run` · `every-Nth` · `daily` (first tick of the day) · `weekly` (Sunday first
tick) · `on-trigger` (never in the batch). Any invalid/out-of-range/malformed
cadence falls back to the default and logs — **a broken or injection-shaped cron
expression is never written to the crontab** (`schedule_config.py` validates the
expression's charset AND per-field semantics; `schedule.sh` trusts that verdict).

- **Runtime duty gating.** Per-employee cadence is resolved **deterministically at
  runtime inside `daily-run.sh`** (one tick, gate duties as data) — NOT N separate
  cron lines (that would multiply token/process cost). Gating is **fail-open**: any
  doubt (no config, missing python, error) runs the step, so maintenance is never
  silently suppressed. `schedule_config.py --should-run STEP --hour H --dow D` is
  the seam.
- **Invariant validator (Layer B enforcement).** `schedule_validator.py` refuses
  any config that would break the red/blue competition — **rules R1–R6** (attacker≠
  builder, attack surface must stay covered, sign-off gate/ledger/role fields are
  not tunable, dispatch topology preserved). On any violation the config is
  **rejected and the company runs with defaults**, logging the named rule; a
  mis-configured competition never *runs*, it falls back. `schedule.sh` and
  `daily-run.sh` both consult the validator before honouring config.
- **SessionStart sync + self-heal (`hook_schedule_guard.sh`).** Because the crontab
  carries an **absolute snapshot** of both the tick and the scripts dir (Phase-7 A1),
  a `cadence`/`research` edit — OR a plugin update that moves the scripts — only
  reaches the live crontab on re-install. The `SessionStart` guard closes both gaps:
  if `schedule.yaml` is absent it no-ops; otherwise it validates (an invalid config
  is a non-blocking warning — daily-run falls back on its own), then compares a
  signature = *desired tick + research cadence + resolved scripts dir* against
  `ops/schedule/.installed-tick` and re-runs `schedule.sh install` **when any of the
  three changed** — so a tick/research edit AND a **plugin update/move** both
  self-heal the cron with no manual step (per-employee sub-cadence edits do NOT
  change the signature, so they never re-install). The scripts dir is read
  ground-truth from `schedule.sh scripts-dir` (single source, honours
  `CLAUDE_PLUGIN_ROOT`); an older 2-field marker self-heals exactly once. It honours
  `SELF_COMPANY_CRONTAB_FILE` and skips silently with no crontab backend, and only
  syncs an already-scheduled project (never auto-installs one).
- **Generated roster.** `ops/schedule/roster.md` is now **GENERATED** by
  `daily-run.sh` from the effective config on every run — do NOT hand-edit it (edit
  `org/schedule.yaml` instead; the file is marked generated).

### July's capability audit (`july_audit.py`) — the capability steward step

July has a load-bearing, recurring job: keep every worker's **functional
capability profile** — the `tools` / `mcp` / `skills` / `plugins` declared in
`org/employees/<name>/context.md` — both **accurate** against the real environment
and **right-sized** (least-privilege) as the toolchain evolves. This runs as a
deterministic `daily-run.sh` step (`july_audit`, owned by **July** in the
`employee.py` topology; gate `_should_run july_audit`; set `july: { cadence:
weekly }` in `schedule.yaml` — recommended, it's low-churn). Built ON the Phase-16
`Employee` model: profiles are read via `Employee.load(name).capabilities()`.

**PROPOSE-ONLY (P17-D2).** july_audit **never edits any `context.md`**. The lesson
from two same-class breaks: available capabilities **cannot be enumerated to
ground truth** from the filesystem — a live, bundled grant like the `deep-research`
skill is not physically discoverable under `~/.claude`, so ANY irreversible
auto-removal on a filesystem view is unsafe. So every finding is a **proposal** the
Chairman/Elon approve; Elon → Phoebe → Tom apply any approved edit.

- **Detect** available capabilities (read-only, graceful): the builtin tool set;
  MCP servers (`.mcp.json` / `.claude/settings*.json` / `~/.claude.json`); skills
  (`.claude/skills` + marketplace); installed plugins (`.claude/plugins`). The
  detector returns `(available, complete, source)`: an **absent / empty /
  malformed** view ⇒ availability `None` ("unknown"), and a malformed candidate
  **poisons** the whole dimension — that dimension is then **skipped**, never a
  crash. `complete` records whether the view was trustworthy-complete; it is
  reported as **context on each proposal**, not a mutation gate (there is no
  mutation).
- **Classify** per worker, per churny dimension (mcp/skills/plugins; `tools` is
  reported, not classified): **STALE** (declared but not found in the enumerable
  environment) → **propose removal**, tagged with the view's completeness so the
  human can tell "genuinely gone" from "non-enumerable"; **CAPABILITY GAP**
  (available + role plausibly needs it) → **propose grant**; **OVER-GRANT**
  (declared + available + role doesn't need it) → **propose removal**.
- **Guardrails.** Managers (Elon/Phoebe/July) are **never audited**. Nothing is
  auto-applied — no `context.md` is edited under any input. Capability grants are
  **orthogonal to red/blue duty assignment** (which `schedule_validator` R1–R6
  owns) — July may not reassign duties, and any proposal touching the **Gibby/Bob
  pair is marked "human review required."** Emits a stable JSON report
  (`july-capability-audit/1`); with `--apply` it WRITES the proposals to
  `ops/plans/capability-audit-<date>.md` (Elon adjudicates → Phoebe dispatches →
  Tom edits the profile) and logs to `org/employees/july/log.md`. Never fails the
  run.

### Per-employee memory index refresh (Phase 18/18b) — the RAG-employee stores

Each **`rag`-mode** worker has their OWN isolated "experience recall" memory store
(`org/employees/<name>/memory/`) captured via `Employee.remember()` and recalled
via `Employee.recall()` / injected at dispatch via `Employee.recall_context()`
(see `references/execution-model.md §7`). `daily-run.sh` keeps each store's OWN
LanceDB index (`memory/index`) fresh by pointing the SAME reused `rag_index.py` at
each employee: `--memory-dir org/employees/<name>/memory --index-dir
org/employees/<name>/memory/index`. The index is **physically the employee's own**
(the Chairman's isolation choice — never a shared owner-filtered index).
Properties, by construction:

- **RAG employees only (Phase 18b).** The refresh loop skips **`flat`** employees
  (Bob/Gibby/Tom by default) entirely — no index, fewer refreshes, lighter. It
  reads the rag/flat split from `employee.py` (`Employee.rag_memory_enabled`,
  driven by each desk's `context.md` `memory:` field), so which employees index is
  CONFIG, not a name hardcoded in the script. The log line reports how many flat
  employees were skipped (`… (N flat employee(s) skipped — no index)`).
- **Incremental & cheap.** `content_hash` skips unchanged files, so an untouched
  store re-embeds nothing; the small rag stores stay ~free.
- **Gated under Tony's existing `rag_index` step** — the same index-infra duty. No
  new Layer-B step owner is added, so the role topology and the R1–R6 validator
  stay byte-identical.
- **Graceful.** RAG venv absent → one-line skip (`emp-memory-index: skipped — RAG
  venv absent …`); capture (`remember`) still writes regardless (stdlib, and only
  for rag employees). Every refresh is `|| true`, so a bad store can never abort
  the already-completed core.
- **Flat & light.** Capture → index → recall only. No per-employee
  decay/verify/entropy/tiers — that anti-entropy machinery stays on the SHARED
  company memory. A per-employee memory carries a fixed `tier: L2` purely so the
  unmodified indexer picks it up (one constant value, nothing promotes/decays it).

### Holding company (fleet orchestrator) — one cron for N sub-companies

> **OPTIONAL layer — most users can skip this whole section.** Fleet is a separate,
> fully-decoupled orchestration layer for running *several* companies on one machine; the
> single-company path (init → daily-run → schedule) never touches it. `fleet.py` /
> `fleet-run.sh` live in `scripts/` alongside everything else and are only engaged when you
> explicitly run `schedule.sh install-fleet`. If you run one company, ignore this.

Instead of N independent crons each paying full maintenance every tick, a **parent
company** can drive its sub-companies from a single schedule. `fleet-run.sh` is the
parent's one cron entry; `schedule.sh install-fleet <parent>` installs it (a
`# self-company-fleet project=<key>` line, managed by the same namespaced set as
`daily`/`research` — `list` shows it as `TYPE=fleet`; `install` ⇄ `install-fleet`
are mutually-exclusive ownership modes per project).

- **Registry.** `<parent>/.company/org/subsidiaries.md` — a human-editable table
  (`path | weight | enabled`) of sub project dirs. Adding a sub is data, not code.
  Dead paths (missing `.company/`) are flagged, not fatal.
- **The token win.** Each tick: one auth pre-flight for the whole fleet; the cheap
  deterministic pass (`daily-run.sh <sub> --no-agent`) for **all** live subs; then
  the expensive CONSOLIDATE agent only for subs whose entropy rose or dup-backlog
  exceeds threshold, ranked by `delta × weight`, **hard-capped at
  `FLEET_AGENT_BUDGET`** (policy §7.9, default 3). Healthy subs cost zero agent
  runs. Cost is allocated by need, not `N × blind`.
- **Isolation invariant.** The parent orchestrates scheduling + budget only. It
  invokes each sub's **own** `daily-run.sh` and writes solely under
  `<parent>/.company/ops/` (`fleet-ledger.md`, `fleet-state.json`); it never reads
  or writes a sub's memory/personas. Each sub's `.company/` and `skeleton_guard`
  are untouched. Standalone (`self`-scheduled) companies keep using plain
  `install` unchanged.

---

## Session Catch-Up Notification (Chairman opt-in: "Option B")

The unattended daily cron (`schedule.sh`) runs silently and only writes logs. The
Chairman shouldn't have to dig through logs, so this is now **automated via a
`SessionStart` hook** (declared plugin-natively in `hooks/hooks.json` alongside the
Stop/CAPTURE hook — see "Plugin-native hooks" below):

- On session start the hook runs `notify-status.py --emit-hook`. If there are new
  background runs AND they are **substantive** (entropy or memory count moved,
  something decayed, or there are pending TODOs), it injects a `SessionStart`
  `additionalContext` line telling the agent to send **one** `PushNotification`
  with the summary — **push only, never Discord** (per the Chairman's
  `push-notification-only` preference). The script self-acks, so the same window is
  never pushed twice.
- If nothing substantive changed, it silently acks and emits nothing — zero noise
  on quiet days. This is the gate the Chairman asked for: notify only on real change.

When you receive that `additionalContext`, also state the one-line summary in your
reply — PushNotification suppresses while the Chairman is actively typing (~60s),
so the in-chat line guarantees he sees it even when the push is held back. The
payload also embeds the recent scheduled-work ledger (see below); render it inline
in your reply so the Chairman sees the report here, not just a file path.

Manual fallback (hook absent / ad-hoc check): run `notify-status.py`, and if
`new_runs > 0` push the `summary`, then `notify-status.py --ack`.

This is how the silent local cron reaches the Chairman's phone without Discord or
a cloud agent: the cron does the work; the SessionStart hook relays the summary.

---

## Plugin-native hooks (the single declaration point)

Since **v0.1.2** all hooks ship **with the plugin**: they are declared once in
`hooks/hooks.json` at the plugin root, and Claude Code loads them automatically the
moment the plugin is installed — **no per-repo `install-hook.sh` edit**. Every command
runs the canonical script via `${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/<script>`,
so the wiring survives plugin version bumps with zero stale-path snapshots.

**The 8 registrations across 7 events** (event → matcher → script, per `hooks/hooks.json`).
`SessionStart` fires **two** scripts, so 8 registrations map onto 7 distinct events:

| Event | Matcher | Script | Timeout | What it does |
|---|---|---|---|---|
| `Stop` | — | `capture-trigger.py --company "$CLAUDE_PROJECT_DIR/.company"` | 120s | CAPTURE: cheap real-time memory capture (cooldown-guarded). |
| `SessionStart` | `startup\|resume\|clear\|compact` | `notify-status.py --emit-hook --company …` | 120s | Catch-up push of unattended runs (push only, self-acks). |
| `SessionStart` | `startup\|resume\|clear\|compact` | `hook_schedule_guard.sh` | 120s | Cron self-heal: re-installs this project's crontab tick if its signature drifted or the line is missing (idempotent; see "SessionStart sync + self-heal" above). |
| `UserPromptSubmit` | — | `hook_memory_inject.py` | **30s** | Ask-time memory injection: ranks L2/high-rc L1 by a **fast stdlib** scorer and injects top-k as `additionalContext`. Relevance-gated (injects nothing if nothing scores), token-capped, never blocks. **No fastembed cold-start on this path** (30s cap). |
| `PreCompact` | `auto\|manual` | `hook_precompact_capture.sh` | 120s | Capture-rescue over the pre-compaction transcript before facts are summarized away; reuses the Stop cooldown to de-dup; never blocks compaction. |
| `PreToolUse` | `Bash` | `hook_memory_guard.sh` | 10s | Denies `rm`/`unlink`/`shred`/`rmdir`/`truncate`/`find … -delete`/`mv`-away of any path under `.company/memory/` **or the `.company` store root** (`rm -rf .company` wipes memory too — physical deletion is the decay reap's job, Phase 6). Broadens in-script; emits `permissionDecision` with reason. |
| `PostToolUse` | `Write\|Edit` | `hook_memory_lint.py` | 10s | Validates frontmatter of any `.company/memory/**.md` write (id/tier/status/sources, tombstone vocab); `block`s malformed writes with a reason. Non-memory files untouched. |
| `SessionEnd` | — | `hook_sessionend_verify.sh` | 120s | Runs the deterministic verify pass so this session's fresh captures are source-stamped before the next SessionStart report. Side-effect only; never fails the session. |

> Matcher key is `matcher` (real Claude Code schema). `PreToolUse` matches all `Bash`
> and the guard script itself narrows to the dangerous `.company/memory` reap paths
> plus the `.company` store root — so `rm`, `unlink`, `shred`, `rmdir`, `truncate`,
> `find … -delete` and `mv` are all seen (defense in depth beside the tar floor).

**Global-fire + `.company` opt-in guard.** Plugin hooks fire in **every** repo the
Chairman opens, not just company repos. So every hook script's FIRST action is an
opt-in guard: if `$CLAUDE_PROJECT_DIR/.company` (or `./.company`) does not exist it
`exit 0`s as a silent no-op (no output, no writes). This one marker check is what keeps
the hooks inert in non-company repos — there is no per-hook special-casing.

**`install-hook.sh` is a legacy-cleaner only.** The plugin now owns these hooks, and plugin
hooks **merge** with `settings.json` hooks — so a legacy pre-v0.1.2 `settings.json` entry
would make Stop(capture)/SessionStart(notify) **double-fire**. There is nothing to install
(the old `install` no-op command was removed in Phase 14); the script exists only to clean
that legacy state:

- `install-hook.sh uninstall` → **removes any legacy self-company hook entries** from
  `.claude/settings.json` (marker-based: `self-company-capture` / `self-company-notify`),
  leaving all other settings/hooks byte-untouched. Run this once on any repo that used
  the old installer to stop the double-fire.
- `install-hook.sh status` → reports **plugin-native**, and warns if legacy entries
  still linger.

Post-install the Chairman verifies wiring with `/hooks`; the hooks are simply *there*.

---

## Scheduled-Work Ledger (autoresearch-style report)

The push is a one-liner; the **report** is `ops/reports/ledger.md`, regenerated at
the end of every `daily-run.sh` by `report.py`. Modeled on Karpathy's autoresearch
`results.tsv`: one row per unattended run, a single headline metric (**entropy**,
lower = healthier — the `val_bpb` analog), a verdict, and a one-line description.

```
| run         | entropy ↓  | mem | status | what happened                  |
| 06-29 18:07 | 0.0356 v   | 45  | keep   | verify +14, merged 8 dup, ...  |
| 06-30 06:07 | 0.0400 =   | 40  | flat   | no-op maintenance              |
```

Verdict: `keep` (something substantive moved), `flat` (clean but no change),
`skip` (agent step capped/absent), `fail` (agent errored). Run on demand with
`report.py --company .company` (`--write` to save, `--tsv` for the raw flat file).
This is the artifact the Chairman wakes up to.

### Surfacing the report mid-session (P1)

The SessionStart hook only fires on a **fresh** session, so in a long continuous
session the report never re-surfaces on its own. To fix that, **at the start of
each reply run `notify-status.py --company .company --delta`**; if it prints a
one-line summary (new *substantive* runs since you last surfaced one), lead your
reply with it. It uses a separate `.last_shown` marker (show-once) so it neither
spams nor collides with the SessionStart push. On demand the Chairman can always
say "report" → `report.py`.

### Adjudication ledger — stop re-flagging judged-distinct pairs (Item 7)

`entropy.py` surfaces duplicate/contradiction **candidate** pairs every run.
Many are false positives of the Jaccard/cosine heuristic — two *distinct*
preferences that merely share vocabulary. When Tony/Elon judge such a pair
`distinct`, that verdict is recorded once in **`ops/adjudications.md`** so the
pair stops re-surfacing (and stops inflating `dup_rate`).

**Format** — a single markdown table keyed by the **unordered** pair
`(id_a, id_b)`; entropy sorts the pair before matching, so column order is
irrelevant:

```
| id_a | id_b | verdict | by | date | reason |
|------|------|---------|----|------|--------|
| foo  | bar  | distinct| Tony | 2026-07-03 | different scope |
```

- `verdict` ∈ `{distinct, duplicate}`. entropy acts on **`distinct`**: it omits
  the pair from the surfaced duplicate/contradiction candidate lists AND does
  not count it in `dup_rate` / `contradiction_score`. `duplicate` rows are an
  audit record only (CONSOLIDATE does the actual merge).
- **Additive & auditable** — append rows, never delete one; a superseding
  verdict is a new row.
- **Stale-guard** — if either id no longer exists, the row is inert (ignored),
  never an error.
- **To adjudicate**, just append one table row — no script needed. entropy reads
  it via `--adjudications` (default `.company/ops/adjudications.md`) and reports
  what it applied under the JSON `adjudications` block.

The ledger is seeded with the 10 preference pairs judged `distinct` on
2026-07-03 (the `format-flexible-presentation` / `delegation-…` /
`visual-work-status-…` / `scheduled-work-report-…` /
`rejects-suboptimal-…` cluster).

### Improvement proposals (Tony, every scheduled run)

The scheduled `daily-run.sh` agent step also has **Tony** append one grounded
improvement proposal (or an explicit "no new proposal") to
`ops/plans/proposals-<date>.md` — so the company keeps proposing its own upgrades
even when the Chairman has none (MISSION.md; policy.md §6). **On session entry,
Elon surfaces any pending proposals** from `ops/plans/proposals-*.md` alongside the
report, so they are seen rather than buried in a file.

---

## On-demand views (Chairman asks → render inline)

When the Chairman asks for any of these, run the script and render its output
inline in your reply (don't just point at a file):

| The Chairman says… | Run | Shows |
|---|---|---|
| "report" / "/report" / "scheduled work" | `report.py --company .company` | the scheduled-work ledger |
| "who's working" / "org status" / "是不是 Elon 在做" | `org-status.py --company .company` | which employees acted recently + who is live now |

`org-status.py` is an honest **snapshot** view: it attributes recent activity from
the real logs (daily-run → Tony/Gibby/Elon/Tom, trigger ledger → Phoebe/Bob,
employee `log.md`, and running `claude -p` processes). Interactive chat is
Elon-fronted; the genuinely-separate work is the cron / dispatch / trigger agents
— the view makes that split visible rather than pretending eight daemons are
always busy.

### Live supervisor (`supervisor.py`) — the skill's own live harness

For a **live** tree of employees working (not a snapshot), `supervisor.py` is a
small, skill-owned orchestration harness: it spawns employees as **child
processes** and reads their stdout streams in real time via `select()`, so status
is event-driven and synced with the actual work — the supervisor IS the parent of
the process tree. It is ephemeral (exists only while work runs) and covers ALL
employees (discovered from `org/employees/`, not a hardcoded subset). OOP:
`Employee` / `Worker` / `Supervisor` / `LiveTree`. Status protocol: a worker
prints `@status <phase>` lines as it works; the same protocol serves a simulated
demo worker and a real `claude -p` agent, so the supervisor is host-agnostic.

- `supervisor.py --demo` — simulate all employees live (no LLM).
- `supervisor.py --dispatch '{"phoebe":"...","bob":"..."}'` — real agents.

Honest ceiling: in a real terminal this is a live TUI; viewed remotely in the
Claude app it streams as text (native widgets belong to the host — the one thing
no modular design can replicate). This is deliberately NOT bound to Claude Code.
