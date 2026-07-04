# Self-Company — System Design v0.1

> A multi-agent company-type skill that automatically learns the Chairman (user) habits, preferences, and background, and continuously fights entropy.
> After installation, creates `./company/` in the current repo, local to a single project, not shared across projects.

---

## 0. Design Philosophy

1. **Markdown is the truth; RAG is just an index** — The memory substrate is human-readable, auditable markdown; RAG/vector store layered on top only handles "find fast", and if broken, can be rebuilt.
2. **The verification loop is the lifeblood** — Drawing from George Xing's superpowers pipeline: quality comes from the verify loop, not from a stronger single agent. Every new memory must point back to a real source, or it gets rejected.
3. **Entropy is the company's KPI** — Not just "RAG can find it", but through maintenance cycles continuously clearing "stale, contradictory, duplicate" data. Measure entropy every maintenance cycle.
4. **Don't judge — rely on decay** — Don't use hard rules to decide "what's worth remembering"; capture cheaply and abundantly, unreinforced memories automatically decay away, only true signal settles.
5. **Tokens have a budget** — Not 24/7. Tiered triggers, cheap models for frequent activities, batch maintenance, per-period caps.

---

## 1. Org Structure (Agents)

| Agent | Title | Tier | Responsibility | Default Model |
|---|---|---|---|---|
| **Chairman (Uwe)** | Chairman | owner | Final decisions, taste, manual triggers | — |
| **Elon** | CEO | manager | Set direction, upgrade adjudication, lead manual deep cleanups | Opus / Sonnet |
| **Phoebe** | PM | manager (above July) | Execution gateway: intent → spec/plan, dispatch tasks, track progress, all work goes through her | Sonnet |
| **July** | HR | team lead (half a tier above the five workers) | **Tune the five workers**: personality/prompt/performance review/enable/disable | Sonnet |
| **Bob** | Build Engineer | worker | Per Phoebe's plan, **produce code / files** | Haiku → Sonnet |
| **Gibby** | QA Engineer | worker | **Red Team** — assume output is broken, rotate attack surfaces to hit Bob; 3 unbroken rounds to pass | Sonnet |
| **Tony** | Improvement Engineer | worker | Think: measure entropy, evaluate company health, memory maintenance logic (decay/dedup/contradiction), RAG strategy, **write upgrade proposals to Elon** | Sonnet |
| **Tom** | IT / Ops Engineer | worker | Execute: `./company/` skeleton, scheduling/hooks, **token breaker**, backups, file integrity, **execute Elon-approved upgrades** | Sonnet |
| **Mike** | R&D Researcher | worker | Survey the external world (literature, comparable harnesses, ecosystem); return **cited, applicability-ranked briefs** — evidence packs for specs. Tony measures inside, Mike surveys outside | Sonnet |

> RAG strategy owned by Tony; token monitoring and breaker owned by Tom (user-specified).

### Bob⚔Gibby Red/Blue Adversarial (Bob=Blue defend/build, Gibby=Red attack)

Not "build vs verify", but **red/blue adversarial hardening**: Gibby assumes output is broken, rotation-attacks attack surfaces relentlessly; when Bob is broken, he doesn't just patch the hole but **hardens into a whole class of defense + locks in regression test**. Every attack/defense move is recorded in the ledger (`ops/red-blue/ledger.md`), **defenses only grow, never shrink → system becomes monotonically more robust over time**.

**Gibby's attack surfaces (rotate each round, no repeats):**

| Attack Surface | What It Hits / Tools |
|---|---|
| Correctness | Logic errors, boundary miscalculations — pytest, run code/CLI |
| Malicious / malformed input | Null, None, oversized, injection — fuzz |
| Concurrency | race condition, order dependencies |
| Resources | exhaustion, leak, infinite loop |
| spec drift | diff against Phoebe's plan |
| Regression | re-run all old attacks from ledger |
| Code quality | linter, type checker, static analysis |
| Frontend / UI | **Playwright MCP** |
| Memory (pipeline B) | Point each memory back to real source, reject if not found |

> Stop condition: 3 consecutive **rounds** of different attack surfaces unbroken = hardened (broken = reset counter). Full spec in `references/red-blue-protocol.md`.

### Tony vs Tom Division (Think vs Do)

