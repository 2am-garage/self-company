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
        r, skip, _cands = rm.plan_reinforcements(mems, [("a", "b", 0.95)], 0.92)
        self.assertEqual(r, [{"canonical": "a", "absorbed": "b",
                              "canonical_tier": "L0", "score": 0.95}])
        self.assertEqual(skip, [])

    def test_l0_into_l1_canonical_is_l1(self):
        mems = {"a": M("a", "L0"), "c": M("c", "L1")}
        r, _, _cands = rm.plan_reinforcements(mems, [("a", "c", 0.97)], 0.92)
        self.assertEqual(r[0]["canonical"], "c")
        self.assertEqual(r[0]["absorbed"], "a")

    def test_l2_is_never_touched(self):
        mems = {"a": M("a", "L0"), "e": M("e", "L2")}
        r, skip, _cands = rm.plan_reinforcements(mems, [("a", "e", 0.99)], 0.92)
        self.assertEqual(r, [])
        self.assertEqual(skip[0]["pair"], ["a", "e"])

    def test_below_threshold_nothing(self):
        mems = {"a": M("a", "L0"), "b": M("b", "L0")}
        r, skip, _cands = rm.plan_reinforcements(mems, [("a", "b", 0.80)], 0.92)
        self.assertEqual((r, skip), ([], []))

    def test_l1_l1_not_auto_merged(self):
        mems = {"a": M("a", "L1"), "b": M("b", "L1")}
        r, _, _cands = rm.plan_reinforcements(mems, [("a", "b", 0.99)], 0.92)
        self.assertEqual(r, [])  # warm memories aren't auto-merged

    def test_each_memory_used_once(self):
        mems = {"a": M("a", "L0", "2026-06-01"),
                "b": M("b", "L0", "2026-06-02"),
                "c": M("c", "L0", "2026-06-03")}
        r, _, _cands = rm.plan_reinforcements(
            mems, [("a", "b", 0.95), ("a", "c", 0.94), ("b", "c", 0.93)], 0.92)
        involved = [x for rr in r for x in (rr["canonical"], rr["absorbed"])]
        self.assertEqual(len(involved), len(set(involved)))  # no id reused

    def test_same_source_near_duplicate_surfaces_as_advisory_candidate(self):
        # Two L0s share a source but their embedding score falls short of the
        # auto-merge threshold (paraphrased, not identical) — cosine alone
        # would silently drop them. The sources-overlap pre-filter still
        # surfaces the pair as an advisory candidate.
        mems = {"a": M("a", "L0", "2026-06-01"), "b": M("b", "L0", "2026-06-02")}
        memories_list = [{"id": "a", "sources": ["[#123]"]},
                          {"id": "b", "sources": ["[#123]"]}]
        r, _skip, cands = rm.plan_reinforcements(
            mems, [("a", "b", 0.80)], 0.92, memories_list=memories_list)
        self.assertEqual(r, [])   # not auto-merged by cosine
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["members"], ["a", "b"])
        self.assertEqual(cands[0]["shared_sources"], ["[#123]"])
        self.assertEqual(cands[0]["match_type"], "exact")

    def test_same_source_distinct_fact_not_auto_merged(self):
        # Same source, but embeddings never even paired them (distinct facts
        # recorded from the same session) — must never appear in
        # `reinforcements`, only (still) as an advisory candidate.
        mems = {"a": M("a", "L0", "2026-06-01"), "b": M("b", "L0", "2026-06-02")}
        memories_list = [{"id": "a", "sources": ["[#999]"]},
                          {"id": "b", "sources": ["[#999]"]}]
        r, skip, cands = rm.plan_reinforcements(
            mems, [], 0.92, memories_list=memories_list)
        self.assertEqual(r, [])      # never auto-merged
        self.assertEqual(skip, [])
        self.assertEqual(len(cands), 1)   # still advisory-surfaced
        self.assertEqual(cands[0]["match_type"], "exact")


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
            # Item 2 (BOB-F2): the absorbed duplicate is TOMBSTONED, not deleted.
            self.assertTrue(os.path.exists(ap))            # still on disk (recoverable)
            with open(ap) as f:
                at = f.read()
            self.assertIn("status: absorbed", at)
            self.assertIn("invalid_at: 2026-06-30", at)
            self.assertIn("body a", at)                    # unique body preserved
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
            self.assertTrue(os.path.exists(ap))            # tombstoned, not deleted
            with open(ap) as f:
                self.assertIn("status: absorbed", f.read())
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


