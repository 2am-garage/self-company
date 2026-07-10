#!/usr/bin/env bash
###############################################################################
# hook_memory_guard.sh — PreToolUse gate (Phase 10, Item 6; hardened Phase 22,
# Phase 26 Item 3, Phase 27 Item C2).
#
# Denies any Bash command that would PHYSICALLY delete or move-away a path under
# .company/memory/ — OR the .company store ROOT (rm -rf .company wipes memory too)
# — via rm / unlink / shred / rmdir / truncate / `find <mem> -delete` / `mv
# <memory-path> elsewhere` / a truncating shell redirect (`>`, `>|`, `&>`, `>&`)
# / bare `tee` (no `-a`).
# Physical deletion of memory is the deterministic decay reap's job (Phase 6);
# agents must TOMBSTONE (status: archived) instead. Skill-owned, host-independent
# enforcement of the no-rm rule — defense in depth beside the tar floor.
#
# Phase 27 C2 (GIB R3 backlog) — `>` truncation is a deletion primitive the
# guard didn't see. `echo x > .company/memory/foo.md` (or `: >`, `>|`, `tee`
# without `-a`) truncates a memory file to nothing and used to sail straight
# through: _normalize() collapsed EVERY punctuation run — `>`/`>>`/`>|`/`&>`/
# `>&` included — into one generic ';' separator before any deleter check ever
# ran, throwing away the truncate-vs-append-vs-separator distinction. Fix:
# every redirect operator is now preserved as its own distinct token; a bare
# `>`/`>|`/`&>`/`>&` (never the `>>`/`&>>` append forms — MUST-FIX 2 added the
# `&>`/`>&` both-streams truncating forms the first pass missed) or a `tee`
# invocation without `-a`/
# `--append` whose target resolves into the store — same fail-closed
# resolution as every deleter above (cd-tracked CWD stack, is_mem_literal
# textual backstop, in_store realpath check) — is DENIED. The nested-shell
# decisive-deny rule is extended the same way, but stays TARGET-AWARE for
# redirects/tee (unlike rm/mv's blanket nested-shell rule): `>`/`tee` are
# common, legitimate primitives for files that have nothing to do with the
# store, so only a store-shaped target trips it. No new binaries enumerated
# (`dd of=`, `python -c open(...,'w')`, etc. stay out of scope per Elon — the
# nested-shell fail-closed rule already covers the interpreter route).
#
# Phase 26 Item 3 — close the `cd` bypass. The Phase-22 guard matched only the
# command's own literal TOKENS ("does this string look like .company/memory"),
# so `cd .company/memory && rm -rf L0-working/*` sailed straight through: the
# rm's own argument never mentions ".company" at all. RESOLVE, DON'T TRACK: we
# read the hook input's `cwd` (the harness's real, persisted working directory
# at the moment this command is about to run) and realpath-resolve every
# deleter path argument against it — following symlinks and `..` traversal —
# rather than pattern-matching literal text. A single command that itself
# contains a `cd` (e.g. `cd .company && rm -rf memory`) is handled by
# simulating that `cd` across the command's own segments, in order, starting
# from the real hook cwd. If a `cd` target can't be resolved with confidence
# (a shell variable, command substitution, or glob) we don't know the
# effective cwd for anything after it — FAIL CLOSED for any later RELATIVE
# deleter argument (a legitimate command can always be re-run with an absolute
# path outside the store; this guard is deleters-only, so the false-positive
# cost is near zero). The Phase-22 literal-text check is KEPT as a fast,
# resolution-independent backstop (it also catches, deliberately broadly, ANY
# absolute path that merely *looks* like a `.company` root, even one outside
# THIS project — the original conservative choice) — either check tripping is
# enough to deny.
#
# GIB re-attack (MUST-FIX 2) — cd-equivalents & wrapper structure. The naive
# raw-text segment splitter tore `bash -c "…"` and `( … )` apart before
# tokenizing, and only the literal token `cd` was treated as a directory
# change, so `pushd` / `env -C` / subshell / `bash -c` walked a deleter into
# the store undetected. The parser now (1) tokenizes QUOTE- and PAREN-aware
# (shlex punctuation_chars) so quoted scripts stay intact and control
# operators/subshells are real boundaries; (2) treats `pushd` and `env -C` /
# `--chdir` as cd-equivalents and scopes subshell cd with a stack; and (3)
# fail-closes any deleter it cannot fully resolve — a nested `bash -c` / `eval`
# / `xargs` whose script invokes a deleter, or a wholly-unparseable
# (unbalanced-quote) command that still mentions a deleter — is DENIED rather
# than silently allowed.
#
# GIB R2 — DECISIVELY fail-closed (stop the spelling whack-a-mole). Two blunt,
# comprehensive rules replace per-spelling handling:
#   (a) TRANSPARENT PREFIXES: `command` / `builtin` / `time` / `exec` / `nohup`
#       / `nice` / `env` are stripped RECURSIVELY to find the real command, so
#       a `cd` behind any stack of them (`command builtin cd …`) still updates
#       the simulated cwd.
#   (b) NESTED SHELL + ANY DELETER = DENY: if the command invokes a nested shell
#       (`bash/sh/zsh/dash/ksh -c`, `eval`, `xargs`, `source`/`.`) AND a deleter
#       token (rm/rmdir/unlink/shred/truncate/find…-delete) appears ANYWHERE at
#       ANY nesting depth, DENY without trying to prove the buried payload safe.
#       This subsumes arbitrarily-nested `bash -c "bash -c '…rm…'"` in one rule.
# Net: a deleter that reaches the store, OR a deleter alongside a nested shell /
# unresolvable cd -> DENY. Only a fully-parseable, cwd-resolved command whose
# every deleter target provably lands OUTSIDE the store -> ALLOW.
#
# HONEST SCOPE (per Elon): this is a best-effort DEFENSE-IN-DEPTH TRIPWIRE
# against an injected/mistaken assistant, NOT a hard security boundary. A
# determined adversary with shell access has many ways to remove files the
# static analysis of a single command string can't all foresee (write a helper
# script and run it, base64-decode a payload, etc.). The REAL recovery
# mechanism is the tar-snapshot floor (pre-mutation `ops/backups/` snapshots,
# policy §7.8). So a truly exotic residual evasion is ACCEPTED, not a merge
# blocker; the tripwire raises the cost of the common/accidental cases.
#
# CONTRACT (PreToolUse): reads stdin JSON {tool_name, tool_input:{command}, cwd}.
#   DENY : exit 0, stdout {"hookSpecificOutput":{"hookEventName":"PreToolUse",
#          "permissionDecision":"deny","permissionDecisionReason":"..."}}
#   ALLOW: same shape with "permissionDecision":"allow".
# Exit 0 + deny (NOT exit 2) so the reason surfaces cleanly to the user.
#
# FAIL-OPEN: opt-in guard first (no .company -> silent exit 0); any parse error
# -> allow. A hook bug must NEVER block a legitimate command. (The Item-3
# cd-ambiguity rule above is fail-CLOSED, but only within the narrow deleter
# scope this hook already governs — it never blocks a non-deleting command.)
###############################################################################
set -uo pipefail

