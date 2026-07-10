"""
Tests for daily-run.sh and schedule.sh (subprocess / black-box).

Covers the deterministic daily core (decay + entropy logging) and the scheduler's
guard paths. The live headless-agent step and real crontab mutation are verified
manually (and the agent is always run with --no-agent here so tests stay
hermetic and token-free).
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

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
        subprocess.run(["cp", "-r", os.path.join(REPO, "plugin", "skills", "self-company", sub), d], check=True)
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
            r = _bash([os.path.join(REPO, "plugin", "skills", "self-company", "scripts","daily-run.sh"), d, "--no-agent"])
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
        # Phase 5 Item 2 (N2): this previously asserted the stale L0 was
        # physically deleted — drop is now a SOFT-DELETE. The file remains as
        # a recoverable tombstone (status: archived + invalid_at).
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-stale", last_reinforced="2026-05-01")  # ~56d -> drop
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            path = os.path.join(company, "memory", "L0-working", "obs-stale.md")
            self.assertTrue(os.path.exists(path), "drop must tombstone, not unlink")
            with open(path) as f:
                txt = f.read()
            self.assertIn("status: archived", txt)
            self.assertIn("invalid_at:", txt)
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_snapshot_created_before_mutation_and_rotated(self):
        # Phase 5 Item 2 (N2): a real (non-dry) run tars memory/ to
        # .company/backups/mem-<UTCts>.tar.gz BEFORE mutating, and rotates to
        # the newest BACKUP_KEEP (policy §7.8 — overridden to 3 here).
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            with open(os.path.join(company, "org", "policy.md"), "a") as f:
                f.write("\n| `BACKUP_KEEP` | **3** | test override | ✓ |\n")
            bdir = os.path.join(company, "backups")
            os.makedirs(bdir)
            for i in range(4):  # pre-existing older snapshots
                with open(os.path.join(bdir, f"mem-0001010{i}T000000Z.tar.gz"), "w") as f:
                    f.write("old")
            _write_mem(company, "obs-fresh")
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            backups = sorted(os.listdir(bdir))
            self.assertEqual(len(backups), 3)              # rotated at N
            self.assertNotIn("mem-00010100T000000Z.tar.gz", backups)  # oldest gone
            self.assertTrue(backups[-1].startswith("mem-2"))  # fresh one kept
            self.assertIn("- backup: memory -> backups/mem-", _read_log(company))
            # the snapshot actually contains the memory tree
            out = subprocess.run(["tar", "-tzf", os.path.join(bdir, backups[-1])],
                                 capture_output=True, text=True)
            self.assertIn("memory/L0-working/obs-fresh.md", out.stdout)
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_dry_run_never_snapshots(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-fresh")
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--dry-run"])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(os.path.join(company, "backups")))
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
    """Plant a fake .company/.rag-venv/bin/python so the reinforce step 'runs'.

    Phase 5 C2: entropy.py now re-execs into this same project venv (resolved
    from --memory-dir, not cwd), so the fake interpreter must apply its canned
    behaviour ONLY to reinforce_memory.py and pass every other script through
    to the real interpreter (the re-exec sets SC_RAG_REEXEC, so no loop)."""
    bindir = os.path.join(company, ".rag-venv", "bin")
    os.makedirs(bindir, exist_ok=True)
    py = os.path.join(bindir, "python")
    with open(py, "w") as f:
        f.write("#!/usr/bin/env bash\n"
                'if [[ "${1:-}" != *reinforce_memory.py ]]; then\n'
                '  exec python3 "$@"\n'
                "fi\n" + script_body)
    os.chmod(py, 0o755)
    return py


def _fake_rag_venv_multi(company, index_json, argfile):
    """Plant a fake .company/.rag-venv/bin/python that handles BOTH RAG-venv
    callers in the deterministic core: it records rag_index.py's argv + emits a
    canned index JSON, emits a trivial reinforce JSON, and passes EVERYTHING ELSE
    (e.g. entropy.py's re-exec) through to the real interpreter (SC_RAG_REEXEC is
    already set on that re-exec, so no loop)."""
    bindir = os.path.join(company, ".rag-venv", "bin")
    os.makedirs(bindir, exist_ok=True)
    py = os.path.join(bindir, "python")
    with open(py, "w") as f:
        f.write(
            "#!/usr/bin/env bash\n"
            'case "${1:-}" in\n'
            "  *rag_index.py)\n"
            f'    printf \'%s \' "$@" > "{argfile}"\n'
            f"    echo '{index_json}'\n"
            "    ;;\n"
            "  *reinforce_memory.py)\n"
            '    echo \'{"applied": true, "threshold": 0.85, "reinforcements": [],'
            ' "skipped_l2": [], "scanned": 0}\'\n'
            "    ;;\n"
            "  *)\n"
            '    exec python3 "$@"\n'
            "    ;;\n"
            "esac\n")
    os.chmod(py, 0o755)
    return py


def _write_l1(company, mid):
    """Write a fresh active L1 memory (counts toward RAG_ENABLE_THRESHOLD)."""
    p = os.path.join(company, "memory", "L1-warm", f"{mid}.md")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(
            f"---\nid: {mid}\ntier: L1\nowner: Tony\nsources: [\"[s#1]\"]\n"
            f"created: 2026-06-01\nlast_reinforced: {_today()}\n"
            f"reinforce_count: 3\ndecay_score: 1.0\nstatus: active\n---\nbody {mid}\n")


class TestDailyRunRagIndex(unittest.TestCase):
    """Phase 13 Stage A: daily incremental RAG index refresh (A.1) + deps-free
    threshold activation surface (A.2). HARD invariant: never fail the core."""

    _INDEX_JSON = ('{"embedded": 2, "skipped_unchanged": 3, "deleted_stale": 0,'
                   ' "table_rows": 5, "l1_l2_count": 5, "warnings": []}')

    def test_index_runs_when_venv_present_incremental_l1l2(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            argfile = os.path.join(d, "ragidx_args.txt")
            _fake_rag_venv_multi(company, self._INDEX_JSON, argfile)
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- rag-index: embedded 2 | skipped 3 | deleted-stale 0 | rows 5 (L1/L2 5)", text)
            with open(argfile) as f:
                args = f.read()
            self.assertIn("rag_index.py", args)
            self.assertIn("--memory-dir", args)
            self.assertIn("--index-dir", args)
            self.assertNotIn("--rebuild", args)       # incremental (idempotent) — never full rebuild
            self.assertNotIn("--include-l0", args)     # D-A: L1/L2 only, no L0
            # index refresh runs AFTER decay (post-consolidation truth)
            self.assertLess(text.index("- decay"), text.index("- rag-index"))
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_venv_absent_one_line_skip_core_completes(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- rag-index: skipped — RAG venv absent", text)
            self.assertIn("- decay --apply:", text)   # core unaffected
            self.assertIn("- entropy", text)
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_threshold_surfaces_activation_candidate_when_over(self):
        # >= RAG_ENABLE_THRESHOLD (50) active L1 + no venv => deps-free
        # threshold-check surfaces the "activate RAG" candidate.
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            for i in range(52):
                _write_l1(company, f"warm-{i:03d}")
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("RAG activation candidate", text)
            self.assertIn("rag_setup.sh install", text)
            self.assertIn("- rag-index: skipped — RAG venv absent", text)  # novenv path
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_threshold_not_surfaced_when_under(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            for i in range(3):
                _write_l1(company, f"warm-{i:03d}")
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertNotIn("RAG activation candidate", text)
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_broken_venv_entropy_still_logs_via_jaccard(self):
        # P13A-1: a BROKEN venv (python present but exits nonzero) used to make
        # `python3 entropy.py` self-re-exec into that dead python and vanish, so the
        # entropy line silently disappeared. Now entropy retries in base python
        # (SC_RAG_REEXEC=1, Jaccard) and always logs its line; core completes.
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            bindir = os.path.join(company, ".rag-venv", "bin")
            os.makedirs(bindir, exist_ok=True)
            py = os.path.join(bindir, "python")
            with open(py, "w") as f:
                f.write("#!/usr/bin/env bash\nexit 1\n")   # broken: present but always dies
            os.chmod(py, 0o755)
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- entropy", text)          # line NOT dropped despite broken venv
            self.assertIn("- decay --apply:", text)   # core intact
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_garbage_stdout_venv_entropy_still_logs(self):
        # P13A-2 (Gibby RA5): a venv python that prints non-JSON to stdout then
        # exits 0 left garbage in $EOUT, so the emptiness-only retry never fired and
        # the entropy line vanished at json.load. The retry now keys on VALID entropy
        # JSON, so the base-python Jaccard pass fires and the line is always present.
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            bindir = os.path.join(company, ".rag-venv", "bin")
            os.makedirs(bindir, exist_ok=True)
            py = os.path.join(bindir, "python")
            with open(py, "w") as f:
                f.write("#!/usr/bin/env bash\n"
                        'echo "WARNING: fastembed notice"\n'   # non-JSON garbage on stdout
                        "exit 0\n")                             # ...but exits 0
            os.chmod(py, 0o755)
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- entropy", text)          # never silently absent
            self.assertIn("- decay --apply:", text)   # core intact
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_index_failure_never_aborts_core(self):
        # A refresh that exits nonzero must not abort the already-completed core.
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            bindir = os.path.join(company, ".rag-venv", "bin")
            os.makedirs(bindir, exist_ok=True)
            py = os.path.join(bindir, "python")
            with open(py, "w") as f:
                f.write("#!/usr/bin/env bash\n"
                        'case "${1:-}" in\n'
                        "  *rag_index.py) echo boom >&2; exit 2 ;;\n"
                        '  *reinforce_memory.py) echo \'{"reinforcements": [], "skipped_l2": [],'
                        ' "scanned": 0, "threshold": 0.85}\' ;;\n'
                        '  *) exec python3 "$@" ;;\n'
                        "esac\n")
            os.chmod(py, 0o755)
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            # nonzero rc => empty $IOUT => graceful "no output" line, core intact
            self.assertIn("- rag-index:", text)
            self.assertIn("- entropy", text)
        finally:
            subprocess.run(["rm", "-rf", d])


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

    def test_prompt_states_actual_budget_and_atomic_pairs(self):
        # B3 (Phase 5 Item 3): the prompt must state the REAL seconds budget
        # (the timeout value), demand a pre-exhaustion hard-stop summary line,
        # and require pair-by-pair ATOMIC completion.
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            self._write_body_mem(company, "pref-dark-theme-one", self.BODY)
            self._write_body_mem(company, "pref-dark-theme-two", self.BODY)
            promptfile = os.path.join(d, "prompt.txt")
            env = {"PATH": _fake_claude(d) + os.pathsep + os.environ["PATH"],
                   "FAKE_CLAUDE_PROMPT_FILE": promptfile,
                   "SELF_COMPANY_DAILY_TIMEOUT": "123"}
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(promptfile) as f:
                prompt = f.read()
            self.assertIn("HARD limit of 123 seconds", prompt)  # actual budget
            self.assertIn("AGENT SUMMARY:", prompt)             # hard-stop line
            self.assertIn("Each pair is ATOMIC", prompt)
            self.assertIn("NEVER interleave steps across pairs", prompt)
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_timeout_preserves_partial_output_and_logs_explicit_line(self):
        # B3 (Phase 5 Item 3, N3): a timed-out agent must leave (a) whatever
        # it streamed before the kill in the audit log and (b) an explicit
        # "TIMEOUT after Ns" line in both logs (report.py keys on it).
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-a")
            bindir = os.path.join(d, "fakebin")
            os.makedirs(bindir, exist_ok=True)
            with open(os.path.join(bindir, "claude"), "w") as f:
                f.write('#!/usr/bin/env bash\n'
                        'if [[ "${1:-}" == "auth" ]]; then echo \'{"loggedIn": true}\'; exit 0; fi\n'
                        'echo "PARTIAL-STREAM-EVENT-1"\n'   # flushed pre-kill
                        'sleep 30\n')
            os.chmod(os.path.join(bindir, "claude"), 0o755)
            env = {"PATH": bindir + os.pathsep + os.environ["PATH"],
                   "SELF_COMPANY_DAILY_TIMEOUT": "1"}
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            date = _today()
            with open(os.path.join(company, "ops", "logs",
                                   f"agent-{date}.log")) as f:
                agent_log = f.read()
            self.assertIn("PARTIAL-STREAM-EVENT-1", agent_log)  # partial kept
            self.assertIn("agent: TIMEOUT after 1s (partial output above)",
                          agent_log)
            text = _read_log(company)
            self.assertIn("- agent: TIMEOUT after 1s (rc 124)", text)
            self.assertIn("partial output in agent-", text)
            # timeout grows the fail streak like any agent failure
            with open(os.path.join(company, "ops", "auth-fail.marker")) as f:
                self.assertIn("reason=agent", f.read())
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


class TestRuntimeReliability(unittest.TestCase):
    """Phase 19 — Item 1 (hard-kill grace on the agent spawn) + Item 2 (flock
    mutual exclusion on the memory-mutating core)."""

    # --- Item 1 -------------------------------------------------------------
    def _fake_claude_ignoring_term(self, d, pidfile):
        """A fake claude that passes auth, records its pid, then TRAPS SIGTERM and
        keeps running — exactly the 'claude that won't die on TERM' orphan case."""
        bindir = os.path.join(d, "fakebin")
        os.makedirs(bindir, exist_ok=True)
        p = os.path.join(bindir, "claude")
        with open(p, "w") as f:
            f.write('#!/usr/bin/env bash\n'
                    'if [[ "${1:-}" == "auth" ]]; then echo \'{"loggedIn": true}\'; exit 0; fi\n'
                    f'echo $$ > "{pidfile}"\n'
                    "trap '' TERM\n"
                    'while true; do sleep 0.5; done\n')
        os.chmod(p, 0o755)
        return bindir

    def test_agent_orphan_hard_killed_past_budget_no_survivor(self):
        # Item 1 (TOM-2): a claude that ignores SIGTERM is SIGKILLed <grace>s past
        # budget — no orphan survives into the next tick (the 336454/336455 repro).
        d = _fresh_project()
        try:
            pidfile = os.path.join(d, "claude.pid")
            bindir = self._fake_claude_ignoring_term(d, pidfile)
            env = {"PATH": bindir + os.pathsep + os.environ["PATH"],
                   "SELF_COMPANY_DAILY_TIMEOUT": "1",
                   "SELF_COMPANY_TIMEOUT_KILL_AFTER": "1"}
            start = time.monotonic()
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d], env=env)
            elapsed = time.monotonic() - start
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLess(elapsed, 30, "budget+grace must bound the spawn")
            with open(pidfile) as f:
                pid = int(f.read().strip())
            time.sleep(0.5)                       # let the kill settle
            alive = True
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
            if alive:
                os.kill(pid, 9)                   # cleanup so we don't leak it
            self.assertFalse(alive, "orphan survived past budget+grace")
            # a TERM-ignoring child SIGKILLed by the -k grace exits 137 -> still
            # classified as a hard TIMEOUT (not a generic failure).
            self.assertIn("- agent: TIMEOUT after 1s (rc 137)", _read_log(company_of(d)))
        finally:
            subprocess.run(["rm", "-rf", d])

    # --- Item 2 -------------------------------------------------------------
    def _hold_lock(self, company, hold_secs):
        """Background holder of .company/ops/.daily.lock; drops a `lock.ready`
        sentinel once it actually owns the lock."""
        ops = os.path.join(company, "ops")
        os.makedirs(ops, exist_ok=True)
        lock = os.path.join(ops, ".daily.lock")
        ready = os.path.join(company, "lock.ready")
        script = f'exec 9>"{lock}"; flock 9; : > "{ready}"; sleep {hold_secs}'
        return subprocess.Popen(["bash", "-c", script])

    def _await_ready(self, company):
        ready = os.path.join(company, "lock.ready")
        for _ in range(200):
            if os.path.exists(ready):
                return
            time.sleep(0.02)
        self.fail("lock holder never acquired the lock")

    def test_cron_skips_when_lock_already_held(self):
        # Item 2: a cron run that finds the lock held SKIPS this tick (no pile-up)
        # and does NOT run the mutating pass.
        d = _fresh_project()
        holder = None
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-stale", last_reinforced="2026-05-01")  # would decay-drop
            holder = self._hold_lock(company, 5)
            self._await_ready(company)
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent", "--cron"])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("cron tick SKIPPED", _read_log(company))
            self.assertIn("skipped", r.stdout)
            # mutating pass never ran -> the stale memory is untouched (no tombstone)
            with open(os.path.join(company, "memory", "L0-working", "obs-stale.md")) as f:
                self.assertNotIn("status: archived", f.read())
        finally:
            if holder:
                holder.wait()
            subprocess.run(["rm", "-rf", d])

    def test_cron_env_flag_also_selects_nonblocking(self):
        # SELF_COMPANY_CRON=1 is an equivalent seam to --cron.
        d = _fresh_project()
        holder = None
        try:
            company = os.path.join(d, ".company")
            holder = self._hold_lock(company, 4)
            self._await_ready(company)
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"],
                      env={"SELF_COMPANY_CRON": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("cron tick SKIPPED", _read_log(company))
        finally:
            if holder:
                holder.wait()
            subprocess.run(["rm", "-rf", d])

    def test_manual_blocks_then_runs_after_lock_releases(self):
        # Item 2: a MANUAL run (default) waits for the in-flight run, then runs —
        # the human's run still happens.
        d = _fresh_project()
        holder = None
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-a", last_reinforced=_today())
            holder = self._hold_lock(company, 2)
            self._await_ready(company)
            start = time.monotonic()
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])  # manual
            elapsed = time.monotonic() - start
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertGreaterEqual(elapsed, 1.0, "manual run must have waited on the lock")
            text = _read_log(company)
            self.assertNotIn("cron tick SKIPPED", text)
            self.assertIn("- decay --apply:", text)             # it actually ran the core
        finally:
            if holder:
                holder.wait()
            subprocess.run(["rm", "-rf", d])

    def test_flock_absent_degrades_with_one_warning(self):
        # Item 2: flock unavailable => ONE warning line, core runs unserialized.
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"],
                      env={"SELF_COMPANY_NO_FLOCK": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertEqual(text.count("flock unavailable"), 1)
            self.assertIn("- decay --apply:", text)             # core still runs
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_dry_run_never_locks(self):
        # Dry-run mutates nothing, so it must never take (or be blocked by) the lock.
        d = _fresh_project()
        holder = None
        try:
            company = os.path.join(d, ".company")
            holder = self._hold_lock(company, 5)
            self._await_ready(company)
            start = time.monotonic()
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--dry-run"])
            elapsed = time.monotonic() - start
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLess(elapsed, 4, "dry-run must not block on a held lock")
            self.assertNotIn("cron tick SKIPPED", _read_log(company))
        finally:
            if holder:
                holder.wait()
            subprocess.run(["rm", "-rf", d])


def _fake_df_low_space(d):
    """Plant a fake `df` reporting Available far below any real threshold,
    regardless of args — used by both the `-Pk` (free KB) and `-P`
    (filesystem name) calls daily-run.sh makes (same fixed 2-line output
    satisfies both column extractions)."""
    bindir = os.path.join(d, "fakebin")
    os.makedirs(bindir, exist_ok=True)
    p = os.path.join(bindir, "df")
    with open(p, "w") as f:
        f.write(
            "#!/usr/bin/env bash\n"
            'echo "Filesystem     1024-blocks      Used Available Capacity Mounted on"\n'
            'echo "fakefs              999999999 999999999 100 99% /"\n'
        )
    os.chmod(p, 0o755)
    return bindir


def _fake_tar_always_fails(d):
    """Plant a fake `tar` that always fails (simulates ENOSPC mid-snapshot)."""
    bindir = os.path.join(d, "fakebin")
    os.makedirs(bindir, exist_ok=True)
    p = os.path.join(bindir, "tar")
    with open(p, "w") as f:
        f.write("#!/usr/bin/env bash\necho 'tar: fake ENOSPC failure' >&2\nexit 2\n")
    os.chmod(p, 0o755)
    return bindir


class TestItem1SafetyFloor(unittest.TestCase):
    """Phase 25 Item 1 — CRITICAL: never enter the mutating core without a
    floor; never truncate-write into a full disk. ENOSPC harness — kept
    permanently in the suite (spec requirement, reproducible)."""

    def test_free_space_preflight_aborts_core_corpus_byte_identical(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-stale", last_reinforced="2026-05-01")  # would decay-drop
            path = os.path.join(company, "memory", "L0-working", "obs-stale.md")
            with open(path) as f:
                before = f.read()
            bindir = _fake_df_low_space(d)
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"],
                      env={"PATH": bindir + os.pathsep + os.environ["PATH"]})
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- CORE ABORTED:", text)
            self.assertIn("free-space preflight", text)
            self.assertIn("- reinforce: skipped — CORE ABORTED", text)
            self.assertIn("- decay: skipped — CORE ABORTED", text)
            self.assertIn("- verify: skipped — CORE ABORTED", text)
            self.assertIn("- entropy", text)   # read-only stage still runs
            with open(path) as f:
                after = f.read()
            self.assertEqual(before, after)    # corpus byte-identical
            self.assertTrue(os.path.exists(os.path.join(company, "ops", "core-abort.marker")))
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_preflight_abort_writes_no_tarball_to_low_disk(self):
        # Gibby re-attack MUST-FIX 2: when the free-space preflight aborts (disk
        # below floor), the tar snapshot must NOT run — no full memory tarball
        # gets written to the very filesystem we just judged unsafely low. The
        # new-plan-file writers (elon_survey/july_audit) are skipped too.
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-fresh")
            bindir = _fake_df_low_space(d)
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"],
                      env={"PATH": bindir + os.pathsep + os.environ["PATH"]})
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- CORE ABORTED:", text)
            self.assertIn("- backup: skipped — free-space preflight already ABORTED", text)
            # NO tarball written to the low-disk filesystem
            bdir = os.path.join(company, "backups")
            if os.path.isdir(bdir):
                self.assertEqual([f for f in os.listdir(bdir) if f.endswith(".tar.gz")], [])
            # the new-plan-file writers are skipped too (ideally-part of MUST-FIX 2)
            self.assertIn("- elon survey: skipped — CORE ABORTED", text)
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_snapshot_failure_aborts_core_no_stray_tarball(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-stale", last_reinforced="2026-05-01")
            bindir = _fake_tar_always_fails(d)
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"],
                      env={"PATH": bindir + os.pathsep + os.environ["PATH"]})
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- CORE ABORTED:", text)
            self.assertIn("snapshot FAILED", text)
            bdir = os.path.join(company, "backups")
            if os.path.isdir(bdir):
                self.assertEqual(os.listdir(bdir), [])  # no truncated/stray tarball
            with open(os.path.join(company, "memory", "L0-working", "obs-stale.md")) as f:
                self.assertNotIn("status: archived", f.read())  # decay never ran
            self.assertTrue(os.path.exists(os.path.join(company, "ops", "core-abort.marker")))
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_agent_skipped_during_abort_no_token_spend(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            bindir = _fake_df_low_space(d)
            claude_bin = _fake_claude(d)
            promptfile = os.path.join(d, "prompt.txt")
            env = {"PATH": claude_bin + os.pathsep + bindir + os.pathsep + os.environ["PATH"],
                   "FAKE_CLAUDE_PROMPT_FILE": promptfile}
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- agent: skipped — CORE ABORTED", text)
            self.assertFalse(os.path.exists(promptfile))  # agent never invoked
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_recovers_cleanly_next_run_with_space_no_manual_step(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-stale", last_reinforced="2026-05-01")
            bindir = _fake_df_low_space(d)
            r1 = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"],
                      env={"PATH": bindir + os.pathsep + os.environ["PATH"]})
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertTrue(os.path.exists(os.path.join(company, "ops", "core-abort.marker")))
            # Next run: real df/tar (healthy) — recovers with NO manual step.
            r2 = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertFalse(os.path.exists(os.path.join(company, "ops", "core-abort.marker")))
            with open(os.path.join(company, "memory", "L0-working", "obs-stale.md")) as f:
                self.assertIn("status: archived", f.read())  # decay finally ran
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_healthy_run_no_abort_line_and_warnings_zero(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-fresh")
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertNotIn("CORE ABORTED", text)
            self.assertIn("- warnings: 0", text)
        finally:
            subprocess.run(["rm", "-rf", d])


class TestItem3WarningsSurfaced(unittest.TestCase):
    """Phase 25 Item 3: decay's per-file corruption/rot warnings are surfaced
    into the daily log BEFORE the temp JSON is reaped, and flip the ledger
    verdict away from flat/keep — never silently thrown away."""

    def test_corrupt_memory_file_surfaces_warnings_line(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            p = os.path.join(company, "memory", "L0-working", "corrupt.md")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("---\ntier: L0\nowner: Tony\n---\nbody\n")  # missing id
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertRegex(text, r"- warnings: 1 \(first 5:.*missing id")
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_warnings_bearing_run_flagged_warn_not_flat_in_ledger(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            p = os.path.join(company, "memory", "L0-working", "corrupt.md")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("---\ntier: L0\nowner: Tony\n---\nbody\n")
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            rc, out, err = _helpers.run_script("report.py", "--company", company)
            self.assertEqual(rc, 0, err)
            self.assertIn("`warn`", out)
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_clean_run_stays_flat_no_false_alarm(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-fresh")
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            rc, out, err = _helpers.run_script("report.py", "--company", company)
            self.assertEqual(rc, 0, err)
            self.assertIn("`flat`", out)
        finally:
            subprocess.run(["rm", "-rf", d])


class TestItem4LockHardening(unittest.TestCase):
    """Phase 25 Item 4: the daily lock must not leak into grandchildren or
    wedge forever."""

    def _fake_claude_with_grandchild(self, d, survivor_pidfile):
        """A fake claude that spawns a detached grandchild (SAME process
        group — no setsid of its own) that traps TERM and survives, while
        claude ITSELF has no trap and dies promptly on TERM — exactly the
        'leader dies, orphan survives' case only a GROUP kill catches."""
        bindir = os.path.join(d, "fakebin")
        os.makedirs(bindir, exist_ok=True)
        p = os.path.join(bindir, "claude")
        with open(p, "w") as f:
            f.write(
                '#!/usr/bin/env bash\n'
                'if [[ "${1:-}" == "auth" ]]; then echo \'{"loggedIn": true}\'; exit 0; fi\n'
                f'( trap "" TERM; echo $$ > "{survivor_pidfile}"; '
                'while true; do sleep 0.2; done ) &\n'
                'disown\n'
                'while true; do sleep 0.5; done\n')
        os.chmod(p, 0o755)
        return bindir

    def test_surviving_grandchild_does_not_block_next_tick_and_gets_killed(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            survivor_pidfile = os.path.join(d, "survivor.pid")
            bindir = self._fake_claude_with_grandchild(d, survivor_pidfile)
            env = {"PATH": bindir + os.pathsep + os.environ["PATH"],
                   "SELF_COMPANY_DAILY_TIMEOUT": "1",
                   "SELF_COMPANY_TIMEOUT_KILL_AFTER": "2"}
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            for _ in range(100):
                if os.path.exists(survivor_pidfile):
                    break
                time.sleep(0.05)
            self.assertTrue(os.path.exists(survivor_pidfile), "grandchild never started")
            with open(survivor_pidfile) as f:
                pid = int(f.read().strip())
            alive = True
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
            if alive:
                os.kill(pid, 9)   # cleanup so we don't leak it
            self.assertFalse(alive, "surviving grandchild was NOT group-killed")
            # the next tick must not be blocked by anything this run left behind
            r2 = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r2.returncode, 0, r2.stderr)
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_stale_lock_reported_loudly_not_auto_broken(self):
        d = _fresh_project()
        holder = None
        try:
            company = os.path.join(d, ".company")
            ops = os.path.join(company, "ops")
            os.makedirs(ops, exist_ok=True)
            old_started = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
            with open(os.path.join(ops, ".daily.lock.holder"), "w") as f:
                f.write(f"pid=999999\nstarted={old_started}\n")
            holder = self._hold_lock(company, 3)
            self._await_ready(company)
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent", "--cron"],
                      env={"SELF_COMPANY_STALE_LOCK_SECS": "60"})
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- LOCK STALE:", text)
            self.assertIn("NOT auto-broken", text)
        finally:
            if holder:
                holder.wait()
            subprocess.run(["rm", "-rf", d])

    def test_fresh_contention_not_misreported_as_stale(self):
        # a NORMAL contention (holder just started) must stay the routine
        # "cron tick SKIPPED" message, not a false STALE alarm.
        d = _fresh_project()
        holder = None
        try:
            company = os.path.join(d, ".company")
            holder = self._hold_lock(company, 3)
            self._await_ready(company)
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent", "--cron"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertNotIn("- LOCK STALE:", text)
            self.assertIn("cron tick SKIPPED", text)
        finally:
            if holder:
                holder.wait()
            subprocess.run(["rm", "-rf", d])

    def test_manual_run_errors_out_after_bounded_wait(self):
        d = _fresh_project()
        holder = None
        try:
            company = os.path.join(d, ".company")
            holder = self._hold_lock(company, 5)
            self._await_ready(company)
            start = time.monotonic()
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"],
                      env={"SELF_COMPANY_MANUAL_LOCK_WAIT": "1"})
            elapsed = time.monotonic() - start
            self.assertNotEqual(r.returncode, 0, "manual run must ERROR, not hang forever")
            self.assertLess(elapsed, 4, "must bound the wait, not hang indefinitely")
            self.assertIn("could not acquire", r.stderr.lower())
        finally:
            if holder:
                holder.wait()
            subprocess.run(["rm", "-rf", d])

    def _hold_lock(self, company, hold_secs):
        ops = os.path.join(company, "ops")
        os.makedirs(ops, exist_ok=True)
        lock = os.path.join(ops, ".daily.lock")
        ready = os.path.join(company, "lock.ready")
        script = f'exec 9>"{lock}"; flock 9; : > "{ready}"; sleep {hold_secs}'
        return subprocess.Popen(["bash", "-c", script])

    def _await_ready(self, company):
        ready = os.path.join(company, "lock.ready")
        for _ in range(200):
            if os.path.exists(ready):
                return
            time.sleep(0.02)
        self.fail("lock holder never acquired the lock")


class TestC3BackupIntegrity(unittest.TestCase):
    """C3 (Gibby F7): tar-to-tmp-then-mv on success / rm -f on failure; the
    rotation glob counts only DATED snapshots (mem-[0-9]*)."""

    def test_special_named_backups_excluded_from_rotation(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            with open(os.path.join(company, "org", "policy.md"), "a") as f:
                f.write("\n| `BACKUP_KEEP` | **2** | test override | ✓ |\n")
            bdir = os.path.join(company, "backups")
            os.makedirs(bdir)
            specials = ("mem-premanual-20260101T000000Z.tar.gz",
                       "mem-preL2demote-20260101T000000Z.tar.gz")
            for name in specials:
                with open(os.path.join(bdir, name), "w") as f:
                    f.write("special")
            _write_mem(company, "obs-fresh")
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            files = sorted(os.listdir(bdir))
            for name in specials:
                self.assertIn(name, files)  # never touched by rotation
            dated = [f for f in files if f not in specials]
            self.assertEqual(len(dated), 1)  # only today's fresh dated snapshot
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_failed_tar_leaves_no_new_tarball(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            bindir = _fake_tar_always_fails(d)
            _write_mem(company, "obs-fresh")
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"],
                      env={"PATH": bindir + os.pathsep + os.environ["PATH"]})
            self.assertEqual(r.returncode, 0, r.stderr)
            bdir = os.path.join(company, "backups")
            if os.path.isdir(bdir):
                self.assertEqual(os.listdir(bdir), [])
        finally:
            subprocess.run(["rm", "-rf", d])


def _read_jsonl_end(company, date=None):
    """Test helper: the LAST 'end' event in today's (or `date`'s) JSONL."""
    date = date or _today()
    path = os.path.join(company, "ops", "logs", f"daily-{date}.jsonl")
    with open(path) as f:
        lines = [json.loads(ln) for ln in f if ln.strip()]
    ends = [e for e in lines if e.get("event") == "end"]
    return ends[-1] if ends else None


# NOTE: elon_survey.py (later in the same pipeline) ALSO invokes decay.py /
# verify_memory.py / entropy.py internally as a read-only subprocess with NO
# timeout of its own, distinguished by lacking "--apply" in argv (its call is
# `--now <date>`, no --apply). If we blindly hang on every invocation, THAT
# untimed nested call hangs forever too. So the sleeper only hangs when
# "--apply" is present (daily-run.sh's own direct, timeout-wrapped core-loop
# call) and behaves as an instant no-op otherwise (matches every other caller).
_SLEEPER_IGNORE_TERM_APPLY_ONLY = (
    "import signal, sys, time\n"
    "if '--apply' not in sys.argv:\n"
    "    print('{}')\n"
    "    sys.exit(0)\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "while True:\n"
    "    time.sleep(0.05)\n"
)


class TestItem4CoreStepTimeout(unittest.TestCase):
    """Phase 27 Item 4: each bare core step is bounded by
    `timeout -k GRACE BUDGET`. A black-holed step (fastembed download, etc.)
    can no longer hold .daily.lock for hours."""

    def _replace_with_sleeper(self, d, script_name):
        path = os.path.join(d, "scripts", script_name)
        with open(path, "w") as f:
            f.write(_SLEEPER_IGNORE_TERM_APPLY_ONLY)

    def test_a_timed_out_step_distinct_log_jsonl_later_steps_run_lock_released(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-a", last_reinforced=_today())
            self._replace_with_sleeper(d, "decay.py")
            env = {"SELF_COMPANY_CORE_STEP_TIMEOUT": "1",
                   "SELF_COMPANY_CORE_STEP_KILL_GRACE": "1"}
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertIn("- decay: TIMED OUT after 1s", text)
            self.assertIn("step skipped this tick, will retry next run", text)
            # later steps still ran (core continues past a step timeout)
            self.assertIn("- entropy", text)
            end = _read_jsonl_end(company)
            self.assertEqual(end["steps"]["decay"]["outcome"], "timeout")
            # lock released: a second, immediate cron tick does not lock-skip
            # (same short budget — decay.py is still the sleeper, but that's
            # irrelevant here: we're only proving the FIRST run's lock didn't
            # leak past its own exit).
            r2 = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent", "--cron"], env=env)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertNotIn("cron tick SKIPPED", _read_log(company))
            # run classified non-flat/keep via report.py
            rc, out, err = _helpers.run_script("report.py", "--company", company)
            self.assertEqual(rc, 0, err)
            self.assertIn("`warn`", out)
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_b_sigterm_ignoring_step_is_sigkilled_within_grace(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-a", last_reinforced=_today())
            self._replace_with_sleeper(d, "decay.py")
            env = {"SELF_COMPANY_CORE_STEP_TIMEOUT": "1",
                   "SELF_COMPANY_CORE_STEP_KILL_GRACE": "1"}
            start = time.monotonic()
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"], env=env)
            elapsed = time.monotonic() - start
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLess(elapsed, 30, "budget+grace must bound the step, not hang")
            self.assertIn("- decay: TIMED OUT after 1s", _read_log(company))
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_c_healthy_run_zero_timing_change_byte_identical_md(self):
        # A healthy run never comes close to the 900s default budget — the
        # .md render must be untouched by Item 4's plumbing.
        d1 = _fresh_project()
        d2 = _fresh_project()
        try:
            c1, c2 = os.path.join(d1, ".company"), os.path.join(d2, ".company")
            _write_mem(c1, "obs-a", last_reinforced=_today())
            _write_mem(c2, "obs-a", last_reinforced=_today())
            r1 = _bash([os.path.join(d1, "scripts", "daily-run.sh"), d1, "--no-agent"])
            r2 = _bash([os.path.join(d2, "scripts", "daily-run.sh"), d2, "--no-agent"],
                      env={"SELF_COMPANY_CORE_STEP_TIMEOUT": "900"})
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            # Normalize the two independently-timed runs' own wall-clock
            # stamps (header ts + backup filename ts) before comparing — the
            # invariant under test is Item 4 changed nothing else in the
            # render, not that two separate processes share a timestamp.
            import re as _re
            def _normalize(text, project_dir):
                text = _re.sub(r"## Daily run \S+", "## Daily run TS", text)
                text = _re.sub(r"mem-\d{8}T\d{6}Z", "mem-TS", text)
                text = text.replace(project_dir, "PROJECT_DIR")
                return text
            self.assertEqual(_normalize(_read_log(c1), d1), _normalize(_read_log(c2), d2))
        finally:
            subprocess.run(["rm", "-rf", d1])
            subprocess.run(["rm", "-rf", d2])


def _instrument_call_counter(d, script_name, counter_path):
    """Insert a one-line side effect right after script_name's shebang that
    appends one char to counter_path every time the script is executed as
    __main__ — proves invocation COUNT without touching the script's real
    behaviour (the rest of the file, and every line number after the insert,
    is untouched; decay.py/verify_memory.py have no venv re-exec, so this
    counts true process invocations 1:1)."""
    path = os.path.join(d, "scripts", script_name)
    with open(path) as f:
        lines = f.readlines()
    insert_at = 1 if lines and lines[0].startswith("#!") else 0
    lines.insert(insert_at,
                 "with open(%r, 'a') as _cc_f: _cc_f.write('x')\n" % counter_path)
    with open(path, "w") as f:
        f.writelines(lines)


class TestItem1SurveyFedNoDoubleRun(unittest.TestCase):
    """Phase 28 Item 1 (Tony C1): daily-run.sh feeds elon_survey the core's own
    $EOUT/$DOUT/$VOUT (--no-recompute) instead of letting the survey re-invoke
    decay/verify as fresh subprocesses minutes later. Net: each runs exactly
    ONCE per tick, not twice."""

    def test_decay_and_verify_invoked_exactly_once_per_tick(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-a", last_reinforced=_today())
            counters = {}
            for name in ("decay.py", "verify_memory.py"):
                cp = os.path.join(d, name + ".count")
                open(cp, "w").close()
                _instrument_call_counter(d, name, cp)
                counters[name] = cp
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            for name, cp in counters.items():
                with open(cp) as f:
                    n = len(f.read())
                self.assertEqual(n, 1, "%s invoked %d time(s), want exactly 1" % (name, n))
            # The survey still reports real numbers from THIS tick's core output
            # (not "no output" / an empty fed set) — the fed JSON actually fed it.
            text = _read_log(company)
            self.assertIn("- elon survey:", text)
            self.assertNotIn("- elon survey: no output", text)
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_gated_survey_step_still_never_recomputes(self):
        # elon.survey gated off for this tick -> the survey block doesn't even
        # run, so decay/verify (the CORE's own calls) still run exactly once
        # each — never a fed-mode recompute sneaking in through the gate.
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            org = os.path.join(company, "org")
            os.makedirs(org, exist_ok=True)
            with open(os.path.join(org, "schedule.yaml"), "w") as f:
                f.write("elon: { cadence: on-trigger }\n")
            _write_mem(company, "obs-a", last_reinforced=_today())
            counters = {}
            for name in ("decay.py", "verify_memory.py"):
                cp = os.path.join(d, name + ".count")
                open(cp, "w").close()
                _instrument_call_counter(d, name, cp)
                counters[name] = cp
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            for name, cp in counters.items():
                with open(cp) as f:
                    n = len(f.read())
                self.assertEqual(n, 1, "%s invoked %d time(s), want exactly 1" % (name, n))
            self.assertIn("gated off elon.survey", _read_log(company))
        finally:
            subprocess.run(["rm", "-rf", d])


class TestItem5Prune(unittest.TestCase):
    """Phase 27 Item 5: age-prune wired into daily-run.sh's end-of-run."""

    def test_prune_line_appears_and_old_logs_removed(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            logs = os.path.join(company, "ops", "logs")
            os.makedirs(logs, exist_ok=True)
            old_date = "2020-01-01"
            open(os.path.join(logs, f"daily-{old_date}.md"), "w").close()
            open(os.path.join(logs, f"agent-{old_date}.log"), "w").close()
            open(os.path.join(logs, f".agent_runs_{old_date}"), "w").close()
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("- prune:", _read_log(company))
            self.assertFalse(os.path.exists(os.path.join(logs, f"daily-{old_date}.md")))
            self.assertFalse(os.path.exists(os.path.join(logs, f"agent-{old_date}.log")))
            self.assertFalse(os.path.exists(os.path.join(logs, f".agent_runs_{old_date}")))
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_prune_never_fatal_on_low_retain_days_refusal(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"],
                      env={"SELF_COMPANY_LOG_RETAIN_DAYS": "1"})   # < window+1 -> refused
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("- prune: prune refused", _read_log(company))
        finally:
            subprocess.run(["rm", "-rf", d])


def company_of(d):
    return os.path.join(d, ".company")


class TestInstallHook(unittest.TestCase):
    SH = os.path.join(REPO, "plugin", "skills", "self-company", "scripts","install-hook.sh")

    def _settings(self, d):
        return os.path.join(d, ".claude", "settings.json")

    def _write_legacy(self, d):
        """Seed a project settings.json with the pre-v0.1.2 self-company hooks."""
        import json
        os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
        cfg = {
            "permissions": {"allow": ["Bash(ls)"]},
            "hooks": {
                "Stop": [{"hooks": [{"type": "command",
                    "command": "python3 capture-trigger.py  # self-company-capture"}]}],
                "SessionStart": [{"hooks": [{"type": "command",
                    "command": "python3 notify-status.py  # self-company-notify"}]}],
            },
        }
        with open(self._settings(d), "w") as f:
            json.dump(cfg, f)

    def test_install_command_removed_is_usage_error(self):
        # Phase 14 Bucket 3: the deprecated `install` no-op branch was removed
        # (hooks are plugin-native). `install` is no longer a command -> usage
        # error (exit 2), and it must still never create/touch settings.json.
        with tempfile.TemporaryDirectory() as d:
            r = _bash([self.SH, "install", d])
            self.assertEqual(r.returncode, 2)
            self.assertIn("usage:", r.stderr.lower())
            self.assertFalse(os.path.exists(self._settings(d)))

    def test_status_reports_plugin_native(self):
        with tempfile.TemporaryDirectory() as d:
            r0 = _bash([self.SH, "status", d])
            self.assertIn("plugin-native", r0.stdout.lower())
            self.assertIn("no legacy", r0.stdout.lower())
            self._write_legacy(d)
            r1 = _bash([self.SH, "status", d])
            self.assertIn("legacy", r1.stdout.lower())
            self.assertIn("double-fir", r1.stdout.lower())

    def test_uninstall_removes_legacy_and_keeps_other_settings(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            self._write_legacy(d)
            r = _bash([self.SH, "uninstall", d])
            self.assertEqual(r.returncode, 0)
            self.assertIn("removed", r.stdout.lower())
            with open(self._settings(d)) as f:
                cfg = json.load(f)
            self.assertNotIn("hooks", cfg)  # legacy self-company entries gone
            self.assertEqual(cfg["permissions"]["allow"], ["Bash(ls)"])  # preserved


class TestScheduleGuards(unittest.TestCase):
    def test_bad_command_exits_2(self):
        r = _bash([os.path.join(REPO, "plugin", "skills", "self-company", "scripts","schedule.sh"), "bogus", "/tmp"])
        self.assertEqual(r.returncode, 2)

    def test_install_without_company_errors(self):
        with tempfile.TemporaryDirectory() as d:
            r = _bash([os.path.join(REPO, "plugin", "skills", "self-company", "scripts","schedule.sh"), "install", d])
            self.assertEqual(r.returncode, 1)
            self.assertIn(".company not found", r.stderr)


class TestSkeletonGuard(unittest.TestCase):
    SH = os.path.join(REPO, "plugin", "skills", "self-company", "scripts","skeleton_guard.sh")

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


def _counting_python3(d, real_python, counter_path):
    """Plant a fake `python3` that appends one char to counter_path for every
    invocation whose argv mentions schedule_config.py, then execs the REAL
    interpreter (by absolute path, captured BEFORE this fakebin dir is
    prepended to PATH) so daily-run.sh's actual python calls still work."""
    bindir = os.path.join(d, "fakepybin")
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, "python3")
    with open(path, "w") as f:
        f.write(
            '#!/usr/bin/env bash\n'
            'case "$*" in\n'
            f'  *schedule_config.py*) printf x >> "{counter_path}" ;;\n'
            'esac\n'
            f'exec "{real_python}" "$@"\n'
        )
    os.chmod(path, 0o755)
    return bindir


class TestPlanTickSpawnCount(unittest.TestCase):
    """Phase 28 Item 3: daily-run.sh sources ONE schedule_config.py --plan-tick
    call (plus the pre-existing, unrelated --roster call) instead of spawning
    schedule_config.py per gate/knob question."""

    def _run_counted(self, d, company, write_yaml=True):
        org = os.path.join(company, "org")
        os.makedirs(org, exist_ok=True)
        yaml_path = os.path.join(org, "schedule.yaml")
        if write_yaml:
            with open(yaml_path, "w") as f:
                f.write("cadence: every 6h\n")
        elif os.path.exists(yaml_path):
            # init_company.sh ships an all-comments schedule.yaml template
            # (schedule_config.py itself treats that as absent-equivalent, but
            # the FILE existing is what today's `_should_run` guard — and this
            # phase's plan-tick guard — actually checks). Remove it so this
            # case is genuinely no-file, not just no-content.
            os.remove(yaml_path)
        _write_mem(company, "obs-a", last_reinforced=_today())
        counter = os.path.join(d, "sc_calls.txt")
        open(counter, "w").close()
        bindir = _counting_python3(d, sys.executable, counter)
        env = {"PATH": bindir + os.pathsep + os.environ["PATH"]}
        r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"], env=env)
        with open(counter) as f:
            calls = f.read()
        return r, calls

    def test_tick_with_yaml_spawns_at_most_twice(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            r, calls = self._run_counted(d, company, write_yaml=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            # ONE --plan-tick call + the pre-existing, unrelated --roster call
            # (Item 3 explicitly leaves --roster alone: "renders a file, not a
            # gate"). Net: ~14 -> 2.
            self.assertLessEqual(len(calls), 2, calls)
        finally:
            subprocess.run(["rm", "-rf", d])

    def test_no_yaml_only_the_roster_spawn_remains(self):
        # Item 3 acceptance (c): "no schedule.yaml -> zero [gating] spawns" —
        # the ~13 --should-run/--agent spawns collapse to zero; the ONE
        # unrelated --roster call (never gated on schedule.yaml, renders
        # today's defaults either way) is untouched by this item.
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            r, calls = self._run_counted(d, company, write_yaml=False)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(len(calls), 1, calls)
        finally:
            subprocess.run(["rm", "-rf", d])


class TestPlanTickFailOpen(unittest.TestCase):
    """Phase 28 Item 3 acceptance (d): a --plan-tick call that crashes or
    returns garbage must fail OPEN — every step still runs, byte-identical to
    today's per-call fail-open — never silently suppress maintenance."""

    def test_garbage_plan_tick_output_fails_open(self):
        d = _fresh_project()
        try:
            company = os.path.join(d, ".company")
            org = os.path.join(company, "org")
            os.makedirs(org, exist_ok=True)
            # This yaml would gate decay OFF if the plan were trusted (tony's
            # duties omit decay).
            with open(os.path.join(org, "schedule.yaml"), "w") as f:
                f.write("tony: { duties: [reinforce] }\n")
            # Replace this fixture's OWN scripts/schedule_config.py copy with a
            # stub that returns non-JSON garbage for --plan-tick (exit 0 — an
            # exit-code lie) and fails everything else (--roster degrades
            # separately/harmlessly, already-tolerated).
            sc_path = os.path.join(d, "scripts", "schedule_config.py")
            with open(sc_path, "w") as f:
                f.write(
                    "#!/usr/bin/env python3\n"
                    "import sys\n"
                    'if "--plan-tick" in sys.argv:\n'
                    '    print("not { json")\n'
                    "    sys.exit(0)\n"
                    "sys.exit(1)\n"
                )
            _write_mem(company, "obs-a", last_reinforced=_today())
            r = _bash([os.path.join(d, "scripts", "daily-run.sh"), d, "--no-agent"])
            self.assertEqual(r.returncode, 0, r.stderr)
            text = _read_log(company)
            self.assertNotIn("gated off tony.decay", text)
            self.assertIn("- decay --apply:", text)   # decay actually ran
        finally:
            subprocess.run(["rm", "-rf", d])


if __name__ == "__main__":
    unittest.main()
