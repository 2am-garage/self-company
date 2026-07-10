#!/usr/bin/env python3
"""
fleet.py — holding-company (Phase 8) subsidiary registry + fleet state.

The PARENT company owns a registry of sub-companies and drives their maintenance
from ONE cron (see fleet-run.sh). This module is the read-only-toward-subs data
layer: it parses the registry, reads each sub's OWN entropy OUTPUT (its daily
log — never its memory files or personas), decides which subs warrant the
expensive agent this tick, and persists a tiny per-sub state file UNDER THE
PARENT. It NEVER writes into a sub's `.company/`.

Isolation invariant: the parent orchestrates SCHEDULING + BUDGET only. Nothing
here opens a sub's memory/ or personas/. Entropy is read from the artifact the
sub's own daily-run.sh already wrote (ops/logs/daily-*.md).

--------------------------------------------------------------------------------
REGISTRY FORMAT — `<parent>/.company/org/subsidiaries.md`
--------------------------------------------------------------------------------
Human-editable markdown table, one row per sub. Blank lines, `#` comments, prose,
and the header/separator rows are ignored, so you can annotate freely:

    | path                 | weight | enabled |
    |----------------------|--------|---------|
    | /home/uwe/proj-alpha | 2      | true    |
    | /home/uwe/proj-beta  | 1      | true    |
    | /home/uwe/retired    | 1      | false   |

Columns (only `path` is required; the rest are optional with defaults):
  - path     absolute project dir of the sub (the dir that contains `.company/`).
             `~` is expanded; a relative path resolves against the PARENT dir.
  - weight   int budget priority, default 1. Higher = more likely to win the
             agent when the budget is tight.
  - enabled  bool, default true. false => the row is dropped (skipped, reported).

Reader semantics (scan_registry):
  - a row whose resolved `<path>/.company/` is MISSING is DEAD: skipped + flagged
    (reported, never fatal).
  - duplicate paths are deduped (first wins) with a warning.
  - disabled rows are dropped (reported).
  - missing / empty / malformed registry => empty live list, never a crash.

Pure stdlib. Run `python3 fleet.py --help` for the CLI used by fleet-run.sh.
"""

import argparse
import json
import os
import re
import sys
from collections import namedtuple

# Phase 27 Item 1: read run boundaries + entropy/memories through the shared
# reader instead of a private run-header/entropy-line regex; only the
# duplicate-candidates prose (not part of daily_log's Run schema — it's
# entropy.py's own scored-pairs detail) is still scanned from the run's raw
# block text.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily_log  # noqa: E402

Sub = namedtuple("Sub", ["path", "weight", "enabled"])

RegistryScan = namedtuple(
    "RegistryScan", ["live", "dead", "disabled", "duplicates", "warnings"]
)

# The duplicate-candidates line (only present when there ARE scored dup pairs):
#   "  - duplicate candidates: [['a', 'b'], ['c', 'd']]"
_DUPLINE_RE = re.compile(r"- duplicate candidates:\s*(\[.*\])\s*$")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def registry_path(parent_dir):
    return os.path.join(parent_dir, ".company", "org", "subsidiaries.md")


def _resolve_path(raw, parent_dir):
    p = os.path.expanduser(raw.strip())
    if not os.path.isabs(p):
        p = os.path.join(parent_dir, p)
    return os.path.normpath(p)


def _parse_bool(raw, default=True):
    v = (raw or "").strip().lower()
    if v in ("true", "yes", "1", "on", "enabled"):
        return True
    if v in ("false", "no", "0", "off", "disabled"):
        return False
    return default


def _parse_weight(raw, default=1):
    try:
        w = int(str(raw).strip())
        return w if w >= 0 else default
    except (ValueError, TypeError):
        return default


