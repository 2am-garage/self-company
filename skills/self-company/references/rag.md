# RAG Playbook — Retrieval-Augmented Memory Search

> **This is Tony's domain.** RAG (Retrieval-Augmented Generation) is an optional vector index over the markdown memory store, speeding up semantic search when memory volume grows large. Shipped **dormant** and **degrades gracefully**; activate with `rag_setup.sh install` (LanceDB + fastembed, local & offline — no Ollama daemon).

---

## 1. What RAG Is Here

The memory substrate is **markdown truth** in `.company/memory/L1-warm/` and `.company/memory/L2-cold/` — readable, auditable, durable. RAG is a **layered vector index** on top, built from that markdown.

**Key principle**: The index is a **derivative, not the source of truth**. Markdown files are the truth; the index can be:
- Rebuilt anytime without loss (content is still in the files).
- Deleted without harm (just lose the speed boost).
- Queried in fallback via `grep` if the index is unavailable.

This design keeps the system **resilient** — RAG failure is annoying, never catastrophic.

---

## 2. When RAG Activates

RAG is not enabled from day one; it waits until the memory store is large enough that semantic search becomes faster than keyword search.

### Activation Criteria

**Threshold**: L1 + L2 active memory count ≥ **50**

- **Below 50**: Full-text `grep` over `.company/memory/` is actually faster to reason about, and simpler. No overhead.
- **At or above 50**: RAG starts to pay for itself; semantic retrieval catches paraphrases that keyword search misses.

### Trigger Modes

1. **Auto-trigger (weekly)**: 
   - Tony's weekly maintenance runs `python3 .company/scripts/rag_index.py --threshold-check`.
   - If L1+L2 count ≥ 50 (script exits 0) → Tony raises an "enable RAG" upgrade candidate to Elon.
   - If still below 50 (script exits 1) → no action needed.

2. **Manual override**:
   - Chairman can order Elon directly: "Enable RAG."
   - Elon approves, Phoebe plans, Tom executes the setup steps (below).

### Status at Ship

Shipped **DORMANT**. No Ollama, no LanceDB, no pip in the environment initially. Scripts exist in `.company/scripts/` but exit gracefully with an actionable message until dependencies are installed.

---

## 3. One-Time Setup (When Activating)

> **CURRENT BACKEND — fastembed (no Ollama).** Activation is now one command:
> ```bash
> bash .company/scripts/rag_setup.sh install
> ```
> This creates a private venv at `.company/.rag-venv` and installs **LanceDB +
> fastembed** (`BAAI/bge-small-en-v1.5`, 384-dim, local CPU, no daemon, fully
> offline). `rag_index.py` / `rag_query.py` auto re-exec into that venv, so
> `python3 .company/scripts/rag_index.py --rebuild --include-l0` and
> `rag_query.py` just work afterwards. Then build: `rag_index.py --rebuild`
> (add `--include-l0` to index working memory too, e.g. for the reinforce path).
>
> The Ollama steps below are **superseded/legacy** — kept only for reference if
> someone prefers an Ollama backend. You can skip them.

When the threshold is crossed or Chairman orders activation, follow these steps in order. This is a one-time task; Tom does it (or a human can during manual activation).

### Step 1: Install Ollama (legacy — superseded by rag_setup.sh)

Ollama runs embedding models locally, offline, without any API calls.

