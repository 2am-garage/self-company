#!/usr/bin/env python3
"""
trigger_engine — the deterministic decision core for Trigger #3 (event-driven).

The company has three ways to start working: (1) the Chairman calls, (2) the
clock (cron), and (3) an EXTERNAL event. This module powers #3. It is a pure,
read-mostly DECISION engine — given a named trigger and a JSON payload, it
decides whether the company should fire, applying three guards. It does NOT spawn
any agent; `fire-trigger.sh` does the orchestration. That split keeps the
decision fully testable (mirrors notify-status: decide here, act in the shell).

Push-first: the producer (your training script / trading bot / CI job) calls
`fire-trigger.sh <name> <payload>` when it has a result. No polling, no daemon —
the company is dormant until fired. A cron poll adapter is OPTIONAL, only for
sources that cannot call us.

Triggers are USER-DEFINED, declarative, one file per trigger under
`org/triggers/<name>.yaml` (flat key: value; the safe YAML subset). The engine is
never edited by users — they only add/remove trigger files:

    name: training-done
    on: push                 # push | poll  (metadata; engine evaluates either way)
    condition: val_bpb < 0.99 # evaluated against the payload; blank = always
    action: Review the training result and propose the next experiment via Phoebe.
    cooldown: 30m            # guard 1: no re-fire within this window
    dedupe: true            # guard 2: skip a payload identical to the last fired
    budget: 20000           # advisory token cap handed to the spawned agent

Guards (all deterministic):
  1. cooldown  — time since last fire < cooldown            -> hold
  2. dedupe    — payload identical to the last fired payload -> hold
  3. daily cap — fires today >= MAX_FIRES_PER_DAY (breaker)  -> hold

Condition grammar (safe, no eval): comparison clauses `field OP literal`
(OP in < <= > >= == !=) joined by `and` / `or` (`or` lowest precedence). A field
resolves from the payload; literals are number / "string" / true / false / null.

Usage:
  trigger_engine.py --company DIR --trigger NAME [--payload JSON] --decide
  trigger_engine.py --company DIR --trigger NAME [--payload JSON] --record
  trigger_engine.py --company DIR --list

Pure stdlib (uses PyYAML only if present; falls back to a flat parser).
"""

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

MAX_FIRES_PER_DAY = 24          # tunable token-breaker (per trigger)
DEFAULT_BUDGET = 20000

TRIGGERS_SUBDIR = "org/triggers"
STATE_SUBDIR = "ops/triggers"   # per-trigger state: <name>.json


# ----------------------------------------------------------------------------- defs

def _flat_yaml(text):
    """Parse the flat `key: value` subset (the only shape triggers use) with no
    third-party dep. `#` comments and blank lines ignored. Values kept as str."""
    out = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip() or ":" not in line:
            continue
        key, val = line.split(":", 1)
        out[key.strip()] = val.strip()
    return out


def load_def(company, name):
    p = Path(company) / TRIGGERS_SUBDIR / f"{name}.yaml"
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    try:
        import yaml  # use it when available; never required
        d = yaml.safe_load(text) or {}
        if not isinstance(d, dict):
            d = _flat_yaml(text)
    except Exception:
        d = _flat_yaml(text)
    d.setdefault("name", name)
    return d


def list_defs(company):
    d = Path(company) / TRIGGERS_SUBDIR
    return sorted(p.stem for p in d.glob("*.yaml")) if d.exists() else []


# ----------------------------------------------------------------------------- condition

def _parse_literal(tok):
    tok = tok.strip()
    low = tok.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    if len(tok) >= 2 and tok[0] in "\"'" and tok[-1] == tok[0]:
        return tok[1:-1]
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        return tok            # bareword string


_CMP = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b,
        "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
        "<": lambda a, b: a < b, ">": lambda a, b: a > b}


def _eval_cmp(expr, payload):
    m = re.match(r"^\s*([\w.]+)\s*(<=|>=|==|!=|<|>)\s*(.+?)\s*$", expr)
    if not m:
        raise ValueError(f"bad condition clause: {expr!r}")
    field, op, lit = m.groups()
    left, right = payload.get(field), _parse_literal(lit)
    if op in ("==", "!="):
        return _CMP[op](left, right)
    # ordered comparison: coerce numerically; missing/non-numeric field -> False
    try:
        return _CMP[op](float(left), float(right))
    except (TypeError, ValueError):
        return False


