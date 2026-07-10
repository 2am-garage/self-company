#!/usr/bin/env python3
"""
corpus — Phase 28 Item 4a (D4): the SINGLE shared memory-corpus walk + parse +
body-extraction primitive.

Before this module, six scripts (decay.py, entropy.py, verify_memory.py,
reinforce_memory.py, rag_index.py, elon_survey.py's tier_counts) each
independently walked memory_dir (`Path.rglob("*.md")`), parsed frontmatter, and
sliced the body after the closing fence. Phase 11 (frontmatter.py) already
unified the fence-delimiter + key:value split; this module goes one level up
and unifies the WALK + the BODY SLICE too — the piece Phase 11 explicitly left
to each caller. That was still duplicated risk:
  (a) a memory visible to one stage's walk/gate and invisible to another is a
      phantom-dedup / silent-skip bug class (the audit's D4 finding), and
  (b) each caller extracting the body slightly differently caps Item 2's
      embedding-cache hit rate — a cache keyed on content_hash(body) only hits
      if two stages hash the SAME body string for the SAME file.

Two layers, matching how differently each caller actually gates its candidate
set (each keeps that POLICY as a thin local wrapper — the Phase 11/22
pattern: consolidate the fragile mechanical seam, leave interpretation local):

  * `iter_memory_records()` — the raw walk + parse + body-extraction, UNGATED
    (every readable *.md file is returned, tombstoned or not, id or not).
    Callers with a bespoke gating ORDER (rag_index's tier-then-tombstone-
    then-id with its own warning text; verify's id-then-tombstone with its
    own warning text) use this directly and keep their existing per-file
    policy loop unchanged on top — only the walk + parse + body-slice moves
    here.

  * `load_memories()` — the common-case convenience matching decay/entropy/
    reinforce's shared policy: id required, tombstoned excluded unless
    `include_archived=True`. This is what most callers actually want, and is
    the literal signature the spec calls for:
    `load_memories(memory_dir, tiers=None, include_archived=False)`.

Pure stdlib; imports only frontmatter.py + tombstone.py (same directory, same
best-effort-import discipline as every other shared module here — they always
ship together).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from frontmatter import parse as _fm_parse  # noqa: E402
from tombstone import is_tombstoned  # noqa: E402

# Tier label -> its canonical subdirectory under memory_dir. Real memory files
# always keep tier field and directory in sync (decay.py moves files on
# promotion/demotion); nothing here ASSUMES that, it is only used by the
# directory-restricted walk (`tiers=`) and count_by_tier.
TIER_DIRS = (("L0", "L0-working"), ("L1", "L1-warm"), ("L2", "L2-cold"))


def iter_memory_paths(memory_dir, tiers=None, sort=True):
    """List of every `*.md` file under memory_dir — the walk every one of the
    six re-implemented loaders performed independently (D4).

    `sort=True` (default) returns them SORTED — the order five of the six
    legacy loaders (decay/verify/reinforce/rag_index, all `sorted(rglob)`) used
    and the safe default for any NEW caller. `sort=False` returns the RAW
    `Path.rglob` OS-traversal order UNCHANGED — this exists for exactly ONE
    caller, entropy.py, whose legacy `load_memories()` used a BARE UNSORTED
    `rglob` (Phase 28 behaviour-preservation: entropy's `compute_dup_rate`
    does an i<j pairwise scan, so the list ORDER is load-bearing for the ORDER
    of `duplicate_pairs`/`review_candidates` it emits — those flow positionally
    into elon_survey's `dups[:4]` todo, daily-run's duplicate-candidates log
    line, and the agent backlog's `pairs[:15]`. Sorting would change today's
    byte output; the law is byte-identical-to-today, so entropy KEEPS the raw
    order). Do NOT flip entropy to `sort=True` to "improve" determinism — that
    is a behaviour change vs today, out of scope for a preservation phase.

    `tiers` (e.g. `["L0"]`) restricts the walk to those tier subdirectories;
    `None` (default) walks the WHOLE tree — today's behaviour for every
    existing caller. A missing memory_dir returns `[]` (no exception; mirrors
    every caller's existing `if not memory_dir.exists(): return` guard —
    `Path.rglob` on an absent directory already yields nothing, so this is an
    explicit fast-path, not a behaviour change)."""
    base = Path(memory_dir)
    if not base.exists():
        return []
    if tiers:
        wanted = {str(t).upper() for t in tiers}
        paths = []
        for tier, sub in TIER_DIRS:
            if tier in wanted:
                d = base / sub
                if d.exists():
                    paths.extend(d.rglob("*.md"))
        return sorted(paths) if sort else paths
    paths = base.rglob("*.md")
    return sorted(paths) if sort else list(paths)


def count_by_tier(memory_dir):
    """Raw per-tier `*.md` file COUNT — no parse, no gating (tombstoned files
    count too). `elon_survey.tier_counts` folds in here as a Phase 28 Item 4a
    byproduct (its own rglob-per-tier-dir loop was byte-identical to this)."""
    base = Path(memory_dir)
    counts = {tier: 0 for tier, _ in TIER_DIRS}
    for tier, sub in TIER_DIRS:
        d = base / sub
        if d.exists():
            counts[tier] = sum(1 for _ in d.rglob("*.md"))
    return counts


def read_record(path):
    """Read + parse ONE memory file. Returns a dict:
        path (str), text (the full raw file text),
        fm (the raw `frontmatter.parse` dict — NO defaults/aliasing injected;
            `{}` if there is no valid `---` ... `---` block),
        body (the stripped text after the closing fence; `""` if there is no
            valid frontmatter block),
        close_index (the 0-based line index of the CLOSING fence in
            `text.split("\\n")`, or -1 if there is no valid frontmatter block —
            reinforce's in-place rewrite needs this exact index).

    Returns None on a read error (`OSError`) — the one thing every existing
    caller already handled by silently skipping the file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.split("\n")
    close_index = -1
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                close_index = i
                break
    fm, body = _fm_parse(text)
    return {
        "path": str(p),
        "text": text,
        "fm": fm,
        "body": body.strip() if isinstance(body, str) else "",
        "close_index": close_index,
    }


def iter_memory_records(memory_dir, tiers=None):
    """The shared walk: `read_record()` over every file `iter_memory_paths()`
    finds, skipping unreadable files. UNGATED — every parseable file is
    returned regardless of id/tombstone/status; callers apply their own
    policy on top (see module docstring)."""
    out = []
    for p in iter_memory_paths(memory_dir, tiers=tiers):
        rec = read_record(p)
        if rec is not None:
            out.append(rec)
    return out


def load_memories(memory_dir, tiers=None, include_archived=False, with_skipped=False):
    """The common-case gate on top of `iter_memory_records`: id required,
    tombstoned excluded unless `include_archived`. Returns a list of dicts,
    one per KEPT file:
        id, tier, status, path, text, body, close_index, fm
    (`fm` is the raw `frontmatter.parse` dict — each caller applies its OWN
    typed defaults / aliasing on top, unchanged, exactly as before this module
    existed — that per-field interpretation is deliberately NOT unified here).

    `with_skipped=True` returns `(mems, skipped)` where `skipped` is
    `[(path, reason)]` for every file EXCLUDED, reason one of `"no_id"` |
    `"tombstoned"` — the seam a caller that must SURFACE a skip (e.g. verify's
    "missing id" warning) uses instead of re-walking the tree itself."""
    mems, skipped = [], []
    for rec in iter_memory_records(memory_dir, tiers=tiers):
        fm = rec["fm"]
        mem_id = fm.get("id") if fm else None
        if not fm or not mem_id:
            skipped.append((rec["path"], "no_id"))
            continue
        if is_tombstoned(fm) and not include_archived:
            skipped.append((rec["path"], "tombstoned"))
            continue
        mems.append({
            "id": mem_id,
            "tier": fm.get("tier", "L0"),
            "status": fm.get("status", "active"),
            "path": rec["path"],
            "text": rec["text"],
            "body": rec["body"],
            "close_index": rec["close_index"],
            "fm": fm,
        })
    if with_skipped:
        return mems, skipped
    return mems
