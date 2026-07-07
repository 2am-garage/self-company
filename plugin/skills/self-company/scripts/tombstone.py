#!/usr/bin/env python3
"""
tombstone — SINGLE authoritative source for the memory tombstone vocabulary.

A "tombstone" is a memory that has been retired from the ACTIVE set but is kept
on disk (recoverable) until the deterministic decay reap physically removes it
past the grace window. Three statuses are tombstones:

  * archived — decay soft-deleted it (L0 drop / L1 archive) OR a completed merge.
  * defunct  — legacy alias the daily agent used to write when its sandboxed
               `rm` couldn't delete a merged-away stub. Normalised to `archived`
               on read by the parsers, but recognised here for safety.
  * absorbed — the consolidation agent merged this duplicate's content INTO a
               canonical memory (Phase 6). It must be excluded from every active
               scan and reaped by decay exactly like `archived`.

Before Phase 6 this vocabulary was open-coded in every scanner
(`entropy.py`, `reinforce_memory.py`, `verify_memory.py`, `capture-trigger.py`,
`decay.py`) and it drifted: `absorbed` was recognised by NONE of them, so the
consolidation agent's `status: absorbed` tombstones stayed in the active set and
the same duplicate pairs re-surfaced every run. Consolidating the set HERE — the
same best-effort import pattern as `charter_ids.py` / `policy_config.py` — means
the vocabulary can never drift again.

Pure stdlib. No side effects.
"""

# The complete tombstone vocabulary. A memory whose `status` is any of these is
# OUT of the active set: excluded from entropy/reinforce/verify/capture scans,
# included only under `--include-archived`, and reapable by decay past grace.
TOMBSTONE_STATUSES = frozenset({"archived", "defunct", "absorbed"})


def is_tombstoned(fm):
    """True if the frontmatter's `status` is a tombstone (archived / defunct /
    absorbed). `fm` is any mapping with a `.get`; a missing/None status -> False
    (an active memory). Robust to case/whitespace noise in the raw value."""
    status = str(fm.get("status") or "").strip().lower()
    return status in TOMBSTONE_STATUSES
