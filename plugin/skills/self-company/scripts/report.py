#!/usr/bin/env python3
"""
report.py — the scheduled-work ledger (autoresearch-style).

Karpathy's autoresearch greets you each morning with a flat, append-only
`results.tsv`: one row per experiment, a single headline metric that goes up or
down, a keep/discard/crash verdict, and a one-line description of what was tried.
You scan it in seconds.

This builds the same thing for the self-company's unattended daily runs. One row
per `daily-run.sh` execution, read through daily_log.py (Phase 27 Item 1's shared
JSONL reader — the .md log stays the human render; the JSONL is the data source):

  run                entropy(down)   mem   status   what happened
  06-29 18:07        0.0356 v0.0516   45   keep     verify +14, merged dups, 1 upgrade-cand

Mapping (autoresearch -> self-company):
  commit      -> run timestamp
  val_bpb     -> entropy        (lower is better; same direction as val_bpb)
  memory_gb   -> memory count
  status      -> keep / flat / skip / fail / warn / abort / locked / stale-lock
                 / running / crashed
  description -> decay/verify/agent actions this run

Status verdict:
  keep       — something substantive moved (entropy dropped, decayed, verified,
               or upgrade candidates surfaced) — the "keep" of a good experiment
  flat       — ran clean but nothing changed (no-op maintenance) — like "discard"
  skip       — agent step was BENIGNLY skipped (daily cap hit / no claude CLI)
  fail       — the agent died (rc!=0), TIMED OUT, or was AUTH_FAIL-skipped — an
               unhealthy agent day is never masked as keep/skip, even when the
               deterministic half moved things (Phase 5 Item 3 / N4: the 18:07
               "keep | verify +68" row on a dead-agent day was the bug)
  warn       — a memory-rot warning (Phase 25 Item 3) OR a core-step timeout
               (Phase 27 Item 4) was recorded this run — never masked behind
               keep/flat even when something else legitimately moved
  abort      — the deterministic core's safety floor failed (Phase 25 Item 1) —
               reinforce/decay/verify did NOT run this tick
  locked     — a benign one-off flock contention: this cron tick found
               .daily.lock already held and skipped (Phase 27 Item 3) — a
               RECORDED tick that did NOT run, distinct from "ran, nothing
               changed"
  stale-lock — a wedged/orphaned holder past the staleness tripwire
               (Phase 25 Item 4.3) — never a silent skip
  running    — the run's agent step is still streaming (in-flight)
  crashed    — a `start` event with no matching `end` past the in-flight
               window (Phase 27 Item 1) — the process died mid-core (kill -9,
               reboot), classified purely from timestamps, never via
               agent-log-mtime probing

Usage:
  report.py [--company DIR]                 # print markdown ledger to stdout
  report.py [--company DIR] --write         # also write ops/reports/ledger.md
  report.py [--company DIR] --tsv           # emit raw TSV instead of markdown
  report.py [--company DIR] --limit N       # only the last N runs
  report.py [--company DIR] --window-days N # only runs within N days (default 30)
  report.py [--company DIR] --all           # full history, ignoring the window

Pure stdlib, read-only (except --write).
"""

import argparse
import os
import sys
from pathlib import Path

# Phase 27 Item 1: the ONE shared reader. report.py no longer owns a private
# run-header regex, a block-walker, or the agent-log-mtime in-flight heuristic — daily_log.py
# does, once, for every consumer (report/notify-status/org-status/fleet).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily_log  # noqa: E402


def collect(company, window_days=daily_log.DEFAULT_WINDOW_DAYS):
    """Every real (non-dry-run) daily-run, oldest first, as a Run dict (see
    daily_log.py). Item 5: reports read a month by default, not a lifetime —
    pass window_days=None (the ledger's --all flag) for the full history."""
    return daily_log.read_runs(company, window_days=window_days)


