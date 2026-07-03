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
from datetime import datetime, timedelta

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
            written, reinforced = ct.write_l0(
                [{"id": "Pref Async", "body": "likes async", "source_lines": [6, 18]}],
                "s.20260626", d, today="2026-06-26")
            self.assertEqual(written, ["pref-async"])
            self.assertEqual(reinforced, [])
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
                ct.write_l0([{"id": "x", "body": "b", "source_lines": []}], "s", d),
                ([], []))
            self.assertEqual(
                ct.write_l0([{"id": "x", "body": "b"}], "s", d), ([], []))

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
            written, _ = ct.write_l0(obs, "s", d)
            self.assertIn("pref", written)
            self.assertNotIn("bug", written)

    def test_respects_max_observations(self):
        with tempfile.TemporaryDirectory() as d:
            self._company(d)
            obs = [{"id": f"o{i}", "body": "b", "source_lines": [i]} for i in range(50)]
            written, _ = ct.write_l0(obs, "s", d)
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


class TestCooldownGate(unittest.TestCase):
    """Item 3(a): per-session CAPTURE cooldown — throttle same-session refires,
    never a different session, and FAIL-OPEN on any marker damage."""

    def _company(self, d):
        c = os.path.join(d, ".company")
        os.makedirs(os.path.join(c, "ops"))
        os.makedirs(os.path.join(c, "memory", "L0-working"))
        return c

    def test_no_marker_means_not_active(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            self.assertFalse(ct.cooldown_active(c, "s1", minutes=30))

    def test_fresh_marker_throttles_same_session_only(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            ct.mark_capture(c, "s1", minutes=30)
            self.assertTrue(ct.cooldown_active(c, "s1", minutes=30))
            # a DIFFERENT session is never blocked by s1's marker
            self.assertFalse(ct.cooldown_active(c, "s2", minutes=30))

    def test_elapsed_cooldown_not_active(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            t0 = datetime(2026, 7, 3, 10, 0, 0)
            ct.mark_capture(c, "s1", minutes=30, now=t0)
            self.assertTrue(ct.cooldown_active(
                c, "s1", minutes=30, now=t0 + timedelta(minutes=29)))
            self.assertFalse(ct.cooldown_active(
                c, "s1", minutes=30, now=t0 + timedelta(minutes=31)))

    def test_corrupt_marker_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            marker = os.path.join(c, "ops", ct.COOLDOWN_MARKER)
            with open(marker, "w") as f:
                f.write("{not json at all")
            self.assertFalse(ct.cooldown_active(c, "s1", minutes=30))
            # corrupt VALUE inside a valid map also fails open
            with open(marker, "w") as f:
                json.dump({"s1": "yesterday-ish"}, f)
            self.assertFalse(ct.cooldown_active(c, "s1", minutes=30))
            # non-dict JSON fails open too
            with open(marker, "w") as f:
                json.dump(["s1"], f)
            self.assertFalse(ct.cooldown_active(c, "s1", minutes=30))

    def test_future_timestamp_fails_open(self):
        # clock skew: a far-future stamp must not suppress captures forever
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            ct.mark_capture(c, "s1", minutes=30,
                            now=datetime.now() + timedelta(days=2))
            self.assertFalse(ct.cooldown_active(c, "s1", minutes=30))

    def test_zero_minutes_disables_throttle(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            ct.mark_capture(c, "s1", minutes=30)
            self.assertFalse(ct.cooldown_active(c, "s1", minutes=0))

    def test_policy_overrides_cooldown_minutes(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            org = os.path.join(c, "org")
            os.makedirs(org)
            # not declared -> built-in default
            self.assertEqual(ct.cooldown_minutes(c),
                             ct.DEFAULT_CAPTURE_COOLDOWN_MINUTES)
            with open(os.path.join(org, "policy.md"), "w") as f:
                f.write("| Constant | Default | Meaning | tunable |\n"
                        "|---|---|---|---|\n"
                        "| `CAPTURE_COOLDOWN_MINUTES` | **120** | throttle | ✓ |\n")
            self.assertEqual(ct.cooldown_minutes(c), 120)
            with open(os.path.join(org, "policy.md"), "w") as f:
                f.write("| `CAPTURE_COOLDOWN_MINUTES` | **0** | off | ✓ |\n")
            self.assertEqual(ct.cooldown_minutes(c), 0)

    def test_mark_capture_prunes_stale_and_corrupt_entries(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            marker = os.path.join(c, "ops", ct.COOLDOWN_MARKER)
            now = datetime(2026, 7, 3, 12, 0, 0)
            stale = (now - timedelta(days=3)).isoformat(timespec="seconds")
            with open(marker, "w") as f:
                json.dump({"old-sess": stale, "junk": "not-a-date"}, f)
            ct.mark_capture(c, "s1", minutes=30, now=now)
            with open(marker) as f:
                data = json.load(f)
            self.assertIn("s1", data)
            self.assertNotIn("old-sess", data)
            self.assertNotIn("junk", data)

    def test_main_second_fire_is_logged_noop(self):
        # end-to-end (in-process, model monkeypatched): fire twice for the same
        # session -> exactly ONE model call; the second is exit 0 + one log line.
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            p = os.path.join(d, "t.jsonl")
            _jsonl(p, [{"type": "user", "message": {"content": "I use vim daily"}}])
            calls = []
            real = ct.run_capture_model
            ct.run_capture_model = lambda *a, **k: calls.append(1) or []
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(ct.main(["--transcript", p, "--session", "s1",
                                              "--company", c]), 0)
                    self.assertEqual(len(calls), 1)
                    # second fire, same session, inside cooldown -> no model call
                    self.assertEqual(ct.main(["--transcript", p, "--session", "s1",
                                              "--company", c]), 0)
                    self.assertEqual(len(calls), 1)
                    # different session id -> proceeds
                    self.assertEqual(ct.main(["--transcript", p, "--session", "s2",
                                              "--company", c]), 0)
                    self.assertEqual(len(calls), 2)
            finally:
                ct.run_capture_model = real
            with open(os.path.join(c, "ops", "logs", "capture.log")) as f:
                log = f.read()
            self.assertEqual(log.count("CAPTURE throttled session=s1"), 1)

    def test_dry_run_reports_but_never_enforces_or_marks(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            p = os.path.join(d, "t.jsonl")
            _jsonl(p, [{"type": "user", "message": {"content": "I use vim daily"}}])
            ct.mark_capture(c, "s1", minutes=30)  # simulate a prior capture
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(ct.main(["--transcript", p, "--session", "s1",
                                          "--company", c, "--dry-run"]), 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(out["cooldown_active"])   # reported...
            self.assertIn("prompt", out)              # ...but not enforced


class TestReinforcePath(unittest.TestCase):
    """Item 3(b): a `reinforce` entry bumps the existing memory in place and
    NEVER writes a new file; unknown target falls back fail-safe."""

    def _company(self, d, mid="canonical-fact"):
        c = os.path.join(d, ".company")
        _helpers.write_memory(
            os.path.join(c, "memory", "L0-working", f"{mid}.md"),
            id=mid, sources='["[old#1]"]', created="2026-07-02",
            last_reinforced="2026-07-02", reinforce_count=1,
            body="Chairman uses vim daily.")
        return c

    def _l0_files(self, c):
        return sorted(os.listdir(os.path.join(c, "memory", "L0-working")))

    def test_reinforce_bumps_existing_and_writes_no_new_file(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            written, reinforced = ct.write_l0(
                [{"reinforce": "canonical-fact", "source_lines": [7]}],
                "sess-1", c, today="2026-07-03")
            self.assertEqual(written, [])
            self.assertEqual(reinforced, ["canonical-fact"])
            self.assertEqual(self._l0_files(c), ["canonical-fact.md"])  # no new file
            with open(os.path.join(c, "memory", "L0-working", "canonical-fact.md")) as f:
                txt = f.read()
            self.assertIn("reinforce_count: 2", txt)
            self.assertIn("last_reinforced: 2026-07-03", txt)
            self.assertIn('sources: ["[old#1]", "[sess-1#7]"]', txt)
            self.assertIn("Chairman uses vim daily.", txt)  # body untouched

    def test_reinforce_without_sources_still_bumps(self):
        # the existing memory already has provenance; a bare reinforce is valid
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            written, reinforced = ct.write_l0(
                [{"reinforce": "canonical-fact"}], "sess-1", c, today="2026-07-03")
            self.assertEqual((written, reinforced), ([], ["canonical-fact"]))
            with open(os.path.join(c, "memory", "L0-working", "canonical-fact.md")) as f:
                txt = f.read()
            self.assertIn("reinforce_count: 2", txt)
            self.assertIn('sources: ["[old#1]"]', txt)  # unchanged

    def test_duplicate_source_token_not_appended_twice(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            ct.write_l0([{"reinforce": "canonical-fact", "source_lines": [7]}],
                        "sess-1", c, today="2026-07-03")
            ct.write_l0([{"reinforce": "canonical-fact", "source_lines": [7]}],
                        "sess-1", c, today="2026-07-03")
            with open(os.path.join(c, "memory", "L0-working", "canonical-fact.md")) as f:
                txt = f.read()
            self.assertEqual(txt.count('"[sess-1#7]"'), 1)
            self.assertIn("reinforce_count: 3", txt)  # both bumps counted

    def test_unknown_target_falls_back_to_new_observation(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            written, reinforced = ct.write_l0(
                [{"reinforce": "no-such-id", "id": "new-fact",
                  "body": "He deploys on Fridays.", "source_lines": [3]}],
                "sess-1", c, today="2026-07-03")
            self.assertEqual(written, ["new-fact"])
            self.assertEqual(reinforced, [])
            self.assertIn("new-fact.md", self._l0_files(c))

    def test_unknown_target_without_body_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            written, reinforced = ct.write_l0(
                [{"reinforce": "no-such-id", "source_lines": [3]}],
                "sess-1", c, today="2026-07-03")
            self.assertEqual((written, reinforced), ([], []))
            self.assertEqual(self._l0_files(c), ["canonical-fact.md"])

    def test_traversal_shaped_target_cannot_escape_memory(self):
        # targets resolve against SCANNED memory ids, never filesystem paths
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            written, reinforced = ct.write_l0(
                [{"reinforce": "../../org/policy"}], "sess-1", c,
                today="2026-07-03")
            self.assertEqual((written, reinforced), ([], []))

    def test_adversarial_session_id_sanitized_on_new_file_path(self):
        # Phase-2 fix regression: the NEW-file path must also emit only the
        # sanitized token (UUID chars pass through; YAML-breakers become '-').
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            evil = 'x"]\nid: forged\ntier: L2\n#'
            written, _ = ct.write_l0(
                [{"id": "obs-one", "body": "He codes at 2am.", "source_lines": [9]}],
                evil, c, today="2026-07-03")
            self.assertEqual(written, ["obs-one"])
            with open(os.path.join(c, "memory", "L0-working", "obs-one.md")) as f:
                txt = f.read()
            self.assertNotIn("\nid: forged", txt)
            self.assertIn('"[x---id--forged-tier--L2--#9]"', txt)
            fm, _ = ct._parse_frontmatter(txt)
            self.assertEqual(fm["id"], "obs-one")
            self.assertEqual(fm["tier"], "L0")

    def test_adversarial_session_id_stays_sanitized(self):
        # Phase-2 fix must hold on the NEW reinforce path too: a YAML-breaking
        # session id must not forge frontmatter in the bumped memory.
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            evil = 'x"]\nid: forged\ntier: L2\n#'
            ct.write_l0([{"reinforce": "canonical-fact", "source_lines": [5]}],
                        evil, c, today="2026-07-03")
            with open(os.path.join(c, "memory", "L0-working", "canonical-fact.md")) as f:
                txt = f.read()
            # no newline/quote injection: the evil sid appears only as ONE
            # sanitized token inside the sources list, never as a new line
            self.assertNotIn("\nid: forged", txt)
            self.assertNotIn('x"]', txt)
            self.assertIn('"[x---id--forged-tier--L2--#5]"', txt)
            fm, _ = ct._parse_frontmatter(txt)
            self.assertEqual(fm["id"], "canonical-fact")
            self.assertEqual(fm["tier"], "L0")


class TestRecentDigestAndPrompt(unittest.TestCase):
    """Item 3(b): digest construction + extended output contract in the prompt."""

    def _company(self, d):
        return os.path.join(d, ".company")

    def test_digest_window_status_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            l0 = os.path.join(c, "memory", "L0-working")
            _helpers.write_memory(os.path.join(l0, "fresh.md"), id="fresh",
                                  created="2026-07-03", body="Fresh fact. More.")
            _helpers.write_memory(os.path.join(l0, "yesterday.md"), id="yesterday",
                                  created="2026-07-02", body="Yesterday fact.")
            _helpers.write_memory(os.path.join(l0, "stale.md"), id="stale",
                                  created="2026-06-20", body="Old fact.")
            _helpers.write_memory(os.path.join(l0, "gone.md"), id="gone",
                                  created="2026-07-03", status="defunct",
                                  body="Merged away.")
            digest = ct.recent_l0_digest(c, now=datetime(2026, 7, 3, 12, 0))
            self.assertEqual([mid for mid, _ in digest], ["fresh", "yesterday"])
            self.assertEqual(digest[0][1], "Fresh fact")  # first-sentence gist

    def test_digest_cap(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._company(d)
            l0 = os.path.join(c, "memory", "L0-working")
            for i in range(40):
                _helpers.write_memory(os.path.join(l0, f"m{i:02d}.md"),
                                      id=f"m{i:02d}", created="2026-07-03",
                                      body=f"Fact {i}.")
            digest = ct.recent_l0_digest(c, now=datetime(2026, 7, 3, 12, 0))
            self.assertEqual(len(digest), ct.RECENT_DIGEST_MAX)

    def test_missing_l0_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ct.recent_l0_digest(self._company(d)), [])

    def test_prompt_contains_digest_and_reinforce_contract(self):
        prompt = ct.build_capture_prompt(
            [(0, "hello")], {"a"},
            recent_memories=[("vim-daily", "Chairman uses vim daily")])
        self.assertIn("=== Recently captured memories (last 48h) ===", prompt)
        self.assertIn("- vim-daily: Chairman uses vim daily", prompt)
        self.assertIn('"reinforce": "<that-id>"', prompt)
        self.assertIn('add "reinforce": "<existing-id>"', prompt)

    def test_prompt_without_digest_is_clean(self):
        prompt = ct.build_capture_prompt([(0, "hello")], {"a"})
        self.assertNotIn("Recently captured memories", prompt)

    def test_digest_block_respects_char_budget(self):
        recent = [(f"id-{i:03d}", "x" * 200) for i in range(100)]
        prompt = ct.build_capture_prompt([(0, "hello")], set(),
                                         recent_memories=recent)
        listed = prompt.count("\n- id-")
        self.assertLess(listed, 100)
        self.assertLessEqual(listed, ct.RECENT_DIGEST_MAX)

    def test_parse_accepts_reinforce_items(self):
        out = ct._parse_observations(
            '[{"reinforce": "vim-daily", "body": "uses vim", "source_lines": [4]},'
            ' {"id": "a", "body": "b", "source_lines": [1]}, {"category": "x"}]')
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["reinforce"], "vim-daily")


class TestHookStdinThrottle(unittest.TestCase):
    """Proof (a) end-to-end over the REAL hook contract: stdin payload,
    subprocess, `claude` absent from PATH (no model call possible)."""

    def _run_hook(self, payload, cwd):
        import subprocess
        import sys as _sys
        script = os.path.join(_helpers.SCRIPTS_DIR, "capture-trigger.py")
        env = {**os.environ, "PATH": cwd}  # no `claude` reachable
        return subprocess.run([_sys.executable, script, "--company",
                               os.path.join(cwd, ".company")],
                              input=json.dumps(payload), text=True,
                              capture_output=True, env=env, cwd=cwd)

    def test_hook_refire_same_session_is_noop_exit_zero(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".company", "ops"))
            os.makedirs(os.path.join(d, ".company", "memory", "L0-working"))
            p = os.path.join(d, "t.jsonl")
            _jsonl(p, [{"type": "user", "message": {"content": "I ship on Fridays"}}])
            payload = {"session_id": "hook-sess", "transcript_path": p,
                       "stop_hook_active": False}
            r1 = self._run_hook(payload, d)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            marker = os.path.join(d, ".company", "ops", ct.COOLDOWN_MARKER)
            self.assertTrue(os.path.exists(marker))
            r2 = self._run_hook(payload, d)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            with open(os.path.join(d, ".company", "ops", "logs", "capture.log")) as f:
                self.assertIn("CAPTURE throttled session=hook-sess", f.read())
            # no L0 was written by either fire (no model available)
            self.assertEqual(os.listdir(
                os.path.join(d, ".company", "memory", "L0-working")), [])


if __name__ == "__main__":
    unittest.main()
