"""
Tests for Phase 18 — per-employee RAG memory (capture -> index -> recall).

Two layers:
  * DEPS-FREE (always run): the capture/degrade guarantees that must hold with NO
    RAG stack — remember() writes a valid, idempotent memory and never raises;
    recall() returns [] gracefully (no venv / empty query); stores are physically
    isolated; daily-run's per-employee refresh emits a clean skip with no venv.
  * VENV-GATED (skipUnless the repo's .company/.rag-venv imports lancedb+fastembed):
    the payoff — recall() finds the semantically-relevant memory in an employee's
    OWN index, and a query for bob never surfaces gibby's memory (isolation).

The load-bearing invariants: FLAT + isolated + never-raises + never-blocks, and
the reused RAG stack is pointed per-employee (no fork).
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest

import _helpers  # noqa: F401  (puts scripts/ on sys.path)

import employee
from employee import Employee

SCRIPTS = _helpers.SCRIPTS_DIR
REPO_VENV_DIR = os.path.join(_helpers.REPO_ROOT, ".company", ".rag-venv")
REPO_VENV_PY = os.path.join(REPO_VENV_DIR, "bin", "python")


def _has_rag_venv():
    """True iff the repo's RAG venv python exists and imports the RAG deps."""
    if not (os.path.exists(REPO_VENV_PY) and os.access(REPO_VENV_PY, os.X_OK)):
        return False
    try:
        proc = subprocess.run(
            [REPO_VENV_PY, "-c", "import lancedb, fastembed"],
            capture_output=True, timeout=60)
        return proc.returncode == 0
    except Exception:
        return False


HAS_VENV = _has_rag_venv()


def _make_company(tmp, employees=("bob", "gibby"), venv=False):
    """Temp .company with empty desks for `employees`. When venv=True, symlink
    .rag-venv/bin/python at the repo's real venv python so recall/index run for real."""
    company = os.path.join(tmp, ".company")
    for name in employees:
        os.makedirs(os.path.join(company, "org", "employees", name), exist_ok=True)
    if venv:
        # Symlink the WHOLE venv dir (not just the python binary) so pyvenv.cfg is
        # reachable from this path — otherwise the interpreter can't find the
        # venv's site-packages and degrades to the deps-less system python.
        os.symlink(REPO_VENV_DIR, os.path.join(company, ".rag-venv"))
    return company


def _index_store(memory_dir):
    """Refresh ONE employee's own index via the reused rag_index.py + real venv."""
    proc = subprocess.run(
        [REPO_VENV_PY, os.path.join(SCRIPTS, "rag_index.py"),
         "--memory-dir", memory_dir, "--index-dir", os.path.join(memory_dir, "index")],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "SC_RAG_REEXEC": "1"})
    return proc


