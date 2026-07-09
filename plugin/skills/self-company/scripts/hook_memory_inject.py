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

EVENT = "UserPromptSubmit"

# Scoring knobs (env-overridable for tuning/tests; sane stdlib defaults).
TOP_K = _env_num("SELF_COMPANY_INJECT_TOPK", 4, int)
TOP_K_CAP = 5                     # hard ceiling regardless of env
CONTEXT_CHAR_CAP = 600           # total additionalContext budget (~token-capped)
PER_MEM_CHARS = 180              # per-memory body trim
MIN_OVERLAP = 1                  # relevance floor: >=1 shared keyword or silent
HIGH_RC = _env_num("SELF_COMPANY_INJECT_HIGH_RC", 2, int)  # L1 gate
TIER_WEIGHT = {"L2": 1.0, "L1": 0.6}

# Phase 24 MUST-FIX 1(b): a single incidental overlap on a short generic word is
# how off-topic ENGLISH prompts sneak past the keyword floor (Gibby: "how do I
# CHANGE a flat tire" injected a git-workflow memory on the word "change"). A
# LONE overlapping token must be either "specific" (>= SPECIFIC_TOKEN_LEN chars)
# or a meaningful fraction (>= MIN_OVERLAP_RATIO) of the prompt's distinct
# tokens; otherwise it does not clear the gate. This hardens the KEYWORD degrade
# path (no-venv); the primary defense is the expanded stoplist below plus the
# semantic layer's definitive no-match verdict (INJECT_NOTHING) when RAG is up.
SPECIFIC_TOKEN_LEN = 5
MIN_OVERLAP_RATIO = _env_num("SELF_COMPANY_INJECT_MIN_OVERLAP_RATIO", 0.34, float)

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
RAG_QUERY_TIMEOUT = _env_num("SELF_COMPANY_INJECT_RAG_TIMEOUT", 7.0, float)  # s
# Ask for more hits than we inject: some will be filtered out as stale/tombstoned/
# out-of-scope when re-validated against the live candidate set, so over-fetch to
# still have enough survivors to fill the cap.
RAG_QUERY_TOPK = max(TOP_K_CAP, TOP_K) * 2
# Semantic relevance floor (cosine): honor the hook's "relevance-gated — never
# pollute the prompt" hard rule. Below this, a hit is treated as off-topic noise
# and dropped; if nothing clears the floor we fall back to the keyword path (which
# has its own MIN_OVERLAP gate) so an off-topic prompt still injects nothing.
#
# Phase 24 Item 1: retuned from 0.30 to 0.35, DATA-DRIVEN — the old
# English-only bge-small model made 0.30 filter nothing at all (every query,
# on- or off-topic, scored 0.45-0.65). Tony's post-swap sweep on the real
# 55-memory corpus (11 off-topic EN+ZH probes vs 16 on-topic EN+ZH queries,
# post model-swap + hybrid/RRF): off-topic top-1 scores topped out at 0.306;
# the lowest true on-topic top-1 score was 0.419. 0.35 sits in that gap with
# margin on both sides. See references/rag.md for the full sweep.
RAG_MIN_SCORE = _env_num("SELF_COMPANY_INJECT_RAG_MIN_SCORE", 0.35, float)

