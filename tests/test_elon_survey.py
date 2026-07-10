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


class TestFedMode(unittest.TestCase):
    """Phase 28 Item 1 (Tony C1): --entropy-json/--decay-json/--verify-json +
    --no-recompute feed the survey the core's own JSON instead of letting it
    re-invoke entropy/decay/verify as subprocesses."""

    def _company(self, d):
        company = os.path.join(d, ".company")
        os.makedirs(os.path.join(company, "memory", "L0-working"))
        os.makedirs(os.path.join(company, "org"))
        with open(os.path.join(company, "org", "policy.md"), "w") as f:
            f.write("| `w1` (dup) | **0.25** |\n")
        return company

    def test_fed_mode_never_spawns_subprocess(self):
        # A subprocess.run call in fed mode would mean the recompute snuck
        # back in — the exact structural bug Item 1 removes.
        with tempfile.TemporaryDirectory() as d:
            company = self._company(d)
            ep = os.path.join(d, "e.json")
            dp = os.path.join(d, "d.json")
            vp = os.path.join(d, "v.json")
            with open(ep, "w") as f:
                json.dump({"entropy": 0.1, "details": {}}, f)
            with open(dp, "w") as f:
                json.dump({"actions": {}}, f)
            with open(vp, "w") as f:
                json.dump({"unverifiable": []}, f)
            with unittest.mock.patch("subprocess.run") as m:
                text, summary = es.survey(company, "2026-07-10",
                                          entropy_json=ep, decay_json=dp,
                                          verify_json=vp, no_recompute=True)
            m.assert_not_called()
            self.assertEqual(summary["timed_out"], [])

    def test_fed_mode_is_pure_function_of_the_three_files(self):
        # Fabricated content that could ONLY appear in the todo list if the fed
        # JSON was actually used (a fresh recompute over this near-empty
        # fixture corpus could never independently produce these exact ids).
        with tempfile.TemporaryDirectory() as d:
            company = self._company(d)
            ep = os.path.join(d, "e.json")
            dp = os.path.join(d, "d.json")
            vp = os.path.join(d, "v.json")
            with open(ep, "w") as f:
                json.dump({"entropy": 0.9, "details": {
                    "contradiction_pairs": [["zz-fabricated-a", "zz-fabricated-b"]],
                    "duplicate_pairs": [], "stale_ids": []}}, f)
            with open(dp, "w") as f:
                json.dump({"actions": {"upgrade_candidates": [], "demote": []}}, f)
            with open(vp, "w") as f:
                json.dump({"unverifiable": ["zz-fabricated-unverified"]}, f)
            with unittest.mock.patch("subprocess.run") as m:
                text, summary = es.survey(company, "2026-07-10",
                                          entropy_json=ep, decay_json=dp,
                                          verify_json=vp, no_recompute=True)
            m.assert_not_called()
            self.assertIn("zz-fabricated-a", text)
            self.assertIn("zz-fabricated-unverified", text)
            # Determinism: re-running against the SAME three files is byte-identical.
            with unittest.mock.patch("subprocess.run") as m2:
                text2, summary2 = es.survey(company, "2026-07-10",
                                            entropy_json=ep, decay_json=dp,
                                            verify_json=vp, no_recompute=True)
            m2.assert_not_called()
            self.assertEqual(text, text2)
            self.assertEqual(summary, summary2)

    def test_missing_or_invalid_fed_json_degrades_to_none_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            company = self._company(d)
            garbage = os.path.join(d, "garbage.json")
            with open(garbage, "w") as f:
                f.write("not json {{{")
            missing = os.path.join(d, "does-not-exist.json")
            with unittest.mock.patch("subprocess.run") as m:
                text, summary = es.survey(company, "2026-07-10",
                                          entropy_json=garbage, decay_json=missing,
                                          verify_json=None, no_recompute=True)
            m.assert_not_called()
            self.assertIn("Daily Survey & TODO", text)
            self.assertEqual(summary["todos"], 0)

    def test_no_recompute_cli_flag_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            company = self._company(d)
            ep = os.path.join(d, "e.json")
            with open(ep, "w") as f:
                json.dump({"entropy": 0.0, "details": {}}, f)
            with unittest.mock.patch("subprocess.run") as m:
                rc = es.main(["--company", company, "--now", "2026-07-10",
                             "--entropy-json", ep, "--no-recompute"])
            m.assert_not_called()
            self.assertEqual(rc, 0)

    def test_standalone_mode_unchanged_when_no_flags_given(self):
        # No entropy/decay/verify-json, no --no-recompute -> the pre-existing
        # subprocess re-invocation path runs exactly as before.
        with tempfile.TemporaryDirectory() as d:
            company = self._company(d)
            with unittest.mock.patch("subprocess.run", wraps=subprocess.run) as m:
                text, summary = es.survey(company, "2026-07-10")
            self.assertTrue(m.called)


if __name__ == "__main__":
    unittest.main()
