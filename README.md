# self-company

A multi-agent **personal company** for Claude Code: a small org of personas (Elon
CEO, Phoebe PM, Tony, Gibby, Bob, Tom, July) that learns your habits and
preferences across sessions and continuously fights entropy through tiered memory,
a verification loop, and decay. Private and project-scoped — all data lives in a
git-ignored `.company/` folder that never leaves your machine.

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
bash skills/self-company/scripts/install-hook.sh install   # CAPTURE + catch-up hooks
bash skills/self-company/scripts/schedule.sh install        # the scheduled work below
```

## Update

```
/plugin update
```

Updates track `main`. Because code and data are separated, an update refreshes the
skill's logic while your `.company/` data is untouched.

> **After updating, re-run** `schedule.sh install` and `install-hook.sh install`.
> The cron and hook lines are absolute-path snapshots taken at install time, so
> re-running them re-points cron/hooks at the updated skill.

## What runs on a schedule

Once `schedule.sh install` is run, two OS cron entries do the unattended work
(local, no cloud — your memory never leaves the box):

| Schedule | Job | What it does |
|---|---|---|
| every 6h (`00:07 / 06:07 / 12:07 / 18:07`) | `daily-run.sh` | decay stale memories, verify provenance, compute the entropy KPI, a conservative consolidation pass, and one Tony improvement proposal |
| weekly (`Sun 03:23`) | `research-scan.sh` | Tony surveys the web for skill improvements and files proposals |

Event-driven (not cron): a `Stop` hook captures memories in real time, and a
`SessionStart` hook surfaces the scheduled-work report when you open a session.

You wake up to `ops/reports/ledger.md` (the run ledger) and
`ops/plans/proposals-<date>.md` (pending improvement proposals).

## Privacy

`.company/` is git-ignored and project-scoped: memory, logs, and org config stay
local and are never committed or shared across projects.
