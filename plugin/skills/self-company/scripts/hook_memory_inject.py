#!/usr/bin/env python3
"""
hook_memory_inject — UserPromptSubmit hook: ask-time Chairman-memory injection.

Phase 10 Item 4 (the big win): memory is finally READ BACK when the Chairman
asks something. On every UserPromptSubmit this hook ranks the Chairman's durable
memories against the prompt with a FAST pure-stdlib scorer and, if anything is
genuinely relevant, injects a compact block as `additionalContext` — added to
Claude's context SILENTLY (never shown to the Chairman, never blocking).

Contract (claude-code-guide, July 2026 docs)
--------------------------------------------
UserPromptSubmit STDIN JSON:
  {session_id, prompt_id, transcript_path, cwd,
   hook_event_name:"UserPromptSubmit", effort}
The prompt text is NOT on stdin — it is the last `type:"user"` turn in the
transcript JSONL at `transcript_path`. We read it there.

To inject silently: exit 0 with stdout
  {"hookSpecificOutput":
     {"hookEventName":"UserPromptSubmit","additionalContext":"<text>"}}
Exit 0 with NO stdout -> no injection. We NEVER exit 2 (that would block the
prompt). **TIMEOUT IS 30s HARD** -> pure stdlib only on this path: no fastembed /
embedding cold-start, no network. Completes well under 1s on a 150-memory corpus.

Hard rules (all enforced below):
  * Opt-in guard FIRST: no `.company` -> exit 0, no output (plugin hooks fire
    globally; this keeps them inert off-company).
  * Relevance-gated: nothing scores above the floor -> inject NOTHING. Never
    pollute the prompt with irrelevant memory.
  * Token-capped output (~600 chars).
  * Robust: ANY error -> exit 0 silently. Never break the Chairman's prompt.

Mike 2026-07-16 Finding 2: alongside the relevance-gated block above, an
always-on, SEPARATE, tiny "core identity" block (Letta/MemGPT's core-memory
concept) is also injected every turn — a handful of explicitly opted-in
(`core: true`) or well-reinforced L2 identity/preference facts, hard-capped by
count and chars, so a session whose opening prompt brushes no L2 fact still
starts knowing the basics. It is ADDITIVE and UNGATED (no relevance check
against the prompt) but fully separate from, and never able to weaken, the
relevance-gated path's "off-topic injects nothing" guarantee — see
select_core_facts()/build_core_context()/_core_config().

CLI (for tests, no real hook env needed):
  hook_memory_inject.py [--company DIR] [--transcript FILE]
Reads the documented stdin JSON when present; explicit flags override it.

Pure stdlib.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _env_num(name, default, cast):
    """Parse an env-tunable number, falling back to `default` on absence OR garbage.

    P13B-1: these knobs are parsed at MODULE LEVEL — outside main()'s backstop
    try/except — so a malformed value (e.g. SELF_COMPANY_INJECT_RAG_TIMEOUT=abc)
    would raise at IMPORT and break the always-on hook on every prompt. This hook's
    hard rule is "ANY error -> exit 0 silently, never break the Chairman's prompt",
    so a bad tuning value must degrade to the default, never crash."""
    try:
        return cast(os.environ[name])
    except (KeyError, ValueError, TypeError):
        return default

# The SINGLE tombstone vocabulary + the frontmatter PARSING SEAM are shared
# sibling modules in THIS directory; put it on sys.path FIRST so the hard imports
# below resolve under every entry point (the UserPromptSubmit hook, direct run,
# the test harness). They always ship together, so the imports never fail.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tombstone import is_tombstoned

# Phase 11 Item 2: the fragile frontmatter delimiter + key:value split is the ONE
# shared module (frontmatter.py).
from frontmatter import parse as _fm_parse

# Phase 22: the `.rag-venv/bin/python` interpreter path is resolved by the ONE
# shared helper (rag_venv.py), never open-coded here.
from rag_venv import venv_python

# Mike 2026-07-16 Finding 2 / policy_config: the core-identity block's
# ENABLE/MAX_COUNT/CHAR_CAP are tunable via org/policy.md (§8.2), same
# resolver decay.py uses. Best-effort import: absent module -> the built-in
# defaults below are used directly (matches every other best-effort import
# in this file — never crash the always-on hook over a missing sibling).
try:
    from policy_config import resolve as _resolve_policy_config
except Exception:  # pragma: no cover - defensive
    _resolve_policy_config = None

EVENT = "UserPromptSubmit"

# Scoring knobs (env-overridable for tuning/tests; sane stdlib defaults).
TOP_K = _env_num("SELF_COMPANY_INJECT_TOPK", 4, int)
TOP_K_CAP = 5                     # hard ceiling regardless of env
# Phase 29 Item 5 (Bob C1): 600 -> 1800 / 180 -> 540 (~3x, post-sonnet-5-tokenizer
# — Item 2's ~+30% tokens/text, re-baselined jointly per the spec). Against a
# 200k-token window this is trivially cheap (~0.15%); the old caps bought
# nothing but mid-thought truncation (a 180-char snippet is ~1.5 sentences,
# minus the header). Phase 24's reranker + relevance gates carry precision now
# — off-topic still injects NOTHING (that was never the caps' job); on-topic
# injections now carry whole memories instead of a "…"-truncated fragment.
# Do NOT also raise TOP_K here — more memories is a different decision than
# whole memories (Elon's note); TOP_K/TOP_K_CAP stay as they are.
CONTEXT_CHAR_CAP = _env_num("SELF_COMPANY_INJECT_CONTEXT_CHAR_CAP", 1800, int)
PER_MEM_CHARS = _env_num("SELF_COMPANY_INJECT_PER_MEM_CHARS", 540, int)
MIN_OVERLAP = 1                  # relevance floor: >=1 shared keyword or silent
HIGH_RC = _env_num("SELF_COMPANY_INJECT_HIGH_RC", 2, int)  # L1 gate
TIER_WEIGHT = {"L2": 1.0, "L1": 0.6}

# Phase 24 R3 MUST-FIX 1: a single incidental overlap on a generic word is how
# off-topic ENGLISH prompts sneak past the keyword floor (Gibby: "how do I CHANGE
# a flat tire" -> git memory on "change"; "rules of CRICKET" -> git-identity-RULES
# on the slug word "rules"; "difference BETWEEN a latte…" -> a memory on the
# preposition "between"). The prior length heuristic (>=5 chars => "specific") was
# wrong: `before`/`language`/`database`/`design`/`rules`/`between` are all >=5
# chars yet generic. R3 replaces it with THREE principled, corpus-derived gates on
# a LONE overlapping token (multi-token overlaps always clear — a shared PAIR of
# meaningful words is real signal):
#   1. CORPUS RARITY (IDF): the token must not be common across the candidate
#      memories — `df(token) <= max(LONE_DF_FLOOR, N * LONE_MAX_DF_RATIO)`. The
#      max(...) floor keeps the gate correct on tiny corpora (a 2-memory test set
#      where every df is "high" by ratio must not gate a real content word). This
#      derives from the live corpus, not a hand-kept list; it removes the
#      dominant-word collisions (e.g. "chairman", "company") that scale up.
#   2. BODY SUBSTANCE: the token must appear in the memory's BODY, not merely its
#      auto-generated id/slug — kills slug collisions ("rules" -> git-identity-RULES).
#   3. FUNCTION WORDS: the base stoplist below carries the closed-class function
#      words (prepositions/conjunctions incl. before/between) so a lone preposition
#      never counts as topical.
# The KEYWORD path is only the no-venv/RAG-hiccup degrade; the primary defense is
# the semantic layer's INJECT_NOTHING verdict. A residual class remains
# irreducible for pure lexical matching: a df==1 GENERAL-English content word that
# collides (e.g. an off-topic prompt sharing "database"/"project" with one memory)
# cannot be told from a real topical match without semantics — that is the
# reranker's job (Item 5), documented in references/rag.md.
LONE_MAX_DF_RATIO = _env_num("SELF_COMPANY_INJECT_LONE_DF_RATIO", 0.25, float)
LONE_DF_FLOOR = 2       # a lone token in <= this many memories is always "rare enough"
# A 4th NECESSARY (never sufficient) condition on a lone match: minimum token
# length. This is NOT the rejected "len>=5 => specific" heuristic (that used
# length as SUFFICIENT, wrongly admitting long generic words like
# "language"/"database"); here length is one of FOUR conjunctive gates. Its only
# job is to drop the short common-English words that a 30-memory corpus is too
# small to see as common by IDF (df==1 words like red/long/stay/day) — a real
# specific term is rarely < 5 chars. Long generic words are still caught by IDF
# (at scale) + the function-word stoplist; short content tokens (e.g. a lone
# "fly") are the one class this suppresses on the no-venv path, and the semantic
# path recovers them whenever the venv is present.
LONE_MIN_LEN = 5

# Phase 24 MUST-FIX 1(a): a DISTINCT signal for "the semantic layer ran and
# found nothing at or above the relevance floor" — a DEFINITIVE relevance
# verdict that means inject NOTHING, NOT the same as None ("RAG unavailable /
# degraded -> fall back to the keyword path"). Before this fix, semantic_top()
# collapsed both cases to None, so an off-topic prompt whose (working) semantic
# search correctly returned only sub-floor hits fell THROUGH to the weaker
# keyword gate and got injected on a single incidental word. A unique sentinel
# (not an empty list) makes run() unable to confuse the two.
INJECT_NOTHING = object()

# --- Phase 13 Stage B (B.1): RAG semantic-retrieval knobs ---------------------
# The semantic path is ADDITIVE: it augments retrieval ONLY when the local RAG
# stack is present; the keyword path (rank()) stays the guaranteed-fast floor and
# the no-venv/degrade path. All bounded so the 30s hook budget is never approached.
# Phase 24 Item 5: bumped 7s -> 15s. The rerank subprocess loads TWO ONNX models
# (embed ~1-2s + cross-encoder ~2s) plus the query; measured ~5.4s warm, so 7s left
# too little margin for slower hardware / disk. 15s is half the 30s hook budget and
# a timeout still degrades cleanly to the keyword path. rag_setup.sh warms both
# models so the first post-install call isn't a ~50s cold load. (Non-rerank callers
# and the no-venv path never spawn the subprocess, so they are unaffected.)
RAG_QUERY_TIMEOUT = _env_num("SELF_COMPANY_INJECT_RAG_TIMEOUT", 15.0, float)  # s
# Ask for more hits than we inject: some will be filtered out as stale/tombstoned/
# out-of-scope when re-validated against the live candidate set, so over-fetch to
# still have enough survivors to fill the cap.
RAG_QUERY_TOPK = max(TOP_K_CAP, TOP_K) * 2
# Semantic relevance floor (cosine): honor the hook's "relevance-gated — never
# pollute the prompt" hard rule. Below this, a hit is treated as off-topic noise
# and dropped; if nothing clears the floor we fall back to the keyword path (which
# has its own MIN_OVERLAP gate) so an off-topic prompt still injects nothing.
#
# Phase 24 Item 1 / R3: retuned to 0.40, DATA-DRIVEN. The old English-only
# bge-small model made 0.30 filter nothing (every query scored 0.45-0.65); the
# multilingual swap opened a real gap. R3 sweep on the real corpus (25 off-topic
# EN + 16 on-topic EN+ZH): 0.40 is the HIGHEST floor that still keeps EVERY
# on-topic diagnostic hit — the lowest true-positive top-1 is `merge-gate` at
# 0.419, so the floor sits just below it (0.019 margin) and every real hit clears.
# It cannot go higher: the reranker escalation is documented in references/rag.md.
#
# Item 5 (below) closes the one residual the cosine floor cannot: an innocent
# off-topic prompt ("schedule my morning gym workout", cosine 0.417) that lands in
# the same band as a real on-topic hit (`merge-gate` 0.419). The floor here is now
# a cheap PRE-FILTER; the cross-encoder reranker is the final gate.
RAG_MIN_SCORE = _env_num("SELF_COMPANY_INJECT_RAG_MIN_SCORE", 0.40, float)

# --- Phase 24 Item 5: cross-encoder reranker gate --------------------------------
# When the local multilingual reranker is available, rag_query.py cross-encodes the
# over-retrieved candidates and returns a `rerank_score` per hit (a joint
# query-document relevance logit the bi-encoder cosine can't see). A hit then
# injects iff it clears BOTH the cosine PRE-FILTER (RAG_MIN_SCORE) AND this reranker
# cutoff — which is what rejects CLEAR off-topic like the "gym workout" case (its
# scheduler hit passes cosine 0.417 but reranks to ~-3.0). DATA-DRIVEN cutoff -2.75
# is the measured best-separation point (just below the on-topic cluster), NOT a
# clean gap: the off-topic/on-topic rerank scores INTERLEAVE near the boundary (an
# off-topic sharing one real concept word can rerank within ~0.03 of a genuine
# on-topic hit), so this is a best-effort precision cutoff, not a perfect separator
# — see references/rag.md "Known limits". When rag_query returns NO `rerank_score`
# (reranker backend absent / model-load or inference error / timeout / concurrent-
# load pressure), this gate is skipped and the cosine floor alone decides —
# byte-identical to the pre-Item-5 behavior (precision-only, never load-bearing).
RERANK_MIN_SCORE = _env_num("SELF_COMPANY_INJECT_RERANK_MIN_SCORE", -2.75, float)

# --- Mike 2026-07-16 Finding 2: always-on identity CORE block -----------------
# Letta/MemGPT keeps a tiny "core memory" block (persona + user facts)
# PERMANENTLY in context, distinct from its retrieval-gated recall/archival
# tiers. Everything above this line (rank/semantic_top/RAG_MIN_SCORE/
# RERANK_MIN_SCORE) is the Phase 24 relevance-gated path and is HARD-OFF-LIMITS
# to this feature — untouched, byte-identical, "off-topic injects nothing"
# still holds through it. The core block is a SEPARATE, ADDITIVE section built
# and capped independently, then concatenated onto (never merged into) the
# relevance-gated output in run().
#
# Selection is an EXPLICIT opt-in signal, never a relevance guess:
#   1. any L2 memory with a truthy `core:` frontmatter flag (author opted it in
#      by hand) — the primary, EXPECTED signal for a real install (Tony/Phoebe
#      tag the handful of true Chairman-identity facts once);
#   2. else (nothing anywhere is flagged yet), L2 memories at/above
#      CORE_FALLBACK_MIN_RC reinforce_count — a rare safety net, deliberately
#      set far ABOVE the routine L1->L2 promotion bar (policy.md L1_TO_L2_RC,
#      default 4) so an ordinary/incidental L2 memory near the promotion floor
#      never leaks into the always-on block; only a fact reinforced far beyond
#      routine qualifies without being asked to.
# "No core facts qualify" (neither signal present) is a normal, common state —
# degrade to injecting nothing extra, never fabricate a block from thin air.
DEFAULT_CORE_ENABLE = True
DEFAULT_CORE_MAX_COUNT = 5              # hard cap by COUNT
DEFAULT_CORE_CHAR_CAP = 500             # hard cap by CHARS (Mike's proposal: ~500)
CORE_FALLBACK_MIN_RC = 10               # deliberately conservative rare-safety-net bar
CORE_PER_MEM_CHARS = 200                # each core fact is meant to be a short one-liner
CORE_HEADER = "Chairman identity (core, always on):"


def _truthy(raw):
    """Loose boolean parse for a frontmatter flag (e.g. `core: true`). Unknown/
    absent/malformed -> False; this NEVER guesses a memory into the core block."""
    return str(raw or "").strip().lower() in ("true", "yes", "1", "on")


def _core_config(company):
    """Resolve (enable, max_count, char_cap) for the core block.

    Layering (highest precedence first): env var override (matches every other
    tunable in this file, and is what the tests flip) > org/policy.md §8.2 (via
    the shared policy_config resolver, matching decay.py's convention) >
    built-in default. Never raises: a missing/malformed policy.md or a garbage
    env var both degrade silently to the next layer down."""
    defaults = {
        "CORE_MEMORY_ENABLE": 1 if DEFAULT_CORE_ENABLE else 0,
        "CORE_MEMORY_MAX_COUNT": DEFAULT_CORE_MAX_COUNT,
        "CORE_MEMORY_CHAR_CAP": DEFAULT_CORE_CHAR_CAP,
    }
    values = dict(defaults)
    if _resolve_policy_config is not None:
        try:
            policy_path = Path(company) / "org" / "policy.md"
            resolved, _sources = _resolve_policy_config(defaults, policy_path)
            values.update(resolved)
        except Exception:
            pass
    enable = bool(_env_num("SELF_COMPANY_INJECT_CORE_ENABLE",
                           values["CORE_MEMORY_ENABLE"], int))
    max_count = max(0, _env_num("SELF_COMPANY_INJECT_CORE_MAX_COUNT",
                                values["CORE_MEMORY_MAX_COUNT"], int))
    char_cap = max(0, _env_num("SELF_COMPANY_INJECT_CORE_CHAR_CAP",
                               values["CORE_MEMORY_CHAR_CAP"], int))
    return enable, max_count, char_cap


def select_core_facts(candidates, max_count):
    """Pick up to `max_count` core-block memories from the L2 slice of
    `candidates` via the explicit opt-in signal documented above. Deterministic
    order (reinforce_count desc, then id asc) so output is stable across runs.
    Returns [] when nothing qualifies (empty-core degrades cleanly)."""
    if max_count <= 0:
        return []
    l2 = [c for c in candidates if c[0] == "L2"]
    flagged = [c for c in l2 if _truthy(c[1].get("core"))]
    pool = flagged if flagged else [
        c for c in l2 if _int(c[1].get("reinforce_count"), 1) >= CORE_FALLBACK_MIN_RC]
    if not pool:
        return []
    pool = sorted(pool, key=lambda c: (-_int(c[1].get("reinforce_count"), 1),
                                       str(c[1].get("id", ""))))
    return pool[:max_count]


def build_core_context(core_facts, char_cap):
    """The core block's own compact, token-capped rendering — same
    collapse-whitespace + per-line-cap shape as build_context(), a DISTINCT
    header, and its OWN char budget so it can never eat into (or be eaten by)
    the relevance-gated block's cap. "" if nothing fits (including no facts)."""
    if not core_facts or char_cap <= 0:
        return ""
    lines, used = [CORE_HEADER], len(CORE_HEADER)
    for _tier, _fm, body, _path in core_facts:
        snippet = " ".join(body.split())
        if len(snippet) > CORE_PER_MEM_CHARS:
            snippet = snippet[:CORE_PER_MEM_CHARS - 1].rstrip() + "…"
        line = "- " + snippet
        if used + 1 + len(line) > char_cap:
            break
        lines.append(line)
        used += 1 + len(line)
    if len(lines) == 1:                            # header only -> nothing fit
        return ""
    return "\n".join(lines)

# Stopword set: the closed-class FUNCTION WORDS (articles, pronouns, prepositions,
# conjunctions, auxiliaries, degree adverbs) plus a few ubiquitous light verbs.
# This is a linguistic class, not per-leak whack-a-mole: a lone overlap on a
# function word (a preposition like "before"/"between", a determiner, an
# auxiliary) is grammatical glue, never a topic signal. Content words (neovim,
# terraform, chinese, database, project, …) are DELIBERATELY absent — telling an
# off-topic content-word collision from a real one is the corpus-rarity gate's job
# (common domain words) and ultimately the semantic layer's / reranker's job (df==1
# general-English content words); a stoplist must never try to enumerate them.
_STOP = frozenset("""
a an the this that these those and or but nor if then else for of to in on at by
with from into over under above below is are was were be been being am do does did
doing have has had having i you he she it we they me him her us them my your his
its our their what which who whom whose how when where why all any some no not can
could would should will shall may might must about as so than too very just also
again more most such only own same both each few other others another new use
using used get gets got make makes made
change changed changing without within going need needs needed want wants wanted
like likes work works working help helps helping set sets setting thing things
way ways know knows think thinks look looks looking find finds tell tells give
gives take takes keep keeps come comes put puts run running still back even well
around along across able really actually maybe perhaps please thanks thank okay
sure something anything everything someone anyone everyone here there where
before after between among amongst amid amidst against toward towards upon onto
beyond beside besides beneath during through throughout until till per via versus
""".split())

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text):
    """Distinct meaningful lowercase tokens (len>=3, non-stopword)."""
    if not text:
        return set()
    return {w for w in _WORD_RE.findall(text.lower())
            if len(w) >= 3 and w not in _STOP}


