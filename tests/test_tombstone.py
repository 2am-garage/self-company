"""
Tests for tombstone.py — the shared tombstone vocabulary (Phase 6 Item 1).

This is the ONE authoritative source every frontmatter scanner imports so the
tombstone status set can't drift. Locks the set contents and is_tombstoned.
"""

import unittest

import _helpers  # noqa: F401 - puts scripts/ on sys.path
import tombstone


class TestTombstoneVocabulary(unittest.TestCase):
    def test_set_contents(self):
        self.assertEqual(tombstone.TOMBSTONE_STATUSES,
                         frozenset({"archived", "defunct", "absorbed"}))

    def test_is_tombstoned_true_for_each(self):
        for st in ("archived", "defunct", "absorbed"):
            self.assertTrue(tombstone.is_tombstoned({"status": st}), st)

    def test_active_and_missing_are_not_tombstoned(self):
        self.assertFalse(tombstone.is_tombstoned({"status": "active"}))
        self.assertFalse(tombstone.is_tombstoned({}))
        self.assertFalse(tombstone.is_tombstoned({"status": None}))

    def test_case_and_whitespace_tolerant(self):
        self.assertTrue(tombstone.is_tombstoned({"status": " Absorbed "}))
        self.assertTrue(tombstone.is_tombstoned({"status": "ARCHIVED"}))


if __name__ == "__main__":
    unittest.main()
