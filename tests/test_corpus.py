"""
Tests for corpus.py — Phase 28 Item 4a (D4): the shared memory-corpus walk +
parse + body-extraction primitive that replaces six independently
re-implemented loaders (decay/entropy/verify/reinforce/rag_index/elon_survey).

Covers: the walk (whole-tree + tier-restricted), id/tombstone gating (and its
`include_archived` escape hatch), body extraction aligned with frontmatter.py's
correct closing-fence detection (not a raw substring search), the
`with_skipped` seam callers with a bespoke gating order use, and count_by_tier
(elon_survey.tier_counts's byte-identical byproduct).
"""

import os
import tempfile
import unittest

import _helpers  # noqa: F401  (puts scripts/ on sys.path)
import corpus


def _write(path, id="mem-1", tier="L0", status="active", body="a body", extra=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            f"id: {id}\n"
            f"tier: {tier}\n"
            f"status: {status}\n"
            'sources: ["[s#1]"]\n'
            f"{extra}"
            "---\n"
            f"{body}\n"
        )


class TestWalk(unittest.TestCase):
    def test_missing_dir_returns_empty(self):
        self.assertEqual(corpus.iter_memory_paths("/no/such/dir"), [])
        self.assertEqual(corpus.load_memories("/no/such/dir"), [])

    def test_walks_whole_tree_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "L1-warm", "b.md"), id="b")
            _write(os.path.join(d, "L0-working", "a.md"), id="a")
            paths = corpus.iter_memory_paths(d)
            self.assertEqual([os.path.basename(str(p)) for p in paths], ["a.md", "b.md"])

    def test_tiers_restricts_walk_to_subdir(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "L0-working", "a.md"), id="a")
            _write(os.path.join(d, "L1-warm", "b.md"), id="b")
            only_l0 = corpus.iter_memory_paths(d, tiers=["L0"])
            self.assertEqual(len(only_l0), 1)
            self.assertTrue(str(only_l0[0]).endswith("a.md"))


class TestLoadMemories(unittest.TestCase):
    def test_gates_missing_id(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "L0-working", "noid.md")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write("---\ntier: L0\nstatus: active\n---\nbody\n")
            self.assertEqual(corpus.load_memories(d), [])

    def test_gates_tombstoned_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "L0-working", "arch.md"), id="a", status="archived")
            self.assertEqual(corpus.load_memories(d), [])
            # include_archived brings it back
            mems = corpus.load_memories(d, include_archived=True)
            self.assertEqual([m["id"] for m in mems], ["a"])

    def test_defunct_and_absorbed_are_tombstones_too(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "L0-working", "d.md"), id="d", status="defunct")
            _write(os.path.join(d, "L0-working", "ab.md"), id="ab", status="absorbed")
            self.assertEqual(corpus.load_memories(d), [])

    def test_active_memory_included_with_expected_fields(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "L0-working", "x.md"), id="x", tier="L0", body="hello world")
            mems = corpus.load_memories(d)
            self.assertEqual(len(mems), 1)
            m = mems[0]
            self.assertEqual(m["id"], "x")
            self.assertEqual(m["tier"], "L0")
            self.assertEqual(m["status"], "active")
            self.assertEqual(m["body"], "hello world")
            self.assertIn("fm", m)
            self.assertEqual(m["fm"]["id"], "x")
            self.assertGreater(m["close_index"], 0)

    def test_body_extraction_not_confused_by_dashes_in_body(self):
        # The exact entropy bug frontmatter.py's docstring documents: a body
        # line of plain dashes must NOT be treated as a fence / truncate body.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "L0-working", "y.md")
            _write(p, id="y", body="first line\n----\nsecond line")
            mems = corpus.load_memories(d)
            self.assertEqual(len(mems), 1)
            self.assertIn("----", mems[0]["body"])
            self.assertIn("second line", mems[0]["body"])

    def test_no_frontmatter_file_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "L0-working", "plain.md")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write("just some text\nno frontmatter here\n")
            self.assertEqual(corpus.load_memories(d), [])

    def test_with_skipped_reasons(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "L0-working", "ok.md"), id="ok")
            _write(os.path.join(d, "L0-working", "arch.md"), id="a", status="archived")
            p = os.path.join(d, "L0-working", "noid.md")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write("---\ntier: L0\nstatus: active\n---\nbody\n")
            mems, skipped = corpus.load_memories(d, with_skipped=True)
            self.assertEqual([m["id"] for m in mems], ["ok"])
            reasons = dict((os.path.basename(p), r) for p, r in skipped)
            self.assertEqual(reasons["arch.md"], "tombstoned")
            self.assertEqual(reasons["noid.md"], "no_id")


class TestCountByTier(unittest.TestCase):
    def test_counts_each_tier_dir_including_tombstoned(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "L0-working", "a.md"), id="a")
            _write(os.path.join(d, "L0-working", "b.md"), id="b", status="archived")
            _write(os.path.join(d, "L1-warm", "c.md"), id="c")
            counts = corpus.count_by_tier(d)
            self.assertEqual(counts, {"L0": 2, "L1": 1, "L2": 0})

    def test_missing_dirs_count_zero(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(corpus.count_by_tier(d), {"L0": 0, "L1": 0, "L2": 0})


class TestReadRecord(unittest.TestCase):
    def test_read_error_returns_none(self):
        self.assertIsNone(corpus.read_record("/no/such/file.md"))

    def test_no_opening_fence_close_index_negative_one(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write("no fence at all\n")
            rec = corpus.read_record(p)
            self.assertEqual(rec["close_index"], -1)
            self.assertEqual(rec["fm"], {})


if __name__ == "__main__":
    unittest.main()
