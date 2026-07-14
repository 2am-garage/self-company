"""
Tests for company-run.sh's Phase 29 fold-in H3: hardened Phoebe plan parsing.

Today (before H3): a hallucinated `{"alice": "do the thing"}` plan is passed
straight to supervisor.py --dispatch, which silently `continue`s past the
unknown id and dispatches NOBODY — a no-op that still ledgers as a clean run.

H3 fix: the plan call runs with `--output-format json` (one parseable
envelope; the model's answer lives in `.result`); every returned key is
validated against the REAL roster (supervisor.py --list); unknown keys are
dropped with a logged warning and a `plan:partial` ledger annotation; a plan
that has NOTHING valid left falls back to the heuristic bob+gibby plan
labeled `heuristic-after-invalid-plan` (never a silent empty dispatch); parse
failures are logged, never swallowed.

Drives the REAL script against a fake `claude` that returns a fixture
`--output-format json` envelope for the PLAN call, and a fast canned
stream-json success for the DISPATCH call (so no real LLM call happens and
the whole cycle completes in well under a second).
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import _helpers

SCRIPT = os.path.join(_helpers.SCRIPTS_DIR, "company-run.sh")


def _company(d, ids=("elon", "phoebe", "bob", "gibby", "tom")):
    base = os.path.join(d, ".company", "org", "employees")
    for i in ids:
        os.makedirs(os.path.join(base, i))
        open(os.path.join(base, i, "persona.md"), "w").close()
    os.makedirs(os.path.join(d, ".company", "scripts"))
    return os.path.join(d, ".company")


# Phase 33: the verification gate arms whenever a real (non-demo) dispatch
# pairs bob+gibby — which every plan fixture below does (a named plan or the
# heuristic fallback). Gibby's dispatch prompt now carries a MANDATORY
# "write a JSON verdict marker to <path>" output-contract clause (Phase 33
# Item 1); supervisor.py reads it after Gibby's run and re-loops on a
# missing/failing one. A fake `claude` that never writes that marker would
# turn every one of these `rc == 0` fixtures into a 3-round UNRESOLVED
# (rc == 1) — not a Phase-33 regression, but this fixture no longer models a
# real Gibby. This snippet, appended to each dispatch-branch, greps its own
# prompt arg for the marker path the contract embeds and writes a PASS
# verdict there — mirroring a Gibby that genuinely found nothing on round 1.
_WRITE_VERDICT_MARKER_SNIPPET = """
  for a in "$@"; do
    case "$a" in
      *qa-verdict-*.json*)
        marker="$(printf '%s' "$a" | grep -oE '/[^ ]*qa-verdict-[^ ]*\\.json' | head -1)"
        if [[ -n "$marker" ]]; then
          mkdir -p "$(dirname "$marker")"
          printf '%s' '{"run_id":"fake","target":"x","verdict":"pass","checked":["fake"],"ts":"now"}' > "$marker"
        fi
        ;;
    esac
  done
"""


def _fake_claude(bindir, plan_result_text):
    """A `claude` stub: for the PLAN call (`--output-format json`) returns a
    JSON envelope whose `.result` is `plan_result_text` (fixture-controlled);
    for the DISPATCH call (`--output-format stream-json`) returns a fast
    canned success so supervisor.py's real (non-demo) dispatch completes
    quickly without a real LLM call. Also writes Gibby's Phase-33 verdict
    marker when its dispatch prompt carries one (see the snippet above)."""
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, "claude")
    envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": plan_result_text,
    })
    body = f"""#!/usr/bin/env bash
is_plan=false
for a in "$@"; do
  if [[ "$a" == "json" ]]; then is_plan=true; fi
done
if $is_plan; then
  cat <<'FIXTURE_EOF'
{envelope}
FIXTURE_EOF
else
{_WRITE_VERDICT_MARKER_SNIPPET}
  echo '{{"type":"assistant","message":{{"content":[{{"type":"text","text":"@status done"}}]}}}}'
  echo '{{"type":"result","subtype":"success","is_error":false,"result":"ok"}}'
fi
exit 0
"""
    with open(path, "w") as f:
        f.write(body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_claude_errored(bindir):
    """`claude` stub that reports is_error=true for the plan call."""
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, "claude")
    body = f"""#!/usr/bin/env bash
is_plan=false
for a in "$@"; do
  if [[ "$a" == "json" ]]; then is_plan=true; fi
done
if $is_plan; then
  echo '{{"type":"result","subtype":"error_max_turns","is_error":true,"result":null}}'
else
{_WRITE_VERDICT_MARKER_SNIPPET}
  echo '{{"type":"result","subtype":"success","is_error":false,"result":"ok"}}'
