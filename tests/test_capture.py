"""
Tests for capture-trigger.py — CAPTURE stage entrypoint.

Deterministic parts only (no real model call): transcript parsing, model-output
parsing, L0 writing + frontmatter, the recursion guard, and graceful no-ops.
"""

import importlib.util
import json
import os
import tempfile
import unittest

import _helpers

# Module filename has a hyphen -> load via importlib.
_spec = importlib.util.spec_from_file_location(
    "capture_trigger", os.path.join(_helpers.SCRIPTS_DIR, "capture-trigger.py"))
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)


def _jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestExtract(unittest.TestCase):
    def test_only_string_user_content_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            _jsonl(p, [
                {"type": "user", "message": {"role": "user", "content": "I prefer pytest"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}},
                {"type": "user", "message": {"role": "user", "content": "<command-name>/foo</command-name>"}},
                {"type": "system", "content": "noise"},
            ])
            lines = ct.extract_chairman_lines(p)
            self.assertEqual(lines, [(0, "I prefer pytest")])

    def test_missing_file_returns_empty(self):
        self.assertEqual(ct.extract_chairman_lines("/no/such.jsonl"), [])

    def test_malformed_lines_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            with open(p, "w") as f:
                f.write("not json\n")
                f.write(json.dumps({"type": "user", "message": {"content": "hi there"}}) + "\n")
            self.assertEqual(ct.extract_chairman_lines(p), [(1, "hi there")])


class TestParseObservations(unittest.TestCase):
    def test_clean_array(self):
        out = ct._parse_observations('[{"id":"a","body":"b","source_lines":[1]}]')
        self.assertEqual(len(out), 1)

    def test_array_embedded_in_prose(self):
        txt = 'Here you go:\n[{"id":"a","body":"b"}]\nThanks!'
        self.assertEqual(ct._parse_observations(txt)[0]["id"], "a")

    def test_drops_items_missing_id_or_body(self):
        out = ct._parse_observations('[{"id":"a","body":"b"},{"id":"x"},{"body":"y"}]')
        self.assertEqual([o["id"] for o in out], ["a"])

    def test_garbage_returns_empty(self):
        self.assertEqual(ct._parse_observations("no json here"), [])
        self.assertEqual(ct._parse_observations(""), [])


class TestWriteL0(unittest.TestCase):
    def _company(self, d):
        os.makedirs(os.path.join(d, "memory", "L0-working"))
        return d

    def test_writes_frontmatter_and_sources(self):
        with tempfile.TemporaryDirectory() as d:
            self._company(d)
            written = ct.write_l0(
                [{"id": "Pref Async", "body": "likes async", "source_lines": [6, 18]}],
                "s.20260626", d, today="2026-06-26")
            self.assertEqual(written, ["pref-async"])
            with open(os.path.join(d, "memory", "L0-working", "pref-async.md")) as f:
                txt = f.read()
            self.assertIn("tier: L0", txt)
            self.assertIn("owner: Tony", txt)
            self.assertIn('sources: ["[s.20260626#6]", "[s.20260626#18]"]', txt)
            self.assertIn("reinforce_count: 1", txt)
            self.assertIn("likes async", txt)

    def test_no_sources_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            self._company(d)
            self.assertEqual(
                ct.write_l0([{"id": "x", "body": "b", "source_lines": []}], "s", d), [])
            self.assertEqual(
                ct.write_l0([{"id": "x", "body": "b"}], "s", d), [])

    def test_existing_id_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            self._company(d)
            ct.write_l0([{"id": "a", "body": "first", "source_lines": [1]}], "s", d)
            ct.write_l0([{"id": "a", "body": "SECOND", "source_lines": [2]}], "s", d)
            with open(os.path.join(d, "memory", "L0-working", "a.md")) as f:
                txt = f.read()
            self.assertIn("first", txt)
            self.assertNotIn("SECOND", txt)

    def test_system_noise_is_quarantined(self):
        # A2: a bug/system-failure "observation" must not be written as memory,
        # but a genuine standing preference must be.
        with tempfile.TemporaryDirectory() as d:
            self._company(d)
            obs = [
                {"id": "bug", "body": "The skill failed at 2am and the cron didn't fire.",
                 "source_lines": [1]},
                {"id": "pref", "body": "Chairman wants push notifications, not Discord.",
                 "source_lines": [2]},
            ]
            written = ct.write_l0(obs, "s", d)
            self.assertIn("pref", written)
            self.assertNotIn("bug", written)

    def test_respects_max_observations(self):
        with tempfile.TemporaryDirectory() as d:
            self._company(d)
            obs = [{"id": f"o{i}", "body": "b", "source_lines": [i]} for i in range(50)]
            written = ct.write_l0(obs, "s", d)
            self.assertLessEqual(len(written), ct.MAX_OBSERVATIONS)


class TestGuardsAndNoOps(unittest.TestCase):
    def test_recursion_guard_env_exits_zero(self):
        old = os.environ.get(ct.RECURSION_GUARD)
        os.environ[ct.RECURSION_GUARD] = "1"
        try:
            self.assertEqual(ct.main(["--transcript", "/whatever", "--session", "s"]), 0)
        finally:
            if old is None:
                os.environ.pop(ct.RECURSION_GUARD, None)
            else:
                os.environ[ct.RECURSION_GUARD] = old

    def test_missing_transcript_noop(self):
        self.assertEqual(ct.main(["--transcript", "/no/such.jsonl", "--session", "s",
                                  "--dry-run"]), 0)

    def test_missing_company_noop(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            _jsonl(p, [{"type": "user", "message": {"content": "hello"}}])
            # company dir does not exist -> no-op, returns 0
            self.assertEqual(ct.main(["--transcript", p, "--session", "s",
                                      "--company", os.path.join(d, "nope")]), 0)

    def test_slug(self):
        self.assertEqual(ct._slug("Hello World!"), "hello-world")
        self.assertEqual(ct._slug("***"), "obs")


if __name__ == "__main__":
    unittest.main()
