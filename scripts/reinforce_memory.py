#!/usr/bin/env python3
"""
reinforce_memory — C1: RAG-powered semantic reinforcement (the consolidation the
tier system depends on).

CAPTURE writes every observation as a NEW L0 file and never reinforces an existing
memory, so re-observing the same thing creates near-duplicates and nothing ever
reaches the rc>=2 promotion gate. This closes that gap: when a new L0 memory is
semantically the SAME as an existing memory (cosine >= threshold), it ABSORBS the
L0 into the canonical one (reinforce_count++, last_reinforced=today, merge sources)
and removes the L0 duplicate — so memories mature L0 -> L1 -> L2.

CONSERVATIVE BY DESIGN:
- The absorbed entry is ALWAYS an L0 (we never delete L1/L2).
- NEVER auto-modifies L2: if the match is an L2 memory, it is only reported, not
  changed.
- High threshold (default 0.92). Dry-run by default; --apply to act. Reversible
  (logged). Requires the RAG venv (re-exec); no-op with a message if absent.

Usage: reinforce_memory.py [--memory-dir DIR] [--threshold 0.92] [--now DATE] [--apply]
"""

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path


# Re-exec into the RAG venv (fastembed/numpy live there), like rag_index.py.
def _reexec_into_rag_venv():
    if os.environ.get("SC_RAG_REEXEC"):
        return
    try:
        import fastembed  # noqa: F401
        import numpy  # noqa: F401
        return
    except Exception:
        pass
    here = Path(__file__).resolve().parent
    for cand in (here.parent / ".rag-venv" / "bin" / "python",
                 Path.cwd() / ".company" / ".rag-venv" / "bin" / "python"):
        if cand.exists():
            os.environ["SC_RAG_REEXEC"] = "1"
            os.execv(str(cand), [str(cand)] + sys.argv)


_reexec_into_rag_venv()

try:
    import rag_embed
    import numpy as np
    _HAS_DEPS = True
except Exception:
    _HAS_DEPS = False

DEFAULT_THRESHOLD = 0.92
SOURCE_ITEM_RE = re.compile(r'"[^"]*"|\[[^\]]*\]')


def parse_frontmatter(text):
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm = {}
            for ln in lines[1:i]:
                s = ln.strip()
                if s and not s.startswith("#") and ":" in s:
                    k, v = s.split(":", 1)
                    fm[k.strip()] = v.strip()
            return fm, i
    return None, -1


def load_memories(memory_dir):
    out = []
    for p in sorted(Path(memory_dir).rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, close = parse_frontmatter(text)
        if not fm or not fm.get("id") or fm.get("status") == "archived":
            continue
        body = "\n".join(text.split("\n")[close + 1:]).strip()
        out.append({
            "id": fm["id"], "tier": fm.get("tier", "L0"), "path": str(p),
            "created": fm.get("created", ""), "body": body, "text": text,
        })
    return out


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
    return SOURCE_ITEM_RE.findall(sources_value or "")


def apply_reinforcement(canon_mem, absorbed_mem, today):
    """Bump canonical rc + date, merge absorbed's sources, delete absorbed file."""
    lines = canon_mem["text"].split("\n")
    fm, close = parse_frontmatter(canon_mem["text"])
    absorbed_fm, _ = parse_frontmatter(absorbed_mem["text"])
    new_sources = _source_items(fm.get("sources", ""))
    for s in _source_items(absorbed_fm.get("sources", "")):
        if s not in new_sources:
            new_sources.append(s)
    try:
        rc = int(fm.get("reinforce_count", "1")) + 1
    except ValueError:
        rc = 2
    for i in range(1, close):
        key = lines[i].split(":", 1)[0].strip() if ":" in lines[i] else ""
        if key == "reinforce_count":
            lines[i] = f"reinforce_count: {rc}"
        elif key == "last_reinforced":
            lines[i] = f"last_reinforced: {today}"
        elif key == "sources":
            lines[i] = "sources: [" + ", ".join(new_sources) + "]"
    Path(canon_mem["path"]).write_text("\n".join(lines), encoding="utf-8")
    Path(absorbed_mem["path"]).unlink()


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
