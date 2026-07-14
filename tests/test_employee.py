"""
Tests for employee.py — the Phase 16 data-driven Employee model.

Two fixtures:
  * the SHIPPED company-template (read-only) — real context.md frontmatter, so we
    test identity + capability parsing against the actual eight desks;
  * a temp company with a schedule.yaml — for should_run/duties/cadence parity
    with schedule_config, and for graceful-default behaviour.

The load-bearing invariants:
  * ONE class, eight data instances (roster) — no per-employee subclass.
  * every accessor resolves from the desk / the fixed Layer-B tables;
  * allows_duty / owns_step / should_run match today's schedule_config exactly;
  * a missing desk / field degrades to a sensible default and NEVER raises.
"""

import os
import shutil
import tempfile
import unittest

import _helpers  # noqa: F401  (puts scripts/ on sys.path)

import employee
from employee import Employee
import schedule_config as sc

TEMPLATE = os.path.join(
    _helpers.REPO_ROOT,
    "plugin", "skills", "self-company", "assets", "company-template",
)


def _make_company(tmp, body=None):
    """Temp .company with an optional schedule.yaml body."""
    org = os.path.join(tmp, ".company", "org")
    os.makedirs(org, exist_ok=True)
    cfg = os.path.join(org, "schedule.yaml")
    if body is None:
        if os.path.exists(cfg):
            os.remove(cfg)
    else:
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(body)
    return os.path.join(tmp, ".company")


# ------------------------------------------------------------------- roster / model
class TestRosterAndModel(unittest.TestCase):
    def test_roster_is_the_eight(self):
        self.assertEqual(
            Employee.roster(),
            ["tony", "gibby", "bob", "mike", "elon", "phoebe", "tom", "july"],
        )

    def test_one_class_no_subclass(self):
        # Every roster member is an instance of the SAME class — a subclass per
        # employee would be the design break this phase exists to prevent.
        for name in Employee.roster():
            e = Employee.load(name, TEMPLATE)
            self.assertIs(type(e), Employee, name)

    def test_load_is_case_insensitive(self):
        self.assertEqual(Employee.load("Bob", TEMPLATE).name, "bob")


# ------------------------------------------------------------------- identity
class TestIdentity(unittest.TestCase):
    def test_bob_identity(self):
        e = Employee.load("bob", TEMPLATE)
        self.assertEqual(e.name, "bob")
        self.assertEqual(e.display_name, "Bob")
        self.assertEqual(e.role, "Build Engineer")
        self.assertEqual(e.manager, "Phoebe")
        self.assertEqual(e.people_lead, "July")
        self.assertEqual(e.tier, "worker")

    def test_inline_comment_stripped_from_scalar(self):
        # bob's manager line carries a trailing `# dispatch source...` comment.
        self.assertEqual(Employee.load("bob", TEMPLATE).manager, "Phoebe")

    def test_tiers(self):
        self.assertEqual(Employee.load("elon", TEMPLATE).tier, "manager")
        self.assertEqual(Employee.load("phoebe", TEMPLATE).tier, "manager")
        self.assertEqual(Employee.load("july", TEMPLATE).tier, "lead")
        for w in ("tony", "gibby", "bob", "mike", "tom"):
            self.assertEqual(Employee.load(w, TEMPLATE).tier, "worker", w)

    def test_null_people_lead_is_none(self):
        # phoebe: `people_lead: null`, elon: `people_lead: ~` -> both None.
        self.assertIsNone(Employee.load("phoebe", TEMPLATE).people_lead)
        self.assertIsNone(Employee.load("elon", TEMPLATE).people_lead)

    def test_every_employee_has_role_and_manager(self):
        for name in Employee.roster():
            e = Employee.load(name, TEMPLATE)
            self.assertTrue(e.role, name)
            self.assertTrue(e.manager, name)


