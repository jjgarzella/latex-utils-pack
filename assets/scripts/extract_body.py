#!/usr/bin/env python3
"""extract_body — strip each entry file down to a chapter body. STUB.

Tier 1 (wrapper stripping) + Tier 2 (flag-and-defer). Owner: pt-axx (T5).
Part of the mol-latex-concat formula (latex-utils pack).

Reads  plan.json: sources[].entry_file/slug, chapter_main target.
Writes contents/<slug>/main.tex + plan.json: KEEP the document body, REMOVE
       \\documentclass, the whole preamble, \\begin{document}/\\end{document},
       \\maketitle/\\title/\\date, inner \\tableofcontents, and the trailing
       \\bibliography{...} (keys flow to the merged bib). Convert
       \\begin{abstract}...\\end{abstract} -> \\section*{Abstract} at the chapter top.
       No sectioning demotion (papers top out at \\section under a \\chapter).
       Tier-2 flag preamble/body boundaries it cannot determine soundly.

CLI:
  extract_body.py --plan PLAN --dest DIR
"""
import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description="strip entry files to chapter bodies (STUB)")
    ap.add_argument("--plan", help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", help="destination project directory")
    ap.parse_known_args(argv)
    sys.stderr.write(
        "STUB: extract_body.py is not implemented yet (pt-axx / T5). "
        "The mol-latex-concat DAG is wired to this stub; body extraction lands in T5.\n"
    )
    return 70  # EX_SOFTWARE: not implemented


if __name__ == "__main__":
    raise SystemExit(main())
