"""
Tests for employee.py — the Phase 16 data-driven Employee model.

Two fixtures:
  * the SHIPPED company-template (read-only) — real context.md frontmatter, so we
    test identity + capability parsing against the actual eight desks;
  * a temp company with a schedule.yaml — for should_run/duties/cadence parity
    with schedule_config, and for graceful-default behaviour.

The load-bearing invariants:
  * ONE class, eight data instances (roster) — no per-employee subclass.
  * every accessor resolves from the desk / the fixed Layer-B tables;
  * allows_duty / owns_step / should_run match today's schedule_config exactly;
  * a missing desk / field degrades to a sensible default and NEVER raises.
"""

import os
import shutil
import tempfile
import unittest

import _helpers  # noqa: F401  (puts scripts/ on sys.path)

import employee
from employee import Employee
import schedule_config as sc

TEMPLATE = os.path.join(
    _helpers.REPO_ROOT,
    "plugin", "skills", "self-company", "assets", "company-template",
)


def _make_company(tmp, body=None):
    """Temp .company with an optional schedule.yaml body."""
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


# ------------------------------------------------------------------- roster / model
class TestRosterAndModel(unittest.TestCase):
    def test_roster_is_the_eight(self):
        self.assertEqual(
            Employee.roster(),
            ["tony", "gibby", "bob", "mike", "elon", "phoebe", "tom", "july"],
        )

    def test_one_class_no_subclass(self):
        # Every roster member is an instance of the SAME class — a subclass per
        # employee would be the design break this phase exists to prevent.
        for name in Employee.roster():
            e = Employee.load(name, TEMPLATE)
            self.assertIs(type(e), Employee, name)

    def test_load_is_case_insensitive(self):
        self.assertEqual(Employee.load("Bob", TEMPLATE).name, "bob")


# ------------------------------------------------------------------- identity
class TestIdentity(unittest.TestCase):
    def test_bob_identity(self):
        e = Employee.load("bob", TEMPLATE)
        self.assertEqual(e.name, "bob")
        self.assertEqual(e.display_name, "Bob")
        self.assertEqual(e.role, "Build Engineer")
        self.assertEqual(e.manager, "Phoebe")
        self.assertEqual(e.people_lead, "July")
        self.assertEqual(e.tier, "worker")

    def test_inline_comment_stripped_from_scalar(self):
        # bob's manager line carries a trailing `# dispatch source...` comment.
        self.assertEqual(Employee.load("bob", TEMPLATE).manager, "Phoebe")

    def test_tiers(self):
        self.assertEqual(Employee.load("elon", TEMPLATE).tier, "manager")
        self.assertEqual(Employee.load("phoebe", TEMPLATE).tier, "manager")
        self.assertEqual(Employee.load("july", TEMPLATE).tier, "lead")
        for w in ("tony", "gibby", "bob", "mike", "tom"):
            self.assertEqual(Employee.load(w, TEMPLATE).tier, "worker", w)

    def test_null_people_lead_is_none(self):
        # phoebe: `people_lead: null`, elon: `people_lead: ~` -> both None.
        self.assertIsNone(Employee.load("phoebe", TEMPLATE).people_lead)
        self.assertIsNone(Employee.load("elon", TEMPLATE).people_lead)

    def test_every_employee_has_role_and_manager(self):
        for name in Employee.roster():
            e = Employee.load(name, TEMPLATE)
            self.assertTrue(e.role, name)
            self.assertTrue(e.manager, name)


# ------------------------------------------------------------------- capabilities
class TestCapabilities(unittest.TestCase):
    def test_bob_capability_slice(self):
        e = Employee.load("bob", TEMPLATE)
        self.assertEqual(e.tools, ["Read", "Edit", "Write", "Bash"])
        self.assertIn("org/employees/bob/scratchpad.md", e.writes)
        self.assertTrue(any("org/employees/bob/" in r for r in e.reads))
        self.assertEqual(e.handoff_to, ["Gibby"])   # scalar -> one-item list

    def test_capabilities_dict_has_todays_fields(self):
        caps = Employee.load("bob", TEMPLATE).capabilities()
        self.assertEqual(set(caps), {"tools", "reads", "writes", "handoff_to"})
        self.assertEqual(caps["tools"], ["Read", "Edit", "Write", "Bash"])

    def test_list_handoff_to_preserved(self):
        # tony's handoff_to is a YAML sequence -> list of names.
        e = Employee.load("tony", TEMPLATE)
        self.assertIn("Elon", e.handoff_to)
        self.assertIn("Gibby", e.handoff_to)
        self.assertIn("Phoebe", e.handoff_to)

    def test_gibby_mcp_tools_parsed(self):
        e = Employee.load("gibby", TEMPLATE)
        self.assertIn("Read", e.tools)
        self.assertIn("Bash", e.tools)
        self.assertIn("mcp__playwright__browser_navigate", e.tools)

    def test_capabilities_is_a_copy(self):
        e = Employee.load("bob", TEMPLATE)
        e.capabilities()["tools"].append("X")
        self.assertNotIn("X", e.tools)   # mutating the returned dict must not leak


