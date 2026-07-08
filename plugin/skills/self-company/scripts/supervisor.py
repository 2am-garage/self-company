#!/usr/bin/env python3
"""
supervisor — the skill's OWN live-orchestration harness (Plan C).

The Chairman wanted the Claude-Workflow experience — a live tree of sub-agents
working — but modular (not bound to Claude Code) and genuinely live (not a polled
file). So this is a small, skill-owned supervisor: it spawns employees as CHILD
processes and reads their stdout streams in real time via select(), so status is
event-driven and synced with the actual work — because the supervisor IS the
parent of the process tree. It is ephemeral: it exists only while work runs.

Every employee has this capability (discovered from org/employees/, not a
hardcoded subset). Built with OOP for readability:

    Member     — one member; knows how to build its run command (real or demo)
    Worker     — one running employee process; parses its live '@status' stream
    Supervisor — spawns workers, multiplexes their streams, drives the renderer
    LiveTree   — renders the live status; repaints on a TTY, streams a feed if not

`Member` is the supervisor's ORCHESTRATION view of a company member (how to spawn
and render it live); the authoritative DATA MODEL is `employee.Employee`
(identity, capabilities, per-employee memory). There is deliberately ONE class
named `Employee` — the data model — and the supervisor BRIDGES to it (Member.
_recall_memory loads it) rather than duplicating it: process-spawning is not a
data-model concern, so the two responsibilities stay separate but the data model
stays single-sourced.

Status protocol: a worker prints lines beginning with '@status <phase>' as it
works ('@status planning', '@status done'). Everything else is treated as a log
line. The same protocol works for a simulated demo worker and for a real
`claude -p` agent told to emit those markers — the supervisor is agnostic.

Honest ceiling: in a real terminal this is a live TUI tree; viewed remotely in
the Claude app it streams as text (the app renders text, not skill widgets). That
is the one thing no modular design can beat — native widgets belong to the host.

Usage:
  supervisor.py --demo [--company DIR]                 # simulate all employees live
  supervisor.py --dispatch '{"phoebe":"plan X",...}' [--company DIR]   # real agents
  supervisor.py --list [--company DIR]

Pure stdlib (subprocess, select). Unix.
"""

import argparse
import enum
import json
import os
import select
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


