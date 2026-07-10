#!/usr/bin/env python3
"""
schedule_config — the reader + single source of truth for `org/schedule.yaml`.

Phase 12: each company declares its OWN schedule and per-employee duties as DATA
here, instead of the timing/work being hardcoded in schedule.sh / daily-run.sh.
This module is the ONE place that:
  * knows the config SCHEMA (which top-level + per-employee keys exist),
  * imports the fixed ROLE topology from employee.py (which duties each employee
    may own — Layer B, NOT configurable; that module is the single source both
    this reader and the validator import, Phase 16),
  * translates a friendly `cadence` into a 5-field cron expression, and
  * answers daily-run.sh's "should STEP run on THIS tick?" gating question.

Design contract (Chairman, 2026-07-06):
  * Layer A (config-adjustable): per-employee cadence, which OPTIONAL duties run,
    budget, enabled; company tick; research on/off; agent knobs.
  * Layer B (locked here in code): which employee holds which ROLE. There is NO
    `role:`/`attacks:` field — an employee's allowed duties are fixed below, so
    config can pick WHICH of an employee's own duties run, never REASSIGN a role.
  * Absent / empty / malformed schedule.yaml => defaults that reproduce today's
    behaviour byte-for-byte, and this module NEVER raises to its callers.

Pure stdlib. PyYAML is used if present; otherwise a small safe parser handles the
exact shape schedule.yaml uses (top-level scalars, one-line `{ }` maps, `[ ]`
lists). Mirrors trigger_engine.py's stdlib-only, dormant-safe discipline.

Usage (CLI seam for bash callers):
  schedule_config.py --company DIR --cron daily    --minute M   # print daily cron expr
  schedule_config.py --company DIR --cron research --minute M   # print research cron expr
  schedule_config.py --company DIR --should-run STEP --hour H --dow D  # exit 0 run / 1 skip
  schedule_config.py --company DIR --agent KEY                  # print model|timeout|daily_cap
  schedule_config.py --company DIR --research-enabled           # exit 0 on / 1 off
  schedule_config.py --company DIR --roster                     # print roster.md body
  schedule_config.py --company DIR --explain                    # print effective config (JSON)
  schedule_config.py --company DIR --plan-tick --hour H --dow D # Phase 28 Item 3:
      ONE JSON with every gate decision + agent knob for this tick, from ONE
      effective() load — replaces daily-run.sh's ~13 separate --should-run/
      --agent spawns with one process. See plan_tick() below.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The fixed role topology (Layer B) is authoritative in employee.py — the single
# source of truth, imported here and by schedule_validator.py so there is exactly
# one home for it (Phase 16). Config may enable/disable an employee and pick which
# of THEIR OWN duties run; it can never grant a duty outside these sets. Editing
# the topology is a code change over there, deliberately — not a config knob.
from employee import (  # noqa: E402
    EMPLOYEES, ALLOWED_DUTIES, ATTACK_DUTIES, BUILD_DUTIES, VERIFY_DUTIES,
    STEP_OWNER, Employee,
)

CONFIG_SUBPATH = "org/schedule.yaml"

# ---------------------------------------------------------------- config schema
# The config SCHEMA (which YAML keys the reader/validator accept or forbid) stays
# here — it is about the config FILE, not about who an employee IS (that is the
# topology, now in employee.py). Per-employee config keys that are allowed;
# anything else is rejected by the
# validator (this is how R3/R4/R5/R6 fall out of ONE structural rule).
EMPLOYEE_KEYS = {"cadence", "duties", "budget", "enabled"}
TOP_KEYS = {"cadence", "research", "agent"} | set(EMPLOYEES)

# Names that must NEVER appear as a config key anywhere (fail-closed footguns:
# they would imply reassigning a role or tuning the sign-off gate — Layer B).
FORBIDDEN_KEYS = {
    "role", "roles", "tier", "attacks", "attacker", "builder", "team",
    "position", "gate", "sign_off", "signoff", "consecutive", "rounds",
    "ledger", "red_blue", "redblue", "immune_memory",
}

# ---------------------------------------------------------------- defaults
DEFAULT_TICK_HOURS = 6        # today: */6  (4x/day)
DEFAULT_RESEARCH_DOW = 0      # Sunday
DEFAULT_RESEARCH_HOUR = 3
DEFAULT_AGENT = {"model": "claude-sonnet-4-6", "timeout": 600, "daily_cap": 4}


# ================================================================ parsing
def _safe_scalar(v):
    """Coerce a scalar token to bool/int/str (never eval)."""
    s = v.strip()
    if s.lower() in ("true", "yes", "on"):
        return True
    if s.lower() in ("false", "no", "off"):
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    # strip matching quotes
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _parse_inline(val):
    """Parse the RHS of a top-level key: a scalar, a `[ ... ]` list, or a
    `{ k: v, ... }` one-line map. Only the exact shapes schedule.yaml uses."""
    s = val.strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_safe_scalar(x) for x in inner.split(",") if x.strip()] if inner else []
    if s.startswith("{") and s.endswith("}"):
        out = {}
        inner = s[1:-1].strip()
        for part in _split_top(inner, ","):
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            out[k.strip()] = _parse_inline(v)  # values may themselves be lists
        return out
    return _safe_scalar(s)


def _split_top(s, sep):
    """Split on `sep` but not inside [] or {} — so nested lists survive."""
    out, depth, buf = [], 0, ""
    for ch in s:
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == sep and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf)
    return out


def _fallback_parse(text):
    """Parse the safe YAML subset without PyYAML. Supports top-level `key: value`
    where value is a scalar, one-line list, or one-line map. Nested block maps
    (indented children) are also supported for the per-employee blocks."""
    root = {}
    cur_key = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.rstrip()
        indented = raw[0] in " \t"
        line = stripped.split("#", 1)[0].rstrip() if "#" in stripped and "://" not in stripped else stripped
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        if not indented:
            cur_key = key
            if val.strip():
                root[key] = _parse_inline(val)
            else:
                root[key] = {}  # opens a block map
        else:
            # child of the current top-level block map
            if isinstance(root.get(cur_key), dict):
                root[cur_key][key] = _parse_inline(val) if val.strip() else {}
    return root


def load_raw(company):
    """Return the parsed dict from schedule.yaml, or {} if absent/unreadable.
    Never raises."""
    p = Path(company) / CONFIG_SUBPATH
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return {}
    try:
        import yaml  # optional
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        try:
            return _fallback_parse(text)
        except Exception:
            return {}


# ================================================================ cadence -> cron
# A cron time field may contain ONLY these characters. Anything else in a raw
# passthrough expr (letters, ';', '&', quotes, whitespace beyond the 5 field
# separators, ...) means it is NOT a cron expr and must never be written into a
# crontab line — see P9-D2: an unvalidated passthrough let junk / an embedded
# newline inject or corrupt the whole crontab.
_CRON_FIELD_RE = re.compile(r"^[0-9*/,\-]+$")

# Valid numeric domain of each of the 5 cron fields, in order:
#   minute 0-59, hour 0-23, day-of-month 1-31, month 1-12, day-of-week 0-7
# (dow 7 is the cron alias for Sunday). Used for SEMANTIC field validation.
_CRON_DOMAINS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _valid_cron_field(field, lo, hi):
    """Validate ONE cron field against its [lo, hi] domain — SEMANTICS, not just
    charset (P9-D3). Accepts `*`, `*/N`, `A`, `A-B`, `A-B/N`, and comma-lists of
    those, and REJECTS: empty list elements (`,,`, leading/trailing comma),
    dangling or backwards ranges (`1-`, `-5`, `5-2`, `0-0-0`), `*/0` or a step
    outside the field range, and any number (or range/step bound) outside the
    field's domain (`99` minute, `13` month, `8` dow, ...)."""
    if field == "":
        return False
    for elem in field.split(","):
        if elem == "":                       # ',,' / leading / trailing comma
            return False
        base = elem
        if "/" in elem:                      # step: base/N
            base, _, step = elem.partition("/")
            if not step.isdigit():           # '*/', '*/x', 'A-B/', '*/*/*'
                return False
            n = int(step)
            if n < 1 or n > hi:              # '*/0' or an absurd step
                return False
        if base == "*":
            continue
        if "-" in base:                       # a range A-B
            a, _, b = base.partition("-")
            if not a.isdigit() or not b.isdigit():   # '1-', '-5', '1-2-3'
                return False
            ai, bi = int(a), int(b)
            if ai < lo or bi > hi or ai > bi:        # out of range / backwards
                return False
        else:                                 # a single value
            if not base.isdigit():
                return False
            val = int(base)
            if val < lo or val > hi:
                return False
    return True


