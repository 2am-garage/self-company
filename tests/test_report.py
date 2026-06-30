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
