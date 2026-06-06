#!/usr/bin/env python3
r"""hoist_preamble — union every source preamble into ONE global preamble.

Tier 1 (package union/dedup-by-name) + Tier 2 (flag-and-defer). Owner: pt-nww (T3).
Part of the mol-latex-concat formula (latex-utils pack). Runs AFTER the sources are
catalogued (inspect_sources.py) and mirrored into the dest (transform step); BEFORE
macro/label collision resolution.

Reads  plan.json: sources[].packages (name+options, in chapter order) and
       sources[].slug; dest.dir / dest.master_tex / dest.mode.
Writes plan.json: preamble.packages_hoisted[] (name, options, from_slugs),
       preamble.packages_dropped[] (name, reason, from_slugs), and a Tier-2
       `option-conflict` flag for every package requested with CONFLICTING options.
Writes on disk: a single deduped \usepackage block into the dest master .tex,
       between idempotent sentinel markers (regenerated verbatim on a re-run).

What it decides (soundly, over the well-behaved subset):
  * UNION + DEDUP BY NAME. One \usepackage per package name across all chapters,
    in first-seen order (chapter order, then each chapter's own order). A package's
    intra-source order — which its author validated — is preserved.
  * DROP class-owned / layout packages. A curated denylist of packages whose whole
    purpose is global page GEOMETRY / MARGINS, text+math FONTS / encoding, line or
    paragraph SPACING, and page-style / sectioning / TOC styling — exactly what a
    destination document class owns. Merging a chapter's `geometry`/`fullpage` or
    font choice into a thesis would fight the thesis class, so these are dropped
    (recorded in packages_dropped, surfaced in REPORT.md). The denylist deliberately
    EXCLUDES symbol/content packages (amssymb, mathtools, graphicx, xcolor, hyperref,
    …): those carry macros chapters need and are always hoisted.
  * DROP already-provided packages. In `existing` dest mode, a package the dest
    master preamble already \usepackage's is dropped (the dest's load wins).

What it FLAGS instead of guessing (Tier 2 — never silently pick):
  * OPTION CONFLICT. The same package requested with different options across
    chapters (or differing from the dest's options). It keeps the EARLIEST chapter's
    options in the emitted block and raises an `option-conflict` flag listing every
    option-set and which slugs asked for it; the agent (Tier 3) decides the merged
    options and edits the block.

What it does NOT do: macro/label/bib collisions (later steps); load-order surgery
(e.g. hyperref-last) — first-seen order is emitted and the compile gate + agent
catch the rare ordering conflict, which is exactly the Tier-2 → Tier-3 backstop.

CLI:
  hoist_preamble.py --plan PLAN [--dest DIR]
Exit: 0 success (flags are normal); 1 schema-invalid (plan still saved); 2 fatal
config (no plan / no sources / dest master not found).
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import planlib

# Reuse the catalogue's TeX-aware parsers so the dest preamble is read EXACTLY the
# way the sources were (same comment stripping, same \usepackage/brace parsing).
# inspect_sources.py is co-located in this pack and imports only stdlib + planlib.
from inspect_sources import (  # noqa: E402
    BEGIN_DOC_RE,
    DOCCLASS_RE,
    ParseError,
    USEPACKAGE_RE,
    _bracket_arg,
    _brace_arg,
    _skip_ws,
    parse_packages,
    strip_comments,
)

# Sentinel markers delimiting the auto-generated block in the dest master .tex.
# A re-run replaces everything between them verbatim, so the step is idempotent.
BLOCK_BEGIN = "% >>> mol-latex-concat: hoisted packages (auto-generated — regenerated on re-run) >>>"
BLOCK_END = "% <<< mol-latex-concat: hoisted packages <<<"


# --------------------------------------------------------------------------- #
# Class-owned / layout denylist (the "drop geometry, fonts, margins" mandate). #
# Curated, conservative: ONLY packages whose entire job is global page/font/   #
# margin/spacing/heading style — never symbol or content packages.             #
# --------------------------------------------------------------------------- #

_DROP = {}  # package name -> human reason


def _deny(reason, *names):
    for n in names:
        _DROP[n] = reason


_deny("page geometry / margins owned by the destination class",
      "geometry", "fullpage", "a4wide", "a4", "vmargin", "anysize", "savetrees",
      "layaureo", "dinat", "wide", "changepage", "marginnote")
_deny("text/math font + encoding owned by the destination class",
      "fontspec", "inputenc", "fontenc", "lmodern", "ae", "aecompl", "times",
      "mathptmx", "mathpazo", "palatino", "helvet", "courier", "charter",
      "libertine", "libertinus", "newtxtext", "newtxmath", "newpxtext",
      "newpxmath", "txfonts", "pxfonts", "kpfonts", "fourier", "utopia",
      "pslatex", "cmbright", "tgtermes", "tgpagella", "tgheros", "tgschola")
_deny("line / paragraph spacing owned by the destination class",
      "setspace", "doublespace", "parskip", "onehalfspace")
_deny("page style / headers / footers owned by the destination class",
      "fancyhdr", "fancyheadings", "titleps", "scrlayer-scrpage")
_deny("sectioning / table-of-contents styling owned by the destination class",
      "titlesec", "titletoc", "sectsty", "tocloft", "tocbibind", "titling",
      "fncychap", "minitoc", "quotchap")


# --------------------------------------------------------------------------- #
# Option-set comparison.                                                        #
# --------------------------------------------------------------------------- #

def _norm_opt(opt: str) -> str:
    """Normalise one package option for comparison: trim, and collapse spaces
    around a `key = value` so `margin = 1in` == `margin=1in`."""
    opt = opt.strip()
    if "=" in opt:
        k, _, v = opt.partition("=")
        return "%s=%s" % (k.strip(), v.strip())
    return opt


def _opt_key(options):
    """Order-insensitive signature of an option list (so `[a,b]` == `[b,a]` but
    `[margin=1in]` != `[margin=2in]`)."""
    return frozenset(_norm_opt(o) for o in options if o.strip())


def _fmt_opts(options):
    return "[%s]" % ",".join(o.strip() for o in options if o.strip()) if options else "(no options)"


# --------------------------------------------------------------------------- #
# Dest master .tex reading / block placement.                                   #
# --------------------------------------------------------------------------- #

def _mask_comments(text: str) -> str:
    """Like strip_comments but PRESERVES length (commented chars -> spaces) so regex
    match positions map 1:1 back onto the raw text for in-place insertion."""
    out = []
    for line in text.splitlines(keepends=True):
        masked = list(line)
        k = 0
        while k < len(line):
            c = line[k]
            if c == "\\":
                k += 2
                continue
            if c == "%":
                j = k
                while j < len(line) and line[j] not in "\r\n":
                    masked[j] = " "
                    j += 1
                break
            k += 1
        out.append("".join(masked))
    return "".join(out)


def _remove_block(text: str) -> str:
    """Drop a previously-emitted sentinel block so a re-run neither re-parses its own
    \\usepackage lines as 'already in the dest' nor stacks duplicate blocks."""
    if BLOCK_BEGIN in text and BLOCK_END in text:
        b = text.index(BLOCK_BEGIN)
        e = text.index(BLOCK_END) + len(BLOCK_END)
        if b < e:
            return text[:b] + text[e:]
    return text


def _stmt_end(masked: str, i: int) -> int:
    """Given an index just past a \\usepackage/\\documentclass head, skip its optional
    [..] and mandatory {..} and return the index just past the end of that line."""
    i = _skip_ws(masked, i)
    try:
        _, i = _bracket_arg(masked, i)
        i = _skip_ws(masked, i)
        _, i = _brace_arg(masked, i)
    except ParseError:
        pass
    nl = masked.find("\n", i)
    return (nl + 1) if nl != -1 else len(masked)


def _dest_packages(text: str):
    """Packages the dest master already loads (its own \\usepackage's, excluding any
    prior hoist block). Returns an ordered {name: options} (first occurrence wins)."""
    clean = strip_comments(_remove_block(text))
    bd = BEGIN_DOC_RE.search(clean)
    preamble = clean[: bd.start()] if bd else clean
    found = {}
    for p in parse_packages(preamble):
        found.setdefault(p["name"], p.get("options", []))
    return found


def _find_insertion(text: str):
    """Pick where to drop a fresh block in `text`. Prefer right after the last
    preamble \\usepackage, else after \\documentclass, else just before
    \\begin{document}, else end-of-file. Returns (char_index, anchor_tag)."""
    masked = _mask_comments(text)
    bd = BEGIN_DOC_RE.search(masked)
    preamble_end = bd.start() if bd else len(masked)

    last_use = None
    pos = 0
    while True:
        m = USEPACKAGE_RE.search(masked, pos)
        if not m or m.start() >= preamble_end:
            break
        last_use = _stmt_end(masked, m.end())
        pos = max(m.end(), last_use)
    if last_use is not None:
        return last_use, "after-usepackage"

    dm = DOCCLASS_RE.search(masked)
    if dm and dm.start() < preamble_end:
        return _stmt_end(masked, dm.end()), "after-documentclass"

    if bd:
        return text.rfind("\n", 0, bd.start()) + 1, "before-begin-document"

    return len(text), "no-anchor"


def _render_block(hoisted, n_dropped, n_sources) -> str:
    lines = [BLOCK_BEGIN]
    lines.append(
        "%% %d package(s) hoisted from %d source(s); %d dropped (class-owned/duplicate). See REPORT.md."
        % (len(hoisted), n_sources, n_dropped)
    )
    for h in hoisted:
        opts = h.get("options") or []
        head = "\\usepackage%s{%s}" % (
            ("[%s]" % ",".join(o.strip() for o in opts if o.strip())) if any(o.strip() for o in opts) else "",
            h["name"],
        )
        prov = ", ".join(h.get("from_slugs") or [])
        lines.append("%s%s" % (head, ("  %% <- %s" % prov) if prov else ""))
    lines.append(BLOCK_END)
    return "\n".join(lines)


def _write_block(master_path: str, block: str) -> bool:
    """Insert (or idempotently replace) the sentinel block in the master .tex.
    Returns True if the file changed. Caller has verified the file exists."""
    with open(master_path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    if BLOCK_BEGIN in text and BLOCK_END in text and text.index(BLOCK_BEGIN) < text.index(BLOCK_END):
        b = text.index(BLOCK_BEGIN)
        e = text.index(BLOCK_END) + len(BLOCK_END)
        new = text[:b] + block + text[e:]
    else:
        at, _tag = _find_insertion(text)
        prefix, suffix = text[:at], text[at:]
        chunk = block + "\n"
        if prefix and not prefix.endswith("\n"):
            chunk = "\n" + chunk
        new = prefix + chunk + suffix

    if new == text:
        return False
    with open(master_path, "w", encoding="utf-8") as fh:
        fh.write(new)
    return True


# --------------------------------------------------------------------------- #
# Driver.                                                                       #
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="hoist source preambles into one global preamble (package union/dedup/flags)"
    )
    ap.add_argument("--plan", required=True, help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", default="", help="destination project dir (overrides plan.dest.dir)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.plan):
        sys.stderr.write("hoist_preamble: plan.json not found at %s.\n" % args.plan)
        return 2
    plan = planlib.load(args.plan)

    sources = sorted(plan.get("sources", []), key=lambda e: e.get("index", 0))
    if not sources:
        sys.stderr.write("hoist_preamble: plan.json has no sources to hoist.\n")
        return 2

    dest_info = plan.get("dest", {}) or {}
    dest_dir = args.dest or dest_info.get("dir") or ""
    master_tex = dest_info.get("master_tex") or "main.tex"
    master_path = os.path.join(dest_dir, master_tex) if dest_dir else master_tex

    # Packages the destination master already provides (existing-mode dedup). For a
    # blank dest the placeholder preamble has none; this is then simply empty.
    dest_pkgs = {}
    master_exists = os.path.isfile(master_path)
    if master_exists:
        with open(master_path, "r", encoding="utf-8", errors="replace") as fh:
            dest_pkgs = _dest_packages(fh.read())

    # ---- Union by name, in first-seen (chapter) order. --------------------- #
    order = []                # package names, first-seen order
    occ = {}                  # name -> [{"options": [...], "slug": ...}, ...]
    for s in sources:
        slug = s.get("slug") or s.get("dir") or ("src%d" % s.get("index", 0))
        for p in s.get("packages", []) or []:
            name = p.get("name")
            if not name:
                continue
            if name not in occ:
                occ[name] = []
                order.append(name)
            occ[name].append({"options": p.get("options", []) or [], "slug": slug})

    # Idempotent re-run: drop this stage's prior flags but remember any resolution
    # the agent already wrote so a re-derived identical flag keeps it (mirrors
    # inspect_sources.py).
    saved_resolutions = {
        (f.get("kind"), f.get("slug"), f.get("message")): f.get("resolution")
        for f in plan.get("flags", [])
        if f.get("stage") == "hoist" and f.get("resolution")
    }
    plan["flags"] = [f for f in plan.get("flags", []) if f.get("stage") != "hoist"]

    hoisted, dropped = [], []

    def _slugs(name):
        # de-dup slugs, preserve order
        return list(dict.fromkeys(o["slug"] for o in occ[name]))

    for name in order:
        slugs = _slugs(name)
        keys = {_opt_key(o["options"]) for o in occ[name]}

        if name in dest_pkgs:
            dropped.append({
                "name": name,
                "reason": "already loaded by the destination master preamble",
                "from_slugs": slugs,
            })
            dest_key = _opt_key(dest_pkgs[name])
            if any(k != dest_key for k in keys):
                planlib.add_flag(
                    plan, tier=2, stage="hoist", kind="option-conflict", slug=None,
                    severity="warn",
                    message=(
                        "Package '%s' is loaded by the destination as %s, but source(s) %s "
                        "request different options (%s). Kept the destination's options and "
                        "dropped the rest — confirm that is correct, or edit the global preamble."
                        % (name, _fmt_opts(dest_pkgs[name]), ", ".join(slugs),
                           "; ".join(sorted(_fmt_opts(o["options"]) for o in occ[name])))
                    ),
                    location=master_path,
                )
            continue

        if name in _DROP:
            dropped.append({"name": name, "reason": _DROP[name], "from_slugs": slugs})
            continue

        # Hoisted. Keep the earliest chapter's options; flag any cross-chapter conflict.
        hoisted.append({
            "name": name,
            "options": list(occ[name][0]["options"]),
            "from_slugs": slugs,
        })
        if len(keys) > 1:
            # Group occurrences by normalised option-set, preserving first appearance.
            groups, seen = [], {}
            for o in occ[name]:
                k = _opt_key(o["options"])
                if k not in seen:
                    seen[k] = {"opts": o["options"], "slugs": []}
                    groups.append(seen[k])
                if o["slug"] not in seen[k]["slugs"]:
                    seen[k]["slugs"].append(o["slug"])
            desc = "; ".join(
                "%s <- %s" % (_fmt_opts(g["opts"]), ", ".join(g["slugs"])) for g in groups
            )
            planlib.add_flag(
                plan, tier=2, stage="hoist", kind="option-conflict", slug=None,
                severity="warn",
                message=(
                    "Package '%s' is requested with conflicting options across chapters: %s. "
                    "Kept %s (earliest chapter). Decide the merged options and edit the global preamble."
                    % (name, desc, _fmt_opts(occ[name][0]["options"]))
                ),
                location=master_path,
            )

    # Re-apply any resolution the agent had written for an identical earlier flag.
    for f in plan.get("flags", []):
        if f.get("stage") == "hoist":
            r = saved_resolutions.get((f.get("kind"), f.get("slug"), f.get("message")))
            if r:
                f["resolution"] = r

    plan.setdefault("preamble", {})
    plan["preamble"]["packages_hoisted"] = hoisted
    plan["preamble"]["packages_dropped"] = dropped

    # ---- Emit the block on disk. ------------------------------------------- #
    wrote = False
    fatal_config = (not dest_dir) or (not master_exists)
    block = _render_block(hoisted, len(dropped), len(sources))
    if not dest_dir:
        planlib.add_flag(plan, tier=2, stage="hoist", kind="no-dest",
                         message="No dest dir given (plan.dest.dir empty and --dest unset); "
                                 "computed the global preamble but wrote it nowhere.",
                         slug=None, severity="blocker")
    elif not master_exists:
        planlib.add_flag(plan, tier=2, stage="hoist", kind="master-not-found",
                         message="Destination master '%s' not found; the global preamble was "
                                 "computed into plan.json but not emitted. Scaffold the master first."
                                 % master_path,
                         slug=None, severity="blocker", location=master_path)
    else:
        wrote = _write_block(master_path, block)
        # A master with no anchor at all is suspicious enough to surface.
        with open(master_path, "r", encoding="utf-8", errors="replace") as fh:
            if not BEGIN_DOC_RE.search(_mask_comments(fh.read())):
                planlib.add_flag(plan, tier=2, stage="hoist", kind="no-begin-document",
                                 message="Destination master '%s' has no \\begin{document}; "
                                         "hoisted block placement may be wrong — verify." % master_path,
                                 slug=None, severity="warn", location=master_path)

    n_conflict = sum(1 for f in plan["flags"]
                     if f.get("stage") == "hoist" and f.get("kind") == "option-conflict")
    planlib.add_log(
        plan, "hoist",
        "Hoisted %d package(s), dropped %d (class-owned/duplicate); %d option-conflict flag(s).%s"
        % (len(hoisted), len(dropped), n_conflict,
           " Wrote global preamble block to %s." % master_path if wrote else
           (" Master unchanged (%s)." % master_path if master_exists else ""))
    )

    try:
        planlib.validate(plan)
    except Exception as e:  # jsonschema.ValidationError or absent
        sys.stderr.write("hoist_preamble: plan.json failed schema validation: %s\n" % e)
        planlib.save(args.plan, plan)
        return 1

    planlib.save(args.plan, plan)
    n_block = sum(1 for f in plan["flags"]
                  if f.get("stage") == "hoist" and f.get("severity") == "blocker")
    sys.stderr.write(
        "[hoist] %s — hoisted %d, dropped %d, %d option-conflict flag(s)%s.\n"
        % (master_path, len(hoisted), len(dropped), n_conflict,
           " (%d BLOCKER — resolve before proceeding)" % n_block if n_block else "")
    )
    # The on-disk emission is half this step's contract; a missing dest/master is a
    # fatal config error (the scaffold step should have created the master). The plan
    # is saved above (computed hoist set + blocker flag preserved) before we bail.
    return 2 if fatal_config else 0


if __name__ == "__main__":
    raise SystemExit(main())
