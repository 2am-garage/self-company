#!/usr/bin/env python3
"""
org-status — visualize which employees are actually working.

The Chairman can't tell, in an interactive session, whether "Elon" is doing
everything or the org is really dividing the work. This renders an honest,
deterministic view from real logs:

  * daily-run logs (ops/logs/daily-*.md)  -> Tony / Gibby / Elon / Tom / agent
  * trigger ledger + trigger logs         -> Phoebe (dispatch) / Bob (dispatched)
  * each employee's own log.md            -> anything they recorded
  * live `claude -p` processes            -> who is running RIGHT NOW

It does not pretend seven daemons are bustling. Interactive chat is Elon-fronted;
the genuinely-separate work is the cron/dispatch/trigger agents. This shows that
split. Read-only, pure stdlib.

Usage:
  org-status.py [--company DIR] [--window-hours N]   # default 24
"""

import argparse
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

RUN_RE = re.compile(r"^## Daily run (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(.*)$")

# id, display, role, and the log-line signals that mean "this employee acted".
EMPLOYEES = [
    ("elon",   "Elon",   "CEO · direction",      [r"elon survey"]),
    ("phoebe", "Phoebe", "PM · exec gateway",    []),   # from the trigger ledger
    ("tony",   "Tony",   "Improvement · entropy",[r"^- decay", r"entropy [0-9.]", r"Consolidation pass", r"Upgrade candidates"]),
    ("gibby",  "Gibby",  "Verify · sources",     [r"verify --apply", r"^## CAPTURE"]),
    ("bob",    "Bob",    "Engineer · builds",    []),   # from dispatch mentions
    ("july",   "July",   "People · personas",    []),
    ("tom",    "Tom",    "Infra · scheduling",   [r"ledger: refreshed", r"agent \(consolidate"]),
]


def _parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _now():
    return datetime.now().replace(microsecond=0)


def scan_daily(company):
    """Return {emp_id: (ts, short_desc)} latest activity from daily-run logs."""
    out = {}
    logs = sorted((Path(company) / "ops" / "logs").glob("daily-*.md"))
    for f in logs[-3:]:                       # recent files only
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
            # Tom owns the run itself.
            out["tom"] = (ts, "ran daily-run.sh")
            block = []
            while i < len(lines) and not RUN_RE.match(lines[i]):
                block.append(lines[i]); i += 1
            text = "\n".join(block)
            for emp_id, _, _, signals in EMPLOYEES:
                for sig in signals:
                    hit = re.search(sig, text, re.M)
                    if hit:
                        desc = _desc_for(emp_id, text)
                        out[emp_id] = (ts, desc)
                        break
    return out


def _desc_for(emp_id, text):
    if emp_id == "tony":
        m = re.search(r"entropy ([0-9.]+).*over (\d+) memories", text)
        if m:
            return f"entropy {m.group(1)} over {m.group(2)} mem"
        return "decay / consolidate"
    if emp_id == "gibby":
        m = re.search(r"newly-verified (\d+)", text)
        return f"verify +{m.group(1)}" if m else "verify / capture"
    if emp_id == "elon":
        m = re.search(r"elon survey: (\d+) todo", text)
        return f"survey → {m.group(1)} todo" if m else "daily survey"
    if emp_id == "tom":
        return "daily-run + ledger"
    return "active"


def scan_triggers(company, out):
    """Phoebe (dispatch) and Bob (dispatched) from the trigger ledger + logs."""
    led = Path(company) / "ops" / "reports" / "triggers.md"
    if led.exists():
        last_fire = None
        for ln in led.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\|\s*(\d{4}-\d{2}-\d{2}T[\d:]+)\s*\|\s*([\w-]+)\s*\|\s*fired", ln)
            if m:
                last_fire = (_parse_ts(m.group(1)), f"dispatched '{m.group(2)}'")
        if last_fire and last_fire[0]:
            out["phoebe"] = last_fire
    # Bob shows up as a dispatch owner inside trigger logs.
    tlogs = sorted((Path(company) / "ops" / "logs").glob("trigger-*.md")) \
        + sorted((Path(company) / "ops" / "logs").glob("trigger-*.log"))
    for f in tlogs[-2:]:
        try:
            t = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(r"Owner:\s*Bob|assigned to Bob", t):
            # stamp with the file's own date if we can find a timestamp line
            m = re.search(r"(\d{4}-\d{2}-\d{2}[ T][\d:]+)", t)
            ts = _parse_ts(m.group(1).replace(" ", "T")) if m else None
            out.setdefault("bob", (ts, "dispatched task"))
    return out


