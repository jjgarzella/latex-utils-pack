#!/usr/bin/env python3
"""rewrite_paths — mirror each source into contents/<slug>/ and rewrite relative paths.

Tier 1 (mechanical, literal path rewriting) + Tier 2 (flag-and-defer). Owner: pt-axx (T5).
Part of the mol-latex-concat formula (latex-utils pack). Runs FIRST in the
`transform-sources` step; extract_body.py runs immediately after it.

Reads  plan.json: sources[].dir/slug/entry_file.
Writes the dest tree + plan.json. For each source it:
  1. copies the source tree VERBATIM into <dest>/contents/<slug>/ (sub-.tex, figures,
     local .sty/.cls/.bib; LaTeX build artifacts like .aux/.log/.bbl are skipped), then
  2. rewrites the relative \\input/\\include/\\subfile/\\includegraphics targets in every
     mirrored .tex so they resolve from the dest ROOT (where the master .tex lives, since
     the master \\input{contents/<slug>/main}s each chapter):
         \\input{intro}            -> \\input{contents/<slug>/intro}
         \\includegraphics{fig}    -> \\includegraphics{contents/<slug>/fig}
  3. records mirror_dir / mirror_entry / chapter_main on the source entry.

Tier-2 flags (flag-and-defer, NEVER guess):
  * \\graphicspath{...}        — figure lookup is governed by graphicspath dirs, not by the
                                 literal \\includegraphics argument, so \\includegraphics in
                                 that source is left untouched for the agent to resolve.
  * a target that does not resolve to a real mirrored file (computed / \\input from a macro
    / a path escaping the source dir).
  * an absolute path (left as-is; it will not move with the mirror).

Mechanical soundness / idempotency:
  * Comments are MASKED (not stripped) so a path in a `% comment` is never rewritten and
    byte offsets still line up for an in-place splice of the original file.
  * Every run re-copies each source file over the mirror before rewriting, so the rewrite
    always starts from pristine source text — re-running can never double-prefix a path.

CLI:
  rewrite_paths.py --plan PLAN --dest DIR
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planlib  # noqa: E402

PRODUCER = "rewrite_paths"
STAGE = "transform"

# Directories never mirrored into the dest.
SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".gc"}

# LaTeX build artifacts: skipped when mirroring (matched by filename suffix so the
# double extensions .synctex.gz / .run.xml are caught too). Figures keep their real
# extensions (.pdf/.png/.jpg/.eps ...), so those are deliberately NOT here.
AUX_SUFFIXES = (
    ".aux", ".log", ".out", ".toc", ".lof", ".lot", ".bbl", ".blg",
    ".synctex.gz", ".fls", ".fdb_latexmk", ".nav", ".snm", ".vrb",
    ".idx", ".ind", ".ilg", ".glo", ".gls", ".glg", ".acn", ".acr",
    ".alg", ".bcf", ".run.xml", ".xdv", ".dvi", ".spl", ".lol",
)

# Extensions the graphics package appends when an \includegraphics target has none.
GRAPHICS_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps", ".mps",
                 ".gif", ".tif", ".tiff", ".bmp")

# The four path-bearing commands this formula rewrites (bead pt-axx). Negative
# lookahead keeps \includegraphics from matching \include, \subfileinclude from
# matching \subfile, etc.
CMD_RE = re.compile(r"\\(input|include|subfile|includegraphics)(?![A-Za-z@])")
GRAPHICSPATH_RE = re.compile(r"\\graphicspath(?![A-Za-z@])")


# --------------------------------------------------------------------------- #
# TeX scanning (sound over the well-behaved subset; same-length comment mask).  #
# --------------------------------------------------------------------------- #

def mask_comments(text: str) -> str:
    """Return a string of identical length with every TeX line comment (an unescaped
    ``%`` to just before the end of line) replaced by spaces. A ``%`` is escaped iff
    preceded by an odd run of backslashes. Same length => offsets found on the mask are
    valid indices into the raw text, so a path inside a comment is never rewritten."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2  # skip the escaped next char (incl. \% and \\)
            continue
        if c == "%":
            j = i
            while j < n and text[j] not in "\r\n":
                out[j] = " "
                j += 1
            i = j
            continue
        i += 1
    return "".join(out)


def skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def read_brace(s: str, i: int):
    """If ``s[i]`` is ``{``, return ``(content, content_start, content_end, past_close)``
    honouring nested braces and escaped ``\\{``/``\\}``; else ``(None, i, i, i)``. An
    unterminated group is reported as no-arg (the caller leaves it alone)."""
    if i >= len(s) or s[i] != "{":
        return None, i, i, i
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
                return s[i + 1:j], i + 1, j, j + 1
        j += 1
    return None, i, i, i


def read_bracket(s: str, i: int):
    """If ``s[i]`` is ``[``, return ``(content, past_close)`` (brackets do not nest but
    may wrap a brace group, e.g. ``[width=\\linewidth]``); else ``(None, i)``."""
    if i >= len(s) or s[i] != "[":
        return None, i
    j = i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            _, _, _, j = read_brace(s, j)
            continue
        if c == "]":
            return s[i + 1:j], j + 1
        j += 1
    return None, i


# --------------------------------------------------------------------------- #
# Mirroring.                                                                    #
# --------------------------------------------------------------------------- #

def _skip_file(name: str) -> bool:
    return name.endswith(AUX_SUFFIXES)


def mirror_source(src_abs: str, mirror_abs: str) -> int:
    """Copy the source tree verbatim into ``mirror_abs`` (overwriting), skipping VCS
    dirs and LaTeX build artifacts. Returns the number of files copied. Existing files
    are overwritten so the rewrite below always starts from pristine source text."""
    n = 0
    for root, dirs, files in os.walk(src_abs):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = os.path.relpath(root, src_abs)
        dest_root = mirror_abs if rel == "." else os.path.join(mirror_abs, rel)
        os.makedirs(dest_root, exist_ok=True)
        for f in files:
            if _skip_file(f):
                continue
            try:
                shutil.copy2(os.path.join(root, f), os.path.join(dest_root, f))
                n += 1
            except OSError as e:
                sys.stderr.write("[transform] WARN could not copy %s: %s\n"
                                 % (os.path.join(root, f), e))
    return n


def list_mirrored_tex(mirror_abs: str):
    out = []
    for root, dirs, files in os.walk(mirror_abs):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".tex"):
                out.append(os.path.join(root, f))
    return sorted(out)


# --------------------------------------------------------------------------- #
# Path rewriting.                                                              #
# --------------------------------------------------------------------------- #

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _resolves(target: str, mirror_abs: str, is_graphic: bool) -> bool:
    """Does ``target`` (relative to the mirror root) point at a real mirrored file?
    For graphics, try the graphics-package extension search; for \\input-likes, try the
    literal name and a ``.tex`` suffix."""
    base = os.path.normpath(os.path.join(mirror_abs, target))
    if os.path.isfile(base):
        return True
    if is_graphic:
        return any(os.path.isfile(base + ext) for ext in GRAPHICS_EXTS)
    return not target.endswith(".tex") and os.path.isfile(base + ".tex")


