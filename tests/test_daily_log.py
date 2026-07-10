"""
Tests for daily_log.py — Phase 27 Item 1: the ONE shared reader.

Covers: atomic single-write append, JSONL start/end pairing into a Run,
in-flight/crashed classification purely from timestamps (no mtime probing),
the legacy prose fallback (field-for-field parity with the old RUN_RE
walkers it replaces), window filtering, and concurrent-append interleave
safety (Gibby's crash/interleave harness, Item 1 acceptance (c)/(f)).
"""

import concurrent.futures
import importlib.util
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta

import _helpers

_spec = importlib.util.spec_from_file_location(
    "daily_log", os.path.join(_helpers.SCRIPTS_DIR, "daily_log.py"))
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)


def _logs(d):
    p = os.path.join(d, ".company", "ops", "logs")
    os.makedirs(p, exist_ok=True)
    return p


def _write_jsonl(logdir, date, events):
    path = os.path.join(logdir, f"daily-{date}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return path


class TestAppendEvent(unittest.TestCase):
    def test_append_creates_file_and_appends(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "daily-2026-07-10.jsonl")
            self.assertTrue(dl.append_event(path, {"event": "start", "ts": "2026-07-10T06:00:00"}))
            self.assertTrue(dl.append_event(path, {"event": "end", "ts": "2026-07-10T06:00:05",
                                                    "start_ts": "2026-07-10T06:00:00"}))
            with open(path) as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["event"], "start")
            self.assertEqual(json.loads(lines[1])["event"], "end")

    def test_cli_append_reads_stdin(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily-2026-07-10.jsonl")
            event = json.dumps({"event": "start", "ts": "2026-07-10T06:00:00", "schema": 1})
            rc, out, err = _helpers.run_script("daily_log.py", "append", "--path", path)
            # no stdin provided by run_script -> empty stdin -> rc 1 (invalid JSON)
            self.assertNotEqual(rc, 0)
            import subprocess
            proc = subprocess.run(
                ["python3", os.path.join(_helpers.SCRIPTS_DIR, "daily_log.py"), "append", "--path", path],
                input=event, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(path) as f:
                self.assertEqual(json.loads(f.read().strip())["event"], "start")

    def test_concurrent_appends_never_interleave_bytes(self):
        # Item 1 acceptance (f): concurrent appends from many processes/threads
        # never interleave bytes — each line parses as valid, complete JSON.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily-2026-07-10.jsonl")

            def _one(i):
                # A big padding string maximizes the odds of catching torn writes
                # if the implementation weren't atomic.
                payload = {"event": "end", "ts": "2026-07-10T06:00:00", "i": i,
                           "pad": "x" * 500}
                return dl.append_event(path, payload)

            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
                results = list(ex.map(_one, range(200)))
            self.assertTrue(all(results))
            with open(path) as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 200)
            seen = set()
            for ln in lines:
                obj = json.loads(ln)          # must NEVER raise on a torn line
                seen.add(obj["i"])
            self.assertEqual(seen, set(range(200)))


class TestSameSecondRuns(unittest.TestCase):
    """MUST-FIX 1: runs that start in the SAME wall-clock second must NOT
    collapse — the reader pairs on the unique run_id, not the ts string."""

    def test_four_same_second_lock_skips_render_as_four_rows(self):
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            open(os.path.join(logdir, "daily-2026-07-10.md"), "w").close()
            events = []
            for streak in range(1, 5):
                rid = f"999{streak}-1783-abcd{streak}"
                events.append({"event": "start", "ts": "2026-07-10T06:00:00",
                               "mode": "cron", "dry_run": False, "pid": 9990 + streak,
                               "run_id": rid, "schema": 1})
                events.append({"event": "end", "ts": "2026-07-10T06:00:00",
                               "start_ts": "2026-07-10T06:00:00", "run_id": rid,
                               "schema": 1, "lock": "skipped", "lock_skip_streak": streak,
                               "core_aborted": False, "abort_reason": None, "steps": {},
                               "agent": None, "dry_run": False})
            _write_jsonl(logdir, "2026-07-10", events)
            runs = dl.read_runs(os.path.join(d, ".company"), window_days=None,
                                 now=datetime(2026, 7, 10, 7))
            self.assertEqual(len(runs), 4)                       # never collapsed
            self.assertEqual([r["lock"] for r in runs], ["skipped"] * 4)
            self.assertEqual(sorted(r["lock_skip_streak"] for r in runs), [1, 2, 3, 4])

    def test_jsonl_only_day_visible_without_a_sibling_md(self):
        # MUST-FIX 1: a day where the .md write failed but the JSONL append
        # succeeded must still surface — read_runs enumerates .jsonl directly.
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            _write_jsonl(logdir, "2026-07-10", [
                {"event": "start", "ts": "2026-07-10T06:00:00", "mode": "cron",
                 "dry_run": False, "pid": 1, "run_id": "r1", "schema": 1},
                {"event": "end", "ts": "2026-07-10T06:00:01", "start_ts": "2026-07-10T06:00:00",
                 "run_id": "r1", "schema": 1, "lock": "acquired", "lock_skip_streak": 0,
                 "core_aborted": False, "abort_reason": None, "steps": {}, "agent": None,
                 "dry_run": False},
            ])
            # NO daily-2026-07-10.md exists at all.
            self.assertFalse(os.path.exists(os.path.join(logdir, "daily-2026-07-10.md")))
            runs = dl.read_runs(os.path.join(d, ".company"), window_days=None,
                                 now=datetime(2026, 7, 10, 7))
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["lock"], "acquired")
            self.assertEqual(runs[0]["md_block"], "")           # no md, but still a row

    def test_legacy_events_without_run_id_still_pair_by_ts(self):
        # Backward-compat: a distinct-second start/end WITHOUT run_id (old data
        # / hand-written fixtures) still pairs via the ts fallback.
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            open(os.path.join(logdir, "daily-2026-07-10.md"), "w").close()
            _write_jsonl(logdir, "2026-07-10", [
                {"event": "start", "ts": "2026-07-10T06:00:00", "mode": "cron",
                 "dry_run": False, "pid": 1, "schema": 1},
                {"event": "end", "ts": "2026-07-10T06:00:02", "start_ts": "2026-07-10T06:00:00",
                 "schema": 1, "lock": "acquired", "lock_skip_streak": 0, "core_aborted": False,
                 "abort_reason": None, "steps": {}, "agent": None, "dry_run": False},
            ])
            runs = dl.read_runs(os.path.join(d, ".company"), window_days=None,
                                 now=datetime(2026, 7, 10, 7))
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["run_state"], "complete")   # end paired, not crashed


