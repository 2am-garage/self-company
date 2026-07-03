# Change Management — the big-change pipeline

> Chairman directive: *"when making a big change Elon discuss with Phoebe and write a
> spec, then let Phoebe handle the work with Bob, Tony, Gibby, Tom."* This file makes that
> a permanent, documented process instead of tribal knowledge.

A big change moves through six stages — **Spec → Dispatch → Build ⚔ Attack → Measure →
Integration → Closeout** — owned in turn by Elon+Phoebe, Phoebe, Bob/Gibby, Tony, Tom, and
Elon. It is the build pipeline (design §3 Pipeline A) applied to *changing the company
itself*: the same red/blue adversarial spirit, but wrapped in a spec and a measured
before/after.

This is not theory. Phases 1 and 2 (`.company/ops/plans/spec-phase1-tier1.md`,
`spec-phase2-tier2.md`) ran this exact loop end-to-end on 2026-07-03. Each stage below is
grounded in what actually happened in those two runs; read them as the worked examples.

---

## Scope guard — where this pipeline may run

**This pipeline changes the skill's own skeleton, so it runs ONLY where skeleton edits are
already permitted.** It does not weaken "Skeleton Immutability" (SKILL.md · Governance) — it
lives *inside* that boundary:

- **Development repo** (a `.self-company-dev` marker at the working-tree root), or an
  explicit Chairman grant (`SELF_COMPANY_ALLOW_SKELETON=1`): the full pipeline runs and may
  edit `SKILL.md`, `scripts/`, `references/`, `assets/`, `design/`, and personas.
- **Usage repo** (any other project): the company still must **NOT** modify its own
  skeleton. There is no "big change" to the skill here — Elon's survey only inspects and
  reports, and all work stays inside that project's `.company/`.

**Before any skeleton edit, run the guard** (this is the first move of every Dispatch):

```bash
bash scripts/skeleton_guard.sh   # exit 0 = allowed, exit 1 = locked
```

Both Phase 1 and Phase 2 opened with this check — "dev repo, skeleton edits allowed" — and
would have refused to start otherwise.

---

## Trigger — what counts as a "big change"

Run the full six-stage pipeline when the change is any of:

- **multi-file** — touches more than one script/reference/persona;
- **core scripts** — `decay.py`, `entropy.py`, `verify_memory.py`, `capture-trigger.py`,
  `reinforce_memory.py`, `daily-run.sh`, or anything in the memory lifecycle;
- **risky / irreversible** — data migrations, deletions/reaping, provenance rules, anything
  that could silently corrupt or lose memory;
- **KPI-touching** — changes how entropy (the company KPI) is computed or what it counts.

**Lightweight path (skip the full loop).** A small, one-file, mechanical edit — a typo, a
comment, a constant rename, a doc wording fix — does **not** need the whole pipeline. Bob
makes the edit and a **single Gibby pass** checks it (still logged if it touches a script).
No spec, no measure, no integration. When in doubt, size up, not down: Phase 2's C1/C2
"cheap hardening" items rode along as a fold-in batch precisely because they were too small
to spec on their own but touched files the big items already owned.

---

## Stage 0 — Spec (Elon + Phoebe, main context)

**Owner:** Elon (CEO) + Phoebe (PM), working in the orchestration/main context.

Elon sets direction (*is this worth doing, what's the priority, what's out of scope*);
Phoebe turns it into a written spec before any worker is dispatched. No spec, no dispatch —
this is the gate.

Each work item is written in one fixed shape (start from
**[../assets/spec-template.md](../assets/spec-template.md)**):

| field | what it holds |
|---|---|
| **Problem** | the concrete defect, in one paragraph |
| **Live evidence** | what was actually observed — numbers, a failing run, a real bug — not a hypothesis |
| **Target file/function** | the exact `script.py::function` (and any reference/data) the change lands in |
| **Change** | what to do, including fallbacks and invariants to preserve |
| **Acceptance** | the checklist Gibby will attack — pass/fail conditions, edge cases named |
| **Owner** | who builds it (usually Bob) |

