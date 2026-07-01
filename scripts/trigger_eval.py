#!/usr/bin/env python3
"""
trigger_eval — measure whether a natural-language query makes the REAL installed
`self-company` skill trigger.

Why this exists
---------------
skill-creator's `run_eval` registers the skill DESCRIPTION as a throwaway
`.claude/commands/` proxy and asks a headless judge whether that proxy would
fire. In this headless-sonnet environment that proxy sits at a near-zero floor:
every description scored 0 -> 0, so it could not tell a good description from a
bad one. That 0 was not a measurement — it was a broken instrument.

This harness measures the thing we actually care about: does a query cause the
REAL, installed skill to fire? It spawns `claude -p <query>` with cwd set to the
repo whose `.claude/skills/self-company` makes the skill discoverable, streams
the JSON events, and detects the skill firing by watching for the actual tool
calls Claude makes:

  * a `Skill` tool_use whose `skill` input contains "self-company", OR
  * a `Read` (or similar) of a path containing "skills/self-company/SKILL.md".

The SessionStart hook injects self-company *context* into every session — that
is deliberately NOT counted. Only a tool_use counts, because only a tool_use
means Claude actively decided to load and run the skill.

CRITICAL guard against repeating the silent-floor mistake: `--self-test` runs a
slam-dunk positive-control query that MUST trigger. If it triggers 0 times, the
detector/harness is broken and the tool says so loudly rather than reporting a
confident 0.

Pure stdlib.
"""

import argparse
import json
import os
import select
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# The slam-dunk positive control. If this does not fire, detection is broken.
POSITIVE_CONTROL_QUERY = (
    "hey Elon and Phoebe, run a memory maintenance pass on our self-company, "
    "decay the stale memories and give me the entropy report"
)

SKILL_PATH_MARKER = "skills/self-company/SKILL.md"
SKILL_NAME_MARKER = "self-company"


# ============================================================ detection (pure)

