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


def _bash(args, env=None, **kw):
    return subprocess.run(["bash", *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL,
                          env={**os.environ, **(env or {})}, **kw)


def _fresh_project():
    """Make a temp project with a real .company (via init), return its path."""
    d = tempfile.mkdtemp()
    for sub in ("assets", "scripts"):
        subprocess.run(["cp", "-r", os.path.join(REPO, "skills", "self-company", sub), d], check=True)
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
            r = _bash([os.path.join(REPO, "skills", "self-company", "scripts","daily-run.sh"), d, "--no-agent"])
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

    def test_token_breaker_caps_agent(self):
        # B1: with the per-day agent-run counter already past the cap, the agent
        # step is skipped (no claude call) — proven hermetically by pre-maxing it.
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            date = subprocess.check_output(["date", "+%F"], text=True).strip()
            logs = os.path.join(company, "ops", "logs")
            os.makedirs(logs, exist_ok=True)
            with open(os.path.join(logs, f".agent_runs_{date}"), "w") as f:
                f.write("99\n")
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d])  # agent ON by default
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(logs, f"daily-{date}.md")) as f:
                text = f.read()
            self.assertIn("cap reached", text)
            # the agent must NOT have been invoked (no audit log created)
            self.assertFalse(os.path.exists(os.path.join(logs, f"agent-{date}.log")))
        finally:
            subprocess.run(["rm", "-rf", d])


def _today():
    return subprocess.check_output(["date", "+%F"], text=True).strip()


def _read_log(company):
    with open(os.path.join(company, "ops", "logs", f"daily-{_today()}.md")) as f:
        return f.read()


def _fake_rag_venv(company, script_body):
    """Plant a fake .company/.rag-venv/bin/python so the reinforce step 'runs'."""
    bindir = os.path.join(company, ".rag-venv", "bin")
    os.makedirs(bindir, exist_ok=True)
    py = os.path.join(bindir, "python")
    with open(py, "w") as f:
        f.write("#!/usr/bin/env bash\n" + script_body)
    os.chmod(py, 0o755)
    return py


def _fake_claude(d):
    """Plant a fake `claude` CLI that passes the auth probe and dumps the -p
    prompt to $FAKE_CLAUDE_PROMPT_FILE. Returns the dir to prepend to PATH."""
    bindir = os.path.join(d, "fakebin")
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, "claude")
    with open(path, "w") as f:
        f.write(
            '#!/usr/bin/env bash\n'
            'if [[ "${1:-}" == "auth" ]]; then echo \'{"loggedIn": true}\'; exit 0; fi\n'
            'while (($#)); do\n'
            '  if [[ "$1" == "-p" ]]; then shift; printf \'%s\' "$1" > "$FAKE_CLAUDE_PROMPT_FILE"; fi\n'
            '  shift\n'
            'done\nexit 0\n')
    os.chmod(path, 0o755)
    return bindir


