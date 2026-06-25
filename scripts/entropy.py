#!/usr/bin/env python3
"""
Self-Company Entropy KPI Measurement Tool

Quantifies entropy across four dimensions:
  1. dup_rate: Approximate duplicate memories (Jaccard similarity)
  2. contradiction_score: Detected contradictions (slug family + opposing keywords)
  3. stale_rate: Expired memories (decay_score below tier thresholds)
  4. unverified_rate: Missing/empty sources

Formula:
  Entropy = w1*dup_rate + w2*contradiction_score + w3*stale_rate + w4*unverified_rate

Output: JSON with dimension scores, total entropy, and detailed candidate lists for review.

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

# Shared policy loader — single source of truth for tunable constants
# (reads org/policy.md §7). Best-effort import; falls back to built-in defaults.
try:
    from policy_config import load_policy_constants as _shared_load_policy
except Exception:  # pragma: no cover - defensive
    _shared_load_policy = None

# ============================================================================
# Constants (tunable, defaults == manifest §1)
# ============================================================================

# Entropy weights (sum = 1.0)
W1_DUP = 0.25
W2_CONTRA = 0.35
W3_STALE = 0.20
W4_UNVERIFIED = 0.20

# Duplicate detection
DUP_JACCARD = 0.8

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
    """
    lines = text.split('\n')
    if len(lines) < 2 or not lines[0].startswith('---'):
        return {}

    result = {}
    in_fm = False
    fm_lines = []

    for line in lines[1:]:
        if line.startswith('---'):
            in_fm = True
            break
        fm_lines.append(line)

    if not in_fm:
        return {}

    for line in fm_lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # Handle sources: [a, b] array syntax
        if line.startswith('sources:'):
            sources_str = line[8:].strip()
            result['sources'] = _parse_sources_array(sources_str)
        elif ':' in line:
            key, val = line.split(':', 1)
            result[key.strip()] = val.strip()

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
    memory_path = Path(memory_dir)

    if not memory_path.exists():
        return memories

    for md_file in memory_path.rglob('*.md'):
        try:
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read()

            fm = parse_frontmatter(content)

            # Skip archived unless requested
            if not include_archived and fm.get('status') == 'archived':
                continue

            # Extract body (after closing ---)
            body_start = content.find('---', 3)
            if body_start != -1:
                body = content[body_start+3:].strip()
            else:
                body = content

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
                'reinforce_count': int(fm.get('reinforce_count', 1)),
                'last_reinforced': fm.get('last_reinforced', ''),
                'decay_score': float(fm.get('decay_score', 1.0)),
                'body': body,
            })
        except Exception as e:
            print(f"Warning: Failed to parse {md_file}: {e}", file=sys.stderr)

    return memories

# ============================================================================
# Entropy Dimensions
# ============================================================================

def compute_dup_rate(memories):
    """
    Find approximate duplicates using Jaccard similarity >= DUP_JACCARD.
    Returns: (dup_rate, duplicate_pairs)
    """
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

            sim = jaccard_similarity(memories[i]['body'], memories[j]['body'])
            if sim >= DUP_JACCARD:
                pairs.append([id1, id2])
                seen_pairs.add(pair_key)

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

def compute_contradiction_score(memories):
    """
    Detect contradictions: two memories about the same topic (same slug
    family OR Jaccard 0.5-0.8) that ALSO contain opposing keywords. Opposing
    keywords are required — sharing a topic without opposition is not a conflict.
    Returns: (contradiction_score, contradiction_pairs)
    """
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

def compute_unverified_rate(memories):
    """
    Compute fraction of memories with empty/missing sources.
    Returns: (unverified_rate, unverified_ids)
    """
    unverified_ids = []

    for mem in memories:
        sources = mem.get('sources', [])
        if not sources or (isinstance(sources, list) and len(sources) == 0):
            unverified_ids.append(mem['id'])

    unverified_rate = len(unverified_ids) / max(1, len(memories)) if memories else 0.0
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

    # P3: provenance — which constants came from policy vs built-in defaults.
    consumed = ['HL_BASE', 'HL_GROWTH', 'L0_DROP_THRESHOLD', 'L1_ARCHIVE_THRESHOLD',
                'L1_DEMOTE_RC', 'L0_TO_L1_RC', 'L1_TO_L2_RC',
                'W1_DUP', 'W2_CONTRA', 'W3_STALE', 'W4_UNVERIFIED', 'DUP_JACCARD']
    config_sources = {k: ('policy' if k in policy_config else 'default') for k in consumed}
    config_exists = Path(args.config).exists() if args.config else False
    fell_back = sorted(k for k, s in config_sources.items() if s == 'default')
    if config_exists and fell_back:
        print(f"[WARN] {args.config}: using built-in defaults for "
              f"{', '.join(fell_back)} (not declared in policy)", file=sys.stderr)

    # Load memories
    memories = load_memories(args.memory_dir, include_archived=args.include_archived)

    # Compute dimensions
    dup_rate, dup_pairs = compute_dup_rate(memories)
    contra_score, contra_pairs = compute_contradiction_score(memories)
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
            'stale_ids': stale_ids,
            'unverified_ids': unverified_ids,
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
