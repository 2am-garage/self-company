#!/usr/bin/env python3
"""
RAG Index Builder — Index memory for semantic search (self-company).

Scans .company/memory/ for L1 and L2 markdown files, computes embeddings via Ollama,
stores in LanceDB (local, offline). Index is a derivative of markdown truth; always rebuildable.

Pure stdlib only: argparse, json, os, sys, re, hashlib, urllib, pathlib, datetime.
Optional lancedb wrapped in try/except with graceful exit(2).

Output: JSON summary with counts, actions, and warnings.
"""

import argparse
import json
import os
import sys
import re
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List
import urllib.request

# Phase 6 Item 1: shared tombstone vocabulary (archived/defunct/absorbed) so a
# tombstoned memory can never leak into the RAG index. Best-effort import.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from tombstone import TOMBSTONE_STATUSES, is_tombstoned
except Exception:  # pragma: no cover - authoritative copy: tombstone.py
    TOMBSTONE_STATUSES = frozenset({"archived", "defunct", "absorbed"})

    def is_tombstoned(fm):
        return str(fm.get("status") or "").strip().lower() in TOMBSTONE_STATUSES

# Phase 11 Item 2: the fragile frontmatter delimiter + key:value split seam lives
# in ONE shared module (frontmatter.py). rag keeps its OWN typed interpretation
# (tier/status validation, sources list parse, _parse_errors) on top; only the
# fence-location + body-split is delegated. Best-effort import + verbatim
# fallback, same pattern as the tombstone import above.
try:
    from frontmatter import split as _fm_split
except Exception:  # pragma: no cover - verbatim fallback (authoritative: frontmatter.py)
    def _fm_split(text):
        lines = text.split('\n')
        if lines[0].strip() != '---':
            return [], text
        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                return lines[1:i], '\n'.join(lines[i + 1:])
        return [], text
import urllib.error

# ============================================================================
# RE-EXEC INTO THE RAG VENV (created by rag_setup.sh) if deps aren't here
# ============================================================================

def _reexec_into_rag_venv():
    """If lancedb/fastembed aren't importable but the project's .rag-venv exists,
    re-launch this script under that venv's python so RAG 'just works'."""
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

# Shared local embedding backend (fastembed). Lazy fastembed import inside.
try:
    import rag_embed
    _HAS_EMBED = True
except Exception:
    _HAS_EMBED = False

# ============================================================================
# OPTIONAL IMPORT (graceful degradation)
# ============================================================================

try:
    import lancedb
    _HAS_LANCEDB = True
except ImportError:
    _HAS_LANCEDB = False


# ============================================================================
# CONSTANTS
# ============================================================================

RAG_ENABLE_THRESHOLD = 50
RAG_MODEL = "BAAI/bge-small-en-v1.5"   # fastembed (local CPU, offline); see rag_embed.py
RAG_OLLAMA_HOST = "http://localhost:11434"  # legacy; unused with fastembed backend
EMBEDDING_DIM = 384


# ============================================================================
# EXCEPTIONS
# ============================================================================

class OllamaUnavailable(Exception):
    """Raised when Ollama is not reachable."""
    pass


# ============================================================================
# FRONTMATTER PARSING (reused from decay.py)
# ============================================================================

