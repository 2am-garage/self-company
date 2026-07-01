"""
Tests for trigger_eval.py — the trigger-accuracy harness.

Deterministic: exercises ONLY the pure parsing/scoring logic by feeding
fabricated stream-json objects and result lists. It never spawns real `claude`.
"""

import importlib.util
import os
import unittest

import _helpers

_spec = importlib.util.spec_from_file_location(
    "trigger_eval", os.path.join(_helpers.SCRIPTS_DIR, "trigger_eval.py"))
tv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tv)


# --- fabricated stream-json helpers ------------------------------------------

def assistant_tool_use(name, inp):
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name,
                                     "input": inp}]}}


def stream_start(index, name):
    return {"type": "stream_event",
            "event": {"type": "content_block_start", "index": index,
                      "content_block": {"type": "tool_use", "name": name,
                                        "input": {}}}}


def stream_delta(index, partial_json):
    return {"type": "stream_event",
            "event": {"type": "content_block_delta", "index": index,
                      "delta": {"type": "input_json_delta",
                                "partial_json": partial_json}}}


# --- detection: final assistant message --------------------------------------

class TestDetectAssistant(unittest.TestCase):
    def test_skill_self_company_fires(self):
        d = tv.TriggerDetector()
        self.assertTrue(d.feed(assistant_tool_use(
            "Skill", {"skill": "self-company", "args": "entropy report"})))
        self.assertTrue(d.triggered)

    def test_read_of_skill_md_fires(self):
        d = tv.TriggerDetector()
        self.assertTrue(d.feed(assistant_tool_use(
            "Read", {"file_path": "/x/.claude/skills/self-company/SKILL.md"})))

    def test_unrelated_skill_does_not_fire(self):
        d = tv.TriggerDetector()
        self.assertFalse(d.feed(assistant_tool_use(
            "Skill", {"skill": "shioaji", "args": "buy"})))
        self.assertFalse(d.triggered)

    def test_unrelated_tool_does_not_fire(self):
        d = tv.TriggerDetector()
        self.assertFalse(d.feed(assistant_tool_use(
            "Bash", {"command": "ls .company/memory"})))
        # even a Bash that mentions self-company in a path is not the skill firing
        self.assertFalse(d.feed(assistant_tool_use(
            "Bash", {"command": "cat self-company/notes"})))
        self.assertFalse(d.triggered)

    def test_read_unrelated_path_does_not_fire(self):
        d = tv.TriggerDetector()
        self.assertFalse(d.feed(assistant_tool_use(
            "Read", {"file_path": "/x/.claude/skills/shioaji/SKILL.md"})))


# --- detection: early streaming ----------------------------------------------

class TestDetectStreaming(unittest.TestCase):
    def test_early_skill_detection_across_deltas(self):
        d = tv.TriggerDetector()
        self.assertFalse(d.feed(stream_start(2, "Skill")))
        self.assertFalse(d.feed(stream_delta(2, "")))
        # trigger detected mid-stream, before the input is even complete
        self.assertTrue(d.feed(stream_delta(2, '{"skill": "self-company')))
        self.assertTrue(d.triggered)

    def test_streaming_unrelated_tool_ignored(self):
        d = tv.TriggerDetector()
        self.assertFalse(d.feed(stream_start(0, "Bash")))
        self.assertFalse(d.feed(stream_delta(0, '{"command": "ls self-company')))
        self.assertFalse(d.triggered)

    def test_streaming_read_of_skill_md(self):
        d = tv.TriggerDetector()
        self.assertFalse(d.feed(stream_start(1, "Read")))
        self.assertTrue(d.feed(stream_delta(
            1, '{"file_path": "/x/skills/self-company/SKILL.md"}')))

    def test_stickiness(self):
        d = tv.TriggerDetector()
        d.feed(assistant_tool_use("Skill", {"skill": "self-company"}))
        # once triggered, later unrelated events keep it True
        self.assertTrue(d.feed(assistant_tool_use("Bash", {"command": "ls"})))

    def test_system_hook_context_is_not_a_trigger(self):
        # the SessionStart hook injects self-company context every session;
        # that must NOT count as the skill firing.
        d = tv.TriggerDetector()
        self.assertFalse(d.feed({
            "type": "system", "subtype": "hook_response",
            "output": "self-company Scheduled-work report ... skills/self-company/SKILL.md"}))
        self.assertFalse(d.triggered)


# --- pure predicate functions ------------------------------------------------