| | **Tony · Improvement Engineer** | **Tom · IT/Ops** |
|---|---|---|
| Essence | Diagnose and propose (think) | Execute and maintain (do) |
| Work | Measure entropy, evaluate company health, memory maintenance logic, RAG strategy | Skeleton, scheduling, token breaker, backups, integrity |
| On the company itself | See what could be better → **write upgrade proposal to Elon** | **Execute Elon-approved upgrades** |

**Company self-upgrade loop (the "self" core of self-company):**

```
Tony diagnoses (entropy high? process broken? agent underperforming? tool swap needed?)
        │ writes into upgrade proposal
        ▼
Elon adjudicates (do / don't / later)
        │ approves
        ▼
Phoebe plans dispatch (break down tasks, fill gaps, set dependencies — gap-prevention gate)
        │
        ▼
Tom executes (modify skeleton, adjust settings, add scheduling…)
```

> **Iron rule: Phoebe is the execution gateway.** Any "actual hands-on work" must go through Phoebe's dispatch planning first, ensuring no missing steps, no missed dependencies. Elon manages direction (do/don't), Phoebe manages execution (how, any gaps).

**Tony vs July boundary (both improving, different tiers):**
- **July** — Daily micro-tuning: fine-tune worker prompts/personality within existing responsibilities, no Elon approval needed.
- **Tony** — Structural overhaul: new processes, redesign, add/swap agents, introduce tools, **requires Elon sign-off**.

---

## 1b. Staff Relationship Diagram

```
                    ┌─────────────┐
                    │  Chairman   │  Uwe — owner / taste / manual triggers
                    └──────┬──────┘
                    ┌──────▼──────┐
                    │ Elon · CEO  │  direction / upgrade adjudication
                    └──────┬──────┘
                    ┌──────▼──────┐
                    │ Phoebe · PM │  execution gateway — all work goes through, dispatch, track progress
                    └──────┬──────┘
                    ┌──────▼──────┐
                    │  July · HR  │  team lead (half a tier above the five workers) — tune / performance
                    └──────┬──────┘
        ┌───────────┬──────┴─────┬─────────────┬─────────────┐
  ┌─────▼─────┐ ┌───▼─────┐ ┌───▼───────┐ ┌───▼──────┐ ┌────▼─────┐
  │ Bob·Build │⚔│Gibby ·QA│ │Tony·Improv│ │ Tom · IT │ │ Mike·R&D │
  │   build   │ │ find bugs│ │ diagnose  │ │ execute  │ │  survey  │
  │           │ │  / test  │ │ / propose │ │ / infra  │ │  outside │
  └───────────┘ └─────────┘ └────┬──────┘ └────▲─────┘ └──────────┘
                                  │ proposal    │ execute
                     Elon adjudicate ◄────┘     │
                          └──► Phoebe dispatch ────┘
```

> Work chain: `Chairman → Elon → Phoebe → July → {Bob, Gibby, Tony, Tom, Mike}`.
> Phoebe is the execution gateway (dispatch); July is the team lead of the five workers (manage people, tune), half a tier above them.

**Three main relationship lines:**

1. **Build loop (red/blue adversarial)** — Phoebe outputs spec → Bob (Blue) builds → Gibby (Red) rotates attack surfaces → breaks, Bob hardens + locks in regression → 3 unbroken rounds = hardened. See `references/red-blue-protocol.md`.
2. **People loop** — July reads each non-manager worker's performance from `ops/logs/` → tunes prompt/personality/reviews; poor performers get "suspended" (disable). July doesn't touch managers (Elon/Phoebe).
3. **Maintenance loop** — Tony looks after memory assets, Tom looks after infrastructure (including token breaker).

**"Learning Chairman's habits" placement (pending confirmation):** cross-department routine reporting — every agent notes observations about the Chairman while working, **Tony consolidates all into memory**, **Gibby verifies sources**. No new hire.

---

## 1c. Addressing Protocol (Who Chairman Talks To)

- `(Tom) I need you...` → name-prefix, route to that worker
- No `(name)` prefix → **default talk to Elon** (CEO receives, re-dispatches downstream)
- Chairman can name-prefix **anyone** (including non-managers), but usually only talks to Elon
- Replies **label the speaker**: `[Tom] Got it, I...`
- **Stickiness rule**: after name-prefix once, subsequent conversation stays with same person, until Chairman changes `(name)` or back to Elon

