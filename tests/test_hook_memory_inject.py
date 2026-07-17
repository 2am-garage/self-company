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
import subprocess
import tempfile
import time
import unittest

import _helpers  # noqa: F401 - puts scripts/ on sys.path

_spec = importlib.util.spec_from_file_location(
    "hook_memory_inject",
    os.path.join(_helpers.SCRIPTS_DIR, "hook_memory_inject.py"))
hmi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hmi)

# Phase 24 Item 2 — venv-gated regression fixture wiring (mirrors
# test_employee_memory.py's HAS_VENV pattern): these tests need REAL
# cross-lingual embeddings (a mocked rag_query.py cannot prove anything about
# the MODEL), so they build a real scratch index via the repo's own
# .company/.rag-venv and skip cleanly when it is absent.
REPO_VENV_DIR = os.path.join(_helpers.REPO_ROOT, ".company", ".rag-venv")
REPO_VENV_PY = os.path.join(REPO_VENV_DIR, "bin", "python")


def _has_rag_venv():
    if not (os.path.exists(REPO_VENV_PY) and os.access(REPO_VENV_PY, os.X_OK)):
        return False
    try:
        proc = subprocess.run([REPO_VENV_PY, "-c", "import lancedb, fastembed"],
                              capture_output=True, timeout=60)
        return proc.returncode == 0
    except Exception:
        return False


HAS_VENV = _has_rag_venv()


def _build_real_index(company):
    """Rebuild a scratch corpus's index via the REAL rag_index.py + venv (not
    the fake-bash-shim trick the other RAG tests use) — the whole point of this
    class is to exercise the REAL embedding model's cross-lingual behavior."""
    mem = os.path.join(company, "memory")
    return subprocess.run(
        [REPO_VENV_PY, os.path.join(_helpers.SCRIPTS_DIR, "rag_index.py"),
         "--memory-dir", mem, "--index-dir", os.path.join(mem, "index"), "--rebuild"],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "SC_RAG_REEXEC": "1"})


def _write_mem(company, tier_dir, name, *, body, tier="L2",
               reinforce_count=1, status="active", category="preferences",
               core=None):
    d = os.path.join(company, "memory", tier_dir, category)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name + ".md")
    core_line = f"core: {core}\n" if core is not None else ""
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            f"id: {name}\n"
            f"tier: {tier}\n"
            f"category: {category}\n"
            f"reinforce_count: {reinforce_count}\n"
            f"status: {status}\n"
            f"{core_line}"
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


def _fake_rag_venv(company, rag_query_body):
    """Plant a fake `.company/.rag-venv/bin/python` that intercepts rag_query.py
    (emitting `rag_query_body` as its shell body — echo canned JSON, sleep, exit N,
    etc.) and passes anything else through to the real interpreter. Also create a
    non-empty index dir so semantic_top's presence guard passes."""
    bindir = os.path.join(company, ".rag-venv", "bin")
    os.makedirs(bindir, exist_ok=True)
    py = os.path.join(bindir, "python")
    with open(py, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\n"
                'case "${1:-}" in\n'
                "  *rag_query.py)\n"
                f"    {rag_query_body}\n"
                "    ;;\n"
                '  *) exec python3 "$@" ;;\n'
                "esac\n")
    os.chmod(py, 0o755)
    idx = os.path.join(company, "memory", "index")
    os.makedirs(idx, exist_ok=True)
    with open(os.path.join(idx, "memory.lance"), "w") as f:
        f.write("x")   # make the index dir non-empty (presence guard)
    return py


def _hits_json(*rows):
    """Build a rag_query-shaped JSON array: rows are (path, tier, score)."""
    return json.dumps([{"id": os.path.basename(p).replace(".md", ""),
                        "tier": t, "path": p, "score": s} for (p, t, s) in rows])


def _hits_json_rr(*rows):
    """Build a rag_query --rerank shaped JSON array: rows are
    (path, tier, cosine_score, rerank_score) — Phase 24 Item 5. Lets the CONSUMER
    reranker gate be tested deterministically without loading the real model."""
    return json.dumps([{"id": os.path.basename(p).replace(".md", ""), "tier": t,
                        "path": p, "score": s, "rerank_score": rr}
                       for (p, t, s, rr) in rows])


