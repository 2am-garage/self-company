# Company Charter

## 1. Language Rule

All content in English. Technical terms (pytest, RAG, Playwright, token, cron, hook, etc.) remain in English. Tone: humble and natural, no AI-speak or verbose phrasing.

---

## 2. Entropy KPI

The company uses entropy as a health indicator. Entropy measures four dimensions:

### 2.1 Definition

```
Entropy = w1·dup_rate + w2·contradiction_score + w3·stale_rate + w4·unverified_rate
```

Each weight (all **tunable**, dimensionless, sum to 1.0, for easy reading as a 0–1 total score):

| Dimension | Symbol | Default Weight | Description |
|---|---|---|---|
| Duplication rate | `dup_rate` | `w1 = 0.25` | Count of duplicate pairs with Jaccard similarity ≥ 0.8 / total memories; uses cheap heuristic (§2.1.1), no LLM |
| Contradiction score | `contradiction_score` | `w2 = 0.35` | Count of detected contradictions / total memories; heuristic detects same-id-prefix or high Jaccard with opposing keywords (§2.1.2); contradictions damage trust most, highest weight |
| Stale rate | `stale_rate` | `w3 = 0.20` | Fraction of active memories with decay_score below tier threshold (L0<0.25, L1<0.15, L2 not counted); auto-cleaned by decay, slightly lower weight |
| Unverified rate | `unverified_rate` | `w4 = 0.20` | Memories not confirmed by VERIFY (no `verified_date`) OR with empty/missing sources / total memories; blessed charter seeds are excluded (axiomatic by construction, not transcript-verifiable); violates verify-loop principle but usually small volume |

### 2.1.1 Duplication Detection (heuristic)

Take memory body (strip frontmatter), normalize (lowercase, remove punctuation, collapse whitespace), then compute Jaccard similarity (token set intersection / union). `>= DUP_JACCARD` (default **0.8**) counts as a duplicate pair. Script: `entropy.py`.

### 2.1.2 Contradiction Detection (heuristic)

Rough heuristic:
- Same id-prefix family (slug common head) or high Jaccard (0.5–0.8, related topic)
- And contains opposing keyword pairs (built-in list like `like/dislike`, `want/don't-want`, `prefer/avoid`, `always/never`, `is/isn't`)
- → counts as candidate contradiction pair

Only list candidates for Tony's review, no auto-modification. Script: `entropy.py`.

### 2.2 Goal

After each maintenance cycle, entropy decreases or stays flat. Decrease or flatness indicates effective maintenance; increase triggers diagnostic follow-up.

### 2.3 Measurement Responsibility

Tony (Improvement Engineer) computes and records entropy; reports changes in weekly reports.

---

## 3. Token Budget

The company does not run 24/7; instead, uses layered triggers and budget limits.

### 3.1 Budget Architecture (all **tunable**)

| Item | Default | Description |
|---|---|---|
| **Daily token ceiling** | **20,000 tokens** | CAPTURE real-time not counted; daily consolidate/decay (§5.2) uses this. This is the per-day total shared across `DAILY_RUNS_PER_DAY` runs (§7.7, default 4× a day) |
| **Weekly token ceiling** | **120,000 tokens** | Full weekly VERIFY + entropy measurement (§5.3) uses this |
| **Manual deep cleanup** | **no ceiling** | Chairman has highest priority; Tom still reports usage |

Each period's budget ceiling monitored by Tom (IT/Ops); stop at ceiling, carry balance to next period.

### 3.2 Budget-Aware Degradation

When usage reaches **≥ 80% ceiling**, execute priority degradation:

1. Skip non-critical maintenance (e.g., RAG rebuild, detailed report generation).
2. Run only CAPTURE (record observations) + VERIFY (verify provenance), keep core anti-entropy intact.
3. List deferrable maintenance tasks as backlog for next period.

### 3.3 Token Breaker

Tom is responsible for cutting power when budget is exhausted, and listing stop points and recovery time.

---

## 4. Write Rules

### 4.1 Least-Privilege Principle