def rewrite_text(raw: str, slug: str, mirror_abs: str, has_graphicspath: bool):
    """Rewrite the path arguments in one mirrored .tex file.

    Returns ``(new_text, unresolved, absolutes)`` where ``unresolved`` / ``absolutes``
    are lists of ``"\\cmd{target}"`` strings for the flag channel. \\includegraphics is
    left untouched when the source sets \\graphicspath (figure lookup is governed by the
    graphicspath dirs, not the literal argument — defer to the agent)."""
    masked = mask_comments(raw)
    prefix = "contents/%s/" % slug
    edits = []            # (start, end, replacement) in raw coords
    unresolved, absolutes = [], []

    for m in CMD_RE.finditer(masked):
        cmd = m.group(1)
        is_graphic = (cmd == "includegraphics")
        i = skip_ws(masked, m.end())
        if is_graphic:
            _, i2 = read_bracket(masked, i)      # optional [width=...] etc.
            i = skip_ws(masked, i2)

        content, c0, c1, _past = read_brace(masked, i)
        if content is None:
            if cmd == "input":
                # plain-TeX bare form: \input filename (whitespace-delimited).
                mt = re.match(r"[^\s{}\\]+", masked[i:])
                if not mt:
                    continue
                c0, c1 = i + mt.start(), i + mt.end()
            else:
                continue

        target = raw[c0:c1].strip()
        if not target:
            continue
        if is_graphic and has_graphicspath:
            continue                              # governed by \graphicspath -> defer
        if target.startswith(prefix):
            continue                              # already prefixed (defensive)
        ref = "\\%s{%s}" % (cmd, target)
        if os.path.isabs(target) or "://" in target:
            absolutes.append(ref)
            continue

        # Splice the prefix in front of the (stripped) target, preserving any
        # surrounding whitespace that lived inside the braces.
        seg = raw[c0:c1]
        lead = len(seg) - len(seg.lstrip())
        s0 = c0 + lead
        s1 = s0 + len(target)
        edits.append((s0, s1, prefix + target))
        if not _resolves(target, mirror_abs, is_graphic):
            unresolved.append(ref)

    for s0, s1, rep in sorted(edits, key=lambda e: e[0], reverse=True):
        raw = raw[:s0] + rep + raw[s1:]
    return raw, unresolved, absolutes


# --------------------------------------------------------------------------- #
# Per-source driver.                                                           #
# --------------------------------------------------------------------------- #

def transform_one(plan, entry, dest_abs):
    """Mirror + path-rewrite one source (a ``sources[]`` entry, mutated in place);
    append any Tier-2 flags to ``plan``."""
    slug = entry.get("slug")
    src_dir = entry.get("dir")
    if not slug:
        planlib.add_flag(plan, tier=2, stage=STAGE, kind="missing-slug", severity="blocker",
                         slug=None, message="Source %r has no slug; run inspect first." % src_dir,
                         location=src_dir)["producer"] = PRODUCER
        return
    src_abs = src_dir if os.path.isabs(src_dir) else os.path.abspath(src_dir)
    if not os.path.isdir(src_abs):
        planlib.add_flag(plan, tier=2, stage=STAGE, kind="source-dir-not-found", severity="blocker",
                         slug=slug, message="Source directory does not exist: %s" % src_dir,
                         location=src_dir)["producer"] = PRODUCER
        return

    mirror_rel = "contents/%s" % slug
    mirror_abs = os.path.join(dest_abs, mirror_rel)
    n_files = mirror_source(src_abs, mirror_abs)

    tex_files = list_mirrored_tex(mirror_abs)
    has_gp = any(GRAPHICSPATH_RE.search(mask_comments(_read(f))) for f in tex_files)
    if has_gp:
        planlib.add_flag(
            plan, tier=2, stage=STAGE, kind="graphicspath", severity="warn", slug=slug,
            message=("Source %s sets \\graphicspath; \\includegraphics targets were NOT "
                     "rewritten (their lookup is governed by the graphicspath dirs). Point "
                     "\\graphicspath at the mirrored figure dirs (under %s/) by hand."
                     % (slug, mirror_rel)),
            location=mirror_rel)["producer"] = PRODUCER

    unresolved, absolutes, n_rewritten = [], [], 0
    for f in tex_files:
        raw = _read(f)
        new, unres, absol = rewrite_text(raw, slug, mirror_abs, has_gp)
        if new != raw:
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(new)
            n_rewritten += 1
        rel = os.path.relpath(f, dest_abs)
        unresolved += [(rel, r) for r in unres]
        absolutes += [(rel, r) for r in absol]

    if unresolved:
        planlib.add_flag(
            plan, tier=2, stage=STAGE, kind="unresolved-path", severity="warn", slug=slug,
            message=("%d path target(s) did not resolve to a mirrored file and may be "
                     "computed/macro-built or escape the source dir — verify by hand: %s"
                     % (len(unresolved), "; ".join("%s in %s" % (r, f) for f, r in unresolved[:12])
                        + (" …" if len(unresolved) > 12 else ""))),
            location=unresolved[0][0])["producer"] = PRODUCER
    if absolutes:
        planlib.add_flag(
            plan, tier=2, stage=STAGE, kind="absolute-path", severity="info", slug=slug,
            message=("%d absolute path(s) left unchanged (they do not move with the mirror): %s"
                     % (len(absolutes), "; ".join("%s in %s" % (r, f) for f, r in absolutes[:12]))),
            location=absolutes[0][0])["producer"] = PRODUCER

    entry["mirror_dir"] = mirror_rel
    if entry.get("entry_file"):
        entry["mirror_entry"] = "%s/%s" % (mirror_rel, entry["entry_file"])
    # chapter_main is the destination the stripped body lands at (extract_body writes it);
    # the wire-main step \input{contents/<slug>/main}s exactly this.
    entry["chapter_main"] = "%s/main.tex" % mirror_rel

    sys.stderr.write(
        "[transform] %-8s mirrored %d file(s) -> %s/ ; rewrote paths in %d tex%s\n"
        % (slug, n_files, mirror_rel, n_rewritten,
           " ; %d unresolved" % len(unresolved) if unresolved else ""))


