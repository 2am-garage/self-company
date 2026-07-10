"""
Tests for schedule_validator.py — the Phase 12 Layer-B invariant enforcer.

The centerpiece: a config that would break the red/blue competition, the
positions, or the sign-off gate must be REJECTED (exit 3, rule id printed) so the
callers fall back to defaults. A valid or absent config passes (exit 0). Every
test drives the CLI as a subprocess (the exact seam schedule.sh / the guard use).

Covers R1–R6 each rejecting with its rule id, plus the parse-error fail-closed
path and the valid/absent pass-through.
"""

import os
import shutil
import tempfile
import unittest

import _helpers
from _helpers import run_script


def _make_company(tmp, body=None):
    org = os.path.join(tmp, ".company", "org")
    os.makedirs(org, exist_ok=True)
    cfg = os.path.join(org, "schedule.yaml")
    if body is None:
        if os.path.exists(cfg):
            os.remove(cfg)
    else:
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(body)
    return os.path.join(tmp, ".company")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def company(self, body=None):
        return _make_company(self.tmp, body)

    def validate(self, company, *extra):
        return run_script("schedule_validator.py", "--company", company, *extra)


class TestValidPass(Base):
    def test_absent_config_passes(self):
        rc, out, _ = self.validate(self.company())
        self.assertEqual(rc, 0)
        self.assertIn("ok", out)

    def test_wellformed_config_passes(self):
        c = self.company(
            "cadence: every 2h\n"
            "research: { enabled: true, cadence: weekly-sun-03 }\n"
            "agent: { model: claude-sonnet-4-6, timeout: 600, daily_cap: 4 }\n"
            "tony: { cadence: every-run, duties: [decay, entropy], budget: 20000 }\n"
            "gibby: { cadence: every-run, duties: [verify, attack] }\n"
            "mike: { cadence: weekly, duties: [research] }\n"
        )
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 0, out)