# ------------------------------------------------------------------- capabilities
class TestCapabilities(unittest.TestCase):
    def test_bob_capability_slice(self):
        e = Employee.load("bob", TEMPLATE)
        self.assertEqual(e.tools, ["Read", "Edit", "Write", "Bash"])
        self.assertIn("org/employees/bob/scratchpad.md", e.writes)
        self.assertTrue(any("org/employees/bob/" in r for r in e.reads))
        self.assertEqual(e.handoff_to, ["Gibby"])   # scalar -> one-item list

    def test_capabilities_dict_has_four_functional_dimensions(self):
        # Phase 17: capabilities() is the FUNCTIONAL profile July stewards —
        # tools/mcp/skills/plugins. (reads/writes/handoff_to are a separate
        # data-access slice, reachable via attributes, not in capabilities().)
        e = Employee.load("bob", TEMPLATE)
        caps = e.capabilities()
        self.assertEqual(set(caps), {"tools", "mcp", "skills", "plugins"})
        self.assertEqual(caps["tools"], ["Read", "Edit", "Write", "Bash"])
        self.assertEqual(caps["mcp"], [])          # least-privilege default
        # reads/writes/handoff_to remain accessible as attributes (separate slice):
        self.assertIn("org/employees/bob/scratchpad.md", e.writes)

    def test_list_handoff_to_preserved(self):
        # tony's handoff_to is a YAML sequence -> list of names.
        e = Employee.load("tony", TEMPLATE)
        self.assertIn("Elon", e.handoff_to)
        self.assertIn("Gibby", e.handoff_to)
        self.assertIn("Phoebe", e.handoff_to)

    def test_gibby_mcp_tools_parsed(self):
        e = Employee.load("gibby", TEMPLATE)
        self.assertIn("Read", e.tools)
        self.assertIn("Bash", e.tools)
        self.assertIn("mcp__playwright__browser_navigate", e.tools)

    def test_capabilities_is_a_copy(self):
        e = Employee.load("bob", TEMPLATE)
        e.capabilities()["tools"].append("X")
        self.assertNotIn("X", e.tools)   # mutating the returned dict must not leak


# ------------------------------------------------------------------- execution
class TestExecution(unittest.TestCase):
    def test_model_from_context(self):
        self.assertEqual(Employee.load("tony", TEMPLATE).model, "sonnet")

    def test_model_from_context_bob(self):
        # Phase 29 Item 1: bob's template model: is now a plain resolvable alias
        # (was prose "haiku → sonnet" before the model table was wired up).
        self.assertEqual(Employee.load("bob", TEMPLATE).model, "haiku")

    def test_model_pinned_literal_id_phoebe(self):
        # Phoebe is Chairman-pinned to an exact claude-* id, not an alias.
        self.assertEqual(Employee.load("phoebe", TEMPLATE).model, "claude-sonnet-4-6")

    def test_model_elon_fable(self):
        self.assertEqual(Employee.load("elon", TEMPLATE).model, "fable")

    def test_duties_default_to_role_set(self):
        c = _make_company(tempfile.mkdtemp())
        try:
            self.assertEqual(sorted(Employee.load("gibby", c).duties),
                             ["attack", "verify"])
        finally:
            shutil.rmtree(os.path.dirname(os.path.dirname(c)), ignore_errors=True)

    def test_cadence_default_every_run(self):
        c = _make_company(tempfile.mkdtemp())
        try:
            self.assertEqual(Employee.load("tony", c).cadence, "every-run")
        finally:
            shutil.rmtree(os.path.dirname(os.path.dirname(c)), ignore_errors=True)

    def test_duties_and_cadence_reflect_config(self):
        tmp = tempfile.mkdtemp()
        try:
            c = _make_company(tmp, "tony: { cadence: daily, duties: [decay] }\n")
            e = Employee.load("tony", c)
            self.assertEqual(e.cadence, "daily")
            self.assertEqual(e.duties, ["decay"])
            self.assertTrue(e.enabled)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_disabled_reflected(self):
        tmp = tempfile.mkdtemp()
        try:
            c = _make_company(tmp, "elon: { enabled: false }\n")
            self.assertFalse(Employee.load("elon", c).enabled)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------- Phase 29 Item 1 resolved_model
