#!/usr/bin/env python3
"""
employee — the data-driven `Employee` model (Phase 16). ONE class, eight
instances; employees differ by DATA (persona text, tools, duties, model, role),
never by code type. There is deliberately NO `class Bob` / `class Gibby`: a
per-employee subclass would BE the design break this phase exists to prevent.
Role-specific CODE (Gibby's red-team loop, Tony's decay math) stays in the task
scripts; this module is a DATA MODEL + helper library used BY those scripts, the
same way `frontmatter.py` and `tombstone.py` are shared seams — it does not
change how employees are dispatched or act.

Two things live here:

  1. The AUTHORITATIVE role topology (Layer B). The fixed tables — who the eight
     employees are, which duties each may own, the red/blue duty classes, and
     which deterministic daily-run step each owns — used to live in
     schedule_config.py. They move here as the single source of truth; BOTH
     schedule_config.py and schedule_validator.py import them from this module.
     Editing these is a code change, deliberately — not a config knob. (The
     config SCHEMA — which YAML keys are allowed / forbidden — stays in
     schedule_config.py, because that is about the config file, not about who an
     employee IS.)

  2. The `Employee` class. `Employee.load(name, company_dir)` reads an employee's
     desk (context.md frontmatter, via the shared frontmatter.py) and exposes
     identity, its least-privilege capability slice, execution knobs, and desk
     paths, plus the small methods that replace lookups previously scattered
     across scripts (`allows_duty`, `owns_step`, `should_run`, `log`,
     `capabilities`, `roster`).

Pure stdlib. NEVER raises to a caller: a missing desk, absent field, or malformed
frontmatter degrades to a sensible default, never an exception.
"""

import hashlib
import json
import math
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import frontmatter  # shared markdown-frontmatter parsing seam
except Exception:                                              # pragma: no cover
    frontmatter = None

try:
    # Phase 22: the ONE shared `.rag-venv/bin/python` resolver (rag_venv.py).
    from rag_venv import venv_python as _venv_python
except Exception:                                              # pragma: no cover
    def _venv_python(company):
        return Path(company) / ".rag-venv" / "bin" / "python"

try:
    # The SINGLE tombstone vocabulary (archived/absorbed/defunct). Reused so the
    # shared-memory re-validation (recall_shared) skips retired memories with the
    # exact same rule hook_memory_inject/entropy/decay use — never a private copy.
    from tombstone import is_tombstoned as _is_tombstoned
except Exception:                                              # pragma: no cover
    def _is_tombstoned(fm):
        return str(fm.get("status") or "").strip().lower() in (
            "archived", "absorbed", "defunct")

# ---------------------------------------------------------------- Phase 18 knobs
# Per-employee "experience recall" memory (Phase 18): capture -> index -> recall,
# FLAT and LIGHT. No per-employee L0/L1/L2 tiers, no decay/verify/entropy — the
# anti-entropy machinery stays on the SHARED company memory only. The RAG stack
# (rag_embed/rag_index/rag_query) is REUSED as-is, parameterized per employee; we
# never fork the embedding/query logic.
#
# The reused rag_index.py only indexes files whose `tier` is L1/L2. A per-employee
# memory is therefore stamped with ONE fixed tier (L2 = durable) purely so the
# unmodified indexer will pick it up. This is NOT a per-employee tier pipeline:
# there is exactly one constant value, nothing promotes/demotes/decays it.
_MEMORY_TIER = "L2"


def _env_num(name, default, cast):
    """Env-tunable number, falling back to `default` on absence OR garbage."""
    try:
        return cast(os.environ[name])
    except (KeyError, ValueError, TypeError):
        return default


# Recall shells rag_query.py with a HARD timeout (mirrors hook_memory_inject's
# ask-time budget discipline) so recall can never block a dispatch.
# Phase 24 Item 5: bumped 7s -> 15s — the rerank subprocess loads two ONNX models
# (~5.4s warm); a timeout still degrades cleanly to []/cosine order.
_RECALL_TIMEOUT = _env_num("SELF_COMPANY_RECALL_TIMEOUT", 15.0, float)

# Dispatch-injection budget cap (Phase 18b). recall_context() renders each recalled
# memory to at most this many chars so the "Relevant past experience:" block can
# never balloon a worker's prompt (budget-capped, mirroring the ask-time discipline).
_RECALL_SNIPPET_CHARS = _env_num("SELF_COMPANY_RECALL_SNIPPET_CHARS", 240, int)

# OVERALL dispatch-injection budget (Phase 18c). The own-store "Relevant past
# experience" block and the shared "Relevant company memory" block together render
# to at most this many chars, so injecting BOTH can never balloon a worker prompt.
# dispatch_context() renders the own block first, then hands the shared block only
# the REMAINING budget — the two blocks share one cap.
# Phase 29 Item 5 (C1): 900 -> 2700 (~3x). Against a 200k-token window this is
# trivially cheap (~0.15%); the old cap bought nothing but mid-thought truncation
# — Phase 24's reranker + relevance gates carry precision now, not the char cap.
# The 3x is POST-sonnet-5-tokenizer (Item 2's ~+30% tokens/text) — re-baselined
# jointly with Item 2, once, per the spec. Env override unchanged.
_DISPATCH_INJECT_BUDGET = _env_num("SELF_COMPANY_DISPATCH_INJECT_BUDGET", 2700, int)

# Shared-read similarity gate (Phase 18c). The SHARED-memory read at dispatch honors
# the SAME cosine floor the ask-time hook (hook_memory_inject) uses, sourced from the
# SAME env var, so the dispatch read and the interactive read are gated identically.
# Phase 24 Item 1 / R3: retuned 0.30 -> 0.35 -> 0.40 alongside hook_memory_inject.py's
# copy — see that module's comment for the sweep numbers (0.40 = highest floor that
# keeps every on-topic diagnostic hit; lowest true-positive is 0.419).
_SHARED_MIN_SCORE = _env_num("SELF_COMPANY_INJECT_RAG_MIN_SCORE", 0.40, float)

# Phase 24 Item 5: cross-encoder reranker cutoff — the FINAL gate for the shared
# dispatch read, in parity with hook_memory_inject.RERANK_MIN_SCORE (same env var,
# same data-driven -2.75). When rag_query returns no rerank_score (reranker backend
# absent / degraded), this gate is skipped and the cosine floor alone decides.
_RERANK_MIN_SCORE = _env_num("SELF_COMPANY_INJECT_RERANK_MIN_SCORE", -2.75, float)