class TestR1AttackerNotBuilder(Base):
    def test_gibby_may_not_build(self):
        rc, out, _ = self.validate(self.company("gibby: { duties: [build] }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R1", out)

    def test_bob_may_not_attack(self):
        rc, out, _ = self.validate(self.company("bob: { duties: [attack] }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R1", out)

    def test_bob_may_not_verify(self):
        rc, out, _ = self.validate(self.company("bob: { duties: [verify] }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R1", out)

    def test_no_employee_holds_both_attack_and_build(self):
        rc, out, _ = self.validate(self.company("gibby: { duties: [attack, build] }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("attacker != builder", out)


class TestR2AttackSurfaceCovered(Base):
    def test_bob_builds_gibby_disabled_rejected(self):
        rc, out, _ = self.validate(self.company("gibby: { enabled: false }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R2", out)

    def test_bob_builds_gibby_without_attack_rejected(self):
        rc, out, _ = self.validate(self.company("gibby: { duties: [verify] }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R2", out)

    def test_bob_disabled_then_gibby_may_drop_attack(self):
        # If Bob doesn't build, there is no build surface to cover -> valid.
        rc, out, _ = self.validate(self.company(
            "bob: { enabled: false }\ngibby: { duties: [verify] }\n"))
        self.assertEqual(rc, 0, out)

    # --- P9-D1: an EXPLICIT empty gibby duty list must NOT be read as "absent" --
    def test_gibby_explicit_empty_duties_rejected(self):
        # The exact spec fixture: bob builds, gibby: {duties: []} -> no red team.
        rc, out, _ = self.validate(self.company(
            "bob: { duties: [build] }\ngibby: { duties: [] }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R2", out)

    def test_gibby_explicit_empty_duties_default_bob_rejected(self):
        # bob absent -> defaults to building; gibby explicit-empty -> uncovered.
        rc, out, _ = self.validate(self.company("gibby: { duties: [] }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R2", out)

    def test_gibby_enabled_false_rejected(self):
        rc, out, _ = self.validate(self.company(
            "bob: { duties: [build] }\ngibby: { enabled: false }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R2", out)

    def test_bob_explicit_empty_duties_is_ok(self):
        # Bob explicitly NOT building -> no build surface -> gibby may be empty.
        rc, out, _ = self.validate(self.company(
            "bob: { duties: [] }\ngibby: { duties: [] }\n"))
        self.assertEqual(rc, 0, out)

    def test_gibby_on_trigger_cadence_is_out_of_R2_scope(self):
        # 'attack' is DISPATCHED competition work, not a scheduled batch step, so a
        # sub-cadence (which only gates the deterministic batch) cannot remove the
        # red team. R2 guards the attack DUTY assignment, not batch timing -> pass.
        rc, out, _ = self.validate(self.company(
            "gibby: { cadence: on-trigger, duties: [verify, attack] }\n"))
        self.assertEqual(rc, 0, out)


class TestR3R5R6ForbiddenKeys(Base):
    def test_R3_signoff_gate_key_rejected(self):
        rc, out, _ = self.validate(self.company("tony: { consecutive: 5 }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R6", out)      # forbidden footgun key, sourced from R3's namespace

    def test_R5_ledger_key_rejected(self):
        rc, out, _ = self.validate(self.company("ledger: { enabled: false }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R6", out)

    def test_R6_role_field_rejected(self):
        rc, out, _ = self.validate(self.company("tony: { role: attacker }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R6", out)

    def test_R6_attacks_field_rejected(self):
        rc, out, _ = self.validate(self.company("bob: { attacks: gibby }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R6", out)

    def test_R6_tier_field_rejected(self):
        rc, out, _ = self.validate(self.company("mike: { tier: L2 }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R6", out)


class TestR4Topology(Base):
    def test_unknown_top_level_key_rejected(self):
        rc, out, _ = self.validate(self.company("widget: { foo: 1 }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R4", out)

    def test_unknown_employee_key_rejected(self):
        rc, out, _ = self.validate(self.company("tony: { frequency: daily }\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R4", out)

    def test_employee_block_must_be_mapping(self):
        rc, out, _ = self.validate(self.company("tony: not-a-map\n"))
        self.assertEqual(rc, 3)
        self.assertIn("R4", out)


class TestParseFailClosed(Base):
    def test_malformed_yaml_via_config_is_rejected(self):
        # The explicit --config path reports a parse error and fails closed.
        bad = os.path.join(self.tmp, "bad.yaml")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("a: b: c\n")
        rc, out, _ = self.validate(self.company(), "--config", bad)
        self.assertEqual(rc, 3)
        self.assertIn("PARSE", out)

    def test_top_level_non_mapping_via_config_rejected(self):
        lst = os.path.join(self.tmp, "list.yaml")
        with open(lst, "w", encoding="utf-8") as f:
            f.write("- x\n- y\n")
        rc, out, _ = self.validate(self.company(), "--config", lst)
        self.assertEqual(rc, 3)
        self.assertIn("PARSE", out)

    def test_quiet_suppresses_output_but_keeps_exit_code(self):
        rc, out, _ = self.validate(self.company("gibby: { duties: [build] }\n"), "--quiet")
        self.assertEqual(rc, 3)
        self.assertEqual(out.strip(), "")


# ==================================================== Phase 29 Item 1 model WARN
class TestModelWarnings(Base):
    """A bad `context.md` model: value is a WARN finding, never a rejection —
    exit code is driven ONLY by R1-R6 violations, model warnings ride alongside."""

    def _desk_with_model(self, company, name, model_value):
        d = os.path.join(company, "org", "employees", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "context.md"), "w", encoding="utf-8") as f:
            f.write(f"---\nname: {name}\nmodel: {model_value}\n---\n{name}.\n")

    def test_no_desks_no_warn_lines(self):
        rc, out, _ = self.validate(self.company())
        self.assertEqual(rc, 0)
        self.assertIn("ok", out)
        self.assertNotIn("WARN", out)

    def test_bad_model_warns_but_still_exits_0(self):
        c = self.company()
        self._desk_with_model(c, "bob", "haiku → sonnet")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 0)          # a model finding is NOT a violation
        self.assertIn("ok", out)
        self.assertIn("WARN", out)
        self.assertIn("bob", out)

    def test_bad_model_warns_alongside_a_real_violation(self):
        c = self.company("gibby: { duties: [build] }\n")   # R1 violation
        self._desk_with_model(c, "bob", "haiku → sonnet")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)           # R1 still rejects
        self.assertIn("R1", out)
        self.assertIn("WARN", out)
        self.assertIn("bob", out)

    def test_valid_model_produces_no_warn_line(self):
        c = self.company()
        self._desk_with_model(c, "bob", "haiku")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 0)
        self.assertNotIn("WARN", out)

    def test_quiet_suppresses_model_warnings_too(self):
        c = self.company()
        self._desk_with_model(c, "bob", "haiku → sonnet")
        rc, out, _ = self.validate(c, "--quiet")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")


if __name__ == "__main__":
    unittest.main()
