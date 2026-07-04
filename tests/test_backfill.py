"""
Tests for backfill_rc.py — Phase 5 Item 1 one-time backfill: clamp inflated
reinforce_count down to the distinct-session-id count in sources.

Dry-run default, --apply is surgical (only the reinforce_count line changes),
never raises rc, charter seeds are reported but untouched.
"""

import os
import tempfile
import unittest

import _helpers


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestBackfillRc(unittest.TestCase):
    def _corpus(self, d):
        l0 = os.path.join(d, "L0-working")
        # Inflated: rc=5 but only 2 distinct sessions (the N1 live pattern).
        _helpers.write_memory(
            os.path.join(l0, "inflated.md"), id="inflated",
            sources='["[sessA#1]", "[sessA#4]", "[sessB#2]"]', reinforce_count=5)
        # Honest: rc equals distinct-session count -> untouched.
        _helpers.write_memory(
            os.path.join(l0, "honest.md"), id="honest",
            sources='["[sessA#1]", "[sessB#2]"]', reinforce_count=2)
        # Below distinct count: NEVER raised.
        _helpers.write_memory(
            os.path.join(l0, "low.md"), id="low",
            sources='["[sessA#1]", "[sessB#2]", "[sessC#3]"]', reinforce_count=1)
        return l0

    def test_dry_run_reports_but_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as d:
            l0 = self._corpus(d)
            data = _helpers.run_json("backfill_rc.py", "--memory-dir", d)
            self.assertFalse(data["applied"])
            self.assertEqual([c["id"] for c in data["corrected"]], ["inflated"])
            c = data["corrected"][0]
            self.assertEqual((c["rc_before"], c["rc_after"],
                              c["distinct_sessions"]), (5, 2, 2))
            # dry-run: file unchanged
            self.assertIn("reinforce_count: 5",
                          _read(os.path.join(l0, "inflated.md")))

    def test_apply_corrects_only_inflated_and_is_surgical(self):
        with tempfile.TemporaryDirectory() as d:
            l0 = self._corpus(d)
            before_honest = _read(os.path.join(l0, "honest.md"))
            before_low = _read(os.path.join(l0, "low.md"))
            data = _helpers.run_json("backfill_rc.py", "--memory-dir", d,
                                     "--apply")
            self.assertTrue(data["applied"])
            txt = _read(os.path.join(l0, "inflated.md"))
            self.assertIn("reinforce_count: 2", txt)
            # surgical: sources/status/body untouched
            self.assertIn('sources: ["[sessA#1]", "[sessA#4]", "[sessB#2]"]', txt)
            self.assertIn("status: active", txt)
            # honest + low untouched byte-for-byte (rc never raised)
            self.assertEqual(_read(os.path.join(l0, "honest.md")), before_honest)
            self.assertEqual(_read(os.path.join(l0, "low.md")), before_low)
            # idempotent: re-run corrects nothing
            data2 = _helpers.run_json("backfill_rc.py", "--memory-dir", d,
                                      "--apply")
            self.assertEqual(data2["corrected"], [])

    def test_charter_seed_reported_never_mutated(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "L2-cold", "profile", "merge-gate.md")
            _helpers.write_memory(p, id="merge-gate", tier="L2",
                                  sources='["charter:merge-gate"]',
                                  reinforce_count=9)
            data = _helpers.run_json("backfill_rc.py", "--memory-dir", d,
                                     "--apply")
            self.assertEqual(data["corrected"], [])
            self.assertEqual([c["id"] for c in data["skipped_charter"]],
                             ["merge-gate"])
            self.assertIn("reinforce_count: 9", _read(p))

    def test_missing_dir_warns(self):
        data = _helpers.run_json("backfill_rc.py", "--memory-dir",
                                 "/no/such/dir")
        self.assertTrue(data["warnings"])
        self.assertEqual(data["corrected"], [])


if __name__ == "__main__":
    unittest.main()
