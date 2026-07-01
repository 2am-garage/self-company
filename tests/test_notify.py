"""
Tests for notify-status.py — the Option-B catch-up summary.

Deterministic: builds a temp .company with fabricated daily logs and checks new-run
detection (excluding dry-runs), the marker/ack cycle, and the summary string.
"""

import contextlib
import importlib.util
import io
import json
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

    def test_emit_hook_substantive_shows_ledger_and_pushes(self):
        # LOG has drop>0 and entropy moving 0.1 -> 0.0: clearly substantive.
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, LOG)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ns.main(["--company", c, "--emit-hook"])
            out = buf.getvalue().strip()
            self.assertTrue(out, "expected a hook payload")
            ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Scheduled-work report", ctx)        # report always present
            self.assertIn("| run | entropy", ctx)              # the ledger table
            self.assertIn("PushNotification", ctx)             # + push, since substantive
            self.assertIn("Discord", ctx)                      # push-only guard mentioned
            # push self-acked, but the REPORT must STILL show on a second run
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                ns.main(["--company", c, "--emit-hook"])
            ctx2 = json.loads(buf2.getvalue().strip())["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Scheduled-work report", ctx2)       # report not swallowed
            self.assertNotIn("PushNotification", ctx2)         # but no second push

    def test_emit_hook_flat_still_shows_report_no_push(self):
        # All-flat runs: report must STILL surface (decoupled), but no push.
        flat = ("## Daily run 2026-06-26T06:07:01\n"
                "- decay --apply: scanned 9 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0\n"
                "- entropy 0.03 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.03) over 9 memories\n"
                "\n## Daily run 2026-06-26T12:07:01\n"
                "- decay --apply: scanned 9 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0\n"
                "- entropy 0.03 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.03) over 9 memories\n")
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, flat)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ns.main(["--company", c, "--emit-hook"])
            ctx = json.loads(buf.getvalue().strip())["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Scheduled-work report", ctx)        # report shows anyway
            self.assertNotIn("PushNotification", ctx)          # but nothing substantive -> no push

    def test_delta_bootstrap_then_shows_once(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, LOG)
            shown = os.path.join(c, "ops", ".last_shown")
            # first call bootstraps silently and writes the marker
            b0 = io.StringIO()
            with contextlib.redirect_stdout(b0):
                ns.main(["--company", c, "--delta"])
            self.assertEqual(b0.getvalue().strip(), "")
            self.assertTrue(os.path.exists(shown))
            # rewind the marker so the LOG's substantive runs count as new
            with open(shown, "w") as f:
                f.write("2026-06-26T00:00:00\n")
            b1 = io.StringIO()
            with contextlib.redirect_stdout(b1):
                ns.main(["--company", c, "--delta"])
            self.assertIn("daily run", b1.getvalue())          # one-line delta shown
            # show-once: immediate re-call is silent (marker advanced)
            b2 = io.StringIO()
            with contextlib.redirect_stdout(b2):
                ns.main(["--company", c, "--delta"])
            self.assertEqual(b2.getvalue().strip(), "")

    def test_emit_hook_silent_when_no_runs(self):
        with tempfile.TemporaryDirectory() as d:
            logs = os.path.join(d, ".company", "ops", "logs")
            os.makedirs(logs)
            open(os.path.join(logs, "daily-2026-06-26.md"), "w").close()  # no runs
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ns.main(["--company", os.path.join(d, ".company"), "--emit-hook"])
            self.assertEqual(buf.getvalue().strip(), "")        # silent: never ran


if __name__ == "__main__":
    unittest.main()
