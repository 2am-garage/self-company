"""
Tests for Phase 33 — the machine-enforced Bob<->Gibby verification gate.

spec: .company/ops/plans/spec-phase33-verification-gate.md

Elon's mechanism correction (spec §0): workers are `claude -p` subprocesses,
not Task-tool subagents, so a SubagentStop hook never fires for them. The
gate lives at the supervisor DISPATCH layer instead (supervisor.py), which
already owns the red/blue cycle.

Covers (spec §5):
  * marker round-trip classification: pass/fail/malformed/absent
  * the bounded re-loop: fail->re-dispatch, clean-in-2, never-pass->UNRESOLVED
    at the exact cap (no off-by-one, no infinite loop)
  * cap validation: 0/-1/999999/junk all clamp to the default
  * a non-red/blue (lone-worker) dispatch is unaffected — byte-identical
  * the ledger records rounds used + final verdict (company-run.sh, see
    test_company_run_plan_parse.py's TestCleanJSON for the end-to-end case)

All dispatch here is `demo=False` with `Member.real_command` MONKEYPATCHED to
a fast local `bash` command (the established pattern in test_supervisor.py's
TestDispatchBudget/TestModelRouting) — no real `claude -p` is ever spawned.
"""

import io
import json
import os
import tempfile
import unittest

import _helpers
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "supervisor", os.path.join(_helpers.SCRIPTS_DIR, "supervisor.py"))
sv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sv)


def _redblue_company(d, ids=("bob", "gibby")):
    """A minimal valid desk (persona.md + context.md) for each id — the same
    strict per-desk predicate Member.roster() enforces (Phase 32 hotfix)."""
    base = os.path.join(d, ".company", "org", "employees")
    for i in ids:
        os.makedirs(os.path.join(base, i))
        open(os.path.join(base, i, "persona.md"), "w").close()
        with open(os.path.join(base, i, "context.md"), "w") as f:
            f.write("---\nname: %s\n---\n" % i.capitalize())
    return os.path.join(d, ".company")


# ============================================================ marker round-trip
class TestQaVerdictPath(unittest.TestCase):
    def test_shape(self):
        p = sv.qa_verdict_path("/tmp/co", "run1-r1")
        self.assertEqual(str(p), "/tmp/co/ops/reports/qa-verdict-run1-r1.json")


class TestReadQaVerdict(unittest.TestCase):
    def _write(self, d, name, content):
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_pass_marker_is_clean(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "m.json", json.dumps(
                {"run_id": "r1", "target": "x", "verdict": "pass",
                 "checked": ["injection"], "ts": "2026-07-14T00:00:00"}))
            result = sv.read_qa_verdict(p)
            self.assertTrue(result["clean"])
            self.assertEqual(result["verdict"], "pass")
            self.assertEqual(result["checked"], ["injection"])

    def test_fail_marker_not_clean(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "m.json", json.dumps(
                {"run_id": "r1", "target": "x", "verdict": "fail",
                 "checked": ["boundary"], "ts": "now"}))
            result = sv.read_qa_verdict(p)
            self.assertFalse(result["clean"])
            self.assertEqual(result["verdict"], "fail")

    def test_absent_marker_not_clean(self):
        with tempfile.TemporaryDirectory() as d:
            result = sv.read_qa_verdict(os.path.join(d, "never-written.json"))
            self.assertFalse(result["clean"])
            self.assertEqual(result["verdict"], "missing")

    def test_invalid_json_syntax_not_clean_never_raises(self):
        # Syntactically broken JSON is caught by the outer try/except (same
        # path as an OS read error) rather than the inner shape check —
        # either way: not clean, never a crash (spec §3's fail-loud rule
        # doesn't care WHICH not-clean bucket it lands in).
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "m.json", "{not json at all")
            result = sv.read_qa_verdict(p)          # must not raise
            self.assertFalse(result["clean"])
            self.assertIn(result["verdict"], ("malformed", "error"))

    def test_non_dict_json_not_clean(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "m.json", json.dumps(["pass"]))
            result = sv.read_qa_verdict(p)
            self.assertFalse(result["clean"])
            self.assertEqual(result["verdict"], "malformed")

    def test_missing_verdict_field_not_clean(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "m.json", json.dumps({"run_id": "r1"}))
            result = sv.read_qa_verdict(p)
            self.assertFalse(result["clean"])
            self.assertEqual(result["verdict"], "malformed")

    def test_verdict_not_pass_or_fail_not_clean(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "m.json", json.dumps({"verdict": "maybe"}))
            result = sv.read_qa_verdict(p)
            self.assertFalse(result["clean"])
            self.assertEqual(result["verdict"], "malformed")

    def test_unreadable_marker_path_never_crashes(self):
        # A path that IS a directory (read_text -> IsADirectoryError) is
        # exactly the kind of "trouble reading the marker" spec §3 requires
        # to degrade to not-clean, never to a supervisor crash.
        with tempfile.TemporaryDirectory() as d:
            trap = os.path.join(d, "a-directory.json")
            os.makedirs(trap)
            result = sv.read_qa_verdict(trap)        # must not raise
            self.assertFalse(result["clean"])
            self.assertEqual(result["verdict"], "error")


