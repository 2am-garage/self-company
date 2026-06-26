#!/usr/bin/env bash
###############################################################################
# install-hook.sh — install/uninstall/status the CAPTURE Stop hook (Tom's job).
#
# Ships the hook-install mechanism *inside the skill*. Writes the Stop hook into
# the project's `.claude/settings.json` (the SHARED settings file) — NOT
# settings.local.json, because Claude Code's permission auto-writer rewrites
# settings.local.json and would clobber an externally-added hook. settings.json
# is stable. The merge preserves any existing settings/hooks.
#
# Usage:
#   install-hook.sh install   [PROJECT_DIR]
#   install-hook.sh uninstall [PROJECT_DIR]
#   install-hook.sh status    [PROJECT_DIR]
###############################################################################
set -uo pipefail

CMD="${1:-status}"
PROJECT_DIR="${2:-${SELF_COMPANY_PROJECT_DIR:-$PWD}}"
PROJECT_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd || echo "$PROJECT_DIR")"
SETTINGS="$PROJECT_DIR/.claude/settings.json"
MARK="self-company-capture"
HOOK_CMD='python3 "$CLAUDE_PROJECT_DIR/.company/scripts/capture-trigger.py" --company "$CLAUDE_PROJECT_DIR/.company"'

python3 - "$CMD" "$SETTINGS" "$MARK" "$HOOK_CMD" <<'PY'
import json, os, sys

cmd, settings, mark, hook_cmd = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

def load():
    try:
        with open(settings) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[install-hook] error: {settings} is not valid JSON ({e})", file=sys.stderr)
        sys.exit(1)

def is_ours(group):
    for h in group.get("hooks", []):
        if mark in h.get("command", ""):
            return True
    return False

def save(d):
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    with open(settings, "w") as f:
        json.dump(d, f, indent=2)
        f.write("\n")

d = load()
stop = d.setdefault("hooks", {}).setdefault("Stop", [])
# our hook command carries the marker as a trailing shell comment so we can find it
marked_cmd = hook_cmd + f'  # {mark}'

if cmd == "install":
    stop[:] = [g for g in stop if not is_ours(g)]            # idempotent
    stop.append({"hooks": [{"type": "command", "command": marked_cmd}]})
    save(d)
    print(f"[install-hook] installed CAPTURE Stop hook -> {settings}")
elif cmd == "uninstall":
    before = len(stop)
    stop[:] = [g for g in stop if not is_ours(g)]
    if not stop:
        d["hooks"].pop("Stop", None)
        if not d["hooks"]:
            d.pop("hooks", None)
    save(d)
    print("[install-hook] removed CAPTURE Stop hook" if before else "[install-hook] nothing to remove")
elif cmd == "status":
    installed = any(is_ours(g) for g in stop)
    print("[install-hook] INSTALLED" if installed else "[install-hook] not installed")
    sys.exit(0 if installed else 0)
else:
    print("usage: install-hook.sh [install|uninstall|status] [PROJECT_DIR]", file=sys.stderr)
    sys.exit(2)
PY
