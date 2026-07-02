# Memory Tiers — L0 / L1 / L2

> Design source: Self-Company Design §4 + Build Manifest §1 / §3
>
> Memory doesn't rely on hard rules to decide "what's worth keeping." Instead, we use a tiered + decay approach: capture cheaply and abundantly, let unreinforced memories decay away naturally, and only true signal settles.

---

## 1. Three-Tier Definition

| Tier | Directory | Characteristics | Lifespan | Purpose |
|---|---|---|---|---|
| **L0 Working** | `.company/memory/L0-working/` | This-session lightweight capture; unverified, draft | Short (decays and clears after 2 weeks with no reinforce) | CAPTURE phase quick observations, not yet confirmed as important |
| **L1 Warm** | `.company/memory/L1-warm/` | Project-level, weeks-scale; promoted only after re-observation or Chairman confirmation | Medium (weeks to months, depending on reinforce count) | Work-related, repeats but not yet stable enough to be a core trait |
| **L2 Cold** | `.company/memory/L2-cold/{profile,preferences,projects}/` | Permanent — stable traits, confirmed preferences, identity attributes | Permanent (does not decay) | Long-term patterns: working style, confirmed preferences, personality, identity; changes only via "contradiction update" channel |

### L0 Working Detail

- **When created**: CAPTURE phase observes conversation, extracts segments "potentially relevant to Chairman" and writes as markdown.
- **Quality control**: Don't over-filter — capture cheaply and abundantly, noise self-clears via decay. **But must have sources** (point back to conversation location); if no source, discard immediately.
- **Who can touch it**: CAPTURE (draft) → ORGANIZE (check placement) → WRITE (polish) → VERIFY (confirm sources).
- **Decay**: If unreinforced (no re-observation or confirmation), decay_score drops below `L0_DROP_THRESHOLD = 0.25`, delete file permanently.

### L1 Warm Detail

- **When promoted**: L0 is **re-observed** or **confirmed by Chairman**, reinforce_count reaches `L0_TO_L1_RC = 2` → promote to L1.
- **Characteristics**: Repeats, confirmed as "worth remembering"; but not yet stable enough to be "part of Chairman's core traits." Example: current project preference, short-term work habit.
- **Decay**: If re-reinforced, stays alive; if no reinforce, decay_score < `L1_ARCHIVE_THRESHOLD = 0.15` → **demote or archive**:
  - If `reinforce_count ≤ L1_DEMOTE_RC = 2`: demote back to L0 (awaiting next natural death). L1 memory just promoted (rc=2) that goes cold gets dropped to L0.
  - If `reinforce_count > L1_DEMOTE_RC` (i.e., rc ≥ 3): mark `status: archived`, preserve as history, no longer decay but stays inactive.

### L2 Cold Detail

- **When promoted**: L1 memory re-reinforced until reinforce_count reaches `L1_TO_L2_RC = 4` → promote to L2.
- **Characteristics**: Verified many times, extremely stable Chairman traits: working style, work preferences, identity, completed important projects, ingrained habits.
- **Subdirectory classification** (Tony lands during WRITE):
  - `profile/` — identity, background, personality, strengths.
  - `preferences/` — working style, likes, operational principles.
  - `projects/` — ongoing or completed, context worth preserving.
- **Decay & update**:
  - **Does not decay** — decay.py still computes decay_score (for monitoring), but does not delete or demote based on it.
  - **Accepts contradiction update** — if L2 contradicts other memories, don't delete the old; instead, use "update" mode: rewrite body, add sources, keep original background.

---

## 2. Consolidation — Promotion Rules

Promotion demonstrates reinforcement: when a memory is re-observed or confirmed, it doesn't just stay alive, it moves up in quality.

### Promotion Thresholds

```
L0 → L1:  reinforce_count reaches 2 (re-observed or confirmed a 2nd time)
L1 → L2:  reinforce_count reaches 4 (re-observed or confirmed a 4th time, well-verified)
```

| Tier Transition | Trigger | Executor | When |
|---|---|---|---|
| L0 → L1 | `reinforce_count ≥ L0_TO_L1_RC (=2)` | Tony (WRITE) | On 2nd reinforce, move file to `L1-warm/` + set `tier: L1` |
| L1 → L2 | `reinforce_count ≥ L1_TO_L2_RC (=4)` | Tony (WRITE) | On 4th reinforce, move file to `L2-cold/<category>/` + set `tier: L2` |

### Reinforcement Mechanism

Each time a memory is **re-observed or confirmed**:
- `reinforce_count` increments by 1.
- `last_reinforced` updates to today (ISO date).
- decay_score recalculates (see formula below).
- If promotion threshold is met, WRITE syncs file move + tier update.

