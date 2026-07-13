"""Tests for the plugin-native hooks declaration (hooks/hooks.json) and the
install-hook.sh migration to a plugin-native no-op / legacy-cleaner.

Phase 10 B4: hooks.json is the single declaration point for all hooks. Phase
32 Item 5 added the 9th registration (hook_org_lint.sh, PostToolUse) — now 9
registrations across 7 events (SessionStart AND PostToolUse each fire two);
every command must run a canonical skill script via ${CLAUDE_PLUGIN_ROOT}.
install-hook.sh is deprecated: `install` is a no-op, `uninstall` removes only
legacy self-company entries from a project settings.json (so existing installs
stop double-firing).
"""

import json
import os
import subprocess
import tempfile
import unittest

import _helpers

REPO_ROOT = _helpers.REPO_ROOT
HOOKS_JSON = os.path.join(REPO_ROOT, "plugin", "hooks", "hooks.json")
INSTALL_HOOK = os.path.join(REPO_ROOT, "plugin", "skills", "self-company", "scripts", "install-hook.sh")

# The 7 events the plugin must declare.
EXPECTED_EVENTS = {
    "Stop", "SessionStart", "UserPromptSubmit", "PreCompact",
    "PreToolUse", "PostToolUse", "SessionEnd",
}

# The agreed canonical script names each event's command must reference.
# SessionStart carries TWO groups (plugin hooks merge): notify-status.py and the
# Phase 12 schedule guard hook_schedule_guard.sh. PostToolUse also carries TWO
# (Phase 32 Item 5): hook_memory_lint.py and the new hook_org_lint.sh.
AGREED_SCRIPTS = {
    "capture-trigger.py", "notify-status.py", "hook_memory_inject.py",
    "hook_precompact_capture.sh", "hook_memory_guard.sh", "hook_memory_lint.py",
    "hook_sessionend_verify.sh", "hook_schedule_guard.sh", "hook_org_lint.sh",
}

# Each event maps to the SET of canonical scripts its commands may reference.
EVENT_SCRIPT = {
    "Stop": {"capture-trigger.py"},
    "SessionStart": {"notify-status.py", "hook_schedule_guard.sh"},
    "UserPromptSubmit": {"hook_memory_inject.py"},
    "PreCompact": {"hook_precompact_capture.sh"},
    "PreToolUse": {"hook_memory_guard.sh"},
    "PostToolUse": {"hook_memory_lint.py", "hook_org_lint.sh"},
    "SessionEnd": {"hook_sessionend_verify.sh"},
}


def _load_hooks():
    with open(HOOKS_JSON, encoding="utf-8") as f:
        return json.load(f)


def _commands(hooks):
    for event, groups in hooks["hooks"].items():
        for group in groups:
            for entry in group.get("hooks", []):
                yield event, entry


