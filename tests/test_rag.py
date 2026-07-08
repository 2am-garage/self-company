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


if __name__ == "__main__":
    unittest.main()
