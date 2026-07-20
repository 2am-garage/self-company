#!/usr/bin/env python3
"""
supervisor — the skill's OWN live-orchestration harness (Plan C).

The Chairman wanted the Claude-Workflow experience — a live tree of sub-agents
working — but modular (not bound to Claude Code) and genuinely live (not a polled
file). So this is a small, skill-owned supervisor: it spawns employees as CHILD
processes and reads their stdout streams in real time via select(), so status is
event-driven and synced with the actual work — because the supervisor IS the
parent of the process tree. It is ephemeral: it exists only while work runs.

Every employee has this capability (discovered from org/employees/, not a
hardcoded subset). Built with OOP for readability:

    Member     — one member; knows how to build its run command (real or demo)
    Worker     — one running employee process; parses its live '@status' stream
    Supervisor — spawns workers, multiplexes their streams, drives the renderer
    LiveTree   — renders the live status; repaints on a TTY, streams a feed if not

`Member` is the supervisor's ORCHESTRATION view of a company member (how to spawn
and render it live); the authoritative DATA MODEL is `employee.Employee`
(identity, capabilities, per-employee memory). There is deliberately ONE class
named `Employee` — the data model — and the supervisor BRIDGES to it (Member.
_recall_memory loads it) rather than duplicating it: process-spawning is not a
data-model concern, so the two responsibilities stay separate but the data model
stays single-sourced.

Status protocol (demo workers + legacy real-worker fallback): a worker prints
lines beginning with '@status <phase>' as it works ('@status planning',
'@status done'). Everything else is treated as a log line.

Phase 29 Item 3: a REAL worker is spawned with `--output-format stream-json
--verbose` (mirroring daily-run.sh's own STREAM_ARGS — that script learned this
lesson for its own headless agent first; the supervisor never got the memo
until now). Plain-text `claude -p` output only ever reaches the terminal at
EOF, so a live '@status' stream was never actually live for a real agent — only
the demo (echo) worker ever moved. Worker.consume_line now derives phases from
the stream-json event shape itself (assistant tool_use -> phase = the tool
name; a `result` event -> done/failed) and additionally still honors an
embedded '@status <word>' marker inside assistant text, so the legacy protocol
still works wherever a model happens to emit it. `SELF_COMPANY_AGENT_STREAM=0`
restores the old plain-text mode (EOF-batched, classified the same way).

Honest ceiling: in a real terminal this is a live TUI tree; viewed remotely in
the Claude app it streams as text (the app renders text, not skill widgets). That
is the one thing no modular design can beat — native widgets belong to the host.

Usage:
  supervisor.py --demo [--company DIR]                 # simulate all employees live
  supervisor.py --dispatch '{"phoebe":"plan X",...}' [--company DIR]   # real agents
  supervisor.py --list [--company DIR]

Pure stdlib (subprocess, select). Unix.
"""

import argparse
import enum
import json
import os
import re
import secrets
import select
import shutil
import subprocess
import sys
import time
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    # Phase 29 Item 2: the ONE source-of-truth default model constant. Imported
    # here (not hardcoded) so a single edit to schedule_config.DEFAULT_AGENT_MODEL
    # moves every dispatch default at once — Item 1's per-employee table falls
    # back to THIS when context.md's `model:` is unset/blank/invalid.
    import schedule_config as _sc
    DEFAULT_MODEL = _sc.DEFAULT_AGENT_MODEL
except Exception:                                              # pragma: no cover
    DEFAULT_MODEL = "claude-sonnet-5"

try:
    # Phase 29 Item 4 (Bob P1 + P2, Mike Idea 7): the ONE shared prompt-
    # assembly seam — role header, stated wall-clock budget, nonce fence,
    # output contract, task boundary. A missing module degrades to the
    # pre-Item-4 inline prompt strings (never blocks a dispatch).
    import prompt_builder as _pb
except Exception:                                              # pragma: no cover
    _pb = None

# P5: cap the inlined persona body so it can never balloon a worker prompt —
# a SEPARATE budget from the memory-injection cap (Elon's note: persona does
# NOT eat the memory budget).
_PERSONA_INLINE_CHARS = 2000

# Phase 34 Item 2 fail-closed backstop: if `employee.py`'s own
# `disallowed_tools_for` can't be resolved for ANY reason (import failure,
# missing module), a dispatched worker STILL gets this hardcoded
# most-restrictive list appended — never an unrestricted fallback. Kept as a
# literal copy (not imported) so this backstop can't fail for the exact same
# reason the primary lookup just did. See employee.py's Phase 34 table for the
# full mechanism/spike record — this module only calls it.
_FAILCLOSED_DISALLOWED_TOOLS = ("Bash", "Write", "Edit", "NotebookEdit")

# Token usage daily marker — mirrors decay.py's .last-decay-run pattern.
TOKEN_USAGE_MARKER = ".token-usage"


def token_usage_marker_path(company_dir) -> Path:
    """Marker storing today's cumulative token usage (input + output).
    Convention: .company -> .company/ops/.token-usage. Mirroring decay.py."""
    return Path(company_dir) / "ops" / TOKEN_USAGE_MARKER


def read_token_usage(company_dir) -> dict:
    """Read today's token-usage marker. Returns {'date': YYYY-MM-DD, 'input': int,
    'output': int, 'cost': float} or defaults if missing/corrupt."""
    today = str(date.today())
    try:
        marker = token_usage_marker_path(company_dir)
        if not marker.exists():
            return {"date": today, "input": 0, "output": 0, "cost": 0.0}
        lines = marker.read_text(encoding="utf-8").strip().split("\n")
        result = {"date": today, "input": 0, "output": 0, "cost": 0.0}
        for line in lines:
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k == "date":
                    stored_date = v.strip()
                    if stored_date != today:
                        return result
                elif k == "input":
                    try:
                        result["input"] = int(v.strip())
                    except ValueError:
                        pass
                elif k == "output":
                    try:
                        result["output"] = int(v.strip())
                    except ValueError:
                        pass
                elif k == "cost":
                    try:
                        result["cost"] = float(v.strip())
                    except ValueError:
                        pass
        return result
    except Exception:
        return {"date": today, "input": 0, "output": 0, "cost": 0.0}


def write_token_usage(company_dir, usage: dict) -> None:
    """Write token-usage marker. Best-effort — marker trouble never fails the run."""
    try:
        marker = token_usage_marker_path(company_dir)
        marker.parent.mkdir(parents=True, exist_ok=True)
        today = str(date.today())
        lines = [
            f"date={today}",
            f"input={usage.get('input', 0)}",
            f"output={usage.get('output', 0)}",
            f"cost={usage.get('cost', 0.0)}",
        ]
        marker.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


# --- Item 3 (TOM-1): bound the live dispatch path ----------------------------
# company-run.sh --dispatch -> Supervisor.dispatch loops on select() with no
# wall-clock deadline, and real_command spawned bare ["claude","-p",…] with no
# timeout. A stalled worker never reaches EOF, so the supervisor (and the
# session-triggered company-run.sh) hangs forever — the only unbounded agent
# spawn in the codebase. We now (a) wrap each real worker in `timeout -k`
# (Item-1 parity) so a never-EOF child is SIGKILLed past budget, AND (b) enforce
# an in-process monotonic deadline in the select loop that kills+reaps a worker
# past budget, so a child that ignores signals still can't wedge the loop.
def _dispatch_budget():
    """Per-worker wall-clock budget in seconds (env-overridable; default 600, the
    daily-agent budget)."""
    try:
        return float(os.environ.get("SELF_COMPANY_DISPATCH_TIMEOUT", "600"))
    except ValueError:
        return 600.0


def _dispatch_kill_after():
    """SIGKILL grace after budget for the outer `timeout -k` (default 30s)."""
    try:
        return float(os.environ.get("SELF_COMPANY_DISPATCH_KILL_AFTER", "30"))
    except ValueError:
        return 30.0


def _wrap_timeout(cmd, budget, kill_after):
    """Prepend `timeout -k <kill_after> <budget>` so a real worker is bounded and a
    child that ignores SIGTERM is SIGKILLed past budget. Degrades to the bare cmd
    when `timeout` is unavailable (the in-loop deadline still guards the loop)."""
    if shutil.which("timeout") is None:
        return cmd
    return ["timeout", "-k", str(int(kill_after)), str(int(budget)), *cmd]