class TestC1InsertMissingKeys(unittest.TestCase):
    """C1 (BOB-F3): the old in-place-only rewrite silently dropped an update when
    the canonical lacked the key. A canonical missing `reinforce_count` must still
    get it written with the incremented value — otherwise the rc bump vanishes and
    the memory never reaches the rc>=2 promotion gate."""

    def test_missing_reinforce_count_is_inserted_with_bump(self):
        with tempfile.TemporaryDirectory() as d:
            cp = os.path.join(d, "c.md")
            ap = os.path.join(d, "a.md")
            # canonical has NO reinforce_count line at all
            with open(cp, "w") as f:
                f.write("---\nid: c\ntier: L0\nowner: Tony\nsources: [\"[A#1]\"]\n"
                        "created: 2026-06-01\nlast_reinforced: 2026-06-01\n"
                        "decay_score: 1.0\nstatus: active\n---\nbody c\n")
            _write(ap, "a", '["[B#2]"]')   # distinct session -> rc should bump
            with open(cp) as f:
                canon = {"id": "c", "path": cp, "text": f.read()}
            with open(ap) as f:
                absorbed = {"id": "a", "path": ap, "text": f.read()}
            rm.apply_reinforcement(canon, absorbed, "2026-06-30")
            with open(cp) as f:
                t = f.read()
            self.assertIn("reinforce_count: 2", t)   # inserted, not dropped
            self.assertIn("last_reinforced: 2026-06-30", t)
            # frontmatter stays well-formed: exactly two fences, key inside block
            self.assertEqual(t.count("\n---\n") + (1 if t.startswith("---\n") else 0), 2)
            fm, close = rm.parse_frontmatter(t)
            self.assertEqual(fm.get("reinforce_count"), "2")

    def test_missing_sources_and_last_reinforced_inserted(self):
        with tempfile.TemporaryDirectory() as d:
            cp = os.path.join(d, "c.md")
            ap = os.path.join(d, "a.md")
            # canonical missing sources AND last_reinforced AND reinforce_count
            with open(cp, "w") as f:
                f.write("---\nid: c\ntier: L0\nowner: Tony\n"
                        "created: 2026-06-01\ndecay_score: 1.0\nstatus: active\n"
                        "---\nbody c\n")
            _write(ap, "a", '["[B#2]"]')
            with open(cp) as f:
                canon = {"id": "c", "path": cp, "text": f.read()}
            with open(ap) as f:
                absorbed = {"id": "a", "path": ap, "text": f.read()}
            rm.apply_reinforcement(canon, absorbed, "2026-06-30")
            with open(cp) as f:
                t = f.read()
            self.assertIn("reinforce_count: 2", t)
            self.assertIn("last_reinforced: 2026-06-30", t)
            self.assertIn('"[B#2]"', t)              # absorbed source carried in
            fm, _ = rm.parse_frontmatter(t)
            self.assertEqual(fm.get("id"), "c")      # still parses cleanly


class TestItem2AbsorbTombstones(unittest.TestCase):
    """Item 2 (BOB-F2): absorb TOMBSTONES the L0 (status: absorbed + invalid_at)
    instead of hard-deleting it — activating the documented recoverable-tombstone
    design so a false-positive dedup is recoverable within decay's grace window."""

    def test_absorbed_is_tombstoned_recoverable_and_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            cp = os.path.join(d, "c.md")
            ap = os.path.join(d, "a.md")
            _write(cp, "c", '["[A#1]"]')
            _write(ap, "a", '["[B#2]"]')
            with open(cp) as f:
                canon = {"id": "c", "path": cp, "text": f.read()}
            with open(ap) as f:
                absorbed = {"id": "a", "path": ap, "text": f.read()}
            rm.apply_reinforcement(canon, absorbed, "2026-06-30")
            # file survives with tombstone markers + unique body (recoverable)
            self.assertTrue(os.path.exists(ap))
            with open(ap) as f:
                at = f.read()
            self.assertIn("status: absorbed", at)
            self.assertIn("invalid_at: 2026-06-30", at)
            self.assertIn("body a", at)
            fm, _ = rm.parse_frontmatter(at)
            self.assertTrue(rm.is_tombstoned(fm))    # out of the active set
            # excluded from active scans (recall/reinforce): only canonical loads
            ids = {m["id"] for m in rm.load_memories(d)}
            self.assertEqual(ids, {"c"})

    def test_l0_guard_refuses_to_tombstone_l1(self):
        # Defense-in-depth: even if a caller passed an L1/L2 as `absorbed`
        # (plan_reinforcements never does), _tombstone_absorbed must refuse —
        # never retire a warm/cold memory that decay's reap would then delete.
        with tempfile.TemporaryDirectory() as d:
            ap = os.path.join(d, "warm.md")
            with open(ap, "w") as f:
                f.write("---\nid: w\ntier: L1\nowner: Tony\nsources: [\"[A#1]\"]\n"
                        "created: 2026-06-01\nlast_reinforced: 2026-06-01\n"
                        "reinforce_count: 2\ndecay_score: 1.0\nstatus: active\n"
                        "---\nbody w\n")
            with open(ap) as f:
                absorbed = {"id": "w", "tier": "L1", "path": ap, "text": f.read()}
            rm._tombstone_absorbed(absorbed, "2026-06-30")
            with open(ap) as f:
                at = f.read()
            self.assertIn("status: active", at)       # untouched
            self.assertNotIn("absorbed", at)

    def test_existing_status_line_is_overwritten_not_duplicated(self):
        # If the absorbed file already had `status: active`, that line is
        # rewritten in place (not duplicated) and invalid_at inserted.
        with tempfile.TemporaryDirectory() as d:
            cp = os.path.join(d, "c.md")
            ap = os.path.join(d, "a.md")
            _write(cp, "c", '["[A#1]"]')
            _write(ap, "a", '["[B#2]"]')
            with open(cp) as f:
                canon = {"id": "c", "path": cp, "text": f.read()}
            with open(ap) as f:
                absorbed = {"id": "a", "path": ap, "text": f.read()}
            rm.apply_reinforcement(canon, absorbed, "2026-06-30")
            with open(ap) as f:
                at = f.read()
            self.assertEqual(at.count("status:"), 1)      # not duplicated
            self.assertNotIn("status: active", at)
            self.assertEqual(at.count("invalid_at:"), 1)


if __name__ == "__main__":
    unittest.main()
