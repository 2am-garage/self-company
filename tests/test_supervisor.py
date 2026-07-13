"""
Tests for supervisor.py — the skill's OWN live-orchestration harness (Plan C).

Deterministic: builds a temp org/employees/ tree, runs the demo dispatch (real
child processes via bash, delay 0 for speed), and checks the OOP pieces —
Member roster discovery, Worker @status parsing/transitions, Supervisor running
all workers to completion with a live event log, and LiveTree rendering.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import _helpers

_spec = importlib.util.spec_from_file_location(
    "supervisor", os.path.join(_helpers.SCRIPTS_DIR, "supervisor.py"))
sv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sv)


def _company(d, ids=("elon", "phoebe", "tony", "bob")):
    # Phase 32 hotfix: Member.roster() now uses the strict per-desk predicate
    # (employee.is_valid_desk) — a valid desk needs BOTH persona.md AND
    # context.md — so fixtures must create both, not just persona.md.
    base = os.path.join(d, ".company", "org", "employees")
    for i in ids:
        os.makedirs(os.path.join(base, i))
        open(os.path.join(base, i, "persona.md"), "w").close()
        with open(os.path.join(base, i, "context.md"), "w") as f:
            f.write("---\nname: %s\n---\n" % i.capitalize())
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

    def test_order_includes_mike_between_gibby_and_bob(self):
        # Phase 32 fix: ORDER was missing "mike" entirely (display-order bug)
        # — a real mike desk fell through to the "not in ORDER" append-at-end
        # branch instead of its canonical position.
        self.assertIn("mike", sv.ORDER)
        self.assertEqual(sv.ORDER.index("mike"), sv.ORDER.index("gibby") + 1)
        self.assertLess(sv.ORDER.index("mike"), sv.ORDER.index("bob"))

    def test_full_core_roster_orders_mike_correctly(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, ids=("elon", "phoebe", "tony", "gibby", "mike",
                                 "bob", "july", "tom"))
            ids = [e.id for e in sv.Member.roster(c)]
            self.assertEqual(ids, ["elon", "phoebe", "tony", "gibby", "mike",
                                   "bob", "july", "tom"])

    def test_cli_list_orders_mike_correctly(self):
        # End-to-end: `supervisor.py --list` (the CLI seam) reflects the same
        # fixed ORDER, not just the in-process Member.roster() call.
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, ids=("elon", "phoebe", "tony", "gibby", "mike",
                                 "bob", "july", "tom"))
            script = os.path.join(_helpers.SCRIPTS_DIR, "supervisor.py")
            r = subprocess.run([sys.executable, script, "--company", c, "--list"],
                              capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads(r.stdout)
            self.assertEqual(data["roster"], ["elon", "phoebe", "tony", "gibby",
                                              "mike", "bob", "july", "tom"])


class TestRosterStrictMembership(unittest.TestCase):
    """Phase 32 hotfix Finding 2: Member.roster() must share the SAME strict
    per-desk predicate (employee.is_valid_desk) as discover()/R7 — so a desk
    that the validator excludes (persona-only ghost, symlinked persona, bad
    charset) is NOT listed here either and never reaches the live dispatch path
    that inlines persona.md into a worker prompt."""

    def _base(self, d):
        base = os.path.join(d, ".company", "org", "employees")
        os.makedirs(base, exist_ok=True)
        return base

    def _valid_desk(self, base, name):
        desk = os.path.join(base, name)
        os.makedirs(desk, exist_ok=True)
        with open(os.path.join(desk, "persona.md"), "w") as f:
            f.write("persona\n")
        with open(os.path.join(desk, "context.md"), "w") as f:
            f.write("---\nname: %s\n---\n" % name.capitalize())
        return desk

    def _roster_ids(self, company):
        return [e.id for e in sv.Member.roster(company)]

    def test_persona_only_ghost_desk_not_listed(self):
        with tempfile.TemporaryDirectory() as d:
            base = self._base(d)
            for i in ("elon", "phoebe", "tony", "gibby", "mike", "bob", "july", "tom"):
                self._valid_desk(base, i)
            ghost = os.path.join(base, "ghost")           # persona.md ONLY, no context.md
            os.makedirs(ghost)
            with open(os.path.join(ghost, "persona.md"), "w") as f:
                f.write("SMUGGLED GHOST PERSONA\n")
            ids = self._roster_ids(os.path.join(d, ".company"))
            self.assertNotIn("ghost", ids)

    def test_symlinked_persona_desk_not_listed(self):
        with tempfile.TemporaryDirectory() as d:
            base = self._base(d)
            for i in ("elon", "phoebe", "tony", "gibby", "mike", "bob", "july", "tom"):
                self._valid_desk(base, i)
            outside = os.path.join(d, "outside-persona.md")
            with open(outside, "w") as f:
                f.write("OUT-OF-TREE PERSONA\n")
            evil = os.path.join(base, "evil-desk")
            os.makedirs(evil)
            with open(os.path.join(evil, "context.md"), "w") as f:
                f.write("---\nname: Evil\n---\n")
            os.symlink(outside, os.path.join(evil, "persona.md"))
            ids = self._roster_ids(os.path.join(d, ".company"))
            self.assertNotIn("evil-desk", ids)

    def test_bad_charset_desk_not_listed(self):
        with tempfile.TemporaryDirectory() as d:
            base = self._base(d)
            self._valid_desk(base, "bob")
            self._valid_desk(base, "BadCase")            # valid files, invalid id charset
            ids = self._roster_ids(os.path.join(d, ".company"))
            self.assertNotIn("BadCase", ids)

    def test_valid_hired_desk_is_listed_after_core_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            base = self._base(d)
            for i in ("elon", "phoebe", "tony", "gibby", "mike", "bob", "july", "tom"):
                self._valid_desk(base, i)
            self._valid_desk(base, "sam-jr")             # a real hired desk
            ids = self._roster_ids(os.path.join(d, ".company"))
            self.assertIn("sam-jr", ids)
            # core order first (the fixed ORDER), then discovered ids sorted
            self.assertEqual(ids, ["elon", "phoebe", "tony", "gibby", "mike",
                                   "bob", "july", "tom", "sam-jr"])

    def test_zero_hired_desk_list_is_the_core_roster(self):
        # Byte-identity: with all 8 core desks and NO hired desk, --list is the
        # 8 core ids in the fixed ORDER (unchanged from pre-hotfix).
        with tempfile.TemporaryDirectory() as d:
            base = self._base(d)
            for i in ("elon", "phoebe", "tony", "gibby", "mike", "bob", "july", "tom"):
                self._valid_desk(base, i)
            company = os.path.join(d, ".company")
            self.assertEqual(self._roster_ids(company),
                             ["elon", "phoebe", "tony", "gibby", "mike",
                              "bob", "july", "tom"])
            script = os.path.join(_helpers.SCRIPTS_DIR, "supervisor.py")
            r = subprocess.run([sys.executable, script, "--company", company, "--list"],
                              capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["roster"],
                             ["elon", "phoebe", "tony", "gibby", "mike",
                              "bob", "july", "tom"])

    def test_ghost_and_symlink_absent_from_cli_list_too(self):
        # The REAL dispatch seam is `supervisor.py --list`; prove exclusion end-to-end.
        with tempfile.TemporaryDirectory() as d:
            base = self._base(d)
            self._valid_desk(base, "bob")
            os.makedirs(os.path.join(base, "ghost"))
            with open(os.path.join(base, "ghost", "persona.md"), "w") as f:
                f.write("ghost\n")
            outside = os.path.join(d, "out.md")
            with open(outside, "w") as f:
                f.write("out\n")
            evil = os.path.join(base, "evil-desk")
            os.makedirs(evil)
            with open(os.path.join(evil, "context.md"), "w") as f:
                f.write("---\nname: E\n---\n")
            os.symlink(outside, os.path.join(evil, "persona.md"))
            script = os.path.join(_helpers.SCRIPTS_DIR, "supervisor.py")
            r = subprocess.run([sys.executable, script, "--company",
                               os.path.join(d, ".company"), "--list"],
                              capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            roster = json.loads(r.stdout)["roster"]
            self.assertIn("bob", roster)
            self.assertNotIn("ghost", roster)
            self.assertNotIn("evil-desk", roster)


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
            with open(os.path.join(base, i, "context.md"), "w") as f:
                f.write("---\nname: %s\n---\n" % i.capitalize())
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

    def test_partial_line_stall_does_not_block_deadline_or_siblings(self):
        # Item C1 (Phase 26 fold-in / Gibby #4): a worker that emits a PARTIAL
        # line (no trailing '\n') and then stalls used to block the buffered
        # readline() forever, wedging the whole select loop — starving the
        # in-loop deadline check for every OTHER worker too. Non-blocking +
        # manual buffer assembly means the stalled worker is still killed on
        # schedule, and a normal sibling keeps running/finishing unaffected.
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d, ids=("bob", "elon"))
            orig = sv.Member.real_command

            def _cmd(self, task, model="m"):
                if self.id == "bob":
                    # partial line, no newline, then hang (ignoring TERM)
                    return ["bash", "-c",
                            "trap '' TERM; printf '@status working-partial'; "
                            "while :; do sleep 0.2; done"]
                return ["bash", "-c", "echo '@status done'"]

            sv.Member.real_command = _cmd
            os.environ["SELF_COMPANY_DISPATCH_TIMEOUT"] = "1"
            os.environ["SELF_COMPANY_DISPATCH_KILL_AFTER"] = "1"
            try:
                sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()))
                t0 = time.monotonic()
                workers = sup.dispatch({"bob": "hang please", "elon": "quick"},
                                       demo=False)
                elapsed = time.monotonic() - t0
            finally:
                sv.Member.real_command = orig
                os.environ.pop("SELF_COMPANY_DISPATCH_TIMEOUT", None)
                os.environ.pop("SELF_COMPANY_DISPATCH_KILL_AFTER", None)
            self.assertLess(elapsed, 15,
                            "a partial-line stall must not block the deadline check")
            self.assertEqual(workers["bob"].status, sv.Status.FAILED)
            self.assertTrue(workers["bob"].timed_out)
            self.assertEqual(workers["elon"].status, sv.Status.DONE)

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

    def test_stream_json_args_present_by_default(self):
        m = sv.Member("bob")
        cmd = m.real_command("do a thing")
        self.assertIn("--output-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--verbose", cmd)

    def test_self_company_agent_stream_0_restores_plain_text(self):
        # Item 3 acceptance (c): SELF_COMPANY_AGENT_STREAM=0 restores the old
        # plain-text mode — no --output-format/stream-json/--verbose at all.
        os.environ["SELF_COMPANY_AGENT_STREAM"] = "0"
        try:
            m = sv.Member("bob")
            cmd = m.real_command("do a thing")
        finally:
            os.environ.pop("SELF_COMPANY_AGENT_STREAM", None)
        self.assertNotIn("--output-format", cmd)
        self.assertNotIn("stream-json", cmd)
        self.assertNotIn("--verbose", cmd)
        self.assertEqual(cmd[0], "claude")
        self.assertEqual(cmd[1], "-p")


# --------------------------------------------- Phase 29 Item 1: model routing
class TestPromptBuilderIntegration(unittest.TestCase):
    """Phase 29 Item 4 (Bob P1/P2, Mike Idea 7) + P5: real_command's prompt is
    assembled via the shared prompt_builder — role header, stated wall-clock
    budget, an inlined (fence-safe) persona body instead of a "go read it"
    errand, an output contract, and a task boundary (Idea 7's four elements)."""

    def _company(self, d, ids=("bob",), personas=None):
        base = os.path.join(d, ".company", "org", "employees")
        personas = personas or {}
        for i in ids:
            desk = os.path.join(base, i)
            os.makedirs(desk, exist_ok=True)
            with open(os.path.join(desk, "persona.md"), "w", encoding="utf-8") as f:
                f.write(personas.get(i, ""))
        return os.path.join(d, ".company")

    def test_role_header_and_budget_present(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            bob = sv.Member("bob", company_dir=c)
            prompt = bob.real_command("fix the bug")[2]
            self.assertIn("You are Bob (Engineer · builds) in the self-company, working non-interactively.", prompt)
            self.assertIn("wall-clock budget", prompt)

    def test_persona_inlined_not_a_read_errand(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d, personas={"bob": "I am Bob, the Build Engineer. I write tests first."})
            bob = sv.Member("bob", company_dir=c)
            prompt = bob.real_command("fix the bug")[2]
            self.assertIn("I write tests first", prompt)
            self.assertNotIn("Read your persona at", prompt)

    def test_persona_capped_at_budget(self):
        with tempfile.TemporaryDirectory() as d:
            long_persona = "x" * 5000
            c = self._company(d, personas={"bob": long_persona})
            bob = sv.Member("bob", company_dir=c)
            prompt = bob.real_command("fix the bug")[2]
            self.assertIn("x" * 100, prompt)               # some persona text present
            self.assertNotIn("x" * 2001, prompt)           # but not the full 5000 chars
            self.assertIn("…", prompt)                     # truncation marker

    def test_missing_persona_degrades_to_role_only_never_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, ".company", "org", "employees", "bob")
            os.makedirs(base, exist_ok=True)   # NO persona.md at all
            c = os.path.join(d, ".company")
            bob = sv.Member("bob", company_dir=c)
            prompt = bob.real_command("fix the bug")[2]    # must not raise
            self.assertIn("You are Bob", prompt)

    def test_output_contract_and_task_boundary_present(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            bob = sv.Member("bob", company_dir=c)
            prompt = bob.real_command("fix the bug")[2]
            self.assertIn("Output contract:", prompt)
            self.assertIn("Boundaries:", prompt)

    def test_four_idea7_elements_present(self):
        # Mike's Idea 7: objective, output contract, tool/source guidance, task
        # boundaries — all four present in a sample dispatch prompt.
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d, personas={"bob": "Bob's persona."})
            bob = sv.Member("bob", company_dir=c)
            prompt = bob.real_command("fix the bug")[2]
            self.assertIn("Task: fix the bug", prompt)          # objective
            self.assertIn("Output contract:", prompt)           # output contract
            self.assertIn("your granted tools", prompt)         # tool/source guidance
            self.assertIn("Boundaries:", prompt)                # task boundaries

    def test_persona_fenced_with_nonce(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d, personas={"bob": "Bob's persona body."})
            bob = sv.Member("bob", company_dir=c)
            prompt = bob.real_command("fix the bug")[2]
            self.assertRegex(prompt, r"===== PERSONA [0-9a-f]+ =====")
            self.assertIn("never instructions", prompt)


class TestModelRouting(unittest.TestCase):
    """real_command resolves the --model argv from THIS employee's context.md
    (Employee.resolved_model), never the old hardcoded 'claude-sonnet-4-6' —
    and the resolved model (or a degrade warning) is visible on the dispatch
    event log so a real run's per-worker model is provable, not assumed."""

    def _company_with_model(self, d, models):
        base = os.path.join(d, ".company", "org", "employees")
        for name, model in models.items():
            desk = os.path.join(base, name)
            os.makedirs(desk, exist_ok=True)
            open(os.path.join(desk, "persona.md"), "w").close()
            with open(os.path.join(desk, "context.md"), "w", encoding="utf-8") as f:
                f.write(f"---\nname: {name.capitalize()}\nmodel: {model}\n---\n")
        return os.path.join(d, ".company")

    def test_alias_resolves_in_real_command_argv(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company_with_model(d, {"bob": "haiku"})
            bob = sv.Member("bob", company_dir=c)
            cmd = bob.real_command("build it")
            self.assertIn("--model", cmd)
            self.assertEqual(cmd[cmd.index("--model") + 1], "claude-haiku-4-5")

    def test_unset_model_falls_back_to_module_default(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company_with_model(d, {})
            base = os.path.join(c, "org", "employees", "bob")
            os.makedirs(base, exist_ok=True)
            open(os.path.join(base, "persona.md"), "w").close()
            bob = sv.Member("bob", company_dir=c)      # no context.md at all
            cmd = bob.real_command("build it")
            self.assertEqual(cmd[cmd.index("--model") + 1], sv.DEFAULT_MODEL)

    def test_pinned_claude_star_id_passes_through(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company_with_model(d, {"phoebe": "claude-sonnet-4-6"})
            phoebe = sv.Member("phoebe", company_dir=c)
            cmd = phoebe.real_command("plan it")
            self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-4-6")

    @staticmethod
    def _fast_real_command(self, task, default_model=None):
        """Test stub: KEEP real model resolution (Item 1's actual code path),
        but replace the spawn shape with a trivial local echo so dispatch()
        doesn't need a real `claude` binary. `--model <resolved>` stays in the
        argv at the same relative position real_command uses, so both
        `_model_from_cmd` (dispatch's own introspection) and the
        `last_model_warning` side-channel behave exactly as they do for real."""
        if default_model is None:
            default_model = sv.DEFAULT_MODEL
        model, warning = self._resolve_model(default_model)
        self.last_model_warning = warning
        return ["bash", "-c", "echo '@status done'", "--model", model]

    def test_two_employees_show_different_models_in_event_log(self):
        # Acceptance (e): a before/after two-employee dispatch shows TWO
        # DIFFERENT --model values in the event log where today it shows one.
        with tempfile.TemporaryDirectory() as d:
            c = self._company_with_model(d, {"bob": "haiku", "elon": "fable"})
            events = []
            orig = sv.Member.real_command
            sv.Member.real_command = self._fast_real_command
            try:
                sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()),
                                    event_log=events)
                sup.dispatch({"bob": "build", "elon": "decide"}, demo=False)
            finally:
                sv.Member.real_command = orig
            starts = {e["emp"]: e.get("model") for e in events if e["kind"] == "start"}
            self.assertEqual(starts["bob"], "claude-haiku-4-5")
            self.assertEqual(starts["elon"], "claude-fable-5")
            self.assertNotEqual(starts["bob"], starts["elon"])

    def test_invalid_model_warning_surfaces_on_event_log(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company_with_model(d, {"bob": "haiku → sonnet"})
            events = []
            orig = sv.Member.real_command
            sv.Member.real_command = self._fast_real_command
            try:
                sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()),
                                    event_log=events)
                sup.dispatch({"bob": "build"}, demo=False)
            finally:
                sv.Member.real_command = orig
            starts = [e for e in events if e["kind"] == "start" and e["emp"] == "bob"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["model"], sv.DEFAULT_MODEL)
            self.assertIn("model_warning", starts[0])
            self.assertIn("bob", starts[0]["model_warning"])

    def test_real_command_records_warning_on_member_for_emit(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company_with_model(d, {"bob": "haiku → sonnet"})
            bob = sv.Member("bob", company_dir=c)
            bob.real_command("build it")
            self.assertIsNotNone(bob.last_model_warning)
            self.assertIn("bob", bob.last_model_warning)

    def test_default_model_constant_matches_schedule_config(self):
        import schedule_config as sc
        self.assertEqual(sv.DEFAULT_MODEL, sc.DEFAULT_AGENT_MODEL)


# --------------------------------------------- Phase 29 Item 3: stream-json
class TestStreamJsonConsumeLine(unittest.TestCase):
    """Worker.consume_line derives phases from stream-json events (assistant
    tool_use -> phase = tool name; result -> done/failed) instead of relying
    solely on a buffered-to-EOF '@status' marker. Malformed JSON degrades to a
    plain log line; the legacy marker (bare, or embedded in assistant text)
    still works."""

    def _worker(self):
        emp = sv.Member("bob")
        return sv.Worker(emp, "t", ["true"])

    def test_tool_use_event_sets_phase_to_tool_name(self):
        w = self._worker()
        line = __import__("json").dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "1"}]},
        })
        w.consume_line(line)
        self.assertEqual(w.phase, "bash")
        self.assertEqual(w.status, sv.Status.WORKING)

    def test_result_event_sets_done(self):
        w = self._worker()
        line = __import__("json").dumps({"type": "result", "is_error": False})
        w.consume_line(line)
        self.assertEqual(w.phase, "done")
        self.assertEqual(w.status, sv.Status.DONE)

    def test_result_event_error_sets_failed(self):
        w = self._worker()
        line = __import__("json").dumps({"type": "result", "is_error": True})
        w.consume_line(line)
        self.assertEqual(w.phase, "failed")
        self.assertEqual(w.status, sv.Status.FAILED)

    def test_two_distinct_phase_transitions_before_result(self):
        # Acceptance (a): >= 2 distinct phase transitions BEFORE process exit.
        w = self._worker()
        j = __import__("json").dumps
        w.consume_line(j({"type": "assistant",
                          "message": {"content": [{"type": "tool_use", "name": "Read"}]}}))
        self.assertEqual(w.phase, "read")
        w.consume_line(j({"type": "assistant",
                          "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}))
        self.assertEqual(w.phase, "bash")
        self.assertNotEqual(w.status, sv.Status.DONE)   # not done yet — still working
        w.consume_line(j({"type": "result", "is_error": False}))
        self.assertEqual(w.phase, "done")

    def test_embedded_status_marker_inside_assistant_text_honored(self):
        w = self._worker()
        line = __import__("json").dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "working now\n@status reviewing"}]},
        })
        w.consume_line(line)
        self.assertEqual(w.phase, "reviewing")
        self.assertEqual(w.status, sv.Status.WORKING)

    def test_embedded_status_done_inside_text(self):
        w = self._worker()
        line = __import__("json").dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "@status done"}]},
        })
        w.consume_line(line)
        self.assertEqual(w.phase, "done")
        self.assertEqual(w.status, sv.Status.DONE)

    def test_bare_status_marker_still_works(self):
        # Legacy demo protocol / plain-text fallback (SELF_COMPANY_AGENT_STREAM=0).
        w = self._worker()
        w.consume_line("@status planning")
        self.assertEqual(w.phase, "planning")
        self.assertEqual(w.status, sv.Status.WORKING)

    def test_malformed_json_degrades_to_log_text_never_crashes(self):
        w = self._worker()
        w.consume_line('{"type": "assistant", "message": {')   # truncated/malformed
        self.assertEqual(w.last, '{"type": "assistant", "message": {')
        self.assertEqual(w.status, sv.Status.IDLE)          # unchanged, no crash

    def test_status_word_embedded_inside_json_string_not_confused_for_bare_marker(self):
        # A JSON line whose text payload happens to contain '@status' must be
        # parsed as JSON (one json.loads), not string-scraped at the top level.
        w = self._worker()
        line = __import__("json").dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text",
                                     "text": 'reporting: saw literal "@status x" in a log'}]},
        })
        w.consume_line(line)
        # The embedded-status regex requires it at a line start (post ^/\n),
        # not mid-sentence inside quotes — so this should NOT hijack the phase.
        self.assertNotEqual(w.phase, "x")

    def test_non_dict_json_falls_back_to_log_text(self):
        w = self._worker()
        w.consume_line("[1, 2, 3]")
        self.assertEqual(w.last, "[1, 2, 3]")

    def test_giant_line_does_not_crash(self):
        w = self._worker()
        big_text = "x" * 200000
        line = __import__("json").dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": big_text}]},
        })
        w.consume_line(line)   # must not raise
        self.assertEqual(w.status, sv.Status.WORKING)

    def test_real_worker_stream_json_produces_two_phase_transitions_live(self):
        # End-to-end: a real spawned (fake) `claude`-shaped process emitting
        # stream-json produces >= 2 distinct phase transitions before EOF —
        # the acceptance-(a) headline ("today: zero").
        script = (
            "import json,sys,time\n"
            "print(json.dumps({'type':'assistant','message':{'content':"
            "[{'type':'tool_use','name':'Read'}]}}));sys.stdout.flush();time.sleep(0.05)\n"
            "print(json.dumps({'type':'assistant','message':{'content':"
            "[{'type':'tool_use','name':'Bash'}]}}));sys.stdout.flush();time.sleep(0.05)\n"
            "print(json.dumps({'type':'result','is_error':False}))\n"
        )
        import sys as _sys
        emp = sv.Member("bob")
        w = sv.Worker(emp, "t", [_sys.executable, "-c", script])
        w.start()
        phases_seen = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = __import__("select").select([w.fd], [], [], 0.2)
            if ready:
                lines, eof = w.read_available()
                for line in lines:
                    before = w.phase
                    w.consume_line(line)
                    if w.phase != before and w.phase:
                        phases_seen.append(w.phase)
                if eof:
                    w.on_eof()
                    break
        self.assertGreaterEqual(len(set(phases_seen)), 2, phases_seen)
        self.assertEqual(w.status, sv.Status.DONE)


