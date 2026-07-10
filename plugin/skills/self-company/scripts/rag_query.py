#!/usr/bin/env python3
"""
RAG Query Script — Search LanceDB index.

Embeds a query text via the local fastembed backend (rag_embed), searches the
LanceDB index, returns JSON results. No network / no Ollama — embeddings run
locally in-process.
Graceful degradation: if the embedding backend / LanceDB is unavailable, exit(2)
with an actionable message + grep fallback hint.

Usage:
  python3 rag_query.py --query "find memories about X" [--top-k 5] [--index-dir .company/memory/index] [--query-type hybrid|vector]
  (--model is accepted for back-compat but ignored — the fastembed model is fixed.)

Phase 24 Item 4: --query-type defaults to "hybrid" (vector + BM25/FTS fused via
LanceDB's native RRFReranker — catches exact-identifier queries pure cosine
misses); "vector" is the pre-Item-4 pure-cosine path. Hybrid degrades to vector
automatically on any error (e.g. an older index without the FTS column/index).

Phase 24 Item 1: the index is model-stamped; a stale or missing stamp (a model
swap, or a legacy pre-Phase-24 index) is treated as index-absent (exit 2) rather
than silently scored — see references/rag.md.

Output (JSON, stdout):
  [{"id": "...", "tier": "L1|L2", "path": "...", "score": 0.95}, ...]
  score = TRUE cosine similarity (0..1, higher = closer match) — ALWAYS, even in
  hybrid mode. RRF's fused rank-score is never returned as `score`: it is not on
  the same scale as the relevance floor every consumer gates on (see
  _query_hybrid's gate-placement note).
"""

import sys
import json
import argparse
import urllib.request
import urllib.error
import socket
import hashlib
import os
from pathlib import Path


# The shared sibling modules (rag_venv, rag_embed) live in THIS directory; put it
# on sys.path FIRST so the imports resolve under every entry point (direct run,
# venv re-exec, cron, the test harness). Re-exec into .company/.rag-venv when the
# RAG backend (lancedb/fastembed) isn't importable here — the ONE shared copy.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rag_venv import reexec_if_needed

reexec_if_needed(["lancedb", "fastembed"])

try:
    import rag_embed
    _HAS_EMBED = True
except Exception:
    _HAS_EMBED = False

# Phase 24 Item 1: the shared model-stamp seam (pure stdlib) — every consumer of
# this script (hook_memory_inject, Employee.recall/recall_shared) inherits the
# stamp check for free because they all shell out to THIS script; refusing here
# is refusing everywhere, with no per-consumer duplication.
from rag_stamp import read_stamp, stamp_matches

# Guard optional imports
try:
    import lancedb
    _HAS_LANCEDB = True
except ImportError:
    _HAS_LANCEDB = False


class EmbeddingUnavailable(Exception):
    """Raised when the local embedding backend (fastembed in .company/.rag-venv)
    is unavailable — the shared signal the query path degrades on (parity with
    rag_index's twin). No network / no Ollama is involved."""
    pass


def embed(text, model=None, host=None):
    """Embed text via the local fastembed backend (rag_embed). model/host ignored."""
    if not _HAS_EMBED:
        raise EmbeddingUnavailable("rag_embed/fastembed not importable")
    try:
        return rag_embed.embed(text)
    except Exception as e:
        raise EmbeddingUnavailable(f"local embedding failed: {e}") from e


def _finite(x):
    try:
        return x == x and x not in (float("inf"), float("-inf"))  # not NaN/inf
    except Exception:
        return False


def _cosine(a, b):
    """Plain cosine similarity between two equal-length float vectors. Pure
    Python (no numpy dependency in this fallback) — used only for the rare
    FTS-only hybrid hit that has no vector-leg `_distance` to restore (see
    query_rag_hybrid's gate-placement note)."""
    try:
        a = list(a)
        b = list(b)
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)
    except Exception:
        return 0.0


