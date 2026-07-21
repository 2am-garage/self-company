#!/usr/bin/env python3
"""
forget_memory — Chairman-driven HARD FORGET (Mike 2026-07-20 Finding 1,
`.company/ops/plans/proposals-2026-07-20.md`).

GAP this closes: decay.py only ever forgets PASSIVELY — time + reinforcement
decide, and even that stays non-destructive for L2 (memory-tiers.md §1/§9:
"accepts contradiction update ... don't delete the old"). There was no way for
the Chairman to say "forget that, it was wrong/private" and have it actually
take effect NOW: not wait on a decay tick, not just get demoted, and not keep
surfacing from the live RAG index until its next scheduled rebuild. This
script is that path, given an EXPLICIT memory id.

DIVISION OF LABOR (unchanged from decay's own soft-delete contract): this
script NEVER physically deletes a markdown file. It only:
  1. Finds the memory by id anywhere under memory_dir (L0/L1/L2, tombstoned or
     not — forget must be idempotent against an id decay already archived)
     and tombstones it RIGHT NOW: `status: archived` + `invalid_at: <today>`,
     via the shared tombstone vocabulary (tombstone.py) and the shared atomic
     writer (frontmatter._atomic_write). The rewrite is an in-place line edit
     — mirrors reinforce_memory._tombstone_absorbed / decay.apply_action's
     "drop" branch — so every OTHER frontmatter field and the body survive
     byte-for-byte. UNCONDITIONAL: unlike decay (which never even reaches an
     L2 record — `if mem["tier"] == "L2": return "l2-keep"`), this OVERRIDES
     L2's normal never-decay rule for this ONE id. An explicit Chairman
     request is not automatic staleness — decay's protection exists to guard
     against premature/accidental forgetting, not against a deliberate one.
  2. Writes a `memory_audit` event: `op="forget"`, `source="forget_memory"` —
     both new vocabulary entries (see memory_audit.py's updated docstring).
     Best-effort / non-blocking, exactly like every other audit call site.
  3. Deletes that ONE id's row from the live LanceDB index immediately (never
     a full `rag_index.py --rebuild`), by shelling out to the project's RAG
     venv and reusing `rag_index.get_or_create_table` + `table.delete`.
     Degrades cleanly — logs one line, never raises — when the RAG venv or
     the index directory is absent; the index simply catches up on its next
     scheduled rebuild.

Physical file removal stays entirely decay.py's job, past its normal
grace-windowed reap — this script only makes the tombstone-and-deindex step
immediate and Chairman-triggered instead of waiting on the daily cycle.

SAFETY (destructive + Chairman-triggered — never fires without explicit
intent):
  (a) requires `--yes`, OR an interactive y/N confirmation.
  (b) refuses a blessed charter axiom (`charter_ids.is_blessed_charter`)
      unless `--force-charter` is also given — an architectural invariant
      can't be forgotten by accident.
  (c) a non-existent id exits nonzero with a clear message and changes
      nothing.

Pure stdlib. The LanceDB delete is isolated to a subprocess under the RAG
venv (rag_venv.venv_python — the same interpreter-resolution helper the rest
of the toolchain uses) so importing or running this module never risks an
`os.execv` of the calling process, unlike a module that imports rag_index
directly at top level (rag_index.py itself re-execs into the venv on import
when lancedb/fastembed aren't present in the current interpreter).

Usage:
    forget_memory.py --forget <id> [--yes] [--force-charter]
                     [--memory-dir DIR] [--index-dir DIR] [--now YYYY-MM-DD]
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Bucket 2 pattern (Phase 14): the shared sibling modules live in THIS
# directory. Put it on sys.path FIRST so the hard imports below resolve under
# every entry point (direct run, import-as-library, test harness).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from frontmatter import _atomic_write          # noqa: E402
from tombstone import is_tombstoned            # noqa: E402
from charter_ids import is_blessed_charter     # noqa: E402
import corpus                                  # noqa: E402

# Pure path resolution only — venv_python() does NOT re-exec anything, unlike
# reexec_if_needed(). Never call reexec_if_needed() here: this module must stay
# safely importable (and its RAG delete safely callable) from an in-process
# test harness without risking an os.execv of the whole process.
from rag_venv import venv_python               # noqa: E402

# Audit log writer (Phase 35). Best-effort: missing/broken audit must never
# block the tombstone.
try:
    import memory_audit
except ImportError:                            # pragma: no cover - defensive
    memory_audit = None


def find_memory(memory_dir, memory_id):
    """Locate the memory record with this EXACT id anywhere under memory_dir
    — L0/L1/L2, tombstoned or not (forget must be able to target an id decay
    already archived too, idempotently). Returns the corpus.load_memories
    record dict (id/tier/status/path/text/body/close_index/fm) or None."""
    for mem in corpus.load_memories(memory_dir, include_archived=True):
        if mem["id"] == memory_id:
            return mem
    return None


def _fm_close_index(text):
    """Closing-fence line index in text.split('\\n'), or -1 if there is no
    valid frontmatter block. Mirrors reinforce_memory.parse_frontmatter's own
    private helper (same `.strip()=='---'`, opening-fence-must-be-line-0
    contract)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i
    return -1


