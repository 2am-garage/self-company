#!/usr/bin/env python3
"""
daily_log.py — Phase 27 Item 1: the ONE shared reader for daily-run.sh's
per-run history.

Source of truth: `ops/logs/daily-<date>.jsonl`, append-only, two events per
run (`start`, `end`) written by daily-run.sh. The `.md` log in the same
directory REMAINS and remains human-first — this module never writes it, and
every signal it carries keeps rendering there exactly as before. The JSONL
ADDS a machine-readable channel; it never replaces the render.

Every consumer (report.py, notify-status.py, org-status.py, fleet.py) reads
history through `read_runs()` — the single place that understands both the
JSONL schema below and the legacy prose format. A search for the run-header
pattern's compiled name over scripts/ should find exactly one declaration:
the constant below, marked legacy.

--------------------------------------------------------------------------------
JSONL EVENT SCHEMA (schema: 1)
--------------------------------------------------------------------------------
start:
  {"event":"start","ts":ISO,"mode":"cron"|"manual","dry_run":bool,"pid":int,
   "run_id":str,"schema":1}

end (paired to a start via run_id — see _pair_events; start_ts is a human
     cross-reference, not the pairing key):
  {"event":"end","ts":ISO,"start_ts":ISO,"run_id":str,"schema":1,
   "lock":"acquired"|"skipped"|"stale-holder"|"unserialized"|"wait-timeout"|null,
   "lock_skip_streak": int|null,
   "core_aborted": bool, "abort_reason": str|null,
   "steps": {"<name>": {"outcome": str, "warnings": int, "warning_samples": [str],
                          # decay only: "drop"/"demote"/"archive"/"upgrade"
                          # verify only: "verified"/"unverifiable"
                          # entropy only: "value"/"dims"/"memories"
                         }, ...},
   "agent": {"outcome": str|null, "rc": int|null, "runs_today": int|null,
             "cap": int|null, "fail_streak": int|null} | null,
   "dry_run": bool}

Two events per run, always — no streaming/partial step events (Phase-28
territory if ever), paired by `run_id`. A `start` with no matching `end`: within
SELF_COMPANY_INFLIGHT_WINDOW (default 300s) of now => `in-flight`; older =>
`crashed`. This classification is computed from timestamps ONLY — zero mtime
probing — which is what kills the Phase-19-C2 race class by construction
(the old heuristic classified a possibly-half-written prose block by probing
agent-<date>.log's mtime; JSONL lines are appended whole, one O_APPEND write
per event, comfortably under POSIX's atomic-write size).

--------------------------------------------------------------------------------
LEGACY FALLBACK — cutover 2026-07-10
--------------------------------------------------------------------------------
A `daily-<date>.md` with NO sibling `.jsonl` (any day before this phase
landed) is parsed by ONE deprecated prose walker (the last surviving copy of
the pattern that used to be duplicated four ways across report.py,
notify-status.py, org-status.py and fleet.py). It reproduces today's exact
per-run field extraction, including the old agent-log-mtime in-flight
heuristic — kept ONLY for pre-cutover history so historical runs still
render sensibly. Once Item 5's 30-day parse window rolls past 2026-08-09
this whole branch is provably dead and should be deleted in the next hygiene
pass.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = 1
DEFAULT_WINDOW_DAYS = 30
INFLIGHT_WINDOW_SECS = int(os.environ.get("SELF_COMPANY_INFLIGHT_WINDOW", "300"))

# The ONE surviving RUN_RE — legacy prose fallback only. Every consumer must
# read through read_runs()/read_run_blocks() instead of declaring its own.
LEGACY_RUN_RE = re.compile(r"^## Daily run (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(.*)$")


def _parse_ts(s):
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _logs_dir(company):
    return Path(company) / "ops" / "logs"


def _safe_read(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _empty_run(ts):
    return {
        "ts": ts, "end_ts": None, "source": None, "run_state": "complete",
        "dry_run": False, "mode": None,
        "lock": None, "lock_skip_streak": None,
        "core_aborted": False, "abort_reason": None,
        "steps": {}, "entropy": None, "dims": None, "memories": None,
        "drop": 0, "demote": 0, "archive": 0, "upgrade": 0,
        "verified": 0, "unverifiable": 0,
        "merged": 0, "promoted": 0, "warnings": 0,
        "agent": None, "agent_rc": None, "agent_runs_today": None,
        "agent_cap": None, "agent_fail_streak": None,
        "lock_stale": False, "md_block": "",
    }


# ---------------------------------------------------------------------------
# Atomic append — the ONE writer path (used by daily-run.sh, directly or via
# the `append` CLI below). A single os.write() to an O_APPEND fd is one
# write(2) syscall; POSIX guarantees no interleaving for writes under
# PIPE_BUF (4096 on Linux) even across concurrent processes — the invariant
# Item 1(f) requires.
# ---------------------------------------------------------------------------

def append_event(path, event):
    line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

def _read_jsonl_events(path):
    events = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue   # a corrupt/partial line never crashes the reader
                if isinstance(obj, dict):
                    events.append(obj)
    except OSError:
        return []
    return events


def _pair_events(events):
    """Pair start/end events into runs.

    MUST-FIX 1: key on the UNIQUE `run_id` daily-run.sh writes into BOTH the
    start and end event — NOT the second-resolution `ts` string. Four cron
    ticks that lose the flock in the same wall-clock second carry four
    distinct run_ids, so they stay four rows instead of collapsing
    last-write-wins. Pre-run_id legacy events (hand-written fixtures, or the
    brief transition window) fall back to the old ts-based key — for those the
    same-second collision is an irreducible limitation of data that never
    carried a unique id, but it can never affect a real emitted run again.
    """
    def _start_key(e):
        rid = e.get("run_id")
        return rid if isinstance(rid, str) and rid else e.get("ts")

    def _end_key(e):
        rid = e.get("run_id")
        if isinstance(rid, str) and rid:
            return rid
        return e.get("start_ts") or e.get("ts")

    starts, ends, order = {}, {}, []
    for e in events:
        if e.get("event") == "start":
            key = _start_key(e)
            if not isinstance(key, str):
                continue
            if key not in starts:
                order.append(key)
            starts[key] = e
        elif e.get("event") == "end":
            key = _end_key(e)
            if isinstance(key, str):
                ends[key] = e
    # unmatched ends (no start line at all — corrupted/truncated file) still
    # surface as a run, keyed by their own key, appended after paired ones.
    for key in ends:
        if key not in starts and key not in order:
            order.append(key)
    return [(starts.get(k), ends.get(k)) for k in order]


def _normalize_step_outcome(raw):
    """Map daily-run.sh's internal per-step state vocabulary (ran/gated/
    aborted/novenv/missing/timeout) onto the spec's ok/skipped:<why>/error/
    timeout shape, once, in the one place that matters."""
    if raw is None:
        return None
    m = {
        "ran": "ok", "gated": "skipped:gated", "aborted": "skipped:core-abort",
        "novenv": "skipped:no-venv", "missing": "skipped:missing-script",
        "timeout": "timeout", "errored": "error",
    }
    return m.get(raw, raw)


def _normalize_agent_outcome(raw):
    if not raw:
        return None
    if raw == "ok":
        return "ok"
    if raw.startswith("skipped"):
        return "skipped"
    if raw in ("auth-fail", "timeout", "failed"):
        return raw
    return "skipped"


def _apply_end(r, end):
    r["end_ts"] = _parse_ts(end.get("ts"))
    r["lock"] = end.get("lock")
    r["lock_stale"] = (end.get("lock") == "stale-holder")
    r["lock_skip_streak"] = end.get("lock_skip_streak")
    r["core_aborted"] = bool(end.get("core_aborted"))
    r["abort_reason"] = end.get("abort_reason")
    steps = end.get("steps") or {}
    r["steps"] = steps
    total_warn = 0
    for s in steps.values():
        if isinstance(s, dict):
            total_warn += int(s.get("warnings") or 0)
    r["warnings"] = total_warn
    decay = steps.get("decay") or {}
    r["drop"] = int(decay.get("drop") or 0)
    r["demote"] = int(decay.get("demote") or 0)
    r["archive"] = int(decay.get("archive") or 0)
    r["upgrade"] = int(decay.get("upgrade") or 0)
    verify = steps.get("verify") or {}
    r["verified"] = int(verify.get("verified") or 0)
    r["unverifiable"] = int(verify.get("unverifiable") or 0)
    ent = steps.get("entropy") or {}
    r["entropy"] = ent.get("value")
    r["dims"] = ent.get("dims")
    r["memories"] = ent.get("memories")
    agent = end.get("agent") or {}
    r["agent"] = _normalize_agent_outcome(agent.get("outcome"))
    r["agent_rc"] = agent.get("rc")
    r["agent_runs_today"] = agent.get("runs_today")
    r["agent_cap"] = agent.get("cap")
    r["agent_fail_streak"] = agent.get("fail_streak")
    r["run_state"] = "complete"


def _run_from_jsonl(start, end, now):
    if start is None:
        # An end with no start line at all (truncated/corrupted history) —
        # still surface it rather than dropping data silently.
        ts = _parse_ts(end.get("start_ts")) or _parse_ts(end.get("ts"))
        if ts is None:
            return None
        r = _empty_run(ts)
        r["source"] = "jsonl"
        _apply_end(r, end)
        return r

    ts = _parse_ts(start.get("ts"))
    if ts is None:
        return None
    if start.get("dry_run"):
        return None   # dry runs excluded from the read model (parity w/ legacy)

    r = _empty_run(ts)
    r["source"] = "jsonl"
    r["dry_run"] = bool(start.get("dry_run"))
    r["mode"] = start.get("mode")

    if end is None:
        age = (now - ts).total_seconds()
        if age <= INFLIGHT_WINDOW_SECS:
            r["run_state"] = "in-flight"
            r["agent"] = "running"
        else:
            r["run_state"] = "crashed"
        return r

    _apply_end(r, end)
    return r


def _split_md_blocks(text):
    """{ts_iso: block_text} for every '## Daily run …' block in a .md file."""
    lines = text.splitlines()
    out = {}
    i = 0
    while i < len(lines):
        m = LEGACY_RUN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        ts_iso = m.group(1)
        start_i = i
        i += 1
        while i < len(lines) and not LEGACY_RUN_RE.match(lines[i]):
            i += 1
        out[ts_iso] = "\n".join(lines[start_i:i])
    return out


# ---------------------------------------------------------------------------
# Legacy prose fallback (deprecated — see module docstring "cutover")
# ---------------------------------------------------------------------------

def _agent_log_fresh(company, ts, now=None):
    p = Path(company) / "ops" / "logs" / f"agent-{ts.strftime('%Y-%m-%d')}.log"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return False
    now = time.time() if now is None else now
    return (now - mtime) <= INFLIGHT_WINDOW_SECS


def _legacy_parse_file(path):
    """Deprecated: parse one daily-<date>.md (no sibling .jsonl) into Run
    dicts, reproducing the pre-Phase-27 RUN_RE block-walk field-for-field."""
    text = _safe_read(path)
    lines = text.splitlines()
    runs = []
    i = 0
    while i < len(lines):
        m = LEGACY_RUN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        ts, tail = _parse_ts(m.group(1)), m.group(2)
        block_start = i
        i += 1
        if ts is None or "dry-run" in tail:
            while i < len(lines) and not LEGACY_RUN_RE.match(lines[i]):
                i += 1
            continue
        r = _empty_run(ts)
        r["source"] = "legacy-md"
        agent_attempted = False
        while i < len(lines) and not LEGACY_RUN_RE.match(lines[i]):
            ln = lines[i]
            dm = re.search(r"drop (\d+) \| demote (\d+) \| archive (\d+) \| upgrade-candidates (\d+)", ln)
            if dm:
                r["drop"], r["demote"], r["archive"], r["upgrade"] = map(int, dm.groups())
            vm = re.search(r"newly-verified (\d+)", ln)
            if vm:
                r["verified"] = int(vm.group(1))
            vm2 = re.search(r"unverifiable (\d+)", ln)
            if vm2:
                r["unverifiable"] = int(vm2.group(1))
            em = re.search(r"entropy ([0-9.]+).*over (\d+) memories", ln)
            if em:
                r["entropy"] = float(em.group(1))
                r["memories"] = int(em.group(2))
            wm = re.match(r"^- warnings: (\d+)", ln)
            if wm:
                r["warnings"] += int(wm.group(1))
            if ln.startswith("- CORE ABORTED:"):
                r["core_aborted"] = True
                r["abort_reason"] = ln[len("- CORE ABORTED:"):].strip()
            if ln.startswith("- LOCK STALE:"):
                r["lock_stale"] = True
            if ln.startswith(("- agent:", "- agent (", "- agent prompt")):
                if ln.startswith("- agent prompt"):
                    agent_attempted = True
                elif "AUTH_FAIL" in ln or "auth pre-flight" in ln:
                    r["agent"] = "auth-fail"
                elif "TIMEOUT" in ln:
                    r["agent"] = "timeout"
                elif " ok" in ln:
                    r["agent"] = "ok"
                elif "skip" in ln:
                    r["agent"] = "skipped"
                else:
                    r["agent"] = "failed"
            if "absorbed" in ln.lower() or re.search(r"→ status: archived", ln):
                r["merged"] += 1
            if re.search(r"promoted|L0\s*->\s*L1|L0→L1", ln):
                r["promoted"] += 1
            i += 1
        if agent_attempted and r["agent"] is None:
            r["agent"] = "failed"
            r["_silent_death"] = True
        r["md_block"] = "\n".join(lines[block_start:i])
        runs.append(r)
    return runs


def _reclassify_legacy_inflight(company, runs):
    """Retained ONLY for legacy (pre-cutover) history: the last run overall,
    if it's a legacy silent-death block AND its agent log is still fresh, is
    `running` rather than a false `failed`. Never applied to jsonl-sourced
    runs (those classify in-flight/crashed from timestamps alone, above)."""
    if not runs:
        return
    last = runs[-1]
    if last.get("source") == "legacy-md" and last.pop("_silent_death", False):
        if _agent_log_fresh(company, last["ts"]):
            last["agent"] = "running"


# ---------------------------------------------------------------------------
# Public reader
# ---------------------------------------------------------------------------

def read_runs(company, window_days=DEFAULT_WINDOW_DAYS, now=None):
    """Return Run dicts (see module docstring), sorted by ts ascending.

    window_days=None (or the --all CLI flag) returns the full history for
    consumers that genuinely want it (org-status/fleet); the default 30-day
    window bounds SessionStart-hook latency permanently (Item 5)."""
    now = now or datetime.now()
    logdir = _logs_dir(company)
    runs = []
    # MUST-FIX 1: the JSONL is the source of truth, so enumerate the .jsonl
    # files DIRECTLY (a day where the .md write failed but the JSONL append
    # succeeded was previously invisible — read_runs only globbed .md and
    # checked for a sibling). Collect every date that has EITHER artifact; a
    # date with a .jsonl reads through it (md_block is a best-effort extra from
    # the .md if present), and a .md-only date falls back to the legacy parser.
    try:
        jsonl_files = list(logdir.glob("daily-*.jsonl"))
        md_files = list(logdir.glob("daily-*.md"))
    except OSError:
        jsonl_files, md_files = [], []
    jsonl_dates = {p.stem[len("daily-"):] for p in jsonl_files}
    md_dates = {p.stem[len("daily-"):] for p in md_files}
    for date_str in sorted(jsonl_dates | md_dates):
        jsonl = logdir / f"daily-{date_str}.jsonl"
        md = logdir / f"daily-{date_str}.md"
        if date_str in jsonl_dates:
            pairs = _pair_events(_read_jsonl_events(jsonl))
            md_blocks = _split_md_blocks(_safe_read(md)) if date_str in md_dates else {}
            for start, end in pairs:
                r = _run_from_jsonl(start, end, now)
                if r is None:
                    continue
                r["md_block"] = md_blocks.get(r["ts"].isoformat(), "")
                runs.append(r)
        else:
            runs.extend(_legacy_parse_file(md))
    runs.sort(key=lambda r: r["ts"])
    _reclassify_legacy_inflight(company, runs)
    if window_days is not None:
        cutoff = now - timedelta(days=window_days)
        runs = [r for r in runs if r["ts"] >= cutoff]
    return runs


# ---------------------------------------------------------------------------
# Item 5: age-prune ops/logs by FILENAME date, never mtime (a restored backup
# must not resurrect ancient logs' lifetimes). Best-effort: a failure (e.g. a
# read-only dir) must never fail the caller's run — daily-run.sh logs a
# warning and moves on.
# ---------------------------------------------------------------------------

DEFAULT_RETAIN_DAYS = 90

_AGENT_LOG_RE = re.compile(r"^agent-(\d{4}-\d{2}-\d{2})\.log$")
_DAILY_FILE_RE = re.compile(r"^daily-(\d{4}-\d{2}-\d{2})\.(md|jsonl)$")
_AGENT_RUNS_RE = re.compile(r"^\.agent_runs_(\d{4}-\d{2}-\d{2})$")


def prune(company, retain_days=DEFAULT_RETAIN_DAYS, today=None, window_days=DEFAULT_WINDOW_DAYS):
    """Delete agent-*.log / daily-*.md / daily-*.jsonl older than retain_days
    (by FILENAME date), and every .agent_runs_* counter not from `today` (the
    daily token-cap ledger — today's counter must never be touched, or the
    breaker resets mid-day). Returns (removed_count, warning_or_None).

    Refuses (removes nothing) when retain_days < window_days + 1 — never
    delete what the default reader still parses."""
    if retain_days < window_days + 1:
        return 0, (f"prune refused: retain-days {retain_days} < window+1 "
                    f"({window_days + 1}) — would delete what the default reader still reads")
    today = today or datetime.now().date()
    if isinstance(today, str):
        try:
            today = datetime.strptime(today, "%Y-%m-%d").date()
        except ValueError:
            today = datetime.now().date()
    cutoff = today - timedelta(days=retain_days)
    logdir = _logs_dir(company)
    try:
        entries = list(logdir.iterdir())
    except FileNotFoundError:
        return 0, None   # nothing to prune yet (fresh install) — not a failure
    except OSError as e:
        return 0, f"prune skipped: {e}"   # e.g. a read-only/inaccessible dir

    removed = 0
    for p in entries:
        name = p.name
        m = _AGENT_RUNS_RE.match(name)
        if m:
            try:
                file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if file_date >= today:
                continue   # NEVER today's counter — the cap ledger stays intact mid-day
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
            continue
        m = _AGENT_LOG_RE.match(name) or _DAILY_FILE_RE.match(name)
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue   # malformed filename date — skip, never crash
        if file_date < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed, None


# ---------------------------------------------------------------------------
# CLI — `append` is daily-run.sh's write path; the rest are debug helpers.
# ---------------------------------------------------------------------------

def _cmd_append(args):
    data = sys.stdin.read()
    try:
        obj = json.loads(data)
    except ValueError:
        return 1
    return 0 if append_event(args.path, obj) else 1


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError


def _cmd_dump(args):
    window = None if args.all else args.window_days
    runs = read_runs(args.company, window_days=window)
    print(json.dumps(runs, default=_json_default, indent=2))
    return 0


def _cmd_prune(args):
    removed, warning = prune(args.company, retain_days=args.retain_days,
                              today=args.today, window_days=args.window_days)
    if warning:
        print(warning)
        return 0   # best-effort: a prune warning is never a fatal CLI error
    print(f"removed {removed} file(s) (>{args.retain_days}d)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("append", help="append one JSON event (read from stdin) to --path")
    p.add_argument("--path", required=True)
    p.set_defaults(func=_cmd_append)

    p = sub.add_parser("dump", help="debug: print read_runs() as JSON")
    p.add_argument("--company", default=".company")
    p.add_argument("--window-days", dest="window_days", type=int, default=DEFAULT_WINDOW_DAYS)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=_cmd_dump)

    p = sub.add_parser("prune", help="age-prune ops/logs by filename date (Item 5)")
    p.add_argument("--company", default=".company")
    p.add_argument("--retain-days", dest="retain_days", type=int, default=DEFAULT_RETAIN_DAYS)
    p.add_argument("--today", default=None)
    p.add_argument("--window-days", dest="window_days", type=int, default=DEFAULT_WINDOW_DAYS)
    p.set_defaults(func=_cmd_prune)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
