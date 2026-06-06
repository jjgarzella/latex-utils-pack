#!/usr/bin/env python3
"""write_report — render REPORT.md from the accumulated plan.json. STUB.

Tier 1 (report rendering). Owner: pt-jgg (T8).
Part of the mol-latex-concat formula (latex-utils pack).

Reads  plan.json: everything (sources, renames, preamble, bib, flags, log).
Writes <dest>/REPORT.md: per chapter, the source -> contents/<slug>/ mapping and
       inferred slug/title; every renamed macro (old/new/reason); every slug-prefixed
       label namespace; every dropped or option-conflicting package (FLAGGED); every
       collapsed/renamed bib key; coauthorship footnotes; and every Tier-2 flag with
       its agent resolution. This is the human's review surface — be specific.

CLI:
  write_report.py --plan PLAN --dest DIR
"""
import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description="render REPORT.md from plan.json (STUB)")
    ap.add_argument("--plan", help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", help="destination project directory")
    ap.parse_known_args(argv)
    sys.stderr.write(
        "STUB: write_report.py is not implemented yet (pt-jgg / T8). "
        "The mol-latex-concat DAG is wired to this stub; report rendering lands in T8.\n"
    )
    return 70  # EX_SOFTWARE: not implemented


if __name__ == "__main__":
    raise SystemExit(main())
