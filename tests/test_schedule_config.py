"""
Tests for schedule_config.py — the Phase 12 per-company schedule reader.

Every test drives the CLI as a subprocess (the exact seam daily-run.sh /
schedule.sh use) via _helpers.run_script, plus a few direct calls into the safe
flat-YAML fallback parser. The overriding invariant: an ABSENT or empty
schedule.yaml reproduces today's behaviour byte-for-byte (defaults are sacred).

Covers:
  * defaults == today (no yaml): daily/research cron, agent knobs, all steps run
  * cadence translations: every Nh, hourly, weekdays-9-17, raw-cron passthrough,
    invalid -> fallback with rc 2
  * should_run gating: every-run / daily / weekly / on-trigger / every-Nth, plus
    duty de-selection and a disabled employee
  * roster generation reflects the config
  * the stdlib fallback parser (scalars / one-line maps / lists / block maps)
"""

import os
import shutil
import tempfile
import unittest

import _helpers
from _helpers import run_script

import schedule_config as sc


def _make_company(tmp, body=None):
    """Create tmp/.company/org, seeding schedule.yaml with `body`. body=None means
    NO config file (removing any stale one, so a reused tmp is truly default)."""
    org = os.path.join(tmp, ".company", "org")
    os.makedirs(org, exist_ok=True)
    cfg = os.path.join(org, "schedule.yaml")
    if body is None:
        if os.path.exists(cfg):
            os.remove(cfg)
    else:
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(body)
    return os.path.join(tmp, ".company")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def company(self, body=None):
        return _make_company(self.tmp, body)

    def cron(self, company, kind="daily", minute="7"):
        return run_script("schedule_config.py", "--company", company,
                          "--cron", kind, "--minute", minute)

    def should(self, company, step, hour, dow):
        rc, _, _ = run_script("schedule_config.py", "--company", company,
                              "--should-run", step, "--hour", str(hour), "--dow", str(dow))
        return rc


class TestDefaults(Base):
    def test_no_yaml_daily_is_today(self):
        rc, out, _ = self.cron(self.company(), "daily", "7")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "7 */6 * * *")

    def test_no_yaml_research_is_today(self):
        rc, out, _ = self.cron(self.company(), "research", "9")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "9 3 * * 0")

    def test_no_yaml_agent_defaults(self):
        c = self.company()
        for key, val in (("model", "claude-sonnet-4-6"), ("timeout", "600"), ("daily_cap", "4")):
            rc, out, _ = run_script("schedule_config.py", "--company", c, "--agent", key)
            self.assertEqual((rc, out.strip()), (0, val), key)

    def test_no_yaml_all_steps_run(self):
        c = self.company()
        for step in ("backup", "reinforce", "decay", "verify", "entropy",
                     "survey", "report", "agent"):
            self.assertEqual(self.should(c, step, 3, 2), 0, step)

    def test_empty_commented_yaml_is_absent_equivalent(self):
        # The shipped template is all-comments -> must behave exactly like absent.
        rc, out, _ = self.cron(self.company("# only comments\n\n#cadence: every 2h\n"), "daily", "7")
        self.assertEqual((rc, out.strip()), (0, "7 */6 * * *"))

    def test_research_disabled_flag(self):
        c = self.company("research: { enabled: false }\n")
        rc, _, _ = run_script("schedule_config.py", "--company", c, "--research-enabled")
        self.assertEqual(rc, 1)                       # 1 == off
        rc2, _, _ = run_script("schedule_config.py", "--company", self.company(), "--research-enabled")
        self.assertEqual(rc2, 0)                      # default on


