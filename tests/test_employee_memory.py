"""
Tests for per-employee memory — Phase 18 (capture -> index -> recall) plus the
Phase 18b memory MODE (rag/flat) + recall-at-dispatch injection.

Three layers:
  * DEPS-FREE (always run): the mode toggle (default table + context.md override),
    the flat-vs-rag gating (flat -> remember no-op / recall [] / recall_context "";
    rag unchanged), the capture/degrade guarantees, physical store isolation, and
    daily-run's per-employee refresh emitting a clean skip with no venv.
  * VENV-GATED (skipUnless the repo's .company/.rag-venv imports lancedb+fastembed):
    the payoff — recall() finds the semantically-relevant memory in a RAG
    employee's OWN index; a query for one rag employee never surfaces another's
    (isolation); recall_context() renders the "Relevant past experience:" block;
    and daily-run's refresh SKIPS flat employees (no index) while indexing rag ones.

The load-bearing invariants: the rag/flat split is CONFIG-driven (context.md, not
a name hardcoded in logic); flat employees get NO store / NO index / NO recall /
NO injection; rag employees keep the FLAT + isolated + never-raises + never-blocks
Phase-18 behavior; the reused RAG stack is pointed per-employee (no fork).
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

# A canonical rag-mode and flat-mode employee (per employee.MEMORY_MODE_DEFAULTS).
RAG_EMP = "tony"     # analyst -> rag
RAG_EMP2 = "mike"    # research -> rag
FLAT_EMP = "bob"     # executor -> flat
FLAT_EMP2 = "gibby"  # executor -> flat


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


def _make_company(tmp, employees=(RAG_EMP, FLAT_EMP), venv=False):
    """Temp .company with empty desks for `employees` (so memory_mode comes from
    the default table). When venv=True, symlink .rag-venv/bin/python at the repo's
    real venv python so recall/index run for real."""
    company = os.path.join(tmp, ".company")
    for name in employees:
        os.makedirs(os.path.join(company, "org", "employees", name), exist_ok=True)
    if venv:
        # Symlink the WHOLE venv dir (not just the python binary) so pyvenv.cfg is
        # reachable from this path — otherwise the interpreter can't find the
        # venv's site-packages and degrades to the deps-less system python.
        os.symlink(REPO_VENV_DIR, os.path.join(company, ".rag-venv"))
    return company


def _write_context(company, name, mode):
    """Write a minimal context.md declaring `memory: <mode>` for `name`."""
    desk = os.path.join(company, "org", "employees", name)
    os.makedirs(desk, exist_ok=True)
    with open(os.path.join(desk, "context.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name.capitalize()}\nmemory: {mode}\n---\nbody\n")


def _index_store(memory_dir):
    """Refresh ONE employee's own index via the reused rag_index.py + real venv."""
    proc = subprocess.run(
        [REPO_VENV_PY, os.path.join(SCRIPTS, "rag_index.py"),
         "--memory-dir", memory_dir, "--index-dir", os.path.join(memory_dir, "index")],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "SC_RAG_REEXEC": "1"})
    return proc


# ============================================================ memory MODE toggle
class TestMemoryMode(unittest.TestCase):
    """Phase 18b — the rag/flat toggle: default table + context.md override."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _make_company(
            self.tmp, ("bob", "gibby", "tom", "tony", "mike", "elon", "phoebe", "july"))

    def test_default_table_splits_flat_vs_rag(self):
        # Executors default flat; analysts/planners default rag (empty desks =>
        # default table applies).
        for n in ("bob", "gibby", "tom"):
            self.assertEqual(Employee.load(n, self.company).memory_mode, "flat", n)
            self.assertFalse(Employee.load(n, self.company).rag_memory_enabled, n)
        for n in ("tony", "mike", "elon", "phoebe", "july"):
            self.assertEqual(Employee.load(n, self.company).memory_mode, "rag", n)
            self.assertTrue(Employee.load(n, self.company).rag_memory_enabled, n)

    def test_context_md_overrides_default(self):
        # The toggle is CONFIG, not a hardcoded name: context.md wins over the table.
        _write_context(self.company, "bob", "rag")     # flip a flat default -> rag
        _write_context(self.company, "tony", "flat")   # flip a rag default -> flat
        self.assertTrue(Employee.load("bob", self.company).rag_memory_enabled)
        self.assertFalse(Employee.load("tony", self.company).rag_memory_enabled)

    def test_bad_value_degrades_to_default(self):
        _write_context(self.company, "bob", "bogus")   # typo
        self.assertEqual(Employee.load("bob", self.company).memory_mode, "flat")
        _write_context(self.company, "tony", "")       # empty
        self.assertEqual(Employee.load("tony", self.company).memory_mode, "rag")


# ============================================================ flat gating
class TestFlatEmployee(unittest.TestCase):
    """A flat employee has NO per-employee RAG store: remember no-ops, recall is
    [], recall_context is "", and no memory file is ever written."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _make_company(self.tmp, (FLAT_EMP,))
        self.flat = Employee.load(FLAT_EMP, self.company)

    def test_remember_is_noop(self):
        self.assertIsNone(self.flat.remember("a would-be lesson", tags=["x"]))
        # No store materializes, no file written.
        self.assertFalse(
            self.flat.memory_dir.exists() and list(self.flat.memory_dir.glob("*.md")))

    def test_recall_returns_empty(self):
        self.assertEqual(self.flat.recall("anything"), [])

    def test_recall_context_returns_blank(self):
        self.assertEqual(self.flat.recall_context("anything"), "")


