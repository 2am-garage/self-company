#!/usr/bin/env python3
"""
Self-Company Entropy KPI Measurement Tool

Quantifies entropy across four dimensions:
  1. dup_rate: Approximate duplicate memories (Jaccard similarity)
  2. contradiction_score: Detected contradictions (slug family + opposing keywords)
  3. stale_rate: Expired memories (decay_score below tier thresholds)
  4. unverified_rate: Not confirmed by VERIFY (no verified_date) or no sources

Formula:
  Entropy = w1*dup_rate + w2*contradiction_score + w3*stale_rate + w4*unverified_rate

Output: JSON with dimension scores, total entropy, and detailed candidate lists for review.
Each detected contradiction pair also carries an ADVISORY-ONLY `recommend`
(details.contradiction_recommendations) — a deterministic pick between the two
memories from `last_reinforced`/`reinforce_count` already on disk, no LLM, no
network. It never changes what's detected/scored and never auto-resolves
anything; Tony still adjudicates every pair by hand (see
compute_contradiction_recommendations).

Usage:
  python3 scripts/entropy.py [--memory-dir .company/memory] [--config .company/org/policy.md] [--now YYYY-MM-DD] [--include-archived]
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict
from difflib import SequenceMatcher

# Bucket 2 (Phase 14): the shared sibling modules (policy_config, charter_ids,
# tombstone, frontmatter) live in THIS directory. Put it on sys.path FIRST so the
# hard imports below resolve under every entry point — direct run, cron, venv
# re-exec, a hook, or an import by another module / the test harness (mirrors
# schedule_validator.py). They always ship together, so the imports never fail.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Shared RAG venv re-exec helper (rag_venv.py, same dir) — the ONE copy of the
# .company/.rag-venv re-exec logic (imported like tombstone/charter_ids). Importing
# it is side-effect-free: it NEVER re-execs at import, which is what lets this
# stdlib-only module stay import-safe from a base-python process.
from rag_venv import reexec_if_needed

# Shared policy loader — single source of truth for tunable constants
# (reads org/policy.md §7). Best-effort import; falls back to built-in defaults.
try:
    from policy_config import load_policy_constants as _shared_load_policy
except Exception:  # pragma: no cover - defensive
    _shared_load_policy = None

# --- Item 2: optional offline embedding backend for the semantic dedup pass ---
# entropy.py is stdlib-only and MUST run without the RAG venv. When the venv IS
# present we re-exec into it (same pattern reinforce_memory.py uses) so the
# embedding second pass in compute_dup_rate can run against the SAME offline
# model (rag_embed / bge-small-en-v1.5, 384-dim). When it is absent we fall back
# to Jaccard-only — never a hard fail, never a network call. Set SC_NO_RAG=1 to
# force Jaccard-only (used by tests to exercise the fallback path).
def _memory_dir_from_argv(argv):
    """Peek --memory-dir out of argv without argparse (the re-exec runs first).

    Handles both `--memory-dir PATH` and `--memory-dir=PATH`. Falls back to the
    argparse default (.company/memory) when the flag is absent, so the derived
    venv path degenerates to the old cwd-based fallback in that case.
    """
    for i, arg in enumerate(argv):
        if arg == "--memory-dir" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--memory-dir="):
            return arg.split("=", 1)[1]
    return ".company/memory"


def _reexec_into_rag_venv():
    # SC_NO_RAG (force Jaccard-only) is entropy-specific, so it is guarded HERE at
    # the callsite rather than in the shared helper — keeping every other caller
    # byte-identical. The shared helper handles the SC_RAG_REEXEC short-circuit,
    # the fastembed/numpy probe, and the candidate-interpreter search.
    if os.environ.get("SC_NO_RAG"):
        return
    # C2 (Phase 5): resolve the project venv from --memory-dir, whose parent is
    # that project's .company — NOT from whatever cwd happens to hold. An off-cwd
    # invocation with a foreign .company under cwd must never exec the wrong
    # project's interpreter. With no --memory-dir the default (.company/memory)
    # resolves against cwd, preserving the previous cwd fallback behavior.
    reexec_if_needed(["fastembed", "numpy"],
                     mem_dir=_memory_dir_from_argv(sys.argv))


# NOTE: _reexec_into_rag_venv() is intentionally NOT called at import time.
# Importing entropy from a base-python process must never os.execv and replace
# the caller's program. The re-exec is performed only when this file is run
# directly, from the __main__ entrypoint below (see main()).

try:
    if os.environ.get("SC_NO_RAG"):
        raise ImportError("SC_NO_RAG set")
    import rag_embed
    import numpy as np
    _HAS_RAG = True
except Exception:
    _HAS_RAG = False

# Populated by compute_dup_rate; surfaced in JSON as `semantic_dedup` so callers
# can see whether the embedding pass ran or was skipped (and why).
_SEMANTIC_META = None

# ============================================================================
# Constants (tunable, defaults == manifest §1)
# ============================================================================

# Entropy weights (sum = 1.0)
W1_DUP = 0.25
W2_CONTRA = 0.35
W3_STALE = 0.20
W4_UNVERIFIED = 0.20

# Duplicate detection
DUP_JACCARD = 0.8   # cheap first pass: word-overlap >= this counts as a dup

# Semantic (embedding) second pass — Item 2. Jaccard misses cross-session
# paraphrase dups (independently authored bodies -> low word overlap). For pairs
# whose Jaccard lands in an AMBIGUOUS band we add an offline cosine-embedding
# check. The band is bounded so we only spend embeddings where they help: below
# _LO the bodies are unrelated word-wise, above _HI Jaccard is already near its
# own dup gate.
#
# Values below are Tony's evidence-based tuning from 11,175 measured pairs on the
# live corpus (shipped offline model / bge-small-en-v1.5):
#   * Real cross-session paraphrase dups all sit at Jaccard < 0.15, cosine
#     0.74-0.89 -> the OLD band [0.30, 0.60] was dead code (never entered by a
#     real dup). BAND_LO drops to 0.05, BAND_HI rises to 0.80 to cover them.
#   * Distinct-but-on-topic hard-negatives reach cosine up to 0.811, and the
#     overlap zone [0.74, 0.81] cannot be split by cosine alone. So the SCORED
#     hard-dup gate DUP_COSINE sits at 0.82 (FP=0 on Tony's labeled
#     hard-negatives; do NOT go below 0.812).
#
# Two-tier flag: pairs with DUP_REVIEW_COSINE <= cosine < DUP_COSINE (0.78-0.82)
# are REVIEW CANDIDATES — surfaced in the JSON for Tony but NOT counted as
# duplicates (they must not inflate dup_rate/entropy or auto-consolidate). Only
# cosine >= DUP_COSINE counts as a scored duplicate. DUP_COSINE stays distinct
# from (and lower than) reinforce_memory's 0.85 auto-absorb threshold: entropy
# only FLAGS pairs for Tony's review (reversible).
DUP_SEM_BAND_LO = 0.05
DUP_SEM_BAND_HI = 0.8
DUP_COSINE = 0.82
DUP_REVIEW_COSINE = 0.78

# --- Item 6: charter/axiom source class -------------------------------------
# Charter memories are install-seeded architectural axioms (true by
# construction), NOT transcript-sourced claims — so unverified_rate must NOT
# count them (they can never carry a traceable [session#line] source). A memory
# is charter-class when it self-declares charter provenance (`provenance:
# charter` frontmatter OR a `charter:<slug>` source) AND its id is in the
# blessed seed set. A NON-blessed memory that merely self-declares charter is
# an abuse attempt — it is NOT excluded (still counted unverified) and is
# surfaced separately as `suspicious_charter_ids`. The seed set lives in ONE
# shared place (charter_ids.py, same dir) since Phase 4 Item 1 — verify_memory,
# entropy, and decay all import it (hard import; the sibling always ships here).
from charter_ids import (CHARTER_SEED_IDS,
                         self_declares_charter as _shared_self_declares_charter)

# --- Phase 6 Item 1: tombstone vocabulary --------------------------------------
# The set of statuses that mark a memory as OUT of the active set lives in ONE
# shared place (tombstone.py, same dir) so it can never drift across scanners
# again. `absorbed` (written by the consolidation agent when it merges a dup into
# a canonical) is a tombstone alongside `archived`/`defunct`.
from tombstone import TOMBSTONE_STATUSES, is_tombstoned

# Phase 11: the fragile frontmatter PARSING SEAM is the ONE shared module
# (frontmatter.py, same dir). This ALSO fixed entropy's long-standing bug: the old
# inline parser gated fences on `startswith('---')`, so it accepted a malformed
# `---xyz` opener and TRUNCATED frontmatter at any body line beginning with `---`
# (e.g. a `----` markdown rule), classifying a memory differently from every other
# scanner. The shared `.strip() == '---'` delimiter fixes it. entropy keeps its OWN
# sources-as-list conversion, defunct->archived, and 6-key defaults layered on top.
from frontmatter import (split as _fm_split, parse as _fm_parse,
                         serialize as _fm_serialize,
                         SOURCE_ITEM_RE, tokenize_sources)

# Phase 28 Item 4a (D4): the file WALK is now the shared corpus.py primitive
# (same directory, same best-effort-import discipline) — one implementation
# instead of six independently re-implemented rglob loops (the audit's
# "phantom dedup / silent skip" drift class: a memory visible to one stage's
# walk and invisible to another).
import corpus

# Decay thresholds (must match decay.py)
HL_BASE = 7.0
HL_GROWTH = 0.5
L0_DROP_THRESHOLD = 0.25
L1_ARCHIVE_THRESHOLD = 0.15
L1_DEMOTE_RC = 2
L0_TO_L1_RC = 2
L1_TO_L2_RC = 4

# Contradiction detection keywords
# Curated opposing single-word pairs. Kept strong/intent-bearing only:
# over-generic words (is/not/yes/no/true/false) cause false positives, and
# multi-word phrases never match the single-token body set, so both are dropped.
POSITIVE_KEYWORDS = {
    "like", "prefer", "want", "always", "can",
    "allow", "support", "enable", "async",
}
NEGATIVE_KEYWORDS = {
    "dislike", "avoid", "reject", "never", "cannot",
    "forbid", "oppose", "disable", "sync",
}

# ============================================================================
# Frontmatter Parser (shared with decay.py pattern)
# ============================================================================

def parse_frontmatter(text):
    """
    Minimal YAML-like frontmatter parser.
    Extracts key: value pairs between --- markers.
    Returns dict with safe defaults for missing fields.

    Phase 11: the delimiter + key:value split now goes through the shared
    `frontmatter.parse` (the correct `.strip()=='---'` fence — see the module
    import note above for the bug this fixes). entropy keeps its own layer on
    top: `sources` is stored as a parsed LIST (quotes stripped) rather than the
    raw string, `defunct` is normalised to `archived`, and the 6 defaults are
    injected. No-frontmatter -> `{}` (no defaults), exactly as before.
    """
    fm_raw, _body = _fm_parse(text)
    if not fm_raw:
        return {}

    result = dict(fm_raw)

    # entropy represents `sources` as a parsed list (quotes stripped), not the
    # raw `[...]` string — keep that representation via its own array parser.
    if 'sources' in result:
        result['sources'] = _parse_sources_array(result['sources'])

    # `defunct` is a legacy alias for `archived` (the daily agent writes it
    # when its sandboxed `rm` can't delete a merged-away stub). Mirror
    # decay.py's on-read migration so both scanners agree on the active set:
    # without this, defunct stubs kept counting as live memories — inflating
    # total_memories and every rate — for the whole reap grace window, so a
    # completed merge didn't lower measured entropy until reap. (Phase 4 #5)
    if result.get('status') == 'defunct':
        result['status'] = 'archived'

    # Defaults
    result.setdefault('tier', 'L0')
    result.setdefault('owner', 'Tony')
    result.setdefault('status', 'active')
    result.setdefault('sources', [])
    result.setdefault('reinforce_count', 1)
    result.setdefault('decay_score', 1.0)

    return result

def _parse_sources_array(s):
    """
    Parse sources: [a, b, c] → ['a', 'b', 'c']
    Handles simple square bracket arrays; malformed → []
    """
    s = s.strip()
    if not (s.startswith('[') and s.endswith(']')):
        return []
    inner = s[1:-1].strip()
    if not inner:
        return []
    parts = inner.split(',')
    return [p.strip().strip('"\'') for p in parts if p.strip()]

# ============================================================================
# Decay Score Computation (shared with decay.py)
# ============================================================================

def compute_decay_score(age_days, rc):
    """
    Compute decay_score = 0.5 ** (age_days / half_life(rc))
    where half_life(rc) = HL_BASE * (1 + HL_GROWTH * (rc - 1))
    """
    if rc < 1:
        rc = 1
    half_life = HL_BASE * (1.0 + HL_GROWTH * (rc - 1))
    decay = 0.5 ** (age_days / half_life)
    return max(0.0, min(1.0, decay))

def get_decay_threshold(tier):
    """Return decay_score threshold below which memory is considered stale."""
    if tier == 'L0':
        return L0_DROP_THRESHOLD
    elif tier == 'L1':
        return L1_ARCHIVE_THRESHOLD
    else:  # L2
        return None  # L2 never stales

# ============================================================================
# Item N: O(n) Sources-Array Grouping (exact/overlap pre-filter)
# ============================================================================

def group_by_sources(memories):
    """
    O(n) pre-filter: group memories by exact sources match (or single-source overlap).
    For multi-source entries, check if they share at least one common source.

    Returns a dict: sources_key -> list of memory ids in that group.
    Only groups of size >= 2 are included (>=2 is a candidate condition).

    The key is constructed as a canonicalized sources string:
      - Empty sources -> empty string (canonical)
      - Single source -> that source string
      - Multiple sources -> sorted unique sources joined (e.g., "[#58,#125]")

    This is advisory only — never auto-merges.
    """
    source_groups = {}

    for mem in memories:
        sources = mem.get('sources', []) or []
        if not sources:
            # Empty sources -> group them separately
            key = ''
        elif len(sources) == 1:
            key = sources[0]
        else:
            # Multi-source: canonical key is sorted unique sources
            key = ','.join(sorted(set(sources)))

        if key not in source_groups:
            source_groups[key] = []
        source_groups[key].append(mem['id'])

    # Return only groups of size >= 2 (candidate condition)
    return {k: v for k, v in source_groups.items() if len(v) >= 2}


def compute_sources_overlap_candidates(memories):
    """
    Find candidate duplicate groups via sources-array exact or subset matching.

    Two memories are candidates if:
      1. They share at least one exact source (e.g., both have "[#123]")
      2. One source array is a subset of the other (e.g., [#58] subset of [#58, #125])

    Returns: list of candidate groups [{'members': [id_a, id_b, ...], 'shared_sources': [...]}]
    with group size >= 2. Emitted in advisory JSON only, never counts toward scoring.
    """
    exact_groups = group_by_sources(memories)

    # Build a map: id -> sources for fast lookup
    id_to_sources = {mem['id']: set(mem.get('sources', []) or [])
                     for mem in memories}

    candidates = []
    processed = set()

    # 1. Exact-match groups (same sources key)
    for sources_key, ids in exact_groups.items():
        # Skip empty-source groups (sources_key == '')
        if not sources_key:
            continue
        if len(ids) < 2:
            continue
        group_tuple = tuple(sorted(ids))
        if group_tuple in processed:
            continue
        processed.add(group_tuple)

        # Compute shared sources for this group
        shared = set(sources_key.split(',')) if ',' in sources_key else {sources_key}

        candidates.append({
            'members': sorted(ids),
            'shared_sources': sorted(list(shared)) if shared else [],
            'match_type': 'exact'
        })

    # 2. Subset-match: for each pair (i, j) with i < j, check if one's sources
    #    are a subset of the other's (O(n^2) pair check, but only on groups
    #    that haven't been exact-matched already).
    n = len(memories)
    for i in range(n):
        for j in range(i+1, n):
            id1, id2 = memories[i]['id'], memories[j]['id']
            pair_tuple = tuple(sorted([id1, id2]))

            if pair_tuple in processed:
                continue

            src1 = id_to_sources.get(id1, set())
            src2 = id_to_sources.get(id2, set())

            # Check for subset relationship: one is a subset of the other
            shared = src1 & src2
            if shared and (src1 < src2 or src2 < src1):
                # One is a proper subset of the other
                processed.add(pair_tuple)
                candidates.append({
                    'members': sorted([id1, id2]),
                    'shared_sources': sorted(list(shared)),
                    'match_type': 'subset'
                })

    return candidates

# ============================================================================
# Jaccard Similarity (for duplicate detection)
# ============================================================================

def normalize_text(text):
    """Normalize for similarity: lowercase, remove punctuation, collapse spaces."""
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)  # Remove punctuation
    text = re.sub(r'\s+', ' ', text).strip()  # Collapse whitespace
    return text

def jaccard_similarity(text1, text2):
    """
    Compute Jaccard similarity between two texts using word tokens.
    Returns 0.0-1.0.
    """
    norm1 = normalize_text(text1)
    norm2 = normalize_text(text2)

    if not norm1 or not norm2:
        return 0.0

    words1 = set(norm1.split())
    words2 = set(norm2.split())

    if not words1 or not words2:
        return 0.0

    intersection = len(words1 & words2)
    union = len(words1 | words2)

    if union == 0:
        return 0.0

    return intersection / union

# ============================================================================
# Memory Loading & Extraction
# ============================================================================

def load_memories(memory_dir, include_archived=False):
    """
    Load all markdown files from memory_dir.
    Returns: list of (path, id, frontmatter, body) tuples.
    """
    memories = []

    # Phase 28 Item 4a: the walk is corpus.iter_memory_paths (shared with
    # decay/reinforce/rag_index) instead of entropy's own rglob — one file
    # enumeration everyone agrees on. entropy keeps its OWN parse_frontmatter
    # (sources-as-list, defunct->archived, 6-key defaults) and gating ORDER
    # (tombstone check, then id check) exactly as before; only the walk moves.
    # `sort=False` (behaviour-preservation, Phase 28): entropy's legacy walk
    # was a BARE UNSORTED `rglob`, and compute_dup_rate's i<j pairwise scan
    # makes that raw order load-bearing for the ORDER of duplicate_pairs /
    # review_candidates it emits (positional consumers: elon_survey dups[:4],
    # daily-run's duplicate-candidates line + agent backlog pairs[:15]).
    # Sorting would change today's byte output — the other five callers already
    # sorted, so only entropy takes the raw path.
    for md_file in corpus.iter_memory_paths(memory_dir, sort=False):
        try:
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read()

            fm = parse_frontmatter(content)

            # Skip tombstones unless requested. The tombstone vocabulary
            # (archived / defunct / absorbed) lives in the shared tombstone
            # module so it can't drift: `defunct` is also normalised to
            # `archived` by parse_frontmatter (decay.py parity), and `absorbed`
            # (a consolidation-agent merge) is excluded here too — all included
            # by --include-archived.
            if not include_archived and is_tombstoned(fm):
                continue

            # Extract body (everything after the closing fence, via the SAME
            # shared split every other consumer uses now — Phase 28 Item 4a).
            # This replaces a substring `content.find('---', 3)` search that
            # could truncate early if a frontmatter VALUE happened to contain
            # the literal text "---" before the real closing fence line; the
            # line-based fence detection cannot mis-fire that way, and aligning
            # this body value with reinforce/rag_index's is the Item 2
            # embedding-cache hit-rate prerequisite (same body -> same hash).
            _, body_raw = _fm_parse(content)
            body = body_raw.strip() if isinstance(body_raw, str) else content.strip()

            # Require an explicit id, mirroring decay.py (which warns + skips on
            # missing id). Keeping the two scanners on the same memory set means
            # entropy's total_memories matches decay's scanned count.
            mem_id = fm.get('id')
            if not mem_id:
                print(f"Warning: {md_file} missing id, skipping", file=sys.stderr)
                continue

            memories.append({
                'path': str(md_file),
                'id': mem_id,
                'tier': fm.get('tier', 'L0'),
                'status': fm.get('status', 'active'),
                'sources': fm.get('sources', []),
                'provenance': fm.get('provenance'),  # Item 6: charter/axiom class
                'verified_date': fm.get('verified_date'),
                'reinforce_count': int(fm.get('reinforce_count', 1)),
                'last_reinforced': fm.get('last_reinforced', ''),
                'decay_score': float(fm.get('decay_score', 1.0)),
                'body': body,
            })
        except Exception as e:
            print(f"Warning: Failed to parse {md_file}: {e}", file=sys.stderr)

    return memories

# ============================================================================
# Item 7: Adjudication ledger (persisted verdicts on candidate pairs)
# ============================================================================

def load_adjudications(path):
    """
    Parse the adjudication ledger — a markdown table at .company/ops/
    adjudications.md keyed by the UNORDERED pair (id_a, id_b):

        | id_a | id_b | verdict | by | date | reason |
        |------|------|---------|----|------|--------|
        | foo  | bar  | distinct| Elon | 2026-07-03 | ... |

    Returns a dict keyed by the sorted-tuple pair -> {verdict, by, date, reason}.
    Only rows whose verdict is a recognised value ({distinct, duplicate}) are
    kept; the header and `---` separator rows are ignored. Missing file -> {};
    never raises (a malformed ledger degrades to "no adjudications", never a
    hard fail). Stale-guard is implicit: an entry for ids that no longer exist
    simply never matches a live pair, so it is inert (ignored), not an error.
    """
    records = {}
    try:
        p = Path(path)
        if not p.exists():
            return records
        for raw in p.read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if not line.startswith('|'):
                continue
            cells = [c.strip() for c in line.strip('|').split('|')]
            if len(cells) < 3:
                continue
            a, b, verdict = cells[0], cells[1], cells[2].lower()
            if verdict not in ('distinct', 'duplicate'):
                continue  # skips header ("verdict") and separator ("---") rows
            if not a or not b:
                continue
            key = tuple(sorted([a, b]))
            records[key] = {
                'verdict': verdict,
                'by': cells[3] if len(cells) > 3 else '',
                'date': cells[4] if len(cells) > 4 else '',
                'reason': cells[5] if len(cells) > 5 else '',
            }
    except Exception as e:  # pragma: no cover - defensive
        print(f"[entropy] adjudication ledger skipped ({e})", file=sys.stderr)
    return records


def distinct_pairs_from(adjudications):
    """Set of sorted-tuple pairs adjudicated `distinct` (a false-positive pair).
    Kept for the JSON provenance count; suppression uses `suppressed_pairs_from`
    (which ALSO covers `duplicate` verdicts) — see below."""
    return {k for k, v in adjudications.items() if v['verdict'] == 'distinct'}


def duplicate_pairs_from(adjudications):
    """Set of sorted-tuple pairs adjudicated `duplicate` (Phase 6 Item 3). A
    `duplicate` verdict means "already judged — being resolved via
    tombstone/reap", so entropy must stop re-flagging it (else it re-surfaces in
    scored dups + review candidates and re-inflates dup_rate every run until the
    reap lands)."""
    return {k for k, v in adjudications.items() if v['verdict'] == 'duplicate'}


def suppressed_pairs_from(adjudications):
    """Phase 6 Item 3: the set of pairs entropy omits from surfaced candidate
    lists AND from the dup_rate / contradiction counts — pairs adjudicated
    EITHER `distinct` (a false positive) OR `duplicate` (already judged, being
    resolved via tombstone/reap). Stale-guard is applied downstream: a pair
    whose ids are absent from the live memory set simply never matches, so it is
    inert."""
    return distinct_pairs_from(adjudications) | duplicate_pairs_from(adjudications)


# ============================================================================
# Entropy Dimensions
# ============================================================================

def _semantic_cosines(memories, band_candidates):
    """
    Cosine similarity for each (i, j) index pair in band_candidates, using the
    shared offline embedding backend (rag_embed / bge-small-en-v1.5, 384-dim) —
    the SAME model reinforce_memory.py uses. Only the memories that actually
    appear in a band candidate are embedded, keeping the second pass cheap. No
    network. Returns a list of cosines aligned with band_candidates.
    """
    idxs = sorted({k for pair in band_candidates for k in pair})
    bodies = [memories[k]['body'] or memories[k]['id'] for k in idxs]
    vecs = np.array(rag_embed.embed_batch(bodies), dtype="float32")
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = vecs / norms
    row = {k: r for k, r in zip(idxs, unit)}
    return [float(row[i] @ row[j]) for i, j in band_candidates]


def compute_dup_rate(memories, suppressed_pairs=None):
    """
    Find approximate duplicates in two passes:
      1. Jaccard (cheap, always): word-overlap >= DUP_JACCARD -> duplicate.
      2. Semantic (optional): pairs whose Jaccard lands in the ambiguous band
         [DUP_SEM_BAND_LO, DUP_SEM_BAND_HI] get an offline cosine-embedding
         re-check, resolved in two tiers:
           * cosine >= DUP_COSINE      -> SCORED duplicate (counts in dup_rate,
                                          appears in the returned `pairs`).
           * DUP_REVIEW_COSINE <= cosine < DUP_COSINE -> REVIEW CANDIDATE:
                                          surfaced in _SEMANTIC_META
                                          ['review_candidates'] for Tony but NOT
                                          counted (never inflates dup_rate, never
                                          in `pairs`, never auto-consolidates).
         This catches cross-session paraphrase dups (same meaning, different
         words) that Jaccard alone misses, while keeping the scored gate
         precision-safe against on-topic hard-negatives.

    Graceful fallback: when the RAG backend (fastembed/numpy) is absent, the
    embedding pass is skipped and behaviour is IDENTICAL to Jaccard-only. The
    outcome is recorded in the module-level _SEMANTIC_META (surfaced in JSON)
    and a one-line notice goes to stderr — never a hard failure.

    Item 7 + Phase 6 Item 3: `suppressed_pairs` (a set of sorted-tuple id pairs
    adjudicated EITHER `distinct` OR `duplicate`) is honoured — such a pair is
    NEVER surfaced as a duplicate or review candidate and NEVER counted in
    dup_rate. A `duplicate` verdict means "already judged, being resolved via
    tombstone/reap" so it must stop re-flagging. Stale-guard: a pair whose ids
    are absent from `memories` simply never matches, so it is inert.

    Returns: (dup_rate, duplicate_pairs)   [contract unchanged; review
    candidates are carried out-of-band via _SEMANTIC_META so they never touch
    the (dup_rate, pairs) contract]
    """
    global _SEMANTIC_META
    suppressed_pairs = suppressed_pairs or set()
    pairs = []
    review_candidates = []  # {'id_a','id_b','cosine'}; NOT scored duplicates
    n = len(memories)

    if n < 2:
        _SEMANTIC_META = {'pass': 'jaccard-only', 'reason': 'fewer than 2 memories',
                          'band_candidates': 0, 'embedding_pairs': 0,
                          'review_candidates': []}
        return 0.0, pairs

    seen_pairs = set()
    band_candidates = []  # (i, j) index pairs to re-check with embeddings
    for i in range(n):
        for j in range(i+1, n):
            id1, id2 = memories[i]['id'], memories[j]['id']
            pair_key = tuple(sorted([id1, id2]))

            if pair_key in seen_pairs:
                continue
            # Item 7 + Phase 6 Item 3: adjudicated pairs (distinct OR duplicate)
            # are never candidates and never counted — skip before any scoring
            # (covers Jaccard, the embedding band, and review candidates in one
            # place).
            if pair_key in suppressed_pairs:
                continue

            sim = jaccard_similarity(memories[i]['body'], memories[j]['body'])
            if sim >= DUP_JACCARD:
                pairs.append([id1, id2])
                seen_pairs.add(pair_key)
            elif DUP_SEM_BAND_LO <= sim <= DUP_SEM_BAND_HI:
                band_candidates.append((i, j))

    # Pass 2: semantic embedding check on the ambiguous band only.
    if not band_candidates:
        _SEMANTIC_META = {'pass': 'jaccard+embedding', 'reason': 'no in-band candidates',
                          'band_candidates': 0, 'embedding_pairs': 0,
                          'cosine_threshold': DUP_COSINE,
                          'review_cosine_threshold': DUP_REVIEW_COSINE,
                          'review_candidates': []}
    elif not _HAS_RAG:
        # C1: name the actual trigger — SC_NO_RAG force-disable vs venv truly
        # absent. (Runtime backend failures get their own message below.)
        why = ('force-disabled via SC_NO_RAG' if os.environ.get('SC_NO_RAG')
               else 'RAG venv absent')
        notice = f'embedding pass skipped ({why})'
        print(f"[entropy] {notice}", file=sys.stderr)
        _SEMANTIC_META = {'pass': 'jaccard-only', 'reason': notice,
                          'band_candidates': len(band_candidates), 'embedding_pairs': 0,
                          'review_candidates': []}
    else:
        # Import succeeded, but embedding can still fail at runtime (e.g. model
        # not cached). Degrade to Jaccard-only rather than hard-fail or hit the
        # network — same guarantee as the venv-absent branch.
        try:
            cosines = _semantic_cosines(memories, band_candidates)
        except Exception as e:
            notice = f'embedding pass skipped (backend error: {e})'
            print(f"[entropy] {notice}", file=sys.stderr)
            _SEMANTIC_META = {'pass': 'jaccard-only', 'reason': notice,
                              'band_candidates': len(band_candidates), 'embedding_pairs': 0,
                              'review_candidates': []}
            cosines = None
        if cosines is not None:
            embedding_pairs = 0
            for (i, j), cos in zip(band_candidates, cosines):
                id1, id2 = memories[i]['id'], memories[j]['id']
                pair_key = tuple(sorted([id1, id2]))
                if pair_key in seen_pairs:
                    continue
                if cos >= DUP_COSINE:
                    # Tier 1: SCORED duplicate — counts in dup_rate + pairs.
                    pairs.append([id1, id2])
                    seen_pairs.add(pair_key)
                    embedding_pairs += 1
                elif cos >= DUP_REVIEW_COSINE:
                    # Tier 2: REVIEW CANDIDATE — surfaced for Tony only. Do NOT
                    # add to seen_pairs/pairs and do NOT count toward dup_rate.
                    review_candidates.append(
                        {'id_a': id1, 'id_b': id2, 'cosine': round(cos, 4)})
            _SEMANTIC_META = {'pass': 'jaccard+embedding', 'reason': 'ok',
                              'band_candidates': len(band_candidates),
                              'embedding_pairs': embedding_pairs,
                              'cosine_threshold': DUP_COSINE,
                              'review_cosine_threshold': DUP_REVIEW_COSINE,
                              'review_candidates': review_candidates}

    dup_rate = len(pairs) / max(1, n)
    dup_rate = min(1.0, dup_rate)

    return dup_rate, pairs

# Tier prefixes that are NOT a topic slug. An ID like 'l0-pref-async-001' or
# 'pref-async-001' should both yield the topic family 'pref', never the tier.
_TIER_PREFIXES = {"l0", "l1", "l2"}

def slug_family_prefix(mem_id):
    """
    Extract the topic-slug family from a memory ID, ignoring any leading tier
    token (l0/l1/l2). This prevents every same-tier memory from being lumped
    into one giant false-positive "family".

    ID naming convention (see memory-tiers.md §6):
      '{topic}-{...}-{seq}'            -> family = topic
      'l{0,1,2}-{topic}-{...}-{seq}'   -> family = topic (tier token skipped)

    Examples:
      'pref-async-001'    -> 'pref'
      'l1-pref-sync-001'  -> 'pref'
      'l0-001'            -> 'l0-001' (no topic token; fall back to full id)
    """
    if not mem_id:
        return mem_id
    parts = mem_id.split('-')
    # Drop a leading tier token if a topic token follows it.
    if len(parts) >= 2 and parts[0].lower() in _TIER_PREFIXES:
        parts = parts[1:]
    if not parts or not parts[0]:
        return mem_id
    # Family = first remaining token, unless it's purely numeric (no real slug).
    if parts[0].isdigit():
        return mem_id
    return parts[0]

def compute_contradiction_score(memories, suppressed_pairs=None):
    """
    Detect contradictions: two memories about the same topic (same slug
    family OR Jaccard 0.5-0.8) that ALSO contain opposing keywords. Opposing
    keywords are required — sharing a topic without opposition is not a conflict.

    Item 7 + Phase 6 Item 3: `suppressed_pairs` (sorted-tuple id pairs
    adjudicated EITHER `distinct` OR `duplicate`) are omitted from the surfaced
    contradiction candidate list and do not count in the score. Stale-guard:
    pairs referencing absent ids simply never match.

    Returns: (contradiction_score, contradiction_pairs)
    """
    suppressed_pairs = suppressed_pairs or set()
    pairs = []
    n = len(memories)

    if n < 2:
        return 0.0, pairs

    seen_pairs = set()

    for i in range(n):
        for j in range(i+1, n):
            id1, id2 = memories[i]['id'], memories[j]['id']
            pair_key = tuple(sorted([id1, id2]))

            if pair_key in seen_pairs:
                continue
            if pair_key in suppressed_pairs:
                continue  # adjudicated (distinct/duplicate) — never a candidate

            # Same slug family (tier-aware prefix) OR topical similarity only
            # tells us two memories are ABOUT the same thing — that is necessary
            # context, not a contradiction by itself. Two same-topic memories
            # that AGREE are not in conflict. A real contradiction additionally
            # needs OPPOSING keywords (like/dislike, want/avoid), so we gate on
            # has_keywords rather than treating slug family alone as a conflict.
            prefix1 = slug_family_prefix(id1)
            prefix2 = slug_family_prefix(id2)
            is_family = (prefix1 == prefix2)

            sim = jaccard_similarity(memories[i]['body'], memories[j]['body'])
            same_topic = is_family or (0.5 <= sim <= 0.8)

            has_keywords = _has_opposing_keywords(memories[i]['body'], memories[j]['body'])

            if same_topic and has_keywords:
                pairs.append([id1, id2])
                seen_pairs.add(pair_key)

    contra_score = len(pairs) / max(1, n)
    contra_score = min(1.0, contra_score)

    return contra_score, pairs


def _safe_last_reinforced_date(raw):
    """Parse a `last_reinforced` frontmatter value as a date. Missing/malformed
    values (not a str, or not `%Y-%m-%d`) return None — the caller treats None
    as the OLDEST possible date, so it never outranks a real one."""
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _safe_reinforce_count(raw):
    """Parse a `reinforce_count` frontmatter value as an int. Missing/malformed
    values degrade to 0 — the caller treats 0 as the lowest possible count, so
    it never outranks a real one."""
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


def compute_contradiction_recommendations(memories, contra_pairs):
    """
    Mike (R&D) 2026-07-18 Finding 1: for each contradiction pair ALREADY
    detected by compute_contradiction_score, compute a deterministic,
    ADVISORY recommendation from metadata already on disk
    (`last_reinforced`, `reinforce_count`) — pure comparison, no LLM call, no
    network, no new dependency. It does NOT change what gets detected or
    scored (that stays entirely inside compute_contradiction_score); this is
    an additive suggestion Tony can accept, override, or ignore. Tony still
    adjudicates every pair by hand — in particular the L2 "contradiction
    update" path deliberately KEEPS BOTH records, which this function has no
    say over (it only ever names a `recommend` starting point, never mutates
    or auto-resolves anything).

    Rule (research: LLM-judged freshness resolution underperforms a
    deterministic metadata rule on conflict-resolution tasks — see
    references/pipeline.md and CHANGELOG for the citation):
      1. Prefer the memory with the more recent `last_reinforced` (max date).
      2. Tie on date -> prefer the higher `reinforce_count`.
      3. Still tied (including both dates unparseable AND both counts equal)
         -> no recommendation (`recommend: None`, `basis: "tie"`).
    Missing/malformed `last_reinforced` is treated as the OLDEST possible
    date (never wins on date) and missing/malformed `reinforce_count` as 0
    (never wins on count) — malformed metadata degrades cleanly rather than
    raising or silently winning.

    Returns a list aligned 1:1 with `contra_pairs`, each entry:
      {"pair": [id1, id2], "recommend": "<winner_id>" | None, "basis": "<str>"}
    """
    by_id = {m['id']: m for m in memories}
    recommendations = []

    for id1, id2 in contra_pairs:
        m1, m2 = by_id.get(id1), by_id.get(id2)
        if m1 is None or m2 is None:
            # Stale-guard: shouldn't happen (ids come straight from
            # `memories`), but degrade to no-recommendation rather than raise.
            recommendations.append({'pair': [id1, id2], 'recommend': None,
                                    'basis': 'missing memory data'})
            continue

        d1 = _safe_last_reinforced_date(m1.get('last_reinforced'))
        d2 = _safe_last_reinforced_date(m2.get('last_reinforced'))
        eff1 = d1 if d1 is not None else date.min
        eff2 = d2 if d2 is not None else date.min
        disp1 = d1.isoformat() if d1 is not None else 'unparseable'
        disp2 = d2.isoformat() if d2 is not None else 'unparseable'

        if eff1 > eff2:
            recommendations.append({'pair': [id1, id2], 'recommend': id1,
                                    'basis': f'last_reinforced {disp1} > {disp2}'})
        elif eff2 > eff1:
            recommendations.append({'pair': [id1, id2], 'recommend': id2,
                                    'basis': f'last_reinforced {disp2} > {disp1}'})
        else:
            rc1 = _safe_reinforce_count(m1.get('reinforce_count'))
            rc2 = _safe_reinforce_count(m2.get('reinforce_count'))
            if rc1 > rc2:
                recommendations.append({'pair': [id1, id2], 'recommend': id1,
                                        'basis': f'last_reinforced tied ({disp1}); '
                                                 f'reinforce_count {rc1} > {rc2}'})
            elif rc2 > rc1:
                recommendations.append({'pair': [id1, id2], 'recommend': id2,
                                        'basis': f'last_reinforced tied ({disp1}); '
                                                 f'reinforce_count {rc2} > {rc1}'})
            else:
                recommendations.append({'pair': [id1, id2], 'recommend': None,
                                        'basis': 'tie'})

    return recommendations


def _has_opposing_keywords(text1, text2):
    """
    Check if text1 and text2 contain an opposing keyword pair, in EITHER
    direction: pos in text1 & neg in text2, OR neg in text1 & pos in text2.
    A contradiction is symmetric, so a one-directional test would miss the
    case where the negative phrasing happens to land in the first text.
    """
    words1_low = normalize_text(text1).split()
    words2_low = normalize_text(text2).split()

    pos_norm = {normalize_text(kw) for kw in POSITIVE_KEYWORDS}
    neg_norm = {normalize_text(kw) for kw in NEGATIVE_KEYWORDS}

    w1, w2 = set(words1_low), set(words2_low)

    for pos_kw in pos_norm:
        if pos_kw in w1 and (neg_norm & w2):
            return True
        if pos_kw in w2 and (neg_norm & w1):
            return True

    return False

def compute_stale_rate(memories, now_date):
    """
    Compute fraction of active memories with decay_score below their tier threshold.
    L2 never stales. Returns: (stale_rate, stale_ids)
    """
    stale_ids = []
    active_count = 0

    for mem in memories:
        if mem['status'] != 'active':
            continue

        active_count += 1

        tier = mem['tier']
        threshold = get_decay_threshold(tier)

        if threshold is None:  # L2
            continue

        # Compute current decay score
        try:
            last_reinforced = datetime.strptime(mem['last_reinforced'], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            last_reinforced = now_date

        age_days = (now_date - last_reinforced).days
        decay_score = compute_decay_score(float(age_days), mem['reinforce_count'])

        if decay_score < threshold:
            stale_ids.append(mem['id'])

    stale_rate = len(stale_ids) / max(1, active_count) if active_count > 0 else 0.0
    stale_rate = min(1.0, stale_rate)

    return stale_rate, stale_ids

def _self_declares_charter(mem):
    """True if a memory SELF-DECLARES charter provenance — via a
    `provenance: charter` frontmatter key OR any `charter:<slug>` source. Says
    nothing about trust; the blessed-set gate below decides that.
    Delegates to the shared charter_ids helper when it imported; the inline
    fallback is behaviour-identical for entropy's parsed-list `sources`."""
    if _shared_self_declares_charter is not None:
        return _shared_self_declares_charter(mem)
    prov = str(mem.get('provenance') or '').strip().lower()
    if prov == 'charter':
        return True
    for s in mem.get('sources', []) or []:
        if str(s).strip().startswith('charter:'):
            return True
    return False


def is_charter_memory(mem):
    """A memory is HONOURED as charter-class only when it self-declares charter
    provenance AND its id is in the blessed install-seed set. Non-blessed
    charter claims are NOT charter-class (they stay ordinary claims)."""
    return _self_declares_charter(mem) and mem['id'] in CHARTER_SEED_IDS


def find_suspicious_charter(memories):
    """Item 6 anti-abuse: NON-blessed memories that self-declare charter. These
    are surfaced (never trusted, never excluded from unverified_rate)."""
    return [m['id'] for m in memories
            if _self_declares_charter(m) and m['id'] not in CHARTER_SEED_IDS]


def compute_unverified_rate(memories):
    """
    Fraction of memories the VERIFY loop has NOT confirmed.

    A memory is unverified if it lacks a `verified_date` (VERIFY never stamped it)
    OR has empty/missing sources (it can never be verified). The old definition
    only checked empty sources — but CAPTURE always writes a non-empty source
    placeholder, so that metric was structurally pinned at 0.0 and the KPI reported
    a perfect score for a stage (VERIFY) that had never run once. Counting
    verified_date makes the KPI reflect whether provenance was actually confirmed.

    Item 6: charter/axiom-class memories (blessed install seeds) are EXCLUDED
    entirely — from both numerator and denominator. They are axioms true by
    construction, not claims needing a transcript, so counting them as
    "unverified" permanently pinned the KPI's largest component. A non-blessed
    memory that merely self-declares charter is NOT excluded (anti-abuse: it is
    still counted here and separately flagged as suspicious).

    Returns: (unverified_rate, unverified_ids)
    """
    unverified_ids = []
    considered = 0

    for mem in memories:
        if is_charter_memory(mem):
            continue  # axiom, not a claim — excluded from the rate entirely
        considered += 1
        sources = mem.get('sources', [])
        has_sources = bool(sources) and not (isinstance(sources, list) and len(sources) == 0)
        verified = bool(mem.get('verified_date'))
        if not has_sources or not verified:
            unverified_ids.append(mem['id'])

    unverified_rate = len(unverified_ids) / max(1, considered) if considered else 0.0
    unverified_rate = min(1.0, unverified_rate)

    return unverified_rate, unverified_ids

# ============================================================================
# Policy Loading (optional)
# ============================================================================

def load_policy_constants(policy_path):
    """
    Extract tunable constants from policy.md via the shared policy_config loader,
    remapped to entropy's internal weight keys (w1->W1_DUP, ...).

    Returns a dict with only the constants found; missing file / constant ->
    omitted (caller keeps its default). Never raises.
    """
    if _shared_load_policy is None:
        return {}
    raw = _shared_load_policy(policy_path)
    keymap = {
        'w1': 'W1_DUP', 'w2': 'W2_CONTRA',
        'w3': 'W3_STALE', 'w4': 'W4_UNVERIFIED',
    }
    return {keymap.get(k, k): v for k, v in raw.items()}

# ============================================================================
# Main
# ============================================================================

def main():
    # Re-exec into the RAG venv (if present) so the embedding second pass can
    # run. Done here, at the direct-run entrypoint — NOT at import time — so that
    # `import entropy` from another program never replaces the caller's process.
    # SC_NO_RAG=1 suppresses this (jaccard-only, no re-exec).
    _reexec_into_rag_venv()

    parser = argparse.ArgumentParser(
        description='Measure entropy KPI across Chairman memory dimensions.'
    )
    parser.add_argument(
        '--memory-dir',
        default='.company/memory',
        help='Memory root directory (default: .company/memory)'
    )
    parser.add_argument(
        '--config',
        default='.company/org/policy.md',
        help='Policy file to load tunable constants (optional)'
    )
    parser.add_argument(
        '--now',
        default=None,
        help='Override current date (YYYY-MM-DD) for testing'
    )
    parser.add_argument(
        '--include-archived',
        action='store_true',
        help='Include archived memories in entropy calculation'
    )
    parser.add_argument(
        '--adjudications',
        default='.company/ops/adjudications.md',
        help='Adjudication ledger of judged pairs (Item 7). '
             'distinct-verdict pairs are omitted from candidates and not counted.'
    )

    args = parser.parse_args()

    # Determine current date
    if args.now:
        try:
            now_date = datetime.strptime(args.now, '%Y-%m-%d').date()
        except ValueError:
            print(f"Error: Invalid date format {args.now}, use YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
    else:
        now_date = date.today()

    # Load policy constants (overrides defaults)
    policy_config = load_policy_constants(args.config)

    global HL_BASE, HL_GROWTH, L0_DROP_THRESHOLD, L1_ARCHIVE_THRESHOLD
    global L1_DEMOTE_RC, L0_TO_L1_RC, L1_TO_L2_RC
    global W1_DUP, W2_CONTRA, W3_STALE, W4_UNVERIFIED, DUP_JACCARD
    global DUP_SEM_BAND_LO, DUP_SEM_BAND_HI, DUP_COSINE, DUP_REVIEW_COSINE

    if 'HL_BASE' in policy_config:
        HL_BASE = policy_config['HL_BASE']
    if 'HL_GROWTH' in policy_config:
        HL_GROWTH = policy_config['HL_GROWTH']
    if 'L0_DROP_THRESHOLD' in policy_config:
        L0_DROP_THRESHOLD = policy_config['L0_DROP_THRESHOLD']
    if 'L1_ARCHIVE_THRESHOLD' in policy_config:
        L1_ARCHIVE_THRESHOLD = policy_config['L1_ARCHIVE_THRESHOLD']
    if 'L1_DEMOTE_RC' in policy_config:
        L1_DEMOTE_RC = int(policy_config['L1_DEMOTE_RC'])
    if 'L0_TO_L1_RC' in policy_config:
        L0_TO_L1_RC = int(policy_config['L0_TO_L1_RC'])
    if 'L1_TO_L2_RC' in policy_config:
        L1_TO_L2_RC = int(policy_config['L1_TO_L2_RC'])
    if 'W1_DUP' in policy_config:
        W1_DUP = policy_config['W1_DUP']
    if 'W2_CONTRA' in policy_config:
        W2_CONTRA = policy_config['W2_CONTRA']
    if 'W3_STALE' in policy_config:
        W3_STALE = policy_config['W3_STALE']
    if 'W4_UNVERIFIED' in policy_config:
        W4_UNVERIFIED = policy_config['W4_UNVERIFIED']
    if 'DUP_JACCARD' in policy_config:
        DUP_JACCARD = policy_config['DUP_JACCARD']
    if 'DUP_SEM_BAND_LO' in policy_config:
        DUP_SEM_BAND_LO = policy_config['DUP_SEM_BAND_LO']
    if 'DUP_SEM_BAND_HI' in policy_config:
        DUP_SEM_BAND_HI = policy_config['DUP_SEM_BAND_HI']
    if 'DUP_COSINE' in policy_config:
        DUP_COSINE = policy_config['DUP_COSINE']
    if 'DUP_REVIEW_COSINE' in policy_config:
        DUP_REVIEW_COSINE = policy_config['DUP_REVIEW_COSINE']

    # P3: provenance — which constants came from policy vs built-in defaults.
    consumed = ['HL_BASE', 'HL_GROWTH', 'L0_DROP_THRESHOLD', 'L1_ARCHIVE_THRESHOLD',
                'L1_DEMOTE_RC', 'L0_TO_L1_RC', 'L1_TO_L2_RC',
                'W1_DUP', 'W2_CONTRA', 'W3_STALE', 'W4_UNVERIFIED', 'DUP_JACCARD',
                'DUP_SEM_BAND_LO', 'DUP_SEM_BAND_HI', 'DUP_COSINE', 'DUP_REVIEW_COSINE']
    config_sources = {k: ('policy' if k in policy_config else 'default') for k in consumed}
    config_exists = Path(args.config).exists() if args.config else False
    fell_back = sorted(k for k, s in config_sources.items() if s == 'default')
    if config_exists and fell_back:
        print(f"[WARN] {args.config}: using built-in defaults for "
              f"{', '.join(fell_back)} (not declared in policy)", file=sys.stderr)

    # Load memories
    memories = load_memories(args.memory_dir, include_archived=args.include_archived)

    # Item 7 + Phase 6 Item 3: adjudication ledger — pairs judged EITHER
    # `distinct` (false positive) OR `duplicate` (already judged, resolving via
    # tombstone/reap) are dropped from candidate lists and excluded from
    # dup_rate / contradiction_score.
    adjudications = load_adjudications(args.adjudications)
    distinct_pairs = distinct_pairs_from(adjudications)
    duplicate_pairs = duplicate_pairs_from(adjudications)
    suppressed_pairs = distinct_pairs | duplicate_pairs
    live_ids = {m['id'] for m in memories}
    applied_distinct = sorted(list(k) for k in distinct_pairs
                              if k[0] in live_ids and k[1] in live_ids)
    applied_suppressed = sorted(list(k) for k in suppressed_pairs
                                if k[0] in live_ids and k[1] in live_ids)

    # Item 6 anti-abuse: non-blessed memories self-declaring charter (suspicious).
    suspicious_charter_ids = find_suspicious_charter(memories)

    # Item N: sources-array grouping pre-filter (O(n), advisory only)
    sources_candidates = compute_sources_overlap_candidates(memories)

    # Compute dimensions
    dup_rate, dup_pairs = compute_dup_rate(memories, suppressed_pairs=suppressed_pairs)
    contra_score, contra_pairs = compute_contradiction_score(memories, suppressed_pairs=suppressed_pairs)
    # Mike 2026-07-18 Finding 1: advisory-only recommendation per detected
    # contradiction pair, computed from metadata already on disk. Does NOT
    # feed back into contra_score/contra_pairs above — those stay exactly as
    # compute_contradiction_score produced them.
    contra_recommendations = compute_contradiction_recommendations(memories, contra_pairs)
    stale_rate, stale_ids = compute_stale_rate(memories, now_date)
    unverified_rate, unverified_ids = compute_unverified_rate(memories)

    # Compute total entropy
    total_entropy = (
        W1_DUP * dup_rate +
        W2_CONTRA * contra_score +
        W3_STALE * stale_rate +
        W4_UNVERIFIED * unverified_rate
    )
    total_entropy = min(1.0, max(0.0, total_entropy))

    # Build output
    output = {
        'now': now_date.isoformat(),
        'memory_dir': args.memory_dir,
        'total_memories': len(memories),
        'dimensions': {
            'dup_rate': round(dup_rate, 4),
            'contradiction_score': round(contra_score, 4),
            'stale_rate': round(stale_rate, 4),
            'unverified_rate': round(unverified_rate, 4),
        },
        'weights': {
            'w1': round(W1_DUP, 4),
            'w2': round(W2_CONTRA, 4),
            'w3': round(W3_STALE, 4),
            'w4': round(W4_UNVERIFIED, 4),
        },
        'entropy': round(total_entropy, 4),
        'details': {
            'duplicate_pairs': dup_pairs,
            'contradiction_pairs': contra_pairs,
            # Mike 2026-07-18 Finding 1: advisory-only starting suggestion per
            # contradiction pair (last_reinforced/reinforce_count comparison,
            # no LLM). Tony still adjudicates every pair by hand; this never
            # auto-resolves/auto-merges/mutates anything.
            'contradiction_recommendations': contra_recommendations,
            'stale_ids': stale_ids,
            'unverified_ids': unverified_ids,
            # Item 6 anti-abuse: memories self-declaring charter without being a
            # blessed seed (surfaced, never trusted, never excluded above).
            'suspicious_charter_ids': suspicious_charter_ids,
            # Item N: O(n) sources-array grouping pre-filter (advisory, never scored)
            'sources_overlap_candidates': sources_candidates,
        },
        # Item 2: whether the semantic (embedding) dedup pass ran or fell back.
        'semantic_dedup': _SEMANTIC_META,
        # Item 7 + Phase 6 Item 3: adjudication ledger provenance — how many
        # distinct/duplicate-verdict pairs were loaded and which live pairs they
        # suppressed this run. `distinct_pairs`/`applied_distinct_pairs` kept for
        # back-compat; `duplicate_pairs`/`suppressed_pairs`/`applied_suppressed_pairs`
        # added (extend-not-break) so a `duplicate` verdict's effect is visible.
        'adjudications': {
            'source_file': args.adjudications if Path(args.adjudications).exists() else None,
            'distinct_pairs': len(distinct_pairs),
            'duplicate_pairs': len(duplicate_pairs),
            'suppressed_pairs': len(suppressed_pairs),
            'applied_distinct_pairs': applied_distinct,
            'applied_suppressed_pairs': applied_suppressed,
        },
        'config': {
            'source_file': args.config if config_exists else None,
            'sources': config_sources,
        }
    }

    # Output JSON
    print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
