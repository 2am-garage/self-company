"""
Tests for trigger_engine.py — the event-driven Trigger #3 decision core.

Deterministic: fabricates a trigger def + payloads and checks the condition
grammar, the three guards (cooldown / dedupe / daily-cap), and record().
"""

import importlib.util
import os
import tempfile
import unittest

import _helpers

_spec = importlib.util.spec_from_file_location(
    "trigger_engine", os.path.join(_helpers.SCRIPTS_DIR, "trigger_engine.py"))
te = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(te)


def _company(d, body):
    trig = os.path.join(d, ".company", "org", "triggers")
    os.makedirs(trig)
    with open(os.path.join(trig, "t.yaml"), "w") as f:
        f.write(body)
    return os.path.join(d, ".company")


DEF = "name: t\non: push\ncondition: val_bpb < 0.99\naction: do the thing\ncooldown: 30m\n"


class TestCondition(unittest.TestCase):
    def test_basic_comparisons(self):
        self.assertTrue(te.eval_condition("val_bpb < 0.99", {"val_bpb": 0.98}))
        self.assertFalse(te.eval_condition("val_bpb < 0.99", {"val_bpb": 1.05}))
        self.assertTrue(te.eval_condition("status == \"ok\"", {"status": "ok"}))
        self.assertFalse(te.eval_condition("status != \"ok\"", {"status": "ok"}))

    def test_and_or_and_blank(self):
        self.assertTrue(te.eval_condition("", {}))                       # blank = always
        self.assertTrue(te.eval_condition("a > 1 and b < 5", {"a": 2, "b": 3}))
        self.assertFalse(te.eval_condition("a > 1 and b < 5", {"a": 2, "b": 9}))
        self.assertTrue(te.eval_condition("a > 1 or b < 5", {"a": 0, "b": 3}))

    def test_missing_field_is_false_not_crash(self):
        self.assertFalse(te.eval_condition("val_bpb < 0.99", {}))        # field absent


class TestDecide(unittest.TestCase):
    def test_condition_false_holds(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, DEF)
            r = te.decide(c, "t", {"val_bpb": 1.2})
            self.assertFalse(r["fire"])
            self.assertEqual(r["reason"], "condition false")

    def test_fire_then_cooldown(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, DEF)
            r1 = te.decide(c, "t", {"val_bpb": 0.98})
            self.assertTrue(r1["fire"])
            te.record(c, "t", {"val_bpb": 0.98})
            r2 = te.decide(c, "t", {"val_bpb": 0.95})        # different payload, but...
            self.assertFalse(r2["fire"])
            self.assertEqual(r2["reason"], "cooldown")        # ...within 30m

    def test_dedupe_isolated(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, "name: t\ncondition: val_bpb < 0.99\ncooldown: 0\naction: x\n")
            te.record(c, "t", {"val_bpb": 0.98})
            r = te.decide(c, "t", {"val_bpb": 0.98})          # identical payload
            self.assertFalse(r["fire"])
            self.assertEqual(r["reason"], "duplicate payload")
            # a DIFFERENT payload fires (no cooldown)
            self.assertTrue(te.decide(c, "t", {"val_bpb": 0.5})["fire"])

    def test_daily_cap(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, "name: t\ncondition:\ncooldown: 0\ndedupe: false\nmax_fires_per_day: 2\naction: x\n")
            te.record(c, "t", {"i": 1})
            te.record(c, "t", {"i": 2})
            r = te.decide(c, "t", {"i": 3})
            self.assertFalse(r["fire"])
            self.assertIn("daily cap", r["reason"])

    def test_unknown_trigger(self):
        with tempfile.TemporaryDirectory() as d:
            c = _company(d, DEF)
            self.assertFalse(te.decide(c, "nope", {})["fire"])


class TestDuration(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(te._parse_duration("30m"), 1800)
        self.assertEqual(te._parse_duration("1h"), 3600)
        self.assertEqual(te._parse_duration("45s"), 45)
        self.assertEqual(te._parse_duration("90"), 90)
        self.assertEqual(te._parse_duration(None), 0)


if __name__ == "__main__":
    unittest.main()
