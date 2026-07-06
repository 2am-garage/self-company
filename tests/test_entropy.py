"""
Tests for entropy.py — the four entropy dimensions + policy-driven weights.

Black-box via the CLI (JSON output) so tests are decoupled from internal dict
shapes, plus a provenance check that weights now come from policy (P1/P3).
"""

import os
import tempfile
import unittest

import _helpers

REAL_POLICY = os.path.join(
    _helpers.REPO_ROOT, "skills", "self-company", "assets", "company-template", "org", "policy.md")


class TestDimensions(unittest.TestCase):
    def _entropy(self, d):
        return _helpers.run_json("entropy.py", "--memory-dir", d,
                                 "--now", "2026-06-25", "--config", "/nonexistent.md")

    def test_empty_is_zero(self):
        with tempfile.TemporaryDirectory() as d:
            data = self._entropy(d)
            self.assertEqual(data["total_memories"], 0)
            self.assertEqual(data["entropy"], 0.0)

    def test_duplicate_pair_detected(self):
        with tempfile.TemporaryDirectory() as d:
            body = "The Chairman prefers async await patterns in Python design clearly."
            _helpers.write_memory(os.path.join(d, "L0-working", "d1.md"),
                                  id="pref-async-1", body=body)
            _helpers.write_memory(os.path.join(d, "L0-working", "d2.md"),
                                  id="pref-async-2", body=body)
            data = self._entropy(d)
            self.assertGreater(data["dimensions"]["dup_rate"], 0.0)
            self.assertEqual(len(data["details"]["duplicate_pairs"]), 1)

    def test_absorbed_excluded_from_totals_and_dups(self):
        # Phase 6 Item 1: an `absorbed` tombstone (consolidation-agent merge)
        # is excluded from active scans — it must not count in total_memories
        # nor re-surface as a duplicate candidate against its canonical.
        with tempfile.TemporaryDirectory() as d:
            body = "The Chairman prefers async await patterns in Python design clearly."
            _helpers.write_memory(os.path.join(d, "L0-working", "canon.md"),
                                  id="pref-async-canon", body=body)
            _helpers.write_memory(os.path.join(d, "L0-working", "dup.md"),
                                  id="pref-async-dup", body=body, status="absorbed")
            data = self._entropy(d)
            self.assertEqual(data["total_memories"], 1)  # absorbed not counted
            self.assertEqual(data["details"]["duplicate_pairs"], [])
            self.assertEqual(data["dimensions"]["dup_rate"], 0.0)

    def test_absorbed_included_under_include_archived(self):
        # --include-archived brings the tombstone back into scope (and the
        # dup pair re-appears), proving it was excluded by status, not lost.
        with tempfile.TemporaryDirectory() as d:
            body = "The Chairman prefers async await patterns in Python design clearly."
            _helpers.write_memory(os.path.join(d, "L0-working", "canon.md"),
                                  id="pref-async-canon", body=body)
            _helpers.write_memory(os.path.join(d, "L0-working", "dup.md"),
                                  id="pref-async-dup", body=body, status="absorbed")
            data = _helpers.run_json("entropy.py", "--memory-dir", d, "--now",
                                     "2026-06-25", "--config", "/nonexistent.md",
                                     "--include-archived")
            self.assertEqual(data["total_memories"], 2)
            self.assertEqual(len(data["details"]["duplicate_pairs"]), 1)

    def test_contradiction_detected(self):
        # Same slug family (pref-*) with opposing keywords async/sync.
        with tempfile.TemporaryDirectory() as d:
            _helpers.write_memory(os.path.join(d, "L0-working", "c1.md"),
                                  id="pref-mode-1",
                                  body="Chairman likes async patterns and wants async everywhere.")
            _helpers.write_memory(os.path.join(d, "L0-working", "c2.md"),
                                  id="pref-mode-2",
                                  body="Chairman dislikes async, prefers sync and wants sync everywhere.")
            data = self._entropy(d)
            self.assertGreater(data["dimensions"]["contradiction_score"], 0.0,
                               "expected a contradiction candidate")

    def test_stale_detected(self):
        # L0 memory far past the drop threshold counts as stale.
        with tempfile.TemporaryDirectory() as d:
            _helpers.write_memory(os.path.join(d, "L0-working", "s.md"),
                                  id="old-1", last_reinforced="2026-01-01")
            data = _helpers.run_json("entropy.py", "--memory-dir", d,
                                     "--now", "2026-06-25", "--config", REAL_POLICY)
            self.assertGreater(data["dimensions"]["stale_rate"], 0.0)
            self.assertIn("old-1", data["details"]["stale_ids"])

    def test_unverified_detected(self):
        with tempfile.TemporaryDirectory() as d:
            # sourced but NOT verified -> unverified (the honest, new definition)
            _helpers.write_memory(os.path.join(d, "L0-working", "a.md"), id="needs-verify")
            # no sources -> unverified (can never be verified)
            _helpers.write_memory(os.path.join(d, "L0-working", "b.md"), id="nosrc-1", sources="[]")
            # has verified_date -> NOT unverified
            with open(os.path.join(d, "L0-working", "c.md"), "w") as f:
                f.write("---\nid: verified-1\ntier: L0\nowner: Tony\n"
                        'sources: ["[s#1]"]\ncreated: 2026-06-01\nlast_reinforced: 2026-06-01\n'
                        "reinforce_count: 1\ndecay_score: 1.0\nstatus: active\n"
                        "verified_date: 2026-06-02\nverified_by: Gibby\n---\nbody\n")
            data = self._entropy(d)
            ids = data["details"]["unverified_ids"]
            self.assertIn("needs-verify", ids)
            self.assertIn("nosrc-1", ids)
            self.assertNotIn("verified-1", ids)
            self.assertAlmostEqual(data["dimensions"]["unverified_rate"], 2 / 3, places=2)