fi
exit 0
"""
    with open(path, "w") as f:
        f.write(body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_claude_garbage(bindir):
    """`claude` stub that returns non-JSON garbage for the plan call."""
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, "claude")
    body = f"""#!/usr/bin/env bash
is_plan=false
for a in "$@"; do
  if [[ "$a" == "json" ]]; then is_plan=true; fi
done
if $is_plan; then
  echo 'not json at all, just noise'
else
{_WRITE_VERDICT_MARKER_SNIPPET}
  echo '{{"type":"result","subtype":"success","is_error":false,"result":"ok"}}'
fi
exit 0
"""
    with open(path, "w") as f:
        f.write(body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.company = _company(self.tmp)
        self.bindir = os.path.join(self.tmp, "bin")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, extra_env=None):
        env = {**os.environ, "PATH": self.bindir + os.pathsep + os.environ.get("PATH", ""),
              "SELF_COMPANY_PROJECT_DIR": self.tmp}
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", SCRIPT, "do the thing", "--company", self.company],
            capture_output=True, text=True, env=env, timeout=60)

    def _ledger_body(self):
        ledger = os.path.join(self.company, "ops", "reports", "company-runs.md")
        with open(ledger, encoding="utf-8") as f:
            return f.read()


class TestCleanJSON(Base):
    def test_clean_json_dispatches_named_employees(self):
        _fake_claude(self.bindir, '{"bob":"build it","gibby":"verify it"}')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("plan (Phoebe):", r.stdout)
        self.assertNotIn("plan:partial", r.stdout)
        body = self._ledger_body()
        self.assertIn("Phoebe", body)
        self.assertNotIn("heuristic", body)


class TestJSONPlusProse(Base):
    def test_json_embedded_in_prose_still_parses(self):
        _fake_claude(self.bindir,
                    'Sure! Here is the plan:\n{"bob":"build it"}\nHope that helps.')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("plan (Phoebe):", r.stdout)
        body = self._ledger_body()
        self.assertIn("Phoebe", body)


class TestHallucinatedEmployee(Base):
    def test_partial_hallucination_drops_unknown_keeps_valid(self):
        _fake_claude(self.bindir, '{"bob":"build it","alice":"do something"}')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("plan:partial", r.stdout)
        plan_log_dir = os.path.join(self.company, "ops", "logs")
        logs = [f for f in os.listdir(plan_log_dir) if f.startswith("company-run-plan-")]
        self.assertEqual(len(logs), 1)
        with open(os.path.join(plan_log_dir, logs[0]), encoding="utf-8") as f:
            log_body = f.read()
        self.assertIn("alice", log_body)
        self.assertIn("dropped", log_body)
        body = self._ledger_body()
        self.assertIn("plan:partial", body)


class TestAllHallucinated(Base):
    def test_all_hallucinated_falls_back_to_heuristic(self):
        _fake_claude(self.bindir, '{"alice":"do something","zed":"do another"}')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("heuristic-after-invalid-plan", r.stdout)
        body = self._ledger_body()
        self.assertIn("heuristic-after-invalid-plan", body)
        # Real work still happened (bob+gibby heuristic), never a silent no-op:
        self.assertIn("bob", body)

    def test_all_hallucinated_logs_dropped_ids(self):
        _fake_claude(self.bindir, '{"alice":"x"}')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        plan_log_dir = os.path.join(self.company, "ops", "logs")
        logs = [f for f in os.listdir(plan_log_dir) if f.startswith("company-run-plan-")]
        with open(os.path.join(plan_log_dir, logs[0]), encoding="utf-8") as f:
            log_body = f.read()
        self.assertIn("alice", log_body)


class TestGarbageOutput(Base):
    def test_garbage_output_logged_and_falls_back(self):
        _fake_claude_garbage(self.bindir)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("heuristic-after-invalid-plan", r.stdout)
        plan_log_dir = os.path.join(self.company, "ops", "logs")
        logs = [f for f in os.listdir(plan_log_dir) if f.startswith("company-run-plan-")]
        self.assertEqual(len(logs), 1)
        with open(os.path.join(plan_log_dir, logs[0]), encoding="utf-8") as f:
            log_body = f.read()
        self.assertIn("parse", log_body.lower())    # parse failure logged, not silent
        self.assertTrue(len(log_body.strip()) > 0)


class TestErrorEnvelope(Base):
    def test_is_error_envelope_falls_back_and_logs(self):
        _fake_claude_errored(self.bindir)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("heuristic-after-invalid-plan", r.stdout)
        plan_log_dir = os.path.join(self.company, "ops", "logs")
        logs = [f for f in os.listdir(plan_log_dir) if f.startswith("company-run-plan-")]
        with open(os.path.join(plan_log_dir, logs[0]), encoding="utf-8") as f:
            log_body = f.read()
        self.assertIn("error", log_body.lower())


if __name__ == "__main__":
    unittest.main()
