"""
Tests for entropy.py — the four entropy dimensions + policy-driven weights.

Black-box via the CLI (JSON output) so tests are decoupled from internal dict
shapes, plus a provenance check that weights now come from policy (P1/P3).
"""

import os
import tempfile
import unittest

import _helpers

REAL_POLICY = os.path.join(
    _helpers.REPO_ROOT, "skills", "self-company", "assets", "company-template", "org", "policy.md")


class TestDimensions(unittest.TestCase):
    def _entropy(self, d):
        return _helpers.run_json("entropy.py", "--memory-dir", d,
                                 "--now", "2026-06-25", "--config", "/nonexistent.md")

    def test_empty_is_zero(self):
        with tempfile.TemporaryDirectory() as d:
            data = self._entropy(d)
            self.assertEqual(data["total_memories"], 0)
            self.assertEqual(data["entropy"], 0.0)

    def test_duplicate_pair_detected(self):
        with tempfile.TemporaryDirectory() as d:
            body = "The Chairman prefers async await patterns in Python design clearly."
            _helpers.write_memory(os.path.join(d, "L0-working", "d1.md"),
                                  id="pref-async-1", body=body)
            _helpers.write_memory(os.path.join(d, "L0-working", "d2.md"),
                                  id="pref-async-2", body=body)
            data = self._entropy(d)
            self.assertGreater(data["dimensions"]["dup_rate"], 0.0)
            self.assertEqual(len(data["details"]["duplicate_pairs"]), 1)

    def test_contradiction_detected(self):
        # Same slug family (pref-*) with opposing keywords async/sync.
        with tempfile.TemporaryDirectory() as d:
            _helpers.write_memory(os.path.join(d, "L0-working", "c1.md"),
                                  id="pref-mode-1",
                                  body="Chairman likes async patterns and wants async everywhere.")
            _helpers.write_memory(os.path.join(d, "L0-working", "c2.md"),
                                  id="pref-mode-2",
                                  body="Chairman dislikes async, prefers sync and wants sync everywhere.")
            data = self._entropy(d)
            self.assertGreater(data["dimensions"]["contradiction_score"], 0.0,
                               "expected a contradiction candidate")

    def test_stale_detected(self):
        # L0 memory far past the drop threshold counts as stale.
        with tempfile.TemporaryDirectory() as d:
            _helpers.write_memory(os.path.join(d, "L0-working", "s.md"),
                                  id="old-1", last_reinforced="2026-01-01")
            data = _helpers.run_json("entropy.py", "--memory-dir", d,
                                     "--now", "2026-06-25", "--config", REAL_POLICY)
            self.assertGreater(data["dimensions"]["stale_rate"], 0.0)
            self.assertIn("old-1", data["details"]["stale_ids"])

    def test_unverified_detected(self):
        with tempfile.TemporaryDirectory() as d:
            # sourced but NOT verified -> unverified (the honest, new definition)
            _helpers.write_memory(os.path.join(d, "L0-working", "a.md"), id="needs-verify")
            # no sources -> unverified (can never be verified)
            _helpers.write_memory(os.path.join(d, "L0-working", "b.md"), id="nosrc-1", sources="[]")
            # has verified_date -> NOT unverified
            with open(os.path.join(d, "L0-working", "c.md"), "w") as f:
                f.write("---\nid: verified-1\ntier: L0\nowner: Tony\n"
                        'sources: ["[s#1]"]\ncreated: 2026-06-01\nlast_reinforced: 2026-06-01\n'
                        "reinforce_count: 1\ndecay_score: 1.0\nstatus: active\n"
                        "verified_date: 2026-06-02\nverified_by: Gibby\n---\nbody\n")
            data = self._entropy(d)
            ids = data["details"]["unverified_ids"]
            self.assertIn("needs-verify", ids)
            self.assertIn("nosrc-1", ids)
            self.assertNotIn("verified-1", ids)
            self.assertAlmostEqual(data["dimensions"]["unverified_rate"], 2 / 3, places=2)


class TestWeightProvenance(unittest.TestCase):
    def test_weights_from_policy(self):
        with tempfile.TemporaryDirectory() as d:
            data = _helpers.run_json("entropy.py", "--memory-dir", d,
                                     "--now", "2026-06-25", "--config", REAL_POLICY)
            self.assertEqual(data["weights"],
                             {"w1": 0.25, "w2": 0.35, "w3": 0.2, "w4": 0.2})
            self.assertEqual(data["config"]["sources"]["W1_DUP"], "policy")

    def test_tuning_weight_in_table_changes_weights(self):
        with open(REAL_POLICY, encoding="utf-8") as f:
            text = f.read()
        tuned = text.replace("| `w1` (duplication) | **0.25**",
                             "| `w1` (duplication) | **0.50**")
        self.assertNotEqual(tuned, text, "w1 policy fixture line not found")
        with tempfile.TemporaryDirectory() as d:
            pol = os.path.join(d, "policy.md")
            with open(pol, "w", encoding="utf-8") as f:
                f.write(tuned)
            data = _helpers.run_json("entropy.py", "--memory-dir", d,
                                     "--now", "2026-06-25", "--config", pol)
            self.assertEqual(data["weights"]["w1"], 0.5)


if __name__ == "__main__":
    unittest.main()
