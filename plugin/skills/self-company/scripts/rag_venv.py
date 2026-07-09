#!/usr/bin/env python3
"""
rag_venv — SINGLE authoritative source for the RAG venv re-exec + the
`.rag-venv/bin/python` interpreter-path resolution.

Before Phase 22 this logic was copy-pasted as four near-identical
`_reexec_into_rag_venv()` bodies (rag_query.py, rag_index.py,
reinforce_memory.py, entropy.py — rag_query/rag_index byte-identical) and the
`.rag-venv/bin/python` literal was open-coded across seven files. Exactly the
drift class `frontmatter.py` / `tombstone.py` were built to kill: `entropy.py`
had already grown a subtly different variant (the `--memory-dir` project
resolution + the import-time-safety carve-out). Consolidating the mechanism
HERE means the re-exec semantics can never drift between scanners again.

Imported the same best-effort way as `tombstone.py` / `charter_ids.py`: siblings
in this directory, put on `sys.path` first, then a plain import.

Pure stdlib, no side effects on import — importing this module NEVER re-execs
(that is what lets stdlib-only `entropy.py` stay import-safe from a base python).
The re-exec fires only when a caller explicitly calls `reexec_if_needed()`.
"""

import os
import sys
from pathlib import Path


def venv_python(company):
    """Path to the RAG venv interpreter for a given root.

    `company` is the directory whose `.rag-venv/bin/python` is the target
    interpreter — either a project's `.company` dir (the daily/hook callers) or
    the skill dir that ships the plugin-local venv. Returns a `Path`; it may or
    may not exist (callers stat/`os.access` it).
    """
    return Path(company) / ".rag-venv" / "bin" / "python"


def reexec_if_needed(probe_modules, mem_dir=None):
    """Re-launch the current script under the project's `.rag-venv` python when
    the RAG backend isn't importable in this interpreter.

    `probe_modules` is the list of module names the caller needs (e.g.
    `["lancedb", "fastembed"]` for the query/index path, `["fastembed", "numpy"]`
    for the reinforce/entropy semantic pass). They are imported in order; if all
    import, the backend is present and this is a no-op.

    Semantics preserved byte-for-byte from the four legacy copies:
      * `SC_RAG_REEXEC` set  -> already inside the venv (the re-exec'd child) or
        told not to re-exec -> no-op short-circuit.
      * every `probe_modules` entry importable here -> no-op.
      * otherwise `os.execv` the FIRST existing candidate interpreter, setting
        `SC_RAG_REEXEC=1` so the child short-circuits instead of looping.

    Candidate order (first existing wins):
      1. the skill-local venv (`scripts/../.rag-venv`) that ships with the plugin.
      2. the project venv. When `mem_dir` is given the project root is
         `Path(mem_dir).resolve().parent` (mem_dir's parent is that project's
         `.company`) — so an off-cwd caller like `entropy.py --memory-dir` targets
         the RIGHT project instead of whatever `.company` happens to sit under
         cwd. When `mem_dir` is None the project root is `<cwd>/.company`,
         preserving the query/index/reinforce cwd-based fallback exactly.

    Returns normally when no re-exec happened; otherwise never returns (execv).

    NOTE: `SC_NO_RAG` (entropy's force-Jaccard-only switch) is intentionally NOT
    checked here — it is not universal across callers. entropy guards on it at
    its own callsite before delegating, keeping every caller byte-identical.
    """
    if os.environ.get("SC_RAG_REEXEC"):
        return
    for name in probe_modules:
        try:
            __import__(name)
        except Exception:
            break
    else:
        return  # every probe module imported -> backend present, no re-exec
    here = Path(__file__).resolve().parent          # the scripts dir
    if mem_dir is not None:
        project = Path(mem_dir).resolve().parent
    else:
        project = Path.cwd() / ".company"
    for cand in (venv_python(here.parent), venv_python(project)):
        if cand.exists():
            os.environ["SC_RAG_REEXEC"] = "1"
            os.execv(str(cand), [str(cand)] + sys.argv)
