"""
Tests for july_audit.py — July's Phase 17 capability-steward audit (PROPOSE-ONLY).

P17-D2 decision: july_audit NEVER edits an employee's context.md. It detects,
classifies (STALE / GAP / OVER-GRANT), and writes every finding as a PROPOSAL for
the Chairman/Elon to approve (Elon→Phoebe→Tom apply any approved edit). The
overriding invariant these tests pin: **no input, under --apply or not, ever
mutates a context.md** — because filesystem availability cannot be ground truth (a
live bundled grant like `deep-research` is not enumerable), so no auto-mutation is
ever safe.

Every test builds a fake project (a `.company` with worker desks) + a fake home
(the MCP/skills/plugins registries), so detection is fully controlled.
"""

import json
import os
import shutil
import tempfile
import unittest

import _helpers  # noqa: F401  (puts scripts/ on sys.path)
from _helpers import run_script

import july_audit
from employee import Employee


CTX_TMPL = """---
name: {name}
role: {role}
manager: Phoebe
people_lead: July
model: sonnet
tools: [Read, Bash]
mcp: {mcp}
skills: {skills}
plugins: {plugins}
handoff_to: [Phoebe]
---
{name}'s desk.
"""


def _desk(company, name, *, role="Worker", mcp="[]", skills="[]", plugins="[]"):
    d = os.path.join(company, "org", "employees", name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "context.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(CTX_TMPL.format(name=name, role=role, mcp=mcp, skills=skills, plugins=plugins))
    return path


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = os.path.join(self.tmp, "proj")
        self.company = os.path.join(self.proj, ".company")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.company, exist_ok=True)
        os.makedirs(self.home, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- fake environment builders ------------------------------------------
    def set_mcp(self, *servers):
        with open(os.path.join(self.proj, ".mcp.json"), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {s: {} for s in servers}}, f)

    def set_skills(self, *names):
        # Always create the base dir (even with no names) so a "present-but-empty"
        # local skills registry can be exercised.
        base = os.path.join(self.home, ".claude", "skills")
        os.makedirs(base, exist_ok=True)
        for n in names:
            os.makedirs(os.path.join(base, n), exist_ok=True)
            open(os.path.join(base, n, "SKILL.md"), "w").close()

    def set_plugins(self, *names):
        base = os.path.join(self.home, ".claude", "plugins")
        os.makedirs(base, exist_ok=True)
        for n in names:
            pd = os.path.join(base, n, ".claude-plugin")
            os.makedirs(pd, exist_ok=True)
            with open(os.path.join(pd, "plugin.json"), "w") as f:
                f.write("{}")

    def audit(self, apply=False, now="2026-07-07"):
        return july_audit.audit(self.company, home=self.home, now=now, apply=apply)

    def assertContextUnchanged(self, name, before):
        path = os.path.join(self.company, "org", "employees", name, "context.md")
        self.assertEqual(_read(path), before, f"{name}'s context.md was mutated")


# ============================================================ classification
class TestClassification(Base):
    def test_stale_gap_overgrant_all_surface_as_findings(self):
        self.set_mcp("good")
        self.set_skills("deep-research")
        self.set_plugins("p1")
        _desk(self.company, "tony", mcp="[good, dead]")            # dead STALE; good OVER-GRANT
        _desk(self.company, "mike", role="R&D", skills="[]")       # deep-research GAP
        r = self.audit()
        t = r["employees"]["tony"]
        self.assertEqual(t["stale"], {"mcp": ["dead"]})
        self.assertEqual(t["over_grant"], {"mcp": ["good"]})
        self.assertEqual(r["employees"]["mike"]["gap"], {"skills": ["deep-research"]})

    def test_hint_shields_needed_grant(self):
        self.set_mcp("playwright")
        _desk(self.company, "gibby", role="QA", mcp="[playwright]")
        g = self.audit()["employees"]["gibby"]
        self.assertEqual(g["over_grant"], {})
        self.assertEqual(g["stale"], {})

    def test_report_carries_completeness_context(self):
        self.set_mcp("good")
        _desk(self.company, "tony", mcp="[good, dead]")
        r = self.audit()
        self.assertIn("completeness", r)
        self.assertTrue(r["completeness"]["mcp"])       # authoritative config -> complete


