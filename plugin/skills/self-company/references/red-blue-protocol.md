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
  assignee, Gibby is required; if absent, Gibby is **auto-injected**. Arming is a pure
  builder-presence check — the task's TEXT plays no part in it (see the superseded-heuristic note
  below).

**Finalization pass — what it does and does NOT close (Gibby's adversarial pass, 2026-07-18, is honest about the limits):**

The two mechanisms below **raise the cost of forgery and handle the accidental/non-adversarial
case**, but Gibby proved neither fully closes its target against an **actively-malicious builder**,
because Bob, Gibby, and the supervisor are unprivileged **same-uid sibling processes** sharing one
host with a live `/proc` and no uid/namespace isolation. They are kept as defense-in-depth and as
the scaffolding for the sound fixes (noted inline), NOT as airtight controls.

- **Verdict NONCE — raises the bar; does NOT close echo-forgery vs. a malicious builder.**
  `_dispatch_redblue` mints a fresh `secrets.token_hex(8)` nonce per gate run and embeds it in
  **Gibby's dispatch prompt only** (`_verdict_contract`); the sentinel becomes `@qa-verdict <NONCE>
  {json}` and `_extract_qa_verdict` **requires** the exact nonce (plain `==` — single-use, and it
  won't raise on a hostile non-ASCII token the way `secrets.compare_digest` would). The **parser
  and fd-attribution are sound** (every wrong/absent/echoed-without-nonce case is correctly
  ignored; a nonce-bearing line on a NON-attacker fd is still ignored). **The hole is the nonce
  DELIVERY, not the check:** the prompt (nonce included) is passed as a `claude -p` **argv element**
  (`Member.real_command`), and on Linux any same-uid process reads another process's full argv from
  `/proc/<pid>/cmdline` (this host has no `hidepid`). Bob's worker runs concurrently with Gibby's,
  so a full-Bash Bob scans `/proc/*/cmdline`, lifts the nonce in <1s, and plants
  `@qa-verdict <leaked-nonce> {"verdict":"pass"}` for Gibby to echo — the exact echo-forgery this
  was meant to stop, now of the **enforcement** verdict, not just the display. **Sound fix (future,
  not done):** deliver Gibby's prompt via **stdin / an anonymous pipe**, never argv — stdin content
  is not `/proc/cmdline`-readable and is consumed on read, so the nonce would actually be secret.
- **Capture timeout — bounds the accidental hang; does NOT bound a deliberate DoS.** `company-run.sh`
  wraps the stderr-capturing command substitution in `timeout` (env `SELF_COMPANY_GATE_CAPTURE_TIMEOUT`,
  with `SELF_COMPANY_TIMEOUT_KILL_AFTER`). `timeout` only signals its **direct child** (the
  supervisor); a worker that `setsid`-detaches and holds a duplicated reference to
  `/proc/<supervisor-pid>/fd/2` escapes the process-group kill entirely, and in the common case
  (supervisor exits cleanly while a rogue worker persists) the `$(...)` still hangs for the worker's
  full lifetime with `rc=0` — not even classified as a timeout. **Sound fix (future, not done):**
  deliver the gate result via a **file the supervisor writes**, and `wait` on the **supervisor PID**
  with a timeout, instead of a pipe-EOF command substitution a grandchild can wedge.
- **Non-builder content-refusal REMOVED — superseded by Phase 34 (this part is genuinely closed).**
  The interim heuristic (`_looks_like_code_mutation`) is removed, not disabled: Phase 34's per-worker
  `--disallowedTools Bash Write Edit NotebookEdit` fence makes a non-builder **structurally unable to
  mutate source** (verified against a real `claude -p`), so there is no task text to evade and no
  false-refusal of legitimate read/review tasks. Builder-presence ARMING (a builder in the plan
  auto-injects Gibby) is unchanged.

**What the gate DOES reliably provide (its actual value):** Gibby can no longer **silently
rubber-stamp** — a genuine `fail` forces bounded Bob-fix/Gibby-reattack re-loops (cap default 3,
`SELF_COMPANY_REDBLUE_MAX_ROUNDS`), cap-without-pass is **UNRESOLVED** (loud, rc≠0), and the
parser/pipe-identity/first-wins-lock all hold. That closes the "Gibby waved it through while real
defects remained" problem this phase was built for.

**What it does NOT provide (documented, honest — needs the fixes above + OS isolation):**
forgery-resistance against an **actively-malicious or prompt-injected builder** that scans `/proc`.
Such a builder can (a) lift the verdict nonce from Gibby's argv and forge an accepted **enforcement**
verdict, (b) `setsid`-wedge company-run's capture (DoS), and (c) forge the human-facing
**ledger-display** row. The enforcement decision is made inside the supervisor's own process, so
(c) alone can't flip it — but (a) can. Fully closing (a)/(b)/(c) needs the stdin-delivery and
file-based-result fixes above **and** ultimately OS-level uid/namespace isolation between supervisor
and workers. Tracked as the sound follow-up; **shipped as defense-in-depth, not a security
guarantee, and labeled as such so no one over-trusts it.**

---

## Robustness follow-up (2026-07-21) — tolerant extractor, simpler contract, diagnostic UNRESOLVED

**What happened.** The gate's first real dispatch (the verified-decay task, `3a4a630`) came back
**UNRESOLVED** even though the work was correct (CEO-verified: functional proof, full suite green,
code review). Not a security failure — the nonce and pipe-identity design held exactly as designed.
The problem was the opposite: the real `claude -p` Gibby's genuine verdict did not reproduce the
strict `@qa-verdict <NONCE> {json}` sentinel closely enough for `_extract_qa_verdict` to recognize
it, so the supervisor saw no valid verdict and failed closed — a **false negative**, over-blocking
correct work. Asking an LLM to reproduce a 16-hex-char secret AND hand-build well-formed JSON
verbatim, as the literal last line of its response, turned out to be brittle in practice.

**The fix — format tolerance, not a security change.** The nonce requirement is completely
unchanged: a line with a wrong or absent nonce, or a nonce-bearing line on a non-attacker fd, is
still ignored exactly as before (every pre-existing security test in `tests/test_redblue_gate.py`
stays green, unmodified). What changed is what's accepted **once the nonce authenticates a line**:

- **Tolerant extractor.** `_extract_qa_verdict` (via `_parse_qa_payload`) now accepts a bare
  `pass`/`fail` keyword, case-insensitive, tolerant of surrounding whitespace and trailing prose on
  the same line (`pass`, `PASS`, `fail - two edge cases found`, `pass.`) — the new CANONICAL,
  easiest-to-emit form — in addition to the original `{"verdict": "pass"|"fail", ...}` JSON object
  (kept for back-compat). Both forms require the exact nonce first; neither is accepted without it.
- **Simpler, must-emit contract.** `_verdict_contract` now leads with the literal copy-paste line
  (the actual nonce substituted in) — `@qa-verdict <NONCE> pass` or `@qa-verdict <NONCE> fail` — as
  the mandatory, ONLY-thing-read-as-verdict last line of Gibby's response, spelled out verbatim so
  there's nothing left to compose. The JSON form is still mentioned as an accepted alternative, but
  is no longer what Gibby is steered toward reproducing exactly.
- **Diagnostic UNRESOLVED.** A nonce-AUTHENTICATED line whose trailing content is neither recognized
  form (`_qa_verdict_format_miss`) — the exact shape of the live false negative — is classified
  DISTINCTLY from "no authenticated line at all," and never silently read as a pass (fail-closed is
  unchanged). `_unresolved_reason` labels a cap-reached cycle as one of `genuine_fail` (Gibby really
  failed it), `format_miss` (an authenticated but unparseable line — likely a sentinel-format miss,
  not a real fail), or `no_verdict` (nothing authenticated was ever captured — most likely a
  dispatch/harness problem). Surfaced in the supervisor's stderr UNRESOLVED message and folded into
  the `company-run.sh` ledger's verdict cell (`unresolved (format_miss)`, etc.) so a human/CEO
  reading a stalled cycle can tell a genuine fail from a format miss at a glance, without re-reading
  the worker transcripts.

**What did NOT change:** the nonce is still required end to end; pipe-identity attribution,
first-wins+locking, and the bounded re-loop cap are all untouched. This is purely a
format-recognition widening on the ALREADY-authenticated channel, not a relaxation of who can
satisfy the gate.

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
- v2.6: 2026-07-18, Phase 33 finalization (v0.1.19). Verdict nonce + capture timeout added as DEFENSE-IN-DEPTH (they raise the bar but do NOT close forgery/DoS vs. an actively-malicious builder — the nonce leaks via `/proc/<pid>/cmdline` argv, the timeout is escapable via `setsid`; sound fixes = stdin prompt delivery + file-based gate result + OS isolation, documented as future work). The non-builder content-refusal heuristic IS genuinely removed, superseded by Phase 34's structural tool fence. What the gate reliably provides: Gibby can't silently rubber-stamp (re-loop + UNRESOLVED + pipe-identity). See "Finalization pass" above for the honest limits.
- v2.7: 2026-07-21, robustness follow-up (v0.1.20). Fixes the gate's first live FALSE-NEGATIVE (a correct build came back UNRESOLVED because the real Gibby's genuine verdict didn't reproduce the JSON-only sentinel closely enough). `_extract_qa_verdict` now also accepts a bare `pass`/`fail` keyword after the nonce (still REQUIRED, unchanged); `_verdict_contract` leads with the literal copy-paste keyword line instead of hand-built JSON; a nonce-authenticated-but-unparseable line is classified distinctly (`format_miss`) from no-authenticated-line-at-all (`no_verdict`), surfaced in the UNRESOLVED message and ledger so a human can tell a genuine fail from a format miss. Pure format tolerance on the already-authenticated channel — no security change; every prior security test stays green unmodified. See "Robustness follow-up (2026-07-21)" above.