| Scenario | Handling |
|---|---|
| Named person out of scope (ask Tom to write code) | Tom honestly says not his job, refer Bob or report to Elon dispatch |
| Name a manager (Elon/Phoebe/July) | Direct conversation, can re-direct downstream |
| One sentence, two names `(Bob)(Gibby)` | Parallel receive, or enter adversarial loop |
| Name someone for **multi-step actual hands-on work** | Conversation can be direct, but **work itself still registers with Phoebe for planning/tracking**, avoid missing steps/dependencies |

> Conversation can reach anyone directly; but **actual hands-on work to execute always goes through Phoebe's execution gateway**.

---

## 1d. Worker Context Engineering (Anti-Entropy Core)

**Principle: each worker sees only what they need, context always concise.** One person carrying all context is the entropy explosion root cause; splitting solves it. Each worker is a fresh-context subagent call, naturally isolated, loading only their own "desk" folder `org/employees/<name>/`.

Each person's desk has four files: `persona.md` (persona), `context.md` (context spec), `scratchpad.md` (private scratchpad), `log.md` (performance).

Example `context.md` (Bob):

```yaml
---
name: Bob
role: Build Engineer
manager: Phoebe                 # dispatch source
people_lead: July               # performance tuning
model: sonnet
reads:                          # load only these, not everything
  - org/employees/bob/          #   own desk
  - <Phoebe-delivered spec/plan>    #   current task
  - <relevant code files>             #   task-related only
writes:                         # only write these
  - org/employees/bob/scratchpad.md
  - org/employees/bob/log.md
  - <files specified in plan>
tools: [Read, Edit, Write, Bash]
token_budget: <per-call cap>
handoff_to: Gibby
handoff_format: |               # handoff is concise brief only, not entire context
  which files changed, expected behavior, what Gibby should verify
---
```

> `persona.md` maintained by July; `context.md` structural changes go through Tony→Elon→Phoebe upgrade loop; `scratchpad.md` can be cleared per-task, doesn't accumulate (anti-entropy); `log.md` is the basis for July's performance reviews.

**Each worker's context slice:**

| Worker | Loads (reads) | Can't see |
|---|---|---|
| **Phoebe** | Chairman intent, memory summaries, current plans | code details, infra |
| **Bob** | Phoebe's spec, task-related code | logs, memory internals, others' work |
| **Gibby** | Bob's output, spec (check drift) | memory internals, infra |
| **Tony** | `memory/` full area, entropy/health metrics | code, infra details |
| **Tom** | `org/`, infra state, token usage | memory content, code logic |
| **July** | `ops/logs/` (performance) | code, memory content |
| **Elon** | **summaries** from each dept (not raw) | any raw details |

> Handoff passes concise brief only (echo §0 distillation), context doesn't layer-by-layer accumulate → entropy bounded.

---

## 2. Folder Structure

```
./company/
├── org/                      # company settings (not memory content)
│   ├── employees/            # each person's "desk" folder
│   │   ├── elon/
│   │   │   ├── persona.md    #   persona: role, personality, voice, responsibilities, discipline
│   │   │   ├── context.md    #   context engineering: reads/writes/tools/model/budget/handoff
│   │   │   ├── scratchpad.md #   private work scratchpad (working memory, cleared per-task)
│   │   │   └── log.md        #   activity/performance log (July reads to tune)
│   │   ├── phoebe/           #   (same structure as above)
│   │   ├── july/
│   │   ├── bob/
│   │   ├── gibby/
│   │   ├── tony/
│   │   └── tom/
│   ├── policy.md             # company charter: entropy KPI, write rules, language rules, token budget
│   └── triggers.md           # who, at what frequency/condition/tier gets triggered
│
├── memory/                   # Chairman memory — company core asset (markdown truth)
│   ├── L0-working/           # this session's working capture (will decay/promote away)
│   ├── L1-warm/              # project-level, weeks-scale; duplicate-checked, confirmed before promoting up
│   ├── L2-cold/              # permanent: stable traits, confirmed preferences, identity
│   │   ├── profile/          #   identity, background, personality
│   │   ├── preferences/      #   likes, habits, working style
│   │   └── projects/         #   ongoing items
│   └── index/                # RAG index / vector store (Tony manages, layered on top)
│
├── ops/                      # company operations trace
│   ├── logs/                 # each session: recorded what changed (traceable)
│   ├── plans/                # backlog, roadmap
│   └── schedule/             # scheduled-task status
│
└── reports/                  # for Chairman: this period's captures/cleanups, entropy change
```

