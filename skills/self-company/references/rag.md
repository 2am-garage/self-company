# RAG Playbook — Retrieval-Augmented Memory Search

> **Tony's domain.** RAG is a local, offline vector index (LanceDB + fastembed) over the markdown memory store, used to catch semantic matches keyword search misses. Shipped **dormant**; activate with one command:
> ```bash
> bash .company/scripts/rag_setup.sh install
> ```
> This creates a private venv at `.company/.rag-venv` and installs **LanceDB + fastembed** (`BAAI/bge-small-en-v1.5`, 384-dim, local CPU, no daemon, fully offline). No Ollama. Everything degrades gracefully: with no venv the pipeline runs exactly as before.

---

## 1. What RAG Is Here

The memory substrate is **markdown truth** in `.company/memory/L1-warm/` and `.company/memory/L2-cold/` — readable, auditable, durable. RAG is a **vector index** on top, built from that markdown.

**Key principle**: the index is a **derivative, not the source of truth**. Markdown files are the truth; the index can be:
- Rebuilt anytime without loss (content is still in the files).
- Deleted without harm (just lose the speed/recall boost).
- Fallen back from to `grep` if the index is unavailable.

RAG failure is annoying, never catastrophic.

### Two embedding paths (don't confuse them)

There are two consumers of the shared `rag_embed` backend:

1. **In-process semantic dedup (Path A — always used when the venv is present).**
   `reinforce_memory.py` and `entropy.py` call `rag_embed.embed_batch()` directly and compute cosine similarity **in memory** (numpy). They do NOT read the LanceDB index — they embed bodies fresh each run to find near-duplicate / contradiction pairs across all tiers. This is how the daily consolidation matures memories (L0 → L1 → L2).

2. **The LanceDB index (Path B — this playbook).**
   `rag_index.py` builds a persistent vector table; `rag_query.py` queries it by meaning and returns file paths. Index scope is **L1/L2 only** (working L0 is volatile and excluded). This is the path Phase 13 wires into the pipeline (below) and that Stage B (upcoming) will consume for ask-time retrieval.

---

## 2. When RAG Activates

RAG is not enabled from day one; it earns its place once the store is large enough that semantic search beats keyword search.

### The threshold gate

**`RAG_ENABLE_THRESHOLD` = 50** (policy.md §8, tunable). Counts **active L1 + L2** memories (L0 excluded — volatile).

- **Below 50**: keyword `grep` / the Jaccard pass is faster to reason about and simpler. No RAG overhead.
- **At or above 50**: semantic retrieval starts to pay for itself, catching paraphrases keyword search misses.

The gate is the on/off switch, and **the degrade path IS the below-threshold path** — no special-casing. Consumers use keyword/Jaccard below threshold and when the venv is absent; semantic kicks in above.

### Auto-surfacing activation (Phase 13 A.2)

`daily-run.sh` runs `rag_index.py --threshold-check` every run. This is **deps-free** — it only counts active L1+L2, needs no LanceDB/fastembed, and exits 0 (at/over) or 1 (under). When the count is at/over threshold **and the RAG stack is not yet installed**, the daily log surfaces a one-line **"activate RAG" candidate** pointing at `rag_setup.sh install` (Tony → Elon). Below threshold, or once installed, nothing is surfaced.

(This replaces the old aspirational "weekly Tony rebuild" prose — the check is now wired into every daily run.)

### Manual override

Chairman can order activation directly ("Enable RAG"): run `rag_setup.sh install`, then the index refresh below takes over automatically.

### Status at ship

Ships **DORMANT** — no venv, no LanceDB/fastembed. `rag_index.py` / `rag_query.py` exist and exit gracefully with an actionable message until `rag_setup.sh install` runs.

---

## 3. One-Time Setup

```bash
bash .company/scripts/rag_setup.sh install
```

Creates `.company/.rag-venv`, installs LanceDB + fastembed, and warms the embedding model (one-time ~130 MB download of `BAAI/bge-small-en-v1.5`). `rag_index.py` / `rag_query.py` auto re-exec into that venv, so plain `python3 .company/scripts/rag_*.py` just works afterward.

Other subcommands:
```bash
bash .company/scripts/rag_setup.sh status      # INSTALLED / not installed
bash .company/scripts/rag_setup.sh uninstall   # remove the venv
```

The venv lives under `.company/` (gitignored, per-project, never committed).

---