def _valid_cron_expr(expr):
    """True only if `expr` is a safe raw 5-field cron time expression:
      * NO control characters (newline/tab/CR/etc) — a newline would SPLIT the
        crontab into two lines (arbitrary line injection),
      * EXACTLY 5 whitespace-separated fields,
      * every field on the cron charset [0-9*/,-] AND semantically valid for its
        domain (rejects ',, * * * *', '99 99 99 99 99', '1- * * * *', '*/0', ...).
    Fail-closed: anything else returns False so the caller falls back to the
    hardcoded default and logs it. This is the ONE gate every raw passthrough
    (daily + research) goes through — block the class, not a single input."""
    if not expr or any(ord(ch) < 32 for ch in expr):
        return False
    fields = expr.split()
    if len(fields) != 5:
        return False
    if not all(_CRON_FIELD_RE.match(f) for f in fields):
        return False
    return all(_valid_cron_field(f, lo, hi)
               for f, (lo, hi) in zip(fields, _CRON_DOMAINS))


def daily_cron(cadence, minute):
    """Translate a friendly `cadence` into a 5-field cron expr using `minute` for
    the staggered minute slot. Unknown/invalid -> today's default (*/6) — the
    caller logs the fallback. Returns (expr, ok) where ok=False signals fallback."""
    m = str(minute)
    default = f"{m} */{DEFAULT_TICK_HOURS} * * *"
    if not cadence:
        return default, True
    raw = str(cadence).strip()
    c = raw.lower()
    # raw 5-field cron passthrough (advanced escape hatch) — VALIDATED before trust
    # (P9-D2): a 5-token string is only accepted if it is a real, injection-safe
    # cron expr; otherwise fall back to the default rather than write junk.
    if len(raw.split()) == 5:
        if _valid_cron_expr(raw):
            return raw, True
        return default, False
    if c == "hourly":
        return f"{m} * * * *", True
    mm = re.fullmatch(r"every\s*(\d+)h", c)
    if mm:
        n = int(mm.group(1))
        if 1 <= n <= 23:
            return f"{m} */{n} * * *", True
    mm = re.fullmatch(r"weekdays-(\d{1,2})-(\d{1,2})", c)
    if mm:
        a, b = int(mm.group(1)), int(mm.group(2))
        if 0 <= a <= 23 and 0 <= b <= 23 and a <= b:
            return f"{m} {a}-{b} * * 1-5", True
    return default, False


