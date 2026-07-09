"""
Tests for decay.py — decay math and tier classification.

Covers the deterministic core (half-life, decay_score, classify_record) and a
CLI smoke confirming policy provenance is reported (P3) and that the documented
default --config path is honoured.
"""

import os
import tempfile
import unittest
from datetime import datetime

import _helpers
import decay


def mem(**kw):
    base = {
        "id": "m", "tier": "L0", "owner": "Tony", "sources": ["[s#1]"],
        "created": "2026-06-01", "last_reinforced": "2026-06-01",
        "reinforce_count": 1, "decay_score": 1.0, "status": "active", "_body": "x",
    }
    base.update(kw)
    return base


DEF = dict(hl_base=7.0, hl_growth=0.5, l0_drop_threshold=0.25,
           l1_archive_threshold=0.15, l1_demote_rc=2, l0_to_l1_rc=2, l1_to_l2_rc=4)


def classify(m, now="2026-06-15"):
    return decay.classify_record(m, datetime.strptime(now, "%Y-%m-%d"), **DEF)


class TestDecayMath(unittest.TestCase):
    def test_half_life_formula(self):
        self.assertEqual(decay.half_life(1, 7.0, 0.5), 7.0)
        self.assertEqual(decay.half_life(3, 7.0, 0.5), 14.0)
        self.assertEqual(decay.half_life(5, 7.0, 0.5), 21.0)

    def test_decay_score_one_halflife(self):
        # age == half_life -> 0.5
        self.assertAlmostEqual(decay.compute_decay_score(7.0, 1, 7.0, 0.5), 0.5, places=6)

    def test_decay_score_fresh_is_one(self):
        self.assertEqual(decay.compute_decay_score(0.0, 1, 7.0, 0.5), 1.0)

    def test_decay_score_clamped(self):
        self.assertLessEqual(decay.compute_decay_score(1000.0, 1, 7.0, 0.5), 1.0)
        self.assertGreaterEqual(decay.compute_decay_score(1000.0, 1, 7.0, 0.5), 0.0)

    def test_rc_below_one_treated_as_one(self):
        self.assertEqual(decay.compute_decay_score(7.0, 0, 7.0, 0.5),
                         decay.compute_decay_score(7.0, 1, 7.0, 0.5))


class TestClassify(unittest.TestCase):
    def test_l0_fresh_keep(self):
        action, _ = classify(mem(last_reinforced="2026-06-14"))  # 1 day old
        self.assertEqual(action, "keep")

    def test_l0_stale_drops(self):
        # 30 days, rc=1 -> decay ~0.05 < 0.25
        action, info = classify(mem(last_reinforced="2026-05-16"))
        self.assertEqual(action, "drop")
        self.assertLess(info["decay_score"], 0.25)

    def test_l0_rc2_is_upgrade_candidate(self):
        action, _ = classify(mem(reinforce_count=2, last_reinforced="2026-06-14"))
        self.assertEqual(action, "upgrade-candidate")

    def test_l1_low_rc_demotes(self):
        # L1 rc=2 gone cold -> demote (rc <= L1_DEMOTE_RC)
        action, _ = classify(mem(tier="L1", reinforce_count=2, last_reinforced="2026-04-01"))
        self.assertEqual(action, "demote")

    def test_l1_high_rc_archives(self):
        # L1 rc=3 gone cold -> archive (rc > L1_DEMOTE_RC)
        action, _ = classify(mem(tier="L1", reinforce_count=3, last_reinforced="2026-04-01"))
        self.assertEqual(action, "archive")

    def test_l1_rc4_upgrade_candidate(self):
        action, _ = classify(mem(tier="L1", reinforce_count=4, last_reinforced="2026-06-14"))
        self.assertEqual(action, "upgrade-candidate")

    def test_l2_never_decays(self):
        action, _ = classify(mem(tier="L2", reinforce_count=5, last_reinforced="2020-01-01"))
        self.assertEqual(action, "l2-keep")

    def test_archived_never_upgrade_candidate(self):
        # Phase 5 Item 1 + C1 (N6): archived/tombstoned files are never
        # promotion candidates, regardless of rc — closes "archived stubs in
        # Tony's upgrade backlog" and "archived files promoted to L1".
        for tier, rc in (("L0", 2), ("L1", 4)):
            action, _ = classify(mem(tier=tier, reinforce_count=rc,
                                     status="archived",
                                     last_reinforced="2026-06-14"))
            self.assertEqual(action, "keep", f"{tier} rc={rc}")

    def test_archived_is_never_redropped(self):
        # An archived record out of the active lifecycle: even fully decayed
        # it classifies keep (only the reap pass touches it past grace).
        action, _ = classify(mem(status="archived",
                                 last_reinforced="2026-04-01"))
        self.assertEqual(action, "keep")

    def test_absorbed_classifies_keep_like_archived(self):
        # Phase 6 Item 1: `absorbed` (consolidation-agent merge tombstone) is
        # out of the active lifecycle — never promoted, never re-dropped —
        # exactly like `archived`, via the shared is_tombstoned vocabulary.
        for tier, rc in (("L0", 2), ("L1", 4)):
            action, _ = classify(mem(tier=tier, reinforce_count=rc,
                                     status="absorbed",
                                     last_reinforced="2026-04-01"))
            self.assertEqual(action, "keep", f"{tier} rc={rc}")

    def test_missing_status_kept_but_not_promoted(self):
        # Promotion requires status == "active" exactly; ambiguous state
        # (missing status) is kept but never promoted.
        action, _ = classify(mem(reinforce_count=2, status=None,
                                 last_reinforced="2026-06-14"))
        self.assertEqual(action, "keep")

    def test_missing_date_is_kept_untouched(self):
        action, info = classify(mem(last_reinforced=None))
        self.assertEqual(action, "keep")
        self.assertIsNone(info["decay_score"])

    def test_unparseable_date_is_kept_untouched(self):
        action, info = classify(mem(last_reinforced="not-a-date"))
        self.assertEqual(action, "keep")
        self.assertIsNone(info["decay_score"])


