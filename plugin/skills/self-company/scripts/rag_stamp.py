"""
rag_stamp — SINGLE shared seam for the RAG index's model-stamp (Phase 24 Item 1).

The embedding model is swappable (rag_embed.RAG_EMBED_MODEL); an index built with
model A must never be silently cosine-scored by a query embedded with model B —
same dimension does NOT mean same vector space (bge-small-en-v1.5 and the
multilingual MiniLM are both 384-dim but geometrically unrelated). This module
stamps `{model, dim}` into a small JSON sidecar next to the LanceDB table at
build time (rag_index.py) and lets the query path (rag_query.py — the ONE shared
query engine every consumer shells out to: the ask-time hook, per-employee
recall, and the shared-memory dispatch read) check it before trusting the index.

Pure stdlib — importable with no venv, so the stamp check is as cheap and safe as
rag_venv.py's re-exec probe. A mismatched OR ABSENT stamp (e.g. an index built by
pre-Phase-24 code, which never wrote one) is treated identically: the index is not
provably built with the CURRENT model, so it must be treated as index-absent —
the same graceful-degrade path as no-venv/no-index. This is the migration
mechanism: plugin update lands -> next read/refresh detects the mismatch ->
rebuild -> correct. No data is ever at risk — the index is a derivative cache,
always rebuildable from the markdown truth.
"""

import json
from pathlib import Path

STAMP_FILENAME = ".rag_stamp.json"


def stamp_path(index_dir):
    """The sidecar JSON path for a given LanceDB index directory."""
    return Path(index_dir) / STAMP_FILENAME


def read_stamp(index_dir):
    """Return the persisted {"model", "dim"[, "lib"]} dict, or None if absent,
    unreadable, or malformed. Never raises."""
    try:
        p = stamp_path(index_dir)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "model" in data and "dim" in data:
            return data
        return None
    except Exception:
        return None


def write_stamp(index_dir, model, dim, lib=None):
    """Persist the current {model, dim[, lib]} stamp. Best-effort — a failure here
    must never abort an index build (the stamp is a safety net around the index,
    not the index itself). `lib` is the embedding-library version (Phase 24
    MUST-FIX 4): a fastembed upgrade can change the vectors a fixed model
    produces, so folding it into the stamp makes such an upgrade trigger the same
    self-heal rebuild a model swap does. Returns True on success, False otherwise."""
    try:
        Path(index_dir).mkdir(parents=True, exist_ok=True)
        stamp = {"model": model, "dim": dim}
        if lib is not None:
            stamp["lib"] = lib
        stamp_path(index_dir).write_text(json.dumps(stamp), encoding="utf-8")
        return True
    except Exception:
        return False


def stamp_matches(stamp, model, dim, lib=None):
    """True iff `stamp` (as returned by read_stamp) matches the given model + dim
    (+ lib when supplied). None/malformed stamp -> False (mismatch -> index
    treated as absent). When `lib` is given, a stamp WITHOUT a `lib` key (a
    legacy Phase-24-early index) or with a DIFFERENT `lib` is a mismatch — so a
    fastembed upgrade self-heals. When `lib` is None (deps-free callers/tests),
    the lib dimension is not checked."""
    if not stamp:
        return False
    if stamp.get("model") != model or stamp.get("dim") != dim:
        return False
    if lib is not None and stamp.get("lib") != lib:
        return False
    return True
