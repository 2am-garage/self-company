"""
Tests for daily-run.sh and schedule.sh (subprocess / black-box).

Covers the deterministic daily core (decay + entropy logging) and the scheduler's
guard paths. The live headless-agent step and real crontab mutation are verified
manually (and the agent is always run with --no-agent here so tests stay
hermetic and token-free).
"""

import os
import subprocess
import tempfile
import unittest

import _helpers

REPO = _helpers.REPO_ROOT


def _bash(args, **kw):
    return subprocess.run(["bash", *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, **kw)


def _fresh_project():
    """Make a temp project with a real .company (via init), return its path."""
    d = tempfile.mkdtemp()
    for sub in ("assets", "scripts"):
        subprocess.run(["cp", "-r", os.path.join(REPO, sub), d], check=True)
    _bash([os.path.join(d, "scripts", "init_company.sh")], cwd=d)
    return d


def _write_mem(company, mid, last_reinforced="2026-06-26", rc=1):
    p = os.path.join(company, "memory", "L0-working", f"{mid}.md")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(
            f"---\nid: {mid}\ntier: L0\nowner: Tony\nsources: [\"[s#1]\"]\n"
            f"created: 2026-06-01\nlast_reinforced: {last_reinforced}\n"
            f"reinforce_count: {rc}\ndecay_score: 1.0\nstatus: active\n---\nbody\n")


class TestDailyRun(unittest.TestCase):
    def test_missing_company_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            r = _bash([os.path.join(REPO, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0)
            self.assertIn("nothing to do", r.stdout)

    def test_dry_run_logs_and_keeps_memory(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-fresh")
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--dry-run"])
            self.assertEqual(r.returncode, 0, r.stderr)
            log = os.path.join(company, "ops", "logs",
                               "daily-" + subprocess.check_output(["date", "+%F"], text=True).strip() + ".md")
            with open(log) as f:
                text = f.read()
            self.assertIn("(dry-run)", text)
            self.assertIn("- decay:", text)
            self.assertIn("- entropy", text)
            # dry-run must NOT delete anything
            self.assertTrue(os.path.exists(os.path.join(company, "memory", "L0-working", "obs-fresh.md")))
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_no_agent_applies_decay_to_stale(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-stale", last_reinforced="2026-05-01")  # ~56d -> drop
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            # stale L0 should have been dropped by decay --apply
            self.assertFalse(
                os.path.exists(os.path.join(company, "memory", "L0-working", "obs-stale.md")))
        finally:
            subprocess.run(["rm", "-rf", d])


class TestInstallHook(unittest.TestCase):
    SH = os.path.join(REPO, "scripts", "install-hook.sh")

    def _settings(self, d):
        return os.path.join(d, ".claude", "settings.json")

    def test_install_idempotent_and_preserves_existing(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".claude"))
            with open(self._settings(d), "w") as f:
                json.dump({"permissions": {"allow": ["Bash(ls)"]}}, f)
            _bash([self.SH, "install", d])
            _bash([self.SH, "install", d])  # twice -> still one
            with open(self._settings(d)) as f:
                cfg = json.load(f)
            self.assertEqual(len(cfg["hooks"]["Stop"]), 1)
            self.assertEqual(cfg["permissions"]["allow"], ["Bash(ls)"])  # preserved
            cmd = cfg["hooks"]["Stop"][0]["hooks"][0]["command"]
            self.assertIn("capture-trigger.py", cmd)
            self.assertIn("self-company-capture", cmd)

    def test_status_reports_state(self):
        with tempfile.TemporaryDirectory() as d:
            r0 = _bash([self.SH, "status", d])
            self.assertIn("not installed", r0.stdout)
            _bash([self.SH, "install", d])
            r1 = _bash([self.SH, "status", d])
            self.assertIn("INSTALLED", r1.stdout)

    def test_uninstall_removes_and_keeps_other_settings(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".claude"))
            with open(self._settings(d), "w") as f:
                json.dump({"permissions": {"allow": ["Bash(ls)"]}}, f)
            _bash([self.SH, "install", d])
            _bash([self.SH, "uninstall", d])
            with open(self._settings(d)) as f:
                cfg = json.load(f)
            self.assertNotIn("hooks", cfg)
            self.assertEqual(cfg["permissions"]["allow"], ["Bash(ls)"])


class TestScheduleGuards(unittest.TestCase):
    def test_bad_command_exits_2(self):
        r = _bash([os.path.join(REPO, "scripts", "schedule.sh"), "bogus", "/tmp"])
        self.assertEqual(r.returncode, 2)

    def test_install_without_company_errors(self):
        with tempfile.TemporaryDirectory() as d:
            r = _bash([os.path.join(REPO, "scripts", "schedule.sh"), "install", d])
            self.assertEqual(r.returncode, 1)
            self.assertIn(".company not found", r.stderr)


if __name__ == "__main__":
    unittest.main()