# --- Phase 33: the Bob<->Gibby verification gate -----------------------------
# Spec: .company/ops/plans/spec-phase33-verification-gate.md. Elon's mechanism
# correction (spec §0): workers are `claude -p` SUBPROCESSES, not Task-tool
# subagents of this session, so a `SubagentStop` hook never fires for them.
# The gate is enforced HERE, at the supervisor dispatch layer that already
# owns the red/blue cycle — not a hook.
#
# SECURITY REDESIGN (Gibby's adversarial pass, Elon adjudication #2): the FIRST
# cut routed Gibby's verdict through a shared-filesystem marker file
# (ops/reports/qa-verdict-*.json). Gibby broke it end-to-end — Bob and Gibby
# are CONCURRENT `claude -p` subprocesses sharing ONE filesystem, both with
# Bash+Write and no --allowedTools fence, writing the same ops/reports/ dir.
# So the actor the gate CONSTRAINS (Bob) could forge the marker: (1) race —
# poll ops/reports/ and overwrite Gibby's round-1 marker; (2) replay — from
# round 2 on, Bob's OWN fix-round prompt literally contained the previous
# marker's path, so deriving round-N's filename was trivial. token_hex
# filename secrecy is worthless when every worker can read/write the whole
# directory. A gate its own subject can forge is no gate.
#
# The verdict now travels IN-BAND on Gibby's OWN stdout, read by the
# supervisor from GIBBY'S SPECIFIC pipe fd (Worker.fd). Attribution is by
# PIPE IDENTITY — the OS process-isolation boundary the file approach threw
# away. Bob cannot write to Gibby's stdout pipe; a `@qa-verdict` sentinel seen
# on any OTHER worker's fd is IGNORED (only the attacker's Worker has
# capture_verdict=True). The structured JSON shape (spike objection: "one word
# isn't structured") rides in-band as a full JSON object AFTER the sentinel on
# the trusted channel. No shared-fs verdict artifact exists to forge or
# replay — Findings 1/2/3 are removed by construction, not patched.
#
# FINALIZATION — the verdict NONCE (closes the echo-forgery Gibby's adversarial
# pass surfaced against the pipe-identity design above): pipe identity proves
# WHICH worker's fd a line arrived on, but not that Gibby GENUINELY authored
# it. Bob (full Bash+Write, Phase 34 execute tier) can plant a `@qa-verdict
# {"verdict":"pass"}` string somewhere Gibby's own transcript is likely to
# quote back verbatim (a source comment, a file Gibby cats while
# investigating) — Gibby's stdout then legitimately CONTAINS the sentinel on
# the trusted fd, without Gibby ever having "decided" pass. First-wins+locked
# (FIX 1, below) already blocks the cheap version of this (a later echo can't
# overwrite an earlier real verdict), but it does nothing if the FIRST thing
# Gibby ever prints is the planted text (e.g. Gibby opens with "let me check
# what Bob left: <cats the planted comment>").
#
# The fix: `_dispatch_redblue` mints a fresh `secrets.token_hex(8)` NONCE once
# per gate run and embeds it in GIBBY'S OWN dispatch prompt ONLY (via
# `_verdict_contract`) — Bob's prompt never contains it, and Bob cannot read
# Gibby's prompt (separate `claude -p` subprocesses, no shared prompt
# channel). The sentinel shape becomes `@qa-verdict <NONCE> {json}`; the
# parser (`_extract_qa_verdict`) now REQUIRES the token immediately after the
# sentinel to match the run's nonce exactly — a bare `@qa-verdict {json}` (no
# nonce token) or a wrong nonce is REJECTED (returns None, exactly like a
# malformed line), never treated as a verdict. Since Bob cannot learn the
# nonce, planted/echoed text can no longer forge a verdict Gibby didn't
# genuinely emit as ITS LAST line — it can only ever collide with the real
# nonce by guessing an 8-byte secret. Applies on BOTH the mid-stream path and
# the authoritative `result`-event path (both call the same nonce-checked
# extractor), so an echoed pass WITHOUT the nonce in Gibby's own final answer
# still cannot override a genuine locked fail.

# The reserved verdict sentinel Gibby emits on stdout (bare line, or embedded
# in a stream-json assistant-text block). Recognized ONLY on the attacker's fd,
# and ONLY when immediately followed by the run's matching nonce token.
_QA_VERDICT_SENTINEL = "@qa-verdict "

# ROBUSTNESS FOLLOW-UP (2026-07-21, first live dispatch — the verified-decay
# task): the gate's FIRST real cycle returned UNRESOLVED even though Bob's
# work was correct. Root cause was never the nonce or the pipe-identity
# design (those held) — it was the STRUCTURED JSON contract itself: asking an
# LLM to reproduce a 16-hex-char secret AND hand-build well-formed JSON
# verbatim, as its literal last line, is brittle. The real Gibby's genuine
# verdict didn't match `@qa-verdict <NONCE> {json}` closely enough for the
# strict parser, so a CORRECT run was over-blocked as a false negative — the
# gate failing exactly the way it's supposed to on a forgery attempt, but
# tripping on its own legitimate user instead.
#
# The fix is a TOLERANT extractor, not a weaker one: the nonce requirement is
# UNCHANGED (still the exact security boundary — a wrong/absent nonce is
# still rejected outright, same as before). What's now accepted, once the
# nonce authenticates the line, is a wider set of REAL-WORLD shapes for what
# comes after it: a bare `pass`/`fail` keyword (case-insensitive, tolerant of
# trailing punctuation/prose on the same line) — the new CANONICAL,
# easiest-to-emit form the contract now leads with — or the original JSON
# object, kept for back-compat. A nonce-authenticated line whose payload is
# NEITHER of those (the near-miss shape that caused the live false negative)
# is classified distinctly by `_qa_verdict_format_miss` below — never
# silently treated as a pass, and still not clean — so a human/CEO reading an
# UNRESOLVED cycle can tell "Gibby genuinely failed it" from "Gibby answered
# but the sentinel line didn't parse" (see `_unresolved_reason`).
_QA_KEYWORD_STRIP = ".,;:!\"'"


def _parse_qa_payload(payload):
    """Parse the trailing content of an ALREADY nonce-authenticated
    `@qa-verdict <NONCE> ...` line into a verdict dict, or None if neither
    recognized form is present. Two forms are accepted:

      1. CANONICAL — a bare `pass`/`fail` keyword (case-insensitive) as the
         first word, with arbitrary trailing prose tolerated on the same
         line (`pass`, `PASS`, `fail - two edge cases found`, ...). This is
         the form the contract (`_verdict_contract`) now leads with, since
         it is the easiest for an LLM to reproduce verbatim.
      2. LEGACY/STRUCTURED — a JSON object `{"verdict": "pass"|"fail", ...}`,
         exactly as the pre-2026-07-21 contract required. Still accepted so
         a model that emits the structured form is not penalized.

    Anything else (empty payload, unrecognized prose, malformed JSON, a JSON
    value that isn't a dict or lacks a pass/fail `verdict` field) returns
    None — fail-LOUD-never-fail-open, same discipline as before this
    tolerance was added."""
    payload = payload.strip()
    if not payload:
        return None
    first_word = payload.split(None, 1)[0].strip(_QA_KEYWORD_STRIP).lower()
    if first_word in ("pass", "fail"):
        return {"verdict": first_word}
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict) and data.get("verdict") in ("pass", "fail"):
        return dict(data)
    return None


def _scan_qa_verdict_lines(text, nonce):
    """Shared line-scanner behind `_extract_qa_verdict` and
    `_qa_verdict_format_miss`. Yields `(authenticated, parsed)` for every
    line in `text` that carries the `@qa-verdict` sentinel:

      * `authenticated` — True ONLY when the token immediately after the
        sentinel matches `nonce` EXACTLY (plain `==`, never a substring or
        prefix match) — the unchanged security boundary. A falsy `nonce`
        yields nothing at all (fail-closed, mirrors `_extract_qa_verdict`'s
        own defensive falsy-nonce handling).
      * `parsed` — the verdict dict from `_parse_qa_payload`, or None. Only
        ever attempted when `authenticated` is True — an unauthenticated
        line's payload is never even parsed, so a wrong/absent nonce can
        never surface a verdict OR a format-miss diagnostic."""
    if not nonce:
        return
    for ln in str(text).splitlines():
        s = ln.strip()
        if not s.startswith(_QA_VERDICT_SENTINEL):
            continue
        rest = s[len(_QA_VERDICT_SENTINEL):].strip()
        parts = rest.split(None, 1)
        if not parts:
            continue                       # no token at all -> not authenticated
        token = parts[0]
        payload = parts[1] if len(parts) > 1 else ""
        authenticated = (token == nonce)
        parsed = _parse_qa_payload(payload) if authenticated else None
        yield authenticated, parsed


def _extract_qa_verdict(text, nonce):
    """Scan `text` (one bare line, or a whole stream-json assistant-text
    block) for the reserved verdict sentinel and return the parsed verdict
    dict, or None. Fail-LOUD-never-fail-open (spec §3): a line that isn't a
    nonce-authenticated `@qa-verdict <NONCE> pass|fail` (or the legacy
    `{json}` form) yields None (the caller treats None as NOT clean), never a
    crash. Only a dict whose `verdict` is exactly "pass"/"fail" is accepted.

    `nonce` is the run's secret verdict nonce (see the FINALIZATION note
    above) — the token immediately after the sentinel MUST match it EXACTLY
    or the line is rejected outright, same as a malformed one. A falsy
    `nonce` (defensive: should never happen on the real dispatch path)
    rejects every line — fail-closed, never "any sentinel goes." Plain
    equality (not a constant-time compare): the nonce is single-use, freshly
    minted per run and never brute-forced live off this comparison, so a
    timing side-channel buys an attacker nothing here — and plain `==` can
    never raise on the attacker-controlled (possibly non-ASCII) token, unlike
    `secrets.compare_digest`, which would (this parser must never crash the
    supervisor's select loop on hostile input).

    2026-07-21 robustness follow-up: once the nonce authenticates a line, the
    trailing content is parsed by `_parse_qa_payload`, which now ALSO accepts
    a bare `pass`/`fail` keyword — not just the original JSON object. This is
    a FORMAT tolerance, not a security change: an unauthenticated line is
    still rejected exactly as before, and a nonce-authenticated line that
    still doesn't parse to pass/fail still returns None here (see
    `_qa_verdict_format_miss` for surfacing THAT case distinctly, so it never
    silently reads as a pass)."""
    for authenticated, parsed in _scan_qa_verdict_lines(text, nonce):
        if authenticated and parsed is not None:
            return parsed
    return None


