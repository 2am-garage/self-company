# Memory Pipeline Playbook

> This is the v2 memory pipeline's hands-on playbook — a four-stage execution guide from daily observations to mature memories. Design source: self-company design §3 pipeline B.
>
> **Deterministic math is not in this file**: decay_score calculation, promotion/demotion judgment, entropy measurement are all handled by Python scripts (`scripts/decay.py` / `scripts/entropy.py`). This file only documents steps requiring "human judgment": selecting observations, deciding placement, verifying provenance.
> 
> **Trigger frequency**: CAPTURE is real-time (end of each conversation); ORGANIZE→WRITE→VERIFY can be real-time lightweight or daily/weekly batch.

---

## Stage [1] CAPTURE — Lightweight observation capture

**Owner**: cross-departmental all staff (lightweight observation)  
**Model**: Haiku (cheap; high call frequency is fine at this cost level)  
**Trigger**: end of each conversation (real-time, no token budget ceiling)  

### Input
- Full conversation content of the current session
- Summary of existing L1/L2 memories (avoid duplicate capture of the same event)

### Output
- L0 draft files (0..N entries), each containing:
  - `id`: slug (lowercase + hyphen)
  - `body`: actual observation content (1–3 sentences)
  - `sources`: precise reference to the conversation (session id + chunk)
  - frontmatter initial values: tier=L0, owner=Tony, reinforce_count=1, decay_score=1.0, status=active, created=today, last_reinforced=today

### Exact Steps

1. **Scan conversation**: find "observations worth recording about the Chairman"
   - Preferences: "Chairman mentioned liking / disliking something"
   - Habits: "Chairman tends to work / decide in a certain way"
   - Identity / Background: "Chairman mentioned past experience, identity, goals"
   - Ongoing projects: "what the Chairman is currently doing, progress"
   - Decisions: "specific decisions the Chairman made in this conversation"
   - **Style**: "Chairman's tone, priorities, discipline"

2. **Capture cheaply and abundantly** — don't over-filter
   - "Noise" at this stage will naturally decay away; no need to demand 100% accuracy in CAPTURE
   - Better to capture more; rely on later reinforcement/decay to filter

3. **Every entry must have sources** — point back to where in the conversation
   - Format: "`[session-id#chunk-n]`" or "`[s.timestamp:dialogue excerpt]`"
   - Discard observations with no sources; don't write as L0
   - sources can be multiple (same thing mentioned multiple times)

4. **Write to L0**
   - Path: `memory/L0-working/<id>.md`
   - Complete frontmatter (from design §2): id / tier / owner / sources / created / last_reinforced / reinforce_count / decay_score / status
   - Concise body (1–3 sentences), can include markdown but not deep references

5. **Produce handoff brief**
   - List: "New L0 draft list": id + one-line summary + sources origin
   - Don't hand Phoebe the entire conversation

### Example

```markdown
# Observation: Uwe prefer async Python

session-id: s.20260624-1530
sources:
  - "[s.20260624-1530#6] Chairman said: 'I prefer async/await, it's clearer than sync'"
  - "[s.20260624-1530#18] Chairman during code review confirmed: 'The async pattern is easier for me to see clearly'"

## Observation Content
The Chairman tends toward asynchronous patterns (async/await) in Python design. Mentioned in two independent scenarios, showing stable preference, not a momentary thought.
```

**Handoff brief format:**
```
New L0 drafts:
  - uwe-prefer-async: "Chairman tends toward Python async/await pattern" [s.1530#6, s.1530#18]
  - uwe-dislike-callback-hell: "Chairman avoids callback nesting style" [s.1530#22]
  ...
```

---

## Stage [2] ORGANIZE — Decide placement, judge promotion

**Owner**: Phoebe (PM, execution gateway)  
**Model**: Sonnet (requires judgment)  
**Trigger**: real-time lightweight or daily batch  

### Input
- CAPTURE handoff list (L0 draft id + sources)
- Summary of existing L1/L2 memories (structured, easy to compare)
- (during daily batch) promotion candidates JSON from decay.py

### Output
Placement decision for each draft:
```
id: uwe-prefer-async
action: new | update | contradiction | drop
target_tier: L0 | L1 | L2  (recommended tier)
target_id: <if update, points to existing target file id>
contradiction_with: <if conflict, list conflicting id pair>
notes: "brief reasoning"
```

### Exact Steps

1. **Compare each entry against existing memories**
   - Is this a **brand new observation**?
   - Or is it a **re-observation of existing memory** (same thing, Chairman or staff mentioned it again)?