Frontmatter for each memory file:
```yaml
---
id: <slug>
tier: L0 | L1 | L2
owner: Tony                                    # memories unified consolidated by Tony
sources: [<provenance: session id / conversation snippet cite>]   # Gibby verifies these
created: <ISO date>
last_reinforced: <ISO date>
reinforce_count: <int>
decay_score: <float>          # Tony calculates
status: active | archived
---
```

---

## 3. Core Processes (Two Pipelines)

The company has two pipelines, both mirror George's `brainstorm → plan → execute → review`, sharing the same verify loop spirit.

### A. Build Pipeline (When Chairman Wants Something)

```
[1] BRIEF     (Chairman → Elon → Phoebe)
      Chairman expresses intent, Elon sets direction, Phoebe outputs spec/plan
            │
[2] BUILD     (Bob=Blue, Sonnet)
      Produce code / files per plan, first version includes basic defense
            │
[3] RED/BLUE  (Gibby=Red ⚔ Bob=Blue, Sonnet)   ←── red/blue adversarial, N=3
      Each round: run regression first → rotate one attack surface
      ├─ Broken → record ledger → Bob hardens (block a whole class) + lock in regression → reset counter, next round
      └─ 3 consecutive unbroken rounds on different surfaces → hardened ✓
            │
[4] REPORT    (Phoebe / Elon)
      Report deliverables, attack/defense ledger summary, residual risks
```

> See `references/red-blue-protocol.md`. Defenses only grow, system gets more robust with each attack.

### B. Memory Pipeline (Learn Chairman, Background Routine)

```
[1] CAPTURE   (cross-dept routine observation, Haiku)
      While working, note observations about Chairman → write into L0
            │
[2] ORGANIZE  (Phoebe, Sonnet)
      Check against existing memory: new / update / conflict with old? Decide placement and tier
            │
[3] WRITE     (Tony, Sonnet)
      Actually write to markdown, update frontmatter and index
            │
[4] VERIFY    (Gibby, Sonnet)   ←── loop until clean
      Can each memory point back to a real source? Reject → send back to [1] for re-capture
            │
[5] REPORT    (Elon, as needed)
      What was captured this period, what was cleaned, entropy change
```

> Both pipelines gatekept by Gibby, people tuned continuously by July, infrastructure guarded by Tom.

---

## 4. Memory Tiers + Decay (Answer "What's Worth Remembering")

Don't use hard rules to judge; instead use auto-forgetting tiers (like LRU cache / brain memory consolidation):

**Promotion (Consolidation)**
- L0 memory **appears again** or **confirmed by Chairman** → promote to L1
- L1 re-reinforced → promote to L2
- Each reinforcement: `reinforce_count++`, `last_reinforced = now`

**Decay (Decay)** — Tony runs periodically
```
decay_score = f(now - last_reinforced, reinforce_count)
if decay_score < threshold:
    L0 → drop directly
    L1 → demote / archive
    L2 → no decay (already stable traits), but still accept contradiction detection updates
```

**Effect**: capture can be generous (cheap), noise disappears on its own, only true signal settles → entropy bounded.

---

## 5. Entropy Management (Three Dimensions)

User noted entropy exists not just in text, but also in code and chat sections. Design splits into three domains, each with countermeasures, measured by unified metrics.

| Domain | Entropy Shows As | Countermeasure | Responsible |
|---|---|---|---|
| **Memory / text** | Stale, contradictory, duplicate memories accumulate | Decay + dedup + contradiction detection | Tony |
| **Chat / context** | Session gets longer, key messages buried | Periodically distill session into structured memory, archive/summarize original chat | Tony + Phoebe |
| **Code / project** | Code drift, decision amnesia, context rot | Per-project maintain `project-state` doc (architecture/decisions/current state), periodically detect drift | Bob + Tony |

**Entropy metrics (KPI, measured each maintenance and written into report):**
```
Entropy ≈ w1·duplication_rate + w2·contradiction_score + w3·stale_rate + w4·unverified_rate
Target: entropy declines or stays flat after each maintenance
```

