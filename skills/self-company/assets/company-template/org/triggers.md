# Trigger Matrix

A comprehensive overview of trigger timing, participants, actions, and token budgets across the company's hierarchy.

> **Important Declaration**: The **hooks** in this file (Stop CAPTURE, catch-up, etc.) are **plugin-native since v0.1.2** — Claude Code loads them automatically on install (`hooks/hooks.json`), and a `.company` opt-in guard keeps them inert in non-company repos, so no per-repo install step is needed. **Cron scheduling** (daily/weekly) is still opt-in and requires **explicit Chairman approval before Tom installs it**.

---

## Overview Table

| Trigger Level | Timing | Who Works | Core Action | Default Model | Parallel / Serial |
|---|---|---|---|---|---|
| **Real-Time** | After each conversation | Cross-dept (Haiku) + Gibby (Sonnet) | CAPTURE observations → quick VERIFY → write L0 | Haiku + Sonnet | Parallel: capture; Serial: → verify → write |
| **Daily** | Scheduled trigger | Tony (Sonnet) + Gibby (Sonnet verifies sources) + Tom watches budget | consolidate, decay, write, verify, upgrade-candidate handoff | Sonnet | Serial: consolidate → decay → write → verify |
| **Weekly** | Scheduled trigger | Tony + Gibby + Phoebe + July (Sonnet) | Full verification, entropy measurement, performance tuning, report generation (the RAG index is refreshed automatically each DAILY run — Phase 13 A.1) | Sonnet | Mostly parallel; verify → report serial |
| **Manual** | Chairman explicitly triggers | Elon (director) + all staff (Opus) | deep cleanup, reorganization, cross-tier review, build pipeline | Opus | Case-by-case (typically spec → build → verify serial) |

---

## 1. Real-Time Trigger

### Timing

Each time the Chairman finishes a conversation with the company (detected by Stop hook; see "Implementation Mechanism" below).

### Who Works

1. **Cross-dept Squad** (Haiku): Participants in the current conversation capture observations sentence by sentence → L0 draft.
2. **Gibby (Sonnet)**: Quick verification that sources are traceable and no obvious contradictions → push to VERIFY queue.

### Action Breakdown (Process Overview)

Three steps chained in series across stages (with dependencies):

- **[1] CAPTURE** (cross-dept, Haiku) — Capture observations about the Chairman during work → draft into L0. Output: raw observations with initial sources. See `references/pipeline.md`.
- **[2] ORGANIZE** (Phoebe, Sonnet) — Lightweight real-time version: compare against existing memories, decide new / update / conflict; hand off to Tony for landing. See `references/pipeline.md`.
- **[3] WRITE** (Tony, Sonnet) — Organize draft into standard frontmatter (see `policy.md §4.2`), write to L0. See `references/pipeline.md`.
- **[4] VERIFY** (Gibby, Sonnet) — Quick check that sources are traceable and no obvious contradictions; Pass → push to daily VERIFY queue, Reject → send back to CAPTURE. See `references/pipeline.md`.

### Token Budget

Real-time CAPTURE (Haiku) tokens **count toward the daily ceiling** (no separate exemption); however, real-time triggers themselves are not blocked by ceiling exhaustion — CAPTURE after conversation end runs normally and is not stopped by hitting the day's budget limit. Tom monitors; if sustained overages occur (e.g., marathon session accumulation), he alerts.

### Implementation Mechanism — Stop Hook (plugin-native since v0.1.2)

Real-time CAPTURE is wired as a **Claude Code `Stop` hook**. Since **v0.1.2** it is
**plugin-native**: declared once in `hooks/hooks.json` at the plugin root and run via
`${CLAUDE_PLUGIN_ROOT}/.../capture-trigger.py`, so Claude Code loads it automatically on
install — there is **no per-repo `install-hook.sh` edit** and no `settings.json` wiring to add.

`capture-trigger.py` reads the transcript path from the hook's stdin JSON and runs Haiku
CAPTURE → L0 drafts. Recursion-guarded (`stop_hook_active` + guard env), cooldown-throttled,
and degrades to a clean no-op if anything is missing.