# --- opt-in guard -------------------------------------------------------------
def resolve_company(explicit):
    """Return the .company dir to use, or None (=> no-op). Priority:
    --company override, else $CLAUDE_PROJECT_DIR/.company, else ./.company."""
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    root = os.environ.get("CLAUDE_PROJECT_DIR")
    candidates = []
    if root:
        candidates.append(Path(root) / ".company")
    candidates.append(Path(".company"))
    for c in candidates:
        if c.exists():
            return c
    return None


# --- prompt extraction (transcript JSONL, last user turn) ---------------------
def latest_prompt(transcript_path):
    """The last `type:"user"` plain-string turn from the transcript, or "".
    Tolerates a missing/short/malformed transcript (returns ""). Never raises."""
    if not transcript_path:
        return ""
    try:
        text = Path(transcript_path).read_text(encoding="utf-8")
    except (OSError, IOError, UnicodeError):
        return ""
    latest = ""
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict) or d.get("type") != "user":
            continue
        msg = d.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            t = content.strip()
            if t and not t.startswith("<"):   # skip command/system echoes
                latest = t
    return latest


# --- minimal stdlib frontmatter parse (no yaml) -------------------------------
def _parse(text):
    """Return (frontmatter_dict, body_str) via the shared parser (Phase 11).
    Malformed / no `---` block -> ({}, ""), matching the old inline sentinel: the
    shared parser returns the text unchanged when it finds no fenced block, which
    we map back to ({}, ""). Body is `.strip()`ped exactly as before so a
    whitespace-only body still reads as empty."""
    fm, body = _fm_parse(text)
    if not fm and body == text:
        return {}, ""
    return fm, body.strip()


