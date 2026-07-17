---
name: self-company
description: |
  Multi-agent personal company that learns the Chairman (Uwe)'s habits, preferences, and background across sessions and fights entropy via tiered memory, a verification loop, and decay. On install it creates a git-ignored, project-scoped .company/ in the current repo.
  Use proactively (even when the user never says "self-company" or names a persona) when the user: addresses or asks about our personas — Elon (CEO), Phoebe (PM), July (HR), Bob (Build), Gibby (QA), Tony (Improvement), Tom (IT), Mike (R&D) — by name or role; wants an agent to REMEMBER their habits/preferences/decisions/context across sessions or to set up such a personal agent org; asks for memory maintenance on this agent's own long-term memory (consolidate/dedupe, decay stale or contradictory records, verify against sources, capture/reinforce, or a memory-entropy report); or wants a company/org status readout (health/entropy, who did what, Tony's proposed upgrades, memory tiers).
  Do NOT trigger when "Elon"/"Musk" means the real person or an external company; for Taiwan stock trading (shioaji, e.g. "buy 2330"); to CREATE, edit, or optimize a NEW skill or slash-command ("create a new skill that…" — that is skill-creator's job); for payroll/email/dashboards/cron/PR-review of the user's REAL company or codebase; or when "entropy/duplicates/cleanup" targets a CODEBASE, Obsidian notes, or a document rather than THIS company's memory. Full itemized detail: references/triggering.md.
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
                    │  July · HR  │  team lead (half a tier above the five workers) — tune / performance
                    └──────┬──────┘
        ┌───────────┬──────┴─────┬─────────────┬─────────────┐
  ┌─────▼─────┐ ┌───▼─────┐ ┌───▼───────┐ ┌───▼──────┐ ┌────▼─────┐
  │ Bob·Build │⚔│Gibby·QA │ │Tony·Improv│ │ Tom · IT │ │ Mike·R&D │
  │   build   │/│attack/test│ │diagnose/propose│execute/infra│ │survey outside│
  └───────────┘ │ │  └────┬──────┘ └────▲─────┘ └──────────┘
  Blue ┌───────┘ Red │proposal        │execute
                     Elon adjudicate ◄────┘           │
                          └──► Phoebe plan dispatch ────┘
```

**Chain**: Chairman → Elon (CEO) → Phoebe (PM, execution gateway) → July (HR team lead) → {Bob (Build), Gibby (QA), Tony (Improvement), Tom (IT), Mike (R&D)}

**Key roles**:
- **Phoebe = execution gateway** — any actual hands-on work goes through her dispatch planning first to ensure no missing steps, no lost dependencies.
- **July = worker team lead + capability steward** — tunes the five workers' personas/performance AND runs a scheduled capability audit (their tools/MCP/skills/plugins, kept accurate against the environment and right-sized to least-privilege); half a tier above them, doesn't touch manager tier.

---

## Staff Roster

| Name | Title | Responsibility |
|---|---|---|
| **Elon** | CEO | Set direction, upgrade adjudication, lead manual deep cleanups |
| **Phoebe** | PM | Execution gateway: convert intent → spec/plan, dispatch tasks, track progress, fill gaps, set dependencies |
| **July** | HR | Tune five workers' personas/prompts/performance; **capability steward** — weekly audit of each worker's tools/MCP/skills/plugins, proposes stale/gap/over-grant fixes for approval (least-privilege; never auto-edits a profile) |
| **Bob** | Build Engineer | Produce code/files per Phoebe's spec |
| **Gibby** | QA Engineer | Attack Bob's output by every means, loop until clean |
| **Tony** | Improvement Engineer | Think: measure entropy, evaluate health, write upgrade proposals for Elon |
| **Tom** | IT/Ops Engineer | Act: skeleton, scheduling, token breaker, execute upgrades |
| **Mike** | R&D Researcher | External literature/ecosystem research, evidence packs for specs (Tony measures inside, Mike surveys outside) |

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

Deterministic math (decay, entropy) runs in Python (`scripts/decay.py`, `scripts/entropy.py`); judgment work (what to capture, placement, source verification) is playbook commands run by the owning agent.

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

The company runs in two tiers. **Orchestration** — Elon, Phoebe, July — work in
the main context, holding the broad picture (skill, design, policy, plans) to set
direction, dispatch, and tune people. **Execution** — the five workers (Bob, Gibby,
Tony, Tom, Mike) — run as **isolated sub-agents**, each seeing only its own `persona.md`,
its `context.md` `reads` slice, and Phoebe's brief — never `SKILL.md`, the design,
or another desk. This keeps attention focused, holds entropy out of the main
thread, and lets independent workers run in parallel (serial handoff for chains
like Bob→Gibby).

**Per-employee memory** — analysts/planners (Tony, Mike, Elon, Phoebe, July) keep a
per-employee RAG store and `recall` relevant past experience at dispatch; executors
(Bob, Gibby, Tom) use flat `log.md` / red-blue ledger memory (no RAG). A
`shared_memory_read` employee (Elon by default) ALSO reads the SHARED company
memory (the Chairman's standing direction) at dispatch — not just via the ask-time
hook — so autonomous/cron/trigger work carries it too.

Full spec: **[references/execution-model.md](references/execution-model.md)**.

---

## Governance: Skeleton Immutability (dev vs usage)

This skill is **self-improving — but only in its own development repo.**

- **Development repo** (marked by a `.self-company-dev` file at the working-tree
  root): the full upgrade loop runs here and edits the skill **skeleton** —
  `SKILL.md`, `scripts/`, `references/`, `assets/`, `design/`, personas.
- **Usage (any other project)**: the company operates **entirely within that
  project's `.company/`** and must **NOT modify its own skeleton unless the
  Chairman explicitly orders it** (`SELF_COMPANY_ALLOW_SKELETON=1`). Elon's daily
  survey there only inspects and reports.

**Before any skill-source edit, consult the guard:**
```bash
bash scripts/skeleton_guard.sh   # exit 0 = allowed, exit 1 = locked
```
Allowed only when `.self-company-dev` is present (dev repo) or the Chairman set
`SELF_COMPANY_ALLOW_SKELETON=1`. This is what makes the self-upgrading company
safe to deploy inside a real codebase: it won't rewrite itself there.

---

## Change Management

Any **big change to the company itself** — multi-file edits, changes to core scripts
(decay/entropy/verify/capture/daily-run), anything risky/irreversible, or anything touching
the memory lifecycle or the entropy KPI — runs a fixed six-stage pipeline: **Elon + Phoebe
write a spec → Phoebe dispatches (file-batched) → Bob builds ⚔ Gibby attacks → Tony measures
entropy before/after → Tom integration-checks → Elon closes out.** Small one-file mechanical
edits take the lightweight path (Bob + a single Gibby pass), no spec. This pipeline edits the
skeleton, so it runs **only where skeleton edits are permitted** (dev repo / Chairman grant,
per Governance above) — in a usage repo the company still must not modify its own skeleton.

Full process: **[references/change-management.md](references/change-management.md)** (six
stages, iron disciplines, worked examples). Start a spec from
**[assets/spec-template.md](assets/spec-template.md)**.

---

## Operations

The company runs on **triggers** (Chairman call · 6-hourly `daily-run.sh` cron ·
`fire-trigger.sh` event · session handoff), a keyed per-project **crontab**
(`schedule.sh`, tunable in `org/schedule.yaml`), and **9 plugin-native hook
registrations across 7 events** (`hooks/hooks.json`, each opt-in-guarded to a
`.company` repo — `SessionStart` and `PostToolUse` each fire two). A durability floor
(pre-mutation `memory/` snapshots, soft-delete tombstones, offline-gap damper) and
reporting (`report.py` ledger, catch-up `SessionStart` push, on-demand views) round it
out. An **optional holding-company fleet** layer (`fleet.py`/`fleet-run.sh`) drives N
sub-companies from one parent cron — single-company users can ignore it.

Read **[references/operations.md](references/operations.md)** for the full detail — how to
run or wire the triggers, daily cron, scheduling/config, hooks, catch-up push, reports, and
the optional fleet.

---

## How to Install

Run:
```bash
./scripts/init_company.sh
```

The script will:
1. Check if `./.company/` already exists
2. If not → copy skeleton from `assets/company-template/` to `./.company/` (preserving `.gitkeep`)
3. Automatically add `.company/` to the repo's `.gitignore` — company memory is private, never uploaded to git
4. If already exists → don't overwrite, prompt for manual handling

`.company/` is **DATA only** (memory, org config, ops). The Python/shell scripts are
NOT copied into it — the runtime (daily-run, schedule, hooks, company-run) resolves and
runs the CANONICAL scripts straight from the skill/plugin, so a skill update takes effect
immediately with no stale-copy drift.

After completion, read `.company/org/policy.md` to understand the company charter, then start talking to Elon.

### Upgrading (after a skill/plugin update)

The cron lines are **absolute-path snapshots** of where the scripts lived at install
time. As of the Phase-12b self-heal this is now **automatic**: the `SessionStart`
guard (`hook_schedule_guard.sh`) folds the resolved scripts dir into its signature,
so when a plugin update/move changes that path it re-runs `schedule.sh install` for
you on the next session — the cron re-points itself with no manual step (only an
already-scheduled project; opted-out repos are never auto-installed). A tick/research
edit self-heals the same way. If you'd rather not wait for the next session, the
manual refresh remains available as a fallback:
```bash
bash scripts/schedule.sh install       # optional: refresh the cron lines now
```
Hooks need **no** re-install: since v0.1.2 they are plugin-native (`hooks/hooks.json`
via `${CLAUDE_PLUGIN_ROOT}`) and survive version bumps automatically.

### Optional local setup (not pre-installed on clone)

These are opt-in and live only on your machine (`.claude/` is git-ignored):

- **Hooks** — none needed: all 9 hook registrations (across 7 events) are plugin-native (`hooks/hooks.json`). If you
  used the pre-v0.1.2 installer, run `scripts/install-hook.sh uninstall` once to remove
  the legacy `.claude/settings.json` entries that would otherwise double-fire.
- **Dev repo only** — if you cloned the skill's *development* repo (the one with a
  `.self-company-dev` marker) and want it to load itself as a skill, run
  `scripts/dev-link-skill.sh` to (re)create the `.claude/skills/self-company/`
  symlinks. These are intentionally not committed.

---

## Language Rules

- **All content in English.**
- **Technical terms** → stay as-is (pytest, RAG, Playwright, token, hook, cron, Sonnet, Haiku, etc.)
- **Tone** → humble and natural, no AI-speak

Example: "I recorded your preference for pytest based on our last conversation; this time when designing the token budget I considered Haiku for routine capture."

---

## More Details

Load these on demand — none is needed to ACT until you're doing the specific thing it covers.

- **[design/self-company-design.md](design/self-company-design.md)** — authoritative architecture (§0 philosophy · §1 org · §3 flows · §4 tiers+decay · §5 entropy).

**References**
- [pipeline.md](references/pipeline.md) — CAPTURE → ORGANIZE → WRITE → VERIFY stages (who/when/steps/handoff).
- [memory-tiers.md](references/memory-tiers.md) — L0/L1/L2, promotion rules, decay formula + half-life tables.
- [execution-model.md](references/execution-model.md) — orchestration vs isolated workers (least-privilege context), dispatch.
- [employee-model-table.md](references/employee-model-table.md) — per-employee `model:` alias map (haiku/opus/fable/sonnet), degrade contract, current assignments.
- [operations.md](references/operations.md) — triggers, scheduling/config, hooks, catch-up push, ledger, views, supervisor.
- [red-blue-protocol.md](references/red-blue-protocol.md) — Bob (Blue) ⚔ Gibby (Red) build-and-attack loop.
- [rag.md](references/rag.md) — RAG index (LanceDB + fastembed, local/offline); wired into the pipeline (index refresh + ask-time injection), venv-gated — `rag_setup.sh install` to activate the semantic path.
- [triggering.md](references/triggering.md) — full itemized TRIGGER / DO-NOT list (the frontmatter description carries a compact version).
- [status.md](references/status.md) — completion checklists + deferred items.

**Scripts** (canonical in `scripts/`, run in place — never copied into `.company/`; stdlib only EXCEPT the `rag_*.py` family below, which needs the opt-in `.company/.rag-venv` — LanceDB + fastembed — created by `rag_setup.sh install`; everything else runs on a bare Python)
- `decay.py` / `entropy.py` — the decay disposal pass and the entropy KPI pass (`--memory-dir .company/memory`, JSON; decay `--apply` mutates).
- `frontmatter.py` / `tombstone.py` / `charter_ids.py` / `policy_config.py` — shared single-source libraries hard-imported across the scanners/hooks (frontmatter delimiter contract: `line.strip() == '---'`, opening fence on line 0). `policy_config.py --get KEY --default N` (Phase 29) is the one CLI seam bash callers use to resolve a tunable instead of an inline python heredoc.
- `prompt_builder.py` — the shared dispatch-prompt assembly seam (Phase 29): role header, a STATED wall-clock budget (seconds, never tokens), a nonce-fenced data block, an output contract, a task boundary. Wired into `supervisor.py`, `fire-trigger.sh`, `research-scan.sh`, `company-run.sh`.
- `corpus.py` — the shared memory-corpus walk + parse + id/tombstone-gate + body-extraction primitive (Phase 28) six loaders (decay/entropy/verify/reinforce/rag_index/elon_survey) now share instead of independently re-implementing.
- `schedule_config.py` — the schedule/duty reader; `--plan-tick --hour H --dow D` (Phase 28) is the one-JSON seam `daily-run.sh` sources instead of ~13 separate `--should-run`/`--agent` spawns.
- `rag_index.py` / `rag_query.py` / `rag_embed.py` / `rag_rerank.py` — the RAG index build / semantic query (hybrid + cross-encoder rerank) / local-embed / reranker layer (see rag.md). `rag_index.py --pair MEM_DIR INDEX_DIR` (repeatable, Phase 28) refreshes multiple stores in one process.
- `daily-run.sh` / `schedule.sh` — the maintenance + cron-scheduling runtime; `fleet.py` / `fleet-run.sh` — the **optional** holding-company layer; `agent_spawn.sh` — the shared bash lib (Phase 28) for CLAUDE_BIN resolution, the kill-after timeout probe, the auth pre-flight probe, and the scripts-dir precedence, sourced by all six.

- **Company folder** (`./.company/`, git-ignored) — `org/` (policy, triggers, personas/context) · `memory/` (L0/L1/L2) · `ops/` (logs/plans/schedule) · `reports/`.

---

## Quick Start

1. **Install** → run `./scripts/init_company.sh`, creates `./.company/` (automatically added to `.gitignore`)
2. **Read charter** → `.company/org/policy.md` and `.company/org/triggers.md`
3. **Talk to Elon** — no prefix = default to CEO, he'll decide who to dispatch to
4. **Give worker commands** → `(Tom) I need you to...` or `(Bob) this file...`
5. **Monitor entropy** — check `.company/reports/` weekly for memory cleanup results
