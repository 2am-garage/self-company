# Entropy KPI Measurement Tool

**File**: `scripts/entropy.py`

## Overview

The entropy measurement script quantifies memory quality across four dimensions, producing a **total entropy score (0–1)** where higher values indicate greater disorder that needs maintenance.

This is a **read-only diagnostic tool** — it never modifies files, only reports findings for Tony's review and action.

---

## Quick Start

```bash
# Basic usage (scans .company/memory by default)
python3 scripts/entropy.py

# With explicit date for testing
python3 scripts/entropy.py --now 2026-06-24

# Include archived memories in calculation
python3 scripts/entropy.py --include-archived

# Load tunable constants from custom policy
python3 scripts/entropy.py --config .company/org/policy.md
```

---

## CLI Parameters

| Parameter | Default | Purpose |
|---|---|---|
| `--memory-dir` | `.company/memory` | Memory root directory; can be overridden for portability across repos |
| `--config` | `.company/org/policy.md` | Policy file; extracts tunable constants (HL_BASE, weights, thresholds, etc.) |
| `--now` | Today's date | Override current date for testing (format: YYYY-MM-DD) |
| `--include-archived` | *(off)* | By default, archived memories are excluded; use this flag to include them |

---

## Entropy Formula

```
Entropy = w₁·dup_rate + w₂·contradiction_score + w₃·stale_rate + w₄·unverified_rate
```

**Range**: 0.0–1.0 (sum of weights = 1.0)

**Goal**: Entropy should **decrease or stay level** after each maintenance period. Sustained increase signals process issues.

---

## Four Dimensions

### 1. **dup_rate** (Duplicate Memories)
- **What it detects**: Text that appears more than once
- **Algorithm**: Word-set Jaccard similarity ≥ `DUP_JACCARD` (default **0.8**)
- **Normalization**: Lowercase, remove punctuation, collapse whitespace
- **Formula**: `dup_rate = (# unique duplicate pairs) / max(1, total memories)`
- **Weight**: `w₁ = 0.25` (25%)

**Example**: Memory "prefer async Python" and "prefer async code" → Jaccard ≥ 0.8 → flagged as duplicate pair.