# ============================================================ PROPOSE-ONLY (never mutates)
class TestProposeOnly(Base):
    def test_apply_never_mutates_context_stale(self):
        # A genuinely-stale grant, complete registry: STILL only proposed, not removed.
        self.set_mcp("good")
        before = _read(_desk(self.company, "tony", mcp="[good, dead]"))
        r = self.audit(apply=True)
        self.assertContextUnchanged("tony", before)
        self.assertEqual(Employee.load("tony", self.company).mcp, ["good", "dead"])
        self.assertEqual(r["employees"]["tony"]["stale"], {"mcp": ["dead"]})  # surfaced
        self.assertNotIn("auto_removed", r["employees"]["tony"])              # no such concept

    def test_gap_never_auto_granted(self):
        self.set_skills("deep-research")
        before = _read(_desk(self.company, "mike", role="R&D", skills="[]"))
        r = self.audit(apply=True)
        self.assertContextUnchanged("mike", before)
        self.assertNotIn("deep-research", Employee.load("mike", self.company).skills)
        self.assertEqual(r["employees"]["mike"]["gap"], {"skills": ["deep-research"]})

    def test_dry_run_writes_nothing(self):
        self.set_mcp("good")
        before = _read(_desk(self.company, "tony", mcp="[good, dead]"))
        r = self.audit(apply=False)
        self.assertContextUnchanged("tony", before)
        # no proposals file on a dry-run
        self.assertFalse(os.path.exists(
            os.path.join(self.company, "ops", "plans", "capability-audit-2026-07-07.md")))
        self.assertNotIn("proposals_path", r)

    def test_apply_writes_proposals_file_with_all_three_kinds(self):
        self.set_mcp("good")
        self.set_skills("deep-research")
        self.set_plugins("p1")
        _desk(self.company, "tony", mcp="[good, dead]")          # STALE + OVER-GRANT
        _desk(self.company, "mike", role="R&D", skills="[]")     # GAP
        r = self.audit(apply=True)
        body = _read(os.path.join(self.company, r["proposals_path"]))
        self.assertIn("STALE (propose removal)", body)
        self.assertIn("GAP (propose grant)", body)
        self.assertIn("OVER-GRANT (propose removal)", body)
        self.assertIn("July does NOT edit", body)               # propose-only banner

    def test_no_finding_writes_no_proposals_file(self):
        self.set_mcp("playwright")
        self.set_skills("deep-research")
        self.set_plugins("p1")
        _desk(self.company, "gibby", role="QA", mcp="[playwright]")
        _desk(self.company, "mike", role="R&D", skills="[deep-research]")
        r = self.audit(apply=True)
        self.assertEqual(r["summary"]["proposals_total"], 0)
        self.assertIsNone(r.get("proposals_path"))


# ============================================================ guardrails
class TestManagerBoundary(Base):
    def test_managers_never_audited_or_touched(self):
        self.set_mcp("good")
        b_elon = _read(_desk(self.company, "elon", role="CEO", mcp="[good, dead]"))
        b_phoebe = _read(_desk(self.company, "phoebe", role="PM", mcp="[dead]"))
        b_july = _read(_desk(self.company, "july", role="HR", mcp="[dead]"))
        r = self.audit(apply=True)
        for mgr, before in (("elon", b_elon), ("phoebe", b_phoebe), ("july", b_july)):
            self.assertFalse(r["employees"][mgr]["audited"], mgr)
            self.assertIn("manager boundary", r["employees"][mgr]["reason"])
            self.assertContextUnchanged(mgr, before)
        self.assertEqual(r["summary"]["managers_skipped"], ["elon", "july", "phoebe"])


class TestRedBluePair(Base):
    def test_red_blue_items_are_proposals_marked_human_review(self):
        self.set_mcp("playwright")
        self.set_plugins("keep")
        b_gibby = _read(_desk(self.company, "gibby", role="QA", mcp="[playwright, dead-mcp]"))
        b_bob = _read(_desk(self.company, "bob", role="Build", plugins="[keep, gone]"))
        r = self.audit(apply=True)
        self.assertTrue(r["employees"]["gibby"]["red_blue"])
        self.assertEqual(r["employees"]["gibby"]["stale"], {"mcp": ["dead-mcp"]})
        self.assertEqual(r["employees"]["bob"]["stale"], {"plugins": ["gone"]})
        self.assertEqual(r["employees"]["bob"]["over_grant"], {"plugins": ["keep"]})
        # NOT mutated
        self.assertContextUnchanged("gibby", b_gibby)
        self.assertContextUnchanged("bob", b_bob)
        # proposals mark red/blue human-review
        body = _read(os.path.join(self.company, r["proposals_path"]))
        self.assertIn("human review required", body)
        self.assertIn("dead-mcp", body)


