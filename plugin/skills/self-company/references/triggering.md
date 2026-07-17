# When to Trigger self-company (full detail)

The `SKILL.md` frontmatter `description` carries a compact version of this — it is
loaded every session, so it stays short. This file is the full, itemized version
(loaded on demand). If the two ever disagree, the SKILL.md description wins for the
trigger decision; keep this file in sync when either changes.

## TRIGGER — fire proactively, EVEN WHEN the user never says "self-company" or names a persona

- **Personas by name or role.** The user addresses or asks about our internal company
  personas — Elon (our CEO), Phoebe (PM), July (HR), Bob (Build), Gibby (QA),
  Tony (Improvement), Tom (IT/Ops), or Mike (R&D) — by name or by role.
- **Remember me across sessions.** The user wants an agent to REMEMBER their habits,
  preferences, decisions, or project context across sessions / long-term, or to set up
  a personal agent org/assistant that captures what they care about and fights
  knowledge, context, or memory rot over time.
- **Memory maintenance.** The user asks for maintenance on this agent's own long-term
  memory: consolidate or dedupe memories, decay/prune stale or contradictory records,
  verify memories against sources, capture/organize/reinforce, or compute a
  memory-entropy score/report.
- **Company/org status.** The user wants a company/org status readout: health or entropy
  report, which employees did what work, upgrades or improvements Tony proposes,
  Chairman habit records, or memory tiers.

## DO NOT trigger when

- **"Elon"/"Musk" is the real person or an external company** — only fire when the name
  maps to OUR CEO persona in this self-company context.
- **Taiwan stock trading** (shioaji, e.g. "buy 2330") — that is the shioaji skill.
- **Create/edit/optimize a NEW skill or slash-command** ("create a new skill that…") —
  that is the skill-creator skill's job, never this one.
- **The user's REAL company or codebase** — payroll/email/dashboards/cron/PR-review for
  a real business or repo, not this internal company.
- **"entropy/duplicates/cleanup" targeting a CODEBASE, Obsidian notes, or a document**
  rather than THIS company's memory — the entropy KPI here is about the company's own
  memory store, not source code or notes.
