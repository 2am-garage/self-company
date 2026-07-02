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
| **Execution** | Bob (RD), Gibby (QA), Tony (Improvement), Tom (IT/Ops) | Narrow: **only** its own `persona.md`, the `reads` slice in its own `context.md`, and the task brief Phoebe hands it | **Isolated sub-agent** (one per task) |

**Why July is orchestration, not a worker sub-agent:** July's job *is* to read the
four workers' desks and logs to tune them. She needs cross-worker visibility, so
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
Gibby → Playwright MCP; Tony/Gibby → RAG (`rag_query`); Tony/Bob → `deep-research`.

---

Version: v1.1 (2026-06-29) — orchestration/execution split, sub-agent isolation, parallel dispatch, tools registry.
Related: `SKILL.md` (org chart, addressing protocol), `org/triggers.md` (cadence/parallelism), `org/tools.md` (tool inventory + grants), each `org/employees/<name>/context.md` (per-worker slice).
