# Red/Blue Adversarial Protocol (Red/Blue Adversarial Hardening)

> The confrontation between Bob (Blue, build + defend) and Gibby (Red, attack) is not just "test and move on" —
> **each attack makes the system permanently stronger**. The red/blue ledger is the system's immune memory, defenses only grow,
> the system becomes monotonically more robust over time.

This is the heart of the build pipeline (see §3 Pipeline A). The memory pipeline (Pipeline B) applies the same adversarial spirit in VERIFY, but targets memory provenance.

> **These invariants are now MACHINE-ENFORCED (Phase 9).** Per-company schedule and
> per-employee duties are configurable via `org/schedule.yaml`, but that config can
> only tune *when* and *which* duties run — it can **never** reassign a role,
> uncover the attack surface, or touch the sign-off gate. `schedule_validator.py`
> encodes the invariants as **rules R1–R6** and REFUSES any config that violates
> them, falling back to defaults + logging the named rule (a mis-configured
> competition never runs). R1 attacker≠builder · R2 attack surface must stay covered
> when a build is active · R3 the 3-consecutive sign-off gate is not tunable ·
> R4 dispatch topology (Phoebe gateway / July HR-tuning / Tony≠Gibby) preserved ·
> R5 ledger integrity / immune-memory never disabled · R6 no `role:`/`tier:`/
> `attacks:` field exists — its presence is a hard reject. The rules used to live
> only in this prose; they are now checked in code (`schedule_validator.py`, tests
> in `tests/test_schedule_validator.py`).

---

## Role

| | **Bob · Blue Team** | **Gibby · Red Team** |
|---|---|---|
| Mindset | Build + **harden** | **Assume it's broken, I will find the break** |
| When broken | Not just patch the hole — add defense so "this class" of attack can't succeed again + lock in a regression test | Record in the ledger, come back from a different angle |
| Win condition | **3 consecutive** rounds unbroken → system hardened | Find any real break |

> The adversarial tension itself is the quality engine: Gibby wants to break, Bob wants to hold, both forces push the system toward robustness.

---

## Attack-Surface List (Gibby rotates each round, no repeats)

| Attack Surface | Target |
|---|---|
| **Correctness** | Logic errors, boundary calculations off, off-by-one, type misuse |
| **Malicious / Malformed Input** | Null, None, too-long, special characters, injection, encoding anomalies |
| **Concurrency** | Race condition, order dependency, reentrancy, shared state pollution |
| **Resources** | Exhaustion (memory/file handles), leaks, infinite loops, large input degradation |
| **spec drift** | diff against Phoebe's spec, doing doesn't match requirement |
| **Regression** | Re-run all old attacks from the ledger, confirm no holes reopen |

Gibby's arsenal (by attack surface): pytest, live code/CLI runs, fuzz, linter, type checker, static analysis, Playwright MCP (UI), diff spec, memory provenance queries.

---

## Red/Blue Adversarial Loop (N=3)

```
Phoebe issues spec/plan → Bob builds (v1 with basic defense)
       │
       ▼
  ┌────────────────────────── Round k ──────────────────────────┐
  │ Gibby picks one "untested" attack surface and makes a move  │
  │   ├─ Break                                                   │
  │   │    1. Record in red/blue ledger (attack surface, move, repro steps) │
  │   │    2. Bob not just patches — add defense (guard/verify/invariant) │
  │   │    3. Bob locks this attack into a regression test       │
  │   │    4. Reset count → Gibby rotates back to attack-surface list │
  │   └─ No break → increment consecutive-unbroken count, Gibby picks next attack surface │
  └─────────────────────────────────────────────────────────────┘
       │
       ▼
  3 consecutive rounds, different attack surfaces, none broke → stand down, system deemed hardened ✓
       │
       ▼
  Gibby reports to Phoebe: hardened, with red/blue ledger summary for this round
```

**Key Discipline:**
- **"Break resets the count"** — Any round broken, consecutive-unbroken count goes to zero, Gibby rotates attack surfaces. Ensures hardened means "still unbroken 3 rounds after repair", not "tried 3 times total".
- **Regression is mandatory** — Each round before attacking, Gibby runs all old attacks' regression tests from the ledger. Old hole reopens = immediate break, highest priority.
- **Defenses only grow, never shrink** — Bob's guards/tests are retained permanently, even after refactor. This is the physical guarantee of "the more it's hit, the more robust it gets".

---

## Red/Blue Ledger (`ops/red-blue/ledger.md`)

Each attack interaction leaves one entry; **old entries never delete** — this is the system's immune memory.