class TestWeightProvenance(unittest.TestCase):
    def test_weights_from_policy(self):
        with tempfile.TemporaryDirectory() as d:
            data = _helpers.run_json("entropy.py", "--memory-dir", d,
                                     "--now", "2026-06-25", "--config", REAL_POLICY)
            self.assertEqual(data["weights"],
                             {"w1": 0.25, "w2": 0.35, "w3": 0.2, "w4": 0.2})
            self.assertEqual(data["config"]["sources"]["W1_DUP"], "policy")

    def test_tuning_weight_in_table_changes_weights(self):
        with open(REAL_POLICY, encoding="utf-8") as f:
            text = f.read()
        tuned = text.replace("| `w1` (duplication) | **0.25**",
                             "| `w1` (duplication) | **0.50**")
        self.assertNotEqual(tuned, text, "w1 policy fixture line not found")
        with tempfile.TemporaryDirectory() as d:
            pol = os.path.join(d, "policy.md")
            with open(pol, "w", encoding="utf-8") as f:
                f.write(tuned)
            data = _helpers.run_json("entropy.py", "--memory-dir", d,
                                     "--now", "2026-06-25", "--config", pol)
            self.assertEqual(data["weights"]["w1"], 0.5)


def _charter_mem(path, *, id, provenance=None, source="[s#1]"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    prov = f"provenance: {provenance}\n" if provenance else ""
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            f'---\nid: {id}\ntier: L0\nowner: Tony\nsources: ["{source}"]\n'
            f"created: 2026-06-01\nlast_reinforced: 2026-06-01\nreinforce_count: 1\n"
            f"decay_score: 1.0\nstatus: active\n{prov}---\nbody\n")


class TestCharterExclusion(unittest.TestCase):
    """Item 6 — blessed charter memories are excluded from unverified_rate;
    non-blessed charter claims are NOT trusted, only surfaced."""

    def _entropy(self, d, adj="/nonexistent.md"):
        return _helpers.run_json("entropy.py", "--memory-dir", d, "--now",
                                 "2026-06-25", "--config", "/nonexistent.md",
                                 "--adjudications", adj)

    def test_blessed_charter_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            # blessed seed, no verified_date, charter provenance -> excluded
            _charter_mem(os.path.join(d, "L0-working", "seed.md"),
                         id="org-hierarchy", provenance="charter",
                         source="charter:org-hierarchy")
            # ordinary unverified memory -> counted
            _helpers.write_memory(os.path.join(d, "L0-working", "n.md"),
                                  id="needs-verify")
            data = self._entropy(d)
            ids = data["details"]["unverified_ids"]
            self.assertNotIn("org-hierarchy", ids)
            self.assertIn("needs-verify", ids)
            # charter dropped from BOTH numerator and denominator: 1 of 1 counts
            self.assertAlmostEqual(data["dimensions"]["unverified_rate"], 1.0, places=4)

    def test_nonblessed_charter_not_trusted_but_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            _charter_mem(os.path.join(d, "L0-working", "fake.md"),
                         id="fake-axiom", provenance="charter",
                         source="charter:fake-axiom")
            data = self._entropy(d)
            # still counted as unverified (anti-abuse), AND surfaced as suspicious
            self.assertIn("fake-axiom", data["details"]["unverified_ids"])
            self.assertIn("fake-axiom", data["details"]["suspicious_charter_ids"])