### 2. **contradiction_score** (Opposing Statements)
- **What it detects**: Memories that contradict each other
- **Two triggers**:
  1. **ID prefix family**: Same slug prefix (e.g., `async-preference-*`) → potential family contradiction
  2. **Opposing keywords + high similarity**: Jaccard 0.5–0.8 + contains pairs like (prefer/avoid, always/never, like/dislike, is/isn't) → contradiction candidate
- **Formula**: `contradiction_score = (# contradiction pairs) / max(1, total memories)`
- **Weight**: `w₂ = 0.35` (35%, highest)

**Example**: Memory "Uwe likes async" + memory "Uwe avoids async" → opposite keywords detected → flagged.

### 3. **stale_rate** (Expired Memories)
- **What it detects**: Active memories that are beyond their freshness window
- **Algorithm**: Compute current `decay_score` for each memory, compare against tier-specific threshold
  - `decay_score = 0.5 ^ (age_days / half_life(reinforce_count))`
  - Threshold depends on tier:
    - **L0**: stale if `decay_score < 0.25` (~14 days without reinforcement)
    - **L1**: stale if `decay_score < 0.15` (~19+ days depending on reinforce_count)
    - **L2**: never stale (permanent)
- **Formula**: `stale_rate = (# stale active memories) / max(1, # active memories)`
- **Weight**: `w₃ = 0.20` (20%)

**Example**: L0 memory from 20 days ago, never reinforced → decay_score ≈ 0.07 < 0.25 → stale.

### 4. **unverified_rate** (Missing Sources)
- **What it detects**: Memories without source attribution
- **Rule**: Violates the VERIFY loop mandate ("every memory must trace back to reality")
- **Check**: `sources` field is empty, `null`, or missing
- **Formula**: `unverified_rate = (# memories with no sources) / max(1, total memories)`
- **Weight**: `w₄ = 0.20` (20%)

**Example**: Memory with `sources: []` → unverified.

---

## Default Tunable Constants

All constants match the values in **design manifest §1** and **policy.md §7**:

| Constant | Value | Where Used | Meaning |
|---|---|---|---|
| `HL_BASE` | **7.0** days | decay_score formula | Half-life for reinforce_count=1 |
| `HL_GROWTH` | **0.5** | decay_score formula | Per-reinforcement half-life growth multiplier |
| `L0_DROP_THRESHOLD` | **0.25** | stale_rate detection | L0 decay threshold |
| `L1_ARCHIVE_THRESHOLD` | **0.15** | stale_rate detection | L1 decay threshold |
| `L1_DEMOTE_RC` | **1** | (informational, not used here) | Reinforcement count threshold for L1 demotion |
| `L0_TO_L1_RC` | **2** | (informational) | Consolidation threshold |
| `L1_TO_L2_RC` | **4** | (informational) | Consolidation threshold |
| `W1_DUP` | **0.25** | entropy formula | Duplicate weight |
| `W2_CONTRA` | **0.35** | entropy formula | Contradiction weight |
| `W3_STALE` | **0.20** | entropy formula | Stale weight |
| `W4_UNVERIFIED` | **0.20** | entropy formula | Unverified weight |
| `DUP_JACCARD` | **0.8** | duplicate detection | Similarity threshold (0–1) |

**Loading order**:
1. Attempt to load from `--config` file (policy.md)
2. Fall back to internal defaults (above)

---

## JSON Output Structure

```json
{
  "now": "2026-06-24",
  "memory_dir": ".company/memory",
  "total_memories": 42,
  "dimensions": {
    "dup_rate": 0.0952,
    "contradiction_score": 0.1429,
    "stale_rate": 0.3333,
    "unverified_rate": 0.0714
  },
  "weights": {
    "w1": 0.25,
    "w2": 0.35,
    "w3": 0.2,
    "w4": 0.2
  },
  "entropy": 0.1878,
  "details": {
    "duplicate_pairs": [["id-a", "id-b"], ["id-c", "id-d"]],
    "contradiction_pairs": [["id-x", "id-y"]],
    "stale_ids": ["id-stale-1", "id-stale-2"],
    "unverified_ids": ["id-no-source"]
  }
}
```

### Field Explanations

- **now**: Measurement date (affected by `--now` flag)
- **memory_dir**: Directory scanned
- **total_memories**: Count of active memories (or all, if `--include-archived`)
- **dimensions**: Four dimension scores (each 0–1)
- **weights**: Applied weight for each dimension
- **entropy**: Final composite score = `w₁·dup + w₂·contra + w₃·stale + w₄·unverified`
- **details**: Candidate lists for Tony's review
  - `duplicate_pairs`: Suspected duplicate memory ID pairs
  - `contradiction_pairs`: Suspected contradiction pairs
  - `stale_ids`: Memories below freshness threshold
  - `unverified_ids`: Memories missing sources

---

## Frontmatter Requirements

For the script to correctly parse memories, each `.md` file must have a frontmatter block (YAML-like, between `---` markers) with these fields:

```yaml
---
id: <slug>                    # Unique identifier (lowercase, hyphens)
tier: L0 | L1 | L2
owner: Tony
sources: [<ref1>, <ref2>]     # Array of references; must not be empty
created: YYYY-MM-DD
last_reinforced: YYYY-MM-DD
reinforce_count: <int>        # ≥ 1
decay_score: <float>          # 0–1; usually set by decay.py
status: active | archived     # Only 'active' counted by default
---
<body>
```

**Parsing robustness**:
- Missing fields → safe defaults (sources = [], decay_score = 1.0, status = active)
- Malformed YAML → warning printed to stderr, file skipped (not fatal)
- Division by zero → guarded against (`max(1, denominator)`)
- Empty memory directory → outputs entropy = 0, dimensions = 0, all lists empty

---

## Typical Workflow

1. **After memory maintenance** (CAPTURE→ORGANIZE→WRITE→VERIFY cycle):
   ```bash
   python3 scripts/entropy.py --now $(date +%Y-%m-%d)
   ```
   Save output to `ops/reports/entropy-<date>.json` for trend tracking.

2. **Tony reviews the output**:
   - Check if total entropy decreased vs. previous period ✓
   - Review `details` candidates (duplicates, contradictions, unverified)
   - Decide if heuristic false positives warrant ignoring that dimension

3. **Iterative improvement**:
   - If `unverified_rate` high → Gibby needs stricter source validation
   - If `contradiction_score` high → check for genuine conflicts vs. false positives
   - If `stale_rate` high → consolidation/decay not aggressive enough
   - If `dup_rate` high → deduplication needed

---

## Edge Cases & Robustness

| Case | Behavior |
|---|---|
| **Empty memory dir** | Returns all dimensions = 0, entropy = 0, details empty |
| **Total memories = 0** | Rates computed as 0 (no division by zero) |
| **Missing `sources` field** | Treated as empty array → counts as unverified |
| **Unparseable date** | Warning printed; memory skipped for stale calculation (conservative) |
| **Mixed tier/status** | Each memory evaluated independently; L2 never stales |
| **Archived memories** | Excluded from stale_rate denominator; included in dup/contra/unverified if `--include-archived` |

---

## Performance Notes

- **O(n²) pairs**: Duplicate and contradiction detection compares all memory pairs; scales with memory count
- **Typical repos**: For 100–500 active memories, runs in <1 second
- **Memory overhead**: Minimal (loads frontmatter + body text)
- **No network/LLM calls**: Pure local computation using standard library only

---

## Integration with Other Tools

### decay.py ↔ entropy.py
- Both use identical decay_score formula and constants
- decay.py **modifies** (--apply): drops L0, archives L1, computes decay_score
- entropy.py **reads** (read-only): uses decay_score to compute stale_rate

### Tony's WRITE phase
- Tony writes new memories with `decay_score: 1.0` (fresh)
- entropy.py reads this and computes current decay_score based on `last_reinforced`

### Gibby's VERIFY phase
- Gibby ensures `sources` is never empty
- entropy.py flags any that slip through as unverified (double-check safety net)

---

## Customizing Weights & Thresholds

Edit **`.company/org/policy.md`** section §7 (Memory Pipeline Parameters) with new values:

```yaml
---
## § 7. Memory Pipeline Parameters (tunable)

HL_BASE = 7.0                    # Half-life base (days)
HL_GROWTH = 0.5                  # Growth per reinforce
L0_DROP_THRESHOLD = 0.25         # L0 stale threshold
L1_ARCHIVE_THRESHOLD = 0.15      # L1 stale threshold
W1_DUP = 0.25                    # Duplicate weight
W2_CONTRA = 0.35                 # Contradiction weight
W3_STALE = 0.20                  # Stale weight
W4_UNVERIFIED = 0.20             # Unverified weight
DUP_JACCARD = 0.8                # Similarity threshold for duplicates
---
```

Then run:
```bash
python3 scripts/entropy.py --config .company/org/policy.md
```

Script automatically loads and applies new values.

---

## Technical Details

### Jaccard Similarity (Duplicate Detection)

Two memories are similar if their normalized word sets have Jaccard index ≥ threshold:

```
Jaccard(A, B) = |A ∩ B| / |A ∪ B|
```

**Normalization steps**:
1. Lowercase
2. Remove punctuation (keep alphanumerics + spaces)
3. Collapse multiple spaces to single space
4. Split on space to get word tokens

**Example**:
- Text A: "Uwe **likes** async Python!"
- Text B: "Uwe **likes** async code"
- Normalized: ["uwe", "likes", "async", "python"] vs ["uwe", "likes", "async", "code"]
- Intersection: {uwe, likes, async} → 3 words
- Union: {uwe, likes, async, python, code} → 5 words
- Jaccard: 3/5 = **0.6** < 0.8 → NOT a duplicate (if threshold is 0.8)

### Decay Score Formula

Memories naturally decay over time; but being reinforced (reviewed/confirmed) slows decay:

```
decay_score(t) = 0.5 ^ (age_days / half_life(reinforce_count))

where:
  age_days = days since last_reinforced
  half_life(rc) = HL_BASE · (1 + HL_GROWTH · (rc - 1))
```

**Intuition**:
- rc=1 → half-life = 7 days → after 7 days, decay_score = 0.5 (50% fresh)
- rc=3 → half-life = 14 days → slower decay, lasts twice as long
- rc=5 → half-life = 21 days → even stickier

### Contradiction Detection (Heuristic)

Two memories are flagged as potential contradictions if:
- **Same ID prefix** (e.g., `preference-async-*`), OR
- **Opposing keywords** (like/dislike, prefer/avoid, always/never, is/isn't) AND
- **Moderate Jaccard similarity** (0.5–0.8, indicates related but different)

This is intentionally **loose** (to avoid false negatives) and flagged as **candidates for review** (Tony decides true contradictions).

---

## Limitations & Future Improvements

1. **Heuristic contradictions**: May produce false positives; Tony's manual review is essential
2. **Semantic similarity**: Uses word-set Jaccard, not semantic embeddings; fine for now, RAG handles semantic later
3. **No LLM**: By design (deterministic, cheap); contradictions purely syntactic
4. **L2 never stales**: Intentional (permanent memories); but could add metadata for "last audit date"
5. **No time-series trending**: Output is snapshot; external job (e.g., daily cron) builds trend history

---

## Version

- **Script version**: v2 (memory pipeline)
- **Manifest alignment**: Build Manifest v2, §1–5
- **Python**: 3.6+, standard library only

