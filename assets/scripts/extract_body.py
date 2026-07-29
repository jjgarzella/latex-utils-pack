#!/usr/bin/env python3
"""extract_body — strip each entry file down to a bare chapter body.

Tier 1 (wrapper stripping) + Tier 2 (flag-and-defer).
Part of the mol-latex-concat formula (latex-utils pack). Runs immediately AFTER
rewrite_paths.py in the `transform-sources` step (it reads the mirrored, path-rewritten
entry file rewrite_paths produced).

Reads  plan.json: sources[].slug/entry_file/chapter_main/authors; the mirrored entry at
       <dest>/contents/<slug>/<entry_file>.
Writes <dest>/contents/<slug>/main.tex (the value of sources[].chapter_main) + plan.json.
       KEEPS the document body verbatim; REMOVES \\documentclass + the whole preamble
       (everything before \\begin{document}), \\begin{document}/\\end{document} and any
       trailing matter, \\maketitle, \\title{...}, \\date{...}, \\author{...}, every inner
       \\tableofcontents, and the trailing \\bibliography{...}/\\bibliographystyle{...}/
       \\printbibliography (citation keys flow to the merged bib). CONVERTS
       \\begin{abstract}...\\end{abstract} into a \\section*{Abstract} at the chapter top.
       NO sectioning demotion (papers top out at \\section under the master's \\chapter).

Tier-2 flags (flag-and-defer, NEVER guess):
  * coauthorship (>1 \\author) — preserved as a FLAGGED chapter footnote the agent adds
    when it wires the \\chapter in (wire-main).
  * a manual \\begin{thebibliography} — left in place (its \\bibitem keys back the chapter's
    \\cite uses); the agent decides how it folds into the merged bibliography.
  * an entry whose \\begin{document} boundary cannot be found soundly.

Mechanical soundness / idempotency:
  * Comments are MASKED (not stripped) so the kept body stays VERBATIM — a commented-out
    \\maketitle is never removed, and the agent's prose/comments survive untouched.
  * The body is sliced from the RAW text at offsets found on the same-length mask.
  * Re-running the transform step is idempotent: rewrite_paths re-establishes the pristine
    (path-rewritten) entry, so a second strip yields the same main.tex. When the entry IS
    main.tex (stripped in place), a re-run finds no \\begin{document} but a recorded
    `transformed` and leaves the already-stripped body alone.

CLI:
  extract_body.py --plan PLAN --dest DIR
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planlib  # noqa: E402
# Share the TeX scanner with rewrite_paths.py (the other half of this T5 transform pair).
from rewrite_paths import mask_comments, skip_ws, read_brace, read_bracket  # noqa: E402

PRODUCER = "extract_body"
STAGE = "transform"

BEGIN_DOC_RE = re.compile(r"\\begin\s*\{document\}")
END_DOC_RE = re.compile(r"\\end\s*\{document\}")
BEGIN_ABSTRACT_RE = re.compile(r"\\begin\s*\{abstract\}")
END_ABSTRACT_RE = re.compile(r"\\end\s*\{abstract\}")
THEBIB_RE = re.compile(r"\\begin\s*\{thebibliography\}")

# Frontmatter / wrapper commands removed from the body.
ARGLESS_REMOVE = ("maketitle", "tableofcontents")
BRACED_REMOVE = ("title", "date", "author", "bibliography", "bibliographystyle")


# --------------------------------------------------------------------------- #
# Span collection over the masked body (edits applied to the raw body).         #
# --------------------------------------------------------------------------- #

def _expand_to_line(text: str, s: int, e: int):
    """Grow ``[s, e)`` to swallow the whole physical line when the command stands alone
    on it (so removing it leaves no stranded blank line); otherwise return it unchanged."""
    ls = s
    while ls > 0 and text[ls - 1] in " \t":
        ls -= 1
    le = e
    while le < len(text) and text[le] in " \t":
        le += 1
    alone = (ls == 0 or text[ls - 1] == "\n") and (le >= len(text) or text[le] in "\r\n")
    if not alone:
        return s, e
    if le < len(text) and text[le] == "\r":
        le += 1
    if le < len(text) and text[le] == "\n":
        le += 1
    return ls, le


def _argless_spans(bmasked: str, cmd: str):
    for m in re.finditer(r"\\" + cmd + r"(?![A-Za-z@])", bmasked):
        yield _expand_to_line(bmasked, m.start(), m.end())


def _braced_spans(bmasked: str, cmd: str):
    """Spans of ``\\cmd[opt]{arg}`` (optional bracket, required brace). A bare ``\\cmd``
    with no brace argument is left alone — it is not the frontmatter form we strip."""
    for m in re.finditer(r"\\" + cmd + r"(?![A-Za-z@])", bmasked):
        i = skip_ws(bmasked, m.end())
        _, i = read_bracket(bmasked, i)
        i = skip_ws(bmasked, i)
        content, _c0, _c1, past = read_brace(bmasked, i)
        if content is None:
            continue
        yield _expand_to_line(bmasked, m.start(), past)


def _printbib_spans(bmasked: str):
    for m in re.finditer(r"\\printbibliography(?![A-Za-z@])", bmasked):
        i = skip_ws(bmasked, m.end())
        _, i = read_bracket(bmasked, i)
        yield _expand_to_line(bmasked, m.start(), i)


def strip_body(raw: str):
    """Reduce one (mirrored, path-rewritten) entry file to its chapter body.

    Returns ``(body_text_or_None, info)`` where ``info`` carries the boundary status and
    detected constructs for the flag channel. ``None`` body => no \\begin{document}."""
    masked = mask_comments(raw)
    info = {"no_begin": False, "no_end": False, "thebib": False, "abstracts": 0}

    bd = BEGIN_DOC_RE.search(masked)
    if not bd:
        info["no_begin"] = True
        return None, info
    ed = END_DOC_RE.search(masked, bd.end())
    if not ed:
        info["no_end"] = True
    b0 = bd.end()
    b1 = ed.start() if ed else len(raw)

    body = raw[b0:b1]
    bmask = masked[b0:b1]
    info["thebib"] = bool(THEBIB_RE.search(bmask))

    edits = []  # (start, end, replacement) in body coords

    # Abstract environment -> \section*{Abstract} heading (drop the \end{abstract}).
    for m in BEGIN_ABSTRACT_RE.finditer(bmask):
        edits.append((m.start(), m.end(), "\\section*{Abstract}"))
        info["abstracts"] += 1
    for m in END_ABSTRACT_RE.finditer(bmask):
        s, e = _expand_to_line(bmask, m.start(), m.end())
        edits.append((s, e, ""))

    # Frontmatter / wrapper removals.
    for cmd in ARGLESS_REMOVE:
        edits += [(s, e, "") for s, e in _argless_spans(bmask, cmd)]
    for cmd in BRACED_REMOVE:
        edits += [(s, e, "") for s, e in _braced_spans(bmask, cmd)]
    edits += [(s, e, "") for s, e in _printbib_spans(bmask)]

    for s, e, rep in sorted(edits, key=lambda t: t[0], reverse=True):
        body = body[:s] + rep + body[e:]

    body = re.sub(r"\n{3,}", "\n\n", body).lstrip("\r\n").rstrip() + "\n"
    return body, info


# --------------------------------------------------------------------------- #
# Per-source driver.                                                           #
# --------------------------------------------------------------------------- #

def extract_one(plan, entry, dest_abs):
    """Strip one mirrored entry to its chapter body (``sources[]`` entry mutated in place);
    append any Tier-2 flags to ``plan``."""
    slug = entry.get("slug")
    entry_file = entry.get("entry_file")
    if not slug or not entry_file:
        planlib.add_flag(plan, tier=2, stage=STAGE, kind="missing-entry-file", severity="blocker",
                         slug=slug, message="Source %r has no entry_file; run inspect first." % (slug or entry.get("dir")),
                         location=entry.get("dir"))["producer"] = PRODUCER
        return

    chapter_main_rel = entry.get("chapter_main") or "contents/%s/main.tex" % slug
    mirror_entry_rel = entry.get("mirror_entry") or "contents/%s/%s" % (slug, entry_file)
    mirror_entry_abs = os.path.join(dest_abs, mirror_entry_rel)
    chapter_main_abs = os.path.join(dest_abs, chapter_main_rel)

    if not os.path.isfile(mirror_entry_abs):
        planlib.add_flag(plan, tier=2, stage=STAGE, kind="mirror-missing", severity="blocker",
                         slug=slug, message=("Mirrored entry %s not found — run rewrite_paths "
                                             "before extract_body." % mirror_entry_rel),
                         location=mirror_entry_rel)["producer"] = PRODUCER
        return

    raw = open(mirror_entry_abs, "r", encoding="utf-8", errors="replace").read()
    body, binfo = strip_body(raw)

    if body is None:
        # No \begin{document}. If we already stripped this entry in place on a prior run,
        # that is the idempotent re-run case — leave the existing body alone.
        same_file = os.path.abspath(mirror_entry_abs) == os.path.abspath(chapter_main_abs)
        if entry.get("transformed") and same_file and os.path.isfile(chapter_main_abs):
            sys.stderr.write("[transform] %-8s already stripped in place (idempotent no-op)\n" % slug)
            return
        planlib.add_flag(plan, tier=2, stage=STAGE, kind="no-begin-document", severity="blocker",
                         slug=slug, message=("Mirrored entry %s has no \\begin{document}; cannot "
                                             "locate the chapter body to extract." % mirror_entry_rel),
                         location=mirror_entry_rel)["producer"] = PRODUCER
        return

    if binfo["no_end"]:
        planlib.add_flag(plan, tier=2, stage=STAGE, kind="no-end-document", severity="warn",
                         slug=slug, message=("Mirrored entry %s has \\begin{document} but no "
                                             "\\end{document}; kept everything after the body start."
                                             % mirror_entry_rel), location=mirror_entry_rel)["producer"] = PRODUCER
    if binfo["thebib"]:
        planlib.add_flag(plan, tier=2, stage=STAGE, kind="manual-bibliography", severity="warn",
                         slug=slug, message=("Chapter %s carries a manual \\begin{thebibliography} "
                                             "(its \\bibitem keys back this chapter's \\cite uses). It "
                                             "was LEFT in place — decide how it folds into the merged "
                                             "bibliography." % slug), location=chapter_main_rel)["producer"] = PRODUCER

    authors = entry.get("authors", []) or []
    if len(authors) > 1:
        planlib.add_flag(plan, tier=2, stage=STAGE, kind="coauthorship", severity="warn", slug=slug,
                         message=("Chapter %s has %d authors (%s); add a coauthorship \\footnote to "
                                  "its \\chapter when wiring it in." % (slug, len(authors), ", ".join(authors))),
                         location=chapter_main_rel)["producer"] = PRODUCER

    title = entry.get("title_override") or entry.get("title") or slug
    header = ("%% %s — chapter body for \"%s\", extracted by mol-latex-concat "
              "(preamble/wrappers stripped, paths rewritten).\n" % (chapter_main_rel, title))
    os.makedirs(os.path.dirname(chapter_main_abs) or ".", exist_ok=True)
    with open(chapter_main_abs, "w", encoding="utf-8") as fh:
        fh.write(header + body)

    entry["chapter_main"] = chapter_main_rel
    entry["transformed"] = True
    sys.stderr.write("[transform] %-8s body -> %s (%d line(s)%s%s)\n"
                     % (slug, chapter_main_rel, body.count("\n"),
                        ", abstract->section*" if binfo["abstracts"] else "",
                        ", %d coauthors" % len(authors) if len(authors) > 1 else ""))


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
    ap = argparse.ArgumentParser(description="strip mirrored entry files down to chapter bodies")
    ap.add_argument("--plan", required=True, help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", required=True, help="destination project directory")
    args = ap.parse_args(argv)

    if not os.path.exists(args.plan):
        sys.stderr.write("extract_body: plan.json not found at %s (run inspect first).\n" % args.plan)
        return 2
    plan = planlib.load(args.plan)

    dest_abs = os.path.abspath(args.dest)
    if not os.path.isdir(dest_abs):
        sys.stderr.write("extract_body: dest dir %s does not exist (run scaffold-dest first).\n" % dest_abs)
        return 2

    sources = plan.get("sources", [])
    if not sources:
        sys.stderr.write("extract_body: plan.json has no sources.\n")
        return 2

    saved = _reset_my_flags(plan)
    for s in sorted(sources, key=lambda e: e.get("index", 0)):
        extract_one(plan, s, dest_abs)
    _restore_resolutions(plan, saved)

    n_done = sum(1 for s in sources if s.get("transformed"))
    n_flags = sum(1 for f in plan["flags"] if f.get("producer") == PRODUCER)
    planlib.add_log(plan, "transform:body",
                    "Stripped %d/%d entry file(s) to chapter bodies (preamble/wrappers removed, "
                    "abstract->\\section*); %d flag(s) raised." % (n_done, len(sources), n_flags))

    try:
        planlib.validate(plan)
    except Exception as e:
        sys.stderr.write("extract_body: plan.json failed schema validation: %s\n" % e)
        planlib.save(args.plan, plan)
        return 1

    planlib.save(args.plan, plan)
    sys.stderr.write("[transform] extract_body wrote %s — %d/%d transformed, %d flag(s).\n"
                     % (args.plan, n_done, len(sources), n_flags))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