def tombstone_now(mem, today):
    """Rewrite mem's frontmatter IN PLACE to `status: archived` + `invalid_at:
    <today>`, UNCONDITIONALLY — any tier, any decay_score, including L2 (an
    explicit forget overrides L2's normal never-decay rule for this one id).
    Idempotent: an existing `invalid_at` anchor is never reset (mirrors
    decay.apply_action's "drop" branch — the grace window always runs from the
    FIRST invalidation, not the most recent forget).

    Returns `(new_content, old_status)`, or `(None, None)` if mem's
    frontmatter is unparseable (defensive — corpus.load_memories already
    required a valid id, so real data never hits this)."""
    text = mem["text"]
    close = _fm_close_index(text)
    if close < 0:
        return None, None
    lines = text.split("\n")
    old_status = mem["fm"].get("status")
    had_invalid_at = bool(mem["fm"].get("invalid_at"))
    seen = set()
    for i in range(1, close):
        key = lines[i].split(":", 1)[0].strip() if ":" in lines[i] else ""
        if key == "status":
            lines[i] = "status: archived"
            seen.add("status")
        elif key == "invalid_at":
            if not had_invalid_at:
                lines[i] = f"invalid_at: {today}"
            seen.add("invalid_at")
    inserts = []
    if "status" not in seen:
        inserts.append("status: archived")
    if "invalid_at" not in seen:
        inserts.append(f"invalid_at: {today}")
    if inserts:
        lines[close:close] = inserts
    return "\n".join(lines), old_status


def _rag_delete_id(memory_id, company_dir, index_dir=None):
    """Best-effort: delete `memory_id`'s row from the live LanceDB index RIGHT
    NOW (single-id delete, never a rebuild). Runs entirely inside a subprocess
    under the project's RAG venv (rag_venv.venv_python) — this process itself
    never imports lancedb/rag_index. Always returns a short status string;
    NEVER raises. Degrades cleanly (a one-line "skipped: ..." status) when the
    venv or the index directory is absent — the index catches up on its next
    scheduled rebuild."""
    company_dir = Path(company_dir)
    index_dir = Path(index_dir) if index_dir else company_dir / "memory" / "index"
    venv_py = venv_python(company_dir)
    if not venv_py.exists():
        return ("skipped: RAG venv absent ("
                f"{venv_py} not found) — index catches up on its next "
                "scheduled rebuild")
    if not index_dir.exists():
        return f"skipped: RAG index absent ({index_dir} not built yet) — nothing to deindex"

    scripts_dir = str(Path(__file__).resolve().parent)
    safe_id = memory_id.replace("'", "''")   # LanceDB filter is a SQL string literal
    prog = (
        "import sys, lancedb\n"
        f"sys.path.insert(0, {scripts_dir!r})\n"
        "import rag_index as R\n"
        f"db = lancedb.connect({str(index_dir)!r})\n"
        "table = R.get_or_create_table(db)\n"
        "if table is not None:\n"
        f"    table.delete(\"id = '{safe_id}'\")\n"
        "print('deleted')\n"
    )
    try:
        proc = subprocess.run(
            [str(venv_py), "-c", prog],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "SC_RAG_REEXEC": "1"},
        )
    except Exception as e:
        return f"skipped: RAG delete subprocess failed to launch: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        return f"skipped: RAG delete subprocess exited {proc.returncode}: {(err[-1] if err else '')[:300]}"
    return "deleted from live index"


