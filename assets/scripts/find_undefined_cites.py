#!/usr/bin/env python3
"""find_undefined_cites — seed the add-bib-references worklist from the source tree.

Part of the add-bib-references formula (latex-utils pack). Mechanical Tier-1 discovery
(+ Tier-2 flag-and-defer): the agent does the judgment (identify/classify/draft each
entry); this helper only enumerates WHICH keys need an entry.

What it does:
  * Reads the master source (`config.master_source`), inline-expands its
    \\input/\\include/\\subfile tree (reusing inspect_sources), and collects every
    citation key referenced by the \\cite family — \\cite \\citep \\citet \\citeauthor
    \\citeyear \\textcite \\parencite \\autocite \\footcite … and the multicite forms
    (\\cites{a}{b}) — honouring comma-separated groups and skipping comment/verbatim
    regions.
  * Collects the union of keys DEFINED across the resolved .bib file(s) (reusing
    merge_bib's parser).
  * Worklist = keys referenced but not defined. When `vars.references` is set, the
    worklist is exactly those keys instead (a listed ref is still included even if it
    is already defined or not yet \\cite'd — the author may not have cited it yet).
  * Optional cross-check: parse a compile `.log` for verify.py's
    'Citation `key' … undefined' lines; a log key the source scan missed is flagged
    and still added to the worklist.

The worklist is merged into plan.json idempotently by key: existing entries (and any
human-set status/classification/citekey/…) are preserved; only missing keys are added
as `pending`.

CLI:
  find_undefined_cites.py --plan PLAN
      [--master-source FILE]    # override config.master_source
      [--bib FILE ...]          # override the resolved .bib union (repeatable)
      [--references K1,K2,…]     # override vars.references (empty => all undefined)
      [--log FILE]              # engine .log for the 'Citation … undefined' cross-check

`--master-source` is required only when PLAN does not exist yet or its
config.master_source is empty (so the helper is runnable standalone against a fixture).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planlib  # noqa: E402
import inspect_sources  # noqa: E402
import merge_bib  # noqa: E402
import texrewrite  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 1  # usage / environment (missing input) — not a discovery failure

# verify.py's per-occurrence undefined-citation phrase: 'Citation `key' on page N … undefined'.
UNDEF_CITE_RE = re.compile(r"Citation [`']([^'`]+)['`]")

# The flag kinds this helper owns; dropped + re-derived each run so a re-run does not
# accumulate duplicates (reusing merge_bib's stable-id plumbing, the pack's convention).
MY_FLAG_KINDS = {"unresolved-include", "bib-missing", "bib-parse-error",
                 "log-missing", "cite-scan-miss"}


# --------------------------------------------------------------------------- #
# \cite-family enumeration (reads keys; merge_bib rewrites them).               #
# --------------------------------------------------------------------------- #

def _in_spans(pos: int, spans) -> bool:
    """True if ``pos`` falls inside any protected (comment/verbatim) span."""
    for a, b in spans:
        if a <= pos < b:
            return True
        if pos < a:
            break
    return False


def _paren_arg(s: str, i: int):
    """If ``s[i]`` is ``(``, return ``(inner, index_past_close)`` honouring nested ``()``
    and ``{…}`` groups (a ``)`` inside braces is literal). Otherwise ``(None, i)``.
    Raises texrewrite.ParseError on an unterminated group."""
    if i >= len(s) or s[i] != "(":
        return None, i
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            _, j = texrewrite.brace_arg(s, j)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    raise texrewrite.ParseError("unterminated paren group")


def _skip_optional(text: str, i: int, open_ch: str, limit: int) -> int:
    """Skip up to ``limit`` optional ``[…]`` (open_ch='[') or ``(…)`` (open_ch='(')
    argument groups and surrounding whitespace; return the index of the next token."""
    reader = texrewrite.bracket_arg if open_ch == "[" else _paren_arg
    for _ in range(limit):
        i = texrewrite.skip_ws(text, i)
        if i < len(text) and text[i] == open_ch:
            _, i = reader(text, i)
        else:
            break
    return texrewrite.skip_ws(text, i)


def collect_cite_keys(text: str, spans=None):
    """Ordered-unique citation keys referenced by any \\cite-family command in ``text``,
    skipping protected (comment/verbatim) regions. One keylist is read for a single-cite
    command; every keylist is read for a multicite command (\\cites{a}{b}). A malformed
    argument is skipped rather than guessed. ``\\nocite{*}``'s ``*`` is not a key."""
    if spans is None:
        spans = texrewrite.protected_spans(text)
    keys, seen = [], set()

    def _add(content: str) -> None:
        for part in (content or "").split(","):
            k = part.strip()
            if not k or k == "*":
                continue
            if k not in seen:
                seen.add(k)
                keys.append(k)

    for m in merge_bib.CITE_HEAD_RE.finditer(text):
        if _in_spans(m.start(), spans):
            continue
        cmd = m.group(1)
        try:
            i = texrewrite.skip_ws(text, m.end())
            if cmd in merge_bib.MULTI_CITE_CMDS:
                i = _skip_optional(text, i, "(", 2)   # global (pre)(post) notes
                while True:
                    i = _skip_optional(text, i, "[", 2)  # per-cite [pre][post]
                    if i < len(text) and text[i] == "{":
                        content, i = texrewrite.brace_arg(text, i)
                        _add(content)
                    else:
                        break
            else:
                i = _skip_optional(text, i, "[", 2)
                if i < len(text) and text[i] == "{":
                    content, _ = texrewrite.brace_arg(text, i)
                    _add(content)
        except texrewrite.ParseError:
            continue
    return keys


