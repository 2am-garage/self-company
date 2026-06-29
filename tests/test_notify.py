"""
Tests for notify-status.py — the Option-B catch-up summary.

Deterministic: builds a temp .company with fabricated daily logs and checks new-run
detection (excluding dry-runs), the marker/ack cycle, and the summary string.
"""

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest

import _helpers

_spec = importlib.util.spec_from_file_location(
    "notify_status", os.path.join(_helpers.SCRIPTS_DIR, "notify-status.py"))
ns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns)


def _company(d, log_body):
    logs = os.path.join(d, ".company", "ops", "logs")
    os.makedirs(logs)
    with open(os.path.join(logs, "daily-2026-06-26.md"), "w") as f:
        f.write(log_body)
    return os.path.join(d, ".company")


LOG = """\
## Daily run 2026-06-26T13:00:00 (dry-run)
- decay: scanned 5 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0
- entropy 0.0 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.0) over 5 memories

## Daily run 2026-06-26T18:07:01
- decay --apply: scanned 10 | drop 2 | demote 0 | archive 0 | upgrade-candidates 1
- entropy 0.1 (dup 0.0 | contra 0.0 | stale 0.1 | unverified 0.0) over 10 memories

## Consolidation pass 2026-06-26
- Merged a into b (rc 1->2, promoted L0->L1)
- agent (consolidate/verify): ok

## Daily run 2026-06-26T20:24:00
- decay --apply: scanned 8 | drop 1 | demote 0 | archive 0 | upgrade-candidates 0
- entropy 0.0 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.0) over 8 memories
- agent: skipped/failed (rc 1) — deterministic maintenance still applied
"""


class TestNotify(unittest.TestCase):
    def test_counts_real_runs_excludes_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, LOG)
            runs = ns.collect_runs(c, None)
            self.assertEqual(len(runs), 2)                 # dry-run excluded
            s = ns.summarize(runs)
            self.assertIn("2 daily runs", s)
            self.assertIn("3 decayed", s)                  # 2 + 1
            self.assertIn("agent ok 1/2", s)               # one ok, one skipped

    def test_marker_suppresses_old_runs(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, LOG)
            with open(os.path.join(c, "ops", ".last_notified"), "w") as f:
                f.write("2026-06-26T23:00:00\n")
            since = ns.read_marker(c)
            self.assertEqual(len(ns.collect_runs(c, since)), 0)

    def test_ack_writes_marker(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, LOG)
            with contextlib.redirect_stdout(io.StringIO()):  # don't leak JSON to suite output
                ns.main(["--company", c, "--ack"])
            self.assertTrue(os.path.exists(os.path.join(c, "ops", ".last_notified")))
            # after ack, nothing is "new"
            self.assertEqual(len(ns.collect_runs(c, ns.read_marker(c))), 0)

    def test_empty_summary_when_no_runs(self):
        self.assertIn("no new", ns.summarize([]))


if __name__ == "__main__":
    unittest.main()