class TestAdjudicationLedger(unittest.TestCase):
    """Item 7 — persisted distinct verdicts drop pairs from candidates + score."""

    _BODY = "The Chairman prefers async await patterns in Python design clearly."

    def _dup(self, d, adj):
        return _helpers.run_json("entropy.py", "--memory-dir", d, "--now",
                                 "2026-06-25", "--config", "/nonexistent.md",
                                 "--adjudications", adj)

    def _two_dups(self, d):
        _helpers.write_memory(os.path.join(d, "L0-working", "a.md"),
                              id="dup-a", body=self._BODY)
        _helpers.write_memory(os.path.join(d, "L0-working", "b.md"),
                              id="dup-b", body=self._BODY)

    def _ledger(self, path, rows):
        with open(path, "w", encoding="utf-8") as f:
            f.write("| id_a | id_b | verdict | by | date | reason |\n")
            f.write("|---|---|---|---|---|---|\n")
            for a, b, v in rows:
                f.write(f"| {a} | {b} | {v} | Tony | 2026-06-25 | test |\n")

    def test_distinct_pair_dropped_and_uncounted(self):
        with tempfile.TemporaryDirectory() as d:
            self._two_dups(d)
            # baseline: no ledger -> the pair is a scored duplicate
            base = self._dup(d, "/nonexistent.md")
            self.assertEqual(len(base["details"]["duplicate_pairs"]), 1)
            self.assertGreater(base["dimensions"]["dup_rate"], 0.0)
            # adjudicate distinct -> pair gone, dup_rate back to 0
            adj = os.path.join(d, "adj.md")
            self._ledger(adj, [("dup-b", "dup-a", "distinct")])  # unordered
            out = self._dup(d, adj)
            self.assertEqual(out["details"]["duplicate_pairs"], [])
            self.assertEqual(out["dimensions"]["dup_rate"], 0.0)
            self.assertEqual(out["adjudications"]["distinct_pairs"], 1)

    def test_unlisted_pair_behaves_as_before(self):
        with tempfile.TemporaryDirectory() as d:
            self._two_dups(d)
            adj = os.path.join(d, "adj.md")
            self._ledger(adj, [("other-x", "other-y", "distinct")])
            out = self._dup(d, adj)
            self.assertEqual(len(out["details"]["duplicate_pairs"]), 1)

    def test_stale_guard_missing_id_no_crash(self):
        with tempfile.TemporaryDirectory() as d:
            self._two_dups(d)
            adj = os.path.join(d, "adj.md")
            # one real id + one nonexistent id: entry is inert, no error
            self._ledger(adj, [("dup-a", "ghost-id", "distinct")])
            out = self._dup(d, adj)
            self.assertEqual(len(out["details"]["duplicate_pairs"]), 1)

    def test_duplicate_verdict_also_dropped_and_uncounted(self):
        # Phase 6 Item 3: a `duplicate`-adjudicated pair ("already judged, being
        # resolved via tombstone/reap") is ALSO omitted from candidates and does
        # not count toward dup_rate — not just `distinct`.
        with tempfile.TemporaryDirectory() as d:
            self._two_dups(d)
            adj = os.path.join(d, "adj.md")
            self._ledger(adj, [("dup-b", "dup-a", "duplicate")])  # unordered
            out = self._dup(d, adj)
            self.assertEqual(out["details"]["duplicate_pairs"], [])
            self.assertEqual(out["dimensions"]["dup_rate"], 0.0)
            # provenance surfaced (extend-not-break): distinct count stays 0,
            # duplicate/suppressed counts reflect the new verdict.
            self.assertEqual(out["adjudications"]["distinct_pairs"], 0)
            self.assertEqual(out["adjudications"]["duplicate_pairs"], 1)
            self.assertEqual(out["adjudications"]["suppressed_pairs"], 1)

    def test_stale_guard_missing_id_duplicate_verdict_inert(self):
        with tempfile.TemporaryDirectory() as d:
            self._two_dups(d)
            adj = os.path.join(d, "adj.md")
            self._ledger(adj, [("dup-a", "ghost-id", "duplicate")])
            out = self._dup(d, adj)
            self.assertEqual(len(out["details"]["duplicate_pairs"]), 1)