def verdict(r, prev_entropy):
    # Phase 27 Item 1: a run whose `start` event has no matching `end` — the
    # process died mid-core (kill -9, box reboot) — classifies `crashed`
    # purely from timestamps (daily_log.py), never `flat`/`keep`, and never
    # via mtime-probing an agent log (that race is retired by construction).
    if r.get("run_state") == "crashed":
        return "crashed"
    # Phase 25 Item 1: an ABORTED deterministic core (the pre-apply snapshot
    # failed, or the free-space preflight came in under threshold — the
    # safety floor failed) is the single highest-priority signal: it is NEVER
    # rendered flat/keep, checked before everything else including a running
    # agent (a CORE_ABORT also skips the agent step, so the two never
    # co-occur, but the priority is explicit regardless).
    if r.get("core_aborted"):
        return "abort"
    # Phase 25 Item 4.3: a stale-lock tripwire (a wedged/orphaned run holding
    # .daily.lock past the escalation threshold) is likewise never a silent
    # skip — it needs a human to look, not a routine contention line.
    if r.get("lock_stale"):
        return "stale-lock"
    # Item 3: a benign one-off flock contention (this cron tick found the lock
    # already held and skipped, no pile-up) is a RECORDED tick that did NOT
    # run — rendered distinctly as `locked`, never `flat` ("ran clean but
    # nothing changed") or `skip` (the agent's own benign-skip vocabulary).
    if r.get("lock") == "skipped":
        return "locked"
    # C2: an in-flight run (latest block, prompt built, agent still streaming) is
    # neither keep nor fail yet — it is `running`, and self-corrects on the
    # outcome line. Checked FIRST so a live agent is never rendered `fail`.
    if r["agent"] == "running":
        return "running"
    # B3 (Phase 5 Item 3, N4): a run where the agent died (rc!=0), timed out,
    # or was AUTH_FAIL-skipped is a FAILED run — the deterministic half's
    # progress is noted in the description column but can never turn the
    # verdict green. Only benign skips (daily cap / no CLI) stay `skip`.
    if r["agent"] in ("failed", "timeout", "auth-fail"):
        return "fail"
    # Phase 25 Item 3 (Gibby re-attack fix): memory-rot warnings (corrupt file /
    # missing id / refused reap / failed-to-apply) computed this run must never
    # render as a healthy verdict — and that includes `keep`, not only `flat`.
    # This check MUST precede the `moved` check: a tick where something
    # legitimately moved (a stale file dropped) AND decay also flagged a
    # corrupt file would otherwise render `keep`, hiding the corruption in the
    # status field (it survived only in the prose column). Item 3's acceptance
    # is explicit — a warnings-bearing run "must not render as healthy
    # flat/keep" — so `warn` wins over movement. A clean run (warnings: 0) is
    # unaffected and still classifies by movement below. (notify-status.py
    # already treats warnings unconditionally; this brings report.py in line.)
    if r.get("warnings"):
        return "warn"
    # Item 4: a core-step timeout (reinforce/decay/verify/entropy/rag-index
    # killed at budget) must never render flat/keep — the tick made no
    # promise it kept, even if some OTHER step legitimately moved something.
    if any(isinstance(s, dict) and s.get("outcome") == "timeout"
           for s in (r.get("steps") or {}).values()):
        return "warn"
    moved = (
        r["drop"] or r["demote"] or r["archive"] or r["upgrade"]
        or r["verified"] or r["merged"] or r["promoted"]
        or (prev_entropy is not None and r["entropy"] is not None and r["entropy"] < prev_entropy)
    )
    if moved:
        return "keep"
    if r["agent"] == "skipped":
        return "skip"
    return "flat"