def scan_registry(parent_dir):
    """
    Parse the registry into a RegistryScan. Never raises: a missing / empty /
    malformed registry yields an all-empty scan.

    live       : list[Sub] — enabled, deduped, `.company/` present (drives the sweep)
    dead       : list[str] — enabled rows whose `<path>/.company/` is missing
    disabled   : list[str] — rows dropped because enabled=false
    duplicates : list[str] — duplicate paths dropped (first occurrence wins)
    warnings   : list[str] — human-readable notes (dead / dup / malformed)
    """
    live, dead, disabled, duplicates, warnings = [], [], [], [], []
    path = registry_path(parent_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except OSError:
        return RegistryScan(live, dead, disabled, duplicates, warnings)

    seen = set()
    for lineno, line in enumerate(raw_lines, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if not s.startswith("|"):
            continue  # prose / notes outside the table
        cells = [c.strip() for c in s.strip("|").split("|")]
        first = cells[0].lower() if cells else ""
        # Skip the header row and the |---|---| separator row.
        if first in ("path", "") and not cells[0].startswith(("/", "~", ".")):
            if set("".join(cells)) <= set("-: "):
                continue
            if first == "path":
                continue
        if not cells or not cells[0]:
            continue
        raw_path = cells[0]
        if set(raw_path) <= set("-: "):  # separator row like `|---|---|`
            continue

        resolved = _resolve_path(raw_path, parent_dir)
        weight = _parse_weight(cells[1]) if len(cells) > 1 else 1
        enabled = _parse_bool(cells[2]) if len(cells) > 2 else True

        if resolved in seen:
            duplicates.append(resolved)
            warnings.append(f"duplicate path deduped (line {lineno}): {resolved}")
            continue
        seen.add(resolved)

        if not enabled:
            disabled.append(resolved)
            continue

        if not os.path.isdir(os.path.join(resolved, ".company")):
            dead.append(resolved)
            warnings.append(
                f"DEAD sub skipped (line {lineno}): no .company/ at {resolved}"
            )
            continue

        live.append(Sub(path=resolved, weight=weight, enabled=True))

    return RegistryScan(live, dead, disabled, duplicates, warnings)


def read_registry(parent_dir):
    """Convenience: the live subs only (enabled, deduped, `.company/` present)."""
    return scan_registry(parent_dir).live


# ---------------------------------------------------------------------------
# Per-sub state store — <parent>/.company/ops/fleet-state.json
# ---------------------------------------------------------------------------

def state_path(parent_dir):
    return os.path.join(parent_dir, ".company", "ops", "fleet-state.json")


def read_state(parent_dir):
    """sub-path -> {"last_entropy": float, "last_tick": "YYYY-MM-DD"}. {} on any error."""
    try:
        with open(state_path(parent_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(parent_dir, state):
    p = state_path(parent_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Read a sub's post-run entropy from its OWN daily log (never its memory)
# ---------------------------------------------------------------------------

def read_sub_entropy(sub_path):
    """
    Read the sub's newest recorded run — entropy + memories via daily_log.py
    (Item 1's shared reader), scored-dup backlog via the run's own raw block
    text (a detail entropy.py prints that isn't part of the Run schema). This
    reads the sub's own OUTPUT artifact, not its memory/ or personas/.

    Returns {"entropy": float, "dup_backlog": int, "memories": int} or None if
    the sub has no run with a parseable entropy value.
    """
    company = os.path.join(sub_path, ".company")
    runs = [r for r in daily_log.read_runs(company, window_days=None)
            if r.get("entropy") is not None]
    if not runs:
        return None
    last = runs[-1]
    dup_backlog = 0
    dm = _DUPLINE_RE.search(last.get("md_block") or "")
    if dm:
        try:
            import ast
            pairs = ast.literal_eval(dm.group(1))
            dup_backlog = len(pairs) if isinstance(pairs, list) else 0
        except (ValueError, SyntaxError):
            dup_backlog = 0
    return {"entropy": last["entropy"], "dup_backlog": dup_backlog,
            "memories": last["memories"]}


# ---------------------------------------------------------------------------
# Budget / qualification logic
# ---------------------------------------------------------------------------

def qualify(entropy, last_entropy, dup_backlog, dup_threshold):
    """
    A sub QUALIFIES for the expensive agent this tick if its entropy ROSE since
    its last recorded tick OR its scored-dup backlog exceeds the threshold.

    Returns (qualified: bool, delta: float, reason: str). delta = entropy -
    last_entropy (0.0 when there is no prior tick — a first-seen sub can only
    qualify via its backlog, never on an unknowable rise).
    """
    if last_entropy is None:
        delta = 0.0
        rose = False
    else:
        delta = round(entropy - last_entropy, 6)
        rose = delta > 0
    backlog_over = dup_backlog > dup_threshold
    if rose and backlog_over:
        reason = "risen+backlog"
    elif rose:
        reason = "risen"
    elif backlog_over:
        reason = "backlog"
    else:
        reason = "stable"
    return (rose or backlog_over), delta, reason


def plan_fleet(results, state, budget, dup_threshold):
    """
    Decide the agent pass. `results` is a list of dicts:
        {"path", "entropy", "dup_backlog", "weight"}
    `state` is read_state()'s mapping. Returns a list of decision dicts (one per
    sub, in the SAME order as `results`) with keys:
        path, entropy, delta, weight, qualified, reason, rank_score, selected,
        defer_rank
    Selection: qualifying subs ranked by (delta * weight) desc (ties broken by
    dup_backlog desc then path) — top `budget` are `selected`; the rest are
    budget-deferred with their 1-based rank among qualifiers in `defer_rank`.
    Never selects more than `budget` (hard ceiling).
    """
    decisions = []
    for r in results:
        last = state.get(r["path"], {}).get("last_entropy")
        q, delta, reason = qualify(
            r["entropy"], last, r["dup_backlog"], dup_threshold
        )
        decisions.append({
            "path": r["path"],
            "entropy": r["entropy"],
            "delta": delta,
            "weight": r["weight"],
            "dup_backlog": r["dup_backlog"],
            "qualified": q,
            "reason": reason,
            "rank_score": round(delta * r["weight"], 6),
            "selected": False,
            "defer_rank": None,
        })

    qualifiers = [d for d in decisions if d["qualified"]]
    qualifiers.sort(
        key=lambda d: (d["rank_score"], d["dup_backlog"], d["path"]),
        reverse=True,
    )
    for rank, d in enumerate(qualifiers, 1):
        if rank <= max(0, budget):
            d["selected"] = True
        else:
            d["defer_rank"] = rank
    return decisions


# ---------------------------------------------------------------------------
# Fleet ledger — <parent>/.company/ops/reports/fleet-ledger.md
# ---------------------------------------------------------------------------

def ledger_path(parent_dir):
    return os.path.join(parent_dir, ".company", "ops", "reports", "fleet-ledger.md")


def append_ledger(parent_dir, tick, rows):
    """
    Append one fleet-run's per-sub rows to the combined ledger. `rows` is a list
    of dicts: {path, entropy, delta, verdict, agent}. Creates the table header
    on first write.
    """
    p = ledger_path(parent_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    new = not os.path.exists(p)
    header = (
        "# Fleet Ledger — holding-company sweeps\n\n"
        "_One block per `fleet-run.sh` tick; one row per sub. entropy is the "
        "sub's post-cheap-pass headline; delta is vs its last recorded tick; "
        "agent = ran | budget-deferred | auth-skip. Generated by fleet-run.sh._\n"
    )
    lines = []
    if new:
        lines.append(header)
    lines.append(f"\n## Fleet tick {tick}\n")
    lines.append("| sub | entropy | delta | verdict | agent |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        d = r["delta"]
        darrow = f"+{d}" if isinstance(d, (int, float)) and d > 0 else f"{d}"
        lines.append(
            f"| {r['path']} | {r['entropy']} | {darrow} | "
            f"{r['verdict']} | {r['agent']} |"
        )
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# CLI (used by fleet-run.sh)
# ---------------------------------------------------------------------------

def _cmd_scan(args):
    scan = scan_registry(args.parent)
    if args.json:
        print(json.dumps({
            "live": [s._asdict() for s in scan.live],
            "dead": scan.dead,
            "disabled": scan.disabled,
            "duplicates": scan.duplicates,
            "warnings": scan.warnings,
        }, indent=2))
    else:
        for s in scan.live:
            print(f"{s.path}\t{s.weight}")
    return 0


def _cmd_sub_entropy(args):
    e = read_sub_entropy(args.sub)
    if e is None:
        print("", end="")
        return 1
    print(f"{e['entropy']}\t{e['dup_backlog']}\t{e['memories']}")
    return 0


def _cmd_plan(args):
    results = []
    with open(args.results, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.rstrip("\n")
            if not ln:
                continue
            path, entropy, dup_backlog, weight = ln.split("\t")
            results.append({
                "path": path,
                "entropy": float(entropy),
                "dup_backlog": int(dup_backlog),
                "weight": int(weight),
            })
    state = read_state(args.parent)
    decisions = plan_fleet(results, state, args.budget, args.dup_threshold)
    # Emit decision TSV: path, entropy, delta, verdict, selected(1/0), defer_rank, reason
    for d in decisions:
        defer = d["defer_rank"] if d["defer_rank"] is not None else 0
        print("\t".join([
            d["path"], str(d["entropy"]), str(d["delta"]),
            d["reason"], "1" if d["selected"] else "0",
            str(defer), str(d["dup_backlog"]),
        ]))
    return 0


def _cmd_commit(args):
    """Read final decided rows (TSV) + update fleet-state, append fleet-ledger."""
    rows = []
    state = read_state(args.parent)
    with open(args.rows, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.rstrip("\n")
            if not ln:
                continue
            path, entropy, delta, verdict, agent = ln.split("\t")
            rows.append({
                "path": path, "entropy": float(entropy),
                "delta": float(delta), "verdict": verdict, "agent": agent,
            })
            state[path] = {"last_entropy": float(entropy), "last_tick": args.tick}
    append_ledger(args.parent, args.tick, rows)
    write_state(args.parent, state)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="self-company fleet registry + state")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="parse the registry")
    p.add_argument("--parent", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_scan)

    p = sub.add_parser("sub-entropy", help="read a sub's post-run entropy from its log")
    p.add_argument("--sub", required=True)
    p.set_defaults(func=_cmd_sub_entropy)

    p = sub.add_parser("plan", help="compute qualify/rank/selection")
    p.add_argument("--parent", required=True)
    p.add_argument("--results", required=True, help="TSV: path\\tentropy\\tdup_backlog\\tweight")
    p.add_argument("--budget", type=int, default=3)
    p.add_argument("--dup-threshold", dest="dup_threshold", type=int, default=5)
    p.set_defaults(func=_cmd_plan)

    p = sub.add_parser("commit", help="append ledger + update fleet-state")
    p.add_argument("--parent", required=True)
    p.add_argument("--rows", required=True, help="TSV: path\\tentropy\\tdelta\\tverdict\\tagent")
    p.add_argument("--tick", required=True)
    p.set_defaults(func=_cmd_commit)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