# --------------------------------------------------------------------------- #
# Idempotent re-run: drop this producer's prior flags, keep agent resolutions.  #
# --------------------------------------------------------------------------- #

def _reset_my_flags(plan):
    saved = {
        (f.get("kind"), f.get("slug"), f.get("message")): f.get("resolution")
        for f in plan.get("flags", [])
        if f.get("producer") == PRODUCER and f.get("resolution")
    }
    plan["flags"] = [f for f in plan.get("flags", []) if f.get("producer") != PRODUCER]
    return saved


def _restore_resolutions(plan, saved):
    for f in plan.get("flags", []):
        if f.get("producer") != PRODUCER:
            continue
        key = (f.get("kind"), f.get("slug"), f.get("message"))
        if saved.get(key):
            f["resolution"] = saved[key]


# --------------------------------------------------------------------------- #
# Driver.                                                                       #
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description="mirror sources into contents/<slug>/ and rewrite relative paths")
    ap.add_argument("--plan", required=True, help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", required=True, help="destination project directory")
    args = ap.parse_args(argv)

    if not os.path.exists(args.plan):
        sys.stderr.write("rewrite_paths: plan.json not found at %s (run inspect first).\n" % args.plan)
        return 2
    plan = planlib.load(args.plan)

    dest_abs = os.path.abspath(args.dest)
    if not os.path.isdir(dest_abs):
        sys.stderr.write("rewrite_paths: dest dir %s does not exist (run scaffold-dest first).\n" % dest_abs)
        return 2

    sources = plan.get("sources", [])
    if not sources:
        sys.stderr.write("rewrite_paths: plan.json has no sources.\n")
        return 2

    saved = _reset_my_flags(plan)
    for s in sorted(sources, key=lambda e: e.get("index", 0)):
        transform_one(plan, s, dest_abs)
    _restore_resolutions(plan, saved)

    n_flags = sum(1 for f in plan["flags"] if f.get("producer") == PRODUCER)
    planlib.add_log(plan, "transform:paths",
                    "Mirrored %d source(s) into contents/<slug>/ and rewrote relative "
                    "\\input/\\include/\\subfile/\\includegraphics paths; %d flag(s) raised."
                    % (len(sources), n_flags))

    try:
        planlib.validate(plan)
    except Exception as e:
        sys.stderr.write("rewrite_paths: plan.json failed schema validation: %s\n" % e)
        planlib.save(args.plan, plan)
        return 1

    planlib.save(args.plan, plan)
    sys.stderr.write("[transform] rewrite_paths wrote %s — %d source(s), %d flag(s).\n"
                     % (args.plan, len(sources), n_flags))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
