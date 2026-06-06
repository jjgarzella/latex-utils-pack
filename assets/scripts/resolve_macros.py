#!/usr/bin/env python3
"""resolve_macros — collapse identical / rename divergent macro collisions, and
hoist every source's macro definitions into one global preamble.

Tier 1 (collapse/rename/hoist) + Tier 2 (flag-and-defer). Owner: pt-dyq (T4).
Shares one TeX-aware safe find-replace core (texrewrite.py) with prefix_labels.py.
Part of the mol-latex-concat formula (latex-utils pack).

Why this helper hoists definitions too: the transform step strips each chapter's
preamble (its `\\newcommand`s vanish with it), and hoist_preamble.py hoists only the
`\\usepackage` lines. So the macro DEFINITIONS have to be reinstated in the master
preamble or the merged document cannot compile — and the helper that already has to
read, compare and rename them is the natural owner. It writes one delimited,
idempotent block (`% >>> mol-latex-concat: hoisted macros >>>`) just before
`\\begin{document}` in the master, *after* whatever package block the hoist step
laid down (so macros that use a hoisted package still resolve).

Reads  plan.json: vars/sources (dir, entry_file, slug, chapter order), dest.
Writes plan.json: renames.macros[], preamble.macros_hoisted[]; the master preamble
       block on disk; and rewrites divergent macros' uses in the later chapter.

Collision protocol (across sources, in chapter order):
  * first source to define a name is canonical and keeps the bare name;
  * a later source whose definition is byte-identical (modulo whitespace) COLLAPSES
    to the canonical one (emitted once);
  * a later source whose definition DIVERGES gets, for a safely-renamable control
    sequence (`\\newcommand`/`\\def`/`\\DeclareMathOperator`/`\\DeclareRobustCommand`),
    a per-slug suffix (`\\foo` => `\\foopid`); its definition and every use in *that*
    chapter are renamed; recorded in renames.macros[].
  * a divergent `\\renewcommand`/`\\providecommand`/`\\let` (semantics tied to a prior
    definition) or a divergent environment (`\\newtheorem`/`\\newenvironment`, whose
    rename would have to rewrite `\\begin`/`\\end` and counters) is NOT guessed: it is
    Tier-2 flagged and the later definition is dropped pending the agent.

A non-letter slug character is encoded to a letter for the suffix (digits 0-9 ->
a-j) so the renamed control sequence stays a valid TeX name.

CLI:
  resolve_macros.py --plan PLAN [--dest DIR]
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planlib  # noqa: E402
import texrewrite  # noqa: E402
import inspect_sources as inspect  # noqa: E402  (reuse its sound preamble parser)

# Control-sequence macro kinds whose divergent collision we can rename soundly.
SAFE_RENAME_KINDS = {
    "newcommand", "DeclareRobustCommand", "DeclareMathOperator",
    "DeclareMathOperator*", "def",
}
# Control-sequence kinds whose meaning depends on a prior/target definition: a
# divergent collision is flagged, never auto-renamed.
UNSAFE_RENAME_KINDS = {"renewcommand", "providecommand", "let"}
# Environment-defining kinds: divergent collisions are flagged (renaming an env
# means rewriting \begin/\end pairs and shared counters — out of scope here).
ENV_KINDS = {"newtheorem", "newenvironment", "renewenvironment"}

MY_FLAG_KINDS = {
    "divergent-macro-collision", "divergent-env-collision", "macro-suffix-collision",
    "macro-hoist-target-missing", "preamble-derivation-empty",
}

BLOCK_BEGIN = "% >>> mol-latex-concat: hoisted macros (resolve_macros.py) — do not edit between markers >>>"
BLOCK_END = "% <<< mol-latex-concat: hoisted macros <<<"


# --------------------------------------------------------------------------- #
# Helpers.                                                                      #
# --------------------------------------------------------------------------- #

def _save_my_flags(plan, kinds):
    """Snapshot this helper's existing flags so an idempotent re-run can reuse their
    stable ids and preserve any agent-written resolution."""
    return {(f.get("kind"), f.get("slug"), f.get("message")): f
            for f in plan.get("flags", []) if f.get("kind") in kinds}


def _stabilize_my_flag_ids(plan, kinds, saved):
    """Give this helper's freshly-derived flags collision-free, stable ids: reuse the
    prior id (and any agent resolution) for a flag that recurs, else mint the next
    free ``Fn``. Other helpers' flag ids are left untouched."""
    flags = plan.get("flags", [])
    used = {f["id"] for f in flags if f.get("kind") not in kinds and f.get("id")}

    def fresh():
        n = 1
        while ("F%d" % n) in used:
            n += 1
        used.add("F%d" % n)
        return "F%d" % n

    for f in flags:
        if f.get("kind") not in kinds:
            continue
        prev = saved.get((f.get("kind"), f.get("slug"), f.get("message")))
        if prev and prev.get("id") and prev["id"] not in used:
            f["id"] = prev["id"]
            used.add(prev["id"])
        else:
            f["id"] = fresh()
        if prev and prev.get("resolution") is not None and not f.get("resolution"):
            f["resolution"] = prev["resolution"]


