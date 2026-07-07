# Spec — <Phase / change title>

**Author:** Elon (CEO) with Phoebe (PM)  ·  **Date:** <YYYY-MM-DD>  ·  **Repo:** <name> (dev; skeleton edits allowed / or Chairman grant)
**Change-management pattern:** Elon+Phoebe write spec → Phoebe dispatches → Bob builds ⚔ Gibby attacks → Tony measures → Tom integration-checks → Elon closeout.
**Predecessor:** <link the previous phase spec, if any, + its verified outcome>

**Why now:** <the trigger — what live evidence makes this a big change worth the full loop>
**Scope guard:** Skeleton edits run ONLY where permitted (dev repo `.self-company-dev`, or `SELF_COMPANY_ALLOW_SKELETON=1`). Confirm with `bash scripts/skeleton_guard.sh` before dispatch. In a usage repo the company must NOT modify its own skeleton.

> Full process: `references/change-management.md`. Copy the block below once per work item.

---

## Item <N> — <short title>

**Problem.** <the concrete defect, one paragraph — what is wrong today.>

**Live evidence.** <what was actually observed: numbers, a failing run, a real bug. Not a hypothesis. "Observed live on <date>: …">

**Target.** <exact `script.py::function` (+ any reference/data file) the change lands in.>

**Change.** <what to do. Include fallbacks (graceful degradation for optional deps), invariants to preserve, and any data migration.>

**Acceptance.** <the checklist Gibby will attack: pass/fail conditions, named edge cases (empty input, missing venv, idempotency, safety asserts), "no network calls", "no regression to <existing path>".>

**Owner.** <usually Bob.>

---

## Item <N+1> — <short title>

**Problem.** …
**Live evidence.** …
**Target.** …
**Change.** …
**Acceptance.** …
**Owner.** …

---

## Cleanup / fold-in batch (optional)

Small hardening items too minor to spec alone but touching files a big item already owns —
ride them along on the same worker. One line each: item · fix · owner · acceptance.

- **C1 — <title>.** <fix> Owner: <name>. Acceptance: <condition>.

---

## Dispatch plan (Phoebe)

> **File-batching rule:** parallel workers must touch DISJOINT files. When two items touch
> the SAME file, give them to ONE worker sequentially — never two writers on one file.

| batch | items | file(s) | owner | notes |
|---|---|---|---|---|
| <B1> | <#N, #M> | <file(s)> | Bob | same file → one worker, serial within batch |
| <B2> | <#K> | <independent file> | Bob | independent → runs in parallel |
| — | attack all | tests/fixtures | Gibby | red-team each acceptance list; empowered self-fix on small defects (log to `ops/red-blue/ledger.md` first) |
| — | measure | scratch corpus | Tony | entropy before/after on a COPY of memory; report honestly, incl. what did NOT move |
| — | integration | cron/hook wiring | Tom | end-to-end parse/run without real cron/model; GO / NO-GO; skeleton guard still passes |
| — | closeout | all of the above | Elon | honest summary: shipped / regressed / deferred; update ledger |

**Guardrails.** Skeleton edits allowed (guard checked). Stdlib-only for hard deps; RAG/optional
features degrade gracefully. Test on scratch copies — do NOT mutate live `.company/memory`
during development. Each item ships only after Gibby's attack is clean; then Tony measures,
Tom integration-checks, Elon closes out.

**Deferred to the next phase (backlog, not this one):** <list — closeout turns "ran out of time" into a tracked item, never a lost one.>