# ==================================================================== pairing
class TestRedBluePairIds(unittest.TestCase):
    def test_default_pair_is_bob_gibby(self):
        builder, attacker = sv._redblue_pair_ids()
        self.assertEqual(builder, "bob")
        self.assertEqual(attacker, "gibby")


# =============================================================== cap validation
class TestRedBlueMaxRounds(unittest.TestCase):
    def setUp(self):
        os.environ.pop("SELF_COMPANY_REDBLUE_MAX_ROUNDS", None)

    def tearDown(self):
        os.environ.pop("SELF_COMPANY_REDBLUE_MAX_ROUNDS", None)

    def test_default_is_three(self):
        self.assertEqual(sv._redblue_max_rounds(), 3)

    def test_valid_override_respected(self):
        os.environ["SELF_COMPANY_REDBLUE_MAX_ROUNDS"] = "5"
        self.assertEqual(sv._redblue_max_rounds(), 5)

    def test_zero_clamps_to_default(self):
        os.environ["SELF_COMPANY_REDBLUE_MAX_ROUNDS"] = "0"
        self.assertEqual(sv._redblue_max_rounds(), 3)

    def test_negative_clamps_to_default(self):
        os.environ["SELF_COMPANY_REDBLUE_MAX_ROUNDS"] = "-1"
        self.assertEqual(sv._redblue_max_rounds(), 3)

    def test_absurdly_large_clamps_to_default(self):
        # spec §3: the cap must not be tunable to effectively-infinite.
        os.environ["SELF_COMPANY_REDBLUE_MAX_ROUNDS"] = "999999"
        self.assertEqual(sv._redblue_max_rounds(), 3)

    def test_junk_clamps_to_default(self):
        os.environ["SELF_COMPANY_REDBLUE_MAX_ROUNDS"] = "junk"
        self.assertEqual(sv._redblue_max_rounds(), 3)

    def test_whitespace_padded_value_still_parses(self):
        os.environ["SELF_COMPANY_REDBLUE_MAX_ROUNDS"] = " 4 "
        self.assertEqual(sv._redblue_max_rounds(), 4)


# ===================================================== Gibby's output contract
class TestVerdictContractInPrompt(unittest.TestCase):
    def test_real_command_carries_marker_path_and_verdict_language(self):
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d)
            gibby = sv.Member("gibby", company_dir=c)
            marker = sv.qa_verdict_path(c, "run1-r1")
            contract = sv._verdict_contract(marker)
            prompt = gibby.real_command("verify it", extra_contract=contract)[2]
            self.assertIn(str(marker), prompt)
            self.assertIn("verdict", prompt)
            self.assertIn("MANDATORY", prompt)

    def test_extra_contract_omitted_when_none_prompt_unaffected(self):
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d)
            gibby = sv.Member("gibby", company_dir=c)
            with_none = gibby.real_command("verify it")[2]
            with_explicit_none = gibby.real_command("verify it", extra_contract=None)[2]
            self.assertEqual(with_none, with_explicit_none)


