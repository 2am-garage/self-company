"""
Tests for the Phase 10 safety hooks (Batch B2, Items 6 + 7):

  * scripts/hook_memory_guard.sh — PreToolUse: deny rm/mv-away under
    .company/memory/, allow everything else, fail-open.
  * scripts/hook_memory_lint.py  — PostToolUse: block malformed memory writes,
    pass valid / non-memory writes, fail-open.

Hooks are just scripts that read stdin JSON and write stdout JSON / exit, so we
feed the exact documented stdin fixture and assert the emitted decision + exit.
Both must no-op (exit 0, no output/decision) when CLAUDE_PROJECT_DIR has no
.company marker (the opt-in guard — plugin hooks fire globally).
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "plugin", "skills", "self-company", "scripts")
GUARD = os.path.join(SCRIPTS, "hook_memory_guard.sh")
LINT = os.path.join(SCRIPTS, "hook_memory_lint.py")


def _run(cmd, stdin, project_dir):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": project_dir}
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def run_guard(payload, project_dir):
    return _run(["bash", GUARD], json.dumps(payload), project_dir)


def run_guard_raw(raw, project_dir):
    return _run(["bash", GUARD], raw, project_dir)


def run_lint(payload, project_dir):
    return _run([sys.executable, LINT], json.dumps(payload), project_dir)


def run_lint_raw(raw, project_dir):
    return _run([sys.executable, LINT], raw, project_dir)


VALID_MEM = (
    "---\n"
    "id: test-valid\n"
    "tier: L0\n"
    "owner: Tony\n"
    'sources: ["[abc#12]"]\n'
    "status: active\n"
    "---\n"
    "body\n"
)


class _CompanyRepo(unittest.TestCase):
    """Base: a temp dir WITH a .company/memory skeleton, plus a bare no-company dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.mem = os.path.join(self.root, ".company", "memory", "L0-working")
        os.makedirs(self.mem)
        self.nocompany = os.path.join(self.root, "nocompany")
        os.makedirs(self.nocompany)

    def tearDown(self):
        self._tmp.cleanup()

    def write_mem(self, name, content):
        p = os.path.join(self.mem, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p


class TestMemoryGuard(_CompanyRepo):
    def _decision(self, out):
        obj = json.loads(out)
        return obj["hookSpecificOutput"]["permissionDecision"]

    def test_deny_rm_under_memory(self):
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": "rm .company/memory/L0-working/x.md"}},
            self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(self._decision(out), "deny")
        self.assertIn("tombstone", out.lower())

    def test_deny_unlink_and_shred(self):
        for c in ("unlink .company/memory/L0-working/x.md",
                  "shred -u .company/memory/L0-working/x.md"):
            rc, out, _ = run_guard(
                {"tool_name": "Bash", "tool_input": {"command": c}}, self.root)
            self.assertEqual(self._decision(out), "deny", c)

    def test_deny_mv_memory_away(self):
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": "mv .company/memory/L0-working/x.md /tmp/"}},
            self.root)
        self.assertEqual(self._decision(out), "deny")

    def test_allow_mv_into_memory(self):
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": "mv /tmp/x.md .company/memory/L0-working/"}},
            self.root)
        self.assertEqual(self._decision(out), "allow")

    def test_deny_in_command_chain(self):
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": "rm /tmp/a && rm .company/memory/L0-working/x.md"}},
            self.root)
        self.assertEqual(self._decision(out), "deny")

    def test_deny_rm_rf_company_store_root(self):
        # GIB-C4: `rm -rf .company` wipes the whole store (memory included) and
        # was previously ALLOWED because is_mem only matched `.company/memory/`.
        for c in ("rm -rf .company", "rm -rf .company/", "rm -rf ./.company",
                  "rm -rf /home/u/proj/.company"):
            rc, out, _ = run_guard(
                {"tool_name": "Bash", "tool_input": {"command": c}}, self.root)
            self.assertEqual(rc, 0)
            self.assertEqual(self._decision(out), "deny", c)

    def test_deny_rm_rf_company_memory_dir(self):
        rc, out, _ = run_guard(
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf .company/memory"}},
            self.root)
        self.assertEqual(self._decision(out), "deny")

    def test_deny_find_delete_and_truncate(self):
        for c in ("find .company/memory -type f -delete",
                  "truncate -s 0 .company/memory/L0-working/x.md"):
            rc, out, _ = run_guard(
                {"tool_name": "Bash", "tool_input": {"command": c}}, self.root)
            self.assertEqual(self._decision(out), "deny", c)

    def test_allow_readonly_find_over_memory(self):
        # A plain (non---delete) find over memory is read-only -> must stay allowed.
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": "find .company/memory -name '*.md'"}},
            self.root)
        self.assertEqual(self._decision(out), "allow")

    def test_allow_rm_lookalike_not_store_root(self):
        # `.companystuff` / `my.company` must NOT be mistaken for the store root.
        for c in ("rm -rf .companystuff", "rm -rf my.company-backup"):
            rc, out, _ = run_guard(
                {"tool_name": "Bash", "tool_input": {"command": c}}, self.root)
            self.assertEqual(self._decision(out), "allow", c)

    def test_allow_rm_elsewhere(self):
        rc, out, _ = run_guard(
            {"tool_name": "Bash", "tool_input": {"command": "rm /tmp/foo"}},
            self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(self._decision(out), "allow")

    def test_allow_non_bash_tool(self):
        rc, out, _ = run_guard(
            {"tool_name": "Write",
             "tool_input": {"file_path": ".company/memory/L0-working/x.md"}},
            self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(self._decision(out), "allow")

    def test_failopen_on_garbage_stdin(self):
        rc, out, _ = run_guard_raw("this is not json", self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(self._decision(out), "allow")

    def test_noop_without_company(self):
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": "rm .company/memory/L0-working/x.md"}},
            self.nocompany)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")


class TestMemoryGuardCdBypass(_CompanyRepo):
    """Phase 26 Item 3 — the guard must resolve the Bash tool's cwd (not just
    pattern-match the command's own literal tokens), so a `cd` into the store
    can no longer walk a deleter around it. 'Resolve, don't track': the hook
    reads the `cwd` the harness reports and realpath-resolves every deleter
    argument against it (following symlinks/`..`), simulating any `cd` WITHIN
    the same command string."""

    def _decision(self, out):
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"]

    def test_two_step_cd_then_relative_rm_denied_one_liner(self):
        # cd + rm joined in ONE command string (in-hook cd simulation).
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": "cd .company/memory; rm -rf L0-working/*"}},
            self.root)
        self.assertEqual(self._decision(out), "deny")

    def test_two_step_cd_then_relative_rm_denied_via_persisted_cwd(self):
        # Simulates a SEPARATE prior tool call having already cd'd: the
        # harness reports the NEW cwd on THIS call, with no cd in the command
        # itself. Pure resolve-via-cwd, no in-command simulation needed.
        rc, out, _ = run_guard(
            {"tool_name": "Bash", "cwd": os.path.join(self.root, ".company", "memory"),
             "tool_input": {"command": "rm -rf L0-working/*"}},
            self.root)
        self.assertEqual(self._decision(out), "deny")

    def test_cd_company_and_rm_memory_denied(self):
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": "cd .company && rm -rf memory"}},
            self.root)
        self.assertEqual(self._decision(out), "deny")

    def test_relative_traversal_from_inside_company_denied(self):
        rc, out, _ = run_guard(
            {"tool_name": "Bash", "cwd": os.path.join(self.root, ".company", "ops"),
             "tool_input": {"command": "rm -rf ../memory/L0-working/x.md"}},
            self.root)
        self.assertEqual(self._decision(out), "deny")

    def test_symlink_into_memory_denied(self):
        link = os.path.join(self.root, "evil_link")
        os.symlink(self.mem, link)
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": f"rm -rf {link}/x.md"}},
            self.root)
        self.assertEqual(self._decision(out), "deny")

    def test_ambiguous_cd_with_relative_deleter_fails_closed(self):
        # A cd target we can't resolve (variable expansion) followed by a
        # RELATIVE deleter arg must fail CLOSED — we cannot prove it's safe.
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": 'cd "$SOME_VAR" && rm -rf notes'}},
            self.root)
        self.assertEqual(self._decision(out), "deny")

    def test_ambiguous_cd_with_absolute_deleter_outside_allowed(self):
        # An absolute deleter target is unaffected by any earlier cd, so it
        # can still be proven safe even when the cd itself is unresolvable.
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": 'cd "$SOME_VAR" && rm -rf /tmp/definitely-outside'}},
            self.root)
        self.assertEqual(self._decision(out), "allow")

    def test_cd_and_rm_entirely_outside_store_allowed(self):
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": "cd /tmp && rm -rf some-unrelated-file"}},
            self.root)
        self.assertEqual(self._decision(out), "allow")

    def test_cd_into_project_dir_literally_named_memory_allowed(self):
        # A project's OWN "memory" dir (a sibling of .company, not under it)
        # must not be confused with the store just because of the name.
        other_memory = os.path.join(self.root, "memory")
        os.makedirs(other_memory)
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": "cd memory && rm -rf notes.md"}},
            self.root)
        self.assertEqual(self._decision(out), "allow")

    def test_plain_cd_into_memory_without_deleter_allowed(self):
        # cd alone (no deleter anywhere in the command) is not destructive.
        rc, out, _ = run_guard(
            {"tool_name": "Bash",
             "tool_input": {"command": "cd .company/memory && ls"}},
            self.root)
        self.assertEqual(self._decision(out), "allow")


class TestMemoryLint(_CompanyRepo):
    def _rel(self, name):
        return os.path.join(".company", "memory", "L0-working", name)

    def test_block_malformed_bad_tier_and_sources(self):
        self.write_mem("bad.md",
                       "---\nid: b\ntier: L9\nstatus: active\nsources: []\n---\nx\n")
        rc, out, _ = run_lint(
            {"tool_name": "Write", "tool_input": {"file_path": self._rel("bad.md")}},
            self.root)
        self.assertEqual(rc, 0)
        obj = json.loads(out)
        self.assertEqual(obj["decision"], "block")
        self.assertTrue(obj["reason"])

    def test_block_missing_required_field(self):
        self.write_mem("nostatus.md",
                       '---\nid: b\ntier: L0\nsources: ["[a#1]"]\n---\nx\n')
        rc, out, _ = run_lint(
            {"tool_name": "Write", "tool_input": {"file_path": self._rel("nostatus.md")}},
            self.root)
        obj = json.loads(out)
        self.assertEqual(obj["decision"], "block")
        self.assertIn("status", obj["reason"])

    def test_block_missing_frontmatter_block(self):
        self.write_mem("nofm.md", "just body, no frontmatter\n")
        rc, out, _ = run_lint(
            {"tool_name": "Write", "tool_input": {"file_path": self._rel("nofm.md")}},
            self.root)
        obj = json.loads(out)
        self.assertEqual(obj["decision"], "block")

    def test_block_empty_sources(self):
        self.write_mem("nosrc.md",
                       "---\nid: b\ntier: L0\nstatus: active\nsources: []\n---\nx\n")
        rc, out, _ = run_lint(
            {"tool_name": "Write", "tool_input": {"file_path": self._rel("nosrc.md")}},
            self.root)
        obj = json.loads(out)
        self.assertEqual(obj["decision"], "block")

    def test_pass_valid_memory(self):
        self.write_mem("valid.md", VALID_MEM)
        rc, out, _ = run_lint(
            {"tool_name": "Write", "tool_input": {"file_path": self._rel("valid.md")}},
            self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_pass_tombstone_status(self):
        self.write_mem("tomb.md",
                       '---\nid: t\ntier: L1\nstatus: absorbed\nsources: ["[a#1]"]\n---\nx\n')
        rc, out, _ = run_lint(
            {"tool_name": "Write", "tool_input": {"file_path": self._rel("tomb.md")}},
            self.root)
        self.assertEqual(out.strip(), "")

    def test_noop_non_memory_file(self):
        other = os.path.join(self.root, "other.txt")
        with open(other, "w") as f:
            f.write("hi")
        rc, out, _ = run_lint(
            {"tool_name": "Write", "tool_input": {"file_path": other}}, self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_noop_non_md_under_memory(self):
        p = os.path.join(self.mem, "notes.txt")
        with open(p, "w") as f:
            f.write("hi")
        rc, out, _ = run_lint(
            {"tool_name": "Write", "tool_input": {"file_path": p}}, self.root)
        self.assertEqual(out.strip(), "")

    def test_failopen_on_garbage_stdin(self):
        rc, out, _ = run_lint_raw("not json at all", self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_noop_without_company(self):
        # Even a malformed memory path must be ignored with no .company marker.
        rc, out, _ = run_lint(
            {"tool_name": "Write",
             "tool_input": {"file_path": self._rel("bad.md")}},
            self.nocompany)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")


if __name__ == "__main__":
    unittest.main()
