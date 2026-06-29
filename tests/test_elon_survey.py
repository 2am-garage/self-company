"""
Tests for elon_survey.py — Elon's daily TODO generator.

build_todos is pure (rule-based prioritization); test it directly. Also a small
end-to-end check that a survey over a temp .company writes a todo file.
"""

import importlib.util
import json
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
