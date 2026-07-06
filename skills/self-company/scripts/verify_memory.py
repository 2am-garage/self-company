#!/usr/bin/env python3
"""
verify_memory — deterministic VERIFY stage (Gibby's provenance gate).

The verify loop is the company's "lifeblood" but had never run: 0 memories ever
carried a `verified_date`, and the entropy KPI was blind to it. This gives VERIFY
a real, deterministic heartbeat.

For each active memory lacking `verified_date`, it traces every source of the form
`[<session>#<line>]` to an actual transcript: a `<session>.jsonl` under the
transcripts dir (default ~/.claude/projects/*/) whose line index really exists.
If at least one source traces, the memory is stamped `verified_date`/`verified_by:
Gibby`. Sources that are empty, vague, or point to a missing session/line do NOT
verify — that memory stays unverified (and entropy keeps counting it).

This is *existence-level* provenance (the cited line is real); semantic "does the
line say what the memory claims" remains a judgment task for the agent/Gibby.

Item 6 — charter/axiom class: install-seeded architectural axioms carry a
`provenance: charter` / `charter:<slug>` provenance instead of a transcript
source. VERIFY honours them as inherently valid (`verified_by: charter`) but
ONLY for the blessed seed ids (CHARTER_SEED_IDS); a non-blessed memory that
self-declares charter is flagged, never trusted.

Dry-run by default; --apply stamps the files. Pure stdlib.
"""

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

# [<session>#<line>] — session excludes [ ] # " and whitespace so the nested
# JSON-ish form ["[sess#2]"] yields session "sess", not '"[sess'.
SOURCE_RE = re.compile(r'\[([^\[\]#"\s]+)#(\d+)\]')

# --- Item 6: charter/axiom provenance class -------------------------------
# A charter memory is an install-seeded architectural AXIOM (true by
# construction), not a transcript-sourced CLAIM. Its provenance is declared as
# either a `provenance: charter` frontmatter key OR a source of the form
# `charter:<slug>`. VERIFY honours it as inherently valid and stamps it
# `verified_by: charter` — WITHOUT tracing a transcript line (there is none).
#
# ANTI-ABUSE (Chairman-approved): a memory may be honoured as charter ONLY if
# its id is in the blessed seed set below (the 8 known install seeds). A NORMAL
# captured memory that self-declares `charter:` to dodge source-tracing is NOT
# silently trusted — it is FLAGGED (reported, never stamped). This closes the
# obvious escalation: "add `provenance: charter` and skip VERIFY forever".
# The seed set lives in ONE shared place (charter_ids.py, same dir) since
# Phase 4 Item 1 — verify_memory, entropy, and decay all import it. Best-effort
# import with a verbatim fallback copy (same pattern as the policy loader) so a
# missing sibling module degrades instead of crashing the VERIFY stage.
try:
    from charter_ids import (CHARTER_SEED_IDS, CHARTER_SOURCE_RE,
                             self_declares_charter as _self_declares_charter)
except Exception:  # pragma: no cover - defensive fallback (authoritative copy: charter_ids.py)
    CHARTER_SEED_IDS = frozenset({
        "elon-as-manager",
        "org-hierarchy",
        "merge-gate",
        "repo-scoped-skill",
        "sub-agent-isolation",
        "verify-before-commit",
        "four-daily-runs",
        "minimal-permission-overhead",
    })
    CHARTER_SOURCE_RE = re.compile(r'charter:[^\s,"\'\]\[]+')

    def _self_declares_charter(fm):
        if str(fm.get("provenance", "")).strip().lower() == "charter":
            return True
        return bool(CHARTER_SOURCE_RE.search(str(fm.get("sources", "") or "")))


# Phase 6 Item 1: tombstone vocabulary (archived / defunct / absorbed) lives in
# ONE shared place (tombstone.py, same dir) so scanners can't drift. Best-effort
# import + verbatim fallback, mirroring the charter loader above.
try:
    from tombstone import TOMBSTONE_STATUSES, is_tombstoned
except Exception:  # pragma: no cover - defensive fallback (authoritative copy: tombstone.py)
    TOMBSTONE_STATUSES = frozenset({"archived", "defunct", "absorbed"})

    def is_tombstoned(fm):
        return str(fm.get("status") or "").strip().lower() in TOMBSTONE_STATUSES


# Phase 11: the fragile frontmatter PARSING SEAM (delimiter + key:value split) is
# consolidated into ONE shared module (frontmatter.py, same dir) so the legacy
# per-scanner parsers can't drift — the `.strip() == '---'` delimiter is the
# single source. Best-effort import + verbatim fallback (same pattern as
# tombstone.py / charter_ids.py). verify keeps its own `(None, text)`
# no-frontmatter sentinel and all downstream tracing/stamping logic on top.
try:
    from frontmatter import (split as _fm_split, parse as _fm_parse,
                             serialize as _fm_serialize,
                             SOURCE_ITEM_RE, tokenize_sources)