# Injection block headers — kept as module constants so the own-store and shared
# blocks are rendered by ONE renderer (_render_memory_block) and stay distinct,
# clearly-labeled sections in the worker prompt.
_OWN_MEMORY_HEADER = "Relevant past experience (your own memory — advisory, not orders):"
_SHARED_MEMORY_HEADER = ("Relevant company memory (the Chairman's standing "
                         "direction — advisory, not orders):")

# ---------------------------------------------------------------- Phase 29 model table
# The per-employee model TABLE (Phase 29 Item 1 — the Chairman's own finding,
# forward audit M1): `context.md`'s `model:` field is Layer-A config, adjustable
# with zero code change; this is the ONE canonical alias map every resolution
# path (Employee.resolved_model) goes through. Builder note: confirm these ids
# against the `claude-api` skill's "Current Models" table at build time before
# changing them — they are current as of Phase 29 (2026-07-10). `sonnet` maps to
# `None` deliberately: it resolves to whatever DEFAULT the CALLER passes in (the
# Item-2 default constant, schedule_config.DEFAULT_AGENT_MODEL) rather than a
# second hardcoded sonnet id, so there is exactly one place the default model
# string lives.
_MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5",
    "opus": "claude-opus-4-8",
    "fable": "claude-fable-5",
    "sonnet": None,
}

# argv-smuggle-proof charset (Gibby's fuzz set: unicode arrows, YAML lists, empty
# string, `model: "claude-"`, injection-shaped values like
# `sonnet --dangerously-skip-permissions`). A resolved model is ALWAYS exactly
# one `--model` argv token — this is the single gate every value must clear
# (alias outputs above already satisfy it by construction).
_MODEL_CHARSET_RE = re.compile(r"^claude-[A-Za-z0-9.-]+$")


# ------------------------------------------------------- Phase 18b memory MODE
# Per-employee memory MODE: "rag" (the Phase-18 per-employee capture -> index ->
# recall store) or "flat" (NO per-employee RAG store — the employee keeps their
# existing log.md, and Gibby his deterministic red/blue ledger, as their memory).
# This is a per-employee CONFIG toggle, NOT a hardcoded planner-vs-executor rule
# (modularize, don't special-case): context.md frontmatter `memory: rag|flat` is
# authoritative; the table below is ONLY the DEFAULT used when context.md omits
# the field, so a minimal/fresh desk still behaves sensibly. The Chairman's split
# is analysts/planners recall semantically; executors stay flat.
_MEMORY_MODES = ("rag", "flat")
MEMORY_MODE_DEFAULTS = {
    "bob":    "flat",   # Blue Team executor  — keeps log.md
    "gibby":  "flat",   # Red Team executor   — keeps log.md + red/blue ledger
    "tom":    "flat",   # IT/Ops executor     — keeps log.md
    "tony":   "rag",    # Improvement analyst — semantic recall
    "mike":   "rag",    # R&D research        — semantic recall
    "elon":   "rag",    # CEO / planner       — semantic recall
    "phoebe": "rag",    # PM / planner        — semantic recall
    "july":   "rag",    # HR lead / analyst   — semantic recall
}
_DEFAULT_MEMORY_MODE = "flat"   # unknown name -> flat (conservative: no RAG store)


# ------------------------------------------------- Phase 18c shared-memory READ
# A DISPATCHED worker reads only its OWN per-employee store (recall/recall_context
# above); the SHARED company memory (about the Chairman) is injected at ASK time by
# the UserPromptSubmit hook (hook_memory_inject) but NOT at dispatch. So an
# autonomous/cron/trigger-dispatched planner does not semantically recall the
# Chairman's standing directives. This capability wires a SHARED-memory semantic
# read INTO dispatch for the employees who need that — data-driven exactly like
# _MEMORY_MODES: a per-name DEFAULT table (elon=on, everyone else=off) PLUS a
# context.md `shared_memory_read: on|off` override. Enabling another employee later
# is ONE table edit (or one context.md line), never a hardcoded `if id == "elon"`.
# It is orthogonal to memory MODE: shared-READ is about the SHARED corpus; rag/flat
# is about the employee's OWN store.
SHARED_MEMORY_READ_DEFAULTS = {
    "elon": True,   # CEO/planner — recalls the Chairman's standing direction at dispatch
}
_DEFAULT_SHARED_MEMORY_READ = False       # everyone else: off (own-store only)
_TRUE_TOKENS = frozenset(("on", "true", "yes", "1", "enabled"))
_FALSE_TOKENS = frozenset(("off", "false", "no", "0", "disabled"))


# ================================================================ Layer B tables
# The fixed role topology — the single source of truth. Config may enable/disable
# an employee and pick which of THEIR OWN duties run; it can never grant a duty
# outside these sets or reassign a role (modularize, don't special-case).
#
# Phase 32 (hire-as-data): renamed EMPLOYEES -> CORE_EMPLOYEES — these are the
# eight CODE-KNOWN, charter-pinned employees. `EMPLOYEES` stays as a plain alias
# (unchanged value, unchanged identity) so every pre-Phase-32 import keeps
# working byte-for-byte. New code that means "every employee this COMPANY has,
# including hired desks" calls `discover(company_dir)` instead — the ONE new
# seam, not a second hardcoded list.
CORE_EMPLOYEES = ("tony", "gibby", "bob", "mike", "elon", "phoebe", "tom", "july")
EMPLOYEES = CORE_EMPLOYEES

# Organizational tier. Stated in prose in each persona.md; pinned here as
# structured Layer-B data (config may not set `tier:` — it is a forbidden key).
TIERS = {
    "elon":   "manager",   # CEO, reports to Chairman
    "phoebe": "manager",   # PM, above July
    "july":   "lead",      # HR team lead, half a tier above the workers
    "tony":   "worker",
    "gibby":  "worker",
    "bob":    "worker",
    "mike":   "worker",
    "tom":    "worker",
}

ALLOWED_DUTIES = {
    "tony":   {"reinforce", "decay", "entropy", "rag_index", "propose", "agent"},
    "gibby":  {"verify", "attack"},          # Red Team — attack, never build
    "bob":    {"build"},                     # Blue Team — build, never attack/verify
    "mike":   {"research"},                  # external weekly survey
    "elon":   {"survey"},                    # elon_survey -> daily todo
    "tom":    {"backup", "report", "schedule"},
    "phoebe": set(),                         # gateway — no scheduled deterministic duty
    "july":   {"july_audit"},                # Phase 17: capability-steward audit
}

# Red/blue role classes (used by the validator's R1/R2). An employee must never
# hold duties from more than one competing class; the attack surface must stay
# covered whenever the build surface is active.
ATTACK_DUTIES = {"attack"}
BUILD_DUTIES = {"build"}
VERIFY_DUTIES = {"verify"}

