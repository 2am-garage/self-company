#!/usr/bin/env python3
"""
july_audit — July's capability-steward audit (Phase 17). July's load-bearing,
recurring job: keep every worker's FUNCTIONAL capability profile (tools / MCP /
skills / plugins, declared in `org/employees/<name>/context.md`) both ACCURATE
against the real environment and RIGHT-SIZED (least-privilege) as the project's
toolchain evolves.

Built ON the Phase-16 `Employee` model: profiles are read via
`Employee.load(name, company).capabilities()` and the roster via
`Employee.roster()` — this script never re-parses context.md itself for the
profile (it reuses the model + the shared frontmatter seam).

PROPOSE-ONLY (Phase 17, P17-D2 decision). july_audit NEVER edits any employee's
`context.md`. The hard lesson from D1/D2: available capabilities CANNOT be
reliably enumerated to ground truth from the filesystem — a live, shipped grant
like the `deep-research` skill is bundled/otherwise-provided and is NOT physically
enumerable under `~/.claude`, so ANY irreversible auto-removal based on a
filesystem availability view is fundamentally unsafe. So all three findings are
surfaced as PROPOSALS for the Chairman/Elon to approve; Elon → Phoebe → Tom apply
any approved change. This deletes the whole wrongful-auto-removal class outright —
no per-dimension "is this reliably enumerable?" guessing.

Classification (per worker, per audited dimension mcp/skills/plugins) — ALL are
PROPOSALS, none is applied:
  * STALE grant — declared but not found in the enumerable environment (maybe a
    removed MCP/skill/plugin, maybe just non-enumerable). → PROPOSE removal, with
    the completeness of the availability view attached so the human decides with
    eyes open.
  * CAPABILITY GAP — an available capability the role plausibly needs (per a
    conservative role-hint table) but isn't granted. → PROPOSE grant.
  * OVER-GRANT — a declared, still-available capability the role does NOT
    plausibly need (least-privilege violation). → PROPOSE removal.

GUARDRAILS (Gibby will attack these):
  * No mutation — july_audit writes ONLY the proposals file + July's log; it never
    touches an employee's context.md under any input.
  * Manager boundary — July NEVER audits Elon / Phoebe / July profiles.
  * Red/blue — a proposal touching the Gibby/Bob pair is marked "human review
    required." Capability grants are orthogonal to duty assignment (R1–R6, owned
    by schedule_validator) — July may not reassign duties.
  * Never fails the run — every environment source degrades gracefully: an
    absent/unreadable source is reported "unknown" and that dimension is SKIPPED,
    never a crash.
  * The `tools` dimension is REPORTED but not classified for stale/gap/over —
    the builtin tool set is stable and MCP-backed `mcp__*` tools are governed by
    the `mcp` dimension; classifying churn where it actually churns
    (MCP/skills/plugins) is the conservative choice.

Usage:
  july_audit.py --company DIR [--apply] [--home DIR] [--now YYYY-MM-DD]
    (default: dry-run — detect, classify, print JSON report to stdout; no writes)
    --apply : WRITE the proposals file to ops/plans/ + log the audit to July's
              log. Still NO context.md mutation — "apply" means "publish the
              proposals," not "change a profile."
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from employee import Employee, EMPLOYEES
except Exception:                                              # pragma: no cover
    Employee = None
    EMPLOYEES = ("tony", "gibby", "bob", "mike", "elon", "phoebe", "tom", "july")

# ---------------------------------------------------------------- topology
# Managers are off-limits (persona boundary). The Gibby/Bob red-blue pair may be
# CLASSIFIED (so drift is visible) but never AUTO-changed — flagged for review.
MANAGERS = {"elon", "phoebe", "july"}
RED_BLUE_PAIR = {"gibby", "bob"}

# Only these churny dimensions are classified for stale/gap/over-grant. `tools`
# is reported (declared) but not classified — see the module docstring.
AUDITED_DIMENSIONS = ("mcp", "skills", "plugins")

# The builtin tool set (for the report's `available.tools`). Not exhaustive by
# design — it is informational only; no tool is ever auto-removed.
STANDARD_TOOLS = (
    "Read", "Write", "Edit", "Bash", "Glob", "Grep", "Task", "WebSearch",
    "WebFetch", "NotebookEdit", "TodoWrite", "BashOutput", "KillShell",
)

# Conservative role→capability hints: "this role plausibly NEEDS these." Drives
# GAP (need ∩ available − declared) and shields legitimate grants from OVER-GRANT
# (declared ∩ available − need). Deliberately minimal — only clear, role-grounded
# needs. Absence of a hint means "no opinion": nothing is proposed for a grant.
ROLE_CAPABILITY_HINTS = {
    "mike":  {"skills": {"deep-research"}},   # researcher: multi-source research harness
    "gibby": {"mcp": {"playwright"}},         # QA: browser-automation verification
}


def _hint(name, dim):
    return set(ROLE_CAPABILITY_HINTS.get(name, {}).get(dim, ()))


# ============================================================ env detection
def _read_json(path):
    """Parse a JSON file; None on any error (absent/unreadable/malformed)."""
    try:
        p = Path(path)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _mcp_servers_from(obj):
    """Pull MCP server names from a parsed settings object (top-level
    `mcpServers`, or per-project `projects.*.mcpServers`)."""
    names = set()
    if not isinstance(obj, dict):
        return names
    ms = obj.get("mcpServers")
    if isinstance(ms, dict):
        names |= set(ms.keys())
    projs = obj.get("projects")
    if isinstance(projs, dict):
        for pv in projs.values():
            if isinstance(pv, dict) and isinstance(pv.get("mcpServers"), dict):
                names |= set(pv["mcpServers"].keys())
    return names


def detect_mcp(project_dir, home):
    """Available MCP servers. Returns (available:set|None, complete:bool, source).

    P17-D1 fail-safe contract: availability is trusted-COMPLETE (so a declared-but-
    absent grant may be auto-removed) ONLY when at least one config file was parsed
    successfully AND yielded ≥1 server AND no candidate was malformed. Otherwise the
    dimension is UNKNOWN (None) — no source, an empty/zero-server parse, or a
    malformed candidate all POISON the view to unknown, because ∅-from-presence is
    indistinguishable from "couldn't confirm" and a wrongful strip of a git-ignored
    context.md is unrecoverable. Never auto-remove on a maybe."""
    candidates = [
        Path(project_dir) / ".mcp.json",
        Path(project_dir) / ".claude" / "settings.json",
        Path(project_dir) / ".claude" / "settings.local.json",
        Path(home) / ".claude.json",
        Path(home) / ".claude" / "settings.json",
    ]
    any_exist, malformed, names, used = False, False, set(), []
    for c in candidates:
        try:
            if not Path(c).exists():
                continue
        except Exception:
            continue
        any_exist = True
        obj = _read_json(c)
        if obj is None:                 # exists but unreadable/malformed -> POISON
            malformed = True
            continue
        got = _mcp_servers_from(obj)
        if got:
            names |= got
            used.append(c.name)
    if malformed:
        return None, False, "unknown (malformed config)"
    if not any_exist:
        return None, False, "unknown (no config found)"
    if not names:
        return None, False, "unknown (no servers positively parsed)"
    return names, True, ",".join(sorted(set(used)))


def _skill_names_under(root):
    """Skill dir names for every `<root>/*/SKILL.md`."""
    names = set()
    try:
        base = Path(root)
        if base.is_dir():
            for sk in base.iterdir():
                if sk.is_dir() and (sk / "SKILL.md").exists():
                    names.add(sk.name)
    except Exception:
        pass
    return names


def detect_skills(project_dir, home):
    """Available skills. Returns (available:set|None, complete:bool, source).

    Skills come from local `.claude/skills` (project + home) AND plugin-marketplace
    skills (`<home>/.claude/plugins/**/skills/`). CRITICAL (P17-D3): skills are the
    ONE dimension that is NOT authoritatively enumerable from the filesystem —
    BUNDLED / first-party skills (e.g. `deep-research`) are live and agent-usable
    but are NOT physically on disk under `~/.claude`, so a filesystem scan can never
    prove a declared skill is truly gone. Therefore `complete` is ALWAYS False here:
    a skills-STALE finding is ALWAYS surfaced with the "may be non-enumerable —
    verify" caveat, never as an authoritative "it's gone." `available` is still a
    best-effort union (drives GAP/OVER-GRANT proposals); it is None when nothing was
    found at all (a merely-present empty dir yields None, never ∅). This plugin's
    own internal skills dir is deliberately NOT counted (shipped source, not a grant
    registry)."""
    local_roots = [
        Path(project_dir) / ".claude" / "skills",
        Path(home) / ".claude" / "skills",
    ]
    names, any_root = set(), False
    for r in local_roots:
        try:
            if Path(r).is_dir():
                any_root = True
                names |= _skill_names_under(r)
        except Exception:
            continue
    marketplace = Path(home) / ".claude" / "plugins"
    try:
        if marketplace.is_dir():
            any_root = True
            for sk in marketplace.rglob("skills"):
                if sk.is_dir():
                    names |= _skill_names_under(sk)
    except Exception:
        pass
    if not any_root:
        return None, False, "unknown (no skills registry found)"
    available = names or None            # empty union -> unknown, never ∅
    # P17-D3: NEVER complete. The filesystem cannot enumerate bundled/first-party
    # skills, so a scan can never prove a declared skill is truly gone.
    return available, False, "skills registry (fs-enumerable only — NON-AUTHORITATIVE; bundled skills invisible)"


def detect_plugins(project_dir, home):
    """Installed plugins. Returns (available:set|None, complete:bool, source).

    The `.claude/plugins` dir (project + home) IS the authoritative installed-plugin
    registry, so when it exists AND lists ≥1 plugin the view is complete. P17-D1: a
    merely-present EMPTY plugins dir yields None (not ∅) so it can never authorize
    stripping a declared plugin. No plugins root anywhere → unknown."""
    roots = [
        Path(project_dir) / ".claude" / "plugins",
        Path(home) / ".claude" / "plugins",
    ]
    any_root, names = False, set()
    for r in roots:
        try:
            base = Path(r)
            if not base.is_dir():
                continue
            any_root = True
            for child in base.iterdir():
                if not child.is_dir():
                    continue
                if (child / ".claude-plugin" / "plugin.json").exists():
                    names.add(child.name)
                elif child.name not in ("marketplaces", "repos", "cache"):
                    # a plausible installed-plugin dir; skip known infra dirs
                    names.add(child.name)
        except Exception:      # defensive; never crash on a weird tree
            continue
    if not any_root:
        return None, False, "unknown (no plugins registry found)"
    available = names or None            # empty dir -> unknown, never ∅
    complete = available is not None     # authoritative registry with ≥1 plugin
    return available, complete, ("plugins registry" if available else
                                 "unknown (plugins dir present but empty)")


def detect_available(project_dir, home):
    """All four capability dimensions' availability. Each value:
    {"available": sorted list | None, "complete": bool, "source": str}.
    `available` None ⇒ the dimension is skipped entirely (unknown). `complete`
    False ⇒ the view is not trustworthy enough to AUTO-REMOVE a stale grant (though
    GAP/OVER-GRANT proposals may still be emitted from whatever WAS positively
    seen). Only a complete, non-None view authorizes stale auto-removal (P17-D1)."""
    mcp, mcp_ok, mcp_src = detect_mcp(project_dir, home)
    skl, skl_ok, skl_src = detect_skills(project_dir, home)
    plg, plg_ok, plg_src = detect_plugins(project_dir, home)
    return {
        "tools":   {"available": list(STANDARD_TOOLS), "complete": False, "source": "builtin"},
        "mcp":     {"available": None if mcp is None else sorted(mcp), "complete": mcp_ok, "source": mcp_src},
        "skills":  {"available": None if skl is None else sorted(skl), "complete": skl_ok, "source": skl_src},
        "plugins": {"available": None if plg is None else sorted(plg), "complete": plg_ok, "source": plg_src},
    }


# ============================================================ classification
def classify_employee(name, capabilities, available):
    """Classify one worker's declared profile against the environment. Returns a
    dict with declared/stale/gap/over_grant (each dim->sorted list) + red_blue.

    PROPOSE-ONLY (P17-D2): none of these is applied, so all three are computed the
    same conservative way — from whatever the environment POSITIVELY yielded. A
    dimension whose availability is UNKNOWN (None: no source / empty / malformed /
    poisoned) is skipped entirely (we can say nothing). The `complete` flag is NOT
    a gate here (there is no auto-mutation to gate); it is carried in the report so
    each proposal can state whether the availability view was trustworthy-complete
    — the reader (Chairman/Elon) decides with eyes open, since a "stale" skill may
    simply be non-enumerable (e.g. bundled) rather than truly gone."""
    declared_all = {d: list(capabilities.get(d, [])) for d in
                    ("tools", "mcp", "skills", "plugins")}
    stale, gap, over = {}, {}, {}
    for dim in AUDITED_DIMENSIONS:
        avail_entry = available.get(dim, {})
        avail = avail_entry.get("available")
        declared = set(declared_all.get(dim, []))
        if avail is None:
            continue                       # unknown env source -> skip this dim entirely
        avail = set(avail)
        need = _hint(name, dim)
        s = sorted(declared - avail)                      # declared, not enumerable -> propose removal
        g = sorted((need & avail) - declared)             # needed, available, ungranted -> propose grant
        o = sorted((declared & avail) - need)             # granted, available, not needed -> propose removal
        if s:
            stale[dim] = s
        if g:
            gap[dim] = g
        if o:
            over[dim] = o
    return {
        "declared": declared_all,
        "stale": stale,
        "gap": gap,
        "over_grant": over,
        "red_blue": name in RED_BLUE_PAIR,
    }


# ============================================================ proposals
def render_proposals(report):
    """Markdown proposal doc for the Chairman/Elon to APPROVE. Every finding —
    STALE, GAP, OVER-GRANT — is a PROPOSAL; july_audit applies NONE of them. An
    approved change routes Elon (adjudicate) → Phoebe (dispatch) → Tom (edit the
    context.md). Each STALE item carries whether the availability view was
    trustworthy-complete, because a "stale" capability may simply be non-enumerable
    (bundled) rather than truly gone. Returns "" when there is nothing to propose."""
    per = report["employees"]
    complete = report.get("completeness", {})
    lines, any_item = [], False
    lines.append(f"# Capability audit — proposals ({report['generated']})")
    lines.append("")
    lines.append("> Generated by july_audit.py (July, capability steward). These are PROPOSALS")
    lines.append("> only — July does NOT edit any employee's context.md. Approve here, then")
    lines.append("> Elon (adjudicate) → Phoebe (dispatch) → Tom (apply the edit). A capability")
    lines.append("> change is a privilege decision; a red/blue-pair change needs human review.")
    lines.append("> NOTE: availability is filesystem-enumerated and cannot be ground truth — a")
    lines.append("> 'stale' item on an INCOMPLETE view may just be non-enumerable, not gone.")
    lines.append("")
    for name in sorted(per):
        e = per[name]
        if not e.get("audited"):
            continue
        stale, gap, over = e.get("stale") or {}, e.get("gap") or {}, e.get("over_grant") or {}
        if not (stale or gap or over):
            continue
        any_item = True
        tag = " **[red/blue pair — human review required]**" if e.get("red_blue") else ""
        lines.append(f"## {name}{tag}")
        for dim, items in sorted(stale.items()):
            conf = "view COMPLETE" if complete.get(dim) else "view INCOMPLETE — may be non-enumerable, verify before removing"
            lines.append(f"- STALE (propose removal): `{dim}` -= {items} — declared but not found in the enumerable environment ({conf}).")
        for dim, items in sorted(gap.items()):
            lines.append(f"- GAP (propose grant): `{dim}` += {items} — role plausibly needs it, available, not granted.")
        for dim, items in sorted(over.items()):
            lines.append(f"- OVER-GRANT (propose removal): `{dim}` -= {items} — granted + available but role has no plausible need (least-privilege).")
        lines.append("")
    if not any_item:
        return ""
    return "\n".join(lines).rstrip() + "\n"


def write_proposals(company, report):
    """Write the proposals doc to ops/plans/capability-audit-<date>.md (OVERWRITE,
    so re-running the same day is idempotent). Returns the path, or None when there
    is nothing to propose / on any error."""
    md = render_proposals(report)
    if not md:
        return None
    try:
        plans = Path(company) / "ops" / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        out = plans / f"capability-audit-{report['generated']}.md"
        out.write_text(md, encoding="utf-8")
        return str(out.relative_to(company)) if out.is_relative_to(company) else str(out)
    except Exception:
        return None


# ============================================================ audit
def audit(company, home=None, now=None, apply=False):
    """Run the full capability audit — PROPOSE-ONLY (P17-D2). Dry-run by default;
    `apply=True` WRITES the proposals file + logs the audit. NEVER edits any
    employee's context.md under any input. Returns the JSON report dict. Never
    raises."""
    company = Path(company)
    project_dir = company.parent
    home = Path(home) if home else Path.home()
    generated = str(now or date.today())

    available = detect_available(project_dir, home)
    roster = list(Employee.roster()) if Employee else list(EMPLOYEES)

    per_emp, employees_out = {}, {}
    for name in roster:
        if name in MANAGERS:
            employees_out[name] = {
                "tier": "manager", "audited": False,
                "reason": "manager boundary — July does not audit manager profiles",
            }
            continue
        try:
            caps = Employee.load(name, company).capabilities() if Employee else {}
        except Exception:
            caps = {}
        res = classify_employee(name, caps, available)
        per_emp[name] = res

    # A dimension is "unknown" when its availability view is absent (None) — we can
    # say nothing there. (`incomplete_dimensions` separately flags dims that WERE
    # seen but not proven complete — surfaced as low-confidence context, not skipped.)
    unknown_dims = sorted(d for d in AUDITED_DIMENSIONS
                          if available.get(d, {}).get("available") is None)
    incomplete_dims = sorted(
        d for d in AUDITED_DIMENSIONS
        if available.get(d, {}).get("available") is not None
        and not available.get(d, {}).get("complete"))
    totals = {"stale": 0, "gap": 0, "over_grant": 0}
    for name, res in per_emp.items():
        employees_out[name] = {
            "tier": "worker", "audited": True,
            "red_blue": res["red_blue"],
            "declared": {d: list(v) for d, v in res["declared"].items()},
            "stale": res["stale"],
            "gap": res["gap"],
            "over_grant": res["over_grant"],
        }
        totals["stale"] += sum(len(v) for v in res["stale"].values())
        totals["gap"] += sum(len(v) for v in res["gap"].values())
        totals["over_grant"] += sum(len(v) for v in res["over_grant"].values())

    report = {
        "schema": "july-capability-audit/1",
        "generated": generated,
        "applied": bool(apply),
        "available": {d: available[d]["available"] for d in available},
        "completeness": {d: bool(available[d].get("complete")) for d in available},
        "sources": {d: available[d]["source"] for d in available},
        "employees": employees_out,
        "summary": {
            "workers_audited": len(per_emp),
            "managers_skipped": sorted(n for n in roster if n in MANAGERS),
            "unknown_dimensions": unknown_dims,
            "incomplete_dimensions": incomplete_dims,
            "stale_total": totals["stale"],
            "gap_total": totals["gap"],
            "over_grant_total": totals["over_grant"],
            # every finding is a proposal (nothing is auto-applied)
            "proposals_total": totals["stale"] + totals["gap"] + totals["over_grant"],
        },
    }

    if apply:
        report["proposals_path"] = write_proposals(company, report)
        _log_july(company, report)
    return report


def _log_july(company, report):
    """Append a one-line audit summary to July's log.md (via the Employee model)."""
    s = report["summary"]
    line = (f"- capability audit ({report['generated']}): "
            f"audited {s['workers_audited']} workers | "
            f"proposals {s['proposals_total']} "
            f"(stale {s['stale_total']}, gap {s['gap_total']}, over {s['over_grant_total']}) "
            f"— propose-only, no context.md edited"
            + (f" | unknown env: {','.join(s['unknown_dimensions'])}" if s['unknown_dimensions'] else ""))
    try:
        if Employee is not None:
            Employee.load("july", company).log(line)
    except Exception:
        pass


# ============================================================ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="July's capability-steward audit")
    ap.add_argument("--company", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="WRITE the proposals file + log the audit (still edits NO context.md)")
    ap.add_argument("--home", default=None, help="override home dir for env detection (tests)")
    ap.add_argument("--now", default=None, help="override date (YYYY-MM-DD)")
    a = ap.parse_args(argv)

    company = Path(a.company)
    if not company.exists():
        print(json.dumps({"error": "no .company", "schema": "july-capability-audit/1",
                          "summary": {"workers_audited": 0}}))
        return 0
    try:
        report = audit(company, home=a.home, now=a.now, apply=a.apply)
    except Exception as e:
        # Never fail the run — emit a minimal, valid report instead of crashing.
        print(json.dumps({"error": f"audit error: {e}",
                          "schema": "july-capability-audit/1",
                          "summary": {"workers_audited": 0}}))
        return 0
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