# ============================================================ capture (remember)
class TestRemember(unittest.TestCase):
    """The Phase-18 capture guarantees, on a RAG employee (unchanged behavior)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _make_company(self.tmp, (RAG_EMP,))
        self.emp = Employee.load(RAG_EMP, self.company)

    def _files(self, emp):
        d = emp.memory_dir
        return [p for p in d.glob("*.md")] if d.exists() else []

    def test_writes_valid_memory(self):
        path = self.emp.remember(
            "Cache effective() in the model to dodge a circular import.",
            tags=["build", "validator"], source="task-42")
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.path.dirname(str(path)), str(self.emp.memory_dir))
        with open(path, encoding="utf-8") as f:
            text = f.read()
        raw, body = __import__("frontmatter").split(text)
        fm = employee._parse_fm(raw)
        self.assertEqual(fm.get("owner"), RAG_EMP)
        self.assertEqual(fm.get("tier"), "L2")        # fixed index-compat tier
        self.assertTrue(fm.get("id"))
        self.assertTrue(fm.get("created"))
        self.assertEqual(sorted(fm.get("tags")), ["build", "validator"])
        self.assertEqual(fm.get("source"), "task-42")
        self.assertIn("circular import", body)

    def test_idempotent_same_text(self):
        p1 = self.emp.remember("A durable, reusable lesson.")
        p2 = self.emp.remember("A durable, reusable lesson.")
        self.assertEqual(str(p1), str(p2))
        self.assertEqual(len(self._files(self.emp)), 1)

    def test_idempotent_whitespace_normalized(self):
        p1 = self.emp.remember("hello   world  lesson")
        p2 = self.emp.remember("  hello world lesson ")
        self.assertEqual(str(p1), str(p2))
        self.assertEqual(len(self._files(self.emp)), 1)

    def test_empty_text_records_nothing(self):
        self.assertIsNone(self.emp.remember("   \n  "))
        self.assertIsNone(self.emp.remember(""))
        self.assertEqual(self._files(self.emp), [])

    def test_creates_dir_on_first_write(self):
        self.assertFalse(self.emp.memory_dir.exists())
        self.emp.remember("first ever memory")
        self.assertTrue(self.emp.memory_dir.exists())

    def test_never_raises_when_store_path_blocked(self):
        os.makedirs(os.path.dirname(str(self.emp.memory_dir)), exist_ok=True)
        with open(str(self.emp.memory_dir), "w") as f:
            f.write("not a directory")
        self.assertIsNone(self.emp.remember("this cannot be written"))

    def test_distinct_texts_distinct_memories(self):
        self.emp.remember("lesson one about indexing")
        self.emp.remember("a totally different lesson about timeouts")
        self.assertEqual(len(self._files(self.emp)), 2)


# ============================================================ recall — degrade
class TestRecallDegrade(unittest.TestCase):
    """RAG-employee recall degrades to [] with no venv / empty query / garbage."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _make_company(self.tmp, (RAG_EMP,))    # NO venv
        self.emp = Employee.load(RAG_EMP, self.company)

    def test_no_venv_returns_empty(self):
        self.emp.remember("built a validator with fail-open gating")
        self.assertEqual(self.emp.recall("validator"), [])   # no .rag-venv -> []

    def test_empty_query_returns_empty(self):
        self.assertEqual(self.emp.recall(""), [])
        self.assertEqual(self.emp.recall("   "), [])
        self.assertEqual(self.emp.recall(None), [])

    def test_never_raises_on_garbage(self):
        self.assertEqual(self.emp.recall("x", top_k=0), [])
        self.assertEqual(self.emp.recall("x", top_k=-3), [])
        self.assertEqual(self.emp.recall("x", top_k="nan"), [])