class TestMemoryInjectRAG(unittest.TestCase):
    """Phase 13 Stage B.1 — semantic ask-time injection via rag_query, with the
    keyword path as the guaranteed fallback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.company = os.path.join(self.dir, ".company")
        os.makedirs(self.company)
        self.transcript = os.path.join(self.dir, "t.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    def test_paraphrase_injected_via_semantic(self):
        # Prompt shares NO keywords with the memory -> keyword path scores 0 and
        # would stay silent; semantic returns the memory by meaning -> injected.
        path = _write_mem(self.company, "L2-cold", "concurrency-style",
                          body="The Chairman prefers async/await for all "
                               "concurrency work in the codebase.")
        _write_transcript(self.transcript,
                          ["how should I lay out my parallel execution flow"])
        # sanity: with no venv this prompt injects nothing (pure keyword)
        rc0, parsed0, raw0 = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(raw0, "", "keyword path should be silent on a paraphrase")
        # now plant the RAG stack returning the memory with a high score
        _fake_rag_venv(self.company, f"echo '{_hits_json((path, 'L2', 0.71))}'")
        rc, parsed, _ = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(parsed, "semantic hit should be injected")
        self.assertIn("async/await",
                      parsed["hookSpecificOutput"]["additionalContext"])

    def test_no_venv_is_byte_for_byte_keyword(self):
        # With no venv, semantic_top returns None and behavior == the keyword path
        # exactly (relevant keyword prompt still injects; index dir absent).
        _write_mem(self.company, "L2-cold", "editor-preference",
                   body="The Chairman prefers the Neovim editor with a dark theme.")
        _write_transcript(self.transcript, ["set up my neovim colorscheme"])
        rc, parsed, _ = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertIn("Neovim",
                      parsed["hookSpecificOutput"]["additionalContext"])

    def test_rag_timeout_falls_back_to_keyword_under_budget(self):
        # rag_query hangs; the tight subprocess timeout caps it and the keyword
        # path takes over -> total well under the 30s hook budget.
        _write_mem(self.company, "L2-cold", "editor-preference",
                   body="The Chairman prefers the Neovim editor with a dark theme.")
        _write_transcript(self.transcript, ["set up my neovim colorscheme"])
        _fake_rag_venv(self.company, "sleep 30")   # hang
        env = {**os.environ, "SELF_COMPANY_INJECT_RAG_TIMEOUT": "1"}
        start = time.time()
        proc = __import__("subprocess").run(
            [__import__("sys").executable,
             os.path.join(_helpers.SCRIPTS_DIR, "hook_memory_inject.py"),
             "--company", self.company, "--transcript", self.transcript],
            capture_output=True, text=True, input="", env=env)
        elapsed = time.time() - start
        self.assertEqual(proc.returncode, 0)
        self.assertLess(elapsed, 10.0, "timeout must cap the hang well under 30s")
        # keyword fallback still injects the relevant memory
        self.assertIn("Neovim", proc.stdout)

    def test_tombstoned_hit_not_injected(self):
        # rag_query returns a path that is now tombstoned -> re-validation drops it;
        # nothing else matches -> silent (never inject a stale/tombstoned memory).
        path = _write_mem(self.company, "L2-cold", "old-editor",
                          body="The Chairman used to prefer the Emacs editor.",
                          status="archived")
        _write_transcript(self.transcript, ["configure my emacs setup"])
        _fake_rag_venv(self.company, f"echo '{_hits_json((path, 'L2', 0.80))}'")
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertEqual(raw, "", "tombstoned semantic hit must not be injected")

    def test_deleted_hit_not_injected(self):
        # rag_query returns a path to a file that no longer exists on disk.
        ghost = os.path.join(self.company, "memory", "L2-cold", "gone", "ghost.md")
        real = _write_mem(self.company, "L2-cold", "coffee",
                          body="The Chairman drinks oat-milk flat whites.")
        _write_transcript(self.transcript, ["what coffee does the chairman drink"])
        # index returns the GHOST (score high) plus nothing live-relevant
        _fake_rag_venv(self.company, f"echo '{_hits_json((ghost, 'L2', 0.90))}'")
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        # ghost dropped; semantic yields nothing -> keyword fallback finds 'coffee'
        self.assertIsNotNone(parsed)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("oat-milk", ctx)
        self.assertNotIn("ghost", ctx)

    def test_low_score_hit_gated_out(self):
        # A semantic hit BELOW the relevance floor is off-topic noise -> dropped;
        # with nothing else relevant -> keyword fallback -> silent on off-topic.
        path = _write_mem(self.company, "L2-cold", "editor",
                          body="The Chairman prefers the Neovim editor.")
        _write_transcript(self.transcript,
                          ["what is the boiling point of mercury"])
        _fake_rag_venv(self.company, f"echo '{_hits_json((path, 'L2', 0.12))}'")
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertEqual(raw, "", "below-floor semantic hit must not be injected")

    def test_rerank_below_cutoff_gated(self):
        # Phase 24 Item 5, deterministic: a hit that PASSES the cosine floor (0.45
        # >= 0.40) but scores BELOW the reranker cutoff is off-topic (the "gym
        # workout" class — cosine 0.417 but rerank ~-3.0) -> injected NOTHING.
        path = _write_mem(self.company, "L2-cold", "scheduler",
                          body="Fixed a cron scheduler time-dependency bug.")
        _write_transcript(self.transcript,
                          ["how should I schedule my morning gym workout"])
        _fake_rag_venv(self.company,
                       f"echo '{_hits_json_rr((path, 'L2', 0.45, -3.00))}'")
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertEqual(raw, "", "cosine-passing but reranker-rejected hit must not inject")

    def test_rerank_above_cutoff_injected(self):
        # Same hit but a rerank_score ABOVE the cutoff -> injected (real on-topic).
        path = _write_mem(self.company, "L2-cold", "merge",
                          body="The company may merge its own pull request when green.")
        _write_transcript(self.transcript, ["can the company merge its own PRs"])
        _fake_rag_venv(self.company,
                       f"echo '{_hits_json_rr((path, 'L2', 0.45, 1.50))}'")
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(parsed, "cosine+reranker passing hit must inject")
        self.assertIn("merge", parsed["hookSpecificOutput"]["additionalContext"])

    def test_no_rerank_score_is_cosine_only_degrade(self):
        # Reranker backend absent -> rag_query omits rerank_score -> the consumer
        # gate is the cosine floor alone, byte-identical to pre-Item-5. A hit above
        # the cosine floor with NO rerank_score injects.
        path = _write_mem(self.company, "L2-cold", "merge",
                          body="The company may merge its own pull request when green.")
        _write_transcript(self.transcript, ["can the company merge its own PRs"])
        _fake_rag_venv(self.company, f"echo '{_hits_json((path, 'L2', 0.45))}'")
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(parsed, "no rerank_score -> cosine floor decides -> inject")
        self.assertIn("merge", parsed["hookSpecificOutput"]["additionalContext"])

    def test_rerank_nonfinite_treated_below_cutoff(self):
        # A NaN rerank_score must be treated as below-cutoff (gate integrity),
        # never slip past `rr < RERANK_MIN_SCORE`.
        path = _write_mem(self.company, "L2-cold", "scheduler",
                          body="Fixed a cron scheduler time-dependency bug.")
        _write_transcript(self.transcript, ["schedule my gym workout"])
        body = ('echo \'[{"id":"scheduler","tier":"L2","path":"' + path
                + '","score":0.45,"rerank_score":NaN}]\'')
        _fake_rag_venv(self.company, body)
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertEqual(raw, "", "NaN rerank_score must be gated out")

    def test_budget_and_cap_never_exceeded_semantic(self):
        # Many high-score hits -> still capped at 5 and within CONTEXT_CHAR_CAP.
        long_body = ("neovim editor configuration workflow " * 30).strip()
        rows = []
        for i in range(9):
            p = _write_mem(self.company, "L2-cold", f"m{i}", body=long_body,
                           reinforce_count=i + 1)
            rows.append((p, "L2", 0.90 - i * 0.01))
        _write_transcript(self.transcript, ["neovim editor help"])
        _fake_rag_venv(self.company, f"echo '{_hits_json(*rows)}'")
        rc, parsed, _ = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(ctx), hmi.CONTEXT_CHAR_CAP)
        # at most cap 5 memory lines (+1 header)
        self.assertLessEqual(len(ctx.split("\n")), hmi.TOP_K_CAP + 1)

    def test_healthy_path_no_double_inject(self):
        # rag_query returns the same path twice -> injected once.
        path = _write_mem(self.company, "L2-cold", "editor",
                          body="The Chairman prefers the Neovim editor with a dark theme.")
        _write_transcript(self.transcript, ["neovim colorscheme"])
        _fake_rag_venv(self.company,
                       f"echo '{_hits_json((path, 'L2', 0.80), (path, 'L2', 0.79))}'")
        rc, parsed, _ = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx.count("Neovim"), 1, "must not double-inject one memory")

    def test_sc_no_rag_forces_keyword(self):
        # SC_NO_RAG=1 disables the semantic path even with a venv+index present.
        path = _write_mem(self.company, "L2-cold", "concurrency-style",
                          body="The Chairman prefers async/await for concurrency.")
        _write_transcript(self.transcript, ["parallel execution flow layout"])
        _fake_rag_venv(self.company, f"echo '{_hits_json((path, 'L2', 0.71))}'")
        env = {**os.environ, "SC_NO_RAG": "1"}
        proc = __import__("subprocess").run(
            [__import__("sys").executable,
             os.path.join(_helpers.SCRIPTS_DIR, "hook_memory_inject.py"),
             "--company", self.company, "--transcript", self.transcript],
            capture_output=True, text=True, input="", env=env)
        self.assertEqual(proc.returncode, 0)
        # paraphrase has no keyword overlap -> keyword path silent -> no injection
        self.assertEqual(proc.stdout.strip(), "")

    def test_garbage_tuning_envvars_never_crash_at_import(self):
        # P13B-1: a malformed value for any of the four env-tunable numbers must
        # NOT raise at import (module-level parse) — the always-on hook falls back
        # to the default and still runs / exits 0 / injects. Locks "never break the
        # Chairman's prompt".
        _write_mem(self.company, "L2-cold", "editor-preference",
                   body="The Chairman prefers the Neovim editor with a dark theme.")
        _write_transcript(self.transcript, ["set up my neovim colorscheme"])
        vars_ = ("SELF_COMPANY_INJECT_RAG_TIMEOUT",
                 "SELF_COMPANY_INJECT_RAG_MIN_SCORE",
                 "SELF_COMPANY_INJECT_TOPK",
                 "SELF_COMPANY_INJECT_HIGH_RC")
        for name in vars_:
            for bad in ("abc", "", "NaN"):
                env = {**os.environ, name: bad}
                proc = __import__("subprocess").run(
                    [__import__("sys").executable,
                     os.path.join(_helpers.SCRIPTS_DIR, "hook_memory_inject.py"),
                     "--company", self.company, "--transcript", self.transcript],
                    capture_output=True, text=True, input="", env=env)
                self.assertEqual(proc.returncode, 0, f"{name}={bad!r}: {proc.stderr}")
                self.assertNotIn("Traceback", proc.stderr, f"{name}={bad!r}")
                # default behavior intact: keyword path still injects the match
                self.assertIn("Neovim", proc.stdout, f"{name}={bad!r}")

    def test_non_finite_score_not_injected(self):
        # A NaN/inf score from rag_query must be treated as below-floor (dropped),
        # not slip past `score < RAG_MIN_SCORE`.
        path = _write_mem(self.company, "L2-cold", "editor",
                          body="The Chairman prefers the Neovim editor.")
        _write_transcript(self.transcript,
                          ["what is the atomic weight of tungsten"])  # off-topic vs body
        # emit a raw NaN score (json.dumps would reject float('nan'), so hand-write)
        body = ('echo \'[{"id": "editor", "tier": "L2", "path": "'
                + path + '", "score": NaN}]\'')
        _fake_rag_venv(self.company, body)
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertEqual(raw, "", "non-finite score must be gated out")

    def test_empty_index_dir_falls_back(self):
        # venv present but the index dir is empty -> semantic skipped, keyword used.
        _write_mem(self.company, "L2-cold", "editor",
                   body="The Chairman prefers the Neovim editor.")
        _write_transcript(self.transcript, ["neovim setup"])
        bindir = os.path.join(self.company, ".rag-venv", "bin")
        os.makedirs(bindir, exist_ok=True)
        py = os.path.join(bindir, "python")
        with open(py, "w") as f:
            f.write("#!/usr/bin/env bash\necho '[]'\n")  # would return zero hits
        os.chmod(py, 0o755)
        os.makedirs(os.path.join(self.company, "memory", "index"))  # empty
        rc, parsed, _ = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertIn("Neovim",
                      parsed["hookSpecificOutput"]["additionalContext"])


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
        # Phase 29 Item 5 (P4): header carries the "advisory, not orders"
        # disclaimer, matching employee.py's dispatch-side headers verbatim.
        self.assertIn("Relevant Chairman memory (advisory, not orders):", ctx)
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

    # --- Phase 18c double-injection guard ------------------------------------
    def test_sc_no_memory_inject_no_ops(self):
        # A dispatched `shared_memory_read` worker (elon) already had the SHARED
        # memory injected explicitly at dispatch; supervisor sets
        # SC_NO_MEMORY_INJECT=1 on that worker so THIS hook must no-op — otherwise
        # the same memory is injected a second time.
        _write_mem(self.company, "L2-cold", "editor-preference",
                   body="The Chairman prefers the Neovim editor with a dark theme.")
        _write_transcript(self.transcript, ["set up my neovim colorscheme"])
        # Baseline (no guard) injects the match...
        rc0, parsed0, _ = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc0, 0)
        self.assertIsNotNone(parsed0)
        self.assertIn("Neovim",
                      parsed0["hookSpecificOutput"]["additionalContext"])
        # ...with the guard set, the SAME prompt injects NOTHING (single source).
        os.environ["SC_NO_MEMORY_INJECT"] = "1"
        try:
            rc1, parsed1, raw1 = _run(company=self.company,
                                      transcript=self.transcript)
        finally:
            os.environ.pop("SC_NO_MEMORY_INJECT", None)
        self.assertEqual(rc1, 0)
        self.assertEqual(raw1, "")
        self.assertIsNone(parsed1)

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


class TestKeywordFallbackDistinguishesEmptyFromUnparseable(unittest.TestCase):
    """Phase 24 Item 2 fix (deps-free, no venv needed — pure logic bug):
    rank()'s recency-fallback branch must trigger ONLY for a truly empty/blank
    prompt, never for a non-empty prompt that merely tokenizes to nothing
    (e.g. pure CJK, since `_tokens` is ASCII-only) — otherwise an off-topic CJK
    prompt silently gets the freshest memories injected regardless of
    relevance, which is exactly what TestMultilingualRelevanceGate proves end-
    to-end (venv-gated) above."""

    @staticmethod
    def _candidate(id_, body, tier="L2", rc=1):
        fm = {"id": id_, "reinforce_count": rc, "last_reinforced": "2026-01-01"}
        return (tier, fm, body, f"/mem/{id_}.md")

    def test_truly_empty_prompt_falls_back_to_recency(self):
        cands = [self._candidate("a", "something"), self._candidate("b", "something else")]
        out = hmi.rank("", cands)
        self.assertEqual(len(out), 2)          # recency fallback returns candidates

    def test_blank_prompt_falls_back_to_recency(self):
        cands = [self._candidate("a", "something")]
        out = hmi.rank("   \n  ", cands)
        self.assertEqual(len(out), 1)

    def test_cjk_only_prompt_returns_nothing(self):
        cands = [self._candidate("a", "something"), self._candidate("b", "something else")]
        out = hmi.rank("義大利麵要怎麼煮？", cands)
        self.assertEqual(out, [], "a real (non-Latin) prompt must never take the recency fallback")


class TestFreshnessTieBreak(unittest.TestCase):
    """Freshness tie-break (decay_score): when candidates have identical keyword
    scores, rank by decay_score (higher = fresher). Degrade cleanly when missing."""

    @staticmethod
    def _c(id_, body, category="", rc=1, decay_score=None):
        fm = {"id": id_, "reinforce_count": rc, "category": category,
              "last_reinforced": "2026-01-01"}
        if decay_score is not None:
            fm["decay_score"] = decay_score
        return ("L2", fm, body, f"/mem/{id_}.md")

    def test_decay_score_tie_break_higher_first(self):
        # Two memories with identical overlap/tier/rc -> decay_score tie-breaks,
        # higher (fresher) ranks first.
        a = self._c("a", "neovim editor setup tips", rc=1, decay_score=0.95)
        b = self._c("b", "neovim editor configuration guide", rc=1, decay_score=0.60)
        out = hmi.rank("neovim editor help", [a, b])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][2], "neovim editor setup tips", "higher decay_score ranks first")
        self.assertEqual(out[1][2], "neovim editor configuration guide")

    def test_decay_score_missing_degrades_to_recency(self):
        # One memory has decay_score, one doesn't; both have same overlap/tier.
        # Missing decay_score degrades to recency key.
        a = self._c("a", "neovim editor tips", rc=1)  # no decay_score
        b = self._c("b", "neovim editor guide", rc=1, decay_score=0.50)
        b = ("L2", {**b[1], "last_reinforced": "2026-01-02"}, b[2], b[3])  # newer
        out = hmi.rank("neovim editor", [a, b])
        self.assertEqual(len(out), 2)
        # b has recency advantage (2026-01-02 > 2026-01-01)
        self.assertEqual(out[0][2], "neovim editor guide")

    def test_decay_score_zero_ranks_below_missing(self):
        # decay_score=0 (old) ranks below missing/empty (treated as 0.0), then
        # falls back to recency for final ordering.
        a = self._c("a", "neovim editor tips", rc=1, decay_score=0.0)
        b = self._c("b", "neovim editor guide", rc=1)  # no decay_score (0.0)
        # Same recency, so order is stable (both fall through to recency tie)
        out = hmi.rank("neovim editor", [a, b])
        self.assertEqual(len(out), 2)

    def test_decay_score_only_after_relevance_gates(self):
        # A memory with high decay_score but no overlap -> gated out, never ranks.
        high_decay = self._c("stale", "kubernetes deployment", rc=1, decay_score=0.99)
        on_topic = self._c("on", "neovim editor setup", rc=1, decay_score=0.50)
        out = hmi.rank("neovim editor help", [high_decay, on_topic])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][2], "neovim editor setup", "high decay_score does not bypass overlap gate")


class TestLoneTokenKeywordGate(unittest.TestCase):
    """Phase 24 R3 MUST-FIX 1 (deps-free): the three-part lone-token gate that
    stops a single incidental word from injecting on the no-venv keyword path.
    A shared PAIR of tokens always clears; a lone token must be corpus-rare (IDF),
    present in the BODY (not just the slug), long enough, and not a function word."""

    @staticmethod
    def _c(id_, body, category="", rc=2):
        fm = {"id": id_, "reinforce_count": rc, "category": category,
              "last_reinforced": "2026-01-01"}
        return ("L2", fm, body, f"/mem/{id_}.md")

    def test_lone_content_token_still_injects(self):
        # A rare, body-present, >=5-char content word on its own IS real signal.
        cands = [self._c("editor-pref", "The Chairman prefers the Neovim editor.")]
        out = hmi.rank("set up my neovim workspace", cands)
        self.assertEqual(len(out), 1, "a lone rare content token must still inject")

    def test_lone_slug_only_token_gated(self):
        # 'rules' appears only in the id/slug, never the body -> body-substance
        # gate drops it ("rules of cricket" must not hit git-identity-rules).
        cands = [self._c("git-identity-rules", "Keep the existing git identity; no attribution trailers.")]
        self.assertEqual(hmi.rank("what are the rules of cricket", cands), [])

    def test_lone_short_common_word_gated(self):
        # 'red' is in the body ('red-team') but < LONE_MIN_LEN -> gated; a small
        # corpus can't see it as common via IDF, so the length floor catches it.
        cands = [self._c("verify", "Always red-team every implementation before shipping.")]
        self.assertEqual(hmi.rank("how to get a red wine stain out", cands), [])

    def test_lone_corpus_common_token_gated(self):
        # A token in MANY memories is not discriminative: a lone match on it is
        # noise. Build a corpus where 'system' appears in > LONE_MAX_DF_RATIO of memories.
        cands = [self._c(f"m{i}", f"Note {i} about the build system and workflow.")
                 for i in range(10)]
        # 'system' is in all 10 -> df ratio 1.0 >> cap -> a lone 'system' match is gated.
        self.assertEqual(hmi.rank("how does the solar system work", cands), [])

    def test_multi_token_overlap_always_clears(self):
        # Two shared meaningful tokens is real signal regardless of the gate.
        cands = [self._c("chinese", "Reply to the Chairman in Traditional Chinese.")]
        out = hmi.rank("what language should replies to the chairman use chinese", cands)
        self.assertEqual(len(out), 1)


@unittest.skipUnless(HAS_VENV, "RAG venv/deps unavailable")
class TestMultilingualRelevanceGate(unittest.TestCase):
    """Phase 24 Item 2 — the hook's relevance-gate contract ("nothing above the
    floor -> inject nothing") must hold for NON-ENGLISH prompts, end-to-end
    through the REAL hook + a REAL rebuilt index (not a mocked rag_query.py —
    Item 1's fix IS the embedding model, so only real embeddings can prove it).

    Locks the live diagnosis (spec, Phase 24 Item 1): with the old
    English-only `bge-small-en-v1.5` model, EVERY prompt (on- or off-topic,
    any language) scored 0.45-0.65 cosine against the Chairman's memories, so
    the 0.30 floor filtered nothing — a genuinely off-topic Chinese prompt
    ("how do I cook pasta?") injected the Chairman's Chinese-language-
    preference memory, identical to what an on-topic query would surface.
    `test_offtopic_would_have_failed_pre_swap` reproduces that exact failure
    against the OLD model/floor to prove the regression is real, then the
    other two tests prove the CURRENT (multilingual model + retuned floor)
    behavior is correct."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.company = os.path.join(self.dir, ".company")
        os.makedirs(self.company)
        self.transcript = os.path.join(self.dir, "t.jsonl")
        # Symlink the WHOLE venv dir (not just the python binary) so pyvenv.cfg
        # is reachable — mirrors test_employee_memory.py's _make_company(venv=True).
        os.symlink(REPO_VENV_DIR, os.path.join(self.company, ".rag-venv"))
        # A small fixture corpus mirroring the LIVE memory that triggered the
        # diagnosis, plus decoys, so this stays deterministic and never touches
        # the real .company/memory store.
        _write_mem(self.company, "L2-cold", "chairman-reply-language-chinese",
                   body="When replying to the Chairman, staff must answer in "
                        "Traditional Chinese. Keep code, identifiers, file "
                        "paths, commands, and technical terms in English.")
        _write_mem(self.company, "L2-cold", "database-backup",
                   body="Maintains postgres databases and requires automated "
                        "nightly backups at 2am with dump rotation.")
        _write_mem(self.company, "L2-cold", "merge-gate",
                   body="The company may merge its own pull request without "
                        "waiting for approval, provided the full test suite "
                        "passes and integration checks are green.")
        rc = _build_real_index(self.company)
        if rc.returncode != 0:
            self.skipTest(f"rag_index unavailable/offline: {rc.stderr}")

    def tearDown(self):
        self._tmp.cleanup()

    def test_offtopic_chinese_prompt_injects_nothing(self):
        # "How do I cook pasta?" in Traditional Chinese — genuinely unrelated
        # to every fixture memory. Must clear NOTHING under the current
        # multilingual-model + retuned-floor behavior.
        _write_transcript(self.transcript, ["義大利麵要怎麼煮？"])
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertEqual(raw, "", "off-topic Chinese prompt must inject nothing")

    def test_ontopic_chinese_prompt_injects_right_memory(self):
        # Cross-lingual: a Traditional Chinese question about reply language
        # must retrieve the (English-body) chairman-reply-language-chinese
        # memory specifically, not a decoy — proving the multilingual model
        # actually bridges the language gap, not just "injects something".
        _write_transcript(self.transcript, ["回覆董事長要用什麼語言？"])
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(parsed, "on-topic Chinese prompt should inject")
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Traditional Chinese", ctx)

    def test_offtopic_would_have_failed_pre_swap(self):
        # Historical proof (Item 2's acceptance): re-run the SAME off-topic
        # Chinese prompt against the SAME fixture corpus, but scored the OLD
        # way — bge-small-en-v1.5 cosine, 0.30 floor — via a tiny inline
        # reproduction of the pre-Item-1 scoring (no network; loads the model
        # bge-small-en-v1.5 already ships in fastembed's local cache from any
        # prior warm-up, or downloads once). Confirms the bug was real: the
        # off-topic prompt's top score clears the OLD 0.30 floor.
        prog = (
            "import sys, json\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from fastembed import TextEmbedding\n"
            "m = TextEmbedding(model_name='BAAI/bge-small-en-v1.5')\n"
            "def emb(t): return list(m.embed([t]))[0]\n"
            "import numpy as np\n"
            "q = emb(sys.argv[2])\n"
            "bodies = json.loads(sys.argv[3])\n"
            "scores = []\n"
            "for b in bodies:\n"
            "    v = emb(b)\n"
            "    cos = float(np.dot(q, v) / (np.linalg.norm(q) * np.linalg.norm(v)))\n"
            "    scores.append(cos)\n"
            "print(json.dumps(scores))\n"
        )
        bodies = [
            "When replying to the Chairman, staff must answer in Traditional "
            "Chinese. Keep code, identifiers, file paths, commands, and "
            "technical terms in English.",
            "Maintains postgres databases and requires automated nightly "
            "backups at 2am with dump rotation.",
            "The company may merge its own pull request without waiting for "
            "approval, provided the full test suite passes and integration "
            "checks are green.",
        ]
        query = "義大利麵要怎麼煮？"
        proc = subprocess.run(
            [REPO_VENV_PY, "-c", prog, _helpers.SCRIPTS_DIR, query, json.dumps(bodies)],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "SC_RAG_REEXEC": "1"})
        if proc.returncode != 0:
            self.skipTest(f"legacy model unavailable/offline: {proc.stderr}")
        scores = json.loads(proc.stdout)
        old_floor = 0.30
        self.assertTrue(
            any(s >= old_floor for s in scores),
            f"expected the OLD English-only model to score >= {old_floor} on an "
            f"off-topic Chinese prompt (the live bug this phase fixes); got {scores}")