class TestResolvedModel(unittest.TestCase):
    """Employee.resolved_model — the Chairman's adjustable-with-default model
    table. Four required behaviors: unset -> default SILENTLY; alias -> real id;
    claude-* -> verbatim; anything else -> default WITH a warning. Plus Gibby's
    argv-smuggle fuzz set: a resolved model is ALWAYS one safe argv token."""

    DEFAULT = "claude-sonnet-5"

    def _emp(self, model_value):
        return employee.Employee("x", "/no/such/company",
                                 fm={"model": model_value} if model_value is not None else {})

    def test_unset_is_silent_default(self):
        model, warning = self._emp(None).resolved_model(self.DEFAULT)
        self.assertEqual(model, self.DEFAULT)
        self.assertIsNone(warning)

    def test_blank_is_silent_default(self):
        model, warning = self._emp("   ").resolved_model(self.DEFAULT)
        self.assertEqual(model, self.DEFAULT)
        self.assertIsNone(warning)

    def test_haiku_alias(self):
        model, warning = self._emp("haiku").resolved_model(self.DEFAULT)
        self.assertEqual(model, "claude-haiku-4-5")
        self.assertIsNone(warning)

    def test_opus_alias(self):
        model, warning = self._emp("opus").resolved_model(self.DEFAULT)
        self.assertEqual(model, "claude-opus-4-8")
        self.assertIsNone(warning)

    def test_fable_alias(self):
        model, warning = self._emp("fable").resolved_model(self.DEFAULT)
        self.assertEqual(model, "claude-fable-5")
        self.assertIsNone(warning)

    def test_sonnet_alias_resolves_to_the_caller_default(self):
        # `sonnet` deliberately has no fixed id of its own — it IS the default,
        # so bumping the Item-2 default constant moves it with zero rework.
        model, warning = self._emp("sonnet").resolved_model(self.DEFAULT)
        self.assertEqual(model, self.DEFAULT)
        self.assertIsNone(warning)
        model2, _ = self._emp("sonnet").resolved_model("claude-sonnet-4-6")
        self.assertEqual(model2, "claude-sonnet-4-6")

    def test_alias_case_insensitive(self):
        model, warning = self._emp("Haiku").resolved_model(self.DEFAULT)
        self.assertEqual(model, "claude-haiku-4-5")
        self.assertIsNone(warning)

    def test_claude_star_passthrough_verbatim(self):
        model, warning = self._emp("claude-sonnet-4-6").resolved_model(self.DEFAULT)
        self.assertEqual(model, "claude-sonnet-4-6")
        self.assertIsNone(warning)

    def test_claude_star_dated_snapshot_passthrough(self):
        model, warning = self._emp("claude-haiku-4-5-20251001").resolved_model(self.DEFAULT)
        self.assertEqual(model, "claude-haiku-4-5-20251001")
        self.assertIsNone(warning)

    def test_multiline_prose_degrades_with_warning(self):
        # The live bad value cited by the spec: "haiku → sonnet".
        model, warning = self._emp("haiku → sonnet").resolved_model(self.DEFAULT)
        self.assertEqual(model, self.DEFAULT)
        self.assertIsNotNone(warning)
        self.assertIn("x", warning)
        self.assertIn("haiku → sonnet", warning)

    def test_unrecognized_word_degrades_with_warning(self):
        model, warning = self._emp("gpt-4").resolved_model(self.DEFAULT)
        self.assertEqual(model, self.DEFAULT)
        self.assertIsNotNone(warning)

    # ---- Gibby's fuzz set: argv-smuggle-proof, one token, never a crash -----
    def test_bare_claude_prefix_degrades(self):
        model, warning = self._emp("claude-").resolved_model(self.DEFAULT)
        self.assertEqual(model, self.DEFAULT)
        self.assertIsNotNone(warning)

    def test_injection_shaped_value_never_smuggles_argv(self):
        model, warning = self._emp(
            "sonnet --dangerously-skip-permissions").resolved_model(self.DEFAULT)
        self.assertEqual(model, self.DEFAULT)
        self.assertIsNotNone(warning)
        self.assertNotIn(" ", model)

    def test_claude_star_with_shell_metacharacters_rejected(self):
        for bad in ("claude-sonnet-5; rm -rf /", "claude-sonnet-5\ncat /etc/passwd",
                    "claude-sonnet-5 --dangerously-skip-permissions",
                    "claude-sonnet-5`whoami`", "claude-sonnet-5$(whoami)"):
            model, warning = self._emp(bad).resolved_model(self.DEFAULT)
            self.assertEqual(model, self.DEFAULT, bad)
            self.assertIsNotNone(warning, bad)

    def test_yaml_list_value_degrades(self):
        # frontmatter's flow-list parser turns `model: [haiku, sonnet]` into an
        # actual list; the raw field ends up a non-empty non-alias string.
        model, warning = self._emp(["haiku", "sonnet"]).resolved_model(self.DEFAULT)
        self.assertEqual(model, self.DEFAULT)
        self.assertIsNotNone(warning)

    def test_quoted_fragment_degrades(self):
        model, warning = self._emp('"claude-"').resolved_model(self.DEFAULT)
        self.assertEqual(model, self.DEFAULT)
        self.assertIsNotNone(warning)

    def test_resolved_model_never_raises_on_any_input(self):
        for bad in (None, "", " ", "haiku → sonnet", ["a", "b"], "claude-", "\n\n",
                   "sonnet;bob", "claude-sonnet-5" * 50):
            try:
                model, warning = self._emp(bad).resolved_model(self.DEFAULT)
            except Exception as e:                        # pragma: no cover
                self.fail(f"resolved_model raised on {bad!r}: {e}")
            self.assertTrue(model)

    def test_never_returns_more_than_one_argv_token(self):
        # Every resolvable outcome (alias or claude-* passthrough) must be
        # whitespace-free — a single --model argv slot, never a smuggled second
        # token, regardless of how the raw frontmatter value was crafted.
        candidates = ["haiku", "opus", "fable", "sonnet", "claude-opus-4-8",
                     "gpt-4", "haiku → sonnet", "sonnet --dangerously-skip-permissions",
                     "claude-sonnet-5;rm -rf /"]
        for raw in candidates:
            model, _ = self._emp(raw).resolved_model(self.DEFAULT)
            self.assertEqual(len(model.split()), 1, raw)


