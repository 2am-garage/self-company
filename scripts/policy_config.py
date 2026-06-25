#!/usr/bin/env python3
"""
policy_config — shared tunable-constant loader for self-company scripts.

Single source of truth resolver: reads the tunable constants declared in
`org/policy.md §7` (and §2/§8) and hands them to decay.py / entropy.py so that
editing policy.md actually changes script behaviour.

Why this module exists
----------------------
policy.md declares constants in human-readable **markdown tables**, e.g.

    | `HL_BASE` | **7.0** days | half-life when reinforce_count=1 ... | ✓ |

The earlier per-script parsers only matched a bare ``NAME = value`` form, so the
table declarations were silently ignored and the scripts always fell back to
built-in defaults — the "single source of truth" promise was hollow. This module
parses the table form (and the inline ``NAME = value`` / ``NAME: value`` form as
a fallback) robustly, and reports per-constant provenance so callers can show
whether a value came from policy or from a default.

Pure stdlib (re, pathlib). No third-party deps — matches the company's
stdlib-only constraint and ships dormant-safe.
"""

import re
from pathlib import Path

# Constants we know how to read, and the type each should be cast to.
# Names match policy.md exactly (case-sensitive for the upper-case constants;
# the weights w1..w4 are matched case-insensitively in the regex).
CONSTANT_SPECS = {
    "HL_BASE": float,
    "HL_GROWTH": float,
    "L0_DROP_THRESHOLD": float,
    "L1_ARCHIVE_THRESHOLD": float,
    "L1_DEMOTE_RC": int,
    "L0_TO_L1_RC": int,
    "L1_TO_L2_RC": int,
    "w1": float,
    "w2": float,
    "w3": float,
    "w4": float,
    "DUP_JACCARD": float,
    "VERIFY_MAX_RETRY": int,
    "RAG_ENABLE_THRESHOLD": int,
}

# Candidate numeric token. We deliberately capture an optional sign and any
# thousands commas so they can be REJECTED in _clean() — silently dropping a '-'
# or a ',' would turn "-5.0" into 5.0 or "20,000" into 20, i.e. a
# plausible-but-wrong value with no signal. Our tunables are all plain
# non-negative numbers, so an ambiguous token means "not declared" -> default.
_TOKEN = r"([-+]?[0-9][0-9,]*(?:\.[0-9]+)?)"


def _clean(raw):
    """Normalize a captured token; return None for ambiguous/unsupported forms."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if "," in raw:          # thousands separators are ambiguous here
        return None
    if raw[0] in "+-":      # tunables are non-negative magnitudes
        return None
    return raw


def _extract(text, name):
    """
    Return the first valid value declared for `name`, or None.

    Resolution order (policy §7 tables are authoritative):
      1. Markdown table row where `name` sits in the FIRST cell and the value is
         the first number in the NEXT cell:  | `NAME` | **7.0** days | ... |
         Anchoring `name` to the first cell prevents a description column that
         merely mentions another constant from being misread.
      2. Inline declaration anywhere:  NAME = 7.0  /  NAME: 7.0  (bold allowed).

    A malformed value (negative, comma-grouped) is rejected -> None -> the caller
    falls back to its built-in default (and reports the fallback).
    """
    esc = re.escape(name)
    # (?<![\w]) / (?![\w]) keep `w1` from matching inside `w10`, and stop
    # `L0_TO_L1_RC` from being confused with `L1_TO_L2_RC`.
    boundary = r"(?<![\w])" + esc + r"(?![\w])"

    # 1) table row: name in first cell, number in the following cell.
    #    [^|\n]* stays inside the first cell; [^0-9\n+\-]* skips "**"/spaces but
    #    NOT a sign, so a leading '-' is captured (and then rejected by _clean).
    table = re.compile(
        r"^\|[^|\n]*" + boundary + r"[^|\n]*\|[^0-9\n+\-]*" + _TOKEN,
        re.MULTILINE | re.IGNORECASE,
    )
    m = table.search(text)
    if m:
        return _clean(m.group(1))

    # 2) inline fallback: NAME = value or NAME: value (optional ** bold).
    inline = re.compile(boundary + r"\s*[=:]\s*\*{0,2}\s*" + _TOKEN, re.IGNORECASE)
    m = inline.search(text)
    if m:
        return _clean(m.group(1))

    return None


def load_policy_constants(policy_path):
    """
    Parse known tunable constants from a policy.md file.

    Returns a dict {NAME: casted_value} containing ONLY the constants that were
    found and parsed. Missing file or unreadable file -> {} (callers fall back
    to their built-in defaults). Never raises.
    """
    if not policy_path:
        return {}
    p = Path(policy_path)
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return {}

    out = {}
    for name, caster in CONSTANT_SPECS.items():
        raw = _extract(text, name)
        if raw is None:
            continue
        try:
            # int("2.0") raises; go via float first so "**2**" and "2.0" both work.
            out[name] = int(float(raw)) if caster is int else float(raw)
        except (ValueError, TypeError):
            pass
    return out


def resolve(defaults, policy_path):
    """
    Merge parsed policy values over `defaults`.

    `defaults` is a dict {NAME: default_value}. Returns (values, sources) where
    values[NAME] is the effective value and sources[NAME] is "policy" (read from
    the file) or "default" (file absent / constant not declared / parse failed).

    Only names present in `defaults` are returned, so each caller declares
    exactly the constants it consumes.
    """
    parsed = load_policy_constants(policy_path)
    values, sources = {}, {}
    for name, dv in defaults.items():
        if name in parsed:
            values[name] = parsed[name]
            sources[name] = "policy"
        else:
            values[name] = dv
            sources[name] = "default"
    return values, sources
