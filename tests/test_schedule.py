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
import time
import unittest

import _helpers

REPO = _helpers.REPO_ROOT
SH = os.path.join(REPO, "plugin", "skills", "self-company", "scripts", "schedule.sh")


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


GUARD = os.path.join(REPO, "plugin", "skills", "self-company", "scripts", "hook_schedule_guard.sh")


class TestScheduleConfigTick(ScheduleTestBase):
    """Phase 12 — schedule.sh reads the tick from org/schedule.yaml, but only
    after the validator passes; an invalid config keeps the default tick."""

    def _write_cfg(self, project, body):
        org = os.path.join(project, ".company", "org")
        os.makedirs(org, exist_ok=True)
        with open(os.path.join(org, "schedule.yaml"), "w") as f:
            f.write(body)

    def _daily_line(self, path):
        return next(ln for ln in self._sc_lines()
                    if "self-company-daily" in ln and path in ln)

    def test_valid_cadence_changes_installed_tick(self):
        self._write_cfg(self.A, "cadence: every 2h\n")
        r = _run(["install", self.A], self.fake)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = self._daily_line(self.A)
        self.assertIn("*/2 * * *", line)          # config tick honored
        self.assertNotIn("*/6 * * *", line)       # ...not the default

    def test_invalid_config_keeps_default_tick(self):
        # R1 violation (gibby can't build) -> validator refuses -> default */6.
        self._write_cfg(self.A, "cadence: every 2h\ngibby: { duties: [build] }\n")
        r = _run(["install", self.A], self.fake)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = self._daily_line(self.A)
        self.assertIn("*/6 * * *", line)
        self.assertNotIn("*/2", line)

    def test_bad_cadence_falls_back_to_default(self):
        self._write_cfg(self.A, "cadence: banana\n")
        r = _run(["install", self.A], self.fake)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("*/6 * * *", self._daily_line(self.A))

    def test_research_cadence_from_config(self):
        self._write_cfg(self.A, "research: { enabled: true, cadence: weekly-mon-05 }\n")
        _run(["install", self.A], self.fake)
        research = next(ln for ln in self._sc_lines()
                        if "self-company-research" in ln and self.A in ln)
        self.assertIn("5 * * 1", research)        # Monday 05:00

    # --- P9-D2: a junk / injected raw cadence must never reach the crontab ------
    def test_raw_cron_junk_keeps_default_no_bad_line(self):
        self._write_cfg(self.A, "cadence: GARBAGE foo bar baz qux\n")
        r = _run(["install", self.A], self.fake)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("*/6 * * *", self._daily_line(self.A))   # default tick kept
        self.assertNotIn("GARBAGE", self._read())              # junk never written
        self.assertEqual(len(self._sc_lines()), 2)             # exactly daily+research
        self.assertIn(self.foreign, self._lines())             # neighbours intact

    def test_raw_cron_newline_never_injects_extra_line(self):
        # An embedded newline would split the crontab into an extra (injected) line.
        self._write_cfg(self.A, 'cadence: "a b c d\\ne"\n')
        r = _run(["install", self.A], self.fake)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self._sc_lines()), 2)             # no 3rd injected line
        self.assertIn("*/6 * * *", self._daily_line(self.A))
        self.assertIn(self.foreign, self._lines())

    def test_every_self_company_line_has_5_field_time(self):
        # Structural guard: whatever the config, each installed line begins with a
        # well-formed 5-field cron time (never junk that `crontab -` would reject).
        self._write_cfg(self.A, "cadence: GARBAGE foo bar baz qux\n")
        _run(["install", self.A], self.fake)
        for ln in self._sc_lines():
            fields = ln.split()[:5]
            self.assertEqual(len(fields), 5)
            for f in fields:
                self.assertRegex(f, r"^[0-9*/,\-]+$", ln)

    def test_semantically_invalid_cron_keeps_default(self):
        # P9-D3: an out-of-range but charset-clean cadence must not reach the
        # crontab — config_py returns rc 2, schedule.sh keeps the default tick.
        for body in ("cadence: 99 99 99 99 99\n", 'cadence: ",, * * * *"\n',
                     "cadence: */0 * * * *\n"):
            self._write_cfg(self.A, body)
            _run(["uninstall", self.A], self.fake)
            r = _run(["install", self.A], self.fake)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("*/6 * * *", self._daily_line(self.A), body)
            self.assertEqual(len(self._sc_lines()), 2, body)
            # Scope the junk-marker checks to the cron TIME FIELDS (first 5 tokens)
            # only — the rest of the line embeds the random mkdtemp() project path,
            # which can itself contain "99" and cause a false failure (P9-D4).
            cron_fields = " ".join(self._daily_line(self.A).split()[:5])
            for junk in ("99", ",,", "*/0"):
                self.assertNotIn(junk, cron_fields, body)
            self.assertIn(self.foreign, self._lines())