# Small stopword set so incidental common words don't manufacture "relevance".
# Phase 24 MUST-FIX 1(b): expanded with the generic connectors / light verbs that
# let off-topic English prompts collide on ONE incidental word (change, without,
# going, need, want, like, work, help, set, way, thing, …). These carry no topic
# so a lone overlap on them is noise, never relevance. Content words (neovim,
# terraform, chinese, backup, …) are untouched, so real matches are unaffected.
_STOP = frozenset("""
a an the this that these those and or but if then else for of to in on at by
with from into over under is are was were be been being do does did doing have
has had having i you he she it we they me him her us them my your his its our
their what which who whom how when where why all any some no not can could would
should will shall may might must about as so than too very just also again more
most such only own same both each few other new use using used get got make made
change changed changing without within going need needs needed want wants wanted
like likes work works working help helps helping set sets setting thing things
way ways know knows think thinks look looks looking find finds tell tells give
gives take takes keep keeps come comes put puts run running still back even well
around along across able really actually maybe perhaps please thanks thank okay
sure something anything everything someone anyone everyone here there where
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

    scored = []
    for tier, fm, body, path in candidates:
        hay = _tokens(" ".join((fm.get("id", ""), fm.get("category", ""), body)))
        overlap_tokens = p_tokens & hay
        overlap = len(overlap_tokens)
        if overlap < MIN_OVERLAP:            # relevance gate
            continue
        # Phase 24 MUST-FIX 1(b): a LONE overlapping token clears the gate only
        # if it is specific (long enough to be topical) OR a meaningful fraction
        # of the prompt — so one short incidental word (that survived the
        # stoplist) can't manufacture relevance on the no-venv keyword path.
        if overlap == 1:
            lone = next(iter(overlap_tokens))
            if (len(lone) < SPECIFIC_TOKEN_LEN
                    and (overlap / len(p_tokens)) < MIN_OVERLAP_RATIO):
                continue
        weight = TIER_WEIGHT.get(tier, 0.5)
        rc = _int(fm.get("reinforce_count"), 1)
        score = overlap * weight * max(rc, 1)
        scored.append((score, overlap, _recency_key(fm), tier, fm, body, path))
    # Highest score first; ties broken by overlap then recency (deterministic).
    scored.sort(key=lambda s: (s[0], s[1], s[2]), reverse=True)
    return [(t, fm, body, path)
            for (_s, _o, _r, t, fm, body, path) in scored[:min(TOP_K, TOP_K_CAP)]]


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
             "--top-k", str(RAG_QUERY_TOPK), "--index-dir", str(index_dir)],
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
    any_cleared_floor = False              # did ANY hit clear the cosine floor?
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
        if not math.isfinite(score) or score < RAG_MIN_SCORE:  # relevance-gated
            continue
        any_cleared_floor = True           # a hit was semantically relevant
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
    #  * NO hit cleared the cosine floor -> the semantic layer definitively found
    #    nothing relevant (off-topic). Return INJECT_NOTHING so the caller injects
    #    NOTHING and does NOT fall through to the weaker keyword gate (which would
    #    inject on a single incidental word). THIS is the off-topic-English fix.
    #  * some hit DID clear the floor but every above-floor hit was dropped by
    #    path re-validation (stale / deleted / tombstoned / out-of-scope index
    #    rows) -> an index-FRESHNESS gap, not a relevance verdict -> fall back to
    #    the keyword path (None), preserving the Phase-13 stale-hit degrade
    #    (e.g. a deleted top hit still lets the keyword path find a live match).
    if any_cleared_floor:
        _debug("above-floor hits all stale/out-of-scope -> keyword fallback")
        return None
    _debug("no semantic hit cleared the floor -> definitive nothing (no injection)")
    return INJECT_NOTHING


def build_context(top):
    """Compact, token-capped 'Relevant Chairman memory:' block, or "" if empty."""
    if not top:
        return ""
    header = "Relevant Chairman memory:"
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
    prompt = latest_prompt(transcript_arg)
    # Semantic first. Phase 24 MUST-FIX 1(a): three outcomes —
    #  * INJECT_NOTHING => the semantic layer ran and found nothing above the
    #    relevance floor (off-topic) => inject nothing; do NOT fall through to
    #    the keyword gate (which would inject on one incidental word).
    #  * None => RAG genuinely unavailable/degraded => the original keyword path.
    #  * a list => inject it.
    top = semantic_top(company, prompt, candidates)
    if top is INJECT_NOTHING:
        return ""
    if top is None:
        top = rank(prompt, candidates)
    return build_context(top)


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
