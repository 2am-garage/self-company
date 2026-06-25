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
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple


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
        "owner": None,
        "sources": [],
        "created": None,
        "last_reinforced": None,
        "reinforce_count": None,
        "decay_score": None,
        "status": None,
        "_body": "",
        "_parse_errors": []
    }

    # Find frontmatter block
    lines = content.split('\n')
    if not lines or lines[0].strip() != '---':
        result["_parse_errors"].append("No opening --- found")
        return result

    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            end_idx = i
            break

    if end_idx is None:
        result["_parse_errors"].append("No closing --- found")
        return result

    # Parse frontmatter lines
    for line in lines[1:end_idx]:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        if ':' not in line:
            result["_parse_errors"].append(f"Malformed line: {line}")
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
                else:
                    result["_parse_errors"].append(f"Invalid tier: {val_str}")
            elif key == "owner":
                result["owner"] = val_str if val_str else None
            elif key == "status":
                if val_str in ("active", "archived"):
                    result["status"] = val_str
                else:
                    result["_parse_errors"].append(f"Invalid status: {val_str}")
            elif key == "sources":
                # Simple parse: [a, b, c] or empty []
                if val_str.startswith('[') and val_str.endswith(']'):
                    inner = val_str[1:-1].strip()
                    if inner:
                        result["sources"] = [s.strip() for s in inner.split(',')]
                    else:
                        result["sources"] = []
                else:
                    result["_parse_errors"].append(f"Malformed sources: {val_str}")
            elif key == "created":
                result["created"] = val_str if val_str else None
            elif key == "last_reinforced":
                result["last_reinforced"] = val_str if val_str else None
            elif key == "reinforce_count":
                try:
                    result["reinforce_count"] = int(val_str)
                except ValueError:
                    result["_parse_errors"].append(f"Non-int reinforce_count: {val_str}")
            elif key == "decay_score":
                try:
                    result["decay_score"] = float(val_str)
                except ValueError:
                    result["_parse_errors"].append(f"Non-float decay_score: {val_str}")
        except Exception as e:
            result["_parse_errors"].append(f"Error parsing {key}: {e}")

    # Extract body
    result["_body"] = '\n'.join(lines[end_idx + 1:])

    return result


def serialize_frontmatter(meta: Dict[str, Any]) -> str:
    """
    Serialize dict back to YAML-like frontmatter.
    """
    lines = ["---"]

    def format_value(v):
        if isinstance(v, list):
            if not v:
                return "[]"
            return "[" + ", ".join(str(x) for x in v) + "]"
        return str(v) if v is not None else ""

    for key in ["id", "tier", "owner", "sources", "created", "last_reinforced",
                "reinforce_count", "decay_score", "status"]:
        if key in meta and meta[key] is not None:
            lines.append(f"{key}: {format_value(meta[key])}")

    lines.append("---")
    return '\n'.join(lines)


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

    # L0: drop if decay < threshold
    if mem["tier"] == "L0":
        if decay < l0_drop_threshold:
            return "drop", info
        # Check upgrade candidate
        if rc >= l0_to_l1_rc:
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
        if rc >= l1_to_l2_rc:
            return "upgrade-candidate", info
        return "keep", info

    return "keep", info


