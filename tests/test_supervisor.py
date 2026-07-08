"""
Tests for supervisor.py — the skill's OWN live-orchestration harness (Plan C).

Deterministic: builds a temp org/employees/ tree, runs the demo dispatch (real
child processes via bash, delay 0 for speed), and checks the OOP pieces —
Member roster discovery, Worker @status parsing/transitions, Supervisor running
all workers to completion with a live event log, and LiveTree rendering.
"""

import importlib.util
import io
import os
import tempfile
import unittest

import _helpers

_spec = importlib.util.spec_from_file_location(
    "supervisor", os.path.join(_helpers.SCRIPTS_DIR, "supervisor.py"))
sv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sv)


def _company(d, ids=("elon", "phoebe", "tony", "bob")):
    base = os.path.join(d, ".company", "org", "employees")
    for i in ids:
        os.makedirs(os.path.join(base, i))
        open(os.path.join(base, i, "persona.md"), "w").close()
    return os.path.join(d, ".company")


class TestMember(unittest.TestCase):
    def test_roster_discovers_all_and_orders(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, ids=("bob", "elon", "tony"))
            ids = [e.id for e in sv.Member.roster(c)]
            # known ones come in canonical order (elon, tony, bob), all present
            self.assertEqual(ids, ["elon", "tony", "bob"])

    def test_roster_includes_unknown_employees(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, ids=("elon", "zara"))          # zara not in ORDER
            ids = [e.id for e in sv.Member.roster(c)]
            self.assertIn("zara", ids)                      # discovered, not hardcoded


class TestWorker(unittest.TestCase):
    def test_status_parsing_and_transitions(self):
        emp = sv.Member("bob")
        w = sv.Worker(emp, "t", ["true"])
        self.assertEqual(w.status, sv.Status.IDLE)
        w.consume_line("@status planning\n")
        self.assertEqual(w.status, sv.Status.WORKING)
        self.assertEqual(w.phase, "planning")
        w.consume_line("some log line\n")
        self.assertEqual(w.last, "some log line")           # non-status = log
        w.consume_line("@status done\n")
        self.assertEqual(w.status, sv.Status.DONE)


class TestSupervisor(unittest.TestCase):
    def test_demo_dispatch_runs_all_to_done(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d)
            events = []
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()),
                                event_log=events)
            sup.renderer.roster = sup.roster            # render all
            workers = sup.dispatch({e.id: "demo" for e in sup.roster},
                                   demo=True, demo_delay=0.0)
            self.assertEqual(set(workers), {"elon", "phoebe", "tony", "bob"})
            for w in workers.values():
                self.assertEqual(w.status, sv.Status.DONE)
                self.assertIn("planning", [l.replace("@status ", "")
                                           for l in w.lines if l.startswith("@status ")])
            # event log captured start/status/end for every employee
            kinds = {(e["emp"], e["kind"]) for e in events}
            for i in ("elon", "phoebe", "tony", "bob"):
                self.assertIn((i, "start"), kinds)
                self.assertIn((i, "end"), kinds)

    def test_unknown_assignee_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d)
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
            workers = sup.dispatch({"nobody": "x", "bob": "y"}, demo=True, demo_delay=0.0)
            self.assertEqual(set(workers), {"bob"})


class TestLiveTree(unittest.TestCase):
    def test_final_lists_every_employee(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d)
            roster = sv.Member.roster(c)
            buf = io.StringIO()
            tree = sv.LiveTree(roster, stream=buf)
            tree.final({})                                  # nothing running -> all idle
            out = buf.getvalue()
            for name in ("Elon", "Phoebe", "Tony", "Bob"):
                self.assertIn(name, out)
            self.assertIn("idle", out)


import employee as emp_model                                   # the DATA MODEL


class TestDispatchRecallInjection(unittest.TestCase):
    """Phase 18 Item 4 — recall-at-dispatch is now WIRED: Member.real_command
    injects a rag employee's OWN relevant past experience into the worker prompt
    (via the employee.py bridge), a flat employee injects nothing, and any
    recall failure/absence degrades to no-injection without raising."""

    def setUp(self):
        # stash the real recall so each test can stub it and restore cleanly
        self._orig_recall = emp_model.Employee.recall

    def tearDown(self):
        emp_model.Employee.recall = self._orig_recall

    def _company(self, d, ids=("tony", "bob")):
        base = os.path.join(d, ".company", "org", "employees")
        for i in ids:
            os.makedirs(os.path.join(base, i))
            open(os.path.join(base, i, "persona.md"), "w").close()
        return os.path.join(d, ".company")

    def _stub_recall(self, hits):
        emp_model.Employee.recall = lambda self, query, top_k=3: list(hits)

    def _prompt(self, member, task):
        cmd = member.real_command(task)
        return cmd[2]                                          # ["claude","-p",PROMPT,...]

    def test_rag_employee_injects_experience_when_recall_hits(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            self._stub_recall([{"text": "Past build: a validator needs a fail-OPEN default."}])
            tony = sv.Member("tony", company_dir=c)            # rag by default table
            prompt = self._prompt(tony, "build a schedule validator")
            self.assertIn("Relevant past experience", prompt)
            self.assertIn("fail-OPEN default", prompt)

    def test_flat_employee_never_injects(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            # stub recall to RETURN hits — a flat employee must STILL inject nothing
            # (recall_context short-circuits on flat before ever calling recall).
            self._stub_recall([{"text": "should never appear for a flat employee"}])
            bob = sv.Member("bob", company_dir=c)              # flat by default table
            prompt = self._prompt(bob, "build the feature")
            self.assertNotIn("Relevant past experience", prompt)
            self.assertNotIn("should never appear", prompt)

    def test_no_venv_recall_degrades_to_no_injection_no_raise(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)                              # no .rag-venv, no index
            tony = sv.Member("tony", company_dir=c)           # rag, but recall -> []
            prompt = self._prompt(tony, "consolidate memory")  # must not raise
            self.assertNotIn("Relevant past experience", prompt)

    def test_recall_error_never_breaks_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)

            def _boom(self, query, top_k=3):
                raise RuntimeError("recall blew up")

            emp_model.Employee.recall = _boom
            tony = sv.Member("tony", company_dir=c)
            # recall_context wraps recall in try/except -> "" ; dispatch proceeds.
            prompt = self._prompt(tony, "anything")
            self.assertNotIn("Relevant past experience", prompt)
            self.assertIn("@status", prompt)                  # the normal prompt is intact

    def test_injection_budget_cap_respected(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            cap = emp_model._RECALL_SNIPPET_CHARS
            long_text = "x" * (cap * 3)
            self._stub_recall([{"text": long_text}])
            tony = sv.Member("tony", company_dir=c)
            prompt = self._prompt(tony, "big task")
            self.assertIn("Relevant past experience", prompt)
            # the recalled line is truncated to the budget (+ ellipsis), not the full body
            exp_line = next(l for l in prompt.splitlines() if l.startswith("- "))
            self.assertTrue(exp_line.endswith("…"))
            self.assertLessEqual(len(exp_line), cap + 4)       # "- " + snippet + "…"
            self.assertNotIn("x" * (cap + 1), prompt)          # full body never injected


if __name__ == "__main__":
    unittest.main()
