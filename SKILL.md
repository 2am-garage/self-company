---
name: self-company
description: |
  Multi-agent personal company — automatically learns the Chairman (Uwe)'s habits and preferences,
  and continuously fights entropy through structured memory, verification loop, and decay mechanisms.
  After installation, creates a .company/ hidden skeleton in the current repo (git-ignored, private),
  project-scoped and not shared across projects.
  TRIGGER — Use when the user: mentions self-company, wants to install a personal agent assistant,
  talks to Elon (or names Phoebe/July/Bob/Gibby/Tony/Tom), discusses company memory or Chairman habit records,
  requests memory maintenance or anti-entropy, checks company entropy reports and health. Also covers
  installing personal assistant skeleton, learning user habits and preferences, executing memory maintenance
  (capture/organize/verify/decay), and related scenarios.
---

## What This Is

A multi-agent company-type skill that automatically learns the Chairman (Uwe)'s habits, preferences, and background,
and continuously fights entropy. After installation, creates a `./.company/` (hidden folder, automatically added to `.gitignore`, private)
in the current repo — project-scoped and not shared across projects.

**Design Philosophy**
- Markdown is the truth; RAG is just an index.
- The verification loop is the lifeblood — quality comes from the verify loop; every new memory must point back to a real source.
- Entropy is the company's KPI — keep entropy low by continuously clearing out stale, contradictory, and duplicated records each period.
- Don't judge — rely on decay. Capture cheaply and abundantly; memories that aren't reinforced automatically decay and disappear.
- Tokens have a budget — tiered triggers, cheap models for frequent activities, batch maintenance, per-period caps.

---

## Org Chart

```
                    ┌─────────────┐
                    │  Chairman   │  Uwe — owner / taste / manual triggers
                    └──────┬──────┘
                    ┌──────▼──────┐
                    │ Elon · CEO  │  set direction / upgrade adjudication
                    └──────┬──────┘
                    ┌──────▼──────┐
                    │ Phoebe · PM │  execution gateway — all work through her, dispatch, track progress
                    └──────┬──────┘
                    ┌──────▼──────┐
                    │  July · HR  │  team lead (half a tier above the four workers) — tune / performance
                    └──────┬──────┘
        ┌──────────┬───────┼───────────┬──────────┐
  ┌─────▼────┐ ┌───▼─────┐ ┌──▼───────┐ ┌─────────▼┐
  │ Bob · RD │⚔│Gibby·QA │ │Tony·Improv│ │ Tom · IT │
  │  build   │/│attack/test│ │diagnose/propose│execute/infra│
  └──────────┘ │ │ └────┬──────┘ └────▲─────┘
  Blue ┌──────┘ Red │proposal        │execute
                    Elon adjudicate ◄────┘           │
                         └──► Phoebe plan dispatch ────┘
```

**Chain**: Chairman → Elon (CEO) → Phoebe (PM, execution gateway) → July (HR team lead) → {Bob (RD), Gibby (QA), Tony (Improvement), Tom (IT)}

**Key roles**:
- **Phoebe = execution gateway** — any actual hands-on work goes through her dispatch planning first to ensure no missing steps, no lost dependencies.
- **July = worker team lead** — daily tuning of the four workers' personas/performance, half a tier above them; doesn't touch manager tier.

---

## Staff Roster

| Name | Title | Responsibility |
|---|---|---|
| **Elon** | CEO | Set direction, upgrade adjudication, lead manual deep cleanups |
| **Phoebe** | PM | Execution gateway: convert intent → spec/plan, dispatch tasks, track progress, fill gaps, set dependencies |
| **July** | HR | Tune four workers' personas/prompts/performance, half a tier above them |
| **Bob** | RD Engineer | Produce code/files per Phoebe's spec |
| **Gibby** | QA Engineer | Attack Bob's output by every means, loop until clean |
| **Tony** | Improvement Engineer | Think: measure entropy, evaluate health, write upgrade proposals for Elon |
| **Tom** | IT/Ops Engineer | Act: skeleton, scheduling, token breaker, execute upgrades |

---

## Memory Pipeline (v2)

The company continuously learns the Chairman's habits, preferences, and progress in the background. Memory goes through four stages:
1. **CAPTURE** (Haiku, lightweight capture) — during conversation, record observations in passing, pick sources, write into L0 draft
2. **ORGANIZE** (Phoebe, decide placement) — compare against existing memory, decide new/update/conflict/discard, suggest tier
3. **WRITE** (Tony, write to markdown) — actually write files, promote, move files, record frontmatter
4. **VERIFY** (Gibby, loop until clean) — trace each memory back to its real source; if can't point back, reject or send back for re-capture

