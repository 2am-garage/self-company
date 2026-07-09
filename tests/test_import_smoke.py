"""
Import-smoke regression for Phase 14 Bucket 2 (hard sibling imports).

Bucket 2 converted the `try: from <sibling> import X except: <verbatim copy>`
fallbacks in the pipeline scripts into HARD imports, relying on each file's own
`sys.path.insert(0, <own dir>)` to resolve the shared siblings (frontmatter,
tombstone, charter_ids, policy_config) under EVERY entry point.

The existing functional tests run each script via `_helpers.run_script`, a
subprocess whose `sys.path[0]` is the script's own dir — so they only exercise the
DIRECT-RUN entry point. This test locks the OTHER entry point: importing each
converted module as a library from a process whose sys.path does NOT already
contain the scripts dir. If a file's self-insert regressed, the hard import would
raise ImportError and this fails.
"""

import os
import subprocess
import sys
import unittest

import _helpers

CONVERTED = [
    "decay", "entropy", "verify_memory", "capture-trigger", "reinforce_memory",
    "rag_index", "list_uncategorized", "hook_memory_lint",
    "hook_memory_inject",
    # Phase 22: the shared rag_venv helper + its hard-import consumers.
    "rag_venv", "rag_query",
]


class TestSiblingImportSelfResolves(unittest.TestCase):
    def test_each_converted_module_imports_without_scripts_on_path(self):
        scr = _helpers.SCRIPTS_DIR
        # Child loader: scrub the scripts dir from sys.path, then import each file
        # by path — success proves the file's own sys.path.insert self-resolves the
        # hard sibling imports. SC_RAG_REEXEC=1 stops entropy/reinforce re-exec.
        loader = (
            "import importlib.util, os, sys\n"
            f"scr = {scr!r}\n"
            "sys.path = [p for p in sys.path if os.path.abspath(p) != os.path.abspath(scr)]\n"
            f"mods = {CONVERTED!r}\n"
            "bad = []\n"
            "for name in mods:\n"
            "    spec = importlib.util.spec_from_file_location(\n"
            "        name.replace('-', '_'), os.path.join(scr, name + '.py'))\n"
            "    mod = importlib.util.module_from_spec(spec)\n"
            "    try:\n"
            "        spec.loader.exec_module(mod)\n"
            "    except Exception as e:\n"
            "        bad.append(f'{name}: {type(e).__name__}: {e}')\n"
            "if bad:\n"
            "    print('\\n'.join(bad)); sys.exit(1)\n"
            "print('ok')\n"
        )
        env = {**os.environ, "SC_RAG_REEXEC": "1"}
        # run from a foreign cwd so no incidental './scripts' resolution helps
        proc = subprocess.run([sys.executable, "-c", loader],
                              capture_output=True, text=True, cwd="/tmp", env=env)
        self.assertEqual(proc.returncode, 0,
                         f"import failures:\n{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
