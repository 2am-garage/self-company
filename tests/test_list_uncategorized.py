"""
Tests for list_uncategorized.py — the Phase-5 Item-4 backfill helper (B4).

Read-only lister: surfaces active memories missing a valid `category:` field
so the categorization itself can be dispatched to an agent. Never mutates.
"""

import os
import tempfile
import unittest

import _helpers


class TestListUncategorized(unittest.TestCase):
    def _corpus(self, d):
        l0 = os.path.join(d, "L0-working")
        _helpers.write_memory(os.path.join(l0, "no-cat.md"), id="no-cat")
        _helpers.write_memory(os.path.join(d, "L1-warm", "no-cat-l1.md"),
                              id="no-cat-l1", tier="L1")
        _helpers.write_memory(os.path.join(l0, "gone.md"), id="gone",
                              status="archived")
        p = os.path.join(l0, "has-cat.md")
        _helpers.write_memory(p, id="has-cat")
        with open(p) as f:
            txt = f.read()
        with open(p, "w") as f:
            f.write(txt.replace("owner: Tony", "owner: Tony\ncategory: profile"))
        return d

    def test_lists_only_active_uncategorized(self):
        with tempfile.TemporaryDirectory() as d:
            self._corpus(d)
            data = _helpers.run_json("list_uncategorized.py", "--memory-dir", d)
            self.assertEqual(data["count"], 2)
            self.assertEqual(sorted(f["id"] for f in data["files"]),
                             ["no-cat", "no-cat-l1"])
            self.assertEqual(data["total_active"], 3)  # archived excluded

    def test_include_archived_flag(self):
        with tempfile.TemporaryDirectory() as d:
            self._corpus(d)
            data = _helpers.run_json("list_uncategorized.py", "--memory-dir",
                                     d, "--include-archived")
            self.assertEqual(data["count"], 3)
            self.assertIn("gone", [f["id"] for f in data["files"]])

    def test_invalid_category_counts_as_uncategorized(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "L0-working", "bad.md")
            _helpers.write_memory(p, id="bad")
            with open(p) as f:
                txt = f.read()
            with open(p, "w") as f:
                f.write(txt.replace("owner: Tony",
                                    "owner: Tony\ncategory: bogus"))
            data = _helpers.run_json("list_uncategorized.py", "--memory-dir", d)
            self.assertEqual([f["id"] for f in data["files"]], ["bad"])

    def test_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            self._corpus(d)
            before = {}
            for root, _, names in os.walk(d):
                for n in names:
                    p = os.path.join(root, n)
                    with open(p) as f:
                        before[p] = f.read()
            _helpers.run_json("list_uncategorized.py", "--memory-dir", d)
            for p, txt in before.items():
                with open(p) as f:
                    self.assertEqual(f.read(), txt)


if __name__ == "__main__":
    unittest.main()
