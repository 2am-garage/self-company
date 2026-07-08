#!/usr/bin/env python3
"""
notify-status — "catch-up" summary of unattended daily runs since you were last
notified. Powers Option B: the bare cron runs silently, and when the Chairman
next engages the company in a session, the agent runs this, and if there are new
runs, sends ONE PushNotification with the summary, then acks.

This script is deterministic and only READS logs + a marker file; it cannot push
(PushNotification is an agent-only tool). The agent does the push.

Usage:
  notify-status.py [--company DIR]        # print JSON {new_runs, since, summary, details}
  notify-status.py --ack [--company DIR]  # mark "notified up to now" (call after pushing)
  notify-status.py --emit-hook [--company DIR]  # SessionStart hook mode (see below)

--emit-hook is the SessionStart wiring: it runs when the Chairman opens a session.
The REPORT and the PUSH are decoupled:
  * REPORT — if the company has ever run, it ALWAYS emits the recent scheduled-work
    ledger as SessionStart additionalContext, so the Chairman sees the report on
    every entry (this is what he kept not seeing when it was gated).
  * PUSH — only when there are NEW runs since the marker AND they are SUBSTANTIVE
    (entropy/memory moved, decay, or pending TODOs) does it also ask the agent to
    send ONE PushNotification (push only — never Discord) and advance the marker.
The marker governs the push alone; a self-ack can never swallow the report.
If the company has never run, it stays silent.

Pure stdlib.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

MARKER = "ops/.last_notified"          # stores ISO timestamp of last notification
SHOWN_MARKER = "ops/.last_shown"       # P1: last time Elon surfaced a delta in-session
FAIL_MARKER = "ops/auth-fail.marker"   # B3 (Item 4): consecutive agent/auth fail streak
# B3: consecutive-fail count at/above which --emit-hook surfaces a HIGH-priority
# escalation, distinct from the routine ledger. NEW constant, default 2; env
# override for tests. daily-run.sh writes the streak into FAIL_MARKER; we only READ.
FAIL_STREAK_ESCALATE = int(os.environ.get("SELF_COMPANY_FAIL_STREAK_ESCALATE", "2"))
# C2: a run whose agent-<date>.log was written within this many seconds is IN-FLIGHT
# (the agent is still streaming) — kept in lockstep with report.py. Env-overridable.
INFLIGHT_WINDOW = int(os.environ.get("SELF_COMPANY_INFLIGHT_WINDOW", "300"))
RUN_RE = re.compile(r"^## Daily run (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(.*)$")


def _agent_log_fresh(company, ts, now=None):
    """C2: True if ops/logs/agent-<run-date>.log was modified within INFLIGHT_WINDOW.
    Mirrors report.py::_agent_log_fresh (the copy keeps notify standalone-stdlib)."""
    p = Path(company) / "ops" / "logs" / f"agent-{ts.strftime('%Y-%m-%d')}.log"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return False
    now = time.time() if now is None else now
    return (now - mtime) <= INFLIGHT_WINDOW


def _mark_inflight(company, blocks):
    """C2 (REPORT/TOM-5): the MOST-RECENT block only — a prompt-built-but-no-outcome
    silent death that is still in-flight (fresh agent log) is `running`, not a false
    `failed`. Applied to the GLOBAL latest block before any `since` filter."""
    if not blocks:
        return
    last = blocks[-1]
    if last.get("_silent_death") and _agent_log_fresh(company, last["ts"]):
        last["agent"] = "running"


def _parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def read_marker(company):
    p = Path(company) / MARKER
    if not p.exists():
        return None
    return _parse_ts(p.read_text(encoding="utf-8").strip())


def read_fail_marker(company):
    """B3: read the deterministic fail-streak marker written by daily-run.sh's auth
    pre-flight / agent-fail path. Returns (count:int, reason:str|None). Read-only —
    notify-status never writes or resets this marker (a successful agent run does)."""
    p = Path(company) / FAIL_MARKER
    if not p.exists():
        return 0, None
    count, reason = 0, None
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("count="):
                try:
                    count = int(line[len("count="):].strip())
                except ValueError:
                    count = 0
            elif line.startswith("reason="):
                reason = line[len("reason="):].strip()
    except OSError:
        return 0, None
    return count, reason


def escalation_line(company):
    """B3: a HIGH-priority escalation string when the consecutive-fail streak crosses
    FAIL_STREAK_ESCALATE, else "". Wording tracks the marker's reason so the Chairman
    gets an actionable pointer (auth => /login; agent => the agent log)."""
    count, reason = read_fail_marker(company)
    if count < FAIL_STREAK_ESCALATE:
        return ""
    if reason == "auth":
        return (f"⚠ {count} consecutive agent auth-fails — the maintenance "
                f"agent is NOT logged in; run /login to restore unattended runs.")
    return (f"⚠ {count} consecutive agent failures — unattended maintenance "
            f"has not completed; check ops/logs/agent-*.log (and /login).")


def _classify_agent_line(ln):
    """B3: classify one daily-log agent line into report.py's outcome classes.

    MUST stay in lockstep with report.py::collect (a parity test in
    tests/test_notify.py locks the two against drift; the copy exists so this
    script stays standalone-stdlib even if report.py is absent). Returns one of
    "prompt" (breadcrumb, not an outcome), "auth-fail", "timeout", "ok",
    "skipped" (benign: cap reached / no CLI), "failed", or None when the line
    is not an agent WRITER line at all — a CAPTURE entry for a memory slug
    starting with "agent" ("- agent-model-… (L0) — pending_verify") is data,
    never an outcome.
    """
    if not ln.startswith(("- agent:", "- agent (", "- agent prompt")):
        return None
    if ln.startswith("- agent prompt"):
        return "prompt"
    if "AUTH_FAIL" in ln or "auth pre-flight" in ln:
        return "auth-fail"
    if "TIMEOUT" in ln:
        return "timeout"
    if " ok" in ln:
        return "ok"
    if "skip" in ln:
        return "skipped"
    return "failed"


def collect_runs(company, since):
    """Return real (non-dry-run) daily-run blocks newer than `since`, parsed.

    C2: ALL blocks are parsed first so the GLOBAL latest block can be
    reclassified in-flight (running) before the `since` filter is applied — a
    still-streaming latest run must never be summarized as a dead agent, even when
    `since` would otherwise clip it in."""
    logs = sorted((Path(company) / "ops" / "logs").glob("daily-*.md"))
    runs = []
    for f in logs:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        i = 0
        while i < len(lines):
            m = RUN_RE.match(lines[i])
            if not m:
                i += 1
                continue
            ts, tail = _parse_ts(m.group(1)), m.group(2)
            i += 1
            if ts is None or "dry-run" in tail:
                continue
            block = {"ts": ts, "drop": 0, "memories": None, "entropy": None, "agent": None}
            # Extend the block until the NEXT "## Daily run" — not any "## " — so the
            # agent's own "## Consolidation pass" sub-heading and the trailing
            # "- agent ... ok" line stay inside this run's block.
            while i < len(lines) and not RUN_RE.match(lines[i]):
                ln = lines[i]
                dm = re.search(r"drop (\d+).*upgrade-candidates", ln)
                if dm:
                    block["drop"] = int(dm.group(1))
                em = re.search(r"entropy ([0-9.]+).*over (\d+) memories", ln)
                if em:
                    block["entropy"] = float(em.group(1))
                    block["memories"] = int(em.group(2))
                # B3 (Item 3): honest outcome classes, aligned with report.py —
                # the old `" ok" in ln` substring test rendered failed/timeout/
                # AUTH_FAIL runs as benign "skipped" in the catch-up summary.
                cls = _classify_agent_line(ln)
                if cls == "prompt":
                    block["_agent_attempted"] = True
                elif cls is not None:
                    block["agent"] = cls
                i += 1
            # prompt built but NO outcome line: the run died before recording
            # one (silent death) — honest classification: failed (C2 may later
            # reclassify the GLOBAL latest such block to `running` if in-flight).
            if block.pop("_agent_attempted", False) and block["agent"] is None:
                block["agent"] = "failed"
                block["_silent_death"] = True
            runs.append(block)                      # collect ALL, filter after in-flight mark
    runs.sort(key=lambda b: b["ts"])
    _mark_inflight(company, runs)                   # C2: latest silent-death + fresh log -> running
    if since is not None:
        runs = [b for b in runs if b["ts"] > since]
    return runs


def summarize(runs):
    if not runs:
        return "self-company: no new maintenance runs."
    n = len(runs)
    last = runs[-1]
    dropped = sum(b["drop"] for b in runs)
    mem = last["memories"] if last["memories"] is not None else "?"
    ent = last["entropy"] if last["entropy"] is not None else "?"
    agents = sum(1 for b in runs if b["agent"] == "ok")
    fails = sum(1 for b in runs if b["agent"] in ("failed", "timeout", "auth-fail"))
    span = f"since {runs[0]['ts']:%b %d %H:%M}"
    s = (f"self-company: {n} daily run{'s' if n > 1 else ''} {span} — "
         f"memory {mem}, entropy {ent}, {dropped} decayed, agent ok {agents}/{n}")
    if fails:
        # B3: failures are never summarized away as benign skips.
        s += f", {fails} agent-fail"
    return s


def write_marker(company):
    p = Path(company) / MARKER
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().replace(microsecond=0).isoformat()
    p.write_text(now + "\n", encoding="utf-8")
    return now


def recent_ledger_md(company, n=8):
    """Render the last N rows of the scheduled-work ledger (report.py), or "" if
    unavailable. Imported lazily by path so notify-status stays standalone."""
    try:
        import importlib.util
        rp_path = Path(__file__).resolve().parent / "report.py"
        spec = importlib.util.spec_from_file_location("report", rp_path)
        rp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rp)
        table = rp.build(rp.collect(company))
        return rp.render_md(table[-n:]) if table else ""
    except Exception:
        return ""


def substantive(company, runs):
    """Did anything worth a push actually change across the new runs?"""
    if any(b["drop"] for b in runs):
        return True
    ents = [b["entropy"] for b in runs if b["entropy"] is not None]
    if ents and ents[0] != ents[-1]:
        return True
    mems = [b["memories"] for b in runs if b["memories"] is not None]
    if mems and mems[0] != mems[-1]:
        return True
    # pending TODOs for the latest run's date count as worth surfacing
    d = runs[-1]["ts"].strftime("%Y-%m-%d")
    todo = Path(company) / "ops" / "plans" / f"todo-{d}.md"
    if todo.exists() and re.search(r"^\s*\d+\.", todo.read_text(encoding="utf-8"), re.M):
        return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default=".company")
    ap.add_argument("--ack", action="store_true",
                    help="Record 'notified up to now' (call after pushing).")
    ap.add_argument("--emit-hook", action="store_true",
                    help="SessionStart mode: emit a push instruction if substantive, then self-ack.")
    ap.add_argument("--delta", action="store_true",
                    help="P1: one-line delta of new substantive runs since last shown in-session, "
                         "then advance the .last_shown marker. Elon runs this each engagement so "
                         "the report surfaces mid-session (SessionStart only fires on a fresh session).")
    args = ap.parse_args(argv)

    company = args.company
    if not Path(company).exists():
        if not (args.emit_hook or args.delta):
            print(json.dumps({"new_runs": 0, "since": None, "summary": "", "note": "no .company"}))
        return 0

    if args.ack:
        print(json.dumps({"acked_at": write_marker(company)}))
        return 0

    if args.delta:
        now_iso = datetime.now().replace(microsecond=0).isoformat()
        p = Path(company) / SHOWN_MARKER
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)      # bootstrap silently
            p.write_text(now_iso + "\n", encoding="utf-8")
            return 0
        runs = collect_runs(company, _parse_ts(p.read_text(encoding="utf-8").strip()))
        if runs and substantive(company, runs):
            print(summarize(runs))                            # one-line delta to surface
        p.write_text(now_iso + "\n", encoding="utf-8")        # show-once: advance
        return 0

    if args.emit_hook:
        # The REPORT and the PUSH are decoupled (the Chairman kept "not seeing the
        # report"): always surface the ledger on session entry; only the push is
        # gated on substantive new runs + the marker. A self-ack must never be able
        # to swallow the report — the marker governs the push alone.
        all_runs = collect_runs(company, None)
        if not all_runs:
            return 0                          # company has never run — nothing to show
        ledger = recent_ledger_md(company)
        ctx = ("[self-company] Scheduled-work report. Render this ledger inline to the "
               "Chairman in your reply — it is the report he wants to see on entry, "
               "every session, whether or not anything changed:\n\n" + ledger)

        # B3 (Item 4): a fail-streak escalation is HIGH-priority — prepend it so the
        # Chairman sees it FIRST, clearly distinct from the routine ledger. This ADDS
        # to the always-on report contract; it never replaces it.
        esc = escalation_line(company)
        if esc:
            ctx = ("[self-company] ‼ HIGH-PRIORITY ESCALATION — surface this to the "
                   "Chairman FIRST, before the ledger: " + esc + "\n\n" + ctx)

        new_runs = collect_runs(company, read_marker(company))
        if new_runs and substantive(company, new_runs):
            ctx += (f"\n\nAlso, {len(new_runs)} new run(s) since last seen — {summarize(new_runs)}. "
                    f"Send exactly ONE PushNotification with that summary (push only, never "
                    f"Discord). Already acknowledged; do not run notify-status --ack for it.")
            write_marker(company)             # ack ONLY the push, not the report
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": ctx}}))
        return 0

    since = read_marker(company)
    runs = collect_runs(company, since)
    fail_count, fail_reason = read_fail_marker(company)
    print(json.dumps({
        "new_runs": len(runs),
        "since": since.isoformat() if since else None,
        "summary": summarize(runs),
        "fail_streak": fail_count,
        "fail_reason": fail_reason,
        "escalation": escalation_line(company),
        "details": [{"ts": b["ts"].isoformat(), "drop": b["drop"],
                     "memories": b["memories"], "entropy": b["entropy"],
                     "agent": b["agent"]} for b in runs],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