# ============================================================ graceful degrade
class TestGracefulDegrade(Base):
    def test_absent_env_sources_are_unknown_and_skip(self):
        before = _read(_desk(self.company, "tony", mcp="[some-server]", skills="[some-skill]"))
        r = self.audit(apply=True)
        self.assertEqual(sorted(r["summary"]["unknown_dimensions"]),
                         ["mcp", "plugins", "skills"])
        self.assertEqual(r["employees"]["tony"]["stale"], {})   # nothing said on unknown
        self.assertContextUnchanged("tony", before)
        self.assertTrue(r["sources"]["mcp"].startswith("unknown"))

    def test_partial_unknown_still_classifies_known_dims(self):
        self.set_mcp("good")
        before = _read(_desk(self.company, "tony", mcp="[good, dead]", skills="[ghost]"))
        r = self.audit(apply=True)
        self.assertEqual(r["employees"]["tony"]["stale"], {"mcp": ["dead"]})   # mcp known -> proposed
        self.assertIn("skills", r["summary"]["unknown_dimensions"])            # skills unknown -> skipped
        self.assertContextUnchanged("tony", before)                            # still no mutation

    def test_missing_company_never_raises(self):
        rc, out, _ = run_script("july_audit.py", "--company",
                                os.path.join(self.tmp, "nope", ".company"))
        self.assertEqual(rc, 0)
        self.assertIn("no .company", out)

    def test_no_worker_desks_is_clean(self):
        r = self.audit(apply=True)
        self.assertEqual(r["summary"]["proposals_total"], 0)


# ============================================================ CLI + schema
class TestCLI(Base):
    def test_cli_dry_run_valid_json_no_write(self):
        self.set_mcp("good")
        before = _read(_desk(self.company, "tony", mcp="[good, dead]"))
        rc, out, _ = run_script("july_audit.py", "--company", self.company,
                                "--home", self.home, "--now", "2026-07-07")
        self.assertEqual(rc, 0)
        r = json.loads(out)
        self.assertEqual(r["schema"], "july-capability-audit/1")
        for key in ("available", "completeness", "sources", "employees", "summary", "generated"):
            self.assertIn(key, r)
        self.assertContextUnchanged("tony", before)

    def test_cli_apply_writes_proposals_and_july_log_but_no_context_edit(self):
        self.set_mcp("good")
        self.set_skills("deep-research")
        before = _read(_desk(self.company, "mike", role="R&D", skills="[]"))
        rc, out, _ = run_script("july_audit.py", "--company", self.company,
                                "--home", self.home, "--now", "2026-07-07", "--apply")
        self.assertEqual(rc, 0)
        r = json.loads(out)
        self.assertTrue(r["proposals_path"])
        self.assertTrue(os.path.exists(os.path.join(self.company, r["proposals_path"])))
        jlog = os.path.join(self.company, "org", "employees", "july", "log.md")
        with open(jlog, encoding="utf-8") as fh:
            self.assertIn("capability audit", fh.read())
        self.assertContextUnchanged("mike", before)


# ============================================================ P17-D1 / D2 fail-safe
class TestFailSafe(Base):
    """The break class that bit twice: a partial/empty/complete-but-non-enumerable
    availability view must never cause a legitimate grant to be removed. Under
    propose-only this reduces to ONE invariant — no mutation, ever — plus honest
    reporting so the human decides."""

    def test_D1_empty_skills_dir_no_removal(self):
        self.set_skills()                                        # present but empty
        before = _read(_desk(self.company, "mike", role="R&D", skills="[deep-research]"))
        r = self.audit(apply=True)
        self.assertContextUnchanged("mike", before)
        self.assertIn("skills", r["summary"]["unknown_dimensions"])  # empty -> unknown
        self.assertEqual(r["employees"]["mike"]["stale"], {})        # nothing even proposed

    def test_D1_one_unrelated_local_skill_no_removal(self):
        self.set_skills("unrelated-local-skill")                 # local only, no marketplace
        before = _read(_desk(self.company, "mike", role="R&D", skills="[deep-research]"))
        r = self.audit(apply=True)
        self.assertContextUnchanged("mike", before)
        self.assertFalse(r["completeness"]["skills"])            # local-only -> incomplete

    def test_D1_malformed_home_config_poisons_mcp_to_unknown(self):
        with open(os.path.join(self.home, ".claude.json"), "w", encoding="utf-8") as f:
            f.write("{ this is not valid json ")
        before = _read(_desk(self.company, "tony", mcp="[some-server]"))
        r = self.audit(apply=True)
        self.assertContextUnchanged("tony", before)
        self.assertIn("mcp", r["summary"]["unknown_dimensions"])
        self.assertIn("malformed", r["sources"]["mcp"])
        self.assertEqual(r["employees"]["tony"]["stale"], {})

    def test_D2_D3_skills_never_complete_and_stale_carries_caveat(self):
        # THE P17-D2/D3 fixture: even with a marketplace present, the skills view is
        # NEVER "complete" — bundled/first-party skills like deep-research aren't
        # enumerable on disk. So the deep-research STALE finding is (a) still only a
        # PROPOSAL, (b) mike's context.md untouched, and (c) the proposal MUST carry
        # the non-enumerable caveat, never mislabel it "(view COMPLETE)".
        self.set_plugins("some-marketplace-plugin")             # marketplace present
        self.set_skills("other-skill")                          # non-empty, but no deep-research
        before = _read(_desk(self.company, "mike", role="R&D", skills="[deep-research]"))
        r = self.audit(apply=True)
        self.assertFalse(r["completeness"]["skills"])           # P17-D3: NEVER complete
        self.assertEqual(r["employees"]["mike"]["stale"], {"skills": ["deep-research"]})  # proposed
        self.assertContextUnchanged("mike", before)             # but NEVER removed
        self.assertEqual(Employee.load("mike", self.company).skills, ["deep-research"])
        # proposal carries the correct non-enumerable signal (NOT "view COMPLETE")
        body = _read(os.path.join(self.company, r["proposals_path"]))
        self.assertIn("deep-research", body)
        self.assertIn("STALE (propose removal)", body)
        # the deep-research line specifically must warn the human it may be non-enumerable
        dr_line = next(l for l in body.splitlines()
                       if "deep-research" in l and "STALE" in l)
        self.assertIn("may be non-enumerable", dr_line)
        self.assertNotIn("view COMPLETE", dr_line)

    def test_genuinely_stale_still_proposed_not_removed(self):
        # Don't over-correct into silence: a genuinely-dead grant is still surfaced
        # (as a proposal) — just never auto-applied.
        self.set_mcp("good")
        before = _read(_desk(self.company, "tony", mcp="[good, dead]"))
        r = self.audit(apply=True)
        self.assertEqual(r["employees"]["tony"]["stale"], {"mcp": ["dead"]})
        self.assertContextUnchanged("tony", before)