# --- Opt-in guard: inert in any repo without a self-company .company/ -----------
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
if [[ ! -d "$PROJECT_DIR/.company" ]]; then
  exit 0
fi

# Slurp the hook stdin (kept in a var so we can hand it to python3 explicitly and
# avoid depending on jq for JSON parsing).
INPUT="$(cat)"

# Decision logic lives in python3 (stdlib json + shlex + os.path) for robust
# tokenizing and path resolution. PROJECT_DIR is passed via env (the heredoc is
# single-quoted, so no bash interpolation/injection risk).
export SC_PROJECT_DIR="$PROJECT_DIR"

read -r -d '' PYCODE <<'PY' || true
import sys, json, shlex, os, re

PROJECT_DIR = os.environ.get("SC_PROJECT_DIR", "")
STORE = os.path.realpath(os.path.join(PROJECT_DIR, ".company")) if PROJECT_DIR else ""
MEM_ROOT = os.path.join(STORE, "memory") if STORE else ""
MEM_SUBSTR = ".company/memory/"

DELETERS = {"rm", "unlink", "shred", "rmdir", "truncate"}
CD_CMDS = {"cd", "pushd"}                       # GIB re-attack: pushd is a cd too
SHELL_INTERPRETERS = {"bash", "sh", "dash", "zsh", "ksh", "ash", "fish"}
NESTED_WRAPPERS = {"eval", "xargs"}             # run another command we can't resolve
# GIB R2 — transparent wrapper prefixes: they run the REAL command that follows
# (and, for the shell-builtin cd, in the SAME shell, so a `cd` behind them still
# persists). Strip them RECURSIVELY to find the real head. `env` is here too but
# handled specially (its -C/--chdir is non-persistent) in check_command.
TRANSPARENT_PREFIX = {"command", "builtin", "time", "exec", "nohup", "nice", "env"}
# GIB R2 — a nested-shell indicator anywhere in the command. If one is present
# AND a deleter token appears anywhere (any nesting depth), we do NOT try to
# prove the payload safe — we DENY. Decisive fail-closed, no spelling whack-a-mole.
NESTED_INDICATORS = SHELL_INTERPRETERS | {"eval", "xargs", "source"}
AMBIG_CHARS = set("$`*?[]")
PUNCT = set("();<>|&")                          # shell control punctuation
# Phase 27 C2 (GIB R3 backlog) + MUST-FIX 2 — `>`/`>|`/`&>`/`>&` truncate their
# target to zero bytes ON OPEN, before anything is written: that IS deletion of
# the prior contents, not merely "not rm". `&>`/`>&` are the both-stdout+stderr
# truncating forms (`echo x &> f` / `echo x >& f` both truncate `f`), which the
# original C2 pass missed — Gibby live-fired `echo X &> .company/memory/…` and
# it truncated the file undetected. The APPEND forms `>>`/`&>>` are excluded on
# purpose (append is not deletion). All are shell OPERATORS (recognized by
# token identity), never program names, so they never go through the
# head/DELETERS dispatch — they're checked directly wherever they appear in a
# simple command. (`2>&1`-style fd dups tokenize with a NUMERIC target, which
# is never a store path, so they stay allowed by the target-aware check.)
REDIR_TRUNC = {">", ">|", "&>", ">&"}
REDIR_APPEND = {">>", "&>>"}                    # append — allowed, but preserved as tokens
REDIR_OPS = REDIR_TRUNC | REDIR_APPEND          # all preserved through _normalize