2. If it's a re-observation of existing → `action: update`
   - Mark the target id
   - Hand-write note: "reinforce_count++, last_reinforced=today" (WRITE stage executed by Tony)
   - Don't edit the file yourself

3. If completely new → `action: new`
   - Recommended tier: typically initial is L0, unless the observation itself is very certain (rare)

4. If **conflicts with existing memory** → `action: contradiction`
   - Example: "uwe-prefer-async" vs "uwe-prefer-sync-clarity"
   - Mark the target id + conflicting pair
   - Note: "content contradicts, needs Tony judgment: merge / keep new / discard new keep old / L2 contradiction update"
   - Hand off to WRITE for handling

5. If observation too vague / no sources / extremely high duplication rate → `action: drop`
   - Don't write as L0

6. **Don't edit files yourself** — only produce decision list
   - Tony actually writes / updates / moves files in WRITE stage

7. (during daily batch) Reference decay.py promotion candidates
   - reinforce_count reached L0_TO_L1_RC=2 → recommend `target_tier: L1`
   - reinforce_count reached L1_TO_L2_RC=4 → recommend `target_tier: L2`
   - Don't automatically execute promotion; just list candidates

### Handoff brief → WRITE

```yaml
Decision list:
  - id: uwe-prefer-async
    action: new
    target_tier: L0
    notes: "first observation, sources sufficient"
  
  - id: uwe-dislike-callback-hell
    action: new
    target_tier: L0
    notes: "related but independent observation"
  
  - id: uwe-prefers-functional
    action: update
    target_id: uwe-functional-style
    notes: "existing memory re-confirmed, rc++, date=today"
    target_tier: L1  # promotion candidate at rc=2
  
  - id: uwe-hates-python-2
    action: contradiction
    target_id: uwe-enjoys-legacy-systems
    contradiction_with: uwe-hates-python-2
    notes: "content contradicts, needs Tony judgment: does this person actually have both (different contexts)? or is memory misreporting?"
```

---

## Stage [3] WRITE — Write to markdown

**Owner**: Tony (improvement engineer, sole memory writer)  
**Model**: Sonnet (judgment + copywriting)  
**Trigger**: after ORGANIZE decision (no budget ceiling, part of memory pipeline)  

### Input
- ORGANIZE decision list
- L0 draft body (if new)

### Output
- Written markdown memory file (path: `.company/memory/L{0,1,2}-*/<id>.md`)
- Complete frontmatter (design §2)
- Update `ops/logs/daily-<date>.md` (change log)

### Exact Steps