def apply_action(path: Path, action: str, mem: Dict[str, Any],
                info: Dict[str, Any]) -> bool:
    """
    Apply action to file (only if --apply is set, but this gets called only then).

    Returns True if successful, False if error.
    """
    try:
        if action == "drop":
            path.unlink()
            return True

        elif action == "demote":
            # Rewrite frontmatter: tier=L0, move file to L0-working.
            # Persist the freshly-computed decay_score so the demoted memory
            # doesn't carry a stale high score into the next decay sweep.
            body = mem.get("_body", "")
            mem["tier"] = "L0"
            if info.get("decay_score") is not None:
                mem["decay_score"] = info["decay_score"]
            meta = {k: mem[k] for k in ["id", "tier", "owner", "sources",
                                         "created", "last_reinforced", "reinforce_count",
                                         "decay_score", "status"]}
            new_content = serialize_frontmatter(meta) + '\n' + body

            new_path = path.parent.parent / "L0-working" / path.name
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_text(new_content)

            path.unlink()
            return True

        elif action == "archive":
            # Rewrite frontmatter: status=archived, update decay_score
            body = mem.get("_body", "")
            mem["status"] = "archived"
            mem["decay_score"] = info["decay_score"]
            meta = {k: mem[k] for k in ["id", "tier", "owner", "sources",
                                         "created", "last_reinforced", "reinforce_count",
                                         "decay_score", "status"]}
            new_content = serialize_frontmatter(meta) + '\n' + body
            path.write_text(new_content)
            return True

        elif action in ["keep", "upgrade-candidate", "l2-keep"]:
            # Update decay_score only. If decay_score is None (date unparseable /
            # missing), preserve the existing frontmatter value instead of
            # overwriting it with a misleading 0.0.
            body = mem.get("_body", "")
            if info.get("decay_score") is not None:
                mem["decay_score"] = info["decay_score"]
            meta = {k: mem[k] for k in ["id", "tier", "owner", "sources",
                                         "created", "last_reinforced", "reinforce_count",
                                         "decay_score", "status"]}
            new_content = serialize_frontmatter(meta) + '\n' + body
            path.write_text(new_content)
            return True

    except Exception as e:
        print(f"[ERROR applying {action} to {path}]: {e}", file=__import__('sys').stderr)
        return False

    return False


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
                   apply: bool) -> Dict[str, Any]:
    """
    Scan memory_dir recursively for .md files, compute decay, optionally apply actions.
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

            # Apply if requested.
            # drop / archive / demote mutate the file (delete / move / status).
            if apply and action in ("drop", "archive", "demote"):
                if not apply_action(path, action, mem, info):
                    report["warnings"].append(f"{path}: failed to apply {action}")
                    continue

            # keep / l2-keep / upgrade-candidate don't change tier or status, but
            # we still persist the freshly-computed decay_score. upgrade-candidate
            # reuses the 'keep' write path (decay_score update only); the actual
            # tier move is executed later by Tony in WRITE, not here.
            if apply and action in ("keep", "l2-keep"):
                apply_action(path, action, mem, info)
            elif apply and action == "upgrade-candidate":
                apply_action(path, "keep", mem, info)

            # Record action
            if action == "drop":
                report["actions"]["drop"].append(info)
            elif action == "archive":
                report["actions"]["archive"].append(info)
            elif action == "demote":
                report["actions"]["demote"].append(info)
            elif action == "upgrade-candidate":
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
        help="Path to policy.md to read tunable constants. "
             "If not found or not specified, use built-in defaults."
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

    # Load config (optional; use defaults if not found)
    hl_base = DEFAULT_HL_BASE
    hl_growth = DEFAULT_HL_GROWTH
    l0_drop_threshold = DEFAULT_L0_DROP_THRESHOLD
    l1_archive_threshold = DEFAULT_L1_ARCHIVE_THRESHOLD
    l1_demote_rc = DEFAULT_L1_DEMOTE_RC
    l0_to_l1_rc = DEFAULT_L0_TO_L1_RC
    l1_to_l2_rc = DEFAULT_L1_TO_L2_RC

    if args.config:
        config_path = Path(args.config)
        if config_path.exists():
            try:
                config_text = config_path.read_text(encoding='utf-8')
                # Simple parsing: look for lines like "HL_BASE = 7.0" or "HL_BASE: 7.0"
                for line in config_text.split('\n'):
                    if 'HL_BASE' in line and ('=' in line or ':' in line):
                        try:
                            val = re.search(r'[\d.]+', line)
                            if val:
                                hl_base = float(val.group())
                        except:
                            pass
                    elif 'HL_GROWTH' in line and ('=' in line or ':' in line):
                        try:
                            val = re.search(r'[\d.]+', line)
                            if val:
                                hl_growth = float(val.group())
                        except:
                            pass
                    elif 'L0_DROP_THRESHOLD' in line and ('=' in line or ':' in line):
                        try:
                            val = re.search(r'[\d.]+', line)
                            if val:
                                l0_drop_threshold = float(val.group())
                        except:
                            pass
                    elif 'L1_ARCHIVE_THRESHOLD' in line and ('=' in line or ':' in line):
                        try:
                            val = re.search(r'[\d.]+', line)
                            if val:
                                l1_archive_threshold = float(val.group())
                        except:
                            pass
                    elif 'L1_DEMOTE_RC' in line and ('=' in line or ':' in line):
                        try:
                            val = re.search(r'\d+', line)
                            if val:
                                l1_demote_rc = int(val.group())
                        except:
                            pass
                    elif 'L0_TO_L1_RC' in line and ('=' in line or ':' in line):
                        try:
                            val = re.search(r'\d+', line)
                            if val:
                                l0_to_l1_rc = int(val.group())
                        except:
                            pass
                    elif 'L1_TO_L2_RC' in line and ('=' in line or ':' in line):
                        try:
                            val = re.search(r'\d+', line)
                            if val:
                                l1_to_l2_rc = int(val.group())
                        except:
                            pass
            except Exception as e:
                print(f"[WARN] Failed to parse config {args.config}: {e}",
                      file=__import__('sys').stderr)

    # Scan and report
    memory_dir = Path(args.memory_dir)
    report = scan_memory_dir(
        memory_dir, now,
        hl_base, hl_growth,
        l0_drop_threshold,
        l1_archive_threshold,
        l1_demote_rc,
        l0_to_l1_rc,
        l1_to_l2_rc,
        apply=args.apply
    )

    # Output JSON
    print(json.dumps(report, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    exit(main())