def keys_from_master(master_abs: str):
    """Return ``(cite_keys, unresolved_includes)`` for the master source: its
    \\input/\\include tree is inline-expanded (comments stripped) then scanned for the
    \\cite family. ``unresolved_includes`` lists include targets that could not be found."""
    src_root = os.path.dirname(master_abs)
    includes, unresolved = [], []
    expanded = inspect_sources.expand_includes(master_abs, src_root, includes, unresolved, set())
    return collect_cite_keys(expanded), unresolved


# --------------------------------------------------------------------------- #
# Defined-key union (reuses merge_bib's resilient parser).                      #
# --------------------------------------------------------------------------- #

def bib_defined_keys(bib_paths):
    """Return ``{key: bib_path}`` for every entry key defined across ``bib_paths`` (first
    definer wins), plus a list of per-file parse-error notes. Unreadable files are skipped."""
    defined, errors = {}, []
    for p in bib_paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            errors.append("%s: unreadable (%s)" % (p, e))
            continue
        items, errs = merge_bib.parse_bib(text)
        for note in errs:
            errors.append("%s: %s" % (os.path.basename(p), note))
        for it in items:
            if it.get("kind") == "entry" and it.get("key"):
                defined.setdefault(it["key"], p)
    return defined, errors


def undefined_cites_from_log(log_text: str):
    """Ordered-unique citation keys reported undefined in an engine ``.log`` (verify.py's
    'Citation `key' … undefined' heuristic)."""
    out, seen = [], set()
    for m in UNDEF_CITE_RE.finditer(log_text):
        k = m.group(1).strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


# --------------------------------------------------------------------------- #
# Path resolution + worklist merge.                                            #
# --------------------------------------------------------------------------- #

def _resolve_path(path: str, base_dir: str):
    """Resolve a config path to an existing file, trying it as-given (abs/cwd-relative)
    then relative to ``base_dir``. Returns an absolute path or None."""
    if not path:
        return None
    for cand in (path, os.path.join(base_dir, path) if base_dir else path):
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


def _new_entry(key: str, note: str) -> dict:
    """A fresh, schema-valid pending worklist entry (the agent fills the rest at discover)."""
    return {
        "key": key,
        "status": "pending",
        "classification": None,
        "citekey": None,
        "target_bib": None,
        "bibtex": None,
        "verify_url": None,
        "note": note,
    }


