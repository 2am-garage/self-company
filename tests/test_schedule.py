"""
Tests for schedule.sh — Phase 7 multi-company schedule management.

Every test drives schedule.sh as a subprocess with SELF_COMPANY_CRONTAB_FILE
pointed at a temp file. The REAL user crontab is NEVER read or written here: the
C1 backend seam (_cron_read/_cron_write) routes all crontab I/O to the injected
file whenever that env var is set. No `crontab -l` / `crontab -` ever runs.

Covers: project-namespaced ownership (Item 1), auto-stagger (Item 2), fleet
list/prune/scoped-uninstall (Item 3), legacy migration, and non-self-company
line preservation.
"""

import os
import subprocess
import tempfile
import unittest

import _helpers

REPO = _helpers.REPO_ROOT
SH = os.path.join(REPO, "skills", "self-company", "scripts", "schedule.sh")


def _run(args, fake, extra_env=None):
    env = {**os.environ, "SELF_COMPANY_CRONTAB_FILE": fake}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", SH, *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=env)


class ScheduleTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake = os.path.join(self.tmp, "crontab")
        # A pre-existing, non-self-company line that must survive every op.
        self.foreign = "* * * * * echo hello  # my-own-job"
        with open(self.fake, "w") as f:
            f.write(self.foreign + "\n")
        self.A = self._project("projA")
        self.B = self._project("projB")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp])

    def _project(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(p, ".company", "ops", "logs"), exist_ok=True)
        os.makedirs(os.path.join(p, ".company", "scripts"), exist_ok=True)
        return p

    def _read(self):
        with open(self.fake) as f:
            return f.read()

    def _lines(self):
        return [ln for ln in self._read().splitlines() if ln.strip()]

    def _sc_lines(self):
        return [ln for ln in self._lines() if "self-company" in ln]

    def _daily_minute(self, path):
        for ln in self._sc_lines():
            if "self-company-daily" in ln and path in ln:
                return ln.split()[0]
        return None


class TestNamespacedOwnership(ScheduleTestBase):
    def test_install_A_then_B_coexist(self):
        # (a) install A then B -> 4 self-company lines, foreign untouched
        self.assertEqual(_run(["install", self.A], self.fake).returncode, 0)
        self.assertEqual(_run(["install", self.B], self.fake).returncode, 0)
        sc = self._sc_lines()
        self.assertEqual(len(sc), 4, sc)
        self.assertEqual(sum(1 for ln in sc if self.A in ln), 2)
        self.assertEqual(sum(1 for ln in sc if self.B in ln), 2)
        self.assertIn(self.foreign, self._lines())

    def test_uninstall_A_leaves_B(self):
        # (b) uninstall A -> only B's two lines remain
        _run(["install", self.A], self.fake)
        _run(["install", self.B], self.fake)
        r = _run(["uninstall", self.A], self.fake)
        self.assertEqual(r.returncode, 0)
        sc = self._sc_lines()
        self.assertEqual(len(sc), 2, sc)
        self.assertTrue(all(self.B in ln for ln in sc))
        self.assertFalse(any(self.A in ln for ln in sc))
        self.assertIn(self.foreign, self._lines())

    def test_reinstall_no_duplicate(self):
        # (c) re-install A -> replaces, never duplicates
        _run(["install", self.A], self.fake)
        _run(["install", self.A], self.fake)
        _run(["install", self.A], self.fake)
        daily = [ln for ln in self._sc_lines()
                 if "self-company-daily" in ln and self.A in ln]
        research = [ln for ln in self._sc_lines()
                    if "self-company-research" in ln and self.A in ln]
        self.assertEqual(len(daily), 1)
        self.assertEqual(len(research), 1)

    def test_key_is_namespaced_on_line(self):
        _run(["install", self.A], self.fake)
        daily = next(ln for ln in self._sc_lines()
                     if "self-company-daily" in ln and self.A in ln)
        self.assertIn("project=", daily)
        self.assertIn("path=" + self.A, daily)


