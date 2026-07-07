#!/usr/bin/env python3
"""
report.py — the scheduled-work ledger (autoresearch-style).

Karpathy's autoresearch greets you each morning with a flat, append-only
`results.tsv`: one row per experiment, a single headline metric that goes up or
down, a keep/discard/crash verdict, and a one-line description of what was tried.
You scan it in seconds.

This builds the same thing for the self-company's unattended daily runs. One row
per `daily-run.sh` execution, parsed deterministically from ops/logs/daily-*.md:

  run                entropy(down)   mem   status   what happened
  06-29 18:07        0.0356 v0.0516   45   keep     verify +14, merged dups, 1 upgrade-cand

Mapping (autoresearch -> self-company):
  commit      -> run timestamp
  val_bpb     -> entropy        (lower is better; same direction as val_bpb)
  memory_gb   -> memory count
  status      -> keep / flat / skip / fail
  description -> decay/verify/agent actions this run

Status verdict:
  keep  — something substantive moved (entropy dropped, decayed, verified, or
          upgrade candidates surfaced) — the "keep" of a good experiment
  flat  — ran clean but nothing changed (no-op maintenance) — like "discard"
  skip  — agent step was BENIGNLY skipped (daily cap hit / no claude CLI)
  fail  — the agent died (rc!=0), TIMED OUT, or was AUTH_FAIL-skipped — an
          unhealthy agent day is never masked as keep/skip, even when the
          deterministic half moved things (Phase 5 Item 3 / N4: the 18:07
          "keep | verify +68" row on a dead-agent day was the bug)

Usage:
  report.py [--company DIR]                 # print markdown ledger to stdout
  report.py [--company DIR] --write         # also write ops/reports/ledger.md
  report.py [--company DIR] --tsv           # emit raw TSV instead of markdown
  report.py [--company DIR] --limit N       # only the last N runs

Pure stdlib, read-only (except --write).
"""

import argparse
import re
from datetime import datetime
from pathlib import Path

RUN_RE = re.compile(r"^## Daily run (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(.*)$")


def _parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def collect(company):
    """Parse every real (non-dry-run) daily-run block into a structured row."""
    logs = sorted((Path(company) / "ops" / "logs").glob("daily-*.md"))
    rows = []
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
            r = {"ts": ts, "drop": 0, "demote": 0, "archive": 0, "upgrade": 0,
                 "verified": 0, "unverifiable": 0, "entropy": None, "memories": None,
                 "agent": None, "merged": 0, "promoted": 0}
            while i < len(lines) and not RUN_RE.match(lines[i]):
                ln = lines[i]
                dm = re.search(r"drop (\d+) \| demote (\d+) \| archive (\d+) \| upgrade-candidates (\d+)", ln)
                if dm:
                    r["drop"], r["demote"], r["archive"], r["upgrade"] = map(int, dm.groups())
                vm = re.search(r"newly-verified (\d+)", ln)
                if vm:
                    r["verified"] = int(vm.group(1))
                um = re.search(r"unverifiable \[?(\d+)?", ln)  # count in the summary line
                vm2 = re.search(r"unverifiable (\d+)", ln)
                if vm2:
                    r["unverifiable"] = int(vm2.group(1))
                em = re.search(r"entropy ([0-9.]+).*over (\d+) memories", ln)
                if em:
                    r["entropy"] = float(em.group(1))
                    r["memories"] = int(em.group(2))
                if ln.startswith(("- agent:", "- agent (", "- agent prompt")):
                    # B3 (Phase 5 Item 3, N4): classify the agent OUTCOME
                    # honestly. The old substring test counted an AUTH_FAIL
                    # skip as benign "skipped" and knew nothing of timeouts —
                    # a green ledger row on a red day. "- agent prompt: ..."
                    # is a breadcrumb, not an outcome (but remember we saw it:
                    # a prompt with NO outcome line means the run died before
                    # it could record one -> failed, see below).
                    # Only daily-run.sh's own writer shapes count ("- agent:",
                    # "- agent (", "- agent prompt"): a CAPTURE line for a
                    # memory whose slug starts with "agent" ("- agent-model-…
                    # (L0) — pending_verify") is DATA, not an outcome — it must
                    # never flip a healthy day red (or mask a red day).
                    if ln.startswith("- agent prompt"):
                        r["_agent_attempted"] = True
                    elif "AUTH_FAIL" in ln or "auth pre-flight" in ln:
                        r["agent"] = "auth-fail"
                    elif "TIMEOUT" in ln:
                        r["agent"] = "timeout"
                    elif " ok" in ln:
                        r["agent"] = "ok"
                    elif "skip" in ln:
                        r["agent"] = "skipped"   # benign: cap reached / no CLI
                    else:
                        r["agent"] = "failed"
                # agent consolidation prose: count merges / promotions if present
                if "absorbed" in ln.lower() or re.search(r"→ status: archived", ln):
                    r["merged"] += 1
                if re.search(r"promoted|L0\s*->\s*L1|L0→L1", ln):
                    r["promoted"] += 1
                i += 1
            # B3: an agent prompt was built but NO outcome line follows — the
            # run died before it could record one (no-output crash). Honest
            # classification: failed.
            if r.pop("_agent_attempted", False) and r["agent"] is None:
                r["agent"] = "failed"
            rows.append(r)
    rows.sort(key=lambda x: x["ts"])
    return rows


def verdict(r, prev_entropy):
    # B3 (Phase 5 Item 3, N4): a run where the agent died (rc!=0), timed out,
    # or was AUTH_FAIL-skipped is a FAILED run — the deterministic half's
    # progress is noted in the description column but can never turn the
    # verdict green. Only benign skips (daily cap / no CLI) stay `skip`.
    if r["agent"] in ("failed", "timeout", "auth-fail"):
        return "fail"
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
    # B3: agent health leads the description on a red day — the deterministic
    # half's progress follows (reported, but it never greens the verdict).
    if r["agent"] == "timeout":
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
    args = ap.parse_args(argv)

    rows = collect(args.company)
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
