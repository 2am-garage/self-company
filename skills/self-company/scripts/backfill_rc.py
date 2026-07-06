#!/usr/bin/env python3
"""
backfill_rc — one-time Phase-5 Item-1 backfill: recompute reinforce_count for
records whose rc exceeds their distinct-session-id count.

Why: capture-trigger's pre-Phase-5 reinforce path bumped rc on EVERY
restatement within a session (N1) — live records reached rc=5 with only 2
source tokens in one day. rc is the cross-session recurrence signal the
promotion gates (rc>=2 -> L1, rc>=4 -> L2) trust, so inflated records race
toward permanent tiers. This backfill clamps rc down to the number of DISTINCT
session ids in `sources` (never raises rc, never touches records already at or
below their distinct-session count).

Distinct-session counting matches reinforce_memory._session_ids: a token
"[<session-id>#<line>]" contributes the id before the first '#'; any other
token shape (e.g. "charter:foo") counts as one distinct id (the whole token).

Safety:
- Dry-run by DEFAULT; --apply to mutate. Only the `reinforce_count:` line is
  rewritten (surgical line edit — no full re-serialization, nothing else in
  the file changes).
- Never writes rc below 1.
- Blessed charter seeds are reported but NEVER mutated (charter guard parity).

Usage:
  backfill_rc.py [--memory-dir .company/memory] [--apply]

Output: JSON — per-record before/after plus a summary. Pure stdlib.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Same best-effort import pattern as decay.py: the guard must never be
# disabled by a missing sibling module.
try:
    from charter_ids import is_blessed_charter
except Exception:  # pragma: no cover - defensive
    def is_blessed_charter(fm):
        return False

# Phase 11: the fragile frontmatter PARSING SEAM + source tokenizer live in ONE
# shared module (frontmatter.py). Best-effort import + verbatim fallback, same
# pattern as the charter import above. The `.strip()=='---'` delimiter and the
# `SOURCE_ITEM_RE` extractor are now the single source; backfill keeps its OWN
# closing-fence-index recovery (for the surgical rc-line edit) and clamp logic
# layered on top.
try:
    from frontmatter import parse as _fm_parse, SOURCE_ITEM_RE, tokenize_sources
except Exception:  # pragma: no cover - verbatim fallback (authoritative: frontmatter.py)
    SOURCE_ITEM_RE = re.compile(r'"[^"]*"')

    def tokenize_sources(raw):
        return SOURCE_ITEM_RE.findall(raw or "")

    def _fm_parse(text):
        lines = text.split('\n')
        if lines[0].strip() != '---':
            return {}, text
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                end = i
                break
        if end is None:
            return {}, text
        fm = {}
        for line in lines[1:end]:
            s = line.strip()
            if not s or s.startswith('#') or ':' not in s:
                continue
            k, v = s.split(':', 1)
            fm[k.strip()] = v.strip()
        return fm, '\n'.join(lines[end + 1:])

RC_LINE_RE = re.compile(r"^reinforce_count:\s*(\d+)\s*$")


def parse_frontmatter(text):
    """Frontmatter parse -> ({key: raw-value}, closing-line index).

    The fragile delimiter + key:value split is the shared Phase-11 parser
    (`frontmatter.parse`). The closing-fence line INDEX (used by `scan` for the
    surgical `reinforce_count:` line rewrite) is not part of the shared parse
    contract, so it is recovered here from the same `.strip()=='---'` scan. A
    non-empty `fm` guarantees a terminated block, so the closing fence exists.
    """
    fm, _body = _fm_parse(text)
    if not fm:
        return None, -1
    lines = text.split("\n")
    close = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    return fm, close


def distinct_sessions(sources_value):
    """Count distinct session ids in a raw `sources:` value string."""
    out = set()
    for it in tokenize_sources(sources_value):
        inner = it.strip('"')
        if inner.startswith("[") and inner.endswith("]") and "#" in inner:
            out.add(inner[1:].split("#", 1)[0])
        else:
            out.add(inner)
    return len(out)


def scan(memory_dir, apply=False):
    report = {"memory_dir": str(memory_dir), "applied": apply,
              "scanned": 0, "corrected": [], "skipped_charter": [],
              "warnings": []}
    root = Path(memory_dir)
    if not root.exists():
        report["warnings"].append(f"memory dir not found: {memory_dir}")
        return report
    for p in sorted(root.rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            report["warnings"].append(f"{p}: unreadable ({e})")
            continue
        fm, close = parse_frontmatter(text)
        if not fm or not fm.get("id"):
            continue
        report["scanned"] += 1
        try:
            rc = int(fm.get("reinforce_count", "1"))
        except ValueError:
            report["warnings"].append(f"{p}: non-int reinforce_count — skipped")
            continue
        n_sessions = distinct_sessions(fm.get("sources", ""))
        target = max(1, n_sessions)
        if rc <= target:
            continue  # never raise rc; at-or-below distinct count is fine
        entry = {"id": fm["id"], "path": str(p), "rc_before": rc,
                 "rc_after": target, "distinct_sessions": n_sessions}
        # Charter guard parity: report, never mutate a blessed seed.
        # (is_blessed_charter accepts the raw `sources:` string form.)
        if is_blessed_charter({"id": fm["id"],
                               "provenance": fm.get("provenance"),
                               "sources": fm.get("sources", "")}):
            report["skipped_charter"].append(entry)
            continue
        if apply:
            lines = text.split("\n")
            fixed = False
            for i in range(1, close):
                if RC_LINE_RE.match(lines[i].strip()):
                    lines[i] = f"reinforce_count: {target}"
                    fixed = True
                    break
            if not fixed:
                report["warnings"].append(
                    f"{p}: reinforce_count line not found in frontmatter")
                continue
            p.write_text("\n".join(lines), encoding="utf-8")
        report["corrected"].append(entry)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Backfill: clamp reinforce_count to distinct-session count "
                    "(dry-run by default).")
    ap.add_argument("--memory-dir", default=".company/memory")
    ap.add_argument("--apply", action="store_true",
                    help="mutate files (default: dry-run, report only)")
    args = ap.parse_args(argv)
    report = scan(Path(args.memory_dir), apply=args.apply)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