class TestLegacyMigration(ScheduleTestBase):
    def _legacy(self):
        # An old, un-namespaced pair for A's path (pre-Phase-7 format).
        daily = ("7 */6 * * * cd '%s' && bash '%s/.company/scripts/daily-run.sh' "
                 "'%s' >> '%s/.company/ops/logs/cron.log' 2>&1 # self-company-daily"
                 % (self.A, self.A, self.A, self.A))
        research = ("23 3 * * 0 cd '%s' && bash '%s/.company/scripts/research-scan.sh' "
                    "'%s' >> '%s/.company/ops/logs/cron.log' 2>&1 # self-company-research"
                    % (self.A, self.A, self.A, self.A))
        with open(self.fake, "w") as f:
            f.write(self.foreign + "\n" + daily + "\n" + research + "\n")

    def test_legacy_migrated_not_duplicated(self):
        # (d) legacy un-namespaced line for A migrated on install, not duplicated
        self._legacy()
        # sanity: the seed lines are legacy (no project=)
        self.assertTrue(any(ln.rstrip().endswith("# self-company-daily")
                            for ln in self._sc_lines()))
        _run(["install", self.A], self.fake)
        sc = self._sc_lines()
        self.assertEqual(len(sc), 2, sc)                     # not duplicated
        self.assertTrue(all("project=" in ln for ln in sc))  # upgraded
        self.assertFalse(any(ln.rstrip().endswith("# self-company-daily")
                             for ln in sc))                  # no legacy leftover
        self.assertIn(self.foreign, self._lines())

    def test_legacy_migrated_on_uninstall(self):
        self._legacy()
        _run(["uninstall", self.A], self.fake)
        # legacy lines for A removed even though they were un-namespaced
        self.assertEqual(self._sc_lines(), [])
        self.assertIn(self.foreign, self._lines())


class TestAutoStagger(ScheduleTestBase):
    def test_different_paths_different_minutes(self):
        _run(["install", self.A], self.fake)
        _run(["install", self.B], self.fake)
        self.assertNotEqual(self._daily_minute(self.A),
                            self._daily_minute(self.B))

    def test_minute_is_deterministic(self):
        _run(["install", self.A], self.fake)
        m1 = self._daily_minute(self.A)
        _run(["install", self.A], self.fake)
        m2 = self._daily_minute(self.A)
        self.assertEqual(m1, m2)
        self.assertTrue(0 <= int(m1) < 60)

    def test_explicit_override_wins(self):
        # (f) SELF_COMPANY_CRON_MIN=7 forces minute 7
        _run(["install", self.A], self.fake, extra_env={"SELF_COMPANY_CRON_MIN": "7"})
        self.assertEqual(self._daily_minute(self.A), "7")


class TestFleet(ScheduleTestBase):
    def test_list_shows_all_and_flags_orphan(self):
        # (e) list shows both; removing A's .company flags A as ORPHAN
        _run(["install", self.A], self.fake)
        _run(["install", self.B], self.fake)
        out = _run(["list"], self.fake).stdout
        self.assertIn(self.A, out)
        self.assertIn(self.B, out)
        self.assertNotIn("ORPHAN", out)

        subprocess.run(["rm", "-rf", os.path.join(self.A, ".company")])
        out2 = _run(["list"], self.fake).stdout
        a_row = next(ln for ln in out2.splitlines() if self.A in ln)
        b_row = next(ln for ln in out2.splitlines() if self.B in ln)
        self.assertIn("ORPHAN", a_row)
        self.assertNotIn("ORPHAN", b_row)

    def test_status_all_aliases_list(self):
        _run(["install", self.A], self.fake)
        _run(["install", self.B], self.fake)
        out = _run(["status", "--all"], self.fake).stdout
        self.assertIn(self.A, out)
        self.assertIn(self.B, out)
        self.assertIn("PROJECT PATH", out)

    def test_prune_removes_only_orphan(self):
        # (e) prune removes only the orphan (dead-path), never a live one/foreign
        _run(["install", self.A], self.fake)
        _run(["install", self.B], self.fake)
        subprocess.run(["rm", "-rf", os.path.join(self.A, ".company")])
        r = _run(["prune"], self.fake)
        self.assertEqual(r.returncode, 0)
        sc = self._sc_lines()
        self.assertEqual(len(sc), 2, sc)
        self.assertTrue(all(self.B in ln for ln in sc))
        self.assertFalse(any(self.A in ln for ln in sc))
        self.assertIn(self.foreign, self._lines())

    def test_prune_keeps_all_when_no_orphans(self):
        _run(["install", self.A], self.fake)
        _run(["install", self.B], self.fake)
        _run(["prune"], self.fake)
        self.assertEqual(len(self._sc_lines()), 4)