def _dest_dir(plan, dest_arg):
    d = dest_arg or plan.get("dest", {}).get("dir") or ""
    return os.path.abspath(d) if d else ""


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _chapter_tex_files(chapter_dir):
    out = []
    for root, dirs, files in os.walk(chapter_dir):
        dirs[:] = [d for d in dirs if d not in (".git", ".svn", "node_modules")]
        for f in files:
            if f.endswith(".tex"):
                out.append(os.path.join(root, f))
    return sorted(out)


def _macro_suffix(slug: str) -> str:
    """A valid (letters-only) control-sequence suffix derived from a slug. Digits
    map 0-9 -> a-j so two slugs that differ only in a digit stay distinct."""
    out = []
    for ch in slug:
        if ch.isalpha():
            out.append(ch)
        elif ch.isdigit():
            out.append(chr(ord("a") + int(ch)))
    return "".join(out) or "x"


def _is_env(rec) -> bool:
    return rec.get("kind") in ENV_KINDS


def _cskey(name: str) -> str:
    """Normalise a definition's name to the bare control-sequence it occupies, so the
    two namespaces line up: a macro is catalogued WITH its backslash (``\\alg``) but an
    environment WITHOUT (``alg``), yet ``\\newtheorem{alg}`` and ``\\newcommand{\\alg}``
    both define the control sequence ``\\alg`` and therefore clash."""
    return (name or "").lstrip("\\")


def _signature(rec, verbatim, is_env) -> str:
    """A definition signature for the identical-vs-divergent decision. Environments
    compare on their verbatim text (their `\\end` code is not in the parsed record);
    control sequences compare on (kind, nargs, default, normalised body)."""
    if is_env:
        return "env::" + re.sub(r"\s+", " ", verbatim.strip())
    body = re.sub(r"\s+", " ", (rec.get("body") or "").strip())
    return "cs::%s::%s::%s::%s" % (rec.get("kind"), rec.get("nargs"), rec.get("default"), body)


def _derive_preamble(source):
    """Re-derive a source's preamble text (include-expanded, comment-stripped, split
    at the first \\begin{document}) — exactly as inspect_sources catalogued it."""
    src_dir = source.get("dir") or ""
    src_abs = src_dir if os.path.isabs(src_dir) else os.path.abspath(src_dir)
    entry_rel = source.get("entry_file")
    if not entry_rel:
        return None
    entry_abs = os.path.join(src_abs, entry_rel)
    if not os.path.isfile(entry_abs):
        return None
    full = inspect.expand_includes(entry_abs, src_abs, [], [], set())
    bd = inspect.BEGIN_DOC_RE.search(full)
    return full[:bd.start()] if bd else full


def _extract_defs(preamble):
    """Yield ``(record, verbatim_text)`` for each macro definition in ``preamble``,
    using inspect_sources' own parser so the catalogue and this stay consistent."""
    out = []
    pos = 0
    while True:
        m = inspect.MACRO_HEAD_RE.search(preamble, pos)
        if not m:
            break
        kind, star = m.group(1), m.group(2)
        try:
            rec, end = inspect._parse_one_macro(kind, star, preamble, m.end())
        except Exception:
            pos = m.end()
            continue
        out.append((rec, preamble[m.start():end]))
        pos = end if end > m.end() else m.end() + 1
    return out


def _rewrite_uses(chapter_dir, old_full, new_full):
    total = 0
    for p in _chapter_tex_files(chapter_dir):
        try:
            t = _read(p)
        except OSError:
            continue
        new_t, n = texrewrite.rename_csname(t, old_full, new_full)
        if n and new_t != t:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(new_t)
            total += n
    return total


