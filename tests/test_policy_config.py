"""
Tests for policy_config — the shared tunable-constant loader.

This is the regression net for upgrade P1: policy.md §7 declares constants in
markdown TABLE form, which the old per-script parsers could not read. These tests
lock in that the table form (and the inline fallback) is parsed correctly, and
that provenance via resolve() is reported accurately.
"""

import os
import tempfile
import unittest

import _helpers  # noqa: F401 — sets sys.path
import policy_config

REPO_ROOT = _helpers.REPO_ROOT
REAL_POLICY = os.path.join(REPO_ROOT, "assets", "company-template", "org", "policy.md")


class TestRealPolicy(unittest.TestCase):
    """The shipped policy.md must parse to its documented §7 values."""

    EXPECTED = {
        "HL_BASE": 7.0, "HL_GROWTH": 0.5,
        "L0_DROP_THRESHOLD": 0.25, "L1_ARCHIVE_THRESHOLD": 0.15,
        "L1_DEMOTE_RC": 2, "L0_TO_L1_RC": 2, "L1_TO_L2_RC": 4,
        "w1": 0.25, "w2": 0.35, "w3": 0.20, "w4": 0.20,
        "DUP_JACCARD": 0.8, "VERIFY_MAX_RETRY": 2, "RAG_ENABLE_THRESHOLD": 50,
    }

    def test_all_constants_parsed_from_shipped_policy(self):
        parsed = policy_config.load_policy_constants(REAL_POLICY)
        for k, v in self.EXPECTED.items():
            self.assertIn(k, parsed, f"{k} not parsed from shipped policy.md")
            self.assertEqual(parsed[k], v, f"{k}: got {parsed[k]!r}, want {v!r}")

    def test_int_constants_are_ints(self):
        parsed = policy_config.load_policy_constants(REAL_POLICY)
        for k in ("L1_DEMOTE_RC", "L0_TO_L1_RC", "L1_TO_L2_RC",
                  "VERIFY_MAX_RETRY", "RAG_ENABLE_THRESHOLD"):
            self.assertIsInstance(parsed[k], int)


class TestTableParsing(unittest.TestCase):
    """Table-row format must be honoured (the core of P1)."""

    def _parse(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            return policy_config.load_policy_constants(path)
        finally:
            os.unlink(path)

    def test_bold_value_in_table_cell(self):
        text = "| `HL_BASE` | **3.5** days | half-life | ✓ |\n"
        self.assertEqual(self._parse(text)["HL_BASE"], 3.5)

    def test_value_taken_from_value_cell_not_description(self):
        # Description column mentions another number; must not be picked up.
        text = "| `L0_DROP_THRESHOLD` | **0.42** | ~14 days no reinforce | drop | ✓ |\n"
        self.assertEqual(self._parse(text)["L0_DROP_THRESHOLD"], 0.42)

    def test_description_mentioning_other_constant_does_not_bleed(self):
        # HL_GROWTH row mentions HL_BASE in its description; HL_BASE must NOT be
        # read from this row (it is anchored to the first cell only).
        text = (
            "| `HL_GROWTH` | **0.5** | extends half-life by 50% of HL_BASE | ✓ |\n"
        )
        parsed = self._parse(text)
        self.assertEqual(parsed["HL_GROWTH"], 0.5)
        self.assertNotIn("HL_BASE", parsed)

    def test_inline_fallback(self):
        self.assertEqual(self._parse("HL_BASE = 9.0\n")["HL_BASE"], 9.0)
        self.assertEqual(self._parse("HL_BASE: 9.0\n")["HL_BASE"], 9.0)

    def test_negative_value_rejected(self):
        # Regression (red/blue R2): "-5.0" must NOT become 5.0; reject -> absent.
        parsed = self._parse("| `HL_BASE` | **-5.0** days | x | ✓ |\n")
        self.assertNotIn("HL_BASE", parsed)

    def test_comma_grouped_value_rejected(self):
        # Regression (red/blue R2): "20,000" must NOT become 20; reject -> absent.
        parsed = self._parse("| `RAG_ENABLE_THRESHOLD` | **20,000** | x | ✓ |\n")
        self.assertNotIn("RAG_ENABLE_THRESHOLD", parsed)

    def test_negative_inline_rejected(self):
        self.assertNotIn("HL_BASE", self._parse("HL_BASE = -3\n"))

    def test_word_boundary_no_cross_contamination(self):
        # L0_TO_L1_RC and L1_TO_L2_RC must not be confused.
        text = (
            "| `L0_TO_L1_RC` | **2** | promote | ✓ |\n"
            "| `L1_TO_L2_RC` | **4** | promote | ✓ |\n"
        )
        parsed = self._parse(text)
        self.assertEqual(parsed["L0_TO_L1_RC"], 2)
        self.assertEqual(parsed["L1_TO_L2_RC"], 4)


class TestResolve(unittest.TestCase):
    """resolve() must merge policy over defaults and report provenance."""

    def test_missing_file_uses_all_defaults(self):
        defaults = {"HL_BASE": 7.0, "L0_TO_L1_RC": 2}
        values, sources = policy_config.resolve(defaults, "/no/such/file.md")
        self.assertEqual(values, defaults)
        self.assertEqual(set(sources.values()), {"default"})

    def test_policy_overrides_and_marks_source(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("| `HL_BASE` | **2.0** days | x | ✓ |\n")
            path = f.name
        try:
            defaults = {"HL_BASE": 7.0, "L0_TO_L1_RC": 2}
            values, sources = policy_config.resolve(defaults, path)
        finally:
            os.unlink(path)
        self.assertEqual(values["HL_BASE"], 2.0)
        self.assertEqual(sources["HL_BASE"], "policy")
        # Not declared in this file -> stays default.
        self.assertEqual(values["L0_TO_L1_RC"], 2)
        self.assertEqual(sources["L0_TO_L1_RC"], "default")

    def test_none_path_returns_empty(self):
        self.assertEqual(policy_config.load_policy_constants(None), {})


if __name__ == "__main__":
    unittest.main()
