"""
rag_embed — shared local embedding backend for the self-company RAG layer.

Uses **fastembed** (ONNX, CPU, fully offline) instead of an Ollama daemon: no
background service, a ~130 MB model, and the same privacy guarantee (nothing
leaves the machine). Installed into the project's `.company/.rag-venv` by
`rag_setup.sh`; rag_index.py / rag_query.py re-exec into that venv so plain
`python3 .company/scripts/rag_*.py` just works once setup has run.

Privacy: model + inference are entirely local. No network at query time.
"""

RAG_EMBED_MODEL = "BAAI/bge-small-en-v1.5"   # 384-dim, CPU-friendly, offline
EMBEDDING_DIM = 384

_model = None


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