- Each agent writes only files in its scope of responsibility (see `writes` field in `org/employees/<name>/context.md`).
- scratchpad.md: private working area, cleared per task; does not accumulate across tasks.
- log.md: performance and activity log, read by July for tuning; others append their own activity records.

### 4.2 Memory Frontmatter Spec

Each memory file (L0/L1/L2) must start with YAML frontmatter with these fields:

```yaml
---
id: <slug>                              # Unique identifier: lowercase + hyphens, e.g. uwe-prefer-async-lang
tier: L0 | L1 | L2                      # Memory tier (working / warm / cold)
owner: Tony                             # Memory written/curated by Tony exclusively
sources: [<source1>, <source2>, ...]    # Provenance: session id, conversation slice location, reference files; for Gibby's verification; must not be empty
created: <ISO date>                     # Memory creation date (YYYY-MM-DD)
last_reinforced: <ISO date>             # Last date memory was reinforced (re-appeared / confirmed by Chairman) (YYYY-MM-DD)
reinforce_count: <int>                  # Reinforcement count (basis for promotion and decay); starts at 1 on creation
decay_score: <float>                    # Decay score (0.0–1.0, higher = fresher); computed and written back by scripts/decay.py
status: active | archived               # File status (active=valid, archived=archived)
---
```

**Decay formula (determined by §7 tunable constants)**:

```
age_days = now - last_reinforced (unit: days, float)
half_life(rc) = HL_BASE × (1 + HL_GROWTH × (rc - 1))
decay_score = 0.5 ^ (age_days / half_life(rc))
```

Implementation: scripts/decay.py computes and writes back periodically; new files may set decay_score=1.0 on creation.

### 4.3 Memory Pipeline Entrance

- Tony: centralized memory write control, no scatter. CAPTURE/ORGANIZE output drafted, then Tony formats to spec.
- Gibby: verifies provenance of each memory. Insufficient sources or no real source found → reject, request re-capture.
- Memories that cannot point back to a real source are never written.

---

## 5. Phoebe Execution Gateway Iron Rules

### 5.1 Core Principle

**All actual hands-on work goes through Phoebe's planning and dispatch (specification + planning) first.** Ensure no steps are missed, no dependency gaps, work order is sound.

### 5.2 Three-Tier Breakdown