def _int(v, default=1):
    try:
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return default


def load_candidates(company):
    """Yield (fm, body, path) for scoreable memories: all of L2-cold/**, plus
    high-rc L1-warm/**. Tombstones excluded. Best-effort per file."""
    mem = Path(company) / "memory"
    roots = [("L2", mem / "L2-cold"), ("L1", mem / "L1-warm")]
    out = []
    for tier, root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*.md"):
            try:
                fm, body = _parse(f.read_text(encoding="utf-8"))
            except (OSError, IOError, UnicodeError):
                continue
            if not body or is_tombstoned(fm):
                continue
            rc = _int(fm.get("reinforce_count"), 1)
            if tier == "L1" and rc < HIGH_RC:
                continue                       # only high-rc L1 is in scope
            fm.setdefault("tier", tier)
            out.append((tier, fm, body, str(f)))
    return out


def _recency_key(fm):
    """Sortable recency signal from a memory's dates (newest first). Falls back
    to empty string so undated memories sort last."""
    return str(fm.get("last_reinforced") or fm.get("created") or "")


def _corpus_stats(candidates):
    """Phase 24 R3: (document_frequency, body_tokens_by_path) over the candidate
    corpus — the two corpus-derived signals the lone-token gate needs.

    `df[token]` = number of candidate memories whose SEARCHABLE text (id +
    category + body) contains the token — used for the IDF/rarity gate. Counting
    over the same haystack the overlap uses keeps the gate consistent with the
    match. `body_tokens_by_path[path]` = the token set of the BODY alone — used
    for the body-substance gate (a lone match on a token present only in a
    memory's auto-generated slug is not real signal). Pure stdlib, O(corpus)."""
    df = {}
    body_tokens_by_path = {}
    for _tier, fm, body, path in candidates:
        body_toks = _tokens(body)
        body_tokens_by_path[path] = body_toks
        hay = _tokens(" ".join((fm.get("id", ""), fm.get("category", ""), body)))
        for tok in hay:
            df[tok] = df.get(tok, 0) + 1
    return df, body_tokens_by_path