1. For each decision:

   **If `action: new`**
   - Write new file: `memory/L0-working/<id>.md` (new memories always start L0)
   - frontmatter:
     ```yaml
     ---
     id: uwe-prefer-async
     tier: L0
     owner: Tony
     sources: ["[s.20260624-1530#6]","[s.20260624-1530#18]"]
     created: 2026-06-24
     last_reinforced: 2026-06-24
     reinforce_count: 1
     decay_score: 1.0
     status: active
     ---
     ```
   - body: from L0 draft

   **If `action: update` (reinforce existing memory)**
   - Open existing file (`memory/L{0,1,2}*/<target_id>.md`)
   - Modify frontmatter:
     - `last_reinforced: 2026-06-24` (today)
     - `reinforce_count++` (old value + 1)
     - Append new source to `sources` array (no duplicates)
   - body: optionally supplement (usually don't change)
   - **Check promotion candidate**: if `reinforce_count` now reaches `L0_TO_L1_RC=2`, change `tier:L1`, move file to `memory/L1-warm/<id>.md`
   - If reaches `L1_TO_L2_RC=4`, change `tier:L2`, move file to `memory/L2-cold/{profile|preferences|projects}/<id>.md` (categorize by content)

   **If `action: contradiction`**
   - Compare both memories, adjudicate:
     - **Merge**: write new file absorbing old memory, old changes to status=archived
     - **Keep new**: old changes to archived
     - **Discard new keep old**: new memory simply not written
     - **L2 contradiction update**: if both conflicting entries are in L2 (stable traits), don't delete old, new file adds new source ("this trait has both opposing facets")
   - Update the contradiction side's notes to say "see contradiction record"

   **If `action: drop`**
   - Don't write file

2. **Update ops/logs**
   - File: `ops/logs/daily-YYYY-MM-DD.md`
   - Log format:
     ```markdown
     # Daily Memory Log — 2026-06-24

     ## New Memories
     - uwe-prefer-async (L0) — sources: 2/2 verified
     - uwe-dislike-callback-hell (L0)

     ## Reinforced
     - uwe-functional-style (rc: 1→2, L0→L1) — promoted

     ## Contradictions Resolved
     - uwe-hates-python-2 vs uwe-enjoys-legacy-systems
       Decision: both valid (context-dependent)
       Merged into: uwe-python-pragmatism (L2-preferences)

     ## Dropped
     - (none)
     ```

3. **Don't mark final decay_score yourself**
   - New files leave `decay_score: 1.0` (initial value)
   - decay.py will recalculate and write back during daily/weekly consolidation

4. **Produce handoff brief**

### Handoff brief → VERIFY

```yaml
Changes this round:
  - id: uwe-prefer-async
    action: new
    tier: L0
    sources: ["[s.20260624-1530#6]","[s.20260624-1530#18]"]
    status: pending_verify

  - id: uwe-functional-style
    action: reinforce(update)
    tier: L1  # promoted
    target_id: uwe-functional-style
    sources: ["[s.20260624-1530#42]"]  # new source
    status: pending_verify

  - id: uwe-python-pragmatism
    action: contradiction_resolved
    tier: L2-preferences
    notes: "merged from uwe-hates-python-2 + uwe-enjoys-legacy"
    sources: [multiple]
    status: pending_verify
```

---

## Stage [4] VERIFY — Track provenance loop (loop until clean)

**Owner**: Gibby (QA, adversarial engineer)  
**Model**: Sonnet (investigation + judgment)  
**Trigger**: after WRITE handoff; also comprehensive re-verify weekly  

### Input
- WRITE handoff list (id + sources + action)

### Output
- Per-entry **Pass** / **Reject**
- Pass records verification time + signature: verified_date / verified_by
- Reject sends back to CAPTURE, records retry count

### Exact Steps (loop)

1. **Track each source**
   - Expand each source reference
   - Cross-check against original conversation / file / session record
   - Question: does sources truly point to a real source?

2. **Adjudicate Pass / Reject**

   **Pass conditions**
   - ✅ sources precisely point to conversation chunk or file (can trace back to original text)
   - ✅ body content matches source (no distortion)

   **Reject reasons**
   - ❌ sources empty (policy.md §4.3 memory pipeline entry: sources cannot be empty, Gibby rejects all such)
   - ❌ sources point to non-existent session / conversation time point (can't trace)
   - ❌ sources vague ("earlier", "nearby conversation" — can't pinpoint)
   - ❌ body doesn't match source meaning (Tony misunderstood)
   - ❌ new finding conflicts with existing memory (see step 5 "contradiction detection" below: flag to Tony, back to WRITE)

3. **Reject action**
   - **Re-capture**: send back to [1] CAPTURE for fresh observation
   - Record retry_count++
   - If same entry Rejected `VERIFY_MAX_RETRY=2` times still no sources → **permanently discard**, log: "unverifiable, abandoned"

4. **Light strengthening** (sources exist but vague)
   - Can request Tony to strengthen sources lightly (back to WRITE, not counted as Reject)
   - Example: "sources says 's.1530#6', should clarify 's.20260624-1530#6'"

5. **Contradiction detection**
   - If during verification you find a new contradiction (e.g. sources A says "like", sources B says "dislike")
   - Flag to Tony, back to WRITE for adjudication, don't change yourself

6. **All pass**
   - When batch fully Passes → add to memory file frontmatter:
     ```yaml
     verified_date: 2026-06-24
     verified_by: Gibby
     ```
   - End loop

### Loop termination rules

- **Success**: entire batch Passes, record to ops/logs/daily-<date>.md
  ```markdown
  ## Verified by Gibby
  - uwe-prefer-async: PASS [s.20260624-1530#6, #18]
  - uwe-functional-style: PASS [s.20260624-1530#42]
  ...
  ```

- **Failure**: same entry Rejected `VERIFY_MAX_RETRY=2` times, permanently discard, log:
  ```markdown
  ## Rejected & Abandoned
  - some-obs: "sources unverifiable, re-captured 2 times still can't point to source, abandoned"
  ```

### Handoff → Report (v2 doesn't auto-generate)

- After VERIFY completes, v2 **doesn't auto-generate report**
- Results written to `ops/logs/`, for Tony's weekly entropy review
- If Gibby finds high Reject rate, log + notify Tony/Elon for diagnosis

---

## Overall flow diagram

```
┌─────────────────────────────────────────────────────────────┐
│[1] CAPTURE                                                  │
│ Cross-departmental lightweight observation → L0 draft + sources│
│ (Haiku, real-time, no budget ceiling)                       │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│[2] ORGANIZE                                                 │
│ Phoebe decides placement (new/update/contradiction/drop) +  │
│ recommends tier                                              │
│ (Sonnet, real-time lightweight or daily batch)              │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│[3] WRITE                                                    │
│ Tony writes memory markdown file, updates frontmatter        │
│ Executes promotion (move file + change tier), adjudicates   │
│ contradictions, updates ops/logs                             │
│ (Sonnet, after decision, no budget ceiling)                 │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│[4] VERIFY (loop until clean)                                │
│ Gibby tracks each source — points back to real origin?      │
│ (Sonnet, after decision, no budget ceiling)                 │
│                                                             │
│ ✅ Pass → record verified_date / verified_by                │
│ ❌ Reject → send back to [1] CAPTURE for re-capture         │
│           (≤2 retries; reach limit → permanently discard)   │
│                                                             │
│ All Pass → end loop, update ops/logs/daily-<date>.md        │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │  CLEAN ✅       │
                    │ Batch handoff    │
                    │ complete         │
                    └─────────────────┘


If [4] Reject:
  VERIFY Reject
         │
         ├─ back to [1] CAPTURE for re-capture
         │        │
         │        ▼ (re-run through [2-4])
         └───── retry_count++ 
                (max 2)
                ├─ ≤2 times: [2] → [3] → [4]
                └─ >2 times: permanently discard ✗
```

---

## Division of labor: LLM judgment vs Python calculation

| Work | Who | Tool |
|---|---|---|
| Extract observation, write sources | CAPTURE (staff + Haiku) | direct conversation observation, no script |
| Decide placement, judge promotion candidate | ORGANIZE (Phoebe + Sonnet) | playbook (this file) |
| Write markdown, execute promotion | WRITE (Tony + Sonnet) | playbook (this file) |
| Verify provenance, reject loop | VERIFY (Gibby + Sonnet) | playbook (this file) |
| **Calculate decay_score** | **decay.py** (pure Python, stdlib) | `0.5^(age_days/half_life(rc))` |
| **Judge promotion / demotion / decay thresholds** | **decay.py** (output JSON candidates) | threshold constants § 1.1–1.3 |
| **Measure entropy KPI** | **entropy.py** (pure Python, stdlib) | Jaccard / heuristic, output JSON |
| **Review entropy candidates, adjudicate contradictions** | **Tony** (Sonnet) | playbook / human judgment |

> **Strategy**: anything with deterministic math (formulas, thresholds) → Python; anything requiring semantic understanding, context judgment → instruction playbook (Sonnet). This playbook does not repeat decay calculation, does not compute decay_score itself, only documents steps at the "judgment" level.

---

## Appendix: Frontmatter quick reference

```yaml
---
id: <slug>                              # lowercase+hyphen, unique, e.g. uwe-prefer-async
tier: L0 | L1 | L2                      # tier
owner: Tony                             # always Tony (unified memory writer)
sources: [<source1>, <source2>]         # session id / reference; cannot be empty (VERIFY iron rule)
created: <YYYY-MM-DD>                   # creation date
last_reinforced: <YYYY-MM-DD>           # last reinforced date
reinforce_count: <int>                  # reinforcement count (starts 1)
decay_score: <0.0-1.0>                  # calculated by decay.py; new files leave 1.0
status: active | archived               # state
verified_date: <YYYY-MM-DD> (optional)  # date passed VERIFY
verified_by: <Gibby> (optional)         # verifier signature
---
```

---

## Terminology quick reference

| Term | Meaning |
|---|---|
| **L0 working** | This session staging, unverified or rejected re-capture observations |
| **L1 warm** | Memory promoted (rc≥2) from re-observation/confirmation; subject to decay |
| **L2 cold** | Stable traits (rc≥4); no decay, accepts contradiction updates; resides in profile/preferences/projects |
| **reinforce** | Same thing re-observed/confirmed → rc++, date update |
| **consolidation** | promotion process (L0→L1 at rc=2; L1→L2 at rc=4) |
| **decay** | memory decay; longer without reinforcement, score drops; below threshold auto-delete/demote/archive |
| **loop until clean** | VERIFY rejects → back to CAPTURE re-capture, until all Pass |
| **sources** | reference pointing to real source (session id + chunk); can't point back → reject |
| **verified** | Gibby traced back to source confirmed correct, added verified_date / verified_by |

---

Version: v2  
Last updated: 2026-06-24