The spec ends with a **dispatch table** and a short **guardrails** note (scratch-copy
testing, stdlib-only, graceful degradation, skeleton guard).

> **Worked example.** Phase 1 Item 2 (semantic dedup) shows the shape exactly: *problem* —
> `entropy.py::compute_dup_rate` uses only Jaccard; *live evidence* — "reported `dup=0.0`
> across three runs today while 8 real paraphrase duplicates existed"; *target* —
> `entropy.py::compute_dup_rate`, reusing `rag_embed.py`; *change* — two-pass with a
> cosine band and a **graceful fallback if the venv is absent**; *acceptance* — detects the
> 8 known dups, no false positives on a clean fixture, still runs with venv removed, no
> network calls. A worker could build from that alone.

---

## Stage 1 — Dispatch (Phoebe)

**Owner:** Phoebe (PM, execution gateway).

Phoebe turns the spec into the dispatch table and launches workers as isolated sub-agents
(see **[execution-model.md](execution-model.md)** — each worker sees only its persona, its
`context.md` reads-slice, and the brief).

**The file-batching rule — the core discipline of this stage:**

> Group work so that parallel workers touch **disjoint** files. When two items touch the
> **same** file, give them to **one** worker to do **sequentially**. Never let two writers
> edit one file concurrently.

Why it exists: **entropy.py**. In Phase 2, Item 6 (charter provenance) and Item 7
(adjudication ledger) both edited `entropy.py`. Instead of dispatching them in parallel and
racing two writers into one file, Phoebe serialized them onto a single Bob — the dispatch
note reads *"#6 and #7 both touch entropy.py → serialize or one worker."* Phase 1 did the
same for `decay.py` (Items 1+3 batched to one worker). Independent files still go in
parallel — Phase 1 ran B1/B2/B3 as three concurrent batches because each hit a different
file.

The dispatch table columns are `batch · items · file(s) · owner · notes`; the `notes`
column is where the serialization decision is recorded and justified.

---

## Stage 2 — Build ⚔ Attack (Bob / Gibby, red/blue)

**Owner:** Bob (Blue, build) ⚔ Gibby (Red, attack). Full protocol:
**[red-blue-protocol.md](red-blue-protocol.md)**.

- **Bob builds** against the spec, on **scratch copies** — never the live `.company/`. He
  self-tests every acceptance criterion and hardens (guard + regression test), not just
  patches.
- **Gibby attacks independently.** He does **not** trust Bob's build report. He re-derives
  every acceptance criterion himself and adds his own adversarial angles — rotating attack
  surfaces (correctness → malformed input → concurrency → resources → spec drift →
  regression) until three consecutive rounds hold. Each exchange is logged to
  `ops/red-blue/ledger.md` (the immune memory; old entries never delete).
- **Loop until clean.** Blockers / structural defects → back to Bob. **Small defects → the
  empowered Gibby may self-fix directly — but he logs the fix to `ops/red-blue/ledger.md`
  FIRST**, so the fix is auditable and never silent. (This empowerment was granted in
  Phase 2; Phase 1's Gibby only reported back.)

> **Worked examples.** Phase 1's build/attack caught a **blocker data-loss bug** (retire-
> on-promote leaving an L0 shadow) and forced the graceful-fallback path when the RAG venv
> is missing. Phase 2's attack caught a **security injection** — a `session_id` flowing
> unescaped into YAML — and it was Gibby's **empowered self-fix** that closed it on the
> spot, logged to the ledger first. The point of red/blue is exactly this: the bug dies on
> a scratch copy, before it ships.

---

## Stage 3 — Measure (Tony)

**Owner:** Tony (Improvement engineer).

Tony measures the **entropy KPI before and after** the change (plus any item-specific
metric) on a **scratch / frozen copy** of memory — never by mutating the live corpus. He
reports **honestly**, including the changes that did *not* move the number, and why.

