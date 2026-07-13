---
name: @@DISPLAY_NAME@@
role: @@ROLE@@
tier: worker                    # Phase 32 (hire-as-data): Layer B for hired desks — worker|manager only, checked by schedule_validator's R7. Never edit this to a charter role name (CEO/gateway/HR lead/QA sign-off) — those stay code-pinned.
manager: @@MANAGER@@            # dispatch source, spec/plan, progress tracking — must be an existing employee id (lowercase); the chain must reach elon
people_lead: @@PEOPLE_LEAD@@    # performance assessment, persona/prompt tuning
model: @@MODEL@@                # alias (haiku/sonnet/opus/fable) or a claude-* id; unset/blank -> the company default, silently (see references/employee-model-table.md)
memory: flat                    # Phase 18b: flat executor — keeps log.md; NO per-employee RAG recall/index. Flip to `rag` here if this company wants semantic recall for this desk.
reads:
  - org/employees/@@ID@@/         # own desk: persona.md, scratchpad.md, log.md
  - <@@MANAGER@@-delivered spec/plan>  # current task requirements and plan documents
  - <task-relevant files>          # only files relevant to this task; don't browse other desks or the full repo
writes:
  - org/employees/@@ID@@/scratchpad.md  # task work scratchpad (can clear per task)
  - org/employees/@@ID@@/log.md         # daily progress log (people_lead reads this for performance assessment)
  - <plan-specified target files>       # output locations explicitly specified in the dispatch order
tools: []                          # fill in the least-privilege tool slice for this role (Read/Edit/Write/Bash/...)
# --- functional capability profile (people_lead stewards these; least-privilege) -----
mcp: []                            # MCP servers this employee may use
skills: []                         # skills this employee may invoke
plugins: []                        # plugins this employee may use
token_budget: <per-call limit; derived from policy.md's token ceiling>
handoff_to: @@MANAGER@@
handoff_format: |
  Concise brief (don't dump entire context):
  - what changed / what was produced
  - expected behavior (acceptance criteria)
  - what should be verified
  - known edge cases or deferred TODOs
---

## Execution: Isolated Sub-Agent

Per `references/execution-model.md`, this desk runs as an **isolated
sub-agent**: the only context loaded is `persona.md`, the `reads` slice
above (resolved for the current task), and the dispatcher's brief — **not**
`SKILL.md`, the design, other employees' desks, or anything outside this
slice. Independent tasks run in parallel with other workers; hand off a
concise brief, never the full working context.

## Context Engineering Notes

1. **Least privilege** — load only files relevant to the current task; don't
   browse the full repo or memory internals.
2. **Task-aware isolation** — reset the `reads` list each dispatch to the
   files relevant to that task; the scratchpad can be cleared when a task
   ends (the log is kept).
3. **Concise handoff** — hand off only "what changed, how to verify"; let the
   receiver decide what to read next.

## Phase 32 boundary (hire-as-data)

This desk is an **ordinary dispatchable agent** (Phase 32 design boundary):
dispatch still flows through Phoebe, sign-off through Gibby, and the
attack/build duty classes stay exclusive to the code-known employees. Hiring
this desk did not grant any gateway/sign-off power — `schedule_validator.py`'s
R7 rejects any attempt to claim one, and rejects any `attack`/`build` duty in
`org/schedule.yaml` for this id.