# ------------------------------------------------------------------- paths
class TestPaths(unittest.TestCase):
    def test_desk_paths(self):
        e = Employee.load("bob", TEMPLATE)
        self.assertTrue(str(e.desk_dir).endswith("org/employees/bob"))
        self.assertEqual(e.persona_path, e.desk_dir / "persona.md")
        self.assertEqual(e.context_path, e.desk_dir / "context.md")
        self.assertEqual(e.log_path, e.desk_dir / "log.md")
        self.assertEqual(e.scratchpad_path, e.desk_dir / "scratchpad.md")
        self.assertTrue(e.persona_path.exists())   # template really has it

    def test_memory_dir_reserved(self):
        # Reserved accessor for the later per-employee-memory idea; it is a Path,
        # not built — nothing creates it.
        e = Employee.load("bob", TEMPLATE)
        self.assertEqual(e.memory_dir, e.desk_dir / "memory")
        self.assertFalse(e.memory_dir.exists())


# ------------------------------------------------------------------- duties/steps
class TestDutiesAndSteps(unittest.TestCase):
    def test_allows_duty(self):
        self.assertTrue(Employee.load("gibby", TEMPLATE).allows_duty("attack"))
        self.assertTrue(Employee.load("gibby", TEMPLATE).allows_duty("verify"))
        self.assertFalse(Employee.load("gibby", TEMPLATE).allows_duty("build"))
        self.assertTrue(Employee.load("bob", TEMPLATE).allows_duty("build"))
        self.assertFalse(Employee.load("bob", TEMPLATE).allows_duty("attack"))
        self.assertFalse(Employee.load("phoebe", TEMPLATE).allows_duty("build"))
        # Phase 17: July is the capability steward — she owns july_audit, nothing else.
        self.assertTrue(Employee.load("july", TEMPLATE).allows_duty("july_audit"))
        self.assertFalse(Employee.load("july", TEMPLATE).allows_duty("build"))
        self.assertFalse(Employee.load("tony", TEMPLATE).allows_duty("july_audit"))

    def test_allows_duty_matches_table(self):
        for name in Employee.roster():
            e = Employee.load(name, TEMPLATE)
            for duty in ("attack", "build", "verify", "decay", "research", "survey",
                         "backup", "report", "schedule", "reinforce", "entropy",
                         "rag_index", "propose", "agent", "july_audit"):
                self.assertEqual(e.allows_duty(duty),
                                 duty in employee.ALLOWED_DUTIES[name],
                                 f"{name}.{duty}")

    def test_owns_step(self):
        self.assertTrue(Employee.load("gibby", TEMPLATE).owns_step("verify"))
        self.assertTrue(Employee.load("tom", TEMPLATE).owns_step("backup"))
        self.assertTrue(Employee.load("tony", TEMPLATE).owns_step("rag_index"))
        self.assertFalse(Employee.load("bob", TEMPLATE).owns_step("verify"))
        self.assertFalse(Employee.load("tony", TEMPLATE).owns_step("backup"))

    def test_owns_step_matches_table(self):
        for step, owner in employee.STEP_OWNER.items():
            for name in Employee.roster():
                self.assertEqual(Employee.load(name, TEMPLATE).owns_step(step),
                                 name == owner, f"{name}.{step}")