def _qa_verdict_format_miss(text, nonce):
    """True iff `text` contains a line carrying the sentinel with the EXACT
    matching nonce (proof the line is genuinely the attacker's own — the
    same authentication `_extract_qa_verdict` requires) whose trailing
    content did NOT parse into a recognized pass/fail form. This is the
    diagnostic signal for item 3: an AUTHENTICATED near-miss — Gibby was
    genuinely talking to us on its own fd, it just didn't land in a shape the
    parser recognizes — as opposed to no authenticated line existing at all
    (missing sentinel, or one with a wrong/absent nonce, which this returns
    False for). Never treated as a verdict itself (fail-closed is
    unchanged); used only to label an UNRESOLVED cycle's likely cause for a
    human/CEO (`_unresolved_reason`)."""
    return any(authenticated and parsed is None
               for authenticated, parsed in _scan_qa_verdict_lines(text, nonce))


def classify_verdict(raw):
    """Classify the attacker Worker's captured verdict (the parsed dict from
    its `@qa-verdict` sentinel, or None if it never emitted a well-formed
    one). Fail-LOUD: None/absent -> not clean; a dict with verdict pass/fail
    -> clean iff pass. Returns {'clean': bool, 'verdict': ...} plus any fields
    the sentinel carried."""
    if not isinstance(raw, dict):
        return {"clean": False, "verdict": "missing"}
    verdict = raw.get("verdict")
    if verdict not in ("pass", "fail"):
        return {"clean": False, "verdict": "malformed", "raw": raw}
    result = dict(raw)
    result["clean"] = (verdict == "pass")
    return result


def _unresolved_reason(verdict, format_miss):
    """Item 3 — WHY a gate round is not clean, for the diagnostic UNRESOLVED
    surfacing (`_dispatch_redblue`'s cap-reached message + ledger). `verdict`
    is a `classify_verdict()` result; `format_miss` is the attacker Worker's
    `nonce_format_miss` flag for that same round. Three distinct outcomes,
    so a human/CEO reading a stalled cycle can tell them apart at a glance:

      'genuine_fail' — Gibby emitted a valid nonce-authenticated FAIL. The
                        work genuinely did not clear verification.
      'format_miss'  — Gibby's own fd carried a nonce-AUTHENTICATED line
                        (proving it's genuinely Gibby's, not planted), but
                        its content wasn't a recognized pass/fail form. This
                        is the live 2026-07-21 false-negative's exact shape —
                        almost certainly a sentinel-format miss, not a real
                        finding.
      'no_verdict'   — no nonce-authenticated line was captured on Gibby's fd
                        AT ALL (no sentinel, or one with a wrong/absent
                        nonce). Most likely a dispatch/harness problem.

    Never auto-passes on either miss case — fail-closed is unchanged; this
    only labels WHY the gate didn't clear."""
    if verdict.get("verdict") == "fail":
        return "genuine_fail"
    if format_miss:
        return "format_miss"
    return "no_verdict"


# Human-facing detail for each `_unresolved_reason` value — shared by main()'s
# stderr message (below) and available to any other caller that wants the
# same wording (e.g. company-run.sh's ledger, which pulls `reason` straight
# out of the `@redblue-gate {json}` line rather than re-deriving text).
_UNRESOLVED_REASON_TEXT = {
    "genuine_fail": "Gibby genuinely attacked it and reported FAIL",
    "format_miss": "Gibby's fd carried a nonce-authenticated line that wasn't "
                    "a recognized pass/fail form — likely a sentinel-FORMAT "
                    "miss, not a real fail",
    "no_verdict": "no nonce-authenticated verdict was ever captured on "
                   "Gibby's fd (no sentinel, or one with a wrong/absent nonce)",
}


def _verdict_contract(nonce):
    """The output-contract clause Gibby's dispatch prompt gains, composed via
    the SAME `prompt_builder.output_contract` every other dispatch contract
    goes through. The verdict rides on Gibby's OWN stdout as the reserved
    `@qa-verdict <NONCE> ` sentinel line — NOT a file (a shared-fs artifact
    Bob could forge).

    `nonce` (finalization pass): the fresh per-run secret `_dispatch_redblue`
    minted, embedded HERE ONLY — this string goes into Gibby's dispatch
    prompt, never Bob's. Gibby must echo it back verbatim as proof the
    verdict line is genuinely its own last word, not text it happened to
    quote from somewhere Bob could plant it (a comment, a file it cats while
    investigating). Returns None if prompt_builder couldn't be imported
    (real_command already degrades to a plain prompt then; the gate simply
    appends nothing).

    2026-07-21 robustness follow-up (the gate's first live false negative —
    a correct build came back UNRESOLVED because the real Gibby's genuine
    verdict didn't reproduce the old JSON-only sentinel closely enough): this
    contract now LEADS with the simplest possible copy-paste line — a bare
    `pass`/`fail` keyword right after the nonce — spelled out verbatim with
    the actual nonce substituted in, so there is nothing left to compose or
    get subtly wrong. The nonce itself is UNCHANGED and still MANDATORY —
    this is a format simplification, not a security relaxation. The
    structured JSON shape is still mentioned as an accepted fallback
    (`_extract_qa_verdict` parses both), but is no longer what Gibby is
    steered toward reproducing exactly."""
    if _pb is None:                                        # pragma: no cover
        return None
    pass_line = f"@qa-verdict {nonce} pass"
    fail_line = f"@qa-verdict {nonce} fail"
    where = "your OWN stdout"
    fmt = (
        f'EXACTLY ONE line, copied verbatim, as the VERY LAST thing you '
        f'output — nothing after it. If you genuinely attacked this and '
        f'found nothing, that line is literally:\n\n'
        f'    {pass_line}\n\n'
        f'If you found a genuine problem, that line is literally:\n\n'
        f'    {fail_line}\n\n'
        f'`{nonce}` is this run\'s secret verdict nonce — copy it EXACTLY, '
        f'character for character, right after `@qa-verdict `; a wrong or '
        f'missing nonce means your verdict is NOT accepted (this proves the '
        f'line is genuinely your own conclusion, not text quoted from a '
        f'file or comment). This line is MANDATORY — the round is not '
        f'complete without it, and it is the ONLY line read as your '
        f'verdict; nothing else you write is parsed for one. Do NOT write '
        f'it to any file — print it on stdout. Never mention or quote the '
        f'nonce `{nonce}` anywhere else in your output. (A structured form, '
        f'`@qa-verdict {nonce} {{"verdict": "pass"|"fail", "checked": '
        f'[...]}}`, is also accepted if you prefer it, but the plain '
        f'pass/fail line above is simpler and preferred.)'
    )
    return _pb.output_contract(where, fmt)


def _redblue_pair_ids():
    """The Layer-B builder/attacker ids the gate arms for — READ from
    `employee.ALLOWED_DUTIES` (whoever holds the 'build' duty / the 'attack'
    duty), never a second hardcoded pair living beside schedule_validator's
    own tables (modularize, don't special-case). Degrades to the known
    ('bob', 'gibby') pair if employee.py can't be imported — the topology
    those duties encode is fixed (R1/R4), so the fallback is not a guess."""
    try:
        from employee import ALLOWED_DUTIES
        builder = next((k for k, v in ALLOWED_DUTIES.items() if "build" in v), "bob")
        attacker = next((k for k, v in ALLOWED_DUTIES.items() if "attack" in v), "gibby")
        return builder, attacker
    except Exception:
        return "bob", "gibby"


def _builder_ids():
    """Every id holding a BUILD-class duty (FIX B / Finding 4). Read from the
    SAME Layer-B tables as _redblue_pair_ids — so "does this plan contain a
    builder?" (which forces the attacker to be present) can never disagree
    with "who is the builder". Degrades to {'bob'} if employee.py is absent."""
    try:
        from employee import ALLOWED_DUTIES, BUILD_DUTIES
        return {k for k, v in ALLOWED_DUTIES.items() if set(v) & set(BUILD_DUTIES)}
    except Exception:
        return {"bob"}


# FIX B: when a code-touching (builder) plan omits the attacker, the gate
# auto-injects Gibby with this default attack task rather than letting a
# builder run unverified because Phoebe (an LLM fed attacker-influenced $TASK)
# dropped the key.
_AUTO_ATTACK_TASK = (
    "verify the builder's change: attack it across the standard surfaces "
    "(correctness, malformed input, concurrency, resources, spec drift, "
    "regression) and report a machine-checkable verdict")


