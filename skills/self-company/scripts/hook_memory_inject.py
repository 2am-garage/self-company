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
import os
import re
import sys
from pathlib import Path

# --- import the SINGLE tombstone vocabulary (best-effort, same dir) -----------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from tombstone import is_tombstoned
except Exception:  # pragma: no cover - defensive; keep the hook alive
    def is_tombstoned(fm):
        return str(fm.get("status") or "").strip().lower() in (
            "archived", "defunct", "absorbed")

# Phase 11 Item 2: the fragile frontmatter delimiter + key:value split lives in
# ONE shared module (frontmatter.py). Best-effort import + verbatim fallback,
# same pattern as the tombstone import above.
try:
    from frontmatter import parse as _fm_parse
except Exception:  # pragma: no cover - verbatim fallback (authoritative: frontmatter.py)
    def _fm_parse(text):
        lines = text.split('\n')
        if lines[0].strip() != '---':
            return {}, text
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                end = i
                break
        if end is None:
            return {}, text
        fm = {}
        for line in lines[1:end]:
            s = line.strip()
            if not s or s.startswith('#') or ':' not in s:
                continue
            k, v = s.split(':', 1)
            fm[k.strip()] = v.strip()
        return fm, '\n'.join(lines[end + 1:])

EVENT = "UserPromptSubmit"

# Scoring knobs (env-overridable for tuning/tests; sane stdlib defaults).
TOP_K = int(os.environ.get("SELF_COMPANY_INJECT_TOPK", "4"))
TOP_K_CAP = 5                     # hard ceiling regardless of env
CONTEXT_CHAR_CAP = 600           # total additionalContext budget (~token-capped)
PER_MEM_CHARS = 180              # per-memory body trim
MIN_OVERLAP = 1                  # relevance floor: >=1 shared keyword or silent
HIGH_RC = int(os.environ.get("SELF_COMPANY_INJECT_HIGH_RC", "2"))  # L1 gate
TIER_WEIGHT = {"L2": 1.0, "L1": 0.6}

# Small stopword set so incidental common words don't manufacture "relevance".
_STOP = frozenset("""
a an the this that these those and or but if then else for of to in on at by
with from into over under is are was were be been being do does did doing have
has had having i you he she it we they me him her us them my your his its our
their what which who whom how when where why all any some no not can could would
should will shall may might must about as so than too very just also again more
most such only own same both each few other new use using used get got make made
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

    No prompt keywords at all (missing/short transcript) -> fall back to recency
    ranking so a fresh turn still gets the freshest durable facts.
    """
    p_tokens = _tokens(prompt)

    if not p_tokens:
        # Recency fallback: newest durable memories, weighted by tier.
        ranked = sorted(
            candidates,
            key=lambda c: (_recency_key(c[1]), TIER_WEIGHT.get(c[0], 0.5)),
            reverse=True)
        return ranked[:min(TOP_K, TOP_K_CAP)]

    scored = []
    for tier, fm, body, path in candidates:
        hay = _tokens(" ".join((fm.get("id", ""), fm.get("category", ""), body)))
        overlap = len(p_tokens & hay)
        if overlap < MIN_OVERLAP:            # relevance gate
            continue
        weight = TIER_WEIGHT.get(tier, 0.5)
        rc = _int(fm.get("reinforce_count"), 1)
        score = overlap * weight * max(rc, 1)
        scored.append((score, overlap, _recency_key(fm), tier, fm, body, path))
    # Highest score first; ties broken by overlap then recency (deterministic).
    scored.sort(key=lambda s: (s[0], s[1], s[2]), reverse=True)
    return [(t, fm, body, path)
            for (_s, _o, _r, t, fm, body, path) in scored[:min(TOP_K, TOP_K_CAP)]]


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
    """Core: returns additionalContext string ("" => inject nothing)."""
    company = resolve_company(company_arg)
    if company is None:                            # opt-in guard: off-company
        return ""
    candidates = load_candidates(company)
    if not candidates:
        return ""
    prompt = latest_prompt(transcript_arg)
    return build_context(rank(prompt, candidates))


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
