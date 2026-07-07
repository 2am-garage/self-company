#!/usr/bin/env python3
"""
list_uncategorized — deterministic helper for the Phase-5 Item-4 one-time
category backfill (N5: 97/130 active L0s carry no `category:` frontmatter, so
their promotion has no routing signal into L2-cold/<category>/).

This script only LISTS the uncategorized files. Assigning a category requires
judgment over the body text, so the backfill itself is an AGENT task — this
script makes no model call and mutates nothing.

DISPATCH BRIEF (for Tom / the daily agent — run the categorization like this):
  1. Measure:  python3 scripts/list_uncategorized.py --memory-dir .company/memory
  2. For each listed file, read the body and insert exactly ONE frontmatter
     line after the `owner:` line:
         category: profile|projects|preferences
     using the CAPTURE category contract (policy.md §4.2):
       - profile     — WHO the Chairman is (role, background, stack, setup)
       - projects    — WHAT he is building (work, goals, deadlines, constraints)
       - preferences — HOW he likes to work / be served (habits, likes/dislikes)
     Change NOTHING else in the file (id/tier/sources/rc/status stay as-is).
  3. Work in batches (~20 files per run, respect the time budget); re-run
     step 1 to measure what remains.
  Acceptance: uncategorized count ~= 0 across active memories.

Usage:
  list_uncategorized.py [--memory-dir .company/memory] [--include-archived]

Output: JSON {count, total_active, files: [{id, tier, path}]}. Pure stdlib,
read-only.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Phase 6 Item 1: shared tombstone vocabulary (archived/defunct/absorbed). The
# sibling modules live in THIS directory; put it on sys.path FIRST so the hard
# imports below resolve under every entry point (direct run, cron, test harness).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tombstone import is_tombstoned

# Phase 11: the fragile frontmatter PARSING SEAM is the ONE shared module
# (frontmatter.py). This tool keeps only the keys it needs off the top of the
# shared full-parse (id/tier/status/category).
from frontmatter import parse as _fm_parse

# Valid categories (kept in sync with decay.py::L2_CATEGORIES and
# capture-trigger.py::CATEGORIES).
CATEGORIES = ("preferences", "profile", "projects")


def _frontmatter(text):
    """Frontmatter dict via the shared parser; None if no frontmatter block."""
    fm, _body = _fm_parse(text)
    return fm or None


def scan(memory_dir, include_archived=False):
    files, total = [], 0
    root = Path(memory_dir)
    for p in sorted(root.rglob("*.md")):
        try:
            fm = _frontmatter(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not fm or not fm.get("id"):
            continue
        if not include_archived and is_tombstoned(fm):
            continue  # tombstones don't need routing; skip unless asked
        total += 1
        if fm.get("category") not in CATEGORIES:
            files.append({"id": fm["id"], "tier": fm.get("tier", "?"),
                          "path": str(p)})
    return {"count": len(files), "total_active": total, "files": files}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="List active memories missing a category: frontmatter "
                    "field (read-only; the backfill itself is an agent task — "
                    "see the dispatch brief in this script's docstring).")
    ap.add_argument("--memory-dir", default=".company/memory")
    ap.add_argument("--include-archived", action="store_true",
                    help="also list archived/tombstoned files")
    args = ap.parse_args(argv)
    print(json.dumps(scan(args.memory_dir, args.include_archived),
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