# ============================================================ never fails the run
class TestNeverFails(Base):
    def test_malformed_context_does_not_crash(self):
        d = os.path.join(self.company, "org", "employees", "tony")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "context.md"), "w", encoding="utf-8") as f:
            f.write("not valid frontmatter at all\n:::\n")
        self.set_mcp("good")
        r = self.audit(apply=True)
        self.assertEqual(r["schema"], "july-capability-audit/1")
        self.assertTrue(r["employees"]["tony"]["audited"])

    def test_no_context_md_key_named_auto_removed_anywhere(self):
        # Structural: the propose-only report must not carry a mutation concept.
        self.set_mcp("good")
        _desk(self.company, "tony", mcp="[good, dead]")
        r = self.audit(apply=True)
        blob = json.dumps(r)
        self.assertNotIn("auto_removed", blob)
        self.assertNotIn("flagged_for_review", blob)


# ============================================================ Phase 29 Item 1
class TestModelTableWarnings(Base):
    """july_audit.py step 4: a non-empty `model:` that doesn't resolve through
    the alias map / claude-* passthrough is a WARN finding — never a gate, and
    it runs for EVERY roster employee (including managers)."""

    def _desk_with_model(self, name, model_value, role="Worker"):
        d = os.path.join(self.company, "org", "employees", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "context.md"), "w", encoding="utf-8") as f:
            f.write(f"---\nname: {name}\nrole: {role}\nmodel: {model_value}\n---\n"
                    f"{name}'s desk.\n")

    def test_valid_model_produces_no_warning(self):
        self._desk_with_model("tony", "sonnet")
        r = self.audit()
        self.assertEqual(r["model_warnings"], [])
        self.assertEqual(r["summary"]["model_warnings_total"], 0)

    def test_bad_model_surfaces_warn_naming_employee_and_value(self):
        self._desk_with_model("bob", "haiku → sonnet")
        r = self.audit()
        self.assertEqual(len(r["model_warnings"]), 1)
        finding = r["model_warnings"][0]
        self.assertEqual(finding["employee"], "bob")
        self.assertEqual(finding["value"], "haiku → sonnet")
        self.assertIn("bob", finding["warning"])
        self.assertEqual(r["summary"]["model_warnings_total"], 1)

    def test_bad_model_is_a_finding_not_a_gate(self):
        # The audit still completes normally (rc 0, no exception) — a bad
        # model value never blocks the report.
        self._desk_with_model("bob", "haiku → sonnet")
        rc, out, _ = run_script("july_audit.py", "--company", self.company,
                                "--home", self.home, "--now", "2026-07-10")
        self.assertEqual(rc, 0)
        r = json.loads(out)
        self.assertEqual(r["model_warnings"][0]["employee"], "bob")

    def test_manager_model_also_checked(self):
        # Model warnings are NOT gated by the manager boundary (capability
        # audit's manager skip does not apply here — model is execution config).
        self._desk_with_model("elon", "gpt-4", role="CEO")
        r = self.audit()
        self.assertEqual(r["employees"]["elon"]["audited"], False)   # capability audit still skips elon
        self.assertEqual(r["model_warnings"][0]["employee"], "elon")  # model check does not

    def test_no_desks_no_warnings(self):
        r = self.audit()
        self.assertEqual(r["model_warnings"], [])


if __name__ == "__main__":
    unittest.main()
