"""
Tests for decay.py — decay math and tier classification.

Covers the deterministic core (half-life, decay_score, classify_record) and a
CLI smoke confirming policy provenance is reported (P3) and that the documented
default --config path is honoured.
"""

import os
import tempfile
import unittest
from datetime import datetime

import _helpers
import decay


def mem(**kw):
    base = {
        "id": "m", "tier": "L0", "owner": "Tony", "sources": ["[s#1]"],
        "created": "2026-06-01", "last_reinforced": "2026-06-01",
        "reinforce_count": 1, "decay_score": 1.0, "status": "active", "_body": "x",
    }
    base.update(kw)
    return base


DEF = dict(hl_base=7.0, hl_growth=0.5, l0_drop_threshold=0.25,
           l1_archive_threshold=0.15, l1_demote_rc=2, l0_to_l1_rc=2, l1_to_l2_rc=4)


def classify(m, now="2026-06-15"):
    return decay.classify_record(m, datetime.strptime(now, "%Y-%m-%d"), **DEF)


class TestDecayMath(unittest.TestCase):
    def test_half_life_formula(self):
        self.assertEqual(decay.half_life(1, 7.0, 0.5), 7.0)
        self.assertEqual(decay.half_life(3, 7.0, 0.5), 14.0)
        self.assertEqual(decay.half_life(5, 7.0, 0.5), 21.0)

    def test_decay_score_one_halflife(self):
        # age == half_life -> 0.5
        self.assertAlmostEqual(decay.compute_decay_score(7.0, 1, 7.0, 0.5), 0.5, places=6)

    def test_decay_score_fresh_is_one(self):
        self.assertEqual(decay.compute_decay_score(0.0, 1, 7.0, 0.5), 1.0)

    def test_decay_score_clamped(self):
        self.assertLessEqual(decay.compute_decay_score(1000.0, 1, 7.0, 0.5), 1.0)
        self.assertGreaterEqual(decay.compute_decay_score(1000.0, 1, 7.0, 0.5), 0.0)

    def test_rc_below_one_treated_as_one(self):
        self.assertEqual(decay.compute_decay_score(7.0, 0, 7.0, 0.5),
                         decay.compute_decay_score(7.0, 1, 7.0, 0.5))


class TestClassify(unittest.TestCase):
    def test_l0_fresh_keep(self):
        action, _ = classify(mem(last_reinforced="2026-06-14"))  # 1 day old
        self.assertEqual(action, "keep")

    def test_l0_stale_drops(self):
        # 30 days, rc=1 -> decay ~0.05 < 0.25
        action, info = classify(mem(last_reinforced="2026-05-16"))
        self.assertEqual(action, "drop")
        self.assertLess(info["decay_score"], 0.25)

    def test_l0_rc2_is_upgrade_candidate(self):
        action, _ = classify(mem(reinforce_count=2, last_reinforced="2026-06-14"))
        self.assertEqual(action, "upgrade-candidate")

    def test_l1_low_rc_demotes(self):
        # L1 rc=2 gone cold -> demote (rc <= L1_DEMOTE_RC)
        action, _ = classify(mem(tier="L1", reinforce_count=2, last_reinforced="2026-04-01"))
        self.assertEqual(action, "demote")

    def test_l1_high_rc_archives(self):
        # L1 rc=3 gone cold -> archive (rc > L1_DEMOTE_RC)
        action, _ = classify(mem(tier="L1", reinforce_count=3, last_reinforced="2026-04-01"))
        self.assertEqual(action, "archive")

    def test_l1_rc4_upgrade_candidate(self):
        action, _ = classify(mem(tier="L1", reinforce_count=4, last_reinforced="2026-06-14"))
        self.assertEqual(action, "upgrade-candidate")

    def test_l2_never_decays(self):
        action, _ = classify(mem(tier="L2", reinforce_count=5, last_reinforced="2020-01-01"))
        self.assertEqual(action, "l2-keep")

    def test_missing_date_is_kept_untouched(self):
        action, info = classify(mem(last_reinforced=None))
        self.assertEqual(action, "keep")
        self.assertIsNone(info["decay_score"])

    def test_unparseable_date_is_kept_untouched(self):
        action, info = classify(mem(last_reinforced="not-a-date"))
        self.assertEqual(action, "keep")
        self.assertIsNone(info["decay_score"])


class TestApplyDrop(unittest.TestCase):
    def test_apply_actually_deletes_stale_l0(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L0-working", "old.md")
            _helpers.write_memory(path, id="old", last_reinforced="2026-05-01")
            rc, out, err = _helpers.run_script(
                "decay.py", "--memory-dir", d, "--now", "2026-06-25",
                "--config", "/nonexistent.md", "--apply")
            self.assertEqual(rc, 0, err)
            self.assertFalse(os.path.exists(path), "stale L0 memory should be deleted")

    def test_apply_preserves_verified_date(self):
        # Regression: decay --apply rewrites frontmatter and must NOT drop the
        # VERIFY stamp (verified_date/verified_by), or it fights the verify loop.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L1-warm", "v.md")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                f.write("---\nid: ver\ntier: L1\nowner: Tony\n"
                        'sources: ["[s#1]"]\ncreated: 2026-06-01\nlast_reinforced: 2026-06-20\n'
                        "reinforce_count: 2\ndecay_score: 1.0\nstatus: active\n"
                        "verified_date: 2026-06-21\nverified_by: Gibby\n---\nbody\n")
            rc, out, err = _helpers.run_script(
                "decay.py", "--memory-dir", d, "--now", "2026-06-25",
                "--config", "/nonexistent.md", "--apply")
            self.assertEqual(rc, 0, err)
            with open(path) as f:
                txt = f.read()
            self.assertIn("verified_date: 2026-06-21", txt)
            self.assertIn("verified_by: Gibby", txt)


class TestCLIProvenance(unittest.TestCase):
    def test_config_block_reports_policy_source(self):
        real_policy = os.path.join(
            _helpers.REPO_ROOT, "assets", "company-template", "org", "policy.md")
        with tempfile.TemporaryDirectory() as d:
            data = _helpers.run_json(
                "decay.py", "--memory-dir", d, "--now", "2026-06-25",
                "--config", real_policy)
            self.assertEqual(data["config"]["sources"]["HL_BASE"], "policy")
            self.assertEqual(data["config"]["values"]["HL_BASE"], 7.0)

    def test_policy_table_tuning_changes_behavior(self):
        # The P1 guarantee: editing the §7 TABLE actually changes outcomes.
        real_policy = os.path.join(
            _helpers.REPO_ROOT, "assets", "company-template", "org", "policy.md")
        with open(real_policy, encoding="utf-8") as f:
            policy_text = f.read()
        tuned = policy_text.replace("| `L0_DROP_THRESHOLD` | **0.25**",
                                    "| `L0_DROP_THRESHOLD` | **0.99**")
        self.assertNotEqual(tuned, policy_text, "policy fixture line not found")
        with tempfile.TemporaryDirectory() as d:
            _helpers.write_memory(os.path.join(d, "L0-working", "a.md"),
                                  id="a", last_reinforced="2026-06-15")  # 10d, decay~0.37
            pol = os.path.join(d, "policy.md")
            with open(pol, "w", encoding="utf-8") as f:
                f.write(tuned)
            data = _helpers.run_json("decay.py", "--memory-dir", d,
                                     "--now", "2026-06-25", "--config", pol)
            self.assertEqual(data["config"]["values"]["L0_DROP_THRESHOLD"], 0.99)
            self.assertEqual([x["id"] for x in data["actions"]["drop"]], ["a"])


if __name__ == "__main__":
    unittest.main()