except Exception:  # pragma: no cover - defensive fallback (authoritative copy: frontmatter.py)
    import re as _fm_re
    SOURCE_ITEM_RE = _fm_re.compile(r'"[^"]*"')

    def tokenize_sources(raw):
        return SOURCE_ITEM_RE.findall(raw or "")

    def _fm_split(text):
        lines = text.split('\n')
        if lines[0].strip() != '---':
            return [], text
        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                return lines[1:i], '\n'.join(lines[i + 1:])
        return [], text

    def _fm_parse(text):
        raw_fm_lines, body = _fm_split(text)
        fm = {}
        for line in raw_fm_lines:
            s = line.strip()
            if not s or s.startswith('#') or ':' not in s:
                continue
            key, val = s.split(':', 1)
            fm[key.strip()] = val.strip()
        return fm, body

    def _fm_serialize(fm, body, order=None):
        keys = []
        if order:
            for k in order:
                if k in fm and k not in keys:
                    keys.append(k)
        for k in fm:
            if k not in keys:
                keys.append(k)
        out = ['---']
        for k in keys:
            out.append(f"{k}: {fm[k]}")
        out.append('---')
        return '\n'.join(out) + '\n' + body


def is_charter_claim(fm):
    """True if the frontmatter SELF-DECLARES charter provenance (via
    `provenance: charter` or a `charter:<slug>` source). This says nothing about
    whether the claim is trustworthy — that is the blessed-set check below."""
    return _self_declares_charter(fm)


def is_blessed_charter(mem_id):
    """A charter claim is honoured only for the blessed install-seed ids."""
    return mem_id in CHARTER_SEED_IDS


def parse_frontmatter(text):
    # Phase 11: split/parse via the shared module (`.strip()=='---'` delimiter).
    # verify's historical contract returns a `(None, text)` sentinel when there
    # is no frontmatter block; the shared parse returns `({}, text)` for that
    # case (an empty, falsy dict). Every caller gates on `not fm` and discards
    # body on that path, so mapping the empty parse back to `(None, text)` is
    # behaviour-identical.
    fm, body = _fm_parse(text)
    if not fm:
        return None, text
    return fm, body


def parse_sources(raw):
    """raw like: ["[s#1]", "[s#2]"] -> list of (session, line:int)."""
    out = []
    for m in SOURCE_RE.finditer(raw or ""):
        out.append((m.group(1), int(m.group(2))))
    return out


def build_session_index(transcripts_dir):
    """Map session-id -> transcript path (file stem = session id)."""
    idx = {}
    base = Path(os.path.expanduser(transcripts_dir))
    if not base.exists():
        return idx
    for p in base.rglob("*.jsonl"):
        idx.setdefault(p.stem, p)
    return idx


_line_counts = {}


def transcript_has_line(path, line_no):
    n = _line_counts.get(path)
    if n is None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                n = sum(1 for _ in f)
        except OSError:
            n = 0
        _line_counts[path] = n
    return 0 <= line_no < n


def source_traces(session, line_no, session_index):
    path = session_index.get(session)
    return bool(path) and transcript_has_line(path, line_no)


def verify_dir(memory_dir, transcripts_dir, today, apply):
    session_index = build_session_index(transcripts_dir)
    report = {
        "now": today, "memory_dir": str(memory_dir), "applied": apply,
        "verified": [], "already_verified": 0, "unverifiable": [], "scanned": 0,
        # Item 6: charter/axiom source class.
        "charter_verified": [],   # blessed charter seeds stamped verified_by: charter
        "flagged_charter": [],    # NON-blessed memories self-declaring charter (suspicious)
    }
    mem_root = Path(memory_dir)
    if not mem_root.exists():
        return report
    for path in sorted(mem_root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = parse_frontmatter(text)
        if not fm or not fm.get("id"):
            continue
        if is_tombstoned(fm):  # tombstones: archived / defunct (alias) / absorbed
            continue
        report["scanned"] += 1
        if fm.get("verified_date"):
            report["already_verified"] += 1
            continue
        # Item 6: charter/axiom class is inherently valid — but only for the
        # blessed install seeds. A non-blessed memory self-declaring charter is
        # an abuse attempt: flag it, never stamp it (it is NOT trusted).
        if is_charter_claim(fm):
            if is_blessed_charter(fm["id"]):
                report["charter_verified"].append(fm["id"])
                if apply:
                    _stamp(path, text, today, by="charter")
            else:
                report["flagged_charter"].append(fm["id"])
            continue
        srcs = parse_sources(fm.get("sources", ""))
        traced = any(source_traces(s, n, session_index) for s, n in srcs)
        if not traced:
            report["unverifiable"].append(fm["id"])
            continue
        report["verified"].append(fm["id"])
        if apply:
            _stamp(path, text, today)
    return report


def _stamp(path, text, today, by="Gibby"):
    """Insert verified_date/verified_by before the closing frontmatter ---."""
    lines = text.split("\n")
    # find closing --- (second one)
    fences = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
    if len(fences) < 2:
        return
    close = fences[1]
    inject = [f"verified_date: {today}", f"verified_by: {by}"]
    new = lines[:close] + inject + lines[close:]
    path.write_text("\n".join(new), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic VERIFY: stamp memories whose sources trace to a real transcript line.")
    ap.add_argument("--memory-dir", default=".company/memory")
    ap.add_argument("--transcripts-dir", default="~/.claude/projects")
    ap.add_argument("--now", default=None)
    ap.add_argument("--apply", action="store_true", help="Write verified_date (default: dry-run).")
    args = ap.parse_args(argv)
    today = args.now or date.today().isoformat()
    report = verify_dir(Path(args.memory_dir), args.transcripts_dir, today, args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
