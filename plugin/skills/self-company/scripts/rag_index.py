#!/usr/bin/env python3
"""
RAG Index Builder — Index memory for semantic search (self-company).

Scans .company/memory/ for L1 and L2 markdown files, computes embeddings via the
local fastembed backend (rag_embed, in .company/.rag-venv), stores in LanceDB
(local, offline). Index is a derivative of markdown truth; always rebuildable.

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
# tombstoned memory can never leak into the RAG index. The sibling modules live in
# THIS directory; put it on sys.path FIRST so the hard imports below resolve under
# every entry point (direct run, venv re-exec, cron, test harness).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tombstone import TOMBSTONE_STATUSES, is_tombstoned

# Phase 11 Item 2: the fragile frontmatter delimiter + key:value split seam is the
# ONE shared module (frontmatter.py). rag keeps its OWN typed interpretation
# (tier/status validation, sources list parse, _parse_errors) on top; only the
# fence-location + body-split is delegated to the shared split().
from frontmatter import split as _fm_split
import urllib.error

# ============================================================================
# RE-EXEC INTO THE RAG VENV (created by rag_setup.sh) if deps aren't here
# ============================================================================
# The ONE shared copy (rag_venv.py, same dir): re-launch under .company/.rag-venv
# python when lancedb/fastembed aren't importable, so RAG 'just works'.
from rag_venv import reexec_if_needed

reexec_if_needed(["lancedb", "fastembed"])

# Shared local embedding backend (fastembed). Lazy fastembed import inside.
try:
    import rag_embed
    _HAS_EMBED = True
except Exception:
    _HAS_EMBED = False

# Phase 24 Item 1: model-stamp the index so a query can never silently score
# against a different model's vectors (same dim does not mean same space).
from rag_stamp import read_stamp, write_stamp, stamp_matches

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

# Phase 24 Item 1: single source of truth for model + dim, imported from
# rag_embed (was a duplicated literal here before this phase). rag_embed.py is
# pure stdlib until embed() lazily imports fastembed, so this import cannot fail
# for lack of the venv; the literal fallback below is belt-and-suspenders only.
if _HAS_EMBED:
    RAG_MODEL = rag_embed.RAG_EMBED_MODEL
    EMBEDDING_DIM = rag_embed.EMBEDDING_DIM
else:                                                          # pragma: no cover
    RAG_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    EMBEDDING_DIM = 384


# ============================================================================
# EXCEPTIONS
# ============================================================================

class EmbeddingUnavailable(Exception):
    """Raised when the local embedding backend (fastembed in .company/.rag-venv)
    is unavailable — the shared signal the index path degrades on (exit 2)."""
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


def incremental_up_to_date(prev: Dict[str, Any], content_hash: str,
                           path_str: str, tier: str) -> bool:
    """BOB-F1: decide whether an already-indexed memory may be SKIPPED (not
    re-embedded) on an incremental refresh.

    A row is up to date ONLY when the live file's body hash AND its stored path
    AND its stored tier all still match the indexed row. Hashing the body alone
    is insufficient: an L1->L2 promotion MOVES the file (L1-warm ->
    L2-cold/<cat>) and flips tier L1->L2 while leaving the body byte-identical,
    so a hash-only check would skip it and leave the index row pointing at the
    dead L1 path — and both consumers drop any hit whose path isn't a live file,
    silently dropping the promoted memory from recall. Returning False here forces
    a re-embed/refresh so path+tier track reality. `prev` is the stored row dict
    {content_hash, path, tier}. Pure — no deps, unit-testable without a venv.

    C1 (fold-in): the path comparison is REALPATH-normalized on both sides (mirrors
    hook_memory_inject.semantic_top's `_norm`), so a relative-vs-absolute
    `--memory-dir` invocation for the SAME file compares equal instead of forcing a
    needless full re-embed. `os.path.realpath` does not require the path to exist,
    so this stays pure and works on the synthetic paths the unit tests use."""
    def _norm(p):
        try:
            return os.path.realpath(p) if p else p
        except Exception:
            return p
    return (prev.get("content_hash", "") == content_hash
            and _norm(prev.get("path", "")) == _norm(path_str)
            and prev.get("tier", "") == tier)


# ============================================================================
# EMBEDDING (local fastembed backend — see rag_embed.py)
# ============================================================================

def embed(text: str) -> List[float]:
    """
    Embed text via the local fastembed backend (rag_embed). Raises
    EmbeddingUnavailable if fastembed / the .company/.rag-venv isn't installed.
    """
    if not _HAS_EMBED:
        raise EmbeddingUnavailable("rag_embed/fastembed not importable")
    try:
        return rag_embed.embed(text)
    except Exception as e:
        raise EmbeddingUnavailable(f"local embedding failed: {e}")


# ============================================================================
# LANCEDB TABLE OPERATIONS
# ============================================================================

def get_or_create_table(db, table_name: str = "memory"):
    """
    Get existing LanceDB table or create it if absent.
    Returns the table object.
    """
    try:
        # Check if table exists. `list_tables()` is the current LanceDB API;
        # the older `table_names()` is deprecated (emits a DeprecationWarning in
        # daily-run logs). NOTE: newer LanceDB returns a `ListTablesResponse`
        # object (names under `.tables`), NOT a bare list — a naive
        # `name in db.list_tables()` is always False on it, which would make
        # get_or_create_table report every existing table as absent and silently
        # overwrite it on each run (breaking incremental refresh). Extract the
        # names list defensively so both the object and a plain-list return work.
        listed = db.list_tables()
        tables = getattr(listed, "tables", listed)
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
    Scan memory_dir for L1/L2 active files, embed via fastembed, upsert to LanceDB.

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

    # Load existing rows if incremental. BOB-F1: the incremental cache keys on
    # (content_hash, path, tier), NOT content_hash alone. When decay promotes a
    # memory L1->L2 it MOVES the file (same id, same body) into L2-cold/<cat>/;
    # the body is unchanged, so a hash-only skip would `continue` and leave the
    # stored row pointing at the dead L1-warm path with tier: L1 — both consumers
    # (hook_memory_inject.semantic_top, employee.recall_shared) drop any hit whose
    # path isn't a live file, so the promoted, highest-value memory silently
    # vanishes from semantic recall. Tracking path+tier lets us re-embed/refresh
    # exactly those moved rows while still skipping the common unchanged case.
    existing_rows = {}
    if not rebuild:
        try:
            db = lancedb.connect(str(index_dir))
            table = get_or_create_table(db)
            if table:
                existing_table_rows = table.search().to_list()
                # Phase 24 Item 1 — model-stamp self-heal (Phase 12b pattern): a
                # table with real rows but a stamp that doesn't match the
                # CURRENT embedding model (mismatched, or absent — a legacy
                # pre-Phase-24 index never wrote one) cannot be trusted for an
                # incremental refresh: unchanged rows would keep OLD-model
                # vectors sitting beside newly-embedded NEW-model vectors in the
                # SAME cosine space, silently corrupting every future query (same
                # dim, so no crash — just wrong answers). Force a full rebuild
                # exactly once; the loop below then re-embeds everything and the
                # new stamp is written at the end. A brand-new/empty table is NOT
                # a mismatch (nothing to migrate) — the check only fires when
                # there is real prior data.
                if existing_table_rows:
                    stamp = read_stamp(index_dir)
                    if not stamp_matches(stamp, RAG_MODEL, EMBEDDING_DIM):
                        report["warnings"].append(
                            "model-stamp mismatch (index stamp="
                            f"{stamp!r}, current={{'model': {RAG_MODEL!r}, "
                            f"'dim': {EMBEDDING_DIM}}}) — forcing full rebuild "
                            "for cosine-space safety"
                        )
                        rebuild = True
                        report["mode"] = "rebuild (stamp-forced)"
                if not rebuild:
                    for row in existing_table_rows:
                        existing_rows[row["id"]] = {
                            "content_hash": row.get("content_hash", ""),
                            "path": row.get("path", ""),
                            "tier": row.get("tier", ""),
                        }
        except Exception as e:
            report["warnings"].append(f"Failed to load existing index: {e}")

    # Pre-flight: prove the local embedding backend is available before scanning.
    try:
        _ = embed("test")
    except EmbeddingUnavailable:
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

        # Skip if unchanged (incremental mode). BOB-F1: "unchanged" means body
        # hash AND stored path AND stored tier all still match the live file. A
        # promotion move changes path (L1-warm -> L2-cold/<cat>) and tier
        # (L1 -> L2) while leaving the body identical — such a row must NOT be
        # skipped; it is re-embedded below so its stored path/tier track reality
        # (upsert_rows deletes-then-adds by id, so the stale row is replaced).
        if not rebuild and mem["id"] in existing_rows:
            if incremental_up_to_date(existing_rows[mem["id"]], content_hash,
                                      str(path), mem["tier"]):
                report["skipped_unchanged"] += 1
                continue

        # Embed body (backend availability already proven by the pre-flight above).
        try:
            vec = embed(body)
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
                "vector": vec,
                # Phase 24 Item 4: store the embedded body text so an FTS
                # (BM25) index can be built alongside the vector column —
                # a deliberate reversal of the old "index stores no body"
                # stance (references/rag.md §7). Acceptable because the
                # index is gitignored/local and always rebuildable from the
                # markdown truth; nothing new leaves the machine.
                "text": body,
            }
            rows_to_add.append(row)
            report["embedded"] += 1

        except EmbeddingUnavailable as e:
            # Pre-flight already proved the backend present, so this is a transient
            # per-item failure: skip that one memory with a warning rather than
            # aborting the whole index build.
            report["warnings"].append(f"{mem['id']}: embedding failed: {e}")
            continue

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

            # Phase 24 Item 4: (re)build the FTS (BM25) index on the `text`
            # column so rag_query.py's hybrid search has a lexical leg for
            # exact-token queries (script names, ids, error strings) that
            # cosine alone misses. Native LanceDB FTS — zero new dependencies.
            # Best-effort: an empty/near-empty table or a transient index-build
            # hiccup must never fail the whole run (the vector-only path still
            # works without it — rag_query degrades to vector search).
            if report["table_rows"] > 0:
                try:
                    table.create_fts_index("text", replace=True)
                except Exception as e:
                    report["warnings"].append(f"FTS index build failed: {e}")

        # Phase 24 Item 1: stamp the index with the model actually used to
        # produce every vector now in the table (fresh build or the common
        # unchanged-rows-preserved incremental case — both are internally
        # consistent by construction: forced rebuild re-embeds everything above,
        # and the non-forced path only reaches here when the prior stamp already
        # matched). Best-effort; a failure here degrades to "no stamp" on the
        # NEXT run, which is itself handled (treated as mismatch -> rebuild).
        write_stamp(index_dir, RAG_MODEL, EMBEDDING_DIM)

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
        help=f"embedding model, informational (fastembed model is fixed; default: {RAG_MODEL})"
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
             "Deps-free: does NOT require the RAG venv (LanceDB/fastembed)."
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
            "[rag_index] RAG backend not installed. Run:\n"
            "  bash .company/scripts/rag_setup.sh install\n"
            "(installs LanceDB + fastembed into .company/.rag-venv; see references/rag.md)",
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
