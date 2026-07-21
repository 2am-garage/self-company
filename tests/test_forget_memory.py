"""
Tests for forget_memory.py — Chairman-driven HARD FORGET (Mike 2026-07-20
Finding 1 / .company/ops/plans/proposals-2026-07-20.md).

Covers: finding a memory across tiers, the unconditional tombstone (including
overriding L2's normal never-decay rule), the "forget"/"forget_memory" audit
vocabulary, the charter-axiom refusal + --force-charter override, the
non-existent-id error path, the --yes/interactive-confirmation requirement,
and the RAG venv-absent degrade (a fresh tempdir naturally has no
.company/.rag-venv, so this is the REAL degrade path, not a mock).
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import _helpers  # noqa: F401  (puts scripts/ on sys.path)
import forget_memory
import memory_audit


def _write(path, **kw):
    _helpers.write_memory(path, **kw)


class TestFindMemory(unittest.TestCase):
    def test_finds_across_tiers(self):
        with tempfile.TemporaryDirectory() as d:
            memory_dir = Path(d) / "memory"
            _write(memory_dir / "L0-working" / "a.md", id="mem-a", tier="L0")
            _write(memory_dir / "L1-warm" / "b.md", id="mem-b", tier="L1")
            _write(memory_dir / "L2-cold" / "preferences" / "c.md", id="mem-c", tier="L2")

            self.assertEqual(forget_memory.find_memory(memory_dir, "mem-a")["tier"], "L0")
            self.assertEqual(forget_memory.find_memory(memory_dir, "mem-b")["tier"], "L1")
            self.assertEqual(forget_memory.find_memory(memory_dir, "mem-c")["tier"], "L2")

    def test_missing_id_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            memory_dir = Path(d) / "memory"
            _write(memory_dir / "L0-working" / "a.md", id="mem-a", tier="L0")
            self.assertIsNone(forget_memory.find_memory(memory_dir, "nope"))

    def test_finds_already_tombstoned_memory_too(self):
        # forget must be idempotent against an id decay already archived.
        with tempfile.TemporaryDirectory() as d:
            memory_dir = Path(d) / "memory"
            _write(memory_dir / "L0-working" / "a.md", id="mem-a", tier="L0", status="archived")
            mem = forget_memory.find_memory(memory_dir, "mem-a")
            self.assertIsNotNone(mem)
            self.assertEqual(mem["status"], "archived")


class TestTombstoneNow(unittest.TestCase):
    def test_l2_is_tombstoned_despite_never_decay(self):
        """decay.classify_record never even reaches an L2 record ('l2-keep'
        short-circuit) -- forget_memory's tombstone_now must NOT have that
        guard: an explicit Chairman forget overrides L2's never-decay rule."""
        with tempfile.TemporaryDirectory() as d:
            memory_dir = Path(d) / "memory"
            path = memory_dir / "L2-cold" / "preferences" / "c.md"
            _write(path, id="mem-l2", tier="L2", status="active", reinforce_count=4)
            mem = forget_memory.find_memory(memory_dir, "mem-l2")
            new_content, old_status = forget_memory.tombstone_now(mem, "2026-07-21")
            self.assertEqual(old_status, "active")
            self.assertIn("status: archived", new_content)
            self.assertIn("invalid_at: 2026-07-21", new_content)
            # tier/other fields untouched
            self.assertIn("tier: L2", new_content)
            self.assertIn("reinforce_count: 4", new_content)

    def test_idempotent_invalid_at_not_reset(self):
        with tempfile.TemporaryDirectory() as d:
            memory_dir = Path(d) / "memory"
            path = memory_dir / "L0-working" / "a.md"
            content = (
                "---\nid: mem-a\ntier: L0\nstatus: archived\n"
                "invalid_at: 2026-01-01\ncreated: 2026-01-01\n"
                "last_reinforced: 2026-01-01\nreinforce_count: 1\n---\nbody\n"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            mem = forget_memory.find_memory(memory_dir, "mem-a")
            new_content, old_status = forget_memory.tombstone_now(mem, "2026-07-21")
            self.assertEqual(old_status, "archived")
            self.assertIn("invalid_at: 2026-01-01", new_content)
            self.assertNotIn("invalid_at: 2026-07-21", new_content)

    def test_unparseable_frontmatter_returns_none(self):
        mem = {"text": "no frontmatter here", "fm": {}}
        new_content, old_status = forget_memory.tombstone_now(mem, "2026-07-21")
        self.assertIsNone(new_content)
        self.assertIsNone(old_status)

    def test_preserves_body_and_unrelated_fields(self):
        with tempfile.TemporaryDirectory() as d:
            memory_dir = Path(d) / "memory"
            path = memory_dir / "L0-working" / "a.md"
            _write(path, id="mem-a", tier="L0", body="A multi\nline body.")
            mem = forget_memory.find_memory(memory_dir, "mem-a")
            new_content, _ = forget_memory.tombstone_now(mem, "2026-07-21")
            self.assertIn("A multi\nline body.", new_content)
            self.assertIn("owner: Tony", new_content)


class TestForgetCLIRequiresConfirmation(unittest.TestCase):
    def _mem_dir(self, tmp, **kw):
        memory_dir = Path(tmp) / ".company" / "memory"
        path = memory_dir / "L0-working" / "a.md"
        _write(path, id="mem-a", tier="L0", **kw)
        return memory_dir

    def test_yes_flag_tombstones(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = self._mem_dir(tmp)
            rc = forget_memory.main([
                "--memory-dir", str(memory_dir), "--forget", "mem-a",
                "--yes", "--now", "2026-07-21",
            ])
            self.assertEqual(rc, 0)
            content = (memory_dir / "L0-working" / "a.md").read_text()
            self.assertIn("status: archived", content)

    def test_interactive_yes_tombstones(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = self._mem_dir(tmp)
            with mock.patch("builtins.input", return_value="y"):
                rc = forget_memory.main([
                    "--memory-dir", str(memory_dir), "--forget", "mem-a",
                    "--now", "2026-07-21",
                ])
            self.assertEqual(rc, 0)
            content = (memory_dir / "L0-working" / "a.md").read_text()
            self.assertIn("status: archived", content)

    def test_interactive_no_leaves_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = self._mem_dir(tmp)
            before = (memory_dir / "L0-working" / "a.md").read_text()
            with mock.patch("builtins.input", return_value="n"):
                rc = forget_memory.main([
                    "--memory-dir", str(memory_dir), "--forget", "mem-a",
                    "--now", "2026-07-21",
                ])
            self.assertNotEqual(rc, 0)
            after = (memory_dir / "L0-working" / "a.md").read_text()
            self.assertEqual(before, after)

    def test_eof_on_stdin_treated_as_no(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = self._mem_dir(tmp)
            before = (memory_dir / "L0-working" / "a.md").read_text()
            with mock.patch("builtins.input", side_effect=EOFError):
                rc = forget_memory.main([
                    "--memory-dir", str(memory_dir), "--forget", "mem-a",
                    "--now", "2026-07-21",
                ])
            self.assertNotEqual(rc, 0)
            after = (memory_dir / "L0-working" / "a.md").read_text()
            self.assertEqual(before, after)


class TestForgetCLICharterGuard(unittest.TestCase):
    def _charter_mem_dir(self, tmp):
        memory_dir = Path(tmp) / ".company" / "memory"
        path = memory_dir / "L2-cold" / "profile" / "charter.md"
        content = (
            "---\nid: merge-gate\ntier: L2\nprovenance: charter\n"
            'sources: ["charter:merge-gate"]\ncreated: 2026-01-01\n'
            "last_reinforced: 2026-01-01\nreinforce_count: 4\n"
            "decay_score: 1.0\nstatus: active\n---\nMerge gate axiom.\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return memory_dir

    def test_refused_without_force_charter(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = self._charter_mem_dir(tmp)
            path = memory_dir / "L2-cold" / "profile" / "charter.md"
            before = path.read_text()
            rc = forget_memory.main([
                "--memory-dir", str(memory_dir), "--forget", "merge-gate",
                "--yes", "--now", "2026-07-21",
            ])
            self.assertNotEqual(rc, 0)
            self.assertEqual(before, path.read_text())

    def test_force_charter_allows_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = self._charter_mem_dir(tmp)
            path = memory_dir / "L2-cold" / "profile" / "charter.md"
            rc = forget_memory.main([
                "--memory-dir", str(memory_dir), "--forget", "merge-gate",
                "--yes", "--force-charter", "--now", "2026-07-21",
            ])
            self.assertEqual(rc, 0)
            self.assertIn("status: archived", path.read_text())


class TestForgetCLINonexistentId(unittest.TestCase):
    def test_nonexistent_id_exits_nonzero_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / ".company" / "memory"
            path = memory_dir / "L0-working" / "a.md"
            _write(path, id="mem-a", tier="L0")
            before = path.read_text()
            rc = forget_memory.main([
                "--memory-dir", str(memory_dir), "--forget", "does-not-exist",
                "--yes",
            ])
            self.assertNotEqual(rc, 0)
            self.assertEqual(before, path.read_text())


class TestForgetCLIAuditEvent(unittest.TestCase):
    def test_writes_forget_op_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / ".company" / "memory"
            path = memory_dir / "L0-working" / "a.md"
            _write(path, id="mem-a", tier="L0", status="active")
            rc = forget_memory.main([
                "--memory-dir", str(memory_dir), "--forget", "mem-a",
                "--yes", "--now", "2026-07-21",
            ])
            self.assertEqual(rc, 0)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            audit_file = Path(tmp) / ".company" / "ops" / "logs" / f"memory-audit-{today}.jsonl"
            self.assertTrue(audit_file.exists())
            events = [json.loads(l) for l in audit_file.read_text().strip().splitlines()]
            forget_events = [e for e in events if e["op"] == "forget"]
            self.assertEqual(len(forget_events), 1)
            ev = forget_events[0]
            self.assertEqual(ev["id"], "mem-a")
            self.assertEqual(ev["source"], "forget_memory")
            self.assertEqual(ev["field"], "status")
            self.assertEqual(ev["from"], "active")
            self.assertEqual(ev["to"], "archived")
            self.assertEqual(ev["schema"], 1)


class TestRagDeleteVenvAbsentDegrades(unittest.TestCase):
    """A fresh tempdir has no .company/.rag-venv -- this is the REAL
    venv-absent degrade path (no mock needed for this half). Confirms the
    tombstone lands and the CLI does not crash / exits 0 anyway."""

    def test_venv_absent_returns_skipped_status_no_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / ".company"
            status = forget_memory._rag_delete_id("mem-a", company_dir)
            self.assertTrue(status.startswith("skipped: RAG venv absent"))

    def test_index_dir_absent_returns_skipped_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / ".company"
            # Fake a venv python existing but no index built yet.
            venv_bin = company_dir / ".rag-venv" / "bin"
            venv_bin.mkdir(parents=True)
            fake_python = venv_bin / "python"
            fake_python.write_text("#!/bin/sh\nexit 0\n")
            fake_python.chmod(0o755)
            status = forget_memory._rag_delete_id("mem-a", company_dir)
            self.assertTrue(status.startswith("skipped: RAG index absent"))

    def test_cli_end_to_end_with_venv_absent_still_tombstones_and_exits_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / ".company" / "memory"
            path = memory_dir / "L0-working" / "a.md"
            _write(path, id="mem-a", tier="L0", status="active")
            rc = forget_memory.main([
                "--memory-dir", str(memory_dir), "--forget", "mem-a",
                "--yes", "--now", "2026-07-21",
            ])
            self.assertEqual(rc, 0)
            self.assertIn("status: archived", path.read_text())

    def test_delete_subprocess_launch_failure_degrades_not_raises(self):
        # A real venv python AND a real (empty) index dir both "exist", so
        # _rag_delete_id gets past both early-return guards and reaches the
        # subprocess call -- which is mocked to blow up. Must still degrade
        # to a status string, never propagate.
        with tempfile.TemporaryDirectory() as tmp:
            company_dir = Path(tmp) / ".company"
            venv_bin = company_dir / ".rag-venv" / "bin"
            venv_bin.mkdir(parents=True)
            fake_python = venv_bin / "python"
            fake_python.write_text("#!/bin/sh\nexit 0\n")
            fake_python.chmod(0o755)
            index_dir = company_dir / "memory" / "index"
            index_dir.mkdir(parents=True)
            with mock.patch("forget_memory.subprocess.run",
                            side_effect=OSError("boom")):
                status = forget_memory._rag_delete_id("mem-a", company_dir)
            self.assertTrue(status.startswith("skipped:"))


if __name__ == "__main__":
    unittest.main()