class TestApplyDrop(unittest.TestCase):
    def test_apply_drop_is_soft_delete_tombstone(self):
        # Phase 5 Item 2 (N2): this previously asserted the stale L0 was
        # physically unlinked — that WAS the durability hole. Drop is now a
        # soft-delete: the file remains as a recoverable tombstone
        # (status: archived + invalid_at: <now>), excluded from active scans.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L0-working", "old.md")
            _helpers.write_memory(path, id="old", last_reinforced="2026-05-01")
            rc, out, err = _helpers.run_script(
                "decay.py", "--memory-dir", d, "--now", "2026-06-25",
                "--config", "/nonexistent.md", "--apply")
            self.assertEqual(rc, 0, err)
            self.assertTrue(os.path.exists(path), "drop must tombstone, not unlink")
            with open(path) as f:
                txt = f.read()
            self.assertIn("status: archived", txt)
            self.assertIn("invalid_at: 2026-06-25", txt)
            self.assertIn("body", txt)  # content recoverable

    def test_tombstone_reaped_only_after_grace_from_invalid_at(self):
        # Grace runs from the LATER of last_reinforced/invalid_at: a tombstone
        # dropped today stays recoverable a full REAP_GRACE_DAYS even though
        # last_reinforced is ancient; past the window the reap pass unlinks it.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L0-working", "old.md")
            _helpers.write_memory(path, id="old", last_reinforced="2026-05-01")
            _helpers.run_script("decay.py", "--memory-dir", d, "--now",
                                "2026-06-25", "--config", "/nonexistent.md",
                                "--apply")
            # inside grace (invalid_at 2026-06-25 + 7d): still present
            data = _helpers.run_json("decay.py", "--memory-dir", d, "--now",
                                     "2026-06-30", "--config",
                                     "/nonexistent.md", "--apply")
            self.assertEqual(data["actions"]["reaped"], [])
            self.assertTrue(os.path.exists(path))
            # idempotent: the tombstone is not re-dropped (invalid_at stable)
            with open(path) as f:
                self.assertIn("invalid_at: 2026-06-25", f.read())
            # past grace: physically reaped
            data = _helpers.run_json("decay.py", "--memory-dir", d, "--now",
                                     "2026-07-03", "--config",
                                     "/nonexistent.md", "--apply")
            self.assertEqual([x["id"] for x in data["actions"]["reaped"]],
                             ["old"])
            self.assertFalse(os.path.exists(path))

    def test_absorbed_tombstone_reaped_past_grace(self):
        # Phase 6 Item 1: an `absorbed` file (agent merge tombstone with
        # invalid_at) is physically reaped by decay past grace, exactly like
        # `archived` — grace runs from the later of last_reinforced/invalid_at.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L0-working", "dup.md")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                f.write("---\nid: dup\ntier: L0\nowner: Tony\n"
                        'sources: ["[s#1]"]\ncreated: 2026-05-01\n'
                        "last_reinforced: 2026-05-01\nreinforce_count: 1\n"
                        "decay_score: 1.0\nstatus: absorbed\n"
                        "invalid_at: 2026-06-25\n---\nbody\n")
            # inside grace (invalid_at + 7d): kept, status preserved verbatim
            data = _helpers.run_json("decay.py", "--memory-dir", d, "--now",
                                     "2026-06-30", "--config",
                                     "/nonexistent.md", "--apply")
            self.assertEqual(data["actions"]["reaped"], [])
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                self.assertIn("status: absorbed", f.read())  # not normalised away
            # past grace: physically reaped just like archived
            data = _helpers.run_json("decay.py", "--memory-dir", d, "--now",
                                     "2026-07-05", "--config",
                                     "/nonexistent.md", "--apply")
            self.assertEqual([x["id"] for x in data["actions"]["reaped"]], ["dup"])
            self.assertFalse(os.path.exists(path))

    def test_dateless_tombstone_reaped_via_created(self):
        # C2 (BOB-F5): a tombstone with NEITHER last_reinforced NOR invalid_at
        # previously stayed keep forever. It now anchors the grace clock on
        # `created` (stable across the keep-pass rewrite), so it ages out.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L0-working", "stub.md")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                f.write("---\nid: stub\ntier: L0\nowner: Tony\n"
                        'sources: ["[s#1]"]\ncreated: 2026-06-25\n'
                        "reinforce_count: 1\ndecay_score: 1.0\n"
                        "status: archived\n---\nbody\n")
            # inside grace (created 2026-06-25 + 7d): kept
            data = _helpers.run_json("decay.py", "--memory-dir", d, "--now",
                                     "2026-06-30", "--config",
                                     "/nonexistent.md", "--apply")
            self.assertEqual(data["actions"]["reaped"], [])
            self.assertTrue(os.path.exists(path))
            # past grace: reaped (anchor from `created`)
            data = _helpers.run_json("decay.py", "--memory-dir", d, "--now",
                                     "2026-07-05", "--config",
                                     "/nonexistent.md", "--apply")
            self.assertEqual([x["id"] for x in data["actions"]["reaped"]], ["stub"])
            self.assertFalse(os.path.exists(path))

    def test_fully_dateless_mtime_stamps_anchor_and_survives_keep_writes(self):
        # C2 (BOB-F5) must-fix: a stub lacking last_reinforced/invalid_at/created
        # falls back to mtime. But a within-grace tombstone is classified `keep`
        # and the keep-pass REWRITES the file every --apply run, bumping mtime to
        # ~now — so a pure-mtime anchor would reset each run and NEVER cross
        # grace. This models the REAL multi-run cadence: mtime is pinned old only
        # once, and every subsequent run's keep-write is allowed to bump it (as
        # production does). The fix STAMPS invalid_at from the mtime on first
        # encounter, so the grace clock is thereafter stable and it still reaps.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L0-working", "nodate.md")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                f.write("---\nid: nodate\ntier: L0\nowner: Tony\n"
                        'sources: ["[s#1]"]\nreinforce_count: 1\n'
                        "status: archived\n---\nbody\n")
            os.utime(path, (datetime(2026, 6, 25).timestamp(),) * 2)

            # Run 1 (within grace): NOT reaped; invalid_at stamped from mtime.
            data = _helpers.run_json("decay.py", "--memory-dir", d, "--now",
                                     "2026-06-26", "--config",
                                     "/nonexistent.md", "--apply")
            self.assertEqual(data["actions"]["reaped"], [])
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                self.assertIn("invalid_at: 2026-06-25", f.read())  # anchor persisted
            # The keep-write bumped the real file mtime to ~now — proving a
            # pure-mtime anchor would now be worthless. The stamped invalid_at is
            # what carries the clock forward.
            self.assertGreater(os.path.getmtime(path),
                               datetime(2026, 6, 26).timestamp())

            # Run 2 (still within grace by the STAMPED anchor 06-25 + 7d): kept.
            data = _helpers.run_json("decay.py", "--memory-dir", d, "--now",
                                     "2026-06-29", "--config",
                                     "/nonexistent.md", "--apply")
            self.assertEqual(data["actions"]["reaped"], [])
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                self.assertIn("invalid_at: 2026-06-25", f.read())  # anchor stable

            # Run 3 (past grace from the stamped anchor, gap <=7d so no damper):
            # reaped despite mtime having been bumped to ~now by prior runs.
            data = _helpers.run_json("decay.py", "--memory-dir", d, "--now",
                                     "2026-07-03", "--config",
                                     "/nonexistent.md", "--apply")
            self.assertEqual([x["id"] for x in data["actions"]["reaped"]],
                             ["nodate"])
            self.assertFalse(os.path.exists(path))

    def test_apply_preserves_verified_date(self):
        # Regression: decay --apply rewrites frontmatter and must NOT drop the
        # VERIFY stamp (verified_date/verified_by), or it fights the verify loop.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L1-warm", "v.md")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                f.write("---\nid: ver\ntier: L1\nowner: Tony\n"
                        'sources: ["[s#1]"]\ncreated: 2026-06-01\nlast_reinforced: 2026-06-20\n'
                        "reinforce_count: 2\ndecay_score: 1.0\nstatus: active\n"
                        "verified_date: 2026-06-21\nverified_by: Gibby\n---\nbody\n")
            rc, out, err = _helpers.run_script(
                "decay.py", "--memory-dir", d, "--now", "2026-06-25",
                "--config", "/nonexistent.md", "--apply")
            self.assertEqual(rc, 0, err)
            with open(path) as f:
                txt = f.read()
            self.assertIn("verified_date: 2026-06-21", txt)
            self.assertIn("verified_by: Gibby", txt)


