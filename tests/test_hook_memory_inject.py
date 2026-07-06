"""
Tests for hook_memory_inject.py — UserPromptSubmit ask-time memory injection.

Deterministic, stdlib-only, fixture-driven: each test builds a scratch .company
memory corpus + a fixture transcript JSONL and feeds the exact documented stdin
JSON (or CLI overrides), then asserts the additionalContext / silence / exit code
contract. NEVER touches the live .company/memory store.

Locks:
  * relevant prompt -> injects the matching L2 fact as additionalContext
  * irrelevant prompt -> zero output, exit 0
  * no .company -> exit 0, no output
  * tombstoned memory -> never injected
  * malformed transcript -> exit 0
  * always exits 0 (never blocks the prompt)
  * completes fast on a ~150-memory corpus (loose time bound)
"""

import importlib.util
import json
import os
import tempfile
import time
import unittest

import _helpers  # noqa: F401 - puts scripts/ on sys.path

_spec = importlib.util.spec_from_file_location(
    "hook_memory_inject",
    os.path.join(_helpers.SCRIPTS_DIR, "hook_memory_inject.py"))
hmi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hmi)


def _write_mem(company, tier_dir, name, *, body, tier="L2",
               reinforce_count=1, status="active", category="preferences"):
    d = os.path.join(company, "memory", tier_dir, category)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            f"id: {name}\n"
            f"tier: {tier}\n"
            f"category: {category}\n"
            f"reinforce_count: {reinforce_count}\n"
            f"status: {status}\n"
            "---\n"
            f"{body}\n")
    return path


def _write_transcript(path, user_texts):
    """Write a JSONL transcript with the given user turns (in order)."""
    with open(path, "w", encoding="utf-8") as f:
        for t in user_texts:
            f.write(json.dumps({"type": "user",
                                "message": {"role": "user", "content": t}}) + "\n")


def _run(company=None, transcript=None, stdin=None):
    """Run the hook as a subprocess with the documented stdin JSON. Returns
    (returncode, parsed_stdout_or_None, raw_stdout)."""
    args = []
    if company is not None:
        args += ["--company", company]
    if transcript is not None:
        args += ["--transcript", transcript]
    cmd = [__import__("sys").executable,
           os.path.join(_helpers.SCRIPTS_DIR, "hook_memory_inject.py"), *args]
    proc = __import__("subprocess").run(
        cmd, capture_output=True, text=True,
        input=(json.dumps(stdin) if stdin is not None else ""))
    out = proc.stdout.strip()
    parsed = json.loads(out) if out else None
    return proc.returncode, parsed, out