def research_cron(cadence, minute):
    """Weekly research cron. `weekly-<dow>-<hh>` (dow: sun..sat or 0..6) or raw
    cron. Default: Sunday 03:00. Returns (expr, ok)."""
    m = str(minute)
    default = f"{m} {DEFAULT_RESEARCH_HOUR} * * {DEFAULT_RESEARCH_DOW}"
    if not cadence:
        return default, True
    raw = str(cadence).strip()
    c = raw.lower()
    # raw 5-field cron passthrough — VALIDATED before trust (P9-D2), same gate as
    # daily_cron so a junk/newline research cadence can't corrupt the crontab.
    if len(raw.split()) == 5:
        if _valid_cron_expr(raw):
            return raw, True
        return default, False
    dows = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
    mm = re.fullmatch(r"weekly-([a-z]{3}|\d)-(\d{1,2})", c)
    if mm:
        d = dows.get(mm.group(1), None)
        if d is None and mm.group(1).isdigit():
            d = int(mm.group(1))
        h = int(mm.group(2))
        if d is not None and 0 <= d <= 6 and 0 <= h <= 23:
            return f"{m} {h} * * {d}", True
    return default, False


# ================================================================ effective view
def effective(company):
    """Merge config over defaults into a normalized effective view. Pure read;
    does NOT validate role invariants (that is schedule_validator's job) — but it
    IS tolerant: unknown/garbage keys are simply ignored here so a bad config can
    never crash a caller. Returns a plain dict."""
    raw = load_raw(company)
    emp = {}
    for name in EMPLOYEES:
        block = raw.get(name) if isinstance(raw.get(name), dict) else {}
        duties = block.get("duties")
        if not isinstance(duties, list):
            duties = sorted(ALLOWED_DUTIES[name])  # default: all of the role's own duties
        # keep only duties this employee is actually allowed to own
        duties = [d for d in duties if d in ALLOWED_DUTIES[name]]
        emp[name] = {
            "enabled": block.get("enabled", True) is not False,
            "cadence": str(block.get("cadence", "every-run")),
            "duties": duties,
            "budget": block.get("budget"),
        }
    agent = dict(DEFAULT_AGENT)
    if isinstance(raw.get("agent"), dict):
        for k in ("model", "timeout", "daily_cap"):
            if k in raw["agent"]:
                agent[k] = raw["agent"][k]
    research = {"enabled": True, "cadence": None}
    if isinstance(raw.get("research"), dict):
        research["enabled"] = raw["research"].get("enabled", True) is not False
        research["cadence"] = raw["research"].get("cadence")
    return {
        "cadence": raw.get("cadence"),
        "research": research,
        "agent": agent,
        "employees": emp,
        "present": bool(raw),
    }


