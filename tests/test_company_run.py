"""
Integration smoke test for company-run.sh — the session-triggered company cycle.

Runs the real script in --demo mode (no LLM) against a temp company and checks
that it dispatches the supervisor and writes a company-runs ledger row.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import time
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


class TestGateCaptureTimeout(unittest.TestCase):
    """Finalization pass Change 2: closes the DoS Gibby found — a worker with
    full Bash (Phase 34 execute tier) can open `/proc/<supervisor-pid>/fd/2`
    and hold it open, so company-run.sh's `$(...)` capture of the
    supervisor's stderr never reaches EOF and hangs the session forever, no
    matter how the supervisor's OWN in-process budgets are set (they bound
    the WORKER, not the capture pipe). `timeout` now wraps the whole capture
    (SELF_COMPANY_GATE_CAPTURE_TIMEOUT / SELF_COMPANY_TIMEOUT_KILL_AFTER).

    This test doesn't need to reproduce the exact /proc trick — the fix
    bounds the capture regardless of WHY it never returns. We fake out the
    entire `supervisor.py` (via CLAUDE_PLUGIN_ROOT) with a stub that ignores
    SIGTERM and never exits on its own; `claude` is made unresolvable on PATH
    so company-run.sh takes the heuristic-plan path and reaches the DISPATCH
    step's capture directly, without needing a real LLM call anywhere."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _company(self.tmp)
        self.fake_root = os.path.join(self.tmp, "fake-plugin-root")
        fake_scripts = os.path.join(self.fake_root, "skills", "self-company", "scripts")
        os.makedirs(fake_scripts, exist_ok=True)
        stub = os.path.join(fake_scripts, "supervisor.py")
        with open(stub, "w") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "while True:\n"
                "    time.sleep(0.2)\n"
            )
        st = os.stat(stub)
        os.chmod(stub, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, **extra):
        env = {
            **os.environ,
            "PATH": "/usr/bin:/bin",            # `claude` unresolvable -> heuristic plan
            "HOME": self.tmp,                   # ...and no $HOME/.local/bin/claude either
            "SELF_COMPANY_PROJECT_DIR": self.tmp,
            "CLAUDE_PLUGIN_ROOT": self.fake_root,
        }
        env.update(extra)
        return env

    def test_hung_capture_is_bounded_and_classified_unresolved(self):
        env = self._env(SELF_COMPANY_GATE_CAPTURE_TIMEOUT="2",
                        SELF_COMPANY_TIMEOUT_KILL_AFTER="1")
        t0 = time.monotonic()
        r = subprocess.run(
            ["bash", SCRIPT, "improve X", "--company", self.company],
            capture_output=True, text=True, env=env, timeout=30)
        elapsed = time.monotonic() - t0
        # Bounded: ~GATE_CAPTURE_TIMEOUT(2) + KILL_AFTER(1) grace, nowhere
        # near "hangs forever" (and nowhere near the supervisor's own 600s
        # default per-worker budget, which this stub never even reaches).
        self.assertLess(elapsed, 15, "capture was not bounded by the timeout")
        # Loud, non-zero — never a silent/clean exit.
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("GATE CAPTURE TIMEOUT", r.stderr)
        ledger = os.path.join(self.company, "ops", "reports", "company-runs.md")
        self.assertTrue(os.path.exists(ledger))
        with open(ledger, encoding="utf-8") as f:
            body = f.read()
        rows = [ln for ln in body.splitlines() if ln.startswith("| 20")]
        self.assertEqual(len(rows), 1)
        cells = [c.strip() for c in rows[0].strip("|").split("|")]
        # Never "-"/"-" (that means "gate never armed") and never "clean".
        # Diagnostic UNRESOLVED (2026-07-21): the verdict carries a reason
        # suffix, e.g. "unresolved (capture_timeout)" — still unresolved, never
        # clean. Assert the prefix so the security intent (not "clean") holds.
        self.assertTrue(cells[-1].startswith("unresolved"),
                        f"expected an unresolved verdict, got {cells[-1]!r}")
        self.assertIn("capture_timeout", cells[-1])

    def test_default_timeout_is_bounded_but_generous(self):
        # The default (no override) must still be a real ceiling, not
        # accidentally unbounded — but this test only needs to prove the knob
        # reads back sanely, not wait 900s. Covered functionally by the
        # explicit-override test above; this just locks the documented
        # default so a future edit can't silently drop it. 2400s (raised from
        # 900s on 2026-07-18) must exceed a legit rounds×per-worker-budget cycle
        # (3×600s=1800s) so a real multi-round gate isn't false-killed.
        with open(SCRIPT, encoding="utf-8") as f:
            script = f.read()
        self.assertIn("SELF_COMPANY_GATE_CAPTURE_TIMEOUT:-2400", script)


if __name__ == "__main__":
    unittest.main()
