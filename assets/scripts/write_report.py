#!/usr/bin/env python3
"""write_report — render REPORT.md from the accumulated plan.json.

Tier 1 (report rendering). Owner: pt-jgg (T8).
Part of the mol-latex-concat formula (latex-utils pack).

Reads  plan.json (the agent<->script contract): vars, dest, the per-source
       inventory, the preamble hoist/drop, the macro/label/bib renames, the
       bib merge, every Tier-2 flag (+ resolution) and the step log.
Writes <dest>/REPORT.md — the human's review surface. For every chapter it
       records the source -> contents/<slug>/ mapping and inferred slug/title;
       every renamed macro (old/new/reason); every slug-prefixed label
       namespace; every dropped or option-conflicting package; every
       collapsed/renamed bib key; coauthorship footnotes; and EVERY Tier-2
       flag with its resolution (UNRESOLVED ones called out up top).

This helper is READ-ONLY: it never mutates plan.json. The report is rendered
purely from the contract the earlier steps accumulated, so re-running it is
free and side-effect-free. Be specific — a human merges and ships on the
strength of this document.

CLI:
  write_report.py --plan PLAN [--dest DIR] [-o OUT | -o -]

  --plan   path to plan.json (required).
  --dest   destination project dir; REPORT.md is written to <dest>/REPORT.md.
           Defaults to plan.dest.dir, then plan.vars.dest, then ".".
  -o/--out explicit output path; "-" prints to stdout (handy for testing).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planlib  # noqa: E402  (shared load/save plumbing lives beside this file)


# --------------------------------------------------------------------------- #
# Small markdown helpers.                                                       #
# --------------------------------------------------------------------------- #

def code(s) -> str:
    """Inline code span. Empty/None -> an em dash so a cell is never blank."""
    s = "" if s is None else str(s)
    return "`%s`" % s if s else "—"


def cell(s) -> str:
    """A table-cell-safe rendering: code-span, with pipes/newlines escaped so a
    LaTeX macro or path never breaks the GitHub-flavoured-markdown table grid."""
    if s is None or s == "":
        return "—"
    s = str(s).replace("\n", " ").replace("|", "\\|")
    return "`%s`" % s


def txt(s) -> str:
    """Plain table cell (no code span) with pipes/newlines neutralised."""
    if s is None or s == "":
        return "—"
    return str(s).replace("\n", " ").replace("|", "\\|")


def yesno(v) -> str:
    """Render a tri-state boolean: true -> ✓, false -> ✗, null/absent -> ?."""
    if v is True:
        return "✓"
    if v is False:
        return "✗"
    return "?"


SEV_BADGE = {"blocker": "🛑 blocker", "warn": "⚠ warn", "info": "ℹ info"}


def sev(s) -> str:
    return SEV_BADGE.get(s, s or "—")


def plural(n, one, many=None) -> str:
    many = many if many is not None else one + "s"
    return "%d %s" % (n, one if n == 1 else many)


# --------------------------------------------------------------------------- #
# Section renderers. Each returns a list of markdown lines (possibly empty).    #
# --------------------------------------------------------------------------- #

def section_summary(plan) -> list:
    vars_ = plan.get("vars", {}) or {}
    dest = plan.get("dest", {}) or {}
    sources = plan.get("sources", []) or []
    renames = plan.get("renames", {}) or {}
    pre = plan.get("preamble", {}) or {}
    bib = plan.get("bib", {}) or {}
    flags = plan.get("flags", []) or []

    macros = renames.get("macros", []) or []
    labels = renames.get("labels", []) or []
    bib_keys = renames.get("bib_keys", []) or []
    hoisted = pre.get("packages_hoisted", []) or []
    dropped = pre.get("packages_dropped", []) or []
    dups = bib.get("duplicates_collapsed", []) or []

    unresolved = [f for f in flags if not f.get("resolution")]
    blockers = [f for f in unresolved if f.get("severity") == "blocker"]

    out = ["## Summary", ""]
    out.append("| Field | Value |")
    out.append("|---|---|")
    out.append("| Formula | %s (schema v%s) |"
               % (txt(plan.get("formula", "mol-latex-concat")),
                  txt(plan.get("schema_version", "?"))))
    out.append("| TeX engine | %s |" % cell(vars_.get("tex_engine")))
    out.append("| Sources (chapter order) | %s |"
               % (", ".join(cell(s) for s in vars_.get("sources", [])) or "—"))
    out.append("| Destination | %s |" % cell(dest.get("dir") or vars_.get("dest")))
    out.append("| Dest mode | %s |" % txt(dest.get("mode")))
    out.append("| Dest class | %s |" % cell(dest.get("document_class")))
    out.append("| Master .tex | %s |" % cell(dest.get("master_tex")))
    out.append("| Merged bib | %s (%s) |"
               % (cell(dest.get("master_bib") or bib.get("merged_bib")),
                  txt(dest.get("bib_backend") or bib.get("backend"))))
    out.append("")
    out.append("**Tallies** — "
               + ", ".join([
                   plural(len(sources), "chapter"),
                   plural(len(hoisted), "package") + " hoisted",
                   plural(len(dropped), "package") + " dropped",
                   plural(len(macros), "macro rename"),
                   plural(len(labels), "label") + " namespaced",
                   plural(bib.get("entries_total", 0) or 0, "bib entry", "bib entries"),
                   plural(len(dups), "duplicate") + " collapsed",
                   plural(len(bib_keys), "bib-key rename"),
                   plural(len(flags), "flag"),
               ])
               + ".")
    out.append("")
    if blockers:
        out.append("> 🛑 **%s** — the merge is NOT clean until these are resolved."
                   % (plural(len(blockers), "unresolved BLOCKER flag")))
    elif unresolved:
        out.append("> ⚠ **%s** awaiting an agent resolution (see below)."
                   % (plural(len(unresolved), "unresolved flag")))
    else:
        out.append("> ✓ Every flag raised during the merge has a recorded resolution.")
    out.append("")
    return out


def _flag_table(flags) -> list:
    out = ["| ID | Sev | Stage | Slug | Kind | Detail | Resolution |",
           "|---|---|---|---|---|---|---|"]
    for f in flags:
        msg = txt(f.get("message"))
        loc = f.get("location")
        if loc:
            msg += " <br>↳ " + cell(loc)
        out.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            txt(f.get("id")),
            sev(f.get("severity")),
            txt(f.get("stage")),
            cell(f.get("slug")) if f.get("slug") else "—",
            cell(f.get("kind")),
            msg,
            txt(f.get("resolution")) if f.get("resolution") else "**UNRESOLVED**",
        ))
    return out


def section_unresolved(plan) -> list:
    flags = plan.get("flags", []) or []
    unresolved = [f for f in flags if not f.get("resolution")]
    if not unresolved:
        return []
    # Blockers first, then warns, then infos, stable within each band.
    order = {"blocker": 0, "warn": 1, "info": 2}
    unresolved.sort(key=lambda f: order.get(f.get("severity"), 3))
    out = ["## ⚠ Unresolved flags (%d)" % len(unresolved), "",
           "These were deferred by a helper (Tier 2) and still need an agent "
           "decision (Tier 3). The compile gate is the backstop, but resolve "
           "them deliberately rather than leaning on it.", ""]
    out += _flag_table(unresolved)
    out.append("")
    return out


def section_chapters(plan) -> list:
    sources = sorted(plan.get("sources", []) or [], key=lambda e: e.get("index", 0))
    if not sources:
        return ["## Chapters", "", "_No sources in the plan._", ""]

    out = ["## Chapters", "",
           "Each source becomes a `\\chapter`, in `sources` order, mirrored into "
           "`contents/<slug>/`.", ""]
    out += ["| # | Slug | Chapter title | Source dir | Chapter body | Baseline | Merged |",
            "|---|---|---|---|---|---|---|"]
    for s in sources:
        title = s.get("title_override") or s.get("title") or s.get("slug") or ""
        out.append("| %d | %s | %s | %s | %s | %s | %s |" % (
            s.get("index", 0),
            cell(s.get("slug")),
            txt(title),
            cell(s.get("dir")),
            cell(s.get("chapter_main")),
            yesno(s.get("baseline_compiled")),
            yesno(s.get("transformed")),
        ))
    out.append("")

    # Per-chapter detail — the specifics a reviewer wants without re-opening plan.json.
    for s in sources:
        slug = s.get("slug") or "?"
        title = s.get("title_override") or s.get("title") or slug
        out.append("### Chapter %d — %s (`%s`)" % (s.get("index", 0), title, slug))
        if s.get("title_override"):
            out.append("- Title **overridden** from %s." % cell(s.get("title")))
        out.append("- Source: %s → %s" % (cell(s.get("dir")), cell(s.get("chapter_main"))))
        out.append("- Entry file: %s · class %s%s · engine hint %s" % (
            cell(s.get("entry_file")),
            cell(s.get("document_class")),
            ("[" + ",".join(s.get("class_options") or []) + "]") if s.get("class_options") else "",
            cell(s.get("tex_engine")),
        ))
        out.append("- Bib backend: %s · bib files: %s" % (
            txt(s.get("bib_backend")),
            ", ".join(cell(b) for b in (s.get("bib_files") or [])) or "—",
        ))
        counts = "; ".join([
            plural(len(s.get("packages") or []), "package"),
            plural(len(s.get("macros") or []), "macro"),
            plural(len(s.get("labels") or []), "label"),
            plural(len(s.get("includes") or []), "include"),
            plural(len(s.get("figures") or []), "figure"),
        ])
        out.append("- Inventory: %s." % counts)
        if s.get("local_sty"):
            out.append("- Local style files: %s." % ", ".join(cell(x) for x in s["local_sty"]))
        authors = s.get("authors") or []
        if len(authors) > 1:
            out.append("- **Coauthored** (%d): %s — preserved as a chapter `\\footnote` (see Coauthorship)."
                       % (len(authors), ", ".join(txt(a) for a in authors)))
        elif len(authors) == 1:
            out.append("- Author: %s." % txt(authors[0]))
        out.append("")
    return out


def section_preamble(plan) -> list:
    pre = plan.get("preamble", {}) or {}
    hoisted = pre.get("packages_hoisted", []) or []
    dropped = pre.get("packages_dropped", []) or []
    if not hoisted and not dropped:
        return []
    out = ["## Preamble (package union)", ""]
    if hoisted:
        out.append("### Packages hoisted into the global preamble (%d)" % len(hoisted))
        out.append("")
        out += ["| Package | Options | Requested by |", "|---|---|---|"]
        for h in sorted(hoisted, key=lambda x: (x.get("name") or "")):
            opts = h.get("options") or []
            out.append("| %s | %s | %s |" % (
                cell(h.get("name")),
                cell(", ".join(opts)) if opts else "—",
                ", ".join(cell(x) for x in (h.get("from_slugs") or [])) or "—",
            ))
        out.append("")
    if dropped:
        out.append("### Packages dropped (%d)" % len(dropped))
        out.append("")
        out.append("Dropped because the destination class already owns them or "
                   "they conflict with its layout — review if a chapter relied on one.")
        out.append("")
        out += ["| Package | Reason | Requested by |", "|---|---|---|"]
        for d in sorted(dropped, key=lambda x: (x.get("name") or "")):
            out.append("| %s | %s | %s |" % (
                cell(d.get("name")),
                txt(d.get("reason")),
                ", ".join(cell(x) for x in (d.get("from_slugs") or [])) or "—",
            ))
        out.append("")
    return out


def section_macros(plan) -> list:
    macros = (plan.get("renames", {}) or {}).get("macros", []) or []
    out = ["## Macro renames", ""]
    if not macros:
        out.append("_No divergent macro collisions — every shared macro either "
                   "collapsed to one definition or was unique._")
        out.append("")
        return out
    out.append("Divergent collisions: the earlier chapter keeps the bare name; "
               "the later chapter's macro gets a per-slug suffix, with its uses "
               "find-replaced in that chapter only.")
    out.append("")
    out += ["| Slug | From | To | Reason |", "|---|---|---|---|"]
    for m in macros:
        out.append("| %s | %s | %s | %s |" % (
            cell(m.get("slug")),
            cell(m.get("from")),
            cell(m.get("to")),
            txt(m.get("reason")),
        ))
    out.append("")
    return out


def section_labels(plan) -> list:
    labels = (plan.get("renames", {}) or {}).get("labels", []) or []
    out = ["## Label namespaces", ""]
    if not labels:
        out.append("_No labels were prefixed._")
        out.append("")
        return out
    out.append("Every `\\label` is proactively slug-prefixed (and its whole "
               "`\\ref`/`\\eqref`/`\\cref`… family rewritten) so two chapters "
               "can reuse a key without clashing.")
    out.append("")
    # Group by slug so the namespaces read as namespaces, not one flat wall.
    by_slug = {}
    for r in labels:
        by_slug.setdefault(r.get("slug") or "?", []).append(r)
    for slug in sorted(by_slug):
        rows = by_slug[slug]
        # Derive the common namespace prefix from a sample to_value.
        sample = rows[0].get("to") or ""
        frm = rows[0].get("from") or ""
        prefix = sample[:-len(frm)] if frm and sample.endswith(frm) else ""
        head = "### `%s` — %s" % (slug, plural(len(rows), "label"))
        if prefix:
            head += " namespaced under `%s`" % prefix
        out.append(head)
        out.append("")
        out += ["| From | To |", "|---|---|"]
        for r in sorted(rows, key=lambda x: (x.get("from") or "")):
            out.append("| %s | %s |" % (cell(r.get("from")), cell(r.get("to"))))
        out.append("")
    return out


def section_bib(plan) -> list:
    bib = plan.get("bib", {}) or {}
    bib_keys = (plan.get("renames", {}) or {}).get("bib_keys", []) or []
    dups = bib.get("duplicates_collapsed", []) or []
    if not bib and not bib_keys:
        return []
    out = ["## Bibliography", ""]
    out.append("- Merged bib: %s · backend %s · %s%s" % (
        cell(bib.get("merged_bib")),
        txt(bib.get("backend")),
        plural(bib.get("entries_total", 0) or 0, "entry", "entries"),
        (" · style %s" % cell(bib.get("style"))) if bib.get("style") else "",
    ))
    out.append("")
    if dups:
        out.append("### Exact duplicates collapsed (%d)" % len(dups))
        out.append("")
        out += ["| Key | Appeared in |", "|---|---|"]
        for d in sorted(dups, key=lambda x: (x.get("key") or "")):
            out.append("| %s | %s |" % (
                cell(d.get("key")),
                ", ".join(cell(x) for x in (d.get("from_slugs") or [])) or "—",
            ))
        out.append("")
    if bib_keys:
        out.append("### Same-key-different-work renames (%d)" % len(bib_keys))
        out.append("")
        out.append("A later paper reused a key for a *different* work; its key was "
                   "suffixed and its `\\cite` uses rewritten in that chapter.")
        out.append("")
        out += ["| Slug | From | To | Reason |", "|---|---|---|---|"]
        for r in bib_keys:
            out.append("| %s | %s | %s | %s |" % (
                cell(r.get("slug")),
                cell(r.get("from")),
                cell(r.get("to")),
                txt(r.get("reason")),
            ))
        out.append("")
    return out


def section_coauthorship(plan) -> list:
    flags = plan.get("flags", []) or []
    co = [f for f in flags if f.get("kind") == "coauthorship"]
    # Fall back to the per-source author lists if no explicit flag was raised.
    src_co = [s for s in (plan.get("sources", []) or []) if len(s.get("authors") or []) > 1]
    if not co and not src_co:
        return []
    out = ["## Coauthorship footnotes", "",
           "A multi-author source keeps its `\\author` dropped as a macro but its "
           "coauthorship preserved as a chapter `\\footnote`. Confirm each made it "
           "into the wired chapter.", ""]
    if co:
        out += ["| Slug | Note | Location | Resolution |", "|---|---|---|---|"]
        for f in co:
            out.append("| %s | %s | %s | %s |" % (
                cell(f.get("slug")),
                txt(f.get("message")),
                cell(f.get("location")),
                txt(f.get("resolution")) if f.get("resolution") else "_pending_",
            ))
        out.append("")
    else:
        out += ["| Slug | Authors |", "|---|---|"]
        for s in sorted(src_co, key=lambda e: e.get("index", 0)):
            out.append("| %s | %s |" % (
                cell(s.get("slug")),
                ", ".join(txt(a) for a in (s.get("authors") or [])),
            ))
        out.append("")
    return out


def section_all_flags(plan) -> list:
    flags = plan.get("flags", []) or []
    out = ["## All flags (%d)" % len(flags), ""]
    if not flags:
        out.append("_No Tier-2 flags were raised — every transform was mechanically clean._")
        out.append("")
        return out
    out.append("The full Tier-2 flag-and-defer ledger (raised → resolved). "
               "Anything a helper could not soundly transform lands here instead "
               "of being guessed.")
    out.append("")
    out += _flag_table(flags)
    out.append("")
    return out


def section_log(plan) -> list:
    log = plan.get("log", []) or []
    if not log:
        return []
    out = ["## Step log", "",
           "Append-only trace of what each formula step did.", "",
           "| Step | When (UTC) | Summary |", "|---|---|---|"]
    for e in log:
        out.append("| %s | %s | %s |" % (
            cell(e.get("step")),
            txt(e.get("ts")),
            txt(e.get("summary")),
        ))
    out.append("")
    return out


# --------------------------------------------------------------------------- #
# Report assembly.                                                              #
# --------------------------------------------------------------------------- #

def render(plan) -> str:
    sources = plan.get("sources", []) or []
    last_ts = ""
    log = plan.get("log", []) or []
    if log:
        last_ts = log[-1].get("ts") or ""

    lines = [
        "# Merge report — `mol-latex-concat`",
        "",
        "Generated by `write_report.py` from `plan.json` (the formula's "
        "agent⇄script contract). It records every transformation the merge "
        "applied and every construct it deferred, so the merge can be reviewed "
        "without re-reading the diff.",
        "",
    ]
    if last_ts:
        lines.append("_Plan last updated: %s._" % last_ts)
        lines.append("")
    lines.append("This run merged %s into %s."
                 % (plural(len(sources), "source"),
                    code((plan.get("dest", {}) or {}).get("dir")
                         or (plan.get("vars", {}) or {}).get("dest") or "the destination")))
    lines.append("")

    for builder in (
        section_summary,
        section_unresolved,
        section_chapters,
        section_preamble,
        section_macros,
        section_labels,
        section_bib,
        section_coauthorship,
        section_all_flags,
        section_log,
    ):
        try:
            lines += builder(plan)
        except Exception as e:  # a malformed section must not sink the whole report
            lines += ["## %s — render error" % builder.__name__,
                      "", "_%s_" % e, ""]
    # Normalise trailing blank lines to exactly one.
    text = "\n".join(lines).rstrip("\n") + "\n"
    return text


# --------------------------------------------------------------------------- #
# Driver.                                                                       #
# --------------------------------------------------------------------------- #

def resolve_out_path(args, plan) -> str:
    if args.out:
        return args.out
    dest = args.dest
    if not dest:
        dest = (plan.get("dest", {}) or {}).get("dir") or (plan.get("vars", {}) or {}).get("dest") or "."
    return os.path.join(dest, "REPORT.md")


def main(argv=None):
    ap = argparse.ArgumentParser(description="render REPORT.md from plan.json")
    ap.add_argument("--plan", required=True, help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", help="destination project directory (REPORT.md is written to <dest>/REPORT.md)")
    ap.add_argument("-o", "--out", help="explicit output path; '-' prints to stdout")
    args = ap.parse_args(argv)

    if not os.path.exists(args.plan):
        sys.stderr.write("write_report: plan.json not found at %s (run the inspect step first).\n" % args.plan)
        return 2
    try:
        plan = planlib.load(args.plan)
    except Exception as e:
        sys.stderr.write("write_report: could not read plan.json (%s): %s\n" % (args.plan, e))
        return 2

    text = render(plan)

    if args.out == "-":
        sys.stdout.write(text)
        return 0

    out_path = resolve_out_path(args, plan)
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    if not os.path.isdir(out_dir):
        sys.stderr.write("write_report: output dir %s does not exist (run scaffold-dest first).\n" % out_dir)
        return 2
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)

    n_flags = len(plan.get("flags", []) or [])
    n_unres = sum(1 for f in (plan.get("flags", []) or []) if not f.get("resolution"))
    sys.stderr.write("[report] wrote %s — %s, %d unresolved.\n"
                     % (out_path, plural(n_flags, "flag"), n_unres))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