class TestOfflineGapDamper(unittest.TestCase):
    """Phase 5 Item 2 (N2): a long machine outage must not purge the store on
    the first tick back — elapsed age is capped at marker + OFFLINE_GAP_DAYS
    and physical reaps are deferred for that run."""

    def _plant_marker(self, d, date_str):
        with open(os.path.join(d, ".last-decay-run"), "w") as f:
            f.write(date_str + "\n")

    def test_15_day_gap_drops_nothing_and_logs_notice(self):
        with tempfile.TemporaryDirectory() as d:
            # fresh at last run (2026-07-01); 15-day outage follows
            path = os.path.join(d, "L0-working", "fresh-at-shutdown.md")
            _helpers.write_memory(path, id="fresh-at-shutdown",
                                  last_reinforced="2026-07-01")
            # an already-tombstoned file whose grace expires during the gap
            arch = os.path.join(d, "L0-working", "tomb.md")
            _helpers.write_memory(arch, id="tomb", status="archived",
                                  last_reinforced="2026-06-20")
            self._plant_marker(d, "2026-07-01")
            rc, out, err = _helpers.run_script(
                "decay.py", "--memory-dir", d, "--now", "2026-07-16",
                "--config", "/nonexistent.md", "--apply")
            self.assertEqual(rc, 0, err)
            import json
            data = json.loads(out)
            # damper engaged + one gap notice (stderr AND warnings)
            self.assertTrue(data["gap_damper"]["active"])
            self.assertEqual(data["gap_damper"]["effective_now"], "2026-07-08")
            self.assertIn("[GAP]", err)
            self.assertTrue(any("offline-gap damper" in w
                                for w in data["warnings"]))
            # damped aging: effective age 7d -> decay 0.5 -> keep, no drop
            self.assertEqual(data["actions"]["drop"], [])
            self.assertTrue(os.path.exists(path))
            # physical reap DEFERRED — nothing unlinked this run
            self.assertEqual(data["actions"]["reaped"], [])
            self.assertEqual([x["id"] for x in data["actions"]["reap_deferred"]],
                             ["tomb"])
            self.assertTrue(os.path.exists(arch))

    def test_normal_gap_no_damper(self):
        with tempfile.TemporaryDirectory() as d:
            _helpers.write_memory(os.path.join(d, "L0-working", "m.md"),
                                  id="m", last_reinforced="2026-07-01")
            self._plant_marker(d, "2026-07-01")
            data = _helpers.run_json("decay.py", "--memory-dir", d, "--now",
                                     "2026-07-05", "--config",
                                     "/nonexistent.md")
            self.assertFalse(data["gap_damper"]["active"])

    def test_missing_marker_no_damper_and_apply_writes_it(self):
        with tempfile.TemporaryDirectory() as d:
            _helpers.write_memory(os.path.join(d, "L0-working", "m.md"),
                                  id="m", last_reinforced="2026-07-01")
            data = _helpers.run_json("decay.py", "--memory-dir", d, "--now",
                                     "2026-07-02", "--config",
                                     "/nonexistent.md", "--apply")
            self.assertFalse(data["gap_damper"]["active"])
            self.assertIsNone(data["gap_damper"]["last_run"])
            marker = os.path.join(d, ".last-decay-run")
            self.assertTrue(os.path.exists(marker))
            with open(marker) as f:
                self.assertEqual(f.read().strip(), "2026-07-02")

    def test_dry_run_never_writes_marker(self):
        with tempfile.TemporaryDirectory() as d:
            _helpers.write_memory(os.path.join(d, "L0-working", "m.md"),
                                  id="m", last_reinforced="2026-07-01")
            _helpers.run_json("decay.py", "--memory-dir", d, "--now",
                              "2026-07-02", "--config", "/nonexistent.md")
            self.assertFalse(os.path.exists(os.path.join(d, ".last-decay-run")))

    def test_memory_named_dir_uses_ops_marker(self):
        # convention: .../memory -> sibling ops/.last-decay-run
        with tempfile.TemporaryDirectory() as d:
            mem = os.path.join(d, "memory")
            _helpers.write_memory(os.path.join(mem, "L0-working", "m.md"),
                                  id="m", last_reinforced="2026-07-01")
            _helpers.run_json("decay.py", "--memory-dir", mem, "--now",
                              "2026-07-02", "--config", "/nonexistent.md",
                              "--apply")
            self.assertTrue(os.path.exists(
                os.path.join(d, "ops", ".last-decay-run")))


