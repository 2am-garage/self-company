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

    def test_agent_outcome_classes_match_report(self):
        # B3 parity lock: notify-status's standalone classifier must agree
        # with report.py's collect() on every writer line shape (and both must
        # ignore CAPTURE lines whose memory slug starts with "agent").
        _rspec = importlib.util.spec_from_file_location(
            "report_for_parity", os.path.join(_helpers.SCRIPTS_DIR, "report.py"))
        rp = importlib.util.module_from_spec(_rspec)
        _rspec.loader.exec_module(rp)
        shapes = {
            "- agent (consolidate/verify): ok [run 1/4; stdout in agent-x.log]": "ok",
            "- agent: skipped — daily agent-run cap reached (4/4, token breaker)": "skipped",
            "- agent: claude CLI not found — skipped (deterministic maintenance applied)": "skipped",
            "- agent: skipped — auth pre-flight: NOT logged in (AUTH_FAIL x1) — run /login; deterministic maintenance applied": "auth-fail",
            "- agent: TIMEOUT after 600s (rc 124) [run 1/4; streak 1] — partial output in agent-x.log; deterministic maintenance still applied": "timeout",
            "- agent: failed (rc 7) [run 1/4; streak 1] — deterministic maintenance still applied": "failed",
            "- agent-model-optimization-iterative-cycle (L0) — pending_verify": None,
        }
        for ln, expected in shapes.items():
            self.assertEqual(ns._classify_agent_line(ln), expected, ln)
            # report.py parity: run the line through a one-block fixture
            with tempfile.TemporaryDirectory() as d:
                logs = os.path.join(d, "ops", "logs")
                os.makedirs(logs)
                with open(os.path.join(logs, "daily-2026-06-28.md"), "w") as f:
                    f.write("## Daily run 2026-06-28T06:07:01\n" + ln + "\n")
                rows = rp.collect(d)
                self.assertEqual(rows[0]["agent"], expected, ln)
        # breadcrumb + silent death: prompt with no outcome -> failed, both sides
        self.assertEqual(ns._classify_agent_line("- agent prompt: generic"), "prompt")
        with tempfile.TemporaryDirectory() as d:
            logs = os.path.join(d, "ops", "logs")
            os.makedirs(logs)
            with open(os.path.join(logs, "daily-2026-06-28.md"), "w") as f:
                f.write("## Daily run 2026-06-28T06:07:01\n- agent prompt: generic\n")
            self.assertEqual(rp.collect(d)[0]["agent"], "failed")
            self.assertEqual(ns.collect_runs(d, None)[0]["agent"], "failed")

    def test_summary_surfaces_fail_count(self):
        # B3: failed/timeout/auth-fail runs are never summarized as benign —
        # the catch-up line carries an explicit agent-fail count.
        log = (
            "## Daily run 2026-06-26T06:07:01\n"
            "- decay --apply: scanned 10 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0\n"
            "- entropy 0.1 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.1) over 10 memories\n"
            "- agent: failed (rc 7) [run 1/4; streak 1] — deterministic maintenance still applied\n"
            "\n## Daily run 2026-06-26T12:07:01\n"
            "- decay --apply: scanned 10 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0\n"
            "- entropy 0.1 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.1) over 10 memories\n"
            "- agent: TIMEOUT after 600s (rc 124) [run 2/4; streak 2] — partial output in agent-x.log; deterministic maintenance still applied\n"
            "\n## Daily run 2026-06-26T18:07:01\n"
            "- decay --apply: scanned 10 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0\n"
            "- entropy 0.1 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.1) over 10 memories\n"
            "- agent (consolidate/verify): ok [run 3/4; stdout in agent-x.log]\n"
        )
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, log)
            runs = ns.collect_runs(c, None)
            self.assertEqual([b["agent"] for b in runs],
                             ["failed", "timeout", "ok"])
            s = ns.summarize(runs)
            self.assertIn("agent ok 1/3", s)
            self.assertIn("2 agent-fail", s)

    _INFLIGHT = (
        "## Daily run 2026-06-26T18:07:01\n"
        "- decay --apply: scanned 10 | drop 1 | demote 0 | archive 0 | upgrade-candidates 0\n"
        "- entropy 0.02 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.02) over 10 memories\n"
        "- agent prompt: measured backlog injected (scored pairs + review candidates from this run)\n"
    )

    def test_inflight_latest_run_is_running_not_failed(self):
        # C2: latest block is a silent death but the agent log is FRESH ->
        # collect_runs classifies it `running`, never a false `failed` (nor an
        # agent-fail in the summary).
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, self._INFLIGHT)
            with open(os.path.join(c, "ops", "logs", "agent-2026-06-26.log"), "w") as f:
                f.write("stream\n")                              # fresh
            runs = ns.collect_runs(c, None)
            self.assertEqual(runs[-1]["agent"], "running")
            self.assertNotIn("agent-fail", ns.summarize(runs))

    def test_inflight_stale_log_still_failed(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, self._INFLIGHT)
            p = os.path.join(c, "ops", "logs", "agent-2026-06-26.log")
            with open(p, "w") as f:
                f.write("stream\n")
            old = time.time() - 99999
            os.utime(p, (old, old))                             # stale
            runs = ns.collect_runs(c, None)
            self.assertEqual(runs[-1]["agent"], "failed")

    def test_inflight_survives_since_filter(self):
        # The global-latest in-flight run is reclassified BEFORE the `since` filter,
        # so even a marker that would clip it in still sees `running`, not failed.
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, self._INFLIGHT)
            with open(os.path.join(c, "ops", "logs", "agent-2026-06-26.log"), "w") as f:
                f.write("stream\n")
            runs = ns.collect_runs(c, ns._parse_ts("2026-06-26T00:00:00"))
            self.assertEqual(runs[-1]["agent"], "running")

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
