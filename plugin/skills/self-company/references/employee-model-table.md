# Per-employee model table (Phase 29 Item 1)

The Chairman's own finding (forward audit M1): "Bob=Haiku, Elon=Opus" was a fiction
the company paid Sonnet prices to maintain — `context.md`'s `model:` field was read
(`employee.py`) but never CONSUMED anywhere. Every dispatched worker ran the same
hardcoded model regardless of what its desk said. Phase 29 wires it end to end.

## Contract (Chairman, 2026-07-10 — verbatim intent)

The per-employee model is **adjustable, with a safe default fallback**:

- **Unset / blank / missing `model:`** → the employee runs on the system DEFAULT,
  **silently** (no warning). This is the tested happy path: *"if I'm not saying
  anything it runs with default."*
- **A recognized alias** (`haiku` / `sonnet` / `opus` / `fable`, case-insensitive)
  → resolves to a real model id.
- **A literal `claude-*` id** (e.g. `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`)
  → passed through verbatim. Use this to PIN an employee to an exact id instead of
  an alias that moves with the default.
- **Anything else** (multi-line prose, a YAML list, an unrecognized word, an
  injection-shaped string) → degrades to the DEFAULT **with a warning** naming the
  employee and the bad value. Never blocks a dispatch, never crashes, never leaves
  `--model` blank.

This is `Employee.resolved_model(default)` in `scripts/employee.py` — the ONE
resolution function every dispatch path goes through (`supervisor.py`'s
`Member.real_command`). No second alias table exists anywhere else.

## Alias map

| Alias | Resolves to |
|---|---|
| `haiku` | `claude-haiku-4-5` |
| `opus` | `claude-opus-4-8` |
| `fable` | `claude-fable-5` |
| `sonnet` | the CALLER's `default` (see below) — deliberately no fixed id |

`sonnet` has no fixed id of its own: it means "whatever the system default is",
which happens to also be a Sonnet-family model (`schedule_config.DEFAULT_AGENT_MODEL`,
currently `claude-sonnet-5`). This is a single-source-of-truth choice — bumping the
default constant moves every `sonnet`-aliased employee with it, with no second
place to edit. Builder note: confirm these ids against the `claude-api` skill's
"Current Models" table before changing them; do not guess model strings.

## Argv-smuggle-proofing

A resolved model is ALWAYS exactly one `--model` argv token: alias outputs are
fixed known-good strings, and a `claude-*` passthrough must match
`^claude-[A-Za-z0-9.-]+$` before being trusted. Anything that doesn't fit this
charset — embedded spaces, shell metacharacters, newlines, a value like
`sonnet --dangerously-skip-permissions` — is rejected wholesale (degrade-and-warn),
never sanitized-in-place. This closes the "context.md model: field smuggles a
second argv token into the spawn" class of attack.

## Current per-employee assignments (Layer-A data — edit `context.md`, no code change)

| Employee | `model:` | Resolves to | Why |
|---|---|---|---|
| Bob | `haiku` | `claude-haiku-4-5` | executor — cheap + fast |
| Gibby | `haiku` | `claude-haiku-4-5` | executor — cheap + fast |
| Tom | `haiku` | `claude-haiku-4-5` | executor — cheap + fast |
| Tony | `sonnet` | the DEFAULT | analyst |
| Mike | `sonnet` | the DEFAULT | analyst |
| July | `sonnet` | the DEFAULT | analyst |
| Phoebe | `claude-sonnet-4-6` | `claude-sonnet-4-6` (**pinned literal**) | Chairman-pinned — NOT bumped to the new default |
| Elon | `fable` | `claude-fable-5` | CEO — rare, high-judgment dispatches |

Phoebe is the one deliberate exception: her `context.md` names the exact
`claude-sonnet-4-6` id rather than the `sonnet` alias, so she does NOT move when
the system default is bumped. This is a Chairman decision, not a code constraint —
change her line whenever the Chairman says so.

**These are DATA.** Retuning any employee's model is a one-line `context.md` edit —
no code change, no redeploy. Both the shipped template
(`assets/company-template/org/employees/*/context.md`) and a live company's own
copy under `.company/org/employees/*/context.md` carry this field; only the
TEMPLATE copies ship with the skill (a live company's copy is the Chairman's own
data and is never force-rewritten by an upgrade).

## Validation surfaces (never blocks)

`july_audit.py` and `schedule_validator.py` both report a bad `model:` value as a
WARN finding naming the employee and the value — a *finding*, not a gate. The
dispatch path has already degraded safely by the time either tool runs; these
exist so the Chairman sees the typo instead of it silently costing nothing (and
silently fixing nothing).

## Where the model is proven, not assumed

The dispatch event log (`Supervisor._emit`) carries the model each worker actually
ran (`"model": "claude-haiku-4-5"`, etc.) and any degrade warning
(`"model_warning": "..."`) — a two-employee dispatch shows two different `--model`
values in the run record, not one.