# FIX 3, SUPERSEDED (finalization pass) — Finding 1 defense-in-depth (build
# work routed to a NON-builder skips the gate, since gate-arming keys on the
# builder id being present). The original fix was a CONSERVATIVE content
# heuristic — `_looks_like_code_mutation` (a code-MUTATION verb AND a
# code-file/path signal in the task text) — that REFUSED such a dispatch
# loudly. Gibby's finalization-pass review found it cost more than it closed:
# it false-refused legitimate READ-only tasks whose phrasing brushed a
# mutation verb ("audit/review Bob's patch to supervisor.py" reads as
# borderline to a regex no matter how it's tuned), and it was never airtight
# to begin with (a cleverly-worded build task could still evade it) — pure
# defense-in-theatre once the REAL fix landed.
#
# Phase 34 (merged, `employee.py` CORE_TOOL_PROFILES / `disallowed_tools_for`)
# IS that real fix: every non-builder (tony/mike/elon/phoebe/july) is spawned
# with `--disallowedTools Bash Write Edit NotebookEdit` — Bash/Write/Edit are
# structurally ABSENT from the model's own tool schema, not merely
# discouraged (verified against a real `claude -p`). A non-builder is
# therefore PHYSICALLY unable to mutate source, full stop — no task-text
# guess required, and nothing to evade. This class of finding is closed by
# construction, so the heuristic (`_looks_like_code_mutation`,
# `_MUTATION_VERB_RE`, `_CODE_SIGNAL_RE`, and the `dispatch()` refusal branch
# that called it) is REMOVED here, not merely disabled. The builder-presence
# ARMING logic (a plan with a builder present still auto-injects Gibby so the
# gate always runs — FIX B, below) is UNCHANGED; only the redundant
# non-builder text-refusal is gone.

_REDBLUE_MAX_ROUNDS_DEFAULT = 3
_REDBLUE_MAX_ROUNDS_CEILING = 10   # an "absurd" round count is refused, not honored


def _redblue_max_rounds():
    """SELF_COMPANY_REDBLUE_MAX_ROUNDS — the MANDATORY iteration cap (spec
    §2/§3): "a block-forever loop is the proposal's own named risk," so this
    is never tunable to effectively-infinite. A non-positive value, an
    unparseable value, OR an absurdly large one all clamp to the default —
    exactly the fail-closed-to-default discipline schedule_config's cadence
    validation already uses for a bad cron field."""
    raw = os.environ.get("SELF_COMPANY_REDBLUE_MAX_ROUNDS", str(_REDBLUE_MAX_ROUNDS_DEFAULT))
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return _REDBLUE_MAX_ROUNDS_DEFAULT
    if n < 1 or n > _REDBLUE_MAX_ROUNDS_CEILING:
        return _REDBLUE_MAX_ROUNDS_DEFAULT
    return n


# FIX 2 (Finding 3 — a shared-fs ledger marker is worker-forgeable): the gate
# result is NOT written to any file under the company tree. A Bob worker (no
# tool fence yet — that's Phase 34) can overwrite ANY known path there, incl. a
# `ops/.last-redblue-gate.json`, flipping the human-facing ledger cell to a
# forged "clean". Instead the supervisor emits the result on its OWN stderr as
# a `@redblue-gate {json}` line (see main()); company-run.sh captures the
# supervisor's stderr directly (an anonymous pipe, no filesystem path any
# worker can target — a worker's stderr is merged into its OWN stdout pipe, it
# cannot write the supervisor's fd 2). Same trust boundary as the pipe-identity
# verdict channel: parent-controlled, worker-unreachable.
_GATE_RESULT_SENTINEL = "@redblue-gate "


