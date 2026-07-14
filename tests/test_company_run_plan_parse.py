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


# Phase 33 (security redesign): the verification gate arms whenever a real
# (non-demo) dispatch contains a builder — which every plan fixture below does
# (a named bob plan or the heuristic bob+gibby fallback). Gibby's dispatch
# prompt now carries a MANDATORY output-contract clause requiring it to print
# a `@qa-verdict {json}` sentinel ON ITS OWN STDOUT (NOT a shared-fs file that
# Bob could forge — that was Gibby's break). supervisor.py reads it off
# Gibby's pipe fd and re-loops on a missing/failing verdict. A fake `claude`
# that never emits it would turn every `rc == 0` fixture into a 3-round
# UNRESOLVED — so this snippet, in the dispatch branch, detects the contract
# in its OWN prompt (only Gibby's prompt carries `@qa-verdict`) and emits a
# PASS sentinel as a stream-json assistant-text event, mirroring a real Gibby
# that found nothing on round 1. Bob's prompt has no such clause, so Bob never
# emits it — matching production, where only Gibby's fd is the trusted channel.
_EMIT_VERDICT_SNIPPET = r"""
  emit_verdict=false
  for a in "$@"; do
    case "$a" in
      *@qa-verdict*) emit_verdict=true ;;
    esac
  done
  if $emit_verdict; then
    printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"text","text":"@qa-verdict {\"verdict\":\"pass\",\"target\":\"x\",\"checked\":[\"fake\"]}"}]}}'
  fi
"""


def _fake_claude(bindir, plan_result_text):
    """A `claude` stub: for the PLAN call (`--output-format json`) returns a
    JSON envelope whose `.result` is `plan_result_text` (fixture-controlled);
    for the DISPATCH call (`--output-format stream-json`) returns a fast
    canned success so supervisor.py's real (non-demo) dispatch completes
    quickly without a real LLM call. Also emits Gibby's Phase-33 `@qa-verdict`
    stdout sentinel when its dispatch prompt carries the contract (snippet)."""
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
{_EMIT_VERDICT_SNIPPET}
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
{_EMIT_VERDICT_SNIPPET}
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
{_EMIT_VERDICT_SNIPPET}
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

    def test_ledger_records_redblue_rounds_and_verdict(self):
        # Phase 33 Item 2: a real bob+gibby dispatch arms the verification
        # gate; the fake claude's Gibby call writes a passing verdict marker
        # on round 1 (see _EMIT_VERDICT_SNIPPET), so the ledger row
        # should record exactly 1 round and a "clean" verdict — not the "-"
        # placeholder a lone-worker/non-gated dispatch would get.
        _fake_claude(self.bindir, '{"bob":"build it","gibby":"verify it"}')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self._ledger_body()
        rows = [ln for ln in body.splitlines() if ln.startswith("| 20")]
        self.assertEqual(len(rows), 1)
        cells = [c.strip() for c in rows[0].strip("|").split("|")]
        self.assertEqual(cells[-2], "1")           # rounds
        self.assertEqual(cells[-1], "clean")       # verdict


class TestJSONPlusProse(Base):
    def test_json_embedded_in_prose_still_parses(self):
        _fake_claude(self.bindir,
                    'Sure! Here is the plan:\n{"bob":"build it"}\nHope that helps.')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("plan (Phoebe):", r.stdout)
        body = self._ledger_body()
        self.assertIn("Phoebe", body)

    def test_builder_only_plan_auto_arms_gibby(self):
        # Phase 33 FIX B (Finding 4): a bob-ONLY plan (Phoebe dropped the gibby
        # key) MUST still verify — Gibby is auto-injected, so the ledger records
        # a real gate cycle (1 round, clean), NOT an unverified lone-worker pass.
        _fake_claude(self.bindir, '{"bob":"build it"}')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self._ledger_body()
        rows = [ln for ln in body.splitlines() if ln.startswith("| 20")]
        self.assertEqual(len(rows), 1)
        cells = [c.strip() for c in rows[0].strip("|").split("|")]
        self.assertEqual(cells[-2], "1")           # rounds
        self.assertEqual(cells[-1], "clean")       # verdict

    def test_non_builder_lone_plan_ledgers_placeholder_rounds_verdict(self):
        # Phase 33 FIX B: a genuinely non-builder lone task (tom backup) is
        # UNCHANGED — no gate arms, so the trailing columns stay "-"/"-".
        _fake_claude(self.bindir, '{"tom":"run a backup"}')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self._ledger_body()
        rows = [ln for ln in body.splitlines() if ln.startswith("| 20")]
        self.assertEqual(len(rows), 1)
        cells = [c.strip() for c in rows[0].strip("|").split("|")]
        self.assertEqual(cells[-2], "-")
        self.assertEqual(cells[-1], "-")


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