# A realistic-sized corpus (32 memories) mirroring the real .company themes —
# big enough to expose the off-topic-English keyword-collision bug that a 3-memory
# synthetic fixture hides (Gibby MUST-FIX 1). None of these bodies contain the
# CONTENT words of the off-topic probes below (tire/bike/risotto/photosynthesis/
# yoga/wine/…); the ONLY possible overlaps are generic connectors (change/without/
# going/make/…), which the hardened keyword gate must NOT treat as relevance.
_REALISTIC_CORPUS = [
    ("git-identity", "Keep the existing git identity; never add a Co-Authored-By or Claude attribution trailer to commits. Commits are the Chairman's only."),
    ("database-backups", "Maintains postgres databases and requires automated nightly backups at 2am with dump rotation."),
    ("merge-gate", "The company may merge its own pull request when the full test suite passes and integration checks are green."),
    ("verify-before-commit", "Never trust, always test: verify scripts actually run before committing, and red-team every implementation."),
    ("chinese-replies", "When replying to the Chairman, staff answer in Traditional Chinese, keeping code and identifiers in English."),
    ("list-format", "Prefers list format for summaries and overviews of complex information when detailed specs are unavailable."),
    ("completion-confirm", "Seeks explicit confirmation that work is actually implemented before considering a task finished."),
    ("delegation-phoebe", "Routes testing, skill optimization, and quality architecture work to Phoebe as collaborative design."),
    ("model-optimization", "Assigns agent models by task requirement, deploying stronger reasoning models for specification writing."),
    ("granular-commits", "Wants granular reversible commits pushed as a pull request rather than one large squashed change."),
    ("entropy-metric", "Treats entropy as the memory-quality KPI; after each maintenance cycle entropy should drop or stay flat."),
    ("approval-gate", "Structural changes need Elon sign-off; routine persona tweaks within scope do not require approval."),
    ("rag-connect", "The RAG index should be connected into the pipeline, never deleted; it is a derivative of markdown truth."),
    ("sub-agent-isolation", "Dispatches build work to employee subagents; Bob builds and Gibby attacks in separate isolated agents."),
    ("four-daily-runs", "Runs the company loop four times daily via cron to keep memory consolidation fresh."),
    ("token-budget", "Watches token cost across sub-companies; a holding orchestrator manages children instead of many crons."),
    ("permission-minimal", "Prefers minimal permission overhead and least-privilege capability slices for each employee."),
    ("repo-scoped", "The skill is repo-scoped; company memory stays private under a gitignored directory, never pushed."),
    ("org-hierarchy", "Elon is CEO reporting to the Chairman; Phoebe plans and dispatches; July stewards people and capabilities."),
    ("payroll-ops", "The Chairman runs actual company payroll operations and expects the org to model real execution structure."),
    ("diagnostic-first", "Sequences work diagnostic-first: measure the real problem before proposing or building a solution."),
    ("event-triggers", "Wants event-driven triggers designed with depth, not shallow polling, for autonomous work initiation."),
    ("inclusive-design", "Values inclusive, accessibility-conscious design in anything user-facing the company produces."),
    ("code-switching", "Communicates bilingually, code-switching between Chinese and English depending on the operational context."),
    ("weekly-research", "Mike runs a weekly external research survey to surface new tooling and capability options for review."),
    ("self-merge", "Since a stated date the Chairman authorizes self-merge of the company's own PR after a green test suite."),
    ("format-flexible", "Presentation format is flexible; readability for the Chairman matters more than a rigid template."),
    ("improvement-solicit", "Solicits improvement proposals before big decisions, expecting grounded metrics rather than gut calls."),
    ("decay-tiers", "Memory decays across L0, L1, and L2 tiers; durable identity-level facts are promoted to the cold tier."),
    ("supervisor-dispatch", "Autonomous dispatch flows through the supervisor, injecting standing directives into headless workers."),
    ("charter-authority", "Charter-level directives carry standing authority and supersede one-off instructions unless explicitly revoked."),
    ("scheduled-reports", "Tom produces a daily report so the Chairman can review company activity at a glance."),
]

