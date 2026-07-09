"""
Phase 26 — trigger-security follow-ups.

Item 1 (CRITICAL) — fire-trigger.sh commits state only AFTER STAGE-1 parse /
schema validation / require_confirm resolution, not before. This is the
inverted DoS regression: N schema-invalid payloads must consume ZERO cap
slots, and a park must consume nothing until it actually proceeds. These tests
drive the real `fire-trigger.sh` (not just the python engine) since the bug
lived in the shell script's ORDERING, not in trigger_engine.py itself.

Item 2 — require_confirm is now an honest, deterministic HOLD: no
`.pending.json` artifact, no state consumed, a clear manual-dispatch log line.
"""

import concurrent.futures as cf
import json
import os
import subprocess
import tempfile
import unittest

import _helpers

SCRIPTS = _helpers.SCRIPTS_DIR
FIRE_SH = os.path.join(SCRIPTS, "fire-trigger.sh")


def _company(d, triggers):
    tdir = os.path.join(d, ".company", "org", "triggers")
    os.makedirs(tdir)
    for name, body in triggers.items():
        with open(os.path.join(tdir, f"{name}.yaml"), "w") as f:
            f.write(body)
    return os.path.join(d, ".company")


def _state(d, name):
    """d is the PARENT dir (as passed to _run/SELF_COMPANY_PROJECT_DIR); the
    state file lives under d/.company/ops/triggers/<name>.json."""
    p = os.path.join(d, ".company", "ops", "triggers", f"{name}.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def _run(d, name, payload, *extra):
    """Run fire-trigger.sh --no-spawn (decide+commit(if it proceeds)+log, no
    real agent spawn) against the .company skeleton rooted at parent dir `d`."""
    env = {**os.environ, "SELF_COMPANY_PROJECT_DIR": d}
    p = subprocess.run(["bash", FIRE_SH, name, payload, "--no-spawn", *extra],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


TRIGGERS = {
    # untrusted (default), no confirm, cap=2, no cooldown/dedupe noise.
    "cap2": "name: cap2\naction: x\ncondition:\ncooldown: 0\ndedupe: false\n"
            "max_fires_per_day: 2\n",
    # untrusted, dedupe ON (default) — to prove a rejected payload can't
    # poison dedupe against a later distinct valid one.
    "dedupe_on": "name: dedupe_on\naction: x\ncondition:\ncooldown: 0\n",
    # require_confirm gate.
    "payout": "name: payout\naction: wire the payout\ncondition:\ncooldown: 0\n"
              "dedupe: false\nrequire_confirm: true\n",
    # a plain cap=3 trigger for the concurrency re-proof.
    "cap3": "name: cap3\naction: x\ncondition:\ncooldown: 0\ndedupe: false\n"
            "max_fires_per_day: 3\n",
}

SCHEMA_BAD = json.dumps({"note": "line1\nrm -rf /"})   # embedded newline -> schema reject


class TestItem1InvertedDoS(unittest.TestCase):
    """The live repro, inverted: N schema-invalid payloads must consume ZERO
    cap/cooldown/dedupe, and a later valid event must still fire normally."""

    def test_schema_invalid_payloads_consume_zero_cap(self):
        with tempfile.TemporaryDirectory() as d:
            _company(d, TRIGGERS)
            for _ in range(3):
                rc, out, _ = _run(d, "cap2", SCHEMA_BAD)
                self.assertEqual(rc, 0)
                self.assertIn("held (schema)", out)
            # nothing was ever committed by the 3 garbage payloads
            st = _state(d, "cap2")
            self.assertTrue(st is None or st.get("fires") in (None, {}),
                            f"schema-rejects must not consume state: {st}")
            # a later, valid, DISTINCT payload still fires (cap intact)
            rc, out, _ = _run(d, "cap2", json.dumps({"v": 1}))
            self.assertIn("fired (no-spawn)", out)
            st = _state(d, "cap2")
            today = list(st["fires"].keys())[0]
            self.assertEqual(st["fires"][today], 1)
            # and the cap (2) still allows one more genuine fire
            rc, out, _ = _run(d, "cap2", json.dumps({"v": 2}))
            self.assertIn("fired (no-spawn)", out)
            # a third genuine fire now correctly hits the (untouched) cap
            rc, out, _ = _run(d, "cap2", json.dumps({"v": 3}))
            self.assertIn("held:", out)
            self.assertIn("daily_cap_reached_(2)", out.replace(" ", "_"))

    def test_rejected_payload_does_not_poison_dedupe(self):
        with tempfile.TemporaryDirectory() as d:
            _company(d, TRIGGERS)
            rc, out, _ = _run(d, "dedupe_on", SCHEMA_BAD)
            self.assertIn("held (schema)", out)
            # a distinct, VALID payload must still fire — not held as a
            # "duplicate" of anything, since the reject never touched last_hash
            rc, out, _ = _run(d, "dedupe_on", json.dumps({"v": 1}))
            self.assertIn("fired (no-spawn)", out)
            # dedupe still works normally for a genuinely fired payload
            rc, out, _ = _run(d, "dedupe_on", json.dumps({"v": 1}))
            self.assertIn("held:", out)
            self.assertIn("duplicate", out)

    def test_genuine_fire_consumes_exactly_one_slot(self):
        with tempfile.TemporaryDirectory() as d:
            _company(d, TRIGGERS)
            _run(d, "cap2", json.dumps({"v": 1}))
            st = _state(d, "cap2")
            today = list(st["fires"].keys())[0]
            self.assertEqual(st["fires"][today], 1)

    def test_concurrent_burst_cap_never_exceeded_new_ordering(self):
        # Phase 21's concurrency property, re-proven end-to-end through the
        # NEW commit-after-validation ordering (not just at the engine level).
        with tempfile.TemporaryDirectory() as d:
            _company(d, TRIGGERS)

            def go(i):
                return _run(d, "cap3", json.dumps({"i": i}))

            with cf.ThreadPoolExecutor(max_workers=10) as ex:
                results = list(ex.map(go, range(12)))
            fired = sum(1 for _, out, _ in results if "fired (no-spawn)" in out)
            self.assertEqual(fired, 3)
            st = _state(d, "cap3")
            today = list(st["fires"].keys())[0]
            self.assertEqual(st["fires"][today], 3)


class TestItem2HonestHold(unittest.TestCase):
    def test_require_confirm_holds_consumes_nothing_no_pending_file(self):
        with tempfile.TemporaryDirectory() as d:
            _company(d, TRIGGERS)
            rc, out, _ = _run(d, "payout", json.dumps({"amount": 100}))
            self.assertEqual(rc, 0)
            self.assertIn("held: require_confirm", out)
            self.assertIn("manual dispatch required", out)
            # no state committed
            self.assertIsNone(_state(d, "payout"))
            # no pending-file artifact anywhere under ops/triggers
            trig_dir = os.path.join(d, ".company", "ops", "triggers")
            if os.path.isdir(trig_dir):
                self.assertFalse(any(f.endswith(".pending.json")
                                     for f in os.listdir(trig_dir)))
            # firing again is still a clean hold (not "duplicate": nothing to
            # dedupe against — it never got recorded the first time)
            rc, out, _ = _run(d, "payout", json.dumps({"amount": 100}))
            self.assertIn("held: require_confirm", out)


if __name__ == "__main__":
    unittest.main()
