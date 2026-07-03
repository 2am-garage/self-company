#!/usr/bin/env python3
"""
charter_ids — SINGLE authoritative source for the blessed charter seed set.

The 8 install-seeded architectural AXIOMS (true by construction, not
transcript-sourced claims) are recognised across the toolchain by id +
self-declared charter provenance. Before this module the seed list existed in
duplicate (verify_memory.py, entropy.py) and decay.py was about to add a third
copy for its data-loss guard — this consolidates all of them (Phase 4, Item 1).

Consumers (all in this directory, imported the same best-effort way the
policy_config loader is): verify_memory.py, entropy.py, decay.py,
migrate_charter_seeds.py.

ANTI-ABUSE contract (Chairman-approved, Phase 2 Item 6): a memory is honoured
as charter-class ONLY when it BOTH self-declares charter provenance
(`provenance: charter` frontmatter OR a `charter:<slug>` source) AND its id is
in the blessed set below. A normal captured memory that self-declares charter
to dodge VERIFY/decay is never trusted.

Pure stdlib. No side effects.
"""

import re

# The 8 blessed install seeds. Adding an id here is a Chairman-level decision:
# every member is exempted from VERIFY source-tracing, excluded from the
# unverified_rate KPI, and protected from decay drop/demote/archive/reap.
CHARTER_SEED_IDS = frozenset({
    "elon-as-manager",
    "org-hierarchy",
    "merge-gate",
    "repo-scoped-skill",
    "sub-agent-isolation",
    "verify-before-commit",
    "four-daily-runs",
    "minimal-permission-overhead",
})

# charter:<slug> — slug excludes whitespace and the quote/bracket/comma noise of
# the sources array so `sources: ["charter:org-hierarchy"]` yields the bare tag.
CHARTER_SOURCE_RE = re.compile(r'charter:[^\s,"\'\]\[]+')


def self_declares_charter(fm):
    """True if the frontmatter SELF-DECLARES charter provenance — via a
    `provenance: charter` key OR a `charter:<slug>` source. Says nothing about
    trustworthiness; that is `is_blessed_charter` below.

    Accepts both `sources` shapes used by the callers: a parsed list (entropy,
    decay — decay keeps the surrounding quotes, so quote-strip before matching)
    or the raw frontmatter string (verify_memory — regex-scanned).
    """
    prov = str(fm.get("provenance") or "").strip().lower()
    if prov == "charter":
        return True
    sources = fm.get("sources")
    if isinstance(sources, (list, tuple)):
        return any(str(s).strip().strip('"\'').startswith("charter:")
                   for s in sources)
    return bool(CHARTER_SOURCE_RE.search(str(sources or "")))


def is_blessed_charter(fm):
    """A memory is charter-class ONLY when it self-declares charter provenance
    AND its id is in the blessed install-seed set (anti-abuse: a non-blessed
    memory claiming charter stays an ordinary claim)."""
    return self_declares_charter(fm) and fm.get("id") in CHARTER_SEED_IDS
