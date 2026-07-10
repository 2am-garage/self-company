# self-company

A multi-agent **personal company** for Claude Code: a small org of personas (Elon
CEO, Phoebe PM, July HR, Bob Build, Gibby QA, Tony Improvement, Tom IT, Mike R&D)
that learns your habits and preferences across sessions and continuously fights
entropy through tiered memory, a verification loop, and decay. Private and
project-scoped — all data lives in a git-ignored `.company/` folder that never
leaves your machine.

Full skill docs: [`plugin/skills/self-company/SKILL.md`](plugin/skills/self-company/SKILL.md).

## Install (as a plugin)

```
/plugin marketplace add 2am-garage/self-company
/plugin install self-company
```

Then create the private company in your project and (optionally) wire up the
automation:

```bash
# creates ./.company/ (git-ignored data: memory, ops, org config)
bash plugin/skills/self-company/scripts/init_company.sh

# optional automation, opt-in and local:
bash plugin/skills/self-company/scripts/schedule.sh install        # the scheduled work below
```

> Hooks need **no** setup: since v0.1.2 all 8 registrations across 7 events are **plugin-native** (declared in
> `hooks/hooks.json`, run via `${CLAUDE_PLUGIN_ROOT}`) and load automatically with the
> plugin. If you used the pre-v0.1.2 installer, run
> `install-hook.sh uninstall` once to drop the legacy `.claude/settings.json` entries
> (plugin hooks merge with settings hooks, so leaving them would double-fire).

## Update

```
/plugin update
```

Updates track `main`. Because code and data are separated, an update refreshes the
skill's logic while your `.company/` data is untouched.

> **Updates self-heal the cron.** The cron lines are absolute-path snapshots taken
> at install time, but the `SessionStart` guard now detects a moved scripts path
> after a plugin update and re-points cron for you on the next session — no manual
> step for a routine update. You can still run `schedule.sh install` yourself to
> refresh immediately. Hooks need no re-install either — they are plugin-native and
> use `${CLAUDE_PLUGIN_ROOT}`.

## What runs on a schedule

Once `schedule.sh install` is run, two OS cron entries do the unattended work
(local, no cloud — your memory never leaves the box):

| Schedule | Job | What it does |
|---|---|---|
| every 6h (`00:07 / 06:07 / 12:07 / 18:07`) — default, configurable | `daily-run.sh` | decay stale memories, verify provenance, compute the entropy KPI, a conservative consolidation pass, and one Tony improvement proposal |
| weekly (`Sun 03:23`) — default, configurable | `research-scan.sh` | Mike surveys the web for skill improvements and files proposals |

Those cadences are **defaults**: a company can set its own tick, research schedule,
agent knobs, and each employee's `cadence`/`duties`/`budget`/`enabled` in
`org/schedule.yaml` (absent = today's behaviour, unchanged) — that's Layer A. Layer B
(who attacks vs builds, the sign-off gate) stays in code and is **validator-guarded**:
any config that would break the red/blue competition is rejected and falls back to
defaults. A `SessionStart` guard syncs a tick change into the crontab.

Event-driven (not cron): **8 plugin-native hook registrations across 7 events** work automatically. `Stop`/`PreCompact`
capture memories in real time, `SessionStart` surfaces the scheduled-work report on
entry, **`UserPromptSubmit` injects your relevant memories into the prompt at ask-time**
(so the company actually *uses* what it learned), `SessionEnd` verifies fresh captures,
and `PreToolUse`/`PostToolUse` guard the memory store. All no-op in any repo without a
`.company/`.

## Running multiple companies

The skill is built for **several companies on one machine** (one per repo), fully
isolated (each `.company/` is separate). `schedule.sh` namespaces cron per project and
staggers them, so installing in a second repo never disturbs the first. To avoid paying
full maintenance N times, a **holding company** can drive them all from one cron on a
shared token budget:

```bash
# list every scheduled company / spot dead installs
bash plugin/skills/self-company/scripts/schedule.sh list

# holding-company mode: one cron drives the sub-companies in .company/org/subsidiaries.md
bash plugin/skills/self-company/scripts/schedule.sh install-fleet <parent-repo>
```

The parent runs the cheap deterministic pass for every sub-company each tick and spends
the expensive consolidation agent only where entropy actually rose (capped by
`FLEET_AGENT_BUDGET`). It orchestrates scheduling + budget only — it never touches a
sub-company's memory.

You wake up to `ops/reports/ledger.md` (the run ledger) and
`ops/plans/proposals-<date>.md` (pending improvement proposals).

## Privacy

`.company/` is git-ignored and project-scoped: memory, logs, and org config stay
local and are never committed or shared across projects.