**Global-fire + opt-in guard.** Plugin hooks fire in **every** repo the Chairman opens, so
the hook's first action is an opt-in guard: no `$CLAUDE_PROJECT_DIR/.company` marker → silent
`exit 0`. That single check keeps it inert in non-company repos, so no explicit "enable"
step is required.

> **`install-hook.sh` is deprecated.** `install` is a no-op ("hooks are plugin-native since
> v0.1.2 — nothing to install, see `hooks/hooks.json`"); `uninstall` only removes legacy
> `.claude/settings.json` hook entries left by the pre-v0.1.2 installer (which would otherwise
> **double-fire** against the plugin hooks); `status` reports plugin-native. See
> `references/operations.md` for the full hook table.

**Companion hooks.** Beyond `Stop`, the plugin now ships six more hooks: `SessionStart`
(catch-up push of unattended runs), `UserPromptSubmit` (ask-time memory injection),
`PreCompact` (capture-rescue before compaction), `PreToolUse` (deny `rm` under
`.company/memory`), `PostToolUse` (lint memory writes), and `SessionEnd` (verify fresh
captures) — **7 plugin-native hooks** in total.

**Note**: CAPTURE can also be triggered manually (conversation prefix `(cross-dept)` or
Phoebe manually initiating).

---

## 2. Daily Trigger

### Timing

Scheduled batch runs **`DAILY_RUNS_PER_DAY` times a day** (default **4×**, every 6 hours: 00:00 / 06:00 / 12:00 / 18:00 local time); count and times adjustable by Chairman via `policy.md §7.7`. The batch is idempotent, so the extra runs simply consolidate newly-captured L0 sooner and re-running on already-disposed memory is a no-op. The §7.6 daily token ceiling is a per-day total shared across these runs (Tom's token breaker enforces the day total, not each run).

### Who Works

- **Tony (Sonnet)**: Main driver; organize L0 → dedup → decay assessment → upgrade decision → land WRITE.
- **Gibby (Sonnet)**: Verify source of each day's WRITE output (VERIFY loop until clean), clear pending_verify.
- **Tom (Sonnet)**: Beside him watching budget; if today's usage approaches ceiling, stop this round; at batch end write upgrade-candidate JSON to log.

### Action Breakdown (Process Overview)

Five steps: Tony as main driver, Gibby guards provenance, Tom watches budget:

- **[1] CONSOLIDATE** (Tony, Sonnet) — Read L0 new memories, compare against L1/L2, decide new / update (reinforce) / mark contradiction. **Also read upgrade candidates from previous round's `decay.py` output**: Tom has written `upgrade_candidates` JSON to the previous day's `ops/logs/daily-<date>.md` "upgrade candidates" section; Tony in this round executes file move + tier change on these memories. See `references/pipeline.md` stage [2] ORGANIZE.
- **[2] DECAY** (Python script) — Execute `python3 .company/scripts/decay.py --apply`: calculate decay_score, apply tier-based disposition for those exceeding threshold (L0 delete / L1 demote or archive / L2 retain). Upgrade candidates are only listed in the `upgrade_candidates` JSON, not auto-moved. See `references/memory-tiers.md`.
- **[3] WRITE** (Tony, Sonnet) — Land decisions: new / update / conflict adjudication + execute upgrade file move, write standard frontmatter markdown. See `references/pipeline.md` stage [3] WRITE.
- **[4] VERIFY** (Gibby, Sonnet) — For each day's WRITE output, trace sources; Pass writes to log (add verified_date / verified_by), Reject sends back to CAPTURE for re-capture (max 2 retries then discard, see `policy.md §7.5`). Loop until clean; memories written on this day do not remain in pending_verify state. See `references/pipeline.md` stage [4] VERIFY.
- **[5] TOKEN-CHECK** (Tom) — Compare daily usage against budget ceiling (see `policy.md §3.1`), alert when approaching, stop subsequent tasks when exhausted. At batch end, write this round's `decay.py` `upgrade_candidates` JSON to `ops/logs/daily-<date>.md` "upgrade candidates" section for Tony's next CONSOLIDATE/WRITE round.

### Output

Update `ops/logs/daily-<date>.md` record of this round's changes:
- Which memories added (ID, tier)
- How many decayed away (count)
- Upgrade / demote / conflict (ID lists)
- VERIFY result (Pass / Reject / discard count)
- Upgrade candidates (this round's `decay.py` output for next round to execute file move)
- Token usage

### Implementation Mechanism — Cron + `/schedule` Proposal

**[Proposal]** Use Claude Code's `/schedule` skill or `CronCreate` tool to periodically trigger the daily batch.

**Implementation**:
1. **Option A (Recommended): Use `/schedule` skill**
   - **Ships in the skill (recommended):** `bash .company/scripts/schedule.sh install` installs an OS crontab entry `7 */6 * * *` (4× a day, off-minute) that runs `daily-run.sh`. `schedule.sh uninstall` / `status` manage it; idempotent. Local + unattended — runs whenever the machine is on, memory never leaves the box.
   - `daily-run.sh` does the deterministic core every run (`decay.py --apply` + `entropy.py`, logged, no tokens) plus an optional bounded headless `claude -p` consolidate/verify pass (hard timeout + recursion-guarded; `--no-agent` skips it).
   - Cron `7 */6 * * *` ≈ 00:07 / 06:07 / 12:07 / 18:07, matching `DAILY_RUNS_PER_DAY=4` (`policy.md §7.7`). NOTE: `/schedule` and `CronCreate` are **session-bound** (only fire while a Claude REPL is running idle), so they are NOT used for unattended 2am runs — the OS cron above is.

2. **Option B: Use `CronCreate` tool**
   - Directly call CronCreate to set up cron job; command same as above.

3. **Token Breaker (Tom watches)**
   - `daily-run.sh`'s deterministic core (decay + entropy) spends no tokens; only the optional agent step does. A future token-ceiling gate (read `policy.md §3.1`, default 20,000) would skip the agent step when usage is high — for now the agent has a hard timeout and `--no-agent` is available.
   - If already used ≥ 80% ceiling → only run DECAY script, skip Tony's CONSOLIDATE/WRITE.
   - If already used ≥ 100% ceiling → stop, write alert to log.

**When to Enable**: Chairman must explicitly approve before Tom runs `/schedule` or `CronCreate`. Automation is **not pre-installed by default**.

**Note**: Cron minute/cadence is tunable (`SELF_COMPANY_CRON_MIN` env; `DAILY_RUNS_PER_DAY` in `policy.md §7.7`); Tom maintains `scripts/daily-run.sh` + `scripts/schedule.sh` and the token accounting logic.

---

## 3. Weekly Trigger

### Timing

Scheduled to run at a fixed day and time each week (default **Monday 02:00 local time, off-peak**); day and time can be adjusted by Chairman.

### Who Works

1. **Tony (Sonnet)**: Diagnose entropy, review the daily RAG-index refresh, overall memory inventory.
2. **Gibby (Sonnet)**: Full memory source verification, random sampling of build pipeline.
3. **Phoebe (Sonnet)**: Final ORGANIZE memory decision, plan state review.
4. **July (Sonnet)**: Five-staff performance review, recommend enable/disable adjustments.
5. **Tom (Sonnet)**: Infra state check, backup, completeness verification.

### Action Breakdown

```
[1] FULL-VERIFY (Gibby, Sonnet)
    - All memories added/modified last week (L0→L2) verified individually
    - Trace each source → real provenance (conversation excerpt, session id, file reference)
    - Can't trace source → reject, send back to CAPTURE for re-capture (loop until clean, max 2 retries then discard)
    - Contradictions found → mark for Tony decision
    - Passing ones record verification time + signature
    - See `references/pipeline.md` stage [4] VERIFY

[2] ENTROPY-REPORT (Python script + Tony, Sonnet)
    - Execute `python3 .company/scripts/entropy.py`: calculate this week's Entropy metrics
    - Four dimensions (duplication / contradiction / stale / unverified): specific values and percentages (see `policy.md §2.1` & `references/memory-tiers.md`)
    - Compare vs. last week: down / flat / up?
    - If up → Tony diagnoses root cause

[3] PERFORMANCE-REVIEW (July, Sonnet)
    - Read `ops/logs/` activity records for all staff this week
    - Evaluate Bob / Gibby / Tony / Tom performance (delivery quality, speed, collaboration)
    - Which staff should have prompt adjusted / suspended / re-enabled?
    - Write recommendations to Elon (approval) and Phoebe (dispatch)

[4] INFRA-CHECK (Tom, Sonnet)
    - `.company/` skeleton file completeness
    - Backup state
    - Scheduled runs missing?
    - Token weekly accumulated usage vs. ceiling (see `policy.md §3.1`)
    - Forecast budget remaining for next week

[5] RAG-INDEX REFRESH (Tony) — now AUTOMATIC in the daily core, not a weekly step
    - The LanceDB index is refreshed INCREMENTALLY every daily run by `daily-run.sh`,
      after reinforce+decay+verify+entropy (Phase 13 A.1) — no separate weekly rebuild.
      It runs `python3 .company/scripts/rag_index.py` (L1/L2 only), skipping unchanged
      bodies via content_hash.
    - Activation is auto-surfaced: `rag_index.py --threshold-check` (deps-free) runs each
      daily and, when active L1+L2 ≥ 50 while the RAG stack is not installed, logs an
      "activate RAG" candidate (Phase 13 A.2). Below threshold → nothing surfaced.
    - Deps: the fastembed venv (`bash .company/scripts/rag_setup.sh install`). Absent OR
      broken venv → one logged skip line; the deterministic core always completes.
    - See `references/rag.md` §2/§4 for details. (Semantic query consumption —
      ask-time injection — is Stage B, upcoming, not yet wired.)

[6] LOG-COMPILE (Tony, Sonnet)
    - Aggregate all week's changes (new / upgrade / decay / verify result)
    - Write weekly log → `ops/logs/weekly-<date>.md`
    - Content: this week's new memory summary, decay count, contradiction adjudication, entropy values, staff status, token usage
    - [v2 current] This version does not auto-send report; merely produces log; v3+ can add proactive notification to Chairman
```

### Output

`ops/logs/weekly-<date>.md` weekly log:
```yaml
---
week_start: <Monday ISO date>
entropy_score: <calculated value>
entropy_delta: <change vs. previous week>
memory_added: <new memory count>
memory_upgraded: <tier upgrade count>
memory_decayed: <decay delete/archive count>
verify_rejects: <reject pending re-capture count>
team_status: |
  - Bob: <activity summary>
  - Gibby: <verify pass rate>
  - Tony: <decision count>
  - Tom: <scheduling execution status>
token_usage:
  daily_ceiling: <daily usage limit>
  weekly_ceiling: <weekly usage limit>
  week_used: <actual weekly usage>
  remaining: <estimated remaining>
---

## Memory Overview

This week's new memories (id + tier + sources + content summary)

## Decay Disposition

L0 delete / L1 demote archive / tier upgrade records

## Contradiction Detection

Contradictions found and Tony's adjudication (merge / retain / drop old)

## Entropy Analysis

Tony diagnosis: four-dimension values (duplication / contradiction / stale / unverified), vs. last week, anomaly root cause

## Staff Performance

July evaluation: each staff this week, recommendations for adjustment (suspend / enable / tune prompt)

## Next Week Forecast

Tom report: expected token usage, scheduled runs, risks to watch
```

### Implementation Mechanism — Cron + `/schedule` + Parallel Coordination Proposal

**[Proposal]** Use `/schedule` skill or `CronCreate` to periodically trigger; use file locking + logging for multi-person coordination.

**Implementation**:
1. **Scheduling trigger** (same as daily proposal)
   - Cron expression `0 2 * * 1` = every Monday at 02:00 (local time).
   - Command: `cd /home/uwe/2am-garage && python3 scripts/weekly-consolidate.py --verify --entropy`.
   - **Note:** `weekly-consolidate.py` is **not yet authored** (Tom writes it on activation). The pieces it orchestrates — `entropy.py` and `rag_index.py` — ship and run today and can be invoked directly in the meantime.

2. **Parallel coordination mechanism**
   - Five tasks (FULL-VERIFY / ENTROPY-REPORT / PERFORMANCE-REVIEW / INFRA-CHECK / LOG-COMPILE) are largely independent, can run in parallel.
   - Use simple **file locking**: `.company/ops/locks/weekly-<date>.lock` ensures only one run per week.
   - Each agent subprocess writes output to its own `.company/ops/logs/weekly-<date>-<name>.json`; Tony aggregates at the end.

3. **Manual coordination**
   - If a subprocess blocks, Phoebe monitors log progress; Tom provides scheduling status.
   - Weekly report not auto-pushed (v2 decision); results in `ops/logs/` await Chairman or Elon's review.

**When to Enable**: Chairman must explicitly approve before Tom sets up scheduling. Automation is **not pre-installed by default**.

**Note**: the RAG index refresh is wired into the daily core (Phase 13 A.1; ships dormant, activated with `rag_setup.sh install` — fastembed + LanceDB, no Ollama). Report push mechanism (PushNotification / Discord) deferred to v3+.

---

## 4. Manual Trigger

### Timing

Explicitly initiated by Chairman, such as "I want to reorganize company memory" or "build X feature" or "check company health".

### Who Works

- **Elon (CEO, Opus)**: Director; survey the whole, adjudicate major adjustments, mobilize all staff.
- **All staff** (as needed): Execute build / repair per Elon's dispatch.

### Action Breakdown

Varies by trigger type:

#### 4a. Deep Cleanup (Chairman: reorganize memory)

```
[1] BRIEF (Chairman → Elon)
    Express intent: "memory is messy" / "a preference got buried" / "need to clean files"

[2] PLAN (Elon → Phoebe)
    Adjudicate scope + goal → Phoebe produces plan (which tiers / which IDs involved)

[3] EXECUTE (Tony + Gibby)
    - Tony: perform deep cleanup
      ├─ Identify duplicate pairs (using entropy.py Jaccard heuristic)
      ├─ Identify contradictions (slug same family or opposing keywords)
      ├─ Identify stale (decay_score extremely low but not yet decayed)
      └─ List candidates for Phoebe / Elon approval
    - Gibby: verify each source one by one, can they merge?
    - All staff provide perspective (especially Bob / July on code / people dimensions)

[4] CONSOLIDATE & MERGE (Tony, Sonnet)
    - Execute merge / delete / tier change
    - Write change log to `ops/logs/manual-cleanup-<date>.md`

[5] VERIFY (Gibby, loop)
    Full verification of post-cleanup memory
    loop until clean

[6] REPORT (Elon)
    Present to Chairman before/after comparison, entropy shift, new memory structure
```

#### 4b. Build Pipeline (Chairman: build something)

```
[1] BRIEF (Chairman → Elon → Phoebe)

[2] SPEC (Phoebe)
    Produce detailed spec + plan

[3] BUILD (Bob, Sonnet → Opus)
    Write code / files per plan

[4] CHALLENGE (Gibby, loop until clean)
    pytest / run code / Playwright / lint / diff / memory source

[5] REPORT (Phoebe / Elon)
    Deliver to Chairman + risk disclosure
```

#### 4c. Organization Adjustment (Chairman / Elon: change persona / tools / process)

```
[1] PROPOSAL (Tony diagnoses → propose to Elon)
    Write upgrade proposal (markdown format, see policy.md §6 upgrade loop)
    Cover: current pain / proposal / expected benefit / scope of impact

[2] DECISION (Elon)
    Do / don't / later
    If do → assign to Phoebe

[3] PLANNING (Phoebe)
    Break into concrete tasks, confirm dependencies and people
    Register with Tom (infra) / July (persona tuning) / all staff (process adaptation)
    Ensure no missing steps

[4] EXECUTION (Tom + designated staff)
    - Tom: change skeleton / config / scheduling (including new hook / cron requiring Chairman approval)
    - July: tune persona / prompt / performance metrics
    - All staff: adapt to new process

[5] VERIFY (Gibby)
    Test new config takes effect, no side effects (run pytest / actual operation)

[6] REPORT (Elon / Phoebe)
    Explain new state to Chairman, benefit assessment, meets expectations?
```

### Budget

Manual triggers are not limited by daily / weekly ceiling (Chairman is highest priority), but Tom still monitors and reports token usage.

### Implementation Mechanism — Manual Trigger Initiation (Already Implemented)

**Chairman's ways to initiate**:

1. **Conversation Prefix (addressing protocol, already implemented)**
   - No prefix → default to talking to Elon (CEO); Elon can dispatch after receiving.
   - `(Elon) I want to...` → explicit name, Elon prioritizes starting deep cleanup / adjudication flow.
   - `(Phoebe) ...` / `(Tony) ...` → name different role, each responds.

2. **Active Signal**
   - State explicitly: "I want to reorganize memory" "check company health" "adjust someone" "build X feature" etc.
   - Elon recognizes trigger intent, launches corresponding big flow (deep cleanup / diagnosis / org adjustment / build).

3. **Future Option (v3+)**
   - Slash command `/invoke-elon`, `/cleanup-memory`, `/build` etc. quick entry.
   - v2 not yet implemented; rely on natural language + Elon's judgment.

---

## Parallel / Serial Rules

### Within Same Level Parallelization

**Real-Time Level**: Cross-dept CAPTURE can run in parallel (each person records independently); VERIFY single Gibby runs serially.
**Daily Level**: CONSOLIDATE → DECAY → WRITE → VERIFY runs serially across stages (with dependencies: WRITE awaits DECAY's upgrade candidates, VERIFY awaits WRITE landing); TOKEN-CHECK (Tom) guards token count at the end.
**Weekly Level**: VERIFY / ENTROPY-REPORT / PERFORMANCE-REVIEW / INFRA-CHECK tasks are largely independent, can run in parallel; REPORT-COMPILE runs last serially. (The RAG index refresh is automatic in the daily core — Phase 13 A.1 — not a weekly task.)

### Cross-Level Serialization

- Real-Time WRITE → Daily CONSOLIDATE (L0 draft already organized).
- Daily decay decision → Weekly VERIFY (verify whether last week's demotes were appropriate).
- Weekly report → Manual trigger (if building, report is briefing input).

---

## Monitoring and Exception Handling

**Common anomalies and disposition**:

1. **Real-Time Explosion (marathon session)**
   - Symptom: single conversation exceeds 50,000 tokens, CAPTURE drafts accumulate.
   - Disposition: Tom monitors and alerts → Phoebe decides: delay ORGANIZE to next day, or have Tony cherry-pick key points for WRITE on the spot.
   - Note: budget still counts toward daily / weekly ceiling.

2. **High VERIFY Reject Rate**
   - Symptom: Gibby rejects >30% of submissions each round.
   - Diagnostician: Tony. Possible causes: (a) CAPTURE quality poor (too broad, sources fuzzy); (b) verification standard too strict (Gibby over-zealous).
   - Adjustment: July and Gibby discuss standard, or improve cross-dept CAPTURE training.

3. **Scheduling Delay**
   - Symptom: daily / weekly schedule fails to start on time.
   - Tracker: Tom. Cause: (a) schedule not running; (b) prior task not finished (file lock stuck); (c) budget exhausted.
   - Disposition: Tom re-triggers or delays to next window; notify Phoebe.

4. **Decay and Upgrade Misjudgment**
   - Symptom: memory that shouldn't be deleted is deleted by decay.py, or upgrade misjudged.
   - Cause: Usually constants in policy.md set incorrectly.
   - Adjustment: After Chairman approval, Tony updates `policy.md §7`; decay.py / entropy.py re-read from policy.

5. **Contradiction Detection False Positive**
   - Symptom: entropy.py flags heuristic contradictions, but they're not actually contradictory (false positive).
   - Disposition: Tony notes in entropy report, judged as false alarm; don't auto-merge files.
   - Improvement: can micro-tune DUP_JACCARD or contradiction-keyword list (see policy.md §7).

---

## Trigger Mechanism Overview

| Trigger Level | Detection | Tool | Status |
|---|---|---|---|
| **Real-Time** | `Stop` hook (session end) | Plugin-native (`hooks/hooks.json`) | **Plugin-native since v0.1.2**; auto-loads on install, `.company`-guarded |
| **Daily (4×/6h)** | Cron / `/schedule` | CronCreate or `/schedule` skill | **Pending approval**; not pre-installed |
| **Weekly Mon 02:00** | Cron / `/schedule` | CronCreate or `/schedule` skill | **Pending approval**; not pre-installed |
| **Manual** | Natural language / prefix | Elon judgment + Phoebe dispatch | On-the-fly decision (Chairman says ok) |

---

Version: v2.5 (memory pipeline + RAG index wired into the daily core, ships dormant)  
Built by: Haiku (Claude Code)  
Last updated: 2026-06-25