def scan_emp_logs(company, out):
    """Each employee's own log.md — use its last dated line if newer."""
    for emp_id, *_ in EMPLOYEES:
        p = Path(company) / "org" / "employees" / emp_id / "log.md"
        if not p.exists():
            continue
        dates = re.findall(r"(\d{4}-\d{2}-\d{2})", p.read_text(encoding="utf-8"))
        if dates:
            ts = _parse_ts(max(dates) + "T00:00:00")
            prev = out.get(emp_id)
            if ts and (prev is None or prev[0] is None or ts > prev[0]):
                out[emp_id] = (ts, "log entry")
    return out


def scan_company_runs(company, out):
    """Employees dispatched by a session-triggered company run (Trigger #4).

    Reads <company>/ops/reports/company-runs.md. Each real table row is
    `| time | task | planned by | assignments | rc |`. The assignments column
    is a code-spanned JSON fragment that may be TRUNCATED (e.g. `{"bob": "...",
    "gib`), so ids are pulled best-effort and kept only if they name a real
    employee. Latest-wins. Robust to a missing/empty file.
    """
    p = Path(company) / "ops" / "reports" / "company-runs.md"
    if not p.exists():
        return out
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return out
    # Real employees: prefer the on-disk roster, fall back to the known set.
    known = {e[0] for e in EMPLOYEES}
    emp_dir = Path(company) / "org" / "employees"
    if emp_dir.is_dir():
        known |= {d.name for d in emp_dir.iterdir() if d.is_dir()}
    for ln in text.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        ts = _parse_ts(cells[0])
        if ts is None:                        # header / separator / prose line
            continue
        task = cells[1]
        for emp_id in re.findall(r'"(\w+)"\s*:', cells[3]):
            if emp_id not in known:
                continue                       # skip truncated / bogus keys
            prev = out.get(emp_id)
            if prev is None or prev[0] is None or ts > prev[0]:
                out[emp_id] = (ts, f"session: {task}")
    return out


def live_agents():
    """Count running `claude -p` and describe them (cron / trigger / dispatch)."""
    try:
        r = subprocess.run(["pgrep", "-af", "claude -p"],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    out = []
    for ln in r.stdout.splitlines():
        if "org-status" in ln or "pgrep" in ln:
            continue
        low = ln.lower()
        if "trigger" in low or "event trigger" in low:
            out.append("trigger dispatch")
        elif "consolidat" in low or "daily" in low:
            out.append("daily maintenance")
        else:
            out.append("agent")
    return out


def render(company, window_hours):
    now = _now()
    acts = scan_daily(company)
    scan_triggers(company, acts)
    scan_emp_logs(company, acts)
    scan_company_runs(company, acts)
    live = live_agents()
    cutoff = now - timedelta(hours=window_hours)

    W = 74
    def line(s=""):
        return "│ " + s[:W].ljust(W) + " │"

    rows = ["╭" + "─" * (W + 2) + "╮",
            line(f"self-company · org status        {now:%Y-%m-%d %H:%M}"),
            "├" + "─" * (W + 2) + "┤"]
    for emp_id, name, role, _ in EMPLOYEES:
        ts_desc = acts.get(emp_id)
        if ts_desc and ts_desc[0] and ts_desc[0] >= cutoff:
            dot = "*"                          # active in window
            when = f"{ts_desc[0]:%m-%d %H:%M}"; desc = ts_desc[1]
        elif ts_desc and ts_desc[0]:
            dot = "."; when = f"{ts_desc[0]:%m-%d %H:%M}"; desc = ts_desc[1]
        else:
            dot = "."; when = " --  --"; desc = "idle"
        rows.append(line(f"[{dot}] {name:<7}{role:<22} {when}  {desc}"))
    rows.append("├" + "─" * (W + 2) + "┤")
    if live:
        rows.append(line(f">> live now: {len(live)} claude -p — " + ", ".join(live[:3])))
    else:
        rows.append(line(">> live now: no background agents running"))
    rows.append(line("you talk to Elon in chat; the rest act via cron / dispatch / trigger"))
    rows.append(line("[*] active <=window   [.] idle / last-seen"))
    rows.append("╰" + "─" * (W + 2) + "╯")
    return "\n".join(rows)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default=".company")
    ap.add_argument("--window-hours", type=int, default=24)
    args = ap.parse_args(argv)
    print(render(args.company, args.window_hours))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