def parse_frontmatter(content: str) -> Dict[str, Any]:
    """
    Parse YAML-like frontmatter from markdown.

    Returns dict with parsed fields including _body (the content after frontmatter).
    """
    result = {
        "id": None,
        "tier": None,
        "owner": None,
        "sources": [],
        "created": None,
        "last_reinforced": None,
        "reinforce_count": None,
        "decay_score": None,
        "status": None,
        "_body": "",
        "_parse_errors": []
    }

    # Fence-location + body-split delegated to the shared parser (Phase 11).
    # rag keeps its own opening/closing error accounting: the shared split
    # collapses no-opening and no-closing to ([], text), so re-derive the two
    # distinct messages from the same delimiter rag always used.
    lines = content.split('\n')
    if not lines or lines[0].strip() != '---':
        result["_parse_errors"].append("No opening --- found")
        return result

    raw_fm_lines, body = _fm_split(content)
    if not raw_fm_lines and body == content:
        # opening fence present (checked above) but no matching close
        result["_parse_errors"].append("No closing --- found")
        return result

    # Parse frontmatter lines
    for line in raw_fm_lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        if ':' not in line:
            result["_parse_errors"].append(f"Malformed line: {line}")
            continue

        key, val_str = line.split(':', 1)
        key = key.strip()
        val_str = val_str.strip()

        try:
            if key == "id":
                result["id"] = val_str if val_str else None
            elif key == "tier":
                if val_str in ("L0", "L1", "L2"):
                    result["tier"] = val_str
                else:
                    result["_parse_errors"].append(f"Invalid tier: {val_str}")
            elif key == "owner":
                result["owner"] = val_str if val_str else None
            elif key == "status":
                if val_str == "active" or val_str in TOMBSTONE_STATUSES:
                    result["status"] = val_str
                else:
                    result["_parse_errors"].append(f"Invalid status: {val_str}")
            elif key == "sources":
                if val_str.startswith('[') and val_str.endswith(']'):
                    inner = val_str[1:-1].strip()
                    if inner:
                        result["sources"] = [s.strip() for s in inner.split(',')]
                    else:
                        result["sources"] = []
                else:
                    result["_parse_errors"].append(f"Malformed sources: {val_str}")
            elif key == "created":
                result["created"] = val_str if val_str else None
            elif key == "last_reinforced":
                result["last_reinforced"] = val_str if val_str else None
            elif key == "reinforce_count":
                try:
                    result["reinforce_count"] = int(val_str)
                except ValueError:
                    result["_parse_errors"].append(f"Non-int reinforce_count: {val_str}")
            elif key == "decay_score":
                try:
                    result["decay_score"] = float(val_str)
                except ValueError:
                    result["_parse_errors"].append(f"Non-float decay_score: {val_str}")
        except Exception as e:
            result["_parse_errors"].append(f"Error parsing {key}: {e}")

    # Extract body (everything after closing ---), as located by the shared split.
    result["_body"] = body

    return result


# ============================================================================
# CONTENT HASHING
# ============================================================================

def normalize_text(text: str) -> str:
    """
    Normalize text for hashing: strip, collapse whitespace.
    Preserves punctuation so embeddings get real text.
    """
    text = text.strip()
    text = re.sub(r'\s+', ' ', text)
    return text


def compute_content_hash(body: str) -> str:
    """
    Compute SHA256 hash of normalized body.
    Used for incremental indexing: skip re-embedding if unchanged.
    """
    normalized = normalize_text(body)
    return hashlib.sha256(normalized.encode()).hexdigest()


# ============================================================================
# EMBEDDING VIA OLLAMA
# ============================================================================

def embed(text: str, model: str = None, host: str = None) -> List[float]:
    """
    Embed text via the local fastembed backend (rag_embed). The `model`/`host`
    params are kept for signature compatibility with the old Ollama callers and
    are ignored. Raises OllamaUnavailable (reused as a generic "embedding
    backend unavailable" signal) if fastembed/the venv isn't installed.
    """
    if not _HAS_EMBED:
        raise OllamaUnavailable("rag_embed/fastembed not importable")
    try:
        return rag_embed.embed(text)
    except Exception as e:
        raise OllamaUnavailable(f"local embedding failed: {e}")


# ============================================================================
# LANCEDB TABLE OPERATIONS
# ============================================================================

def get_or_create_table(db, table_name: str = "memory"):
    """
    Get existing LanceDB table or create it if absent.
    Returns the table object.
    """
    try:
        # Check if table exists
        tables = db.table_names()
        if table_name in tables:
            return db.open_table(table_name)
        else:
            # Create empty table (will be populated)
            return None
    except Exception:
        return None


def upsert_rows(db, rows: List[Dict[str, Any]], table_name: str = "memory") -> int:
    """
    Upsert rows into LanceDB table.
    Deletes rows by id first (simulating upsert), then adds.
    Returns count of rows added.
    """
    if not rows:
        return 0

    table = get_or_create_table(db, table_name)

    # Delete existing ids
    ids_to_delete = [r["id"] for r in rows]
    if table:
        for row_id in ids_to_delete:
            try:
                table.delete(f"id = '{row_id}'")
            except Exception:
                pass  # Row might not exist yet

    # Add new rows
    try:
        if table is None:
            # Create table with first row's schema
            table = db.create_table(table_name, data=rows, mode="overwrite")
        else:
            table.add(rows)
    except Exception as e:
        print(f"[ERROR] Failed to add rows to table: {e}", file=sys.stderr)
        raise

    return len(rows)


def delete_stale_rows(db, live_ids: set, table_name: str = "memory") -> int:
    """
    Delete rows from table whose id is not in live_ids (markdown files no longer exist).
    Returns count of deleted rows.
    """
    table = get_or_create_table(db, table_name)
    if not table:
        return 0

    try:
        existing_rows = table.search().to_list()
        deleted_count = 0
        for row in existing_rows:
            if row["id"] not in live_ids:
                try:
                    table.delete(f"id = '{row['id']}'")
                    deleted_count += 1
                except Exception:
                    pass
        return deleted_count
    except Exception as e:
        print(f"[WARN] Failed to clean stale rows: {e}", file=sys.stderr)
        return 0


