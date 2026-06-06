#!/usr/bin/env python3
"""prefix_labels — slug-prefix every \\label and rewrite the full ref family. STUB.

Tier 1 (mechanical, proactive). Owner: pt-dyq (T4).
Shares one TeX-aware safe find-replace core with resolve_macros.py.
Part of the mol-latex-concat formula (latex-utils pack).

Reads  plan.json: sources[].labels, slug, chapter_main.
Writes plan.json: renames.labels[]; rewrites each chapter's files in place. Every
       \\label{k} -> \\label{<slug>:k}, and every reference to it is rewritten across
       the whole ref family: \\ref \\eqref \\cref \\Cref \\autoref \\pageref \\nameref
       \\labelcref \\vref \\cpageref \\namecref ... Done PROACTIVELY (all chapters) so
       cross-paper label clashes cannot occur. Tier-2 flag custom/package-defined
       ref-like macros it does not recognize (e.g. \\myref) so the agent can extend
       the family or rewrite by hand.

CLI:
  prefix_labels.py --plan PLAN --dest DIR
"""
import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description="slug-prefix labels and rewrite refs (STUB)")
    ap.add_argument("--plan", help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", help="destination project directory")
    ap.parse_known_args(argv)
    sys.stderr.write(
        "STUB: prefix_labels.py is not implemented yet (pt-dyq / T4). "
        "The mol-latex-concat DAG is wired to this stub; label prefixing lands in T4.\n"
    )
    return 70  # EX_SOFTWARE: not implemented


if __name__ == "__main__":
    raise SystemExit(main())
