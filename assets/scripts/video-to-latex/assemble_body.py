#!/usr/bin/env python3
"""Concatenate pass2/body_segment_*.tex into body.tex in sorted segment-id order.

Usage:
  assemble_body.py --paper-dir DIR
"""
import argparse
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-dir", required=True)
    args = ap.parse_args()

    paper_dir = Path(args.paper_dir).resolve()
    pass2 = paper_dir / "pass2"
    out = paper_dir / "body.tex"

    parts = sorted(pass2.glob("body_segment_*.tex"))
    if not parts:
        out.write_text("% no body_segment_*.tex files found\n")
        print("[assemble_body] warning: no body_segment_*.tex files")
        return

    chunks = []
    for p in parts:
        chunks.append(f"% ---- begin {p.name} ----")
        chunks.append(p.read_text().rstrip())
        chunks.append(f"% ---- end {p.name} ----")
        chunks.append("")
    out.write_text("\n".join(chunks) + "\n")
    print(f"[assemble_body] wrote {out} from {len(parts)} segment files")


if __name__ == "__main__":
    main()