def allow():
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "no memory-destructive operation detected",
    }}))
    sys.exit(0)


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def is_mem_literal(tok):
    # Phase 22 backstop: a path token that TEXTUALLY looks like the memory
    # store (relative or absolute), independent of any cwd resolution.
    # Deliberately broad: ANY absolute path ending in "/.company" is treated
    # as a store root (even one that isn't literally this project's, since
    # deleting a whole .company store wholesale is dangerous in general).
    t = tok.strip().strip('"').strip("'").rstrip("/")
    if MEM_SUBSTR in tok or t.endswith(".company/memory"):
        return True
    return t == ".company" or t.endswith("/.company")


def resolve(base, arg):
    p = arg if os.path.isabs(arg) else os.path.join(base or "/", arg)
    return os.path.realpath(p)


def in_store(path):
    # path is already realpath'd. Scope matches the original guard exactly:
    # the store ROOT itself, or anything AT/UNDER the memory subtree — not
    # every other subdir of .company (e.g. .company/ops is out of scope).
    if not STORE:
        return False
    return path == STORE or path == MEM_ROOT or path.startswith(MEM_ROOT + os.sep)


REASON = ("physical deletion of memory is the deterministic decay reap's job "
          "(Phase 6) — tombstone instead (status: archived)")
REDIR_REASON = (
    "a shell redirect/`tee` truncates its target to zero bytes on open, "
    "before anything new is written — that IS physical deletion of the "
    "prior contents, the same as rm (Phase 6: tombstone instead, status: "
    "archived). Use `>>`/`tee -a` to append, or write outside .company/memory")
AMBIG_REASON = (
    "a `cd` earlier in this command lands somewhere this guard can't resolve "
    "(a variable/substitution/glob target), so a RELATIVE deletion target "
    "after it can't be proven safe — fail-closed. Re-run with an absolute "
    "path outside .company, or split the cd and the delete into separate "
    "steps")
