#!/usr/bin/env python3
"""
reinforce_memory — C1: RAG-powered semantic reinforcement (the consolidation the
tier system depends on).

CAPTURE writes every observation as a NEW L0 file and never reinforces an existing
memory, so re-observing the same thing creates near-duplicates and nothing ever
reaches the rc>=2 promotion gate. This closes that gap: when a new L0 memory is
semantically the SAME as an existing memory (cosine >= threshold), it ABSORBS the
L0 into the canonical one (reinforce_count++, last_reinforced=today, merge sources)
and TOMBSTONES the L0 duplicate (status: absorbed + invalid_at, reaped by decay
after a grace window — recoverable until then) — so memories mature L0 -> L1 -> L2.

CONSERVATIVE BY DESIGN:
- The absorbed entry is ALWAYS an L0 (we never delete L1/L2).
- NEVER auto-modifies L2: if the match is an L2 memory, it is only reported, not
  changed.
- High threshold (default 0.85 = DEFAULT_THRESHOLD, tuned on live-corpus pairs;
  above entropy's 0.82 review-flag gate). Dry-run by default; --apply to act.
  Reversible (logged). Requires the RAG venv (re-exec); no-op with a message if
  absent.

Usage: reinforce_memory.py [--memory-dir DIR] [--threshold 0.85] [--now DATE] [--apply]
"""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path


# Bucket 2 (Phase 14): the shared sibling modules (rag_venv, tombstone,
# frontmatter) live in THIS directory. Put it on sys.path FIRST so the hard
# imports below resolve under every entry point — direct run, cron, the venv
# re-exec below, or an import by the test harness (mirrors schedule_validator.py).
# They always ship together here.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Re-exec into the RAG venv (fastembed/numpy live there), like rag_index.py — the
# ONE shared copy in rag_venv.py.
from rag_venv import reexec_if_needed

reexec_if_needed(["fastembed", "numpy"])

# Phase 6 Item 1: tombstone vocabulary (archived / defunct / absorbed) is the ONE
# shared set in tombstone.py (same dir), so it can't drift. A tombstoned memory
# must not be a reinforcement candidate (it can't become canonical, and an
# already-absorbed dup must not re-surface).
from tombstone import TOMBSTONE_STATUSES, is_tombstoned

# Phase 11: the fragile frontmatter PARSING SEAM (delimiter + key:value split +
# source tokenization) is the ONE shared module (frontmatter.py, same dir). This
# also single-sources the SOURCE_ITEM_RE that was copied verbatim here and in
# capture-trigger.py (C2). reinforce keeps its own closing-fence INDEX return (its
# in-place line rewrite needs it) and all sources/rc merge logic layered on top.
from frontmatter import (split as _fm_split, parse as _fm_parse,
                         serialize as _fm_serialize,
                         SOURCE_ITEM_RE, tokenize_sources,
                         _atomic_write)

# Phase 28 Item 4a (D4): the walk + id/tombstone gate + body-extraction that
# used to be reinforce's own private loop is now the ONE shared corpus.py
# primitive (same directory, same best-effort-import discipline). Aligning the
# body extraction across every consumer is also Item 2's cache-hit-rate
# prerequisite (a content_hash cache only hits if two stages hash the SAME
# body string for the SAME file).
import corpus

try:
    import rag_embed
    import numpy as np
    _HAS_DEPS = True
except Exception:
    _HAS_DEPS = False

DEFAULT_THRESHOLD = 0.85   # tuned: catches clear re-observations (~0.88) but not
                           # merely-related-but-distinct ones (~0.80), with margin.


def parse_frontmatter(text):
    # Phase 11: dict built by the shared parser; reinforce still returns the
    # closing-fence line INDEX (not a body string) because apply_reinforcement
    # rewrites frontmatter lines in place by index. The opening-fence gate
    # (`lines[0].strip()=='---'`, no leading-blank skip) and the `(None, -1)`
    # no-frontmatter sentinel are preserved exactly; only the key:value dict is
    # now sourced from `frontmatter.parse`.
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm, _body = _fm_parse(text)
            return fm, i
    return None, -1


def load_memories(memory_dir):
    # Phase 28 Item 4a: the walk + id/tombstone gate + body-slice now come from
    # corpus.load_memories (shared with decay/entropy/rag_index) — this is a
    # pure field-reshape on top, byte-identical to the old private loop: same
    # gate (id present, not tombstoned), same body (closing-fence slice,
    # stripped once).
    return [
        {
            "id": mem["id"], "tier": mem["tier"], "path": mem["path"],
            "created": mem["fm"].get("created", ""), "body": mem["body"],
            "text": mem["text"],
        }
        for mem in corpus.load_memories(memory_dir)
    ]