class TestCadenceTranslation(Base):
    def test_every_2h(self):
        rc, out, _ = self.cron(self.company("cadence: every 2h\n"), "daily", "7")
        self.assertEqual((rc, out.strip()), (0, "7 */2 * * *"))

    def test_hourly(self):
        rc, out, _ = self.cron(self.company("cadence: hourly\n"), "daily", "7")
        self.assertEqual((rc, out.strip()), (0, "7 * * * *"))

    def test_weekdays_window(self):
        rc, out, _ = self.cron(self.company("cadence: weekdays-9-17\n"), "daily", "7")
        self.assertEqual((rc, out.strip()), (0, "7 9-17 * * 1-5"))

    def test_raw_cron_passthrough(self):
        rc, out, _ = self.cron(self.company("cadence: 15 8 * * 1\n"), "daily", "7")
        self.assertEqual((rc, out.strip()), (0, "15 8 * * 1"))

    def test_invalid_cadence_falls_back_rc2(self):
        rc, out, _ = self.cron(self.company("cadence: banana\n"), "daily", "7")
        self.assertEqual(rc, 2)                        # rc 2 signals fallback
        self.assertEqual(out.strip(), "7 */6 * * *")   # ...but a valid default is still printed

    def test_out_of_range_hours_falls_back_rc2(self):
        rc, out, _ = self.cron(self.company("cadence: every 99h\n"), "daily", "7")
        self.assertEqual((rc, out.strip()), (2, "7 */6 * * *"))

    def test_research_weekly_translation(self):
        rc, out, _ = self.cron(self.company("research: { enabled: true, cadence: weekly-mon-05 }\n"),
                               "research", "9")
        self.assertEqual((rc, out.strip()), (0, "9 5 * * 1"))

    def test_research_invalid_falls_back_rc2(self):
        rc, out, _ = self.cron(self.company("research: { cadence: nonsense }\n"), "research", "9")
        self.assertEqual((rc, out.strip()), (2, "9 3 * * 0"))

    # --- P9-D2: raw passthrough must be validated before it is trusted ---------
    def test_raw_cron_junk_fields_falls_back_rc2(self):
        # 5 tokens but non-cron chars -> reject, keep the valid default.
        rc, out, _ = self.cron(self.company("cadence: GARBAGE foo bar baz qux\n"), "daily", "7")
        self.assertEqual((rc, out.strip()), (2, "7 */6 * * *"))

    def test_raw_cron_newline_injection_falls_back(self):
        # A double-quoted YAML scalar with \n embeds a REAL newline -> would split
        # the crontab into two lines. Must be refused; output stays one clean line.
        rc, out, _ = self.cron(self.company('cadence: "a b c d\\ne"\n'), "daily", "7")
        self.assertEqual(rc, 2)
        self.assertEqual(out.strip(), "7 */6 * * *")
        self.assertNotIn("\n", out.strip())

    def test_raw_cron_bad_charset_field_falls_back(self):
        # 5 fields, cron-ish, but a shell metacharacter in one field -> reject.
        rc, out, _ = self.cron(self.company("cadence: 15 8 * * mon;rm\n"), "daily", "7")
        self.assertEqual((rc, out.strip()), (2, "7 */6 * * *"))

    def test_raw_cron_semantically_invalid_falls_back_rc2(self):
        # P9-D3 end-to-end through the CLI: charset-clean but out-of-range/malformed
        # exprs must exit 2 and print the default, never the junk expr.
        for body in ('cadence: ",, * * * *"\n',
                     "cadence: 99 99 99 99 99\n",
                     "cadence: 1- * * * *\n",
                     "cadence: */0 * * * *\n"):
            rc, out, _ = self.cron(self.company(body), "daily", "7")
            self.assertEqual((rc, out.strip()), (2, "7 */6 * * *"), body)


