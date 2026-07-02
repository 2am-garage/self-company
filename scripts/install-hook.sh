#!/usr/bin/env bash
###############################################################################
# install-hook.sh — install/uninstall/status the self-company hooks (Tom's job).
#
# Installs TWO hooks into the project's `.claude/settings.json` (the SHARED
# settings file) — NOT settings.local.json, because Claude Code's permission
# auto-writer rewrites settings.local.json and would clobber an externally-added
# hook. settings.json is stable. The merge preserves any existing settings/hooks.
#
#   Stop         -> capture-trigger.py     (CAPTURE: cheap real-time memory capture)
#   SessionStart -> notify-status.py --emit-hook  (catch-up PUSH on session entry:
#                   surfaces unattended daily-run results; pushes only when something
#                   substantive changed, then self-acks. push-only, never Discord.)
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
# Code/data separation: the hook runs the CANONICAL script, not a .company/scripts
# copy. Under a plugin, write the LITERAL ${CLAUDE_PLUGIN_ROOT}/scripts so the hook
# shell expands it at runtime (survives plugin version bumps); else snapshot the
# resolved absolute dev path. --company stays the data dir (unchanged). A1: because
# the dev path is an absolute snapshot, re-run install-hook.sh after a skill move.
if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
  HOOK_SCRIPTS='${CLAUDE_PLUGIN_ROOT}/scripts'
else
  HOOK_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
STOP_MARK="self-company-capture"
STOP_CMD="python3 \"$HOOK_SCRIPTS/capture-trigger.py\" --company \"\$CLAUDE_PROJECT_DIR/.company\""
NOTIFY_MARK="self-company-notify"
NOTIFY_CMD="python3 \"$HOOK_SCRIPTS/notify-status.py\" --company \"\$CLAUDE_PROJECT_DIR/.company\" --emit-hook"

python3 - "$CMD" "$SETTINGS" "$STOP_MARK" "$STOP_CMD" "$NOTIFY_MARK" "$NOTIFY_CMD" <<'PY'
import json, os, sys

cmd, settings = sys.argv[1], sys.argv[2]
stop_mark, stop_cmd, notify_mark, notify_cmd = sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]

# (settings event, marker, command) for every hook this skill owns
HOOKS = [
    ("Stop", stop_mark, stop_cmd),
    ("SessionStart", notify_mark, notify_cmd),
]

def load():
    try:
        with open(settings) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[install-hook] error: {settings} is not valid JSON ({e})", file=sys.stderr)
        sys.exit(1)

def is_ours(group, mark):
    return any(mark in h.get("command", "") for h in group.get("hooks", []))

def save(d):
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    with open(settings, "w") as f:
        json.dump(d, f, indent=2)
        f.write("\n")

d = load()

if cmd == "install":
    hooks = d.setdefault("hooks", {})
    for event, mark, hook_cmd in HOOKS:
        groups = hooks.setdefault(event, [])
        groups[:] = [g for g in groups if not is_ours(g, mark)]   # idempotent
        groups.append({"hooks": [{"type": "command", "command": f"{hook_cmd}  # {mark}"}]})
    save(d)
    print(f"[install-hook] installed Stop(capture) + SessionStart(notify) hooks -> {settings}")
elif cmd == "uninstall":
    hooks = d.get("hooks", {})
    removed = 0
    for event, mark, _ in HOOKS:
        groups = hooks.get(event, [])
        before = len(groups)
        groups[:] = [g for g in groups if not is_ours(g, mark)]
        removed += before - len(groups)
        if not groups:
            hooks.pop(event, None)
    if not hooks:
        d.pop("hooks", None)
    save(d)
    print(f"[install-hook] removed {removed} self-company hook(s)" if removed
          else "[install-hook] nothing to remove")
elif cmd == "status":
    hooks = d.get("hooks", {})
    for event, mark, _ in HOOKS:
        ok = any(is_ours(g, mark) for g in hooks.get(event, []))
        print(f"[install-hook] {event}: {'INSTALLED' if ok else 'not installed'}")
else:
    print("usage: install-hook.sh [install|uninstall|status] [PROJECT_DIR]", file=sys.stderr)
    sys.exit(2)
PY