def plan_reinforcements(mems, pairs, threshold):
    """
    Pure decision step. `mems`: {id: mem}. `pairs`: iterable of (a_id, b_id, score).
    Returns (reinforcements, skipped_l2).
      - absorbed is ALWAYS an L0; canonical is the kept memory.
      - if the partner is L2 -> skip (report only; never touch L2).
      - partner L1 -> canonical = L1, absorbed = the L0.
      - both L0 -> canonical = older by created (tie -> lexically smaller id),
        absorbed = the other L0.
    Each memory is used at most once (as absorbed); processed by score desc.
    """
    reinforcements, skipped_l2 = [], []
    used = set()
    for a_id, b_id, score in sorted(pairs, key=lambda t: -t[2]):
        if score < threshold or a_id == b_id:
            continue
        a, b = mems.get(a_id), mems.get(b_id)
        if not a or not b:
            continue
        tiers = {a["tier"], b["tier"]}
        if "L2" in tiers:
            skipped_l2.append({"pair": sorted([a_id, b_id]), "score": round(score, 4)})
            continue
        # choose canonical (keep) and absorbed (must be L0)
        if a["tier"] == "L1" and b["tier"] == "L0":
            canon, absorbed = a, b
        elif b["tier"] == "L1" and a["tier"] == "L0":
            canon, absorbed = b, a
        elif a["tier"] == "L0" and b["tier"] == "L0":
            canon, absorbed = (a, b) if (a["created"], a["id"]) <= (b["created"], b["id"]) else (b, a)
        else:
            continue  # e.g. L1<->L1: don't merge warm memories automatically
        if canon["id"] in used or absorbed["id"] in used:
            continue
        used.add(canon["id"])
        used.add(absorbed["id"])
        reinforcements.append({"canonical": canon["id"], "absorbed": absorbed["id"],
                               "canonical_tier": canon["tier"], "score": round(score, 4)})
    return reinforcements, skipped_l2


def _source_items(sources_value):
    # C2 dedupe: the source-token extractor is the shared one (frontmatter.py).
    return tokenize_sources(sources_value)


def _session_ids(items):
    """Distinct session ids from source tokens. A token looks like
    '"[<session-id>#<line>]"'; the id is everything before the first '#'
    (session ids are sanitised to [A-Za-z0-9._-], so '#' never appears in
    one). Tokens without the [..#..] shape (e.g. "charter:foo") count as one
    distinct id each, using the whole token."""
    out = set()
    for it in items:
        inner = it.strip('"')
        if inner.startswith("[") and inner.endswith("]") and "#" in inner:
            out.add(inner[1:].split("#", 1)[0])
        else:
            out.add(inner)
    return out


def apply_reinforcement(canon_mem, absorbed_mem, today):
    """Merge absorbed's sources into canonical, update last_reinforced, and
    TOMBSTONE the absorbed file (status: absorbed + invalid_at) — no longer a
    hard delete (Item 2 / BOB-F2). Phase 5 Item 1 (N1): rc bumps at most once
    per DISTINCT session id — the merge increments reinforce_count only when the
    absorbed memory contributes at least one session id the canonical didn't
    already have (same-session near-duplicates consolidate without inflating the
    cross-session recurrence signal the promotion gates trust)."""
    lines = canon_mem["text"].split("\n")
    fm, close = parse_frontmatter(canon_mem["text"])
    absorbed_fm, _ = parse_frontmatter(absorbed_mem["text"])
    new_sources = _source_items(fm.get("sources", ""))
    canon_sessions = _session_ids(new_sources)
    for s in _source_items(absorbed_fm.get("sources", "")):
        if s not in new_sources:
            new_sources.append(s)
    adds_new_session = bool(_session_ids(new_sources) - canon_sessions)
    try:
        rc = int(fm.get("reinforce_count", "1"))
    except ValueError:
        rc = 1
    if adds_new_session:
        rc += 1
    # C1 (BOB-F3): rewrite EXISTING frontmatter lines in place, but track which
    # of the three mutated keys were present. The old loop only rewrote existing
    # lines, so a canonical LACKING `reinforce_count` (or last_reinforced /
    # sources) silently dropped the update — the rc bump vanished and the memory
    # never reached the rc>=2 promotion gate. Any key that was absent is inserted
    # just before the closing fence below.
    seen_keys = set()
    for i in range(1, close):
        key = lines[i].split(":", 1)[0].strip() if ":" in lines[i] else ""
        if key == "reinforce_count":
            lines[i] = f"reinforce_count: {rc}"
            seen_keys.add("reinforce_count")
        elif key == "last_reinforced":
            lines[i] = f"last_reinforced: {today}"
            seen_keys.add("last_reinforced")
        elif key == "sources":
            lines[i] = "sources: [" + ", ".join(new_sources) + "]"
            seen_keys.add("sources")
    inserts = []
    if "reinforce_count" not in seen_keys:
        inserts.append(f"reinforce_count: {rc}")
    if "last_reinforced" not in seen_keys:
        inserts.append(f"last_reinforced: {today}")
    if "sources" not in seen_keys:
        inserts.append("sources: [" + ", ".join(new_sources) + "]")
    if inserts:
        lines[close:close] = inserts   # insert before the closing fence
    _atomic_write(canon_mem["path"], "\n".join(lines), encoding="utf-8")
    _tombstone_absorbed(absorbed_mem, today)