class TestGating(Base):
    def test_every_run_always(self):
        c = self.company("tony: { cadence: every-run }\n")
        self.assertEqual(self.should(c, "reinforce", 0, 0), 0)
        self.assertEqual(self.should(c, "reinforce", 13, 3), 0)

    def test_daily_first_tick_only(self):
        c = self.company("elon: { cadence: daily }\n")
        self.assertEqual(self.should(c, "survey", 3, 2), 0)    # first tick of the day
        self.assertEqual(self.should(c, "survey", 12, 2), 1)   # a later tick -> skip

    def test_weekly_sunday_first_tick(self):
        c = self.company("elon: { cadence: weekly }\n")
        self.assertEqual(self.should(c, "survey", 3, 0), 0)    # Sunday, first tick
        self.assertEqual(self.should(c, "survey", 3, 3), 1)    # Wednesday -> skip
        self.assertEqual(self.should(c, "survey", 12, 0), 1)   # Sunday, later tick -> skip

    def test_on_trigger_never_in_batch(self):
        c = self.company("tony: { cadence: on-trigger }\n")
        self.assertEqual(self.should(c, "decay", 0, 0), 1)
        self.assertEqual(self.should(c, "decay", 13, 4), 1)

    def test_every_nth(self):
        c = self.company("tony: { cadence: every-2 }\n")
        self.assertEqual(self.should(c, "entropy", 0, 0), 0)   # idx 0 -> run
        self.assertEqual(self.should(c, "entropy", 6, 0), 1)   # idx 1 -> skip
        self.assertEqual(self.should(c, "entropy", 12, 0), 0)  # idx 2 -> run

    def test_rag_index_step_owned_by_tony_runs_by_default(self):
        # Phase 13 A.1: the new deterministic step is Tony's; absent config it runs.
        self.assertEqual(self.should(self.company(), "rag_index", 3, 2), 0)

    def test_rag_index_deselectable_via_tony_duties(self):
        c = self.company("tony: { duties: [reinforce, decay, entropy] }\n")
        self.assertEqual(self.should(c, "rag_index", 3, 2), 1)  # omitted -> skip
        self.assertEqual(self.should(c, "decay", 3, 2), 0)      # kept

    def test_duty_deselection_skips_only_that_step(self):
        c = self.company("tony: { duties: [decay] }\n")
        self.assertEqual(self.should(c, "decay", 3, 2), 0)     # kept
        self.assertEqual(self.should(c, "reinforce", 3, 2), 1) # not in duties -> skip

    def test_disabled_employee_skips_its_step(self):
        c = self.company("elon: { enabled: false }\n")
        self.assertEqual(self.should(c, "survey", 3, 2), 1)

    def test_unowned_step_always_runs(self):
        # 'build' is a competition duty, not a scheduled deterministic step:
        # no STEP_OWNER -> fail-open -> run, even with its employee disabled.
        c = self.company("bob: { enabled: false }\n")
        self.assertEqual(self.should(c, "build", 3, 2), 0)


class TestRoster(Base):
    def test_roster_reflects_config(self):
        c = self.company("cadence: every 2h\nmike: { cadence: weekly, duties: [research] }\n")
        rc, out, _ = run_script("schedule_config.py", "--company", c, "--roster")
        self.assertEqual(rc, 0)
        self.assertIn("Schedule Roster", out)
        self.assertIn("GENERATED", out)      # marked generated, never hand-edited
        self.assertIn("*/2", out)            # the configured tick
        self.assertIn("mike", out)