class TestDefunctParity(unittest.TestCase):
    """Phase 4 Item 5 — entropy treats `defunct` like `archived` (decay.py
    parity): merged-away stubs must not count in total_memories or any rate
    during the reap grace window. --include-archived still includes them."""

    _BODY = "The Chairman prefers async await patterns in Python design clearly."

    def _corpus(self, d):
        _helpers.write_memory(os.path.join(d, "L0-working", "live1.md"),
                              id="live-1", body=self._BODY)
        _helpers.write_memory(os.path.join(d, "L0-working", "live2.md"),
                              id="live-2",
                              body="Gibby attacks every build with scratch fixtures before merge.")
        # defunct stub duplicates live-1 word-for-word: if it were counted it
        # would add a scored duplicate pair and land in unverified_ids
        _helpers.write_memory(os.path.join(d, "L0-working", "stub.md"),
                              id="stub-1", body=self._BODY, status="defunct")

    def _entropy(self, d, *extra):
        rc, out, err = _helpers.run_script(
            "entropy.py", "--memory-dir", d, "--now", "2026-06-25",
            "--config", "/nonexistent.md", "--adjudications", "/nonexistent.md",
            *extra, env={"SC_NO_RAG": "1"})  # Jaccard-only: fast + deterministic
        self.assertEqual(rc, 0, err)
        import json
        return json.loads(out)

    def test_defunct_excluded_from_totals_and_rates(self):
        with tempfile.TemporaryDirectory() as d:
            self._corpus(d)
            data = self._entropy(d)
            self.assertEqual(data["total_memories"], 2)
            self.assertEqual(data["details"]["duplicate_pairs"], [])
            self.assertEqual(data["dimensions"]["dup_rate"], 0.0)
            self.assertNotIn("stub-1", data["details"]["unverified_ids"])

    def test_include_archived_still_includes_defunct(self):
        with tempfile.TemporaryDirectory() as d:
            self._corpus(d)
            data = self._entropy(d, "--include-archived")
            self.assertEqual(data["total_memories"], 3)
            # once included, the word-identical stub IS a scored duplicate
            self.assertEqual(sorted(data["details"]["duplicate_pairs"][0]),
                             ["live-1", "stub-1"])


class TestNoRagReasonString(unittest.TestCase):
    """C1 — the fallback reason names the actual trigger: SC_NO_RAG=1 says
    force-disabled (not 'absent'); a genuinely absent backend says absent."""

    _BODY_A = "Chairman prefers offline embedding models for privacy reasons always."
    _BODY_B = "The Chairman likes local embedding backends because privacy matters."

    def _band_corpus(self, d):
        # partial word overlap -> Jaccard lands in the semantic band, so the
        # embedding pass is attempted and the skip reason is surfaced
        _helpers.write_memory(os.path.join(d, "L0-working", "a.md"),
                              id="band-a", body=self._BODY_A)
        _helpers.write_memory(os.path.join(d, "L0-working", "b.md"),
                              id="band-b", body=self._BODY_B)

    def _run(self, d, env):
        rc, out, err = _helpers.run_script(
            "entropy.py", "--memory-dir", d, "--now", "2026-06-25",
            "--config", "/nonexistent.md", env=env)
        self.assertEqual(rc, 0, err)
        import json
        return json.loads(out)

    def test_force_disabled_names_env_var(self):
        with tempfile.TemporaryDirectory() as d:
            self._band_corpus(d)
            data = self._run(d, {"SC_NO_RAG": "1"})
            reason = data["semantic_dedup"]["reason"]
            self.assertIn("SC_NO_RAG", reason)
            self.assertNotIn("absent", reason)

    def test_genuinely_absent_says_absent(self):
        with tempfile.TemporaryDirectory() as d:
            self._band_corpus(d)
            # SC_RAG_REEXEC=1 suppresses the venv re-exec, and an empty
            # PYTHONPATH-free base python has no fastembed -> import fails
            # without SC_NO_RAG being set: the genuine-absence branch.
            data = self._run(d, {"SC_RAG_REEXEC": "1", "SC_NO_RAG": ""})
            meta = data["semantic_dedup"]
            # If the base interpreter happens to carry the backend, the import
            # succeeds and the absence branch is untestable here (embedding
            # either runs or hits the distinct backend-error branch) — only the
            # true import-failure path must say "absent" and never the env var.
            if meta["pass"] == "jaccard-only" and "backend error" not in meta["reason"]:
                self.assertEqual(meta["reason"],
                                 "embedding pass skipped (RAG venv absent)")
                self.assertNotIn("SC_NO_RAG", meta["reason"])