# Which daily-run.sh deterministic step each employee owns (for --should-run
# gating and the roster). Steps NOT here (e.g. red/blue attack/build) are not part
# of the scheduled batch — they are dispatched competition work.
STEP_OWNER = {
    "backup":    "tom",
    "reinforce": "tony",
    "decay":     "tony",
    "verify":    "gibby",
    "entropy":   "tony",
    "rag_index": "tony",   # Phase 13 A.1: daily incremental LanceDB index refresh
    "survey":    "elon",
    "report":    "tom",
    "agent":     "tony",
    "july_audit": "july",  # Phase 17: July's capability-steward audit (weekly, low-churn)
}


# ============================================================ Phase 32 discovery
# hire-as-data (Item 1): a company's real roster is CORE_EMPLOYEES plus every
# HIRED desk under org/employees/<id>/ that looks like a real desk. The id
# charset is enforced HERE (defense in depth alongside hire.sh's own check,
# Item 2) — a hand-crafted directory with a bad-charset name is simply never
# yielded by discover(), so it is never dispatched and never gains a Layer-B
# duty; schedule_validator's R7 separately FLAGS such a directory (rather than
# silently leaving it inert forever) by scanning org/employees/ directly.
_DESK_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,23}$")


def _desk_regular_file(desk_dir, filename):
    """True iff `desk_dir/filename` is a REAL, in-tree regular file — present, a
    regular file, NOT a symlink, and physically resolving INSIDE `desk_dir`.

    Phase 32 Bug 4 (least-privilege): `Path.is_file()` FOLLOWS symlinks, so a
    desk whose `persona.md` is symlinked to `/tmp/outside` used to be discovered
    and read verbatim into the dispatch prompt — an out-of-tree file invisible
    in a `git diff` of the store. A desk file must live in the desk. We reject
    the symlinked FILE and (defense in depth) any resolution that escapes the
    desk dir (e.g. a symlinked parent). Never raises."""
    try:
        p = desk_dir / filename
        if p.is_symlink():
            return False
        if not p.is_file():
            return False
        rp = Path(os.path.realpath(str(p)))
        desk_rp = Path(os.path.realpath(str(desk_dir)))
        return desk_rp == rp or desk_rp in rp.parents
    except OSError:
        return False


def discover(company_dir):
    """Return CORE_EMPLOYEES plus every valid HIRED desk for this company,
    sorted, appended after the core eight. A "valid" hired desk: its directory
    name matches `_DESK_ID_RE`, is not a core id (core always wins — a
    directory literally named e.g. "elon" is simply ignored here, never
    shadowing or extending the real core desk), is a real (non-symlink)
    directory, and has BOTH `persona.md` AND `context.md` present as REAL,
    in-tree regular files — not symlinks pointing outside the desk (Phase 32
    Bug 4: `is_file()` follows symlinks, which would let an out-of-tree file be
    read verbatim into the dispatch prompt; see `_desk_regular_file`). A
    half-scaffolded or symlink-smuggling desk is not a real employee.

    Zero-desk behavior returns EXACTLY `CORE_EMPLOYEES` (same tuple identity
    semantics, same order) — every consumer of this seam (schedule_config's
    TOP_KEYS/effective/roster_md, schedule_validator, supervisor) is therefore
    byte-identical to pre-Phase-32 behavior when a company has hired nobody.

    Never raises: any filesystem trouble (missing dir, permission error,
    non-directory company_dir, even `company_dir=None`) degrades to
    `CORE_EMPLOYEES` alone."""
    try:
        base = Path(company_dir) / "org" / "employees"
        if not base.is_dir():
            return CORE_EMPLOYEES
        found = []
        for d in sorted(base.iterdir()):
            name = d.name
            if name in CORE_EMPLOYEES:
                continue                       # core always wins, never shadowed
            if not _DESK_ID_RE.match(name):
                continue                       # bad charset -> ignored (R7 flags it)
            try:
                if d.is_symlink() or not d.is_dir():
                    continue                   # symlinked/non-dir desk -> ignored
                if (_desk_regular_file(d, "persona.md")
                        and _desk_regular_file(d, "context.md")):
                    found.append(name)
            except OSError:
                continue
        return CORE_EMPLOYEES + tuple(sorted(found))
    except Exception:
        return CORE_EMPLOYEES


# ================================================================ fm helpers
def _strip_comment(s):
    """Drop a trailing ` # inline comment` from a scalar value. Requires
    whitespace before the `#` so a legitimate `#` inside a token (or a URL's
    `://`) is preserved, mirroring schedule_config's fallback parser."""
    out = []
    prev_ws = False
    for ch in s:
        if ch == "#" and (prev_ws or not out):
            break
        out.append(ch)
        prev_ws = ch in " \t"
    return "".join(out).strip()


def _parse_fm(raw_lines):
    """Parse the RAW frontmatter lines (as returned by frontmatter.split) into a
    dict of scalars, block sequences (`key:` then `- item` children) and block
    scalars (`key: |`). Only the exact shapes context.md uses; never raises.

    Scalars keep their string value with any inline `# comment` stripped. Block
    sequences become a list of item strings (item comments stripped). Block
    scalars (`|`/`>`) become the joined child text. This is a small, tolerant
    reader — a superset of what frontmatter.parse (flat scalars only) gives, kept
    local because the capability lists are YAML sequences frontmatter.py leaves to
    each caller by design (it parses/splits only)."""
    data = {}
    i, n = 0, len(raw_lines)
    while i < n:
        raw = raw_lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1] in (" ", "\t"):
            continue                       # stray child without a parent key
        if ":" not in raw:
            continue
        key, _, val = raw.partition(":")
        key = key.strip()
        val = val.strip()
        if val == "":
            # Opens a block: collect indented children (sequence items and/or a
            # nested map's keys). Blank lines are tolerated; the next unindented
            # line ends the block.
            seq, nested = [], {}
            while i < n and (raw_lines[i][:1] in (" ", "\t") or raw_lines[i].strip() == ""):
                child = raw_lines[i]
                i += 1
                cs = child.strip()
                if not cs or cs.startswith("#"):
                    continue
                if cs.startswith("- "):
                    seq.append(_strip_comment(cs[2:]))
                elif cs == "-":
                    seq.append("")
                elif ":" in cs:
                    ck, _, cv = cs.partition(":")
                    nested[ck.strip()] = _strip_comment(cv)
            data[key] = seq if seq else (nested if nested else [])
        elif val in ("|", ">", "|-", ">-", "|+", ">+"):
            # Block scalar: gather indented body, join stripped lines.
            buf = []
            while i < n and (raw_lines[i][:1] in (" ", "\t") or raw_lines[i].strip() == ""):
                buf.append(raw_lines[i].strip())
                i += 1
            data[key] = "\n".join(l for l in buf).strip()
        else:
            data[key] = _parse_value(val)
    return data