STRUCT_REASON = (
    "a deleter is wrapped in structure this narrow guard can't fully parse "
    "(nested shell -c / eval / xargs / subshell / unbalanced quoting), so its "
    "target can't be proven to lie outside the memory store — fail-closed "
    "(GIB re-attack). Re-run the deletion directly with an absolute path "
    "outside .company")


def _basename(t):
    return t.rsplit("/", 1)[-1]


def _ambiguous(t):
    return any(ch in t for ch in AMBIG_CHARS)


def tok_is_deleter(a):
    b = _basename(a)
    return b in DELETERS or b in ("find", "mv")


# Phase 27 C2 — nesting-and-quote-agnostic raw-text scan for a truncating
# redirect or bare `tee`. Mirrors _all_words's approach for raw_has_deleter
# (regex over the ENTIRE raw text, quotes and all — we do not recursively
# simulate a nested shell's cwd, so this looks for the deleter-EQUIVALENT
# signal at any nesting depth). UNLIKE rm/mv's blanket "nested shell + deleter
# ANYWHERE = deny" rule, this stays TARGET-AWARE: `>`/`tee` are common,
# legitimate shell primitives for files that have nothing to do with the
# store (a nested `bash -c "make > /tmp/log"` must stay allowed), so only a
# store-shaped target counts as a hit. `>>` is excluded (never a hit); `tee
# -a`/`--append` anywhere in the same tee invocation's tail is treated as
# non-truncating for that invocation.
# MUST-FIX 2: every redirect operator (append + truncating), longest-first so
# `&>>`/`>>` win over `&>`/`>`, each captured with the token that follows it.
# We classify in code (append forms skipped) rather than one clever lookaround —
# the alternation ordering is what makes `&>>` not read as `&>` + `>`.
_REDIR_OP_RE = re.compile(r"(&>>|>>|&>|>&|>\||>)\s*([^\s;&|()<>]*)")
_TEE_INVOCATION_RE = re.compile(r"\btee\b([^;&|()]*)")


def text_has_store_redirect(s):
    if not isinstance(s, str) or not s:
        return False
    for m in _REDIR_OP_RE.finditer(s):
        op, target = m.group(1), m.group(2)
        if op in (">>", "&>>"):
            continue                    # append is not deletion
        if target and is_mem_literal(target):
            return True
    for m in _TEE_INVOCATION_RE.finditer(s):
        tail = m.group(1).split()
        if any(a in ("-a", "--append") for a in tail):
            continue
        if any(is_mem_literal(a) for a in tail if not a.startswith("-")):
            return True
    return False


def script_has_deleter(s):
    # Best-effort: does an inline script string invoke a deleter? Used to
    # fail-closed on `bash -c "…rm…"` / `eval "…"` where we won't recursively
    # simulate the nested cwd. shlex-tokenize when we can, else scan words.
    if not isinstance(s, str):
        return False
    try:
        toks = shlex.split(s)
    except ValueError:
        toks = re.findall(r"[A-Za-z0-9_./-]+", s)
    return (any(tok_is_deleter(t) or _basename(t) in NESTED_WRAPPERS for t in toks)
            or text_has_store_redirect(s))


def text_has_hard_deleter(s):
    # For a wholly-unparseable command (unbalanced quotes): is a hard deleter
    # even present? If so, fail-closed; if not, there is nothing to guard.
    words = re.findall(r"[A-Za-z0-9_./-]+", s or "")
    return any(_basename(w) in DELETERS for w in words) or text_has_store_redirect(s)


def _all_words(s):
    # Quoting-AGNOSTIC scan: we WANT to see inside quotes (a deleter buried in a
    # nested `bash -c "…"` string must still be counted). Returns basenames.
    return [_basename(w) for w in re.findall(r"[A-Za-z0-9_./-]+", s or "")]


