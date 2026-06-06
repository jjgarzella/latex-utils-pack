#!/usr/bin/env python3
"""inspect_sources — catalogue each LaTeX source into plan.json.

Tier 1 (mechanical catalogue) + Tier 2 (flag-and-defer). Owner: pt-srs (T2).
Part of the mol-latex-concat formula (latex-utils pack).

Reads  plan.json: vars.sources, vars.tex_engine, sources[].dir/slug
       (slugs are SUGGESTED here; the agent confirms/overrides them).
Writes plan.json: per source -> entry_file, title, document_class/class_options,
       tex_engine hint, bib_backend, bib_files[], bib_style, packages[], macros[],
       labels[], includes[], figures[], local_sty[], authors[]. Tier-2 flags for
       exotic class (beamer/standalone/poster/letter -> refuse), 0-or->1
       \\begin{document}, programmatic preamble TeX it cannot model soundly
       (\\csname/\\catcode/\\@ifpackageloaded), unresolved includes, and any macro
       it cannot parse. TeX is Turing-complete: flag, NEVER guess.

How it stays sound on the well-behaved subset (and flags the rest):
  * Comments are stripped first (so commented-out \\usepackage/\\newcommand are
    never catalogued).
  * Includes are inline-EXPANDED, then the document is split at the first
    \\begin{document} -- so a preamble that lives in an \\input{preamble} file
    (class + packages + macros) is catalogued just like an inline one.
  * \\title/\\author are searched across the whole document (some papers put them
    before \\begin{document}, others after \\maketitle).
  * The macro/include/figure scanners use negative lookahead so \\newtheoremstyle
    is not mistaken for \\newtheorem, \\includegraphics not for \\include, etc.

CLI:
  inspect_sources.py --plan PLAN [--sources DIR1,DIR2,...] [--dest DIR] [--engine pdflatex]

`--sources` is required only when PLAN does not exist yet (first run seeds it);
on a re-run the existing plan's source list and any agent-set slug/order are kept.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planlib  # noqa: E402


# --------------------------------------------------------------------------- #
# Class / package knowledge.                                                    #
# --------------------------------------------------------------------------- #

# Report/article-like classes this formula handles (the IN side of the envelope).
KNOWN_GOOD_CLASSES = {
    "article", "amsart", "amsproc", "report", "book",
    "scrartcl", "scrreprt", "scrbook", "memoir",
    "extarticle", "extreport", "extbook",
    "mwart", "mwrep", "mwbk", "ucsddissertation",
}

# Classes this formula does NOT handle -> a blocker `exotic-class` flag (refuse).
REFUSE_CLASSES = {
    "beamer", "standalone", "poster", "a0poster", "beamerposter",
    "tikzposter", "sciposter", "leaflet", "letter", "scrlttr2", "myletter",
}

# Packages that pin the engine to a Unicode TeX (xelatex/lualatex).
UNICODE_ENGINE_PACKAGES = {
    "fontspec", "unicode-math", "polyglossia", "xltxtra", "xunicode",
    "luatextra", "luacode", "luaotfload", "fontawesome5",
}

# Standard BibTeX styles; anything else is flagged `exotic-bibstyle` for review.
STANDARD_BST = {
    "plain", "alpha", "abbrv", "unsrt", "plainnat", "abbrvnat", "unsrtnat",
    "amsplain", "amsalpha", "ieeetr", "acm", "siam", "apalike", "plaindin",
}


# --------------------------------------------------------------------------- #
# Low-level TeX scanning helpers (sound over the well-behaved subset).          #
# --------------------------------------------------------------------------- #

class ParseError(Exception):
    """A construct could not be read soundly -> flag-and-defer, never guess."""


def strip_comments(text: str) -> str:
    """Drop TeX line comments: from an unescaped ``%`` to end of line. A ``%`` is
    escaped iff preceded by an odd run of backslashes (``\\%`` is a literal percent).
    Newlines are preserved so downstream line accounting stays roughly aligned."""
    out = []
    for line in text.splitlines(keepends=True):
        cut = None
        k = 0
        while k < len(line):
            c = line[k]
            if c == "\\":
                k += 2  # skip the escaped next char (incl. \% and \\)
                continue
            if c == "%":
                cut = k
                break
            k += 1
        if cut is None:
            out.append(line)
        else:
            # keep any trailing newline so line numbers don't collapse
            nl = line[len(line.rstrip("\r\n")):] if line.endswith(("\n", "\r")) else ""
            out.append(line[:cut] + nl)
    return "".join(out)


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _brace_arg(s: str, i: int):
    """If ``s[i]`` is ``{``, return ``(content, index_past_close)`` honouring nested
    braces and escaped ``\\{``/``\\}``. Otherwise ``(None, i)``. Raises ParseError on
    an unterminated group."""
    if i >= len(s) or s[i] != "{":
        return None, i
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    raise ParseError("unterminated brace group")


def _bracket_arg(s: str, i: int):
    """If ``s[i]`` is ``[``, return ``(content, index_past_close)`` (brackets do not
    nest but may wrap a brace group). Otherwise ``(None, i)``."""
    if i >= len(s) or s[i] != "[":
        return None, i
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            _, j = _brace_arg(s, j)
            continue
        if c == "]":
            return s[i + 1:j], j + 1
        j += 1
    raise ParseError("unterminated optional argument")


def _read_csname(s: str, i: int):
    """Read a control sequence at ``s[i]`` (``\\foo`` or a single control symbol).
    Returns ``(name_with_backslash, next_index)`` or ``(None, i)``."""
    if i >= len(s) or s[i] != "\\":
        return None, i
    j = i + 1
    if j < len(s) and (s[j].isalpha() or s[j] == "@"):
        k = j
        while k < len(s) and (s[k].isalpha() or s[k] == "@"):
            k += 1
        return s[i:k], k
    # single-character control symbol, e.g. \,
    return s[i:j + 1], j + 1


# --------------------------------------------------------------------------- #
# Include expansion.                                                            #
# --------------------------------------------------------------------------- #

INCLUDE_RE = re.compile(r"\\(input|include|subfile)(?![A-Za-z@])\s*")


def _resolve_include(target: str, base_dir: str):
    """Resolve an \\input/\\include/\\subfile target to a file on disk, trying the
    literal name and a ``.tex`` suffix. Returns an absolute path or None."""
    target = target.strip()
    if not target:
        return None
    cands = [target]
    if not target.endswith(".tex"):
        cands.append(target + ".tex")
    for c in cands:
        p = c if os.path.isabs(c) else os.path.join(base_dir, c)
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def expand_includes(entry_abs, src_root, includes_out, unresolved_out, seen):
    """Return the comment-stripped text of ``entry_abs`` with every \\input/\\include/
    \\subfile target inlined in place (transitively). Records each include target
    (relative to ``src_root``) in ``includes_out`` and each unresolvable target in
    ``unresolved_out``. ``seen`` guards against include cycles."""
    entry_abs = os.path.abspath(entry_abs)
    if entry_abs in seen:
        return ""  # cycle: already inlined once; caller flags via unresolved/cycle
    seen.add(entry_abs)
    try:
        text = strip_comments(_read(entry_abs))
    except OSError:
        return ""
    out = []
    pos = 0
    for m in INCLUDE_RE.finditer(text):
        if m.start() < pos:
            continue
        content, end = _brace_arg(text, m.end()) if m.end() < len(text) and text[m.end()] == "{" else (None, m.end())
        if content is None:
            # bare \input filename (whitespace-delimited) — plain-TeX form
            mt = re.match(r"([^\s{}\\]+)", text[m.end():])
            if not mt:
                continue
            content = mt.group(1)
            end = m.end() + mt.end()
        out.append(text[pos:m.start()])
        target = content.strip()
        rel = target if not target.endswith(".tex") else target[:-4]
        if rel not in includes_out:
            includes_out.append(rel)
        resolved = _resolve_include(target, os.path.dirname(entry_abs))
        if resolved is None:
            resolved = _resolve_include(target, src_root)
        if resolved is None:
            unresolved_out.append(target)
        else:
            out.append(expand_includes(resolved, src_root, includes_out, unresolved_out, seen))
        pos = end
    out.append(text[pos:])
    return "".join(out)


# --------------------------------------------------------------------------- #
# Preamble parsers.                                                             #
# --------------------------------------------------------------------------- #

DOCCLASS_RE = re.compile(r"\\documentclass\b")
USEPACKAGE_RE = re.compile(r"\\(usepackage|RequirePackage)(?![A-Za-z@])\s*")

MACRO_HEAD_RE = re.compile(
    r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand|"
    r"DeclareMathOperator|newtheorem|newenvironment|renewenvironment|def|let)"
    r"(?![A-Za-z@])(\*?)"
)


def parse_document_class(preamble: str):
    """Return ``(class_name, [options])`` from the first \\documentclass, or
    ``(None, [])`` if none is present in the (expanded) preamble."""
    m = DOCCLASS_RE.search(preamble)
    if not m:
        return None, []
    i = _skip_ws(preamble, m.end())
    opts, i = _bracket_arg(preamble, i)
    i = _skip_ws(preamble, i)
    name, _ = _brace_arg(preamble, i)
    options = [o.strip() for o in (opts or "").split(",") if o.strip()]
    return (name.strip() if name else None), options


def parse_packages(preamble: str):
    """Return ``[{name, options}]`` for every \\usepackage/\\RequirePackage. A single
    statement loading several comma-separated packages expands to one entry each."""
    pkgs = []
    pos = 0
    while True:
        m = USEPACKAGE_RE.search(preamble, pos)
        if not m:
            break
        i = m.end()
        try:
            opts, i = _bracket_arg(preamble, i)
            i = _skip_ws(preamble, i)
            names, i = _brace_arg(preamble, i)
        except ParseError:
            pos = m.end()
            continue
        if names is None:
            pos = m.end()
            continue
        options = [o.strip() for o in (opts or "").split(",") if o.strip()]
        for nm in names.split(","):
            nm = nm.strip()
            if nm:
                pkgs.append({"name": nm, "options": list(options)})
        pos = i
    return pkgs


def _parse_one_macro(kind: str, star: str, s: str, i: int):
    """Parse the arguments of a single macro-defining command starting at index ``i``
    (just past the command head). Returns ``(record, next_index)``."""
    i = _skip_ws(s, i)

    def read_name_braced_or_bare():
        nonlocal i
        if i < len(s) and s[i] == "{":
            raw, j = _brace_arg(s, i)
            i = j
            return (raw or "").strip()
        if i < len(s) and s[i] == "\\":
            nm, j = _read_csname(s, i)
            i = j
            return nm
        raise ParseError("expected macro name")

    rec = {"name": None, "kind": kind, "nargs": None, "default": None, "body": None}

    if kind in ("newcommand", "renewcommand", "providecommand", "DeclareRobustCommand"):
        rec["name"] = read_name_braced_or_bare()
        i = _skip_ws(s, i)
        n_opt, i = _bracket_arg(s, i)
        if n_opt is not None:
            ns = n_opt.strip()
            rec["nargs"] = int(ns) if ns.lstrip("-").isdigit() else None
            i = _skip_ws(s, i)
            d_opt, i = _bracket_arg(s, i)
            if d_opt is not None:
                rec["default"] = d_opt
                i = _skip_ws(s, i)
        body, i = _brace_arg(s, i)
        rec["body"] = body

    elif kind == "DeclareMathOperator":
        rec["kind"] = "DeclareMathOperator*" if star else "DeclareMathOperator"
        rec["name"] = read_name_braced_or_bare()
        i = _skip_ws(s, i)
        body, i = _brace_arg(s, i)
        rec["body"] = body

    elif kind == "newtheorem":
        env, i = _brace_arg(s, i)
        rec["name"] = (env or "").strip()      # environment name (no backslash)
        rec["env"] = rec["name"]
        i = _skip_ws(s, i)
        shared, i = _bracket_arg(s, i)         # [shared counter]
        i = _skip_ws(s, i)
        title, i = _brace_arg(s, i)
        rec["body"] = (title or "").strip()
        i = _skip_ws(s, i)
        within, i = _bracket_arg(s, i)         # [numbered within]
        if shared:
            rec["counter"] = shared.strip()
        if within:
            rec["within"] = within.strip()

    elif kind in ("newenvironment", "renewenvironment"):
        env, i = _brace_arg(s, i)
        rec["name"] = (env or "").strip()
        rec["env"] = rec["name"]
        i = _skip_ws(s, i)
        n_opt, i = _bracket_arg(s, i)
        if n_opt is not None:
            ns = n_opt.strip()
            rec["nargs"] = int(ns) if ns.lstrip("-").isdigit() else None
            i = _skip_ws(s, i)
            d_opt, i = _bracket_arg(s, i)
            if d_opt is not None:
                rec["default"] = d_opt
                i = _skip_ws(s, i)
        begin, i = _brace_arg(s, i)            # \begin code
        i = _skip_ws(s, i)
        _end, i = _brace_arg(s, i)             # \end code (consumed, not stored)
        rec["body"] = begin

    elif kind == "def":
        nm, i = _read_csname(s, i)
        if nm is None:
            raise ParseError("\\def without a control sequence")
        rec["name"] = nm
        # parameter text: everything up to the body's opening brace
        j = i
        while j < len(s) and s[j] != "{":
            if s[j] == "\\":
                j += 2
                continue
            j += 1
        paramtext = s[i:j]
        rec["nargs"] = paramtext.count("#")
        # delimited parameter text (anything but #digits/space) is NOT mechanical
        if re.search(r"[^#\d\s]", paramtext):
            raise ParseError("\\def with delimited parameter text")
        body, i = _brace_arg(s, j)
        rec["body"] = body

    elif kind == "let":
        nm, i = _read_csname(s, i)
        if nm is None:
            raise ParseError("\\let without a control sequence")
        rec["name"] = nm
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == "=":
            i += 1
            i = _skip_ws(s, i)
        if i < len(s) and s[i] == "\\":
            tgt, i = _read_csname(s, i)
        elif i < len(s):
            tgt, i = s[i], i + 1
        else:
            tgt = None
        rec["body"] = tgt
    else:
        raise ParseError("unhandled macro kind %r" % kind)

    if not rec["name"]:
        raise ParseError("empty macro name")
    return rec, i


def parse_macros(preamble: str):
    """Return ``([macro_record, ...], [parse_error_snippet, ...])`` for the preamble.
    A construct it cannot read soundly is NOT guessed — it is returned as an error
    snippet so the caller can raise a Tier-2 flag."""
    macros = []
    errors = []
    pos = 0
    while True:
        m = MACRO_HEAD_RE.search(preamble, pos)
        if not m:
            break
        kind, star = m.group(1), m.group(2)
        try:
            rec, end = _parse_one_macro(kind, star, preamble, m.end())
            macros.append(rec)
            pos = end if end > m.end() else m.end() + 1
        except (ParseError, ValueError, IndexError):
            snippet = preamble[m.start():m.start() + 60].replace("\n", " ")
            errors.append(snippet.strip())
            pos = m.end()
    return macros, errors


# --------------------------------------------------------------------------- #
# Whole-document scanners (title/author/labels/figures/bib).                    #
# --------------------------------------------------------------------------- #

LABEL_RE = re.compile(r"\\label\s*\{")
GRAPHICS_RE = re.compile(r"\\includegraphics(?![A-Za-z@])\s*(?:\[[^\]]*\])?\s*\{")
TITLE_RE = re.compile(r"\\title\s*(?:\[[^\]]*\])?\s*\{")
AUTHOR_RE = re.compile(r"\\author\s*(?:\[[^\]]*\])?\s*\{")
BIBLIOGRAPHY_RE = re.compile(r"\\bibliography(?![A-Za-z@])\s*\{")
BIBSTYLE_RE = re.compile(r"\\bibliographystyle\s*\{")
ADDBIB_RE = re.compile(r"\\addbibresource\s*(?:\[[^\]]*\])?\s*\{")


def _all_brace_targets(text: str, head_re: re.Pattern):
    """Yield the balanced ``{...}`` content following each match of ``head_re``."""
    pos = 0
    while True:
        m = head_re.search(text, pos)
        if not m:
            return
        try:
            content, end = _brace_arg(text, m.end() - 1)
        except ParseError:
            pos = m.end()
            continue
        yield content
        pos = end


def first_title(text: str):
    for t in _all_brace_targets(text, TITLE_RE):
        return re.sub(r"\s+", " ", t.replace("\\\\", " ")).strip()
    return None


def authors(text: str):
    """Return the author list from the first \\author{...}, split on \\and and commas
    (best-effort: the exact split is not load-bearing, only the count, which gates a
    coauthorship footnote downstream)."""
    for raw in _all_brace_targets(text, AUTHOR_RE):
        parts = re.split(r"\\and\b|,", raw)
        names = [re.sub(r"\s+", " ", p).strip() for p in parts]
        return [n for n in names if n]
    return []


def labels(text: str):
    out = []
    for k in _all_brace_targets(text, LABEL_RE):
        k = k.strip()
        if k and k not in out:
            out.append(k)
    return out


def figures(text: str):
    out = []
    for g in _all_brace_targets(text, GRAPHICS_RE):
        g = g.strip()
        if g and g not in out:
            out.append(g)
    return out


def detect_bib(text: str, packages, src_abs):
    """Return ``(backend, bib_files, bib_style)``. biblatex when the biblatex package
    or \\addbibresource is present; bibtex when \\bibliography is present; else
    ``(None, ...)``."""
    pkg_names = {p["name"] for p in packages}
    bib_files, style = [], None

    sm = next(_all_brace_targets(text, BIBSTYLE_RE), None)
    if sm:
        style = sm.strip()

    is_biblatex = ("biblatex" in pkg_names) or bool(ADDBIB_RE.search(text))
    if is_biblatex:
        for res in _all_brace_targets(text, ADDBIB_RE):
            res = res.strip()
            if res and res not in bib_files:
                bib_files.append(res)
        return "biblatex", bib_files, style

    if BIBLIOGRAPHY_RE.search(text):
        for arg in _all_brace_targets(text, BIBLIOGRAPHY_RE):
            for nm in arg.split(","):
                nm = nm.strip()
                if not nm:
                    continue
                fn = nm if nm.endswith(".bib") else nm + ".bib"
                if fn not in bib_files:
                    bib_files.append(fn)
        return "bibtex", bib_files, style

    return None, bib_files, style


def detect_engine_hint(packages):
    pkg_names = {p["name"] for p in packages}
    if pkg_names & UNICODE_ENGINE_PACKAGES:
        return "xelatex"  # xelatex or lualatex; xelatex is the conservative default
    return "pdflatex"


# --------------------------------------------------------------------------- #
# Source discovery.                                                            #
# --------------------------------------------------------------------------- #

BEGIN_DOC_RE = re.compile(r"\\begin\s*\{document\}")


def list_tex_files(src_abs):
    out = []
    for root, dirs, files in os.walk(src_abs):
        dirs[:] = [d for d in dirs if d not in (".git", ".svn", "node_modules")]
        for f in files:
            if f.endswith(".tex"):
                out.append(os.path.join(root, f))
    return sorted(out)


def list_local_sty(src_abs):
    out = []
    for root, dirs, files in os.walk(src_abs):
        dirs[:] = [d for d in dirs if d not in (".git", ".svn", "node_modules")]
        for f in files:
            if f.endswith(".sty"):
                out.append(os.path.relpath(os.path.join(root, f), src_abs))
    return sorted(out)


def find_entry_file(src_abs):
    """Find the entry .tex (the one with \\begin{document}). Returns
    ``(entry_abs_or_None, root_candidates)``.

    When several files carry \\begin{document}, the ones that are \\input/\\subfile'd
    by another file are dropped (the subfiles idiom) before deciding ambiguity;
    ``root_candidates`` is what remains, so ``len > 1`` means a genuinely ambiguous
    entry the agent must disambiguate."""
    candidates = []
    file_text = {}
    for p in list_tex_files(src_abs):
        try:
            t = strip_comments(_read(p))
        except OSError:
            continue
        file_text[p] = t
        if BEGIN_DOC_RE.search(t):
            candidates.append(p)

    if not candidates:
        return None, []

    # which candidate files are pulled in by some other file?
    included = set()
    for p, t in file_text.items():
        for m in INCLUDE_RE.finditer(t):
            try:
                content, _ = _brace_arg(t, m.end()) if m.end() < len(t) and t[m.end()] == "{" else (None, m.end())
            except ParseError:
                content = None
            if not content:
                continue
            r = _resolve_include(content, os.path.dirname(p)) or _resolve_include(content, src_abs)
            if r:
                included.add(os.path.abspath(r))

    roots = [c for c in candidates if os.path.abspath(c) not in included] or candidates
    if len(roots) == 1:
        return roots[0], roots
    # prefer a top-level main.tex when still ambiguous (still report the ambiguity)
    for pref in roots:
        if os.path.basename(pref) == "main.tex" and os.path.dirname(pref) == src_abs.rstrip("/"):
            return pref, roots
    return roots[0], roots


# --------------------------------------------------------------------------- #
# Slug suggestion.                                                              #
# --------------------------------------------------------------------------- #

def suggest_slug(dir_path: str, taken: set):
    """Suggest a short lowercase slug from the directory basename's word initials
    (``all-heights-k3-surfaces`` -> ``ahks``), deduped against ``taken``."""
    base = os.path.basename(os.path.normpath(dir_path)).lower()
    words = re.split(r"[^a-z0-9]+", base)
    words = [w for w in words if w]
    if words and len(words) >= 2:
        slug = "".join(w[0] for w in words)
    elif words:
        slug = words[0][:4]
    else:
        slug = "src"
    slug = re.sub(r"[^a-z0-9]", "", slug) or "src"
    cand, n = slug, 2
    while cand in taken:
        cand = "%s%d" % (slug, n)
        n += 1
    taken.add(cand)
    return cand


# --------------------------------------------------------------------------- #
# Per-source cataloguing.                                                       #
# --------------------------------------------------------------------------- #

def inspect_one(plan, entry, run_engine):
    """Catalogue one source (a ``sources[]`` entry, mutated in place) and append any
    Tier-2 flags to ``plan``. ``entry['dir']`` is the source directory as given."""
    src_dir = entry["dir"]
    src_abs = src_dir if os.path.isabs(src_dir) else os.path.abspath(src_dir)
    slug = entry.get("slug") or "?"

    if not os.path.isdir(src_abs):
        planlib.add_flag(plan, tier=2, stage="inspect", kind="source-dir-not-found",
                         severity="blocker", slug=entry.get("slug") or None,
                         message="Source directory does not exist: %s" % src_dir,
                         location=src_dir)
        return

    entry["local_sty"] = list_local_sty(src_abs)

    entry_abs, root_candidates = find_entry_file(src_abs)
    if entry_abs is None:
        planlib.add_flag(plan, tier=2, stage="inspect", kind="no-entry-file",
                         severity="blocker", slug=entry.get("slug") or None,
                         message="No .tex with \\begin{document} found under %s" % src_dir,
                         location=src_dir)
        return
    if len(root_candidates) > 1:
        # more than one un-included \begin{document} -> the agent must pick
        chosen = os.path.relpath(entry_abs, src_abs)
        others = ", ".join(sorted(os.path.relpath(c, src_abs) for c in root_candidates))
        planlib.add_flag(plan, tier=2, stage="inspect", kind="multiple-entry-files",
                         severity="blocker", slug=entry.get("slug") or None,
                         message=("Multiple \\begin{document} files (%s); guessed entry "
                                  "%s — confirm or override." % (others, chosen)),
                         location=src_dir)

    entry["entry_file"] = os.path.relpath(entry_abs, src_abs)

    # Inline-expand includes, then split the document at the first \begin{document}.
    includes, unresolved, seen = [], [], set()
    full = expand_includes(entry_abs, src_abs, includes, unresolved, seen)
    entry["includes"] = includes
    bd = BEGIN_DOC_RE.search(full)
    preamble = full[:bd.start()] if bd else full
    if not bd:
        planlib.add_flag(plan, tier=2, stage="inspect", kind="no-begin-document",
                         severity="blocker", slug=entry.get("slug") or None,
                         message="Entry %s has no \\begin{document} after include expansion."
                                 % entry["entry_file"], location=entry["entry_file"])

    for tgt in unresolved:
        planlib.add_flag(plan, tier=2, stage="inspect", kind="unresolved-include",
                         severity="warn", slug=entry.get("slug") or None,
                         message=("Could not resolve \\input/\\include target %r — the "
                                  "catalogue of its packages/macros/labels may be "
                                  "incomplete." % tgt), location=entry["entry_file"])

    # Document class (+ exotic-class gate).
    cls, cls_opts = parse_document_class(preamble)
    if cls is None:
        planlib.add_flag(plan, tier=2, stage="inspect", kind="missing-document-class",
                         severity="warn", slug=entry.get("slug") or None,
                         message="No \\documentclass found in the (expanded) preamble of %s."
                                 % entry["entry_file"], location=entry["entry_file"])
    else:
        entry["document_class"] = cls
        entry["class_options"] = cls_opts
        if cls in REFUSE_CLASSES:
            planlib.add_flag(plan, tier=2, stage="inspect", kind="exotic-class",
                             severity="blocker", slug=entry.get("slug") or None,
                             message=("Document class %r is out of scope (beamer/standalone/"
                                      "poster/letter-like). This formula does NOT handle it — "
                                      "STOP and report rather than guess." % cls),
                             location=entry["entry_file"])
        elif cls not in KNOWN_GOOD_CLASSES:
            planlib.add_flag(plan, tier=2, stage="inspect", kind="unrecognized-class",
                             severity="warn", slug=entry.get("slug") or None,
                             message=("Document class %r is not on the known article/report-like "
                                      "list — confirm it behaves like one (standard \\section…) "
                                      "before merging." % cls), location=entry["entry_file"])

    # Packages, macros.
    packages = parse_packages(preamble)
    entry["packages"] = packages
    macros, macro_errors = parse_macros(preamble)
    entry["macros"] = macros
    for snip in macro_errors:
        planlib.add_flag(plan, tier=2, stage="inspect", kind="unparseable-macro",
                         severity="warn", slug=entry.get("slug") or None,
                         message=("Could not parse a macro definition soundly near %r — "
                                  "review by hand when resolving collisions." % snip),
                         location=entry["entry_file"])

    # Programmatic preamble TeX the mechanical catalogue cannot model soundly.
    prog = sorted({tok for tok in ("\\csname", "\\catcode", "\\@ifpackageloaded",
                                   "\\@ifclassloaded") if tok in preamble})
    if prog:
        planlib.add_flag(plan, tier=2, stage="inspect", kind="programmatic-preamble",
                         severity="warn", slug=entry.get("slug") or None,
                         message=("Preamble uses programmatic TeX (%s) that this catalogue does "
                                  "not model; its hoisting/effects were NOT analysed — review "
                                  "this block when hoisting the preamble." % ", ".join(prog)),
                         location=entry["entry_file"])

    # Title, authors.
    title = first_title(full)
    if title:
        entry["title"] = title
    auth = authors(full)
    if auth:
        entry["authors"] = auth

    # Labels, figures.
    entry["labels"] = labels(full)
    entry["figures"] = figures(full)

    # Bibliography backend + style.
    backend, bib_files, bib_style = detect_bib(full, packages, src_abs)
    if backend:
        entry["bib_backend"] = backend
    if bib_files:
        entry["bib_files"] = bib_files
    if bib_style:
        entry["bib_style"] = bib_style
        if bib_style not in STANDARD_BST:
            planlib.add_flag(plan, tier=2, stage="inspect", kind="exotic-bibstyle",
                             severity="warn", slug=entry.get("slug") or None,
                             message=("Bibliography style %r is non-standard; the compile gate "
                                      "may need the matching .bst on the path." % bib_style),
                             location=entry["entry_file"])
    if backend is None:
        on_disk = [f for f in os.listdir(src_abs) if f.endswith(".bib")] if os.path.isdir(src_abs) else []
        if on_disk:
            planlib.add_flag(plan, tier=2, stage="inspect", kind="bib-backend-undetected",
                             severity="info", slug=entry.get("slug") or None,
                             message=("Found .bib file(s) %s but no \\bibliography/\\addbibresource "
                                      "in the document — confirm the bibliography backend."
                                      % ", ".join(sorted(on_disk))), location=src_dir)

    # Engine hint vs run engine.
    hint = detect_engine_hint(packages)
    entry["tex_engine"] = hint
    if hint != "pdflatex" and run_engine == "pdflatex":
        planlib.add_flag(plan, tier=2, stage="inspect", kind="engine-mismatch",
                         severity="warn", slug=entry.get("slug") or None,
                         message=("Source pulls in Unicode-TeX packages (hint: %s) but the run "
                                  "engine is pdflatex — set tex_engine accordingly or expect a "
                                  "baseline failure." % hint), location=entry["entry_file"])

    sys.stderr.write(
        "[inspect] %-8s class=%-12s pkgs=%-3d macros=%-4d labels=%-4d figs=%-2d "
        "bib=%s authors=%d entry=%s\n" % (
            slug, entry.get("document_class", "?"), len(packages), len(macros),
            len(entry["labels"]), len(entry["figures"]), entry.get("bib_backend", "-"),
            len(entry.get("authors", [])), entry["entry_file"]))


# --------------------------------------------------------------------------- #
# Driver.                                                                       #
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description="catalogue LaTeX sources into plan.json")
    ap.add_argument("--plan", required=True, help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--sources", help="comma-separated ordered source dirs (seeds a new plan.json)")
    ap.add_argument("--dest", default="", help="destination project dir (\"\" => blank merged-latex/)")
    ap.add_argument("--engine", default="pdflatex", help="run-wide tex engine (pdflatex|xelatex|lualatex)")
    args = ap.parse_args(argv)

    if os.path.exists(args.plan):
        plan = planlib.load(args.plan)
        if args.sources and not plan.get("sources"):
            plan = planlib.new(args.sources, args.dest, args.engine)
    else:
        if not args.sources:
            sys.stderr.write("inspect_sources: --sources is required to seed a new plan.json.\n")
            return 2
        plan = planlib.new(args.sources, args.dest, args.engine)

    sources = plan.get("sources", [])
    if not sources:
        sys.stderr.write("inspect_sources: plan.json has no sources to catalogue.\n")
        return 2

    run_engine = plan.get("vars", {}).get("tex_engine", args.engine or "pdflatex")

    # Idempotent re-run: drop this stage's prior flags (a restart may re-invoke
    # inspect) but remember any resolution the agent already wrote so re-derived
    # flags keep it.
    saved_resolutions = {
        (f.get("kind"), f.get("slug"), f.get("message")): f.get("resolution")
        for f in plan.get("flags", [])
        if f.get("stage") == "inspect" and f.get("resolution")
    }
    plan["flags"] = [f for f in plan.get("flags", []) if f.get("stage") != "inspect"]

    # Suggest slugs for any source the agent has not already named.
    taken = {s["slug"] for s in sources if s.get("slug")}
    for s in sources:
        if not s.get("slug"):
            s["slug"] = suggest_slug(s["dir"], taken)

    for s in sorted(sources, key=lambda e: e.get("index", 0)):
        inspect_one(plan, s, run_engine)

    # Re-apply any resolution the agent had written for an identical earlier flag.
    for f in plan.get("flags", []):
        key = (f.get("kind"), f.get("slug"), f.get("message"))
        if f.get("stage") == "inspect" and saved_resolutions.get(key):
            f["resolution"] = saved_resolutions[key]

    n_flags = len(plan.get("flags", []))
    n_block = sum(1 for f in plan["flags"] if f.get("severity") == "blocker")
    planlib.add_log(plan, "inspect",
                    "Catalogued %d source(s); %d flag(s) raised (%d blocker)."
                    % (len(sources), n_flags, n_block))

    try:
        planlib.validate(plan)
    except Exception as e:  # jsonschema.ValidationError or absent
        sys.stderr.write("inspect_sources: plan.json failed schema validation: %s\n" % e)
        planlib.save(args.plan, plan)
        return 1

    planlib.save(args.plan, plan)
    sys.stderr.write("[inspect] wrote %s — %d source(s), %d flag(s)%s.\n"
                     % (args.plan, len(sources), n_flags,
                        " (%d BLOCKER — resolve before proceeding)" % n_block if n_block else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