def _parse_value(val):
    """Parse a scalar RHS: an inline flow list `[a, b]` (incl. empty `[]`) becomes
    a list; anything else is the comment-stripped scalar string. This lets a
    capability field be written compactly (`mcp: []`, `skills: [deep-research]`) or
    as a block sequence — both yield a list. Only the flat shapes context.md uses;
    never raises."""
    s = _strip_comment(val)
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_strip_comment(x).strip().strip('"').strip("'")
                for x in inner.split(",") if x.strip()]
    return s


def _as_list(v):
    """Normalize a frontmatter value to a list of strings. A scalar becomes a
    one-item list; None/empty -> []."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    s = str(v).strip()
    return [s] if s else []


# ================================================================ memory helpers
_WS_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _normalize_memory_text(text):
    """Strip + collapse whitespace (mirrors rag_index.normalize_text) so the
    content hash — and therefore the memory id — is stable across incidental
    whitespace differences. This is what makes a re-record idempotent."""
    return _WS_RE.sub(" ", str(text).strip())


def _content_hash(normalized):
    """Short stable content fingerprint (first 12 hex of sha256)."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _slugify(normalized, max_words=6):
    """A short filesystem-safe slug from the first few words of the memory."""
    words = _SLUG_RE.sub("-", normalized.lower()).strip("-").split("-")
    words = [w for w in words if w][:max_words]
    slug = "-".join(words)
    return slug or "memory"


def _render_tags(tags):
    """Render a tags list as an inline flow sequence (`[a, b]`) the loader's
    _parse_value reads back. None/empty -> `[]`."""
    items = _as_list(tags)
    return "[" + ", ".join(items) + "]"


