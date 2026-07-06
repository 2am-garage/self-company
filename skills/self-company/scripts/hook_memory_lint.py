#!/usr/bin/env python3
###############################################################################
# hook_memory_lint.py — PostToolUse validator (Phase 10, Item 7).
#
# After a Write/Edit to a file under .company/memory/*.md, validate the memory
# frontmatter. If it is malformed, return corrective feedback so Claude fixes it
# BEFORE it corrupts the tiered store; otherwise pass silently.
#
# CONTRACT (PostToolUse): reads stdin JSON {tool_name, tool_input:{file_path},
#   tool_response}. To send corrective feedback: exit 0 with stdout
#   {"decision":"block","reason":"<what to fix>"} (Claude re-evaluates).
#   Valid / not-a-memory-file: exit 0, no output.
#
# FAIL-OPEN: opt-in guard first (no .company -> exit 0); ANY error -> exit 0 with
# no block. A hook bug must never block legitimate work.
###############################################################################

import json
import os
import re
import sys

MEM_MARKER = ".company/memory/"
VALID_TIERS = {"L0", "L1", "L2"}

# Reuse the SINGLE authoritative tombstone vocabulary (best-effort import with a
# verbatim fallback — same pattern as verify_memory / entropy / decay).
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from tombstone import TOMBSTONE_STATUSES
except Exception:  # pragma: no cover - fallback copy
    TOMBSTONE_STATUSES = frozenset({"archived", "defunct", "absorbed"})

# Phase 11 Item 2: the fragile frontmatter delimiter + key:value split lives in
# ONE shared module (frontmatter.py). Best-effort import + verbatim fallback,
# same pattern as the tombstone vocabulary above.
try:
    from frontmatter import parse as _fm_parse
except Exception:  # pragma: no cover - verbatim fallback (authoritative: frontmatter.py)
    def _fm_parse(text):
        lines = text.split('\n')
        if lines[0].strip() != '---':
            return {}, text
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                end = i
                break
        if end is None:
            return {}, text
        fm = {}
        for line in lines[1:end]:
            s = line.strip()
            if not s or s.startswith('#') or ':' not in s:
                continue
            k, v = s.split(':', 1)
            fm[k.strip()] = v.strip()
        return fm, '\n'.join(lines[end + 1:])

VALID_STATUSES = {"active"} | set(TOMBSTONE_STATUSES)
REQUIRED = ("id", "tier", "status", "sources")


def block(reason):
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def parse_frontmatter(text):
    """Frontmatter parse via the shared parser (Phase 11). Returns (dict, body)
    for a valid `---` block, or (None, text) if the block is missing/unterminated
    — the None sentinel the `block(...)` logic relies on to flag a frontmatter-less
    file. The shared parser collapses both no-fence and unterminated cases to
    ({}, unchanged-text); we re-derive the sentinel by checking that the body was
    NOT sliced (an empty-but-valid `---\\n---` block yields a sliced body and so
    still parses to ({}, body), keeping the old 'missing required field' path)."""
    fm, body = _fm_parse(text)
    if not fm and body == text:
        return None, text
    return fm, body


def sources_nonempty(raw):
    """True if the sources value names at least one concrete source. Accepts the
    `["[sid#line]", ...]` list form or any non-empty, non-`[]` scalar."""
    v = (raw or "").strip()
    if not v or v in ("[]", "''", '""', "null", "None"):
        return False
    if re.search(r"\[[^\]]+\]", v):  # at least one [ ... ] source token
        return True
    return v not in ("[", "]")


def main():
    # --- Opt-in guard: inert outside a self-company repo ----------------------
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if not os.path.isdir(os.path.join(project_dir, ".company")):
        return

    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # unparseable stdin -> fail open

    if not isinstance(data, dict):
        return
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return
    file_path = tool_input.get("file_path") or ""
    if not isinstance(file_path, str) or not file_path:
        return

    # Only care about markdown files under the memory store.
    norm = file_path.replace("\\", "/")
    if MEM_MARKER not in norm or not norm.endswith(".md"):
        return  # not a memory write -> no-op pass

    # Read the on-disk file (PostToolUse fires AFTER the write landed). If it is
    # not readable for any reason, fail open rather than block.
    path = file_path
    if not os.path.isabs(path):
        path = os.path.join(project_dir, path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return  # fail open

    fm, _ = parse_frontmatter(text)
    if fm is None:
        block("memory frontmatter missing or unterminated — every "
              ".company/memory/*.md file must open and close a `---` YAML block")
        return

    # Required fields present.
    missing = [k for k in REQUIRED if not str(fm.get(k, "")).strip()]
    if missing:
        block("memory frontmatter is missing required field(s): "
              + ", ".join(missing)
              + " (required: id, tier, status, sources)")
        return

    tier = fm.get("tier", "").strip()
    if tier not in VALID_TIERS:
        block("invalid tier '%s' — must be one of L0, L1, L2" % tier)
        return

    status = fm.get("status", "").strip().lower()
    if status not in VALID_STATUSES:
        block("invalid status '%s' — must be one of %s"
              % (fm.get("status", "").strip(),
                 ", ".join(sorted(VALID_STATUSES))))
        return

    if not sources_nonempty(fm.get("sources", "")):
        block("sources is empty — every memory must cite at least one source "
              "(e.g. sources: [\"[session-id#line]\"]) or provenance: charter")
        return

    # Valid -> pass silently.
    return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute fail-open backstop: never block on a hook bug.
        pass
    sys.exit(0)
