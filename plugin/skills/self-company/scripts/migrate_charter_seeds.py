#!/usr/bin/env python3
"""
migrate_charter_seeds — one-time, idempotent migration of the 8 blessed charter
axiom seeds into the permanent tier (Phase 4, Item 1).

WHY: the seeds were install-written into L0-working/ with tier: L0, rc: 1 and
nothing ever reinforces them, so decay's L0-drop path was set to physically
delete them around 2026-07-11. Axioms are identity/architecture facts —
permanent by definition — so they belong in L2-cold/profile/ with tier: L2
(L2 never decays and is never reaped). decay.py now carries a belt-and-braces
charter guard, but the guard WARNS on every run until this migration lands;
this script is the real fix.

EXECUTION DISCIPLINE (per the approved Phase-4 spec): Tom runs this LAST,
after Gibby clears batch B1. Dry-run is the default; nothing is written
without --apply.

    # preview (no mutation)
    python3 scripts/migrate_charter_seeds.py --memory-dir .company/memory

    # execute
    python3 scripts/migrate_charter_seeds.py --memory-dir .company/memory --apply

Per blessed seed id:
  - already at L2-cold/profile/<file> with tier: L2  -> "noop" (idempotent)
  - at L2-cold/profile/ but frontmatter disagrees    -> "fix-frontmatter"
    (rewrite in place: tier: L2, category: profile)
  - anywhere else (e.g. L0-working/)                 -> "migrate": rewrite
    frontmatter (tier: L2, category: profile, everything else preserved),
    write to L2-cold/profile/<same filename>, unlink the old path (same-path
    guard: never unlink the file just written)
  - target already exists AND a stray copy exists    -> "skip-duplicate"
    (never clobber the migrated file; stray is left for manual review)
  - not found anywhere                               -> "missing" warning

Anti-abuse: a file is only treated as a seed when charter_ids.is_blessed_charter
passes (blessed id AND self-declared charter provenance) — a random file that
merely reuses a seed id without charter provenance is not touched.

Pure stdlib. Reuses decay.py's frontmatter parse/serialize so the rewritten
files are byte-consistent with what the decay pass itself would write.
"""

import argparse
import json
import sys
from pathlib import Path

from charter_ids import CHARTER_SEED_IDS, is_blessed_charter
from decay import parse_frontmatter, serialize_frontmatter

TARGET_SUBDIR = ("L2-cold", "profile")
FM_KEYS = ["id", "tier", "category", "owner", "provenance", "sources",
           "created", "last_reinforced", "reinforce_count", "decay_score",
           "status", "verified_date", "verified_by"]


def rewrite_as_l2_profile(mem):
    """Return file content with tier: L2, category: profile; body preserved."""
    mem = dict(mem)
    mem["tier"] = "L2"
    mem["category"] = "profile"
    meta = {k: mem.get(k) for k in FM_KEYS}
    return serialize_frontmatter(meta) + "\n" + mem.get("_body", "")


def migrate(memory_dir: Path, apply: bool):
    target_dir = memory_dir / Path(*TARGET_SUBDIR)
    report = {
        "applied": apply,
        "memory_dir": str(memory_dir),
        "target_dir": str(target_dir),
        "actions": [],
        "warnings": [],
    }

    if not memory_dir.exists():
        report["warnings"].append(f"memory dir not found: {memory_dir}")
        return report

    # id -> list of (path, mem) for every blessed-charter file found anywhere.
    found = {}
    for path in sorted(memory_dir.rglob("*.md")):
        try:
            mem = parse_frontmatter(path.read_text(encoding="utf-8"))
        except Exception as e:
            report["warnings"].append(f"{path}: unreadable ({e})")
            continue
        if mem.get("id") in CHARTER_SEED_IDS and is_blessed_charter(mem):
            found.setdefault(mem["id"], []).append((path, mem))

    for seed_id in sorted(CHARTER_SEED_IDS):
        copies = found.get(seed_id, [])
        if not copies:
            report["warnings"].append(
                f"blessed seed id={seed_id}: no charter-provenance file found")
            report["actions"].append({"id": seed_id, "action": "missing"})
            continue

        at_target = [(p, m) for p, m in copies if p.parent == target_dir]
        elsewhere = [(p, m) for p, m in copies if p.parent != target_dir]

        # 1) Ensure the target copy (if present) has correct frontmatter.
        target_exists = bool(at_target)
        for path, mem in at_target:
            if mem.get("tier") == "L2" and mem.get("category") == "profile":
                report["actions"].append(
                    {"id": seed_id, "action": "noop", "path": str(path)})
            else:
                if apply:
                    path.write_text(rewrite_as_l2_profile(mem), encoding="utf-8")
                report["actions"].append(
                    {"id": seed_id, "action": "fix-frontmatter", "path": str(path)})

        # 2) Move (at most one) stray copy to the target; never clobber.
        for path, mem in elsewhere:
            new_path = target_dir / path.name
            if target_exists or new_path.exists():
                report["warnings"].append(
                    f"{path}: target already exists for id={seed_id}; "
                    f"leaving stray copy in place for manual review")
                report["actions"].append(
                    {"id": seed_id, "action": "skip-duplicate", "path": str(path)})
                continue
            if apply:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                new_path.write_text(rewrite_as_l2_profile(mem), encoding="utf-8")
                # Same-path guard (mirrors decay.apply_action): never unlink
                # the file we just wrote.
                if new_path.resolve() != path.resolve():
                    path.unlink()
            report["actions"].append({
                "id": seed_id, "action": "migrate",
                "from": str(path), "to": str(new_path),
            })
            target_exists = True

    counts = {}
    for a in report["actions"]:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    report["summary"] = counts
    return report


def main():
    parser = argparse.ArgumentParser(
        description="One-time idempotent migration of the 8 blessed charter "
                    "seeds to L2-cold/profile/ (tier: L2). Dry-run by default.")
    parser.add_argument("--memory-dir", default=".company/memory",
                        help="Root memory directory (default: .company/memory)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually move/rewrite files. Default is dry-run.")
    args = parser.parse_args()

    report = migrate(Path(args.memory_dir), apply=args.apply)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    # Missing seeds are a warning, not a failure: re-runs after a successful
    # migration must stay rc=0 (idempotent no-op).
    return 0


if __name__ == "__main__":
    sys.exit(main())
