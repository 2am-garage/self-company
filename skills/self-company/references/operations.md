# Operations reference

How to run and wire the company's day-to-day operations. Four areas:

1. **[Triggers](#triggers--three-ways-the-company-starts-working)** — the four ways the company starts working (call / clock / event / session), session vs headless dispatch, the §5.5 chain, the event-driven `fire-trigger.sh` flow, poll adapter.
2. **[Session Catch-Up Notification](#session-catch-up-notification-chairman-opt-in-option-b)** — the `SessionStart` hook, `notify-status.py --emit-hook`, push-only rule, manual fallback.
3. **[Scheduled-Work Ledger](#scheduled-work-ledger-autoresearch-style-report)** — `report.py`, `ops/reports/ledger.md`, the entropy-headline table, verdict vocabulary.
4. **[On-demand views](#on-demand-views-chairman-asks--render-inline)** — the "Chairman says → run → shows" table, `org-status.py` snapshot, `supervisor.py` live harness.

---

## Triggers — three ways the company starts working

| # | Trigger | Mechanism | Fired by |
|---|---|---|---|
| 1 | Chairman calls | conversation | the Chairman |
| 2 | Clock | cron → `daily-run.sh` (every 6h) | time |
| 3 | **Event** | **`fire-trigger.sh <name> <payload>`** (push) | any external program / user-defined |
| 4 | **Session** | **`company-run.sh "<task>"`** | the interactive session (Elon hands work to the company) |

**Trigger #4 (session).** Per MISSION.md, this repo is run by the self-company to
improve the self-company. Rather than Elon silently editing every file, the
session hands a task to the company.

There are two dispatch paths — pick by origin:

**Session dispatch (default when the Chairman is present) — real, visible agents.**
When a cycle is triggered from an interactive session, dispatch employees as REAL
subagents via the **harness Agent tool** (not a script). These are genuinely
separate agents AND the Claude app renders them live. Follow the chain and the
reporting rule (policy.md §5.5):

1. **Phoebe** (Agent) plans a `{employee: subtask}` assignment.
2. **Assigned workers** (Agent, e.g. Bob) implement. Use `isolation: worktree`
   when workers edit code in parallel, to avoid conflicts; sequential single-owner
   edits need no worktree.
3. **Gibby** (Agent) verifies — reads the diff, runs the suite, sanity-runs the tool.
4. Workers and Gibby **report to Phoebe**; Phoebe aggregates and **reports to Elon**.
5. **Elon** resolves small tasks with Phoebe; escalates big ones to the Chairman.

> A bash script cannot call the Agent tool — app-visible subagents are inherently
> spawned by the main session. So this path is driven by the session agent (Elon),
> documented here as the standard operating procedure, not by `company-run.sh`.

**Headless dispatch (cron / external trigger / no session) — portable, text-only.**
`company-run.sh "<task>"` has Phoebe plan, then `supervisor.py` spawns the assigned
employees as live child processes (`claude -p`); the cycle is logged to
`ops/reports/company-runs.md`. Not app-visible (native widgets belong to the host),
but fully modular. `--demo` runs the whole flow with no LLM.

**Trigger #3 (event-driven)** is push-first: the company is dormant until an
external producer (a training run, trading bot, CI job, …) fires it — no polling,
no daemon. Triggers are **user-defined**, declarative, one file per trigger under
`org/triggers/<name>.yaml` (flat `key: value`; see `org/triggers/README.md`). The
engine is never edited:

```
your program ── fire-trigger.sh training-done '{"val_bpb":0.98}' ──┐
                                                                    ▼
   trigger_engine.py: eval condition → guards(cooldown/dedupe/daily-cap)
                                                                    │ pass
                                          detached, bounded `claude -p` → Phoebe
```

Decision is deterministic and testable (`trigger_engine.py`); orchestration —
the bounded, recursion-guarded, detached agent — lives in `fire-trigger.sh`, the
same split as `daily-run.sh`. Every call (fired or held) is appended to
`ops/reports/triggers.md`. For sources that *cannot* call us, an optional cron
**poll adapter** can check them and call the same entry point — push primary,
poll only as a fallback.

---

## Session Catch-Up Notification (Chairman opt-in: "Option B")

The unattended daily cron (`schedule.sh`) runs silently and only writes logs. The
Chairman shouldn't have to dig through logs, so this is now **automated via a
`SessionStart` hook** (installed by `install-hook.sh` alongside the Stop/CAPTURE
hook):

- On session start the hook runs `notify-status.py --emit-hook`. If there are new
  background runs AND they are **substantive** (entropy or memory count moved,
  something decayed, or there are pending TODOs), it injects a `SessionStart`
  `additionalContext` line telling the agent to send **one** `PushNotification`
  with the summary — **push only, never Discord** (per the Chairman's
  `push-notification-only` preference). The script self-acks, so the same window is
  never pushed twice.
- If nothing substantive changed, it silently acks and emits nothing — zero noise
  on quiet days. This is the gate the Chairman asked for: notify only on real change.

When you receive that `additionalContext`, also state the one-line summary in your
reply — PushNotification suppresses while the Chairman is actively typing (~60s),
so the in-chat line guarantees he sees it even when the push is held back. The
payload also embeds the recent scheduled-work ledger (see below); render it inline
in your reply so the Chairman sees the report here, not just a file path.

Manual fallback (hook absent / ad-hoc check): run `notify-status.py`, and if
`new_runs > 0` push the `summary`, then `notify-status.py --ack`.

This is how the silent local cron reaches the Chairman's phone without Discord or
a cloud agent: the cron does the work; the SessionStart hook relays the summary.

---

## Scheduled-Work Ledger (autoresearch-style report)

The push is a one-liner; the **report** is `ops/reports/ledger.md`, regenerated at
the end of every `daily-run.sh` by `report.py`. Modeled on Karpathy's autoresearch
`results.tsv`: one row per unattended run, a single headline metric (**entropy**,
lower = healthier — the `val_bpb` analog), a verdict, and a one-line description.

```
| run         | entropy ↓  | mem | status | what happened                  |
| 06-29 18:07 | 0.0356 v   | 45  | keep   | verify +14, merged 8 dup, ...  |
| 06-30 06:07 | 0.0400 =   | 40  | flat   | no-op maintenance              |
```

Verdict: `keep` (something substantive moved), `flat` (clean but no change),
`skip` (agent step capped/absent), `fail` (agent errored). Run on demand with
`report.py --company .company` (`--write` to save, `--tsv` for the raw flat file).
This is the artifact the Chairman wakes up to.

### Surfacing the report mid-session (P1)

The SessionStart hook only fires on a **fresh** session, so in a long continuous
session the report never re-surfaces on its own. To fix that, **at the start of
each reply run `notify-status.py --company .company --delta`**; if it prints a
one-line summary (new *substantive* runs since you last surfaced one), lead your
reply with it. It uses a separate `.last_shown` marker (show-once) so it neither
spams nor collides with the SessionStart push. On demand the Chairman can always
say "report" → `report.py`.

### Improvement proposals (Tony, every scheduled run)

The scheduled `daily-run.sh` agent step also has **Tony** append one grounded
improvement proposal (or an explicit "no new proposal") to
`ops/plans/proposals-<date>.md` — so the company keeps proposing its own upgrades
even when the Chairman has none (MISSION.md; policy.md §6). **On session entry,
Elon surfaces any pending proposals** from `ops/plans/proposals-*.md` alongside the
report, so they are seen rather than buried in a file.

---

## On-demand views (Chairman asks → render inline)

When the Chairman asks for any of these, run the script and render its output
inline in your reply (don't just point at a file):

| The Chairman says… | Run | Shows |
|---|---|---|
| "report" / "/report" / "scheduled work" | `report.py --company .company` | the scheduled-work ledger |
| "who's working" / "org status" / "是不是 Elon 在做" | `org-status.py --company .company` | which employees acted recently + who is live now |

`org-status.py` is an honest **snapshot** view: it attributes recent activity from
the real logs (daily-run → Tony/Gibby/Elon/Tom, trigger ledger → Phoebe/Bob,
employee `log.md`, and running `claude -p` processes). Interactive chat is
Elon-fronted; the genuinely-separate work is the cron / dispatch / trigger agents
— the view makes that split visible rather than pretending seven daemons are
always busy.

### Live supervisor (`supervisor.py`) — the skill's own live harness

For a **live** tree of employees working (not a snapshot), `supervisor.py` is a
small, skill-owned orchestration harness: it spawns employees as **child
processes** and reads their stdout streams in real time via `select()`, so status
is event-driven and synced with the actual work — the supervisor IS the parent of
the process tree. It is ephemeral (exists only while work runs) and covers ALL
employees (discovered from `org/employees/`, not a hardcoded subset). OOP:
`Employee` / `Worker` / `Supervisor` / `LiveTree`. Status protocol: a worker
prints `@status <phase>` lines as it works; the same protocol serves a simulated
demo worker and a real `claude -p` agent, so the supervisor is host-agnostic.

- `supervisor.py --demo` — simulate all employees live (no LLM).
- `supervisor.py --dispatch '{"phoebe":"...","bob":"..."}'` — real agents.

Honest ceiling: in a real terminal this is a live TUI; viewed remotely in the
Claude app it streams as text (native widgets belong to the host — the one thing
no modular design can replicate). This is deliberately NOT bound to Claude Code.
