#!/usr/bin/env python3
"""
RAG Query Script — Search LanceDB index.

Embeds a query text via the local fastembed backend (rag_embed), searches the
LanceDB index, returns JSON results. No network / no Ollama — embeddings run
locally in-process.
Graceful degradation: if the embedding backend / LanceDB is unavailable, exit(2)
with an actionable message + grep fallback hint.

Usage:
  python3 rag_query.py --query "find memories about X" [--top-k 5] [--index-dir .company/memory/index]
  (--model is accepted for back-compat but ignored — the fastembed model is fixed.)

Output (JSON, stdout):
  [{"id": "...", "tier": "L1|L2", "path": "...", "score": 0.95}, ...]
  score = cosine similarity (0..1, higher = closer match).
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


def _reexec_into_rag_venv():
    """Re-launch under .company/.rag-venv python if RAG deps aren't here."""
    if os.environ.get("SC_RAG_REEXEC"):
        return
    try:
        import lancedb  # noqa: F401
        import fastembed  # noqa: F401
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
    _HAS_EMBED = True
except Exception:
    _HAS_EMBED = False

# Guard optional imports
try:
    import lancedb
    _HAS_LANCEDB = True
except ImportError:
    _HAS_LANCEDB = False


class OllamaUnavailable(Exception):
    """Raised when the embedding backend is unavailable (name kept for compat)."""
    pass


def embed(text, model=None, host=None):
    """Embed text via the local fastembed backend (rag_embed). model/host ignored."""
    if not _HAS_EMBED:
        raise OllamaUnavailable("rag_embed/fastembed not importable")
    try:
        return rag_embed.embed(text)
    except Exception as e:
        raise OllamaUnavailable(f"local embedding failed: {e}") from e


def query_rag(query_text, top_k=5, index_dir=".company/memory/index", model="nomic-embed-text"):
    """
    Query the RAG index.

    Args:
        query_text (str): Query string
        top_k (int): Number of results
        index_dir (str): Path to LanceDB index
        model (str): accepted for back-compat, ignored (fastembed model is fixed)

    Returns:
        list[dict]: Results with keys: id, tier, path, score

    Raises:
        OllamaUnavailable: if the local embedding backend is unavailable
            (class name kept for back-compat; no Ollama is involved)
        FileNotFoundError: If index not found
    """
    if not _HAS_LANCEDB:
        raise ImportError("LanceDB not installed")

    index_path = Path(index_dir)
    if not index_path.exists():
        raise FileNotFoundError(f"Index not found at {index_dir}")

    # Embed the query
    query_vec = embed(query_text, model)

    # Connect to index
    db = lancedb.connect(str(index_path))

    # Check if table exists
    try:
        table = db.open_table("memory")
    except Exception:
        raise FileNotFoundError(f"No 'memory' table in index at {index_dir}")

    # Search
    results = table.search(query_vec).metric("cosine").limit(top_k).to_list()

    # Format output: map _distance to score (similarity = 1 - distance)
    output = []
    for row in results:
        # LanceDB returns _distance for cosine metric
        distance = row.get("_distance", 0.0)
        score = 1.0 - distance  # cosine similarity
        output.append({
            "id": row["id"],
            "tier": row["tier"],
            "path": row["path"],
            "score": score
        })

    # Sort descending by score
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

    args = parser.parse_args()

    try:
        # Check deps
        if not _HAS_LANCEDB or not _HAS_EMBED:
            msg = (
                "[rag_query] RAG backend not installed. Run:\n"
                "  bash .company/scripts/rag_setup.sh install\n"
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
            model=args.model
        )

        # Output JSON
        print(json.dumps(results, indent=2))
        sys.exit(0)

    except FileNotFoundError as e:
        msg = (
            f"[rag_query] {e}\n"
            "[rag_query] No index yet. Tony must run rag_index.py first, or grep .company/memory directly.\n"
            "Fallback: grep -ri '<keywords>' .company/memory"
        )
        print(msg, file=sys.stderr)
        sys.exit(2)

    except OllamaUnavailable as e:
        msg = (
            f"[rag_query] embedding backend unavailable: {e}\n"
            "[rag_query] Run: bash .company/scripts/rag_setup.sh install\n"
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