# --------------------------------------------------- Phase 34: duty -> tool profile
class TestToolProfiles(unittest.TestCase):
    """Item 1: the duty->tool-profile table (Layer B, code-locked next to
    ALLOWED_DUTIES). The single security invariant: only bob (build) and tom
    (infra) may mutate source; every other dispatched worker gets NO mutation
    tools (Bash/Write/Edit/NotebookEdit) at all."""

    def test_bob_is_build_unrestricted(self):
        self.assertEqual(employee.tool_profile_for("bob"), "build")
        self.assertEqual(employee.disallowed_tools_for("bob"), ())

    def test_tom_is_infra_unrestricted(self):
        self.assertEqual(employee.tool_profile_for("tom"), "infra")
        self.assertEqual(employee.disallowed_tools_for("tom"), ())

    def test_every_other_core_employee_is_restricted(self):
        for name in ("gibby", "tony", "mike", "elon", "phoebe", "july"):
            self.assertEqual(employee.tool_profile_for(name), "restricted", name)
            tools = employee.disallowed_tools_for(name)
            for mutating in ("Bash", "Write", "Edit", "NotebookEdit"):
                self.assertIn(mutating, tools, f"{name} must deny {mutating}")

    def test_gibby_does_not_get_bash_despite_attack_duty(self):
        # Phase 34 spike finding: Bash command-pattern scoping does not stop
        # shell chaining, so it cannot be safely granted even to the
        # attack/QA role — a deliberate deviation from the original draft.
        self.assertIn("Bash", employee.disallowed_tools_for("gibby"))

    def test_only_build_and_infra_may_mutate_source(self):
        # The single security invariant, asserted directly against the whole
        # core roster: exactly bob and tom have an EMPTY disallow list
        # (unrestricted); everyone else denies all four mutating tools.
        mutators = [n for n in employee.CORE_EMPLOYEES
                    if employee.disallowed_tools_for(n) == ()]
        self.assertEqual(sorted(mutators), ["bob", "tom"])

    def test_unknown_duty_or_hired_desk_is_most_restrictive(self):
        for bogus in ("", None, "zzz-not-a-real-employee", "../../etc",
                      "hired-researcher"):
            self.assertEqual(employee.tool_profile_for(bogus), "restricted", repr(bogus))
            tools = employee.disallowed_tools_for(bogus)
            for mutating in ("Bash", "Write", "Edit", "NotebookEdit"):
                self.assertIn(mutating, tools, repr(bogus))

    def test_hired_desk_never_resolves_to_build_or_infra(self):
        # A hired worker/manager desk (R7 forbids it from holding a build/
        # attack duty in the first place) must never fall through to an
        # unrestricted profile just because its name isn't recognized.
        for hired_name in ("zara", "quinn-analyst", "newhire"):
            self.assertNotEqual(employee.tool_profile_for(hired_name), "build")
            self.assertNotEqual(employee.tool_profile_for(hired_name), "infra")
            self.assertNotEqual(employee.disallowed_tools_for(hired_name), ())

    def test_disallowed_tools_for_always_returns_tuple_or_list_never_raises(self):
        for weird in (123, [], {}, object()):
            try:
                result = employee.disallowed_tools_for(weird)
            except Exception as e:  # pragma: no cover - must never happen
                self.fail(f"disallowed_tools_for({weird!r}) raised {e!r}")
            self.assertIsInstance(result, (tuple, list))

    def test_case_insensitive_lookup(self):
        self.assertEqual(employee.tool_profile_for("BOB"), "build")
        self.assertEqual(employee.tool_profile_for("Bob"), "build")
        self.assertEqual(employee.tool_profile_for("  bob  "), "build")

    def test_core_tool_profiles_covers_every_core_employee(self):
        for name in employee.CORE_EMPLOYEES:
            self.assertIn(name, employee.CORE_TOOL_PROFILES, name)