class TestTokenUsage(unittest.TestCase):
    def test_read_write_token_usage(self):
        with tempfile.TemporaryDirectory() as d:
            company_path = os.path.join(d, ".company")
            os.makedirs(os.path.join(company_path, "ops"))
            # Read nonexistent marker: defaults
            usage = sv.read_token_usage(company_path)
            self.assertEqual(usage["input"], 0)
            self.assertEqual(usage["output"], 0)
            self.assertEqual(usage["cost"], 0.0)
            # Write and read back
            sv.write_token_usage(company_path, {"input": 100, "output": 50, "cost": 0.12})
            usage = sv.read_token_usage(company_path)
            self.assertEqual(usage["input"], 100)
            self.assertEqual(usage["output"], 50)
            self.assertAlmostEqual(usage["cost"], 0.12, places=4)

    def test_token_usage_date_reset(self):
        with tempfile.TemporaryDirectory() as d:
            company_path = os.path.join(d, ".company")
            os.makedirs(os.path.join(company_path, "ops"))
            # Write with old date
            marker = sv.token_usage_marker_path(company_path)
            marker.write_text("date=2020-01-01\ninput=1000\noutput=500\ncost=1.23\n")
            # Read should reset to today's defaults
            usage = sv.read_token_usage(company_path)
            self.assertEqual(usage["input"], 0)
            self.assertEqual(usage["output"], 0)
            self.assertEqual(usage["cost"], 0.0)

    def test_result_event_usage_capture(self):
        emp = sv.Member("bob")
        w = sv.Worker(emp, "t", ["true"])
        # Simulate a result event with usage
        result_line = json.dumps({
            "type": "result",
            "is_error": False,
            "usage": {
                "input_tokens": 250,
                "output_tokens": 100,
                "cost": 0.05
            }
        })
        w.consume_line(result_line)
        self.assertEqual(w.usage["input"], 250)
        self.assertEqual(w.usage["output"], 100)
        self.assertAlmostEqual(w.usage["cost"], 0.05, places=4)
        self.assertEqual(w.status, sv.Status.DONE)

    def test_result_event_no_usage(self):
        emp = sv.Member("bob")
        w = sv.Worker(emp, "t", ["true"])
        # Result event without usage field
        result_line = json.dumps({"type": "result", "is_error": False})
        w.consume_line(result_line)
        self.assertEqual(w.usage["input"], 0)
        self.assertEqual(w.usage["output"], 0)
        self.assertEqual(w.usage["cost"], 0.0)
        self.assertEqual(w.status, sv.Status.DONE)

    def test_supervisor_accumulates_usage(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d)
            events = []
            sup = sv.Supervisor(c, renderer=sv.LiveTree([], stream=io.StringIO()),
                                event_log=events)
            # Create workers with mock usage (demo workers)
            workers = {}
            for emp_id in ("bob", "tony"):
                emp = sv.Member(emp_id)
                w = sv.Worker(emp, "task", ["true"])
                w.usage = {"input": 100, "output": 50, "cost": 0.05}
                workers[emp_id] = w
            # Accumulate usage
            sup._accumulate_usage(workers)
            # Verify marker was written
            usage = sv.read_token_usage(c)
            self.assertEqual(usage["input"], 200)
            self.assertEqual(usage["output"], 100)
            self.assertAlmostEqual(usage["cost"], 0.10, places=4)


if __name__ == "__main__":
    unittest.main()
