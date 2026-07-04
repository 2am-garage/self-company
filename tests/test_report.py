"""
Tests for report.py — the autoresearch-style scheduled-work ledger.

Deterministic: fabricates daily logs and checks row parsing, the entropy headline
with up/down direction, the keep/flat/skip/fail verdict, and dry-run exclusion.
"""

import importlib.util
import os
import tempfile
import unittest

import _helpers

_spec = importlib.util.spec_from_file_location(
    "report", os.path.join(_helpers.SCRIPTS_DIR, "report.py"))
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

LOG = """\
## Daily run 2026-06-26T00:07:01 (dry-run)
- decay: scanned 5 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0
- entropy 0.0 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.0) over 5 memories

## Daily run 2026-06-26T06:07:01
- decay --apply: scanned 10 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0
- verify --apply: newly-verified 0 | already 0 | unverifiable 0
- entropy 0.0667 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.0667) over 10 memories
- agent (consolidate/verify): ok

## Daily run 2026-06-26T12:07:01
- decay --apply: scanned 12 | drop 0 | demote 0 | archive 0 | upgrade-candidates 1
- verify --apply: newly-verified 14 | already 0 | unverifiable 8
- entropy 0.0356 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.0356) over 45 memories
- agent (consolidate/verify): ok

## Daily run 2026-06-26T18:07:01
- decay --apply: scanned 12 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0
- entropy 0.0356 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.0356) over 45 memories
- agent: skipped — daily agent-run cap reached (4/4, token breaker)
"""

# B3 (Phase 5 Item 3) fixture: the four unhealthy-agent day shapes. The first
# reproduces the live N4 masking case: agent died but verify +68 moved — the
# old verdict logic emitted `keep` for it.
LOG_FAIL = """\
## Daily run 2026-06-27T00:07:01
- decay --apply: scanned 200 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0
- verify --apply: newly-verified 68 | already 0 | unverifiable 0
- entropy 0.0709 (dup 0.2 | contra 0.0 | stale 0.0 | unverified 0.0) over 165 memories
- agent prompt: measured backlog injected (scored pairs + review candidates from this run)
- agent: failed (rc 1) [run 2/4; streak 1] — deterministic maintenance still applied

## Daily run 2026-06-27T06:07:01
- decay --apply: scanned 200 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0
- entropy 0.0700 (dup 0.2 | contra 0.0 | stale 0.0 | unverified 0.0) over 165 memories
- agent: skipped — auth pre-flight: NOT logged in (AUTH_FAIL x1) — run /login; deterministic maintenance applied

## Daily run 2026-06-27T12:07:01
- decay --apply: scanned 200 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0
- entropy 0.0700 (dup 0.2 | contra 0.0 | stale 0.0 | unverified 0.0) over 165 memories
- agent prompt: measured backlog injected (scored pairs + review candidates from this run)
- agent: TIMEOUT after 600s (rc 124) [run 3/4; streak 2] — partial output in agent-2026-06-27.log; deterministic maintenance still applied

## Daily run 2026-06-27T18:07:01
- decay --apply: scanned 200 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0
- entropy 0.0700 (dup 0.2 | contra 0.0 | stale 0.0 | unverified 0.0) over 165 memories
- agent prompt: measured backlog injected (scored pairs + review candidates from this run)
"""


def _company(d):
    logs = os.path.join(d, ".company", "ops", "logs")
    os.makedirs(logs)
    with open(os.path.join(logs, "daily-2026-06-26.md"), "w") as f:
        f.write(LOG)
    return os.path.join(d, ".company")


