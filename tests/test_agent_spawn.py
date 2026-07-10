"""
Tests for agent_spawn.sh — Phase 28 Item 4b (D1/D2/D6): the shared bash lib for
the claude-spawn scaffolding (CLAUDE_BIN resolution, the kill-after timeout
probe, the auth pre-flight probe, the plain CAPTURE_ACTIVE + `claude -p`
wrapper, and the scripts-dir precedence) that had drifted across five spawn
scripts + schedule.sh.

Every test sources the lib into a throwaway bash subprocess (never the real
shell) and drives its functions directly via `bash -c 'source LIB; ...'` — the
exact seam every caller uses. Env contracts (SELF_COMPANY_FORCE_AUTH_FAIL,
SELF_COMPANY_SKIP_AUTH_PROBE, SELF_COMPANY_AUTH_PROBE_TIMEOUT,
CLAUDE_PLUGIN_ROOT) are asserted byte-for-byte against the pre-Phase-28
per-script copies.
"""

import os
import stat
import subprocess
import tempfile
import unittest

import _helpers

LIB = os.path.join(_helpers.SCRIPTS_DIR, "agent_spawn.sh")
SPAWN_SCRIPTS = ("daily-run.sh", "company-run.sh", "fleet-run.sh",
                "research-scan.sh", "fire-trigger.sh", "schedule.sh")


def _sh(script, env=None):
    full_env = {**os.environ}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f"source '{LIB}'; {script}"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, env=full_env)