class TestRawCronValidation(unittest.TestCase):
    """P9-D2: daily_cron/research_cron must never return an unsafe raw expr with
    ok=True. Direct-import unit locks the gate independent of YAML parsing."""

    def test_junk_fields_rejected(self):
        expr, ok = sc.daily_cron("GARBAGE foo bar baz qux", "7")
        self.assertFalse(ok)
        self.assertEqual(expr, "7 */6 * * *")

    def test_newline_injection_rejected(self):
        expr, ok = sc.daily_cron("a b c d\ne", "7")
        self.assertFalse(ok)
        self.assertNotIn("\n", expr)
        self.assertEqual(expr, "7 */6 * * *")

    def test_tab_between_fields_rejected(self):
        expr, ok = sc.daily_cron("15 8 *\t* 1", "7")
        self.assertFalse(ok)
        self.assertEqual(expr, "7 */6 * * *")

    def test_charset_violation_rejected(self):
        expr, ok = sc.daily_cron("15 8 * * mon;rm", "7")
        self.assertFalse(ok)
        self.assertEqual(expr, "7 */6 * * *")

    def test_valid_raw_cron_accepted(self):
        expr, ok = sc.daily_cron("*/2 8 * * 1", "7")
        self.assertTrue(ok)
        self.assertEqual(expr, "*/2 8 * * 1")

    def test_research_raw_junk_rejected(self):
        expr, ok = sc.research_cron("x y z w v", "9")
        self.assertFalse(ok)
        self.assertEqual(expr, "9 3 * * 0")

    def test_valid_cron_expr_helper(self):
        self.assertTrue(sc._valid_cron_expr("0 */6 * * *"))
        self.assertTrue(sc._valid_cron_expr("30 9-17 * * 1-5"))
        self.assertFalse(sc._valid_cron_expr("0 */6 * *"))        # 4 fields
        self.assertFalse(sc._valid_cron_expr("0 */6 * * * *"))    # 6 fields
        self.assertFalse(sc._valid_cron_expr("0 */6 * * x\n"))    # control char
        self.assertFalse(sc._valid_cron_expr("0 */6 * * mon"))    # name outside charset

    # --- P9-D3: charset-clean but SEMANTICALLY-invalid exprs must be rejected --
    D3_BAD = [
        ",, * * * *",        # empty list elements
        "99 99 99 99 99",    # every field out of range
        "1- * * * *",        # dangling range
        "* * * * */0",       # */0 step
        "0-0-0 * * * *",     # malformed range
        "*/*/* * * * *",     # double step
        "5-2 * * * *",       # backwards range
        "* 25 * * *",        # hour out of range
        "* * 0 * *",         # day-of-month below 1
        "* * * 13 *",        # month out of range
        "* * * * 8",         # day-of-week out of range
        "*/0 * * * *",       # */0 in minute
    ]
    D3_GOOD = ["15 8 * * 1", "*/15 9-17 * * 1-5", "0 0 1 1 *", "* * * * 7"]

    def test_d3_semantically_invalid_rejected(self):
        for c in self.D3_BAD:
            expr, ok = sc.daily_cron(c, "7")
            self.assertFalse(ok, c)
            self.assertEqual(expr, "7 */6 * * *", c)   # default kept

    def test_d3_valid_still_pass(self):
        for c in self.D3_GOOD:
            expr, ok = sc.daily_cron(c, "7")
            self.assertTrue(ok, c)
            self.assertEqual(expr, c, c)

    def test_valid_cron_field_domains(self):
        self.assertTrue(sc._valid_cron_field("*/15", 0, 59))
        self.assertTrue(sc._valid_cron_field("9-17", 0, 23))
        self.assertTrue(sc._valid_cron_field("1,15,31", 1, 31))
        self.assertFalse(sc._valid_cron_field(",,", 0, 59))       # empty elements
        self.assertFalse(sc._valid_cron_field("5-2", 0, 59))      # backwards
        self.assertFalse(sc._valid_cron_field("1-", 0, 59))       # dangling
        self.assertFalse(sc._valid_cron_field("*/0", 0, 59))      # zero step
        self.assertFalse(sc._valid_cron_field("60", 0, 59))       # over max
        self.assertFalse(sc._valid_cron_field("0", 1, 31))        # under min (dom)


class TestGibbyEmptyDutiesReader(Base):
    """P9-D1 exploit surface: the reader HONORS an explicit empty duty list (it
    really does gate verify off), which is why the VALIDATOR must reject it."""

    def test_explicit_empty_gibby_skips_verify(self):
        c = self.company("gibby: { duties: [] }\n")
        self.assertEqual(self.should(c, "verify", 3, 2), 1)


class TestFallbackParser(unittest.TestCase):
    """The stdlib safe-YAML subset (used when PyYAML is absent)."""

    def test_scalars_lists_and_inline_maps(self):
        d = sc._fallback_parse(
            "cadence: every 2h\n"
            "research: { enabled: false, cadence: weekly-sun-03 }\n"
            "tony: { cadence: every-run, duties: [decay, entropy] }\n"
        )
        self.assertEqual(d["cadence"], "every 2h")
        self.assertEqual(d["research"]["enabled"], False)
        self.assertEqual(d["research"]["cadence"], "weekly-sun-03")
        self.assertEqual(d["tony"]["duties"], ["decay", "entropy"])

    def test_comments_and_blanks_ignored(self):
        self.assertEqual(sc._fallback_parse("# a\n\n   # b\n"), {})

    def test_block_map_children(self):
        d = sc._fallback_parse("tony:\n  cadence: daily\n  budget: 5\n")
        self.assertEqual(d["tony"]["cadence"], "daily")
        self.assertEqual(d["tony"]["budget"], 5)

    def test_int_and_bool_coercion(self):
        d = sc._fallback_parse("agent: { timeout: 300, daily_cap: 2 }\nmike: { enabled: yes }\n")
        self.assertEqual(d["agent"]["timeout"], 300)
        self.assertEqual(d["agent"]["daily_cap"], 2)
        self.assertIs(d["mike"]["enabled"], True)


if __name__ == "__main__":
    unittest.main()