# Gibby's 25 off-topic English probes (R2/R3). Genuinely unrelated to the corpus;
# the only possible token overlaps are generic function words / short common
# English words (change / rules / between / red / long / stay / day / …) — none of
# which the R3 keyword gate (corpus-IDF rarity + body-substance + length floor +
# function-word stoplist) may treat as relevance.
_OFFTOPIC_EN = [
    "How do I change a flat tire on a mountain bike?",
    "What's a good recipe for mushroom risotto?",
    "Can you explain how photosynthesis works?",
    "What are some beginner yoga poses I should try?",
    "How do I get a red wine stain out of a white shirt?",
    "What's the best way to brew espresso at home?",
    "Which planets in the solar system have rings?",
    "How long should I roast a whole chicken?",
    "How should I schedule my morning gym workout?",
    "What's the difference between a latte and a cappuccino?",
    "How do I train my puppy to sit and stay?",
    "What are the rules of cricket?",
    "How do I fold a fitted bed sheet neatly?",
    "What's a good beginner hike near the mountains?",
    "How do I keep basil plants alive indoors?",
    "What's the capital city of Australia?",
    "How do I tie a bow tie for a wedding?",
    "What temperature should I set my fridge to?",
    "How do I remove a splinter from my finger?",
    "What's a fun board game for four players?",
    "How do I make a paper airplane that flies far?",
    "What causes the northern lights?",
    "How much water should I drink each day?",
    "What's the best way to organize my sock drawer?",
    "How do I whistle with my fingers?",
]

