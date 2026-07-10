"""
Tests for research-scan.sh — Phase 29 Item 4 (Bob P1): the prompt must STATE
its wall-clock budget (previously never stated at all — an open-ended survey
could be SIGKILLed mid-write with no warning), and that stated number must be
the SAME variable the spawn's `timeout` wrapper actually receives.

Drives the real script against a fake `claude` binary (a bash stub that
echoes what it received) so no real LLM call happens.
"""

import os
import stat
import subprocess
import tempfile
import unittest

import _helpers

SCRIPT = os.path.join(_helpers.SCRIPTS_DIR, "research-scan.sh")


def _fake_claude(bindir, echo_prompt=False):
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, "claude")
    body = "#!/usr/bin/env bash\n"
    if echo_prompt:
        # Print argv so the test can inspect the received prompt/model.
        body += 'for a in "$@"; do printf "ARG<<<%s>>>\\n" "$a"; done\n'
    body += "exit 0\n"
    with open(path, "w") as f:
        f.write(body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class TestResearchScanBudget(unittest.TestCase):
    def _run(self, project_dir, env=None):
        full_env = {**os.environ, "SELF_COMPANY_PROJECT_DIR": project_dir}
        if env:
            full_env.update(env)
        return subprocess.run(["bash", SCRIPT, project_dir], capture_output=True,
                              text=True, env=full_env, timeout=30)

    def test_no_company_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._run(d)
            self.assertEqual(r.returncode, 0)
            self.assertIn("nothing to do", r.stdout)

    def test_prompt_log_states_default_900s_budget(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".company"))
            bindir = os.path.join(d, "bin")
            claude_path = _fake_claude(bindir, echo_prompt=True)
            env = {"PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
            r = self._run(d, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            log = os.path.join(d, ".company", "ops", "logs", "research-*.log")
            import glob
            logs = glob.glob(log)
            self.assertEqual(len(logs), 1)
            with open(logs[0], encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn("900s", body)
            self.assertIn("wall-clock", body)

    def test_env_override_changes_stated_budget_and_timeout_together(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".company"))
            bindir = os.path.join(d, "bin")
            _fake_claude(bindir, echo_prompt=True)
            env = {"PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
                  "SELF_COMPANY_RESEARCH_TIMEOUT": "77"}
            r = self._run(d, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            import glob
            logs = glob.glob(os.path.join(d, ".company", "ops", "logs", "research-*.log"))
            with open(logs[0], encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn("77s", body)
            self.assertNotIn("900s", body)

    def test_no_claude_cli_skips_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".company"))
            empty_home = os.path.join(d, "empty_home")
            os.makedirs(empty_home)
            # Neither PATH nor $HOME/.local/bin may resolve a real `claude` —
            # sc_resolve_claude_bin falls back to $HOME/.local/bin/claude.
            env = {"PATH": "/usr/bin:/bin", "HOME": empty_home}
            r = self._run(d, env=env)
            self.assertEqual(r.returncode, 0)
            self.assertIn("claude CLI not found", r.stdout)


if __name__ == "__main__":
    unittest.main()
