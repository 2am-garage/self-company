#!/usr/bin/env python3
"""
elon_survey — Elon's daily CEO survey + prioritized TODO list.

So the CEO is load-bearing every day (not just when addressed): this surveys the
company's real state (entropy, decay/promotion candidates, verification gaps, tier
distribution) and writes a PRIORITIZED todo list to ops/plans/todo-<date>.md. It
is deterministic and read-only over memory.

Phase 28 Item 1 (Tony C1): two clean input modes, no hard coupling between them.

  * FED mode (`--entropy-json/--decay-json/--verify-json` + `--no-recompute`,
    daily-run's path): the core (daily-run.sh's reinforce/decay/verify/entropy
    pass) already computed these three JSONs THIS tick, minutes earlier, into
    its own temp files. Reading them directly means entropy/decay/verify are
    invoked ZERO times by the survey — the structural removal of the same-tick
    recompute Phase 27 only bounded/surfaced (see `_run_json`'s docstring
    below). A missing/gated/timed-out step's JSON is empty/invalid -> treated
    as `None` (`build_todos`/rendering already tolerate `None` per input) —
    fed mode NEVER falls back to respawning the core inside a tick, or the
    recompute sneaks back in through the "gated" path.
  * STANDALONE mode (no flags — a manual `elon_survey.py --company X` with no
    core JSON available): byte-identical to today, including the
    `SELF_COMPANY_CORE_STEP_TIMEOUT` bound and the `timed_out` surfacing. The
    survey stays a usable standalone tool, not a daily-run appendage — this is
    exactly when a wedged corpus needs the timeout machinery.

HONESTY NOTE: fed mode reports THIS tick's actual core outputs (what daily-run
just computed and logged); today's always-on recompute reported a POST-APPLY
re-derivation minutes later, against a corpus reinforce/decay may have already
mutated, using a subprocess that could resolve a different interpreter than the
core's venv pass — the two could disagree on the same tick. Where they diverge,
fed mode is the MORE truthful one (it reports what actually happened this tick,
not a re-guess) — acceptance is pinned against the fed inputs being a pure
function, not byte-parity with the old double-run.

`build_todos()` / the rendered `.md` are pure functions of the three input
dicts regardless of where they came from — this is an INPUT swap, not a logic
change; the todo rules themselves are untouched.

Usage: elon_survey.py [--company DIR] [--now YYYY-MM-DD]
       elon_survey.py --company DIR --no-recompute \\
           --entropy-json EOUT --decay-json DOUT --verify-json VOUT
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

# Phase 27 MUST-FIX 3 (STANDALONE mode only, Phase 28): elon_survey re-invokes
# the deterministic core scripts (entropy/decay/verify) as read-only
# subprocesses when no fed JSON is available (a manual run against a corpus
# daily-run hasn't just processed). That re-invocation was bounded by a
# HARDCODED 120s, independent of daily-run.sh's core-step budget, AND a
# timeout was silently swallowed (returned None → "0 todos", no log/JSONL
# line) — a hung decay stretched one tick to 2m6s absorbed as healthy. Route
# the budget through the SAME env daily-run.sh uses, and make a timeout
# VISIBLE (recorded in `timed_out`, surfaced by daily-run.sh). Phase 28 Item 1
# (Tony C1) is the structural removal for the DAILY-RUN path: fed mode below
# never calls this at all.
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


def _load_fed_json(path):
    """Phase 28 Item 1: load ONE core-step JSON the caller already computed
    this tick (daily-run's $EOUT/$DOUT/$VOUT). Missing path, unreadable file,
    or invalid JSON -> `None` — the SAME "step didn't produce usable output"
    signal `_run_json` returns on a timeout/crash, which `build_todos` and the
    rendering below already tolerate per-input. NEVER raises, and NEVER falls
    back to recomputing — that fallback is exactly the recompute this item
    removes; a gated/aborted/timed-out step degrades to `None`, not a spawn."""
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


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


def survey(company, today, entropy_json=None, decay_json=None, verify_json=None,
           no_recompute=False):
    """`entropy_json`/`decay_json`/`verify_json` are PATHS to the core's
    already-computed JSON (daily-run's $EOUT/$DOUT/$VOUT). Phase 28 Item 1:

      * `no_recompute=True` (FED mode, daily-run's path): load each provided
        path directly (`_load_fed_json` -> `None` on missing/invalid) — ZERO
        subprocess, regardless of whether a path loaded successfully. This is
        the hard requirement (Elon's note): fed mode must NEVER fall back to
        respawning the core, or the recompute sneaks back in through the
        gated/timed-out path.
      * `no_recompute=False` (STANDALONE mode, today's default): re-invoke the
        core as read-only subprocesses exactly as before, bounded by
        `SELF_COMPANY_CORE_STEP_TIMEOUT` with `timed_out` surfaced — a manual
        `elon_survey.py --company X` with no fed JSON stays a fully usable
        standalone tool.
    """
    mem = str(Path(company) / "memory")
    policy = str(Path(company) / "org" / "policy.md")
    timed_out = []
    if no_recompute:
        entropy = _load_fed_json(entropy_json)
        decay = _load_fed_json(decay_json)
        verify = _load_fed_json(verify_json)
    else:
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
    # Phase 28 Item 1 (Tony C1): fed-mode input contract. Additive — every
    # existing standalone call site (no flags) is byte-identical to today.
    ap.add_argument("--entropy-json", default=None)
    ap.add_argument("--decay-json", default=None)
    ap.add_argument("--verify-json", default=None)
    ap.add_argument("--no-recompute", action="store_true")
    args = ap.parse_args(argv)
    today = args.now or date.today().isoformat()
    company = args.company
    if not Path(company).exists():
        print(json.dumps({"error": "no .company", "todos": 0}))
        return 0
    text, summary = survey(company, today,
                           entropy_json=args.entropy_json,
                           decay_json=args.decay_json,
                           verify_json=args.verify_json,
                           no_recompute=args.no_recompute)
    plans = Path(company) / "ops" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    out = plans / f"todo-{today}.md"
    out.write_text(text, encoding="utf-8")
    summary["written"] = str(out)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