def _split_top(expr, sep):
    return re.split(rf"\s+{sep}\s+", expr)


def eval_condition(expr, payload):
    expr = (expr or "").strip()
    if not expr:
        return True
    return any(
        all(_eval_cmp(clause, payload) for clause in _split_top(or_part, "and"))
        for or_part in _split_top(expr, "or")
    )


# ----------------------------------------------------------------------------- guards / state

def _parse_duration(v):
    """'30m' / '1h' / '45s' / '90' -> seconds. Bare number = seconds."""
    if v is None:
        return 0
    s = str(v).strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([smhd]?)$", s)
    if not m:
        return 0
    n = float(m.group(1))
    return int(n * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)])


def _payload_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _state_path(company, name):
    return Path(company) / STATE_SUBDIR / f"{name}.json"


def load_state(company, name):
    p = _state_path(company, name)
    if not p.exists():
        return {"last_fired": None, "last_hash": None, "fires": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"last_fired": None, "last_hash": None, "fires": {}}


def _now():
    return datetime.now().replace(microsecond=0)


def decide(company, name, payload):
    """Return a decision dict. Pure read — never mutates state."""
    d = load_def(company, name)
    if d is None:
        return {"fire": False, "reason": f"unknown trigger '{name}'", "trigger": name}

    phash = _payload_hash(payload)
    base = {"trigger": name, "payload_hash": phash,
            "action": str(d.get("action", "")).strip(),
            "budget": int(d.get("budget", DEFAULT_BUDGET) or DEFAULT_BUDGET)}

    # condition
    try:
        if not eval_condition(d.get("condition", ""), payload):
            return {**base, "fire": False, "reason": "condition false"}
    except ValueError as e:
        return {**base, "fire": False, "reason": f"bad condition: {e}"}

    st = load_state(company, name)
    now = _now()

    # guard 1: cooldown
    cd = _parse_duration(d.get("cooldown", 0))
    if cd and st.get("last_fired"):
        last = datetime.fromisoformat(st["last_fired"])
        if (now - last).total_seconds() < cd:
            return {**base, "fire": False, "reason": "cooldown"}

    # guard 2: dedupe (identical payload to the last FIRED one)
    dedupe = str(d.get("dedupe", "true")).lower() not in ("false", "0", "no")
    if dedupe and st.get("last_hash") == phash:
        return {**base, "fire": False, "reason": "duplicate payload"}

    # guard 3: daily fire cap (token breaker)
    cap = int(d.get("max_fires_per_day", MAX_FIRES_PER_DAY) or MAX_FIRES_PER_DAY)
    today = now.strftime("%Y-%m-%d")
    if st.get("fires", {}).get(today, 0) >= cap:
        return {**base, "fire": False, "reason": f"daily cap reached ({cap})"}

    return {**base, "fire": True, "reason": "fire"}


def record(company, name, payload):
    """Stamp a successful fire into state (called by fire-trigger.sh after firing)."""
    st = load_state(company, name)
    now = _now()
    today = now.strftime("%Y-%m-%d")
    st["last_fired"] = now.isoformat()
    st["last_hash"] = _payload_hash(payload)
    fires = st.setdefault("fires", {})
    fires[today] = fires.get(today, 0) + 1
    # prune fire counters older than ~7 days to keep the file small
    cutoff = (now.toordinal() - 7)
    st["fires"] = {k: v for k, v in fires.items()
                   if datetime.fromisoformat(k + "T00:00:00").toordinal() >= cutoff}
    p = _state_path(company, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")
    return st


# ----------------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default=".company")
    ap.add_argument("--trigger")
    ap.add_argument("--payload", default="{}")
    ap.add_argument("--decide", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    if args.list:
        print(json.dumps({"triggers": list_defs(args.company)}))
        return 0

    if not args.trigger:
        print(json.dumps({"error": "no --trigger"}))
        return 2
    try:
        payload = json.loads(args.payload or "{}")
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        print(json.dumps({"fire": False, "reason": "payload not a JSON object",
                          "trigger": args.trigger}))
        return 0

    if args.record:
        print(json.dumps({"recorded": record(args.company, args.trigger, payload)}))
        return 0

    # default action is to decide
    print(json.dumps(decide(args.company, args.trigger, payload), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