class TestVenvReexecResolution(unittest.TestCase):
    """C2 — the RAG venv re-exec resolves against --memory-dir's project root
    (memory-dir's parent is that project's .company), never against a foreign
    .company that happens to sit under cwd."""

    SCRIPT = os.path.join(_helpers.SCRIPTS_DIR, "entropy.py")

    def setUp(self):
        # The skill-local venv (scripts/../.rag-venv) is checked first by
        # design; if it exists on this machine the project-root fallback is
        # never consulted and this scenario is untestable here.
        skill_venv = os.path.join(os.path.dirname(_helpers.SCRIPTS_DIR),
                                  ".rag-venv", "bin", "python")
        if os.path.exists(skill_venv):
            self.skipTest("skill-local .rag-venv present; fallback not reached")
        # The re-exec only fires when the base interpreter lacks the backend.
        import importlib.util
        if importlib.util.find_spec("fastembed") is not None:
            self.skipTest("base interpreter has fastembed; no re-exec occurs")

    def _fake_project(self, root, marker):
        """Create <root>/.company/{.rag-venv/bin/python, memory/} with a fake
        interpreter that prints a marker instead of running anything."""
        bindir = os.path.join(root, ".company", ".rag-venv", "bin")
        os.makedirs(bindir)
        py = os.path.join(bindir, "python")
        with open(py, "w", encoding="utf-8") as f:
            f.write(f"#!/bin/sh\necho EXEC:{marker}\nexit 0\n")
        os.chmod(py, 0o755)
        memdir = os.path.join(root, ".company", "memory")
        os.makedirs(memdir)
        return memdir

    def _run_from(self, cwd, *args):
        import subprocess
        import sys as _sys
        env = {k: v for k, v in os.environ.items()
               if k not in ("SC_NO_RAG", "SC_RAG_REEXEC")}
        proc = subprocess.run([_sys.executable, self.SCRIPT, *args],
                              capture_output=True, text=True, cwd=cwd, env=env)
        return proc.returncode, proc.stdout, proc.stderr

    def test_memory_dir_project_beats_foreign_cwd(self):
        # cwd holds a DECOY .company/.rag-venv; --memory-dir points at REAL.
        # The re-exec must pick REAL's interpreter, not the decoy's.
        with tempfile.TemporaryDirectory() as decoy, \
                tempfile.TemporaryDirectory() as real:
            self._fake_project(decoy, "DECOY")
            real_mem = self._fake_project(real, "REAL")
            rc, out, err = self._run_from(decoy, "--memory-dir", real_mem)
            self.assertEqual(rc, 0, err)
            self.assertIn("EXEC:REAL", out)
            self.assertNotIn("DECOY", out)

    def test_memory_dir_equals_form(self):
        # --memory-dir=PATH must resolve identically to the two-token form.
        with tempfile.TemporaryDirectory() as decoy, \
                tempfile.TemporaryDirectory() as real:
            self._fake_project(decoy, "DECOY")
            real_mem = self._fake_project(real, "REAL")
            rc, out, err = self._run_from(decoy, f"--memory-dir={real_mem}")
            self.assertEqual(rc, 0, err)
            self.assertIn("EXEC:REAL", out)
            self.assertNotIn("DECOY", out)

    def test_default_still_resolves_cwd(self):
        # No --memory-dir: the default (.company/memory) resolves against cwd,
        # preserving the pre-C2 fallback for in-project invocations.
        with tempfile.TemporaryDirectory() as proj:
            self._fake_project(proj, "CWDPROJ")
            rc, out, err = self._run_from(proj)
            self.assertEqual(rc, 0, err)
            self.assertIn("EXEC:CWDPROJ", out)

    def test_sc_no_rag_never_reexecs(self):
        # SC_NO_RAG=1 must run the real entropy (jaccard-only), not the fake
        # interpreter — clean degradation is untouched by C2.
        import json
        import subprocess
        import sys as _sys
        with tempfile.TemporaryDirectory() as proj:
            memdir = self._fake_project(proj, "MUST-NOT-RUN")
            _helpers.write_memory(os.path.join(memdir, "L0-working", "m.md"),
                                  id="pref-x-1", body="Chairman prefers pytest.")
            env = {**os.environ, "SC_NO_RAG": "1"}
            env.pop("SC_RAG_REEXEC", None)
            proc = subprocess.run(
                [_sys.executable, self.SCRIPT, "--memory-dir", memdir,
                 "--now", "2026-06-25", "--config", "/nonexistent.md"],
                capture_output=True, text=True, cwd=proj, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("MUST-NOT-RUN", proc.stdout)
            data = json.loads(proc.stdout)
            self.assertEqual(data["total_memories"], 1)


class TestFrontmatterDelimiterFix(unittest.TestCase):
    """Phase 11 Item 2: entropy migrated to the shared frontmatter parser, which
    FIXES the old `startswith('---')` delimiter bug. A `----` line no longer
    accepts a malformed opener or truncates the block — entropy now agrees with
    decay/verify/reinforce (all use the correct `.strip()=='---'` delimiter)."""

    def test_internal_rule_now_parses_full_frontmatter(self):
        # A `----` line INSIDE the frontmatter (before the real closing ---) used
        # to TRUNCATE parsing there; the shared parser reads the whole block.
        import entropy
        text = ("---\n"
                "id: pref-band-001\n"
                "tier: L1\n"
                "----\n"
                "sources: [\"[sessJ#5]\"]\n"
                "reinforce_count: 4\n"
                "status: active\n"
                "---\n"
                "body\n")
        fm = entropy.parse_frontmatter(text)
        # fields AFTER the `----` are now parsed (were lost/defaulted before)
        self.assertEqual(fm["sources"], ["[sessJ#5]"])
        self.assertEqual(fm["reinforce_count"], "4")

    def test_quadruple_dash_opener_rejected(self):
        # `----` is NOT a valid opening fence; the malformed memory is rejected
        # (empty dict) — matching decay/verify/reinforce which all skip it.
        import entropy
        text = "----\nid: pref-opener-001\ntier: L0\n---\nbody\n"
        self.assertEqual(entropy.parse_frontmatter(text), {})

    def test_wellformed_body_rule_unaffected(self):
        # A well-formed memory whose BODY contains a `----` rule parses normally
        # (the closing fence is found first) — no behavior change here.
        import entropy
        text = ("---\nid: proj-notes-001\ntier: L0\nstatus: active\n---\n"
                "Section one.\n\n----\n\nSection two.\n")
        fm = entropy.parse_frontmatter(text)
        self.assertEqual((fm["id"], fm["tier"]), ("proj-notes-001", "L0"))

    def test_scanners_agree_on_malformed_opener(self):
        # The point of the fix: entropy's active-set membership now matches
        # decay/verify/reinforce on a malformed-opener memory (all reject it).
        import entropy, decay, verify_memory, reinforce_memory
        text = "----\nid: pref-opener-001\ntier: L0\n---\nbody\n"
        self.assertEqual(entropy.parse_frontmatter(text), {})           # rejected
        self.assertIsNone(decay.parse_frontmatter(text)["id"])          # no id
        self.assertIsNone(verify_memory.parse_frontmatter(text)[0])     # None sentinel
        self.assertEqual(reinforce_memory.parse_frontmatter(text), (None, -1))


if __name__ == "__main__":
    unittest.main()