class TestScheduleGuardSync(ScheduleTestBase):
    """Phase 12 Item 4 — the SessionStart guard syncs a TICK change into the
    crontab (fake backend) but ignores per-employee sub-cadence edits, and never
    syncs a rejected config."""

    def _write_cfg(self, project, body):
        org = os.path.join(project, ".company", "org")
        os.makedirs(org, exist_ok=True)
        with open(os.path.join(org, "schedule.yaml"), "w") as f:
            f.write(body)

    def _marker(self, project):
        return os.path.join(project, ".company", "ops", "schedule", ".installed-tick")

    def _run_guard(self, project, extra_env=None):
        env = {**os.environ, "SELF_COMPANY_CRONTAB_FILE": self.fake,
               "CLAUDE_PROJECT_DIR": project}
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["bash", GUARD], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=env)

    def _daily_line(self, path):
        return next(ln for ln in self._sc_lines()
                    if "self-company-daily" in ln and path in ln)

    def test_no_schedule_yaml_installed_syncs_and_converges(self):
        # Phase 12b gap fix: a DEFAULT-schedule company (no schedule.yaml) that is
        # already installed still gets the scripts-dir self-heal. The first guard
        # run writes the marker (default-derived sig) and converges; a second run
        # does NOT churn. (Old behaviour short-circuited before this and never
        # healed a default-schedule cron after a plugin move.)
        _run(["install", self.A], self.fake)
        r = self._run_guard(self.A)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self._marker(self.A)))     # marker written
        r2 = self._run_guard(self.A)                              # settled -> no churn
        self.assertEqual(r2.returncode, 0)
        self.assertNotIn("re-installed", r2.stderr)
        self.assertEqual(len(self._sc_lines()), 2)               # daily+research, no dup

    def test_tick_change_reinstalls_and_writes_marker(self):
        self._write_cfg(self.A, "cadence: every 2h\n")
        _run(["install", self.A], self.fake)
        r = self._run_guard(self.A)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self._marker(self.A)))
        with open(self._marker(self.A)) as f:
            self.assertIn("*/2", f.read())
        self.assertIn("*/2", self._daily_line(self.A))

    def test_subcadence_change_leaves_marker_untouched(self):
        self._write_cfg(self.A, "cadence: every 2h\ntony: { cadence: every-run }\n")
        _run(["install", self.A], self.fake)
        self._run_guard(self.A)
        with open(self._marker(self.A)) as f:
            sig1 = f.read()
        # change ONLY a per-employee sub-cadence -> tick signature is identical
        self._write_cfg(self.A, "cadence: every 2h\ntony: { cadence: daily }\n")
        self._run_guard(self.A)
        with open(self._marker(self.A)) as f:
            sig2 = f.read()
        self.assertEqual(sig1, sig2)

    def test_rejected_config_is_not_synced(self):
        self._write_cfg(self.A, "gibby: { duties: [build] }\n")
        _run(["install", self.A], self.fake)
        r = self._run_guard(self.A)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self._marker(self.A)))   # refused -> no sync
        self.assertIn("REJECTED", r.stderr)

    def test_uninstalled_project_not_auto_installed(self):
        # A company with a config but never scheduled is NOT auto-installed.
        self._write_cfg(self.A, "cadence: every 2h\n")
        r = self._run_guard(self.A)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._sc_lines(), [])
        self.assertFalse(os.path.exists(self._marker(self.A)))

    def test_foreign_line_untouched_by_guard(self):
        self._write_cfg(self.A, "cadence: every 2h\n")
        _run(["install", self.A], self.fake)
        self._run_guard(self.A)
        self.assertIn(self.foreign, self._lines())


