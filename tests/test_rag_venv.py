"""
Phase 22 Item 1 — the shared `rag_venv.py` helper.

Before Phase 22 four scanners (rag_query, rag_index, reinforce_memory, entropy)
each carried a near-identical `_reexec_into_rag_venv()` copy. They were collapsed
onto one helper. These tests lock the BEHAVIOR-PRESERVING contract:

  * `reexec_if_needed` short-circuits on SC_RAG_REEXEC and when the probe modules
    already import;
  * every one of the four scripts still re-execs into the project's
    `<cwd>/.company/.rag-venv/bin/python` when the RAG backend is absent;
  * `entropy` stays import-safe (importing it NEVER re-execs);
  * `venv_python` resolves the interpreter path.

Each re-exec test plants a FAKE `.company/.rag-venv/bin/python` that just echoes a
marker and exits — so a successful re-exec is observable as the marker in stdout,
without needing a real fastembed/lancedb venv.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

import _helpers

SCRIPTS = _helpers.SCRIPTS_DIR

# The re-exec only fires when the BASE interpreter genuinely lacks the backend and
# no skill-local venv shadows the project fallback. Guard both preconditions so the
# parity assertions are meaningful (and skip cleanly on a dev box that has them).
_SKILL_VENV = os.path.join(os.path.dirname(SCRIPTS), ".rag-venv", "bin", "python")


def _base_has(mod):
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def _plant_fake_venv(root, marker):
    """<root>/.company/{.rag-venv/bin/python (echoes marker), memory/}."""
    bindir = os.path.join(root, ".company", ".rag-venv", "bin")
    os.makedirs(bindir)
    py = os.path.join(bindir, "python")
    with open(py, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\necho EXEC:%s\nexit 0\n" % marker)
    os.chmod(py, 0o755)
    os.makedirs(os.path.join(root, ".company", "memory"))
    return root


class TestVenvPython(unittest.TestCase):
    def test_venv_python_path(self):
        import rag_venv
        from pathlib import Path
        got = rag_venv.venv_python("/x/y/.company")
        self.assertEqual(Path(got), Path("/x/y/.company/.rag-venv/bin/python"))


class TestReexecShortCircuits(unittest.TestCase):
    """Pure in-process no-op paths — no venv, no execv, must simply return."""

    def setUp(self):
        import rag_venv
        self.rag_venv = rag_venv
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_sc_rag_reexec_set_is_noop(self):
        os.environ["SC_RAG_REEXEC"] = "1"
        # Even with a bogus probe module and no venv, it must just return.
        self.rag_venv.reexec_if_needed(["definitely_missing_xyz"])

    def test_all_probes_importable_is_noop(self):
        os.environ.pop("SC_RAG_REEXEC", None)
        self.rag_venv.reexec_if_needed(["os", "sys", "json"])

    def test_no_candidate_venv_degrades(self):
        os.environ.pop("SC_RAG_REEXEC", None)
        # From a cwd with no .company, a missing probe module must NOT raise —
        # the no-venv degrade path just returns to the caller.
        with tempfile.TemporaryDirectory() as d:
            cwd0 = os.getcwd()
            try:
                os.chdir(d)
                self.rag_venv.reexec_if_needed(["definitely_missing_xyz"])
            finally:
                os.chdir(cwd0)


@unittest.skipIf(os.path.exists(_SKILL_VENV),
                 "skill-local .rag-venv present; project-fallback re-exec unreachable")
class TestReexecParityAcrossScripts(unittest.TestCase):
    """Every scanner re-execs into <cwd>/.company/.rag-venv when its backend is
    absent — the single behavior the four legacy copies each implemented."""

    # (script, args, probe modules the script needs)
    CASES = [
        ("rag_query.py", ["--query", "x"], ["lancedb", "fastembed"]),
        ("rag_index.py", [], ["lancedb", "fastembed"]),
        ("reinforce_memory.py", [], ["fastembed", "numpy"]),
        ("entropy.py", [], ["fastembed", "numpy"]),
    ]

    def _run(self, script, args, cwd):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SC_RAG_REEXEC", "SC_NO_RAG")}
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args],
                              capture_output=True, text=True, cwd=cwd, env=env)

    def test_each_script_reexecs_into_cwd_company_venv(self):
        for script, args, probes in self.CASES:
            if all(_base_has(m) for m in probes):
                continue  # base has the backend -> no re-exec expected/needed
            with self.subTest(script=script), tempfile.TemporaryDirectory() as d:
                _plant_fake_venv(d, "CWD-" + script)
                p = self._run(script, args, d)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("EXEC:CWD-" + script, p.stdout)

    def test_sc_rag_reexec_child_does_not_loop(self):
        # With SC_RAG_REEXEC=1 the script must NOT re-exec (the fake would echo the
        # marker); it runs in-process and degrades on the absent backend instead.
        for script, args, probes in self.CASES:
            if all(_base_has(m) for m in probes):
                continue
            with self.subTest(script=script), tempfile.TemporaryDirectory() as d:
                _plant_fake_venv(d, "MUST-NOT-RUN-" + script)
                env = {**os.environ, "SC_RAG_REEXEC": "1"}
                env.pop("SC_NO_RAG", None)
                p = subprocess.run(
                    [sys.executable, os.path.join(SCRIPTS, script), *args],
                    capture_output=True, text=True, cwd=d, env=env)
                self.assertNotIn("MUST-NOT-RUN", p.stdout)


class TestEntropyImportSafety(unittest.TestCase):
    """entropy MUST be importable from a base-python process without re-execing —
    importing it must never os.execv and replace the caller's program."""

    def test_importing_entropy_never_reexecs(self):
        # A child that imports entropy and then prints a sentinel. If entropy
        # re-execs at import (into the fake venv), the sentinel never prints.
        with tempfile.TemporaryDirectory() as d:
            _plant_fake_venv(d, "REEXEC-BUG")
            code = (
                "import sys; sys.path.insert(0, %r)\n" % SCRIPTS +
                "import entropy\n"
                "print('IMPORTED-NO-REEXEC')\n"
            )
            env = {k: v for k, v in os.environ.items()
                   if k not in ("SC_RAG_REEXEC", "SC_NO_RAG")}
            p = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, cwd=d, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("IMPORTED-NO-REEXEC", p.stdout)
            self.assertNotIn("REEXEC-BUG", p.stdout)


if __name__ == "__main__":
    unittest.main()
