# Triggers (event-driven, Trigger #3)

User-defined, declarative. One `*.yaml` file per trigger (flat `key: value` — the
safe YAML subset; works with or without PyYAML). The engine (`trigger_engine.py`)
is never edited — you only add/remove files here.

Fire one from your own program (push model — no polling). Scripts are not
copied into `.company/scripts/` (code/data separation) — from inside a Claude
Code session use `${CLAUDE_PLUGIN_ROOT}`; an external, non-Claude-Code caller
should hardcode the actual absolute path to the same file once:

    ${CLAUDE_PLUGIN_ROOT}/skills/self-company/scripts/fire-trigger.sh <name> '<payload-json>'

Fields:

| key | meaning |
|---|---|
| `name` | trigger id (match the filename) |
| `on` | `push` (producer calls us) or `poll` (a cron adapter checks a source) — metadata |
| `condition` | `field OP literal` clauses joined by `and`/`or`; evaluated against the payload; blank = always fire |
| `action` | what the dispatched agent should do (routed through Phoebe) |
| `cooldown` | guard: no re-fire within this window (`30m`, `1h`, `45s`) |
| `dedupe` | guard: skip a payload identical to the last fired (default `true`) |
| `budget` | advisory token figure carried in the decision JSON (`trigger_engine.py`'s daily/weekly accounting) — since Phase 29 it is NOT echoed into the dispatched agent's prompt (a CLI worker has no usage counter to pace a token figure against); the prompt instead states the REAL wall-clock timeout (`SELF_COMPANY_TRIGGER_TIMEOUT`, default 600s) via the shared `prompt_builder.py` |
| `max_fires_per_day` | guard: hard daily cap (token breaker; default 24) |
| `source_trust` | `trusted` (Chairman-approved direct dispatch) or `untrusted` (default; fail-closed) — an untrusted payload goes through a tool-less STAGE-1 parse + schema validation before any agent ever sees it (privilege separation) |
| `require_confirm` | `true`/`false` (default `false`), applies to **any** trigger regardless of `source_trust`. **Currently means hold-for-manual, nothing more:** a qualifying event HOLDS (logged `held: require_confirm — held for manual dispatch; approval workflow planned`), consumes no state (no cap/cooldown/dedupe touched), and writes no file. There is **no auto-dispatch and no recovery flag today** — an approval-queue workflow (list/approve/deny + a real override) is planned but not yet built; to act on a held event, run the intended work by hand |

State only commits (cooldown/cap/dedupe) once an event has actually cleared
schema validation and is not `require_confirm`-held — a rejected or parked
payload consumes nothing, so a malformed producer can never burn your daily
budget.