class TestMemoryInject(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.company = os.path.join(self.dir, ".company")
        os.makedirs(self.company)
        self.transcript = os.path.join(self.dir, "t.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    # --- relevant prompt injects the matching L2 fact ------------------------
    def test_relevant_prompt_injects_matching_l2(self):
        _write_mem(self.company, "L2-cold", "editor-preference",
                   body="The Chairman prefers the Neovim editor with a dark "
                        "colorscheme for all coding work.")
        _write_mem(self.company, "L2-cold", "coffee-order",
                   body="The Chairman drinks oat-milk flat whites in the morning.")
        _write_transcript(self.transcript,
                          ["Can you set up my neovim colorscheme?"])
        rc, parsed, _ = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(parsed)
        hso = parsed["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        ctx = hso["additionalContext"]
        self.assertIn("Relevant Chairman memory:", ctx)
        self.assertIn("Neovim", ctx)
        self.assertNotIn("oat-milk", ctx)   # irrelevant memory not injected

    # --- irrelevant prompt -> silence, exit 0 --------------------------------
    def test_irrelevant_prompt_silent(self):
        _write_mem(self.company, "L2-cold", "editor-preference",
                   body="The Chairman prefers Neovim with a dark colorscheme.")
        _write_transcript(self.transcript,
                          ["What is the population of Jupiter's moons?"])
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertEqual(raw, "")
        self.assertIsNone(parsed)

    # --- no .company -> no-op ------------------------------------------------
    def test_no_company_noop(self):
        empty = os.path.join(self.dir, "empty-repo")
        os.makedirs(empty)
        # point --company at a non-existent .company under a clean dir
        ghost = os.path.join(empty, ".company")
        _write_transcript(self.transcript, ["neovim please"])
        rc, parsed, raw = _run(company=ghost, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertEqual(raw, "")
        self.assertIsNone(parsed)

    # --- tombstoned memory is never injected ---------------------------------
    def test_tombstone_excluded(self):
        _write_mem(self.company, "L2-cold", "old-editor",
                   body="The Chairman used to prefer the Emacs editor exclusively.",
                   status="archived")
        _write_mem(self.company, "L2-cold", "absorbed-editor",
                   body="Duplicate note about the Emacs editor preference.",
                   status="absorbed")
        _write_transcript(self.transcript, ["set up my emacs editor config"])
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertEqual(raw, "")          # only tombstones matched -> silent
        self.assertIsNone(parsed)

    # --- malformed transcript -> exit 0, never crashes -----------------------
    def test_malformed_transcript_exit0(self):
        _write_mem(self.company, "L2-cold", "editor-preference",
                   body="The Chairman prefers Neovim.")
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write("{not valid json at all\n\x00\x01garbage\n")
        rc, _parsed, _raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)

    def test_missing_transcript_exit0(self):
        _write_mem(self.company, "L2-cold", "editor-preference",
                   body="The Chairman prefers Neovim.")
        rc, _parsed, _raw = _run(
            company=self.company,
            transcript=os.path.join(self.dir, "does-not-exist.jsonl"))
        self.assertEqual(rc, 0)

    # --- stdin transcript_path is honored (documented contract) --------------
    def test_reads_transcript_from_stdin_json(self):
        _write_mem(self.company, "L2-cold", "deploy-pref",
                   body="The Chairman deploys via Fly.io using blue-green rollouts.")
        _write_transcript(self.transcript, ["how do I deploy with fly?"])
        stdin = {"session_id": "s1", "prompt_id": "p1",
                 "transcript_path": self.transcript, "cwd": self.dir,
                 "hook_event_name": "UserPromptSubmit", "effort": "medium"}
        # NOTE: no --transcript flag; the path comes from stdin JSON.
        rc, parsed, _ = _run(company=self.company, stdin=stdin)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(parsed)
        self.assertIn("Fly.io",
                      parsed["hookSpecificOutput"]["additionalContext"])

    # --- token cap on additionalContext -------------------------------------
    def test_context_char_capped(self):
        long_body = ("neovim editor configuration " * 40).strip()
        for i in range(6):
            _write_mem(self.company, "L2-cold", f"m{i}", body=long_body,
                       reinforce_count=i + 1)
        _write_transcript(self.transcript, ["neovim editor configuration help"])
        rc, parsed, _ = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(ctx), hmi.CONTEXT_CHAR_CAP)

    # --- high-rc gate for L1: low-rc L1 is out of scope ----------------------
    def test_l1_requires_high_rc(self):
        _write_mem(self.company, "L1-warm", "l1-lowrc", tier="L1",
                   reinforce_count=1,
                   body="Chairman likes zeppelin airships as a hobby topic.")
        _write_transcript(self.transcript, ["tell me about zeppelin airships"])
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertEqual(raw, "")          # low-rc L1 excluded -> silent

        _write_mem(self.company, "L1-warm", "l1-highrc", tier="L1",
                   reinforce_count=3,
                   body="Chairman likes zeppelin airships as a hobby topic.")
        rc2, parsed2, _ = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc2, 0)
        self.assertIn("zeppelin",
                      parsed2["hookSpecificOutput"]["additionalContext"])

    # --- always exit 0 even on total garbage stdin ---------------------------
    def test_garbage_stdin_exit0(self):
        rc, _p, _r = _run(company=self.company, stdin="not-a-dict-just-a-string")
        self.assertEqual(rc, 0)

    # --- loose time bound on a ~150-memory corpus ----------------------------
    def test_fast_on_150_memories(self):
        topics = ["kubernetes", "postgres", "rust", "typescript", "terraform"]
        for i in range(150):
            _write_mem(self.company, "L2-cold", f"mem{i}",
                       body=f"Chairman note {i} about {topics[i % len(topics)]} "
                            f"and general workflow preferences.",
                       reinforce_count=(i % 3) + 1)
        _write_transcript(self.transcript, ["help me with my terraform setup"])
        start = time.time()
        rc, parsed, _ = _run(company=self.company, transcript=self.transcript)
        elapsed = time.time() - start
        self.assertEqual(rc, 0)
        self.assertIsNotNone(parsed)
        self.assertIn("terraform",
                      parsed["hookSpecificOutput"]["additionalContext"])
        # Generous: a subprocess spawn + stdlib scan of 150 files is well < 5s.
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
