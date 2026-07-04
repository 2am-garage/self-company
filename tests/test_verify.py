"""
Tests for verify_memory.py — the deterministic VERIFY provenance gate.

Builds a temp memory dir + a fake transcripts dir and checks: a source that
traces to a real transcript line verifies; missing session / out-of-range line /
empty sources do not; --apply stamps verified_date; already-verified is skipped.
"""

import importlib.util
import os
import tempfile
import unittest

import _helpers

_spec = importlib.util.spec_from_file_location(
    "verify_memory", os.path.join(_helpers.SCRIPTS_DIR, "verify_memory.py"))
vm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vm)


def _mem(path, *, id, sources, verified=False, provenance=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    extra = "verified_date: 2026-06-01\nverified_by: Gibby\n" if verified else ""
    prov = f"provenance: {provenance}\n" if provenance else ""
    with open(path, "w") as f:
        f.write(
            f"---\nid: {id}\ntier: L0\nowner: Tony\nsources: {sources}\n"
            f"created: 2026-06-01\nlast_reinforced: 2026-06-01\nreinforce_count: 1\n"
            f"decay_score: 1.0\nstatus: active\n{prov}{extra}---\nbody\n")


def _transcripts(d, session, nlines):
    base = os.path.join(d, "transcripts", "proj")
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, f"{session}.jsonl"), "w") as f:
        for i in range(nlines):
            f.write('{"type":"user","message":{"content":"x"}}\n')
    return os.path.join(d, "transcripts")


class TestVerify(unittest.TestCase):
    def test_traces_and_stamps_with_apply(self):
        with tempfile.TemporaryDirectory() as d:
            mem = os.path.join(d, "memory")
            _mem(os.path.join(mem, "L0-working", "a.md"), id="ok", sources='["[sessA#2]"]')
            _mem(os.path.join(mem, "L0-working", "b.md"), id="badline", sources='["[sessA#99]"]')
            _mem(os.path.join(mem, "L0-working", "c.md"), id="nosession", sources='["[ghost#1]"]')
            _mem(os.path.join(mem, "L0-working", "e.md"), id="empty", sources='[]')
            tdir = _transcripts(d, "sessA", 5)

            rep = vm.verify_dir(mem, tdir, "2026-06-30", apply=True)
            self.assertIn("ok", rep["verified"])
            for bad in ("badline", "nosession", "empty"):
                self.assertIn(bad, rep["unverifiable"])
            # the traced one got stamped on disk
            with open(os.path.join(mem, "L0-working", "a.md")) as f:
                self.assertIn("verified_date: 2026-06-30", f.read())
            # an untraceable one did NOT
            with open(os.path.join(mem, "L0-working", "b.md")) as f:
                self.assertNotIn("verified_date", f.read())

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            mem = os.path.join(d, "memory")
            _mem(os.path.join(mem, "L0-working", "a.md"), id="ok", sources='["[sessA#0]"]')
            tdir = _transcripts(d, "sessA", 3)
            rep = vm.verify_dir(mem, tdir, "2026-06-30", apply=False)
            self.assertEqual(rep["verified"], ["ok"])
            with open(os.path.join(mem, "L0-working", "a.md")) as f:
                self.assertNotIn("verified_date", f.read())

    def test_already_verified_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            mem = os.path.join(d, "memory")
            _mem(os.path.join(mem, "L0-working", "a.md"), id="done",
                 sources='["[sessA#0]"]', verified=True)
            tdir = _transcripts(d, "sessA", 3)
            rep = vm.verify_dir(mem, tdir, "2026-06-30", apply=True)
            self.assertEqual(rep["already_verified"], 1)
            self.assertEqual(rep["verified"], [])

    def test_tombstones_skipped(self):
        # Phase 6 Item 1: VERIFY skips ALL tombstones (archived / defunct /
        # absorbed) via the shared vocabulary — a merged-away dup must not be
        # scanned or stamped.
        with tempfile.TemporaryDirectory() as d:
            mem = os.path.join(d, "memory")
            for i, st in enumerate(("archived", "defunct", "absorbed")):
                p = os.path.join(mem, "L0-working", f"t{i}.md")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(f"---\nid: tomb{i}\ntier: L0\nowner: Tony\n"
                            f'sources: ["[sessA#0]"]\ncreated: 2026-06-01\n'
                            f"last_reinforced: 2026-06-01\nreinforce_count: 1\n"
                            f"decay_score: 1.0\nstatus: {st}\n---\nbody\n")
            _mem(os.path.join(mem, "L0-working", "live.md"), id="live",
                 sources='["[sessA#0]"]')
            tdir = _transcripts(d, "sessA", 3)
            rep = vm.verify_dir(mem, tdir, "2026-06-30", apply=True)
            self.assertEqual(rep["scanned"], 1)  # only the live one
            self.assertEqual(rep["verified"], ["live"])


class TestCharterClass(unittest.TestCase):
    """Item 6 — charter/axiom provenance class + anti-abuse."""

    def _emptytx(self, d):
        tdir = os.path.join(d, "transcripts")
        os.makedirs(tdir, exist_ok=True)
        return tdir

    def test_blessed_charter_stamped_by_charter(self):
        # A blessed seed with charter provenance and NO traceable source is
        # honoured — stamped verified_by: charter, not listed unverifiable.
        with tempfile.TemporaryDirectory() as d:
            mem = os.path.join(d, "memory")
            _mem(os.path.join(mem, "L0-working", "a.md"),
                 id="org-hierarchy", sources='["charter:org-hierarchy"]',
                 provenance="charter")
            rep = vm.verify_dir(mem, self._emptytx(d), "2026-07-03", apply=True)
            self.assertIn("org-hierarchy", rep["charter_verified"])
            self.assertNotIn("org-hierarchy", rep["unverifiable"])
            with open(os.path.join(mem, "L0-working", "a.md")) as f:
                txt = f.read()
            self.assertIn("verified_by: charter", txt)
            self.assertIn("verified_date: 2026-07-03", txt)

    def test_charter_via_source_tag_only(self):
        # Provenance declared purely via a charter:<slug> source (no frontmatter
        # key) is still recognised for a blessed id.
        with tempfile.TemporaryDirectory() as d:
            mem = os.path.join(d, "memory")
            _mem(os.path.join(mem, "L0-working", "c.md"),
                 id="merge-gate", sources='["charter:merge-gate"]')
            rep = vm.verify_dir(mem, self._emptytx(d), "2026-07-03", apply=True)
            self.assertIn("merge-gate", rep["charter_verified"])

    def test_nonblessed_charter_flagged_not_trusted(self):
        # A NON-blessed memory self-declaring charter is flagged, never stamped.
        with tempfile.TemporaryDirectory() as d:
            mem = os.path.join(d, "memory")
            _mem(os.path.join(mem, "L0-working", "b.md"),
                 id="fake-axiom", sources='["charter:fake-axiom"]',
                 provenance="charter")
            rep = vm.verify_dir(mem, self._emptytx(d), "2026-07-03", apply=True)
            self.assertIn("fake-axiom", rep["flagged_charter"])
            self.assertNotIn("fake-axiom", rep["charter_verified"])
            self.assertNotIn("fake-axiom", rep["verified"])
            with open(os.path.join(mem, "L0-working", "b.md")) as f:
                self.assertNotIn("verified_date", f.read())


if __name__ == "__main__":
    unittest.main()
