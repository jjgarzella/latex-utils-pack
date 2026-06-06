#!/usr/bin/env python3
"""merge_bib — concat/dedup every source bibliography into one. STUB.

Tier 1 (parse/dedup/merge) + Tier 2 (flag-and-defer). Owner: pt-nni (T6).
Part of the mol-latex-concat formula (latex-utils pack).

Reads  plan.json: sources[].bib_files/bib_backend/slug.
Writes the merged .bib + plan.json bib.*: concat all entries, COLLAPSE exact
       duplicates (same key, same work) to one; where the SAME key refers to
       DIFFERENT works, slug-rename the later paper's key and find-replace its
       \\cite uses within that chapter (recorded in renames.bib_keys). Keys are NOT
       slug-prefixed wholesale. Supports bibtex (\\bibliography) and biblatex/biber
       (\\addbibresource), auto-detected. Tier-2 flag exotic .bst weirdness and any
       same-key conflict it cannot resolve mechanically.

CLI:
  merge_bib.py --plan PLAN --dest DIR
"""
import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description="merge and dedup bibliographies (STUB)")
    ap.add_argument("--plan", help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", help="destination project directory")
    ap.parse_known_args(argv)
    sys.stderr.write(
        "STUB: merge_bib.py is not implemented yet (pt-nni / T6). "
        "The mol-latex-concat DAG is wired to this stub; bib merge lands in T6.\n"
    )
    return 70  # EX_SOFTWARE: not implemented


if __name__ == "__main__":
    raise SystemExit(main())
