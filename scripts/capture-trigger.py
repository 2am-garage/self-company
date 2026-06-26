#!/usr/bin/env python3
"""
capture-trigger — CAPTURE stage entrypoint for the self-company memory pipeline.

Invoked by a Claude Code **Stop hook** at the end of each conversation. Reads the
session transcript, asks a cheap model (Haiku) to extract observations about the
Chairman, and writes them as L0 draft memories with sources (per
references/pipeline.md stage [1] CAPTURE and policy.md §4.2 frontmatter).

Design constraints honoured here:
- **No recursion.** Claude Code sets `stop_hook_active: true` when a Stop hook is
  already running; we exit immediately in that case. We also set a guard env var
  for the headless model call as a second layer.
- **Graceful degradation.** If `.company/` is missing, the transcript is
  unreadable, the `claude` CLI is absent, or the model returns nothing parseable,
  we exit 0 without writing — CAPTURE never crashes a session.
- **Real-time, not budget-gated.** Per policy.md §3.2 / triggers.md §1, real-time
  CAPTURE runs even at the daily ceiling, so there is no token-breaker gate here.
- **Pure stdlib** (json, os, re, subprocess, datetime, pathlib).

Hook input (stdin JSON, Claude Code Stop hook):
  { "session_id": "...", "transcript_path": "/abs/....jsonl",
    "cwd": "...", "stop_hook_active": false }

Test/manual usage (bypass stdin):
  capture-trigger.py --transcript PATH --session ID [--company DIR] [--dry-run]
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

RECURSION_GUARD = "SELF_COMPANY_CAPTURE_ACTIVE"
DEFAULT_MODEL = os.environ.get("SELF_COMPANY_CAPTURE_MODEL", "claude-haiku-4-5-20251001")
MAX_CHAIRMAN_CHARS = 24000   # cap transcript size fed to the model
MAX_OBSERVATIONS = 12        # cap L0 drafts written per session


# ----------------------------------------------------------------------------
# Transcript reading (deterministic, unit-tested)
# ----------------------------------------------------------------------------

def extract_chairman_lines(transcript_path):
    """
    Return [(line_index, text)] of Chairman (user) utterances from a Claude Code
    transcript .jsonl. Only plain-string user content is the Chairman typing;
    list content is tool_result noise and is skipped. Never raises.
    """
    out = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for i, ln in enumerate(f):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if d.get("type") != "user":
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str):
                    text = content.strip()
                    # skip command/system echoes and empties
                    if text and not text.startswith("<"):
                        out.append((i, text))
    except (OSError, IOError):
        return []
    return out


def build_capture_prompt(chairman_lines, existing_ids):
    """Construct the Haiku CAPTURE instruction. Pure string, easy to test."""
    convo, total = [], 0
    for idx, text in chairman_lines:
        chunk = f"[#{idx}] {text}"
        total += len(chunk)
        if total > MAX_CHAIRMAN_CHARS:
            break
        convo.append(chunk)
    convo_text = "\n".join(convo)
    existing = ", ".join(sorted(existing_ids)) if existing_ids else "(none)"
    return (
        "You are the CAPTURE stage of a personal-memory pipeline. From the "
        "Chairman's messages below, extract durable observations about the "
        "Chairman: preferences, habits, identity/background, ongoing projects, "
        "decisions, working style. Capture cheaply but each observation MUST cite "
        "the message index it came from.\n\n"
        f"Existing memory ids (do not duplicate): {existing}\n\n"
        "Return ONLY a JSON array (no prose), each item:\n"
        '  {"id": "kebab-slug", "body": "1-2 sentence observation", '
        '"source_lines": [<int message index>, ...]}\n'
        f"Return at most {MAX_OBSERVATIONS} items. If nothing durable, return [].\n\n"
        "=== Chairman messages ===\n"
        f"{convo_text}\n"
    )


# ----------------------------------------------------------------------------
# Model call (guarded, degrades to [])
# ----------------------------------------------------------------------------

def run_capture_model(prompt, model=DEFAULT_MODEL, timeout=120):
    """
    Run the headless `claude` CLI to perform extraction. Returns a list of
    observation dicts, or [] on any failure. Sets the recursion guard env so the
    child's own Stop hook (if any) no-ops.
    """
    if not _which("claude"):
        return []
    env = dict(os.environ)
    env[RECURSION_GUARD] = "1"
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    return _parse_observations(proc.stdout)


def _parse_observations(text):
    """Extract the first JSON array of observations from model output. [] on fail."""
    if not text:
        return []
    # find the first '[' ... matching ']' span and try to parse
    start = text.find("[")
    if start == -1:
        return []
    for end in range(len(text), start, -1):
        if text[end - 1] != "]":
            continue
        try:
            data = json.loads(text[start:end])
        except ValueError:
            continue
        if isinstance(data, list):
            return [o for o in data if isinstance(o, dict) and o.get("id") and o.get("body")]
    return []


def _which(name):
    from shutil import which
    return which(name) is not None


# ----------------------------------------------------------------------------
# L0 writing (deterministic, unit-tested)
# ----------------------------------------------------------------------------

def _slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")
    return s or "obs"


def existing_memory_ids(company_dir):
    ids = set()
    mem = Path(company_dir) / "memory"
    if not mem.exists():
        return ids
    for p in mem.rglob("*.md"):
        m = re.search(r"^id:\s*(.+)$", p.read_text(encoding="utf-8"), re.MULTILINE)
        if m:
            ids.add(m.group(1).strip())
    return ids


def write_l0(observations, session_id, company_dir, today=None):
    """
    Write each observation as an L0 draft. Skips entries without source_lines
    (sources cannot be empty — VERIFY iron rule). Returns list of written ids.
    Does not overwrite an existing id (appends nothing; CAPTURE is additive).
    """
    today = today or date.today().isoformat()
    l0 = Path(company_dir) / "memory" / "L0-working"
    l0.mkdir(parents=True, exist_ok=True)
    written = []
    for obs in observations[:MAX_OBSERVATIONS]:
        srcs = obs.get("source_lines") or []
        if not srcs:
            continue  # no provenance -> never write (Gibby would reject)
        oid = _slug(obs["id"])
        path = l0 / f"{oid}.md"
        if path.exists():
            continue
        sources = "[" + ", ".join(
            f'"[{session_id}#{int(s)}]"' for s in srcs if str(s).lstrip("-").isdigit()
        ) + "]"
        if sources == "[]":
            continue
        body = str(obs["body"]).strip().replace("\n", " ")
        path.write_text(
            "---\n"
            f"id: {oid}\n"
            "tier: L0\n"
            "owner: Tony\n"
            f"sources: {sources}\n"
            f"created: {today}\n"
            f"last_reinforced: {today}\n"
            "reinforce_count: 1\n"
            "decay_score: 1.0\n"
            "status: active\n"
            "---\n"
            f"{body}\n",
            encoding="utf-8",
        )
        written.append(oid)
    return written


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _read_hook_stdin():
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        return json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}


def main(argv=None):
    # Second-layer recursion guard (the headless model call sets this).
    if os.environ.get(RECURSION_GUARD):
        return 0

    ap = argparse.ArgumentParser(description="CAPTURE stage: transcript -> L0 drafts.")
    ap.add_argument("--transcript", help="Path to session transcript .jsonl")
    ap.add_argument("--session", help="Session id (for sources)")
    ap.add_argument("--company", default=".company", help="Company dir (default: .company)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                    help="Extract + build prompt but do NOT call the model or write files")
    args = ap.parse_args(argv)

    # Only consult the hook payload on stdin in hook mode (no explicit
    # --transcript). In manual/test mode we never touch stdin — reading it there
    # can block forever on a non-tty pipe.
    hook = {} if args.transcript else _read_hook_stdin()
    # Official anti-recursion: Claude Code sets this when a Stop hook is active.
    if hook.get("stop_hook_active"):
        return 0

    transcript = args.transcript or hook.get("transcript_path")
    session = args.session or hook.get("session_id") or "unknown-session"
    company = args.company

    if not transcript or not Path(transcript).exists():
        return 0  # nothing to capture; never error
    if not Path(company).exists():
        return 0  # company not installed here; no-op

    chairman = extract_chairman_lines(transcript)
    if not chairman:
        return 0

    existing = existing_memory_ids(company)
    prompt = build_capture_prompt(chairman, existing)

    if args.dry_run:
        print(json.dumps({
            "session": session, "chairman_lines": len(chairman),
            "existing_ids": len(existing), "prompt_chars": len(prompt),
            "would_call_model": _which("claude"),
        }, ensure_ascii=False))
        return 0

    observations = run_capture_model(prompt, model=args.model)
    written = write_l0(observations, session, company)

    if written:
        log = Path(company) / "ops" / "logs" / f"daily-{date.today().isoformat()}.md"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n## CAPTURE ({session})\n")
            for oid in written:
                f.write(f"- {oid} (L0) — pending_verify\n")
    print(json.dumps({"session": session, "written": written}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
