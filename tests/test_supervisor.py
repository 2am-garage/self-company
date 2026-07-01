"""
Tests for supervisor.py — the skill's OWN live-orchestration harness (Plan C).

Deterministic: builds a temp org/employees/ tree, runs the demo dispatch (real
child processes via bash, delay 0 for speed), and checks the OOP pieces —
Employee roster discovery, Worker @status parsing/transitions, Supervisor running
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


class TestEmployee(unittest.TestCase):
    def test_roster_discovers_all_and_orders(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, ids=("bob", "elon", "tony"))
            ids = [e.id for e in sv.Employee.roster(c)]
            # known ones come in canonical order (elon, tony, bob), all present
            self.assertEqual(ids, ["elon", "tony", "bob"])

    def test_roster_includes_unknown_employees(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, ids=("elon", "zara"))          # zara not in ORDER
            ids = [e.id for e in sv.Employee.roster(c)]
            self.assertIn("zara", ids)                      # discovered, not hardcoded


class TestWorker(unittest.TestCase):
    def test_status_parsing_and_transitions(self):
        emp = sv.Employee("bob")
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
            roster = sv.Employee.roster(c)
            buf = io.StringIO()
            tree = sv.LiveTree(roster, stream=buf)
            tree.final({})                                  # nothing running -> all idle
            out = buf.getvalue()
            for name in ("Elon", "Phoebe", "Tony", "Bob"):
                self.assertIn(name, out)
            self.assertIn("idle", out)


if __name__ == "__main__":
    unittest.main()
