"""Shared test helpers: put scripts/ on sys.path and run scripts as subprocesses."""

import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


def run_script(name, *args):
    """Run scripts/<name> with args. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, name), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def run_json(name, *args):
    """Run a script and parse its stdout as JSON."""
    rc, out, err = run_script(name, *args)
    assert rc == 0, f"{name} exited {rc}: {err}"
    return json.loads(out)


def write_memory(path, *, id, tier="L0", sources='["[s#1]"]',
                 created="2026-06-15", last_reinforced="2026-06-15",
                 reinforce_count=1, decay_score=1.0, status="active", body="body"):
    """Write a memory markdown file with frontmatter."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = (
        "---\n"
        f"id: {id}\n"
        f"tier: {tier}\n"
        "owner: Tony\n"
        f"sources: {sources}\n"
        f"created: {created}\n"
        f"last_reinforced: {last_reinforced}\n"
        f"reinforce_count: {reinforce_count}\n"
        f"decay_score: {decay_score}\n"
        f"status: {status}\n"
        "---\n"
        f"{body}\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