def raw_has_deleter(s):
    # Any deleter token anywhere in the raw command string, at any nesting depth.
    # `mv` counts too (GIB R3): a `mv`-AWAY is a disguised delete, treated as a
    # deleter by tok_is_deleter + the per-command branch — without it here, a
    # DEPTH>=2 nested `bash -c "bash -c 'mv .company/memory … /tmp'"` slipped the
    # nested-shell net (single-level mv already denied). No new false positives:
    # a legit mv can use absolute paths or run un-nested.
    words = set(_all_words(s))
    if words & DELETERS or "mv" in words:
        return True
    if "find" in words and "-delete" in (s or ""):
        return True
    # Phase 27 C2: a truncating redirect (`>`/`>|`) or bare `tee` whose
    # target textually looks like the store is a deleter-equivalent too —
    # see text_has_store_redirect for why this stays target-aware.
    if text_has_store_redirect(s):
        return True
    return False


def has_nested_shell(s):
    # A shell interpreter invocation (bash/sh/… -c), eval, xargs, or source —
    # anywhere. Whole-word (basename) match so `refresh.sh`/`ssh`/`myeval` don't
    # trip it. Also the bare-dot `.` source builtin used as a command.
    if set(_all_words(s)) & NESTED_INDICATORS:
        return True
    return bool(re.search(r"(?:^|[;&|(]\s*)\.\s", s or ""))

try:
    data = json.load(sys.stdin)
except Exception:
    allow()  # unparseable stdin -> never block

# Matcher is Bash(rm *); if some other tool slips through, don't touch it.
if not isinstance(data, dict) or data.get("tool_name") != "Bash":
    allow()

cmd = ((data.get("tool_input") or {}) if isinstance(data.get("tool_input"), dict) else {}).get("command") or ""
if not isinstance(cmd, str) or not cmd.strip():
    allow()

# GIB R2 — DECISIVE fail-closed rule (stop the spelling whack-a-mole). If the
# command wraps a nested shell (`bash/sh/… -c`, `eval`, `xargs`, `source`, `.`)
# AND a deleter token appears ANYWHERE at ANY nesting depth, we do NOT try to
# prove the buried payload targets outside the store — we DENY. This subsumes
# arbitrarily-nested `bash -c "bash -c '…rm…'"` and every future shell-wrapping
# spelling in one rule. False positives are cheap here (a legit deleter can
# always use an absolute path outside .company, and no legit workflow buries a
# store-deleting rm inside a nested shell in a self-company repo).
# Phase 27 C2: raw_has_deleter ALSO counts a truncating redirect (`>`/`>|`) or
# bare `tee` whose target textually looks like the store — UNLIKE rm/mv this
# stays target-aware (see text_has_store_redirect), because `>`/`tee` are
# common, legitimate primitives for files that have nothing to do with the
# store; only a store-shaped target trips this.
if has_nested_shell(cmd) and raw_has_deleter(cmd):
    deny(STRUCT_REASON)

# Item 3: the harness's own persisted cwd at the moment this command is about
# to run — the ground truth a `cd` in a PRIOR, separate tool call already
# updated. Falls back to PROJECT_DIR (the pre-Phase-26 assumption) if absent.
hook_cwd = data.get("cwd")
if not isinstance(hook_cwd, str) or not hook_cwd.strip():
    hook_cwd = PROJECT_DIR
if hook_cwd and not os.path.isabs(hook_cwd):
    hook_cwd = os.path.join(PROJECT_DIR or "/", hook_cwd)
cur_cwd = os.path.realpath(hook_cwd) if hook_cwd else None

# GIB re-attack (MUST-FIX 2): tokenize QUOTE- and PAREN-aware so a deleter
# hidden inside `bash -c "…"`, a subshell `( … )`, or `env -C …` can't be torn
# apart at raw-text level before we ever see it. shlex(posix, punctuation_chars)
# keeps quoted strings intact as single tokens AND surfaces the control
# operators; unbalanced quoting is unparseable -> fail-closed if a deleter is
# even present.
try:
    _lx = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    _lx.whitespace_split = True
    raw_tokens = list(_lx)
except ValueError:
    if text_has_hard_deleter(cmd):
        deny(STRUCT_REASON)            # can't parse + a deleter is present
    allow()


