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
If there are new background runs AND they are SUBSTANTIVE (entropy/memory moved,
something decayed, or there are pending TODOs), it prints a SessionStart
additionalContext payload instructing the agent to send exactly ONE PushNotification
(push only — never Discord, per the Chairman's standing preference) and then
self-acks so the same window is never re-pushed. If nothing substantive changed it
silently acks and prints nothing — zero noise on quiet days.

Pure stdlib.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

MARKER = "ops/.last_notified"          # stores ISO timestamp of last notification
RUN_RE = re.compile(r"^## Daily run (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(.*)$")


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


def collect_runs(company, since):
    """Return real (non-dry-run) daily-run blocks newer than `since`, parsed."""
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
                if ln.startswith("- agent"):
                    block["agent"] = "ok" if " ok" in ln else "skipped"
                i += 1
            if since is None or ts > since:
                runs.append(block)
    runs.sort(key=lambda b: b["ts"])
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
    span = f"since {runs[0]['ts']:%b %d %H:%M}"
    return (f"self-company: {n} daily run{'s' if n > 1 else ''} {span} — "
            f"memory {mem}, entropy {ent}, {dropped} decayed, agent ok {agents}/{n}")


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
    args = ap.parse_args(argv)

    company = args.company
    if not Path(company).exists():
        if not args.emit_hook:
            print(json.dumps({"new_runs": 0, "since": None, "summary": "", "note": "no .company"}))
        return 0

    if args.ack:
        print(json.dumps({"acked_at": write_marker(company)}))
        return 0

    if args.emit_hook:
        runs = collect_runs(company, read_marker(company))
        if not runs:
            return 0                          # nothing new — silent
        if not substantive(company, runs):
            write_marker(company)             # quiet ack, no notification
            return 0
        ctx = (
            f"[self-company] {len(runs)} background maintenance run(s) completed while you were "
            f"away. {summarize(runs)}. Per the Chairman's standing push-notification-only "
            f"preference, send exactly ONE PushNotification with this summary now — push only, "
            f"do NOT use Discord. This catch-up is already acknowledged; do not run "
            f"notify-status --ack for it."
        )
        ledger = recent_ledger_md(company)    # show the report inline (Chairman's request)
        if ledger:
            ctx += ("\nThen display this scheduled-work ledger to the Chairman inline in your "
                    "reply, rendered as-is (it is the report he asked to see here):\n\n" + ledger)
        write_marker(company)                 # ack now: deterministic, never double-pushes
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": ctx}}))
        return 0

    since = read_marker(company)
    runs = collect_runs(company, since)
    print(json.dumps({
        "new_runs": len(runs),
        "since": since.isoformat() if since else None,
        "summary": summarize(runs),
        "details": [{"ts": b["ts"].isoformat(), "drop": b["drop"],
                     "memories": b["memories"], "entropy": b["entropy"],
                     "agent": b["agent"]} for b in runs],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
