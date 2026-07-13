"""
Tests for schedule_validator.py — the Phase 12 Layer-B invariant enforcer.

The centerpiece: a config that would break the red/blue competition, the
positions, or the sign-off gate must be REJECTED (exit 3, rule id printed) so the
callers fall back to defaults. A valid or absent config passes (exit 0). Every
test drives the CLI as a subprocess (the exact seam schedule.sh / the guard use).

Covers R1–R6 each rejecting with its rule id, plus the parse-error fail-closed
path and the valid/absent pass-through. Also R7 (Phase 32, hire-as-data): the
Layer-B invariants on a DISCOVERED (hired) employee's own desk.
"""

import os
import shutil
import tempfile
import unittest

import _helpers
from _helpers import run_script

TEMPLATE = os.path.join(
    _helpers.REPO_ROOT, "plugin", "skills", "self-company", "assets", "company-template")


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


def _mkdesk(company, name, tier="worker", manager="phoebe", role="QA",
           people_lead="july"):
    d = os.path.join(company, "org", "employees", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "context.md"), "w", encoding="utf-8") as f:
        f.write(
            f"---\nname: {name}\nrole: {role}\ntier: {tier}\n"
            f"manager: {manager}\npeople_lead: {people_lead}\n---\nbody\n"
        )
    with open(os.path.join(d, "persona.md"), "w", encoding="utf-8") as f:
        f.write("persona\n")
    return d


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


# ==================================================== Phase 32 Item 3 (R7)
class TestR7HireAsData(Base):
    """R7 — Layer B invariants on a DISCOVERED (hired) employee's own desk,
    independent of schedule.yaml. Core 8 stay exempt (R1-R6 govern them)."""

    def _copy_template(self):
        # Real core desks (manager chains that resolve to elon) — R7's
        # manager-chain walk reads the CORE desks' own context.md too.
        c = self.company()
        for name in os.listdir(os.path.join(TEMPLATE, "org", "employees")):
            src = os.path.join(TEMPLATE, "org", "employees", name)
            if not os.path.isdir(src):
                continue
            dst = os.path.join(c, "org", "employees", name)
            shutil.copytree(src, dst)
        return c

    def test_zero_hired_desks_no_r7_lines(self):
        c = self._copy_template()
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 0)
        self.assertNotIn("R7", out)

    def test_valid_hired_worker_passes(self):
        c = self._copy_template()
        _mkdesk(c, "sam-jr")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 0, out)

    def test_valid_hired_manager_reporting_to_elon_passes(self):
        c = self._copy_template()
        _mkdesk(c, "pat-mgr", tier="manager", manager="elon")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 0, out)

    def test_bad_tier_value_rejected(self):
        c = self._copy_template()
        _mkdesk(c, "sam-jr", tier="ceo")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("R7", out)
        self.assertIn("tier", out)

    def test_missing_tier_rejected(self):
        c = self._copy_template()
        d = os.path.join(c, "org", "employees", "sam-jr")
        os.makedirs(d)
        with open(os.path.join(d, "context.md"), "w") as f:
            f.write("---\nname: Sam\nmanager: phoebe\n---\nbody\n")
        with open(os.path.join(d, "persona.md"), "w") as f:
            f.write("x")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("R7", out)
        self.assertIn("(missing)", out)

    def test_role_claims_ceo_rejected(self):
        c = self._copy_template()
        _mkdesk(c, "fake-ceo", role="CEO of everything")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("R7", out)
        self.assertIn("charter singleton", out)

    def test_role_claims_execution_gateway_rejected(self):
        c = self._copy_template()
        _mkdesk(c, "fake-gw", role="Execution Gateway")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("R7", out)

    def test_role_claims_hr_lead_rejected(self):
        c = self._copy_template()
        _mkdesk(c, "fake-hr", role="HR Lead")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("R7", out)

    def test_role_claims_qa_signoff_rejected(self):
        c = self._copy_template()
        _mkdesk(c, "fake-qa", role="QA Sign-off")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("R7", out)

    def test_ordinary_role_title_not_flagged(self):
        c = self._copy_template()
        _mkdesk(c, "sam-jr", role="R&D Researcher")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 0, out)

    def test_attack_duty_rejected_via_r1(self):
        # Attack/build classes stay exclusive to code-known employees — this
        # is R1's OWN loop, widened to discover(company); R7(b) reuses it
        # rather than duplicating the check.
        c = self._copy_template()
        _mkdesk(c, "sam-jr")
        cfg = os.path.join(c, "org", "schedule.yaml")
        with open(cfg, "w") as f:
            f.write("sam-jr: { duties: [attack] }\n")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("R1", out)
        self.assertIn("sam-jr", out)

    def test_build_duty_rejected_via_r1(self):
        c = self._copy_template()
        _mkdesk(c, "sam-jr")
        cfg = os.path.join(c, "org", "schedule.yaml")
        with open(cfg, "w") as f:
            f.write("sam-jr: { duties: [build] }\n")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("R1", out)

    def test_manager_cycle_a_to_b_to_a_rejected(self):
        c = self._copy_template()
        _mkdesk(c, "aa", manager="bb")
        _mkdesk(c, "bb", manager="aa")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("R7", out)
        self.assertIn("cycle", out)

    def test_manager_self_reference_is_a_cycle(self):
        c = self._copy_template()
        _mkdesk(c, "sam-jr", manager="sam-jr")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("cycle", out)

    def test_unknown_manager_reference_rejected(self):
        c = self._copy_template()
        _mkdesk(c, "sam-jr", manager="nope")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("R7", out)
        self.assertIn("unknown employee", out)

    def test_worker_under_hired_manager_reaches_elon(self):
        c = self._copy_template()
        _mkdesk(c, "pat-mgr", tier="manager", manager="elon")
        _mkdesk(c, "sam-jr", manager="pat-mgr")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 0, out)

    def test_bad_charset_directory_flagged_not_silently_ignored(self):
        c = self._copy_template()
        d = os.path.join(c, "org", "employees", "BadCase")
        os.makedirs(d)
        with open(os.path.join(d, "context.md"), "w") as f:
            f.write("---\nname: Bad\n---\nbody\n")
        with open(os.path.join(d, "persona.md"), "w") as f:
            f.write("x")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("R7", out)
        self.assertIn("BadCase", out)
        self.assertTrue("charset" in out.lower() or "must match" in out)

    def test_core_employees_exempt_from_r7(self):
        # A core desk's tier/role are Layer-B (TIERS table) — R7 never even
        # looks at core context.md tier/role text.
        c = self._copy_template()
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("R7", out)


