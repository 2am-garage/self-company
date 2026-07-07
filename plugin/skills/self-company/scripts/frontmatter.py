#!/usr/bin/env python3
"""
frontmatter — SINGLE authoritative source for the fragile markdown-frontmatter
PARSING SEAM (delimiter + key:value split + body split + source tokenization).

Before Phase 11 this seam was open-coded in TEN separate parsers (decay.py,
entropy.py, verify_memory.py, reinforce_memory.py, capture-trigger.py,
rag_index.py, hook_memory_lint.py, hook_memory_inject.py, backfill_rc.py,
list_uncategorized.py) with five incompatible shapes — and they had genuinely
drifted. In particular entropy.py gated the fences on `startswith('---')` while
the other nine used `.strip() == '---'`, so entropy alone would accept a
malformed `---xyz` opener and TRUNCATE frontmatter at any body line beginning
with `---` (e.g. a `----` markdown rule). Two scanners could therefore classify
the SAME file's active-set membership differently. This module consolidates the
seam to ONE correct implementation (the `.strip() == '---'` delimiter), imported
the same best-effort + verbatim-fallback way as tombstone.py / charter_ids.py.

SCOPE — this module does PARSE / SPLIT / SERIALIZE / TOKENIZE ONLY. It injects
NO defaults, does NO tier/status validation, does NO `defunct -> archived`
migration, and does NOT choose the sources-list-vs-raw representation. Every one
of those interpretation layers STAYS in the individual caller. The single job
here is to make the fragile string-slicing identical everywhere.

Pure stdlib. No side effects. Import once; wrap with your own layer.
"""

import re

# ---------------------------------------------------------------------------
# Source-token extractor (C2 dedupe target)
#
# Source items are the quoted strings inside a `sources: ["[s#1]", "[s#2]"]`
# list. This regex + `.findall` is the EXACT extractor that was duplicated
# verbatim in capture-trigger.py:389 and reinforce_memory.py:79 (both were
# `re.compile(r'"[^"]*"')` — byte-identical, no diff). Tokens are returned WITH
# their surrounding double quotes (e.g. '"[s#1]"'), because both call sites
# compare/append the quoted form (capture-trigger builds `f'"[{sid}#{n}]"'`
# tokens and does `tok in items`; reinforce derives session ids from the quoted
# token). NEVER reintroduce a `"..."|[...]` alternative — the bracket branch
# matched the OUTER list bracket and corrupted the merged `sources` line.
# ---------------------------------------------------------------------------
SOURCE_ITEM_RE = re.compile(r'"[^"]*"')


def tokenize_sources(raw):
    """Extract source tokens from a raw `sources:` value string.

    Returns the list of double-quoted items, quotes INCLUDED, in order of
    appearance — identical to `SOURCE_ITEM_RE.findall(raw or "")`, which is
    exactly what capture-trigger.py and reinforce_memory.py did inline.

    >>> tokenize_sources('["[a#1]", "charter:x"]')
    ['"[a#1]"', '"charter:x"']
    >>> tokenize_sources('')
    []
    >>> tokenize_sources(None)
    []
    """
    return SOURCE_ITEM_RE.findall(raw or "")


def split(text):
    """Split `text` into (raw_fm_lines, body).

    A frontmatter block is delimited by a line whose `.strip() == '---'` — the
    CORRECT delimiter shared by nine of the ten legacy parsers. A body line such
    as `----` is NOT a fence (it does not `.strip()` to `'---'`), so it never
    truncates the block. (This is precisely the bug the entropy `startswith`
    check had.)

    Rules:
      * The OPENING fence must be the FIRST line (legacy contract:
        `lines[0].strip() == '---'`, no leading-blank skip). If the first line
        does not `.strip()` to `'---'` — including a file that starts with blank
        line(s) — there is no frontmatter -> return `([], text)` with the
        original text intact. (All ten legacy parsers rejected leading-blank
        files uniformly; this preserves that.)
      * The CLOSING fence is the first subsequent line that `.strip() == '---'`.
      * Missing closing fence -> `([], text)`. This replicates decay.py's
        behavior: an unterminated `---` block is NOT treated as frontmatter
        (decay bails out, parses no fields). The rest is returned as `body` so
        no text is lost (matches verify_memory / hook_memory_lint's `(None,
        text)` no-frontmatter sentinel too).
      * Empty / whitespace-only text -> `([], text)`.

    Returns:
      raw_fm_lines: the RAW (un-stripped) lines strictly BETWEEN the two fences.
                    `parse` strips them; `split` leaves them verbatim.
      body:         everything AFTER the closing fence, `'\\n'`-joined. On the
                    no-frontmatter paths it is the untouched original `text`.

    CRLF: lines are split on `'\\n'`, so a `'---\\r'` fence still `.strip()`s to
    `'---'` (detected); `'\\r'` survives inside `body` and inside `raw_fm_lines`
    until `parse` strips it. A leading blank line means the opening fence is not
    line 0, so the file is treated as having no frontmatter (matching legacy);
    real memory files never have them.
    """
    lines = text.split('\n')

    # The opening fence must be the FIRST line (legacy `lines[0].strip()=='---'`);
    # a file that starts with blank line(s) has no frontmatter.
    if lines[0].strip() != '---':
        return [], text

    # Locate the closing fence.
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            raw_fm_lines = lines[1:i]
            body = '\n'.join(lines[i + 1:])
            return raw_fm_lines, body

    # Opening fence but no closing fence -> not valid frontmatter.
    return [], text


def parse(text):
    """Parse `text` into (fm_dict, body).

    `fm_dict` maps each `key: value` line in the frontmatter block to its RAW
    string value. The key is stripped; the value is the remainder of the line
    with surrounding whitespace removed and is kept AS A STRING (no coercion).
    A value containing further `:` is split on the FIRST `:` only, so
    `sources: ["a:b"]` yields `'["a:b"]'`.

    Contract (matches the nine `.strip()=='---'` legacy parsers, minus their
    per-caller layers):
      * NO defaults are injected. A field absent from the file is simply absent
        from the dict — callers apply their own defaults / validation.
      * Unknown keys are preserved verbatim.
      * Blank lines and lines whose stripped form starts with `#` are skipped.
      * A frontmatter line without a `:` is skipped (no error raised here;
        callers that tracked `_parse_errors` re-derive that themselves).
      * No frontmatter (see `split`) -> `({}, text)`.

    `body` is everything after the closing fence, exactly as `split` returns it.
    """
    raw_fm_lines, body = split(text)
    fm = {}
    for line in raw_fm_lines:
        s = line.strip()
        if not s or s.startswith('#') or ':' not in s:
            continue
        key, val = s.split(':', 1)
        fm[key.strip()] = val.strip()
    return fm, body


def serialize(fm, body, order=None):
    """Inverse of `parse`: render `fm` + `body` back to a frontmatter document.

    Emits `---`, one `key: value` line per item, `---`, then `body`:

        ---
        <key>: <value>
        ...
        ---
        <body>

    Key order:
      * If `order` is given, those keys are emitted FIRST, in that order (any
        listed key absent from `fm` is skipped), then every remaining `fm` key
        in dict-insertion order.
      * If `order` is None, keys are emitted in dict-insertion order.

    Values are emitted via `str(value)`. Because `parse` stores raw strings,
    round-tripping `parse` then `serialize` (with `order = list(fm)` or None)
    reproduces a normal memory file BYTE-IDENTICALLY: the fence lines, the
    `key: value` spacing, and the trailing body (including its trailing newline)
    are all preserved. Note fences are re-emitted as plain `---` (a CRLF file's
    `\\r` on the fence line is normalised away).
    """
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
