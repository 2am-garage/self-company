# Execution Model — Orchestration Tier vs Isolated Worker Sub-Agents

> Chairman directive (2026-06-25): non-management employees should not read "the
> section" (the whole skill / design / other desks). They run as **isolated
> sub-agents** so they keep their full attention on their own task, and
> independent ones run **in parallel**.

This file is the authoritative spec for *how* the company executes work. The org
chart (SKILL.md) says *who* reports to whom; the addressing protocol says *how to
talk* to them; this says *how a worker is actually run*.

---

## 1. Two Tiers

| Tier | Members | Context | Runs as |
|---|---|---|---|
| **Orchestration** | Elon (CEO), Phoebe (PM), July (HR lead) | Broad: reads `SKILL.md`, `design/`, `references/`, `org/policy.md`, summaries, plans, and (July) worker desks | Main thread / main context |
| **Execution** | Bob (Build), Gibby (QA), Tony (Improvement), Tom (IT/Ops), Mike (R&D) | Narrow: **only** its own `persona.md`, the `reads` slice in its own `context.md`, and the task brief Phoebe hands it | **Isolated sub-agent** (one per task) |

**Why July is orchestration, not a worker sub-agent:** July's job *is* to read the
five workers' desks and logs to tune them. She needs cross-worker visibility, so
she sits on the orchestration side even though she is "half a tier above" the
workers and not a manager. She still does not read code, memory internals, or
infra — her least-privilege scope is unchanged.

---

## 2. What an Execution Worker Sub-Agent Sees

When Phoebe dispatches a task to a worker, the worker is launched as a fresh
sub-agent whose entire context is constructed from exactly three things:

1. **Role** = the worker's `org/employees/<name>/persona.md` (voice, scope, iron rules).
2. **Allowed reads** = the `reads:` list in `org/employees/<name>/context.md`,
   resolved to concrete files for *this* task (e.g. Bob gets only the spec + the
   task-relevant code files; Gibby gets only the files Phoebe named + the spec).
3. **Task** = Phoebe's concise spec / brief (inputs, outputs, acceptance criteria).

Everything else is **out of scope and not loaded**:
- ❌ `SKILL.md`, `design/`, `references/` (except a specific reference Phoebe
  explicitly hands in, e.g. red-blue-protocol.md for a build task).
- ❌ Other employees' desks (`org/employees/<other>/`).
- ❌ Sections / memory tiers / logs outside the worker's `context.md` slice.

This is the same least-privilege boundary each `context.md` already declares — the
execution model is what *enforces* it: a sub-agent literally does not carry the
parent's context, so a worker cannot wander into "the section."

### Rationale
- **Attention.** A worker reasoning over only its task + permitted files makes
  fewer mistakes and spends fewer tokens than one swimming in the whole skill.
- **Entropy control.** Isolation is the structural defense against context rot —
  no accumulation of unrelated material across tasks (matches each worker's
  "task-aware context / scratchpad cleared per task" rule).
- **Parallelism.** Isolated sub-agents share no mutable context, so independent
  tasks can run at the same time without interference.

---

## 3. Parallel vs Serial Dispatch (Phoebe decides)

Phoebe breaks intent into tasks, marks dependencies, then launches workers:

- **Independent tasks → parallel sub-agents**, dispatched in a single batch.
  - e.g. *Tony* auditing entropy ∥ *Tom* checking infra — no shared state, run together.
- **Dependent chain → serial handoff.**
  - e.g. *Bob* builds → (only then) *Gibby* verifies. Gibby's input is Bob's output.
- Each sub-agent returns a **concise handoff brief** to Phoebe (result + what's
  next), never a dump of its full working context.

Mapping to the trigger matrix (`org/triggers.md`):

| Trigger | Parallel | Serial |
|---|---|---|
| Real-time | CAPTURE across workers (each records independently) | → VERIFY (single Gibby) |
| Daily (4×/day) | independent memory items within a stage | CONSOLIDATE → DECAY → WRITE → VERIFY (dependency chain) |
| Weekly | FULL-VERIFY ∥ ENTROPY ∥ PERFORMANCE ∥ INFRA ∥ RAG-REBUILD | → LOG-COMPILE (last) |