class TestReport(unittest.TestCase):
    def test_excludes_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            rows = rp.collect(_company(d))
            self.assertEqual(len(rows), 3)              # dry-run dropped

    def test_entropy_direction_and_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            table = rp.build(rp.collect(_company(d)))
            # 06:07 — first real run, no prior entropy, upgrade=0/verify=0 -> flat (agent ok, nothing moved)
            self.assertEqual(table[0]["status"], "flat")
            # 12:07 — verify +14 and entropy dropped 0.0667 -> 0.0356 -> keep, arrow down
            self.assertEqual(table[1]["status"], "keep")
            self.assertIn("v", table[1]["entropy"])     # downward arrow
            self.assertIn("verify +14", table[1]["desc"])
            # 18:07 — agent skipped, nothing moved -> skip
            self.assertEqual(table[2]["status"], "skip")

    def test_tsv_and_md_render(self):
        with tempfile.TemporaryDirectory() as d:
            table = rp.build(rp.collect(_company(d)))
            md = rp.render_md(table)
            self.assertIn("| run | entropy", md)
            tsv = rp.render_tsv(table)
            self.assertIn("run\tentropy\tmem\tstatus\tdescription", tsv)
            self.assertNotIn(" v", tsv.splitlines()[2])  # arrows stripped in tsv

    def test_unhealthy_agent_days_are_fail_never_masked(self):
        # B3 (Phase 5 Item 3, N4): agent died / AUTH_FAIL / timeout / prompt-
        # with-no-outcome all yield `fail`; healthy days stay unchanged.
        with tempfile.TemporaryDirectory() as d:
            c = _company(d)
            with open(os.path.join(c, "ops", "logs",
                                   "daily-2026-06-27.md"), "w") as f:
                f.write(LOG_FAIL)
            table = rp.build(rp.collect(c))
            # healthy days unchanged (from the 06-26 LOG): flat, keep, skip
            self.assertEqual([t["status"] for t in table[:3]],
                             ["flat", "keep", "skip"])
            # 00:07 — the live N4 case: verify +68 moved, but the agent died
            # -> fail; deterministic progress still shows in the description.
            self.assertEqual(table[3]["status"], "fail")
            self.assertIn("agent died", table[3]["desc"])
            self.assertIn("verify +68", table[3]["desc"])
            # 06:07 — AUTH_FAIL skip is a failure, never a benign "skipped"
            self.assertEqual(table[4]["status"], "fail")
            self.assertIn("AUTH_FAIL", table[4]["desc"])
            # 12:07 — explicit timeout line -> fail with the partial-trail hint
            self.assertEqual(table[5]["status"], "fail")
            self.assertIn("agent TIMEOUT", table[5]["desc"])
            # 18:07 — prompt built but NO outcome line (run died silently)
            self.assertEqual(table[6]["status"], "fail")
            self.assertIn("agent died", table[6]["desc"])

    def test_capture_slug_starting_with_agent_never_flips_verdict(self):
        # Gibby (Phase 5 red-team): a CAPTURE line for a memory whose slug
        # starts with "agent" ("- agent-model-… (L0) — pending_verify" exists
        # in the live log) is DATA, not an outcome line — it must never flip a
        # healthy ok day to `fail` (nor mask a red day).
        log = (
            "## Daily run 2026-06-28T06:07:01\n"
            "- decay --apply: scanned 10 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0\n"
            "- verify --apply: newly-verified 2 | already 0 | unverifiable 0\n"
            "- entropy 0.01 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.01) over 10 memories\n"
            "- agent (consolidate/verify): ok [run 1/4; stdout in agent-2026-06-28.log]\n"
            "\n## CAPTURE (8e466e7c)\n"
            "- agent-model-optimization-iterative-cycle (L0) — pending_verify\n"
            "\n## Daily run 2026-06-28T12:07:01\n"
            "- decay --apply: scanned 10 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0\n"
            "- entropy 0.01 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.01) over 10 memories\n"
            "- agent prompt: measured backlog injected (scored pairs + review candidates from this run)\n"
            "- agent: TIMEOUT after 600s (rc 124) [run 2/4; streak 1] — partial output in agent-2026-06-28.log; deterministic maintenance still applied\n"
            "\n## CAPTURE (8e466e7c)\n"
            "- agent-ok-preference (L0) — pending_verify\n"
        )
        with tempfile.TemporaryDirectory() as d:
            logs = os.path.join(d, ".company", "ops", "logs")
            os.makedirs(logs)
            with open(os.path.join(logs, "daily-2026-06-28.md"), "w") as f:
                f.write(log)
            rows = rp.collect(os.path.join(d, ".company"))
            table = rp.build(rows)
            self.assertEqual(rows[0]["agent"], "ok")       # slug didn't flip it
            self.assertEqual(table[0]["status"], "keep")
            self.assertEqual(rows[1]["agent"], "timeout")  # slug didn't mask it
            self.assertEqual(table[1]["status"], "fail")

    def test_no_cli_skip_benign_and_silent_death_with_movement_fails(self):
        # B3: "claude CLI not found" stays a benign skip; a silent death
        # (prompt + AGENT SUMMARY but NO outcome line) is `fail` even when the
        # deterministic half moved things — movement reports, never greens.
        log = (
            "## Daily run 2026-06-28T00:07:01\n"
            "- decay --apply: scanned 10 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0\n"
            "- entropy 0.05 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.05) over 10 memories\n"
            "- agent: claude CLI not found — skipped (deterministic maintenance applied)\n"
            "\n## Daily run 2026-06-28T06:07:01\n"
            "- decay --apply: scanned 10 | drop 3 | demote 0 | archive 0 | upgrade-candidates 2\n"
            "- verify --apply: newly-verified 9 | already 0 | unverifiable 0\n"
            "- entropy 0.01 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.01) over 10 memories\n"
            "- agent prompt: measured backlog injected (scored pairs + review candidates from this run)\n"
            "AGENT SUMMARY: completed pair 1, pairs 2-5 remain\n"
        )
        with tempfile.TemporaryDirectory() as d:
            logs = os.path.join(d, ".company", "ops", "logs")
            os.makedirs(logs)
            with open(os.path.join(logs, "daily-2026-06-28.md"), "w") as f:
                f.write(log)
            table = rp.build(rp.collect(os.path.join(d, ".company")))
            self.assertEqual(table[0]["status"], "skip")
            self.assertEqual(table[1]["status"], "fail")
            self.assertIn("verify +9", table[1]["desc"])   # reported, not green

    def test_write_creates_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d)
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                rp.main(["--company", c, "--write"])
            reports = os.path.join(c, "ops", "reports")
            self.assertTrue(os.path.exists(os.path.join(reports, "ledger.md")))
            self.assertTrue(os.path.exists(os.path.join(reports, "ledger.tsv")))  # tsv by default


if __name__ == "__main__":
    unittest.main()