def _normalize(tokens):
    # A run of punctuation chars may lex as one token (e.g. ');'). Split it into
    # atoms: '(' and ')' individually (subshell scoping); a truncating/append
    # redirect (`>`, `>|`, `>>`) preserved AS ITS OWN DISTINCT token (Phase 27
    # C2 — collapsing `>`/`>>`/`>|` into the same generic ';' as every other
    # separator would throw away the truncate-vs-append-vs-separator
    # distinction before any deleter check ever runs, which is exactly how a
    # `>`/`tee` truncation of a memory file sailed through undetected); every
    # OTHER operator (&& || | & ; <) collapsed to a single ';' separator — all
    # we need there is "flush the current simple command here". shlex's
    # punctuation_chars tokenizer already hands `>`/`>>`/`>|` back as whole,
    # un-merged tokens in every realistic (whitespace-adjacent-to-a-target)
    # case, so the whole-token check below is enough; the rare degenerate
    # merge (e.g. a target-less `>);` with no filename at all) falls through
    # to the old collapse path unchanged — there's no resolvable target to
    # check in that case anyway.
    out = []
    for tok in tokens:
        if tok in REDIR_OPS:
            out.append(tok)
            continue
        if tok and all(c in PUNCT for c in tok):
            pending = False
            for ch in tok:
                if ch in "()":
                    if pending:
                        out.append(";"); pending = False
                    out.append(ch)
                else:
                    pending = True
            if pending:
                out.append(";")
        else:
            out.append(tok)
    return out


toks_norm = _normalize(raw_tokens)

# CWD[0] is the simulated cwd; a stack scopes subshell `( … )` cd so it doesn't
# leak out (avoids false positives after the subshell closes).
CWD = [cur_cwd]
cwd_stack = []


def check_targets(paths, cwd, reason=REASON):
    for a in paths:
        if a.startswith("-"):
            continue                   # a flag (e.g. -rf), never a path arg
        if is_mem_literal(a):
            deny(reason)
        if cwd is None:
            if not os.path.isabs(a):
                deny(AMBIG_REASON)      # relative + unknown cwd -> fail closed
            resolved = os.path.realpath(a)
        else:
            resolved = resolve(cwd, a)
        if in_store(resolved):
            deny(reason)


def _cd_target(args):
    positional = [a for a in args if not a.startswith("-")]
    return positional[0] if positional else "~"


def _apply_cd(cwd, target):
    if cwd is None:
        return None                    # already ambiguous; stays ambiguous
    if _ambiguous(target):
        return None                    # variable/substitution/glob -> unknown
    return resolve(cwd, os.path.expanduser(target))


def _parse_env(rest):
    # `env [-i] [-u VAR] [VAR=val…] [-C DIR | --chdir DIR|=DIR] CMD ARGS…`
    # -> (cd_target_or_None, inner_command_tokens). The chdir is NON-persistent
    # (only the wrapped command sees it).
    cd_target = None
    j = 0
    while j < len(rest):
        t = rest[j]
        if t in ("-C", "--chdir"):
            if j + 1 < len(rest):
                cd_target = rest[j + 1]; j += 2; continue
            j += 1; continue
        if t.startswith("--chdir="):
            cd_target = t.split("=", 1)[1]; j += 1; continue
        if t == "-u":
            j += 2; continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            j += 1; continue           # VAR=val assignment
        if t.startswith("-"):
            j += 1; continue           # -i and other flags
        break                          # first bare word = the wrapped command
    return cd_target, rest[j:]


def _strip_prefix_opts(head, rest):
    # Skip a transparent prefix's OWN leading options so we land on the REAL
    # command (`nice -n 10 rm …` -> `rm …`, `command -p rm …` -> `rm …`).
    j = 0
    while j < len(rest):
        t = rest[j]
        if head == "nice" and t == "-n":
            j += 2; continue           # nice -n <adjustment>
        if t.startswith("-"):
            j += 1; continue
        break
    return rest[j:]


