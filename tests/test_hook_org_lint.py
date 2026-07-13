"""
Tests for hook_org_lint.sh — Phase 32 Item 5 (the actual Claude Code hook).

PostToolUse, .company opt-in guard first, WARN-NEVER-BLOCK: on an Edit/Write
under org/employees/**, run schedule_validator.py's R7 checks against the
touched (hired) desk and print a warning to stderr — but ALWAYS exit 0. Core
employees' own desks are out of scope (R1-R6 already govern them).
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import _helpers

REPO = _helpers.REPO_ROOT
SCRIPTS = os.path.join(REPO, "plugin", "skills", "self-company", "scripts")
HOOK = os.path.join(SCRIPTS, "hook_org_lint.sh")
TEMPLATE = os.path.join(REPO, "plugin", "skills", "self-company",
                        "assets", "company-template")


def _run(payload, project_dir):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": project_dir}
    return subprocess.run(["bash", HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


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


class OrgLintTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = os.path.join(self.tmp, "proj")
        self.company = os.path.join(self.project, ".company")
        shutil.copytree(TEMPLATE, self.company)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file_path(self, rel):
        return os.path.join(".company", rel)


class TestOptInGuard(unittest.TestCase):
    def test_no_company_silent_exit_0(self):
        with tempfile.TemporaryDirectory() as d:
            r = _run({"tool_input": {"file_path": "x"}}, d)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")
            self.assertEqual(r.stderr, "")


class TestNoOpCases(OrgLintTestBase):
    def test_non_org_employees_file_silent(self):
        r = _run({"tool_input": {"file_path": ".company/org/policy.md"}}, self.project)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr.strip(), "")

    def test_core_desk_file_silent(self):
        r = _run({"tool_input": {
            "file_path": self.file_path("org/employees/bob/context.md")}},
            self.project)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr.strip(), "")

    def test_unparseable_stdin_fails_open(self):
        env = {**os.environ, "CLAUDE_PROJECT_DIR": self.project}
        r = subprocess.run(["bash", HOOK], input="not json",
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)

    def test_valid_hired_desk_silent(self):
        _mkdesk(self.company, "sam-jr")
        r = _run({"tool_input": {
            "file_path": self.file_path("org/employees/sam-jr/context.md")}},
            self.project)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr.strip(), "")


class TestWarnings(OrgLintTestBase):
    def test_bad_tier_warns_but_exits_0(self):
        _mkdesk(self.company, "sam-jr", tier="ceo")
        r = _run({"tool_input": {
            "file_path": self.file_path("org/employees/sam-jr/context.md")}},
            self.project)
        self.assertEqual(r.returncode, 0)          # never blocks
        self.assertIn("WARNING", r.stderr)
        self.assertIn("R7", r.stderr)
        self.assertIn("sam-jr", r.stderr)

    def test_role_claim_warns(self):
        _mkdesk(self.company, "fake-ceo", role="CEO of everything")
        r = _run({"tool_input": {
            "file_path": self.file_path("org/employees/fake-ceo/context.md")}},
            self.project)
        self.assertEqual(r.returncode, 0)
        self.assertIn("R7", r.stderr)
        self.assertIn("fake-ceo", r.stderr)

    def test_never_emits_block_decision(self):
        _mkdesk(self.company, "sam-jr", tier="ceo")
        r = _run({"tool_input": {
            "file_path": self.file_path("org/employees/sam-jr/context.md")}},
            self.project)
        self.assertNotIn('"decision"', r.stdout)
        self.assertNotIn('"block"', r.stdout)

    def test_warning_scoped_to_touched_employee_only(self):
        # Two hired desks, only one bad — the warning must name only the
        # TOUCHED one, not spill over from the other.
        _mkdesk(self.company, "good-emp")
        _mkdesk(self.company, "bad-emp", tier="ceo")
        r = _run({"tool_input": {
            "file_path": self.file_path("org/employees/good-emp/context.md")}},
            self.project)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr.strip(), "")


if __name__ == "__main__":
    unittest.main()
