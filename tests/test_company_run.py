"""
Integration smoke test for company-run.sh — the session-triggered company cycle.

Runs the real script in --demo mode (no LLM) against a temp company and checks
that it dispatches the supervisor and writes a company-runs ledger row.
"""

import os
import subprocess
import tempfile
import unittest

import _helpers

SCRIPT = os.path.join(_helpers.SCRIPTS_DIR, "company-run.sh")


def _company(d, ids=("elon", "phoebe", "bob", "gibby")):
    base = os.path.join(d, ".company", "org", "employees")
    for i in ids:
        os.makedirs(os.path.join(base, i))
        open(os.path.join(base, i, "persona.md"), "w").close()
        # Phase 32 hotfix: a valid desk needs both files (is_valid_desk).
        with open(os.path.join(base, i, "context.md"), "w") as f:
            f.write("---\nname: %s\n---\n" % i.capitalize())
    os.makedirs(os.path.join(d, ".company", "scripts"))
    return os.path.join(d, ".company")


class TestCompanyRun(unittest.TestCase):
    def test_demo_cycle_dispatches_and_logs(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d)
            r = subprocess.run(
                ["bash", SCRIPT, "improve X", "--demo", "--company", c],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("plan (heuristic)", r.stdout)         # planning step ran
            self.assertIn("live supervisor", r.stdout)          # supervisor dispatched
            ledger = os.path.join(c, "ops", "reports", "company-runs.md")
            self.assertTrue(os.path.exists(ledger))
            body = open(ledger, encoding="utf-8").read()
            self.assertIn("improve X", body)
            self.assertIn("heuristic", body)

    def test_validator_violation_refuses_dispatch(self):
        # Phase 32 hotfix Finding 2 (optional gate): a desk that fails Layer-B
        # validation (here a bad-charset hired desk dir -> R7) makes company-run
        # REFUSE to dispatch rather than send workers into a flagged company.
        with tempfile.TemporaryDirectory() as d:
            c = _company(d)
            bad = os.path.join(c, "org", "employees", "BadCase")
            os.makedirs(bad)
            with open(os.path.join(bad, "persona.md"), "w") as f:
                f.write("p\n")
            with open(os.path.join(bad, "context.md"), "w") as f:
                f.write("---\nname: B\n---\n")
            r = subprocess.run(
                ["bash", SCRIPT, "improve X", "--demo", "--company", c],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertIn("REFUSING to dispatch", r.stderr)
            self.assertNotIn("live supervisor", r.stdout)       # never dispatched


if __name__ == "__main__":
    unittest.main()
