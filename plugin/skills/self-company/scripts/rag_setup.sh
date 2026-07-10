#!/usr/bin/env bash
###############################################################################
# rag_setup.sh — install/verify the local RAG stack INTO the skill (Tom's job).
#
# Creates a private venv at .company/.rag-venv and installs LanceDB (vector
# store) + fastembed (local CPU embeddings). No Ollama, no daemon, fully offline.
# The venv lives under .company/ (gitignored, per-project, never committed).
# rag_index.py / rag_query.py auto re-exec into this venv.
#
# Usage:
#   rag_setup.sh install [PROJECT_DIR]   # create venv + install deps + warm model
#   rag_setup.sh status  [PROJECT_DIR]
#   rag_setup.sh uninstall [PROJECT_DIR] # remove the venv
###############################################################################
set -uo pipefail

CMD="${1:-status}"
PROJECT_DIR="${2:-${SELF_COMPANY_PROJECT_DIR:-$PWD}}"
PROJECT_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd || echo "$PROJECT_DIR")"
VENV="$PROJECT_DIR/.company/.rag-venv"
PY="$VENV/bin/python"

case "$CMD" in
  install)
    if [[ ! -d "$PROJECT_DIR/.company" ]]; then
      echo "[rag_setup] error: $PROJECT_DIR/.company not found — run init_company.sh first." >&2
      exit 1
    fi
    if [[ ! -x "$PY" ]]; then
      echo "[rag_setup] creating venv at $VENV"
      python3 -m venv "$VENV" || { echo "[rag_setup] venv creation failed (need python3-venv)." >&2; exit 1; }
    fi
    echo "[rag_setup] installing lancedb + fastembed (offline RAG stack)…"
    "$PY" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
    # Phase 24 MUST-FIX 4: PIN fastembed. The same model name can produce
    # different vectors across fastembed releases (e.g. the multilingual MiniLM
    # switched from CLS to mean pooling), which would silently violate the
    # index's "same {model, dim} = same vector space" invariant. Pinning keeps
    # every install on the version the index stamp was validated against; a
    # deliberate future bump changes the stamped `lib` and self-heals the index.
    if ! "$PY" -m pip install -q lancedb 'fastembed==0.8.0'; then
      echo "[rag_setup] pip install failed (check network)." >&2
      exit 1
    fi
    echo "[rag_setup] warming the embedding + reranker models (one-time downloads: embed ~470MB, reranker ~600MB, multilingual)…"
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    "$PY" - "$SCRIPT_DIR" <<'PY' || { echo "[rag_setup] model warm-up failed." >&2; exit 1; }
import sys
sys.path.insert(0, sys.argv[1])
from rag_embed import RAG_EMBED_MODEL, EMBEDDING_DIM   # Phase 24: single source of truth
from fastembed import TextEmbedding
m = TextEmbedding(model_name=RAG_EMBED_MODEL)
list(m.embed(["warmup"]))
print(f"  embedding backend ready ({RAG_EMBED_MODEL}, {EMBEDDING_DIM}-dim, CPU, offline)")
# Phase 24 Item 5: warm the cross-encoder reranker too (downloads + compiles the
# ONNX session once, so the first ask-time hook after install is fast, not a 50s
# cold load). Reranking degrades gracefully if this is skipped, so warm-up failure
# is non-fatal here (log + continue) — the rest of RAG still works.
try:
    from rag_rerank import RERANK_MODEL
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    r = TextCrossEncoder(model_name=RERANK_MODEL)
    list(r.rerank("warmup query", ["warmup document"]))
    print(f"  reranker backend ready ({RERANK_MODEL}, CPU, offline)")
except Exception as e:
    print(f"  reranker warm-up skipped ({e}); semantic injection will use the cosine floor only")
PY
    echo "[rag_setup] done. Build the index with: python3 $SCRIPT_DIR/rag_index.py --rebuild"
    ;;
  status)
    if [[ -x "$PY" ]] && "$PY" -c "import lancedb, fastembed" 2>/dev/null; then
      echo "[rag_setup] INSTALLED ($VENV)"
    else
      echo "[rag_setup] not installed"
    fi
    ;;
  uninstall)
    rm -rf "$VENV" && echo "[rag_setup] removed $VENV"
    ;;
  *)
    echo "usage: rag_setup.sh [install|status|uninstall] [PROJECT_DIR]" >&2
    exit 2
    ;;
esac