# ------------------------------------------------------------------- execution
class TestExecution(unittest.TestCase):
    def test_model_from_context(self):
        self.assertEqual(Employee.load("tony", TEMPLATE).model, "sonnet")

    def test_model_with_arrow(self):
        # bob: `model: haiku → sonnet  # ...` -> comment stripped, arrow kept.
        self.assertEqual(Employee.load("bob", TEMPLATE).model, "haiku → sonnet")

    def test_duties_default_to_role_set(self):
        c = _make_company(tempfile.mkdtemp())
        try:
            self.assertEqual(sorted(Employee.load("gibby", c).duties),
                             ["attack", "verify"])
        finally:
            shutil.rmtree(os.path.dirname(os.path.dirname(c)), ignore_errors=True)

    def test_cadence_default_every_run(self):
        c = _make_company(tempfile.mkdtemp())
        try:
            self.assertEqual(Employee.load("tony", c).cadence, "every-run")
        finally:
            shutil.rmtree(os.path.dirname(os.path.dirname(c)), ignore_errors=True)

    def test_duties_and_cadence_reflect_config(self):
        tmp = tempfile.mkdtemp()
        try:
            c = _make_company(tmp, "tony: { cadence: daily, duties: [decay] }\n")
            e = Employee.load("tony", c)
            self.assertEqual(e.cadence, "daily")
            self.assertEqual(e.duties, ["decay"])
            self.assertTrue(e.enabled)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_disabled_reflected(self):
        tmp = tempfile.mkdtemp()
        try:
            c = _make_company(tmp, "elon: { enabled: false }\n")
            self.assertFalse(Employee.load("elon", c).enabled)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------- paths
class TestPaths(unittest.TestCase):
    def test_desk_paths(self):
        e = Employee.load("bob", TEMPLATE)
        self.assertTrue(str(e.desk_dir).endswith("org/employees/bob"))
        self.assertEqual(e.persona_path, e.desk_dir / "persona.md")
        self.assertEqual(e.context_path, e.desk_dir / "context.md")
        self.assertEqual(e.log_path, e.desk_dir / "log.md")
        self.assertEqual(e.scratchpad_path, e.desk_dir / "scratchpad.md")
        self.assertTrue(e.persona_path.exists())   # template really has it

    def test_memory_dir_reserved(self):
        # Reserved accessor for the later per-employee-memory idea; it is a Path,
        # not built — nothing creates it.
        e = Employee.load("bob", TEMPLATE)
        self.assertEqual(e.memory_dir, e.desk_dir / "memory")
        self.assertFalse(e.memory_dir.exists())


# ------------------------------------------------------------------- duties/steps
class TestDutiesAndSteps(unittest.TestCase):
    def test_allows_duty(self):
        self.assertTrue(Employee.load("gibby", TEMPLATE).allows_duty("attack"))
        self.assertTrue(Employee.load("gibby", TEMPLATE).allows_duty("verify"))
        self.assertFalse(Employee.load("gibby", TEMPLATE).allows_duty("build"))
        self.assertTrue(Employee.load("bob", TEMPLATE).allows_duty("build"))
        self.assertFalse(Employee.load("bob", TEMPLATE).allows_duty("attack"))
        self.assertFalse(Employee.load("phoebe", TEMPLATE).allows_duty("build"))

    def test_allows_duty_matches_table(self):
        for name in Employee.roster():
            e = Employee.load(name, TEMPLATE)
            for duty in ("attack", "build", "verify", "decay", "research", "survey",
                         "backup", "report", "schedule", "reinforce", "entropy",
                         "rag_index", "propose", "agent"):
                self.assertEqual(e.allows_duty(duty),
                                 duty in employee.ALLOWED_DUTIES[name],
                                 f"{name}.{duty}")

    def test_owns_step(self):
        self.assertTrue(Employee.load("gibby", TEMPLATE).owns_step("verify"))
        self.assertTrue(Employee.load("tom", TEMPLATE).owns_step("backup"))
        self.assertTrue(Employee.load("tony", TEMPLATE).owns_step("rag_index"))
        self.assertFalse(Employee.load("bob", TEMPLATE).owns_step("verify"))
        self.assertFalse(Employee.load("tony", TEMPLATE).owns_step("backup"))

    def test_owns_step_matches_table(self):
        for step, owner in employee.STEP_OWNER.items():
            for name in Employee.roster():
                self.assertEqual(Employee.load(name, TEMPLATE).owns_step(step),
                                 name == owner, f"{name}.{step}")