class TestScheduleGuardSelfHeal(ScheduleTestBase):
    """Phase 12b — the SessionStart guard self-heals the cron after a PLUGIN
    UPDATE/MOVE: the crontab carries an absolute scripts-dir snapshot, so when the
    resolver points at a new dir (simulated via CLAUDE_PLUGIN_ROOT) the guard
    re-points the daily line with no manual step. All via the fake-crontab seam."""

    REAL_SCRIPTS = os.path.join(REPO, "plugin", "skills", "self-company", "scripts")

    def _write_cfg(self, project, body):
        org = os.path.join(project, ".company", "org")
        os.makedirs(org, exist_ok=True)
        with open(os.path.join(org, "schedule.yaml"), "w") as f:
            f.write(body)

    def _marker(self, project):
        return os.path.join(project, ".company", "ops", "schedule", ".installed-tick")

    def _run_guard(self, project, extra_env=None):
        env = {**os.environ, "SELF_COMPANY_CRONTAB_FILE": self.fake,
               "CLAUDE_PROJECT_DIR": project}
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["bash", GUARD], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=env)

    def _daily_line(self, path):
        return next(ln for ln in self._sc_lines()
                    if "self-company-daily" in ln and path in ln)

    def _fake_plugin_root(self):
        """A fake plugin root whose skills/self-company/scripts is a full copy of
        the real scripts dir — simulates the post-update on-disk layout."""
        y = os.path.join(self.tmp, "newplugin")
        dest = os.path.join(y, "skills", "self-company", "scripts")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        subprocess.run(["cp", "-r", self.REAL_SCRIPTS, dest], check=True)
        return y, dest

    def test_scripts_dir_query_honors_plugin_root(self):
        # The read-only ground-truth query resolves the dir install would embed.
        r0 = _run(["scripts-dir", self.A], self.fake)
        self.assertEqual(r0.returncode, 0, r0.stderr)
        self.assertEqual(r0.stdout.strip(), self.REAL_SCRIPTS)
        y, dest = self._fake_plugin_root()
        r1 = _run(["scripts-dir", self.A], self.fake,
                  extra_env={"CLAUDE_PLUGIN_ROOT": y})
        self.assertEqual(r1.stdout.strip(), dest)

    def test_plugin_update_reinstalls_to_new_scripts_dir(self):
        self._write_cfg(self.A, "cadence: every 2h\n")
        _run(["install", self.A], self.fake)
        self._run_guard(self.A)                       # sync marker to the real dir
        self.assertIn(self.REAL_SCRIPTS, self._daily_line(self.A))
        # simulate a plugin update: resolver now points at Y
        y, dest = self._fake_plugin_root()
        r = self._run_guard(self.A, extra_env={"CLAUDE_PLUGIN_ROOT": y})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(dest, self._daily_line(self.A))          # re-pointed to Y
        self.assertNotIn(self.REAL_SCRIPTS + "/daily-run.sh", self._daily_line(self.A))
        self.assertEqual(len(self._sc_lines()), 2)             # no dup / evict
        self.assertIn(self.foreign, self._lines())             # neighbour intact
        with open(self._marker(self.A)) as f:
            self.assertIn(dest, f.read())                      # marker healed

    def test_plugin_update_is_one_shot_no_churn(self):
        self._write_cfg(self.A, "cadence: every 2h\n")
        _run(["install", self.A], self.fake)
        self._run_guard(self.A)
        y, dest = self._fake_plugin_root()
        self._run_guard(self.A, extra_env={"CLAUDE_PLUGIN_ROOT": y})   # heals
        # a SECOND run at the SAME new dir must NOT re-install (no churn)
        r = self._run_guard(self.A, extra_env={"CLAUDE_PLUGIN_ROOT": y})
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("re-installed", r.stderr)
        self.assertEqual(len(self._sc_lines()), 2)

    def test_same_path_same_tick_no_reinstall(self):
        self._write_cfg(self.A, "cadence: every 2h\n")
        _run(["install", self.A], self.fake)
        self._run_guard(self.A)                        # first run heals + marks
        r = self._run_guard(self.A)                    # second run: nothing changed
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("re-installed", r.stderr)

    def test_tick_change_still_reinstalls(self):
        # Phase-12 behavior preserved: a tick edit alone still re-installs.
        self._write_cfg(self.A, "cadence: every 2h\n")
        _run(["install", self.A], self.fake)
        self._run_guard(self.A)
        self._write_cfg(self.A, "cadence: every 3h\n")
        r = self._run_guard(self.A)
        self.assertIn("re-installed", r.stderr)
        self.assertIn("*/3", self._daily_line(self.A))

    def test_uninstalled_project_not_auto_installed_on_plugin_update(self):
        # An opted-out company is never auto-scheduled, even on a plugin update.
        self._write_cfg(self.A, "cadence: every 2h\n")
        y, dest = self._fake_plugin_root()
        r = self._run_guard(self.A, extra_env={"CLAUDE_PLUGIN_ROOT": y})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._sc_lines(), [])
        self.assertFalse(os.path.exists(self._marker(self.A)))

    def test_old_two_field_marker_migrates_once(self):
        # A pre-12b marker (2 fields, no scripts dir) must self-heal exactly once.
        self._write_cfg(self.A, "cadence: every 2h\n")
        _run(["install", self.A], self.fake)
        os.makedirs(os.path.dirname(self._marker(self.A)), exist_ok=True)
        with open(self._marker(self.A), "w") as f:
            f.write("M */2 * * *|M 3 * * 0\n")          # legacy 2-field format
        r = self._run_guard(self.A)
        self.assertIn("re-installed", r.stderr)         # migrated once
        with open(self._marker(self.A)) as f:
            self.assertIn(self.REAL_SCRIPTS, f.read())  # upgraded to 3-field
        # ...and now converged: a second run does not churn
        r2 = self._run_guard(self.A)
        self.assertNotIn("re-installed", r2.stderr)