**Who triggers reinforce:**
- ORGANIZE phase compares with existing memories; if judged "re-observation of existing memory" (not new), mark `action: update/reinforce`, hand to WRITE.
- VERIFY phase, if sources confirmed, also counts as one reinforce (confirmation is important).

---

## 3. Decay — Decay Rules

Memories age: the longer unreinforced, the more they fade. But **the more reinforced, the more resilient** (true signal settles).

### Decay Formula

```
age_days       = now - last_reinforced       # in days, float (hour/24)
half_life(rc)  = HL_BASE · (1 + HL_GROWTH · (rc - 1))
decay_score    = 0.5 ** (age_days / half_life(rc))
```

Where:
- `HL_BASE = 7.0` (days): half-life when rc=1, new memory loses half strength in a week without reinforce.
- `HL_GROWTH = 0.5`: each additional reinforce extends half-life by 50% of HL_BASE.
- `decay_score` starts at 1.0 (fresh) and approaches 0 (complete forgetting), no lower bound.

### Half-Life Intuition

| rc | half_life(days) | Meaning |
|---|---|---|
| 1 | 7.0 | New memory, halves in a week |
| 2 | 10.5 | Confirmed once, halves in 10.5 days |
| 3 | 14.0 | Confirmed twice, halves in two weeks |
| 5 | 21.0 | Confirmed multiple times, halves in three weeks |

> More confirmations → slower decay → aligns with "true signal settles."

### Decay Thresholds and Actions

```
if decay_score < threshold (by tier):
    L0 → delete (discard immediately, no trace)
    L1 → demote or archive (depends on reinforce_count)
    L2 → no action (never decays; accepts contradiction update)
```

#### L0 Decay Threshold

| Threshold | Default | Corresponds to age(rc=1, HL=7d) | Action |
|---|---|---|---|
| `L0_DROP_THRESHOLD` | **0.25** | ~14 days no reinforce | **Delete** (executed by `decay.py --apply`) |

> 14 days unreinforced, new memory decays to 1/4 strength → discard. Cheap enough, noise self-clears.

#### L1 Decay Threshold

| Threshold | Default | Corresponds to age(rc=2, HL≈10.5d) | Decision |
|---|---|---|---|
| `L1_ARCHIVE_THRESHOLD` | **0.15** | ~19 days no reinforce | **Demote or archive** |
| `L1_DEMOTE_RC` | **2** | reinforce_count check | Condition |

**Demotion logic**:
- If `decay_score < L1_ARCHIVE_THRESHOLD` and `reinforce_count ≤ L1_DEMOTE_RC`: → **demote to L0** (executed by `decay.py --apply`), awaiting next decay round's natural death.
- If `decay_score < L1_ARCHIVE_THRESHOLD` and `reinforce_count > L1_DEMOTE_RC`: → **mark `status: archived`**, preserve file as history, stop decay evaluation.

> Logic: memory just promoted to L1 (rc=2) that goes cold hasn't settled enough, demote to L0 for next round; if confirmed many times (rc≥3), even if cold, deserves archival for reference.
>
> **Why `L1_DEMOTE_RC = 2` not 1**: L0→L1 threshold is `L0_TO_L1_RC = 2`, so normally promoted L1 memory has reinforce_count minimum 2. If demote threshold is 1, "demote to L0" path never happens (rc always ≥2), L1 decay only has archive. Set to 2 so L1 decay with rc=2 correctly demotes to L0.

#### L2 Decay Threshold

**L2 does not decay** — `decay.py` for L2 only computes decay_score (for monitoring/dashboard), **does not delete or demote based on it**.

> L2 is verified-stable trait, won't be forgotten just because "not mentioned recently." Trust that consolidation already filtered thoroughly.

---

## 4. Decay and Promotion Interaction

- **Promotion protection**: After promoting memory to L1/L2, reinforce_count increases, lengthens half-life → decay slows. Positive feedback for promotion.
- **Cold demotion**: L1 memory ignored long-term keeps reinforce_count but age_days accumulate → eventually decays+demotes. Normal flow.
- **L2 protection**: Hard to enter L2 (need rc≥4), but once in, permanently "protected" — decay formula stops applying. In other words, consolidation already ensures only true signal reaches L2.

---

## 5. Alignment with decay.py / entropy.py

All constants and formulas in this doc **must align with these two files**:

