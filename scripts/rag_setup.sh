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
    if ! "$PY" -m pip install -q lancedb fastembed; then
      echo "[rag_setup] pip install failed (check network)." >&2
      exit 1
    fi
    echo "[rag_setup] warming the embedding model (one-time ~130MB download)…"
    "$PY" - <<'PY' || { echo "[rag_setup] model warm-up failed." >&2; exit 1; }
from fastembed import TextEmbedding
m = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
list(m.embed(["warmup"]))
print("  embedding backend ready (BAAI/bge-small-en-v1.5, 384-dim, CPU, offline)")
PY
    echo "[rag_setup] done. Build the index with: python3 .company/scripts/rag_index.py --rebuild"
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
