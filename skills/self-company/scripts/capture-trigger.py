#!/usr/bin/env python3
"""
capture-trigger — CAPTURE stage entrypoint for the self-company memory pipeline.

Invoked by a Claude Code **Stop hook** at the end of each conversation. Reads the
session transcript, asks a cheap model (Haiku) to extract observations about the
Chairman, and writes them as L0 draft memories with sources (per
references/pipeline.md stage [1] CAPTURE and policy.md §4.2 frontmatter).

Design constraints honoured here:
- **No recursion.** Claude Code sets `stop_hook_active: true` when a Stop hook is
  already running; we exit immediately in that case. We also set a guard env var
  for the headless model call as a second layer.
- **Graceful degradation.** If `.company/` is missing, the transcript is
  unreadable, the `claude` CLI is absent, or the model returns nothing parseable,
  we exit 0 without writing — CAPTURE never crashes a session.
- **Real-time, not budget-gated.** Per policy.md §3.2 / triggers.md §1, real-time
  CAPTURE runs even at the daily ceiling, so there is no token-breaker gate here.
- **Pure stdlib** (json, os, re, subprocess, datetime, pathlib).

Hook input (stdin JSON, Claude Code Stop hook):
  { "session_id": "...", "transcript_path": "/abs/....jsonl",
    "cwd": "...", "stop_hook_active": false }

Test/manual usage (bypass stdin):
  capture-trigger.py --transcript PATH --session ID [--company DIR] [--dry-run]
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Shared policy loader (org/policy.md §7) — best-effort import, mirroring
# decay.py: if the module is somehow missing we fall back to the built-in
# defaults rather than ever crashing the Stop hook.
try:
    from policy_config import resolve as _resolve_config
except Exception:  # pragma: no cover - defensive
    _resolve_config = None

# Phase 6 Item 1: tombstone vocabulary (archived / defunct / absorbed) lives in
# ONE shared place (tombstone.py, same dir) so scanners can't drift. Best-effort
# import + verbatim fallback, mirroring the policy loader above.
try:
    from tombstone import TOMBSTONE_STATUSES, is_tombstoned
except Exception:  # pragma: no cover - defensive fallback (authoritative copy: tombstone.py)
    TOMBSTONE_STATUSES = frozenset({"archived", "defunct", "absorbed"})

    def is_tombstoned(fm):
        return str(fm.get("status") or "").strip().lower() in TOMBSTONE_STATUSES

# Phase 11 Item 2 / C2: the fragile frontmatter PARSE/SPLIT/SERIALIZE seam AND
# the `sources:` token extractor now live in ONE shared module (frontmatter.py,
# same dir) so the ten scanners can't drift. Best-effort import + verbatim
# fallback, mirroring the tombstone import above. This dedupes the SOURCE_ITEM_RE
# copy that used to sit inline below (byte-identical to reinforce_memory.py's).
try:
    from frontmatter import parse as _fm_parse, serialize as _fm_serialize, \
        SOURCE_ITEM_RE, tokenize_sources  # noqa: F401
except Exception:  # pragma: no cover - verbatim fallback (authoritative: frontmatter.py)
    SOURCE_ITEM_RE = re.compile(r'"[^"]*"')

    def tokenize_sources(raw):
        return SOURCE_ITEM_RE.findall(raw or "")

    def _fm_split(text):
        lines = text.split('\n')
        if lines[0].strip() != '---':
            return [], text
        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                return lines[1:i], '\n'.join(lines[i + 1:])
        return [], text

    def _fm_parse(text):
        raw_fm_lines, body = _fm_split(text)
        fm = {}
        for line in raw_fm_lines:
            s = line.strip()
            if not s or s.startswith('#') or ':' not in s:
                continue
            key, val = s.split(':', 1)
            fm[key.strip()] = val.strip()
        return fm, body

    def _fm_serialize(fm, body, order=None):
        keys = []
        if order:
            for k in order:
                if k in fm and k not in keys:
                    keys.append(k)
        for k in fm:
            if k not in keys:
                keys.append(k)
        out = ['---']
        for k in keys:
            out.append(f"{k}: {fm[k]}")
        out.append('---')
        return '\n'.join(out) + '\n' + body

RECURSION_GUARD = "SELF_COMPANY_CAPTURE_ACTIVE"
DEFAULT_MODEL = os.environ.get("SELF_COMPANY_CAPTURE_MODEL", "claude-haiku-4-5-20251001")
MAX_CHAIRMAN_CHARS = 24000   # cap transcript size fed to the model
MAX_OBSERVATIONS = 12        # cap L0 drafts written per session

# Per-session CAPTURE cooldown (survey F3): the Stop hook fires on EVERY
# reply-stop, not once per conversation — one session produced 19 CAPTURE
# blocks and 63 L0 files in a day. After a capture ATTEMPT for a session,
# further hook fires for the SAME session inside this window are logged
# no-ops (no model call, no transcript read). Policy §7.7
# CAPTURE_COOLDOWN_MINUTES overrides the default; 0 disables the throttle.
DEFAULT_CAPTURE_COOLDOWN_MINUTES = 30
# One small JSON map {safe-session-id: iso-timestamp} under <company>/ops/,
# mirroring the other ops dotfile markers (.last_notified, logs/.agent_runs_*).
COOLDOWN_MARKER = ".capture-cooldown.json"

# Reinforce-not-duplicate (survey F3, second half): a compact digest of RECENT
# L0 memories is fed to the prompt so the model returns
# `"reinforce": "<existing-id>"` for an already-captured fact instead of
# minting a fresh slug for it (the id-only dedup cannot catch fresh slugs).
RECENT_WINDOW_HOURS = 48          # digest window
RECENT_DIGEST_MAX = 30            # max digest entries
RECENT_GIST_CHARS = 140           # per-entry one-line gist cap
RECENT_DIGEST_CHAR_BUDGET = 4000  # total prompt chars the digest may consume

# The three L0/L2 knowledge classes CAPTURE tags each observation with. Kept in
# sync with decay.py::L2_CATEGORIES so the promoter routes a promoted memory to
# L2-cold/<category>/. Default is the historical class when the model omits it.
CATEGORIES = ("profile", "projects", "preferences")
DEFAULT_CATEGORY = "preferences"


def _norm_category(value):
    """Map a model-supplied category onto {profile, projects, preferences}.

    Tolerant of singular/spacing/case ('Project' -> 'projects'); anything
    unrecognised (or missing) falls back to the safe default, preferences.
    """
    v = str(value or "").strip().lower()
    if v in CATEGORIES:
        return v
    if v in ("project", "profiles", "preference", "prefs", "pref"):
        return {"project": "projects", "profiles": "profile",
                "preference": "preferences", "prefs": "preferences",
                "pref": "preferences"}[v]
    return DEFAULT_CATEGORY

# A2 backstop: quarantine observations that are clearly operational noise (the
# company's own malfunctions) rather than durable facts about the Chairman.
# Conservative — requires a system noun NEAR a failure verb, so it won't catch a
# standing preference like "I want push notifications, not Discord".
SYSTEM_NOISE_RE = re.compile(
    r"\b(skill|script|cron|scheduler|agent|hook|index|rag|pipeline|daemon|job)\b"
    r"[^.]{0,40}\b(fail|failed|failing|error|errored|broke|broken|crash|"
    r"didn'?t\s+(?:run|fire|work)|not\s+(?:run|install|work)|misbehav|bug)\b",
    re.IGNORECASE,
)

# B4 (Phase 5 Item 4, N5): quarantine observations about the COMPANY'S OWN
# work-state — maintenance/phases/entropy/PR progress is transient session
# state about this machinery, not durable Chairman knowledge (and via N1 such
# records were racing toward permanent L2). Deliberately NARROW: every
# alternative names the company's own machinery or a repo-workflow event
# ("phase 4 merged", "entropy dropped"), so a genuine standing preference
# ("wants push notifications, not Discord") never matches.
# Gibby (Phase 5 red-team): a Chairman who genuinely works ON entropy/ML/repos
# must not have real facts eaten, so (a) "entropy" only matches with a
# movement/valued verb — bare information-theory nouns ("the entropy rate of
# Markov chains", "a maximum-entropy score function") survive; (b) the PR
# alternate requires a SPECIFIC numbered PR ("PR #28 merged") — numberless
# workflow preferences ("wants PRs merged via squash") survive. Numberless
# noise ("the pull request was merged") is accepted leakage: the capture
# prompt's exclusion is the first line of defense and decay kills the residue;
# a silently eaten preference is permanent.
META_NOISE_RE = re.compile(
    r"(?:\bself-company\b"
    r"|\bentropy\s+(?:(?:score|rate)\s+)?"
    r"(?:dropped|rose|jumped|tripled|doubled|fell|climbed"
    r"|went\s+(?:up|down)\b|went\s+(?:from|to)\s+[0-9.]"
    r"|(?:is|was)\s+(?:now\s+)?[0-9.]|hit\s+[0-9.]|reached\s+[0-9.])"
    r"|\bupgrade[\s-]candidates?\b"
    r"|\b(?:phase|sprint)\s*#?\d+\s+(?:was\s+|is\s+|got\s+)?"
    r"(?:merged|landed|completed?|done|shipped|approved)"
    r"|\b(?:pr|pull\s+request)s?\s*#?\s*\d+\s+(?:was\s+|is\s+|got\s+|are\s+)?"
    r"(?:merged|landed|opened?|approved|closed)"
    r"|\b(?:merged|landed|opened|approved|closed)\s+(?:the\s+)?"
    r"(?:pr|pull\s+request)s?\s*#?\s*\d+\b"
    r"|\bmemory\s+(?:pipeline|tiers?|entropy|consolidation)\b"
    r"|\bconsolidation\s+(?:pass|run|agent|backlog)\b"
    r"|\bdecay\s+(?:pass|sweep|run|score)\b"
    r"|\bL[012][\s-](?:working|warm|cold)\b"
    r"|\badjudications?\b"
    r"|\bfail[\s-]streak\b"
    r")",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------------
# Transcript reading (deterministic, unit-tested)
# ----------------------------------------------------------------------------

def extract_chairman_lines(transcript_path):
    """
    Return [(line_index, text)] of Chairman (user) utterances from a Claude Code
    transcript .jsonl. Only plain-string user content is the Chairman typing;
    list content is tool_result noise and is skipped. Never raises.
    """
    out = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for i, ln in enumerate(f):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if d.get("type") != "user":
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str):
                    text = content.strip()
                    # skip command/system echoes and empties
                    if text and not text.startswith("<"):
                        out.append((i, text))
    except (OSError, IOError):
        return []
    return out


def build_capture_prompt(chairman_lines, existing_ids, today=None,
                         recent_memories=None):
    """Construct the Haiku CAPTURE instruction. Pure string, easy to test.

    `today` (ISO run date) is injected so the model can resolve relative
    project dates ("by Friday", "next week") to absolute calendar dates.
    `recent_memories` is an [(id, gist)] digest (see recent_l0_digest); when
    present, the model is told to `reinforce` those ids instead of restating
    the same fact under a fresh slug.
    """
    today = today or date.today().isoformat()
    convo, total = [], 0
    for idx, text in chairman_lines:
        chunk = f"[#{idx}] {text}"
        total += len(chunk)
        if total > MAX_CHAIRMAN_CHARS:
            break
        convo.append(chunk)
    convo_text = "\n".join(convo)
    existing = ", ".join(sorted(existing_ids)) if existing_ids else "(none)"
    recent_block = ""
    if recent_memories:
        entries, used = [], 0
        for mid, gist in list(recent_memories)[:RECENT_DIGEST_MAX]:
            entry = f"- {mid}: {gist}"
            used += len(entry) + 1
            if used > RECENT_DIGEST_CHAR_BUDGET:
                break
            entries.append(entry)
        if entries:
            recent_block = (
                "=== Recently captured memories (last 48h) ===\n"
                "These facts are ALREADY captured. Do NOT restate any of them "
                "under a new id. If an observation is the same fact as one of "
                'these, return it with "reinforce": "<that-id>" instead of a '
                "new id.\n" + "\n".join(entries) + "\n\n"
            )
    return (
        "You are the CAPTURE stage of a personal-memory pipeline. From the "
        "Chairman's messages below, extract durable facts about the *person* (the "
        "Chairman) and classify each into exactly ONE of three equally-important "
        "categories. Weight them evenly — do NOT default to preferences. Aim for a "
        "balanced mix; the memory is currently rich in preferences but empty of "
        "profile and projects, so actively hunt for profile and project facts, "
        "including ones stated only in passing.\n\n"
        "1. profile — durable facts about WHO the Chairman is: role, background, "
        "domain expertise, the tools/stack he uses, his environment/setup. "
        "e.g. 'Trades TWSE futures via the Shioaji API', 'Works primarily in "
        "Python on Linux', 'Background in quantitative finance'.\n"
        "2. projects — WHAT he is actively building: current work, goals, "
        "deadlines, constraints. Convert every relative date to an absolute one "
        f"using the run date {today} (e.g. if he says 'ship by Friday', write the "
        "actual YYYY-MM-DD of that Friday). "
        "e.g. 'Shipping the Phase-2 memory rebalance by 2026-07-10', 'Building a "
        "backtesting harness, constrained to stdlib only'.\n"
        "3. preferences — HOW he likes to be served or work: habits, working "
        "style, likes/dislikes. "
        "e.g. 'Wants push notifications, not Discord', 'Prefers async/await over "
        "sync in Python', 'Reviews diffs before every commit'.\n\n"
        "Prefer substance over restating style: a profile or project fact is more "
        "valuable than another phrasing of a known preference. Each observation "
        "MUST cite the message index and carry its category.\n\n"
        "DO NOT capture transient system/tool state or operational noise — this is "
        "a person's memory, not a bug tracker. Specifically EXCLUDE: reports that "
        "the skill/script/cron/agent failed or misbehaved, error messages, status "
        "of a run, or the company's own behavior. Examples to REJECT: 'the skill "
        "failed at 2am', 'the cron didn't fire', 'RAG isn't installed'. A standing "
        "*preference* the Chairman states (e.g. 'I want push notifications, not "
        "Discord') IS valid; a one-off system glitch is NOT.\n\n"
        "ALSO EXCLUDE this company's OWN work-state: transient session-state "
        "about this memory system's maintenance, improvement phases, entropy "
        "scores, PRs/merges, surveys, upgrade candidates, or consolidation/decay "
        "runs is NOT a Chairman fact. Examples to REJECT: 'phase 4 merged and "
        "entropy dropped', 'the survey found 21 upgrade candidates', 'we fixed "
        "the reinforce semantics today'. Capture only DURABLE "
        "preferences/profile/projects facts about the person — things that will "
        "still be true and useful weeks from now.\n\n"
        f"Existing memory ids (do not duplicate): {existing}\n\n"
        f"{recent_block}"
        "Return ONLY a JSON array (no prose), each item:\n"
        '  {"id": "kebab-slug", "category": "profile|projects|preferences", '
        '"body": "1-2 sentence observation", '
        '"source_lines": [<int message index>, ...]}\n'
        "If an observation is the SAME fact as one of the recently captured "
        'memories listed above, add "reinforce": "<existing-id>" to that item '
        '(keep "body" and "source_lines") instead of minting a new id for an '
        "already-captured fact.\n"
        f"Return at most {MAX_OBSERVATIONS} items. If nothing durable, return [].\n\n"
        "=== Chairman messages ===\n"
        f"{convo_text}\n"
    )


# ----------------------------------------------------------------------------
# Model call (guarded, degrades to [])
# ----------------------------------------------------------------------------

def run_capture_model(prompt, model=DEFAULT_MODEL, timeout=120):
    """
    Run the headless `claude` CLI to perform extraction. Returns a list of
    observation dicts, or [] on any failure. Sets the recursion guard env so the
    child's own Stop hook (if any) no-ops.
    """
    if not _which("claude"):
        return []
    env = dict(os.environ)
    env[RECURSION_GUARD] = "1"
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    return _parse_observations(proc.stdout)


def _parse_observations(text):
    """Extract the first JSON array of observations from model output. [] on fail."""
    if not text:
        return []
    # find the first '[' ... matching ']' span and try to parse
    start = text.find("[")
    if start == -1:
        return []
    for end in range(len(text), start, -1):
        if text[end - 1] != "]":
            continue
        try:
            data = json.loads(text[start:end])
        except ValueError:
            continue
        if isinstance(data, list):
            out = []
            for o in data:
                if not isinstance(o, dict):
                    continue
                # A valid item is a NEW observation (id + body) or a REINFORCE
                # reference to an already-captured memory. A reinforce item
                # should still carry body/source_lines so an unknown target can
                # fall back to the normal new-observation path (fail-safe).
                if (o.get("id") and o.get("body")) or o.get("reinforce"):
                    # Normalise category onto the allowed set (default preferences)
                    # so every downstream observation carries a routable class.
                    o["category"] = _norm_category(o.get("category"))
                    out.append(o)
            return out
    return []


def _which(name):
    from shutil import which
    return which(name) is not None


# ----------------------------------------------------------------------------
# L0 writing (deterministic, unit-tested)
# ----------------------------------------------------------------------------

def _slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")
    return s or "obs"


# Session id is embedded verbatim into the L0 `sources:` frontmatter line. A
# session id carrying YAML-breaking chars (newline, `"`, `[`/`]`, `#`) could
# forge a frontmatter field or prematurely close the block. Real Claude Code
# session ids are UUIDs, but sanitise defensively so write_l0 can NEVER emit
# malformed YAML regardless of caller. UUID chars pass through unchanged.
_SAFE_SOURCE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_source_token(s):
    return _SAFE_SOURCE_TOKEN_RE.sub("-", str(s)) or "unknown-session"


def _parse_frontmatter(text):
    """Frontmatter read via the shared parser (Phase 11) -> ({key: value}, body)
    for a valid `---` block, else (None, ""). The old inline parser returned
    (fm-or-None, closing-line-index); callers now consume the body string
    directly, and the falsy/None sentinel still marks 'no usable frontmatter'
    (no opening/closing fence, or an empty block) exactly as before."""
    fm, body = _fm_parse(text)
    if not fm:
        return None, ""
    return fm, body


def _memory_id_paths(company_dir):
    """Map {memory id: Path} across all tiers. Tolerates unreadable files.
    Reinforce targets are resolved against these scanned ids — never by
    building a filesystem path from model output (no traversal surface)."""
    out = {}
    mem = Path(company_dir) / "memory"
    if not mem.exists():
        return out
    for p in mem.rglob("*.md"):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"^id:\s*(.+)$", text, re.MULTILINE)
        if m:
            out.setdefault(m.group(1).strip(), p)
    return out


def existing_memory_ids(company_dir):
    return set(_memory_id_paths(company_dir))


def _bump_reinforce(path, source_lines, safe_sid, today):
    """Reinforce an existing memory in place: reinforce_count bumps at most
    ONCE per distinct session id, last_reinforced=today, new source tokens
    appended (deduped) — mirrors reinforce_memory.py::apply_reinforcement's
    field updates, minus the absorb/delete (CAPTURE never deletes).

    Phase 5 Item 1 (N1): rc is a CROSS-SESSION recurrence signal (the rc>=2 /
    rc>=4 promotion gates trust it). If the target's sources already carry a
    token for THIS session id, a restatement merges the new source token but
    does NOT increment rc — one session can add at most +1 no matter how many
    times the model restates the fact. Returns True on success; any failure
    returns False so the caller can fall back fail-safe."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)
        if not fm:
            return False
        items = SOURCE_ITEM_RE.findall(fm.get("sources", ""))
        # Distinct-session check BEFORE merging: a token "[<sid>#<line>]" for
        # this session id means this session already reinforced the memory.
        session_prefix = f'"[{safe_sid}#'
        already_this_session = any(it.startswith(session_prefix) for it in items)
        for s in source_lines or []:
            if str(s).lstrip("-").isdigit():
                tok = f'"[{safe_sid}#{int(s)}]"'
                if tok not in items:
                    items.append(tok)
        try:
            rc = int(fm.get("reinforce_count", "1"))
        except ValueError:
            rc = 1
        if not already_this_session:
            rc += 1
        # In-place field updates, mirroring the old line-rewrite: only touch a
        # field that ALREADY exists in the frontmatter (a missing key stays
        # absent — never injected), and only rewrite sources when items exist.
        # serialize with order=list(fm) re-emits keys in the file's own order,
        # so a canonical memory round-trips byte-identically.
        if "reinforce_count" in fm:
            fm["reinforce_count"] = str(rc)
        if "last_reinforced" in fm:
            fm["last_reinforced"] = today
        if "sources" in fm and items:
            fm["sources"] = "[" + ", ".join(items) + "]"
        Path(path).write_text(
            _fm_serialize(fm, body, order=list(fm)), encoding="utf-8")
        return True
    except Exception:
        return False