# ============================================================ capture (remember)
class TestRemember(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _make_company(self.tmp)
        self.bob = Employee.load("bob", self.company)

    def _files(self, emp):
        d = emp.memory_dir
        return [p for p in d.glob("*.md")] if d.exists() else []

    def test_writes_valid_memory(self):
        path = self.bob.remember(
            "Cache effective() in the model to dodge a circular import.",
            tags=["build", "validator"], source="task-42")
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        # Lives under bob's OWN store.
        self.assertEqual(os.path.dirname(str(path)), str(self.bob.memory_dir))
        with open(path, encoding="utf-8") as f:
            text = f.read()
        raw, body = __import__("frontmatter").split(text)
        fm = employee._parse_fm(raw)
        self.assertEqual(fm.get("owner"), "bob")
        self.assertEqual(fm.get("tier"), "L2")        # fixed index-compat tier
        self.assertTrue(fm.get("id"))
        self.assertTrue(fm.get("created"))
        self.assertEqual(sorted(fm.get("tags")), ["build", "validator"])
        self.assertEqual(fm.get("source"), "task-42")
        self.assertIn("circular import", body)

    def test_idempotent_same_text(self):
        p1 = self.bob.remember("A durable, reusable lesson.")
        p2 = self.bob.remember("A durable, reusable lesson.")
        self.assertEqual(str(p1), str(p2))
        self.assertEqual(len(self._files(self.bob)), 1)

    def test_idempotent_whitespace_normalized(self):
        p1 = self.bob.remember("hello   world  lesson")
        p2 = self.bob.remember("  hello world lesson ")
        self.assertEqual(str(p1), str(p2))          # normalized -> same id/file
        self.assertEqual(len(self._files(self.bob)), 1)

    def test_empty_text_records_nothing(self):
        self.assertIsNone(self.bob.remember("   \n  "))
        self.assertIsNone(self.bob.remember(""))
        self.assertEqual(self._files(self.bob), [])

    def test_creates_dir_on_first_write(self):
        self.assertFalse(self.bob.memory_dir.exists())
        self.bob.remember("first ever memory")
        self.assertTrue(self.bob.memory_dir.exists())

    def test_never_raises_when_store_path_blocked(self):
        # Make the memory dir a FILE so mkdir() fails -> remember must return None,
        # not raise.
        os.makedirs(os.path.dirname(str(self.bob.memory_dir)), exist_ok=True)
        with open(str(self.bob.memory_dir), "w") as f:
            f.write("not a directory")
        self.assertIsNone(self.bob.remember("this cannot be written"))

    def test_distinct_texts_distinct_memories(self):
        self.bob.remember("lesson one about indexing")
        self.bob.remember("a totally different lesson about timeouts")
        self.assertEqual(len(self._files(self.bob)), 2)


# ============================================================ recall — degrade
class TestRecallDegrade(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _make_company(self.tmp)            # NO venv
        self.bob = Employee.load("bob", self.company)

    def test_no_venv_returns_empty(self):
        self.bob.remember("built a validator with fail-open gating")
        self.assertEqual(self.bob.recall("validator"), [])   # no .rag-venv -> []

    def test_empty_query_returns_empty(self):
        self.assertEqual(self.bob.recall(""), [])
        self.assertEqual(self.bob.recall("   "), [])
        self.assertEqual(self.bob.recall(None), [])

    def test_never_raises_on_garbage(self):
        # No index, weird top_k -> [] not an exception.
        self.assertEqual(self.bob.recall("x", top_k=0), [])
        self.assertEqual(self.bob.recall("x", top_k=-3), [])
        self.assertEqual(self.bob.recall("x", top_k="nan"), [])


# ============================================================ isolation
class TestIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _make_company(self.tmp, ("bob", "gibby"))
        self.bob = Employee.load("bob", self.company)
        self.gibby = Employee.load("gibby", self.company)

    def test_stores_physically_separate(self):
        self.assertNotEqual(self.bob.memory_dir, self.gibby.memory_dir)
        self.assertNotEqual(self.bob.memory_index_dir, self.gibby.memory_index_dir)

    def test_remember_writes_only_own_store(self):
        self.bob.remember("bob's build note")
        self.gibby.remember("gibby's attack note")
        bob_files = list(self.bob.memory_dir.glob("*.md"))
        gibby_files = list(self.gibby.memory_dir.glob("*.md"))
        self.assertEqual(len(bob_files), 1)
        self.assertEqual(len(gibby_files), 1)
        # bob's file body never mentions gibby's note and vice-versa.
        self.assertIn("build note", bob_files[0].read_text())
        self.assertIn("attack note", gibby_files[0].read_text())
        self.assertNotIn("attack note", bob_files[0].read_text())


# ============================================================ daily-run refresh
class TestDailyRunRefresh(unittest.TestCase):
    """The per-employee index-refresh step in daily-run.sh: venv-absent -> a clean
    one-line skip that never fails the core."""

    def _run_daily(self, project_dir):
        proc = subprocess.run(
            ["bash", os.path.join(SCRIPTS, "daily-run.sh"), project_dir,
             "--dry-run", "--no-agent"],
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "SELF_COMPANY_PROJECT_DIR": project_dir})
        return proc

    def _daily_log(self, company):
        import glob
        logs = glob.glob(os.path.join(company, "ops", "logs", "daily-*.md"))
        out = []
        for p in logs:
            with open(p, encoding="utf-8") as f:
                out.append(f.read())
        return "".join(out)

    def test_skip_on_no_venv_never_fails_core(self):
        tmp = tempfile.mkdtemp()
        company = _make_company(tmp, ("bob",))           # NO venv
        bob = Employee.load("bob", company)
        bob.remember("a captured lesson to (eventually) index")
        proc = self._run_daily(tmp)
        self.assertEqual(proc.returncode, 0, f"daily-run failed: {proc.stderr}")
        log = self._daily_log(company)
        self.assertIn("emp-memory-index: skipped", log)
        self.assertIn("RAG venv absent", log)
        # capture still wrote regardless of the (absent) index refresh.
        self.assertTrue(list(bob.memory_dir.glob("*.md")))

    @unittest.skipUnless(HAS_VENV, "RAG venv/deps unavailable")
    def test_refreshes_store_with_venv(self):
        tmp = tempfile.mkdtemp()
        company = _make_company(tmp, ("bob",), venv=True)
        bob = Employee.load("bob", company)
        bob.remember("indexed lesson for the daily refresh path")
        proc = self._run_daily(tmp)
        self.assertEqual(proc.returncode, 0, f"daily-run failed: {proc.stderr}")
        log = self._daily_log(company)
        self.assertIn("emp-memory-index: refreshed 1", log)
        # The employee's OWN index materialized.
        self.assertTrue(os.path.isdir(os.path.join(str(bob.memory_dir), "index")))


# ============================================================ recall — the payoff
@unittest.skipUnless(HAS_VENV, "RAG venv/deps unavailable")
class TestRecallWithVenv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _make_company(self.tmp, ("bob", "gibby"), venv=True)
        self.bob = Employee.load("bob", self.company)
        self.gibby = Employee.load("gibby", self.company)
        # fastembed cold-start can exceed the default recall budget; widen it here.
        self._orig_timeout = employee._RECALL_TIMEOUT
        employee._RECALL_TIMEOUT = 120.0

    def tearDown(self):
        employee._RECALL_TIMEOUT = self._orig_timeout

    def test_finds_relevant_and_isolated(self):
        # Bob records two distinct memories; gibby records his own.
        self.bob.remember(
            "To harden the schedule validator, cache the effective config slice "
            "so R1 through R6 stay byte-identical across runs.", tags=["validator"])
        self.bob.remember(
            "The backup rotation keeps the newest BACKUP_KEEP tarballs of memory.",
            tags=["backup"])
        self.gibby.remember(
            "Attack the parser with a frontmatter block that has no closing fence.",
            tags=["attack"])

        # Refresh each OWN index (isolated).
        rc_b = _index_store(str(self.bob.memory_dir))
        rc_g = _index_store(str(self.gibby.memory_dir))
        if rc_b.returncode != 0 or rc_g.returncode != 0:
            self.skipTest(f"rag_index unavailable/offline: {rc_b.stderr} {rc_g.stderr}")

        hits = self.bob.recall("how do I keep the R1-R6 validator stable", top_k=3)
        self.assertTrue(hits, "expected a semantically-relevant recall for bob")
        top = hits[0]
        self.assertIn("validator", top["text"].lower())
        self.assertEqual(top["owner"], "bob")
        # ISOLATION: none of bob's hits is gibby's memory (path or content).
        for h in hits:
            self.assertIn(str(self.bob.memory_dir), h["path"])
            self.assertNotIn(str(self.gibby.memory_dir), h["path"])
            self.assertNotIn("frontmatter", h["text"].lower())

    def test_gibby_query_never_returns_bob(self):
        self.bob.remember("Bob's unique build lesson about idempotent writes.")
        self.gibby.remember("Gibby probes concurrency races on the shared index.")
        rc_b = _index_store(str(self.bob.memory_dir))
        rc_g = _index_store(str(self.gibby.memory_dir))
        if rc_b.returncode != 0 or rc_g.returncode != 0:
            self.skipTest("rag_index unavailable/offline")
        hits = self.gibby.recall("idempotent build lesson", top_k=3)
        for h in hits:
            self.assertIn(str(self.gibby.memory_dir), h["path"])
            self.assertNotIn(str(self.bob.memory_dir), h["path"])


if __name__ == "__main__":
    unittest.main()