class TestR7GibbyFixRound(Base):
    """Regression tests for Gibby's Phase 32 adversarial pass (validator side)."""

    _counter = 0

    def _copy_template(self):
        # A FRESH isolated company per call (some tests build several), each a
        # full copy of the shipped template so R7's manager-chain walk sees the
        # real core desks.
        TestR7GibbyFixRound._counter += 1
        c = os.path.join(self.tmp, f"co{TestR7GibbyFixRound._counter}", ".company")
        shutil.copytree(TEMPLATE, c)
        return c

    # --- BUG 1: a .fired tombstone dir must NOT be flagged as a bad-charset id
    def test_bug1_fired_tombstone_dir_not_flagged(self):
        c = self._copy_template()
        fired = os.path.join(c, "org", "employees", ".fired", "temp-2026-07-13")
        os.makedirs(fired)
        with open(os.path.join(fired, "context.md"), "w") as f:
            f.write("---\nname: T\n---\nb\n")
        with open(os.path.join(fired, "persona.md"), "w") as f:
            f.write("x")
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 0, out)          # NOT 3
        self.assertNotIn("R7", out)
        self.assertNotIn(".fired", out)

    def test_bug1_any_dotfile_entry_skipped(self):
        c = self._copy_template()
        os.makedirs(os.path.join(c, "org", "employees", ".scratch"))
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 0, out)

    # --- BUG 3: role-claim detection catches ANY phrasing --------------------
    def _role_flagged(self, role):
        c = self._copy_template()
        _mkdesk(c, "rc-desk", role=role)
        rc, out, _ = self.validate(c)
        return rc == 3 and "charter singleton" in out

    def test_bug3_hyphen_and_spacing_variants_all_caught(self):
        for role in ("execution-gateway", "HR-Lead", "qa-signoff",
                     "chief-executive-officer", "execution  gateway",
                     "EXECUTION_GATEWAY", "Chief Executive Officer",
                     "qa sign-off", "human-resources-team-lead"):
            self.assertTrue(self._role_flagged(role),
                            f"{role!r} should be flagged as a charter role claim")

    def test_bug3_ordinary_titles_still_not_flagged(self):
        for role in ("Build Engineer", "R&D Researcher", "QA Assistant",
                     "Data Analyst", "Marketing Lead-Gen Specialist"):
            self.assertFalse(self._role_flagged(role),
                             f"{role!r} must NOT be flagged")

    # --- BUG 4: a symlinked desk file is flagged (and never discovered) ------
    def test_bug4_symlinked_persona_flagged(self):
        c = self._copy_template()
        outside = os.path.join(self.tmp, "outside-persona.md")
        with open(outside, "w") as f:
            f.write("SMUGGLED\n")
        d = os.path.join(c, "org", "employees", "evil-desk")
        os.makedirs(d)
        with open(os.path.join(d, "context.md"), "w") as f:
            f.write("---\nname: X\nrole: QA\ntier: worker\n"
                    "manager: elon\npeople_lead: july\n---\nb\n")
        os.symlink(outside, os.path.join(d, "persona.md"))
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("R7", out)
        self.assertIn("evil-desk", out)
        self.assertIn("symlink", out)

    def test_bug4_symlinked_context_flagged(self):
        c = self._copy_template()
        outside = os.path.join(self.tmp, "outside-context.md")
        with open(outside, "w") as f:
            f.write("---\nname: X\ntier: worker\nmanager: elon\n---\nb\n")
        d = os.path.join(c, "org", "employees", "evil-desk")
        os.makedirs(d)
        with open(os.path.join(d, "persona.md"), "w") as f:
            f.write("persona\n")
        os.symlink(outside, os.path.join(d, "context.md"))
        rc, out, _ = self.validate(c)
        self.assertEqual(rc, 3)
        self.assertIn("symlink", out)


if __name__ == "__main__":
    unittest.main()