class TestScheduleGuardSelfHealNoYaml(ScheduleTestBase):
    """Phase 12b gap fix — the scripts-dir self-heal must reach a DEFAULT-schedule
    company (NO org/schedule.yaml) too, since a plugin move breaks its cron path
    identically to a configured company. This is exactly how self-company's own
    cron silently broke. All via the fake-crontab seam; the real crontab is never
    touched. NO schedule.yaml is ever written in this class."""

    REAL_SCRIPTS = os.path.join(REPO, "plugin", "skills", "self-company", "scripts")

    def _marker(self, project):
        return os.path.join(project, ".company", "ops", "schedule", ".installed-tick")

    def _run_guard(self, project, extra_env=None):
        env = {**os.environ, "SELF_COMPANY_CRONTAB_FILE": self.fake,
               "CLAUDE_PROJECT_DIR": project}
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["bash", GUARD], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=env)

    def _daily_line(self, path):
        return next(ln for ln in self._sc_lines()
                    if "self-company-daily" in ln and path in ln)

    def _fake_plugin_root(self):
        y = os.path.join(self.tmp, "newplugin")
        dest = os.path.join(y, "skills", "self-company", "scripts")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        subprocess.run(["cp", "-r", self.REAL_SCRIPTS, dest], check=True)
        return y, dest

    # (a) no schedule.yaml + stale installed scripts dir -> re-installs to current
    def test_no_yaml_stale_path_reinstalls_to_current_dir(self):
        _run(["install", self.A], self.fake)
        self._run_guard(self.A)                          # settle marker at real dir
        self.assertIn(self.REAL_SCRIPTS, self._daily_line(self.A))
        # simulate a plugin update: resolver now points at Y (no yaml written)
        y, dest = self._fake_plugin_root()
        r = self._run_guard(self.A, extra_env={"CLAUDE_PLUGIN_ROOT": y})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(dest, self._daily_line(self.A))               # healed to Y
        self.assertNotIn(self.REAL_SCRIPTS + "/daily-run.sh", self._daily_line(self.A))
        self.assertEqual(len(self._sc_lines()), 2)                 # exactly one pair
        self.assertIn(self.foreign, self._lines())                 # neighbour intact
        with open(self._marker(self.A)) as f:
            self.assertIn(dest, f.read())                          # marker healed

    # (b) no schedule.yaml + already-correct path -> no churn (idempotent)
    def test_no_yaml_correct_path_no_churn(self):
        _run(["install", self.A], self.fake)
        self._run_guard(self.A)                          # first run heals + marks
        r = self._run_guard(self.A)                      # nothing changed
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("re-installed", r.stderr)
        self.assertEqual(len(self._sc_lines()), 2)

    # (b') the self-heal is one-shot: after healing to Y, a second run at Y no-ops
    def test_no_yaml_self_heal_is_one_shot(self):
        _run(["install", self.A], self.fake)
        self._run_guard(self.A)
        y, dest = self._fake_plugin_root()
        self._run_guard(self.A, extra_env={"CLAUDE_PLUGIN_ROOT": y})   # heals
        r = self._run_guard(self.A, extra_env={"CLAUDE_PLUGIN_ROOT": y})
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("re-installed", r.stderr)
        self.assertEqual(len(self._sc_lines()), 2)

    # (c) uninstalled project (no yaml) -> guard never auto-installs
    def test_no_yaml_uninstalled_project_not_auto_installed(self):
        y, dest = self._fake_plugin_root()
        r = self._run_guard(self.A, extra_env={"CLAUDE_PLUGIN_ROOT": y})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._sc_lines(), [])                     # nothing installed
        self.assertFalse(os.path.exists(self._marker(self.A)))
        self.assertIn(self.foreign, self._lines())                 # neighbour intact

    # foreign / other-company lines are never disturbed by a no-yaml self-heal
    def test_no_yaml_other_company_untouched(self):
        _run(["install", self.A], self.fake)
        _run(["install", self.B], self.fake)
        self._run_guard(self.A)
        y, dest = self._fake_plugin_root()
        self._run_guard(self.A, extra_env={"CLAUDE_PLUGIN_ROOT": y})   # heal only A
        # B's daily line is untouched (still at the real dir), foreign intact
        b_daily = next(ln for ln in self._sc_lines()
                       if "self-company-daily" in ln and self.B in ln)
        self.assertIn(self.REAL_SCRIPTS, b_daily)
        self.assertIn(self.foreign, self._lines())

    # (d) the REAL crontab binary is never invoked while the file seam is active
    def test_no_yaml_real_crontab_never_invoked(self):
        _run(["install", self.A], self.fake)
        # A fake `crontab` on PATH that trips a sentinel if it is ever executed.
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        tripwire = os.path.join(self.tmp, "REAL_CRONTAB_TOUCHED")
        shim = os.path.join(bindir, "crontab")
        with open(shim, "w") as f:
            f.write("#!/usr/bin/env bash\ntouch '%s'\nexit 0\n" % tripwire)
        os.chmod(shim, 0o755)
        y, dest = self._fake_plugin_root()
        r = self._run_guard(self.A, extra_env={
            "CLAUDE_PLUGIN_ROOT": y,
            "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
        })
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(tripwire),
                         "guard shelled the real crontab binary despite the file seam")
        self.assertIn(dest, self._daily_line(self.A))              # still healed via file


