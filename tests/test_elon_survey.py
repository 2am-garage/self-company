"""
Tests for elon_survey.py — Elon's daily TODO generator.

build_todos is pure (rule-based prioritization); test it directly. Also a small
end-to-end check that a survey over a temp .company writes a todo file.
"""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock

import _helpers

_spec = importlib.util.spec_from_file_location(
    "elon_survey", os.path.join(_helpers.SCRIPTS_DIR, "elon_survey.py"))
es = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(es)


class TestBuildTodos(unittest.TestCase):
    def test_contradiction_is_top_priority(self):
        entropy = {"entropy": 0.4, "details": {"contradiction_pairs": [["a", "b"]],
                                               "duplicate_pairs": [["c", "d"]], "stale_ids": []}}
        todos = es.build_todos(entropy, None, None)
        self.assertTrue(todos[0][1].startswith("Adjudicate"))  # contradictions first

    def test_unverifiable_and_promote_surface(self):
        verify = {"unverifiable": ["x", "y"]}
        decay = {"actions": {"upgrade_candidates": [{"id": "z"}], "demote": []}}
        todos = es.build_todos(None, decay, verify)
        texts = " ".join(t[1] for t in todos)
        self.assertIn("Re-capture", texts)
        self.assertIn("Promote", texts)

    def test_healthy_company_no_todos(self):
        entropy = {"entropy": 0.0, "details": {"contradiction_pairs": [],
                                               "duplicate_pairs": [], "stale_ids": []}}
        verify = {"unverifiable": []}
        decay = {"actions": {"upgrade_candidates": [], "demote": []}}
        self.assertEqual(es.build_todos(entropy, decay, verify), [])


class TestSurveyE2E(unittest.TestCase):
    def test_writes_todo_file(self):
        with tempfile.TemporaryDirectory() as d:
            company = os.path.join(d, ".company")
            os.makedirs(os.path.join(company, "memory", "L0-working"))
            os.makedirs(os.path.join(company, "org"))
            # minimal policy so the sub-scripts run
            with open(os.path.join(company, "org", "policy.md"), "w") as f:
                f.write("| `w1` (dup) | **0.25** |\n")
            rc = es.main(["--company", company, "--now", "2026-06-30"])
            self.assertEqual(rc, 0)
            todo = os.path.join(company, "ops", "plans", "todo-2026-06-30.md")
            self.assertTrue(os.path.exists(todo))
            with open(todo) as f:
                self.assertIn("Daily Survey & TODO", f.read())


class TestCoreStepTimeout(unittest.TestCase):
    """Phase 27 MUST-FIX 3: elon_survey's internal core re-invocation is bounded
    by SELF_COMPANY_CORE_STEP_TIMEOUT (not a hardcoded 120s) and a timeout is
    SURFACED (in `timed_out`), never silently absorbed as a healthy quiet day."""

    def test_timeout_env_honored_default_and_override(self):
        self.assertEqual(es._core_step_timeout(), 900)   # default
        with unittest.mock.patch.dict(os.environ, {"SELF_COMPANY_CORE_STEP_TIMEOUT": "42"}):
            self.assertEqual(es._core_step_timeout(), 42)
        with unittest.mock.patch.dict(os.environ, {"SELF_COMPANY_CORE_STEP_TIMEOUT": "nonsense"}):
            self.assertEqual(es._core_step_timeout(), 900)   # bad value -> safe default

    def test_run_json_records_timeout_never_silent(self):
        timed = []
        real_run = subprocess.run

        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        with unittest.mock.patch("subprocess.run", side_effect=fake_run):
            r = es._run_json("decay.py", "--x", timed_out=timed)
        self.assertIsNone(r)
        self.assertEqual(timed, ["decay.py"])   # surfaced, not swallowed

    def test_survey_summary_carries_timed_out_and_log_notes_it(self):
        def fake_run(cmd, **kw):
            # Only decay.py (survey's dry-run call) times out; the others no-op.
            if any("decay.py" in str(c) for c in cmd):
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            class _R:  # minimal stand-in with empty-json stdout
                stdout = "{}"
            return _R()

        with tempfile.TemporaryDirectory() as d:
            company = os.path.join(d, ".company")
            os.makedirs(os.path.join(company, "memory", "L0-working"))
            os.makedirs(os.path.join(company, "org"))
            with open(os.path.join(company, "org", "policy.md"), "w") as f:
                f.write("| `w1` (dup) | **0.25** |\n")
            with unittest.mock.patch("subprocess.run", side_effect=fake_run):
                text, summary = es.survey(company, "2026-07-10")
            self.assertIn("decay.py", summary["timed_out"])
            self.assertIn("core-step TIMEOUT", text)   # loud in the plan file too


if __name__ == "__main__":
    unittest.main()