def _open_index(index_dir):
    """Shared open+stamp-check: connect, open the `memory` table, and refuse a
    mismatched/absent-stamp index (Phase 24 Item 1). Raises FileNotFoundError
    for every "cannot trust this index" case, which every subprocess consumer
    (hook_memory_inject, Employee.recall/recall_shared) ALREADY treats as
    index-absent -> graceful degrade — no consumer-side change needed."""
    index_path = Path(index_dir)
    if not index_path.exists():
        raise FileNotFoundError(f"Index not found at {index_dir}")
    db = lancedb.connect(str(index_path))
    try:
        table = db.open_table("memory")
    except Exception:
        raise FileNotFoundError(f"No 'memory' table in index at {index_dir}")
    stamp = read_stamp(index_path)
    _lib = rag_embed.lib_version()
    if not stamp_matches(stamp, rag_embed.RAG_EMBED_MODEL,
                         rag_embed.EMBEDDING_DIM, _lib):
        # Absent stamp (legacy pre-Phase-24 index), a genuine model swap, OR a
        # fastembed-version change (Phase 24 MUST-FIX 4 — same model can produce
        # different vectors across library versions): either way the vectors in
        # this table cannot be trusted to share the CURRENT query embedding's
        # space. Treat as index-absent; the daily self-heal (rag_index.py)
        # rebuilds it on the next tick.
        raise FileNotFoundError(
            f"Index at {index_dir} has a stale or missing model stamp "
            f"(stamp={stamp!r}, current model={rag_embed.RAG_EMBED_MODEL!r} "
            f"dim={rag_embed.EMBEDDING_DIM} lib={_lib!r}) — treat as absent; "
            "rebuild with rag_index.py --rebuild (auto-heals on the next daily tick)."
        )
    return table


# Phase 24 Item 5: default over-retrieve count for reranking — cross-encode this
# many top candidates, then keep top_k. ~20 short pairs is tens of ms after the
# model is warm (measured); the candidate cap keeps latency bounded.
RERANK_FETCH_DEFAULT = 20


def query_rag(query_text, top_k=5, index_dir=".company/memory/index",
              model="nomic-embed-text", query_type="hybrid",
              rerank=False, rerank_fetch=RERANK_FETCH_DEFAULT):
    """
    Query the RAG index.

    Args:
        query_text (str): Query string
        top_k (int): Number of results
        index_dir (str): Path to LanceDB index
        model (str): accepted for back-compat, ignored (fastembed model is fixed)
        query_type (str): "hybrid" (default, Phase 24 Item 4 — vector + BM25/FTS
            fused via RRF) or "vector" (pure cosine, the pre-Item-4 behavior).
            "hybrid" degrades to "vector" automatically on ANY error (e.g. an
            index built before the FTS column/index existed) — never raises for
            that reason.
        rerank (bool): Phase 24 Item 5 — when True, OVER-retrieve `rerank_fetch`
            candidates, cross-encode each against the query with the local
            multilingual reranker (rag_rerank), sort by reranker score, and keep
            top_k. Each returned hit gains a `rerank_score` field; the consumer
            gates the semantic injection on THAT (a joint query-document relevance
            the bi-encoder cosine can't see). GRACEFUL: any reranker failure
            (backend absent / model load / inference error) silently falls back to
            the cosine ordering and OMITS `rerank_score`, so the consumer's cosine
            floor stays in charge — byte-identical to rerank=False.

    Returns:
        list[dict]: Results with keys: id, tier, path, score [, rerank_score].
        `score` is ALWAYS the vector-leg cosine similarity (Elon's Item-4
        gate-placement rule — never the RRF fused rank-score). When reranking
        succeeded, `rerank_score` is the cross-encoder's joint relevance logit
        (higher = more relevant, unbounded ~-6..+6) and the list is sorted by it;
        `score` (cosine) is still carried for the degrade path and diagnostics.

    Raises:
        EmbeddingUnavailable: if the local embedding backend is unavailable
            (no Ollama is involved; embeddings run locally via fastembed)
        FileNotFoundError: If index not found (also raised for a mismatched or
            missing model-stamp — see _open_index)
    """
    if not _HAS_LANCEDB:
        raise ImportError("LanceDB not installed")

    table = _open_index(index_dir)

    # Embed the query
    query_vec = embed(query_text, model)

    # Over-retrieve when reranking so the cross-encoder has a real candidate pool;
    # include the stored body text (Item 4) so we can cross-encode without a second
    # read. Non-rerank path is byte-identical to before (fetch == top_k, no text).
    fetch = max(top_k, rerank_fetch) if rerank else top_k

    if query_type == "hybrid":
        try:
            results = _query_hybrid(table, query_text, query_vec, fetch, include_text=rerank)
        except Exception:
            results = _query_vector(table, query_vec, fetch, include_text=rerank)
    else:
        results = _query_vector(table, query_vec, fetch, include_text=rerank)

    if rerank:
        return _apply_rerank(query_text, results, top_k)
    return results[:top_k]