# ============================================================ recall_context
class TestRecallContextDegrade(unittest.TestCase):
    """recall_context() degrades to "" for every no-injection case (deps-free)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _make_company(self.tmp, (RAG_EMP, FLAT_EMP))

    def test_flat_employee_blank(self):
        self.assertEqual(Employee.load(FLAT_EMP, self.company).recall_context("x"), "")

    def test_rag_no_venv_blank(self):
        emp = Employee.load(RAG_EMP, self.company)
        emp.remember("some lesson to (eventually) recall")
        self.assertEqual(emp.recall_context("lesson"), "")   # no venv -> no injection

    def test_empty_query_blank(self):
        self.assertEqual(Employee.load(RAG_EMP, self.company).recall_context(""), "")


# ============================================================ isolation
class TestIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _make_company(self.tmp, (RAG_EMP, RAG_EMP2))
        self.a = Employee.load(RAG_EMP, self.company)
        self.b = Employee.load(RAG_EMP2, self.company)

    def test_stores_physically_separate(self):
        self.assertNotEqual(self.a.memory_dir, self.b.memory_dir)
        self.assertNotEqual(self.a.memory_index_dir, self.b.memory_index_dir)

    def test_remember_writes_only_own_store(self):
        self.a.remember("first analyst's build note")
        self.b.remember("second analyst's research note")
        a_files = list(self.a.memory_dir.glob("*.md"))
        b_files = list(self.b.memory_dir.glob("*.md"))
        self.assertEqual(len(a_files), 1)
        self.assertEqual(len(b_files), 1)
        self.assertIn("build note", a_files[0].read_text())
        self.assertIn("research note", b_files[0].read_text())
        self.assertNotIn("research note", a_files[0].read_text())


# ============================================================ daily-run refresh
class TestDailyRunRefresh(unittest.TestCase):
    """The per-employee index-refresh step in daily-run.sh: venv-absent -> a clean
    one-line skip; venv-present -> rag employees indexed, flat employees skipped."""

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
        company = _make_company(tmp, (RAG_EMP,))          # NO venv
        emp = Employee.load(RAG_EMP, company)
        emp.remember("a captured lesson to (eventually) index")
        proc = self._run_daily(tmp)
        self.assertEqual(proc.returncode, 0, f"daily-run failed: {proc.stderr}")
        log = self._daily_log(company)
        self.assertIn("emp-memory-index: skipped", log)
        self.assertIn("RAG venv absent", log)
        # capture still wrote regardless of the (absent) index refresh.
        self.assertTrue(list(emp.memory_dir.glob("*.md")))

    @unittest.skipUnless(HAS_VENV, "RAG venv/deps unavailable")
    def test_refreshes_rag_store_with_venv(self):
        tmp = tempfile.mkdtemp()
        company = _make_company(tmp, (RAG_EMP,), venv=True)
        emp = Employee.load(RAG_EMP, company)
        emp.remember("indexed lesson for the daily refresh path")
        proc = self._run_daily(tmp)
        self.assertEqual(proc.returncode, 0, f"daily-run failed: {proc.stderr}")
        log = self._daily_log(company)
        self.assertIn("emp-memory-index: refreshed 1 rag-employee", log)
        self.assertTrue(os.path.isdir(os.path.join(str(emp.memory_dir), "index")))

    @unittest.skipUnless(HAS_VENV, "RAG venv/deps unavailable")
    def test_flat_employee_skipped_no_index(self):
        # A flat employee with a stray memory file (written directly, bypassing the
        # no-op remember) must NOT be indexed; the rag employee beside it IS.
        tmp = tempfile.mkdtemp()
        company = _make_company(tmp, (RAG_EMP, FLAT_EMP), venv=True)
        rag = Employee.load(RAG_EMP, company)
        flat = Employee.load(FLAT_EMP, company)
        rag.remember("rag employee's real indexed lesson")
        # Directly plant a valid memory file in the flat employee's store.
        os.makedirs(str(flat.memory_dir), exist_ok=True)
        with open(os.path.join(str(flat.memory_dir), "stray-000000000000.md"), "w") as f:
            f.write("---\nid: stray-000000000000\nowner: %s\ntier: L2\n"
                    "created: 2026-07-07\ntags: []\n---\nstray flat memory\n" % FLAT_EMP)
        proc = self._run_daily(tmp)
        self.assertEqual(proc.returncode, 0, f"daily-run failed: {proc.stderr}")
        log = self._daily_log(company)
        self.assertIn("emp-memory-index: refreshed 1 rag-employee", log)
        self.assertIn("flat employee(s) skipped", log)
        # RAG employee got an index; the flat employee did NOT.
        self.assertTrue(os.path.isdir(os.path.join(str(rag.memory_dir), "index")))
        self.assertFalse(os.path.isdir(os.path.join(str(flat.memory_dir), "index")))


# ============================================================ recall — the payoff
@unittest.skipUnless(HAS_VENV, "RAG venv/deps unavailable")
class TestRecallWithVenv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _make_company(self.tmp, (RAG_EMP, RAG_EMP2), venv=True)
        self.a = Employee.load(RAG_EMP, self.company)
        self.b = Employee.load(RAG_EMP2, self.company)
        # fastembed cold-start can exceed the default recall budget; widen it here.
        self._orig_timeout = employee._RECALL_TIMEOUT
        employee._RECALL_TIMEOUT = 120.0

    def tearDown(self):
        employee._RECALL_TIMEOUT = self._orig_timeout

    def test_finds_relevant_and_isolated(self):
        self.a.remember(
            "To harden the schedule validator, cache the effective config slice "
            "so R1 through R6 stay byte-identical across runs.", tags=["validator"])
        self.a.remember(
            "The backup rotation keeps the newest BACKUP_KEEP tarballs of memory.",
            tags=["backup"])
        self.b.remember(
            "Probe the parser with a frontmatter block that has no closing fence.",
            tags=["research"])

        rc_a = _index_store(str(self.a.memory_dir))
        rc_b = _index_store(str(self.b.memory_dir))
        if rc_a.returncode != 0 or rc_b.returncode != 0:
            self.skipTest(f"rag_index unavailable/offline: {rc_a.stderr} {rc_b.stderr}")

        hits = self.a.recall("how do I keep the R1-R6 validator stable", top_k=3)
        self.assertTrue(hits, "expected a semantically-relevant recall")
        top = hits[0]
        self.assertIn("validator", top["text"].lower())
        self.assertEqual(top["owner"], RAG_EMP)
        for h in hits:
            self.assertIn(str(self.a.memory_dir), h["path"])
            self.assertNotIn(str(self.b.memory_dir), h["path"])
            self.assertNotIn("frontmatter", h["text"].lower())

    def test_one_rag_query_never_returns_another(self):
        self.a.remember("First analyst's unique lesson about idempotent writes.")
        self.b.remember("Second analyst probes concurrency races on the index.")
        rc_a = _index_store(str(self.a.memory_dir))
        rc_b = _index_store(str(self.b.memory_dir))
        if rc_a.returncode != 0 or rc_b.returncode != 0:
            self.skipTest("rag_index unavailable/offline")
        hits = self.b.recall("idempotent build lesson", top_k=3)
        for h in hits:
            self.assertIn(str(self.b.memory_dir), h["path"])
            self.assertNotIn(str(self.a.memory_dir), h["path"])

    def test_recall_context_injection_block(self):
        # recall_context renders a compact, prependable "Relevant past experience:"
        # block for a rag employee with relevant memories.
        self.a.remember(
            "Wire recall at dispatch through Employee.recall_context so the worker "
            "prompt gets its own past lessons, gated on rag_memory_enabled.",
            tags=["dispatch"])
        rc_a = _index_store(str(self.a.memory_dir))
        if rc_a.returncode != 0:
            self.skipTest("rag_index unavailable/offline")
        block = self.a.recall_context("how is recall wired at dispatch", top_k=3)
        self.assertTrue(block.startswith("Relevant past experience"))
        self.assertIn("recall_context", block)
        # Every rendered line is a bullet; nothing from the other store leaks.
        for line in block.splitlines()[1:]:
            self.assertTrue(line.startswith("- "))


if __name__ == "__main__":
    unittest.main()