def _fake_claude_never_verifies(bindir, plan_result_text):
    """A `claude` stub whose Gibby NEVER emits a `@qa-verdict` sentinel — the
    end-to-end UNRESOLVED-at-cap path (spec §2: 'cap-without-pass ⇒ stop,
    mark the cycle UNRESOLVED (loud), never silently done'). Every worker
    just echoes '@status done' — company-run.sh's rc must go non-zero."""
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


class TestRedBlueUnresolved(Base):
    def test_gibby_never_passes_ends_unresolved_and_rc_nonzero(self):
        _fake_claude_never_verifies(self.bindir, '{"bob":"build it","gibby":"verify it"}')
        r = self._run(extra_env={"SELF_COMPANY_REDBLUE_MAX_ROUNDS": "2"})
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)   # fail LOUD
        self.assertIn("UNRESOLVED", r.stderr)
        body = self._ledger_body()
        rows = [ln for ln in body.splitlines() if ln.startswith("| 20")]
        self.assertEqual(len(rows), 1)
        cells = [c.strip() for c in rows[0].strip("|").split("|")]
        self.assertEqual(cells[-2], "2")            # cap respected exactly
        self.assertEqual(cells[-1], "unresolved")


def _fake_claude_forges_ledger_marker(bindir):
    """FIX 2 (Finding 3) end-to-end regression: Gibby NEVER emits a verdict
    (so the true outcome is UNRESOLVED), but Bob FORGES the old shared-fs
    ledger marker `ops/.last-redblue-gate.json` with a "clean" verdict. The
    ledger must reflect the TRUE unresolved (read from the supervisor's stderr
    channel), proving the forgeable file is ignored. The marker path is passed
    to Bob via the SC_FORGE_MARKER env var (a worker inherits the env)."""
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, "claude")
    body = """#!/usr/bin/env bash
is_plan=false; has_verdict=false
for a in "$@"; do
  case "$a" in
    json) is_plan=true ;;
    *@qa-verdict*) has_verdict=true ;;
  esac
done
if $is_plan; then
  echo '{"type":"result","subtype":"success","is_error":false,"result":"{\\"bob\\":\\"build it\\",\\"gibby\\":\\"verify it\\"}"}'
  exit 0
fi
# Bob (no @qa-verdict contract) forges the old ledger marker file:
if ! $has_verdict && [[ -n "${SC_FORGE_MARKER:-}" ]]; then
  mkdir -p "$(dirname "$SC_FORGE_MARKER")"
  printf '%s' '{"rounds":1,"verdict":"clean","builder":"bob","attacker":"gibby"}' > "$SC_FORGE_MARKER"
fi
# Gibby never emits @qa-verdict -> true verdict is UNRESOLVED
echo '{"type":"result","subtype":"success","is_error":false,"result":"ok"}'
exit 0
"""
    with open(path, "w") as f:
        f.write(body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class TestLedgerForgeryResisted(Base):
    def test_bob_forged_marker_file_cannot_flip_ledger_verdict(self):
        forge = os.path.join(self.company, "ops", ".last-redblue-gate.json")
        _fake_claude_forges_ledger_marker(self.bindir)
        r = self._run(extra_env={"SELF_COMPANY_REDBLUE_MAX_ROUNDS": "1",
                                 "SC_FORGE_MARKER": forge})
        # Bob actually wrote the forged file...
        self.assertTrue(os.path.exists(forge), "fixture should have forged the marker")
        with open(forge, encoding="utf-8") as f:
            forged = json.load(f)
        self.assertEqual(forged["verdict"], "clean")
        # ...but the ledger reflects the TRUE unresolved from the stderr channel.
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        body = self._ledger_body()
        rows = [ln for ln in body.splitlines() if ln.startswith("| 20")]
        self.assertEqual(len(rows), 1)
        cells = [c.strip() for c in rows[0].strip("|").split("|")]
        self.assertEqual(cells[-1], "unresolved")   # NOT the forged "clean"


class TestNonBuilderBuildWorkRefused(Base):
    def test_phoebe_routing_build_work_to_non_builder_is_refused(self):
        # FIX 3 (Finding 1 defense-in-depth): a plan routing code-mutation work
        # to a non-builder (tom) is REFUSED at the supervisor — company-run
        # exits non-zero and ledgers the refusal, never a silent unverified run.
        _fake_claude(self.bindir, '{"tom":"refactor the backup logic in scripts/foo.sh"}')
        r = self._run()
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("REFUSED", r.stderr)
        body = self._ledger_body()
        rows = [ln for ln in body.splitlines() if ln.startswith("| 20")]
        self.assertEqual(len(rows), 1)
        cells = [c.strip() for c in rows[0].strip("|").split("|")]
        self.assertEqual(cells[-1], "refused")


if __name__ == "__main__":
    unittest.main()