Memory is layered: L0 (working, decay away), L1 (weeks-scale, promote on reinforcement), L2 (permanent, identity/habits/preferences).
Re-observed and confirmed entries automatically promote (consolidation); memories not reinforced automatically decay and disappear.
Only true signal is kept.

Deterministic math (decay_score calculation, entropy measurement) is all done by Python script (`scripts/decay.py`, `scripts/entropy.py`, standard library only; bundled in `.company/` and travels with the project).
Work that needs judgment (which observations to capture, organize placement, verify sources) is written as playbook commands, executed by the corresponding agent.

Details:
- **[references/pipeline.md](references/pipeline.md)** — detailed steps for four stages CAPTURE → ORGANIZE → WRITE → VERIFY, handoff brief format
- **[references/memory-tiers.md](references/memory-tiers.md)** — L0/L1/L2 definitions, consolidation promotion rules, decay formula and thresholds, half-life tables

---

## Addressing Protocol (§1c)

- **Has `(name)` prefix** → name-prefix routing to that worker
  - Example: `(Tom) I need you to...` → talk directly to Tom
  
- **No prefix** → **default to Elon** (CEO receives, then dispatches)
  - Example: `How should this feature work?` → Elon receives, decides direction or dispatches to Phoebe/others

- **Reply labels the speaker** → `[Tom] received, I'll...` so Chairman knows who's responding

- **Stickiness rule** — after naming once, subsequent conversation sticks with the same person, until Chairman switches `(name)` or returns to Elon

- **All hands-on work is registered with Phoebe** — conversation can reach anyone directly, but actual hands-on work (code/memory/upgrades) goes through Phoebe's dispatch planning first to avoid missed steps/dependencies

---

## Execution Model (orchestration vs isolated worker sub-agents)

The company runs in two tiers:

- **Orchestration tier** — Elon (CEO), Phoebe (PM), July (HR lead) operate in the
  main context. They hold the broad picture (this skill, design, policy,
  summaries, plans) needed to set direction, plan/dispatch, and tune people.
- **Execution tier** — the four workers Bob (RD), Gibby (QA), Tony (Improvement),
  Tom (IT/Ops) run as **isolated sub-agents**. Each gets only its own `persona.md`,
  the `reads` slice in its own `context.md`, and Phoebe's task brief — **not**
  `SKILL.md`, the design, other employees' desks, or anything outside its slice.
  This keeps each worker's full attention on its task, holds entropy out of the
  main thread, and lets independent workers run **in parallel** (Phoebe dispatches
  parallel sub-agents for independent tasks, serial handoff for dependency chains
  like Bob→Gibby).

Full spec: **[references/execution-model.md](references/execution-model.md)**.

---

## Session Catch-Up Notification (Chairman opt-in: "Option B")

The unattended daily cron (`schedule.sh`) runs silently and only writes logs. So
the Chairman doesn't have to dig through logs, do this **once when self-company is
first engaged in a session**:

1. Run `python3 .company/scripts/notify-status.py`.
2. If the JSON shows `new_runs > 0`, send the Chairman **one** `PushNotification`
   with the `summary` string, AND state the same one-line summary in your reply —
   PushNotification suppresses while the Chairman is actively typing (~60s), so the
   in-chat line guarantees he sees it even when the push is held back.
3. Then run `python3 .company/scripts/notify-status.py --ack` to mark "notified up
   to here" so the same runs aren't reported again.
4. If `new_runs == 0`, stay silent — no notification.

This is how the silent local cron reaches the Chairman's phone without Discord or
a cloud agent: the cron does the work; the next agent session relays the summary.

---

## How to Install

Run:
```bash
./scripts/init_company.sh
```

The script will:
1. Check if `./.company/` already exists
2. If not → copy skeleton from `assets/company-template/` to `./.company/` (preserving `.gitkeep`)
3. Copy `decay.py`, `entropy.py`, `rag_index.py`, and `rag_query.py` into `./.company/scripts/` (travel with project; can run directly via `python3 .company/scripts/decay.py`; the rag scripts are dormant until Ollama + LanceDB are installed)
4. Automatically add `.company/` to the repo's `.gitignore` — company memory is private, never uploaded to git
5. If already exists → don't overwrite, prompt for manual handling

After completion, read `.company/org/policy.md` to understand the company charter, then start talking to Elon.