def merge_worklist(plan: dict, ordered_keys, notes) -> int:
    """Merge ``ordered_keys`` into ``plan['worklist']`` idempotently by key, in order.
    Existing entries (and their human-set fields) are preserved and moved to the front in
    the computed order; keys already present but absent from ``ordered_keys`` are kept at
    the tail. ``notes`` maps a new key to its seed note. Returns the count of keys added."""
    existing = {e.get("key"): e for e in plan.get("worklist", []) if e.get("key")}
    added, out, placed = 0, [], set()
    for k in ordered_keys:
        if k in placed:
            continue
        placed.add(k)
        if k in existing:
            out.append(existing[k])
        else:
            out.append(_new_entry(k, notes.get(k, "")))
            added += 1
    for e in plan.get("worklist", []):
        k = e.get("key")
        if k and k not in placed:
            placed.add(k)
            out.append(e)
    plan["worklist"] = out
    return added


# --------------------------------------------------------------------------- #
# Driver.                                                                       #
# --------------------------------------------------------------------------- #

def _load_or_seed(plan_path: str, master_arg: str, bib_args, single_bib: bool) -> dict:
    """Load an existing plan, or seed a minimal add-bib-references skeleton so the helper
    runs standalone against a fixture. CLI config fills only empty config fields."""
    if os.path.exists(plan_path):
        plan = planlib.load(plan_path)
        plan.setdefault("config", {})
        plan.setdefault("worklist", [])
        plan.setdefault("baseline", {"errors": [], "undefined": []})
        return plan
    published = bib_args[0] if bib_args else ""
    preprints = published if single_bib else (bib_args[1] if len(bib_args) > 1 else "")
    return planlib.new_bib_plan(master_source=master_arg, published_bib=published,
                                preprints_bib=preprints, single_bib=single_bib)


def _resolved_bibs(plan: dict, bib_args, base_dir: str):
    """Return ``(found_abs, missing)`` for the .bib union: CLI ``--bib`` if given, else
    config.published_bib + config.preprints_bib (deduped; single-bib mode may share one)."""
    cfg = plan.get("config", {})
    declared = list(bib_args) if bib_args else [
        b for b in (cfg.get("published_bib"), cfg.get("preprints_bib")) if b
    ]
    found, missing, seen = [], [], set()
    for name in declared:
        hit = _resolve_path(name, base_dir)
        if hit:
            real = os.path.realpath(hit)
            if real not in seen:
                seen.add(real)
                found.append(hit)
        else:
            if name not in missing:
                missing.append(name)
    return found, missing