class Status(enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    WORKING = "working"
    DONE = "done"
    FAILED = "failed"


GLYPH = {Status.IDLE: " ", Status.STARTING: "…", Status.WORKING: "~",
         Status.DONE: "=", Status.FAILED: "x"}

# Preferred display order + fallback role labels (real roles come from personas).
ROLE_HINTS = {
    "elon": "CEO · direction", "phoebe": "PM · gateway", "tony": "Improvement · entropy",
    "gibby": "Verify · sources", "bob": "Engineer · builds", "july": "People · personas",
    "tom": "Infra · scheduling",
}
ORDER = ["elon", "phoebe", "tony", "gibby", "bob", "july", "tom"]


class Member:
    """The supervisor's ORCHESTRATION view of one company member: how to spawn it
    (demo/real command) and render it live. All members share this capability
    (Chairman: everyone). The authoritative identity/capability/memory model is
    `employee.Employee` — this class BRIDGES to it (see _recall_memory) rather
    than re-implementing it, so employee.py stays the single data-driven class."""

    def __init__(self, emp_id, name=None, role=None, company_dir="."):
        self.id = emp_id
        self.name = name or emp_id.capitalize()
        self.role = role or ROLE_HINTS.get(emp_id, "member")
        self.company_dir = company_dir

    @classmethod
    def roster(cls, company_dir):
        """Discover ALL employees from org/employees/ (not a hardcoded subset)."""
        base = Path(company_dir) / "org" / "employees"
        found = [d.name for d in sorted(base.iterdir())
                 if (d / "persona.md").exists()] if base.exists() else []
        ids = [e for e in ORDER if e in found] + [e for e in found if e not in ORDER]
        return [cls(i, company_dir=company_dir) for i in (ids or ORDER)]

    def demo_command(self, task, delay=0.3):
        """A simulated worker: emits the @status protocol so the live tree moves."""
        phases = ["planning", "working", "reviewing", "done"]
        script = "; ".join(f"echo '@status {p}'; sleep {delay}" for p in phases)
        return ["bash", "-c", script]

    def _recall_memory(self, task):
        """Phase 18 Item 4 + Phase 18c — dispatch-time MEMORY injection. BRIDGE to
        the employee.py data model: load THIS member's real `Employee` and ask it
        for the ready-to-prepend memory block for `task`. We import lazily and
        bridge (rather than merge the two classes) because process-spawning is not a
        data-model concern — employee.py stays the single data-driven class that
        owns identity/capabilities/memory.

        `dispatch_context()` returns TWO distinct sections, both internally gated +
        budget-capped + timeout-degrading:
          * "Relevant past experience" — this employee's OWN store (rag employee).
          * "Relevant company memory"  — the SHARED Chairman corpus, ONLY for a
            `shared_memory_read` employee (elon by default). Phase 18c wires this
            read INTO dispatch so autonomous/cron/trigger work carries the Chairman's
            standing direction, not just the interactive ask-time hook.
        A `flat`, no-venv, empty-index, timeout, or zero-hit case yields "" for the
        relevant half — dispatch is never blocked and never fails on recall. The
        try/except here only guards the import; dispatch_context never raises."""
        try:
            from employee import Employee as EmployeeModel
        except Exception:
            return ""
        try:
            return EmployeeModel.load(self.id, self.company_dir).dispatch_context(task)
        except Exception:
            return ""

    def worker_env(self):
        """Environment for a spawned real worker, or None to inherit unchanged.

        DOUBLE-INJECTION GUARD (Phase 18c). A spawned `claude -p` worker ALSO fires
        the plugin's UserPromptSubmit hook (hook_memory_inject.py) on its own prompt
        — confirmed: `-p` fires UserPromptSubmit before Claude processes it. For a
        `shared_memory_read` employee we already inject the SHARED company memory
        EXPLICITLY into the worker prompt at dispatch (see real_command), so we set
        SC_NO_MEMORY_INJECT=1 to make that worker's hook a clean no-op — otherwise
        the shared memory would be injected a SECOND time. The explicit dispatch
        injection is then the single source. For every OTHER employee we return None
        (inherit), so the hook keeps providing shared memory as before — no
        regression for non-shared-read workers."""
        try:
            from employee import Employee as EmployeeModel
            if EmployeeModel.load(self.id, self.company_dir).shared_memory_read:
                return {**os.environ, "SC_NO_MEMORY_INJECT": "1"}
        except Exception:
            pass
        return None

    def real_command(self, task, model="claude-sonnet-4-6"):
        """A real headless agent, primed with this employee's role and the task,
        told to emit @status markers so the supervisor can stream live phases.

        Before dispatch, inject this employee's relevant MEMORY (Phase 18 Item 4 +
        Phase 18c): for a `rag`-mode employee, the OWN-store "Relevant past
        experience" block; for a `shared_memory_read` employee (elon), ALSO the
        SHARED "Relevant company memory" block. A `flat`, non-shared-read employee
        (e.g. bob/gibby/tom) with no relevant memory injects NOTHING
        (dispatch_context returns ""). The two blocks share one budget and are
        deduped by employee.py."""
        prompt = (
            f"You are {self.name} ({self.role}) in the self-company, working "
            f"non-interactively. Task: {task}\n"
            f"Read your persona at .company/org/employees/{self.id}/persona.md and stay "
            f"in role. As you work, print progress lines beginning with '@status ' "
            f"followed by ONE short phase word (e.g. '@status planning', '@status "
            f"working', '@status reviewing'). Print '@status done' when finished. "
            f"Keep it tight."
        )
        memory = self._recall_memory(task)
        if memory:
            prompt = f"{prompt}\n\n{memory}"
        return ["claude", "-p", prompt, "--model", model]


class Worker:
    """Wraps one running employee process; parses its live @status stream."""

    def __init__(self, employee, task, command, env=None):
        self.emp = employee
        self.task = task
        self.command = command
        self.env = env                # None -> inherit; set to guard the memory hook
        self.status = Status.IDLE
        self.phase = ""
        self.last = ""
        self.lines = []
        self.proc = None
        self._t0 = None
        self._t1 = None

    def start(self):
        self.proc = subprocess.Popen(
            self.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=self.env)
        self.status = Status.STARTING
        self._t0 = time.monotonic()

    @property
    def fd(self):
        return self.proc.stdout.fileno() if self.proc and self.proc.stdout else None

    def consume_line(self, line):
        line = line.rstrip("\n")
        self.lines.append(line)
        if line.startswith("@status "):
            self.phase = line[len("@status "):].strip()
            self.status = Status.DONE if self.phase == "done" else Status.WORKING
        elif line:
            self.last = line

    def on_eof(self):
        rc = self.proc.wait() if self.proc else 0
        if self.proc and self.proc.stdout:
            self.proc.stdout.close()           # release the pipe fd (many workers)
        if self.status not in (Status.DONE, Status.FAILED):
            self.status = Status.DONE if rc == 0 else Status.FAILED
        self._t1 = time.monotonic()

    def elapsed(self):
        if self._t0 is None:
            return 0.0
        return (self._t1 or time.monotonic()) - self._t0


class LiveTree:
    """Renders the live status. Repaints in place on a TTY; streams a feed if not."""

    def __init__(self, roster, stream=None):
        self.roster = roster
        self.stream = stream or sys.stdout
        self.tty = self.stream.isatty()
        self._painted = 0

    def _rows(self, workers):
        W = 52
        now = datetime.now().strftime("%H:%M:%S")
        head = f" self-company · live supervisor · {now} "
        rows = ["╭" + head.center(W + 2, "─") + "╮"]
        for emp in self.roster:
            w = workers.get(emp.id)
            if w is None:
                body = f"[ ] {emp.name:<7} idle"
            else:
                ph = w.phase or w.status.value
                body = f"[{GLYPH[w.status]}] {emp.name:<7} {ph:<12} {w.elapsed():4.1f}s"
            rows.append("│ " + body[:W].ljust(W) + " │")
        rows.append("╰" + "─" * (W + 2) + "╯")
        return rows

    def repaint(self, workers):
        rows = self._rows(workers)
        if self.tty:
            if self._painted:
                self.stream.write(f"\x1b[{self._painted}A")   # cursor up
            self.stream.write("\n".join(rows) + "\n")
            self._painted = len(rows)
        self.stream.flush()

    def feed(self, worker):
        """Non-TTY: emit one live event line per status change (reads well in app)."""
        if not self.tty:
            ph = worker.phase or worker.status.value
            self.stream.write(f"{datetime.now():%H:%M:%S}  {worker.emp.name:<7} → {ph}\n")
            self.stream.flush()

    def final(self, workers):
        self.stream.write("\n".join(self._rows(workers)) + "\n")
        self.stream.flush()


class Supervisor:
    """Ephemeral orchestrator: spawn workers, multiplex their live streams, render."""

    def __init__(self, company_dir, renderer=None, event_log=None):
        self.company_dir = company_dir
        self.roster = Member.roster(company_dir)
        self.by_id = {e.id: e for e in self.roster}
        self.renderer = renderer if renderer is not None else LiveTree(self.roster)
        self.event_log = event_log

    def _emit(self, worker, kind):
        if self.event_log is not None:
            self.event_log.append({
                "ts": datetime.now().replace(microsecond=0).isoformat(),
                "emp": worker.emp.id, "kind": kind,
                "status": worker.status.value, "phase": worker.phase})

    def dispatch(self, assignments, demo=False, demo_delay=0.3):
        """assignments: {emp_id: task}. Spawn matching workers, run to completion live."""
        workers = {}
        for emp_id, task in assignments.items():
            emp = self.by_id.get(emp_id)
            if emp is None:
                continue
            cmd = emp.demo_command(task, demo_delay) if demo else emp.real_command(task)
            # Real workers get the double-injection guard env for shared_memory_read
            # employees (elon); demo workers just echo, so they inherit unchanged.
            env = None if demo else emp.worker_env()
            w = Worker(emp, task, cmd, env=env)
            w.start()
            workers[emp_id] = w
            self._emit(w, "start")
        self.renderer.repaint(workers)

        active = {w.fd: w for w in workers.values()}
        while active:
            ready, _, _ = select.select(list(active), [], [], 0.2)
            for fd in ready:
                w = active[fd]
                line = w.proc.stdout.readline()
                if line == "":                 # EOF -> process finished
                    w.on_eof()
                    del active[fd]
                    self._emit(w, "end")
                else:
                    w.consume_line(line)
                    if line.startswith("@status "):
                        self.renderer.feed(w)
                        self._emit(w, "status")
                self.renderer.repaint(workers)
        self.renderer.final(workers)
        return workers


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default=".company")
    ap.add_argument("--demo", action="store_true", help="simulate all employees live")
    ap.add_argument("--demo-delay", type=float, default=0.3)
    ap.add_argument("--dispatch", help="JSON {emp_id: task} — spawn real agents")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    sup = Supervisor(args.company)
    if args.list:
        print(json.dumps({"roster": [e.id for e in sup.roster]}))
        return 0
    if args.demo:
        sup.dispatch({e.id: "demo task" for e in sup.roster},
                     demo=True, demo_delay=args.demo_delay)
        return 0
    if args.dispatch:
        sup.dispatch(json.loads(args.dispatch), demo=False)
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