---

## 6. Token Management (Can't Run 24/7)

| Tactic | How |
|---|---|
| **Tiered triggers** | Real-time=cheap small work; daily/weekly=batch big work (see §7) |
| **Cheap models for frequent work** | CAPTURE uses Haiku; VERIFY / maintenance uses Sonnet; only manual deep cleanups touch Opus |
| **Batch not per-item** | Tony doesn't run per-item, accumulates to once-daily/weekly |
| **Per-period budget cap** | `policy.md` sets daily / weekly token ceiling, stop if exceeded, defer to next period |
| **Event-driven, not polling** | Triggered by conversation end / scheduling, no active polling/idle spinning |
| **Budget-aware degradation** | When budget running low, only run CAPTURE + VERIFY, skip non-essential maintenance |

---

## 7. Trigger Matrix (Frequency / Model / Tier)

| Trigger | Timing | Who Works | Action | Model |
|---|---|---|---|---|
| **Real-time** | End of each conversation | cross-dept capture → Gibby | Note observations + quick verify, write to L0 | Haiku + Sonnet |
| **Daily** | Scheduled | Tony (+ Tom guards budget) | Dedup, decay, promotion assessment | Sonnet |
| **Weekly** | Scheduled | Tony + Gibby + Phoebe + July | Full verify, rebuild RAG, produce report, measure entropy, worker performance tune | Sonnet |
| **Manual** | Chairman calls | Elon (lead) + everyone | Deep cleanup, reorganize, cross-layer review, build pipeline | Opus |

> Parallelizable: multiple agents can work on same stage; cross-stage serial (capture→organize→write→verify has dependencies). Build pipeline (Bob⚔Gibby) triggered per-project by Chairman.

---

## 8. RAG (Tony's Domain, Deployed Dormant)

**Status: Deployed (dormant; requires Ollama + LanceDB to activate)**

RAG infrastructure is now built and installed:
- **Embedding**: Ollama, model 'nomic-embed-text', called via local HTTP (http://localhost:11434/api/embeddings). Uses Python stdlib `urllib` — no 'requests' dependency.
- **Vector store**: LanceDB (embedded, serverless), index at `.company/memory/index/`. Derivative of markdown truth, always rebuildable.
- **Scripts**: `rag_index.py` (rebuild index from markdown) and `rag_query.py` (semantic query interface) installed to `.company/scripts/`.
- **Trigger**: Rebuild (a) auto when L1+L2 memory count crosses threshold, OR (b) Chairman manual order. Ships dormant.
- **Owner**: Tony builds/maintains the index; Tony + Gibby query it (Gibby for semantic dup/contradiction search during VERIFY). Others access through Tony.

**Graceful degradation**: If Ollama or LanceDB unavailable, clear actionable message to stderr (what to install/start), exit code 2. No uncaught tracebacks. Can fall back to full-text grep over `.company/memory`.

**Progressive activation**: 
- **Now (v2.5)**: Cold deployment, not active. Install scripts and docs only.
- **When Chairman orders** or L1/L2 memory crosses threshold: activate by installing Ollama + LanceDB locally, then trigger `python3 .company/scripts/rag_index.py --rebuild`.

**Role**: index, not source of truth. Can be rebuilt anytime from markdown. See [references/rag.md](../references/rag.md) for full technical reference.

---

## 9. Install Behavior

- Skill downloads to **current repo's local** (not shared across projects).
- On install, creates `./company/` skeleton + default `org/` settings in the repo.
- Doesn't touch existing `uwe-history/` (this design unrelated to it).

---

## 10. Pending Questions / Next Steps

1. **How to implement scheduling** — Does daily/weekly trigger use cron (`/schedule`) or hook? (affects "not 24/7" realization)
2. **Session boundary** — How to detect "conversation end"? Use Stop hook?
3. **Auto-push reports** — After weekly report generated, auto-notify Chairman or just leave for you to read?
4. **v1 scope** — First release only Memory/text entropy (L0/L1/L2 + decay + verify), defer Code/Chat entropy to v2?

> Recommendation for v1 focus: `capture → organize → write → verify (loop)` + L0/L1/L2 + decay + real-time/daily triggers. Code/Chat entropy and report automation deferred to later versions. RAG deployed dormant in v2.5 (see §8).
