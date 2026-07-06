#!/usr/bin/env bash
###############################################################################
# install-hook.sh — DEPRECATED since v0.2.0 (hooks are plugin-native).
#
# The self-company hooks are now declared ONCE in the plugin's `hooks/hooks.json`
# (at the plugin root; commands use `${CLAUDE_PLUGIN_ROOT}`), so Claude Code loads
# them automatically when the plugin is installed — no per-repo settings.json edit.
# Plugin hooks MERGE with settings.json hooks, so a LEGACY install-hook entry would
# make Stop(capture) / SessionStart(notify) DOUBLE-FIRE. Therefore:
#
#   install    -> NO-OP. Prints that hooks are plugin-native; nothing to install.
#   uninstall  -> removes any LEGACY self-company hook entries from
#                 `.claude/settings.json` (so existing installs stop double-firing).
#   status     -> reports "plugin-native" + whether legacy entries still linger.
#
# The full 7-hook set is documented in references/operations.md and SKILL.md.
# The uninstall path preserves the original marker-based settings.json editing.
#
# Usage:
#   install-hook.sh install   [PROJECT_DIR]   # no-op (see hooks/hooks.json)
#   install-hook.sh uninstall [PROJECT_DIR]   # clean legacy double-fire entries
#   install-hook.sh status    [PROJECT_DIR]
###############################################################################
set -uo pipefail

CMD="${1:-status}"
PROJECT_DIR="${2:-${SELF_COMPANY_PROJECT_DIR:-$PWD}}"
PROJECT_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd || echo "$PROJECT_DIR")"
SETTINGS="$PROJECT_DIR/.claude/settings.json"

# Legacy markers this skill ever wrote into settings.json (uninstall targets these).
STOP_MARK="self-company-capture"
NOTIFY_MARK="self-company-notify"

if [[ "$CMD" == "install" ]]; then
  echo "[install-hook] hooks are plugin-native since v0.2.0 — nothing to install (see hooks/hooks.json)"
  exit 0
fi

python3 - "$CMD" "$SETTINGS" "$STOP_MARK" "$NOTIFY_MARK" <<'PY'
import json, os, sys

cmd, settings = sys.argv[1], sys.argv[2]
stop_mark, notify_mark = sys.argv[3], sys.argv[4]

# Legacy (settings event, marker) pairs this skill used to install.
HOOKS = [
    ("Stop", stop_mark),
    ("SessionStart", notify_mark),
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

if cmd == "uninstall":
    hooks = d.get("hooks", {})
    removed = 0
    for event, mark in HOOKS:
        groups = hooks.get(event, [])
        before = len(groups)
        groups[:] = [g for g in groups if not is_ours(g, mark)]
        removed += before - len(groups)
        if not groups:
            hooks.pop(event, None)
    if not hooks:
        d.pop("hooks", None)
    # Only rewrite a settings file that actually exists (never create an empty one).
    if os.path.exists(settings):
        save(d)
    print(f"[install-hook] removed {removed} legacy self-company hook entr"
          f"{'y' if removed == 1 else 'ies'} (plugin-native since v0.2.0)"
          if removed else
          "[install-hook] no legacy self-company hook entries found — nothing to remove")
elif cmd == "status":
    hooks = d.get("hooks", {})
    legacy = [event for event, mark in HOOKS
              if any(is_ours(g, mark) for g in hooks.get(event, []))]
    print("[install-hook] hooks are plugin-native since v0.2.0 (declared in hooks/hooks.json)")
    if legacy:
        print(f"[install-hook] WARNING: legacy settings.json entries still present for "
              f"{', '.join(legacy)} — run 'install-hook.sh uninstall' to stop double-firing")
    else:
        print("[install-hook] no legacy settings.json entries — clean")
else:
    print("usage: install-hook.sh [install|uninstall|status] [PROJECT_DIR]", file=sys.stderr)
    sys.exit(2)
PY