# ============================================================== the re-loop
class TestRedBlueReloop(unittest.TestCase):
    """Drives Supervisor.dispatch() end-to-end with Member.real_command
    monkeypatched to a scripted fake that writes (or withholds/corrupts) the
    verdict marker at the EXACT path the real code computes
    (qa_verdict_path(company_dir, f"{run_id}-r{round_no}")) — deterministic
    because the test supplies its own `run_id`, never parses prompt text."""

    def setUp(self):
        self._orig_real_command = sv.Member.real_command

    def tearDown(self):
        sv.Member.real_command = self._orig_real_command
        os.environ.pop("SELF_COMPANY_REDBLUE_MAX_ROUNDS", None)

    def _script(self, company, run_id, verdicts):
        """verdicts[i] is round (i+1)'s outcome for gibby:
        "pass" | "fail" | "absent" (write nothing) | "malformed" (bad JSON).
        bob is always a fast no-op echo."""
        calls = {"n": 0}

        def _fake(self_emp, task, default_model=None, extra_contract=None):
            if self_emp.id != "gibby" or extra_contract is None:
                return ["bash", "-c", "echo '@status done'"]
            round_no = calls["n"] + 1
            calls["n"] += 1
            outcome = verdicts[min(round_no - 1, len(verdicts) - 1)]
            marker = sv.qa_verdict_path(company, f"{run_id}-r{round_no}")
            if outcome == "absent":
                return ["bash", "-c", "echo '@status done'"]
            body = ("not json" if outcome == "malformed" else
                    json.dumps({"run_id": run_id, "target": task,
                                "verdict": outcome, "checked": ["x"], "ts": "now"}))
            script = (f"mkdir -p '{marker.parent}' && cat > '{marker}' <<'MARKER_EOF'\n"
                     f"{body}\nMARKER_EOF\necho '@status done'")
            return ["bash", "-c", script]

        return _fake, calls

    def test_pass_on_round_one_is_clean_immediately(self):
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d)
            fake, calls = self._script(c, "rb1", ["pass"])
            sv.Member.real_command = fake
            events = []
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()),
                                event_log=events)
            workers = sup.dispatch({"bob": "build it", "gibby": "verify it"},
                                   demo=False, run_id="rb1")
            self.assertEqual(calls["n"], 1)
            self.assertEqual(sup.last_gate["verdict"], "clean")
            self.assertEqual(sup.last_gate["rounds"], 1)
            self.assertEqual({w for w in workers}, {"bob", "gibby"})
            starts = [e for e in events if e["kind"] == "start"]
            self.assertEqual(len([e for e in starts if e["emp"] == "bob"]), 1)
            self.assertEqual(len([e for e in starts if e["emp"] == "gibby"]), 1)

    def test_fail_then_pass_is_clean_in_two_rounds(self):
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d)
            fake, calls = self._script(c, "rb2", ["fail", "pass"])
            sv.Member.real_command = fake
            events = []
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()),
                                event_log=events)
            sup.dispatch({"bob": "build it", "gibby": "verify it"},
                        demo=False, run_id="rb2")
            self.assertEqual(sup.last_gate["verdict"], "clean")
            self.assertEqual(sup.last_gate["rounds"], 2)
            starts = [e for e in events if e["kind"] == "start"]
            # Bob and Gibby each re-dispatched — exactly 2 rounds, not 1, not 3.
            self.assertEqual(len([e for e in starts if e["emp"] == "bob"]), 2)
            self.assertEqual(len([e for e in starts if e["emp"] == "gibby"]), 2)

    def test_never_pass_is_unresolved_at_default_cap(self):
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d)
            fake, calls = self._script(c, "rb3", ["fail", "fail", "fail", "fail"])
            sv.Member.real_command = fake
            events = []
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()),
                                event_log=events)
            sup.dispatch({"bob": "build it", "gibby": "verify it"},
                        demo=False, run_id="rb3")
            self.assertEqual(sup.last_gate["verdict"], "unresolved")
            self.assertEqual(sup.last_gate["rounds"], 3)     # default cap, exact
            self.assertEqual(calls["n"], 3)                  # never a 4th round
            starts = [e for e in events if e["kind"] == "start"]
            self.assertEqual(len([e for e in starts if e["emp"] == "bob"]), 3)
            self.assertEqual(len([e for e in starts if e["emp"] == "gibby"]), 3)

    def test_cap_respected_exactly_with_custom_env(self):
        os.environ["SELF_COMPANY_REDBLUE_MAX_ROUNDS"] = "2"
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d)
            fake, calls = self._script(c, "rb4", ["fail", "fail", "fail"])
            sv.Member.real_command = fake
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
            sup.dispatch({"bob": "build it", "gibby": "verify it"},
                        demo=False, run_id="rb4")
            self.assertEqual(sup.last_gate["rounds"], 2)
            self.assertEqual(sup.last_gate["verdict"], "unresolved")
            self.assertEqual(calls["n"], 2)

    def test_absent_marker_treated_as_not_clean_and_reloops(self):
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d)
            fake, calls = self._script(c, "rb5", ["absent", "pass"])
            sv.Member.real_command = fake
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
            sup.dispatch({"bob": "build it", "gibby": "verify it"},
                        demo=False, run_id="rb5")
            self.assertEqual(sup.last_gate["verdict"], "clean")
            self.assertEqual(sup.last_gate["rounds"], 2)

    def test_malformed_marker_treated_as_not_clean_and_reloops(self):
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d)
            fake, calls = self._script(c, "rb6", ["malformed", "pass"])
            sv.Member.real_command = fake
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
            sup.dispatch({"bob": "build it", "gibby": "verify it"},
                        demo=False, run_id="rb6")
            self.assertEqual(sup.last_gate["verdict"], "clean")
            self.assertEqual(sup.last_gate["rounds"], 2)

    def test_gate_marker_file_written_for_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d)
            fake, calls = self._script(c, "rb7", ["pass"])
            sv.Member.real_command = fake
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
            sup.dispatch({"bob": "build it", "gibby": "verify it"},
                        demo=False, run_id="rb7")
            marker = sv.redblue_gate_marker_path(c)
            self.assertTrue(marker.exists())
            data = json.loads(marker.read_text())
            self.assertEqual(data["rounds"], 1)
            self.assertEqual(data["verdict"], "clean")
            self.assertEqual(data["builder"], "bob")
            self.assertEqual(data["attacker"], "gibby")


