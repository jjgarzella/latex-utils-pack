#!/usr/bin/env python3
"""hoist_preamble — union source preambles into one global preamble. STUB.

Tier 1 (package union/dedup) + Tier 2 (flag-and-defer). Owner: pt-nww (T3).
Part of the mol-latex-concat formula (latex-utils pack).

Reads  plan.json: sources[].packages, dest.document_class/mode.
Writes plan.json: preamble.packages_hoisted[], preamble.packages_dropped[], and
       the dest global preamble block on disk. Dedups \\usepackage by name; DROPS
       class-owned / layout packages (geometry, fonts, margins, anything the dest
       class already provides). Tier-2 flag for the SAME package requested with
       CONFLICTING options (keep one, flag the conflict; never silently pick) and
       for "is this class-owned?" calls it cannot make soundly.

CLI:
  hoist_preamble.py --plan PLAN --dest DIR
"""
import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description="hoist source preambles into one global preamble (STUB)")
    ap.add_argument("--plan", help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", help="destination project directory")
    ap.parse_known_args(argv)
    sys.stderr.write(
        "STUB: hoist_preamble.py is not implemented yet (pt-nww / T3). "
        "The mol-latex-concat DAG is wired to this stub; package union lands in T3.\n"
    )
    return 70  # EX_SOFTWARE: not implemented


if __name__ == "__main__":
    raise SystemExit(main())