> **Worked examples.** Phase 1 measured entropy dropping after the live shadow migration
> (0.125 → 0.1103) and confirmed the new detector found all 8 known paraphrase dups.
> Phase 2's honest measurement is the instructive one: the charter/provenance change was
> *designed* to cut `unverified_rate` by exempting the 8 axioms — but when Tony measured the
> real before/after, it **did not move the headline entropy KPI**, and he said so plainly
> rather than dressing it up as a win. Measuring the real before/after is exactly what tells
> Elon whether the work actually paid — including when the honest answer is "it didn't."

---

## Stage 4 — Integration (Tom)

**Owner:** Tom (IT/Ops engineer).

Tom does the end-to-end wiring check **without** a real cron tick and **without** a real
model call — does the changed script still parse and run in the daily-run / hook harness?
He returns a **GO / NO-GO** for the live cron + Claude Code hooks.

> **Worked examples.** Phase 1 Tom confirmed `daily-run.sh` + the `SessionStart` hook still
> parse and run end-to-end after the auth-escalation change. Phase 2 Tom confirmed the
> CAPTURE `Stop` hook still fires and that `daily-run.sh` came back **WARN-free** once the
> reap constant was wired into policy. He also re-runs `skeleton_guard.sh` — the pipeline
> must still be inside its own governance boundary at the end, not just at the start.

---

## Stage 5 — Closeout (Elon)

**Owner:** Elon (CEO).

Elon writes the **honest summary**: what shipped, what regressed, what was deferred. Deferred
work becomes an explicit **backlog** (not silently dropped), and the ledger is updated.

> **Worked example.** Both specs end with a "Deferred to the next phase" list — Phase 1
> deferred the NLI cross-encoder and the LOW hardening items into Phase 2; Phase 2 deferred
> the maintenance catch-up, the charter `verified_date` cleanup, and institutionalizing
> *this* pipeline into Phase 3. Closeout is what turns "we ran out of time on X" into a
> tracked item instead of a lost one.

---

## Iron disciplines (extracted from the runs)

These held across both phases and are non-negotiable for any big change:

1. **Scratch-copy testing.** Never mutate live `.company/` during development — Bob, Gibby,
   and Tony all work on copies. Only after the change is clean and measured does it touch
   real data (and a data migration is itself a spec'd item, e.g. Phase 2's charter-seed
   migration).
2. **Stdlib-only for hard dependencies.** Anything the pipeline *must* have runs on the
   Python standard library — no new mandatory install.
3. **Graceful degradation for optional dependencies.** RAG / embeddings are optional: if the
   venv or model is absent, degrade to the cheap path (Jaccard-only) and print a one-line
   notice — **never hard-fail** (Phase 1 Item 2's mandatory fallback).
4. **Run the skeleton guard before skeleton edits** — every time (Stage 1 opener; re-checked
   at Stage 4).
5. **Verify before ship.** Nothing ships until Gibby's attack is clean; nothing is claimed
   until Tony has measured it.
6. **No silent truncation / no silent failure.** If something is dropped, skipped, or
   degraded, it is logged and surfaced (Phase 1's `AUTH_FAIL` marker and escalation is the
   canonical case).

---

## Roles at a glance

| Stage | Owner(s) | Output |
|---|---|---|
| 0 · Spec | Elon + Phoebe | written spec (per-item format) + dispatch table |
| 1 · Dispatch | Phoebe | file-batched dispatch (disjoint-files rule) |
| 2 · Build ⚔ Attack | Bob / Gibby | hardened change on scratch copy + red/blue ledger |
| 3 · Measure | Tony | honest entropy before/after on a scratch copy |
| 4 · Integration | Tom | GO / NO-GO for live cron + hooks |
| 5 · Closeout | Elon | honest summary + backlog, ledger updated |

**Related:** [../assets/spec-template.md](../assets/spec-template.md) (Stage 0 skeleton) ·
[red-blue-protocol.md](red-blue-protocol.md) (Stage 2) ·
[execution-model.md](execution-model.md) (how workers are dispatched) ·
[pipeline.md](pipeline.md) (the memory pipeline the changes operate on) · SKILL.md §
Governance (Skeleton Immutability, reconciled above).

Worked examples: `.company/ops/plans/spec-phase1-tier1.md`,
`.company/ops/plans/spec-phase2-tier2.md`.