# ================================================================ gating
def _cadence_matches(cadence, hour, dow, tick_hours=DEFAULT_TICK_HOURS):
    """Deterministic sub-cadence gate for a single tick, using only the wall
    clock (no stored state). tick index within the day = hour // tick_hours."""
    c = (cadence or "every-run").strip().lower()
    idx = hour // max(1, tick_hours)
    if c in ("every-run", "everyrun", "always", ""):
        return True
    if c == "on-trigger":
        return False           # only fired via fire-trigger.sh, never in the batch
    if c == "daily":
        return hour < tick_hours          # first tick of the day
    if c == "weekly":
        return dow == DEFAULT_RESEARCH_DOW and hour < tick_hours
    mm = re.fullmatch(r"every-(\d+)(?:th|st|nd|rd)?", c)
    if mm:
        n = int(mm.group(1))
        return n > 0 and idx % n == 0
    return True                 # unknown sub-cadence -> fail-open (run)


def should_run(company, step, hour, dow):
    """Should deterministic STEP run on this tick? Fail-OPEN: any doubt -> True,
    so a bad config or missing owner never silently suppresses maintenance.

    Routes through the Employee model (Phase 16): resolve the step's owner and ask
    THAT employee — which reuses this module's effective()/_cadence_matches, so the
    verdict is byte-identical to the pre-model owner-centric lookup."""
    owner = STEP_OWNER.get(step)
    if owner is None:
        return True
    return Employee.load(owner, company).should_run(step, hour, dow)


# ================================================================ plan-tick
def plan_tick(company, hour, dow):
    """Phase 28 Item 3: ONE JSON blob combining every gate decision + agent knob
    daily-run.sh needs for a single tick, from ONE effective() load. Replaces the
    ~13 separate schedule_config.py spawns (10 --should-run gates + 3 --agent knob
    reads) daily-run.sh made per tick with one process.

    The step set is STEP_OWNER's key set — the SAME table should_run() consults —
    so there is no second list that can drift from should_run's routing
    (modularize, don't special-case). Each boolean is should_run(company, step,
    hour, dow) itself: byte-identical to what the individual --should-run call
    would have returned for the same inputs. The agent knobs are effective()'s
    already-defaulted agent dict, same as individual --agent KEY reads.

    Never raises: should_run/effective are both already tolerant of a bad/absent
    config; the CLI layer additionally wraps this in try/except so a JSON encode
    failure or unexpected error still yields no output (caller fail-opens)."""
    steps = {step: should_run(company, step, hour, dow) for step in STEP_OWNER}
    eff = effective(company)
    agent = eff["agent"]
    return {
        "schema": 1,
        "steps": steps,
        "agent": {
            "model": agent.get("model", DEFAULT_AGENT["model"]),
            "timeout": agent.get("timeout", DEFAULT_AGENT["timeout"]),
            "daily_cap": agent.get("daily_cap", DEFAULT_AGENT["daily_cap"]),
        },
    }