class TestJsonlRuns(unittest.TestCase):
    def test_complete_run_pairs_start_and_end(self):
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            with open(os.path.join(logdir, "daily-2026-07-10.md"), "w") as f:
                f.write("\n## Daily run 2026-07-10T06:00:00\n- decay --apply: scanned 5\n")
            _write_jsonl(logdir, "2026-07-10", [
                {"event": "start", "ts": "2026-07-10T06:00:00", "mode": "cron",
                 "dry_run": False, "pid": 123, "schema": 1},
                {"event": "end", "ts": "2026-07-10T06:00:05", "start_ts": "2026-07-10T06:00:00",
                 "schema": 1, "lock": "acquired", "lock_skip_streak": 0,
                 "core_aborted": False, "abort_reason": None,
                 "steps": {"decay": {"outcome": "ran", "warnings": 0, "drop": 2, "demote": 1,
                                     "archive": 0, "upgrade": 1},
                           "entropy": {"outcome": "ran", "warnings": 0, "value": 0.05,
                                       "dims": {"dup_rate": 0.1}, "memories": 12}},
                 "agent": {"outcome": "ok", "rc": 0, "runs_today": 1, "cap": 4, "fail_streak": 0},
                 "dry_run": False},
            ])
            runs = dl.read_runs(os.path.join(d, ".company"), window_days=None)
            self.assertEqual(len(runs), 1)
            r = runs[0]
            self.assertEqual(r["source"], "jsonl")
            self.assertEqual(r["run_state"], "complete")
            self.assertEqual(r["lock"], "acquired")
            self.assertEqual(r["drop"], 2)
            self.assertEqual(r["demote"], 1)
            self.assertEqual(r["upgrade"], 1)
            self.assertEqual(r["entropy"], 0.05)
            self.assertEqual(r["memories"], 12)
            self.assertEqual(r["agent"], "ok")
            self.assertEqual(r["agent_rc"], 0)
            self.assertIn("## Daily run", r["md_block"])

    def test_dry_run_start_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            open(os.path.join(logdir, "daily-2026-07-10.md"), "w").close()
            _write_jsonl(logdir, "2026-07-10", [
                {"event": "start", "ts": "2026-07-10T06:00:00", "mode": "manual",
                 "dry_run": True, "pid": 1, "schema": 1},
            ])
            runs = dl.read_runs(os.path.join(d, ".company"), window_days=None)
            self.assertEqual(runs, [])

    def test_lock_skipped_is_a_recorded_run(self):
        # Item 1 acceptance (e): a locked-out cron tick appears as a run with
        # lock:"skipped" — a skipped tick is a RECORDED tick.
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            open(os.path.join(logdir, "daily-2026-07-10.md"), "w").close()
            _write_jsonl(logdir, "2026-07-10", [
                {"event": "start", "ts": "2026-07-10T06:00:00", "mode": "cron",
                 "dry_run": False, "pid": 1, "schema": 1},
                {"event": "end", "ts": "2026-07-10T06:00:01", "start_ts": "2026-07-10T06:00:00",
                 "schema": 1, "lock": "skipped", "lock_skip_streak": 1,
                 "core_aborted": False, "abort_reason": None, "steps": {}, "agent": None,
                 "dry_run": False},
            ])
            runs = dl.read_runs(os.path.join(d, ".company"), window_days=None)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["lock"], "skipped")
            self.assertEqual(runs[0]["lock_skip_streak"], 1)

    def test_inflight_classified_from_ts_age_no_mtime_probe(self):
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            open(os.path.join(logdir, "daily-2026-07-10.md"), "w").close()
            now = datetime(2026, 7, 10, 6, 2, 0)
            _write_jsonl(logdir, "2026-07-10", [
                {"event": "start", "ts": "2026-07-10T06:00:00", "mode": "manual",
                 "dry_run": False, "pid": 1, "schema": 1},
            ])
            runs = dl.read_runs(os.path.join(d, ".company"), window_days=None, now=now)
            self.assertEqual(runs[0]["run_state"], "in-flight")
            self.assertEqual(runs[0]["agent"], "running")

    def test_crashed_classified_from_ts_age_no_mtime_probe(self):
        # Item 1 acceptance (c): kill -9 mid-core -> crashed, not flat, not
        # in-flight after the window — and NO agent-log mtime is ever probed
        # for a jsonl-sourced run (no agent-*.log is even created here).
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            open(os.path.join(logdir, "daily-2026-07-10.md"), "w").close()
            now = datetime(2026, 7, 10, 8, 0, 0)   # 2h after start, no end line
            _write_jsonl(logdir, "2026-07-10", [
                {"event": "start", "ts": "2026-07-10T06:00:00", "mode": "cron",
                 "dry_run": False, "pid": 1, "schema": 1},
            ])
            runs = dl.read_runs(os.path.join(d, ".company"), window_days=None, now=now)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["run_state"], "crashed")
            self.assertNotEqual(runs[0]["run_state"], "in-flight")

    def test_window_days_filters_old_runs(self):
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            open(os.path.join(logdir, "daily-2026-06-01.md"), "w").close()
            _write_jsonl(logdir, "2026-06-01", [
                {"event": "start", "ts": "2026-06-01T06:00:00", "mode": "cron",
                 "dry_run": False, "pid": 1, "schema": 1},
                {"event": "end", "ts": "2026-06-01T06:00:01", "start_ts": "2026-06-01T06:00:00",
                 "schema": 1, "lock": "acquired", "lock_skip_streak": 0, "core_aborted": False,
                 "abort_reason": None, "steps": {}, "agent": None, "dry_run": False},
            ])
            now = datetime(2026, 7, 10, 0, 0, 0)
            self.assertEqual(dl.read_runs(os.path.join(d, ".company"), window_days=30, now=now), [])
            self.assertEqual(len(dl.read_runs(os.path.join(d, ".company"), window_days=None, now=now)), 1)


