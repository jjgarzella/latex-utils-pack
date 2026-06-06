#!/usr/bin/env python3
"""rewrite_paths — mirror each source into contents/<slug>/ and rewrite paths. STUB.

Tier 1 (mechanical, literal path rewriting). Owner: pt-axx (T5).
Part of the mol-latex-concat formula (latex-utils pack).

Reads  plan.json: sources[].dir/slug/includes/figures/local_sty.
Writes the dest tree + plan.json: copy each source verbatim into contents/<slug>/
       (sub-.tex, figures, local .sty), then rewrite relative references so they
       resolve from the dest root: \\input/\\include/\\subfile{intro} ->
       {contents/<slug>/intro}, \\includegraphics{foo} -> {contents/<slug>/foo}.
       Records chapter_main / mirrored paths on the source entry. Tier-2 flag any
       path it cannot resolve to a real mirrored file (computed/\\graphicspath paths).

CLI:
  rewrite_paths.py --plan PLAN --dest DIR
"""
import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description="mirror sources and rewrite relative paths (STUB)")
    ap.add_argument("--plan", help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--dest", help="destination project directory")
    ap.parse_known_args(argv)
    sys.stderr.write(
        "STUB: rewrite_paths.py is not implemented yet (pt-axx / T5). "
        "The mol-latex-concat DAG is wired to this stub; path rewriting lands in T5.\n"
    )
    return 70  # EX_SOFTWARE: not implemented


if __name__ == "__main__":
    raise SystemExit(main())