def rank(prompt, candidates):
    """Return the relevance-gated top-k memories for `prompt`.

    Scorer (fast, pure stdlib):
        score = keyword_overlap(prompt, id+category+body)
                * tier_weight[tier] * reinforce_count
    where keyword_overlap is the count of DISTINCT meaningful prompt tokens that
    appear in the memory's searchable text. A memory with zero overlap is below
    the relevance floor and dropped -> nothing irrelevant is ever injected.

    No prompt AT ALL (missing/blank transcript) -> fall back to recency ranking
    so a fresh turn still gets the freshest durable facts.

    Phase 24 Item 2 fix: a prompt that HAS content but tokenizes to nothing
    (the fast scorer's `_tokens` regex is `[a-z0-9]+` — ASCII-only) is NOT the
    same as no prompt. Before this fix the two were conflated: any pure-CJK (or
    other non-Latin-script) prompt silently took the recency-fallback branch
    and got the freshest memories injected regardless of topic — precisely the
    "off-topic prompt pollutes context" failure this hook exists to prevent,
    and the Chairman's DEFAULT language is Traditional Chinese, so this was not
    a corner case. The keyword path genuinely cannot assess relevance for such
    a prompt (it has no non-Latin vocabulary), so it must degrade to NOTHING,
    not to a false "recency == relevant" positive. Non-Latin-script relevance
    is the semantic/RAG path's job (tried first in run(), before this
    fallback); this is only the safety net when RAG is absent/degraded.

    Freshness tie-break (decay_score, 0..1): when candidates have identical
    keyword scores, the memory with the higher decay_score (fresher per the
    decay model) ranks first. Missing decay_score degrades to the recency key.
    """
    if not prompt or not prompt.strip():
        # Recency fallback: newest durable memories, weighted by tier.
        ranked = sorted(
            candidates,
            key=lambda c: (_recency_key(c[1]), TIER_WEIGHT.get(c[0], 0.5)),
            reverse=True)
        return ranked[:min(TOP_K, TOP_K_CAP)]

    p_tokens = _tokens(prompt)
    if not p_tokens:
        # Real prompt content, but the ASCII-only tokenizer found nothing to
        # score against -> relevance cannot be established -> inject nothing.
        return []

    # Phase 24 R3 MUST-FIX 1: precompute per-token corpus DOCUMENT FREQUENCY (how
    # many candidate memories contain the token) and each memory's BODY tokens, to
    # power the lone-token gate below. O(corpus) once, not per-candidate.
    df, body_tokens_by_path = _corpus_stats(candidates)
    n = max(1, len(candidates))
    lone_df_cap = max(LONE_DF_FLOOR, n * LONE_MAX_DF_RATIO)

    scored = []
    for tier, fm, body, path in candidates:
        hay = _tokens(" ".join((fm.get("id", ""), fm.get("category", ""), body)))
        overlap_tokens = p_tokens & hay
        overlap = len(overlap_tokens)
        if overlap < MIN_OVERLAP:            # relevance gate
            continue
        # Phase 24 R3 MUST-FIX 1: a LONE overlapping token must survive three
        # principled gates before it counts as relevance (a shared PAIR of tokens
        # always clears — real signal). See the LONE_MAX_DF_RATIO comment block.
        if overlap == 1:
            lone = next(iter(overlap_tokens))
            # 1. corpus rarity (IDF): a token common across memories is not
            #    discriminative — one incidental match on it is noise.
            if df.get(lone, 0) > lone_df_cap:
                continue
            # 2. body substance: the token must be in the BODY, not merely the
            #    memory's auto-generated id/slug (kills "rules" -> git-identity-RULES).
            if lone not in body_tokens_by_path.get(path, frozenset()):
                continue
            # 3. length floor (NECESSARY, not sufficient): drop short common-English
            #    words a small corpus can't see as common via IDF (red/long/stay/day).
            if len(lone) < LONE_MIN_LEN:
                continue
        weight = TIER_WEIGHT.get(tier, 0.5)
        rc = _int(fm.get("reinforce_count"), 1)
        score = overlap * weight * max(rc, 1)
        decay = fm.get("decay_score")
        try:
            decay_score = float(decay) if decay is not None else 0.0
        except (TypeError, ValueError):
            decay_score = 0.0
        scored.append((score, overlap, _recency_key(fm), decay_score, tier, fm, body, path))
    # Highest score first; ties broken by overlap then freshness (decay_score)
    # then recency (deterministic).
    scored.sort(key=lambda s: (s[0], s[1], s[3], s[2]), reverse=True)
    return [(t, fm, body, path)
            for (_s, _o, _r, _d, t, fm, body, path) in scored[:min(TOP_K, TOP_K_CAP)]]