def _run_install_hook(cmd, project_dir):
    proc = subprocess.run(
        ["bash", INSTALL_HOOK, cmd, project_dir],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


class HooksJsonStructureTest(unittest.TestCase):
    def setUp(self):
        self.hooks = _load_hooks()

    def test_valid_json_with_hooks_object(self):
        # json.load already parsed it; assert the documented top-level shape.
        self.assertIsInstance(self.hooks, dict)
        self.assertIn("hooks", self.hooks)
        self.assertIsInstance(self.hooks["hooks"], dict)

    def test_declares_all_seven_events(self):
        self.assertEqual(set(self.hooks["hooks"].keys()), EXPECTED_EVENTS)

    def test_every_command_references_plugin_root(self):
        for event, entry in _commands(self.hooks):
            self.assertEqual(entry["type"], "command", f"{event}: non-command type")
            self.assertIn("${CLAUDE_PLUGIN_ROOT}", entry["command"],
                          f"{event}: command missing ${{CLAUDE_PLUGIN_ROOT}}: {entry['command']}")

    def test_every_command_uses_agreed_script(self):
        for event, entry in _commands(self.hooks):
            referenced = [s for s in AGREED_SCRIPTS if s in entry["command"]]
            self.assertEqual(len(referenced), 1,
                             f"{event}: must reference exactly one agreed script, got {referenced}")
            self.assertIn(referenced[0], EVENT_SCRIPT[event],
                          f"{event}: expected one of {EVENT_SCRIPT[event]}, got {referenced[0]}")

    def test_sessionstart_declares_schedule_guard(self):
        # Phase 12: the schedule guard rides alongside notify-status on SessionStart.
        cmds = [e["command"] for ev, e in _commands(self.hooks) if ev == "SessionStart"]
        self.assertTrue(any("notify-status.py" in c for c in cmds), cmds)
        self.assertTrue(any("hook_schedule_guard.sh" in c for c in cmds), cmds)

    def test_scripts_live_under_skill_scripts_dir(self):
        for event, entry in _commands(self.hooks):
            self.assertIn("skills/self-company/scripts/", entry["command"],
                          f"{event}: script not under skill scripts dir")

    def test_total_registrations_is_nine_across_seven_events(self):
        # Fold C1 (TONY-2) + Phase 32 Item 5: the real count is 9 registrations
        # over 7 events — SessionStart carries two (notify-status + schedule
        # guard) and PostToolUse now carries two (memory lint + org lint).
        total = sum(1 for _ in _commands(self.hooks))
        self.assertEqual(total, 9, "expected 9 hook registrations")
        self.assertEqual(len(self.hooks["hooks"]), 7, "expected 7 distinct events")

    def test_userpromptsubmit_timeout_is_30(self):
        entries = [e for ev, e in _commands(self.hooks) if ev == "UserPromptSubmit"]
        self.assertTrue(entries, "UserPromptSubmit not declared")
        for e in entries:
            self.assertEqual(e.get("timeout"), 30, "UserPromptSubmit must cap at 30s")

    def test_every_command_has_sane_timeout(self):
        for event, entry in _commands(self.hooks):
            t = entry.get("timeout")
            self.assertIsInstance(t, int, f"{event}: timeout not int")
            self.assertTrue(0 < t <= 600, f"{event}: bad timeout {t!r}")

    def test_matchers_use_matcher_key_and_correct_values(self):
        """Real Claude Code (July 2026) uses the "matcher" key for event filtering."""
        matchers = {}
        for event, groups in self.hooks["hooks"].items():
            for group in groups:
                self.assertNotIn("if", group, f"{event}: group uses 'if' not 'matcher'")
                if "matcher" in group:
                    matchers[event] = group["matcher"]
        self.assertEqual(set(matchers["SessionStart"].split("|")),
                         {"startup", "resume", "clear", "compact"})
        self.assertEqual(set(matchers["PreCompact"].split("|")), {"auto", "manual"})
        self.assertIn("Bash", matchers["PreToolUse"])
        self.assertEqual(matchers["PostToolUse"], "Write|Edit")


class HookCountDocTruthTest(unittest.TestCase):
    """Fold C1 + Phase 32: the docs must state the ACCURATE count (NINE
    registrations / 7 events — SessionStart AND PostToolUse each fire two after
    Phase 32 added hook_org_lint.sh) and must not carry a stale undercount
    ('7 hooks' from Fold C1, or the '8 registrations' that predated the org-lint
    hook). Ties the prose to the real hooks.json count (see
    test_total_registrations_is_nine_across_seven_events) so a future edit to
    either side is caught."""

    DOCS = {
        "SKILL.md": os.path.join(REPO_ROOT, "plugin", "skills", "self-company", "SKILL.md"),
        "operations.md": os.path.join(REPO_ROOT, "plugin", "skills", "self-company",
                                      "references", "operations.md"),
        "status.md": os.path.join(REPO_ROOT, "plugin", "skills", "self-company",
                                   "references", "status.md"),
    }

    def _text(self, name):
        with open(self.DOCS[name], encoding="utf-8") as f:
            return f.read()

    def test_no_doc_claims_seven_hooks(self):
        import re
        # The stale undercount phrasing: "7 hooks" / "all 7 hooks" / "seven hooks".
        stale = re.compile(r"\b(7|seven)\s+hooks\b", re.IGNORECASE)
        for name in self.DOCS:
            m = stale.search(self._text(name))
            self.assertIsNone(m, f"{name} still claims '{m.group(0) if m else ''}'")

    def test_docs_state_nine_registrations(self):
        import re
        # Accurate phrasing (Phase 32): "9 … registrations across 7 events"
        # (order-flexible). The count went 8 -> 9 when hook_org_lint.sh landed
        # as the second PostToolUse registration; the docs were swept, this
        # test tracks the swept value and must never be reverted to 8.
        pat = re.compile(r"9[\s\S]{0,40}?registrations[\s\S]{0,40}?7 events",
                         re.IGNORECASE)
        for name in self.DOCS:
            self.assertRegex(self._text(name), pat,
                             f"{name} should state '9 … registrations across 7 events'")

    def test_operations_table_lists_schedule_guard(self):
        # The operations.md hook table must include the previously-missing row.
        self.assertIn("hook_schedule_guard.sh", self._text("operations.md"))


class InstallHookMigrationTest(unittest.TestCase):
    def _legacy_settings(self, project_dir):
        claude = os.path.join(project_dir, ".claude")
        os.makedirs(claude, exist_ok=True)
        settings = os.path.join(claude, "settings.json")
        legacy = {
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {
                "Stop": [{"hooks": [{"type": "command",
                          "command": "python3 capture-trigger.py  # self-company-capture"}]}],
                "SessionStart": [{"hooks": [{"type": "command",
                          "command": "python3 notify-status.py  # self-company-notify"}]}],
            },
        }
        with open(settings, "w", encoding="utf-8") as f:
            json.dump(legacy, f, indent=2)
        return settings

    def test_install_command_removed_is_usage_error(self):
        # Phase 14 Bucket 3: the dead `install` no-op branch was removed (hooks are
        # plugin-native). `install` is no longer a recognized command -> usage error
        # (exit 2), and it must never create settings.json.
        with tempfile.TemporaryDirectory() as d:
            rc, out, err = _run_install_hook("install", d)
            self.assertEqual(rc, 2)
            self.assertIn("usage:", err.lower())
            self.assertFalse(os.path.exists(os.path.join(d, ".claude", "settings.json")))

    def test_uninstall_removes_legacy_entries(self):
        with tempfile.TemporaryDirectory() as d:
            settings = self._legacy_settings(d)
            rc, out, err = _run_install_hook("uninstall", d)
            self.assertEqual(rc, 0, err)
            self.assertIn("removed", out.lower())
            with open(settings, encoding="utf-8") as f:
                data = json.load(f)
            self.assertFalse(data.get("hooks", {}).get("Stop"))
            self.assertFalse(data.get("hooks", {}).get("SessionStart"))
            self.assertEqual(data["permissions"]["allow"], ["Bash(ls:*)"])

    def test_uninstall_preserves_foreign_hooks(self):
        with tempfile.TemporaryDirectory() as d:
            claude = os.path.join(d, ".claude")
            os.makedirs(claude)
            settings = os.path.join(claude, "settings.json")
            payload = {"hooks": {"Stop": [
                {"hooks": [{"type": "command",
                            "command": "python3 capture-trigger.py  # self-company-capture"}]},
                {"hooks": [{"type": "command", "command": "echo not-ours"}]},
            ]}}
            with open(settings, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            rc, out, err = _run_install_hook("uninstall", d)
            self.assertEqual(rc, 0, err)
            with open(settings, encoding="utf-8") as f:
                data = json.load(f)
            stop = data["hooks"]["Stop"]
            self.assertEqual(len(stop), 1)
            self.assertEqual(stop[0]["hooks"][0]["command"], "echo not-ours")

    def test_uninstall_on_missing_settings_is_clean(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, err = _run_install_hook("uninstall", d)
            self.assertEqual(rc, 0, err)
            self.assertIn("nothing to remove", out.lower())
            self.assertFalse(os.path.exists(os.path.join(d, ".claude", "settings.json")))

    def test_status_reports_plugin_native(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out, err = _run_install_hook("status", d)
            self.assertEqual(rc, 0, err)
            self.assertIn("plugin-native", out.lower())

    def test_status_warns_on_lingering_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            self._legacy_settings(d)
            rc, out, err = _run_install_hook("status", d)
            self.assertEqual(rc, 0, err)
            self.assertIn("legacy", out.lower())
            self.assertIn("double-fir", out.lower())


if __name__ == "__main__":
    unittest.main()
