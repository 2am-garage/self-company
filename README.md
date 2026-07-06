# self-company

A multi-agent **personal company** for Claude Code: a small org of personas (Elon
CEO, Phoebe PM, July HR, Bob Build, Gibby QA, Tony Improvement, Tom IT, Mike R&D)
that learns your habits and preferences across sessions and continuously fights
entropy through tiered memory, a verification loop, and decay. Private and
project-scoped — all data lives in a git-ignored `.company/` folder that never
leaves your machine.

Full skill docs: [`skills/self-company/SKILL.md`](skills/self-company/SKILL.md).

## Install (as a plugin)

```
/plugin marketplace add 2am-garage/self-company
/plugin install self-company
```

Then create the private company in your project and (optionally) wire up the
automation:

```bash
# creates ./.company/ (git-ignored data: memory, ops, org config)
bash skills/self-company/scripts/init_company.sh

# optional automation, opt-in and local:
bash skills/self-company/scripts/schedule.sh install        # the scheduled work below
```

> Hooks need **no** setup: since v0.1.2 all 7 hooks are **plugin-native** (declared in
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

> **After updating, re-run** `schedule.sh install`. The cron lines are absolute-path
> snapshots taken at install time, so re-running re-points cron at the updated skill.
> Hooks need no re-install — they are plugin-native and use `${CLAUDE_PLUGIN_ROOT}`.

## What runs on a schedule

Once `schedule.sh install` is run, two OS cron entries do the unattended work
(local, no cloud — your memory never leaves the box):

| Schedule | Job | What it does |
|---|---|---|
| every 6h (`00:07 / 06:07 / 12:07 / 18:07`) | `daily-run.sh` | decay stale memories, verify provenance, compute the entropy KPI, a conservative consolidation pass, and one Tony improvement proposal |
| weekly (`Sun 03:23`) | `research-scan.sh` | Mike surveys the web for skill improvements and files proposals |

Event-driven (not cron): **7 plugin-native hooks** work automatically. `Stop`/`PreCompact`
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
bash skills/self-company/scripts/schedule.sh list

# holding-company mode: one cron drives the sub-companies in .company/org/subsidiaries.md
bash skills/self-company/scripts/schedule.sh install-fleet <parent-repo>
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