def _apply_rerank(query_text, results, top_k):
    """Phase 24 Item 5: cross-encode the over-retrieved `results` (each carrying a
    `text` body) against `query_text`, attach `rerank_score`, sort by it, keep
    top_k. GRACEFUL DEGRADE (non-negotiable): ANY failure — reranker backend
    absent, model load error, inference error, score-count mismatch — returns the
    cosine-ordered results (top_k) with NO `rerank_score`, so the consumer's
    cosine+IDF floor stays in charge exactly as if rerank were never requested.
    Never raises. Always strips the bulky `text` field from the output."""
    def _strip(rows):
        for r in rows:
            r.pop("text", None)
        return rows

    try:
        if not results:
            return []
        import rag_rerank
        docs = [r.get("text") or "" for r in results]
        scores = rag_rerank.rerank_scores(query_text, docs)
        if len(scores) != len(results):
            raise ValueError(f"rerank score count {len(scores)} != {len(results)}")
        for r, s in zip(results, scores):
            r["rerank_score"] = s
        results.sort(key=lambda r: r["rerank_score"], reverse=True)
        return _strip(results[:top_k])
    except Exception as e:
        # Degrade to cosine ordering (results are already sorted by cosine score).
        print(f"[rag_query] rerank unavailable ({e}); cosine-order fallback",
              file=sys.stderr)
        for r in results:
            r.pop("rerank_score", None)
        return _strip(results[:top_k])


def _row_out(row, score, include_text):
    """Build one result dict. `text` (the stored body) is included ONLY when a
    caller needs it for reranking (Item 5) — the normal output never ships bodies."""
    out = {"id": row["id"], "tier": row["tier"], "path": row["path"], "score": score}
    if include_text:
        out["text"] = row.get("text") or ""
    return out


def _query_vector(table, query_vec, top_k, include_text=False):
    """Pure cosine vector search — the pre-Item-4 path, unchanged."""
    results = table.search(query_vec).metric("cosine").limit(top_k).to_list()
    output = []
    for row in results:
        distance = row.get("_distance", 0.0)
        score = 1.0 - distance  # cosine similarity
        output.append(_row_out(row, score, include_text))
    output.sort(key=lambda x: x["score"], reverse=True)
    return output


def _query_hybrid(table, query_text, query_vec, top_k, include_text=False):
    """Phase 24 Item 4 — vector + BM25/FTS fused via Reciprocal Rank Fusion
    (LanceDB native: Tantivy-or-native FTS + RRFReranker; zero new dependencies).
    Catches an exact-identifier query (script name, id, error string) that pure
    cosine misses, per Anthropic's contextual-retrieval measurement the spec
    cites.

    GATE PLACEMENT (Elon's explicit risk flag — the one real risk in Item 4):
    RRF's fused score is a RANK-FUSION number, not a cosine similarity, and must
    NEVER be used as (or silently replace) the relevance floor every consumer
    gates on. So `score` here is ALWAYS the true vector-leg cosine:
      - if the fused row carries a restored `_distance` (it was found by the
        vector leg, whether alone or in both legs) -> score = 1 - distance,
        identical to the pure-vector path;
      - else (an FTS-ONLY hit — the vector leg never surfaced it, e.g. an exact
        token match at low semantic similarity) -> compute the cosine directly
        against the row's own stored vector and the query vector. NEVER
        fabricate a passing score and never drop the floor check — an
        FTS-only hit still has to clear RAG_MIN_SCORE like anything else,
        keeping "off-topic injects nothing" true post-fusion (Item 2's tests)."""
    from lancedb.rerankers import RRFReranker
    results = (
        table.search(query_type="hybrid")
        .vector(query_vec)
        .text(query_text)
        .metric("cosine")
        .limit(top_k)
        .rerank(reranker=RRFReranker())
        .to_list()
    )
    output = []
    for row in results:
        distance = row.get("_distance")
        if distance is not None and _finite(distance):
            score = 1.0 - float(distance)
        else:
            vec = row.get("vector")
            score = _cosine(query_vec, vec) if vec is not None else 0.0
        output.append(_row_out(row, score, include_text))
    output.sort(key=lambda x: x["score"], reverse=True)
    return output