# ================================================================ the model
class Employee:
    """One data-driven employee. Construct via `Employee.load(name, company_dir)`;
    all fields are read from the employee's desk + the fixed Layer-B tables. Never
    raises — an absent desk or field degrades to a default."""

    # The FUNCTIONAL capability dimensions July stewards — "what each employee may
    # use." Phase 16 reserved this seam with `tools`; Phase 17 extends it with
    # `mcp` / `skills` / `plugins` (the toolchain that churns as the project
    # evolves). Extending it is adding a name here + the field on load, nothing
    # structural — exactly what this seam was for. (The data-access slice —
    # `reads` / `writes` / `handoff_to` — is a SEPARATE concern kept as its own
    # attributes below; it is routing/permission, not a "function" July tunes.)
    CAPABILITY_FIELDS = ("tools", "mcp", "skills", "plugins")

    def __init__(self, name, company_dir, fm=None):
        self.name = str(name).strip().lower()
        self.company_dir = Path(company_dir)
        fm = fm or {}
        self._fm = fm

        # ---- identity -------------------------------------------------------
        self.display_name = str(fm.get("name") or self.name.capitalize())
        self.role = str(fm.get("role") or "")
        self.manager = str(fm.get("manager") or "")
        self.people_lead = self._clean_optional(fm.get("people_lead"))
        # ---- tier: Layer B for the core 8 (TIERS table, never config); a
        # discovered (hired) employee declares its own tier in ITS OWN
        # context.md `tier:` field (Phase 32 R7(a)) — worker/manager only,
        # defaulting to "worker" on anything absent/invalid. `_tier_raw` keeps
        # the UNvalidated raw string so schedule_validator's R7 can tell "field
        # missing" from "field present but garbage" for its own error message.
        self._tier_raw = str(fm.get("tier") or "").strip().lower()
        if self.name in TIERS:
            self.tier = TIERS[self.name]
        else:
            self.tier = self._tier_raw if self._tier_raw in ("worker", "manager") else "worker"

        # ---- functional capabilities (the least-privilege slice July stewards)
        self.tools = _as_list(fm.get("tools"))
        self.mcp = _as_list(fm.get("mcp"))
        self.skills = _as_list(fm.get("skills"))
        self.plugins = _as_list(fm.get("plugins"))

        # ---- data-access / routing slice (separate concern, not a capability)
        self.reads = _as_list(fm.get("reads"))
        self.writes = _as_list(fm.get("writes"))
        self.handoff_to = _as_list(fm.get("handoff_to"))

        # ---- execution ------------------------------------------------------
        self.model = str(fm.get("model") or "").strip()
        self.token_budget = self._clean_optional(fm.get("token_budget"))
        self._eff_self = None   # lazily filled effective-config slice

        # ---- memory MODE (Phase 18b) ---------------------------------------
        # context.md `memory:` frontmatter is authoritative; fall back to the
        # per-name default table, then to the conservative flat default. An
        # unrecognized value (typo) also degrades to the default — never raises.
        mode = str(fm.get("memory") or "").strip().lower()
        if mode not in _MEMORY_MODES:
            mode = MEMORY_MODE_DEFAULTS.get(self.name, _DEFAULT_MEMORY_MODE)
        self._memory_mode = mode

        # ---- shared-memory READ capability (Phase 18c) ---------------------
        # context.md `shared_memory_read: on|off` is authoritative; an unset /
        # unrecognized value falls back to the per-name default table (elon on,
        # all others off), then the conservative off. Never raises.
        flag = str(fm.get("shared_memory_read") or "").strip().lower()
        if flag in _TRUE_TOKENS:
            self._shared_memory_read = True
        elif flag in _FALSE_TOKENS:
            self._shared_memory_read = False
        else:
            self._shared_memory_read = SHARED_MEMORY_READ_DEFAULTS.get(
                self.name, _DEFAULT_SHARED_MEMORY_READ)

    # ------------------------------------------------------------ construction
    @staticmethod
    def _clean_optional(v):
        """A frontmatter placeholder like `null`, `~`, `none` or empty -> None."""
        if v is None:
            return None
        s = str(v).strip()
        if s == "" or s.lower() in ("null", "none", "~"):
            return None
        return s

    @classmethod
    def load(cls, name, company_dir):
        """Build an Employee from `<company_dir>/org/employees/<name>/context.md`.
        A missing desk or unreadable/absent frontmatter yields an employee with
        empty capability slices and default identity — never an exception."""
        name = str(name).strip().lower()
        desk = Path(company_dir) / "org" / "employees" / name
        fm = {}
        ctx = desk / "context.md"
        try:
            if ctx.exists() and frontmatter is not None:
                text = ctx.read_text(encoding="utf-8")
                raw_lines, _ = frontmatter.split(text)
                fm = _parse_fm(raw_lines)
        except Exception:
            fm = {}
        return cls(name, company_dir, fm)

    @classmethod
    def roster(cls):
        """The eight employee names, in canonical order."""
        return list(EMPLOYEES)

    # ------------------------------------------------------------------- paths
    @property
    def desk_dir(self):
        return self.company_dir / "org" / "employees" / self.name

    @property
    def persona_path(self):
        return self.desk_dir / "persona.md"

    @property
    def context_path(self):
        return self.desk_dir / "context.md"

    @property
    def log_path(self):
        return self.desk_dir / "log.md"

    @property
    def scratchpad_path(self):
        return self.desk_dir / "scratchpad.md"

    @property
    def memory_dir(self):
        """This employee's OWN per-employee memory store (Phase 16 reserved the
        seam; Phase 18 builds it). `remember()` materializes it on first write;
        `recall()` reads its index under `memory_dir/index`. Physically isolated
        from every other employee and from the shared company memory."""
        return self.desk_dir / "memory"

    @property
    def declared_tier(self):
        """The RAW `tier:` frontmatter value on THIS desk's context.md
        (lowercased, stripped), or "" if absent — Phase 32. Used by
        schedule_validator's R7 to distinguish "no tier declared" from "a tier
        was declared but it's not worker/manager" for its error message. Core
        employees' `self.tier` is Layer-B (the TIERS table) and never reads
        this field; `declared_tier` is meaningful only for a discovered
        (hired) employee."""
        return self._tier_raw

    # ------------------------------------------------------------ capabilities
    def capabilities(self):
        """The employee's functional capability profile as a dict — the four
        dimensions July stewards: `tools`, `mcp`, `skills`, `plugins`. This is the
        single source of "what this employee may use," diffed by july_audit.py
        against the real environment. (Data access — reads/writes/handoff_to — is a
        separate slice, reachable via the like-named attributes.)"""
        return {field: list(getattr(self, field)) for field in self.CAPABILITY_FIELDS}

    # -------------------------------------------------------------------- model
    def resolved_model(self, default):
        """Resolve this employee's `context.md` `model:` field to a REAL model id
        for a single `--model` argv slot (Phase 29 Item 1 — the Chairman's
        adjustable-with-default model table). ONE resolution function; every
        dispatch path (supervisor.py's real_command) routes through this.

        Chairman's contract (2026-07-10, verbatim intent):
          * unset / blank / missing -> `default`, SILENTLY (no warning) — the
            tested happy path: "if I'm not saying anything it runs with default."
          * a recognized alias (`haiku`/`opus`/`fable`/`sonnet`, case-insensitive)
            -> the mapped real id (`sonnet` -> `default` itself — see
            _MODEL_ALIASES for why there is deliberately no second sonnet id).
          * any `claude-*` id -> passed through verbatim (builder-confirmed
            against the claude-api skill at edit time; never guessed here).
          * anything else (blank arrows/prose like "haiku → sonnet", a YAML list,
            an injection-shaped string, a bare "claude-" fragment, ...) -> degrade
            to `default` WITH a warning naming this employee and the bad value.
            Never raises, never blocks a dispatch, never returns anything but
            `default` on the invalid path — degrade-and-warn, not degrade-and-guess.

        Returns (model_id, warning_or_None). `model_id` is ALWAYS either `default`
        (trusted verbatim — the caller's responsibility to pass a valid id) or a
        string matching `^claude-[A-Za-z0-9.-]+$` — i.e. always exactly ONE
        argv-safe token. This is the argv-smuggle-proof guarantee: no frontmatter
        value, however crafted (embedded spaces, flags, shell metacharacters,
        newlines), can ever expand into more than one `--model` token — anything
        that doesn't fit the charset is rejected wholesale, not sanitized in
        place."""
        raw = str(self.model or "").strip()
        if not raw:
            return default, None
        key = raw.lower()
        if key in _MODEL_ALIASES:
            resolved = _MODEL_ALIASES[key]
            return (default if resolved is None else resolved), None
        if _MODEL_CHARSET_RE.match(raw):
            return raw, None
        return default, (
            f"employee '{self.name}': model '{raw}' is not a recognized alias "
            f"(haiku/sonnet/opus/fable) or a valid claude-* id — degrading to "
            f"default '{default}'"
        )

    # ----------------------------------------------------------------- duties
    def allows_duty(self, duty):
        """True iff `duty` is within this employee's fixed role set (Layer B)."""
        return duty in ALLOWED_DUTIES.get(self.name, set())

    def owns_step(self, step):
        """True iff this employee is the owner of deterministic daily-run `step`."""
        return STEP_OWNER.get(step) == self.name

    @property
    def duties(self):
        """The employee's effective scheduled duties (from org/schedule.yaml,
        defaulting to the role's full own set). Read-only view."""
        return list(self._effective_self().get("duties", []))

    @property
    def cadence(self):
        """The employee's effective sub-cadence (default `every-run`)."""
        return self._effective_self().get("cadence", "every-run")

    @property
    def enabled(self):
        """Whether the employee is enabled in the effective config (default True)."""
        return bool(self._effective_self().get("enabled", True))

    # ------------------------------------------------------------ memory mode
    @property
    def memory_mode(self):
        """This employee's memory MODE (Phase 18b): `"rag"` (per-employee
        capture -> index -> recall store) or `"flat"` (NO per-employee RAG store;
        the employee keeps their log.md, and Gibby his red/blue ledger). Sourced
        from context.md `memory:` frontmatter, defaulting per
        MEMORY_MODE_DEFAULTS. This is CONFIG, not Layer B — a company may flip an
        employee's mode by editing their context.md."""
        return self._memory_mode

    @property
    def rag_memory_enabled(self):
        """True iff this employee uses the Phase-18 per-employee RAG memory
        (mode == "rag"). Flat employees get NO index refresh, NO recall, and NO
        dispatch injection — remember() no-ops and recall() returns []."""
        return self._memory_mode == "rag"

    @property
    def shared_memory_read(self):
        """True iff this employee reads the SHARED company memory (about the
        Chairman) at DISPATCH (Phase 18c) — so an autonomous/cron/trigger-dispatched
        worker semantically recalls the Chairman's standing directives, not only its
        own store. Sourced from context.md `shared_memory_read: on|off`, defaulting
        per SHARED_MEMORY_READ_DEFAULTS (elon on, all others off). CONFIG + a
        data-driven table, NOT a hardcoded name — orthogonal to memory MODE."""
        return self._shared_memory_read

    @property
    def shared_memory_index_dir(self):
        """The SHARED company-memory LanceDB index (`<company>/memory/index`) — the
        Chairman corpus the ask-time hook also queries. Distinct from this
        employee's OWN per-employee `memory_index_dir`."""
        return self.company_dir / "memory" / "index"

    def _effective_self(self):
        """This employee's slice of schedule_config.effective(). Cached. Imported
        lazily so the topology tables can live here without a circular import at
        module load — employee.py never imports schedule_config at top level."""
        if self._eff_self is None:
            try:
                import schedule_config as sc
                self._eff_self = sc.effective(self.company_dir)["employees"].get(self.name, {})
            except Exception:
                self._eff_self = {}
        return self._eff_self

    def should_run(self, step, hour, dow):
        """Should deterministic `step` run on THIS tick, for this employee? Reuses
        schedule_config's config reader + cadence matcher so the verdict is
        byte-identical to schedule_config.should_run. Fail-OPEN: any doubt -> True,
        so a bad config or missing owner never silently suppresses maintenance.

        A step this employee does not own returns True (it does not suppress
        someone else's step); the deterministic gate applies only to the owner."""
        owner = STEP_OWNER.get(step)
        if owner is None:
            return True
        if owner != self.name:
            return True
        try:
            import schedule_config as sc
            e = sc.effective(self.company_dir)["employees"].get(self.name)
            if not e:
                return True
            if not e["enabled"]:
                return False
            if step not in e["duties"]:
                return False
            return sc._cadence_matches(e["cadence"], hour, dow)
        except Exception:
            return True

    # -------------------------------------------------------------------- log
    def log(self, entry):
        """Append `entry` (one line) to the employee's log.md, creating the desk
        directory if needed. Returns True on success, False on any error — never
        raises."""
        try:
            self.desk_dir.mkdir(parents=True, exist_ok=True)
            line = entry if entry.endswith("\n") else entry + "\n"
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line)
            return True
        except Exception:
            return False

    # ----------------------------------------------------------------- memory
    # Phase 18 — the per-employee "experience recall" store. capture (remember)
    # -> index (daily-run, per employee) -> recall. FLAT and isolated: writes go
    # ONLY to this employee's own memory_dir; recall reads ONLY this employee's
    # own index. Both are pure-degrade: remember is stdlib and never raises;
    # recall shells the RAG stack and returns [] on any absence/error/timeout.
    def remember(self, text, *, tags=None, source=None):
        """Record ONE structured memory to this employee's own memory store and
        return the file Path (or None on any failure — never raises).

        The file `<desk>/memory/<slug>-<hash>.md` carries frontmatter
        `id / owner=<self.name> / tier / created / tags / source` + the body.
        The id embeds a content hash, so re-recording identical text resolves to
        the SAME file and is a no-op (idempotent — no churn, `created` stable).
        A trivial/empty memory is skipped (returns None). Pure stdlib; the memory
        dir materializes on first write.

        Phase 18b — a FLAT employee (memory_mode != "rag") has NO per-employee RAG
        store: remember() is a no-op returning None, so no file is ever written and
        the daily index never sees them. They keep log.md / the red-blue ledger."""
        try:
            if not self.rag_memory_enabled:         # flat employee: no RAG store
                return None
            # Phase 24 MUST-FIX 2: reject non-STRING input rather than writing
            # `str(text)` as the body. `remember(None)` / `remember(12345)` /
            # `remember(['a','b'])` used to silently write a memory whose body
            # was "None" / "12345" / "['a', 'b']" — junk that later surfaced as
            # a "- None" bullet in a recall block. "never raises" held but
            # "well-formed" did not. A memory body must be real text; anything
            # else is skipped (return None), never written, never raised.
            if not isinstance(text, str):
                return None
            normalized = _normalize_memory_text(text)
            if not normalized:                       # empty / whitespace-only -> nothing to record
                return None
            mem_id = f"{_slugify(normalized)}-{_content_hash(normalized)}"
            path = self.memory_dir / f"{mem_id}.md"
            if path.exists():                        # dedup-by-content -> idempotent
                return path
            body = str(text).strip()
            fm = [
                "---",
                f"id: {mem_id}",
                f"owner: {self.name}",
                f"tier: {_MEMORY_TIER}",
                f"created: {date.today().isoformat()}",
                f"tags: {_render_tags(tags)}",
            ]
            src = self._clean_optional(source)
            if src is not None:
                fm.append(f"source: {src}")
            fm.append("---")
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            content = "\n".join(fm) + "\n" + body + "\n"
            # Phase 25 Item 2: route through the shared atomic-write helper
            # (write-temp-same-dir + os.replace) so a killed/ENOSPC write can
            # never leave a truncated per-employee memory file. `frontmatter`
            # is a best-effort import (None if somehow missing) — degrade to
            # the plain write in that case rather than ever raising.
            if frontmatter is not None:
                frontmatter._atomic_write(path, content, encoding="utf-8")
            else:                                          # pragma: no cover
                path.write_text(content, encoding="utf-8")
            return path
        except Exception:
            return None

    @property
    def memory_index_dir(self):
        """This employee's OWN LanceDB index dir (physically isolated, per the
        Chairman's choice — NOT a shared owner-filtered index)."""
        return self.memory_dir / "index"

    def recall(self, query, top_k=3):
        """Return this employee's OWN past memories most relevant to `query`.

        Shells `rag_query.py --index-dir <desk>/memory/index` (mirroring
        hook_memory_inject's subprocess+timeout+degrade discipline), then
        RE-VALIDATES every hit against the live memory files: a hit is kept only
        if its path physically lives under THIS employee's memory_dir (the
        isolation backstop — a query for bob can never surface gibby's memory)
        and the file still exists. Each kept memory is returned as a dict:
        `{id, owner, tags, source, path, score, text}` (text = live body).

        Graceful degrade — returns [] (never raises, never blocks) for ANY of:
        empty query, no `.rag-venv`, absent/empty index, missing rag_query.py,
        subprocess timeout, nonzero exit, non-JSON output, or zero usable hits.

        Phase 18b — a FLAT employee (memory_mode != "rag") has NO semantic recall:
        recall() short-circuits to [] before any venv/index work."""
        try:
            if not self.rag_memory_enabled:         # flat employee: no recall
                return []
            q = str(query or "").strip()
            if not q:
                return []
            try:
                k = max(1, int(top_k))
            except (TypeError, ValueError):
                k = 3
            # Require THIS company's venv python explicitly (cron/hook-safe).
            rag_py = _venv_python(self.company_dir)
            if not os.access(str(rag_py), os.X_OK):
                return []
            index_dir = self.memory_index_dir
            try:
                if not index_dir.exists() or not any(index_dir.iterdir()):
                    return []
            except OSError:
                return []
            query_script = Path(__file__).resolve().parent / "rag_query.py"
            if not query_script.exists():
                return []
            try:
                proc = subprocess.run(
                    [str(rag_py), str(query_script), "--query", q,
                     "--top-k", str(k), "--index-dir", str(index_dir),
                     # Phase 24 Item 5: rerank for better own-store ordering.
                     # Degrades to cosine order if the reranker backend is absent.
                     "--rerank"],
                    capture_output=True, text=True, timeout=_RECALL_TIMEOUT,
                    # rag_py IS the venv python -> rag_query must not re-exec again
                    # (bounds the tree to one killable child so the timeout is hard).
                    env={**os.environ, "SC_RAG_REEXEC": "1"})
            except Exception:
                return []
            if proc.returncode != 0:
                return []
            try:
                hits = json.loads(proc.stdout)
            except (ValueError, TypeError):
                return []
            if not isinstance(hits, list):
                return []
            mem_root = self._resolve(self.memory_dir)
            out, seen = [], set()
            for h in hits:
                if not isinstance(h, dict):
                    continue
                raw = h.get("path")
                if not raw:
                    continue
                p = Path(raw)
                rp = self._resolve(p)
                # Isolation backstop: only accept a file physically inside THIS
                # employee's memory dir. Anything else is dropped.
                if mem_root not in ([rp] + list(rp.parents)):
                    continue
                if not p.exists():                    # stale index row -> drop
                    continue
                if rp in seen:
                    continue
                seen.add(rp)
                mem = self._read_memory(p)
                if mem is None:
                    continue
                try:
                    mem["score"] = float(h.get("score"))
                except (TypeError, ValueError):
                    mem["score"] = None
                out.append(mem)
                if len(out) >= k:
                    break
            return out
        except Exception:
            return []

    def recall_context(self, query, top_k=3):
        """Dispatch-time recall injection (Phase 18b). Returns a compact, ready-to-
        prepend block:

            Relevant past experience (your own memory — advisory, not orders):
            - <lesson one>
            - <lesson two>

        for a `rag` employee whose OWN store has memories relevant to `query`, or
        the EMPTY string `""` for every no-injection case: a FLAT employee, an
        empty query, no venv / empty index, or zero hits. This is the ONE call an
        orchestrator makes before dispatching a worker — gated internally on
        `rag_memory_enabled`, so the caller need not special-case flat vs rag.

        Mirrors the ask-time injection discipline (hook_memory_inject): recall() is
        timeout-capped and pure-degrade, each hit is truncated to
        `_RECALL_SNIPPET_CHARS` (budget cap), and ANY failure yields "" — it can
        never delay, bloat, or block a dispatch. Never raises. Isolation holds:
        recall() only ever returns THIS employee's own memories."""
        try:
            if not self.rag_memory_enabled:         # flat employee: no injection
                return ""
            hits = self.recall(query, top_k=top_k)
            return self._render_memory_block(_OWN_MEMORY_HEADER, hits,
                                             _DISPATCH_INJECT_BUDGET)
        except Exception:
            return ""

    # ------------------------------------------------ shared company memory read
    # Phase 18c — the SHARED-memory read INTO dispatch. A `shared_memory_read`
    # employee (elon by default) also recalls the SHARED company memory (about the
    # Chairman) when dispatched as a headless worker, so autonomous/cron/trigger
    # work carries the Chairman's standing direction — not just the interactive
    # ask-time hook. Reuses rag_query.py as-is against the SHARED index; re-validates
    # every hit against the LIVE shared memory files (skips tombstoned/deleted,
    # exactly like hook_memory_inject); same similarity gate; pure-degrade.
    def recall_shared(self, query, top_k=3):
        """Return the SHARED company memories most relevant to `query`.

        Gated on `shared_memory_read` (elon-only by default). Shells rag_query.py
        against the SHARED index (`<company>/memory/index`), applies the SAME cosine
        floor the ask-time hook uses (`_SHARED_MIN_SCORE`), and RE-VALIDATES every
        hit against the live shared memory files — dropping any that no longer exist,
        are tombstoned (archived/absorbed/defunct), or have an empty body — exactly
        as hook_memory_inject does. Over-fetches (2x) so post-filter survivors still
        fill `top_k`. Each kept memory: `{id, owner, tags, source, path, score,
        text}` (text = live body). Returns [] (never raises, never blocks) for:
        flag off, empty query, no venv, absent/empty index, missing rag_query.py,
        timeout, nonzero exit, non-JSON output, or zero surviving hits."""
        try:
            if not self.shared_memory_read:          # capability off: no shared read
                return []
            q = str(query or "").strip()
            if not q:
                return []
            try:
                k = max(1, int(top_k))
            except (TypeError, ValueError):
                k = 3
            rag_py = _venv_python(self.company_dir)
            if not os.access(str(rag_py), os.X_OK):
                return []
            index_dir = self.shared_memory_index_dir
            try:
                if not index_dir.exists() or not any(index_dir.iterdir()):
                    return []
            except OSError:
                return []
            query_script = Path(__file__).resolve().parent / "rag_query.py"
            if not query_script.exists():
                return []
            try:
                proc = subprocess.run(
                    [str(rag_py), str(query_script), "--query", q,
                     "--top-k", str(k * 2), "--index-dir", str(index_dir),
                     # Phase 24 Item 5: cross-encoder rerank (same as the ask-time
                     # hook). Degrades to cosine order if the backend is absent.
                     "--rerank"],
                    capture_output=True, text=True, timeout=_RECALL_TIMEOUT,
                    env={**os.environ, "SC_RAG_REEXEC": "1"})
            except Exception:
                return []
            if proc.returncode != 0:
                return []
            try:
                hits = json.loads(proc.stdout)
            except (ValueError, TypeError):
                return []
            if not isinstance(hits, list):
                return []
            mem_root = self._resolve(self.company_dir / "memory")
            out, seen = [], set()
            for h in hits:
                if not isinstance(h, dict):
                    continue
                # Same relevance gate as the ask-time hook; a non-finite score
                # (NaN slips past `<`) is treated as below-floor so the gate holds.
                try:
                    score = float(h.get("score"))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(score) or score < _SHARED_MIN_SCORE:  # cosine PRE-FILTER
                    continue
                # Phase 24 Item 5: reranker is the FINAL gate when present (parity
                # with hook_memory_inject). Absent rerank_score (backend degraded)
                # -> cosine floor alone decides, byte-identical to pre-Item-5.
                rr = h.get("rerank_score")
                if rr is not None:
                    try:
                        rrf = float(rr)
                    except (TypeError, ValueError):
                        rrf = None
                    if rrf is None or not math.isfinite(rrf) or rrf < _RERANK_MIN_SCORE:
                        continue
                raw = h.get("path")
                if not raw:
                    continue
                p = Path(raw)
                rp = self._resolve(p)
                # Scope backstop: only accept a file physically inside the SHARED
                # company memory dir.
                if mem_root not in ([rp] + list(rp.parents)):
                    continue
                if rp in seen:
                    continue
                seen.add(rp)
                mem = self._read_shared_memory(p)     # None if gone/tombstoned/empty
                if mem is None:
                    continue
                mem["score"] = score
                out.append(mem)
                if len(out) >= k:
                    break
            return out
        except Exception:
            return []

    def recall_shared_context(self, query, top_k=3, exclude=None, char_budget=None):
        """Ready-to-prepend SHARED-memory block for a `shared_memory_read` employee:

            Relevant company memory (the Chairman's standing direction — advisory, not orders):
            - <directive one>

        or "" for every no-injection case (capability off, empty query, no venv /
        empty index, zero hits). `exclude` is a set of normalized texts / ids to
        skip (dedup against the own-store block); `char_budget` caps the whole block
        (defaults to the full dispatch budget). Never raises."""
        try:
            if not self.shared_memory_read:
                return ""
            hits = self.recall_shared(query, top_k=top_k)
            hits = self._dedup_hits(hits, exclude)
            budget = _DISPATCH_INJECT_BUDGET if char_budget is None else char_budget
            return self._render_memory_block(_SHARED_MEMORY_HEADER, hits, budget)
        except Exception:
            return ""

    def dispatch_context(self, query, top_k=3):
        """The ONE call an orchestrator makes before dispatching THIS employee as a
        headless worker (Phase 18c). Returns the combined, budget-capped injection:
        the own-store "Relevant past experience" block (rag employee) followed by a
        SEPARATE shared "Relevant company memory" block (shared_memory_read
        employee), or "" when neither has anything.

        Dedup: a shared hit whose content OR id already appears in the own-store
        block is dropped (own-store wins). The two blocks SHARE one overall char
        budget (`_DISPATCH_INJECT_BUDGET`) — the own block renders first, the shared
        block gets only what remains — so injecting BOTH can never balloon the
        worker prompt. Each half degrades independently; never raises, never
        blocks."""
        try:
            budget = _DISPATCH_INJECT_BUDGET
            parts, used = [], 0

            own_hits = []
            if self.rag_memory_enabled:
                try:
                    own_hits = self.recall(query, top_k=top_k)
                except Exception:
                    own_hits = []
            own_block = self._render_memory_block(_OWN_MEMORY_HEADER, own_hits, budget)
            if own_block:
                parts.append(own_block)
                used += len(own_block)

            exclude = set()
            for h in own_hits:
                t = _normalize_memory_text(h.get("text") or "")
                if t:
                    exclude.add(t)
                hid = str(h.get("id") or "").strip()
                if hid:
                    exclude.add(hid)

            # Remaining budget (account for the "\n\n" separator between blocks).
            remaining = budget - used - (2 if parts else 0)
            shared_block = self.recall_shared_context(
                query, top_k=top_k, exclude=exclude,
                char_budget=max(0, remaining))
            if shared_block:
                parts.append(shared_block)

            return "\n\n".join(parts)
        except Exception:
            return ""

    @staticmethod
    def _dedup_hits(hits, exclude):
        """Drop hits whose normalized text OR id is in `exclude` (a set). None/empty
        exclude -> hits unchanged."""
        if not exclude:
            return list(hits)
        out = []
        for h in hits:
            t = _normalize_memory_text(h.get("text") or "")
            hid = str(h.get("id") or "").strip()
            if (t and t in exclude) or (hid and hid in exclude):
                continue
            out.append(h)
        return out

    @staticmethod
    def _render_memory_block(header, hits, char_budget):
        """Render `hits` as `header` + `- <snippet>` bullets. Each snippet is
        whitespace-collapsed and trimmed to `_RECALL_SNIPPET_CHARS`; the whole block
        is capped at `char_budget` chars. Returns "" if there are no hits, the
        budget can't fit the header + one bullet, or nothing renders."""
        if not hits or char_budget <= 0:
            return ""
        lines = [header]
        used = len(header)
        for h in hits:
            snippet = _WS_RE.sub(" ", str(h.get("text") or "").strip())
            if not snippet:
                continue
            if len(snippet) > _RECALL_SNIPPET_CHARS:
                snippet = snippet[:_RECALL_SNIPPET_CHARS].rstrip() + "…"
            line = f"- {snippet}"
            if used + 1 + len(line) > char_budget:
                break
            lines.append(line)
            used += 1 + len(line)
        if len(lines) == 1:                          # header only -> nothing fit
            return ""
        return "\n".join(lines)

    def _read_shared_memory(self, path):
        """Parse a live SHARED memory file into {id, owner, tags, source, path,
        text}, or None if it is gone, tombstoned (archived/absorbed/defunct), or has
        an empty body — the same live re-validation the ask-time hook applies."""
        try:
            if not Path(path).exists():
                return None
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, IOError, UnicodeError):
            return None
        fm, body = {}, text
        try:
            if frontmatter is not None:
                raw_lines, body = frontmatter.split(text)
                fm = _parse_fm(raw_lines)
        except Exception:
            fm, body = {}, text
        if _is_tombstoned(fm):                        # retired memory -> skip
            return None
        body = (body or "").strip()
        if not body:
            return None
        return {
            "id": str(fm.get("id") or ""),
            "owner": str(fm.get("owner") or ""),
            "tags": _as_list(fm.get("tags")),
            "source": self._clean_optional(fm.get("source")),
            "path": str(path),
            "text": body,
        }

    @staticmethod
    def _resolve(p):
        try:
            return Path(os.path.realpath(str(p)))
        except Exception:
            return Path(str(p))

    def _read_memory(self, path):
        """Parse a live memory file into {id, owner, tags, source, path, text}.
        Returns None on any read error or if the body is empty."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, IOError, UnicodeError):
            return None
        fm, body = {}, text
        try:
            if frontmatter is not None:
                raw_lines, body = frontmatter.split(text)
                fm = _parse_fm(raw_lines)
        except Exception:
            fm, body = {}, text
        body = (body or "").strip()
        if not body:
            return None
        return {
            "id": str(fm.get("id") or ""),
            "owner": str(fm.get("owner") or ""),
            "tags": _as_list(fm.get("tags")),
            "source": self._clean_optional(fm.get("source")),
            "path": str(path),
            "text": body,
        }

    def __repr__(self):
        return f"Employee({self.name!r}, tier={self.tier!r}, role={self.role!r})"
