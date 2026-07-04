"""
Tests for reinforce_memory.plan_reinforcements (C1) — the pure decision step.

The embedding/nearest-neighbour part needs the RAG venv and is exercised by a live
dry-run; here we lock the conservative rules: absorbed is always L0, L2 is never
touched, L1 is the canonical when paired with L0, threshold is respected.
"""

import os
os.environ["SC_RAG_REEXEC"] = "1"  # do NOT re-exec into the venv during tests

import importlib.util
import tempfile
import unittest

import _helpers

_spec = importlib.util.spec_from_file_location(
    "reinforce_memory", os.path.join(_helpers.SCRIPTS_DIR, "reinforce_memory.py"))
rm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rm)


def M(id, tier, created="2026-06-01"):
    return {"id": id, "tier": tier, "created": created}


class TestPlan(unittest.TestCase):
    def test_l0_l0_keeps_older_absorbs_newer(self):
        mems = {"a": M("a", "L0", "2026-06-01"), "b": M("b", "L0", "2026-06-05")}
        r, skip = rm.plan_reinforcements(mems, [("a", "b", 0.95)], 0.92)
        self.assertEqual(r, [{"canonical": "a", "absorbed": "b",
                              "canonical_tier": "L0", "score": 0.95}])
        self.assertEqual(skip, [])

    def test_l0_into_l1_canonical_is_l1(self):
        mems = {"a": M("a", "L0"), "c": M("c", "L1")}
        r, _ = rm.plan_reinforcements(mems, [("a", "c", 0.97)], 0.92)
        self.assertEqual(r[0]["canonical"], "c")
        self.assertEqual(r[0]["absorbed"], "a")

    def test_l2_is_never_touched(self):
        mems = {"a": M("a", "L0"), "e": M("e", "L2")}
        r, skip = rm.plan_reinforcements(mems, [("a", "e", 0.99)], 0.92)
        self.assertEqual(r, [])
        self.assertEqual(skip[0]["pair"], ["a", "e"])

    def test_below_threshold_nothing(self):
        mems = {"a": M("a", "L0"), "b": M("b", "L0")}
        r, skip = rm.plan_reinforcements(mems, [("a", "b", 0.80)], 0.92)
        self.assertEqual((r, skip), ([], []))

    def test_l1_l1_not_auto_merged(self):
        mems = {"a": M("a", "L1"), "b": M("b", "L1")}
        r, _ = rm.plan_reinforcements(mems, [("a", "b", 0.99)], 0.92)
        self.assertEqual(r, [])  # warm memories aren't auto-merged

    def test_each_memory_used_once(self):
        mems = {"a": M("a", "L0", "2026-06-01"),
                "b": M("b", "L0", "2026-06-02"),
                "c": M("c", "L0", "2026-06-03")}
        r, _ = rm.plan_reinforcements(
            mems, [("a", "b", 0.95), ("a", "c", 0.94), ("b", "c", 0.93)], 0.92)
        involved = [x for rr in r for x in (rr["canonical"], rr["absorbed"])]
        self.assertEqual(len(involved), len(set(involved)))  # no id reused


def _write(path, id, sources):
    with open(path, "w") as f:
        f.write(f"---\nid: {id}\ntier: L0\nowner: Tony\nsources: {sources}\n"
                f"created: 2026-06-01\nlast_reinforced: 2026-06-01\nreinforce_count: 1\n"
                f"decay_score: 1.0\nstatus: active\n---\nbody {id}\n")


class TestLoadSkipsTombstones(unittest.TestCase):
    """Phase 6 Item 1: load_memories excludes ALL tombstones (archived /
    defunct / absorbed) via the shared vocabulary — a tombstoned dup must not
    become a reinforcement candidate or re-surface as a merge target."""

    def _w(self, path, id, status):
        with open(path, "w") as f:
            f.write(f"---\nid: {id}\ntier: L0\nowner: Tony\nsources: [\"[s#1]\"]\n"
                    f"created: 2026-06-01\nlast_reinforced: 2026-06-01\n"
                    f"reinforce_count: 1\ndecay_score: 1.0\nstatus: {status}\n"
                    f"---\nbody {id}\n")

    def test_absorbed_defunct_archived_all_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            self._w(os.path.join(d, "live.md"), "live", "active")
            self._w(os.path.join(d, "arch.md"), "arch", "archived")
            self._w(os.path.join(d, "def.md"), "def", "defunct")
            self._w(os.path.join(d, "abs.md"), "abs", "absorbed")
            ids = {m["id"] for m in rm.load_memories(d)}
            self.assertEqual(ids, {"live"})


class TestApplyMergesSources(unittest.TestCase):
    def test_merge_produces_valid_balanced_sources(self):
        # Regression: the source merge must not corrupt the frontmatter (an earlier
        # regex produced unbalanced `[["[...]]`).
        with tempfile.TemporaryDirectory() as d:
            import os
            cp = os.path.join(d, "c.md")
            ap = os.path.join(d, "a.md")
            _write(cp, "c", '["[A#1]"]')
            _write(ap, "a", '["[B#2]"]')
            with open(cp) as f:
                canon = {"id": "c", "path": cp, "text": f.read()}
            with open(ap) as f:
                absorbed = {"id": "a", "path": ap, "text": f.read()}
            rm.apply_reinforcement(canon, absorbed, "2026-06-30")
            self.assertFalse(os.path.exists(ap))           # duplicate removed
            with open(cp) as f:
                t = f.read()
            sline = [l for l in t.splitlines() if l.startswith("sources:")][0]
            v = sline.split(":", 1)[1].strip()
            self.assertEqual(v.count("["), v.count("]"))   # balanced — no corruption
            self.assertIn('"[A#1]"', t)
            self.assertIn('"[B#2]"', t)                    # both sources merged in
            self.assertIn("reinforce_count: 2", t)
            self.assertIn("last_reinforced: 2026-06-30", t)

    def test_same_session_absorption_does_not_bump_rc(self):
        # Phase 5 Item 1 (N1): if the absorbed memory's sources are all from
        # sessions the canonical already has, the merge must NOT inflate rc
        # (rc bumps at most once per distinct session id). Sources still merge
        # and the absorbed duplicate is still removed.
        with tempfile.TemporaryDirectory() as d:
            import os
            cp = os.path.join(d, "c.md")
            ap = os.path.join(d, "a.md")
            _write(cp, "c", '["[A#1]"]')
            _write(ap, "a", '["[A#7]"]')     # same session A, different line
            with open(cp) as f:
                canon = {"id": "c", "path": cp, "text": f.read()}
            with open(ap) as f:
                absorbed = {"id": "a", "path": ap, "text": f.read()}
            rm.apply_reinforcement(canon, absorbed, "2026-06-30")
            self.assertFalse(os.path.exists(ap))           # still consolidated
            with open(cp) as f:
                t = f.read()
            self.assertIn('"[A#1]"', t)
            self.assertIn('"[A#7]"', t)                    # sources merged
            self.assertIn("reinforce_count: 1", t)         # rc NOT double-counted
            self.assertIn("last_reinforced: 2026-06-30", t)

    def test_session_ids_helper(self):
        self.assertEqual(rm._session_ids(['"[A#1]"', '"[A#9]"', '"[B#2]"']),
                         {"A", "B"})
        # non-bracket tokens count as one distinct id each (whole token)
        self.assertEqual(rm._session_ids(['"charter:merge-gate"']),
                         {"charter:merge-gate"})


if __name__ == "__main__":
    unittest.main()