# ============================================ non-red/blue dispatch unaffected
class TestNonRedBlueDispatchUnaffected(unittest.TestCase):
    """spec §2: 'Non-red/blue dispatches (a lone worker, no Gibby) are
    unaffected — gate only arms when a builder+Gibby pair is present.' Proves
    it structurally: _dispatch_redblue is simply never invoked, not just that
    the observable result happens to match."""

    def setUp(self):
        self._orig_real_command = sv.Member.real_command
        self._orig_redblue = sv.Supervisor._dispatch_redblue

    def tearDown(self):
        sv.Member.real_command = self._orig_real_command
        sv.Supervisor._dispatch_redblue = self._orig_redblue

    def _poison_gate(self):
        def _boom(self, *a, **kw):
            raise AssertionError("_dispatch_redblue must not be called")
        sv.Supervisor._dispatch_redblue = _boom

    def test_lone_builder_never_arms_gate(self):
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d, ids=("bob",))
            self._poison_gate()
            sv.Member.real_command = lambda self, task, default_model=None, \
                extra_contract=None: ["bash", "-c", "echo '@status done'"]
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
            workers = sup.dispatch({"bob": "build it"}, demo=False)
            self.assertEqual(set(workers), {"bob"})
            self.assertIsNone(sup.last_gate)

    def test_gibby_alone_never_arms_gate(self):
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d, ids=("gibby",))
            self._poison_gate()
            sv.Member.real_command = lambda self, task, default_model=None, \
                extra_contract=None: ["bash", "-c", "echo '@status done'"]
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
            workers = sup.dispatch({"gibby": "verify it"}, demo=False)
            self.assertEqual(set(workers), {"gibby"})
            self.assertIsNone(sup.last_gate)

    def test_demo_mode_never_arms_gate_even_with_pair(self):
        # supervisor's own --demo (simulate-ALL) mode dispatches bob+gibby
        # with a fixed "demo task" — a trusted local echo, not real QA — so
        # the gate must not arm there (it would turn the existing --demo
        # smoke path into an always-UNRESOLVED 3-round loop).
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d, ids=("bob", "gibby"))
            self._poison_gate()
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
            workers = sup.dispatch({"bob": "demo task", "gibby": "demo task"},
                                   demo=True, demo_delay=0.0)
            self.assertEqual(set(workers), {"bob", "gibby"})
            self.assertIsNone(sup.last_gate)
            for w in workers.values():
                self.assertEqual(w.status, sv.Status.DONE)

    def test_lone_worker_dispatch_cmd_shape_unaffected_by_extra_contract_plumbing(self):
        # A pre-Phase-33-style monkeypatch (2-arg real_command, no
        # extra_contract kwarg) must still work for a non-gated dispatch —
        # proves _dispatch_once never forces the new kwarg on a caller that
        # has no contract to add.
        with tempfile.TemporaryDirectory() as d:
            c = _redblue_company(d, ids=("bob",))
            sv.Member.real_command = lambda self, task, model="m": [
                "bash", "-c", "echo '@status done'"]
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
            workers = sup.dispatch({"bob": "build it"}, demo=False)   # must not raise
            self.assertEqual(workers["bob"].status, sv.Status.DONE)


if __name__ == "__main__":
    unittest.main()
