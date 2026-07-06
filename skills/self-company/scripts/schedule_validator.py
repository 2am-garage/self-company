#!/usr/bin/env python3
"""
schedule_validator — enforce the red/blue Layer-B invariants against a company's
`org/schedule.yaml`. THE centerpiece of Phase 12.

The Chairman's worry: making per-employee schedule/duties configurable could break
the red/blue competition (Gibby-attack vs Bob-build), the positions, or the
3-consecutive-unbroken sign-off gate — SILENTLY, because those rules lived only in
prose (references/red-blue-protocol.md + personas). This module makes them
MACHINE-CHECKED: a config that would violate an invariant is REJECTED, and every
caller (schedule.sh, daily-run.sh, hook_schedule_guard.sh) falls back to defaults
+ logs the named rule. A mis-configured competition never RUNS; it fails closed.

Rules (each sourced from references/red-blue-protocol.md):
  R1  attacker != builder — no employee may hold an attack-class and a build-class
      duty; and a duty may never be granted outside the employee's fixed role set
      (Gibby can't build, Bob can't attack/verify, Tony can't build, ...).
  R2  attack surface covered — if Bob's `build` is active, Gibby must be enabled
      and keep `attack`. You cannot ship builds with no red team.
  R3  sign-off gate not tunable — no key may set/override the "3 consecutive
      unbroken" count, reset, or defenses-only-grow. Such keys do not exist in the
      schema; presence is rejected.
  R4  dispatch topology preserved — Phoebe=gateway, July=HR-tuning, Tony!=Gibby.
      Config carries no routing field; any key outside the known schema is rejected.
  R5  ledger integrity — no key may disable the red/blue ledger or the
      "old entries never delete" immune-memory rule.
  R6  no role field — the schema has no role/tier/attacks field; presence is a hard
      reject (fail-closed against a future footgun).

R3–R6 fall out of ONE general structural rule — "every key must be in the known
schema, and a forbidden footgun key is a hard reject" — plus the fixed role tables
in schedule_config.py. That is the modular design: not six special-cased clauses
but a whitelist + a fixed topology.

Exit codes: 0 = valid, 3 = invalid (violations printed to stdout, one per line).
Never raises; a parse failure is reported as a violation, not a crash.

Usage:
  schedule_validator.py --company DIR [--config FILE] [--quiet]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schedule_config as sc  # shared schema + role tables (one source of truth)


def _load(company, config):
    """Return (raw_dict, parse_error_or_None)."""
    if config:
        p = Path(config)
        if not p.exists():
            return {}, None
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            return {}, f"cannot read {config}: {e}"
        try:
            import yaml
            data = yaml.safe_load(text)
            if data is None:
                return {}, None
            if not isinstance(data, dict):
                return {}, "top-level YAML is not a mapping"
            return data, None
        except ImportError:
            try:
                return sc._fallback_parse(text), None
            except Exception as e:
                return {}, f"parse error: {e}"
        except Exception as e:
            return {}, f"parse error: {e}"
    # no explicit file -> use the company's schedule.yaml (may be absent = {})
    return sc.load_raw(company), None


def _keys_deep(d):
    """Yield every mapping KEY appearing anywhere in the config (top + nested)."""
    if isinstance(d, dict):
        for k, v in d.items():
            yield str(k)
            yield from _keys_deep(v)
    elif isinstance(d, list):
        for v in d:
            yield from _keys_deep(v)


def validate(raw, parse_error=None):
    """Return a list of violation strings (empty = valid)."""
    v = []
    if parse_error:
        return [f"PARSE: {parse_error} — cannot trust config, using defaults"]
    if not isinstance(raw, dict):
        return ["PARSE: top-level config is not a mapping"]

    # R6 / R3 / R5 — forbidden footgun keys anywhere (fail-closed).
    for k in _keys_deep(raw):
        if k.lower() in sc.FORBIDDEN_KEYS:
            v.append(
                f"R6: forbidden key '{k}' — role/gate/ledger topology is Layer B, "
                "not configurable"
            )

    # R4 — structural whitelist: unknown top-level keys are rejected (this is how
    # dispatch-routing / sign-off keys are refused without special-casing each).
    for k in raw:
        if str(k) not in sc.TOP_KEYS:
            v.append(f"R4: unknown top-level key '{k}' — not in the schema")

    # Per-employee blocks: only known keys, and duties within the fixed role set.
    for name in sc.EMPLOYEES:
        block = raw.get(name)
        if block is None:
            continue
        if not isinstance(block, dict):
            v.append(f"R4: '{name}' must be a mapping")
            continue
        for k in block:
            if str(k) not in sc.EMPLOYEE_KEYS:
                v.append(f"R4: '{name}.{k}' is not an allowed employee key")
        duties = block.get("duties", [])
        if duties is None:
            duties = []
        if not isinstance(duties, list):
            v.append(f"R1: '{name}.duties' must be a list")
            duties = []
        dset = {str(d) for d in duties}
        # R1a — a duty outside this employee's fixed role set (Gibby can't build,
        # Bob can't attack/verify, Tony can't build, ...).
        stray = dset - sc.ALLOWED_DUTIES[name]
        for d in sorted(stray):
            v.append(
                f"R1: '{name}' may not own duty '{d}' — outside {name}'s fixed role "
                f"(allowed: {sorted(sc.ALLOWED_DUTIES[name]) or 'none'})"
            )
        # R1b — no single employee holds BOTH an attack-class and a build-class duty.
        if (dset & sc.ATTACK_DUTIES) and (dset & sc.BUILD_DUTIES):
            v.append(f"R1: '{name}' holds both attack and build duties — attacker != builder")

    # R2 — if the build surface is active, the attack surface must be covered.
    def _emp(name):
        b = raw.get(name)
        return b if isinstance(b, dict) else {}

    def _duties_of(block, name):
        # P9-D1: distinguish ABSENT duties (fall back to the role's full default
        # set) from an EXPLICIT empty list (the employee owns NO duty). The old
        # `block.get("duties") or DEFAULT` idiom treated `duties: []` as absent —
        # so `gibby: {duties: []}` silently substituted gibby's full set and R2
        # waved through a build with no red team. An explicit [] must mean [].
        dd = block.get("duties")
        if isinstance(dd, list):
            return {str(d) for d in dd}
        return set(sc.ALLOWED_DUTIES[name])

    bob, gib = _emp("bob"), _emp("gibby")
    bob_enabled = bob.get("enabled", True) is not False
    bob_duties = _duties_of(bob, "bob")
    bob_builds = bob_enabled and bool(sc.BUILD_DUTIES & bob_duties)
    if bob_builds:
        gib_enabled = gib.get("enabled", True) is not False
        gib_duties = _duties_of(gib, "gibby")
        if not gib_enabled:
            v.append("R2: Bob builds but Gibby is disabled — attack surface uncovered")
        elif not (sc.ATTACK_DUTIES & gib_duties):
            # Covers an EXPLICIT empty gibby duty list too (P9-D1 regression).
            v.append("R2: Bob builds but Gibby has no 'attack' duty — attack surface uncovered")

    # NOTE on cadence (Gibby's related observation): R2 deliberately inspects the
    # attack DUTY assignment, NOT a per-employee sub-cadence. `attack`/`build` are
    # DISPATCHED competition work (Phoebe -> Gibby/Bob), not scheduled batch steps
    # (they are absent from STEP_OWNER). A sub-cadence only gates the deterministic
    # daily-run BATCH, so `gibby: {cadence: on-trigger}` cannot remove the red team
    # from the competition loop — hence cadence is correctly out of R2's scope.

    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument("--config", default=None, help="validate this file instead of the company's")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    raw, err = _load(a.company, a.config)
    violations = validate(raw, err)
    if violations:
        if not a.quiet:
            for line in violations:
                print(line)
        sys.exit(3)
    if not a.quiet:
        print("ok")
    sys.exit(0)


if __name__ == "__main__":
    main()