# ------------------------------------------------------------------- should_run parity
class TestShouldRunParity(unittest.TestCase):
    """Employee.should_run must match schedule_config.should_run everywhere — the
    model routes the exact same verdict the CLI has always produced."""

    CONFIGS = [
        None,
        "tony: { cadence: every-run }\n",
        "elon: { cadence: daily }\n",
        "elon: { cadence: weekly }\n",
        "tony: { cadence: on-trigger }\n",
        "tony: { cadence: every-2 }\n",
        "tony: { duties: [decay] }\n",
        "elon: { enabled: false }\n",
        "gibby: { duties: [] }\n",
    ]
    STEPS = ["backup", "reinforce", "decay", "verify", "entropy", "rag_index",
             "survey", "report", "agent", "build"]

    def test_parity_grid(self):
        tmp = tempfile.mkdtemp()
        try:
            for body in self.CONFIGS:
                c = _make_company(tmp, body)
                for step in self.STEPS:
                    owner = employee.STEP_OWNER.get(step)
                    for hour in (0, 3, 6, 12, 13):
                        for dow in (0, 2, 3, 4):
                            want = sc.should_run(c, step, hour, dow)
                            if owner is None:
                                # unowned -> every employee fails open to True
                                got = Employee.load("bob", c).should_run(step, hour, dow)
                            else:
                                got = Employee.load(owner, c).should_run(step, hour, dow)
                            self.assertEqual(got, want,
                                             f"{body!r} {step} h{hour} d{dow}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_non_owner_does_not_suppress(self):
        # A step this employee doesn't own -> fail open (never suppresses).
        c = _make_company(tempfile.mkdtemp())
        try:
            self.assertTrue(Employee.load("bob", c).should_run("verify", 3, 2))
        finally:
            shutil.rmtree(os.path.dirname(os.path.dirname(c)), ignore_errors=True)


# ------------------------------------------------------------------- log
class TestLog(unittest.TestCase):
    def test_log_appends_and_creates(self):
        tmp = tempfile.mkdtemp()
        try:
            c = os.path.join(tmp, ".company")
            e = Employee.load("bob", c)             # desk does not exist yet
            self.assertTrue(e.log("first entry"))
            self.assertTrue(e.log("second entry\n"))
            text = e.log_path.read_text(encoding="utf-8")
            self.assertEqual(text, "first entry\nsecond entry\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------- graceful defaults
class TestGracefulDefaults(unittest.TestCase):
    def test_missing_company_never_raises(self):
        e = Employee.load("bob", "/no/such/company/at/all")
        self.assertEqual(e.name, "bob")
        self.assertEqual(e.display_name, "Bob")
        self.assertEqual(e.role, "")
        self.assertEqual(e.manager, "")
        self.assertIsNone(e.people_lead)
        self.assertEqual(e.tools, [])
        self.assertEqual(e.mcp, [])
        self.assertEqual(e.skills, [])
        self.assertEqual(e.plugins, [])
        self.assertEqual(e.reads, [])
        self.assertEqual(e.writes, [])
        self.assertEqual(e.handoff_to, [])
        self.assertEqual(e.model, "")
        self.assertIsNone(e.token_budget)
        self.assertEqual(e.capabilities(),
                         {"tools": [], "mcp": [], "skills": [], "plugins": []})

    def test_unknown_name_defaults(self):
        e = Employee.load("nobody", TEMPLATE)
        self.assertEqual(e.name, "nobody")
        self.assertEqual(e.tier, "worker")           # default tier
        self.assertFalse(e.allows_duty("build"))     # no fixed role set
        self.assertEqual(e.duties, [])               # not in effective employees

    def test_should_run_unknown_owner_fails_open(self):
        # A step whose owner isn't a real desk still fails open (never raises).
        e = Employee.load("nobody", "/no/such/company")
        self.assertTrue(e.should_run("build", 3, 2))     # unowned -> True


if __name__ == "__main__":
    unittest.main()
