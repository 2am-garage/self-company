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
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Phase 27 Item 1: the ONE shared reader — no more private run-header regex /
# mtime in-flight heuristic here; daily_log.py owns both, once, for every consumer.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily_log  # noqa: E402

MARKER = "ops/.last_notified"          # stores ISO timestamp of last notification
SHOWN_MARKER = "ops/.last_shown"       # P1: last time Elon surfaced a delta in-session
FAIL_MARKER = "ops/auth-fail.marker"   # B3 (Item 4): consecutive agent/auth fail streak
# Item 2: "dark for days" must not look like "quiet". Beyond this multiple of
# the INSTALLED cadence (parsed from the user crontab), --emit-hook escalates.
# No cron entry installed => no alarm, ever (an intentionally-uninstalled or
# single-shot company is healthy-by-definition here).
STALE_RUN_FACTOR = float(os.environ.get("SELF_COMPANY_STALE_RUN_FACTOR", "2"))
# Item 3: a lock-skip STREAK (consecutive flock-contended cron ticks) at/above
# this threshold escalates HIGH — one contended tick is normal life (manual-run
# overlap); a streak means the .daily.lock is likely wedged.
LOCK_SKIP_STREAK_ESCALATE = int(os.environ.get("SELF_COMPANY_LOCK_SKIP_STREAK_ESCALATE", "3"))
# B3: consecutive-fail count at/above which --emit-hook surfaces a HIGH-priority
# escalation, distinct from the routine ledger. NEW constant, default 2; env
# override for tests. daily-run.sh writes the streak into FAIL_MARKER; we only READ.
FAIL_STREAK_ESCALATE = int(os.environ.get("SELF_COMPANY_FAIL_STREAK_ESCALATE", "2"))


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
    """Classify one daily-log agent line into report.py's outcome classes.

    Kept as a small standalone pure function (no longer used by collect_runs,
    which now reads through daily_log.py) purely so a bare agent-line shape
    can still be sanity-checked in isolation. Returns one of "prompt"
    (breadcrumb, not an outcome), "auth-fail", "timeout", "ok", "skipped"
    (benign: cap reached / no CLI), "failed", or None when the line is not an
    agent WRITER line at all — a CAPTURE entry for a memory slug starting
    with "agent" ("- agent-model-… (L0) — pending_verify") is data, never an
    outcome.
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


def collect_runs(company, since, window_days=30):
    """Real (non-dry-run) daily-runs newer than `since`, read through
    daily_log.py (Phase 27 Item 1's shared reader — no private run-header regex, no
    mtime-probing in-flight heuristic here anymore)."""
    runs = daily_log.read_runs(company, window_days=window_days)
    if since is not None:
        runs = [r for r in runs if r["ts"] > since]
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
    # Phase 25 Item 3: memory-rot warnings are never summarized away either.
    warned = sum(b.get("warnings", 0) for b in runs)
    if warned:
        s += f", {warned} warning(s)"
    aborts = sum(1 for b in runs if b.get("core_aborted"))
    if aborts:
        s += f", {aborts} CORE ABORTED"
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
    # Phase 25 Item 1/3: an aborted core or a surfaced memory-rot warning is
    # ALWAYS worth a push — these must never wait behind "did entropy move".
    if any(b.get("core_aborted") for b in runs):
        return True
    if any(b.get("warnings") for b in runs):
        return True
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


def core_abort_escalation_line(runs):
    """Phase 25 Item 1: a HIGH-priority escalation string when the MOST
    RECENT run's deterministic core was ABORTED (safety floor failed), else
    "". Mirrors escalation_line()'s shape but keys on the log-parsed
    core_aborted flag (the SAME channel Item 3's warnings ride) rather than a
    separate fail-streak marker — a single abort is worth surfacing
    immediately, not only after a streak."""
    if not runs:
        return ""
    last = runs[-1]
    if not last.get("core_aborted"):
        return ""
    reason = last.get("abort_reason") or "safety floor failed"
    return (f"‼ CORE ABORTED — the deterministic memory core (reinforce/decay/"
            f"verify) did NOT run: {reason}. Investigate free space; "
            f"ops/core-abort.marker is present until a healthy run clears it.")


def lock_skip_escalation_line(runs):
    """Item 3: a HIGH-priority escalation when the MOST RECENT run's
    lock-skip streak (daily-run.sh's own marker, carried through the JSONL
    end event) is at/above LOCK_SKIP_STREAK_ESCALATE. One contended tick
    stays quiet (manual-run overlap is normal life); a streak means the
    .daily.lock is likely wedged — a different fix than a dead scheduler."""
    if not runs:
        return ""
    streak = runs[-1].get("lock_skip_streak")
    if not isinstance(streak, int) or streak < LOCK_SKIP_STREAK_ESCALATE:
        return ""
    return (f"‼ {streak} consecutive cron ticks lock-skipped — the .daily.lock "
            f"is likely wedged (see LOCK STALE in the log; P25 stale-holder "
            f"guidance).")


def _read_crontab():
    """Item 2: read the user crontab through the SAME env seam schedule.sh
    uses (SELF_COMPANY_CRONTAB_FILE for tests; SELF_COMPANY_CRONTAB_CMD else
    the real `crontab` binary) — never touches the real crontab in a test."""
    cf = os.environ.get("SELF_COMPANY_CRONTAB_FILE")
    if cf:
        try:
            return Path(cf).read_text(encoding="utf-8")
        except OSError:
            return ""
    cmd = os.environ.get("SELF_COMPANY_CRONTAB_CMD", "crontab")
    try:
        r = subprocess.run([cmd, "-l"], capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _find_daily_cron_line(project_dir):
    """The `# self-company-daily ... path=<project_dir>` line for THIS
    project, or None if no such entry is installed (schedule.sh's mark
    scheme, schedule.sh:19 — path= is always the line's last field)."""
    text = _read_crontab()
    if not text:
        return None
    candidates = {os.path.normpath(os.path.abspath(project_dir))}
    try:
        candidates.add(os.path.realpath(project_dir))
    except OSError:
        pass
    for line in text.splitlines():
        if "# self-company-daily" not in line:
            continue
        m = re.search(r"path=(\S+)\s*$", line.strip())
        if m and m.group(1) in candidates:
            return line
    return None


def _cadence_hours(hour_field):
    """Parse the cron hour-field: `*/N` => every N hours; a fixed-hour list
    (e.g. "0,6,12,18") or anything else unparseable => a conservative 24h —
    never a false "dead" alarm from a cadence we can't confidently compute."""
    hour_field = (hour_field or "").strip()
    m = re.match(r"^\*/(\d+)$", hour_field)
    if m:
        n = int(m.group(1))
        return float(n) if n > 0 else 24.0
    return 24.0


def staleness_escalation_line(company, now=None):
    """Item 2: "dark for days" must not look like "quiet". Compares now minus
    the latest REAL run's ts against STALE_RUN_FACTOR x the installed cron
    cadence for this project. No cron entry installed => "" always (an
    intentionally-uninstalled or single-shot company is healthy-by-definition
    here — Elon's decision, no @reboot catch-up either).

    Reads daily_log directly with window_days=None (NOT the hook's windowed
    `all_runs`) — a scheduler dead for longer than the parse window must
    still report its true gap, not misclassify as "never ran"."""
    now = now or datetime.now()
    project_dir = str(Path(company).resolve().parent)
    line = _find_daily_cron_line(project_dir)
    if line is None:
        return ""
    fields = line.strip().split()
    hour_field = fields[1] if len(fields) > 1 else "*"
    cadence_h = _cadence_hours(hour_field)
    threshold_h = cadence_h * STALE_RUN_FACTOR

    all_runs = daily_log.read_runs(company, window_days=None, now=now)
    real_runs = [r for r in all_runs if not r.get("dry_run")]
    if not real_runs:
        # (d) cron installed, ZERO runs ever: we have no install timestamp to
        # measure "2x cadence since install" against, so fall back to the
        # cron-line's presence + total JSONL silence => escalate now, named
        # distinctly from an ordinary staleness gap.
        return (f"‼ STALE: cron is installed (every {cadence_h:g}h) but no daily "
                f"run has EVER completed for this company — installed but never "
                f"ran. Check crontab -l, ops/logs/cron.log, and the .daily.lock.")

    last_ts = max(r["ts"] for r in real_runs)
    gap_h = (now - last_ts).total_seconds() / 3600.0
    if gap_h <= threshold_h:
        return ""
    msg = (f"‼ STALE: last daily run was {gap_h:.0f}h ago (cron installed: every "
           f"{cadence_h:g}h — expected ≤{threshold_h:.0f}h). Scheduler may be "
           f"dead: check crontab -l, ops/logs/cron.log, and the .daily.lock.")
    streak = real_runs[-1].get("lock_skip_streak")
    if isinstance(streak, int) and streak > 0:
        msg += (f" Lock-skip streak: {streak} consecutive cron tick(s) skipped "
                f"— a wedged lock and a wiped crontab need different fixes.")
    return msg


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
            # Item 2 (d): even with NO run ever, a company whose crontab HAS an
            # entry installed is a distinguishable failure ("installed but
            # never ran") from a genuinely never-touched company (no cron —
            # silent, as before). Compute this BEFORE going fully silent.
            stale_esc = staleness_escalation_line(company)
            if stale_esc:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "[self-company] " + stale_esc}}))
            return 0                          # company has never run — nothing more to show
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

        # Phase 25 Item 1: a CORE-ABORT escalation is likewise HIGH-priority and
        # independent of the fail-streak marker (a single abort matters
        # immediately, not only after a streak) — prepend it too.
        abort_esc = core_abort_escalation_line(all_runs)
        if abort_esc:
            ctx = ("[self-company] " + abort_esc + "\n\n" + ctx)

        # Item 3: a lock-skip streak (wedged .daily.lock) — HIGH-priority,
        # independent of the other escalations (a streak of "healthy-looking"
        # skipped ticks needs its own signal or it never surfaces).
        lock_esc = lock_skip_escalation_line(all_runs)
        if lock_esc:
            ctx = ("[self-company] " + lock_esc + "\n\n" + ctx)

        # Item 2: total darkness (a wiped crontab, dead cron PATH, laptop-off
        # week) is the one failure mode Phase 25's per-run signals can't catch
        # — there is no run to carry them. Silent when no cron entry is
        # installed for this project (Elon's decision: never a false fire).
        stale_esc = staleness_escalation_line(company)
        if stale_esc:
            ctx = ("[self-company] " + stale_esc + "\n\n" + ctx)

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
        "core_abort_escalation": core_abort_escalation_line(runs),
        "lock_skip_escalation": lock_skip_escalation_line(runs),
        "staleness_escalation": staleness_escalation_line(company),
        "details": [{"ts": b["ts"].isoformat(), "drop": b["drop"],
                     "memories": b["memories"], "entropy": b["entropy"],
                     "agent": b["agent"], "warnings": b.get("warnings", 0),
                     "core_aborted": b.get("core_aborted", False),
                     "lock": b.get("lock")} for b in runs],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
