"""
Tests for frontmatter.py — the shared parse/split/serialize/tokenize seam.

Covers the contract B2/B3 migrate against: byte-identical round-trip of a real
memory file, the `.strip()=='---'` delimiter (a `----` body line does NOT
truncate — the exact entropy bug the migration fixes), no-frontmatter ->
([], text), missing/extra keys, quoted `sources` tokenization, CRLF, and empty
input. This module does ONLY parse/split/serialize/tokenize — no defaults, no
validation — so these tests assert exactly that scope.
"""

import unittest

import _helpers  # noqa: F401  (puts scripts/ on sys.path)
import frontmatter


# A realistic memory file with a deliberately non-alphabetical key order, to
# prove `serialize` respects a supplied order and round-trips byte-identically.
REAL_MEMORY = (
    "---\n"
    "id: no-claude-folder-in-git\n"
    "tier: L0\n"
    "owner: Tony\n"
    'sources: ["[8e466e7c-d3b8-4584-a567-80ec04380dab#594]"]\n'
    "created: 2026-06-30\n"
    "last_reinforced: 2026-06-30\n"
    "reinforce_count: 1\n"
    "decay_score: 0.5520447568369062\n"
    "status: active\n"
    "verified_date: 2026-07-01\n"
    "verified_by: Gibby\n"
    "---\n"
    "Chairman decided .claude/ folder should not be committed to GitHub; it "
    "remains repository-scoped and local-only.\n"
)


class TestRoundTrip(unittest.TestCase):
    def test_byte_identical_round_trip_with_order(self):
        fm, body = frontmatter.parse(REAL_MEMORY)
        out = frontmatter.serialize(fm, body, order=list(fm.keys()))
        self.assertEqual(out, REAL_MEMORY)

    def test_byte_identical_round_trip_default_order(self):
        # order=None emits dict-insertion order, which equals the file order.
        fm, body = frontmatter.parse(REAL_MEMORY)
        out = frontmatter.serialize(fm, body)
        self.assertEqual(out, REAL_MEMORY)

    def test_explicit_reorder_moves_listed_keys_first(self):
        fm = {"a": "1", "b": "2", "c": "3"}
        out = frontmatter.serialize(fm, "body\n", order=["c", "a"])
        self.assertEqual(out, "---\nc: 3\na: 1\nb: 2\n---\nbody\n")


class TestDelimiter(unittest.TestCase):
    def test_dashes_body_line_is_not_a_fence(self):
        # The exact bug the migration fixes: a `----` (or `---text`) BODY line
        # must NOT be treated as the closing fence. entropy's old
        # `startswith('---')` truncated here; `.strip()=='---'` does not.
        text = (
            "---\n"
            "id: has-rule\n"
            "tier: L1\n"
            "---\n"
            "First paragraph.\n"
            "----\n"           # markdown horizontal rule in the BODY
            "Second paragraph after the rule.\n"
        )
        fm, body = frontmatter.parse(text)
        self.assertEqual(fm, {"id": "has-rule", "tier": "L1"})
        self.assertIn("----", body)
        self.assertIn("Second paragraph after the rule.", body)

    def test_indented_dashes_are_not_a_fence(self):
        # A fence must be `line.strip()=='---'`; leading text like `---xyz`
        # is not a fence either.
        text = "---\nid: x\n---\n---abc\nbody\n"
        fm, body = frontmatter.parse(text)
        self.assertEqual(fm, {"id": "x"})
        self.assertEqual(body, "---abc\nbody\n")

    def test_leading_blank_lines_before_opening_fence(self):
        # The opening fence must be line 0 (legacy `lines[0].strip()=='---'`).
        # A file that starts with blank line(s) has NO frontmatter — matching
        # what all ten pre-Phase-11 parsers did uniformly (no leading-blank
        # skip), so no scanner drifts into parsing it.
        text = "\n\n---\nid: y\n---\nbody\n"
        self.assertEqual(frontmatter.split(text), ([], text))
        self.assertEqual(frontmatter.parse(text), ({}, text))