class TestFleetMode(ScheduleTestBase):
    """Phase 8 — holding-company fleet driver line (install-fleet)."""

    def _fleet_lines(self, path=None):
        return [ln for ln in self._sc_lines()
                if "self-company-fleet" in ln and (path is None or path in ln)]

    def _daily_lines(self, path):
        return [ln for ln in self._sc_lines()
                if "self-company-daily" in ln and path in ln]

    def _research_lines(self, path):
        return [ln for ln in self._sc_lines()
                if "self-company-research" in ln and path in ln]

    def _write_registry(self, parent, sub_paths):
        org = os.path.join(parent, ".company", "org")
        os.makedirs(org, exist_ok=True)
        rows = "".join("| %s | 1 | true |\n" % p for p in sub_paths)
        with open(os.path.join(org, "subsidiaries.md"), "w") as f:
            f.write("# Subsidiaries\n| path | weight | enabled |\n|---|---|---|\n" + rows)

    def test_install_fleet_one_line_no_daily(self):
        # (a) install-fleet -> exactly one fleet line + research line, NO daily line
        r = _run(["install-fleet", self.A], self.fake)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self._fleet_lines(self.A)), 1)
        self.assertEqual(len(self._research_lines(self.A)), 1)
        self.assertEqual(self._daily_lines(self.A), [])
        line = self._fleet_lines(self.A)[0]
        self.assertIn("fleet-run.sh", line)
        self.assertIn("project=", line)
        self.assertIn("path=" + self.A, line)
        # staggered minute is a valid 0..59
        minute = int(line.split()[0])
        self.assertTrue(0 <= minute < 60)

    def test_fleet_reinstall_no_duplicate(self):
        _run(["install-fleet", self.A], self.fake)
        _run(["install-fleet", self.A], self.fake)
        self.assertEqual(len(self._fleet_lines(self.A)), 1)
        self.assertEqual(len(self._research_lines(self.A)), 1)

    def test_fleet_coexists_with_plain_install(self):
        # (a) fleet P + plain A coexist; foreign intact
        _run(["install-fleet", self.A], self.fake)
        _run(["install", self.B], self.fake)
        self.assertEqual(len(self._fleet_lines(self.A)), 1)
        self.assertEqual(len(self._daily_lines(self.B)), 1)
        self.assertEqual(self._fleet_lines(self.B), [])   # B is not a fleet parent
        self.assertEqual(self._daily_lines(self.A), [])    # A is not a daily company
        self.assertIn(self.foreign, self._lines())

    def test_install_fleet_switches_mode(self):
        # plain install then install-fleet on the SAME key -> daily evicted, fleet in
        _run(["install", self.A], self.fake)
        self.assertEqual(len(self._daily_lines(self.A)), 1)
        _run(["install-fleet", self.A], self.fake)
        self.assertEqual(self._daily_lines(self.A), [])
        self.assertEqual(len(self._fleet_lines(self.A)), 1)
        # and back again
        _run(["install", self.A], self.fake)
        self.assertEqual(self._fleet_lines(self.A), [])
        self.assertEqual(len(self._daily_lines(self.A)), 1)

    def test_list_shows_type_column(self):
        # (b) list TYPE column: fleet vs daily
        _run(["install-fleet", self.A], self.fake)
        _run(["install", self.B], self.fake)
        out = _run(["list"], self.fake).stdout
        self.assertIn("TYPE", out)
        a_row = next(ln for ln in out.splitlines() if self.A in ln)
        b_row = next(ln for ln in out.splitlines() if self.B in ln)
        self.assertIn("fleet", a_row)
        self.assertIn("daily", b_row)

    def test_list_shows_subcount(self):
        # sub count read best-effort from subsidiaries.md; "-" when absent
        _run(["install-fleet", self.A], self.fake)
        out0 = _run(["list"], self.fake).stdout
        a0 = next(ln for ln in out0.splitlines() if self.A in ln)
        self.assertRegex(a0, r"fleet\s+\d+\s+\w+\s+-\s")   # SUBS="-" (no registry)
        self._write_registry(self.A, ["/tmp/sub-one", "/tmp/sub-two"])
        out1 = _run(["list"], self.fake).stdout
        a1 = next(ln for ln in out1.splitlines() if self.A in ln)
        self.assertRegex(a1, r"fleet\s+\d+\s+\w+\s+2\s")   # SUBS=2

    def test_uninstall_fleet_scoped(self):
        # (c) uninstall removes only the fleet parent's lines, leaves plain company
        _run(["install-fleet", self.A], self.fake)
        _run(["install", self.B], self.fake)
        _run(["uninstall", self.A], self.fake)
        self.assertEqual(self._fleet_lines(self.A), [])
        self.assertEqual(self._research_lines(self.A), [])
        self.assertEqual(len(self._daily_lines(self.B)), 1)
        self.assertIn(self.foreign, self._lines())

    def test_prune_scopes_fleet_orphan(self):
        # (c) prune drops an orphaned fleet parent, keeps a live plain company
        _run(["install-fleet", self.A], self.fake)
        _run(["install", self.B], self.fake)
        subprocess.run(["rm", "-rf", os.path.join(self.A, ".company")])
        _run(["prune"], self.fake)
        self.assertEqual(self._fleet_lines(self.A), [])
        self.assertEqual(self._research_lines(self.A), [])
        self.assertEqual(len(self._daily_lines(self.B)), 1)
        self.assertIn(self.foreign, self._lines())

    def test_status_reports_fleet_driver(self):
        _run(["install-fleet", self.A], self.fake)
        out = _run(["status", self.A], self.fake).stdout
        self.assertIn("INSTALLED", out)
        self.assertIn("self-company-fleet", out)
        self.assertNotIn("(daily: missing)", out)

    def test_foreign_untouched_full_fleet_lifecycle(self):
        # (e) real-crontab seam: foreign line survives a full fleet lifecycle
        _run(["install-fleet", self.A], self.fake)
        _run(["install", self.B], self.fake)
        _run(["uninstall", self.A], self.fake)
        _run(["prune"], self.fake)
        _run(["uninstall", self.B], self.fake)
        self.assertIn(self.foreign, self._lines())