class TriggerDetector:
    """Streaming, sticky detector for the self-company skill firing.

    Feed it parsed stream-json objects (dicts) one at a time via `.feed(obj)`.
    Returns True as soon as it has seen the skill fire; stays True thereafter.

    It understands two shapes of evidence, so it can fire EARLY (from partial
    streaming events) without waiting for the full assistant turn:

      1. A complete tool_use block on an `assistant` message.
      2. A streaming `content_block_start` (tool_use) plus the accumulated
         `input_json_delta` partial-JSON for that block.
    """

    def __init__(self):
        # index -> {"name": str, "buf": str} for in-flight streaming tool_use blocks
        self._blocks = {}
        self.triggered = False

    def feed(self, obj):
        if self.triggered:
            return True
        if not isinstance(obj, dict):
            return False
        t = obj.get("type")

        if t == "assistant":
            msg = obj.get("message", {}) or {}
            for block in (msg.get("content", []) or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                if tool_use_is_trigger(block.get("name"), block.get("input")):
                    self.triggered = True
                    return True

        elif t == "stream_event":
            ev = obj.get("event", {}) or {}
            et = ev.get("type")
            if et == "content_block_start":
                idx = ev.get("index")
                cb = ev.get("content_block", {}) or {}
                if cb.get("type") == "tool_use":
                    self._blocks[idx] = {"name": cb.get("name"), "buf": ""}
                    # a start event can already carry (partial) input
                    if tool_use_is_trigger(cb.get("name"), cb.get("input")):
                        self.triggered = True
                        return True
            elif et == "content_block_delta":
                idx = ev.get("index")
                delta = ev.get("delta", {}) or {}
                if delta.get("type") == "input_json_delta":
                    blk = self._blocks.get(idx)
                    if blk is not None:
                        blk["buf"] += delta.get("partial_json", "") or ""
                        if raw_input_is_trigger(blk["name"], blk["buf"]):
                            self.triggered = True
                            return True
            elif et == "content_block_stop":
                self._blocks.pop(ev.get("index"), None)

        return self.triggered


def tool_use_is_trigger(name, inp):
    """True if a fully-formed tool_use block means self-company fired."""
    if not name or not isinstance(inp, dict):
        return False
    if name == "Skill":
        skill = str(inp.get("skill", "") or inp.get("command", "") or "")
        return SKILL_NAME_MARKER in skill
    # Read / Grep / Glob / etc. touching the skill file
    path = str(
        inp.get("file_path", "")
        or inp.get("path", "")
        or inp.get("pattern", "")
        or ""
    )
    return SKILL_PATH_MARKER in path


def raw_input_is_trigger(name, raw_buf):
    """True if the accumulated raw partial-JSON for a tool_use is a trigger.

    Used for EARLY detection while the input is still streaming in as text.
    """
    if name == "Skill":
        return SKILL_NAME_MARKER in raw_buf
    # any other tool that names the skill file path is Claude reaching for it
    return SKILL_PATH_MARKER in raw_buf


# ============================================================ scoring (pure)

def score_query(query, should_trigger, run_results, threshold):
    """Score one query given its per-run boolean results.

    run_results: list[bool] (True = fired that run; timeouts count as False).
    Returns a dict with trigger_rate and pass.
    """
    runs = len(run_results)
    triggers = sum(1 for r in run_results if r)
    rate = (triggers / runs) if runs else 0.0
    fired = rate >= threshold
    passed = fired if should_trigger else (not fired)
    return {
        "query": query,
        "should_trigger": should_trigger,
        "runs": runs,
        "triggers": triggers,
        "trigger_rate": round(rate, 4),
        "fired": fired,
        "pass": passed,
    }


def summarize(query_scores):
    """Aggregate per-query scores into an overall summary with recall/precision.

    recall    = should-trigger queries that fired / total should-trigger
    precision = should-not   queries that stayed silent / total should-not
                (as specified: fraction of should-not that stayed silent)
    """
    total = len(query_scores)
    passed = sum(1 for q in query_scores if q["pass"])

    pos = [q for q in query_scores if q["should_trigger"]]
    neg = [q for q in query_scores if not q["should_trigger"]]

    pos_fired = sum(1 for q in pos if q["fired"])
    neg_silent = sum(1 for q in neg if not q["fired"])

    recall = (pos_fired / len(pos)) if pos else None
    precision = (neg_silent / len(neg)) if neg else None

    return {
        "total_queries": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "should_trigger_count": len(pos),
        "should_not_count": len(neg),
        "recall": round(recall, 4) if recall is not None else None,
        "precision": round(precision, 4) if precision is not None else None,
    }


# ============================================================ runner (impure)

def build_command(query, model):
    cmd = [
        "claude", "-p", query,
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def child_env():
    """Env for the child: drop CLAUDECODE so nesting/subprocess claude works."""
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    return env


def run_once(query, repo, model, timeout, verbose=False):
    """Spawn one `claude -p` and return True if self-company fired.

    Streams stdout line-by-line, feeds the detector, and terminates the child
    early the moment a trigger is detected. Timeouts count as NOT triggered.
    """
    cmd = build_command(query, model)
    detector = TriggerDetector()
    triggered = False
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=repo,
            env=child_env(),
            text=True,
            bufsize=1,
        )
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if verbose:
                    _log(f"  timeout after {timeout}s: {query[:50]!r}")
                break
            rlist, _, _ = select.select([proc.stdout], [], [], remaining)
            if not rlist:
                if verbose:
                    _log(f"  timeout after {timeout}s: {query[:50]!r}")
                break
            line = proc.stdout.readline()
            if line == "":
                break  # EOF: process finished
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate non-JSON / truncated lines
            if detector.feed(obj):
                triggered = True
                break
    except Exception as e:  # never let one bad run crash the sweep
        if verbose:
            _log(f"  run error ({type(e).__name__}): {e}")
    finally:
        if proc is not None:
            _terminate(proc)
    return triggered


def _terminate(proc):
    try:
        if proc.stdout:
            proc.stdout.close()
    except Exception:
        pass
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def evaluate(eval_set, repo, runs_per_query, model, num_workers, timeout,
             threshold, verbose=False):
    """Run the full sweep. Returns the report dict."""
    # Flatten into individual run-jobs so workers stay busy across queries.
    jobs = []  # (query_index, query, should_trigger)
    for qi, item in enumerate(eval_set):
        for _ in range(runs_per_query):
            jobs.append((qi, item["query"], bool(item["should_trigger"])))

    results = {qi: [] for qi in range(len(eval_set))}
    done = 0
    total = len(jobs)

    def worker(job):
        qi, query, _st = job
        return qi, run_once(query, repo, model, timeout, verbose)

    with ThreadPoolExecutor(max_workers=num_workers) as ex:
        futures = [ex.submit(worker, j) for j in jobs]
        for fut in as_completed(futures):
            qi, fired = fut.result()
            results[qi].append(fired)
            done += 1
            if verbose:
                _log(f"[{done}/{total}] query#{qi} "
                     f"{'FIRED' if fired else 'silent'}")

    query_scores = []
    for qi, item in enumerate(eval_set):
        query_scores.append(
            score_query(item["query"], bool(item["should_trigger"]),
                        results[qi], threshold))

    return {
        "config": {
            "repo": os.path.abspath(repo),
            "runs_per_query": runs_per_query,
            "model": model,
            "num_workers": num_workers,
            "timeout": timeout,
            "threshold": threshold,
        },
        "summary": summarize(query_scores),
        "queries": query_scores,
    }


# ============================================================ self-test

def self_test(repo, model, runs_per_query, timeout, num_workers, verbose):
    """Positive control: the slam-dunk query MUST fire at least once.

    Returns (ok, report). ok is False if triggers == 0 -> harness is BROKEN.
    """
    _log("=== SELF-TEST (positive control) ===")
    _log(f"query: {POSITIVE_CONTROL_QUERY!r}")
    _log(f"runs: {runs_per_query}")
    eval_set = [{"query": POSITIVE_CONTROL_QUERY, "should_trigger": True}]
    report = evaluate(eval_set, repo, runs_per_query, model, num_workers,
                      timeout, threshold=0.5, verbose=verbose)
    triggers = report["queries"][0]["triggers"]
    runs = report["queries"][0]["runs"]
    ok = triggers > 0
    report["self_test"] = {
        "query": POSITIVE_CONTROL_QUERY,
        "triggers": triggers,
        "runs": runs,
        "ok": ok,
        "verdict": (
            "HARNESS WORKS: positive control fired"
            if ok else
            "HARNESS BROKEN: positive control fired 0 times — detection is not "
            "working; do NOT trust any 0 from this tool"
        ),
    }
    _log(f"positive control: {triggers}/{runs} fired -> "
         f"{'OK' if ok else 'BROKEN'}")
    return ok, report


# ============================================================ CLI

def load_eval_set(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("eval-set must be a JSON list of "
                         "{query, should_trigger} objects")
    for item in data:
        if "query" not in item or "should_trigger" not in item:
            raise ValueError("each eval item needs 'query' and "
                             "'should_trigger'")
    return data


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Measure whether queries trigger the real self-company skill.")
    p.add_argument("--eval-set",
                   help="JSON file: list of {query, should_trigger}.")
    p.add_argument("--repo", default=os.getcwd(),
                   help="Repo dir whose .claude/skills/self-company makes the "
                        "skill available (default: cwd).")
    p.add_argument("--runs-per-query", type=int, default=3)
    p.add_argument("--model", default=None,
                   help="e.g. claude-sonnet-4-6 (optional).")
    p.add_argument("--num-workers", type=int, default=8)
    p.add_argument("--timeout", type=float, default=45.0,
                   help="Per-run wall-clock seconds (default 45).")
    p.add_argument("--threshold", type=float, default=0.5,
                   help="trigger_rate >= threshold counts as 'fired' "
                        "(default 0.5).")
    p.add_argument("--self-test", action="store_true",
                   help="Run only the positive-control query and verify it "
                        "fires > 0 times.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.self_test:
        ok, report = self_test(args.repo, args.model, args.runs_per_query,
                               args.timeout, args.num_workers, args.verbose)
        print(json.dumps(report, indent=2))
        return 0 if ok else 2

    if not args.eval_set:
        p.error("--eval-set is required unless --self-test is used")

    eval_set = load_eval_set(args.eval_set)
    report = evaluate(eval_set, args.repo, args.runs_per_query, args.model,
                      args.num_workers, args.timeout, args.threshold,
                      args.verbose)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
