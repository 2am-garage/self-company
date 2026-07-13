"""
Tests for hire.sh — Phase 32 Item 2 (hire-as-data: the mechanism).

Every test drives hire.sh as a subprocess against a temp copy of the shipped
company-template, exactly the house convention schedule.sh/schedule_validator.py
tests use. Covers: happy worker + happy manager hire, every refusal (existing
id, core id, bad charset, bad tier, unknown manager/people-lead ref, invalid
model), atomicity (a validator failure leaves no scaffolded desk), and
--fire (refuses core, tombstones, is idempotent).
"""

import os
import shutil
import subprocess
import tempfile
import unittest

import _helpers

REPO = _helpers.REPO_ROOT
SCRIPTS = os.path.join(REPO, "plugin", "skills", "self-company", "scripts")
HIRE = os.path.join(SCRIPTS, "hire.sh")
TEMPLATE = os.path.join(REPO, "plugin", "skills", "self-company",
                        "assets", "company-template")


def _run(args, cwd=None):
    return subprocess.run(["bash", HIRE, *args], capture_output=True, text=True,
                          cwd=cwd, stdin=subprocess.DEVNULL)


class HireTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = os.path.join(self.tmp, ".company")
        shutil.copytree(TEMPLATE, self.company)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def desk(self, empid):
        return os.path.join(self.company, "org", "employees", empid)

    def hire(self, *args):
        return _run([*args, "--company", self.company])


