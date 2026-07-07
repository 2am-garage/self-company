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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import frontmatter  # shared markdown-frontmatter parsing seam
except Exception:                                              # pragma: no cover
    frontmatter = None


# ================================================================ Layer B tables
# The fixed role topology — the single source of truth. Config may enable/disable
# an employee and pick which of THEIR OWN duties run; it can never grant a duty
# outside these sets or reassign a role (modularize, don't special-case).
EMPLOYEES = ("tony", "gibby", "bob", "mike", "elon", "phoebe", "tom", "july")

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
    "july":   set(),                         # HR tuning — no scheduled deterministic duty
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
}


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
            data[key] = _strip_comment(val)
    return data


def _as_list(v):
    """Normalize a frontmatter value to a list of strings. A scalar becomes a
    one-item list; None/empty -> []."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    s = str(v).strip()
    return [s] if s else []


# ================================================================ the model
class Employee:
    """One data-driven employee. Construct via `Employee.load(name, company_dir)`;
    all fields are read from the employee's desk + the fixed Layer-B tables. Never
    raises — an absent desk or field degrades to a default."""

    # The capability field names that make up the least-privilege slice. This is
    # the SEAM July will later extend (mcp / skills / plugins); Phase 16 carries
    # only what exists in context.md today. Extending capabilities is adding a
    # name here + the field on load, nothing structural.
    CAPABILITY_FIELDS = ("tools", "reads", "writes", "handoff_to")

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
        self.tier = TIERS.get(self.name, "worker")

        # ---- capabilities (least-privilege slice) ---------------------------
        self.tools = _as_list(fm.get("tools"))
        self.reads = _as_list(fm.get("reads"))
        self.writes = _as_list(fm.get("writes"))
        self.handoff_to = _as_list(fm.get("handoff_to"))

        # ---- execution ------------------------------------------------------
        self.model = str(fm.get("model") or "").strip()
        self.token_budget = self._clean_optional(fm.get("token_budget"))
        self._eff_self = None   # lazily filled effective-config slice

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
        """RESERVED accessor for the later per-employee-memory idea (Phase 16
        declares the seam; it is not built). Points at a per-employee memory
        folder under the desk; nothing here creates or reads it yet."""
        return self.desk_dir / "memory"

    # ------------------------------------------------------------ capabilities
    def capabilities(self):
        """The employee's least-privilege capability slice as a dict. The seam
        July will extend (mcp / skills / plugins); Phase 16 emits only the fields
        that exist today (tools / reads / writes / handoff_to)."""
        return {field: list(getattr(self, field)) for field in self.CAPABILITY_FIELDS}

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

    def __repr__(self):
        return f"Employee({self.name!r}, tier={self.tier!r}, role={self.role!r})"
