#!/usr/bin/env python3
"""inspect_sources — catalogue each LaTeX source into plan.json. STUB.

Tier 1 (mechanical catalogue) + Tier 2 (flag-and-defer). Owner: pt-srs (T2).
Part of the mol-latex-concat formula (latex-utils pack).

Reads  plan.json: vars.sources, vars.tex_engine, sources[].dir/slug
       (slugs are pre-assigned by the agent in the inspect step).
Writes plan.json: per source -> entry_file, title, document_class/class_options,
       tex_engine hint, bib_backend, bib_files[], packages[], macros[], labels[],
       includes[], figures[], local_sty[], authors[]. Tier-2 flags for exotic
       class (beamer/standalone/poster/letter -> refuse), 0-or->1 \\begin{document},
       and any construct it cannot parse soundly (TeX is Turing-complete: flag,
       never guess).

CLI:
  inspect_sources.py --plan PLAN [--engine pdflatex]
"""
import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description="catalogue LaTeX sources into plan.json (STUB)")
    ap.add_argument("--plan", help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--engine", help="run-wide tex engine (pdflatex|xelatex|lualatex)")
    ap.parse_known_args(argv)
    sys.stderr.write(
        "STUB: inspect_sources.py is not implemented yet (pt-srs / T2). "
        "The mol-latex-concat DAG is wired to this stub; the cataloguer lands in T2.\n"
    )
    return 70  # EX_SOFTWARE: not implemented


if __name__ == "__main__":
    raise SystemExit(main())
