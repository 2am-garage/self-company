# RAG Playbook — Retrieval-Augmented Memory Search

> **Tony's domain.** RAG is a local, offline vector index (LanceDB + fastembed) over the markdown memory store, used to catch semantic matches keyword search misses. It is **wired into the pipeline** as of Phase 13 — the daily index refresh (Stage A) and ask-time semantic injection (Stage B, v0.1.5) both run live. As of **Phase 24** the index is also **hybrid** (vector + BM25/FTS fused via RRF, §7) and **model-stamped** (§13 — the migration mechanism for a future embedding-model swap). The one piece that isn't pre-installed is the local venv; until you run the one command below the company transparently uses the keyword floor:
> ```bash
> bash ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_setup.sh install
> ```
> This creates a private venv at `.company/.rag-venv` and installs **LanceDB + fastembed** (`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, 384-dim, MULTILINGUAL, local CPU, no daemon, fully offline). No Ollama. Everything degrades gracefully: with no venv the pipeline runs exactly as before.
>
> **Phase 24 — why the model changed.** The original `BAAI/bge-small-en-v1.5` was English-only: it compressed every query (any language) into an undiscriminating 0.45–0.65 cosine band, so the injection floor filtered nothing, and the Chairman's default language (Traditional Chinese) retrieved WORSE than random — an off-topic Chinese prompt injected the same memory an on-topic one would. The multilingual swap fixes this; see §1 and §13 for the full diagnosis, measurement, and rollback story.

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
   `rag_index.py` builds a persistent vector table; `rag_query.py` queries it by meaning and returns file paths. Index scope is **L1/L2 only** (working L0 is volatile and excluded). This is the path Phase 13 wires into the pipeline (below) and that Stage B (shipped v0.1.5) consumes for ask-time retrieval.

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

The RAG **logic is wired** (daily index refresh + ask-time semantic injection), but the **venv ships uninstalled** — no LanceDB/fastembed until `rag_setup.sh install` runs. Until then `rag_index.py` / `rag_query.py` exit gracefully with an actionable message and the ask-time hook falls back to the keyword path, so the company runs exactly as before.

---

## 3. One-Time Setup

```bash
bash ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_setup.sh install
```

Creates `.company/.rag-venv`, installs LanceDB + fastembed, and warms the embedding model (one-time ~470 MB download of `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, multilingual). `rag_index.py` / `rag_query.py` auto re-exec into that venv, so plain `python3 ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_*.py` just works afterward.

Other subcommands:
```bash
bash ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_setup.sh status      # INSTALLED / not installed
bash ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_setup.sh uninstall   # remove the venv
```

The venv lives under `.company/` (gitignored, per-project, never committed).

---

## 4. Index Refresh (Phase 13 A.1 — automatic)

Once activated, the index is kept fresh **automatically** by `daily-run.sh`. There is no separate weekly job.

