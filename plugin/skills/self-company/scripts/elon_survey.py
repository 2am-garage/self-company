#!/usr/bin/env python3
"""
elon_survey — Elon's daily CEO survey + prioritized TODO list.

So the CEO is load-bearing every day (not just when addressed): this surveys the
company's real state (entropy, decay/promotion candidates, verification gaps, tier
distribution) and writes a PRIORITIZED todo list to ops/plans/todo-<date>.md. It
is deterministic and read-only over memory (runs decay/verify in dry-run); it does
NOT mutate memory or the skeleton — pure direction-setting.

Usage: elon_survey.py [--company DIR] [--now YYYY-MM-DD]
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
# Phase 28 Item 4a (D4): tier_counts folds into the shared corpus.py primitive
# as a byproduct — its rglob-per-tier-dir loop was byte-identical to
# corpus.count_by_tier.
import corpus  # noqa: E402

# Phase 27 MUST-FIX 3: elon_survey re-invokes the deterministic core scripts
# (entropy/decay/verify) as read-only subprocesses. That re-invocation was
# bounded by a HARDCODED 120s, independent of daily-run.sh's core-step budget,
# AND a timeout was silently swallowed (returned None → "0 todos", no log/JSONL
# line) — a hung decay stretched one tick to 2m6s absorbed as healthy. Route
# the budget through the SAME env daily-run.sh uses, and make a timeout VISIBLE
# (recorded in `timed_out`, surfaced by daily-run.sh). NOTE: the structural fix
# — elon_survey should not re-invoke the core at all, but read the core's
# already-computed JSON — is Phase 28's job (Tony C1); this only makes the
# current re-invocation observable + bounded.
def _core_step_timeout():
    raw = os.environ.get("SELF_COMPANY_CORE_STEP_TIMEOUT", "900")
    try:
        v = int(raw)
        return v if v > 0 else 900
    except ValueError:
        return 900


def _run_json(script, *args, timed_out=None):
    p = SCRIPTS / script
    if not p.exists():
        return None
    try:
        out = subprocess.run([sys.executable, str(p), *args],
                             capture_output=True, text=True,
                             timeout=_core_step_timeout()).stdout
        return json.loads(out)
    except subprocess.TimeoutExpired:
        if timed_out is not None:
            timed_out.append(script)   # NEVER silently absorbed — surfaced below
        return None
    except Exception:
        return None


def tier_counts(memory_dir):
    # Phase 28 Item 4a: folds into corpus.count_by_tier (byte-identical loop).
    return corpus.count_by_tier(memory_dir)


def build_todos(entropy, decay, verify):
    """Rule-based prioritized todo list. Returns list of (priority, text)."""
    todos = []
    if entropy:
        det = entropy.get("details", {})
        contra = det.get("contradiction_pairs", [])
        if contra:
            todos.append((1, f"Adjudicate {len(contra)} contradiction candidate(s) "
                             f"(Tony): {contra[:4]}"))
        dups = det.get("duplicate_pairs", [])
        if dups:
            todos.append((4, f"Dedup {len(dups)} near-duplicate pair(s): {dups[:4]}"))
        stale = det.get("stale_ids", [])
        if stale:
            todos.append((5, f"Review {len(stale)} stale memory(ies): {stale[:6]}"))
        ent = entropy.get("entropy", 0)
        if ent and ent > 0.3:
            todos.append((2, f"Entropy elevated ({ent}) — schedule a deep-clean pass"))
    if verify:
        unver = verify.get("unverifiable", [])
        if unver:
            todos.append((3, f"Re-capture/confirm {len(unver)} memory(ies) whose sources "
                             f"don't trace: {unver[:6]}"))
    if decay:
        uc = decay.get("actions", {}).get("upgrade_candidates", [])
        if uc:
            todos.append((3, f"Promote {len(uc)} reinforced memory(ies): "
                             f"{[c['id'] for c in uc][:6]}"))
        demote = decay.get("actions", {}).get("demote", [])
        if demote:
            todos.append((6, f"{len(demote)} L1 memory(ies) gone cold — demotion pending"))
    todos.sort(key=lambda t: t[0])
    return todos


def survey(company, today):
    mem = str(Path(company) / "memory")
    policy = str(Path(company) / "org" / "policy.md")
    timed_out = []
    entropy = _run_json("entropy.py", "--memory-dir", mem, "--config", policy,
                        "--now", today, timed_out=timed_out)
    decay = _run_json("decay.py", "--memory-dir", mem, "--config", policy,
                      "--now", today, timed_out=timed_out)
    verify = _run_json("verify_memory.py", "--memory-dir", mem,
                       "--transcripts-dir", os.path.expanduser("~/.claude/projects"),
                       "--now", today, timed_out=timed_out)
    tiers = tier_counts(mem)
    todos = build_todos(entropy, decay, verify)

    lines = [f"# Elon — Daily Survey & TODO ({today})", ""]
    ent = entropy.get("entropy") if entropy else "?"
    dims = entropy.get("dimensions", {}) if entropy else {}
    lines.append(f"**Health:** entropy {ent} | memory L0={tiers['L0']} L1={tiers['L1']} "
                 f"L2={tiers['L2']} | unverified_rate {dims.get('unverified_rate', '?')}")
    lines.append("")
    lines.append("## TODO (priority order)")
    if todos:
        for i, (_, text) in enumerate(todos, 1):
            lines.append(f"{i}. {text}")
    else:
        lines.append("1. Company healthy — no action needed. Keep capturing.")
    if timed_out:
        lines.append("")
        lines.append(f"> ⚠ core-step TIMEOUT during survey: {', '.join(timed_out)} "
                     f"did not finish within the budget — this survey's numbers "
                     f"may be partial (a hung core step, not a healthy quiet day).")
    lines.append("")
    lines.append("_Generated by Elon's daily survey (read-only; deterministic)._")
    return "\n".join(lines) + "\n", {"entropy": ent, "tiers": tiers,
                                     "todos": len(todos), "timed_out": timed_out}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default=".company")
    ap.add_argument("--now", default=None)
    args = ap.parse_args(argv)
    today = args.now or date.today().isoformat()
    company = args.company
    if not Path(company).exists():
        print(json.dumps({"error": "no .company", "todos": 0}))
        return 0
    text, summary = survey(company, today)
    plans = Path(company) / "ops" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    out = plans / f"todo-{today}.md"
    out.write_text(text, encoding="utf-8")
    summary["written"] = str(out)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
