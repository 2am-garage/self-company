# Triggers (event-driven, Trigger #3)

User-defined, declarative. One `*.yaml` file per trigger (flat `key: value` — the
safe YAML subset; works with or without PyYAML). The engine (`trigger_engine.py`)
is never edited — you only add/remove files here.

Fire one from your own program (push model — no polling):

    .company/scripts/fire-trigger.sh <name> '<payload-json>'

Fields:

| key | meaning |
|---|---|
| `name` | trigger id (match the filename) |
| `on` | `push` (producer calls us) or `poll` (a cron adapter checks a source) — metadata |
| `condition` | `field OP literal` clauses joined by `and`/`or`; evaluated against the payload; blank = always fire |
| `action` | what the dispatched agent should do (routed through Phoebe) |
| `cooldown` | guard: no re-fire within this window (`30m`, `1h`, `45s`) |
| `dedupe` | guard: skip a payload identical to the last fired (default `true`) |
| `budget` | advisory token cap handed to the agent |
| `max_fires_per_day` | guard: hard daily cap (token breaker; default 24) |