---

## 4. Implementation Mapping (Claude Code runtime)

When the company is operated inside Claude Code:

- The **orchestration tier runs in the main thread** — Elon sets direction, Phoebe
  plans and dispatches, July tunes. They hold the broad context this file grants.
- Each **execution worker is launched via the Agent/Task tool as a sub-agent**,
  with a prompt assembled from (persona + resolved reads-slice + Phoebe's brief).
- **Parallel workers** = multiple sub-agent calls issued in one batch; the
  orchestration tier waits for their handoff briefs and integrates results.
- A worker sub-agent's **final message is its handoff brief** — that is the only
  thing that returns to Phoebe; its intermediate context is discarded (entropy
  stays out of the main thread).

> Hard rule: an execution worker is never given the parent/main context wholesale.
> If a worker needs something outside its slice, it asks Phoebe, who either widens
> the brief or hands in the specific file — deliberately, not by default.

---

## 5. Boundaries Unchanged

This model changes *how* workers are run (isolated, parallel), not *what* they may
touch. Every `writes:` / `reads:` / `cannot see` rule in each `context.md` and in
`policy.md §4.1` (least privilege) still holds. The execution model only makes the
isolation structural instead of advisory.

---

## 6. Tools: MCP servers and skills (least privilege)

Workers do their jobs with tools beyond the built-ins (Read/Edit/Bash/…). When
Phoebe dispatches a task, the worker sub-agent is granted **only the MCP tools and
skills its role needs** — the same least-privilege slice as its memory and file
access, never the full tool surface:

- The granting agent passes the worker the relevant MCP tools by name (some load
  on demand via tool-search) and permits the skills listed for its role.
- Unreachable tools degrade gracefully — the worker falls back to built-ins and
  notes it.

The authoritative inventory and per-role grants live in **`org/tools.md`** (Tom
owns it; July keeps each worker's `context.md` grants matched to it). Examples:
Gibby → Playwright MCP; Tony/Gibby → RAG (`rag_query`); Mike/Bob → `deep-research`.

---

## 7. Per-employee memory: mode (rag/flat), recall at dispatch, capture at close (Phase 18 / 18b)

**Not every employee needs RAG memory.** Each employee carries a per-employee
memory **MODE** — a CONFIG toggle in their `context.md` frontmatter
(`memory: rag|flat`), surfaced as `Employee.memory_mode` /
`Employee.rag_memory_enabled` (Phase 18b). It is a config knob, not a
planner-vs-executor rule hardcoded in the logic (modularize, don't special-case):
`context.md` is authoritative, and `employee.py` holds only a DEFAULT table used
when a desk omits the field.

| Mode | Employees (default) | What they use as memory |
|---|---|---|
| **`flat`** (RAG off) | **Bob, Gibby, Tom** | their existing `log.md` — and, for Gibby, the deterministic red/blue **ledger** (already a superior structured memory). NO per-employee RAG store, NO index, NO recall, NO dispatch injection. |
| **`rag`** (RAG on) | **Tony, Mike, Elon, Phoebe, July** | the per-employee capture → index → recall store below, PLUS their `log.md`. |

The Chairman's split: executors keep flat memory (deterministic/structured is
enough); analysts and planners get semantic recall. A company can flip any
employee by editing their `context.md` `memory:` field — no code change.

For a **`rag`** employee, the store lives at `org/employees/<name>/memory/` with
the employee's OWN LanceDB index under `memory/index`, so **workers grow with the
project**: Tony recalls his past diagnoses, Mike his past research. It is FLAT and
light — **capture → index → recall** — with NO per-employee tiers/decay/verify/
entropy; the anti-entropy machinery stays on the SHARED company memory only. The
RAG stack (`rag_embed`/`rag_index`/`rag_query`) is reused as-is, pointed per
employee; the `Employee` model (`scripts/employee.py`) owns the seams (all gated
on `rag_memory_enabled` — a `flat` employee no-ops every one of them):

- **`Employee.remember(text, *, tags=None, source=None)`** — writes ONE
  structured memory file (`id / owner / tier / created / tags / source` +
  body) to the employee's own store. Pure stdlib, never raises; dedup by content
  hash makes a re-record idempotent. **Flat employee → no-op returning `None`**
  (no file, so the index never sees them).
- **`Employee.recall(query, top_k=3)`** — shells `rag_query.py` against the
  employee's OWN index, re-validates each hit against the live memory files
  (isolation backstop: only files physically inside this employee's store are
  ever returned), and returns the relevant memories. Timeout-capped; degrades to
  `[]` on no venv / empty index / any error — never raises, never blocks.
  **Flat employee → `[]`** (short-circuits before any venv/index work).
- **`Employee.recall_context(query, top_k=3)`** — the dispatch-injection helper:
  returns a compact, ready-to-prepend **"Relevant past experience: …"** block
  (each hit truncated to a char budget) for a `rag` employee with relevant
  memories, or the EMPTY string `""` for every no-injection case (flat employee,
  empty query, no venv / empty index, zero hits). Gated internally on
  `rag_memory_enabled`, so the caller need not branch on mode.

**Recall at dispatch (the payoff, now wired).** Before a `rag` worker acts, the
orchestration tier calls `Employee.load(name, company).recall_context(<task
brief>)` and prepends the returned block to the worker's prompt slice (alongside
persona + reads-slice + brief, §2). The concrete call site is
`supervisor.py` — `Member.real_command()` (the live dispatcher that builds each
worker's `claude -p` prompt) bridges to the data model and injects the recalled
block when it is non-empty. Because the worker prompt is assembled by the
orchestration tier itself (the Agent/Task dispatch in §4), `recall_context()` **is**
that wiring point — one call, config-gated, with the whole ask-time injection
discipline (`hook_memory_inject`) baked in: tight timeout, capped size, and a
clean fall-back to no-injection when the RAG stack is absent or the employee is
flat, so it can never delay or block a dispatch. For a **flat** worker it returns
`""` — no injection. Isolation holds: a worker only ever receives memories from
its OWN store.

**Capture at close.** A `rag` worker's final act, after its handoff brief, is to
record **ONE** structured memory of the single reusable lesson from the task, via
`remember()` (its persona's "Memory — grow with the project" section states this).
Conservative by rule: one durable lesson per task, *skip entirely* if the task
carried no real lesson — durable experience, not chatter. This is separate from
the worker's `log.md` progress note. A **flat** worker skips capture (its
`remember()` no-ops) and relies on `log.md` / the red-blue ledger instead.

**Index refresh.** `daily-run.sh` refreshes the **`rag` employees'** stores
incrementally under Tony's existing `rag_index` step (content-hash skip → an
unchanged store is ~free; venv absent → one-line skip; `|| true` → never fails the
core). **Flat employees are skipped entirely** — no index, fewer refreshes,
lighter. The refresh reads the rag/flat split from `employee.py`
(`rag_memory_enabled`, context.md-driven), so it stays config-gated, not
name-hardcoded. No new Layer-B step owner is introduced, so the role topology and
the R1–R6 validator are untouched.

---

Version: v1.3 (2026-07-07) — + per-employee memory MODE (rag/flat) + recall-at-dispatch wired via `Employee.recall_context`.
Prior: v1.2 (2026-07-07) — + per-employee memory (recall at dispatch, capture at close).
Prior: v1.1 (2026-06-29) — orchestration/execution split, sub-agent isolation, parallel dispatch, tools registry.
Related: `SKILL.md` (org chart, addressing protocol), `org/triggers.md` (cadence/parallelism), `org/tools.md` (tool inventory + grants), each `org/employees/<name>/context.md` (per-worker slice + `memory:` mode), `scripts/employee.py` (`memory_mode`/`remember`/`recall`/`recall_context`).