class TestNoFrontmatter(unittest.TestCase):
    def test_no_opening_fence_returns_text_unchanged(self):
        text = "just a body\nwith lines\n"
        self.assertEqual(frontmatter.split(text), ([], text))
        self.assertEqual(frontmatter.parse(text), ({}, text))

    def test_missing_closing_fence_returns_text_unchanged(self):
        # Matches decay.py: an unterminated block is not frontmatter.
        text = "---\nid: dangling\nno closing fence here\n"
        self.assertEqual(frontmatter.split(text), ([], text))
        self.assertEqual(frontmatter.parse(text), ({}, text))

    def test_empty_file(self):
        self.assertEqual(frontmatter.split(""), ([], ""))
        self.assertEqual(frontmatter.parse(""), ({}, ""))

    def test_whitespace_only(self):
        text = "   \n\n"
        self.assertEqual(frontmatter.split(text), ([], text))
        self.assertEqual(frontmatter.parse(text), ({}, text))


class TestKeys(unittest.TestCase):
    def test_missing_keys_not_defaulted(self):
        # Only what's present appears; NO defaults injected (unlike decay/entropy).
        fm, _ = frontmatter.parse("---\nid: only-id\n---\nbody\n")
        self.assertEqual(fm, {"id": "only-id"})

    def test_unknown_keys_preserved(self):
        fm, _ = frontmatter.parse(
            "---\nid: x\nweird_key: some value\nprovenance: charter\n---\nb\n"
        )
        self.assertEqual(
            fm, {"id": "x", "weird_key": "some value", "provenance": "charter"}
        )

    def test_value_with_colon_split_on_first_only(self):
        fm, _ = frontmatter.parse("---\nk: a: b: c\n---\nbody\n")
        self.assertEqual(fm, {"k": "a: b: c"})

    def test_comment_and_blank_lines_skipped(self):
        fm, _ = frontmatter.parse("---\n# a comment\nid: x\n\n# more\n---\nb\n")
        self.assertEqual(fm, {"id": "x"})

    def test_line_without_colon_skipped(self):
        fm, _ = frontmatter.parse("---\nid: x\nnot a kv line\n---\nb\n")
        self.assertEqual(fm, {"id": "x"})


class TestSources(unittest.TestCase):
    def test_tokenize_quoted_items_keep_quotes(self):
        raw = '["[a#1]", "charter:x"]'
        self.assertEqual(frontmatter.tokenize_sources(raw), ['"[a#1]"', '"charter:x"'])

    def test_tokenize_matches_raw_regex(self):
        raw = '["[8e466e7c#594]", "[abc#12]"]'
        self.assertEqual(
            frontmatter.tokenize_sources(raw),
            frontmatter.SOURCE_ITEM_RE.findall(raw),
        )

    def test_tokenize_empty_and_none(self):
        self.assertEqual(frontmatter.tokenize_sources(""), [])
        self.assertEqual(frontmatter.tokenize_sources(None), [])
        self.assertEqual(frontmatter.tokenize_sources("[]"), [])

    def test_sources_value_kept_raw_in_parse(self):
        # parse keeps the raw sources string; tokenization is the caller's job.
        fm, _ = frontmatter.parse(
            '---\nid: x\nsources: ["[a#1]", "[b#2]"]\n---\nbody\n'
        )
        self.assertEqual(fm["sources"], '["[a#1]", "[b#2]"]')
        self.assertEqual(
            frontmatter.tokenize_sources(fm["sources"]), ['"[a#1]"', '"[b#2]"']
        )


class TestCRLF(unittest.TestCase):
    def test_crlf_fences_and_values_parse(self):
        text = "---\r\nid: x\r\ntier: L0\r\n---\r\nbody line\r\n"
        fm, body = frontmatter.parse(text)
        self.assertEqual(fm, {"id": "x", "tier": "L0"})
        # `\r` survives in the body until a caller normalises it.
        self.assertEqual(body, "body line\r\n")


if __name__ == "__main__":
    unittest.main()
