"""
rag_embed — shared local embedding backend for the self-company RAG layer.

Uses **fastembed** (ONNX, CPU, fully offline) instead of an Ollama daemon: no
background service, a ~130 MB model, and the same privacy guarantee (nothing
leaves the machine). Installed into the project's `.company/.rag-venv` by
`rag_setup.sh`; rag_index.py / rag_query.py re-exec into that venv so plain
`python3 .company/scripts/rag_*.py` just works once setup has run.

Privacy: model + inference are entirely local. No network at query time.
"""

# Phase 24 Item 1: model -> embedding dimension, ONE table. EMBEDDING_DIM is
# DERIVED from RAG_EMBED_MODEL so a future model swap can never miss updating a
# dim assertion somewhere — the historical bug this table exists to prevent.
#
# `bge-small-en-v1.5` was English-only: it compressed every query (any language)
# into a narrow 0.45-0.65 cosine band regardless of relevance, so the injection
# floor (RAG_MIN_SCORE) filtered nothing, and Chinese queries scored WORSE than
# random (the Chairman's default language — not a corner case). The multilingual
# swap is the fix; the diagnostic + rollback story lives in references/rag.md.
#
# Elon's correction to the original draft: the multilingual model is 384-dim,
# the SAME as bge-small — EMBEDDING_DIM does NOT change for this swap. The
# heavier `bge-m3` (1024-dim, SOTA multilingual) is deliberately NOT chosen here:
# it would force a dim migration across every assertion/schema for quality gains
# that haven't been measured as needed. Revisit only if a future measurement
# says the ceiling is the model, not the retrieval strategy (hybrid/rerank).
EMBED_MODEL_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,                                      # legacy (English-only; superseded)
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,  # current default (multilingual)
}

RAG_EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIM = EMBED_MODEL_DIMS[RAG_EMBED_MODEL]

_model = None


def lib_version():
    """The installed fastembed version string, or "unknown" if unavailable.

    Phase 24 MUST-FIX 4: the SAME model name can produce DIFFERENT vectors across
    fastembed releases (e.g. the multilingual MiniLM switched from CLS to mean
    pooling), silently violating the "same {model, dim} = same vector space"
    invariant the index stamp relies on. Folding the library version into the
    stamp makes a fastembed upgrade trigger the same self-heal rebuild a model
    swap does. Pure best-effort — never raises."""
    try:
        import fastembed
        return str(getattr(fastembed, "__version__", "unknown"))
    except Exception:
        return "unknown"


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=RAG_EMBED_MODEL)
    return _model


def embed(text):
    """Embed a single string -> list[float] of length EMBEDDING_DIM."""
    return embed_batch([text])[0]


def embed_batch(texts):
    """Embed an iterable of strings -> list[list[float]]."""
    model = _get_model()
    return [vec.tolist() for vec in model.embed(list(texts))]