class TestSingleProjectBackCompat(ScheduleTestBase):
    def test_status_single_project(self):
        r0 = _run(["status", self.A], self.fake)
        self.assertIn("not installed", r0.stdout)
        _run(["install", self.A], self.fake)
        r1 = _run(["status", self.A], self.fake)
        self.assertIn("INSTALLED", r1.stdout)
        self.assertIn(self.A, r1.stdout)
        # single-project status does not report the OTHER company
        _run(["install", self.B], self.fake)
        r2 = _run(["status", self.A], self.fake)
        self.assertNotIn(self.B, r2.stdout)

    def test_foreign_line_never_touched(self):
        # (g) a non-self-company line is never mutated across a full lifecycle
        _run(["install", self.A], self.fake)
        _run(["install", self.B], self.fake)
        _run(["uninstall", self.A], self.fake)
        _run(["prune"], self.fake)
        _run(["uninstall", self.B], self.fake)
        self.assertIn(self.foreign, self._lines())

    def test_bad_command_exits_2(self):
        self.assertEqual(_run(["bogus", "/tmp"], self.fake).returncode, 2)

    def test_install_without_company_errors(self):
        d = os.path.join(self.tmp, "nocompany")
        os.makedirs(d)
        r = _run(["install", d], self.fake)
        self.assertEqual(r.returncode, 1)
        self.assertIn(".company not found", r.stderr)


if __name__ == "__main__":
    unittest.main()