# On-topic English probes that MUST still inject (guard against over-tightening).
_ONTOPIC_EN = [
    ("what's the rule about commit authorship and Claude attribution", "attribution"),
    ("how often are the databases backed up", "backups"),
    ("can the company merge its own pull requests", "merge"),
    ("does the chairman want confirmation before work is done", "confirmation"),
]


def _write_corpus(company, corpus):
    for name, body in corpus:
        _write_mem(company, "L2-cold", name, body=body, reinforce_count=2)


class TestOffTopicEnglishKeywordPath(unittest.TestCase):
    """Phase 24 MUST-FIX 1(b), DEPS-FREE (no venv): the NO-VENV keyword degrade
    path must not inject an unrelated memory on a single incidental connector
    word. Runs against a realistic 32-memory corpus (a 3-memory fixture hides
    this) with SC_NO_RAG so the semantic path is forced off — exactly the
    keyword-only path Gibby attacked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.company = os.path.join(self.dir, ".company")
        os.makedirs(self.company)
        self.transcript = os.path.join(self.dir, "t.jsonl")
        _write_corpus(self.company, _REALISTIC_CORPUS)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_no_rag(self):
        env = {**os.environ, "SC_NO_RAG": "1"}   # force the keyword path
        proc = subprocess.run(
            [__import__("sys").executable,
             os.path.join(_helpers.SCRIPTS_DIR, "hook_memory_inject.py"),
             "--company", self.company, "--transcript", self.transcript],
            capture_output=True, text=True, input="", env=env)
        return proc.returncode, proc.stdout.strip()

    def test_offtopic_english_injects_nothing_keyword_path(self):
        for prompt in _OFFTOPIC_EN:
            _write_transcript(self.transcript, [prompt])
            rc, out = self._run_no_rag()
            self.assertEqual(rc, 0)
            self.assertEqual(out, "",
                             f"off-topic English must inject nothing on the keyword "
                             f"path; leaked on: {prompt!r} -> {out[:160]}")

    def test_ontopic_english_still_injects_keyword_path(self):
        # Guard against over-tightening: real on-topic prompts must still inject.
        for prompt, needle in _ONTOPIC_EN:
            _write_transcript(self.transcript, [prompt])
            rc, out = self._run_no_rag()
            self.assertEqual(rc, 0)
            self.assertNotEqual(out, "",
                                f"on-topic English must still inject on the keyword "
                                f"path; went silent on: {prompt!r}")


class TestByteIdenticalOffTopicRegressionWithDecay(unittest.TestCase):
    """Byte-identical regression: off-topic prompts that inject nothing WITHOUT
    decay_score still inject nothing WITH decay_score present on every memory.
    Decay is a tie-break AFTER relevance gates; it must never weaken them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.company = os.path.join(self.dir, ".company")
        os.makedirs(self.company)
        self.transcript = os.path.join(self.dir, "t.jsonl")
        # A small corpus (same as the realistic one, with decay_score added).
        for name, body in [
            ("git-identity", "Keep the existing git identity; never add attribution."),
            ("database-backups", "Maintains postgres databases with automated backups at 2am."),
            ("merge-gate", "The company may merge its own PR when tests pass."),
        ]:
            _write_mem(self.company, "L2-cold", name, body=body, reinforce_count=1)
            # Inject decay_score into the frontmatter.
            path = os.path.join(self.company, "memory", "L2-cold", "preferences", name + ".md")
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            # Insert decay_score after tier line.
            lines = content.split("\n")
            insert_idx = next((i for i, l in enumerate(lines) if l.startswith("tier:")), 1) + 1
            lines.insert(insert_idx, "decay_score: 0.75")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

    def tearDown(self):
        self._tmp.cleanup()

    def test_offtopic_still_silent_with_decay_score(self):
        # "How do I cook pasta?" must still inject nothing, regardless of
        # decay_score on memories. Decay only tie-breaks among gated-in matches.
        _write_transcript(self.transcript, ["What's a good recipe for mushroom risotto?"])
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertEqual(raw, "", "off-topic must inject nothing even with decay_score present")

    def test_ontopic_still_injects_with_decay_score(self):
        # On-topic prompts still inject, with or without decay_score.
        _write_transcript(self.transcript, ["can the company merge its own PRs"])
        rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(parsed, "on-topic must still inject with decay_score present")
        self.assertIn("merge", parsed["hookSpecificOutput"]["additionalContext"])


