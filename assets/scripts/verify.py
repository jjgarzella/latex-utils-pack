#!/usr/bin/env python3
"""verify — the compile gate: per-source baseline and final-merge build. STUB.

Tier 1 (compile orchestration). Owner: pt-e9u (T7).
Generalizes assets/scripts/build-verify.sh (which stays until this supersedes it).
Part of the mol-latex-concat formula (latex-utils pack).

Two modes, one gate:
  --baseline : compile ONE source standalone (each source must build before we
               touch it). Reads sources[].dir/entry_file/tex_engine/bib_backend;
               sets sources[].baseline_compiled and fails early (exit 2) with the
               source's own log if it will not compile.
  --final    : compile the merged dest as the HARD GATE. Fails (exit 2) on any TeX
               error, undefined reference/citation, or multiply-defined label.

Supports --engine pdflatex|xelatex|lualatex and BOTH bib backends, auto-detected:
bibtex (\\bibliography) and biblatex/biber (\\addbibresource). Exotic .bst => flag.

CLI:
  verify.py --plan PLAN --baseline --source DIR [--engine pdflatex]
  verify.py --plan PLAN --final --dest DIR [--entry main.tex] [--engine pdflatex]
"""
import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description="LaTeX compile gate: baseline + final (STUB)")
    ap.add_argument("--plan", help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--baseline", action="store_true", help="per-source standalone baseline compile")
    ap.add_argument("--final", action="store_true", help="final merged-dest hard gate")
    ap.add_argument("--source", help="source directory (baseline mode)")
    ap.add_argument("--dest", help="destination project directory (final mode)")
    ap.add_argument("--entry", help="master .tex filename (default main.tex)")
    ap.add_argument("--engine", help="tex engine (pdflatex|xelatex|lualatex)")
    ap.parse_known_args(argv)
    sys.stderr.write(
        "STUB: verify.py is not implemented yet (pt-e9u / T7). "
        "The mol-latex-concat DAG is wired to this stub; the generalized gate lands in T7 "
        "(build-verify.sh remains the working pdflatex+bibtex gate until then).\n"
    )
    return 70  # EX_SOFTWARE: not implemented


if __name__ == "__main__":
    raise SystemExit(main())
