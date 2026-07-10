"""
Tests for frontmatter._atomic_write (Phase 25 Item 2) — the ONE shared
write-temp-same-dir + os.replace helper every memory writer routes through.

Includes Gibby's two PERMANENT attack harnesses (spec requirement — they stay
in the suite, reproducible):
  * ENOSPC harness — a write that hits "No space left on device" on the temp
    file must leave the ORIGINAL completely untouched and clean up the temp.
  * kill-mid-write harness — a real SIGKILL at an arbitrary point during
    repeated atomic writes must never leave the target file truncated,
    partial, or zero-byte; a tier-promotion MOVE must never leave zero
    complete copies.
"""

import errno
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _helpers  # noqa: F401  (puts scripts/ on sys.path)
import frontmatter


class TestAtomicWriteBasics(unittest.TestCase):
    def test_writes_new_file_no_stray_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.md"
            frontmatter._atomic_write(p, "hello\n")
            self.assertEqual(p.read_text(), "hello\n")
            self.assertEqual(os.listdir(d), ["f.md"])

    def test_overwrites_existing_no_stray_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.md"
            p.write_text("old\n")
            frontmatter._atomic_write(p, "new\n")
            self.assertEqual(p.read_text(), "new\n")
            self.assertEqual(os.listdir(d), ["f.md"])

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "dir" / "f.md"
            frontmatter._atomic_write(p, "x\n")
            self.assertEqual(p.read_text(), "x\n")

    def test_accepts_str_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.md")
            frontmatter._atomic_write(p, "x\n")
            with open(p) as f:
                self.assertEqual(f.read(), "x\n")


class TestEnospcHarness(unittest.TestCase):
    """Gibby's ENOSPC harness (Phase 25 Item 1/2, kept permanently reproducible):
    a write that hits ENOSPC on the temp file must leave the ORIGINAL
    untouched — never a truncated/zero-byte target — and clean up the temp."""

    def _patched_open_raising_enospc(self):
        real_open = open

        def fake_open(path, mode="r", *a, **kw):
            if ".tmp" in str(path) and "w" in mode:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_open(path, mode, *a, **kw)

        return mock.patch("builtins.open", side_effect=fake_open)

    def test_enospc_leaves_existing_original_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.md"
            p.write_text("ORIGINAL-CONTENT\n")
            with self._patched_open_raising_enospc():
                with self.assertRaises(OSError):
                    frontmatter._atomic_write(p, "NEW-CONTENT-NEVER-LANDS\n")
            # original byte-identical — never truncated, never zero-byte
            self.assertEqual(p.read_text(), "ORIGINAL-CONTENT\n")
            # no stray temp file left behind
            self.assertEqual(os.listdir(d), ["f.md"])

    def test_enospc_on_brand_new_file_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "new.md"   # never existed
            with self._patched_open_raising_enospc():
                with self.assertRaises(OSError):
                    frontmatter._atomic_write(p, "content\n")
            self.assertFalse(p.exists())
            self.assertEqual(os.listdir(d), [])


class TestKillMidWriteHarness(unittest.TestCase):
    """Gibby's kill-mid-write harness (Phase 25 Item 2, kept permanently
    reproducible): SIGKILL a real process at an arbitrary point during
    repeated atomic writes to the SAME target. The target must ALWAYS read
    back as either the complete OLD content or one complete NEW content —
    never truncated, never partial-length, never empty."""

    def _writer_script(self, target, payload_size):
        scripts_dir = _helpers.SCRIPTS_DIR
        return (
            "import sys\n"
            f"sys.path.insert(0, {scripts_dir!r})\n"
            "import frontmatter\n"
            f"content_a = 'A' * {payload_size} + chr(10)\n"
            f"content_b = 'B' * {payload_size} + chr(10)\n"
            "i = 0\n"
            "while True:\n"
            f"    frontmatter._atomic_write({target!r}, content_a if i % 2 == 0 else content_b)\n"
            "    i += 1\n"
        )

    def test_sigkill_never_leaves_truncated_or_partial_file(self):
        payload_size = 2_000_000   # 2MB: large enough writes take measurable time
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "mem.md")
            with open(target, "w") as f:
                f.write("ORIGINAL\n")
            script = self._writer_script(target, payload_size)
            proc = subprocess.Popen([sys.executable, "-c", script])
            try:
                time.sleep(0.05)   # let it get partway into several write cycles
                proc.send_signal(signal.SIGKILL)
                proc.wait(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
            with open(target, "rb") as f:
                data = f.read()
            full_len = payload_size + 1
            self.assertIn(len(data), {len("ORIGINAL\n"), full_len},
                          f"target file has an invalid (partial) length {len(data)} "
                          "— a kill mid-write must never leave a truncated file")
            if len(data) == full_len:
                self.assertIn(data, (b"A" * payload_size + b"\n",
                                     b"B" * payload_size + b"\n"),
                              "target content is neither a complete A nor B payload")
            else:
                self.assertEqual(data, b"ORIGINAL\n")

    def test_timeout_dash_k_sigkill_never_leaves_truncated_file(self):
        # Phase 27 Item 4 acceptance (d): daily-run.sh wraps each core step in
        # `timeout -k GRACE BUDGET ...` — this rides the SAME harness above
        # (extended, not duplicated) but routes the kill through the actual
        # `timeout` coreutil daily-run.sh invokes, at both stages: a plain
        # SIGTERM-at-budget kill AND a `-k` grace SIGKILL of a TERM-ignoring
        # writer. Either way _atomic_write's tmp-then-replace means the
        # target is always either the complete OLD content or one complete
        # NEW payload — never truncated.
        payload_size = 2_000_000
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "mem.md")
            with open(target, "w") as f:
                f.write("ORIGINAL\n")
            script = (
                "import signal\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"  # forces the -k grace SIGKILL
                + self._writer_script(target, payload_size)
            )
            # budget=1s (SIGTERM fires almost immediately, ignored), grace=1s
            # (then SIGKILL) -- bounded well under the test framework's timeout.
            proc = subprocess.run(["timeout", "-k", "1", "1", sys.executable, "-c", script],
                                  timeout=10)
            # 124/137: `timeout` observed the kill and translated its own exit
            # status; -9: this environment's `timeout` propagates SIGKILL to
            # itself too (process-group signalling) — either way confirms the
            # writer was actually SIGKILLed, which is what this test attacks.
            self.assertIn(proc.returncode, (124, 137, -9))
            with open(target, "rb") as f:
                data = f.read()
            full_len = payload_size + 1
            self.assertIn(len(data), {len("ORIGINAL\n"), full_len},
                          f"target file has an invalid (partial) length {len(data)} "
                          "— a `timeout -k` SIGKILL mid-write must never leave a truncated file")
            if len(data) == full_len:
                self.assertIn(data, (b"A" * payload_size + b"\n",
                                     b"B" * payload_size + b"\n"))
            else:
                self.assertEqual(data, b"ORIGINAL\n")


if __name__ == "__main__":
    unittest.main()
