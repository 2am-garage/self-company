"""
Tests for the RAG scripts' graceful degradation (policy.md §8.1 / references/rag.md §8).

The stack ships dormant: without Ollama + LanceDB, the scripts must exit with a
clear code (not crash). --threshold-check must work offline with no deps.
"""

import os
import tempfile
import unittest

import _helpers


# SC_RAG_REEXEC=1 disables the venv re-exec shim so these tests exercise the
# real degradation path under the (deps-free) system interpreter even when a
# .company/.rag-venv happens to exist in the working tree.
NO_REEXEC = {"SC_RAG_REEXEC": "1"}


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


if __name__ == "__main__":
    unittest.main()