1. Download from [ollama.com](https://ollama.com).
2. Install per your platform instructions (Mac, Linux, Windows).
3. Start the Ollama server: `ollama serve` (runs on `http://localhost:11434` by default).
4. Keep it running in the background (e.g., as a daemon or persistent terminal).

### Step 2: Pull the Embedding Model

Once Ollama is running, fetch the embedding model:

```bash
ollama pull nomic-embed-text
```

This downloads a 274 MB model to your local Ollama cache. It's a one-time download; subsequent runs reuse it.

### Step 3: Bootstrap Python Dependencies

LanceDB requires Python 3.8+. If you don't have pip installed, bootstrap it:

```bash
python3 -m ensurepip --upgrade
```

Alternatively, if you prefer a virtual environment (recommended for isolation):

```bash
python3 -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
```

### Step 4: Install LanceDB

```bash
pip install lancedb
```

LanceDB is a lightweight embedded vector database. No server needed; the index lives in `.company/memory/index/` on disk.

### Verification

After setup, verify the stack is ready:

```bash
# Test Ollama is running
curl http://localhost:11434/api/tags  # should list nomic-embed-text

# Test Python + LanceDB
python3 -c "import lancedb; print('LanceDB OK')"

# Test the RAG scripts (should work now)
python3 .company/scripts/rag_index.py --threshold-check
```

If all commands succeed, RAG is active. If any fails, the scripts will guide you with clear error messages.

---

## 4. How Tony Rebuilds the Index (Weekly Maintenance)

Once activated, RAG requires periodic maintenance to stay in sync with markdown changes.

### Incremental Rebuild (Default, Weekly)

```bash
python3 .company/scripts/rag_index.py
```

This runs **incrementally**:
- Scans all L1/L2 active memory files.
- Computes a content hash (SHA-256) for each file body.
- Skips files whose body is unchanged (no embedding call, fast).
- Embeds changed/new files via Ollama.
- Upserts results into LanceDB.
- Deletes index entries for memories no longer on disk (archived/deleted).

**Output**: JSON summary to stdout, including:
- `embedded`: number of new/changed files embedded.
- `skipped_unchanged`: files skipped due to unchanged content.
- `deleted_stale`: index entries for deleted memories.
- `table_rows`: total rows in the index now.
- `over_threshold`: whether L1+L2 count is still above 50.

**Example output**:
```json
{
  "now": "2026-06-18T14:30:00Z",
  "mode": "incremental",
  "embedded": 3,
  "skipped_unchanged": 47,
  "deleted_stale": 0,
  "table_rows": 50,
  "over_threshold": true,
  "warnings": []
}
```

### Full Rebuild (After Deep Cleanup)

If Tony performs a major reorganization, consolidation, or suspects corruption:

```bash
python3 .company/scripts/rag_index.py --rebuild
```

This **drops the entire LanceDB table** and rebuilds from scratch, re-embedding all L1/L2 memories. Slower but ensures consistency. Still uses content hash for subsequent incremental runs.

### Notes

- Incremental rebuild is fast (only changed files).
- Ollama must be running (`ollama serve`).
- LanceDB index lives at `.company/memory/index/` (gitignored, private).
- If Ollama is unreachable, the script exits with code 2 and a clear message; the rebuild is deferred.

---

## 5. How to Query (Tony or Gibby)

Once the index is built, query it semantically:

```bash
python3 .company/scripts/rag_query.py --query "what does the Chairman prefer for documentation?" --top-k 5
```

### Arguments

| Argument | Default | Meaning |
|---|---|---|
| `--query` | (required) | Natural-language search question |
| `--top-k` | 5 | Number of results to return |
| `--index-dir` | `.company/memory/index` | LanceDB index path |
| `--model` | `nomic-embed-text` | Embedding model (must match rag_index.py) |

### Output (JSON)

```json
[
  {
    "id": "chairman-docs-preference",
    "tier": "L2",
    "path": ".company/memory/L2-cold/preferences/documentation.md",
    "score": 0.87
  },
  {
    "id": "onboarding-docs-request",
    "tier": "L1",
    "path": ".company/memory/L1-warm/sessions/2026-06-10.md",
    "score": 0.72
  },
  ...
]
```

- `score`: similarity score (0–1, higher = more relevant). Computed from cosine distance: `1 - distance`.
- Sorted descending by score (best first).
- `path` points back to the markdown source, so you can read the full context.

### Fallback: No Index

If the index isn't available (not built yet, Ollama down, LanceDB missing):

```bash
# Manual grep fallback
grep -ri "documentation" .company/memory/
```

The script hints at this fallback if it exits with code 2.

---

## 6. Gibby's VERIFY Integration

During memory verification (pipeline stage 4), Gibby uses RAG to surface near-duplicates and potential contradictions, complementing the entropy checks.

### Semantic Duplicate Search

When a new memory is written, Gibby queries the index with the memory's body:

```bash
python3 .company/scripts/rag_query.py \
  --query "$(cat .company/memory/L0-working/new_memory.md | tail -n +5)" \
  --top-k 10
```

(Assumes frontmatter is the first 5 lines; adjust as needed.)

High-similarity hits (score > 0.85) are **flagged as potential duplicates** for manual review.

### Contradiction Detection

Gibby also searches for **opposing keywords** in high-similarity results. For example:

```bash
python3 .company/scripts/rag_query.py \
  --query "Chairman prefers async communication" \
  --top-k 10
```

If the results include a memory like "Chairman prefers synchronous meetings" (high similarity but negating keywords), Gibby flags this as a **contradiction** and asks Tony to investigate.

### Workflow

1. New memory written to L0.
2. Gibby runs semantic query of the body.
3. For each high-similarity result:
   - If similarity > 0.85 and text is nearly identical → reject new memory, return to CAPTURE for re-evaluation.
   - If similarity > 0.85 but keywords conflict → flag as contradiction, ask Tony to reconcile sources.
4. If no conflicts, memory moves to L1 (Phoebe approves placement).

This complements the entropy.py heuristic (Jaccard similarity) by catching paraphrases that keyword matching misses.

---

## 7. Privacy & Security

RAG is **offline only**, no exceptions. This is a hard rule.

- **No API embeddings**: Ollama runs locally; all embeddings computed on your machine.
- **No network egress**: Content never leaves your disk except to Ollama (local HTTP).
- **Gitignored**: `.company/memory/index/` is in `.gitignore`; the index never enters version control.
- **Rebuildable**: Delete the index anytime without loss of information; markdown is the truth.

If you ever share a repo or hand off the computer, wipe `.company/` before leaving (or just delete `.company/memory/index/` to remove the indexed copy).

---

## 8. Graceful Degradation

The RAG stack is designed to fail loud and clear, with fallback instructions, never silently crashing.

### Exit Code 2 Signals

When `rag_index.py` or `rag_query.py` exits with code 2, something is missing.

#### LanceDB Not Installed

```
[rag_index] LanceDB not installed. Run:
  python3 -m ensurepip --upgrade && pip install lancedb
(see references/rag.md § Setup)
```

**Fix**: Follow the setup steps in §3 above.

#### Ollama Not Reachable

```
[rag_index] Ollama not reachable at http://localhost:11434.
Start Ollama:
  ollama serve
Then pull the model:
  ollama pull nomic-embed-text
(see references/rag.md § Setup)
```

**Fix**: Start `ollama serve` in a separate terminal and ensure the model is downloaded.

#### No Index Yet

```
[rag_query] No index yet. Tony must run rag_index.py first, or grep .company/memory directly.
Fallback:
  grep -ri '<your_keywords>' .company/memory
```

**Fix**: Run `rag_index.py` to build the index, or use grep.

### Threshold Check (No Deps Required)

The `--threshold-check` flag works **offline** (no Ollama, no LanceDB):

```bash
python3 .company/scripts/rag_index.py --threshold-check
```

This only counts L1/L2 active memories and exits 0 (at/above threshold) or 1 (below). Useful for automation that doesn't require the full stack.

### Grep Fallback

If RAG is unavailable but you need to search memory, use grep:

```bash
# Search all memory
grep -ri "keyword" .company/memory/

# Search a specific tier
grep -ri "keyword" .company/memory/L1-warm/
grep -ri "keyword" .company/memory/L2-cold/

# Show context
grep -B2 -A2 -ri "keyword" .company/memory/
```

Not as fast as semantic search for paraphrases, but always available.

---

## 9. Troubleshooting

### "No module named lancedb"

**Cause**: LanceDB not installed.  
**Fix**: `pip install lancedb` (see §3 Step 4).

### "Connection refused" when querying Ollama

**Cause**: `ollama serve` not running.  
**Fix**: Start Ollama: `ollama serve` in a new terminal.

### "Embedding model 'nomic-embed-text' not found"

**Cause**: Model not downloaded.  
**Fix**: `ollama pull nomic-embed-text`.

### "Index is out of sync after a markdown edit"

**Cause**: Incremental rebuild skipped an edited file (hash collision, rare).  
**Fix**: Force a full rebuild: `rag_index.py --rebuild`.

### "Query returns no results"

**Cause**: 
- Index is empty (no L1/L2 memories yet).
- Query is too specific / uses domain jargon not in memory.
- RAG not activated yet (below threshold).

**Fix**: 
- Check: `grep -ri "your_keyword" .company/memory/` (grep fallback).
- Verify index: `rag_index.py` returns non-zero `table_rows`.
- Try a broader query.

### "Index file is corrupted / LanceDB won't start"

**Cause**: Disk corruption or LanceDB version mismatch (rare).  
**Fix**: Delete and rebuild: `rm -rf .company/memory/index && rag_index.py --rebuild`.

---

## 10. Summary: RAG Lifecycle

| Stage | Who | Action | Condition |
|---|---|---|---|
| **Monitor** | Tony (weekly) | Run `--threshold-check` | Count L1+L2; assess if RAG is worth enabling |
| **Decide** | Elon | Approve activation | Threshold crossed or Chairman orders |
| **Setup** | Tom (one-time) | Install Ollama, LanceDB, pip (§3) | Before first rebuild |
| **Maintain** | Tony (weekly) | Run `rag_index.py` incremental | Keep index in sync with markdown changes |
| **Query** | Tony, Gibby | Run `rag_query.py` | Search when needed, or during Gibby's VERIFY |
| **Rebuild** | Tony (as needed) | Run `--rebuild` flag | After major cleanup or if corruption suspected |
| **Degrade** | All | Exit 2 + fallback message | When Ollama/LanceDB missing; grep as fallback |

---

## 11. Integration with Company Workflow

- **§3 Weekly Action Breakdown** (in `org/triggers.md`): Step [5] is the RAG rebuild (Tony, Sonnet + rag_index.py). Marked as "deps required (Ollama + LanceDB)"; if deps absent, script exits 2 and step is logged but not a failure.
- **Gibby's VERIFY Loop** (in `references/red-blue-protocol.md`): Semantic query for duplicates/contradictions is one attack surface Gibby can rotate; complements entropy.py heuristics.
- **Policy tunables** (in `org/policy.md § RAG`): `RAG_ENABLE_THRESHOLD`, `RAG_MODEL`, `RAG_INDEX_PATH` are configurable per company instance.

---

## 12. References

- **Authoritative design**: `design/self-company-design.md §8`.
- **RAG build manifest**: `.rag-manifest.md` (specs, schema, build order).
- **Scripts**: `.company/scripts/rag_index.py`, `.company/scripts/rag_query.py`.
- **Index location**: `.company/memory/index/` (LanceDB, gitignored).
- **Memory tiers & frontmatter**: `design/self-company-design.md §2` and §4.
- **Gibby VERIFY integration**: `references/red-blue-protocol.md` (attack surfaces, regression).
- **Policy tunables**: `org/policy.md §8 RAG`.
- **Weekly triggers**: `org/triggers.md §3 Action Breakdown, step [5]`.

---

## Section Outline

1. **What RAG Is Here** — index as derivative, markdown as truth, rebuildable.
2. **When RAG Activates** — threshold 50, auto-trigger via `--threshold-check`, manual override.
3. **One-Time Setup** — Ollama install, model pull, pip bootstrap, LanceDB install, verification.
4. **How Tony Rebuilds** — incremental (default), full rebuild (after cleanup), output summary.
5. **How to Query** — command syntax, JSON output, fallback grep.
6. **Gibby's VERIFY Integration** — semantic duplicate search, contradiction detection, workflow.
7. **Privacy & Security** — offline only, no API embeddings, gitignored, rebuildable.
8. **Graceful Degradation** — exit code 2 signals, actionable messages, grep fallback.
9. **Troubleshooting** — common issues and fixes.
10. **Summary: RAG Lifecycle** — stages and who does what.
11. **Integration with Company Workflow** — links to triggers, verify, policy, scripts.
12. **References** — authoritative design, manifest, scripts, locations.
