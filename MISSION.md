# Mission — this repo is run BY self-company, to improve self-company

This repository is not a normal project that a human edits with an assistant on
the side. Its operating model is recursive:

> **This repo is run by the self-company. The company's standing task is to
> improve the self-company (this very skill).**

So the work here is the self-upgrade loop, made literal:

```
Tony  proposes an improvement   ──►  Elon  decides (do / defer / no)
                                        │
                                        ▼
                             Phoebe  plans + dispatches
                                        │
                                        ▼
                        Bob / Tom  implement   ──►  Gibby  verifies
```

## What this means in practice

- **The company does the work, not "Elon alone."** In an interactive session the
  main agent fronts as Elon, but real improvement work should be *dispatched to
  the employees* — run as actual agent processes — rather than Elon silently
  editing every file himself. `company-run.sh` is the session-facing trigger that
  starts a real company cycle (Phoebe plans → `supervisor.py` spawns the assigned
  employees live → verified). See SKILL.md "Triggers".
- **A fourth trigger source: the session itself.** Alongside (1) the Chairman
  calling, (2) the clock, and (3) external events, the interactive session can
  *fire the company into action* on an improvement task — that is what
  `company-run.sh` is.
- **Everything stays honest and visible.** `org-status.py` (snapshot) and
  `supervisor.py` (live) show which employees actually acted, so it is never
  ambiguous whether the company ran or Elon did it by hand.

## Boundaries (unchanged)

- The skeleton is only mutable in this dev repo (the `.self-company-dev` marker);
  self-improvement edits go through the normal branch → PR → merge flow.
- `.company/` runtime (memory, ledgers, live state) stays git-ignored and private.
- Commits carry no Claude attribution trailers; they are the Chairman's.
