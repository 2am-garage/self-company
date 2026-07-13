"""
Tests for employee.discover() — Phase 32 Item 1 (hire-as-data: dynamic
employee discovery, single source stays single).

The load-bearing invariant: ZERO-DESK BYTE-IDENTITY — a company with no hired
desks must discover EXACTLY CORE_EMPLOYEES, in the SAME order, so every
consumer of this seam (schedule_config, schedule_validator, roster_md,
--explain) is byte-identical to pre-Phase-32 behavior. This file proves that
FIRST, then covers the new discovery behavior: a valid hired desk appears,
core ids are never shadowed, and the id charset is enforced (`../evil`,
uppercase, a 25-char id, an underscore) with BOTH files required.
"""

import os
import shutil
import tempfile
import unittest

import _helpers  # noqa: F401  (puts scripts/ on sys.path)

import employee
from employee import CORE_EMPLOYEES, EMPLOYEES, Employee, discover


def _mkdesk(base, name, persona=True, context=True):
    d = os.path.join(base, "org", "employees", name)
    os.makedirs(d, exist_ok=True)
    if persona:
        with open(os.path.join(d, "persona.md"), "w", encoding="utf-8") as f:
            f.write("persona\n")
    if context:
        with open(os.path.join(d, "context.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: X\n---\nbody\n")
    return d


class TestCoreAliasUnchanged(unittest.TestCase):
    """EMPLOYEES stays a working alias of CORE_EMPLOYEES — every pre-Phase-32
    importer of `EMPLOYEES` keeps working byte-for-byte."""

    def test_employees_is_core_employees(self):
        self.assertEqual(
            EMPLOYEES, ("tony", "gibby", "bob", "mike", "elon", "phoebe", "tom", "july"))
        self.assertEqual(EMPLOYEES, CORE_EMPLOYEES)

    def test_roster_unaffected(self):
        # Employee.roster() (no args) stays the fixed 8 — it is not the
        # company-dir-aware seam (that is discover()).
        self.assertEqual(
            Employee.roster(),
            ["tony", "gibby", "bob", "mike", "elon", "phoebe", "tom", "july"])


class TestZeroDeskByteIdentity(unittest.TestCase):
    """The headline invariant: nobody hired -> discover() == CORE_EMPLOYEES,
    same tuple contents AND same order, for every shape of "nobody hired"."""

    def test_no_org_dir_at_all(self):
        self.assertEqual(discover("/no/such/company/at/all"), CORE_EMPLOYEES)

    def test_none_company_dir(self):
        self.assertEqual(discover(None), CORE_EMPLOYEES)

    def test_empty_employees_dir(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "org", "employees"))
            self.assertEqual(discover(tmp), CORE_EMPLOYEES)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_only_core_desks_present(self):
        tmp = tempfile.mkdtemp()
        try:
            for name in CORE_EMPLOYEES:
                _mkdesk(tmp, name)
            self.assertEqual(discover(tmp), CORE_EMPLOYEES)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_real_shipped_template_is_core_only(self):
        template = os.path.join(
            _helpers.REPO_ROOT, "plugin", "skills", "self-company",
            "assets", "company-template")
        self.assertEqual(discover(template), CORE_EMPLOYEES)

    def test_returns_a_tuple(self):
        self.assertIsInstance(discover("/no/such/company"), tuple)


class TestDiscoverHiredDesks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_hired_desk_appended_after_core(self):
        _mkdesk(self.tmp, "sam-jr")
        got = discover(self.tmp)
        self.assertEqual(got[:8], CORE_EMPLOYEES)
        self.assertEqual(got[8:], ("sam-jr",))

    def test_multiple_hired_desks_sorted(self):
        _mkdesk(self.tmp, "zed")
        _mkdesk(self.tmp, "amy")
        got = discover(self.tmp)
        self.assertEqual(got[8:], ("amy", "zed"))

    def test_core_id_directory_never_shadows_or_duplicates(self):
        # A directory literally named "elon" (even with both files) is simply
        # ignored by discover() — core always wins, never shadowed, never
        # appended a second time.
        _mkdesk(self.tmp, "elon")
        got = discover(self.tmp)
        self.assertEqual(got, CORE_EMPLOYEES)
        self.assertEqual(got.count("elon"), 1)

    def test_missing_context_md_not_discovered(self):
        _mkdesk(self.tmp, "half-baked", persona=True, context=False)
        self.assertNotIn("half-baked", discover(self.tmp))

    def test_missing_persona_md_not_discovered(self):
        _mkdesk(self.tmp, "half-baked", persona=False, context=True)
        self.assertNotIn("half-baked", discover(self.tmp))

    def test_stray_file_under_employees_ignored(self):
        base = os.path.join(self.tmp, "org", "employees")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "not-a-dir.md"), "w") as f:
            f.write("x")
        self.assertEqual(discover(self.tmp), CORE_EMPLOYEES)


class TestDiscoverCharsetDefenseInDepth(unittest.TestCase):
    """ID charset ^[a-z][a-z0-9-]{1,23}$ enforced HERE too (defense in depth
    alongside hire.sh) — a hand-crafted evil desk dir must be ignored, even
    with both files present, even if it LOOKS like a path-traversal name."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _try(self, name):
        _mkdesk(self.tmp, name)
        self.assertNotIn(name, discover(self.tmp))

    def test_uppercase_rejected(self):
        self._try("BadCase")

    def test_leading_digit_rejected(self):
        self._try("1abc")

    def test_underscore_rejected(self):
        self._try("bad_case")

    def test_too_short_single_char_rejected(self):
        self._try("a")

    def test_25_char_id_rejected(self):
        self._try("a" * 25)

    def test_24_char_id_accepted(self):
        name = "a" * 24
        _mkdesk(self.tmp, name)
        self.assertIn(name, discover(self.tmp))

    def test_leading_hyphen_rejected(self):
        self._try("-abc")

    def test_dotdot_traversal_shaped_name_never_created_but_would_be_rejected(self):
        # os.mkdir can't literally create "../evil" as a plain child entry (it
        # would escape the dir); iterdir() never yields such a name in the
        # first place. The charset regex would reject it anyway if it somehow
        # appeared (defense in depth, not reliance on iterdir alone).
        import re
        self.assertIsNone(employee._DESK_ID_RE.match("../evil"))


if __name__ == "__main__":
    unittest.main()
