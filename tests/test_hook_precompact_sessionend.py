"""
Tests for the B3 plugin-native hooks (spec Phase 10, Items 5 + 8 + shared #2):

  - hook_precompact_capture.sh  (PreCompact → capture rescue)
  - hook_sessionend_verify.sh   (SessionEnd → verify fresh captures)
  - hook_guard.sh               (shared opt-in guard)

These are black-box wrappers around existing python scripts, so the tests feed
the exact documented stdin JSON and assert stdout / exit / side-effect. No real
model is ever invoked: PreCompact runs capture-trigger.py in --dry-run.
"""

import json
import os
import subprocess
import tempfile
import unittest

import _helpers

SCRIPTS = _helpers.SCRIPTS_DIR
GUARD = os.path.join(SCRIPTS, "hook_guard.sh")
PRECOMPACT = os.path.join(SCRIPTS, "hook_precompact_capture.sh")
SESSIONEND = os.path.join(SCRIPTS, "hook_sessionend_verify.sh")


# Generous bound (never hit on a healthy box; a loaded/CI box just needs
# headroom past ordinary python3-subprocess-per-hook startup cost). Without
# an explicit timeout, a genuinely wedged hook would hang subprocess.run()
# forever -- silently stalling the whole suite instead of failing cleanly
# with a diagnosable TimeoutExpired.
_BASH_TIMEOUT_S = 30


def _bash(args, stdin="", env=None, timeout=_BASH_TIMEOUT_S):
    return subprocess.run(
        ["bash", *args], input=stdin, capture_output=True, text=True,
        env={**os.environ, **(env or {})}, timeout=timeout)


def _mk_company(base):
    """Create a minimal .company/memory skeleton under base; return company dir."""
    company = os.path.join(base, ".company")
    os.makedirs(os.path.join(company, "memory", "L0-working"), exist_ok=True)
    return company


def _write_transcript(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# hook_guard.sh — shared opt-in guard
# ---------------------------------------------------------------------------
class TestHookGuard(unittest.TestCase):
    SNIPPET = f'. "{GUARD}"; sc_hook_optin; echo PASSED "$SC_COMPANY"'

    def test_noop_without_company(self):
        with tempfile.TemporaryDirectory() as d:
            r = _bash(["-c", self.SNIPPET], env={"CLAUDE_PROJECT_DIR": d})
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")  # exited before the echo

    def test_continues_with_company(self):
        with tempfile.TemporaryDirectory() as d:
            company = _mk_company(d)
            r = _bash(["-c", self.SNIPPET], env={"CLAUDE_PROJECT_DIR": d})
            self.assertEqual(r.returncode, 0)
            self.assertIn("PASSED", r.stdout)
            self.assertIn(company, r.stdout)  # SC_COMPANY exported for caller


# ---------------------------------------------------------------------------
# hook_precompact_capture.sh — PreCompact capture rescue
# ---------------------------------------------------------------------------
class TestPreCompact(unittest.TestCase):
    def test_reads_transcript_and_invokes_capture(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_company(d)
            tp = os.path.join(d, "transcript.jsonl")
            _write_transcript(tp, [
                {"type": "user", "message": {"role": "user",
                                             "content": "I trade TWSE futures via Shioaji"}},
            ])
            payload = json.dumps({
                "session_id": "sess-pc", "transcript_path": tp, "cwd": d,
                "hook_event_name": "PreCompact",
                "hookSpecificInput": {"matcher": "auto"},
            })
            # --dry-run is forwarded to capture-trigger.py (no model call).
            r = _bash([PRECOMPACT, "--dry-run"], stdin=payload,
                      env={"CLAUDE_PROJECT_DIR": d})
            self.assertEqual(r.returncode, 0)  # never blocks compaction
            # Guard BEFORE indexing: under load (or on a wedged box) the hook
            # can exit 0 with empty stdout (its `|| true` swallows a crashed
            # capture-trigger.py), and `splitlines()[-1]` on "" raises a bare
            # IndexError with zero diagnosis. Fail with a clear message
            # (carrying rc + stderr) instead of crashing the test itself.
            out = r.stdout.strip()
            self.assertTrue(
                out,
                f"hook_precompact_capture.sh produced no stdout "
                f"(rc={r.returncode}); stderr:\n{r.stderr}")
            # The dry-run report proves the capture path actually ran over the
            # pre-compaction transcript.
            report = json.loads(out.splitlines()[-1])
            self.assertEqual(report["session"], "sess-pc")
            self.assertEqual(report["chairman_lines"], 1)

    def test_noop_without_company(self):
        with tempfile.TemporaryDirectory() as d:
            tp = os.path.join(d, "t.jsonl")
            _write_transcript(tp, [{"type": "user",
                                    "message": {"content": "hi"}}])
            payload = json.dumps({"session_id": "s", "transcript_path": tp})
            r = _bash([PRECOMPACT, "--dry-run"], stdin=payload,
                      env={"CLAUDE_PROJECT_DIR": d})  # d has no .company
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")  # silent no-op

    def test_missing_transcript_is_noop_exit0(self):
        with tempfile.TemporaryDirectory() as d:
            _mk_company(d)
            payload = json.dumps({"session_id": "s",
                                  "transcript_path": os.path.join(d, "gone.jsonl")})
            r = _bash([PRECOMPACT, "--dry-run"], stdin=payload,
                      env={"CLAUDE_PROJECT_DIR": d})
            self.assertEqual(r.returncode, 0)  # never fails


# ---------------------------------------------------------------------------
# hook_sessionend_verify.sh — SessionEnd verify fresh captures
# ---------------------------------------------------------------------------
class TestSessionEnd(unittest.TestCase):
    def test_verifies_scratch_corpus(self):
        with tempfile.TemporaryDirectory() as d:
            company = _mk_company(d)
            # A fake transcript so the memory's source [sess#1] traces.
            tdir = os.path.join(d, "transcripts")
            os.makedirs(tdir, exist_ok=True)
            _write_transcript(os.path.join(tdir, "sess.jsonl"),
                              [{"type": "user", "message": {"content": "a"}},
                               {"type": "assistant", "message": {"content": "b"}}])
            mem = os.path.join(company, "memory", "L0-working", "m.md")
            _helpers.write_memory(mem, id="m", sources='["[sess#1]"]')

            payload = json.dumps({
                "session_id": "sess", "transcript_path": "x", "cwd": d,
                "hook_event_name": "SessionEnd",
                "hookSpecificInput": {"matcher": "normal"},
            })
            # Forwarded args pin the transcripts dir + date for determinism.
            r = _bash([SESSIONEND, "--transcripts-dir", tdir, "--now", "2026-07-06"],
                      stdin=payload, env={"CLAUDE_PROJECT_DIR": d})
            self.assertEqual(r.returncode, 0)
            report = json.loads(r.stdout)
            self.assertIn("m", report["verified"])       # newly verified
            # And the file was actually stamped on disk.
            with open(mem, encoding="utf-8") as f:
                self.assertIn("verified_date: 2026-07-06", f.read())

    def test_noop_without_company(self):
        with tempfile.TemporaryDirectory() as d:
            r = _bash([SESSIONEND], stdin="{}",
                      env={"CLAUDE_PROJECT_DIR": d})  # no .company
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")

    def test_missing_memory_dir_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            # .company exists but no memory/ subdir -> guard no-op.
            os.makedirs(os.path.join(d, ".company"), exist_ok=True)
            r = _bash([SESSIONEND], stdin="{}",
                      env={"CLAUDE_PROJECT_DIR": d})
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
