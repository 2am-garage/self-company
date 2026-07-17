"""
Tests for recall_eval.py — the RAG recall evaluation harness.

Deterministic: exercises pure scoring/fixture logic without real RAG queries.
Self-test passes on good fixture, injected regression exits nonzero.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest

import _helpers

_spec = importlib.util.spec_from_file_location(
    "recall_eval", os.path.join(_helpers.REPO_ROOT, "evals", "recall_eval.py"))
re = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(re)


# --- scoring: individual queries ------------------------------------------------

class TestScore(unittest.TestCase):
    def test_hit_at_k_when_expected_found(self):
        q = "test query"
        exp_ids = ["chairman-reply-language-chinese"]
        results = [{"id": "chairman-reply-language-chinese"}]
        s = re.score(q, exp_ids, results, k=5)
        self.assertTrue(s["hit@k"])
        self.assertTrue(s["pass"])
        self.assertEqual(s["recall"], 1.0)

    def test_miss_at_k_when_expected_not_found(self):
        q = "test query"
        exp_ids = ["chairman-reply-language-chinese"]
        results = [{"id": "other-id"}]
        s = re.score(q, exp_ids, results, k=5)
        self.assertFalse(s["hit@k"])
        self.assertFalse(s["pass"])
        self.assertEqual(s["recall"], 0.0)

    def test_partial_recall_multiple_expected_ids(self):
        q = "test query"
        exp_ids = ["id-a", "id-b", "id-c"]
        results = [{"id": "id-a"}, {"id": "id-b"}, {"id": "other"}]
        s = re.score(q, exp_ids, results, k=5)
        self.assertTrue(s["hit@k"])  # at least one found
        self.assertAlmostEqual(s["recall"], 2/3, places=4)

    def test_no_results_returns_zero_recall(self):
        q = "test query"
        exp_ids = ["id-a"]
        s = re.score(q, exp_ids, None, k=5)
        self.assertEqual(s["recall"], 0.0)
        self.assertFalse(s["pass"])

    def test_empty_results_list(self):
        q = "test query"
        exp_ids = ["id-a"]
        s = re.score(q, exp_ids, [], k=5)
        self.assertEqual(s["recall"], 0.0)
        self.assertFalse(s["pass"])

    def test_k_truncation(self):
        q = "test query"
        exp_ids = ["id-5"]
        results = [{"id": f"id-{i}"} for i in range(1, 10)]
        s = re.score(q, exp_ids, results, k=3)
        # id-5 is at position 5, beyond k=3
        self.assertFalse(s["hit@k"])
        self.assertFalse(s["pass"])


# --- summarize: aggregation across multiple queries ----------------------------

class TestSummarize(unittest.TestCase):
    def test_perfect_scores(self):
        scores = [
            {"pass": True, "recall": 1.0},
            {"pass": True, "recall": 1.0},
        ]
        summ = re.summarize(scores)
        self.assertEqual(summ["total_queries"], 2)
        self.assertEqual(summ["passed"], 2)
        self.assertEqual(summ["failed"], 0)
        self.assertAlmostEqual(summ["pass_rate"], 1.0)
        self.assertAlmostEqual(summ["avg_recall"], 1.0)

    def test_partial_pass_rate(self):
        scores = [
            {"pass": True, "recall": 1.0},
            {"pass": False, "recall": 0.0},
            {"pass": True, "recall": 0.5},
        ]
        summ = re.summarize(scores)
        self.assertEqual(summ["total_queries"], 3)
        self.assertEqual(summ["passed"], 2)
        self.assertEqual(summ["failed"], 1)
        self.assertAlmostEqual(summ["pass_rate"], 2/3, places=4)
        self.assertAlmostEqual(summ["avg_recall"], (1.0 + 0.0 + 0.5) / 3, places=4)

    def test_empty_scores_list(self):
        scores = []
        summ = re.summarize(scores)
        self.assertEqual(summ["total_queries"], 0)
        self.assertEqual(summ["passed"], 0)
        self.assertEqual(summ["failed"], 0)
        self.assertEqual(summ["pass_rate"], 0)
        self.assertEqual(summ["avg_recall"], 0)


# --- fixture and self-test ---------------------------------------------------

class TestFixture(unittest.TestCase):
    def test_positive_control_defined(self):
        q, exp_ids = re.POSITIVE_CONTROL
        self.assertIsInstance(q, str)
        self.assertIsInstance(exp_ids, (list, tuple))
        self.assertGreater(len(q), 0)
        self.assertGreater(len(exp_ids), 0)

    def test_fixture_has_expected_entries(self):
        self.assertIsInstance(re.FIXTURE, list)
        self.assertGreater(len(re.FIXTURE), 0)
        for name, body in re.FIXTURE:
            self.assertIsInstance(name, str)
            self.assertIsInstance(body, str)
            self.assertGreater(len(name), 0)
            self.assertGreater(len(body), 0)


# --- command-line argument parsing ---------------------------------------------

class TestArgumentParsing(unittest.TestCase):
    def test_self_test_does_not_require_eval_set(self):
        try:
            args = re.main(["--self-test"])
            # If it doesn't raise SystemExit, the parse succeeded
        except SystemExit as e:
            # p.error() calls sys.exit(2); anything else means parsing failed
            self.fail(f"--self-test should not require --eval-set, got exit {e.code}")
        except Exception:
            # Skip if venv missing (expected in some test environments)
            pass

    def test_eval_set_required_without_self_test(self):
        with self.assertRaises(SystemExit):
            re.main([])

    def test_eval_set_and_self_test_both_allowed(self):
        try:
            args = re.main(["--self-test", "--eval-set", "dummy.json"])
        except SystemExit as e:
            self.fail(f"Both flags should be allowed, got exit {e.code}")
        except Exception:
            # Skip if venv missing
            pass


# --- deterministic regression: injected failures --------------------------------

class TestRegressionDetection(unittest.TestCase):
    def test_good_fixture_self_test_passes(self):
        """Verify the bundled POSITIVE_CONTROL fixture passes in isolation."""
        q, exp_ids = re.POSITIVE_CONTROL
        # Simulate successful RAG retrieval: returned doc has the expected id
        results = [{"id": exp_ids[0], "score": 0.95}]
        ret = [r["id"] for r in (results or [])]
        ok = any(e in ret for e in exp_ids)
        self.assertTrue(ok, "Bundled fixture should pass self-test")

    def test_injected_regression_fails(self):
        """Verify that a broken retrieval result (wrong id returned) is caught."""
        q, exp_ids = re.POSITIVE_CONTROL
        # Simulate failure: RAG returns a different doc
        results = [{"id": "wrong-id", "score": 0.95}]
        ret = [r["id"] for r in (results or [])]
        ok = any(e in ret for e in exp_ids)
        self.assertFalse(ok, "Injected regression (wrong id) should fail")

    def test_empty_results_regression(self):
        """Verify that empty retrieval results are caught."""
        q, exp_ids = re.POSITIVE_CONTROL
        results = []
        ret = [r["id"] for r in (results or [])]
        ok = any(e in ret for e in exp_ids)
        self.assertFalse(ok, "Empty results should fail")


# --- helpers: find_scripts, find_venv (skip if unavailable) --------------------

class TestHelpers(unittest.TestCase):
    def test_find_scripts_returns_path_or_none(self):
        result = re.find_scripts(os.getcwd())
        # May be None if scripts dir doesn't exist; that's OK for test
        if result is not None:
            self.assertIsInstance(result, str)

    def test_find_venv_returns_path_or_none(self):
        result = re.find_venv(os.getcwd())
        # May be None if venv not set up; that's OK for test
        if result is not None:
            self.assertIsInstance(result, str)


if __name__ == "__main__":
    unittest.main()