# ================================================================ roster
def roster_md(company):
    """Render ops/schedule/roster.md from the effective config. Deterministic;
    marked generated so it is never hand-edited (Chairman's sweep-docs rule)."""
    eff = effective(company)
    dexpr, _ = daily_cron(eff["cadence"], "M")
    lines = [
        "# Schedule Roster — generated from org/schedule.yaml",
        "",
        "> GENERATED by schedule_config.py on each daily-run. Do NOT hand-edit —",
        "> change `org/schedule.yaml` instead. Layer B (roles / red-blue / sign-off",
        "> gate) is NOT configurable and is enforced by schedule_validator.py.",
        "",
        f"**Company tick:** `{dexpr}` (minute auto-staggered per project)",
        f"**Research (Mike):** {'on' if eff['research']['enabled'] else 'OFF'}"
        f" — `{eff['research']['cadence'] or 'weekly-sun-03 (default)'}`",
        f"**Agent:** model `{eff['agent']['model']}`, timeout {eff['agent']['timeout']}s,"
        f" daily-cap {eff['agent']['daily_cap']}",
        "",
        "## Per-employee scheduled duties",
        "",
        "| Employee | Enabled | Cadence | Duties | Budget |",
        "|---|---|---|---|---|",
    ]
    for name in EMPLOYEES:
        e = eff["employees"][name]
        duties = ", ".join(e["duties"]) if e["duties"] else "—"
        lines.append(
            f"| {name} | {'yes' if e['enabled'] else 'NO'} | {e['cadence']} "
            f"| {duties} | {e['budget'] if e['budget'] is not None else '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


# ================================================================ CLI
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument("--cron", choices=["daily", "research"])
    ap.add_argument("--minute", default="0")
    ap.add_argument("--should-run")
    ap.add_argument("--hour", type=int, default=0)
    ap.add_argument("--dow", type=int, default=0)
    ap.add_argument("--agent")
    ap.add_argument("--research-enabled", action="store_true")
    ap.add_argument("--roster", action="store_true")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--plan-tick", action="store_true")
    a = ap.parse_args()

    if a.plan_tick:
        # Fail-closed AT THE CLI LAYER only: any unexpected error prints nothing
        # and exits 1, so a bash caller's fail-open (all steps run, default
        # knobs) kicks in rather than trusting a half-formed JSON.
        try:
            print(json.dumps(plan_tick(a.company, a.hour, a.dow), sort_keys=True))
        except Exception:
            sys.exit(1)
        sys.exit(0)

    if a.cron:
        eff = effective(a.company)
        if a.cron == "daily":
            expr, ok = daily_cron(eff["cadence"], a.minute)
        else:
            expr, ok = research_cron(eff["research"]["cadence"], a.minute)
        print(expr)
        # exit 2 signals a fallback so the caller can log it (still prints a valid expr)
        sys.exit(0 if ok else 2)

    if a.should_run:
        sys.exit(0 if should_run(a.company, a.should_run, a.hour, a.dow) else 1)

    if a.agent:
        eff = effective(a.company)
        print(eff["agent"].get(a.agent, ""))
        sys.exit(0)

    if a.research_enabled:
        eff = effective(a.company)
        sys.exit(0 if eff["research"]["enabled"] else 1)

    if a.roster:
        print(roster_md(a.company))
        sys.exit(0)

    if a.explain:
        print(json.dumps(effective(a.company), indent=2, sort_keys=True))
        sys.exit(0)

    ap.error("no action given")


if __name__ == "__main__":
    main()
