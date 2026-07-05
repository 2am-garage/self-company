"""
Tests for fleet.py — the Phase 8 holding-company registry + fleet-state + budget
logic. Deterministic, stdlib only. No sub memory is ever opened here: entropy is
read from a fabricated sub daily-log (the sub's own OUTPUT artifact).
"""

import importlib.util
import os
import tempfile
import unittest

import _helpers

_spec = importlib.util.spec_from_file_location(
    "fleet", os.path.join(_helpers.SCRIPTS_DIR, "fleet.py"))
fl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fl)


def _write_registry(parent, text):
    d = os.path.join(parent, ".company", "org")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "subsidiaries.md"), "w", encoding="utf-8") as f:
        f.write(text)


def _make_company(base, name):
    """A minimal live sub: a dir with a `.company/` inside."""
    d = os.path.join(base, name)
    os.makedirs(os.path.join(d, ".company", "memory"), exist_ok=True)
    return d


def _write_sub_daily_log(sub, entropy=0.25, dup_pairs=None, memories=4, dry=False):
    """Fabricate a sub's ops/logs/daily-*.md with a run block daily-run.sh shape."""
    logdir = os.path.join(sub, ".company", "ops", "logs")
    os.makedirs(logdir, exist_ok=True)
    dry_tag = " (dry-run)" if dry else ""
    lines = [f"## Daily run 2026-07-05T12:00:00{dry_tag}",
             f"- entropy {entropy} (dup 1.0 | contra 0.0 | stale 0.0 | unverified 0.0) over {memories} memories"]
    if dup_pairs:
        lines.append(f"  - duplicate candidates: {dup_pairs!r}")
    with open(os.path.join(logdir, "daily-2026-07-05.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class TestRegistry(unittest.TestCase):
    def test_live_dead_disabled_dup(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = _make_company(tmp, "parent")
            a = _make_company(tmp, "subA")
            b = _make_company(tmp, "subB")
            dead = os.path.join(tmp, "subDEAD")
            os.makedirs(dead, exist_ok=True)  # dir exists but NO .company/
            _write_registry(parent, f"""\
# scratch registry
| path | weight | enabled |
|------|--------|---------|
| {a} | 2 | true |
| {b} | 1 | true |
| {dead} | 1 | true |
| {b} | 9 | true |
| {os.path.join(tmp, 'subOFF')} | 1 | false |
""")
            scan = fl.scan_registry(parent)
            self.assertEqual([s.path for s in scan.live], [a, b])
            self.assertEqual(scan.live[0].weight, 2)
            # dup keeps the FIRST occurrence's weight (1), not 9
            self.assertEqual(scan.live[1].weight, 1)
            self.assertEqual(scan.dead, [dead])
            self.assertIn(b, scan.duplicates)
            self.assertEqual(len(scan.disabled), 1)
            self.assertEqual(fl.read_registry(parent), scan.live)

    def test_defaults_weight_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = _make_company(tmp, "parent")
            a = _make_company(tmp, "subA")
            _write_registry(parent, f"| {a} |\n")  # path only -> weight 1, enabled true
            live = fl.read_registry(parent)
            self.assertEqual(len(live), 1)
            self.assertEqual(live[0].weight, 1)
            self.assertTrue(live[0].enabled)

    def test_missing_registry_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = _make_company(tmp, "parent")
            self.assertEqual(fl.read_registry(parent), [])
            self.assertEqual(fl.scan_registry(parent).warnings, [])

    def test_malformed_registry_no_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = _make_company(tmp, "parent")
            _write_registry(parent, "not a table\n|||\n| \n\n garbage |\n")
            self.assertEqual(fl.read_registry(parent), [])

    def test_relative_path_resolves_against_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = _make_company(tmp, "parent")
            _make_company(parent, "child")  # <parent>/child/.company
            _write_registry(parent, "| child | 1 | true |\n")
            live = fl.read_registry(parent)
            self.assertEqual(len(live), 1)
            self.assertTrue(live[0].path.endswith(os.path.join("parent", "child")))


class TestState(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = _make_company(tmp, "parent")
            self.assertEqual(fl.read_state(parent), {})
            st = {"/x": {"last_entropy": 0.3, "last_tick": "2026-07-05"}}
            fl.write_state(parent, st)
            self.assertEqual(fl.read_state(parent), st)

    def test_corrupt_state_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = _make_company(tmp, "parent")
            os.makedirs(os.path.dirname(fl.state_path(parent)), exist_ok=True)
            with open(fl.state_path(parent), "w") as f:
                f.write("{not json")
            self.assertEqual(fl.read_state(parent), {})


class TestSubEntropy(unittest.TestCase):
    def test_reads_entropy_and_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = _make_company(tmp, "sub")
            _write_sub_daily_log(sub, entropy=0.25,
                                 dup_pairs=[["a", "b"], ["c", "d"], ["e", "f"]],
                                 memories=7)
            e = fl.read_sub_entropy(sub)
            self.assertEqual(e["entropy"], 0.25)
            self.assertEqual(e["dup_backlog"], 3)
            self.assertEqual(e["memories"], 7)

    def test_no_dup_line_means_zero_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = _make_company(tmp, "sub")
            _write_sub_daily_log(sub, entropy=0.0, dup_pairs=None, memories=3)
            e = fl.read_sub_entropy(sub)
            self.assertEqual(e["dup_backlog"], 0)

    def test_uses_last_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = _make_company(tmp, "sub")
            logdir = os.path.join(sub, ".company", "ops", "logs")
            os.makedirs(logdir, exist_ok=True)
            with open(os.path.join(logdir, "daily-2026-07-05.md"), "w") as f:
                f.write("## Daily run 2026-07-05T06:00:00\n"
                        "- entropy 0.10 (dup 0.0 | contra 0.0 | stale 0.0 | unverified 0.0) over 5 memories\n"
                        "## Daily run 2026-07-05T12:00:00\n"
                        "- entropy 0.40 (dup 1.0 | contra 0.0 | stale 0.0 | unverified 0.0) over 5 memories\n")
            self.assertEqual(fl.read_sub_entropy(sub)["entropy"], 0.40)

    def test_missing_log_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = _make_company(tmp, "sub")
            self.assertIsNone(fl.read_sub_entropy(sub))


class TestQualify(unittest.TestCase):
    def test_risen(self):
        q, delta, reason = fl.qualify(0.30, 0.20, 0, 5)
        self.assertTrue(q)
        self.assertAlmostEqual(delta, 0.10)
        self.assertEqual(reason, "risen")

    def test_stable(self):
        q, delta, reason = fl.qualify(0.20, 0.20, 2, 5)
        self.assertFalse(q)
        self.assertEqual(reason, "stable")

    def test_backlog(self):
        q, delta, reason = fl.qualify(0.20, 0.20, 9, 5)
        self.assertTrue(q)
        self.assertEqual(reason, "backlog")

    def test_first_tick_no_rise(self):
        # no prior tick -> delta 0, can only qualify via backlog
        q, delta, reason = fl.qualify(0.99, None, 1, 5)
        self.assertFalse(q)
        self.assertEqual(delta, 0.0)
        q2, _, r2 = fl.qualify(0.99, None, 9, 5)
        self.assertTrue(q2)
        self.assertEqual(r2, "backlog")


class TestPlan(unittest.TestCase):
    def _results(self):
        return [
            {"path": "/a", "entropy": 0.25, "dup_backlog": 0, "weight": 2},
            {"path": "/b", "entropy": 0.25, "dup_backlog": 0, "weight": 1},
            {"path": "/c", "entropy": 0.25, "dup_backlog": 0, "weight": 3},
        ]

    def test_only_risen_selected_budget1(self):
        state = {"/a": {"last_entropy": 0.25}, "/b": {"last_entropy": 0.25},
                 "/c": {"last_entropy": 0.10}}
        dec = fl.plan_fleet(self._results(), state, budget=1, dup_threshold=20)
        by = {d["path"]: d for d in dec}
        self.assertTrue(by["/c"]["selected"])
        self.assertFalse(by["/a"]["selected"])
        self.assertFalse(by["/b"]["selected"])
        self.assertEqual(sum(d["selected"] for d in dec), 1)

    def test_budget_ceiling(self):
        # all three rise equally (delta 0.15); rank by delta*weight => c(0.45),
        # a(0.30), b(0.15). budget=2 -> c,a selected; b deferred rank 3.
        state = {"/a": {"last_entropy": 0.10}, "/b": {"last_entropy": 0.10},
                 "/c": {"last_entropy": 0.10}}
        dec = fl.plan_fleet(self._results(), state, budget=2, dup_threshold=20)
        by = {d["path"]: d for d in dec}
        self.assertEqual(sum(d["selected"] for d in dec), 2)
        self.assertTrue(by["/c"]["selected"])
        self.assertTrue(by["/a"]["selected"])
        self.assertFalse(by["/b"]["selected"])
        self.assertEqual(by["/b"]["defer_rank"], 3)

    def test_budget_zero_selects_none(self):
        state = {"/a": {"last_entropy": 0.10}}
        dec = fl.plan_fleet(self._results(), state, budget=0, dup_threshold=20)
        self.assertEqual(sum(d["selected"] for d in dec), 0)

    def test_stable_never_qualifies(self):
        state = {"/a": {"last_entropy": 0.25}, "/b": {"last_entropy": 0.25},
                 "/c": {"last_entropy": 0.25}}
        dec = fl.plan_fleet(self._results(), state, budget=3, dup_threshold=20)
        self.assertEqual(sum(d["qualified"] for d in dec), 0)


class TestLedger(unittest.TestCase):
    def test_append_creates_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = _make_company(tmp, "parent")
            rows = [{"path": "/a", "entropy": 0.25, "delta": 0.15,
                     "verdict": "risen", "agent": "ran"},
                    {"path": "/b", "entropy": 0.25, "delta": 0.0,
                     "verdict": "stable", "agent": "-"}]
            fl.append_ledger(parent, "2026-07-05", rows)
            with open(fl.ledger_path(parent)) as f:
                text = f.read()
            self.assertIn("Fleet tick 2026-07-05", text)
            self.assertIn("| /a | 0.25 | +0.15 | risen | ran |", text)
            self.assertIn("| /b | 0.25 | 0.0 | stable | - |", text)
            # a second tick appends, keeping one header
            fl.append_ledger(parent, "2026-07-06", rows)
            with open(fl.ledger_path(parent)) as f:
                text2 = f.read()
            self.assertEqual(text2.count("# Fleet Ledger"), 1)
            self.assertEqual(text2.count("## Fleet tick"), 2)


class TestCLI(unittest.TestCase):
    def test_scan_json_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = _make_company(tmp, "parent")
            a = _make_company(tmp, "subA")
            _write_registry(parent, f"| {a} | 1 | true |\n")
            rc, out, err = _helpers.run_script("fleet.py", "scan",
                                               "--parent", parent, "--json")
            self.assertEqual(rc, 0, err)
            import json
            d = json.loads(out)
            self.assertEqual(len(d["live"]), 1)

    def test_plan_cli_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = _make_company(tmp, "parent")
            fl.write_state(parent, {"/c": {"last_entropy": 0.10}})
            res = os.path.join(tmp, "results.tsv")
            with open(res, "w") as f:
                f.write("/c\t0.25\t0\t1\n")
            rc, out, err = _helpers.run_script(
                "fleet.py", "plan", "--parent", parent, "--results", res,
                "--budget", "1", "--dup-threshold", "20")
            self.assertEqual(rc, 0, err)
            # decision line: path, entropy, delta, reason, selected, defer, backlog
            fields = out.strip().split("\t")
            self.assertEqual(fields[0], "/c")
            self.assertEqual(fields[3], "risen")
            self.assertEqual(fields[4], "1")  # selected


if __name__ == "__main__":
    unittest.main()