# ------------------------------------------------------------------- should_run parity
class TestShouldRunParity(unittest.TestCase):
    """Employee.should_run must match schedule_config.should_run everywhere — the
    model routes the exact same verdict the CLI has always produced."""

    CONFIGS = [
        None,
        "tony: { cadence: every-run }\n",
        "elon: { cadence: daily }\n",
        "elon: { cadence: weekly }\n",
        "tony: { cadence: on-trigger }\n",
        "tony: { cadence: every-2 }\n",
        "tony: { duties: [decay] }\n",
        "elon: { enabled: false }\n",
        "gibby: { duties: [] }\n",
    ]
    STEPS = ["backup", "reinforce", "decay", "verify", "entropy", "rag_index",
             "survey", "report", "agent", "build"]

    def test_parity_grid(self):
        tmp = tempfile.mkdtemp()
        try:
            for body in self.CONFIGS:
                c = _make_company(tmp, body)
                for step in self.STEPS:
                    owner = employee.STEP_OWNER.get(step)
                    for hour in (0, 3, 6, 12, 13):
                        for dow in (0, 2, 3, 4):
                            want = sc.should_run(c, step, hour, dow)
                            if owner is None:
                                # unowned -> every employee fails open to True
                                got = Employee.load("bob", c).should_run(step, hour, dow)
                            else:
                                got = Employee.load(owner, c).should_run(step, hour, dow)
                            self.assertEqual(got, want,
                                             f"{body!r} {step} h{hour} d{dow}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_non_owner_does_not_suppress(self):
        # A step this employee doesn't own -> fail open (never suppresses).
        c = _make_company(tempfile.mkdtemp())
        try:
            self.assertTrue(Employee.load("bob", c).should_run("verify", 3, 2))
        finally:
            shutil.rmtree(os.path.dirname(os.path.dirname(c)), ignore_errors=True)


# ------------------------------------------------------------------- log
class TestLog(unittest.TestCase):
    def test_log_appends_and_creates(self):
        tmp = tempfile.mkdtemp()
        try:
            c = os.path.join(tmp, ".company")
            e = Employee.load("bob", c)             # desk does not exist yet
            self.assertTrue(e.log("first entry"))
            self.assertTrue(e.log("second entry\n"))
            text = e.log_path.read_text(encoding="utf-8")
            self.assertEqual(text, "first entry\nsecond entry\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------- graceful defaults
class TestGracefulDefaults(unittest.TestCase):
    def test_missing_company_never_raises(self):
        e = Employee.load("bob", "/no/such/company/at/all")
        self.assertEqual(e.name, "bob")
        self.assertEqual(e.display_name, "Bob")
        self.assertEqual(e.role, "")
        self.assertEqual(e.manager, "")
        self.assertIsNone(e.people_lead)
        self.assertEqual(e.tools, [])
        self.assertEqual(e.reads, [])
        self.assertEqual(e.writes, [])
        self.assertEqual(e.handoff_to, [])
        self.assertEqual(e.model, "")
        self.assertIsNone(e.token_budget)
        self.assertEqual(e.capabilities(),
                         {"tools": [], "reads": [], "writes": [], "handoff_to": []})

    def test_unknown_name_defaults(self):
        e = Employee.load("nobody", TEMPLATE)
        self.assertEqual(e.name, "nobody")
        self.assertEqual(e.tier, "worker")           # default tier
        self.assertFalse(e.allows_duty("build"))     # no fixed role set
        self.assertEqual(e.duties, [])               # not in effective employees

    def test_should_run_unknown_owner_fails_open(self):
        # A step whose owner isn't a real desk still fails open (never raises).
        e = Employee.load("nobody", "/no/such/company")
        self.assertTrue(e.should_run("build", 3, 2))     # unowned -> True


if __name__ == "__main__":
    unittest.main()
