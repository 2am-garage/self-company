"""
rag_rerank — shared local cross-encoder reranker for the self-company RAG layer
(Phase 24 Item 5). Uses fastembed's `TextCrossEncoder` (ONNX, CPU, fully offline)
with `jinaai/jina-reranker-v2-base-multilingual` — MULTILINGUAL is required: the
English-only ms-marco cross-encoder scores Chinese on-topic pairs negative and
would suppress the Chairman's Traditional-Chinese recall. Installed into the
project's `.company/.rag-venv` by `rag_setup.sh`, alongside the embedding backend.

Why reranking (the last innocent-off-topic leak): a bi-encoder cosine floor cannot
separate an innocent off-topic prompt from a real on-topic one when they land in
the same score band — measured live, "schedule my morning gym workout" scores
0.417 against a scheduler memory, inseparable from the on-topic `merge-gate` at
0.419 by ANY cosine threshold (their neighbour gaps are 0.060 vs 0.062). A
cross-encoder reads the (query, document) PAIR jointly and scores them ~-3 vs
~+1.7 respectively — a clean margin the bi-encoder can't see.

Mechanism (in rag_query.py): over-retrieve the top ~20 by vector/hybrid, cross-
encode each candidate body against the query, sort by reranker score, and use the
reranker score as the FINAL relevance gate for the semantic injection path.

Privacy: model + inference are entirely local (ONNX/CPU). No network at query time.
Graceful degradation is the caller's contract — this module raises on a missing
backend; rag_query.py catches and falls back to the cosine+IDF path. Pure lazy
import so importing this module never pulls fastembed (mirrors rag_embed.py).
"""

RERANK_MODEL = "jinaai/jina-reranker-v2-base-multilingual"  # multilingual, ONNX/CPU/offline

_model = None


def available():
    """True iff the cross-encoder backend is importable (deps present). Never
    raises — a False result is the signal rag_query.py degrades on."""
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: F401
        return True
    except Exception:
        return False


def _get_model():
    global _model
    if _model is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        _model = TextCrossEncoder(model_name=RERANK_MODEL)
    return _model


def rerank_scores(query, documents):
    """Cross-encode `query` against each document -> list[float] relevance scores,
    one per document, in input order. Higher = more relevant (unbounded logits,
    typically ~-6..+6). Raises if the backend is unavailable or inference fails
    (the caller catches and degrades)."""
    docs = list(documents)
    if not docs:
        return []
    model = _get_model()
    return [float(s) for s in model.rerank(query, docs)]
