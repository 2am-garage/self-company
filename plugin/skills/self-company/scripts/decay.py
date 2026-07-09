#!/usr/bin/env python3
"""
Decay — Memory lifecycle management for self-company.

Scans memory/ for markdown frontmatter, computes decay_score using exponential decay,
classifies records as keep/drop/archive/demote/upgrade-candidate, and optionally applies
actions (--dry-run by default, --apply to mutate files).

Pure stdlib only (argparse, os, pathlib, re, json, datetime, sys).
Frontmatter parsed manually (no PyYAML).

Output: JSON summary with scanned count, actions taken, and warnings.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

# Bucket 2 (Phase 14): the shared sibling modules (policy_config, charter_ids,
# tombstone, frontmatter) live in THIS directory. Put it on sys.path FIRST so the
# hard imports below resolve under every entry point — direct run, cron, venv
# re-exec, a hook, or an import by another module / the test harness (mirrors
# schedule_validator.py). They always ship together, so the imports never fail.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Shared policy loader — single source of truth for tunable constants
# (reads org/policy.md §7). Best-effort import: if the module is somehow
# missing, fall back to built-in defaults rather than crashing the batch.
try:
    from policy_config import resolve as _resolve_config
except Exception:  # pragma: no cover - defensive
    _resolve_config = None

# Blessed charter seed set — the SINGLE source is charter_ids.py (same dir). The
# charter guard protects the 8 axioms from decay's reap-unlink, so it must be
# exact: a hard import, never a drift-prone inline copy. The sibling always ships
# beside this file and is on sys.path (above), so the import cannot fail.
from charter_ids import CHARTER_SEED_IDS, is_blessed_charter


# Phase 6 Item 1: tombstone vocabulary (archived / defunct / absorbed) is the ONE
# shared set in tombstone.py (same dir). decay treats `absorbed` exactly like
# `archived`: out of the active lifecycle (keep-short-circuit) AND reapable past
# the grace window.
from tombstone import TOMBSTONE_STATUSES, is_tombstoned


# Phase 11: the fragile frontmatter PARSING SEAM (delimiter + key:value split +
# body split + source tokenization) is the ONE shared module frontmatter.py (same
# dir), so the legacy per-scanner parsers can't drift again — the `.strip()=='---'`
# delimiter is the single source. The module does PARSE/SPLIT/SERIALIZE/TOKENIZE
# ONLY; decay keeps its OWN 13-key defaults, tier/status/category validation,
# defunct->archived normalization, and serialize key order layered on top.
from frontmatter import (split as _fm_split, parse as _fm_parse,
                         serialize as _fm_serialize,
                         SOURCE_ITEM_RE, tokenize_sources,
                         _atomic_write)


# ============================================================================
# BUILT-IN DEFAULTS (== manifest §1, tunable via --config / policy.md)
# ============================================================================

DEFAULT_HL_BASE = 7.0
DEFAULT_HL_GROWTH = 0.5
DEFAULT_L0_DROP_THRESHOLD = 0.25
DEFAULT_L1_ARCHIVE_THRESHOLD = 0.15
DEFAULT_L1_DEMOTE_RC = 2
DEFAULT_L0_TO_L1_RC = 2
DEFAULT_L1_TO_L2_RC = 4
# Reap grace window: an archived/defunct file untouched for this many days
# (since the LATER of last_reinforced / invalid_at — a tombstone stays
# recoverable for the full grace window from the moment it was invalidated)
# is physically dropped in the --apply pass.
DEFAULT_REAP_GRACE_DAYS = 7
# Phase 5 Item 2 (N2) offline-gap damper: if now − last successful --apply run
# (marker ops/.last-decay-run) exceeds this many days, cap every file's
# effective elapsed age at marker + threshold and defer physical reaps this
# run — one missed fortnight must not purge the store in a single tick.
DEFAULT_OFFLINE_GAP_DAYS = 7
# Marker filename recording the date of the last successful --apply run.
LAST_RUN_MARKER = ".last-decay-run"

# Tier directory layout (siblings under memory_dir).
TIER_DIRS = {"L0": "L0-working", "L1": "L1-warm", "L2": "L2-cold"}
# L2 category subdirs; a promoted file lands in one of these under L2-cold/.
L2_CATEGORIES = ("preferences", "profile", "projects")
# Default L2 category when a promoted file doesn't already live under one.
DEFAULT_L2_CATEGORY = "preferences"


# ============================================================================
# FRONTMATTER PARSING (no PyYAML, pure string ops)
# ============================================================================

def parse_frontmatter(content: str) -> Dict[str, Any]:
    """
    Parse YAML-like frontmatter from markdown.

    Format:
        ---
        key: value
        sources: [a, b, c]
        ---
        body...

    Returns dict with parsed fields. Missing/malformed fields get safe defaults.
    """
    result = {
        "id": None,
        "tier": None,
        "category": None,
        "owner": None,
        "provenance": None,
        "sources": [],
        "created": None,
        "last_reinforced": None,
        "reinforce_count": None,
        "decay_score": None,
        "status": None,
        "invalid_at": None,
        "verified_date": None,
        "verified_by": None,
        "_body": ""
    }

    # Find frontmatter block via the shared parser (delimiter `.strip()=='---'`).
    # `_fm_split` returns `([], text)` for BOTH a missing opening fence and a
    # missing closing fence — decay's original early-returns (defaults + empty
    # body) collapse to a single "no frontmatter block" bail-out; both cases
    # simply return the defaults, with no behavioral difference.
    fm_lines, body = _fm_split(content)
    if not fm_lines:
        return result

    # Parse frontmatter lines (validation/interpretation layer kept verbatim).
    for line in fm_lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        if ':' not in line:
            continue

        key, val_str = line.split(':', 1)
        key = key.strip()
        val_str = val_str.strip()

        try:
            if key == "id":
                result["id"] = val_str if val_str else None
            elif key == "tier":
                if val_str in ("L0", "L1", "L2"):
                    result["tier"] = val_str
            elif key == "category":
                # CAPTURE tags each L0 memory with category ∈ L2_CATEGORIES so
                # profile/project memories promote into the right L2-cold subdir.
                # Unknown/missing -> None (promote falls back to path-carry/default).
                if val_str in L2_CATEGORIES:
                    result["category"] = val_str
            elif key == "owner":
                result["owner"] = val_str if val_str else None
            elif key == "provenance":
                # Charter-class marker (`provenance: charter`). Parsed AND
                # round-tripped so rewrites (keep/promote/demote/archive) never
                # strip the charter marker the blessed-seed guard depends on.
                result["provenance"] = val_str if val_str else None
            elif key == "status":
                # `defunct` is a legacy alias for `archived` (the daily agent
                # writes it when its sandboxed `rm` can't delete). Migrate it to
                # `archived` on read so it enters the lifecycle instead of being
                # silently ignored (which let those stubs accumulate forever).
                if val_str == "defunct":
                    result["status"] = "archived"
                elif val_str in ("active", "archived", "absorbed"):
                    # Phase 6 Item 1: `absorbed` (consolidation-agent merge
                    # tombstone) is preserved verbatim — NOT normalised to
                    # `archived` — so the merge provenance survives a round-trip.
                    # decay treats it identically via is_tombstoned (out of the
                    # active lifecycle, reapable past grace).
                    result["status"] = val_str
            elif key == "sources":
                # Simple parse: [a, b, c] or empty []
                if val_str.startswith('[') and val_str.endswith(']'):
                    inner = val_str[1:-1].strip()
                    if inner:
                        result["sources"] = [s.strip() for s in inner.split(',')]
                    else:
                        result["sources"] = []
            elif key == "created":
                result["created"] = val_str if val_str else None
            elif key == "last_reinforced":
                result["last_reinforced"] = val_str if val_str else None
            elif key == "reinforce_count":
                try:
                    result["reinforce_count"] = int(val_str)
                except ValueError:
                    pass
            elif key == "decay_score":
                try:
                    result["decay_score"] = float(val_str)
                except ValueError:
                    pass
            elif key == "invalid_at":
                # Phase 5 Item 2 (N2): tombstone marker — the date a "drop"
                # soft-deleted this record (status: archived + invalid_at).
                # Round-tripped so rewrites never lose the reap-grace anchor.
                result["invalid_at"] = val_str if val_str else None
            elif key == "verified_date":
                result["verified_date"] = val_str if val_str else None
            elif key == "verified_by":
                result["verified_by"] = val_str if val_str else None
        except Exception:
            # A malformed value for one key is skipped; the field keeps its
            # default and parsing continues (previously also noted in an internal,
            # never-surfaced _parse_errors list, removed Phase 14).
            pass

    # Extract body (everything after the closing fence, exactly as split returns).
    result["_body"] = body

    return result


def serialize_frontmatter(meta: Dict[str, Any]) -> str:
    """
    Serialize dict back to YAML-like frontmatter.

    Routed through the shared `frontmatter.serialize` (single source) but keeps
    decay's OWN interpretation: emit ONLY the keys in `order` that are present
    AND non-None, formatting a `sources` list as `[a, b, c]` / `[]`. The shared
    serializer appends `'\n' + body`; decay's contract is the fence block WITHOUT
    a trailing newline (callers do `serialize_frontmatter(meta) + '\n' + body`),
    so we pass `body=""` and drop the single trailing newline. Byte-identical to
    the previous inline implementation.
    """
    order = ["id", "tier", "category", "owner", "provenance", "sources", "created", "last_reinforced",
             "reinforce_count", "decay_score", "status", "invalid_at", "verified_date", "verified_by"]

    def format_value(v):
        if isinstance(v, list):
            if not v:
                return "[]"
            return "[" + ", ".join(str(x) for x in v) + "]"
        return str(v) if v is not None else ""

    fm = {key: format_value(meta[key]) for key in order
          if key in meta and meta[key] is not None}
    doc = _fm_serialize(fm, "", order=order)   # '---\n<k: v>\n...\n---\n'
    return doc[:-1]                            # drop the trailing '\n' after '---'


# ============================================================================
# DECAY CALCULATION
# ============================================================================

def parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO date string (YYYY-MM-DD)."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None


def half_life(reinforce_count: int, hl_base: float, hl_growth: float) -> float:
    """
    Compute half-life in days.

    half_life(rc) = HL_BASE * (1 + HL_GROWTH * (rc - 1))
    """
    return hl_base * (1 + hl_growth * (reinforce_count - 1))


def compute_decay_score(age_days: float, reinforce_count: int,
                       hl_base: float, hl_growth: float) -> float:
    """
    Compute decay_score using exponential decay.

    decay_score = 0.5 ** (age_days / half_life(rc))
    Clamps to [0.0, 1.0].
    """
    if reinforce_count < 1:
        reinforce_count = 1

    hl = half_life(reinforce_count, hl_base, hl_growth)
    if hl <= 0:
        return 0.0

    score = 0.5 ** (age_days / hl)
    return max(0.0, min(1.0, score))


def classify_record(mem: Dict[str, Any], now: datetime,
                   hl_base: float, hl_growth: float,
                   l0_drop_threshold: float,
                   l1_archive_threshold: float,
                   l1_demote_rc: int,
                   l0_to_l1_rc: int,
                   l1_to_l2_rc: int) -> Tuple[str, Dict[str, Any]]:
    """
    Classify a memory record and compute decay info.

    Returns: (action, info_dict)
    where action in ["keep", "drop", "archive", "demote", "upgrade-candidate", "l2-keep"]
    """
    action = "keep"
    info = {
        "id": mem["id"],
        "tier": mem["tier"],
        "reinforce_count": mem.get("reinforce_count", 1),
        "decay_score": 0.0,
        "age_days": 0.0,
    }

    # Compute age_days
    last_reinforced_str = mem.get("last_reinforced")
    if not last_reinforced_str:
        # Missing date: cannot compute decay. Keep file untouched.
        # decay_score=None signals apply_action to NOT overwrite the existing
        # frontmatter value (0.0 would falsely mean "fully forgotten").
        info["age_days"] = None
        info["decay_score"] = None
        return "keep", info

    last_reinforced = parse_date(last_reinforced_str)
    if not last_reinforced:
        # Parse failed (bad format): same handling as missing date.
        # Don't delete or overwrite decay_score on a parse error.
        info["age_days"] = None
        info["decay_score"] = None
        return "keep", info

    age = (now - last_reinforced).total_seconds() / (24 * 3600)
    info["age_days"] = age

    rc = mem.get("reinforce_count", 1)
    info["reinforce_count"] = rc

    # Compute decay_score
    decay = compute_decay_score(age, rc, hl_base, hl_growth)
    info["decay_score"] = decay

    # L2 never decays
    if mem["tier"] == "L2":
        return "l2-keep", info

    # Phase 5 Item 1 + C1 (N6) + Phase 6 Item 1: a tombstoned record (archived /
    # defunct / absorbed) is OUT of the active lifecycle. It is never a promotion
    # candidate (archived stubs were polluting Tony's upgrade backlog — and
    # Tony's earlier finding: archived files promoted to L1), and it is never
    # re-dropped/demoted/archived either: the reap pass in scan_memory_dir is the
    # ONLY thing that touches it once past the grace window. `absorbed`
    # (consolidation-agent merge tombstone) is handled identically here.
    if is_tombstoned(mem):
        return "keep", info

    # Promotion requires a live record: `status: active` exactly (a record
    # with a missing/unknown status is kept but never promoted — promotion is
    # the one action that must never fire on ambiguous state).
    is_active = mem.get("status") == "active"

    # L0: drop if decay < threshold
    if mem["tier"] == "L0":
        if decay < l0_drop_threshold:
            return "drop", info
        # Check upgrade candidate
        if rc >= l0_to_l1_rc and is_active:
            return "upgrade-candidate", info
        return "keep", info

    # L1: archive / demote if decay < threshold
    if mem["tier"] == "L1":
        if decay < l1_archive_threshold:
            # Demote back to L0 if rc <= threshold, else archive
            if rc <= l1_demote_rc:
                return "demote", info
            else:
                return "archive", info
        # Check upgrade candidate
        if rc >= l1_to_l2_rc and is_active:
            return "upgrade-candidate", info
        return "keep", info

    return "keep", info


def apply_action(path: Path, action: str, mem: Dict[str, Any],
                info: Dict[str, Any], now_str: Optional[str] = None) -> bool:
    """
    Apply action to file (only if --apply is set, but this gets called only then).

    `now_str` (YYYY-MM-DD) stamps `invalid_at` on a drop tombstone.
    Returns True if successful, False if error.
    """
    try:
        if action == "drop":
            # Phase 5 Item 2 (N2): "drop" is a SOFT-DELETE, never an unlink.
            # The record becomes a tombstone (status: archived +
            # invalid_at: <now>) — recoverable and excluded from active scans
            # (entropy/reinforce/verify all skip status: archived) — and is
            # physically reaped only by the reap pass once the grace window
            # has elapsed from the later of last_reinforced / invalid_at.
            body = mem.get("_body", "")
            mem["status"] = "archived"
            if not mem.get("invalid_at"):     # idempotent: never reset the anchor
                mem["invalid_at"] = now_str
            if info.get("decay_score") is not None:
                mem["decay_score"] = info["decay_score"]
            meta = {k: mem[k] for k in ["id", "tier", "category", "owner", "provenance", "sources",
                                         "created", "last_reinforced", "reinforce_count",
                                         "decay_score", "status", "invalid_at", "verified_date", "verified_by"]}
            new_content = serialize_frontmatter(meta) + '\n' + body
            _atomic_write(path, new_content, encoding="utf-8")
            return True

        elif action == "demote":
            # Rewrite frontmatter: tier=L0, move file to L0-working.
            # Persist the freshly-computed decay_score so the demoted memory
            # doesn't carry a stale high score into the next decay sweep.
            body = mem.get("_body", "")
            mem["tier"] = "L0"
            if info.get("decay_score") is not None:
                mem["decay_score"] = info["decay_score"]
            meta = {k: mem[k] for k in ["id", "tier", "category", "owner", "provenance", "sources",
                                         "created", "last_reinforced", "reinforce_count",
                                         "decay_score", "status", "invalid_at", "verified_date", "verified_by"]}
            new_content = serialize_frontmatter(meta) + '\n' + body

            new_path = path.parent.parent / "L0-working" / path.name
            new_path.parent.mkdir(parents=True, exist_ok=True)
            # Item 2: write the new-path file COMPLETELY (atomically) before any
            # unlink below — an interruption must leave at least one complete
            # copy, never zero.
            _atomic_write(new_path, new_content, encoding="utf-8")

            # Same-path guard (mirrors the promote branch): if the file is
            # physically already in L0-working but its frontmatter said tier: L1
            # (a dir/frontmatter mismatch that exists in live data), new_path
            # resolves to THIS path — we just rewrote it above, so unlinking it
            # would permanently destroy the memory. Only unlink when the file
            # actually moves to a genuinely different path.
            if new_path.resolve() != path.resolve():
                path.unlink()
            return True

        elif action == "promote":
            # Retire-on-promote: physically MOVE the file up one tier
            # (L0-working->L1-warm->L2-cold/<category>), rewrite tier: in
            # frontmatter, and leave NO file at the old tier (move, never copy).
            # Deterministic + idempotent — this replaces Tony's hand-moving.
            body = mem.get("_body", "")
            from_tier = mem["tier"]
            to_tier = "L1" if from_tier == "L0" else "L2"
            mem["tier"] = to_tier
            if info.get("decay_score") is not None:
                mem["decay_score"] = info["decay_score"]
            meta = {k: mem[k] for k in ["id", "tier", "category", "owner", "provenance", "sources",
                                         "created", "last_reinforced", "reinforce_count",
                                         "decay_score", "status", "invalid_at", "verified_date", "verified_by"]}
            new_content = serialize_frontmatter(meta) + '\n' + body

            # Derive the memory root robustly as the ancestor directly containing
            # the tier dir the file physically lives under. A source file normally
            # sits one level under memory_dir (L0-working/ or L1-warm/), so
            # parent.parent is the root — but a file already 3-deep in an L2 subdir
            # (memory/L2-cold/<cat>/file, the carry case) would nest wrongly under
            # a naive parent.parent. Fall back to parent.parent if no tier dir is
            # found in the path (unchanged behaviour for the normal 2-deep layout).
            tier_dir_names = set(TIER_DIRS.values())
            memory_root = path.parent.parent
            for i, part in enumerate(path.parts):
                if part in tier_dir_names:
                    memory_root = Path(*path.parts[:i]) if i > 0 else Path(".")
                    break
            if to_tier == "L2":
                # Land in L2-cold/<category>/. Resolution order:
                #   (a) frontmatter category: (set by CAPTURE, ∈ L2_CATEGORIES) —
                #       so profile/project memories route to the right subdir;
                #   (b) else the file's existing category subdir carried from
                #       path.parts (a file already under an L2 subdir keeps it);
                #   (c) else DEFAULT_L2_CATEGORY ("preferences").
                fm_category = mem.get("category")
                if fm_category in L2_CATEGORIES:
                    category = fm_category
                else:
                    category = next((p for p in path.parts if p in L2_CATEGORIES),
                                    DEFAULT_L2_CATEGORY)
                new_path = memory_root / TIER_DIRS["L2"] / category / path.name
            else:
                new_path = memory_root / TIER_DIRS["L1"] / path.name

            new_path.parent.mkdir(parents=True, exist_ok=True)
            # Item 2: write the new-path file COMPLETELY (atomically) before any
            # unlink below — an interruption must leave at least one complete
            # copy, never zero.
            _atomic_write(new_path, new_content, encoding="utf-8")
            # Guard idempotency: only unlink the old file if it's a different path
            # (never delete the file we just wrote).
            if new_path.resolve() != path.resolve():
                path.unlink()
            return True

        elif action == "archive":
            # Rewrite frontmatter: status=archived, update decay_score
            body = mem.get("_body", "")
            mem["status"] = "archived"
            mem["decay_score"] = info["decay_score"]
            meta = {k: mem[k] for k in ["id", "tier", "category", "owner", "provenance", "sources",
                                         "created", "last_reinforced", "reinforce_count",
                                         "decay_score", "status", "invalid_at", "verified_date", "verified_by"]}
            new_content = serialize_frontmatter(meta) + '\n' + body
            _atomic_write(path, new_content, encoding="utf-8")
            return True

        elif action in ["keep", "upgrade-candidate", "l2-keep"]:
            # Update decay_score only. If decay_score is None (date unparseable /
            # missing), preserve the existing frontmatter value instead of
            # overwriting it with a misleading 0.0.
            body = mem.get("_body", "")
            if info.get("decay_score") is not None:
                mem["decay_score"] = info["decay_score"]
            meta = {k: mem[k] for k in ["id", "tier", "category", "owner", "provenance", "sources",
                                         "created", "last_reinforced", "reinforce_count",
                                         "decay_score", "status", "invalid_at", "verified_date", "verified_by"]}
            new_content = serialize_frontmatter(meta) + '\n' + body
            _atomic_write(path, new_content, encoding="utf-8")
            return True

    except Exception as e:
        print(f"[ERROR applying {action} to {path}]: {e}", file=__import__('sys').stderr)
        return False

    return False


# ============================================================================
# OFFLINE-GAP DAMPER (Phase 5 Item 2, N2)
# ============================================================================

def last_run_marker_path(memory_dir: Path) -> Path:
    """Marker recording the last successful --apply run.

    Convention: `.company/memory` -> `.company/ops/.last-decay-run`. If the
    scanned dir isn't named `memory` (tests point at bare temp dirs), the
    marker lives INSIDE the scanned dir so we never write outside it."""
    if memory_dir.name == "memory":
        return memory_dir.parent / "ops" / LAST_RUN_MARKER
    return memory_dir / LAST_RUN_MARKER


def read_last_run(memory_dir: Path) -> Optional[datetime]:
    """Date of the last successful --apply, or None (missing/corrupt marker
    -> None: the damper simply doesn't engage; never raises)."""
    try:
        return parse_date(last_run_marker_path(memory_dir)
                          .read_text(encoding="utf-8").strip())
    except Exception:
        return None


def write_last_run(memory_dir: Path, now: datetime) -> None:
    """Record a successful --apply run. Best-effort — marker trouble must
    never fail the batch."""
    try:
        marker = last_run_marker_path(memory_dir)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(now.strftime("%Y-%m-%d") + "\n", encoding="utf-8")
    except Exception:
        pass


# ============================================================================
# MAIN SCANNING & REPORTING
# ============================================================================

def scan_memory_dir(memory_dir: Path, now: datetime,
                   hl_base: float, hl_growth: float,
                   l0_drop_threshold: float,
                   l1_archive_threshold: float,
                   l1_demote_rc: int,
                   l0_to_l1_rc: int,
                   l1_to_l2_rc: int,
                   reap_grace_days: int,
                   apply: bool,
                   defer_reap: bool = False) -> Dict[str, Any]:
    """
    Scan memory_dir recursively for .md files, compute decay, optionally apply actions.

    `now` is the EFFECTIVE reference time — when the offline-gap damper is
    active (see main), the caller passes a clamped now so per-file elapsed
    age is capped, and sets `defer_reap=True` so no physical unlink happens
    on the first tick after a long outage.
    """
    report = {
        "now": now.strftime("%Y-%m-%d"),
        "memory_dir": str(memory_dir),
        "applied": apply,
        "scanned": 0,
        "by_tier": {"L0": 0, "L1": 0, "L2": 0},
        "actions": {
            "drop": [],
            "archive": [],
            "demote": [],
            "upgrade_candidates": [],
            "reaped": [],
            "reap_deferred": [],
            "keep": 0,
            "l2_keep": 0
        },
        "warnings": []
    }

    if not memory_dir.exists():
        report["warnings"].append(f"Memory dir not found: {memory_dir}")
        return report

    # Recursively find all .md files
    md_files = sorted(memory_dir.rglob("*.md"))

    # D5: cap one tier-move per file (by id) per --apply run. Records every id
    # that has been (or would be) promoted this pass so a memory can advance at
    # most ONE tier per invocation. Without this, a memory that exists both as
    # an L0 shadow and at L1 promotes L0->L1 (overwriting the L1) AND then the
    # already-listed L1 advances L1->L2 in the SAME run — reaching the permanent
    # L2 tier same-day with no review.
    promoted_ids = set()

    for path in md_files:
        try:
            content = path.read_text(encoding='utf-8')
            mem = parse_frontmatter(content)

            # Validate required fields
            if mem["id"] is None:
                report["warnings"].append(f"{path}: missing id")
                continue

            if mem["tier"] is None:
                report["warnings"].append(f"{path}: missing/invalid tier")
                continue

            # Count by tier
            report["scanned"] += 1
            report["by_tier"][mem["tier"]] += 1

            # Reap: a tombstoned file (archived / defunct / absorbed) untouched
            # past the grace window is physically dropped here in the
            # deterministic --apply pass (the daily agent's shell `rm` is
            # sandbox-blocked, so these stubs would otherwise accumulate on disk
            # forever). `absorbed` is the consolidation-agent merge tombstone —
            # reaped exactly like `archived`. NEVER reap active; NEVER reap L2
            # (guarded below — L2 is never tombstoned per policy).
            if is_tombstoned(mem):
                # L2 reap-safety: L2 is a permanent tier and must NEVER be
                # reaped. Use an EXPLICIT guard, not `assert` — `python3 -O`
                # strips asserts, which would let an archived L2 file be
                # physically deleted (permanent-tier data loss).
                if mem["tier"] == "L2":
                    report["warnings"].append(
                        f"{path}: refusing to reap L2 (archived); L2 is permanent")
                    continue
                # Blessed-charter reap guard: charter axioms are permanent by
                # definition — treat like L2 regardless of the tier field.
                # Explicit `if`, NOT assert (python3 -O strips asserts — the
                # Phase-1 D2 lesson — which would silently re-arm this
                # data-loss path in optimized runs).
                if is_blessed_charter(mem):
                    report["warnings"].append(
                        f"{path}: charter-guard: refusing to reap blessed "
                        f"charter seed id={mem['id']}; charter seed found "
                        f"below L2 — move it to L2-cold/profile/")
                    continue
                # Phase 5 Item 2 (N2): the grace window runs from the LATER of
                # last_reinforced / invalid_at — a tombstoned drop stays
                # recoverable for the full grace window from the moment it was
                # invalidated, even if last_reinforced is much older.
                reap_candidates = [d for d in (parse_date(mem.get("last_reinforced")),
                                               parse_date(mem.get("invalid_at"))) if d]
                if reap_candidates:
                    reap_last = max(reap_candidates)
                else:
                    # C2 (BOB-F5): a tombstone carrying NEITHER last_reinforced
                    # nor invalid_at previously stayed `keep` forever — no anchor,
                    # so the grace clock never started. Fall back to `created`
                    # first, then the file's mtime, so a dateless
                    # archived/absorbed stub still ages out and gets reaped.
                    # `created` is preferred because it is round-tripped in the
                    # frontmatter and therefore SURVIVES the keep-pass rewrite
                    # (a kept within-grace tombstone is re-serialized every
                    # --apply run, bumping mtime to ~now).
                    reap_last = parse_date(mem.get("created"))
                    if reap_last is None:
                        try:
                            reap_last = datetime.fromtimestamp(path.stat().st_mtime)
                        except Exception:
                            reap_last = None
                        # BOB-F5 must-fix: mtime is the ONLY anchor that is NOT
                        # stable across runs — the keep-pass rewrite below bumps
                        # it to ~now each --apply, which would reset reap_age to
                        # ≈the daily gap every run and mean a pure-mtime anchor
                        # NEVER crosses grace (reaps never). Self-heal on first
                        # encounter: STAMP the mtime date into `invalid_at` so
                        # subsequent runs read a fixed frontmatter anchor (exactly
                        # the stability `created` already gives). The keep-pass
                        # persists mem here; on --apply this fires once then the
                        # anchor is permanent. (`created` needs no stamp — it is
                        # already a stable frontmatter field.)
                        if reap_last is not None and not mem.get("invalid_at"):
                            mem["invalid_at"] = reap_last.strftime("%Y-%m-%d")
                if reap_last is not None:
                    reap_age = (now - reap_last).total_seconds() / (24 * 3600)
                    if reap_age > reap_grace_days:
                        # Offline-gap damper: on the first tick after a long
                        # outage NO physical unlink happens — the reap is
                        # deferred to the next (normal-gap) run. Deleting is
                        # the one act we can't roll back.
                        if defer_reap:
                            report["actions"]["reap_deferred"].append({
                                "id": mem["id"],
                                "tier": mem["tier"],
                                "age_days": reap_age,
                            })
                            continue  # keep the tombstone this run
                        if apply:
                            path.unlink()
                        report["actions"]["reaped"].append({
                            "id": mem["id"],
                            "tier": mem["tier"],
                            "age_days": reap_age,
                            "last_reinforced": mem.get("last_reinforced"),
                        })
                        continue  # reaped: skip classification/apply

            # Classify
            action, info = classify_record(
                mem, now,
                hl_base, hl_growth,
                l0_drop_threshold,
                l1_archive_threshold,
                l1_demote_rc,
                l0_to_l1_rc,
                l1_to_l2_rc
            )

            # Blessed-charter guard (Phase 4, Item 1): a charter-class memory
            # (self-declared charter provenance AND id in the blessed seed set,
            # via charter_ids.is_blessed_charter) is an architectural axiom —
            # permanent by definition. It must NEVER be dropped, demoted, or
            # archived by decay, regardless of its current tier field. Explicit
            # `if`, NOT assert: python3 -O strips asserts (Phase-1 D2 lesson),
            # which would silently re-arm the deletion path in optimized runs.
            # Non-blessed memories that merely self-declare charter fall
            # through and decay normally (anti-abuse preserved).
            # The warning fires only while a seed still sits below L2 — it
            # signals a charter seed hasn't been placed in L2-cold/profile/ yet.
            if action in ("drop", "demote", "archive") and is_blessed_charter(mem):
                report["warnings"].append(
                    f"{path}: charter-guard: refusing to {action} blessed "
                    f"charter seed id={mem['id']} (tier {mem['tier']}); "
                    f"charter seed found below L2 — move it to "
                    f"L2-cold/profile/")
                action = "keep"

            # D5: enforce one tier-move per file (by id) per run. If this id was
            # already promoted earlier this pass, downgrade the action to keep so
            # it advances at most one tier this run (the next run advances it one
            # more step). Deterministic; applies identically in dry-run so the
            # report matches what --apply would do.
            if action == "upgrade-candidate" and mem["id"] in promoted_ids:
                report["warnings"].append(
                    f"{path}: id={mem['id']} already promoted this run; "
                    f"capping at one tier-move per --apply run")
                action = "keep"

            # Apply if requested.
            # drop / archive / demote mutate the file (delete / move / status).
            if apply and action in ("drop", "archive", "demote"):
                if not apply_action(path, action, mem, info,
                                    now_str=now.strftime("%Y-%m-%d")):
                    report["warnings"].append(f"{path}: failed to apply {action}")
                    continue

            # upgrade-candidate now retires-on-promote: physically MOVE the file
            # up one tier (deterministic applier) instead of leaving an L0 shadow
            # for Tony to hand-move. Idempotent; leaves no file at the old tier.
            # Acceptance intent: re-running --apply advances at most one tier per
            # file per run (the D5 cap above downgrades a same-id second promote
            # to keep), so a memory cannot cross two tiers in a single run.
            elif apply and action == "upgrade-candidate":
                if not apply_action(path, "promote", mem, info):
                    report["warnings"].append(f"{path}: failed to apply promote")
                    continue

            # keep / l2-keep don't change tier or status, but we still persist the
            # freshly-computed decay_score.
            elif apply and action in ("keep", "l2-keep"):
                apply_action(path, action, mem, info)

            # Record action
            if action == "drop":
                report["actions"]["drop"].append(info)
            elif action == "archive":
                report["actions"]["archive"].append(info)
            elif action == "demote":
                report["actions"]["demote"].append(info)
            elif action == "upgrade-candidate":
                # D5: mark this id as promoted this run so a later file with the
                # same id (e.g. an L0 shadow vs its L1 copy) is capped to keep
                # rather than advancing a second tier in the same run.
                promoted_ids.add(info["id"])
                report["actions"]["upgrade_candidates"].append({
                    "id": info["id"],
                    "from": info["tier"],
                    "to": "L1" if info["tier"] == "L0" else "L2",
                    "reinforce_count": info["reinforce_count"]
                })
            elif action == "l2-keep":
                report["actions"]["l2_keep"] += 1
            else:  # keep
                report["actions"]["keep"] += 1

        except Exception as e:
            report["warnings"].append(f"{path}: exception: {e}")

    return report


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Decay — compute memory decay_score and classify records."
    )
    parser.add_argument(
        "--memory-dir",
        default=".company/memory",
        help="Root memory directory (default: .company/memory)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply actions (drop/demote/archive/decay_score updates). "
             "Default is dry-run (read-only)."
    )
    parser.add_argument(
        "--now",
        help="Reference date (YYYY-MM-DD). Default: today."
    )
    parser.add_argument(
        "--config",
        default=".company/org/policy.md",
        help="Path to policy.md to read tunable constants (default: "
             ".company/org/policy.md). If the file is absent or a constant is "
             "not declared, built-in defaults are used."
    )

    args = parser.parse_args()

    # Parse --now
    if args.now:
        now = parse_date(args.now)
        if not now:
            print(f"Invalid --now format: {args.now}", file=__import__('sys').stderr)
            return 1
    else:
        now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Resolve tunable constants: policy.md §7 overrides built-in defaults.
    defaults = {
        "HL_BASE": DEFAULT_HL_BASE,
        "HL_GROWTH": DEFAULT_HL_GROWTH,
        "L0_DROP_THRESHOLD": DEFAULT_L0_DROP_THRESHOLD,
        "L1_ARCHIVE_THRESHOLD": DEFAULT_L1_ARCHIVE_THRESHOLD,
        "L1_DEMOTE_RC": DEFAULT_L1_DEMOTE_RC,
        "L0_TO_L1_RC": DEFAULT_L0_TO_L1_RC,
        "L1_TO_L2_RC": DEFAULT_L1_TO_L2_RC,
        "REAP_GRACE_DAYS": DEFAULT_REAP_GRACE_DAYS,
        "OFFLINE_GAP_DAYS": DEFAULT_OFFLINE_GAP_DAYS,
    }
    if _resolve_config is not None:
        values, sources = _resolve_config(defaults, args.config)
    else:
        values = dict(defaults)
        sources = {k: "default" for k in defaults}

    hl_base = values["HL_BASE"]
    hl_growth = values["HL_GROWTH"]
    l0_drop_threshold = values["L0_DROP_THRESHOLD"]
    l1_archive_threshold = values["L1_ARCHIVE_THRESHOLD"]
    l1_demote_rc = values["L1_DEMOTE_RC"]
    l0_to_l1_rc = values["L0_TO_L1_RC"]
    l1_to_l2_rc = values["L1_TO_L2_RC"]
    reap_grace_days = values["REAP_GRACE_DAYS"]
    offline_gap_days = values["OFFLINE_GAP_DAYS"]

    # P3: if a policy file was given and exists but some constant fell back to a
    # default, say so on stderr so tuning is observable instead of silent.
    config_exists = bool(args.config) and Path(args.config).exists()
    fell_back = sorted(k for k, s in sources.items() if s == "default")
    if config_exists and fell_back:
        print(f"[WARN] {args.config}: using built-in defaults for "
              f"{', '.join(fell_back)} (not declared in policy)",
              file=sys.stderr)

    # Phase 5 Item 2 (N2) — offline-gap damper. If the gap since the last
    # successful --apply run exceeds OFFLINE_GAP_DAYS, clamp the effective
    # `now` to marker + OFFLINE_GAP_DAYS (capping every file's elapsed decay
    # age at the threshold) and defer physical reaps this run: a two-week
    # outage must not convert gradual forgetting into a one-tick purge.
    # Deterministic and testable via --now + a planted marker.
    memory_dir = Path(args.memory_dir)
    last_run = read_last_run(memory_dir)
    effective_now, defer_reap = now, False
    gap_damper = {
        "active": False,
        "last_run": last_run.strftime("%Y-%m-%d") if last_run else None,
        "offline_gap_days": offline_gap_days,
    }
    if last_run is not None:
        gap_days = (now - last_run).total_seconds() / (24 * 3600)
        gap_damper["gap_days"] = round(gap_days, 2)
        if gap_days > offline_gap_days:
            effective_now = last_run + timedelta(days=offline_gap_days)
            defer_reap = True
            gap_damper["active"] = True
            gap_damper["effective_now"] = effective_now.strftime("%Y-%m-%d")
            # The one gap notice (also lands in report["warnings"] below);
            # stderr so daily-run.sh surfaces it in the daily log.
            print(f"[GAP] offline-gap damper active: {gap_days:.1f}d since "
                  f"last successful run ({gap_damper['last_run']}) > "
                  f"{offline_gap_days}d — capping elapsed decay age at "
                  f"{gap_damper['effective_now']} and deferring physical "
                  f"reaps this run", file=sys.stderr)

    # Scan and report
    report = scan_memory_dir(
        memory_dir, effective_now,
        hl_base, hl_growth,
        l0_drop_threshold,
        l1_archive_threshold,
        l1_demote_rc,
        l0_to_l1_rc,
        l1_to_l2_rc,
        reap_grace_days,
        apply=args.apply,
        defer_reap=defer_reap
    )
    report["now"] = now.strftime("%Y-%m-%d")   # real now; effective in gap_damper
    report["gap_damper"] = gap_damper
    if gap_damper["active"]:
        report["warnings"].append(
            f"offline-gap damper: {gap_damper['gap_days']}d gap since "
            f"{gap_damper['last_run']} — elapsed age capped at "
            f"{gap_damper['effective_now']}, "
            f"{len(report['actions']['reap_deferred'])} reap(s) deferred")

    # Record the successful --apply so the next run can measure its gap.
    if args.apply:
        write_last_run(memory_dir, now)

    # P3: surface effective constants and their provenance (policy vs default).
    report["config"] = {
        "source_file": args.config if config_exists else None,
        "values": values,
        "sources": sources,
    }

    # Output JSON
    print(json.dumps(report, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    exit(main())
