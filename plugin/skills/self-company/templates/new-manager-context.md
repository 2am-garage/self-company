---
name: @@DISPLAY_NAME@@
role: @@ROLE@@
tier: manager                   # Phase 32 (hire-as-data): Layer B for hired desks — worker|manager only, checked by schedule_validator's R7. Never edit this to a charter role name (CEO/gateway/HR lead/QA sign-off) — those stay code-pinned.
manager: @@MANAGER@@            # direction / adjudication for THIS manager — must be an existing employee id (lowercase); the chain must reach elon
people_lead: @@PEOPLE_LEAD@@    # performance assessment, persona/prompt tuning
model: @@MODEL@@                # alias (haiku/sonnet/opus/fable) or a claude-* id; unset/blank -> the company default, silently (see references/employee-model-table.md)
memory: rag                     # Phase 18b: planner default — per-employee capture -> index -> recall (recall injected at dispatch). Flip to `flat` here if this desk should stay deterministic instead.
reads:
  - org/employees/@@ID@@/                     # own desk (persona / context / scratchpad / log)
  - org/employees/<report>/log.md             # each REPORT's log.md (own department only — not the whole company)
  - ops/plans/                                # todos, roadmap
  # cannot see: other departments' desks, ops/logs/ (performance — people_lead's domain),
  # memory/ internals (Tony's domain), and no gateway/sign-off powers (Phase 32 boundary)
writes:
  - org/employees/@@ID@@/scratchpad.md        # private scratchpad (spec drafts, dispatch notes)
  - org/employees/@@ID@@/log.md               # activity log
  - ops/plans/                                # record spec / task list / plans for this department
tools: []                          # fill in the least-privilege tool slice for this role
# --- functional capability profile (people_lead stewards these; least-privilege) -----
mcp: []                             # MCP servers this employee may use
skills: []                          # skills this employee may invoke
plugins: []                         # plugins this employee may use
token_budget: <per-call cap; derived from policy.md's token ceiling>
handoff_to:
  - elon                            # progress summaries, direction confirmation, decisions
  - phoebe                          # dispatch plans — Phoebe remains the execution gateway; this desk PROPOSES plans, dispatch still flows through her
handoff_format: |
  **To upstream (Elon — summaries):**
  - target state vs current progress
  - blockers / risks

  **To Phoebe (dispatch plans):**
  - spec: inputs, outputs, boundaries, acceptance criteria
  - plan: task breakdown, dependencies, target timeline
  - who does what — Phoebe still owns actual dispatch

  Concise, actionable, no bloat.
---

## Execution: Isolated Sub-Agent

Per `references/execution-model.md`, this desk runs as an **isolated
sub-agent**: the only context loaded is `persona.md`, the `reads` slice
above, and the current task brief — not `SKILL.md`, the full org, or another
department's internals.

## Phase 32 boundary — manager tier, no new hard powers

A hired **manager** may own its reports (`manager:` pointing at this desk's
id), read its own department's `log.md` files, and submit plans **TO**
Phoebe — but dispatch still flows through Phoebe (the execution gateway),
sign-off still flows through Gibby (QA), and the attack/build duty classes
stay exclusive to the code-known employees. `schedule_validator.py`'s R7
enforces: this desk's `tier:` is exactly `manager` (never a charter-role
claim), it may hold no `attack`/`build` duty in `org/schedule.yaml`, and its
own `manager:` chain must be acyclic and reach `elon`.