class Status(enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    WORKING = "working"
    DONE = "done"
    FAILED = "failed"


GLYPH = {Status.IDLE: " ", Status.STARTING: "…", Status.WORKING: "~",
         Status.DONE: "=", Status.FAILED: "x"}

# Preferred display order + fallback role labels (real roles come from personas).
ROLE_HINTS = {
    "elon": "CEO · direction", "phoebe": "PM · gateway", "tony": "Improvement · entropy",
    "gibby": "Verify · sources", "bob": "Engineer · builds", "july": "People · personas",
    "tom": "Infra · scheduling",
}
# Phase 32 fix: "mike" was missing from ORDER entirely — a real mike desk fell
# through to the "not in ORDER" append-at-the-end branch below instead of its
# canonical position. Added after gibby (Chairman's instruction).
ORDER = ["elon", "phoebe", "tony", "gibby", "mike", "bob", "july", "tom"]


class Member:
    """The supervisor's ORCHESTRATION view of one company member: how to spawn it
    (demo/real command) and render it live. All members share this capability
    (Chairman: everyone). The authoritative identity/capability/memory model is
    `employee.Employee` — this class BRIDGES to it (see _recall_memory) rather
    than re-implementing it, so employee.py stays the single data-driven class."""

    def __init__(self, emp_id, name=None, role=None, company_dir="."):
        self.id = emp_id
        self.name = name or emp_id.capitalize()
        self.role = role or ROLE_HINTS.get(emp_id, "member")
        self.company_dir = company_dir

    @classmethod
    def roster(cls, company_dir):
        """Discover the physically-present employees from org/employees/ using
        the SAME strict per-desk predicate the validator/discover() use (Phase
        32 hotfix Finding 2). Before this, roster() did an ad-hoc scan that
        listed any subdir with a `persona.md` — NO id-charset check, NO
        `context.md` requirement, NO symlink rejection — so a persona-only
        "ghost" desk or a symlinked-persona desk that `employee.discover()` /
        R7 correctly exclude still got listed here AND its `persona.md` inlined
        into a real worker prompt (supervisor `--list` -> plan -> `--dispatch`
        -> `real_command`). Routing through `employee.is_valid_desk` makes the
        live dispatch path share the exact strict membership rules, so the three
        discovery paths can no longer disagree.

        Membership here is PHYSICAL presence (a desk on disk) filtered by
        structural validity — deliberately NOT `discover()` itself, because
        `discover()` force-includes every core id whether or not its desk
        exists, whereas the supervisor lists only desks it can actually spawn.
        Display order is unchanged: the fixed ORDER first, then any discovered
        ids not in ORDER, sorted. Zero hired desks -> the same core roster as
        before (byte-identical `--list`)."""
        from employee import is_valid_desk
        base = Path(company_dir) / "org" / "employees"
        found = ([d.name for d in sorted(base.iterdir()) if is_valid_desk(d)]
                 if base.exists() else [])
        ids = [e for e in ORDER if e in found] + [e for e in found if e not in ORDER]
        return [cls(i, company_dir=company_dir) for i in (ids or ORDER)]

    def demo_command(self, task, delay=0.3):
        """A simulated worker: emits the @status protocol so the live tree moves."""
        phases = ["planning", "working", "reviewing", "done"]
        script = "; ".join(f"echo '@status {p}'; sleep {delay}" for p in phases)
        return ["bash", "-c", script]

    def _recall_memory(self, task):
        """Phase 18 Item 4 + Phase 18c — dispatch-time MEMORY injection. BRIDGE to
        the employee.py data model: load THIS member's real `Employee` and ask it
        for the ready-to-prepend memory block for `task`. We import lazily and
        bridge (rather than merge the two classes) because process-spawning is not a
        data-model concern — employee.py stays the single data-driven class that
        owns identity/capabilities/memory.

        `dispatch_context()` returns TWO distinct sections, both internally gated +
        budget-capped + timeout-degrading:
          * "Relevant past experience" — this employee's OWN store (rag employee).
          * "Relevant company memory"  — the SHARED Chairman corpus, ONLY for a
            `shared_memory_read` employee (elon by default). Phase 18c wires this
            read INTO dispatch so autonomous/cron/trigger work carries the Chairman's
            standing direction, not just the interactive ask-time hook.
        A `flat`, no-venv, empty-index, timeout, or zero-hit case yields "" for the
        relevant half — dispatch is never blocked and never fails on recall. The
        try/except here only guards the import; dispatch_context never raises."""
        try:
            from employee import Employee as EmployeeModel
        except Exception:
            return ""
        try:
            return EmployeeModel.load(self.id, self.company_dir).dispatch_context(task)
        except Exception:
            return ""

    def worker_env(self):
        """Environment for a spawned real worker, or None to inherit unchanged.

        DOUBLE-INJECTION GUARD (Phase 18c). A spawned `claude -p` worker ALSO fires
        the plugin's UserPromptSubmit hook (hook_memory_inject.py) on its own prompt
        — confirmed: `-p` fires UserPromptSubmit before Claude processes it. For a
        `shared_memory_read` employee we already inject the SHARED company memory
        EXPLICITLY into the worker prompt at dispatch (see real_command), so we set
        SC_NO_MEMORY_INJECT=1 to make that worker's hook a clean no-op — otherwise
        the shared memory would be injected a SECOND time. The explicit dispatch
        injection is then the single source. For every OTHER employee we return None
        (inherit), so the hook keeps providing shared memory as before — no
        regression for non-shared-read workers."""
        try:
            from employee import Employee as EmployeeModel
            if EmployeeModel.load(self.id, self.company_dir).shared_memory_read:
                return {**os.environ, "SC_NO_MEMORY_INJECT": "1"}
        except Exception:
            pass
        return None

    def _resolve_model(self, default_model):
        """Phase 29 Item 1: resolve THIS employee's `context.md` model: field via
        the ONE resolution function (employee.Employee.resolved_model) — never a
        second alias table here. Any trouble loading the desk degrades to
        `default_model` silently (mirrors _recall_memory's own import-guard
        discipline: a dispatch must never fail because memory/model resolution
        broke). Returns (model_id, warning_or_None)."""
        try:
            from employee import Employee as EmployeeModel
            return EmployeeModel.load(self.id, self.company_dir).resolved_model(default_model)
        except Exception:
            return default_model, None

    def _resolve_tool_profile(self):
        """Phase 34 Item 2: the `--disallowedTools` argv tokens for THIS
        dispatched worker, from employee.py's Layer-B duty->profile table
        (`disallowed_tools_for`) — derived from THIS employee's id, never from
        worker-supplied input. Unlike `_resolve_model` (which degrades OPEN to
        a sane default on any trouble — model choice isn't a security
        boundary), this degrades CLOSED: any import/lookup failure returns the
        module's own `_FAILCLOSED_DISALLOWED_TOOLS` backstop instead of an
        empty/unrestricted list, so a broken employee.py import can never
        silently widen a worker's tools."""
        try:
            from employee import disallowed_tools_for
            tools = disallowed_tools_for(self.id)
            if isinstance(tools, (list, tuple)):
                return list(tools)
        except Exception:
            pass
        return list(_FAILCLOSED_DISALLOWED_TOOLS)

    def _load_persona(self):
        """P5 (Phase 29 fold-in): read THIS employee's persona.md BODY, capped
        to _PERSONA_INLINE_CHARS, so real_command can inline it directly
        instead of sending the worker on an errand ("Read your persona at
        ..."). A missing/unreadable persona (or missing employee.py) degrades
        to "" — real_command then falls back to the role-line-only prompt,
        never blocking a dispatch. Persona does NOT share the memory-injection
        budget (_DISPATCH_INJECT_BUDGET) — a separate, fixed cap."""
        try:
            from employee import Employee as EmployeeModel
            path = EmployeeModel.load(self.id, self.company_dir).persona_path
            text = Path(path).read_text(encoding="utf-8").strip()
        except Exception:
            return ""
        if not text:
            return ""
        if len(text) > _PERSONA_INLINE_CHARS:
            text = text[:_PERSONA_INLINE_CHARS].rstrip() + "…"
        return text

    def real_command(self, task, default_model=None, extra_contract=None):
        """A real headless agent, primed with this employee's role, persona,
        and the task. Assembled via the Phase 29 Item 4 shared prompt_builder
        (role header, stated wall-clock budget, output contract, task
        boundary — Mike's Idea 7 four elements) when available; degrades to
        the pre-Item-4 inline strings if the module can't be imported (never
        blocks a dispatch).

        `extra_contract` (Phase 33): an OPTIONAL extra output-contract clause
        appended after the standard one — real_command stays generic (no
        Gibby special-case in here); the caller (Supervisor's red/blue gate)
        decides when and for whom to pass one, e.g. the verdict-marker
        requirement for an attacker in a gated round.

        Phase 29 Item 1: the model is resolved from THIS employee's context.md
        `model:` field (haiku/sonnet/opus/fable alias, a `claude-*` id verbatim,
        or `default_model` on unset/invalid — see employee.Employee.resolved_model
        for the full degrade contract). `default_model` defaults to the module
        constant DEFAULT_MODEL (schedule_config.DEFAULT_AGENT_MODEL) when the
        caller doesn't pass one, so there is exactly one default in the system.

        Phase 29 Item 3: real workers are spawned with `--output-format
        stream-json --verbose` (mirroring daily-run.sh's STREAM_ARGS) so the
        supervisor's live tree derives phases from the actual event stream
        instead of a buffered-to-EOF '@status' marker the model may forget.
        `SELF_COMPANY_AGENT_STREAM=0` restores the old plain-text mode.

        P5: the persona BODY is inlined directly (fence-safe, via
        prompt_builder.fence) instead of telling the worker to go read it off
        disk — a worker that skips the read (models sometimes do) used to
        silently run persona-less; now the persona travels WITH the prompt.

        Before dispatch, inject this employee's relevant MEMORY (Phase 18 Item 4 +
        Phase 18c): for a `rag`-mode employee, the OWN-store "Relevant past
        experience" block; for a `shared_memory_read` employee (elon), ALSO the
        SHARED "Relevant company memory" block. A `flat`, non-shared-read employee
        (e.g. bob/gibby/tom) with no relevant memory injects NOTHING
        (dispatch_context returns ""). The two blocks share one budget and are
        deduped by employee.py — separate from the persona's own fixed cap."""
        if default_model is None:
            default_model = DEFAULT_MODEL
        model, warning = self._resolve_model(default_model)
        self.last_model_warning = warning   # surfaced by Supervisor._emit
        budget = _dispatch_budget()

        if _pb is not None:
            persona = self._load_persona()
            parts = [
                _pb.role_header(self.name, self.role),
                _pb.budget_line(budget),
                f"Task: {task}",
            ]
            if persona:
                parts.append(_pb.fence(persona, label="PERSONA"))
            parts.append(_pb.output_contract(
                "your tool calls and final reply",
                "do the task directly; print progress lines beginning with "
                "'@status <phase>' (e.g. '@status planning', '@status done') as "
                "optional garnish — the supervisor also derives phases from your "
                "tool calls",
                summary_cap=True))
            if extra_contract:
                parts.append(extra_contract)
            parts.append(_pb.task_boundary(
                "stay in role and use only your granted tools; keep it tight; "
                "wrap up cleanly before the budget above runs out"))
            prompt = "\n\n".join(parts)
        else:                                        # pragma: no cover - defensive
            prompt = (
                f"You are {self.name} ({self.role}) in the self-company, working "
                f"non-interactively. Task: {task}\n"
                f"Read your persona at .company/org/employees/{self.id}/persona.md and stay "
                f"in role. As you work, print progress lines beginning with '@status ' "
                f"followed by ONE short phase word (e.g. '@status planning', '@status "
                f"working', '@status reviewing'). Print '@status done' when finished. "
                f"Keep it tight."
            )
            if extra_contract:
                prompt = f"{prompt}\n\n{extra_contract}"
        memory = self._recall_memory(task)
        if memory:
            prompt = f"{prompt}\n\n{memory}"
        cmd = ["claude", "-p", prompt, "--model", model]
        if os.environ.get("SELF_COMPANY_AGENT_STREAM", "1") != "0":
            cmd += ["--output-format", "stream-json", "--verbose"]
        # Phase 34 Item 2: append the resolved tool-restriction profile LAST —
        # a bare `--disallowedTools <Tool>` list (never a path/command
        # pattern; see employee.py's Phase 34 table for why). Empty list
        # (build/infra) appends nothing, so bob/tom's argv is byte-identical
        # to pre-Phase-34 dispatch.
        disallowed = self._resolve_tool_profile()
        if disallowed:
            cmd += ["--disallowedTools", *disallowed]
        return cmd


def _model_from_cmd(cmd):
    """Pull the `--model` value back out of an already-built argv (post-hoc
    introspection, not a second resolution path) — Phase 29 Item 1 acceptance
    (e): the event log must show which model each worker actually ran, without
    Worker/Supervisor needing their own copy of the resolution logic. Demo cmds
    carry no `--model`; returns None there."""
    try:
        return cmd[cmd.index("--model") + 1]
    except (ValueError, IndexError):
        return None


# Phase 29 Item 3: the legacy '@status <word>' marker, now scanned WITHIN an
# assistant text block (stream-json) as well as a bare plain-text line — a
# model that still emits it (inside a text content block, possibly mid-string)
# keeps working; a model that never does now still produces phase transitions
# from the event stream itself (tool_use -> phase, result -> done/failed).
_EMBEDDED_STATUS_RE = re.compile(r"(?:^|\n)@status\s+(\S+)")


class Worker:
    """Wraps one running employee process; parses its live event stream —
    stream-json for a real worker (Phase 29 Item 3), the legacy '@status'
    marker protocol for a demo worker or a plain-text fallback."""

    def __init__(self, employee, task, command, env=None, budget=None, model=None,
                 capture_verdict=False, verdict_nonce=None):
        self.emp = employee
        self.task = task
        self.command = command
        self.env = env                # None -> inherit; set to guard the memory hook
        self.budget = budget          # Item 3 (TOM-1): wall-clock deadline (s); None -> unbounded
        self.model = model            # Item 1: the --model this worker actually runs (None for demo)
        self.timed_out = False        # Item 3 (TOM-1): killed by the deadline (vs natural exit)
        # Phase 33 security redesign: True ONLY for the attacker (Gibby) worker
        # in a gated round. A `@qa-verdict` sentinel is honored ONLY when this
        # is set — i.e. only when read off THIS (Gibby's) pipe fd. Bob's worker
        # has it False, so a sentinel Bob prints is ignored (attribution by
        # pipe identity, the OS boundary Bob can't cross).
        self.capture_verdict = capture_verdict
        # Finalization: the run's secret verdict nonce, set ONLY on the
        # attacker Worker (capture_verdict=True) — this Worker's fd is the
        # ONLY place `_extract_qa_verdict` is ever called with a real nonce,
        # so a sentinel is honored only when BOTH the pipe-identity check
        # (capture_verdict) AND the nonce match, closing the echo-forgery a
        # bare pipe-identity check alone did not.
        self.verdict_nonce = verdict_nonce
        self.verdict = None           # the parsed verdict dict, once seen on this fd
        # Item 3 (2026-07-21 robustness follow-up): True once a nonce-
        # AUTHENTICATED `@qa-verdict` line has been seen on this fd whose
        # content did NOT parse into pass/fail (see `_qa_verdict_format_miss`).
        # Diagnostic only — never treated as a verdict, never clears once set
        # (one authenticated near-miss is enough to tell a human "Gibby WAS
        # talking to us, just not in a recognized shape").
        self.nonce_format_miss = False
        # Phase 33 FIX 1 (Finding 2 — echo/last-wins forgery): the verdict is
        # FIRST-WINS and LOCKED. Once a well-formed sentinel is recorded, a
        # LATER sentinel on this same fd is ignored — so if Gibby's transcript
        # ever quotes attacker-planted `@qa-verdict pass` text (e.g. Bob wrote
        # it in a source comment Gibby echoes), it cannot overwrite Gibby's own
        # real verdict. The lone EXCEPTION is Gibby's FINAL stream-json `result`
        # event (authoritative=True): its `.result` is Gibby's completed reply,
        # so it is preferred over any mid-stream text and overrides the lock.
        self._verdict_locked = False
        self.status = Status.IDLE
        self.phase = ""
        self.last = ""
        self.lines = []
        self.proc = None
        self._t0 = None
        self._t1 = None
        self._buf = b""            # Item C1: raw bytes pending a '\n'
        self.usage = {"input": 0, "output": 0, "cost": 0.0}  # token/cost tracking from result events

    def start(self):
        # Item C1 (Phase 26 fold-in / Gibby #4): binary, unbuffered pipe — we
        # read it ourselves via os.read() on the raw fd, never through
        # proc.stdout's own buffered readline().
        self.proc = subprocess.Popen(
            self.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, env=self.env)
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.status = Status.STARTING
        self._t0 = time.monotonic()

    @property
    def fd(self):
        return self.proc.stdout.fileno() if self.proc and self.proc.stdout else None

    def read_available(self):
        """Item C1 (Phase 26 fold-in / Gibby #4): non-blocking raw read +
        manual line assembly. `select()` only promises the fd is READABLE —
        it does NOT promise a full line is available. The old code called the
        buffered `readline()`, which keeps issuing blocking reads until it
        sees a '\\n' or EOF; a worker that emits a partial line (no trailing
        newline) and then stalls would block that call forever, wedging the
        WHOLE select loop — including the in-loop deadline check for every
        OTHER worker — until the outer `timeout -k` finally SIGKILLs it. With
        the fd set non-blocking, a short/empty read just means "nothing more
        right now" (BlockingIOError), never a wait, so the loop always comes
        back around to re-check every worker's budget on schedule. The outer
        `timeout -k` wrap remains the backstop for a worker that ignores
        signals entirely.

        Returns (lines, eof): decoded complete lines (newline stripped), and
        whether the far end has closed (mirrors the old readline()=="" EOF
        signal, flushing any final partial line as the last one first)."""
        try:
            chunk = os.read(self.fd, 65536)
        except BlockingIOError:
            return [], False
        except OSError:
            chunk = b""
        if chunk == b"":
            lines = []
            if self._buf:
                lines.append(self._buf.decode("utf-8", errors="replace"))
                self._buf = b""
            return lines, True
        self._buf += chunk
        lines = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            lines.append(line.decode("utf-8", errors="replace"))
        return lines, False

    def consume_line(self, line):
        """Classify one line of worker output. Order (Phase 29 Item 3):
        1. the legacy bare '@status <word>' marker (demo workers; a real worker
           in plain-text fallback mode) — fast path, unchanged from before.
        2. a stream-json event (`line[:1] == '{'`): exactly one `json.loads`,
           never a regex scrape (Elon's note) — malformed JSON falls through to
           plain log text instead of crashing the loop.
        3. anything else: a plain log line (`self.last`), same as always.
        This is sniffed PER LINE, not decided once at spawn time, so demo mode
        and the `SELF_COMPANY_AGENT_STREAM=0` plain-text fallback need zero
        branching here — they simply never produce a line starting with '{'."""
        line = line.rstrip("\n")
        self.lines.append(line)
        # Phase 33: a bare `@qa-verdict {json}` line (demo/plain-text mode) is
        # honored ONLY on the attacker's fd (capture_verdict). On any other
        # worker's fd it is NOT consumed here — it falls through to a plain log
        # line, so Bob printing this sentinel can never satisfy the gate.
        if self.capture_verdict and line.startswith(_QA_VERDICT_SENTINEL):
            self._capture_verdict_line(line)
            return
        if line.startswith("@status "):
            self.phase = line[len("@status "):].strip()
            self.status = Status.DONE if self.phase == "done" else Status.WORKING
            return
        if line[:1] == "{" and self._consume_stream_json(line):
            return
        if line:
            self.last = line

    def _consume_stream_json(self, line):
        """Parse ONE stream-json event line. Returns True if it was consumed as
        valid JSON (whether or not it carried a phase-bearing shape) — the
        caller then does NOT also treat the raw JSON as a plain log line.
        Returns False only for a JSON parse failure, so a malformed/truncated
        line degrades to the ordinary log-text path instead of crashing the
        select loop (Gibby's hostile-stream-json harness: giant lines, split
        UTF-8, interleaved garbage, '@status' inside JSON strings)."""
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return False
        if not isinstance(event, dict):
            return False
        etype = event.get("type")
        if etype == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    name = str(block.get("name") or "tool").strip().lower() or "tool"
                    self.phase = name
                    self.status = Status.WORKING
                    self.last = f"tool: {name}"
                elif btype == "text":
                    text = str(block.get("text") or "")
                    # Phase 33: in stream-json mode Gibby's verdict sentinel
                    # arrives inside its assistant text. Honored ONLY on the
                    # attacker's fd (capture_verdict) — Bob's assistant text is
                    # never scanned for a verdict. FIRST-wins (FIX 1): a later
                    # mid-stream sentinel can't overwrite the first.
                    if self.capture_verdict:
                        self._capture_verdict_line(text)
                    matches = _EMBEDDED_STATUS_RE.findall(text)
                    if matches:
                        word = matches[-1].strip()
                        self.phase = word
                        self.status = Status.DONE if word == "done" else Status.WORKING
                    elif text.strip():
                        if self.status not in (Status.DONE, Status.FAILED):
                            self.status = Status.WORKING
                        self.last = text.strip().splitlines()[0][:200]
        elif etype == "result":
            is_error = bool(event.get("is_error"))
            self.phase = "failed" if is_error else "done"
            self.status = Status.FAILED if is_error else Status.DONE
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.usage["input"] = usage.get("input_tokens", 0)
                self.usage["output"] = usage.get("output_tokens", 0)
                cost = usage.get("cost", 0.0)
                self.usage["cost"] = float(cost) if cost else 0.0
            # FIX 1: the `result` event's `.result` is Gibby's COMPLETED final
            # reply — the cleanest place its verdict lives in the stream-json
            # shape. Prefer it over any mid-stream text (authoritative: it
            # overrides even a locked mid-stream verdict), so a verdict Gibby
            # only firms up in its final answer still wins.
            if self.capture_verdict:
                rtext = event.get("result")
                if isinstance(rtext, str):
                    self._capture_verdict_line(rtext, authoritative=True)
        # else: "system" / "user" / any other recognized JSON shape — valid
        # stream-json, no new phase information; consumed without touching
        # self.last (raw JSON is never shown as the human-readable detail line).
        return True

    def _capture_verdict_line(self, text, authoritative=False):
        """The ONE call site (Item 3, 2026-07-21 follow-up) all three
        verdict-capture points (bare sentinel line, stream-json assistant
        text, the final `result` event's text) now go through, so the
        nonce-authenticated-but-unrecognized-format diagnostic is derived
        from the EXACT SAME scan `_extract_qa_verdict` uses, never a second
        hand-rolled check that could drift from it. Extracts + records a
        verdict as before, AND separately notes (`nonce_format_miss`) when
        `text` carried a line the nonce authenticated but whose content
        wasn't a recognized pass/fail form — diagnostic only, never a
        substitute for a genuine verdict."""
        found = _extract_qa_verdict(text, self.verdict_nonce)
        if found is None and _qa_verdict_format_miss(text, self.verdict_nonce):
            self.nonce_format_miss = True
        self._record_verdict(found, authoritative=authoritative)

    def _record_verdict(self, found, authoritative=False):
        """FIX 1 (Finding 2): record a captured verdict FIRST-wins + LOCKED.
        A None (no/ill-formed sentinel) is ignored. A mid-stream sentinel sets
        the verdict only if none is locked yet, then locks it — so a later
        echoed/planted sentinel on the same fd cannot flip it. Gibby's final
        `result` event is `authoritative`: it overrides the lock (its `.result`
        is the completed reply, preferred over mid-stream text)."""
        if found is None:
            return
        if authoritative:
            self.verdict = found
            self._verdict_locked = True
            return
        if not self._verdict_locked:
            self.verdict = found
            self._verdict_locked = True

    def on_eof(self):
        rc = self.proc.wait() if self.proc else 0
        if self.proc and self.proc.stdout:
            self.proc.stdout.close()           # release the pipe fd (many workers)
        if self.status not in (Status.DONE, Status.FAILED):
            self.status = Status.DONE if rc == 0 else Status.FAILED
        self._t1 = time.monotonic()

    def over_budget(self, now=None):
        """Item 3: True once this worker has run past its wall-clock budget without
        finishing. None budget (demo workers) is unbounded."""
        if self.budget is None or self._t0 is None:
            return False
        now = time.monotonic() if now is None else now
        return (now - self._t0) > self.budget

    def kill_over_budget(self):
        """Item 3: force-terminate a worker that blew its budget without reaching
        EOF. SIGKILL (not TERM) — a child that ignored the outer `timeout`'s TERM
        is exactly why we're here — then reap and mark FAILED so dispatch can
        return cleanly instead of wedging on a never-EOF pipe."""
        self.timed_out = True
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
            if self.proc.stdout:
                try:
                    self.proc.stdout.close()
                except OSError:
                    pass
        self.status = Status.FAILED
        self.phase = self.phase or "timeout"
        self._t1 = time.monotonic()

    def elapsed(self):
        if self._t0 is None:
            return 0.0
        return (self._t1 or time.monotonic()) - self._t0


class LiveTree:
    """Renders the live status. Repaints in place on a TTY; streams a feed if not."""

    def __init__(self, roster, stream=None):
        self.roster = roster
        self.stream = stream or sys.stdout
        self.tty = self.stream.isatty()
        self._painted = 0

    def _rows(self, workers):
        W = 52
        now = datetime.now().strftime("%H:%M:%S")
        head = f" self-company · live supervisor · {now} "
        rows = ["╭" + head.center(W + 2, "─") + "╮"]
        for emp in self.roster:
            w = workers.get(emp.id)
            if w is None:
                body = f"[ ] {emp.name:<7} idle"
            else:
                ph = w.phase or w.status.value
                body = f"[{GLYPH[w.status]}] {emp.name:<7} {ph:<12} {w.elapsed():4.1f}s"
            rows.append("│ " + body[:W].ljust(W) + " │")
        rows.append("╰" + "─" * (W + 2) + "╯")
        return rows

    def repaint(self, workers):
        rows = self._rows(workers)
        if self.tty:
            if self._painted:
                self.stream.write(f"\x1b[{self._painted}A")   # cursor up
            self.stream.write("\n".join(rows) + "\n")
            self._painted = len(rows)
        self.stream.flush()

    def feed(self, worker):
        """Non-TTY: emit one live event line per status change (reads well in app)."""
        if not self.tty:
            ph = worker.phase or worker.status.value
            self.stream.write(f"{datetime.now():%H:%M:%S}  {worker.emp.name:<7} → {ph}\n")
            self.stream.flush()

    def final(self, workers):
        self.stream.write("\n".join(self._rows(workers)) + "\n")
        self.stream.flush()


class Supervisor:
    """Ephemeral orchestrator: spawn workers, multiplex their live streams, render."""

    def __init__(self, company_dir, renderer=None, event_log=None):
        self.company_dir = company_dir
        self.roster = Member.roster(company_dir)
        self.by_id = {e.id: e for e in self.roster}
        self.renderer = renderer if renderer is not None else LiveTree(self.roster)
        self.event_log = event_log
        # Phase 33: set by a gated dispatch (see dispatch()/_dispatch_redblue);
        # stays None for a lone-worker / non-red-blue dispatch, so a caller can
        # tell "gate never armed" apart from "gate armed and resolved clean".
        self.last_gate = None

    def _emit(self, worker, kind):
        if self.event_log is not None:
            event = {
                "ts": datetime.now().replace(microsecond=0).isoformat(),
                "emp": worker.emp.id, "kind": kind,
                "status": worker.status.value, "phase": worker.phase,
                # Phase 29 Item 1 acceptance (e): the run record must show which
                # model each worker actually ran — None for a demo worker (no
                # --model on its argv at all).
                "model": worker.model,
            }
            # Item 1: surface a degrade warning (invalid model: value) on the
            # event log rather than only a code comment nobody sees — a finding,
            # never a dispatch-blocking error.
            warning = getattr(worker.emp, "last_model_warning", None)
            if warning:
                event["model_warning"] = warning
            self.event_log.append(event)

    def _accumulate_usage(self, workers):
        """Accumulate token usage from all workers to daily marker."""
        total_input = 0
        total_output = 0
        total_cost = 0.0
        for w in workers.values():
            total_input += w.usage.get("input", 0)
            total_output += w.usage.get("output", 0)
            total_cost += w.usage.get("cost", 0.0)
        if total_input > 0 or total_output > 0 or total_cost > 0.0:
            current = read_token_usage(Path(self.company_dir))
            current["input"] += total_input
            current["output"] += total_output
            current["cost"] += total_cost
            write_token_usage(Path(self.company_dir), current)

    def dispatch(self, assignments, demo=False, demo_delay=0.3, run_id=None):
        """assignments: {emp_id: task}. Spawn matching workers, run to completion live.

        Phase 33 (FIX B / Finding 4 — gate-arming is ENFORCED, not Phoebe's
        discretion): for a REAL dispatch (demo=False), if the plan contains ANY
        builder-duty assignee then the attacker (Gibby) MUST run. Gate-arming
        is no longer "did the plan happen to include both keys" — a plan of
        `{"bob": "build X"}` alone would let a code-touching change ship
        unverified because Phoebe (an LLM fed attacker-influenced $TASK)
        dropped the Gibby key. So if a builder is present and the attacker is
        NOT, the attacker is AUTO-INJECTED (logged) — the plan stays runnable
        and the gate always covers a build. Genuinely non-builder lone-worker
        dispatches (a lone tony/mike research task, or a `--demo` simulate-all
        run) are UNCHANGED — only a BUILDER present forces Gibby.

        FIX 3, SUPERSEDED (finalization pass): a pre-Phase-34 REVISION of this
        method also refused (rc nonzero) code-mutation work routed to a
        non-builder, via a task-text heuristic (`_looks_like_code_mutation`)
        — a defense-in-depth tripwire for the fact that gate-arming keys on
        the builder id being PRESENT, so routing build work to a non-builder
        would silently skip the gate. That refusal is REMOVED: Phase 34's
        per-worker `--disallowedTools` fence (see `Member._resolve_tool_
        profile` / `employee.disallowed_tools_for`) now makes a non-builder
        PHYSICALLY unable to mutate source (Bash/Write/Edit/NotebookEdit are
        structurally absent from its tool schema), so the class of finding
        the heuristic guessed at is closed by construction — and the
        heuristic itself had a real usability cost (false-refusing legitimate
        read tasks like "audit/review Bob's patch to X.py"). Builder-presence
        ARMING (below) is untouched."""
        builder_id, attacker_id = _redblue_pair_ids()
        if demo:
            self.last_gate = None
            return self._dispatch_once(assignments, demo=True, demo_delay=demo_delay)

        builders = _builder_ids()
        builders_present = builders & set(assignments)
        if builders_present:
            armed = dict(assignments)
            # Loop on a builder that is actually present (defaults to bob).
            loop_builder = builder_id if builder_id in armed else sorted(builders_present)[0]
            auto_injected = attacker_id not in armed
            if auto_injected:
                armed[attacker_id] = _AUTO_ATTACK_TASK
                self._emit_autoarm(loop_builder, attacker_id)
            return self._dispatch_redblue(armed, loop_builder, attacker_id,
                                          demo_delay, run_id, auto_injected=auto_injected)
        # No builder in the plan -> genuinely non-red/blue; unchanged path.
        self.last_gate = None
        return self._dispatch_once(assignments, demo=False, demo_delay=demo_delay)

    def _emit_autoarm(self, builder_id, attacker_id):
        """FIX B: a code-touching plan that omitted the attacker had it
        auto-injected — surface it on the event log AND stderr (not just a
        code comment) so a run record shows the gate armed itself. stderr, not
        stdout: stdout is the live TTY render stream."""
        print(f"[supervisor] gate auto-armed: builder '{builder_id}' present but "
              f"attacker '{attacker_id}' absent from plan — injecting '{attacker_id}'",
              file=sys.stderr)
        if self.event_log is not None:
            self.event_log.append({
                "ts": datetime.now().replace(microsecond=0).isoformat(),
                "kind": "redblue_autoarm", "builder": builder_id,
                "attacker": attacker_id})

    def _dispatch_once(self, assignments, demo=False, demo_delay=0.3,
                       extra_contracts=None, verdict_capture_id=None,
                       verdict_nonce=None):
        """The ORIGINAL dispatch body (pre-Phase-33), now the single per-round
        primitive: spawn matching workers, run to completion live, return.
        `extra_contracts` ({emp_id: contract_str}) lets a caller (the red/blue
        gate) append ONE extra output-contract clause to a specific worker's
        prompt (e.g. the verdict sentinel requirement for the attacker) without
        this method special-casing who that worker is.
        `verdict_capture_id` marks WHICH worker's fd is the trusted verdict
        channel (the attacker); only that Worker honors a `@qa-verdict`
        sentinel — attribution by pipe identity (Phase 33 security redesign).
        `verdict_nonce` (finalization pass): the run's secret nonce, set on
        ONLY the `verdict_capture_id` Worker — every other Worker gets None,
        so even if some future caller mistakenly set capture_verdict on more
        than one worker, a non-attacker Worker still can never validate a
        sentinel (no nonce to compare against)."""
        extra_contracts = extra_contracts or {}
        # Item 3: real workers get a wall-clock budget (demo workers stay unbounded —
        # they are trusted local echoes). The budget bounds BOTH the outer
        # `timeout -k` wrap and the in-loop deadline below.
        budget = _dispatch_budget() if not demo else None
        kill_after = _dispatch_kill_after()
        workers = {}
        for emp_id, task in assignments.items():
            emp = self.by_id.get(emp_id)
            if emp is None:
                continue
            if demo:
                cmd = emp.demo_command(task, demo_delay)
                model = None
            else:
                # Only pass `extra_contract` when one is actually set — a
                # test double (or any caller) that monkeypatches
                # `real_command` with the pre-Phase-33 two-arg signature
                # keeps working unchanged for a non-gated dispatch.
                contract = extra_contracts.get(emp_id)
                real_cmd = (emp.real_command(task, extra_contract=contract)
                           if contract else emp.real_command(task))
                cmd = _wrap_timeout(real_cmd, budget, kill_after)
                model = _model_from_cmd(cmd)
            # Real workers get the double-injection guard env for shared_memory_read
            # employees (elon); demo workers just echo, so they inherit unchanged.
            env = None if demo else emp.worker_env()
            is_attacker = (emp_id == verdict_capture_id)
            w = Worker(emp, task, cmd, env=env, budget=budget, model=model,
                       capture_verdict=is_attacker,
                       verdict_nonce=(verdict_nonce if is_attacker else None))
            w.start()
            workers[emp_id] = w
            self._emit(w, "start")
        self.renderer.repaint(workers)

        active = {w.fd: w for w in workers.values()}
        while active:
            ready, _, _ = select.select(list(active), [], [], 0.2)
            for fd in ready:
                w = active[fd]
                lines, eof = w.read_available()
                for line in lines:
                    w.consume_line(line)
                    if line.startswith("@status "):
                        self.renderer.feed(w)
                        self._emit(w, "status")
                if eof:                        # far end closed -> process finished
                    w.on_eof()
                    del active[fd]
                    self._emit(w, "end")
                self.renderer.repaint(workers)
            # Item 3: per-worker wall-clock deadline. A stalled worker that never
            # reaches EOF (no output, ignores signals) would wedge this loop —
            # and the session — forever. The 0.2s select timeout means we reach
            # here even when nothing is ready, so we can kill+reap any worker past
            # budget, mark it FAILED, and drop it from the active set so dispatch
            # returns cleanly. A killed worker renders as failed, never hung.
            for fd in list(active):
                w = active[fd]
                if w.over_budget():
                    w.kill_over_budget()
                    del active[fd]
                    self._emit(w, "end")
                    self.renderer.repaint(workers)
        self.renderer.final(workers)
        self._accumulate_usage(workers)
        return workers

    def _dispatch_redblue(self, assignments, builder_id, attacker_id, demo_delay,
                          run_id, auto_injected=False):
        """Phase 33 Item 2: the bounded builder+attacker re-loop. Runs up to
        `_redblue_max_rounds()` rounds; after each attacker run, reads the
        verdict from the ATTACKER WORKER'S OWN stdout (its `@qa-verdict`
        sentinel, captured off its pipe fd — never a shared-fs file Bob could
        forge) and stops the instant it's clean. Never reaches the cap without
        recording UNRESOLVED — this method NEVER returns silently on an
        unresolved cap; `self.last_gate` always ends up populated so a caller
        (CLI, tests) can tell.

        FIX C (Finding 5): any THIRD-PARTY assignee (`other`) is dispatched on
        ROUND 1 ONLY — a fix/re-attack round is strictly (builder, attacker).
        Before, `other` was re-seeded every round, so a bob+gibby+tony plan
        that never cleared re-ran tony once PER round.

        FINALIZATION: one secret verdict `nonce` is minted here PER GATE RUN
        (not per round — Bob never learns it regardless, since it only ever
        goes into GIBBY's prompt via `_verdict_contract`, and Bob's prompt is
        built completely separately; reusing it across rounds costs nothing
        and mirrors `run_id`'s own once-per-run scope) and threaded through
        every round's `_dispatch_once` call as `verdict_nonce` — only the
        attacker Worker ever receives it, so only a sentinel on Gibby's own fd
        carrying THIS exact nonce can ever satisfy the gate.

        Item 3 (2026-07-21 robustness follow-up): each round's verdict is
        also tagged with the attacker Worker's `nonce_format_miss` flag, so
        the cap-reached `self.last_gate` can carry a `reason`
        (`_unresolved_reason`) distinguishing a GENUINE fail from Gibby never
        landing a recognizable pass/fail on its authenticated fd at all —
        surfaced to `main()`'s UNRESOLVED message and the ledger."""
        max_rounds = _redblue_max_rounds()
        other = {k: v for k, v in assignments.items()
                 if k not in (builder_id, attacker_id)}
        builder_task = assignments[builder_id]
        attacker_task = assignments[attacker_id]
        run_id = run_id or f"{int(time.time())}-{secrets.token_hex(3)}"
        nonce = secrets.token_hex(8)
        contract = _verdict_contract(nonce)

        workers = {}
        verdict = {"clean": False, "verdict": "missing"}
        rounds_used = 0
        for round_no in range(1, max_rounds + 1):
            rounds_used = round_no
            round_assignments = {}
            if round_no == 1:
                round_assignments.update(other)          # FIX C: round 1 ONLY
                round_assignments[builder_id] = builder_task
                round_assignments[attacker_id] = attacker_task
            else:
                round_assignments[builder_id] = (
                    f"{builder_task}\n\nRound {round_no} FIX: Gibby's previous "
                    f"verdict was '{verdict.get('verdict')}'. Fix what it found "
                    f"before handing back to Gibby.")
                round_assignments[attacker_id] = (
                    f"{attacker_task}\n\nRe-attack round {round_no}: Bob just applied "
                    f"a fix for the previous round's '{verdict.get('verdict')}' "
                    f"verdict. Verify the fix AND regress the earlier finding.")
            workers = self._dispatch_once(
                round_assignments, demo=False, demo_delay=demo_delay,
                extra_contracts={attacker_id: contract},
                verdict_capture_id=attacker_id, verdict_nonce=nonce)
            # Read the verdict off the ATTACKER'S OWN pipe (trusted channel).
            attacker_worker = workers.get(attacker_id)
            raw = attacker_worker.verdict if attacker_worker else None
            verdict = classify_verdict(raw)
            format_miss = bool(attacker_worker and attacker_worker.nonce_format_miss)
            self._emit_gate_round(run_id, round_no, verdict, format_miss)
            if verdict["clean"]:
                self.last_gate = {"run_id": run_id, "rounds": rounds_used,
                                  "verdict": "clean", "builder": builder_id,
                                  "attacker": attacker_id, "auto_injected": auto_injected}
                return workers

        # Cap reached without a clean verdict — fail LOUD, never silent-done
        # (spec §2/§3). The trusted record is `self.last_gate` (in-process) +
        # the `@redblue-gate` line main() prints on the supervisor's own
        # stderr; there is NO shared-fs marker a worker could forge (FIX 2).
        # Item 3: `reason` is derived from the LAST round's verdict/format-miss
        # pair — distinguishing a genuine fail from a sentinel-format miss
        # from Gibby never producing an authenticated verdict at all, so
        # `main()`'s UNRESOLVED message and the ledger can tell a human which
        # one actually happened.
        self.last_gate = {"run_id": run_id, "rounds": rounds_used,
                          "verdict": "unresolved",
                          "reason": _unresolved_reason(verdict, format_miss),
                          "builder": builder_id,
                          "attacker": attacker_id, "auto_injected": auto_injected}
        return workers

    def _emit_gate_round(self, run_id, round_no, verdict, format_miss=False):
        if self.event_log is not None:
            self.event_log.append({
                "ts": datetime.now().replace(microsecond=0).isoformat(),
                "kind": "redblue_round", "run_id": run_id, "round": round_no,
                "verdict": verdict.get("verdict"), "clean": verdict.get("clean"),
                "format_miss": format_miss,
            })


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default=".company")
    ap.add_argument("--demo", action="store_true", help="simulate all employees live")
    ap.add_argument("--demo-delay", type=float, default=0.3)
    ap.add_argument("--dispatch", help="JSON {emp_id: task} — spawn real agents")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    sup = Supervisor(args.company)
    if args.list:
        print(json.dumps({"roster": [e.id for e in sup.roster]}))
        return 0
    if args.demo:
        sup.dispatch({e.id: "demo task" for e in sup.roster},
                     demo=True, demo_delay=args.demo_delay)
        return 0
    if args.dispatch:
        sup.dispatch(json.loads(args.dispatch), demo=False)
        gate = sup.last_gate
        # FIX 2: the gate result is emitted on the supervisor's OWN stderr as a
        # `@redblue-gate {json}` line — the trusted channel company-run.sh reads
        # for the ledger (NOT a worker-forgeable file). Printed for EVERY armed
        # outcome (clean / unresolved); a non-armed dispatch leaves last_gate
        # None and prints nothing (ledger shows the "-" placeholder).
        if gate:
            print(_GATE_RESULT_SENTINEL + json.dumps(gate), file=sys.stderr)
        verdict = gate.get("verdict") if gate else None
        # Fail LOUD, never silent-done.
        if verdict == "unresolved":
            reason = gate.get("reason", "no_verdict")
            detail = _UNRESOLVED_REASON_TEXT.get(reason, reason)
            print(f"[supervisor] UNRESOLVED ({reason}): {gate['builder']}/"
                  f"{gate['attacker']} did not clear verification in "
                  f"{gate['rounds']} round(s) — {detail} (run_id={gate['run_id']})",
                  file=sys.stderr)
            return 1
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