def _debug(reason):
    """Optional one-line degrade reason on stderr (never stdout — the hook's stdout
    is reserved for the injection JSON). Silent unless SELF_COMPANY_INJECT_DEBUG is
    set, so normal hook logs stay clean. Honors the two-reason convention: the
    force-off case (SC_NO_RAG) names the env var; a genuine absence says so."""
    if os.environ.get("SELF_COMPANY_INJECT_DEBUG"):
        try:
            print(f"[hook_memory_inject] semantic fallback: {reason}", file=sys.stderr)
        except Exception:
            pass


def semantic_top(company, prompt, candidates):
    """RAG-augmented candidate selection (Phase 13 Stage B.1).

    When the local RAG stack (the project's `.company/.rag-venv` + a non-empty
    LanceDB index) is available, ask `rag_query.py` for the memories SEMANTICALLY
    closest to `prompt`, then map those hits back to the LIVE candidate files.

    Three-way return (Phase 24 MUST-FIX 1(a) — the distinction is load-bearing):
      * a non-empty **list** [(tier, fm, body, path), …] — inject these.
      * **INJECT_NOTHING** — the semantic layer RAN and definitively found
        nothing at/above the relevance floor (an off-topic prompt). This is a
        real "nothing relevant" verdict; the caller must inject NOTHING and must
        NOT fall through to the weaker keyword gate. This is what stops an
        off-topic ENGLISH prompt from being injected on one incidental word.
      * **None** — RAG is genuinely UNAVAILABLE / could not answer (SC_NO_RAG,
        no venv, absent/empty index, empty prompt, subprocess timeout, nonzero/
        garbage output, OR above-floor hits that were all stale/out-of-scope so
        the index couldn't be trusted). Only THIS case falls back to keyword.

    NEVER raises — any error degrades to None (keyword floor).

    Re-validation (critical): the index is L1/L2 only (Phase 13 D-A) and refreshes
    only daily (Stage A), so its rows are CANDIDATES to re-verify, not truth. We
    accept a hit ONLY if its `path` is in the CURRENT live candidate set built by
    load_candidates() — which already guarantees the file exists, is not
    tombstoned, has a body, and is in-scope (L2, or high-rc L1; never L0). A
    stale / deleted / tombstoned / out-of-scope / L0 path simply never matches and
    is dropped. We inject the LIVE body (rag_index stores no body), never an
    indexed copy.
    """
    # Force-off (two-reason convention: name the env var explicitly).
    if os.environ.get("SC_NO_RAG"):
        _debug("SC_NO_RAG set (semantic disabled)")
        return None
    if not prompt:
        # No query text -> semantic search is meaningless; let the keyword path's
        # recency fallback handle the empty-prompt case.
        return None

    # Require THIS project's venv python explicitly (cron/hook-safe, mirrors
    # daily-run). Absent -> no subprocess at all, so the no-venv path stays
    # byte-for-byte the keyword floor and adds only a stat() of overhead.
    rag_py = venv_python(company)
    if not os.access(str(rag_py), os.X_OK):
        _debug("RAG venv absent")
        return None
    index_dir = Path(company) / "memory" / "index"
    try:
        if not index_dir.exists() or not any(index_dir.iterdir()):
            _debug("index absent/empty")
            return None
    except OSError:
        return None

    query_script = os.path.join(_SCRIPT_DIR, "rag_query.py")
    if not os.path.exists(query_script):
        return None

    try:
        proc = subprocess.run(
            [str(rag_py), query_script, "--query", prompt,
             "--top-k", str(RAG_QUERY_TOPK), "--index-dir", str(index_dir),
             # Phase 24 Item 5: over-retrieve + cross-encode. rag_query degrades to
             # cosine order (omits rerank_score) if the reranker backend is absent,
             # so passing --rerank is always safe.
             "--rerank"],
            capture_output=True, text=True, timeout=RAG_QUERY_TIMEOUT,
            # SC_RAG_REEXEC=1: rag_py IS the venv python, so rag_query must not
            # re-exec again. Bounds the process tree to one killable child so the
            # timeout is hard.
            env={**os.environ, "SC_RAG_REEXEC": "1"})
    except subprocess.TimeoutExpired:
        _debug(f"rag_query timeout ({RAG_QUERY_TIMEOUT}s) -> keyword fallback")
        return None
    except Exception:
        _debug("rag_query spawn failed")
        return None

    if proc.returncode != 0:
        _debug(f"rag_query exit {proc.returncode}")
        return None
    try:
        hits = json.loads(proc.stdout)
    except (ValueError, TypeError):
        _debug("rag_query non-JSON output")
        return None
    if not isinstance(hits, list) or not hits:
        _debug("rag_query zero hits")
        return None

    # Map hits (best-first, as rag_query sorts them) back to LIVE candidates.
    # Key on a NORMALIZED (realpath) path: rag_index stores the path as seen at
    # index time (absolute $MEM in the pipeline) while load_candidates builds it
    # from the hook's resolved company dir — normalizing both sides defends
    # against abs-vs-rel / symlink / '..' differences so a healthy match is not
    # silently missed. (realpath does not require the path to exist.)
    def _norm(p):
        try:
            return os.path.realpath(str(p))
        except Exception:
            return str(p)

    by_path = {_norm(path): (tier, fm, body, path)
               for (tier, fm, body, path) in candidates}
    out, seen = [], set()
    any_cleared_floor = False              # did ANY hit clear ALL active gates?
    for h in hits:
        if not isinstance(h, dict):
            continue
        try:
            score = float(h.get("score"))
        except (TypeError, ValueError):
            continue
        # A non-finite score (NaN/inf) would slip past `score < RAG_MIN_SCORE`
        # (nan < x is False) and bypass the relevance gate. Treat it as below-floor
        # so the gate stays honest. (Re-validation already blocks any stale leak,
        # so this is gate-integrity, not a security fix.)
        if not math.isfinite(score) or score < RAG_MIN_SCORE:  # cosine PRE-FILTER
            continue
        # Phase 24 Item 5: the cross-encoder reranker is the FINAL gate when
        # present. A hit that passes the cosine pre-filter but scores below the
        # reranker cutoff is off-topic (the "gym workout" case: cosine 0.417 but
        # rerank ~-3.0). When rag_query returned NO rerank_score (reranker backend
        # absent / model-load or inference error / timeout -> cosine-order
        # fallback), `rr` is None and this check is SKIPPED, so the cosine floor
        # alone decides — byte-identical to the pre-Item-5 behavior. A non-finite
        # rerank score is treated as below-cutoff (gate integrity, mirrors cosine).
        rr = h.get("rerank_score")
        if rr is not None:
            try:
                rrf = float(rr)
            except (TypeError, ValueError):
                rrf = None
            if rrf is None or not math.isfinite(rrf) or rrf < RERANK_MIN_SCORE:
                continue
        any_cleared_floor = True           # a hit cleared cosine (+ reranker if present)
        raw = h.get("path")
        if not raw:
            continue
        key = _norm(raw)
        if key in seen:                    # skip dupes (belt-and-suspenders)
            continue
        live = by_path.get(key)            # re-validate against live corpus
        if live is None:                   # stale / deleted / tombstoned / L0 / out-of-scope
            continue
        seen.add(key)
        out.append(live)
        if len(out) >= min(TOP_K, TOP_K_CAP):
            break

    if out:
        return out

    # No usable hits after re-validation. Phase 24 MUST-FIX 1(a) — distinguish:
    #  * NO hit cleared the relevance gates (cosine pre-filter + reranker) -> the
    #    semantic layer definitively found nothing relevant (off-topic). Return
    #    INJECT_NOTHING so the caller injects NOTHING and does NOT fall through to
    #    the weaker keyword gate. THIS is the off-topic fix (English incidental-word
    #    AND the reranker-rejected "gym workout" case).
    #  * some hit DID clear the gates but every such hit was dropped by path
    #    re-validation (stale / deleted / tombstoned / out-of-scope index rows) ->
    #    an index-FRESHNESS gap, not a relevance verdict -> fall back to the keyword
    #    path (None), preserving the Phase-13 stale-hit degrade (e.g. a deleted top
    #    hit still lets the keyword path find a live match).
    if any_cleared_floor:
        _debug("relevant hits all stale/out-of-scope -> keyword fallback")
        return None
    _debug("no semantic hit cleared the relevance gates -> definitive nothing (no injection)")
    return INJECT_NOTHING