def _fake_bin_dir(d, name, body):
    bindir = os.path.join(d, "bin")
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, name)
    with open(path, "w") as f:
        f.write(body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


class TestResolveClaudeBin(unittest.TestCase):
    def test_found_on_path(self):
        with tempfile.TemporaryDirectory() as d:
            bindir = _fake_bin_dir(d, "claude", "#!/usr/bin/env bash\necho hi\n")
            r = _sh("sc_resolve_claude_bin",
                   env={"PATH": bindir + os.pathsep + "/usr/bin:/bin",
                        "HOME": d})
            self.assertEqual(r.stdout.strip(), os.path.join(bindir, "claude"))

    def test_falls_back_to_home_local_bin_if_executable(self):
        with tempfile.TemporaryDirectory() as d:
            local_bin = os.path.join(d, ".local", "bin")
            os.makedirs(local_bin)
            path = os.path.join(local_bin, "claude")
            with open(path, "w") as f:
                f.write("#!/usr/bin/env bash\necho hi\n")
            os.chmod(path, 0o755)
            r = _sh("sc_resolve_claude_bin",
                   env={"PATH": "/usr/bin:/bin", "HOME": d})
            self.assertEqual(r.stdout.strip(), path)

    def test_home_fallback_not_executable_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            local_bin = os.path.join(d, ".local", "bin")
            os.makedirs(local_bin)
            path = os.path.join(local_bin, "claude")
            with open(path, "w") as f:
                f.write("not executable\n")   # NO chmod +x
            r = _sh("sc_resolve_claude_bin",
                   env={"PATH": "/usr/bin:/bin", "HOME": d})
            self.assertEqual(r.stdout.strip(), "")

    def test_not_found_prints_empty(self):
        with tempfile.TemporaryDirectory() as d:
            r = _sh("sc_resolve_claude_bin", env={"PATH": "/usr/bin:/bin", "HOME": d})
            self.assertEqual(r.stdout.strip(), "")
            self.assertEqual(r.returncode, 0)   # never a hard failure


class TestTmo(unittest.TestCase):
    def test_sets_kill_after_array_when_supported(self):
        r = _sh('sc_tmo 42; printf "%s\\n" "${SC_TMO[@]}"')
        lines = r.stdout.strip().splitlines()
        # On a GNU coreutils box `timeout -k` is supported -> (timeout -k 42).
        # Degrade gracefully to (timeout) is also acceptable on non-GNU systems.
        self.assertIn(lines[0], ("timeout",))
        if len(lines) > 1:
            self.assertEqual(lines[1], "-k")
            self.assertEqual(lines[2], "42")

    def test_default_kill_after_is_30(self):
        r = _sh('sc_tmo; printf "%s\\n" "${SC_TMO[@]}"')
        self.assertIn("30", r.stdout)


class TestAuthLoggedIn(unittest.TestCase):
    def test_force_auth_fail_wins_first(self):
        r = _sh("sc_auth_logged_in",
               env={"SELF_COMPANY_FORCE_AUTH_FAIL": "1", "CLAUDE_BIN": ""})
        self.assertEqual(r.stdout.strip(), "no")

    def test_skip_auth_probe_returns_unknown(self):
        r = _sh("sc_auth_logged_in",
               env={"SELF_COMPANY_SKIP_AUTH_PROBE": "1"})
        self.assertEqual(r.stdout.strip(), "unknown")

    def test_empty_claude_bin_is_unknown_not_a_crash(self):
        r = _sh("CLAUDE_BIN=; sc_auth_logged_in")
        self.assertEqual(r.stdout.strip(), "unknown")
        self.assertEqual(r.returncode, 0)

    def test_real_probe_true(self):
        with tempfile.TemporaryDirectory() as d:
            bindir = _fake_bin_dir(d, "claude",
                                   '#!/usr/bin/env bash\necho \'{"loggedIn": true}\'\n')
            r = _sh(f'CLAUDE_BIN="{bindir}/claude"; sc_auth_logged_in')
            self.assertEqual(r.stdout.strip(), "yes")

    def test_real_probe_false(self):
        with tempfile.TemporaryDirectory() as d:
            bindir = _fake_bin_dir(d, "claude",
                                   '#!/usr/bin/env bash\necho \'{"loggedIn": false}\'\n')
            r = _sh(f'CLAUDE_BIN="{bindir}/claude"; sc_auth_logged_in')
            self.assertEqual(r.stdout.strip(), "no")

    def test_garbage_output_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            bindir = _fake_bin_dir(d, "claude", '#!/usr/bin/env bash\necho garbage\n')
            r = _sh(f'CLAUDE_BIN="{bindir}/claude"; sc_auth_logged_in')
            self.assertEqual(r.stdout.strip(), "unknown")


class TestSpawnCapture(unittest.TestCase):
    def test_builds_expected_argv(self):
        r = _sh('sc_spawn_capture 30 600 /path/to/claude "hello prompt" sonnet-x '
               "--extra-flag; printf '%s\\n' \"${SC_SPAWN_CMD[@]}\"")
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[0], "env")
        self.assertEqual(lines[1], "SELF_COMPANY_CAPTURE_ACTIVE=1")
        self.assertIn("timeout", lines)
        self.assertIn("600", lines)
        self.assertIn("/path/to/claude", lines)
        self.assertIn("-p", lines)
        self.assertIn("hello prompt", lines)
        self.assertIn("--model", lines)
        self.assertIn("sonnet-x", lines)
        self.assertIn("--extra-flag", lines)

    def test_spawn_cmd_is_actually_executable(self):
        # The array must be a real, runnable argv (env-wrapped) — exercise it
        # against a fake claude to prove sc_spawn_capture's output isn't just
        # syntactically plausible but semantically wrong.
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "fake-claude")
            with open(fake, "w") as f:
                f.write(
                    "#!/usr/bin/env bash\n"
                    'echo "CAPTURE_ACTIVE=$SELF_COMPANY_CAPTURE_ACTIVE prompt=$2 model=$4"\n'
                )
            os.chmod(fake, 0o755)
            r = _sh(f'sc_spawn_capture 30 60 "{fake}" "hi" mymodel; '
                    '"${SC_SPAWN_CMD[@]}"')
            self.assertIn("CAPTURE_ACTIVE=1", r.stdout)
            self.assertIn("prompt=hi", r.stdout)
            self.assertIn("model=mymodel", r.stdout)


