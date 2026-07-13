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
  R7  hire-as-data invariants (Phase 32) — a DISCOVERED (hired, non-core)
      employee's OWN desk (context.md), independent of schedule.yaml: (a) its
      declared `tier:` is worker or manager, never a charter-role claim
      (CEO/gateway/HR/QA — those stay code-pinned); (b) it may hold no
      attack-class or build-class duty — enforced by R1 itself once R1's own
      employee loop widens to `discover(company)` (ALLOWED_DUTIES has no entry
      for a hired id, so ANY duty it claims in schedule.yaml is "stray" —
      reused, not duplicated); (c) its `manager:` chain is acyclic and rooted
      at elon. Also flags (doesn't silently ignore) an org/employees/ directory
      whose name isn't core and fails the id charset — discover() already
      excludes it from dispatch; R7 additionally surfaces it so it doesn't sit
      inert forever unnoticed. Core 8 are EXEMPT (their invariants are R1-R6,
      unchanged).

R3–R6 fall out of ONE general structural rule — "every key must be in the known
schema, and a forbidden footgun key is a hard reject" — plus the fixed role tables
in employee.py (the authoritative Layer-B topology). That is the modular design:
not six special-cased clauses but a whitelist + a fixed topology. R7 is the same
idea applied to the discovered-employee DESK itself, not just schedule.yaml.

Exit codes: 0 = valid, 3 = invalid (violations printed to stdout, one per line).
Never raises; a parse failure is reported as a violation, not a crash.

Usage:
  schedule_validator.py --company DIR [--config FILE] [--quiet]
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schedule_config as sc  # config SCHEMA + parser (allowed/forbidden keys)
import employee as emp        # authoritative Layer-B role topology (single source)

# R7(a): a hired employee's `role:` text may not claim one of the FOUR charter
# singleton roles (Elon=CEO, Phoebe=execution gateway, July=HR line/lead,
# Gibby=QA sign-off). The spec says ANY PHRASING — so we do NOT rely on a
# brittle regex that only sees one separator (Phase 32 Bug 3: `execution-gateway`,
# `HR-Lead`, `qa-signoff`, `chief-executive-officer`, and double-spaced variants
# all slipped past the old `\b...\s+...` regex). Instead we NORMALIZE the role
# text — casefold, then collapse every run of non-alphanumerics (hyphens,
# underscores, whitespace, punctuation) to a single space — and word-boundary
# match the canonical charter phrases against it. Ordinary titles ("Build
# Engineer", "R&D Researcher", "QA Assistant") never match.
_CHARTER_ROLE_PHRASES = (
    "ceo",
    "chief executive",
    "chief executive officer",
    "execution gateway",
    "hr lead",
    "hr team lead",
    "human resources lead",
    "human resources team lead",
    "qa signoff",
    "qa sign off",
    "qa gate",
)


def _normalize_role_text(s):
    """Casefold + collapse every run of non-alphanumeric characters to a single
    space, so 'Execution-Gateway', 'execution  gateway', and 'EXECUTION_GATEWAY'
    all normalize to 'execution gateway'."""
    return re.sub(r"[^a-z0-9]+", " ", str(s).casefold()).strip()


def _claims_charter_role(role):
    """True iff `role` text claims (in any phrasing) one of the four charter
    singleton roles. Word-boundary substring match against the normalized text
    so 'CEO' matches the standalone token but not a longer word containing it."""
    padded = f" {_normalize_role_text(role)} "
    return any(f" {phrase} " in padded for phrase in _CHARTER_ROLE_PHRASES)


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


def validate(raw, parse_error=None, company=None):
    """Return a list of violation strings (empty = valid).

    `company` (Phase 32) is the company dir — needed so R4's schema whitelist
    and the per-employee loop can see HIRED desks too (via `emp.discover`), and
    so R7 can inspect those desks' own context.md directly. `company=None`
    (e.g. a bare `--config FILE` validate-only call) degrades exactly to the
    pre-Phase-32, CORE-only behavior: `discover(None)` -> CORE_EMPLOYEES."""
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
    # Phase 32: company-aware — a HIRED employee's own schedule.yaml block is a
    # valid top-level key too (top_keys(None) == the CORE-only TOP_KEYS).
    top = sc.top_keys(company)
    for k in raw:
        if str(k) not in top:
            v.append(f"R4: unknown top-level key '{k}' — not in the schema")

    # Per-employee blocks: only known keys, and duties within the fixed role set.
    # Phase 32: widened from the static CORE 8 to `discover(company)` — a hired
    # employee has NO entry in ALLOWED_DUTIES (`.get(name, set())` -> empty), so
    # this SAME loop already rejects any duty it claims as "stray" (R7(b) is
    # this rule, reused — not a second special case).
    for name in emp.discover(company):
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
        allowed = emp.ALLOWED_DUTIES.get(name, set())
        # R1a — a duty outside this employee's fixed role set (Gibby can't build,
        # Bob can't attack/verify, Tony can't build, a hired employee can't own
        # ANY Layer-B duty at all, ...).
        stray = dset - allowed
        for d in sorted(stray):
            v.append(
                f"R1: '{name}' may not own duty '{d}' — outside {name}'s fixed role "
                f"(allowed: {sorted(allowed) or 'none'})"
            )
        # R1b — no single employee holds BOTH an attack-class and a build-class duty.
        if (dset & emp.ATTACK_DUTIES) and (dset & emp.BUILD_DUTIES):
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
        return set(emp.ALLOWED_DUTIES[name])

    bob, gib = _emp("bob"), _emp("gibby")
    bob_enabled = bob.get("enabled", True) is not False
    bob_duties = _duties_of(bob, "bob")
    bob_builds = bob_enabled and bool(emp.BUILD_DUTIES & bob_duties)
    if bob_builds:
        gib_enabled = gib.get("enabled", True) is not False
        gib_duties = _duties_of(gib, "gibby")
        if not gib_enabled:
            v.append("R2: Bob builds but Gibby is disabled — attack surface uncovered")
        elif not (emp.ATTACK_DUTIES & gib_duties):
            # Covers an EXPLICIT empty gibby duty list too (P9-D1 regression).
            v.append("R2: Bob builds but Gibby has no 'attack' duty — attack surface uncovered")

    # NOTE on cadence (Gibby's related observation): R2 deliberately inspects the
    # attack DUTY assignment, NOT a per-employee sub-cadence. `attack`/`build` are
    # DISPATCHED competition work (Phoebe -> Gibby/Bob), not scheduled batch steps
    # (they are absent from STEP_OWNER). A sub-cadence only gates the deterministic
    # daily-run BATCH, so `gibby: {cadence: on-trigger}` cannot remove the red team
    # from the competition loop — hence cadence is correctly out of R2's scope.

    # R7 — hire-as-data invariants on the HIRED desks themselves (context.md),
    # independent of schedule.yaml. See r7_violations() for the full contract.
    v.extend(r7_violations(company))

    return v


# ---------------------------------------------------------- Phase 32 Item 3
def r7_violations(company):
    """R7 — Layer B invariants for a DISCOVERED (hired, non-core) employee's
    OWN desk, read directly from the filesystem (independent of
    schedule.yaml). Core employees are exempt — R1-R6 already govern them.
    Reused by hook_org_lint.sh (scoped to one touched desk) and by the CLI
    (the whole company). Never raises: `company=None`/absent/unreadable
    degrades to []."""
    v = []
    if not company:
        return v
    base = Path(company) / "org" / "employees"
    try:
        if not base.is_dir():
            return v
        entries = sorted(base.iterdir())
    except OSError:
        return v

    # Charset defense-in-depth (Item 1): discover() already silently EXCLUDES
    # a bad-charset directory from dispatch; this SURFACES it instead of
    # leaving it inert forever unnoticed.
    for d in entries:
        # Phase 32 Bug 1: a DOTFILE entry (`.fired` tombstone dir, or any
        # internal dot-dir) is NOT a desk and must share discover()'s exclusion
        # — otherwise --fire's `.fired/` tombstone would be flagged as a
        # bad-charset id and the validator would exit 3 FOREVER after (breaking
        # every subsequent hire and the schedule.sh validator gate). discover()
        # already skips these via the leading-`[a-z]` charset rule; mirror it.
        if d.name.startswith("."):
            continue
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        if d.name in emp.CORE_EMPLOYEES:
            continue
        if not emp._DESK_ID_RE.match(d.name):
            v.append(
                f"R7: invalid employee id '{d.name}' under org/employees/ — "
                f"must match ^[a-z][a-z0-9-]{{1,23}}$ (ignored by discover, "
                f"never dispatched)"
            )
            continue
        # Phase 32 Bug 4 (least-privilege): a valid-charset desk whose
        # persona.md/context.md is a SYMLINK is excluded by discover() (so it
        # never dispatches) but must be SURFACED here — a symlinked desk file
        # can smuggle out-of-tree text into a prompt, invisible in a git diff.
        for fn in ("persona.md", "context.md"):
            try:
                if (d / fn).is_symlink():
                    v.append(
                        f"R7: '{d.name}' desk file '{fn}' is a symlink — a desk "
                        f"file must be a real in-tree file, never a symlink "
                        f"(excluded from discover; could smuggle out-of-tree "
                        f"text into the dispatch prompt)"
                    )
            except OSError:
                continue

    discovered = emp.discover(company)
    hired = [n for n in discovered if n not in emp.CORE_EMPLOYEES]

    for name in hired:
        e = emp.Employee.load(name, company)

        # (a) declared tier must be worker|manager; role text may not claim a
        # charter singleton role (those stay code-pinned).
        if e.declared_tier not in ("worker", "manager"):
            v.append(
                f"R7: '{name}' declares tier '{e.declared_tier or '(missing)'}' "
                f"— a hired employee's context.md must set tier: worker or "
                f"tier: manager"
            )
        if _claims_charter_role(e.role or ""):
            v.append(
                f"R7: '{name}' role '{e.role}' claims a charter singleton role "
                f"(CEO / execution gateway / HR lead / QA sign-off) — those "
                f"stay code-pinned, never hired"
            )

        # (b) no attack/build duty — enforced by R1's own loop above (widened
        # to discover(company)); nothing extra to check here.

        # (c) manager: must resolve to a real employee and the chain must be
        # acyclic, rooted at elon.
        mgr_err = _manager_chain_error(name, discovered, company)
        if mgr_err:
            v.append(f"R7: {mgr_err}")

    return v


def _manager_chain_error(start, known_ids, company):
    """Walk `start`'s `manager:` chain (case-insensitive) until it reaches
    'elon' (the root of the org — Elon's OWN manager is the Chairman, external
    to the company) or fails. `known_ids` is the full discover() id set (core +
    hired) for this company. Returns an error string, or None if the chain is
    valid. Bounded walk (len(known_ids) + 2 hops) so an unexpected shape can
    never loop forever; anything left unresolved after that many hops is
    reported as an unresolved chain (a cycle among fewer nodes is caught
    earlier, by the `seen` check)."""
    known = set(known_ids)
    seen = {start}
    current = start
    for _ in range(len(known) + 2):
        e = emp.Employee.load(current, company)
        nxt = str(e.manager or "").strip().lower()
        if not nxt:
            return f"'{start}' manager chain broke at '{current}' — no manager set"
        if nxt == "elon":
            return None                          # reached the root -> valid
        if nxt not in known:
            return (f"'{start}' manager chain references unknown employee "
                    f"'{nxt}' (via '{current}')")
        if nxt in seen:
            return f"'{start}' manager chain has a cycle at '{nxt}'"
        seen.add(nxt)
        current = nxt
    return f"'{start}' manager chain did not resolve to elon within a bounded walk"


# ---------------------------------------------------------- Phase 29 Item 1
def model_warnings(company):
    """Surface a non-empty `context.md` `model:` value that doesn't resolve
    through the alias map / a valid `claude-*` id — one WARN string per bad
    employee, naming the employee and the value. A FINDING, not a violation:
    unlike `validate()`'s R1-R6 (which REJECT the config and exit 3), these
    never affect the exit code — the dispatch path (Employee.resolved_model)
    already degrades safely on its own. Never raises; a company with no
    employee desks at all (e.g. a bare temp dir in tests) yields [].

    Phase 32: widened from the static CORE 8 to `emp.discover(company)` so a
    hired employee's bad `model:` value also surfaces — byte-identical to
    before when nobody is hired."""
    warnings = []
    for name in emp.discover(company):
        try:
            e = emp.Employee.load(name, company)
            _, warning = e.resolved_model(sc.DEFAULT_AGENT_MODEL)
        except Exception:
            continue
        if warning:
            warnings.append(f"WARN: {warning}")
    return warnings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument("--config", default=None, help="validate this file instead of the company's")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    raw, err = _load(a.company, a.config)
    violations = validate(raw, err, company=a.company)
    # Model-table WARN findings never affect the exit code — printed alongside
    # violations/"ok" but computed and reported independently of them.
    warns = model_warnings(a.company)
    if violations:
        if not a.quiet:
            for line in violations:
                print(line)
            for line in warns:
                print(line)
        sys.exit(3)
    if not a.quiet:
        print("ok")
        for line in warns:
            print(line)
    sys.exit(0)


if __name__ == "__main__":
    main()
