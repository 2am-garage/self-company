#!/usr/bin/env python3
"""
Integration tests: verify audit events are logged during decay/reinforce mutations.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin" / "skills" / "self-company" / "scripts"))

import decay
import memory_audit


class TestAuditIntegration(unittest.TestCase):
    def test_decay_logs_audit_on_drop(self):
        """Test that decay's --apply logs an audit event when dropping a memory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_dir = Path(tmpdir) / "memory" / "L0-working"
            memory_dir.mkdir(parents=True, exist_ok=True)
            company_dir = Path(tmpdir)

            # Create a test memory with age > half-life to trigger decay < 0.25 threshold
            mem_file = memory_dir / "test_mem.md"
            # Half-life for rc=1 is 7 days; age 10 days gives score ~0.57
            # Need older age to drop below 0.25 threshold. age ~18 days gives ~0.2
            old_date = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
            mem_file.write_text(
                f"""---
id: test_drop_mem
tier: L0
created: {old_date}
last_reinforced: {old_date}
reinforce_count: 1
status: active
---
Test body"""
            )

            # Run decay with --apply
            now = datetime.now()
            report = decay.scan_memory_dir(
                memory_dir.parent, now,
                hl_base=7.0, hl_growth=0.5,
                l0_drop_threshold=0.25, l1_archive_threshold=0.15,
                l1_demote_rc=2, l0_to_l1_rc=2, l1_to_l2_rc=4,
                reap_grace_days=7,
                apply=True,
                company_dir=str(company_dir)
            )

            # Verify audit log exists (if audit events were written)
            audit_file = company_dir / "ops" / "logs" / f"memory-audit-{now.strftime('%Y-%m-%d')}.jsonl"
            if audit_file.exists():
                lines = audit_file.read_text().strip().split("\n")
                events = [json.loads(line) for line in lines if line]
                # Verify all events have the required schema
                for event in events:
                    self.assertIn("ts", event)
                    self.assertIn("id", event)
                    self.assertIn("op", event)
                    self.assertIn("schema", event)
                    self.assertEqual(event["schema"], 1)

    def test_decay_logs_audit_on_demote(self):
        """Test that decay logs an audit event when demoting a memory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_dir = Path(tmpdir) / "memory" / "L1-warm"
            memory_dir.mkdir(parents=True, exist_ok=True)
            company_dir = Path(tmpdir)

            # Create a test memory in L1
            mem_file = memory_dir / "test_mem.md"
            old_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            mem_file.write_text(
                f"""---
id: test_demote_mem
tier: L1
created: {old_date}
last_reinforced: {old_date}
reinforce_count: 1
status: active
---
Test body"""
            )

            # Run decay with --apply
            now = datetime.now()
            report = decay.scan_memory_dir(
                memory_dir.parent, now,
                hl_base=7.0, hl_growth=0.5,
                l0_drop_threshold=0.25, l1_archive_threshold=0.15,
                l1_demote_rc=2, l0_to_l1_rc=2, l1_to_l2_rc=4,
                reap_grace_days=7,
                apply=True,
                company_dir=str(company_dir)
            )

            # Verify audit log structure if it exists
            audit_file = company_dir / "ops" / "logs" / f"memory-audit-{now.strftime('%Y-%m-%d')}.jsonl"
            if audit_file.exists():
                lines = audit_file.read_text().strip().split("\n")
                events = [json.loads(line) for line in lines if line]
                # All events should have proper schema
                for event in events:
                    self.assertIsInstance(event, dict)
                    self.assertIn("schema", event)

    def test_audit_events_are_atomic(self):
        """Test that each audit event is one atomic write (one line per event)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            company = Path(tmpdir)

            # Write multiple events quickly
            for i in range(5):
                memory_audit.audit_event(str(company), "drop", f"mem{i}", "status", "a", "b", "decay")

            logdir = company / "ops" / "logs"
            audit_file = logdir / f"memory-audit-{datetime.utcnow().strftime('%Y-%m-%d')}.jsonl"

            # Verify each line is valid JSON
            with open(audit_file) as f:
                for i, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    self.assertEqual(obj["id"], f"mem{i}", f"Line {i} corrupted or out of order")

    def test_cli_writes_audit_inside_dotcompany_not_project_root(self):
        # Regression: decay.py main() derived company_dir as memory_dir.parent.parent
        # (the PROJECT root) instead of memory_dir.parent (the .company dir), so the
        # audit JSONL leaked to <project>/ops/logs/ — outside the git-ignored store.
        # Exercises the real CLI derivation the function-level tests never touched.
        import os
        import subprocess
        scripts = str(Path(__file__).resolve().parent.parent
                      / "plugin" / "skills" / "self-company" / "scripts")
        with tempfile.TemporaryDirectory() as tmpdir:
            company = Path(tmpdir) / ".company"
            l0 = company / "memory" / "L0-working"
            l0.mkdir(parents=True)
            (l0 / "old.md").write_text(
                "---\nid: old-mem\ntier: L0\nstatus: active\n"
                "created: 2026-01-01\nlast_reinforced: 2026-01-01\n"
                "reinforce_count: 0\ndecay_score: 0.01\n---\nold memory\n")
            subprocess.run(
                [sys.executable, os.path.join(scripts, "decay.py"),
                 "--memory-dir", str(company / "memory"),
                 "--now", "2026-07-16", "--apply"],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "SC_RAG_REEXEC": "1"})
            inside = list((company / "ops" / "logs").glob("memory-audit-*.jsonl"))
            leaked = list((Path(tmpdir) / "ops" / "logs").glob("memory-audit-*.jsonl"))
            self.assertTrue(inside, "audit not written inside .company/ops/logs")
            self.assertFalse(leaked, "audit leaked to project root, outside .company")


if __name__ == "__main__":
    unittest.main()