class TestHappyPath(HireTestBase):
    def test_happy_worker(self):
        r = self.hire("sam-jr", "--tier", "worker", "--role", "QA Assistant")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isdir(self.desk("sam-jr")))
        for f in ("context.md", "persona.md", "log.md", "scratchpad.md"):
            self.assertTrue(os.path.isfile(os.path.join(self.desk("sam-jr"), f)), f)
        with open(os.path.join(self.desk("sam-jr"), "context.md")) as f:
            ctx = f.read()
        self.assertIn("tier: worker", ctx)
        self.assertIn("manager: phoebe", ctx)          # default for worker
        self.assertIn("people_lead: july", ctx)
        self.assertIn("role: QA Assistant", ctx)

    def test_happy_manager(self):
        r = self.hire("pat-mgr", "--tier", "manager", "--role", "Ops Manager")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.desk("pat-mgr"), "context.md")) as f:
            ctx = f.read()
        self.assertIn("tier: manager", ctx)
        self.assertIn("manager: elon", ctx)             # default for manager
        self.assertIn("memory: rag", ctx)                # manager default

    def test_explicit_manager_and_people_lead_honored(self):
        self.hire("pat-mgr", "--tier", "manager", "--role", "Ops Manager")
        r = self.hire("sam-jr", "--tier", "worker", "--role", "QA",
                      "--manager", "pat-mgr", "--people-lead", "july")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.desk("sam-jr"), "context.md")) as f:
            ctx = f.read()
        self.assertIn("manager: pat-mgr", ctx)

    def test_valid_model_alias_written(self):
        r = self.hire("sam-jr", "--tier", "worker", "--role", "QA", "--model", "haiku")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.desk("sam-jr"), "context.md")) as f:
            ctx = f.read()
        self.assertIn("model: haiku", ctx)

    def test_scaffolded_desk_passes_validator(self):
        self.hire("sam-jr", "--tier", "worker", "--role", "QA")
        validator = os.path.join(SCRIPTS, "schedule_validator.py")
        r = subprocess.run(["python3", validator, "--company", self.company],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_hired_desk_appears_in_discover(self):
        self.hire("sam-jr", "--tier", "worker", "--role", "QA")
        import sys
        sys.path.insert(0, SCRIPTS)
        import importlib
        employee = importlib.import_module("employee")
        importlib.reload(employee)
        self.assertIn("sam-jr", employee.discover(self.company))


class TestRefusals(HireTestBase):
    def test_existing_id_refused(self):
        self.hire("sam-jr", "--tier", "worker", "--role", "QA")
        r = self.hire("sam-jr", "--tier", "worker", "--role", "Dup")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already exists", r.stderr)

    def test_core_id_refused(self):
        r = self.hire("bob", "--tier", "worker", "--role", "X")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("charter/core", r.stderr)
        self.assertFalse(os.path.isdir(os.path.join(self.desk("bob"), "log.md")))

    def test_charter_id_gibby_refused(self):
        r = self.hire("gibby", "--tier", "worker", "--role", "X")
        self.assertNotEqual(r.returncode, 0)

    def test_bad_charset_uppercase_refused(self):
        r = self.hire("BadCase", "--tier", "worker", "--role", "X")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.isdir(self.desk("BadCase")))

    def test_bad_charset_traversal_shaped_refused(self):
        r = self.hire("../evil", "--tier", "worker", "--role", "X")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil")))

    def test_bad_charset_25_chars_refused(self):
        r = self.hire("a" * 25, "--tier", "worker", "--role", "X")
        self.assertNotEqual(r.returncode, 0)

    def test_bad_tier_refused(self):
        r = self.hire("sam-jr", "--tier", "ceo", "--role", "X")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--tier", r.stderr)
        self.assertFalse(os.path.isdir(self.desk("sam-jr")))

    def test_missing_tier_refused(self):
        r = self.hire("sam-jr", "--role", "X")
        self.assertNotEqual(r.returncode, 0)

    def test_missing_role_refused(self):
        r = self.hire("sam-jr", "--tier", "worker")
        self.assertNotEqual(r.returncode, 0)

    def test_unknown_manager_reference_refused(self):
        r = self.hire("sam-jr", "--tier", "worker", "--role", "X", "--manager", "nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown --manager", r.stderr)
        self.assertFalse(os.path.isdir(self.desk("sam-jr")))

    def test_unknown_people_lead_reference_refused(self):
        r = self.hire("sam-jr", "--tier", "worker", "--role", "X",
                      "--people-lead", "nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown --people-lead", r.stderr)

    def test_invalid_model_refused(self):
        r = self.hire("sam-jr", "--tier", "worker", "--role", "X",
                      "--model", "haiku -> sonnet")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("invalid --model", r.stderr)
        self.assertFalse(os.path.isdir(self.desk("sam-jr")))

    def test_injection_shaped_model_refused(self):
        r = self.hire("sam-jr", "--tier", "worker", "--role", "X",
                      "--model", "sonnet --dangerously-skip-permissions")
        self.assertNotEqual(r.returncode, 0)

    def test_model_python_string_breakout_refused_never_executes(self):
        # --model travels through hire.sh's own `python3 -c` model-check to
        # reuse employee.py's resolved_model — it must go via an environment
        # variable, never spliced into the python source text, or a crafted
        # value could break out of the string literal into executable code.
        tripwire = os.path.join(self.tmp, "PWNED")
        payload = ("x''' ; import pathlib; "
                  f"pathlib.Path({tripwire!r}).write_text('pwned') ; y = '''")
        r = self.hire("sam-jr", "--tier", "worker", "--role", "X", "--model", payload)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(tripwire),
                         "model value executed as python code — injection")
        self.assertFalse(os.path.isdir(self.desk("sam-jr")))

    def test_no_company_store_refused(self):
        r = _run(["x", "--tier", "worker", "--role", "y",
                 "--company", os.path.join(self.tmp, "nope")])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a self-company store", r.stderr)

    def test_missing_id_is_usage_error(self):
        r = _run([])
        self.assertEqual(r.returncode, 2)

    def test_flag_before_id_is_rejected(self):
        # hire.sh expects the id FIRST; a leading flag is a distinct, clear
        # refusal (not the bare-no-args usage banner).
        r = _run(["--company", self.company])
        self.assertNotEqual(r.returncode, 0)

    def test_unknown_flag_refused(self):
        r = self.hire("sam-jr", "--tier", "worker", "--role", "X", "--bogus", "y")
        self.assertNotEqual(r.returncode, 0)


class TestAtomicity(HireTestBase):
    def test_role_claiming_charter_role_leaves_no_desk(self):
        r = self.hire("fake-ceo", "--tier", "worker", "--role", "CEO of everything")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("R7", r.stderr)
        self.assertFalse(os.path.exists(self.desk("fake-ceo")))

    def test_attack_duty_via_schedule_yaml_does_not_block_hire(self):
        # Duties are a SEPARATE config concern (org/schedule.yaml), not part of
        # the scaffold hire.sh writes — a fresh hire never OWNS a duty by
        # default, so this is just a normal happy hire (nothing to roll back).
        r = self.hire("sam-jr", "--tier", "worker", "--role", "QA")
        self.assertEqual(r.returncode, 0, r.stderr)


