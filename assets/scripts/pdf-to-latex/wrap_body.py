#!/usr/bin/env python3
"""Transform converter's markdown output into body.tex with Strategy A number fidelity.

Usage (standalone): wrap_body.py --paper-dir DIR --raw-md RAW_MD
Usage (plan-driven): wrap_body.py --plan PLAN_JSON [overrides: --paper-dir, --raw-md]

When --plan is provided, paper_dir and raw_output are read from plan.json. The plan
is extended with a log entry on completion.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
import planlib  # noqa: E402

THM_KINDS = {"Theorem", "Lemma", "Proposition", "Corollary", "Definition",
             "Remark", "Example", "Conjecture"}


def escape_hash_outside_math(tex):
    """Replace `#` with `\\#` outside math regions."""
    math_span_re = re.compile(
        r"(\$\$[\s\S]*?\$\$|\$[^\$\n]+\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]"
        r"|\\begin\{equation\*?\}[\s\S]*?\\end\{equation\*?\}"
        r"|\\begin\{align\*?\}[\s\S]*?\\end\{align\*?\}"
        r"|\\begin\{gather\*?\}[\s\S]*?\\end\{gather\*?\})"
    )
    out = []
    pos = 0
    for m in math_span_re.finditer(tex):
        segment = tex[pos:m.start()]
        out.append(segment.replace("#", r"\#"))
        out.append(m.group(0))
        pos = m.end()
    out.append(tex[pos:].replace("#", r"\#"))
    return "".join(out)


def wrap_theorem_likes(tex, inventory):
    """Ensure each theorem-like entry appears as `\\textbf{Kind N.M}` in tex."""
    unwrapped = []
    for e in inventory:
        if e["kind"] not in THM_KINDS:
            continue
        kind, num = e["kind"], e["number"]
        if re.search(r"\\textbf\{" + kind + r"\s+" + re.escape(num) + r"[\.}]", tex):
            continue
        pat_outside = re.compile(r"\*\*" + kind + r"\s+" + re.escape(num) + r"\*\*\s*\.")
        tex, n = pat_outside.subn(r"\\textbf{" + kind + " " + num + ".}", tex, count=1)
        if n:
            continue
        pat_inside = re.compile(r"\*\*" + kind + r"\s+" + re.escape(num) + r"\.\*\*")
        tex, n = pat_inside.subn(r"\\textbf{" + kind + " " + num + ".}", tex, count=1)
        if n:
            continue
        pat_para = re.compile(r"\\paragraph\{" + kind + r"\s+" + re.escape(num) + r"\.\}")
        tex, n = pat_para.subn(r"\\textbf{" + kind + " " + num + ".}", tex, count=1)
        if n:
            continue
        pat_plain = re.compile(
            r"(?m)^(\s*(?:\\protect\\phantomsection(?:\\label\{[^}]*\})?\{\})?)"
            + kind + r"\s+" + re.escape(num) +
            r"((?:\s+(?:\\hyperref\[[^\]]*\])?[\(\[\{][^\n]{0,200}[\)\]\}])?)\.",
        )

        def repl(m):
            prefix = m.group(1) or ""
            annot = m.group(2) or ""
            return prefix + r"\textbf{" + kind + " " + num + annot + ".}"

        tex, n = pat_plain.subn(repl, tex, count=1)
        if n:
            continue
        if e.get("snippet"):
            snippet = e["snippet"][:30]
            if snippet in tex:
                tex = tex.replace(
                    snippet,
                    r"\textbf{" + kind + " " + num + ".} " + snippet,
                    1,
                )
                continue
        unwrapped.append(f"{kind} {num}")
    return tex, unwrapped


def overlay_section_numbers(tex, inventory):
    """Prefix section/subsection/chapter headings with their inventory number."""
    for e in inventory:
        if e["kind"] not in {"Chapter", "Section", "Subsection"}:
            continue
        num = e["number"]
        snippet = e.get("snippet", "").strip()
        if not snippet:
            continue
        key = snippet[:40]
        cmd = {"Chapter": "chapter", "Section": "section", "Subsection": "subsection"}[e["kind"]]
        pat = re.compile(r"\\" + cmd + r"\*\{([^}]*)\}")

        def repl(m):
            title = m.group(1)
            if key.lower() in title.lower() and num not in title:
                return r"\\" + cmd + "*{" + num + " " + title + "}"
            return m.group(0)

        tex = pat.sub(repl, tex, count=1)
    return tex


def wrap_bare_split(tex):
    """Wrap standalone \\begin{split}...\\end{split} in \\[...\\]."""
    def repl(m):
        return r"\[" + m.group(0) + r"\]"

    return re.sub(
        r"\\begin\{split\}[\s\S]*?\\end\{split\}",
        repl,
        tex,
    )


def overlay_figure_table_captions(tex, inventory):
    """Insert numbered figure/table placeholders for items missing from pandoc output."""
    for e in inventory:
        if e["kind"] not in {"Figure", "Table"}:
            continue
        num = e["number"]
        snippet = e.get("snippet", "").strip()[:40]
        if e["kind"] == "Figure":
            if f"Figure {num}" in tex:
                continue
            tex += f"\n\\begin{{center}}\\fbox{{[Figure {num} omitted: {snippet}]}}\\end{{center}}\n"
        else:
            if f"Table {num}" in tex:
                continue
            tex += f"\n\\begin{{center}}\\fbox{{[Table {num} omitted: {snippet}]}}\\end{{center}}\n"
    return tex


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="",
                    help="path to plan.json; overrides other args when set")
    ap.add_argument("--paper-dir", default="")
    ap.add_argument("--raw-md", default="", help="path to converter's raw .md output")
    args = ap.parse_args()

    plan_data = None
    if args.plan:
        plan_data = planlib.load(args.plan)
        if not args.paper_dir:
            args.paper_dir = plan_data["vars"]["paper_dir"]
        if not args.raw_md:
            args.raw_md = plan_data.get("raw_output", "")

    if not args.paper_dir:
        ap.error("--paper-dir is required (or provide --plan with vars.paper_dir)")
    if not args.raw_md:
        ap.error("--raw-md is required (or provide --plan with raw_output set by run-converter)")

    paper_dir = Path(args.paper_dir)
    raw_md = Path(args.raw_md)
    inv = json.loads((paper_dir / "numbering.json").read_text())

    pandoc_out = paper_dir / "_pandoc.tex"
    subprocess.check_call([
        "pandoc", str(raw_md),
        "-f", "markdown+tex_math_dollars+raw_tex",
        "-t", "latex",
        "--wrap=preserve",
        "-o", str(pandoc_out),
    ])
    tex = pandoc_out.read_text()

    tex = wrap_bare_split(tex)
    tex, unwrapped = wrap_theorem_likes(tex, inv)
    tex = overlay_section_numbers(tex, inv)
    tex = overlay_figure_table_captions(tex, inv)
    tex = escape_hash_outside_math(tex)

    (paper_dir / "body.tex").write_text(tex)
    print(f"wrote body.tex ({len(tex)} chars, {tex.count(chr(10))} lines)")
    if unwrapped:
        print(f"WARNING: {len(unwrapped)} theorem-likes could not be wrapped:")
        for u in unwrapped:
            print(f"  - {u}")

    if plan_data is not None:
        plan_data = planlib.load(args.plan)
        planlib.add_log(plan_data, "wrap-body",
                        f"Wrote body.tex ({len(tex)} chars); {len(unwrapped)} unwrapped theorem-likes")
        planlib.save(args.plan, plan_data)


if __name__ == "__main__":
    main()