def _confirm(memory_id, tier, already_tombstoned):
    """Interactive y/N confirmation. Returns True only on an explicit 'y'/'yes'
    (case-insensitive); EOF (no tty / piped-closed stdin) is treated as 'no' —
    never forget without explicit intent."""
    prompt = (
        f"Forget memory {memory_id!r} (tier {tier}"
        f"{', already tombstoned' if already_tombstoned else ''})? "
        "This tombstones it immediately and de-indexes it from RAG. [y/N] "
    )
    try:
        reply = input(prompt)
    except EOFError:
        reply = ""
    return reply.strip().lower() in ("y", "yes")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="forget_memory — Chairman-driven hard forget: tombstone + "
                    "deindex one EXPLICIT memory id immediately."
    )
    ap.add_argument("--memory-dir", default=".company/memory",
                    help="Root memory directory (default: .company/memory)")
    ap.add_argument("--forget", "--id", dest="memory_id", required=True,
                    metavar="ID", help="the EXACT memory id to forget")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="skip the interactive y/N confirmation")
    ap.add_argument("--force-charter", action="store_true",
                    help="allow forgetting a blessed charter axiom "
                         "(refused by default)")
    ap.add_argument("--now", help="reference date YYYY-MM-DD (default: today)")
    ap.add_argument("--index-dir",
                    help="LanceDB index dir (default: <company>/memory/index)")
    args = ap.parse_args(argv)

    memory_dir = Path(args.memory_dir)
    company_dir = memory_dir.parent if memory_dir.name == "memory" else Path(".company")
    today = args.now if args.now else datetime.now().strftime("%Y-%m-%d")

    mem = find_memory(memory_dir, args.memory_id)
    if mem is None:
        print(f"[forget_memory] no memory found with id={args.memory_id!r} "
              f"under {memory_dir} — nothing changed", file=sys.stderr)
        return 1

    if is_blessed_charter(mem["fm"]) and not args.force_charter:
        print(f"[forget_memory] REFUSED: id={args.memory_id!r} is a blessed "
              "charter axiom (architectural invariant) — pass --force-charter "
              "to override this explicitly. Nothing changed.", file=sys.stderr)
        return 1

    already = is_tombstoned(mem["fm"])
    if not args.yes and not _confirm(args.memory_id, mem["tier"], already):
        print("[forget_memory] aborted — nothing changed.", file=sys.stderr)
        return 1

    new_content, old_status = tombstone_now(mem, today)
    if new_content is None:
        print(f"[forget_memory] id={args.memory_id!r}: unparseable "
              f"frontmatter at {mem['path']} — refusing to rewrite. "
              "Nothing changed.", file=sys.stderr)
        return 1

    _atomic_write(mem["path"], new_content, encoding="utf-8")

    if memory_audit:
        memory_audit.audit_event(str(company_dir), "forget", args.memory_id,
                                 "status", old_status, "archived", "forget_memory")

    rag_status = _rag_delete_id(args.memory_id, company_dir, index_dir=args.index_dir)

    print(f"[forget_memory] id={args.memory_id!r} tombstoned (status: archived, "
         f"invalid_at: {today}) at {mem['path']}; RAG: {rag_status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
