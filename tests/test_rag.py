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


if __name__ == "__main__":
    unittest.main()
