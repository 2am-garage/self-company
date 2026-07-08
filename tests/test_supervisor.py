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
import time
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


class TestSharedMemoryAtDispatch(unittest.TestCase):
    """Phase 18c — the SHARED company-memory read wired INTO dispatch, for a
    `shared_memory_read` employee (elon by default). real_command injects a
    distinct "Relevant company memory" block; a non-shared-read employee never
    gets it; the shared block is deduped against + shares the budget with the
    own-store block; and the worker env carries the double-injection guard."""

    def setUp(self):
        self._orig_recall = emp_model.Employee.recall
        self._orig_shared = emp_model.Employee.recall_shared

    def tearDown(self):
        emp_model.Employee.recall = self._orig_recall
        emp_model.Employee.recall_shared = self._orig_shared

    def _company(self, d, ids=("elon", "tony", "bob")):
        base = os.path.join(d, ".company", "org", "employees")
        for i in ids:
            os.makedirs(os.path.join(base, i))
            open(os.path.join(base, i, "persona.md"), "w").close()
        return os.path.join(d, ".company")

    def _stub_recall(self, hits):
        emp_model.Employee.recall = lambda self, query, top_k=3: list(hits)

    def _stub_shared(self, hits):
        emp_model.Employee.recall_shared = lambda self, query, top_k=3: list(hits)

    def _prompt(self, member, task):
        return member.real_command(task)[2]

    def test_shared_read_employee_injects_company_memory(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            self._stub_recall([])                                  # no own-store hit
            self._stub_shared([{"text": "Chairman wants no-attribution commits.",
                                "id": "sh1"}])
            elon = sv.Member("elon", company_dir=c)                # shared_read on
            prompt = self._prompt(elon, "prepare a commit")
            self.assertIn("Relevant company memory", prompt)
            self.assertIn("no-attribution commits", prompt)

    def test_non_shared_read_employee_never_gets_company_block(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            # Even if recall_shared WOULD return hits, a non-shared-read employee
            # must inject nothing shared (recall_shared_context short-circuits).
            self._stub_recall([{"text": "Tony's own past diagnosis.", "id": "o1"}])
            self._stub_shared([{"text": "should never appear for tony", "id": "x"}])
            tony = sv.Member("tony", company_dir=c)                # rag, shared OFF
            prompt = self._prompt(tony, "diagnose entropy")
            self.assertIn("Relevant past experience", prompt)      # own store fires
            self.assertNotIn("Relevant company memory", prompt)    # shared does not
            self.assertNotIn("should never appear", prompt)

    def test_flat_non_shared_employee_injects_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            self._stub_recall([{"text": "own", "id": "o"}])
            self._stub_shared([{"text": "shared", "id": "s"}])
            bob = sv.Member("bob", company_dir=c)                  # flat + shared OFF
            prompt = self._prompt(bob, "build it")
            self.assertNotIn("Relevant past experience", prompt)
            self.assertNotIn("Relevant company memory", prompt)

    def test_both_blocks_present_and_separate(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            self._stub_recall([{"text": "Own: fail-open the validator.", "id": "o1"}])
            self._stub_shared([{"text": "Shared: ship small, reversible changes.",
                                "id": "s1"}])
            elon = sv.Member("elon", company_dir=c)
            prompt = self._prompt(elon, "plan the release")
            self.assertIn("Relevant past experience", prompt)
            self.assertIn("Relevant company memory", prompt)
            # The two headers are distinct sections (own block precedes shared).
            self.assertLess(prompt.index("Relevant past experience"),
                            prompt.index("Relevant company memory"))
            self.assertIn("fail-open the validator", prompt)
            self.assertIn("ship small", prompt)

    def test_shared_deduped_against_own_store(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            same = "The one shared lesson, recorded in both stores."
            self._stub_recall([{"text": same, "id": "own"}])
            self._stub_shared([{"text": same, "id": "shared"}])   # dup content
            elon = sv.Member("elon", company_dir=c)
            prompt = self._prompt(elon, "x")
            self.assertIn("Relevant past experience", prompt)
            # Own-store wins: the shared block has nothing left -> no company header.
            self.assertNotIn("Relevant company memory", prompt)
            self.assertEqual(prompt.count(same), 1)               # included once

    def test_both_blocks_respect_overall_budget(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            big = "y" * (emp_model._RECALL_SNIPPET_CHARS * 4)
            self._stub_recall([{"text": big, "id": "o1"}, {"text": big, "id": "o2"},
                               {"text": big, "id": "o3"}])
            self._stub_shared([{"text": big, "id": "s1"}, {"text": big, "id": "s2"}])
            elon = sv.Member("elon", company_dir=c)
            prompt = self._prompt(elon, "big")
            # Only the injected memory region is budget-bound; approximate by the
            # length of everything after the first memory header.
            region = prompt[prompt.index("Relevant past experience"):]
            self.assertLessEqual(len(region), emp_model._DISPATCH_INJECT_BUDGET + 4)

    def test_worker_env_sets_guard_only_for_shared_read(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            elon_env = sv.Member("elon", company_dir=c).worker_env()
            self.assertIsNotNone(elon_env)
            self.assertEqual(elon_env.get("SC_NO_MEMORY_INJECT"), "1")
            # A rag-but-not-shared employee and a flat employee inherit unchanged.
            self.assertIsNone(sv.Member("tony", company_dir=c).worker_env())
            self.assertIsNone(sv.Member("bob", company_dir=c).worker_env())

    def test_shared_recall_error_never_breaks_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            self._stub_recall([])

            def _boom(self, query, top_k=3):
                raise RuntimeError("shared recall blew up")

            emp_model.Employee.recall_shared = _boom
            elon = sv.Member("elon", company_dir=c)
            prompt = self._prompt(elon, "anything")               # must not raise
            self.assertNotIn("Relevant company memory", prompt)
            self.assertIn("@status", prompt)                      # prompt intact


class TestDispatchBudget(unittest.TestCase):
    """Phase 19 Item 3 (TOM-1) — the live dispatch path is BOUNDED: a worker that
    never reaches EOF is killed at its wall-clock budget, dispatch returns cleanly,
    and the session-triggered company-run.sh can never hang."""

    def _company(self, d, ids=("bob",)):
        base = os.path.join(d, ".company", "org", "employees")
        for i in ids:
            os.makedirs(os.path.join(base, i))
            open(os.path.join(base, i, "persona.md"), "w").close()
        return os.path.join(d, ".company")

    def test_hung_worker_killed_at_budget_and_dispatch_returns(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            orig = sv.Member.real_command
            # a worker that emits nothing, never EOFs, and IGNORES SIGTERM
            sv.Member.real_command = lambda self, task, model="m": [
                "bash", "-c", "trap '' TERM; while :; do sleep 0.2; done"]
            os.environ["SELF_COMPANY_DISPATCH_TIMEOUT"] = "1"
            os.environ["SELF_COMPANY_DISPATCH_KILL_AFTER"] = "1"
            try:
                sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
                t0 = time.monotonic()
                workers = sup.dispatch({"bob": "hang please"}, demo=False)
                elapsed = time.monotonic() - t0
            finally:
                sv.Member.real_command = orig
                os.environ.pop("SELF_COMPANY_DISPATCH_TIMEOUT", None)
                os.environ.pop("SELF_COMPANY_DISPATCH_KILL_AFTER", None)
            self.assertLess(elapsed, 15, "dispatch must not hang on a stalled worker")
            self.assertEqual(workers["bob"].status, sv.Status.FAILED)  # killed -> failed

    def test_normal_fast_worker_unaffected(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            orig = sv.Member.real_command
            sv.Member.real_command = lambda self, task, model="m": [
                "bash", "-c", "echo '@status done'"]
            os.environ["SELF_COMPANY_DISPATCH_TIMEOUT"] = "30"
            try:
                sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
                workers = sup.dispatch({"bob": "quick"}, demo=False)
            finally:
                sv.Member.real_command = orig
                os.environ.pop("SELF_COMPANY_DISPATCH_TIMEOUT", None)
            self.assertEqual(workers["bob"].status, sv.Status.DONE)
            self.assertFalse(workers["bob"].timed_out)

    def test_wrap_timeout_shape(self):
        # real workers are spawned under `timeout -k <grace> <budget>` (Item-1 parity)
        cmd = sv._wrap_timeout(["claude", "-p", "x", "--model", "m"], 42, 7)
        self.assertEqual(cmd[:4], ["timeout", "-k", "7", "42"])
        self.assertEqual(cmd[4:], ["claude", "-p", "x", "--model", "m"])

    def test_kill_over_budget_marks_failed_and_reaps(self):
        emp = sv.Member("bob")
        w = sv.Worker(emp, "t", ["bash", "-c", "sleep 30"], budget=0.1)
        w.start()
        pid = w.proc.pid
        time.sleep(0.3)
        self.assertTrue(w.over_budget())
        w.kill_over_budget()
        self.assertEqual(w.status, sv.Status.FAILED)
        self.assertTrue(w.timed_out)
        alive = True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
        if alive:
            os.kill(pid, 9)
        self.assertFalse(alive, "kill_over_budget must reap the process")

    def test_real_command_still_bare_claude_at_index_2(self):
        # Contract preserved: real_command returns the BARE claude cmd with the
        # prompt at [2] (the timeout wrap happens in dispatch, not here) — the
        # recall-injection tests depend on cmd[2] being the prompt.
        m = sv.Member("bob")
        cmd = m.real_command("do a thing")
        self.assertEqual(cmd[0], "claude")
        self.assertEqual(cmd[1], "-p")
        self.assertIn("do a thing", cmd[2])

    def test_demo_workers_are_unbounded(self):
        # Demo workers (trusted local echoes) get no budget — never wrapped/killed.
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
            workers = sup.dispatch({"bob": "demo"}, demo=True, demo_delay=0.0)
            self.assertIsNone(workers["bob"].budget)
            self.assertEqual(workers["bob"].status, sv.Status.DONE)


if __name__ == "__main__":
    unittest.main()