class TestCLIProvenance(unittest.TestCase):
    def test_config_block_reports_policy_source(self):
        real_policy = os.path.join(
            _helpers.REPO_ROOT, "plugin", "skills", "self-company", "assets", "company-template", "org", "policy.md")
        with tempfile.TemporaryDirectory() as d:
            data = _helpers.run_json(
                "decay.py", "--memory-dir", d, "--now", "2026-06-25",
                "--config", real_policy)
            self.assertEqual(data["config"]["sources"]["HL_BASE"], "policy")
            self.assertEqual(data["config"]["values"]["HL_BASE"], 7.0)

    def test_policy_table_tuning_changes_behavior(self):
        # The P1 guarantee: editing the §7 TABLE actually changes outcomes.
        real_policy = os.path.join(
            _helpers.REPO_ROOT, "plugin", "skills", "self-company", "assets", "company-template", "org", "policy.md")
        with open(real_policy, encoding="utf-8") as f:
            policy_text = f.read()
        tuned = policy_text.replace("| `L0_DROP_THRESHOLD` | **0.25**",
                                    "| `L0_DROP_THRESHOLD` | **0.99**")
        self.assertNotEqual(tuned, policy_text, "policy fixture line not found")
        with tempfile.TemporaryDirectory() as d:
            _helpers.write_memory(os.path.join(d, "L0-working", "a.md"),
                                  id="a", last_reinforced="2026-06-15")  # 10d, decay~0.37
            pol = os.path.join(d, "policy.md")
            with open(pol, "w", encoding="utf-8") as f:
                f.write(tuned)
            data = _helpers.run_json("decay.py", "--memory-dir", d,
                                     "--now", "2026-06-25", "--config", pol)
            self.assertEqual(data["config"]["values"]["L0_DROP_THRESHOLD"], 0.99)
            self.assertEqual([x["id"] for x in data["actions"]["drop"]], ["a"])