def main():
    parser = argparse.ArgumentParser(
        description="Query the RAG index for similar memories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 rag_query.py --query "Chairman's Python skills"
  python3 rag_query.py --query "project deadlines" --top-k 10
        """
    )
    parser.add_argument("--query", type=str, required=True, help="Query text (required)")
    parser.add_argument("--top-k", type=int, default=5, help="Number of results (default: 5)")
    parser.add_argument("--index-dir", type=str, default=".company/memory/index", help="LanceDB index path")
    parser.add_argument("--model", type=str, default="nomic-embed-text", help="ignored (kept for back-compat; fastembed model is fixed)")
    parser.add_argument("--query-type", type=str, default="hybrid", choices=["hybrid", "vector"],
                        help="hybrid (default, Phase 24 Item 4: vector+BM25/FTS via RRF) or vector (pure cosine)")
    parser.add_argument("--rerank", action="store_true",
                        help="Phase 24 Item 5: over-retrieve then cross-encode with the local "
                             "multilingual reranker; adds rerank_score. Degrades to cosine order "
                             "if the reranker backend is absent.")
    parser.add_argument("--rerank-fetch", type=int, default=RERANK_FETCH_DEFAULT,
                        help=f"candidates to over-retrieve before reranking (default {RERANK_FETCH_DEFAULT})")

    args = parser.parse_args()

    try:
        # Check deps
        if not _HAS_LANCEDB or not _HAS_EMBED:
            msg = (
                "[rag_query] RAG backend not installed. Run:\n"
                "  bash ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_setup.sh install\n"
                "(installs LanceDB + fastembed into .company/.rag-venv; see references/rag.md)\n"
                "Fallback: grep -ri '<keywords>' .company/memory"
            )
            print(msg, file=sys.stderr)
            sys.exit(2)

        # Query
        results = query_rag(
            args.query,
            top_k=args.top_k,
            index_dir=args.index_dir,
            model=args.model,
            query_type=args.query_type,
            rerank=args.rerank,
            rerank_fetch=args.rerank_fetch,
        )

        # Output JSON
        print(json.dumps(results, indent=2))
        sys.exit(0)

    except FileNotFoundError as e:
        msg = (
            f"[rag_query] {e}\n"
            "[rag_query] No usable index (absent, empty, or stale model-stamp). "
            "Tony must run rag_index.py --rebuild, or grep .company/memory directly.\n"
            "Fallback: grep -ri '<keywords>' .company/memory"
        )
        print(msg, file=sys.stderr)
        sys.exit(2)

    except EmbeddingUnavailable as e:
        msg = (
            f"[rag_query] embedding backend unavailable: {e}\n"
            "[rag_query] Run: bash ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_setup.sh install\n"
            "(see references/rag.md)\n"
            "Fallback: grep -ri '<keywords>' .company/memory"
        )
        print(msg, file=sys.stderr)
        sys.exit(2)

    except Exception as e:
        msg = f"[rag_query] Unexpected error: {e}\n[rag_query] Fallback: grep -ri '<keywords>' .company/memory"
        print(msg, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