def _install_block(master_path, block_text):
    """Insert/replace the delimited hoisted-macros block in the master preamble.
    Returns True on success, False if there is no place to put it."""
    try:
        text = _read(master_path)
    except OSError:
        return False
    new_block = BLOCK_BEGIN + "\n" + block_text.rstrip("\n") + "\n" + BLOCK_END + "\n"
    if BLOCK_BEGIN in text and BLOCK_END in text:
        pre = text[:text.index(BLOCK_BEGIN)]
        post = text[text.index(BLOCK_END) + len(BLOCK_END):]
        if post.startswith("\n"):
            post = post[1:]
        new_text = pre + new_block + post
    else:
        m = inspect.BEGIN_DOC_RE.search(text)
        if not m:
            return False
        new_text = text[:m.start()] + new_block + "\n" + text[m.start():]
    if new_text != text:
        with open(master_path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    return True


# --------------------------------------------------------------------------- #
# Driver.                                                                       #
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description="resolve macro collisions and hoist macro definitions")
    ap.add_argument("--plan", required=True, help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", default="", help="destination project dir (default: plan.dest.dir)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.plan):
        sys.stderr.write("resolve_macros: plan.json not found: %s\n" % args.plan)
        return 2
    plan = planlib.load(args.plan)

    dest_dir = _dest_dir(plan, args.dest)
    if not dest_dir or not os.path.isdir(dest_dir):
        sys.stderr.write("resolve_macros: destination dir not found: %r\n" % dest_dir)
        return 2

    # Idempotent re-run: drop this helper's prior flags and rebuild its plan output,
    # but remember their stable ids + resolutions to re-apply after re-derivation.
    saved_flags = _save_my_flags(plan, MY_FLAG_KINDS)
    plan["flags"] = [f for f in plan.get("flags", []) if f.get("kind") not in MY_FLAG_KINDS]
    plan.setdefault("renames", {})["macros"] = []
    plan.setdefault("preamble", {})["macros_hoisted"] = []

    sources = sorted(plan.get("sources", []), key=lambda e: e.get("index", 0))

    # Keyed by the bare control-sequence NAME, because \newtheorem{X}/\newenvironment{X}
    # define the control sequence \X just like \newcommand{\X} does — an env name and a
    # macro name in different chapters genuinely clash on \X (e.g. \newtheorem{alg} vs
    # \newcommand{\alg}). Unifying the namespace here is what lets that be detected.
    seen = {}          # name -> {"sig", "idx", "slug", "is_env", "kind"}
    emit = []          # ordered verbatim definitions for the global preamble
    rename_records = []
    hoisted = []       # report summary

    for source in sources:
        slug = source.get("slug") or ""
        idx = source.get("index", 0)
        chapter_dir = os.path.join(dest_dir, "contents", slug) if slug else ""

        preamble = _derive_preamble(source)
        if preamble is None:
            planlib.add_flag(plan, tier=2, stage="resolve", kind="preamble-derivation-empty",
                             severity="info", slug=slug or None,
                             message=("Could not re-derive the preamble for %s (missing "
                                      "entry_file?) — its macros were not hoisted."
                                      % (source.get("dir") or "?")),
                             location=source.get("entry_file"))
            continue

        for rec, verbatim in _extract_defs(preamble):
            name = rec.get("name")
            if not name:
                continue
            is_env = _is_env(rec)
            key = _cskey(name)  # the control sequence this def occupies (\X), namespace-unified
            sig = _signature(rec, verbatim, is_env)

            if key not in seen:
                seen[key] = {"sig": sig, "idx": idx, "slug": slug, "is_env": is_env,
                             "kind": rec.get("kind")}
                emit.append(verbatim)
                hoisted.append({"name": name, "kind": rec.get("kind"), "slug": slug,
                                "action": "hoisted"})
                continue

            if seen[key]["idx"] == idx:
                # The same source redefining its own macro: keep its sequence as-is.
                emit.append(verbatim)
                continue

            if sig == seen[key]["sig"]:
                hoisted.append({"name": name, "kind": rec.get("kind"), "slug": slug,
                                "action": "collapsed", "into_slug": seen[key]["slug"]})
                continue

            # Divergent cross-source collision on the control sequence \name.
            canon = seen[key]["slug"]
            canon_is_env = seen[key]["is_env"]
            if is_env:
                # The LATER definition is an environment: renaming an env (its
                # \begin/\end and shared counters) is out of scope — flag and defer,
                # whether the canonical owner was an env or a plain macro of the name.
                if canon_is_env:
                    msg = ("Environment %r is defined differently by chapter %r and chapter "
                           "%r; renaming an environment (its \\begin/\\end and counters) is "
                           "not auto-done — keep one and reconcile the other by hand."
                           % (name, canon, slug))
                else:
                    msg = ("Environment %r in chapter %r clashes with the control sequence "
                           "\\%s already defined as a macro by chapter %r; renaming an "
                           "environment is not auto-done — reconcile by hand."
                           % (name, slug, name, canon))
                planlib.add_flag(plan, tier=2, stage="resolve", kind="divergent-env-collision",
                                 severity="warn", slug=slug, message=msg,
                                 location=chapter_dir or None)
                hoisted.append({"name": name, "kind": rec.get("kind"), "slug": slug,
                                "action": "deferred"})
                continue

            if rec.get("kind") in UNSAFE_RENAME_KINDS:
                planlib.add_flag(plan, tier=2, stage="resolve", kind="divergent-macro-collision",
                                 severity="warn", slug=slug,
                                 message=("Macro %s (%s) in chapter %r diverges from chapter %r's "
                                          "definition; a %s cannot be safely auto-renamed (its "
                                          "meaning depends on a prior definition) — reconcile by "
                                          "hand." % (name, rec.get("kind"), slug, canon, rec.get("kind"))),
                                 location=chapter_dir or None)
                hoisted.append({"name": name, "kind": rec.get("kind"), "slug": slug,
                                "action": "deferred"})
                continue

            if rec.get("kind") not in SAFE_RENAME_KINDS:
                planlib.add_flag(plan, tier=2, stage="resolve", kind="divergent-macro-collision",
                                 severity="warn", slug=slug,
                                 message=("Macro %s (%s) in chapter %r diverges from chapter %r's "
                                          "definition and its kind is not safely renamable — "
                                          "reconcile by hand." % (name, rec.get("kind"), slug, canon)),
                                 location=chapter_dir or None)
                hoisted.append({"name": name, "kind": rec.get("kind"), "slug": slug,
                                "action": "deferred"})
                continue

            # Safe divergent rename: \foo -> \foo<suffix>. This also resolves a later
            # command that clashes with an earlier ENVIRONMENT's control sequence
            # (e.g. ptw's \newcommand{\alg} vs ahks's \newtheorem{alg}) — renaming the
            # plain command is sound; the environment keeps the bare name.
            new_name = name + _macro_suffix(slug)
            existing_cs = set(seen.keys())  # every control-sequence key already claimed
            if _cskey(new_name) in existing_cs or any(r["to"] == new_name for r in rename_records):
                planlib.add_flag(plan, tier=2, stage="resolve", kind="macro-suffix-collision",
                                 severity="warn", slug=slug,
                                 message=("Wanted to rename %s -> %s for chapter %r but that name "
                                          "is already taken — reconcile by hand." % (name, new_name, slug)),
                                 location=chapter_dir or None)
                hoisted.append({"name": name, "kind": rec.get("kind"), "slug": slug,
                                "action": "deferred"})
                continue

            renamed_def, _ = texrewrite.rename_csname(verbatim, name, new_name)
            emit.append(renamed_def)
            if chapter_dir and os.path.isdir(chapter_dir):
                _rewrite_uses(chapter_dir, name, new_name)
            reason = ("clash with environment %r defined by chapter %r" % (name, canon)
                      if canon_is_env else "divergent macro collision with chapter %r" % canon)
            rename_records.append({
                "slug": slug, "from": name, "to": new_name, "reason": reason,
            })
            hoisted.append({"name": name, "kind": rec.get("kind"), "slug": slug,
                            "action": "renamed", "to": new_name})
            seen[_cskey(new_name)] = {"sig": sig, "idx": idx, "slug": slug, "is_env": False,
                                      "kind": rec.get("kind")}

    plan["renames"]["macros"] = rename_records
    plan["preamble"]["macros_hoisted"] = hoisted

    # Install the hoisted-macros block into the master preamble.
    if emit:
        master_tex = plan.get("dest", {}).get("master_tex") or "main.tex"
        master_path = os.path.join(dest_dir, master_tex)
        if not os.path.isfile(master_path) and os.path.isfile(os.path.join(dest_dir, "main.tex")):
            master_path = os.path.join(dest_dir, "main.tex")
        block = "\n".join(v.strip("\n") for v in emit)
        if not _install_block(master_path, block):
            planlib.add_flag(plan, tier=2, stage="resolve", kind="macro-hoist-target-missing",
                             severity="warn", slug=None,
                             message=("Could not find a master .tex with \\begin{document} to "
                                      "receive the hoisted macros (looked for %r). The %d hoisted "
                                      "definition(s) must be placed in the preamble by hand or the "
                                      "merged build will report undefined control sequences."
                                      % (os.path.relpath(master_path, dest_dir), len(emit))),
                             location=master_path)

    _stabilize_my_flag_ids(plan, MY_FLAG_KINDS, saved_flags)

    n_renames = len(rename_records)
    n_collapsed = sum(1 for h in hoisted if h.get("action") == "collapsed")
    n_deferred = sum(1 for h in hoisted if h.get("action") == "deferred")
    planlib.add_log(plan, "resolve",
                    "Hoisted %d macro definition(s); %d collapsed, %d renamed, %d deferred (flagged)."
                    % (len(emit), n_collapsed, n_renames, n_deferred))

    try:
        planlib.validate(plan)
    except Exception as e:
        sys.stderr.write("resolve_macros: plan.json failed schema validation: %s\n" % e)
        planlib.save(args.plan, plan)
        return 1

    planlib.save(args.plan, plan)
    sys.stderr.write(
        "[resolve-macros] wrote %s — %d hoisted, %d collapsed, %d renamed, %d deferred.\n"
        % (args.plan, len(emit), n_collapsed, n_renames, n_deferred))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