LEGACY_LOG = """\
## Daily run 2026-06-26T00:07:01 (dry-run)
- decay: scanned 5 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0
- entropy 0.0 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.0) over 5 memories

## Daily run 2026-06-26T06:07:01
- decay --apply: scanned 10 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0
- verify --apply: newly-verified 0 | already 0 | unverifiable 0
- entropy 0.0667 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.0667) over 10 memories
- agent (consolidate/verify): ok

## Daily run 2026-06-26T12:07:01
- decay --apply: scanned 12 | drop 0 | demote 0 | archive 0 | upgrade-candidates 1
- verify --apply: newly-verified 14 | already 0 | unverifiable 8
- entropy 0.0356 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.0356) over 45 memories
- agent (consolidate/verify): ok

## Daily run 2026-06-26T18:07:01
- decay --apply: scanned 12 | drop 0 | demote 0 | archive 0 | upgrade-candidates 0
- entropy 0.0356 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.0356) over 45 memories
- agent: skipped — daily agent-run cap reached (4/4, token breaker)
"""


class TestLegacyFallback(unittest.TestCase):
    def test_legacy_md_only_parses_field_for_field(self):
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            with open(os.path.join(logdir, "daily-2026-06-26.md"), "w") as f:
                f.write(LEGACY_LOG)
            runs = dl.read_runs(os.path.join(d, ".company"), window_days=None,
                                 now=datetime(2026, 7, 10))
            self.assertEqual(len(runs), 3)   # dry-run excluded
            self.assertEqual(runs[0]["agent"], "ok")
            self.assertEqual(runs[1]["upgrade"], 1)
            self.assertEqual(runs[1]["verified"], 14)
            self.assertEqual(runs[1]["unverifiable"], 8)
            self.assertEqual(runs[2]["agent"], "skipped")
            self.assertTrue(all(r["source"] == "legacy-md" for r in runs))

    def test_no_jsonl_sibling_uses_legacy_even_if_other_days_have_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            with open(os.path.join(logdir, "daily-2026-06-26.md"), "w") as f:
                f.write(LEGACY_LOG)
            with open(os.path.join(logdir, "daily-2026-07-10.md"), "w") as f:
                f.write("\n## Daily run 2026-07-10T06:00:00\n")
            _write_jsonl(logdir, "2026-07-10", [
                {"event": "start", "ts": "2026-07-10T06:00:00", "mode": "cron",
                 "dry_run": False, "pid": 1, "schema": 1},
                {"event": "end", "ts": "2026-07-10T06:00:01", "start_ts": "2026-07-10T06:00:00",
                 "schema": 1, "lock": "acquired", "lock_skip_streak": 0, "core_aborted": False,
                 "abort_reason": None, "steps": {}, "agent": None, "dry_run": False},
            ])
            runs = dl.read_runs(os.path.join(d, ".company"), window_days=None,
                                 now=datetime(2026, 7, 10))
            sources = {r["ts"].isoformat(): r["source"] for r in runs}
            self.assertEqual(sources["2026-06-26T06:07:01"], "legacy-md")
            self.assertEqual(sources["2026-07-10T06:00:00"], "jsonl")

    def test_legacy_inflight_reclassifies_only_global_latest(self):
        body = (
            "## Daily run 2026-06-29T06:07:01\n"
            "- decay --apply: scanned 10 | drop 3 | demote 0 | archive 0 | upgrade-candidates 0\n"
            "- entropy 0.02 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.02) over 10 memories\n"
            "- agent prompt: measured backlog injected (scored pairs + review candidates from this run)\n"
        )
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            with open(os.path.join(logdir, "daily-2026-06-29.md"), "w") as f:
                f.write(body)
            with open(os.path.join(logdir, "agent-2026-06-29.log"), "w") as f:
                f.write("stream\n")   # fresh mtime
            runs = dl.read_runs(os.path.join(d, ".company"), window_days=None,
                                 now=datetime(2026, 7, 10))
            self.assertEqual(runs[-1]["agent"], "running")


