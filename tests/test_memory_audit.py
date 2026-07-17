#!/usr/bin/env python3
"""
Tests for memory_audit.py — append-only JSONL audit log.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin" / "skills" / "self-company" / "scripts"))

import memory_audit
import daily_log


class TestMemoryAudit(unittest.TestCase):
    def test_audit_event_well_formed(self):
        """Test that audit_event produces well-formed JSONL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            company = Path(tmpdir)
            result = memory_audit.audit_event(
                str(company), "drop", "test_id_1", "status", "active", "archived", "decay"
            )
            self.assertIs(result, True)

            logfile = company / "ops" / "logs" / f"memory-audit-{datetime.utcnow().strftime('%Y-%m-%d')}.jsonl"
            self.assertTrue(logfile.exists())

            lines = logfile.read_text().strip().split("\n")
            self.assertEqual(len(lines), 1)
            event = json.loads(lines[0])
            self.assertEqual(event["id"], "test_id_1")
            self.assertEqual(event["op"], "drop")
            self.assertEqual(event["field"], "status")
            self.assertEqual(event["from"], "active")
            self.assertEqual(event["to"], "archived")
            self.assertEqual(event["source"], "decay")
            self.assertEqual(event["schema"], 1)
            self.assertIn("ts", event)

    def test_audit_event_multiple_appends(self):
        """Test that multiple audit_event calls append to the same file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            company = Path(tmpdir)
            today = datetime.utcnow().strftime("%Y-%m-%d")

            memory_audit.audit_event(str(company), "drop", "mem1", "status", "active", "archived", "decay")
            memory_audit.audit_event(str(company), "demote", "mem2", "tier", "L1", "L0", "decay")
            memory_audit.audit_event(str(company), "reinforce", "mem3", "reinforce_count", "1", "2", "reinforce_memory")

            logfile = company / "ops" / "logs" / f"memory-audit-{today}.jsonl"
            lines = logfile.read_text().strip().split("\n")
            self.assertEqual(len(lines), 3)

            ids = [json.loads(line)["id"] for line in lines]
            self.assertEqual(ids, ["mem1", "mem2", "mem3"])

    def test_audit_event_write_failure_non_blocking(self):
        """Test that audit_event returns False on write failure but doesn't raise."""
        with tempfile.TemporaryDirectory() as tmpdir:
            company = Path(tmpdir)
            logdir = Path(company) / "ops" / "logs"
            logdir.mkdir(parents=True, exist_ok=True)

            logfile = logdir / f"memory-audit-{datetime.utcnow().strftime('%Y-%m-%d')}.jsonl"
            logfile.write_text("dummy")

            # Make the logfile read-only to force a write failure
            logfile.chmod(0o444)
            try:
                result = memory_audit.audit_event(
                    str(company), "drop", "test_id", "status", "active", "archived", "decay"
                )
                self.assertIs(result, False)  # Write failed but didn't raise
            finally:
                logfile.chmod(0o644)  # restore for cleanup

    def test_read_events_single_day(self):
        """Test read_events with a specific date."""
        with tempfile.TemporaryDirectory() as tmpdir:
            company = Path(tmpdir)
            today = datetime.utcnow().strftime("%Y-%m-%d")

            memory_audit.audit_event(str(company), "drop", "mem1", "status", "active", "archived", "decay")
            memory_audit.audit_event(str(company), "promote", "mem2", "tier", "L0", "L1", "decay")

            logdir = company / "ops" / "logs"
            events = memory_audit.read_events(logdir, today)

            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["id"], "mem1")
            self.assertEqual(events[1]["id"], "mem2")

    def test_read_events_all_files(self):
        """Test read_events reading all memory-audit files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            company = Path(tmpdir)
            logdir = company / "ops" / "logs"

            # Manually create audit files for different dates
            logdir.mkdir(parents=True, exist_ok=True)
            (logdir / "memory-audit-2024-01-01.jsonl").write_text(
                json.dumps({"ts": "2024-01-01T00:00:00Z", "id": "old_mem", "op": "drop"}) + "\n"
            )
            (logdir / "memory-audit-2024-01-02.jsonl").write_text(
                json.dumps({"ts": "2024-01-02T00:00:00Z", "id": "newer_mem", "op": "promote"}) + "\n"
            )

            events = memory_audit.read_events(logdir)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["id"], "old_mem")
            self.assertEqual(events[1]["id"], "newer_mem")

    def test_read_events_nonexistent_dir(self):
        """Test read_events with a nonexistent directory."""
        logdir = Path("/nonexistent/path")
        events = memory_audit.read_events(logdir)
        self.assertEqual(events, [])

    def test_read_events_corrupt_lines(self):
        """Test that corrupt JSONL lines are skipped without crashing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            company = Path(tmpdir)
            logdir = company / "ops" / "logs"
            logdir.mkdir(parents=True, exist_ok=True)

            # Write a mix of valid and corrupt lines
            logfile = logdir / "memory-audit-2024-01-01.jsonl"
            logfile.write_text(
                json.dumps({"id": "mem1", "op": "drop"}) + "\n"
                + "invalid json line\n"
                + json.dumps({"id": "mem2", "op": "promote"}) + "\n"
            )

            events = memory_audit.read_events(logdir)
            self.assertEqual(len(events), 2)  # corrupt line skipped
            self.assertEqual(events[0]["id"], "mem1")
            self.assertEqual(events[1]["id"], "mem2")

    def test_audit_event_null_values(self):
        """Test that None values are serialized as null in JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            company = Path(tmpdir)
            today = datetime.utcnow().strftime("%Y-%m-%d")

            memory_audit.audit_event(str(company), "promote", "mem1", "field", None, "L1", "decay")

            logdir = company / "ops" / "logs"
            events = memory_audit.read_events(logdir, today)

            self.assertEqual(len(events), 1)
            self.assertIsNone(events[0]["from"])
            self.assertEqual(events[0]["to"], "L1")

    def test_audit_event_special_chars(self):
        """Test that special characters and unicode are preserved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            company = Path(tmpdir)
            today = datetime.utcnow().strftime("%Y-%m-%d")

            memory_audit.audit_event(
                str(company), "drop", "mem_с_кириллицей", "notes", "old 中文", "new 日本語", "decay"
            )

            logdir = company / "ops" / "logs"
            events = memory_audit.read_events(logdir, today)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["id"], "mem_с_кириллицей")
            self.assertEqual(events[0]["from"], "old 中文")
            self.assertEqual(events[0]["to"], "new 日本語")

    def test_prune_memory_audit_by_filename_date(self):
        """Test that prune removes memory-audit files older than retain_days."""
        with tempfile.TemporaryDirectory() as tmpdir:
            company = Path(tmpdir)
            logdir = company / "ops" / "logs"
            logdir.mkdir(parents=True, exist_ok=True)

            today = datetime.now().date()
            cutoff_date = today - timedelta(days=90)
            old_date = (cutoff_date - timedelta(days=1)).strftime("%Y-%m-%d")
            recent_date = cutoff_date.strftime("%Y-%m-%d")

            # Create old and recent audit files
            (logdir / f"memory-audit-{old_date}.jsonl").write_text('{"id": "old"}\n')
            (logdir / f"memory-audit-{recent_date}.jsonl").write_text('{"id": "recent"}\n')
            (logdir / f"daily-{old_date}.md").write_text("old daily log")
            (logdir / f"daily-{recent_date}.md").write_text("recent daily log")

            removed, warning = daily_log.prune(str(company), retain_days=90, today=today, window_days=30)

            # Prune should remove both old files (audit and daily)
            self.assertGreaterEqual(removed, 1)
            self.assertFalse((logdir / f"memory-audit-{old_date}.jsonl").exists())
            self.assertTrue((logdir / f"memory-audit-{recent_date}.jsonl").exists())
            self.assertFalse((logdir / f"daily-{old_date}.md").exists())
            self.assertTrue((logdir / f"daily-{recent_date}.md").exists())

    def test_audit_event_zero_values(self):
        """Test that zero/empty values are handled correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            company = Path(tmpdir)
            today = datetime.utcnow().strftime("%Y-%m-%d")

            memory_audit.audit_event(str(company), "drop", "mem", "count", "0", "0", "decay")

            logdir = company / "ops" / "logs"
            events = memory_audit.read_events(logdir, today)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["from"], "0")
            self.assertEqual(events[0]["to"], "0")


if __name__ == "__main__":
    unittest.main()