def recent_l0_digest(company_dir, now=None, window_hours=RECENT_WINDOW_HOURS,
                     cap=RECENT_DIGEST_MAX):
    """[(id, one-line gist)] of active L0 memories created inside the window,
    newest first, capped. Fed to the prompt so the model reinforces an
    already-captured fact instead of minting a fresh slug for it. Never raises."""
    l0 = Path(company_dir) / "memory" / "L0-working"
    if not l0.exists():
        return []
    cutoff = ((now or datetime.now()) - timedelta(hours=window_hours)).date()
    entries = []
    for p in sorted(l0.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, mbody = _parse_frontmatter(text)
        if not fm or not fm.get("id"):
            continue
        if is_tombstoned(fm):  # archived / defunct (alias) / absorbed
            continue
        try:
            created = date.fromisoformat(fm.get("created", ""))
        except ValueError:
            continue
        if created < cutoff:
            continue
        body = " ".join(mbody.split())
        gist = body.split(". ")[0][:RECENT_GIST_CHARS]
        entries.append((fm.get("created", ""), fm["id"], gist))
    entries.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [(mid, gist) for _, mid, gist in entries[:cap]]


def write_l0(observations, session_id, company_dir, today=None):
    """
    Write each NEW observation as an L0 draft; apply each `reinforce` entry to
    its existing memory (rc+1, last_reinforced=today, source appended) — a
    reinforce NEVER writes a new file. An unknown/missing reinforce target
    falls back to the normal new-observation path (fail-safe). Skips new
    entries without source_lines (sources cannot be empty — VERIFY iron rule).
    Returns (written_ids, reinforced_ids). Does not overwrite an existing id
    (appends nothing; CAPTURE is additive).
    """
    today = today or date.today().isoformat()
    l0 = Path(company_dir) / "memory" / "L0-working"
    l0.mkdir(parents=True, exist_ok=True)
    written, reinforced = [], []
    id_paths = None  # lazy: only scanned when a reinforce entry appears
    for obs in observations[:MAX_OBSERVATIONS]:
        safe_sid = _safe_source_token(session_id)
        srcs = obs.get("source_lines") or []
        target = str(obs.get("reinforce") or "").strip()
        if target:
            if id_paths is None:
                id_paths = _memory_id_paths(company_dir)
            tpath = id_paths.get(target)
            if tpath is not None and _bump_reinforce(tpath, srcs, safe_sid, today):
                reinforced.append(target)
                continue
            # Unknown target (or bump failed): treat as a normal NEW
            # observation below — but only if it actually carries one.
            if not obs.get("id") or not obs.get("body"):
                continue
        if not srcs:
            continue  # no provenance -> never write (Gibby would reject)
        oid = _slug(obs["id"])
        path = l0 / f"{oid}.md"
        if path.exists():
            continue
        sources = "[" + ", ".join(
            f'"[{safe_sid}#{int(s)}]"' for s in srcs if str(s).lstrip("-").isdigit()
        ) + "]"
        if sources == "[]":
            continue
        body = str(obs["body"]).strip().replace("\n", " ")
        if SYSTEM_NOISE_RE.search(body):
            continue  # A2: operational noise, not a Chairman memory — don't write
        if META_NOISE_RE.search(body):
            continue  # B4: company work-state (phases/entropy/PRs) — not a
            #           Chairman fact; never write it as memory

        # Knowledge class {profile|projects|preferences}; safe default preferences
        # if the model omitted or mis-tagged it. The promoter routes on this to
        # L2-cold/<category>/ (see decay.py::L2_CATEGORIES).
        category = _norm_category(obs.get("category"))

        path.write_text(
            "---\n"
            f"id: {oid}\n"
            "tier: L0\n"
            "owner: Tony\n"
            f"category: {category}\n"
            f"sources: {sources}\n"
            f"created: {today}\n"
            f"last_reinforced: {today}\n"
            "reinforce_count: 1\n"
            "decay_score: 1.0\n"
            "status: active\n"
            "---\n"
            f"{body}\n",
            encoding="utf-8",
        )
        written.append(oid)
    return written, reinforced


# ----------------------------------------------------------------------------
# Per-session cooldown gate (survey F3: Stop fires on every reply-stop)
# ----------------------------------------------------------------------------

def cooldown_minutes(company_dir):
    """Effective CAPTURE_COOLDOWN_MINUTES: policy.md §7.7 overrides the
    built-in default. Never raises — any trouble means the default (the Stop
    hook must stay quiet and crash-proof)."""
    defaults = {"CAPTURE_COOLDOWN_MINUTES": DEFAULT_CAPTURE_COOLDOWN_MINUTES}
    if _resolve_config is None:
        return DEFAULT_CAPTURE_COOLDOWN_MINUTES
    try:
        values, _ = _resolve_config(
            defaults, str(Path(company_dir) / "org" / "policy.md"))
        return int(values["CAPTURE_COOLDOWN_MINUTES"])
    except Exception:
        return DEFAULT_CAPTURE_COOLDOWN_MINUTES


def _load_cooldown_map(company_dir):
    """Read the {safe-session-id: iso-timestamp} marker map. Missing/corrupt
    file -> {} — FAIL-OPEN: a broken marker must never suppress a capture
    (worst case is one extra capture, vs. silently losing memories)."""
    try:
        raw = (Path(company_dir) / "ops" / COOLDOWN_MARKER).read_text(
            encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def cooldown_active(company_dir, session_id, minutes=None, now=None):
    """True iff the last capture attempt for THIS session id is younger than
    the cooldown. Unknown session / unparsable or future timestamp -> False
    (fail-open, incl. clock skew: a far-future timestamp must not suppress
    captures indefinitely)."""
    if minutes is None:
        minutes = cooldown_minutes(company_dir)
    if minutes <= 0:
        return False
    ts = _load_cooldown_map(company_dir).get(_safe_source_token(session_id))
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(str(ts))
    except (ValueError, TypeError):
        return False  # corrupt entry -> fail-open
    age = (now or datetime.now()) - last
    return timedelta(0) <= age < timedelta(minutes=minutes)


def mark_capture(company_dir, session_id, minutes=None, now=None):
    """Record a capture ATTEMPT for this session — set BEFORE the model call,
    because the throttle protects the model spend (a failed call still cools
    down). Prunes entries older than max(24h, cooldown) so the map never grows
    unbounded; corrupt entries are dropped on rewrite. Never raises."""
    try:
        now = now or datetime.now()
        if minutes is None:
            minutes = cooldown_minutes(company_dir)
        ops = Path(company_dir) / "ops"
        ops.mkdir(parents=True, exist_ok=True)
        data = _load_cooldown_map(company_dir)
        data[_safe_source_token(session_id)] = now.isoformat(timespec="seconds")
        cutoff = now - max(timedelta(hours=24), timedelta(minutes=minutes))
        keep = {}
        for k, v in data.items():
            try:
                if datetime.fromisoformat(str(v)) >= cutoff:
                    keep[k] = v
            except (ValueError, TypeError):
                continue
        (ops / COOLDOWN_MARKER).write_text(
            json.dumps(keep, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        pass  # marker trouble must never break the hook


def _log_throttle(company_dir, session_id, minutes):
    """One-line skip log to ops/logs/capture.log (no silent failure — but also
    no daily-log noise; 19 fires/session would drown it). Never raises."""
    try:
        logdir = Path(company_dir) / "ops" / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        with open(logdir / "capture.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} "
                    f"CAPTURE throttled session={_safe_source_token(session_id)} "
                    f"cooldown={minutes}m\n")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _read_hook_stdin():
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        return json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}


def main(argv=None):
    # Second-layer recursion guard (the headless model call sets this).
    if os.environ.get(RECURSION_GUARD):
        return 0

    ap = argparse.ArgumentParser(description="CAPTURE stage: transcript -> L0 drafts.")
    ap.add_argument("--transcript", help="Path to session transcript .jsonl")
    ap.add_argument("--session", help="Session id (for sources)")
    ap.add_argument("--company", default=".company", help="Company dir (default: .company)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                    help="Extract + build prompt but do NOT call the model or write files")
    args = ap.parse_args(argv)

    # Only consult the hook payload on stdin in hook mode (no explicit
    # --transcript). In manual/test mode we never touch stdin — reading it there
    # can block forever on a non-tty pipe.
    hook = {} if args.transcript else _read_hook_stdin()
    # Official anti-recursion: Claude Code sets this when a Stop hook is active.
    if hook.get("stop_hook_active"):
        return 0

    transcript = args.transcript or hook.get("transcript_path")
    session = args.session or hook.get("session_id") or "unknown-session"
    company = args.company

    if not transcript or not Path(transcript).exists():
        return 0  # nothing to capture; never error
    if not Path(company).exists():
        return 0  # company not installed here; no-op

    # F3 cooldown gate — BEFORE any transcript read or model call. A throttled
    # fire is a one-line-logged no-op with exit 0 (the Stop hook must never
    # block a session). --dry-run bypasses enforcement (it is a diagnostic
    # tool and makes no model call anyway) but reports the state.
    minutes = cooldown_minutes(company)
    throttled = cooldown_active(company, session, minutes=minutes)
    if throttled and not args.dry_run:
        _log_throttle(company, session, minutes)
        return 0

    chairman = extract_chairman_lines(transcript)
    if not chairman:
        return 0

    existing = existing_memory_ids(company)
    recent = recent_l0_digest(company)
    prompt = build_capture_prompt(chairman, existing, recent_memories=recent)

    if args.dry_run:
        print(json.dumps({
            "session": session, "chairman_lines": len(chairman),
            "existing_ids": len(existing), "recent_memories": len(recent),
            "cooldown_minutes": minutes, "cooldown_active": throttled,
            "prompt_chars": len(prompt),
            "would_call_model": _which("claude"),
            "prompt": prompt,
        }, ensure_ascii=False))
        return 0

    # Mark the ATTEMPT before the call: the throttle protects the model spend,
    # so even an empty/failed extraction cools this session down.
    mark_capture(company, session, minutes=minutes)
    observations = run_capture_model(prompt, model=args.model)
    written, reinforced = write_l0(observations, session, company)

    if written or reinforced:
        log = Path(company) / "ops" / "logs" / f"daily-{date.today().isoformat()}.md"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n## CAPTURE ({session})\n")
            for oid in written:
                f.write(f"- {oid} (L0) — pending_verify\n")
            for oid in reinforced:
                f.write(f"- {oid} — reinforced (rc+1, no new file)\n")
    print(json.dumps({"session": session, "written": written,
                      "reinforced": reinforced}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