class TestResolveScriptsDir(unittest.TestCase):
    def test_plugin_root_wins_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            plugin_scripts = os.path.join(d, "plugin", "skills", "self-company", "scripts")
            os.makedirs(plugin_scripts)
            own_dir = os.path.join(d, "own")
            os.makedirs(own_dir)
            company_dir = os.path.join(d, "company")
            r = _sh(f'sc_resolve_scripts_dir "{own_dir}" "{company_dir}"',
                   env={"CLAUDE_PLUGIN_ROOT": os.path.join(d, "plugin")})
            self.assertEqual(r.stdout.strip(), plugin_scripts)

    def test_own_dir_used_when_no_plugin_root(self):
        with tempfile.TemporaryDirectory() as d:
            own_dir = os.path.join(d, "own")
            os.makedirs(own_dir)
            with open(os.path.join(own_dir, "daily-run.sh"), "w") as f:
                f.write("")
            company_dir = os.path.join(d, "company")
            r = _sh(f'sc_resolve_scripts_dir "{own_dir}" "{company_dir}"',
                   env={"CLAUDE_PLUGIN_ROOT": ""})
            self.assertEqual(r.stdout.strip(), own_dir)

    def test_legacy_fallback_when_sentinel_missing_from_own_dir(self):
        with tempfile.TemporaryDirectory() as d:
            own_dir = os.path.join(d, "own")   # exists but lacks daily-run.sh
            os.makedirs(own_dir)
            company_dir = os.path.join(d, "company")
            legacy_scripts = os.path.join(company_dir, "scripts")
            os.makedirs(legacy_scripts)
            with open(os.path.join(legacy_scripts, "daily-run.sh"), "w") as f:
                f.write("")
            r = _sh(f'sc_resolve_scripts_dir "{own_dir}" "{company_dir}"',
                   env={"CLAUDE_PLUGIN_ROOT": ""})
            self.assertEqual(r.stdout.strip(), legacy_scripts)

    def test_custom_sentinel_file(self):
        with tempfile.TemporaryDirectory() as d:
            own_dir = os.path.join(d, "own")
            os.makedirs(own_dir)   # lacks supervisor.py
            company_dir = os.path.join(d, "company")
            legacy_scripts = os.path.join(company_dir, "scripts")
            os.makedirs(legacy_scripts)
            with open(os.path.join(legacy_scripts, "supervisor.py"), "w") as f:
                f.write("")
            r = _sh(f'sc_resolve_scripts_dir "{own_dir}" "{company_dir}" "supervisor.py"',
                   env={"CLAUDE_PLUGIN_ROOT": ""})
            self.assertEqual(r.stdout.strip(), legacy_scripts)

    def test_no_fallback_when_own_dir_already_has_sentinel(self):
        with tempfile.TemporaryDirectory() as d:
            own_dir = os.path.join(d, "own")
            os.makedirs(own_dir)
            with open(os.path.join(own_dir, "daily-run.sh"), "w") as f:
                f.write("")
            company_dir = os.path.join(d, "company")
            # legacy dir ALSO exists with the sentinel, but own_dir already has
            # it -> own_dir wins (no needless fallback).
            legacy_scripts = os.path.join(company_dir, "scripts")
            os.makedirs(legacy_scripts)
            with open(os.path.join(legacy_scripts, "daily-run.sh"), "w") as f:
                f.write("")
            r = _sh(f'sc_resolve_scripts_dir "{own_dir}" "{company_dir}"',
                   env={"CLAUDE_PLUGIN_ROOT": ""})
            self.assertEqual(r.stdout.strip(), own_dir)


class TestGrepZeroDrift(unittest.TestCase):
    """Phase 28 Item 4 acceptance (b): grep-zero — the consolidated scaffolding
    has exactly ONE home (the lib), not a copy re-growing beside it."""

    def _grep_count(self, pattern, path):
        r = subprocess.run(["grep", "-c", pattern, path],
                          capture_output=True, text=True)
        return int(r.stdout.strip() or "0")

    def test_no_command_v_claude_outside_the_lib(self):
        for name in SPAWN_SCRIPTS:
            path = os.path.join(_helpers.SCRIPTS_DIR, name)
            self.assertEqual(self._grep_count("command -v claude", path), 0,
                             f"{name} still has its own CLAUDE_BIN resolution")

    def test_exactly_one_auth_logged_in_definition(self):
        total = 0
        for name in SPAWN_SCRIPTS:
            path = os.path.join(_helpers.SCRIPTS_DIR, name)
            total += self._grep_count(r"^_auth_logged_in()", path)
            total += self._grep_count(r"^sc_auth_logged_in()", path)
        # Only agent_spawn.sh itself defines it now.
        self.assertEqual(total, 0)
        self.assertEqual(self._grep_count(r"^sc_auth_logged_in()", LIB), 1)

    def test_no_bare_tmo_probe_left_in_callers(self):
        # The `_tmo=(timeout); timeout -k 1 1 true ...` probe pattern must be
        # gone from every caller — only agent_spawn.sh's sc_tmo has it.
        for name in ("company-run.sh", "fire-trigger.sh", "research-scan.sh"):
            path = os.path.join(_helpers.SCRIPTS_DIR, name)
            self.assertEqual(self._grep_count(r"_tmo=(timeout)", path), 0, name)


if __name__ == "__main__":
    unittest.main()