| Tier | Responsibility | Decision-Maker |
|---|---|---|
| **Direction** (do or don't) | What's worth doing, prioritize | Elon (CEO) |
| **Hands-on** (how to do, any gaps?) | spec, plan, dispatch, track progress, fill gaps | **Phoebe (PM, execution gateway)** |
| **Execution** (hands on) | Write code / adjust config per plan, verify in loops | Bob / Tom / staff |

### 5.3 Conversation vs. Work Registration

- **Conversation can reach anyone directly** (Chairman can name any employee for direct conversation, see §1c addressing protocol).
- **But work requiring actual hands-on execution must still register with Phoebe** — prevent missing steps, fill gaps, set dependencies, track progress. Named conversation cannot bypass this gateway.

### 5.4 Build Pipeline and Memory Pipeline

Phoebe oversees both pipelines:

1. **Build Pipeline** — Chairman intent → spec/plan (Phoebe output) → Bob builds → Gibby verifies → report back.
2. **Memory Pipeline (v2)** — CAPTURE (cross-department quick observations, Haiku) → ORGANIZE (Phoebe decides placement/tier) → WRITE (Tony writes to markdown) → VERIFY (Gibby traces provenance, loop until clean) → store. See `references/pipeline.md`.

### 5.5 Reporting & Escalation Chain

Reporting flows UP through Phoebe; decisions are gated by task size.

```
Workers (Bob / Tom / Gibby / July)
      │  report to Phoebe after a task (NOT directly to Elon)
      ▼
Phoebe  — sorts / aggregates all task outcomes, reports UP to Elon
      │  with a recommendation and a small/big read
      ▼
Elon    — decides by task size:
            • small task → Elon resolves WITH Phoebe (no Chairman needed)
            • big task   → Elon escalates to the Chairman before acting
```

- **Workers report to Phoebe, not straight to Elon.** Phoebe is the single point
  that collects, de-duplicates, and orders outcomes so Elon sees a clean picture.
- **Phoebe reports up with a recommendation**, including her read of each item as
  *small* (Elon + Phoebe resolve) or *big* (needs the Chairman).
- **Elon holds the size decision.** Small tasks close inside the company (Elon +
  Phoebe). Big tasks — direction shifts, anything hard to reverse or outward-facing
  — are discussed with the Chairman before action.

---

## 6. Upgrade Loop (Company Self-Upgrade)

> **Gated by skeleton immutability.** Any step that edits the skill skeleton
> (SKILL.md / scripts / references / assets / personas) runs ONLY in the skill's
> development repo (`.self-company-dev` marker) or on explicit Chairman order
> (`SELF_COMPANY_ALLOW_SKELETON=1`). Check `scripts/skeleton_guard.sh` first. In a
> usage project the loop still diagnoses and reports, but changes stay inside
> `.company/`. See SKILL.md "Governance: Skeleton Immutability".

Company self-iteration process:

```
Tony diagnoses (high entropy? weak process? poor staff performance? need tool swap?)
       │ write as upgrade proposal
       ▼
Elon adjudicates (do / don't / later)
       │ approves
       ▼
Phoebe plans and dispatches (break down tasks, fill gaps, set dependencies, register)
       │
       ▼
Tom executes (modify skeleton, tune config, add schedule…)
```

### 6.1 Tony vs July Boundary (both improve, different tiers)

| | **Tony · Improvement Engineer** | **July · HR Team Lead** |
|---|---|---|
| Scope of change | Large structural changes: new process, rearchitect, add/swap agents, introduce tools | Daily micro-tuning: tune prompt/persona/performance within existing scope |
| Requires authorization | Needs Elon sign-off (may affect whole company) | No Elon approval needed (routine supervision) |
| Work registration | Via Phoebe's planning and dispatch | July self-tuning (July autonomous) |

### 6.2 Trigger changes must be measured (verify before merge)

The skill's frontmatter `description` IS the trigger — it decides when the skill
activates. Any change to it must be **measured with `scripts/trigger_eval.py`**
against the canonical trigger eval set (`evals/trigger-evals.json` — should-trigger
+ tricky should-not near-misses) BEFORE it merges. Gate: **recall must not drop and
precision must not regress** versus the current description.

Guard against a broken instrument. `trigger_eval.py` measures the REAL installed
skill (does Claude actually invoke the Skill tool for it), not a proxy — first
confirm it works via `--self-test` (the positive control must fire > 0). A harness
that reads every version as ~0 is broken, not a verdict: it makes good and bad
descriptions look identical. (Lesson learned: skill-creator's `run_eval` proxies
the description as a temporary command and floored at ~0 in a headless env, so it
could not discriminate; measure the real skill instead. Also: with only 3
runs/query, queries near the 0.5 threshold flicker within noise — don't chase a
perfect score against sampling noise.)

### 6.3 Proposal approval authority (who signs off Tony's proposals)

Tony's improvement proposals are approved by SIZE, but **only after a mandatory
premise-check** — because auto-generated proposals can rest on stale or wrong
premises (e.g. a 2026-07-02 proposal to prune "retired" memory stubs that did not
exist — the convention is `archived`, and those were already excluded from
metrics).

1. **Premise-check first, always.** Before ANY approval, verify the proposal's
   stated problem is real against the CURRENT state (grep/inspect/measure). No
   approval — by anyone — for a problem that doesn't reproduce. This is the guard
   that stops the company building machinery for phantom problems.
2. **Small proposal → Phoebe approves** and dispatches. Small = contained,
   low-risk, reversible, no direction / skeleton / public-trigger implications.
   This offloads Elon and keeps routine improvement flowing.
3. **Big proposal → Elon adjudicates**, and per §5.5 escalates to the Chairman —
   direction shifts, skeleton or public-trigger changes, anything hard to reverse
   or outward-facing.

Proposer ≠ approver stays true for big items, and the premise-check is independent
of whoever approves.

---

## Appendix A: Memory Tier Reference

### L0 — Working

- Current session temporary captures.
- Unverified, draft state.
- If not reinforced → periodic decay cleanup.

### L1 — Warm

- Project-level, weeks-scale information.
- Appears multiple times or confirmed by Chairman → promotion.
- Periodic decay review.

### L2 — Cold

- Permanent-tier: stable traits, confirmed preferences, identity info.
- No decay, but accepts contradiction detection and updates.
- Subdirectories:
  - `profile/` — identity, background, personality
  - `preferences/` — likes, habits, working style
  - `projects/` — ongoing matters

---

## Appendix B: Memory Verification Principle

When Gibby verifies each memory:

1. **Trace provenance** — are sources fields accurate pointing to real conversation, file, data?
2. **No provenance found** — can't locate real source → reject, request CAPTURE re-capture.
3. **Insufficient provenance** — sources vague or incomplete → request detail.
4. **Memory contradiction** — if clashes with existing memory, mark for Tony to decide merge/keep/discard old.

---

## 7. Memory Pipeline Parameters (Tunable Constants)

This section centralizes all **tunable** values; single source of truth for `scripts/decay.py`, `scripts/entropy.py`, `references/memory-tiers.md`, `references/pipeline.md`. Scripts use built-in defaults if unable to read.

### 7.1 Decay Formula and Constants

**Formula (see §4.2)**: `decay_score = 0.5 ^ (age_days / half_life(rc))`, where `half_life(rc) = HL_BASE × (1 + HL_GROWTH × (rc - 1))`.

| Constant | Default | Meaning | tunable |
|---|---|---|---|
| `HL_BASE` | **7.0** days | half-life when reinforce_count=1; L0 new memory half-life per week | ✓ |
| `HL_GROWTH` | **0.5** | Each additional reinforce extends half-life by 50% of HL_BASE; rc=3→14 days | ✓ |

### 7.2 Decay Thresholds and Actions

| Threshold | Default | Corresponding age (rc=1) | Action | tunable |
|---|---|---|---|---|
| `L0_DROP_THRESHOLD` | **0.25** | ~14 days no reinforce | L0 drop | ✓ |
| `L1_ARCHIVE_THRESHOLD` | **0.15** | ~19 days no reinforce | L1 demote/archive | ✓ |
| `L1_DEMOTE_RC` | **2** | reinforce_count ≤ this | L1 demote back to L0 (else archive); set to 2 because normal L1 promotion has rc minimum 2; setting to 1 makes "demote to L0" impossible | ✓ |
| `REAP_GRACE_DAYS` | **7** | days since `last_reinforced` | archived/defunct file untouched past this grace window is physically dropped in decay.py's `--apply` reap pass (never reaps active; never reaps L2) | ✓ |

**L2 never decays**: L2 memories exempt from decay actions, only accept contradiction detection and updates.

### 7.3 Consolidation Promotion Thresholds

| Threshold | Default | Meaning | tunable |
|---|---|---|---|
| `L0_TO_L1_RC` | **2** | L0 observed/confirmed 2nd time → promote to L1 | ✓ |
| `L1_TO_L2_RC` | **4** | L1 accumulates 4 reinforcements → promote to L2 (stable trait) | ✓ |

Each reinforcement: `reinforce_count++`, `last_reinforced = today`. Promotion "decision" listed by decay.py, "execution" by Tony in WRITE step. L2 must go to one of `profile/ | preferences/ | projects/`.

**Upgrade candidate trigger path (avoid dangling)**:decay.py only outputs `upgrade_candidates` list in JSON, **no auto-file move, no auto-call to Tony**. Execution path:
1. After daily DECAY batch completes, **Tom** reads upgrade candidates from decay.py JSON output, writes "Upgrade Candidates" subsection in `ops/logs/daily-<date>.md`.
2. **Tony** in next CONSOLIDATE/WRITE cycle reads that subsection, executes move + tier change (L0→L1 or L1→L2) for candidates.
3. See `triggers.md §2` daily triggers, [1] CONSOLIDATE and [5] TOKEN-CHECK steps.

### 7.4 Entropy Weights and Heuristic

| Item | Default | Description | tunable |
|---|---|---|---|
| `w1` (duplication) | **0.25** | Duplication is chronic entropy, moderate fraction | ✓ |
| `w2` (contradiction) | **0.35** | Contradiction damages trust most, highest weight | ✓ |
| `w3` (stale) | **0.20** | Stale auto-cleaned by decay, slightly lower weight | ✓ |
| `w4` (unverified) | **0.20** | Violates verify-loop, but usually small volume | ✓ |
| `DUP_JACCARD` | **0.8** | Jaccard similarity ≥ this counts as duplicate | ✓ |
| `DUP_SEM_BAND_LO` | **0.05** | Jaccard lower bound of ambiguous band re-checked by cosine embedding (real paraphrase dups sit below 0.15) | ✓ |
| `DUP_SEM_BAND_HI` | **0.8** | Jaccard upper bound of ambiguous band | ✓ |
| `DUP_COSINE` | **0.82** | Cosine ≥ this = SCORED semantic duplicate (counts in dup_rate + pairs). Do not go below 0.812 | ✓ |
| `DUP_REVIEW_COSINE` | **0.78** | `DUP_REVIEW_COSINE ≤ cosine < DUP_COSINE` = review candidate; surfaced but NOT counted | ✓ |

### 7.5 VERIFY Retry Ceiling

| Constant | Default | Meaning | tunable |
|---|---|---|---|
| `VERIFY_MAX_RETRY` | **2** | Same memory rejected this many times → discard, no re-capture | ✓ |

### 7.6 Token Ceiling (see §3.1)

| Item | Default | tunable |
|---|---|---|
| Daily ceiling | **20,000 tokens** | ✓ |
| Weekly ceiling | **120,000 tokens** | ✓ |
| Degradation trigger | usage **≥ 80%** ceiling | ✓ |

### 7.7 Scheduling Cadence

| Constant | Default | Meaning | tunable |
|---|---|---|---|
| `DAILY_RUNS_PER_DAY` | **4** | Number of daily consolidate/decay batches per day. Default 4 = every 6 hours (00:00 / 06:00 / 12:00 / 18:00). The §7.6 daily ceiling is the **per-day total** shared across these runs; each run's soft budget ≈ daily ceiling ÷ DAILY_RUNS_PER_DAY, and Tom's token breaker enforces the day total. Raising this fights staleness faster (memory consolidates sooner) at higher token cost. | ✓ |
| `CAPTURE_COOLDOWN_MINUTES` | **30** | Per-session CAPTURE throttle. The Stop hook fires on **every reply-stop**, not once per conversation; after a capture attempt for a session, further hook fires for the SAME session within this many minutes are one-line-logged no-ops (no model call, no L0 writes) — see `scripts/capture-trigger.py`. Marker: `ops/.capture-cooldown.json` (small JSON map, auto-pruned); a missing/corrupt marker **fails open** (capture proceeds — never lose a capture to a broken marker). Set to **0** to disable the throttle. | ✓ |

> The daily batch is idempotent (`decay.py --apply` re-run is a no-op on already-disposed memory, verified in the red/blue ledger), so running it 4× a day is safe — extra runs simply catch newly-captured L0 sooner.

### 7.8 Durability (backups + offline-gap damper)

Two tunable constants (Phase 5) keep unattended `--apply` mutation reversible and resistant to a long offline gap. Both are read from this section by `policy_config.py`, the same way as §7.7/§7.9 — add a bold-value table row for the constant to override it; left undeclared here, each falls back to the built-in default noted below.

- **`BACKUP_KEEP`** — durability floor, defaults to 14. Before any mutating (`--apply`) pass, `daily-run.sh` snapshots `memory/` to `ops/backups/mem-<ts>.tar.gz` and prunes to the newest few archives (this many), so one bad `--apply` (or a buggy edit) is always recoverable from the last-good snapshot. Set to zero to disable snapshotting entirely (mutating passes then proceed with **no** floor).
- **`OFFLINE_GAP_DAYS`** — offline-gap damper, defaults to 7. If the gap since the last successful `--apply` run exceeds this many days, `decay.py` clamps the effective "now" to the last run plus this window, so a machine that was off for weeks doesn't over-decay the whole corpus on the first tick back. The damper only ever *reduces* elapsed decay; a missing marker simply means it doesn't engage (never raises).

### 7.9 Fleet (holding company)

**PARENT-company constant** — applies only to a holding company that schedules its subsidiaries with `schedule.sh install-fleet` and orchestrates them via `scripts/fleet-run.sh`. A standalone (self-scheduled) company never reads it. The isolation invariant still holds: the parent orchestrates SCHEDULING + BUDGET only and never reads/writes a sub's `.company/` except by invoking that sub's own `daily-run.sh`.

| Constant | Default | Meaning | tunable |
|---|---|---|---|
| `FLEET_AGENT_BUDGET` | **3** | Max subsidiaries that get the expensive headless CONSOLIDATE agent per fleet tick. `fleet-run.sh` gives EVERY live sub the cheap deterministic pass (reinforce→decay→verify→entropy→report), then spends the agent only on the top-K subs ranked by need (entropy delta × weight), where K = this budget. This is the fleet-wide cost ceiling — healthy subs cost zero agent runs; deferred subs are logged and picked up next tick. Raise to consolidate more subs per tick at higher token cost. | ✓ |

---

## 8. RAG Tunables (Tony's Domain)

Retrieval-Augmented Generation (RAG) enables semantic search over memory. The index is a derivative of markdown truth; always rebuildable. Wired into the pipeline as of Phase 13 (daily index refresh + ask-time semantic injection), but the local venv ships uninstalled — the semantic path activates once you run `bash .company/scripts/rag_setup.sh install`, which creates a private venv (`.company/.rag-venv`) and installs **LanceDB + fastembed** (local CPU embeddings, no daemon, fully offline); until then the company uses the keyword floor. Tony owns building/maintaining the index; Tony and Gibby query it. See `references/rag.md`.

| Constant | Default | Meaning | tunable |
|---|---|---|---|
| `RAG_ENABLE_THRESHOLD` | **50** | L1+L2 active memory count at or above which RAG is worth enabling; below 50, full-text grep over `.company/memory` is faster and cheaper. L0 excluded (volatile). 50 is the inflection point where semantic recall beats keyword search and volume exceeds human eyeballing. | ✓ |
| `RAG_MODEL` | **`BAAI/bge-small-en-v1.5`** | fastembed embedding model, local CPU, runs fully offline; 384-dim, good quality/size trade-off. No API embeddings, no daemon — privacy is a hard rule; memory content never leaves the machine. (Legacy Ollama `nomic-embed-text`/768-dim is superseded.) | ✓ |
| `RAG_INDEX_PATH` | **`.company/memory/index`** | LanceDB vector store location; matches reserved folder in design §2. Gitignored and private; index is rebuildable from markdown, not a source of truth. | ✓ |

### 8.1 Graceful Degradation

RAG degrades to the keyword floor when the venv is absent:
- RAG backend not installed (no `.company/.rag-venv`) → scripts exit code 2 with an actionable message pointing to `rag_setup.sh install`; the ask-time hook falls back to keyword injection.
- Never crash the company; never raise uncaught tracebacks.
- `rag_query.py` unavailable → stderr hint fallback: `grep -ri '<keywords>' .company/memory`.

---

Version: v5 (RAG wired, venv-gated; §7.7 scheduling cadence + CAPTURE throttle; §7.8 durability: BACKUP_KEEP + OFFLINE_GAP_DAYS; §7.9 fleet: FLEET_AGENT_BUDGET)  
Last updated: 2026-07-07