Each daily run, after the deterministic core has settled (**reinforce → decay → verify → entropy**), the RAG-index step runs an **incremental** rebuild so the index reflects **post-consolidation truth** (absorbed L0 duplicates gone, decay's tier promotions applied):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_index.py --memory-dir .company/memory --index-dir .company/memory/index
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
python3 ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_index.py --rebuild
# or nuke and rebuild:
rm -rf .company/memory/index && python3 ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_index.py --rebuild
```

---

## 5. The Index Data Model

- **Location**: `.company/memory/index/` (LanceDB, gitignored, private). Configurable via `RAG_INDEX_PATH` (policy.md §8).
- **Table**: `memory`.
- **Row schema**: `{ id, tier, path, content_hash, vector[384], text }`. **Phase 24 Item 4** added `text` (the embedded body) so a native FTS (BM25) index can be built alongside the vector column — see §7 for why storing body text is now acceptable (a reversal of the original "no body in index" stance).
- **FTS index**: a native LanceDB full-text index on `text` (Tantivy-or-native backend, no new dependency), rebuilt after every index run. Backs the lexical leg of hybrid search (§7).
- **Model stamp**: `.rag_stamp.json` — a small JSON sidecar next to the LanceDB table (`{"model": ..., "dim": ..., "lib": ...}` — model name, embedding dimension, and the fastembed library version), written by `rag_index.py` after every successful build. See §13 for the full migration/rollback mechanism this enables.
- **Scope**: L1 + L2 active memories only.

Because paths are always stored, retrieval always resolves back to live markdown files (and a stale/tombstoned id simply maps to a file the consumer can re-check) — the `text` column is a query-time optimization, never the source of truth; the live markdown body is what gets injected.

---

## 6. Querying (ad-hoc CLI + live ask-time injection)

Once built, query the index semantically:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_query.py --query "what does the Chairman prefer for documentation?" --top-k 5
```

| Argument | Default | Meaning |
|---|---|---|
| `--query` | (required) | Natural-language search question |
| `--top-k` | 5 | Number of results |
| `--index-dir` | `.company/memory/index` | LanceDB index path |
| `--model` | (ignored) | Accepted for back-compat; the fastembed model is fixed |
| `--query-type` | `hybrid` | `hybrid` (Phase 24 Item 4: vector + BM25/FTS fused via RRF) or `vector` (pure cosine, the pre-Item-4 path). Hybrid degrades to vector automatically on any error (e.g. an index built before the FTS column/index existed). |

**Output (JSON)** — one row per hit, best first:
```json
[
  { "id": "chairman-docs-preference", "tier": "L2",
    "path": ".company/memory/L2-cold/preferences/documentation.md", "score": 0.87 }
]
```
`score` = cosine similarity (0–1, higher = closer). `path` points back to the markdown source so you can read the full context.

> **Status:** `rag_query.py` is available for manual/ad-hoc use **and** is the backend for TWO live consumers of the SHARED index: (1) the **ask-time** semantic injection in `hook_memory_inject.py` — Stage B of Phase 13 (v0.1.5); and (2) the **dispatch-time** shared read in `Employee.recall_shared` / `dispatch_context` — Phase 18c. On each `UserPromptSubmit` the hook queries the index semantically (tight timeout) and injects the top matches, falling back to the keyword path when the venv is absent. At **dispatch**, a `shared_memory_read` employee (elon by default) reads the SAME shared index through the SAME query engine and gate before its headless `claude -p` worker runs — so the read side of shared memory is no longer interactive-hook-only. Both re-validate hits against the live files (skip tombstoned/deleted) and share the `SELF_COMPANY_INJECT_RAG_MIN_SCORE` gate. This section documents the query contract both consumers run on.

### Fallback: no index / no venv

```bash
grep -ri "documentation" .company/memory/
```
The scripts hint at this fallback whenever they exit 2.

---

## 7. Privacy & Security, and Hybrid Retrieval (Phase 24 Item 4)

RAG is **offline only** — a hard rule.

- **Local embeddings**: fastembed runs entirely on-device (ONNX/CPU). No API calls, no network at index or query time.
- **Gitignored**: `.company/` (including `memory/index/` and `.rag-venv/`) is in `.gitignore`; the index and venv never enter version control.
- **Rebuildable**: delete the index anytime without information loss — markdown is the truth.

To hand off a repo or machine, wipe `.company/` (or just `.company/memory/index/`) to remove the indexed copy.

### Body-in-index (a deliberate reversal)

Before Phase 24, the index stored **no body text** — only `{id, tier, path, content_hash, vector}` — specifically so the index held nothing the markdown didn't already hold; a leaked/copied index was never more sensitive than the markdown itself, and retrieval always resolved back through `path` to the live file.

**Phase 24 Item 4 adds a `text` column** (the embedded body) so a native BM25/FTS index can be built alongside the vector column. The rationale is that pure cosine can miss exact-token queries (script names, ids, error strings), and Anthropic's contextual-retrieval work measured embeddings+BM25 hybrid cutting top-20 retrieval failure ~49% on large corpora.

> **Measured caveat (be honest — this was Gibby-checked).** On OUR current corpus (~55 active memories, multilingual MiniLM), hybrid's effect is **marginal to nil**: across a set of exact-identifier probes (`PR #28`, `id_ed25519`, `nomic-embed-text 768`, `BACKUP_KEEP tarballs`, …) hybrid returned **byte-identical rankings** to pure vector — the multilingual model's subword tokenizer already places these tokens well, and BM25 rescued nothing cosine missed. Item 4 therefore ships as a **safety net with headroom** (it costs nothing extra — it rode Item 1's one rebuild, adds no dependency, and the gate placement below makes it strictly no-worse than vector), not as a measured win on today's data. Its value should reappear as the corpus grows or gains more code-token-heavy memories; re-measure then before claiming a benefit.

This is safe because nothing about the trust boundary changed:
- The index (now including `text`) is **still gitignored, still local, still rebuildable** from the markdown truth in one command — it just now redundantly duplicates content that was already sitting in `.company/memory/` on the same machine.
- **No new dependency, no new network exposure.** LanceDB's FTS is native (Tantivy-or-native backend, ships with `lancedb` already installed by `rag_setup.sh`).
- Retrieval still ultimately re-validates and injects the **live markdown body** via `path` (never the indexed copy) — every consumer's re-validation logic (hook_memory_inject, `Employee.recall`/`recall_shared`) is unchanged by this.

### Hybrid query (vector + BM25/FTS, fused via RRF)

`rag_query.py` defaults to `query_type="hybrid"`: it runs a vector (cosine) search and a full-text (BM25) search over `text`, then fuses the two rankings with LanceDB's `RRFReranker` (Reciprocal Rank Fusion). The intent is to catch a query for an exact script name or error string that pure cosine ranks low but BM25 ranks first — though, per the measured caveat above, that scenario doesn't yet occur on our small corpus.

**Gate placement — the one real risk (Elon's explicit flag).** RRF produces a rank-fusion score, not a cosine similarity — it must never be used as, or silently replace, the relevance floor (`SELF_COMPANY_INJECT_RAG_MIN_SCORE`) every consumer gates on. So `rag_query.py`'s hybrid path ALWAYS returns the TRUE vector-leg cosine as `score`:
- A hit the vector leg also found carries a restored `_distance` -> `score = 1 - distance`, identical to the pure-vector path.
- An **FTS-only** hit (the vector leg never surfaced it — e.g. an exact token match at low semantic similarity) has no `_distance` to restore, so `rag_query.py` computes the cosine directly against that row's own stored vector and the query vector. It is **never** given a free pass past the floor — an FTS-only hit still has to clear `RAG_MIN_SCORE` like any other hit.

Net effect: every consumer's existing floor-gating code is untouched by Item 4 — hybrid mode returns the same `{id, tier, path, score}` shape with an honest cosine `score`, so "off-topic prompt injects nothing" (Item 2's tests) stays true post-fusion with zero consumer-side changes. `--query-type vector` is still available (CLI + `query_rag(..., query_type="vector")`) for the pure pre-Item-4 behavior, and hybrid mode itself degrades to vector automatically on any error (e.g. querying an index built before the FTS column/index existed).

---

## 8. Graceful Degradation

The RAG stack fails loud and clear with fallback instructions, never silently.

### Exit code 2 — backend missing

Both `rag_index.py` (full build) and `rag_query.py` exit **2** with an actionable message when the RAG venv isn't installed:
```
[rag_index] RAG backend not installed. Run:
  bash ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_setup.sh install
(installs LanceDB + fastembed into .company/.rag-venv; see references/rag.md)
```
`rag_query.py` additionally hints the grep fallback and distinguishes "no index yet" (build it first) from "backend unavailable".

### Deps-free threshold check

`--threshold-check` works with **no venv, no LanceDB, no fastembed**:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_index.py --threshold-check
```
It only counts active L1/L2 and exits 0 (at/over threshold) or 1 (under) — the mechanism behind the daily activation surface (§2).

### Never fails the core

The daily index refresh (§4) is wired so that an absent **or broken** venv, or a nonzero index run, degrades to a single logged line — the deterministic core (reinforce/decay/verify/entropy/capture) always completes and `daily-run.sh` always exits 0.

### Stale or missing model stamp (Phase 24 Item 1)

`rag_query.py` refuses to score against an index whose model stamp doesn't match the CURRENT embedding model (or has no stamp at all — a legacy pre-Phase-24 index). It raises the SAME `FileNotFoundError`/exit-2 signal as "no index yet", which every consumer already treats as index-absent — no per-consumer special-casing needed:
```
[rag_query] Index at ... has a stale or missing model stamp (stamp=..., current model=...) — treat as absent; rebuild with rag_index.py --rebuild (auto-heals on the next daily tick).
```
`rag_index.py` self-heals this automatically on its NEXT run (even an incremental one, no flag needed): a table with real rows but a mismatched/absent stamp forces a full rebuild, then writes the new stamp. See §13 for the full mechanism.

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
Backend not installed → `bash ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_setup.sh install`.

### "Index is out of sync after a markdown edit"
Incremental skipped an edited file (rare hash edge) → force a full rebuild: `rag_index.py --rebuild`.

### "Query returns no results"
Index empty (no L1/L2 yet), query too specific, or RAG below threshold. Check with the grep fallback and verify the index has rows (`rag_index.py` reports `table_rows`).

### "Index file is corrupted / LanceDB won't open"
Delete and rebuild: `rm -rf .company/memory/index && rag_index.py --rebuild`.

### "A query returns wrong-language / obviously irrelevant results" (pre-Phase-24 symptom)
This was the Phase 24 diagnosis: an English-only embedding model on a non-English query. Confirm the model in use (`rag_embed.RAG_EMBED_MODEL`) is the multilingual one and that `references/rag.md §1`'s floor applies; if the index predates the swap, the next daily tick self-heals it (§13), or force it now: `rag_index.py --rebuild`.

---

## 10. RAG Lifecycle

| Stage | Who | Action | Condition |
|---|---|---|---|
| **Monitor** | Tony (daily, deps-free) | `--threshold-check` in daily-run surfaces an activation candidate | Active L1+L2 ≥ 50 and venv not installed |
| **Decide** | Elon | Approve activation | Threshold crossed or Chairman orders |
| **Setup** | Tom / human (one-time) | `rag_setup.sh install` | Before first refresh |
| **Refresh** | Tony (daily, automatic) | Incremental `rag_index.py` after reinforce+decay; self-heals a full rebuild on a model-stamp mismatch (§13) | Keeps the index in sync with markdown |
| **Query** | manual CLI + live ask-time injection (`hook_memory_inject.py`, Stage B / v0.1.5) + dispatch-time shared read (`Employee.recall_shared`, Phase 18c, `shared_memory_read` employees) | `rag_query.py` (hybrid by default, §7) | Search by meaning + exact token |
| **Rebuild** | Tony (as needed) | `--rebuild` | After major cleanup / corruption / model swap |
| **Degrade** | All | Exit 2 + grep fallback; core never fails | Venv absent/broken/stale-stamp |

---

## 11. Integration with Company Workflow

- **Daily core** (`daily-run.sh`): the RAG-index refresh + threshold surface run after the reinforce/decay/verify/entropy core, owned by Tony, gated via `org/schedule.yaml`. Venv absent/broken → one logged skip line, core unaffected. A model-stamp mismatch (§13) is surfaced as its own log line and self-heals with a forced full rebuild — no manual step.
- **One-process batch refresh (Phase 28 Item 2c)**: `rag_index.py` accepts repeatable `--pair MEM_DIR INDEX_DIR`; `daily-run.sh` collapses the company store's refresh and every rag-mode employee's own refresh (see below) into ONE invocation — company pair first, then each employee pair — instead of one process per store. One fastembed model load per tick (zero if every store is unchanged) instead of up to ~8; a single pair's failure is isolated to that pair's own error, the rest still refresh. `--memory-dir`/`--index-dir` single-store mode is unchanged and still used by manual/ad-hoc invocations.
- **In-process dedup** (`reinforce_memory.py`, `entropy.py`): use `rag_embed` directly (Path A, §1) — separate from the LanceDB index. **Not yet cache-backed**: Phase 28 Item 2 scoped a shared content-hash → vector cache (read from the index, so an unchanged body skips re-embedding) plus an `l0_cache` table resurrecting `--include-l0` as a cache-not-retrieval seam — this was evaluated and NOT shipped (kill-switch: proving byte-identical warm-vs-cold output for all three consumers, with a new table + single-writer invariant, wasn't achievable to the required rigor in that pass). `--include-l0` (§4) remains unused by any caller; revisit with a dedicated equality-gate pass before attempting it again.
- **Policy tunables** (`org/policy.md §8`): `RAG_ENABLE_THRESHOLD`, `RAG_MODEL`, `RAG_INDEX_PATH`, `SELF_COMPANY_INJECT_RAG_MIN_SCORE`.
- **Stage B (shipped v0.1.5)**: semantic ask-time injection in `hook_memory_inject.py` consumes `rag_query.py` with a tight timeout and falls back to the keyword path when the venv is absent.
- **Shared read at dispatch (Phase 18c)**: a `shared_memory_read` employee (elon by default) also consumes `rag_query.py` against the SHARED index at DISPATCH — via `Employee.recall_shared` / `dispatch_context` — so a headless worker carries the Chairman's standing directives, not just the interactive hook. Same gate + live re-validation; the dispatcher sets `SC_NO_MEMORY_INJECT=1` on the worker so the worker's own `UserPromptSubmit` hook no-ops (no double injection).
- **Per-employee memory (Phase 18/24 Item 3)**: the 5 rag-mode employees (tony/mike/elon/phoebe/july) capture experience via `Employee.remember()` (a concrete, runnable persona step as of Phase 24 — see each `persona.md`'s "Capture (task close)" section), indexed by the SAME `rag_index.py`/`Employee.recall_context` path — physically isolated per employee, same model + stamp + hybrid machinery as the shared index. Refreshed in the SAME batched process as the company index (Phase 28 Item 2c, above), not one process per employee.

---

## 12. References

- **Scripts**: `${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/rag_setup.sh`, `rag_index.py`, `rag_query.py`, `rag_embed.py`, `rag_stamp.py`, `rag_rerank.py`.
- **Index location**: `.company/memory/index/` (LanceDB, gitignored).
- **Policy tunables**: `org/policy.md §8 RAG`.
- **Design**: `design/self-company-design.md §8`.
- **Memory tiers & frontmatter**: `design/self-company-design.md §2` / §4.

---

## 13. Model Swaps, Stamping & Rollback (Phase 24 Item 1)

### The diagnosis

`BAAI/bge-small-en-v1.5` is English-only. Measured live on the real corpus (154 memories at diagnosis time): it compressed EVERY query — on- or off-topic, any language — into a narrow 0.45–0.65 cosine band, so the injection floor (`RAG_MIN_SCORE=0.30`) filtered nothing (17/17 diagnostic queries had all top-10 hits ≥ 0.30). Chinese queries were WORSE than random (hit@1 0.20 vs EN 0.62) — and the Chairman communicates in Traditional Chinese by default, so this was the default path, not a corner case. An off-topic Chinese prompt ("how to cook pasta") injected the SAME memory an on-topic Chinese query would, silently violating the hook's own "never pollute with irrelevant memory" rule on every Chinese prompt.

### The fix and the measurement

Swapped to `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` — still 384-dim (`EMBEDDING_DIM` did NOT change; the heavier `bge-m3`, 1024-dim SOTA multilingual, was deliberately NOT chosen — it would force a schema/assertion migration for quality gains not yet measured as needed). Re-measured on the real corpus (55 active L1/L2 memories at the time of this phase, EN+ZH+off-topic diagnostic queries, before vs after the model swap AND Item 4's hybrid+RRF):

| Metric | Before (bge-small-en, vector-only) | After (multilingual + hybrid/RRF) |
|---|---|---|
| EN hit@1 / MRR | 0.750 / 0.830 | 0.875 / 0.906 |
| ZH hit@1 / MRR | 0.375 / 0.396 | 0.625 / 0.750 |
| Off-topic top-1 score (EN+ZH, 11 probes) | 0.489–0.648 | 0.091–0.306 |

A SECOND bug surfaced during this measurement (not the embedding model): `hook_memory_inject.rank()`'s keyword-path recency fallback fired whenever the prompt tokenized to nothing — which is true for BOTH a genuinely empty prompt AND any non-empty pure-CJK prompt (the fast tokenizer is ASCII-only). Fixed to fall back to recency ONLY on a truly blank prompt; a real non-Latin-script prompt with no semantic match now correctly injects nothing (`tests/test_hook_memory_inject.py::TestKeywordFallbackDistinguishesEmptyFromUnparseable` / `TestMultilingualRelevanceGate`).

### Precision hardening + the floor (Phase 24 R3, post red-team)

Two precision leaks were fixed after Gibby's red-team round 2, both about off-topic prompts injecting an *unrelated* memory:

**Keyword path (no-venv / RAG-hiccup degrade).** `semantic_top()` returns `None` (→ keyword path) during ANY RAG unavailability — no venv, empty/mid-rebuild/stale-stamp index, timeout — so the keyword path is not just the no-install case; it must be robust on its own. Its gate was `MIN_OVERLAP>=1` (a single shared 3+-char word), which let off-topic English inject on one incidental word ("change" a flat tire → a git memory). A LONE overlapping token now must pass THREE principled, corpus-derived gates (a shared **pair** of tokens always clears — real signal):
1. **Corpus rarity (IDF):** `df(token) <= max(2, N · 0.25)` — a token common across the candidate memories is not discriminative, so a lone incidental match on it is noise (removes dominant-word collisions like "chairman"/"company" that scale up).
2. **Body substance:** the token must appear in the memory's BODY, not merely its auto-generated id/slug (kills "rules" → git-identity-**rules**).
3. **Length floor** (a NECESSARY, never sufficient, condition — this is NOT the earlier wrong "len≥5 ⇒ specific" heuristic): drops the short common-English words (red/long/stay/day) a 30-memory corpus is too small to see as common via IDF.
   Plus the closed-class **function-word stoplist** (prepositions/conjunctions incl. before/between). Result: the IDF gate catches **common-word** collisions — the whole class that dominated the leaks (chairman/company/before/language/design/…), and it verifiably clears Gibby's 25 diagnostic off-topic prompts on the keyword path while on-topic still injects.

   **Irreducible limit (be honest — this is inherent, not a bug):** a *rare* (`df==1`) general-English content word that collides with exactly one memory (an off-topic prompt sharing e.g. "database"/"schedule"/"memory" with one memory) is **indistinguishable** from a real topical match by any corpus-derived lexical rule — on a small corpus its document-frequency is identical to a genuine content word's. So the earlier "all 25 inject nothing on the keyword path" claim holds for the diagnostic set and for common-word collisions, but NOT as a general guarantee: a rare-content-word collision the diagnostic set didn't happen to contain can still slip through. Pure lexical matching cannot close this without semantics. That is why the keyword path is a **graceful floor**, not the precision layer — the semantic path (reranker) is where precision actually lives.

**Semantic floor.** Retuned `SELF_COMPANY_INJECT_RAG_MIN_SCORE` 0.35 → **0.40** — the HIGHEST floor that still keeps EVERY on-topic diagnostic hit (lowest true-positive top-1 = `merge-gate` 0.419). Measured: EN hit@1 0.875, ZH hit@1 0.625 preserved; 24/25 off-topic English inject nothing on the semantic path.

### The reranker — Item 5 (built; a real precision layer, not a perfect gate)

The cosine floor alone could not reject the innocent off-topic prompt "How should I schedule my morning gym workout?" — it scores **0.417** against a scheduler memory (on the word "schedule"), **inseparable** from `merge-gate`'s on-topic **0.419** by any cosine threshold. The Chairman ungated **Item 5** (the gated cross-encoder reranker) — it reads the (query, document) PAIR jointly and scores the gym/scheduler pair **−2.998**, far below any real on-topic hit, so it closes THAT case cleanly. It is a large precision win, but — as measured below — **not a perfect separator**; see "Known limits" at the end of this section.

**Model:** `jinaai/jina-reranker-v2-base-multilingual` via fastembed `TextCrossEncoder` (ONNX/CPU/offline; ships with the already-pinned `fastembed==0.8.0` — NO new dependency). Multilingual is required: the English-only ms-marco cross-encoder scores Chinese on-topic pairs negative and would suppress ZH recall. The shared seam is `rag_rerank.py` (lazy import, like `rag_embed.py`).

**Mechanism:** `rag_query.py --rerank` OVER-retrieves the top ~20 by hybrid/vector, cross-encodes each candidate body (the `text` column from Item 4) against the query, sorts by reranker score, and adds a `rerank_score` per hit. The consumers (`hook_memory_inject.semantic_top`, `Employee.recall`/`recall_shared`) then inject a hit iff it clears **BOTH** the cosine floor (a cheap pre-filter, `RAG_MIN_SCORE` 0.40) **AND** the reranker cutoff — the reranker is the FINAL relevance gate.

**Why the cosine pre-filter stays:** the reranker alone can't separate every case (the least-off off-topic "board game" reranks to −2.36, above the hardest on-topic "AI models" at −2.51). But `board game`'s **cosine is 0.285 < 0.40** — the floor already rejects it. So the two gates **compose**: the cheap cosine floor removes the bulk of off-topic, and the reranker rejects clear off-topic that slips past cosine (like gym, cosine 0.417 / rerank −2.998). It is not a perfect separator — a boundary off-topic that both passes cosine AND reranks near the on-topic cluster can still inject (see "Known limits" below) — but each gate strictly reduces the false-positive rate the other leaves.

**Cutoff `SELF_COMPANY_INJECT_RERANK_MIN_SCORE = -2.75`** — the measured **best-separation** point, NOT a clean gap. On the diagnostic set the nearest off-topic (gym −2.998) and the lowest cosine-passing on-topic (AI-models −2.51) sit far apart, but that spacing is not representative: pushed harder, the two distributions **interleave near the boundary** — e.g. the off-topic "memory palace technique" reranks **−2.719** (its hit: the Chairman's persistent-memory memory) while a real on-topic hit sits at **−2.688**, a ~0.03 separation. So −2.75 is chosen to sit just below the cluster of real on-topic hits; it cannot be raised to catch every boundary off-topic without dropping real recall.

**Measured (real corpus, after Item 5):**
| Metric | before Phase 24 | cosine-only (R3) | + reranker (Item 5) |
|---|---|---|---|
| EN hit@1 | 0.750 | 0.875 | **0.875** |
| ZH hit@1 | 0.375 | 0.625 | **0.875** (reranker reorders ZH better) |
| off-topic injection (diagnostic set) | frequent (~46%) | rare (24/25 clean) | **rare** (clear off-topic like gym closed) |

**Latency:** the rerank of ~20 short pairs is ~0.15 s; the cost is the two ONNX model loads per subprocess (~5.4 s warm total). Within the 30 s hook budget; the recall/query timeout was raised 7 s → 15 s for margin, and `rag_setup.sh` warms both models so the first post-install call isn't a ~50 s cold load.

**Graceful degrade (non-negotiable):** reranker backend absent / model-load or inference error / timeout / **concurrent-RAG-load pressure** → `rag_query` omits `rerank_score` and returns cosine order; the consumer's cosine floor alone decides — **byte-identical to the pre-Item-5 state**. The reranker is a **precision layer, never load-bearing**: when it degrades, precision reverts to the cosine+IDF level (still the large multilingual-model win), never a crash and never a blocked prompt. Never a hard dependency, never raises.

### Known limits / residual (measured, accepted — read before "improving" the threshold)

The Phase 24 stack is a large, real win, but it has a **precision ceiling** on this small corpus, not a bug. No more cutoff-chasing: Gibby confirmed the off-topic/on-topic rerank distributions **interleave** near the boundary, so no single threshold cleanly separates "an off-topic prompt that shares a genuine concept word with one memory" from a real on-topic hit.

- **Semantic path (venv present — the deploy path).** Dramatic improvement over the English-only baseline: ZH hit@1 0.375→0.875, EN 0.750→0.875, off-topic injection frequent→rare. The reranker cleanly rejects clear off-topic (gym −2.998). **Residual:** an off-topic prompt sharing ONE real concept word with a single memory can still occasionally inject — e.g. "memory palace technique" → the Chairman's persistent-memory memory (cosine 0.491, rerank −2.719, just above the −2.75 cutoff), whose rerank score interleaves with genuine on-topic hits (−2.688). Accepted as best-effort; −2.75 is the measured best-separation point, not a clean gap, and raising it would drop real recall.
- **Keyword degrade path (no-venv / RAG hiccup).** The IDF gate catches common-word collisions; a rare (`df==1`) incidental content word colliding with one memory is irreducible for pure lexical matching (inherent — its corpus document-frequency is identical to a real content word's). This path is a graceful floor, not the precision layer.
- **Reranker is precision-only, never load-bearing.** Under concurrent RAG load / resource pressure it can degrade to cosine-only (clean, documented, no crash); in that state precision reverts to the cosine+IDF level. Injection is always advisory context, never an instruction, so a residual false-positive is low-harm — the same best-effort posture as the memory-guard's documented limits.

As the corpus grows and diversifies, the semantic separation should widen; re-measure before touching the cutoff.

### The migration mechanism — model-stamping

Same dimension does NOT mean same vector space: `bge-small-en-v1.5` and the multilingual MiniLM are both 384-dim but geometrically unrelated. Scoring a NEW-model query vector against OLD-model row vectors would silently produce meaningless cosine numbers — no crash (dims match), just wrong answers. The SAME hazard exists across **fastembed library versions**: the same model name can change its output vectors between releases (the multilingual MiniLM switched from CLS to mean pooling), so the stamp includes the library version too (MUST-FIX 4). `rag_stamp.py` is the shared seam that prevents all of this:

1. **Stamp** — `rag_index.py` writes `{model, dim, lib}` (model name, embedding dimension, fastembed version) to `.rag_stamp.json` next to the LanceDB table after every successful build (full or incremental).
2. **Refuse** — `rag_query.py` checks the stamp before scoring; a mismatch on ANY of model / dim / lib, OR a missing stamp (a legacy pre-Phase-24 index that never wrote one), is treated identically to "index absent" — the SAME exit-2/`FileNotFoundError` signal every consumer already degrades on. No consumer (`hook_memory_inject`, `Employee.recall`/`recall_shared`) needed its own stamp-check code; they all inherit the refusal for free because they all shell out to `rag_query.py`.
3. **Self-heal** — `rag_index.py` ALSO checks the stamp on its own incremental refresh: a table with real rows but a mismatched/absent stamp forces a full rebuild (re-embeds everything under the CURRENT model + library), then writes the fresh stamp. This means `daily-run.sh`'s existing (unmodified) incremental invocation self-heals automatically — no bash changes were needed, no manual step, no window where a mixed-model table could exist (Phase 12b self-heal pattern: plugin update lands → next tick detects the mismatch → rebuilds → correct). fastembed is also PINNED in `rag_setup.sh` (`fastembed==<version>`) so an unintended upgrade can't happen silently; a deliberate bump changes the stamped `lib` and self-heals.

### Rollback

Rollback is real and cheap, in either direction:
- **Revert the model**: change `RAG_EMBED_MODEL` back in `rag_embed.py`. The next tick's stamp-mismatch self-heal rebuilds the index under the old model automatically.
- **Revert everything**: `rm -rf .company/memory/index && rag_index.py --rebuild` always restores a fully consistent index from the markdown truth (the index is a derivative cache, never a source of truth) — no data is ever at risk in either direction.
