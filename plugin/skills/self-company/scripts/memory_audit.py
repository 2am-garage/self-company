#!/usr/bin/env python3
"""
memory_audit.py — Append-only JSONL audit log for memory mutations.

One os.write() per event to `.company/ops/logs/memory-audit-<date>.jsonl`.
Stdlib-only. Best-effort: audit failures never block/alter the mutation.

Event schema (schema: 1):
  {
    "ts": ISO-8601 timestamp,
    "id": memory id,
    "op": "drop"|"demote"|"promote"|"archive"|"absorb"|"reinforce"|"forget",
    "field": key name for field changes (e.g. "status", "tier", "reinforce_count"),
    "from": previous value (string),
    "to": new value (string),
    "source": "decay"|"reinforce_memory"|"forget_memory" (the caller),
    "schema": 1
  }

`op: "forget"` / `source: "forget_memory"` (Mike 2026-07-20 Finding 1) is the
Chairman-driven HARD FORGET path: forget_memory.py tombstones an EXPLICIT
memory id immediately — including overriding L2's normal never-decay rule —
and logs it here exactly like decay's own "drop"/"archive" events (same
`field="status"`, `from=<old status>`, `to="archived"`). See
forget_memory.py's module docstring and references/memory-tiers.md §9 for the
full division-of-labor contract (this never physically deletes the file).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def audit_event(company: str, op: str, memory_id: str, field: str,
                from_val: str, to_val: str, source: str) -> bool:
    """
    Append one audit event to the memory-audit log for today.

    Returns True if successful, False on error. Failures never raise.
    """
    if not memory_id or not op or not source:
        return False

    try:
        now_utc = datetime.now(timezone.utc)
        now = now_utc.isoformat().replace("+00:00", "Z")
        today = now_utc.strftime("%Y-%m-%d")
        logdir = Path(company) / "ops" / "logs"
        logdir.mkdir(parents=True, exist_ok=True)

        path = logdir / f"memory-audit-{today}.jsonl"
        event = {
            "ts": now,
            "id": memory_id,
            "op": op,
            "field": field,
            "from": str(from_val) if from_val is not None else None,
            "to": str(to_val) if to_val is not None else None,
            "source": source,
            "schema": 1
        }

        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
            return True
        finally:
            os.close(fd)
    except OSError:
        return False


def read_events(logdir: Path, date_str: Optional[str] = None) -> list:
    """
    Read audit events from memory-audit-*.jsonl files.

    If date_str is given (YYYY-MM-DD), read only that day.
    Otherwise read all memory-audit-*.jsonl files in the logdir.
    Returns list of event dicts.
    """
    events = []
    try:
        if date_str:
            path = logdir / f"memory-audit-{date_str}.jsonl"
            files = [path] if path.exists() else []
        else:
            files = sorted(logdir.glob("memory-audit-*.jsonl"))

        for path in files:
            try:
                with open(path, encoding="utf-8") as f:
                    for raw in f:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            obj = json.loads(raw)
                            if isinstance(obj, dict):
                                events.append(obj)
                        except ValueError:
                            continue
            except OSError:
                continue
    except OSError:
        pass

    return events