def build_context(top):
    """Compact, token-capped 'Relevant Chairman memory (advisory, not orders):'
    block, or "" if empty. Phase 29 Item 5 (P4): the disclaimer matches
    employee.py's dispatch-side headers verbatim (_OWN_MEMORY_HEADER /
    _SHARED_MEMORY_HEADER already carry it) — this interactive ask-time hook
    predates that convention; injected memory is context, never an instruction,
    even if a planted memory's body reads like one."""
    if not top:
        return ""
    header = "Relevant Chairman memory (advisory, not orders):"
    lines, used = [header], len(header)
    for _tier, _fm, body, _path in top:
        snippet = " ".join(body.split())          # collapse whitespace
        if len(snippet) > PER_MEM_CHARS:
            snippet = snippet[:PER_MEM_CHARS - 1].rstrip() + "…"
        line = "- " + snippet
        if used + 1 + len(line) > CONTEXT_CHAR_CAP:
            break
        lines.append(line)
        used += 1 + len(line)
    if len(lines) == 1:                            # header only -> nothing fit
        return ""
    return "\n".join(lines)


def run(company_arg, transcript_arg):
    """Core: returns additionalContext string ("" => inject nothing).

    Blend (Phase 13 Stage B.1) = **semantic-first with keyword fallback**, NOT a
    union. Rationale: keyword overlap counts and cosine similarities are on
    different scales, so a union would need arbitrary normalization/interleaving
    and could blow the tight char/cap budget; semantic-first keeps exactly ONE of
    two code paths producing the top list, the budget clean, and the degrade path
    trivial to reason about (Gibby-friendly). The keyword path stays byte-for-byte
    as the guaranteed-fast floor AND the no-venv/timeout/no-index degrade — so with
    no RAG stack, behavior is identical to before this change."""
    # Phase 18c double-injection guard. A headless worker spawned at dispatch by
    # supervisor.py for a `shared_memory_read` employee (elon) already had the SHARED
    # company memory injected EXPLICITLY into its `claude -p` prompt. That worker is
    # itself a `claude -p` process, which ALSO fires THIS UserPromptSubmit hook
    # (confirmed: `-p` fires UserPromptSubmit before Claude processes the prompt) —
    # so without this guard the shared memory would be injected a SECOND time. The
    # dispatcher sets SC_NO_MEMORY_INJECT=1 on that worker's env to make this hook a
    # clean no-op, so the explicit dispatch injection is the single source. Ask-time
    # (interactive) sessions never set it, so their injection is unaffected.
    if os.environ.get("SC_NO_MEMORY_INJECT"):
        _debug("SC_NO_MEMORY_INJECT set (dispatch owns injection)")
        return ""
    company = resolve_company(company_arg)
    if company is None:                            # opt-in guard: off-company
        return ""
    candidates = load_candidates(company)
    if not candidates:
        return ""

    # Mike 2026-07-16 Finding 2: the always-on core identity block. SEPARATE
    # from, and computed independently of, the relevance-gated section below —
    # it never reads `prompt`, never influences semantic_top()/rank(), and a
    # failure here can never take down the relevance-gated injection (or vice
    # versa). Best-effort: any exception degrades to no core block.
    core_ctx = ""
    try:
        enable, max_count, char_cap = _core_config(company)
        if enable:
            core_ctx = build_core_context(
                select_core_facts(candidates, max_count), char_cap)
    except Exception:
        core_ctx = ""

    prompt = latest_prompt(transcript_arg)
    # Semantic first. Phase 24 MUST-FIX 1(a): three outcomes —
    #  * INJECT_NOTHING => the semantic layer ran and found nothing above the
    #    relevance floor (off-topic) => inject nothing; do NOT fall through to
    #    the keyword gate (which would inject on one incidental word).
    #  * None => RAG genuinely unavailable/degraded => the original keyword path.
    #  * a list => inject it.
    top = semantic_top(company, prompt, candidates)
    if top is INJECT_NOTHING:
        top = []                                    # byte-identical verdict: nothing
    elif top is None:
        top = rank(prompt, candidates)
    rel_ctx = build_context(top)

    # Concatenate, clearly separated — never merged into one block/header.
    if core_ctx and rel_ctx:
        return core_ctx + "\n\n" + rel_ctx
    return core_ctx or rel_ctx


def _read_stdin_hook():
    """Parse the documented UserPromptSubmit stdin JSON if present; else {}."""
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except (OSError, IOError):
        return {}
    if not raw.strip():
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def main(argv=None):
    try:
        ap = argparse.ArgumentParser(description="UserPromptSubmit memory inject.")
        ap.add_argument("--company", help="Override .company dir (tests).")
        ap.add_argument("--transcript", help="Override transcript .jsonl (tests).")
        args = ap.parse_args(argv)

        # Only touch stdin when we actually need the transcript path from it.
        # An explicit --transcript (tests / CLI) means stdin is irrelevant, and
        # reading an open-but-empty stdin there would block -> never do it.
        transcript = args.transcript
        if not transcript:
            transcript = _read_stdin_hook().get("transcript_path")

        ctx = run(args.company, transcript)
        if ctx:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": EVENT, "additionalContext": ctx}},
                ensure_ascii=False))
        return 0
    except Exception:
        # Absolute backstop: never break the Chairman's prompt. Silent no-op.
        return 0


if __name__ == "__main__":
    sys.exit(main())