class TestPredicates(unittest.TestCase):
    def test_tool_use_is_trigger(self):
        self.assertTrue(tv.tool_use_is_trigger("Skill", {"skill": "self-company"}))
        self.assertFalse(tv.tool_use_is_trigger("Skill", {"skill": "other"}))
        self.assertFalse(tv.tool_use_is_trigger("Skill", None))
        self.assertFalse(tv.tool_use_is_trigger(None, {"skill": "self-company"}))
        self.assertTrue(tv.tool_use_is_trigger(
            "Read", {"file_path": "a/skills/self-company/SKILL.md"}))

    def test_raw_input_is_trigger(self):
        self.assertTrue(tv.raw_input_is_trigger("Skill", '{"skill":"self-company'))
        self.assertFalse(tv.raw_input_is_trigger("Skill", '{"skill":"other'))
        self.assertTrue(tv.raw_input_is_trigger(
            "Read", '{"file_path":"x/skills/self-company/SKILL.md"'))


# --- scoring: per-query ------------------------------------------------------

class TestScoreQuery(unittest.TestCase):
    def test_should_trigger_pass(self):
        s = tv.score_query("q", True, [True, True, False], threshold=0.5)
        self.assertEqual(s["triggers"], 2)
        self.assertAlmostEqual(s["trigger_rate"], 0.6667, places=3)
        self.assertTrue(s["fired"])
        self.assertTrue(s["pass"])

    def test_should_trigger_fail_when_silent(self):
        s = tv.score_query("q", True, [False, False, False], threshold=0.5)
        self.assertFalse(s["fired"])
        self.assertFalse(s["pass"])

    def test_should_not_pass_when_silent(self):
        s = tv.score_query("q", False, [False, True, False], threshold=0.5)
        self.assertFalse(s["fired"])       # 1/3 < 0.5
        self.assertTrue(s["pass"])

    def test_should_not_fail_when_fires(self):
        s = tv.score_query("q", False, [True, True, False], threshold=0.5)
        self.assertTrue(s["fired"])        # 2/3 >= 0.5
        self.assertFalse(s["pass"])

    def test_threshold_flag_respected(self):
        # 1/3 fires (~0.333); with a lenient 0.3 threshold it counts as fired
        s = tv.score_query("q", True, [True, False, False], threshold=0.3)
        self.assertTrue(s["fired"])
        s2 = tv.score_query("q", True, [True, False, False], threshold=0.5)
        self.assertFalse(s2["fired"])


# --- scoring: recall / precision summary -------------------------------------

class TestSummarize(unittest.TestCase):
    def test_recall_and_precision(self):
        scores = [
            # 2 should-trigger; one fires, one silent -> recall 0.5
            tv.score_query("p1", True, [True, True, True], 0.5),
            tv.score_query("p2", True, [False, False, False], 0.5),
            # 2 should-not; one silent, one fires -> precision 0.5
            tv.score_query("n1", False, [False, False, False], 0.5),
            tv.score_query("n2", False, [True, True, True], 0.5),
        ]
        summ = tv.summarize(scores)
        self.assertEqual(summ["total_queries"], 4)
        self.assertEqual(summ["should_trigger_count"], 2)
        self.assertEqual(summ["should_not_count"], 2)
        self.assertAlmostEqual(summ["recall"], 0.5)
        self.assertAlmostEqual(summ["precision"], 0.5)
        # p1 pass, p2 fail, n1 pass, n2 fail -> 2 passed
        self.assertEqual(summ["passed"], 2)
        self.assertEqual(summ["failed"], 2)

    def test_perfect_scores(self):
        scores = [
            tv.score_query("p", True, [True, True, True], 0.5),
            tv.score_query("n", False, [False, False, False], 0.5),
        ]
        summ = tv.summarize(scores)
        self.assertEqual(summ["recall"], 1.0)
        self.assertEqual(summ["precision"], 1.0)
        self.assertEqual(summ["pass_rate"], 1.0)

    def test_missing_class_yields_none(self):
        scores = [tv.score_query("p", True, [True], 0.5)]
        summ = tv.summarize(scores)
        self.assertEqual(summ["recall"], 1.0)
        self.assertIsNone(summ["precision"])  # no should-not queries


# --- misc: command / env construction ----------------------------------------

class TestRunnerPlumbing(unittest.TestCase):
    def test_build_command_includes_flags(self):
        cmd = tv.build_command("do a thing", None)
        self.assertEqual(cmd[:3], ["claude", "-p", "do a thing"])
        self.assertIn("stream-json", cmd)
        self.assertIn("--include-partial-messages", cmd)
        self.assertNotIn("--model", cmd)

    def test_build_command_with_model(self):
        cmd = tv.build_command("q", "claude-sonnet-4-6")
        self.assertIn("--model", cmd)
        self.assertIn("claude-sonnet-4-6", cmd)

    def test_child_env_drops_claudecode(self):
        os.environ["CLAUDECODE"] = "1"
        try:
            env = tv.child_env()
            self.assertNotIn("CLAUDECODE", env)
        finally:
            os.environ.pop("CLAUDECODE", None)


if __name__ == "__main__":
    unittest.main()