@unittest.skipUnless(HAS_VENV, "RAG venv/deps unavailable")
class TestOffTopicEnglishSemanticPath(unittest.TestCase):
    """Phase 24 MUST-FIX 1(a), venv-gated: with the semantic path AVAILABLE, an
    off-topic English prompt whose nearest neighbors are all below the cosine
    floor must inject NOTHING — semantic_top() returns the INJECT_NOTHING
    verdict and run() must NOT fall through to the keyword gate. Real index over
    the realistic 32-memory corpus (the bug was invisible on a 3-memory one)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.company = os.path.join(self.dir, ".company")
        os.makedirs(self.company)
        self.transcript = os.path.join(self.dir, "t.jsonl")
        os.symlink(REPO_VENV_DIR, os.path.join(self.company, ".rag-venv"))
        _write_corpus(self.company, _REALISTIC_CORPUS)
        rc = _build_real_index(self.company)
        if rc.returncode != 0:
            self.skipTest(f"rag_index unavailable/offline: {rc.stderr}")

    def tearDown(self):
        self._tmp.cleanup()

    def test_offtopic_english_injects_nothing_with_venv(self):
        for prompt in _OFFTOPIC_EN:
            _write_transcript(self.transcript, [prompt])
            rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
            self.assertEqual(rc, 0)
            self.assertEqual(raw, "",
                             f"off-topic English must inject nothing with the semantic "
                             f"path available; leaked on: {prompt!r} -> {raw[:160]}")

    def test_ontopic_english_still_injects_with_venv(self):
        for prompt, _needle in _ONTOPIC_EN:
            _write_transcript(self.transcript, [prompt])
            rc, parsed, raw = _run(company=self.company, transcript=self.transcript)
            self.assertEqual(rc, 0)
            self.assertIsNotNone(parsed,
                                 f"on-topic English must still inject with venv; "
                                 f"went silent on: {prompt!r}")


@unittest.skipUnless(HAS_VENV, "RAG venv/deps unavailable")
class TestRerankerClosesGymCase(unittest.TestCase):
    """Phase 24 Item 5, venv-gated end-to-end: the innocent off-topic "schedule my
    gym workout" prompt cosine-matches a scheduler memory (~0.42, above the 0.40
    floor) but is REJECTED by the cross-encoder reranker (~-3.0) — the one residual
    the cosine floor alone could not close. On-topic still injects. Uses the REAL
    reranker model via the repo venv; skips if the reranker backend is absent."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.company = os.path.join(self.dir, ".company")
        os.makedirs(self.company)
        self.transcript = os.path.join(self.dir, "t.jsonl")
        os.symlink(REPO_VENV_DIR, os.path.join(self.company, ".rag-venv"))
        # A scheduler memory the gym prompt cosine-collides with (the real-corpus
        # trigger), plus a real on-topic control.
        _write_mem(self.company, "L2-cold", "scheduler-time-dependency",
                   body="Fixed a scheduler time-dependency issue where a cron "
                        "step ran at the wrong hour on the daily schedule.")
        _write_mem(self.company, "L2-cold", "merge-gate",
                   body="The company may merge its own pull request when the full "
                        "test suite passes and integration checks are green.")
        rc = _build_real_index(self.company)
        if rc.returncode != 0:
            self.skipTest(f"rag_index unavailable/offline: {rc.stderr}")
        # Reranker cold-load can exceed the default budget; widen it for the test.
        self._env = {**os.environ, "SELF_COMPANY_INJECT_RAG_TIMEOUT": "90"}

    def tearDown(self):
        self._tmp.cleanup()

    def _run_hook(self, prompt):
        _write_transcript(self.transcript, [prompt])
        proc = subprocess.run(
            [__import__("sys").executable,
             os.path.join(_helpers.SCRIPTS_DIR, "hook_memory_inject.py"),
             "--company", self.company, "--transcript", self.transcript],
            capture_output=True, text=True, input="", env=self._env)
        if proc.returncode != 0:
            self.skipTest("hook errored (reranker backend likely unavailable)")
        return proc.stdout.strip()

    def test_gym_workout_rejected_by_reranker(self):
        out = self._run_hook("How should I schedule my morning gym workout?")
        # If the reranker backend is genuinely absent, semantic_top times out or
        # falls back to cosine; the scheduler hit (cosine ~0.42) would then inject.
        # That degrade is acceptable and tested elsewhere — here we assert the
        # reranker (present in the repo venv) closes the leak.
        self.assertEqual(out, "",
                         f"reranker must reject the off-topic gym prompt; got: {out[:200]}")

    def test_ontopic_merge_still_injects(self):
        out = self._run_hook("can the company merge its own pull requests")
        self.assertNotEqual(out, "", "on-topic merge must still inject under the reranker")
        self.assertIn("merge", out)