# ============================================================================
# THRESHOLD CHECK (no deps needed)
# ============================================================================

def check_threshold(memory_dir: Path, threshold: int) -> bool:
    """
    Count L1 + L2 active memories without parsing anything complex.
    Returns True if count >= threshold.
    """
    count = 0
    memory_dir = Path(memory_dir)

    if not memory_dir.exists():
        return False

    for md_file in memory_dir.rglob("*.md"):
        # Quick scan for tier line
        try:
            content = md_file.read_text(encoding='utf-8')
            if re.search(r'tier:\s*(L1|L2)', content):
                # Quick status check
                if re.search(r'status:\s*active', content) or not re.search(r'status:', content):
                    count += 1
        except Exception:
            pass

    return count >= threshold


# ============================================================================
# MAIN INDEXING
# ============================================================================

def index_memory(memory_dir: Path, index_dir: Path, model: str, rebuild: bool = False,
                 now: Optional[datetime] = None, include_l0: bool = False) -> Dict[str, Any]:
    """
    Scan memory_dir for L1/L2 active files, embed via Ollama, upsert to LanceDB.

    Returns JSON summary dict.
    """
    if now is None:
        now = datetime.now()

    report = {
        "now": now.strftime("%Y-%m-%d"),
        "memory_dir": str(memory_dir),
        "index_dir": str(index_dir),
        "model": model,
        "mode": "rebuild" if rebuild else "incremental",
        "l1_l2_count": 0,
        "threshold": RAG_ENABLE_THRESHOLD,
        "over_threshold": False,
        "embedded": 0,
        "skipped_unchanged": 0,
        "deleted_stale": 0,
        "table_rows": 0,
        "warnings": []
    }

    if not _HAS_LANCEDB:
        report["warnings"].append("LanceDB not installed")
        return report

    memory_dir = Path(memory_dir)
    index_dir = Path(index_dir)

    if not memory_dir.exists():
        report["warnings"].append(f"Memory dir not found: {memory_dir}")
        return report

    # Scan for L1/L2 active files
    md_files = sorted(memory_dir.rglob("*.md"))
    candidates = []
    live_ids = set()

    for path in md_files:
        try:
            content = path.read_text(encoding='utf-8')
            mem = parse_frontmatter(content)

            # Filter by tier (L1/L2 by default; include L0 when asked, e.g. for
            # the reinforce path that must match new captures against all memory).
            allowed = ("L0", "L1", "L2") if include_l0 else ("L1", "L2")
            if mem["tier"] not in allowed:
                continue
            if is_tombstoned(mem):
                continue
            if mem["id"] is None:
                report["warnings"].append(f"{path}: missing id, skipped")
                continue

            candidates.append((path, mem))
            live_ids.add(mem["id"])
            report["l1_l2_count"] += 1

        except Exception as e:
            report["warnings"].append(f"{path}: exception: {e}")

    report["over_threshold"] = report["l1_l2_count"] >= RAG_ENABLE_THRESHOLD

    # Load existing hash map if incremental
    existing_hashes = {}
    if not rebuild:
        try:
            db = lancedb.connect(str(index_dir))
            table = get_or_create_table(db)
            if table:
                for row in table.search().to_list():
                    existing_hashes[row["id"]] = row.get("content_hash", "")
        except Exception as e:
            report["warnings"].append(f"Failed to load existing index: {e}")

    # Connect to Ollama for embedding
    try:
        # Test connection with a tiny embedding
        _ = embed("test", model, RAG_OLLAMA_HOST)
    except OllamaUnavailable as e:
        print("[rag_index] RAG backend not installed. Run:\n"
              "  bash .company/scripts/rag_setup.sh install\n"
              "(installs LanceDB + fastembed into .company/.rag-venv; see references/rag.md)",
              file=sys.stderr)
        sys.exit(2)

    # Prepare rows to upsert
    rows_to_add = []

    for path, mem in candidates:
        body = mem.get("_body", "")
        content_hash = compute_content_hash(body)

        # Skip if unchanged (incremental mode)
        if not rebuild and mem["id"] in existing_hashes:
            if existing_hashes[mem["id"]] == content_hash:
                report["skipped_unchanged"] += 1
                continue

        # Embed body
        try:
            vec = embed(body, model, RAG_OLLAMA_HOST)
            if len(vec) != EMBEDDING_DIM:
                report["warnings"].append(
                    f"{mem['id']}: embedding dim {len(vec)} != {EMBEDDING_DIM}"
                )
                continue

            row = {
                "id": mem["id"],
                "tier": mem["tier"],
                "path": str(path),
                "content_hash": content_hash,
                "vector": vec
            }
            rows_to_add.append(row)
            report["embedded"] += 1

        except OllamaUnavailable as e:
            print(f"[rag_index] Ollama not reachable at {RAG_OLLAMA_HOST}. "
                  f"Start Ollama and run: ollama pull {model}  "
                  f"(see references/rag.md)", file=sys.stderr)
            sys.exit(2)

    # Connect to LanceDB and upsert
    try:
        index_dir.mkdir(parents=True, exist_ok=True)
        db = lancedb.connect(str(index_dir))

        if rebuild:
            # Drop table
            try:
                db.drop_table("memory")
            except Exception:
                pass

        # Upsert rows
        if rows_to_add:
            upsert_rows(db, rows_to_add)

        # Delete stale rows
        report["deleted_stale"] = delete_stale_rows(db, live_ids)

        # Get final table row count
        table = get_or_create_table(db)
        if table:
            report["table_rows"] = len(table.search().to_list())

    except Exception as e:
        print(f"[rag_index] LanceDB error: {e}", file=sys.stderr)
        raise

    return report


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="RAG Index Builder — embed memory for semantic search."
    )
    parser.add_argument(
        "--memory-dir",
        default=".company/memory",
        help="Root memory directory (default: .company/memory)"
    )
    parser.add_argument(
        "--index-dir",
        default=".company/memory/index",
        help="LanceDB index location (default: .company/memory/index)"
    )
    parser.add_argument(
        "--model",
        default=RAG_MODEL,
        help=f"Ollama embedding model (default: {RAG_MODEL})"
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Drop table and rebuild from scratch (vs. incremental)"
    )
    parser.add_argument(
        "--threshold-check",
        action="store_true",
        help="Check if L1+L2 count >= threshold. "
             "Exit 0 if at/over threshold (enable RAG), 1 if under. "
             "Does NOT require Ollama/LanceDB."
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=RAG_ENABLE_THRESHOLD,
        help=f"RAG enable threshold (default: {RAG_ENABLE_THRESHOLD})"
    )
    parser.add_argument(
        "--config",
        help="Path to policy.md to read RAG_ENABLE_THRESHOLD / RAG_MODEL"
    )
    parser.add_argument(
        "--include-l0",
        action="store_true",
        help="Also index L0-working memories (default: L1/L2 only). Needed for the "
             "reinforce path that matches new captures against all existing memory."
    )
    parser.add_argument(
        "--now",
        help="Reference date (YYYY-MM-DD). Default: today."
    )

    args = parser.parse_args()

    # Parse --now if given
    now = None
    if args.now:
        try:
            now = datetime.strptime(args.now, "%Y-%m-%d")
        except ValueError:
            print(f"[ERROR] Invalid --now format: {args.now}", file=sys.stderr)
            return 1

    # Load config if given
    threshold = args.threshold
    model = args.model
    if args.config:
        config_path = Path(args.config)
        if config_path.exists():
            try:
                config_text = config_path.read_text(encoding='utf-8')
                for line in config_text.split('\n'):
                    if 'RAG_ENABLE_THRESHOLD' in line and ('=' in line or ':' in line):
                        try:
                            val = re.search(r'\d+', line)
                            if val:
                                threshold = int(val.group())
                        except Exception:
                            pass
                    elif 'RAG_MODEL' in line and ('=' in line or ':' in line):
                        try:
                            val = re.search(r"['\"]?([a-z\-0-9]+)['\"]?", line)
                            if val:
                                model = val.group(1)
                        except Exception:
                            pass
            except Exception as e:
                print(f"[WARN] Failed to parse config {args.config}: {e}",
                      file=sys.stderr)

    # Threshold check mode (no deps needed)
    if args.threshold_check:
        memory_dir = Path(args.memory_dir)
        is_over = check_threshold(memory_dir, threshold)
        if is_over:
            print(json.dumps({"over_threshold": True, "threshold": threshold}))
            return 0
        else:
            print(json.dumps({"over_threshold": False, "threshold": threshold}))
            return 1

    # Full indexing mode
    if not _HAS_LANCEDB:
        print(
            "[rag_index] LanceDB not installed. Run: "
            "python3 -m ensurepip --upgrade && pip install lancedb  "
            "(see references/rag.md)",
            file=sys.stderr
        )
        sys.exit(2)

    memory_dir = Path(args.memory_dir)
    index_dir = Path(args.index_dir)

    try:
        report = index_memory(memory_dir, index_dir, model, rebuild=args.rebuild, now=now,
                              include_l0=args.include_l0)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    except Exception as e:
        print(f"[rag_index] Fatal error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    exit(main())
