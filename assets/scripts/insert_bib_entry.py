#!/usr/bin/env python3
"""insert_bib_entry — insert ONE approved BibTeX entry into the correct target .bib.

Part of the add-bib-references formula (latex-utils pack). This is the mechanical write
the confirm-and-write step performs AFTER a human approves an entry — the agent never
hand-edits a .bib. It reuses merge_bib's BibTeX parser to read the target and to extract
the entry's key.

Routing:
  * Two-bib mode (single_bib=false): the entry is appended to config.published_bib for a
    `published`/`thesis` classification, or config.preprints_bib for `preprint`.
  * One-bib mode (single_bib=true): the two `% ===== Published =====` /
    `% ===== Preprints =====` section headers are ensured in the single .bib and the entry
    is inserted under the section its classification selects.

Idempotent: if the entry's citation key is already defined in the target .bib, nothing is
written (a re-run is a no-op). The matching plan.json worklist entry is flipped to
`written` with its citekey / target_bib / bibtex recorded.

CLI:
  insert_bib_entry.py --plan PLAN (--bibtex TEXT | --bibtex-file FILE)
      [--key WORKLIST_KEY]                        # defaults to the entry's own key
      [--classification published|preprint|thesis] # routing (default: the worklist entry's)
      [--published-bib FILE] [--preprints-bib FILE] # override config
      [--single-bib | --two-bib]                  # override config.single_bib
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planlib  # noqa: E402
import merge_bib  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 1  # usage / environment (missing/invalid input) — not a write failure

PUBLISHED_HEADER = "% ===== Published ====="
PREPRINTS_HEADER = "% ===== Preprints ====="
# A section header: a comment line of '=' rules around 'Published' or 'Preprint(s)'.
SECTION_HEADER_RE = re.compile(r"^[ \t]*%+[ \t]*=+[ \t]*(published|preprints?)[ \t]*=+[ \t]*$",
                               re.IGNORECASE | re.MULTILINE)


# --------------------------------------------------------------------------- #
# Classification -> section / target routing.                                  #
# --------------------------------------------------------------------------- #

def section_for(classification: str) -> str:
    """Map a classification to its single-bib section name (thesis files with published)."""
    return "Preprints" if classification == "preprint" else "Published"


def _canon_section(matched: str) -> str:
    """Normalise a matched header word ('preprint'/'preprints'/'published') to a section."""
    return "Preprints" if matched.lower().startswith("preprint") else "Published"


# --------------------------------------------------------------------------- #
# Single-bib section handling.                                                  #
# --------------------------------------------------------------------------- #

def _sections_present(text: str):
    """Return the set of canonical section names whose header is present in ``text``."""
    return {_canon_section(m.group(1)) for m in SECTION_HEADER_RE.finditer(text)}


def ensure_sections(text: str, needed: str) -> str:
    """Ensure the single-bib ``text`` carries the header for section ``needed`` (and keeps
    Published-before-Preprints order). Existing content with no headers is placed under
    Published. Returns the (possibly restructured) text — no entry is inserted here."""
    present = _sections_present(text)
    if needed in present:
        return text
    body = text.strip("\n")
    if not present:
        # No headers yet: existing entries (if any) become the Published section.
        parts = [PUBLISHED_HEADER]
        if body:
            parts += ["", body]
        parts += ["", PREPRINTS_HEADER, ""]
        return "\n".join(parts) + "\n"
    if needed == "Preprints":
        # Published exists; append the Preprints header at EOF.
        return (text.rstrip("\n") + "\n\n" + PREPRINTS_HEADER + "\n")
    # needed == "Published", only Preprints exists: put Published header at the top.
    return PUBLISHED_HEADER + "\n\n" + text.lstrip("\n")


def _section_span(text: str, section: str):
    """Return ``(header_end_index, section_end_index)`` for ``section`` in ``text``:
    header_end is just past the header line; section_end is the start of the next section
    header (or EOF). Assumes the header exists (ensure_sections ran first)."""
    hdr = None
    for m in SECTION_HEADER_RE.finditer(text):
        if _canon_section(m.group(1)) == section:
            hdr = m
            break
    if hdr is None:  # defensive: ensure_sections guarantees presence
        return len(text), len(text)
    eol = text.find("\n", hdr.end())
    header_end = len(text) if eol == -1 else eol + 1
    nxt = SECTION_HEADER_RE.search(text, header_end)
    section_end = nxt.start() if nxt else len(text)
    return header_end, section_end


def insert_into_section(text: str, section: str, entry: str) -> str:
    """Insert ``entry`` at the end of ``section``'s body in single-bib ``text`` (headers
    already ensured), keeping one blank line of separation on each side."""
    _, section_end = _section_span(text, section)
    head = text[:section_end].rstrip("\n")
    tail = text[section_end:]
    block = head + "\n\n" + entry.strip() + "\n"
    if tail.strip():
        block += "\n" + tail.lstrip("\n")
    return block


def append_entry(text: str, entry: str) -> str:
    """Append ``entry`` to the end of a two-bib target file, with blank-line separation."""
    body = text.rstrip("\n")
    if body:
        return body + "\n\n" + entry.strip() + "\n"
    return entry.strip() + "\n"


# --------------------------------------------------------------------------- #
# Entry parsing + plan bookkeeping.                                            #
# --------------------------------------------------------------------------- #

def parse_single_entry(bibtex: str):
    """Return ``(citekey, normalised_entry_text)`` for a one-entry BibTeX blob. Raises
    ValueError if it does not contain exactly one @entry (strings/comments are ignored)."""
    items, errors = merge_bib.parse_bib(bibtex)
    entries = [it for it in items if it.get("kind") == "entry"]
    if len(entries) != 1:
        raise ValueError("expected exactly one BibTeX @entry, found %d%s"
                         % (len(entries), (" (%d parse error(s))" % len(errors)) if errors else ""))
    ent = entries[0]
    if not ent.get("key"):
        raise ValueError("the BibTeX entry has no citation key")
    return ent["key"], ent["raw"].strip()


def defined_keys(text: str):
    """Set of citation keys already defined in a .bib ``text``."""
    items, _ = merge_bib.parse_bib(text)
    return {it["key"] for it in items if it.get("kind") == "entry" and it.get("key")}


def _worklist_entry(plan: dict, key: str):
    """Return the worklist entry for ``key`` (or None)."""
    for e in plan.get("worklist", []):
        if e.get("key") == key:
            return e
    return None


def _read_text(path: str) -> str:
    """Read a file, returning '' if it does not exist yet (a fresh target .bib)."""
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _write_text(path: str, text: str) -> None:
    """Atomically write ``text`` to ``path`` (temp file + rename)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)) or ".",
                               prefix=".bib.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _resolve_target(path: str, base_dir: str) -> str:
    """Resolve a declared target .bib path to an absolute path (it need not exist yet)."""
    if os.path.isabs(path):
        return path
    if base_dir:
        return os.path.abspath(os.path.join(base_dir, path))
    return os.path.abspath(path)