### decay.py (deterministic decay calculation)
- Reads (or defaults):
  ```python
  HL_BASE = 7.0
  HL_GROWTH = 0.5
  L0_DROP_THRESHOLD = 0.25
  L1_ARCHIVE_THRESHOLD = 0.15
  L1_DEMOTE_RC = 2
  L0_TO_L1_RC = 2
  L1_TO_L2_RC = 4
  ```
- Executes: **delete** (L0), **demote** (L1→L0), **archive** (L1→archived); does not auto-promote (promotion by Tony in WRITE).

### entropy.py (entropy measurement)
- Reuses decay.py decay_score logic.
- `stale_rate` dimension uses thresholds above to determine "stale" (% of memories with decay_score below tier threshold).
- L2 not counted as stale (because L2 never decays).

### org/policy.md (single source of truth)
- New §7 "Memory Pipeline Parameters (tunable)" declares all constants above.
- decay.py / entropy.py prefer reading from policy.md, defaults if not found.
- memory-tiers.md and pipeline.md reference this section.

---

## 6. Practical Workflow Overview

### ID Naming Convention (slug family detection basis)

Memory `id` determines "same slug family" to detect contradictions (entropy.py). Naming rules:

- Format: `{topic}-{...}-{seq}`, topic is subject slug (lowercase+hyphen), e.g., `pref-async-001`, `pref-sync-001`.
- **topic must be the first segment**; opposite preferences for same topic (e.g., `pref-async` vs `pref-sync`) get grouped as same family for contradiction detection.
- **if tier prefix (`l0-`/`l1-`/`l2-`) is in id, must come before topic**, e.g., `l1-pref-sync-001`. entropy.py skips leading tier prefix then extracts topic, so `l0-*` don't get false-grouped as same family.
- Pure-digit id (e.g., `l0-001`) has no topic token, doesn't participate in slug family matching (avoid false positives).

> Design intent: `pref-async-001` vs `pref-sync-001` both have family=`pref` detected as contradiction candidates; all `l0-*` **do not** cross-trigger false positives just from shared tier prefix.


```
[CAPTURE phase]
  Cross-team note observations "potentially relevant" at end of conversation → write to L0 draft
  Must attach sources (point back to conversation)
  Initial: tier=L0, owner=Tony, reinforce_count=1, decay_score=1.0, status=active

        ↓

[ORGANIZE phase]
  Phoebe compares with existing L1/L2:
  - "Wholly new" → mark new, preserve L0
  - "Already exists" → mark update, trigger reinforce (reinforce_count++)
  - "Conflict" → mark contradiction, hand to Tony for adjudication

        ↓

[WRITE phase]
  Tony lands:
  - new → write file to L0-working/
  - update & reinforce_count hits promotion threshold → move file + change tier
  - contradiction → merge/update/drop old

        ↓

[VERIFY phase]
  Gibby trace sources one by one:
  - sources point back → Pass, record verify time
  - sources don't point back → Reject, send back to CAPTURE for re-capture (loop until clean)
  Re-capture still no source, hits limit → discard (VERIFY_MAX_RETRY=2, see policy.md §7.5)

        ↓ (daily schedule)

[DECAY phase (decay.py --apply)]
  Scan all files:
  - Compute decay_score
  - L0 threshold breached → delete
  - L1 threshold breached → demote or archive
  - L2 → compute score only, no action
  - Promotion candidates (reinforce_count threshold met) listed as JSON, Tony reviews for promotion
```

---

## 7. FAQ

### Q: How long until L0 memory decays away?
**A**: If unreinforced, about 2–3 weeks. decay_score = 0.5^(14/7) = 0.25, triggers `L0_DROP_THRESHOLD`.

### Q: I want to keep an L0 observation, but Chairman hasn't mentioned it again. What do I do?
**A**: Decay is designed for "not mentioned, forgotten." If truly important, either find a chance to get Chairman's confirmation once (trigger reinforce) or make it an L1/L2 memory. Don't manually maintain in L0.

### Q: Where's the line between L1 and L2?
**A**: reinforce_count. L0→L1 needs 2, L1→L2 needs 4. After 4 confirmations, it's "stable trait" for L2.

### Q: Is an L2 memory outdated, should I discard it?
**A**: L2 never auto-decays, so won't be discarded. If found to contradict (e.g., once said liked X, now hates X), use "contradiction update": rewrite body + add sources, change last_reinforced, keep old record.

### Q: When does decay.py run?
**A**: By design, daily schedule 02:00 (CronCreate/schedule mechanism, needs Chairman approval). Dry-run by default; `--apply` modifies files.

---

**Version**: v2 (memory pipeline)  
**Last updated**: 2026-06-24  
**Reference**: Design §4 / Manifest §1.1–1.3 / scripts/decay.py