def _write_charter_seed(path, *, id, tier="L0", status="active",
                        last_reinforced="2026-06-01"):
    """A blessed-style seed: charter:<slug> source, no transcript source."""
    _helpers.write_memory(path, id=id, tier=tier, status=status,
                          sources=f'["charter:{id}"]',
                          created="2026-06-01", last_reinforced=last_reinforced)


class TestCharterGuard(unittest.TestCase):
    """Phase 4 Item 1 — blessed charter seeds are never dropped/demoted/
    archived/reaped by decay, regardless of tier; non-blessed charter
    claims decay normally (anti-abuse)."""

    def test_blessed_seed_survives_l0_drop_with_warning(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L0-working", "org-hierarchy.md")
            _write_charter_seed(path, id="org-hierarchy")
            data = _helpers.run_json("decay.py", "--memory-dir", d,
                                     "--apply", "--now", "2026-07-20")
            self.assertEqual(data["actions"]["drop"], [])
            self.assertTrue(os.path.exists(path))
            self.assertTrue(any("charter-guard" in w for w in data["warnings"]))

    def test_nonblessed_charter_claim_still_drops(self):
        # Anti-abuse: `provenance: charter` + charter:<slug> source on a
        # NON-blessed id gets no protection.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L0-working", "fake-axiom.md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("---\nid: fake-axiom\ntier: L0\nowner: Tony\n"
                        "provenance: charter\nsources: [\"charter:fake-axiom\"]\n"
                        "created: 2026-06-01\nlast_reinforced: 2026-06-01\n"
                        "reinforce_count: 1\ndecay_score: 1.0\nstatus: active\n"
                        "---\nbody\n")
            data = _helpers.run_json("decay.py", "--memory-dir", d,
                                     "--apply", "--now", "2026-07-20")
            self.assertEqual([x["id"] for x in data["actions"]["drop"]],
                             ["fake-axiom"])
            # Phase 5 Item 2: drop is a soft-delete — the non-blessed claim
            # still gets NO charter protection (it is dropped), but the drop
            # now tombstones instead of unlinking.
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                txt = f.read()
            self.assertIn("status: archived", txt)
            self.assertIn("invalid_at: 2026-07-20", txt)

    def test_blessed_seed_never_reaped_when_archived(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L1-warm", "merge-gate.md")
            _write_charter_seed(path, id="merge-gate", tier="L1",
                                status="archived")
            data = _helpers.run_json("decay.py", "--memory-dir", d,
                                     "--apply", "--now", "2026-07-20")
            self.assertEqual(data["actions"]["reaped"], [])
            self.assertTrue(os.path.exists(path))
            self.assertTrue(any("charter-guard" in w for w in data["warnings"]))

    def test_migrated_seed_is_plain_l2_keep_no_warning(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L2-cold", "profile", "merge-gate.md")
            _write_charter_seed(path, id="merge-gate", tier="L2")
            data = _helpers.run_json("decay.py", "--memory-dir", d,
                                     "--apply", "--now", "2026-07-20")
            self.assertEqual(data["actions"]["l2_keep"], 1)
            self.assertEqual([w for w in data["warnings"] if "charter" in w], [])
            self.assertTrue(os.path.exists(path))

    def test_provenance_key_round_trips_through_rewrite(self):
        # decay rewrites frontmatter on keep — the charter marker must survive
        # (it used to be silently stripped by the fixed serialize key list).
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "L0-working", "m.md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("---\nid: m\ntier: L0\nowner: Tony\n"
                        "provenance: charter\nsources: [\"[s#1]\"]\n"
                        "created: 2026-07-19\nlast_reinforced: 2026-07-19\n"
                        "reinforce_count: 1\ndecay_score: 1.0\nstatus: active\n"
                        "---\nbody\n")
            _helpers.run_json("decay.py", "--memory-dir", d,
                              "--apply", "--now", "2026-07-20")
            with open(path, encoding="utf-8") as f:
                self.assertIn("provenance: charter", f.read())


class TestFrontmatterMigration(unittest.TestCase):
    """Phase 11 Item 2: decay routes its parse/serialize through the shared
    frontmatter module while keeping its own defaults/validation/key-order.
    Round-trip must stay byte-identical; a `----` body line must NOT truncate."""

    def test_parse_serialize_round_trip_byte_identical(self):
        text = ("---\n"
                "id: pref-async-001\n"
                "tier: L0\n"
                "owner: Tony\n"
                "provenance: capture\n"
                "sources: [\"[sessA#12]\", \"[sessB#4]\"]\n"
                "created: 2026-06-20\n"
                "last_reinforced: 2026-07-01\n"
                "reinforce_count: 2\n"
                "decay_score: 0.9\n"
                "status: active\n"
                "---\n"
                "Body line one.\n\n----\n\nBody after a rule.\n")
        mem = decay.parse_frontmatter(text)
        rebuilt = decay.serialize_frontmatter(mem) + "\n" + mem["_body"]
        self.assertEqual(rebuilt, text)          # byte-identical round-trip
        # the `----` body rule did NOT truncate the frontmatter:
        self.assertEqual(mem["sources"], ['"[sessA#12]"', '"[sessB#4]"'])
        self.assertEqual(mem["status"], "active")
        self.assertIn("----", mem["_body"])

    def test_no_frontmatter_bails_to_defaults(self):
        mem = decay.parse_frontmatter("no frontmatter here\njust text\n")
        self.assertIsNone(mem["id"])
        self.assertEqual(mem["_body"], "")


class TestItem2AtomicWriteNeverZeroCopies(unittest.TestCase):
    """Phase 25 Item 2: decay's writes route through the shared
    frontmatter._atomic_write. For the tier-promotion MOVE paths (demote,
    promote) the new-path file is written COMPLETELY before the old path is
    ever unlinked — a write failure (ENOSPC, simulated here) must leave the
    OLD file exactly as it was: never zero complete copies, never a
    half-written new file masquerading as real."""

    CONTENT = ("---\nid: obs\ntier: L0\nowner: Tony\n"
              'sources: ["[s#1]"]\n'
              "created: 2026-06-01\nlast_reinforced: 2026-07-01\n"
              "reinforce_count: 2\ndecay_score: 1.0\nstatus: active\n"
              "---\nbody\n")

    def _write_l0(self, mem_dir):
        l0 = os.path.join(mem_dir, "L0-working")
        os.makedirs(l0, exist_ok=True)
        p = os.path.join(l0, "obs.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.CONTENT)
        return p

    def test_promote_write_failure_leaves_old_file_intact(self):
        import unittest.mock as mock
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            mem_dir = os.path.join(d, "memory")
            p = self._write_l0(mem_dir)
            fm = decay.parse_frontmatter(self.CONTENT)
            info = {"decay_score": 0.9}
            real_atomic = decay._atomic_write

            def boom(path, *a, **kw):
                if "L1-warm" in str(path):
                    raise OSError(28, "No space left on device")
                return real_atomic(path, *a, **kw)

            with mock.patch.object(decay, "_atomic_write", side_effect=boom):
                ok = decay.apply_action(Path(p), "promote", fm, info)
            self.assertFalse(ok)  # apply_action's except-catch reports failure
            # OLD file (L0) is COMPLETELY intact — never zero copies.
            self.assertTrue(os.path.exists(p))
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read(), self.CONTENT)
            # NEW file (L1) was never fully written — no half-written stray.
            self.assertFalse(
                os.path.exists(os.path.join(mem_dir, "L1-warm", "obs.md")))
            # no stray temp files anywhere under mem_dir
            leftovers = [f for _, _, files in os.walk(mem_dir) for f in files
                        if ".tmp" in f]
            self.assertEqual(leftovers, [])

    def test_demote_write_failure_leaves_old_file_intact(self):
        import unittest.mock as mock
        from pathlib import Path
        content = ("---\nid: obs\ntier: L1\nowner: Tony\n"
                  'sources: ["[s#1]"]\n'
                  "created: 2026-06-01\nlast_reinforced: 2026-05-01\n"
                  "reinforce_count: 1\ndecay_score: 0.1\nstatus: active\n"
                  "---\nbody\n")
        with tempfile.TemporaryDirectory() as d:
            mem_dir = os.path.join(d, "memory")
            l1 = os.path.join(mem_dir, "L1-warm")
            os.makedirs(l1, exist_ok=True)
            p = os.path.join(l1, "obs.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            fm = decay.parse_frontmatter(content)
            info = {"decay_score": 0.1}
            real_atomic = decay._atomic_write

            def boom(path, *a, **kw):
                if "L0-working" in str(path):
                    raise OSError(28, "No space left on device")
                return real_atomic(path, *a, **kw)

            with mock.patch.object(decay, "_atomic_write", side_effect=boom):
                ok = decay.apply_action(Path(p), "demote", fm, info)
            self.assertFalse(ok)
            self.assertTrue(os.path.exists(p))
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read(), content)
            self.assertFalse(
                os.path.exists(os.path.join(mem_dir, "L0-working", "obs.md")))


if __name__ == "__main__":
    unittest.main()