class TestDailyRunReinforce(unittest.TestCase):
    """P4 Item 2: reinforce_memory.py wired into the deterministic core."""

    def test_venv_absent_one_line_skip_core_completes(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- reinforce: skipped — RAG venv absent", text)
            self.assertIn("- decay --apply:", text)   # core unaffected
            self.assertIn("- entropy", text)
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_reinforce_log_line_and_apply_flag(self):
        # fake venv python: record argv, emit a canned reinforce JSON
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            argfile = os.path.join(d, "reinf_args.txt")
            _fake_rag_venv(company, (
                f'printf \'%s \' "$@" > "{argfile}"\n'
                'echo \'{"applied": true, "threshold": 0.85, "reinforcements": '
                '[{"canonical": "a", "absorbed": "b", "canonical_tier": "L0", "score": 0.95}], '
                '"skipped_l2": [{"pair": ["c", "d"], "score": 0.93}], "scanned": 7}\'\n'))
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- reinforce --apply: absorbed 1 | skipped-L2 1 (scanned 7", text)
            with open(argfile) as f:
                args = f.read()
            self.assertIn("reinforce_memory.py", args)
            self.assertIn("--apply", args)
            self.assertNotIn("--threshold", args)  # never lower the default
            # reinforce must run BEFORE decay: its log line comes first
            self.assertLess(text.index("- reinforce"), text.index("- decay"))
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_dry_run_does_not_pass_apply(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            argfile = os.path.join(d, "reinf_args.txt")
            _fake_rag_venv(company, (
                f'printf \'%s \' "$@" > "{argfile}"\n'
                'echo \'{"applied": false, "threshold": 0.85, "reinforcements": [], '
                '"skipped_l2": [], "scanned": 1}\'\n'))
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--dry-run"])
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(argfile) as f:
                self.assertNotIn("--apply", f.read())
            self.assertIn("- reinforce: absorbed 0", _read_log(company))
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_reinforce_failure_never_aborts_core(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _fake_rag_venv(company, 'echo "boom" >&2\nexit 1\n')
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- reinforce: no output (errored) — deterministic core continues", text)
            self.assertIn("- decay --apply:", text)
            self.assertIn("- entropy", text)
        finally:
            subprocess.run(["rm", "-rf", d])


class TestDailyRunAgentPrompt(unittest.TestCase):
    """P4 Item 4: agent prompt aimed at the measured backlog."""

    BODY = "the chairman prefers dark terminal themes for late night garage work"

    def _write_body_mem(self, company, mid, body):
        p = os.path.join(company, "memory", "L0-working", f"{mid}.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(
                f"---\nid: {mid}\ntier: L0\nowner: Tony\nsources: [\"[s#1]\"]\n"
                f"created: 2026-06-01\nlast_reinforced: {_today()}\n"
                f"reinforce_count: 1\ndecay_score: 1.0\nstatus: active\n---\n{body}\n")

    def _run_with_fake_claude(self, d):
        promptfile = os.path.join(d, "prompt.txt")
        env = {"PATH": _fake_claude(d) + os.pathsep + os.environ["PATH"],
               "FAKE_CLAUDE_PROMPT_FILE": promptfile}
        r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d], env=env)
        return r, promptfile

    def test_prompt_injects_measured_backlog(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            # identical bodies -> Jaccard 1.0 -> scored duplicate pair (no RAG needed)
            self._write_body_mem(company, "pref-dark-theme-one", self.BODY)
            self._write_body_mem(company, "pref-dark-theme-two", self.BODY)
            r, promptfile = self._run_with_fake_claude(d)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(promptfile) as f:
                prompt = f.read()
            self.assertIn("PAIR BY PAIR", prompt)
            self.assertIn("SCORED DUPLICATE pairs", prompt)
            self.assertIn("pref-dark-theme-one", prompt)
            self.assertIn("pref-dark-theme-two", prompt)
            # exact adjudication row format quoted
            self.assertIn("| <id_a> | <id_b> | distinct | Tony |", prompt)
            self.assertIn(".company/ops/adjudications.md", prompt)
            # injection hygiene + budget: ids only, sane size
            self.assertNotIn(self.BODY, prompt)          # bodies never embedded
            self.assertLess(len(prompt), 8000)
            # Tony-proposal tail unchanged
            self.assertIn("as TONY", prompt)
            self.assertIn("measured backlog injected", _read_log(company))
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_prompt_generic_when_no_candidates(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            self._write_body_mem(company, "pref-dark-theme-one", self.BODY)
            r, promptfile = self._run_with_fake_claude(d)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(promptfile) as f:
                prompt = f.read()
            self.assertIn("Read L0-working memories", prompt)   # today's generic text
            self.assertNotIn("SCORED DUPLICATE pairs", prompt)
            self.assertIn("agent prompt: generic", _read_log(company))
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_auth_fail_path_unchanged(self):
        # AUTH pre-flight still short-circuits BEFORE any prompt build/agent call.
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            promptfile = os.path.join(d, "prompt.txt")
            env = {"PATH": _fake_claude(d) + os.pathsep + os.environ["PATH"],
                   "FAKE_CLAUDE_PROMPT_FILE": promptfile,
                   "SELF_COMPANY_FORCE_AUTH_FAIL": "1"}
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("AUTH_FAIL x1", text)
            self.assertFalse(os.path.exists(promptfile))     # agent never invoked
            marker = os.path.join(company, "ops", "auth-fail.marker")
            self.assertTrue(os.path.exists(marker))
            with open(marker) as f:
                self.assertIn("reason=auth", f.read())
        finally:
            subprocess.run(["rm", "-rf", d])


class TestInstallHook(unittest.TestCase):
    SH = os.path.join(REPO, "skills", "self-company", "scripts","install-hook.sh")

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
        r = _bash([os.path.join(REPO, "skills", "self-company", "scripts","schedule.sh"), "bogus", "/tmp"])
        self.assertEqual(r.returncode, 2)

    def test_install_without_company_errors(self):
        with tempfile.TemporaryDirectory() as d:
            r = _bash([os.path.join(REPO, "skills", "self-company", "scripts","schedule.sh"), "install", d])
            self.assertEqual(r.returncode, 1)
            self.assertIn(".company not found", r.stderr)


class TestSkeletonGuard(unittest.TestCase):
    SH = os.path.join(REPO, "skills", "self-company", "scripts","skeleton_guard.sh")

    def test_dev_marker_allows(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, ".self-company-dev"), "w").close()
            self.assertEqual(_bash([self.SH, d]).returncode, 0)

    def test_usage_mode_locked(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(_bash([self.SH, d]).returncode, 1)

    def test_chairman_override_allows(self):
        with tempfile.TemporaryDirectory() as d:
            r = _bash([self.SH, d], env={"SELF_COMPANY_ALLOW_SKELETON": "1"})
            self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