class TestCronModeWiring(ScheduleTestBase):
    """Phase 19 Item 2 — the INSTALLED daily cron line runs in cron mode (--cron),
    so an overlapping tick takes the non-blocking flock SKIP path in production
    (not the manual block-and-wait that would queue + re-spawn an agent). Closes
    the shipped-artifact coverage gap: the installed line's cron behavior is now
    actually exercised, not just the --cron seam in isolation."""

    def _daily_line(self, path):
        return next(ln for ln in self._sc_lines()
                    if "self-company-daily" in ln and path in ln)

    def test_installed_daily_line_runs_in_cron_mode(self):
        _run(["install", self.A], self.fake)
        daily = self._daily_line(self.A)
        # --cron sits after the project-dir arg and before the redirect
        self.assertIn("daily-run.sh' '%s' --cron >>" % self.A, daily)
        # research + fleet lines must NOT carry --cron (no --apply, no lock needed)
        research = next(ln for ln in self._sc_lines()
                        if "self-company-research" in ln and self.A in ln)
        self.assertNotIn("--cron", research)

    def test_installed_line_cron_behavior_skips_when_lock_held(self):
        # Run the EXACT command the crontab would run (extracted from the installed
        # line) with the mutating-core lock held: it must take the cron SKIP path.
        _run(["install", self.A], self.fake)
        daily = self._daily_line(self.A)
        # strip the leading 5 cron time fields -> the shell command cron executes
        # (the trailing ' # self-company-daily …' is a harmless shell comment).
        runner = daily.split(None, 5)[5]
        company = os.path.join(self.A, ".company")
        ops = os.path.join(company, "ops")
        os.makedirs(ops, exist_ok=True)
        lock = os.path.join(ops, ".daily.lock")
        ready = os.path.join(self.A, "lock.ready")
        holder = subprocess.Popen(
            ["bash", "-c", f'exec 9>"{lock}"; flock 9; : > "{ready}"; sleep 5'])
        try:
            for _ in range(200):
                if os.path.exists(ready):
                    break
                time.sleep(0.02)
            r = subprocess.run(["bash", "-c", runner], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, r.stderr)
            date = subprocess.check_output(["date", "+%F"], text=True).strip()
            with open(os.path.join(ops, "logs", f"daily-{date}.md")) as f:
                self.assertIn("cron tick SKIPPED", f.read())
        finally:
            holder.wait()


