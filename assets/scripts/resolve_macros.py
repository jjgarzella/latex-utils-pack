#!/usr/bin/env python3
"""resolve_macros — collapse identical / rename divergent macro collisions. STUB.

Tier 1 (collapse/rename) + Tier 2 (flag-and-defer). Owner: pt-dyq (T4).
Shares one TeX-aware safe find-replace core with prefix_labels.py.
Part of the mol-latex-concat formula (latex-utils pack).

Reads  plan.json: sources[].macros, chapter order.
Writes plan.json: renames.macros[]; rewrites the colliding macro's uses within the
       later chapter's files. IDENTICAL bodies collapse to one; DIVERGENT bodies =>
       the EARLIER chapter keeps the bare name, the LATER chapter's macro gets a
       per-slug suffix (\\foo -> \\foopid) and its uses are find-replaced in that
       chapter only. Tier-2 flag any macro it cannot rewrite soundly (catcode/\\def
       trickery, package-defined names) rather than risk a wrong replace.

CLI:
  resolve_macros.py --plan PLAN --dest DIR
"""
import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description="resolve macro-name collisions (STUB)")
    ap.add_argument("--plan", help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", help="destination project directory")
    ap.parse_known_args(argv)
    sys.stderr.write(
        "STUB: resolve_macros.py is not implemented yet (pt-dyq / T4). "
        "The mol-latex-concat DAG is wired to this stub; the safe-rewrite core lands in T4.\n"
    )
    return 70  # EX_SOFTWARE: not implemented


if __name__ == "__main__":
    raise SystemExit(main())