class TestGibbyFixRound(HireTestBase):
    """Regression tests for Gibby's Phase 32 adversarial pass."""

    def _run_from_copied_scripts(self, scripts_dir, args):
        hire = os.path.join(scripts_dir, "hire.sh")
        return subprocess.run(["bash", hire, *args], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL)

    def test_bug1_fire_then_validator_ok_then_rehire_succeeds(self):
        # BUG 1: --fire tombstones to org/employees/.fired/, which r7_violations
        # used to raw-scan and flag as a bad-charset id -> validator exit 3
        # FOREVER after, breaking every subsequent hire. After the fix the
        # `.fired` dotfile dir is skipped: validator stays `ok` and re-hiring works.
        r1 = self.hire("temp-worker", "--tier", "worker", "--role", "QA")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        rf = _run(["--fire", "temp-worker", "--company", self.company])
        self.assertEqual(rf.returncode, 0, rf.stderr)
        # the tombstone dir really is present (so this test would catch a regression)
        self.assertTrue(os.path.isdir(
            os.path.join(self.company, "org", "employees", ".fired")))
        val = subprocess.run(
            ["python3", os.path.join(SCRIPTS, "schedule_validator.py"),
             "--company", self.company], capture_output=True, text=True)
        self.assertEqual(val.returncode, 0, val.stdout)   # NOT 3
        self.assertIn("ok", val.stdout)
        self.assertNotIn("R7", val.stdout)
        # ...and a subsequent hire still succeeds (would roll back before the fix)
        r2 = self.hire("another-worker", "--tier", "worker", "--role", "Helper")
        self.assertEqual(r2.returncode, 0, r2.stderr)

    def test_bug2_missing_validator_fails_closed_no_desk(self):
        # BUG 2: a missing validator used to let the hire SUCCEED unchecked
        # (fail-open) — a desk claiming a charter role would be written. Now it
        # must fail CLOSED: refuse, scaffold nothing.
        skill_copy = os.path.join(self.tmp, "skillcopy")
        shutil.copytree(os.path.join(REPO, "plugin", "skills", "self-company"),
                        skill_copy)
        scripts_copy = os.path.join(skill_copy, "scripts")
        os.remove(os.path.join(scripts_copy, "schedule_validator.py"))
        r = self._run_from_copied_scripts(
            scripts_copy,
            ["gw", "--tier", "worker", "--role", "execution gateway",
             "--company", self.company])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("fail-closed", r.stderr)
        self.assertFalse(os.path.exists(self.desk("gw")))

    def test_sed_failure_removes_partial_desk_and_fails_loudly(self):
        # Gibby's note: _render's sed calls didn't check exit status, so a
        # `sed: unterminated s command` (e.g. a newline in the role value) wrote
        # a half-rendered desk silently. Now the render failure is loud and the
        # partial desk is removed (atomic).
        r = self.hire("nl-worker", "--tier", "worker",
                      "--role", "line1\nline2")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rendering failed", r.stderr)
        self.assertFalse(os.path.exists(self.desk("nl-worker")))


class TestFire(HireTestBase):
    def test_fire_hired_tombstones(self):
        self.hire("sam-jr", "--tier", "worker", "--role", "QA")
        r = _run(["--fire", "sam-jr", "--company", self.company])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.isdir(self.desk("sam-jr")))
        tomb_dir = os.path.join(self.company, "org", "employees", ".fired")
        entries = os.listdir(tomb_dir)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].startswith("sam-jr-"))
        self.assertTrue(os.path.isfile(
            os.path.join(tomb_dir, entries[0], "context.md")))

    def test_fire_core_refused(self):
        r = _run(["--fire", "bob", "--company", self.company])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("charter/core", r.stderr)
        self.assertTrue(os.path.isdir(self.desk("bob")))   # untouched

    def test_fire_charter_gibby_refused(self):
        r = _run(["--fire", "gibby", "--company", self.company])
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(os.path.isdir(self.desk("gibby")))

    def test_fire_nonexistent_is_idempotent_noop(self):
        r = _run(["--fire", "never-hired", "--company", self.company])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing to fire", r.stdout.lower())

    def test_fire_twice_does_not_error(self):
        self.hire("sam-jr", "--tier", "worker", "--role", "QA")
        r1 = _run(["--fire", "sam-jr", "--company", self.company])
        r2 = _run(["--fire", "sam-jr", "--company", self.company])
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)

    def test_fire_never_deletes_memory(self):
        self.hire("sam-jr", "--tier", "worker", "--role", "QA")
        mem_dir = os.path.join(self.desk("sam-jr"), "memory")
        os.makedirs(mem_dir, exist_ok=True)
        with open(os.path.join(mem_dir, "note.md"), "w") as f:
            f.write("a memory\n")
        r = _run(["--fire", "sam-jr", "--company", self.company])
        self.assertEqual(r.returncode, 0, r.stderr)
        tomb_dir = os.path.join(self.company, "org", "employees", ".fired")
        entry = os.listdir(tomb_dir)[0]
        self.assertTrue(os.path.isfile(
            os.path.join(tomb_dir, entry, "memory", "note.md")))

    def test_fire_bad_charset_id_refused(self):
        r = _run(["--fire", "Bad Id", "--company", self.company])
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