# --------------------------------------------------------------------------- #
# Driver.                                                                       #
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    """CLI entry point: insert one approved entry into the routed .bib, idempotently."""
    ap = argparse.ArgumentParser(description="insert one approved BibTeX entry into the correct .bib")
    ap.add_argument("--plan", required=True, help="path to plan.json (the agent<->script contract)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--bibtex", help="the BibTeX entry text")
    g.add_argument("--bibtex-file", help="a file containing the BibTeX entry")
    ap.add_argument("--key", default="", help="worklist key this entry satisfies (default: the entry's key)")
    ap.add_argument("--classification", choices=["published", "preprint", "thesis"], default="",
                    help="routing (default: the worklist entry's classification)")
    ap.add_argument("--published-bib", default="", help="override config.published_bib")
    ap.add_argument("--preprints-bib", default="", help="override config.preprints_bib")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--single-bib", dest="single_bib", action="store_true", default=None,
                      help="force one-bib mode (comment-separated sections)")
    mode.add_argument("--two-bib", dest="single_bib", action="store_false",
                      help="force two-bib mode (route by classification)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.plan):
        plan = planlib.new_bib_plan(published_bib=args.published_bib,
                                    preprints_bib=args.preprints_bib,
                                    single_bib=bool(args.single_bib))
    else:
        plan = planlib.load(args.plan)
        plan.setdefault("config", {})
        plan.setdefault("worklist", [])
    cfg = plan["config"]

    bibtex = args.bibtex if args.bibtex is not None else _read_text(args.bibtex_file)
    if not (bibtex or "").strip():
        sys.stderr.write("insert_bib_entry: empty BibTeX entry\n")
        return EXIT_USAGE
    try:
        citekey, entry_text = parse_single_entry(bibtex)
    except ValueError as e:
        sys.stderr.write("insert_bib_entry: %s\n" % e)
        return EXIT_USAGE

    work_key = args.key or citekey
    wl = _worklist_entry(plan, work_key)

    classification = args.classification or (wl.get("classification") if wl else "") or ""
    if not classification:
        classification = "published"
        planlib.add_flag(plan, tier=2, stage="confirm-and-write", kind="classification-defaulted",
                         severity="warn", slug=None,
                         message=("No classification supplied for %r; defaulted to 'published'. "
                                  "Confirm this is not a preprint." % citekey))

    single_bib = args.single_bib if args.single_bib is not None else bool(cfg.get("single_bib"))
    published_bib = args.published_bib or cfg.get("published_bib") or ""
    preprints_bib = args.preprints_bib or cfg.get("preprints_bib") or ""
    if single_bib and not preprints_bib:
        preprints_bib = published_bib

    if single_bib:
        target = published_bib
    else:
        target = published_bib if classification in ("published", "thesis") else preprints_bib
    if not target:
        sys.stderr.write("insert_bib_entry: no target .bib resolved (set config.%s or pass --%s)\n"
                         % (("published_bib" if classification in ("published", "thesis") else "preprints_bib"),
                            ("published-bib" if classification in ("published", "thesis") else "preprints-bib")))
        return EXIT_USAGE

    base_dir = os.path.dirname(os.path.abspath(cfg.get("master_source"))) if cfg.get("master_source") else ""
    target_abs = _resolve_target(target, base_dir)
    section = section_for(classification)

    existing = _read_text(target_abs)
    already = citekey in defined_keys(existing)
    if already:
        wrote = False
    else:
        if single_bib:
            structured = ensure_sections(existing, section)
            new_text = insert_into_section(structured, section, entry_text)
        else:
            new_text = append_entry(existing, entry_text)
        _write_text(target_abs, new_text)
        wrote = True

    # Reflect the write in the plan worklist (append if the key was not tracked).
    if wl is None:
        wl = {"key": work_key, "status": "pending", "classification": None, "citekey": None,
              "target_bib": None, "bibtex": None, "verify_url": None, "note": None}
        plan.setdefault("worklist", []).append(wl)
    wl["status"] = "written"
    wl["classification"] = classification
    wl["citekey"] = citekey
    wl["target_bib"] = target_abs
    wl["bibtex"] = entry_text

    planlib.add_log(plan, "confirm-and-write",
                    "%s %r (%s) %s %s [%s]"
                    % ("inserted" if wrote else "already-present",
                       citekey, classification,
                       "into" if wrote else "in", os.path.basename(target_abs),
                       "single-bib/%s" % section if single_bib else "two-bib"))
    planlib.save(args.plan, plan)

    sys.stderr.write("insert_bib_entry: %s %s -> %s%s\n"
                     % ("wrote" if wrote else "skipped (already present)", citekey,
                        os.path.basename(target_abs),
                        (" [%s]" % section) if single_bib else ""))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