## 4. Index Refresh (Phase 13 A.1 — automatic)

Once activated, the index is kept fresh **automatically** by `daily-run.sh`. There is no separate weekly job.

Each daily run, after the deterministic core has settled (**reinforce → decay → verify → entropy**), the RAG-index step runs an **incremental** rebuild so the index reflects **post-consolidation truth** (absorbed L0 duplicates gone, decay's tier promotions applied):

```bash
python3 .company/scripts/rag_index.py --memory-dir .company/memory --index-dir .company/memory/index
```

The step is:
- **Owned by Tony**, gated like every other core step (`_should_run rag_index`); a company can retune or disable it via `org/schedule.yaml` (Tony's `duties`).
- **Incremental & idempotent**: each L1/L2 body is SHA-256 content-hashed; unchanged bodies are skipped (no re-embed). Re-running changes nothing.
- **L1/L2 only** — no `--include-l0` (Chairman D-A).
- **Never able to fail the core**: it resolves this project's `.company/.rag-venv/bin/python` explicitly; if the venv is absent the step logs one skip line and the already-completed core (decay/verify/entropy/capture) is untouched. A nonzero index run is swallowed (`|| true`) and logged.

What it does each run:
- Scans L1/L2 active memory files (tombstoned skipped).
- Embeds changed/new bodies via fastembed; skips unchanged (content hash).
- Upserts rows into LanceDB.
- Deletes index rows for memories no longer on disk (archived/reaped).

**Daily-log line** (example): `- rag-index: embedded 3 | skipped 47 | deleted-stale 1 | rows 50 (L1/L2 50)`.

### Full rebuild (manual, rare)

After a major reorganization or suspected corruption:
```bash
python3 .company/scripts/rag_index.py --rebuild
# or nuke and rebuild:
rm -rf .company/memory/index && python3 .company/scripts/rag_index.py --rebuild
```

---

## 5. The Index Data Model

- **Location**: `.company/memory/index/` (LanceDB, gitignored, private). Configurable via `RAG_INDEX_PATH` (policy.md §8).
- **Table**: `memory`.
- **Row schema**: `{ id, tier, path, content_hash, vector[384] }`. The index stores **no body text** — only a pointer (`path`) back to the markdown source plus the content hash (for incremental skip) and the embedding vector.
- **Scope**: L1 + L2 active memories only.

Because only paths are stored, retrieval always resolves back to live markdown files (and a stale/tombstoned id simply maps to a file the consumer can re-check).

---

## 6. Querying (manual / ad-hoc today)

Once built, query the index semantically:

```bash
python3 .company/scripts/rag_query.py --query "what does the Chairman prefer for documentation?" --top-k 5
```

| Argument | Default | Meaning |
|---|---|---|
| `--query` | (required) | Natural-language search question |
| `--top-k` | 5 | Number of results |
| `--index-dir` | `.company/memory/index` | LanceDB index path |
| `--model` | (ignored) | Accepted for back-compat; the fastembed model is fixed |

**Output (JSON)** — one row per hit, best first:
```json
[
  { "id": "chairman-docs-preference", "tier": "L2",
    "path": ".company/memory/L2-cold/preferences/documentation.md", "score": 0.87 }
]
```
`score` = cosine similarity (0–1, higher = closer). `path` points back to the markdown source so you can read the full context.

> **Status:** `rag_query.py` is available for manual/ad-hoc use. The **user-facing consumer** — semantic ask-time injection in `hook_memory_inject.py` — is **Stage B of Phase 13 and is NOT wired yet** (keyword injection remains the live path). This section documents the query contract Stage B will build on; it does not yet run in the pipeline.

### Fallback: no index / no venv

```bash
grep -ri "documentation" .company/memory/
```
The scripts hint at this fallback whenever they exit 2.

---

## 7. Privacy & Security

RAG is **offline only** — a hard rule.

- **Local embeddings**: fastembed runs entirely on-device (ONNX/CPU). No API calls, no network at index or query time.
- **Gitignored**: `.company/` (including `memory/index/` and `.rag-venv/`) is in `.gitignore`; the index and venv never enter version control.
- **Rebuildable**: delete the index anytime without information loss — markdown is the truth.

To hand off a repo or machine, wipe `.company/` (or just `.company/memory/index/`) to remove the indexed copy.

---

## 8. Graceful Degradation

The RAG stack fails loud and clear with fallback instructions, never silently.

### Exit code 2 — backend missing

Both `rag_index.py` (full build) and `rag_query.py` exit **2** with an actionable message when the RAG venv isn't installed:
```
[rag_index] RAG backend not installed. Run:
  bash .company/scripts/rag_setup.sh install
(installs LanceDB + fastembed into .company/.rag-venv; see references/rag.md)
```
`rag_query.py` additionally hints the grep fallback and distinguishes "no index yet" (build it first) from "backend unavailable".

### Deps-free threshold check

`--threshold-check` works with **no venv, no LanceDB, no fastembed**:
```bash
python3 .company/scripts/rag_index.py --threshold-check
```
It only counts active L1/L2 and exits 0 (at/over threshold) or 1 (under) — the mechanism behind the daily activation surface (§2).

### Never fails the core

The daily index refresh (§4) is wired so that an absent **or broken** venv, or a nonzero index run, degrades to a single logged line — the deterministic core (reinforce/decay/verify/entropy/capture) always completes and `daily-run.sh` always exits 0.

### Environment escape hatches

- `SC_RAG_REEXEC=1` — suppress the auto re-exec into `.company/.rag-venv` (run under the current interpreter; used by tests and the pipeline's explicit venv invocation).
- `SC_NO_RAG=1` — force the in-process semantic pass off in `entropy.py` (Jaccard-only); the skip reason names the env var, distinct from a genuinely absent backend.

### Grep fallback

```bash
grep -ri "keyword" .company/memory/            # all memory
grep -ri "keyword" .company/memory/L1-warm/    # one tier
grep -B2 -A2 -ri "keyword" .company/memory/    # with context
```

---

## 9. Troubleshooting

### "No module named lancedb / fastembed"
Backend not installed → `bash .company/scripts/rag_setup.sh install`.

### "Index is out of sync after a markdown edit"
Incremental skipped an edited file (rare hash edge) → force a full rebuild: `rag_index.py --rebuild`.

### "Query returns no results"
Index empty (no L1/L2 yet), query too specific, or RAG below threshold. Check with the grep fallback and verify the index has rows (`rag_index.py` reports `table_rows`).

### "Index file is corrupted / LanceDB won't open"
Delete and rebuild: `rm -rf .company/memory/index && rag_index.py --rebuild`.

---

## 10. RAG Lifecycle

| Stage | Who | Action | Condition |
|---|---|---|---|
| **Monitor** | Tony (daily, deps-free) | `--threshold-check` in daily-run surfaces an activation candidate | Active L1+L2 ≥ 50 and venv not installed |
| **Decide** | Elon | Approve activation | Threshold crossed or Chairman orders |
| **Setup** | Tom / human (one-time) | `rag_setup.sh install` | Before first refresh |
| **Refresh** | Tony (daily, automatic) | Incremental `rag_index.py` after reinforce+decay | Keeps the index in sync with markdown |
| **Query** | manual today; Stage B (upcoming) will inject at ask-time | `rag_query.py` | Search by meaning |
| **Rebuild** | Tony (as needed) | `--rebuild` | After major cleanup / corruption |
| **Degrade** | All | Exit 2 + grep fallback; core never fails | Venv absent/broken |

---

## 11. Integration with Company Workflow

- **Daily core** (`daily-run.sh`): the RAG-index refresh + threshold surface run after the reinforce/decay/verify/entropy core, owned by Tony, gated via `org/schedule.yaml`. Venv absent/broken → one logged skip line, core unaffected.
- **In-process dedup** (`reinforce_memory.py`, `entropy.py`): use `rag_embed` directly (Path A, §1) — separate from the LanceDB index.
- **Policy tunables** (`org/policy.md §8`): `RAG_ENABLE_THRESHOLD`, `RAG_MODEL`, `RAG_INDEX_PATH`.
- **Stage B (upcoming)**: semantic ask-time injection in `hook_memory_inject.py` will consume `rag_query.py` with a tight timeout and fall back to today's keyword path. Not yet wired.

---

## 12. References

- **Scripts**: `.company/scripts/rag_setup.sh`, `rag_index.py`, `rag_query.py`, `rag_embed.py`.
- **Index location**: `.company/memory/index/` (LanceDB, gitignored).
- **Policy tunables**: `org/policy.md §8 RAG`.
- **Design**: `design/self-company-design.md §8`.
- **Memory tiers & frontmatter**: `design/self-company-design.md §2` / §4.