class TestScheduleGuardLock(ScheduleTestBase):
    """Phase 19 C3 (GIB-S3) — the SessionStart guard's crontab read-modify-write
    is serialized by an flock so two concurrent SessionStarts cannot interleave it;
    convergence is preserved, and flock-absent degrades to the unlocked path."""

    def _write_cfg(self, project, body):
        org = os.path.join(project, ".company", "org")
        os.makedirs(org, exist_ok=True)
        with open(os.path.join(org, "schedule.yaml"), "w") as f:
            f.write(body)

    def _marker(self, project):
        return os.path.join(project, ".company", "ops", "schedule", ".installed-tick")

    def _guard_popen(self, project, extra_env=None):
        env = {**os.environ, "SELF_COMPANY_CRONTAB_FILE": self.fake,
               "CLAUDE_PROJECT_DIR": project}
        if extra_env:
            env.update(extra_env)
        return subprocess.Popen(["bash", GUARD], stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, env=env)

    def test_guard_creates_lockfile(self):
        self._write_cfg(self.A, "cadence: every 2h\n")
        _run(["install", self.A], self.fake)
        self._guard_popen(self.A).communicate()
        self.assertTrue(os.path.exists(
            os.path.join(self.A, ".company", "ops", "schedule", ".guard.lock")))

    def test_concurrent_guards_converge_no_corruption(self):
        # Two guards fire at once against the same fake crontab after a tick change.
        # The flock serializes their RMW: the crontab converges to exactly the two
        # self-company lines (no dup, no split), and the foreign line survives.
        self._write_cfg(self.A, "cadence: every 2h\n")
        _run(["install", self.A], self.fake)
        self._guard_popen(self.A).communicate()          # settle the marker
        self._write_cfg(self.A, "cadence: every 3h\n")   # signature change
        procs = [self._guard_popen(self.A) for _ in range(4)]
        for p in procs:
            p.communicate()
        self.assertEqual(len(self._sc_lines()), 2, self._read())   # daily+research, no dup
        self.assertIn("*/3", self._daily_line(self.A))
        self.assertIn(self.foreign, self._lines())                 # neighbour intact
        # at most one guard actually re-installed (the rest saw the fresh marker)
        with open(self._marker(self.A)) as f:
            self.assertIn("*/3", f.read())

    def test_flock_absent_still_syncs(self):
        # SELF_COMPANY_NO_FLOCK=1 => unlocked path, but the sync still converges.
        self._write_cfg(self.A, "cadence: every 2h\n")
        _run(["install", self.A], self.fake)
        p = self._guard_popen(self.A, extra_env={"SELF_COMPANY_NO_FLOCK": "1"})
        _, err = p.communicate()
        self.assertEqual(p.returncode, 0)
        self.assertTrue(os.path.exists(self._marker(self.A)))
        self.assertEqual(len(self._sc_lines()), 2)

    def _daily_line(self, path):
        return next(ln for ln in self._sc_lines()
                    if "self-company-daily" in ln and path in ln)


if __name__ == "__main__":
    unittest.main()
