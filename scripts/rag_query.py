#!/usr/bin/env python3
"""
RAG Query Script — Search LanceDB index.

Embeds a query text via Ollama, searches the LanceDB index, returns JSON results.
Graceful degradation: if Ollama/LanceDB unavailable, exit(2) with actionable message + grep fallback hint.

Usage:
  python3 rag_query.py --query "find memories about X" [--top-k 5] [--index-dir .company/memory/index] [--model nomic-embed-text]

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

# Guard optional imports
try:
    import lancedb
    _HAS_LANCEDB = True
except ImportError:
    _HAS_LANCEDB = False


class OllamaUnavailable(Exception):
    """Raised when Ollama is not reachable."""
    pass


def embed(text, model, host="http://localhost:11434"):
    """
    Embed text via Ollama.

    Args:
        text (str): Text to embed
        model (str): Model name (e.g., 'nomic-embed-text')
        host (str): Ollama HTTP host

    Returns:
        list[float]: Embedding vector

    Raises:
        OllamaUnavailable: If Ollama is unreachable
    """
    url = f"{host}/api/embeddings"
    payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["embedding"]
    except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError, socket.timeout) as e:
        raise OllamaUnavailable(f"Ollama unreachable at {host}: {e}") from e


def query_rag(query_text, top_k=5, index_dir=".company/memory/index", model="nomic-embed-text"):
    """
    Query the RAG index.

    Args:
        query_text (str): Query string
        top_k (int): Number of results
        index_dir (str): Path to LanceDB index
        model (str): Ollama model name

    Returns:
        list[dict]: Results with keys: id, tier, path, score

    Raises:
        OllamaUnavailable: If Ollama not reachable
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
    parser.add_argument("--model", type=str, default="nomic-embed-text", help="Ollama model name")

    args = parser.parse_args()

    try:
        # Check deps
        if not _HAS_LANCEDB:
            msg = (
                "[rag_query] LanceDB not installed — falling back is manual. "
                "To install: python3 -m ensurepip --upgrade && pip install lancedb  "
                "(see references/rag.md)\n"
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
            f"[rag_query] {e}\n"
            "[rag_query] Ollama not reachable. Start Ollama and pull the model:\n"
            "  ollama serve\n"
            "  ollama pull nomic-embed-text\n"
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
