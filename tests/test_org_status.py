"""
Tests for org-status.py — the "who is actually working" visualizer.

Deterministic: fabricates a daily log + trigger ledger and checks that activity
is attributed to the right employees and the box renders every employee row.
"""

import importlib.util
import os
import tempfile
import unittest

import _helpers

_spec = importlib.util.spec_from_file_location(
    "org_status", os.path.join(_helpers.SCRIPTS_DIR, "org-status.py"))
osx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(osx)

DAILY = """\
## Daily run 2026-07-01T06:07:01
- decay --apply: scanned 58 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0
- verify --apply: newly-verified 3 | already 20 | unverifiable 8
- entropy 0.0276 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.03) over 58 memories
- elon survey: 1 todo(s) -> ops/plans/todo-2026-07-01.md
- agent (consolidate/verify): ok
- ledger: refreshed ops/reports/ledger.{md,tsv}
"""

LEDGER = """\
# Trigger Ledger

| time | trigger | result | reason | payload |
|---|---|---|---|---|
| 2026-06-30T23:44:25 | dummy-e2e | fired | fire | `{"ok": true}` |
"""


# assignments column is a code-spanned JSON fragment that is TRUNCATED mid-key.
COMPANY_RUNS = """\
# Company Runs (session-triggered)

| time | task | planned by | assignments | rc |
|---|---|---|---|---|
| 2026-07-01T11:43:16 | improve the trigger ledger | heuristic | `{"bob": "improve the trigger ledger", "gibby": "verify it", "gib` | 0 |
"""


def _company(d):
    base = os.path.join(d, ".company")
    os.makedirs(os.path.join(base, "ops", "logs"))
    os.makedirs(os.path.join(base, "ops", "reports"))
    with open(os.path.join(base, "ops", "logs", "daily-2026-07-01.md"), "w") as f:
        f.write(DAILY)
    with open(os.path.join(base, "ops", "reports", "triggers.md"), "w") as f:
        f.write(LEDGER)
    with open(os.path.join(base, "ops", "reports", "company-runs.md"), "w") as f:
        f.write(COMPANY_RUNS)
    return base


class TestOrgStatus(unittest.TestCase):
    def test_attribution(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d)
            acts = osx.scan_daily(c)
            osx.scan_triggers(c, acts)
            self.assertIn("tony", acts)
            self.assertIn("entropy", acts["tony"][1])
            self.assertIn("gibby", acts)
            self.assertIn("verify", acts["gibby"][1])
            self.assertIn("elon", acts)
            self.assertIn("tom", acts)
            # Phoebe attributed from the trigger ledger fire
            self.assertIn("phoebe", acts)
            self.assertIn("dummy-e2e", acts["phoebe"][1])

    def test_company_run_attribution(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d)
            acts = {}
            osx.scan_company_runs(c, acts)
            # Both assigned employees attributed from the (truncated) JSON.
            self.assertIn("bob", acts)
            self.assertIn("gibby", acts)
            self.assertTrue(acts["bob"][1].startswith("session:"))
            self.assertIn("improve the trigger ledger", acts["bob"][1])
            self.assertTrue(acts["gibby"][1].startswith("session:"))
            # The truncated key `"gib` must NOT create a phantom employee.
            self.assertNotIn("gib", acts)

    def test_company_run_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, ".company")
            os.makedirs(os.path.join(base, "ops", "reports"))
            acts = {}
            osx.scan_company_runs(base, acts)   # no file -> no crash, no acts
            self.assertEqual(acts, {})

    def test_render_lists_all_employees(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d)
            box = osx.render(c, window_hours=24 * 3650)   # huge window -> all active
            for name in ("Elon", "Phoebe", "Tony", "Gibby", "Bob", "July", "Tom"):
                self.assertIn(name, box)
            self.assertIn("you talk to Elon", box)         # honesty footer

    def test_idle_employee_marked(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d)
            box = osx.render(c, window_hours=24)
            # July has no signal anywhere -> idle
            july_line = [ln for ln in box.splitlines() if "July" in ln][0]
            self.assertIn("idle", july_line)


if __name__ == "__main__":
    unittest.main()