def _tombstone_absorbed(absorbed_mem, today):
    """Item 2 (BOB-F2): a merged-away duplicate is TOMBSTONED, not hard-deleted.

    Rewrite the absorbed L0's frontmatter to `status: absorbed` +
    `invalid_at: <today>` (inserting either key when absent) and leave the file
    on disk with its body verbatim. The shared tombstone vocabulary already
    excludes `absorbed` from every active scan (recall/injection/reinforce), and
    decay's grace-windowed reap physically removes it later — activating the
    documented recovery window (a false-positive dedup on paraphrased-but-distinct
    facts is recoverable until the reap) and the previously-dead `absorbed` reap
    branch. Only ever called on an L0 (plan_reinforcements guarantees absorbed is
    L0), so no L1/L2 file is ever tombstoned."""
    text = absorbed_mem.get("text")
    if text is None:
        text = Path(absorbed_mem["path"]).read_text(encoding="utf-8")
    lines = text.split("\n")
    _fm, close = parse_frontmatter(text)
    if close < 0:
        # Unparseable frontmatter (shouldn't happen — absorbed came from
        # load_memories, which requires a valid block). Leave the file intact
        # rather than risk corrupting/orphaning it.
        return
    # Defense-in-depth L0-only guard (matches decay's explicit-`if`, not-`assert`
    # L2-safety doctrine — python3 -O strips asserts). plan_reinforcements
    # already guarantees `absorbed` is an L0, but NEVER tombstone an L1/L2 here
    # even if a caller regressed that guarantee: it would retire a warm/cold
    # memory that decay's reap then deletes past grace. Prefer the file's own
    # tier; fall back to the passed dict's tier.
    tier = ((_fm or {}).get("tier") or absorbed_mem.get("tier") or "").strip()
    if tier in ("L1", "L2"):
        return
    seen = set()
    for i in range(1, close):
        key = lines[i].split(":", 1)[0].strip() if ":" in lines[i] else ""
        if key == "status":
            lines[i] = "status: absorbed"
            seen.add("status")
        elif key == "invalid_at":
            lines[i] = f"invalid_at: {today}"
            seen.add("invalid_at")
    inserts = []
    if "status" not in seen:
        inserts.append("status: absorbed")
    if "invalid_at" not in seen:
        inserts.append(f"invalid_at: {today}")
    if inserts:
        lines[close:close] = inserts   # insert before the closing fence
    _atomic_write(absorbed_mem["path"], "\n".join(lines), encoding="utf-8")


def nearest_pairs(mems, threshold):
    """Embed bodies, return (a_id, b_id, score) for each memory's nearest other."""
    bodies = [m["body"] or m["id"] for m in mems]
    vecs = np.array(rag_embed.embed_batch(bodies), dtype="float32")
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = vecs / norms
    sim = unit @ unit.T
    np.fill_diagonal(sim, -1.0)
    pairs = []
    for i, m in enumerate(mems):
        j = int(np.argmax(sim[i]))
        s = float(sim[i][j])
        if s >= threshold:
            pairs.append((m["id"], mems[j]["id"], s))
    return pairs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory-dir", default=".company/memory")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--now", default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    today = args.now or date.today().isoformat()

    if not _HAS_DEPS:
        print(json.dumps({"error": "RAG backend not installed — run "
                          "bash .company/scripts/rag_setup.sh install", "reinforcements": []}))
        return 0
    if not Path(args.memory_dir).exists():
        print(json.dumps({"error": "no memory dir", "reinforcements": []}))
        return 0

    mems = load_memories(args.memory_dir)
    by_id = {m["id"]: m for m in mems}
    pairs = nearest_pairs(mems, args.threshold) if mems else []
    reinforcements, skipped_l2 = plan_reinforcements(by_id, pairs, args.threshold)

    if args.apply:
        for r in reinforcements:
            apply_reinforcement(by_id[r["canonical"]], by_id[r["absorbed"]], today)

    print(json.dumps({
        "applied": args.apply, "threshold": args.threshold,
        "reinforcements": reinforcements, "skipped_l2": skipped_l2,
        "scanned": len(mems),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