---

## Language Rules

- **All content in English.**
- **Technical terms** → stay as-is (pytest, RAG, Playwright, token, hook, cron, Sonnet, Haiku, etc.)
- **Tone** → humble and natural, no AI-speak

Example: "I recorded your preference for pytest based on our last conversation; this time when designing the token budget I considered Haiku for routine capture."

---

## More Details

For company design details, see:

- **[Design Document](design/self-company-design.md)** — authoritative architecture
  - §0 design philosophy: Markdown truth, verify loop, entropy KPI, decay, token budget
  - §1 org structure: seven agents, responsibilities, toolkit, context slicing
  - §3 core flows: build pipeline A + memory pipeline B
  - §4 memory tiers + decay: L0/L1/L2, consolidation, decay logic
  - §5 entropy management: three dimensions (text/context/code)

- **v2 Memory Pipeline Implementation Guide**
  - **[references/pipeline.md](references/pipeline.md)** — playbook for four stages CAPTURE → ORGANIZE → WRITE → VERIFY (who, when, inputs/outputs, exact steps, handoff format)
  - **[references/memory-tiers.md](references/memory-tiers.md)** — L0/L1/L2 definitions, consolidation promotion rules, decay formula and thresholds, half-life tables, alignment with scripts
  - **[references/execution-model.md](references/execution-model.md)** — orchestration vs execution tiers, worker sub-agent isolation (least-privilege context), parallel vs serial dispatch

- **Executable Python scripts** (standard library only; skill source in `scripts/`, installed by `init_company.sh` to `.company/scripts/` and travels with project)
  - **[scripts/decay.py](scripts/decay.py)** — scan markdown frontmatter, compute decay_score, produce disposal candidates (drop/archive/demote/upgrade_candidates) per threshold, JSON output; `--apply` flag modifies files. After installation, run `python3 .company/scripts/decay.py`
  - **[scripts/entropy.py](scripts/entropy.py)** — measure entropy across four dimensions (duplication, contradiction, stale, unverified), weighted sum, read-only JSON output + diagnostic candidate list. After installation, run `python3 .company/scripts/entropy.py`
  - **[scripts/rag_index.py](scripts/rag_index.py)** — build/rebuild RAG index from markdown memory (requires Ollama + LanceDB). Dormant by default; activate on Chairman order or when memory crosses threshold. See `references/rag.md`.
  - **[scripts/rag_query.py](scripts/rag_query.py)** — semantic query interface for RAG index (Tony queries during maintenance; Gibby queries during VERIFY for dup/contradiction detection). Offline only, privacy hard rule.

- **Company folder** (`./.company/` — hidden, git-ignored)
  - `org/` — company settings (policy with tunable constants, triggers, worker personas/context)
  - `memory/` — Chairman memory assets (L0/L1/L2 layered, each with frontmatter)
  - `ops/` — operational traces (logs/plans/schedule)
  - `reports/` — this-period reports (entropy/memory deltas)

---

## Completion Status

### v1 Completed

- ✅ Org structure (seven agents, responsibility boundaries, relationship diagram)
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

- ✅ RAG infrastructure deployed (dormant, requires Ollama + LanceDB to activate)
- ✅ Embedding layer — Ollama 'nomic-embed-text' integration via stdlib urllib, no extra dependencies
- ✅ Vector store — LanceDB embedded serverless index at `.company/memory/index/`
- ✅ Graceful degradation — clear error messages and exit code 2 if dependencies unavailable
- ✅ Rebuild/query scripts — `rag_index.py` and `rag_query.py` installed to `.company/scripts/`
- ✅ Technical reference — see `references/rag.md` for full design and usage

### Deferred to Later Versions

- ⏳ Scheduling and trigger mechanism installation (Stop hook / cron) — proposal in `org/triggers.md`, needs Chairman approval before installation
- ⏳ Code/Chat entropy — code drift detection, session distillation
- ⏳ Report automation — v2 produces logs and entropy numbers, report generation deferred to v3

---

## Quick Start

1. **Install** → run `./scripts/init_company.sh`, creates `./.company/` (automatically added to `.gitignore`)
2. **Read charter** → `.company/org/policy.md` and `.company/org/triggers.md`
3. **Talk to Elon** — no prefix = default to CEO, he'll decide who to dispatch to
4. **Give worker commands** → `(Tom) I need you to...` or `(Bob) this file...`
5. **Monitor entropy** — check `.company/reports/` weekly for memory cleanup results