class TestPrune(unittest.TestCase):
    """Phase 27 Item 5: age-prune ops/logs by FILENAME date, never mtime;
    NEVER touch today's .agent_runs_ counter (the daily CAP ledger)."""

    def _seed_200_days(self, logdir, today):
        for i in range(200):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            open(os.path.join(logdir, f"daily-{d}.md"), "w").close()
            open(os.path.join(logdir, f"daily-{d}.jsonl"), "w").close()
            open(os.path.join(logdir, f"agent-{d}.log"), "w").close()
            open(os.path.join(logdir, f".agent_runs_{d}"), "w").close()

    def test_a_200_day_fixture_leaves_at_most_90_and_todays_counter_intact(self):
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            today = datetime(2026, 7, 10).date()
            self._seed_200_days(logdir, today)
            removed, warning = dl.prune(os.path.join(d, ".company"), retain_days=90, today=today)
            self.assertIsNone(warning)
            self.assertGreater(removed, 0)
            remaining_daily = sorted(
                f for f in os.listdir(logdir) if f.startswith("daily-") and f.endswith(".md"))
            self.assertLessEqual(len(remaining_daily), 91)   # today + 90 back
            # today's counter is intact; every OTHER counter is gone
            counters = [f for f in os.listdir(logdir) if f.startswith(".agent_runs_")]
            self.assertEqual(counters, [f".agent_runs_{today.isoformat()}"])

    def test_b_refuses_retain_days_below_window_plus_one(self):
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            today = datetime(2026, 7, 10).date()
            self._seed_200_days(logdir, today)
            before = len(os.listdir(logdir))
            removed, warning = dl.prune(os.path.join(d, ".company"), retain_days=10,
                                         today=today, window_days=30)
            self.assertEqual(removed, 0)
            self.assertIsNotNone(warning)
            self.assertIn("refused", warning)
            self.assertEqual(len(os.listdir(logdir)), before)   # nothing touched

    def test_c_malformed_filenames_never_crash(self):
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            open(os.path.join(logdir, "daily-not-a-date.md"), "w").close()
            open(os.path.join(logdir, "agent-2026-13-99.log"), "w").close()   # invalid calendar date
            open(os.path.join(logdir, "random-file.txt"), "w").close()
            removed, warning = dl.prune(os.path.join(d, ".company"), retain_days=90,
                                         today=datetime(2026, 7, 10).date())
            self.assertIsNone(warning)   # never crashes
            # malformed/unrelated files are left alone (skipped, not guessed at)
            self.assertEqual(len(os.listdir(logdir)), 3)

    def test_d_backup_restored_old_logs_pruned_by_filename_not_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            old_date = "2025-01-01"
            p = os.path.join(logdir, f"daily-{old_date}.md")
            open(p, "w").close()
            fresh = time.time()
            os.utime(p, (fresh, fresh))    # mtime says "just restored", filename says ancient
            removed, warning = dl.prune(os.path.join(d, ".company"), retain_days=90,
                                         today=datetime(2026, 7, 10).date())
            self.assertIsNone(warning)
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(p))

    def test_prune_failure_on_missing_dir_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            # No ops/logs dir at all.
            removed, warning = dl.prune(os.path.join(d, ".company"), retain_days=90,
                                         today=datetime(2026, 7, 10).date())
            self.assertEqual(removed, 0)
            self.assertIsNone(warning)   # missing dir is not an error worth surfacing

    def test_prune_failure_on_unreadable_dir_warns_never_raises(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permission bits")
        with tempfile.TemporaryDirectory() as d:
            logdir = _logs(d)
            open(os.path.join(logdir, "daily-2020-01-01.md"), "w").close()
            os.chmod(logdir, 0o000)
            try:
                removed, warning = dl.prune(os.path.join(d, ".company"), retain_days=90,
                                             today=datetime(2026, 7, 10).date())
            finally:
                os.chmod(logdir, 0o755)   # restore so tempdir cleanup can proceed
            self.assertEqual(removed, 0)
            self.assertIsNotNone(warning)   # surfaced, but never raised


if __name__ == "__main__":
    unittest.main()