def main(argv=None) -> int:
    """CLI entry point: enumerate undefined cites into the plan.json worklist."""
    ap = argparse.ArgumentParser(
        description="seed the add-bib-references worklist from undefined \\cite keys")
    ap.add_argument("--plan", required=True, help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--master-source", default="", help="master .tex (override config.master_source)")
    ap.add_argument("--bib", action="append", default=[], help="a .bib file (repeatable; overrides config)")
    ap.add_argument("--references", default=None,
                    help="comma-separated keys to target (override vars.references; empty => all undefined)")
    ap.add_argument("--log", default="", help="engine .log for the 'Citation … undefined' cross-check")
    args = ap.parse_args(argv)

    plan = _load_or_seed(args.plan, args.master_source, args.bib, False)
    cfg = plan.setdefault("config", {})

    # Idempotent re-run: drop this helper's prior flags and re-derive them, remembering
    # stable ids + any agent resolutions to re-apply (mirrors merge_bib).
    saved_flags = merge_bib._save_my_flags(plan, MY_FLAG_KINDS)
    plan["flags"] = [f for f in plan.get("flags", []) if f.get("kind") not in MY_FLAG_KINDS]

    master = args.master_source or cfg.get("master_source") or ""
    master_abs = os.path.abspath(master) if master else ""
    if not master_abs or not os.path.isfile(master_abs):
        sys.stderr.write("find_undefined_cites: master source not found: %r "
                         "(set config.master_source or pass --master-source)\n" % master)
        return EXIT_USAGE
    if not cfg.get("master_source"):
        cfg["master_source"] = master
    base_dir = os.path.dirname(master_abs)

    # References override: --references wins; else vars.references.
    if args.references is not None:
        references = [k.strip() for k in args.references.split(",") if k.strip()]
    else:
        references = [k for k in (plan.get("vars", {}).get("references") or []) if k]

    cited, unresolved = keys_from_master(master_abs)
    for tgt in unresolved:
        planlib.add_flag(plan, tier=2, stage="load-context", kind="unresolved-include",
                         message="Could not resolve \\input/\\include target %r under the master "
                                 "source tree; cites in it are not scanned." % tgt,
                         location=master)

    bib_found, bib_missing = _resolved_bibs(plan, args.bib, base_dir)
    for name in bib_missing:
        planlib.add_flag(plan, tier=2, stage="load-context", kind="bib-missing",
                         message="Declared .bib %r was not found; its keys are treated as undefined."
                                 % name, location=name)
    defined, bib_errors = bib_defined_keys(bib_found)
    for note in bib_errors:
        planlib.add_flag(plan, tier=2, stage="load-context", kind="bib-parse-error",
                         message="Could not fully parse a .bib (%s); some defined keys may be missed."
                                 % note)

    undefined = [k for k in cited if k not in defined]

    # Log cross-check: keys the compiler reports undefined but the source scan missed.
    log_undef = []
    if args.log:
        log_abs = _resolve_path(args.log, base_dir)
        if log_abs:
            with open(log_abs, "r", encoding="utf-8", errors="replace") as fh:
                log_undef = undefined_cites_from_log(fh.read())
            base = plan.setdefault("baseline", {"errors": [], "undefined": []})
            if not base.get("undefined"):
                base["undefined"] = list(log_undef)
        else:
            planlib.add_flag(plan, tier=2, stage="load-context", kind="log-missing",
                             message="Cross-check log %r not found." % args.log, location=args.log)
    scan_misses = [k for k in log_undef if k not in cited]
    for k in scan_misses:
        planlib.add_flag(plan, tier=2, stage="load-context", kind="cite-scan-miss",
                         message="Key %r is reported undefined by the compile log but was not "
                                 "found by the source \\cite scan; added to the worklist — verify "
                                 "it is really referenced." % k)

    # Build the worklist keys + per-key seed notes. An explicit `references` var restricts
    # the worklist to those keys (each included even if already defined / not yet cited);
    # otherwise the worklist is every undefined \cite in document order.
    notes = {}
    if references:
        ordered = list(references)
        for k in references:
            if k in undefined:
                notes[k] = "explicit reference; undefined \\cite in the source."
            elif k in defined:
                notes[k] = "explicit reference; already defined in %s (re-verify)." % os.path.basename(defined[k])
            elif k in cited:
                notes[k] = "explicit reference; \\cite'd and already resolved."
            else:
                notes[k] = "explicit reference; not yet \\cite'd in the source."
    else:
        ordered = list(undefined)
        for k in undefined:
            notes[k] = "undefined \\cite — referenced in the master source, not defined in any .bib."

    # Log-only keys (either mode): the compile log flagged them but the source scan did not.
    for k in scan_misses:
        if k not in notes:
            ordered.append(k)
            notes[k] = "undefined in the compile log but not found by the source scan — verify manually."

    added = merge_worklist(plan, ordered, notes)

    merge_bib._stabilize_my_flag_ids(plan, MY_FLAG_KINDS, saved_flags)
    planlib.add_log(plan, "load-context",
                    "cites scanned=%d, defined=%d, undefined=%d; worklist=%d (references=%s, +%d new)"
                    % (len(cited), len(defined), len(undefined), len(plan["worklist"]),
                       "set" if references else "auto", added))
    planlib.save(args.plan, plan)

    sys.stderr.write(
        "find_undefined_cites: cited=%d defined=%d undefined=%d worklist=%d (+%d new)%s\n"
        % (len(cited), len(defined), len(undefined), len(plan["worklist"]), added,
           (" log-misses=%d" % len(scan_misses)) if scan_misses else ""))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
