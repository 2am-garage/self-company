"""
Tests for the RAG scripts' graceful degradation (policy.md §8.1 / references/rag.md §8).

The stack ships dormant: without Ollama + LanceDB, the scripts must exit with a
clear code (not crash). --threshold-check must work offline with no deps.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

import _helpers


# SC_RAG_REEXEC=1 disables the venv re-exec shim so these tests exercise the
# real degradation path under the (deps-free) system interpreter even when a
# .company/.rag-venv happens to exist in the working tree.
NO_REEXEC = {"SC_RAG_REEXEC": "1"}


def _find_rag_venv_python():
    """Locate a real .company/.rag-venv python by walking up from the repo root
    (the worktree nests under the project that owns .company). Returns the path
    or None — the promotion round-trip integration test skips when absent, so no
    NEW hard RAG dependency is introduced (Phase 13 guardrail)."""
    p = os.path.abspath(_helpers.REPO_ROOT)
    seen = set()
    while p and p not in seen:
        seen.add(p)
        cand = os.path.join(p, ".company", ".rag-venv", "bin", "python")
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


class TestRagDegradation(unittest.TestCase):
    def test_threshold_check_below_threshold_exits_1(self):
        # Empty/small memory -> below RAG_ENABLE_THRESHOLD -> exit 1, no deps needed.
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "L1-warm"))
            os.makedirs(os.path.join(d, "L2-cold"))
            rc, out, err = _helpers.run_script(
                "rag_index.py", "--threshold-check", "--memory-dir", d, env=NO_REEXEC)
            self.assertEqual(rc, 1, f"expected below-threshold exit 1; out={out} err={err}")

    def test_index_without_deps_exits_2(self):
        # Building the index without the RAG backend must exit 2 (actionable),
        # never raise an uncaught traceback.
        with tempfile.TemporaryDirectory() as d:
            rc, out, err = _helpers.run_script("rag_index.py", "--memory-dir", d, env=NO_REEXEC)
            self.assertEqual(rc, 2, f"expected degradation exit 2; out={out} err={err}")
            self.assertNotIn("Traceback", err)

    def test_query_without_deps_exits_2(self):
        rc, out, err = _helpers.run_script("rag_query.py", "--query", "anything", env=NO_REEXEC)
        self.assertEqual(rc, 2, f"expected degradation exit 2; out={out} err={err}")
        self.assertNotIn("Traceback", err)


class TestIncrementalHash(unittest.TestCase):
    """Phase 13 A.1: the incremental-refresh idempotence guarantee rests on
    compute_content_hash — an unchanged body must hash identically across runs so
    rag_index skips it (no re-embed). Deps-free: content hashing is pure stdlib,
    so this runs under the base interpreter with SC_RAG_REEXEC=1 (no venv needed)."""

    def test_content_hash_stable_normalized_and_distinct(self):
        prog = (
            "import sys; sys.path.insert(0, sys.argv[1]); import rag_index as R\n"
            "a = R.compute_content_hash('hello world')\n"
            "b = R.compute_content_hash('  hello   world  ')\n"   # whitespace-normalized -> same
            "c = R.compute_content_hash('hello world')\n"          # recomputed -> same
            "d = R.compute_content_hash('a different body entirely')\n"
            "assert a == c, 'hash not stable across calls'\n"
            "assert a == b, 'normalization changed the hash'\n"
            "assert a != d, 'distinct bodies collided'\n"
            "print('OK')\n"
        )
        import subprocess
        import sys
        proc = subprocess.run(
            [sys.executable, "-c", prog, _helpers.SCRIPTS_DIR],
            capture_output=True, text=True,
            env={**os.environ, "SC_RAG_REEXEC": "1"})  # no re-exec into any real venv
        self.assertEqual(proc.returncode, 0, f"out={proc.stdout} err={proc.stderr}")
        self.assertIn("OK", proc.stdout)


class TestIncrementalUpToDate(unittest.TestCase):
    """BOB-F1 (Item 1), deps-free: the incremental skip predicate must invalidate
    on path OR tier change, not only body hash — so an L1->L2-promoted memory
    (same body, moved file, flipped tier) is re-embedded and its index row tracks
    the live L2 path/tier instead of the dead L1 path. Pure stdlib; runs under the
    base interpreter with SC_RAG_REEXEC=1 (no venv)."""

    def _run(self, prog):
        proc = subprocess.run(
            [sys.executable, "-c", prog, _helpers.SCRIPTS_DIR],
            capture_output=True, text=True,
            env={**os.environ, "SC_RAG_REEXEC": "1"})
        self.assertEqual(proc.returncode, 0, f"out={proc.stdout} err={proc.stderr}")
        self.assertIn("OK", proc.stdout)

    def test_path_or_tier_change_invalidates(self):
        prog = (
            "import sys; sys.path.insert(0, sys.argv[1]); import rag_index as R\n"
            "h = R.compute_content_hash('same body')\n"
            "prev = {'content_hash': h, 'path': '/m/L1-warm/x.md', 'tier': 'L1'}\n"
            # unchanged: same hash+path+tier -> up to date (skip)
            "assert R.incremental_up_to_date(prev, h, '/m/L1-warm/x.md', 'L1')\n"
            # promotion move: body identical, path+tier changed -> NOT up to date
            "assert not R.incremental_up_to_date(prev, h, '/m/L2-cold/preferences/x.md', 'L2')\n"
            # path change alone -> NOT up to date\n"
            "assert not R.incremental_up_to_date(prev, h, '/m/L2-cold/preferences/x.md', 'L1')\n"
            # tier change alone -> NOT up to date\n"
            "assert not R.incremental_up_to_date(prev, h, '/m/L1-warm/x.md', 'L2')\n"
            # body change -> NOT up to date (original behaviour preserved)\n"
            "assert not R.incremental_up_to_date(prev, 'otherhash', '/m/L1-warm/x.md', 'L1')\n"
            "print('OK')\n"
        )
        self._run(prog)


@unittest.skipUnless(_find_rag_venv_python(),
                     "RAG venv not available (integration test)")
class TestPromotionRecallRoundTrip(unittest.TestCase):
    """BOB-F1 (Item 1) end-to-end on a SCRATCH corpus (never live .company/memory):
    build the index over an L1 memory, simulate decay's L1->L2 promotion (MOVE the
    file, flip tier, same id + body), run an incremental refresh, and confirm the
    index row now points at the live L2 path with tier L2 — so the consumer's
    path-revalidation retrieves it instead of dropping it. Also confirms a truly
    unchanged second refresh still skips (idempotent, no re-embed churn)."""

    SCRIPT = os.path.join(_helpers.SCRIPTS_DIR, "rag_index.py")

    def setUp(self):
        self.venv = _find_rag_venv_python()

    def _index(self, memdir, indexdir):
        proc = subprocess.run(
            [self.venv, self.SCRIPT, "--memory-dir", memdir,
             "--index-dir", indexdir],
            capture_output=True, text=True,
            env={**os.environ, "SC_RAG_REEXEC": "1"})
        # Exit 2 is the documented RAG-backend-unavailable DEGRADE path (LanceDB/
        # fastembed not importable, or a transient model-load failure under
        # resource pressure). That is orthogonal to the Item-1 logic under test —
        # a genuine logic regression surfaces as exit 0 with a wrong row/count,
        # never exit 2 — so treat it as a skip, not a failure, to keep this
        # integration test from flaking when the backend can't spin up.
        if proc.returncode == 2:
            first_line = ((proc.stderr or "").strip().splitlines() or [""])[0]
            self.skipTest("RAG backend unavailable at runtime (degrade path): "
                          + first_line)
        self.assertEqual(proc.returncode, 0,
                         f"index failed: out={proc.stdout} err={proc.stderr}")
        return json.loads(proc.stdout)

    def _rows_for(self, indexdir, mid):
        prog = (
            "import sys, json, lancedb\n"
            "db = lancedb.connect(sys.argv[1])\n"
            "t = db.open_table('memory')\n"
            "rows = [r for r in t.search().to_list() if r['id'] == sys.argv[2]]\n"
            "print(json.dumps([{'id': r['id'], 'tier': r['tier'], "
            "'path': r['path']} for r in rows]))\n")
        proc = subprocess.run(
            [self.venv, "-c", prog, indexdir, mid],
            capture_output=True, text=True,
            env={**os.environ, "SC_RAG_REEXEC": "1"})
        self.assertEqual(proc.returncode, 0,
                         f"read failed: out={proc.stdout} err={proc.stderr}")
        return json.loads(proc.stdout)

    def test_promoted_l2_row_tracks_live_path_and_tier(self):
        with tempfile.TemporaryDirectory() as d:
            memdir = os.path.join(d, "memory")
            indexdir = os.path.join(memdir, "index")
            l1_path = os.path.join(memdir, "L1-warm", "m1.md")
            body = "Chairman prefers concise weekly status digests over daily noise."
            _helpers.write_memory(l1_path, id="m1", tier="L1",
                                  reinforce_count=3, body=body)

            # First index build: row points at the L1 path, tier L1.
            self._index(memdir, indexdir)
            rows = self._rows_for(indexdir, "m1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["tier"], "L1")
            self.assertEqual(os.path.realpath(rows[0]["path"]),
                             os.path.realpath(l1_path))

            # Simulate decay's L1->L2 promotion: MOVE the file into
            # L2-cold/preferences/, flip tier to L2, SAME id + body (the exact
            # BOB-F1 trigger — content_hash unchanged).
            l2_path = os.path.join(memdir, "L2-cold", "preferences", "m1.md")
            _helpers.write_memory(l2_path, id="m1", tier="L2",
                                  reinforce_count=4, body=body)
            os.remove(l1_path)

            # Incremental refresh: the moved memory must be re-embedded (NOT
            # skipped) so its row tracks the live L2 path/tier.
            rep = self._index(memdir, indexdir)
            self.assertEqual(rep["embedded"], 1,
                             "promoted memory must be re-embedded, not skipped")
            rows = self._rows_for(indexdir, "m1")
            self.assertEqual(len(rows), 1, "exactly one row for the id (no dupe)")
            self.assertEqual(rows[0]["tier"], "L2")
            self.assertEqual(os.path.realpath(rows[0]["path"]),
                             os.path.realpath(l2_path))
            # The stored path is a LIVE file — this is precisely what the
            # consumers (hook_memory_inject / employee.recall_shared) re-validate
            # before retrieving; the old dead L1 path would have been dropped.
            self.assertTrue(os.path.exists(rows[0]["path"]))

            # Idempotent: a third refresh with nothing changed skips (no churn).
            rep2 = self._index(memdir, indexdir)
            self.assertEqual(rep2["embedded"], 0)
            self.assertGreaterEqual(rep2["skipped_unchanged"], 1)


class TestModelStamp(unittest.TestCase):
    """Phase 24 Item 1, deps-free: rag_stamp.py is pure stdlib (no lancedb/
    fastembed needed) — read/write/match must all degrade cleanly and never
    raise, since a corrupt or absent stamp is the routine (not exceptional)
    case for a legacy pre-Phase-24 index."""

    def _mod(self):
        import importlib
        sys.path.insert(0, _helpers.SCRIPTS_DIR)
        import rag_stamp
        importlib.reload(rag_stamp)
        return rag_stamp

    def test_write_then_read_round_trips(self):
        rag_stamp = self._mod()
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(rag_stamp.write_stamp(d, "model-a", 384))
            stamp = rag_stamp.read_stamp(d)
            self.assertEqual(stamp, {"model": "model-a", "dim": 384})
            self.assertTrue(rag_stamp.stamp_matches(stamp, "model-a", 384))
            self.assertFalse(rag_stamp.stamp_matches(stamp, "model-b", 384))
            self.assertFalse(rag_stamp.stamp_matches(stamp, "model-a", 768))

    def test_absent_stamp_reads_none_and_never_matches(self):
        rag_stamp = self._mod()
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(rag_stamp.read_stamp(d))
            self.assertFalse(rag_stamp.stamp_matches(None, "model-a", 384))

    def test_lib_version_is_part_of_the_stamp(self):
        # Phase 24 MUST-FIX 4: a fastembed-version change (same model+dim) must
        # count as a mismatch so the index self-heals.
        rag_stamp = self._mod()
        with tempfile.TemporaryDirectory() as d:
            rag_stamp.write_stamp(d, "model-a", 384, "0.8.0")
            stamp = rag_stamp.read_stamp(d)
            self.assertEqual(stamp.get("lib"), "0.8.0")
            self.assertTrue(rag_stamp.stamp_matches(stamp, "model-a", 384, "0.8.0"))
            # different lib -> mismatch (triggers rebuild)
            self.assertFalse(rag_stamp.stamp_matches(stamp, "model-a", 384, "0.9.0"))
            # a legacy stamp WITHOUT a lib key mismatches once we start passing lib
            legacy = {"model": "model-a", "dim": 384}
            self.assertFalse(rag_stamp.stamp_matches(legacy, "model-a", 384, "0.8.0"))
            # lib not supplied (deps-free caller) -> lib dimension not checked
            self.assertTrue(rag_stamp.stamp_matches(legacy, "model-a", 384))

    def test_malformed_stamp_file_degrades_to_none(self):
        rag_stamp = self._mod()
        with tempfile.TemporaryDirectory() as d:
            with open(rag_stamp.stamp_path(d), "w") as f:
                f.write("not json at all {{{")
            self.assertIsNone(rag_stamp.read_stamp(d))

    def test_write_stamp_never_raises_on_unwritable_dir(self):
        rag_stamp = self._mod()
        with tempfile.TemporaryDirectory() as d:
            blocked = os.path.join(d, "blocked")
            with open(blocked, "w") as f:
                f.write("a file, not a directory")
            # index_dir points AT a file -> mkdir(parents=True) must raise
            # internally but write_stamp must swallow it and return False.
            self.assertFalse(rag_stamp.write_stamp(
                os.path.join(blocked, "sub"), "model-a", 384))


@unittest.skipUnless(_find_rag_venv_python(),
                     "RAG venv not available (integration test)")
class TestStampMismatchSelfHeal(unittest.TestCase):
    """Phase 24 Item 1 — Gibby's attack list: (a) a stale/missing-stamp index
    can NEVER be scored by rag_query.py (treated as absent, not silently
    cross-space-cosine'd), and (b) rag_index.py self-heals it automatically —
    even on a plain INCREMENTAL invocation, no --rebuild flag needed — by
    forcing one full rebuild, and that rebuild is idempotent (a second run
    right after does nothing)."""

    SCRIPT = os.path.join(_helpers.SCRIPTS_DIR, "rag_index.py")
    QUERY = os.path.join(_helpers.SCRIPTS_DIR, "rag_query.py")

    def setUp(self):
        self.venv = _find_rag_venv_python()
        sys.path.insert(0, _helpers.SCRIPTS_DIR)
        import rag_stamp
        self.rag_stamp = rag_stamp

    def _index(self, memdir, indexdir, rebuild=False):
        args = [self.venv, self.SCRIPT, "--memory-dir", memdir, "--index-dir", indexdir]
        if rebuild:
            args.append("--rebuild")
        proc = subprocess.run(args, capture_output=True, text=True,
                              env={**os.environ, "SC_RAG_REEXEC": "1"})
        if proc.returncode == 2:
            self.skipTest("RAG backend unavailable at runtime (degrade path): "
                          + ((proc.stderr or "").strip().splitlines() or [""])[0])
        self.assertEqual(proc.returncode, 0,
                         f"index failed: out={proc.stdout} err={proc.stderr}")
        return json.loads(proc.stdout)

    def _query(self, indexdir, query="status digests"):
        return subprocess.run(
            [self.venv, self.QUERY, "--query", query, "--index-dir", indexdir],
            capture_output=True, text=True,
            env={**os.environ, "SC_RAG_REEXEC": "1"})

    def _seed(self, memdir, n=3):
        for i in range(n):
            _helpers.write_memory(
                os.path.join(memdir, "L2-cold", f"m{i}.md"), id=f"m{i}", tier="L2",
                body=f"Chairman prefers concise weekly status digests, item {i}.")

    def test_query_refuses_wrong_model_stamp(self):
        with tempfile.TemporaryDirectory() as d:
            memdir, indexdir = os.path.join(d, "memory"), os.path.join(d, "memory", "index")
            self._seed(memdir)
            self._index(memdir, indexdir, rebuild=True)
            # Corrupt the stamp to a DIFFERENT model (simulates a model swap
            # that hasn't self-healed yet, e.g. a query racing the refresh).
            self.rag_stamp.write_stamp(indexdir, "some-other-model", 384)
            proc = self._query(indexdir)
            self.assertEqual(proc.returncode, 2,
                             f"expected refusal on stamp mismatch: {proc.stdout} {proc.stderr}")
            self.assertIn("stamp", (proc.stderr or "").lower())

    def test_query_refuses_missing_stamp_legacy_index(self):
        with tempfile.TemporaryDirectory() as d:
            memdir, indexdir = os.path.join(d, "memory"), os.path.join(d, "memory", "index")
            self._seed(memdir)
            self._index(memdir, indexdir, rebuild=True)
            # Simulate a legacy pre-Phase-24 index: no stamp file at all.
            os.remove(self.rag_stamp.stamp_path(indexdir))
            proc = self._query(indexdir)
            self.assertEqual(proc.returncode, 2,
                             f"expected refusal on missing stamp: {proc.stdout} {proc.stderr}")

    def test_incremental_refresh_self_heals_on_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            memdir, indexdir = os.path.join(d, "memory"), os.path.join(d, "memory", "index")
            self._seed(memdir, n=3)
            rep0 = self._index(memdir, indexdir, rebuild=True)
            self.assertEqual(rep0["embedded"], 3)

            # Corrupt the stamp (simulates an embedding-model swap landing).
            self.rag_stamp.write_stamp(indexdir, "some-other-model", 384)

            # Plain INCREMENTAL call (no --rebuild) must self-heal: force a
            # full rebuild internally (all 3 re-embedded) and rewrite the
            # correct stamp — the Phase 12b self-heal pattern, no manual step.
            rep1 = self._index(memdir, indexdir, rebuild=False)
            self.assertEqual(rep1["embedded"], 3,
                             "stamp mismatch must force a full re-embed, not an incremental skip")
            self.assertTrue(any("stamp mismatch" in w for w in rep1.get("warnings", [])),
                            f"expected a stamp-mismatch warning; got {rep1.get('warnings')}")

            # The new stamp is correct -> query now succeeds (no longer refused).
            proc = self._query(indexdir)
            self.assertEqual(proc.returncode, 0, f"query should succeed post-heal: {proc.stderr}")

            # Idempotent: an immediate follow-up incremental run re-embeds
            # NOTHING (stamp now matches; unchanged bodies all skip).
            rep2 = self._index(memdir, indexdir, rebuild=False)
            self.assertEqual(rep2["embedded"], 0)
            self.assertEqual(rep2["skipped_unchanged"], 3)

    def test_missing_stamp_legacy_index_also_self_heals(self):
        with tempfile.TemporaryDirectory() as d:
            memdir, indexdir = os.path.join(d, "memory"), os.path.join(d, "memory", "index")
            self._seed(memdir, n=2)
            self._index(memdir, indexdir, rebuild=True)
            os.remove(self.rag_stamp.stamp_path(indexdir))   # simulate legacy index

            rep = self._index(memdir, indexdir, rebuild=False)
            self.assertEqual(rep["embedded"], 2,
                             "a legacy (unstamped) index with real rows must force a full rebuild")
            stamp = self.rag_stamp.read_stamp(indexdir)
            self.assertIsNotNone(stamp, "the self-heal must leave a fresh, correct stamp")

    def test_fresh_empty_index_is_not_a_mismatch(self):
        # A brand-new index (no prior rows) must NOT be treated as a stamp
        # mismatch -- nothing to migrate, just a normal first build.
        with tempfile.TemporaryDirectory() as d:
            memdir, indexdir = os.path.join(d, "memory"), os.path.join(d, "memory", "index")
            self._seed(memdir, n=1)
            rep = self._index(memdir, indexdir, rebuild=False)
            self.assertEqual(rep["mode"], "incremental")
            self.assertEqual(rep["embedded"], 1)


if __name__ == "__main__":
    unittest.main()