def check_command(toks, cwd):
    """Analyze one simple command. Returns the (persistently) updated cwd; may
    deny() and never return."""
    # Phase 27 C2 + MUST-FIX 2 — a truncating redirect (`>`/`>|`/`&>`/`>&`,
    # never the `>>`/`&>>` append forms) attaches to THIS simple command
    # regardless of where in it the operator appears (bash allows
    # leading/interspersed redirects, e.g. `> file cmd args`) and regardless
    # of which program is invoked: the shell opens/truncates the target before
    # the program ever runs. Same fail-closed resolution
    # (cd-tracked cwd, is_mem_literal backstop, in_store realpath check) as
    # every deleter target below.
    redir_targets = [toks[j + 1] for j, t in enumerate(toks)
                      if t in REDIR_TRUNC and j + 1 < len(toks)]
    if redir_targets:
        check_targets(redir_targets, cwd, reason=REDIR_REASON)

    i = 0
    while i < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]):
        i += 1                         # skip leading VAR=val
    if i >= len(toks):
        return cwd
    head = _basename(toks[i])
    args = toks[i + 1:]

    if head in CD_CMDS:
        return _apply_cd(cwd, _cd_target(args))

    # GIB R2 — transparent wrapper prefixes. `env` is special (its chdir is
    # NON-persistent); the rest (`command`/`builtin`/`time`/`exec`/`nohup`/
    # `nice`) run the following command in the SAME shell, so a `cd` behind them
    # persists — strip and RE-DISPATCH on the real head, returning its cwd. This
    # is recursive, so `command builtin cd …` unwraps fully.
    if head == "env":
        cd_t, inner = _parse_env(args)
        eff = cwd
        if cd_t is not None:
            eff = _apply_cd(cwd, cd_t)
        check_command(inner, eff)      # non-persistent chdir -> discard result
        return cwd
    if head in TRANSPARENT_PREFIX:
        return check_command(_strip_prefix_opts(head, args), cwd)

    if head in SHELL_INTERPRETERS:
        # A nested shell (`bash -c "cd … && rm …"`) we won't recursively
        # simulate — if its script invokes a deleter, fail-closed.
        if any(script_has_deleter(a) for a in args):
            deny(STRUCT_REASON)
        return cwd

    if head in NESTED_WRAPPERS:        # eval / xargs run a command we can't resolve
        if any(tok_is_deleter(a) for a in args) or \
                any(script_has_deleter(a) for a in args):
            deny(STRUCT_REASON)
        return cwd

    if head in DELETERS:
        check_targets(args, cwd)
    elif head == "find" and "-delete" in args:
        # `find <memory> -delete` is a disguised recursive rm. Deny ONLY when
        # -delete is present so a plain read-only `find` over memory still works.
        check_targets([a for a in args if not a.startswith("-")], cwd)
    elif head == "mv":
        # Moving a memory path AWAY = a disguised delete. Deny if a memory path
        # appears as any source (i.e. not solely the final destination arg).
        positional = [a for a in args if not a.startswith("-")]
        sources = positional[:-1] if len(positional) >= 2 else positional
        check_targets(sources, cwd)
    elif head == "tee":
        # Phase 27 C2 — `tee TARGET` truncates TARGET on open, same as `>`;
        # `tee -a`/`--append` TARGET appends (not deletion). No other binary
        # is special-cased here (Elon: enumerating binaries is the
        # special-casing this guard avoids; `dd of=`, `python -c open(...,
        # 'w')`, etc. are out of scope — the nested-shell fail-closed rule
        # already covers the interpreter route).
        appended = any(a in ("-a", "--append") for a in args)
        if not appended:
            check_targets([a for a in args if not a.startswith("-")], cwd,
                          reason=REDIR_REASON)
    return cwd


simple = []


def _flush():
    if simple:
        CWD[0] = check_command(list(simple), CWD[0])
    simple.clear()


for tok in toks_norm:
    if tok == "(":
        _flush()
        cwd_stack.append(CWD[0])       # enter subshell — scope its cd
    elif tok == ")":
        _flush()
        if cwd_stack:
            CWD[0] = cwd_stack.pop()    # restore: subshell cd doesn't leak out
    elif tok == ";":
        _flush()
    else:
        simple.append(tok)
_flush()

allow()
PY

printf '%s' "$INPUT" | python3 -c "$PYCODE"
# Any failure to even launch python3 -> fail open (allow implicitly).
rc=$?
if [[ $rc -ne 0 ]]; then
  # python3 unavailable or crashed: emit an explicit allow, never block.
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"guard fell open"}}'
fi
exit 0