def _write_policy(company, rows):
    """Write a scratch `org/policy.md` under `company` with one bold-value table
    row per (name, value) pair — the exact shape `policy_config.py` parses:
    "| `NAME` | **VALUE** | ... | tunable |"."""
    d = os.path.join(company, "org")
    os.makedirs(d, exist_ok=True)
    lines = ["| Constant | Default | Meaning | tunable |", "|---|---|---|---|"]
    for name, value in rows:
        lines.append(f"| `{name}` | **{value}** | test override | tunable |")
    with open(os.path.join(d, "policy.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class TestCoreIdentityBlockSelection(unittest.TestCase):
    """Mike 2026-07-16 Finding 2, deps-free unit tests (no subprocess): the
    explicit opt-in selection signal, the deterministic ordering, and the
    hard count/char caps for the always-on core identity block. Mirrors the
    direct-function-call style already used by TestFreshnessTieBreak /
    TestLoneTokenKeywordGate for hmi.rank()."""

    @staticmethod
    def _c(id_, body, tier="L2", rc=1, core=None):
        fm = {"id": id_, "reinforce_count": rc}
        if core is not None:
            fm["core"] = core
        return (tier, fm, body, f"/mem/{id_}.md")

    # --- explicit opt-in flag is the PRIMARY signal ---------------------
    def test_core_flag_selected_over_unflagged_high_rc(self):
        flagged = self._c("chairman-name", "The Chairman is Uwe.", rc=1, core="true")
        unflagged_high_rc = self._c("other-stable", "Some other stable fact.", rc=9)
        out = hmi.select_core_facts([flagged, unflagged_high_rc], max_count=5)
        # Flagged memories present -> ONLY the flagged pool is used, regardless
        # of a higher reinforce_count on an unflagged memory.
        self.assertEqual([c[3] for c in out], [flagged[3]])

    def test_core_flag_variants_are_truthy(self):
        for val in ("true", "True", "yes", "1", "on"):
            c = self._c("x", "body", core=val)
            self.assertEqual(hmi.select_core_facts([c], 5), [c], f"core: {val!r}")
        for val in ("false", "no", "0", "off", ""):
            c = self._c("x", "body", core=val, rc=1)  # rc below fallback floor too
            self.assertEqual(hmi.select_core_facts([c], 5), [],
                             f"core: {val!r} must not opt in")

    # --- fallback: no flag anywhere -> highest-reinforce_count L2 ONLY at/above
    #     the stable-trait bar (CORE_FALLBACK_MIN_RC) ----------------------
    def test_fallback_requires_min_reinforce_count(self):
        low = self._c("low", "Low-signal fact.", rc=hmi.CORE_FALLBACK_MIN_RC - 1)
        high = self._c("high", "Stable well-reinforced fact.",
                       rc=hmi.CORE_FALLBACK_MIN_RC)
        out = hmi.select_core_facts([low, high], max_count=5)
        self.assertEqual([c[3] for c in out], [high[3]],
                         "only the memory at/above the stable-trait bar qualifies")

    def test_no_flag_and_all_below_floor_selects_nothing(self):
        cands = [self._c(f"m{i}", f"note {i}", rc=1) for i in range(5)]
        self.assertEqual(hmi.select_core_facts(cands, max_count=5), [])

    # --- L1 is never eligible for the core block (L2-only, per spec) -----
    def test_l1_never_selected_even_if_flagged(self):
        l1_flagged = self._c("l1x", "L1 fact.", tier="L1", rc=9, core="true")
        out = hmi.select_core_facts([l1_flagged], max_count=5)
        self.assertEqual(out, [], "L1 memories are out of scope for the core block")

    # --- deterministic ordering: reinforce_count desc, then id asc -------
    def test_ordering_is_reinforce_count_desc_then_id(self):
        a = self._c("bbb", "b fact", rc=5, core="true")
        b = self._c("aaa", "a fact", rc=9, core="true")
        c = self._c("ccc", "c fact", rc=5, core="true")
        out = hmi.select_core_facts([a, b, c], max_count=5)
        self.assertEqual([o[3] for o in out], [b[3], a[3], c[3]])

    # --- hard cap by COUNT -------------------------------------------------
    def test_count_cap_enforced(self):
        cands = [self._c(f"m{i}", f"fact {i}", rc=5, core="true") for i in range(9)]
        out = hmi.select_core_facts(cands, max_count=3)
        self.assertEqual(len(out), 3)

    def test_zero_max_count_selects_nothing(self):
        cands = [self._c("m", "fact", rc=5, core="true")]
        self.assertEqual(hmi.select_core_facts(cands, max_count=0), [])

    # --- hard cap by CHARS ---------------------------------------------
    def test_char_cap_enforced_on_rendered_block(self):
        long_body = ("identity fact " * 50).strip()   # well over the per-mem cap
        cands = [self._c(f"m{i}", long_body, rc=5, core="true") for i in range(5)]
        selected = hmi.select_core_facts(cands, max_count=5)
        # Budget room for the header + exactly ONE (truncated) fact line, not two.
        ctx = hmi.build_core_context(selected, char_cap=300)
        self.assertLessEqual(len(ctx), 300)
        self.assertIn(hmi.CORE_HEADER, ctx)
        self.assertEqual(ctx.count("\n- "), 1,
                         "the char cap must stop a second fact line from fitting")
        # A much larger budget lets every selected fact through.
        ctx_big = hmi.build_core_context(selected, char_cap=5000)
        self.assertEqual(ctx_big.count("\n- "), len(selected))

    def test_char_cap_zero_yields_nothing(self):
        cands = [self._c("m", "fact", rc=5, core="true")]
        selected = hmi.select_core_facts(cands, max_count=5)
        self.assertEqual(hmi.build_core_context(selected, char_cap=0), "")

    # --- empty-core degrades cleanly ---------------------------------------
    def test_no_facts_selected_renders_empty_string(self):
        self.assertEqual(hmi.build_core_context([], char_cap=500), "")


class TestCoreIdentityBlockEndToEnd(unittest.TestCase):
    """End-to-end (real hook subprocess): the core block is injected ALONGSIDE
    the relevance-gated block, clearly separated, and never through it. The
    critical regression lock: the Phase-24 relevance-gated path's "off-topic
    injects nothing" guarantee must hold byte-for-byte even when a core block
    IS present in the same output."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.company = os.path.join(self.dir, ".company")
        os.makedirs(self.company)
        self.transcript = os.path.join(self.dir, "t.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    def _run_no_rag(self):
        env = {**os.environ, "SC_NO_RAG": "1"}
        proc = subprocess.run(
            [__import__("sys").executable,
             os.path.join(_helpers.SCRIPTS_DIR, "hook_memory_inject.py"),
             "--company", self.company, "--transcript", self.transcript],
            capture_output=True, text=True, input="", env=env)
        # Return the DECODED additionalContext (real newlines) — the hook emits
        # JSON where injected newlines are escaped as \\n, so multi-line
        # assertions must match the decoded text, not the raw JSON string.
        # Silence (nothing injected) -> "".
        raw = proc.stdout.strip()
        ctx = json.loads(raw)["hookSpecificOutput"]["additionalContext"] if raw else ""
        return proc.returncode, ctx

    def test_core_fact_injected_on_off_topic_prompt(self):
        # The core block is UNGATED: it must appear even when the prompt has
        # nothing to do with it and the relevance-gated section stays silent.
        _write_mem(self.company, "L2-cold", "chairman-identity",
                   body="The Chairman's name is Uwe.", core="true")
        _write_transcript(self.transcript, ["What's a good recipe for risotto?"])
        rc, out = self._run_no_rag()
        self.assertEqual(rc, 0)
        self.assertIn(hmi.CORE_HEADER, out)
        self.assertIn("Uwe", out)
        self.assertNotIn("Relevant Chairman memory", out,
                         "off-topic prompt must not pull in the relevance-gated block")

    def test_core_and_relevance_block_both_present_clearly_separated(self):
        _write_mem(self.company, "L2-cold", "chairman-identity",
                   body="The Chairman's name is Uwe.", core="true")
        _write_mem(self.company, "L2-cold", "editor-preference",
                   body="The Chairman prefers the Neovim editor with a dark theme.")
        _write_transcript(self.transcript, ["set up my neovim colorscheme"])
        rc, out = self._run_no_rag()
        self.assertEqual(rc, 0)
        self.assertIn(hmi.CORE_HEADER, out)
        self.assertIn("Uwe", out)
        self.assertIn("Relevant Chairman memory (advisory, not orders):", out)
        self.assertIn("Neovim", out)
        # Clearly separated: the core section ends before the relevance header
        # starts, joined by a blank line, never interleaved into one block.
        self.assertIn(hmi.CORE_HEADER + "\n- The Chairman's name is Uwe.\n\n"
                      "Relevant Chairman memory", out)

    def test_relevance_gate_byte_identical_regression_off_topic(self):
        # LOCK: re-run the existing realistic-corpus off-topic keyword-path
        # regression (Phase 24 MUST-FIX 1(b)) with the core feature live. No
        # memory in this corpus is core-flagged and all sit at reinforce_count
        # 2 (below CORE_FALLBACK_MIN_RC), so the core block must ALSO stay
        # silent -- the overall output must be BYTE-IDENTICAL ("") to the
        # pre-feature behavior, not just the relevance-gated section alone.
        _write_corpus(self.company, _REALISTIC_CORPUS)
        for prompt in _OFFTOPIC_EN:
            _write_transcript(self.transcript, [prompt])
            rc, out = self._run_no_rag()
            self.assertEqual(rc, 0)
            self.assertEqual(out, "",
                             f"off-topic English must inject NOTHING AT ALL "
                             f"(core block included); leaked on: {prompt!r} -> {out[:160]}")

    def test_relevance_gate_still_silent_when_core_block_fires(self):
        # A stronger version of the same lock: even when the core block DOES
        # fire (a flagged fact present), the relevance-gated section must stay
        # exactly as silent on an off-topic prompt as it was pre-feature.
        _write_corpus(self.company, _REALISTIC_CORPUS)
        _write_mem(self.company, "L2-cold", "chairman-identity",
                   body="The Chairman's name is Uwe.", core="true")
        _write_transcript(self.transcript, ["How do I get a red wine stain out?"])
        rc, out = self._run_no_rag()
        self.assertEqual(rc, 0)
        self.assertIn(hmi.CORE_HEADER, out, "core block should still fire")
        self.assertNotIn("Relevant Chairman memory", out,
                         "relevance-gated section must stay silent on an off-topic prompt")
        # And on-topic still injects through the (untouched) relevance path.
        _write_transcript(self.transcript, ["can the company merge its own PRs"])
        rc2, out2 = self._run_no_rag()
        self.assertEqual(rc2, 0)
        self.assertIn("Relevant Chairman memory (advisory, not orders):", out2)
        self.assertIn("merge", out2)

    def test_empty_core_degrades_cleanly_leaves_relevance_output_unchanged(self):
        # No core-eligible memory at all (no flag, rc below the fallback
        # floor) -> the whole output is exactly what build_context() alone
        # would have produced -- no CORE_HEADER anywhere.
        _write_mem(self.company, "L2-cold", "editor-preference",
                   body="The Chairman prefers the Neovim editor with a dark theme.")
        _write_transcript(self.transcript, ["set up my neovim colorscheme"])
        rc, out = self._run_no_rag()
        self.assertEqual(rc, 0)
        self.assertNotIn(hmi.CORE_HEADER, out)
        self.assertTrue(out.startswith("Relevant Chairman memory (advisory, not orders):"))

    def test_count_and_char_cap_hold_end_to_end(self):
        for i in range(9):
            _write_mem(self.company, "L2-cold", f"core{i}",
                       body=("identity fact number " + str(i) + " " * 5) * 10,
                       core="true")
        _write_transcript(self.transcript, ["totally unrelated off-topic prompt"])
        rc, out = self._run_no_rag()
        self.assertEqual(rc, 0)
        self.assertIn(hmi.CORE_HEADER, out)
        core_section = out.split("\n\n", 1)[0]
        self.assertLessEqual(len(core_section), hmi.DEFAULT_CORE_CHAR_CAP)
        # at most cap 5 fact lines + 1 header line
        self.assertLessEqual(len(core_section.split("\n")), hmi.DEFAULT_CORE_MAX_COUNT + 1)


class TestCoreIdentityBlockPolicyConfig(unittest.TestCase):
    """Mike 2026-07-16 Finding 2: ENABLE/MAX_COUNT/CHAR_CAP are tunable via
    org/policy.md through the shared policy_config resolver (decay.py's own
    convention), with an env-var override on top. Direct calls to the private
    `_core_config()` resolver -- deps-free, no subprocess needed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.company = os.path.join(self.dir, ".company")
        os.makedirs(self.company)

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_with_no_policy_file(self):
        enable, max_count, char_cap = hmi._core_config(self.company)
        self.assertEqual(enable, hmi.DEFAULT_CORE_ENABLE)
        self.assertEqual(max_count, hmi.DEFAULT_CORE_MAX_COUNT)
        self.assertEqual(char_cap, hmi.DEFAULT_CORE_CHAR_CAP)

    def test_policy_md_overrides_max_count_and_char_cap(self):
        _write_policy(self.company, [("CORE_MEMORY_MAX_COUNT", 2),
                                     ("CORE_MEMORY_CHAR_CAP", 123)])
        enable, max_count, char_cap = hmi._core_config(self.company)
        self.assertTrue(enable)
        self.assertEqual(max_count, 2)
        self.assertEqual(char_cap, 123)

    def test_policy_md_disables_core_block(self):
        _write_policy(self.company, [("CORE_MEMORY_ENABLE", 0)])
        enable, _max_count, _char_cap = hmi._core_config(self.company)
        self.assertFalse(enable)

    def test_env_override_wins_over_policy_md(self):
        _write_policy(self.company, [("CORE_MEMORY_MAX_COUNT", 2)])
        os.environ["SELF_COMPANY_INJECT_CORE_MAX_COUNT"] = "9"
        try:
            _enable, max_count, _char_cap = hmi._core_config(self.company)
        finally:
            os.environ.pop("SELF_COMPANY_INJECT_CORE_MAX_COUNT", None)
        self.assertEqual(max_count, 9)

    def test_disable_via_policy_end_to_end_suppresses_core_block(self):
        _write_policy(self.company, [("CORE_MEMORY_ENABLE", 0)])
        _write_mem(self.company, "L2-cold", "chairman-identity",
                   body="The Chairman's name is Uwe.", core="true")
        transcript = os.path.join(self.dir, "t.jsonl")
        _write_transcript(transcript, ["unrelated off-topic prompt about wine stains"])
        env = {**os.environ, "SC_NO_RAG": "1"}
        proc = subprocess.run(
            [__import__("sys").executable,
             os.path.join(_helpers.SCRIPTS_DIR, "hook_memory_inject.py"),
             "--company", self.company, "--transcript", transcript],
            capture_output=True, text=True, input="", env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "",
                         "CORE_MEMORY_ENABLE=0 via policy.md must fully suppress the block")


if __name__ == "__main__":
    unittest.main()