def describe(r):
    bits = []
    if r.get("run_state") == "crashed":
        bits.append("CRASHED — start recorded, no end (process died mid-run)")
        return ", ".join(bits)
    # Phase 25 Item 1/4.3: the two safety-floor signals lead the description
    # unconditionally — a human reading one line must see these first.
    if r.get("core_aborted"):
        bits.append(f"CORE ABORTED — {r.get('abort_reason') or 'safety floor failed'}")
    if r.get("lock_stale"):
        bits.append("LOCK STALE — wedged/orphaned run holding .daily.lock")
    if r.get("lock") == "skipped":
        bits.append("locked — cron tick skipped (concurrent run held .daily.lock)")
        return ", ".join(bits)
    # C2: an in-flight agent leads the description — the deterministic half's
    # progress follows, but the run is not done, so it is neither keep nor fail.
    if r["agent"] == "running":
        bits.append("agent running (in-flight)")
    # B3: agent health leads the description on a red day — the deterministic
    # half's progress follows (reported, but it never greens the verdict).
    elif r["agent"] == "timeout":
        bits.append("agent TIMEOUT (partial trail in agent log)")
    elif r["agent"] == "failed":
        bits.append("agent died")
    elif r["agent"] == "auth-fail":
        bits.append("AUTH_FAIL — run /login")
    if r["verified"]:
        bits.append(f"verify +{r['verified']}")
    if r["drop"]:
        bits.append(f"decayed {r['drop']}")
    if r["demote"]:
        bits.append(f"demoted {r['demote']}")
    if r["archive"]:
        bits.append(f"archived {r['archive']}")
    if r["merged"]:
        bits.append(f"merged {r['merged']} dup")
    if r["promoted"]:
        bits.append(f"+{r['promoted']}→L1")
    if r["upgrade"]:
        bits.append(f"{r['upgrade']} upgrade-cand")
    if r.get("warnings"):
        bits.append(f"{r['warnings']} warning(s)")
    timed_out = [name for name, s in (r.get("steps") or {}).items()
                 if isinstance(s, dict) and s.get("outcome") == "timeout"]
    if timed_out:
        bits.append(f"TIMEOUT: {', '.join(sorted(timed_out))}")
    if not bits:
        bits.append("no-op maintenance")
    return ", ".join(bits)


def build(rows):
    out, prev = [], None
    for r in rows:
        ent = r["entropy"]
        arrow = ""
        if ent is not None and prev is not None:
            arrow = " v" if ent < prev else (" ^" if ent > prev else " =")
        out.append({
            "run": r["ts"].strftime("%m-%d %H:%M"),
            "entropy": f"{ent:.4f}{arrow}" if ent is not None else "?",
            "mem": r["memories"] if r["memories"] is not None else "?",
            "status": verdict(r, prev),
            "desc": describe(r),
        })
        if ent is not None:
            prev = ent
    return out


def render_md(table):
    head = "| run | entropy ↓ | mem | status | what happened |\n|---|---|---|---|---|"
    body = "\n".join(
        f"| {t['run']} | {t['entropy']} | {t['mem']} | `{t['status']}` | {t['desc']} |"
        for t in table)
    return head + "\n" + body if table else head + "\n| _no runs yet_ |  |  |  |  |"


def render_tsv(table):
    head = "run\tentropy\tmem\tstatus\tdescription"
    body = "\n".join(
        f"{t['run']}\t{t['entropy'].replace(' v','').replace(' ^','').replace(' =','')}\t"
        f"{t['mem']}\t{t['status']}\t{t['desc']}"
        for t in table)
    return head + "\n" + body


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default=".company")
    ap.add_argument("--write", action="store_true", help="write ops/reports/ledger.md")
    ap.add_argument("--tsv", action="store_true", help="emit raw TSV instead of markdown")
    ap.add_argument("--limit", type=int, default=0, help="only the last N runs")
    ap.add_argument("--window-days", dest="window_days", type=int,
                    default=daily_log.DEFAULT_WINDOW_DAYS,
                    help="only runs within the last N days (Item 5; default 30)")
    ap.add_argument("--all", action="store_true",
                    help="full history, ignoring --window-days (Item 5)")
    args = ap.parse_args(argv)

    rows = collect(args.company, window_days=None if args.all else args.window_days)
    table = build(rows)
    if args.limit > 0:
        table = table[-args.limit:]

    if args.tsv:
        print(render_tsv(table))
        return 0

    n = len(rows)
    last = table[-1] if table else None
    header = (f"# Scheduled-Work Ledger — {n} run{'s' if n != 1 else ''}\n\n"
              f"_One row per unattended daily-run. entropy is the headline metric "
              f"(lower = healthier). Generated by `report.py`._\n")
    if last:
        header += f"\n**Latest:** {last['run']} — entropy {last['entropy']}, memory {last['mem']}, `{last['status']}`\n"
    md = header + "\n" + render_md(table) + "\n"
    print(md)

    if args.write:
        rep = Path(args.company) / "ops" / "reports"
        rep.mkdir(parents=True, exist_ok=True)
        (rep / "ledger.md").write_text(md, encoding="utf-8")
        # Also emit the raw autoresearch-style flat file by default.
        (rep / "ledger.tsv").write_text(render_tsv(table) + "\n", encoding="utf-8")
        print(f"[report] wrote {rep / 'ledger.md'} + ledger.tsv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