```markdown
## <date> · <target:task or file>

| Round | Attack Surface | Gibby's Move | Result | Bob's Defense | Lock in Regression |
|---|---|---|---|---|---|
| 1 | Malicious Input | Empty list → crash | 🔴 Break | Add guard + default | test_empty_list |
| 2 | Concurrency | Two writers simultaneously → race | 🔴 Break | File lock | test_concurrent_write |
| 3 | Boundary | Huge input (1M records) | 🟢 Hold | — | — |
| 4 | spec drift | diff against plan | 🟢 Hold | — | — |
| 5 | Regression | Re-run R1/R2 tests | 🟢 Hold | — | — |

**Conclusion:** 3 consecutive rounds (R3–R5) unbroken → hardened ✓
**New regression tests:** test_empty_list, test_concurrent_write (retained permanently)
```

The ledger's value:
1. **Regression baseline** — Old attacks become permanent tests, system won't regress.
2. **Attack knowledge accumulation** — Next time, Gibby knows which surfaces are hardened, prioritizes new angles.
3. **Audit trail** — Chairman/July can see how many times the system was hit, how many patches, quantify robustness.

---

## Machine-enforced gate (Phase 33) — supervisor layer, not a hook

The red/blue sign-off is no longer convention-only: `supervisor.py` **machine-enforces**
Gibby's verdict on every dispatched build. Key design points (so they are not re-litigated):

- **Why the supervisor layer, not a `SubagentStop` hook.** self-company dispatches workers as
  `claude -p` **subprocesses** (see `supervisor.py` `real_command`), NOT Task-tool subagents of
  the session. Claude Code's `SubagentStop` fires only for Task-tool subagents, so a hook scoped
  to Gibby would **never fire** — a no-op gate that only *looks* like enforcement. The gate lives
  where the red/blue cycle actually runs: the supervisor dispatch loop.
- **Verdict attribution is by PIPE IDENTITY.** Gibby emits its verdict as a reserved
  `@qa-verdict {"verdict":"pass"|"fail",...}` sentinel on its **own stdout**, which the supervisor
  reads off Gibby's specific pipe fd (`Worker.capture_verdict`). Bob and Gibby are concurrent
  subprocesses sharing one filesystem, so an earlier design that wrote the verdict to a
  shared-fs marker file was **forgeable by Bob** (race/overwrite, and the round-N path leaked into
  Bob's own prompt). A worker cannot write another worker's stdout pipe — that OS boundary is the
  attribution. A `@qa-verdict` line on any other worker's fd is ignored. The verdict is
  **first-wins + locked** (a later echoed sentinel can't flip it), with Gibby's final stream-json
  `result` event preferred as authoritative.
- **The gate result is on the supervisor's own stderr**, not a file. `supervisor.py` prints a
  `@redblue-gate {json}` line on its own stderr; `company-run.sh` captures that trusted channel for
  the ledger. No shared-fs artifact a worker could overwrite to forge the human-facing verdict cell.
- **Bounded re-loop.** missing/malformed/`fail` ⇒ not clean ⇒ re-dispatch Bob(fix)+Gibby(re-attack)
  up to `SELF_COMPANY_REDBLUE_MAX_ROUNDS` (default 3, env-tunable, clamped to default on a
  non-positive/absurd value). Cap-without-pass ⇒ **UNRESOLVED** (loud, non-zero exit), never a
  silent "done".
- **Arming is enforced, not the planner's discretion.** If a plan contains any builder-duty
  assignee, Gibby is required; if absent, Gibby is **auto-injected**. A plan routing code-mutation
  work to a **non-builder** (which would skip arming) is **refused loudly** by a conservative
  content check (a code-mutation verb + a code-file/path signal in the task) — read-only tasks are
  never refused.

**Known limitation → follow-up (Phase 34):** the non-builder refusal is **defense-in-depth, not
airtight** — a cleverly-worded build task can evade the text heuristic. The sound fix is
**per-worker `--allowedTools` tool restriction** (fence each worker to the tools its role needs, so
a non-builder physically cannot mutate source), tracked as **Phase 34**. Until then the content
check is a tripwire, not a wall.

---

## Relationship with Memory Pipeline VERIFY

Memory Pipeline B's VERIFY (§3 design) is a "lightweight version" of this adversarial approach: Gibby makes a single attack surface against each memory record — **provenance attack** (can't point back to real source, reject), send back to CAPTURE, discard after `VERIFY_MAX_RETRY=2`. Pipeline A is the full multi-surface, N=3 red/blue adversarial.

---

## Win and Performance

- Gibby's KPI (July evaluates): **break rate** (proportion of true breaks found), attack-surface coverage, regression rigor.
- Bob's KPI (July evaluates): **rounds survived**, defense depth (patch one hole, stop a class), regression lock-in rate.
- The two oppose, but same team — goal is to harden the system, not defeat each other. Gibby finding a hole is win-win (system gets stronger), Bob holding is win-win (quality reaches target).

---

## Version
- v2.5: 2026-06-24, red/blue protocol, N=3. Full build pipeline implementation (Bob really writes code, Gibby really attacks) pending later activation; this protocol is its behavioral spec.
