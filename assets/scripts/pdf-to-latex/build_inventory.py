#!/usr/bin/env python3
"""Extract a numbering inventory from a PDF's text layer into numbering.json.

Usage (standalone): build_inventory.py --paper-dir DIR --input-pdf PDF [--kinds K1,K2,...] [--skip-kinds K1,...]
Usage (plan-driven): build_inventory.py --plan PLAN_JSON [overrides: --paper-dir, --input-pdf, --kinds, --skip-kinds]

When --plan is provided, paper_dir, input_pdf, extra_kinds, and skip_kinds are read
from plan.json (vars.paper_dir, input_pdf, vars.extra_kinds, vars.skip_kinds). The
plan is extended with inventory_counts and a log entry on completion.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
import planlib  # noqa: E402

DEFAULT_KINDS = ["Theorem", "Lemma", "Proposition", "Corollary", "Definition",
                 "Remark", "Example", "Conjecture"]


def discover_kinds(text, extra, skip):
    found = set()
    for m in re.finditer(r"^[ \t]*([A-Z][a-z]+)\s+\d+(?:\.\d+)*\.?\s", text, re.MULTILINE):
        found.add(m.group(1))
    found |= set(extra)
    found -= {"Chapter", "Section", "Subsection", "Figure", "Table", "Equation",
               "May", "Examples"}
    found -= set(skip)
    return sorted(found)


def page_of(offset, ff_offsets):
    p = 1
    for ff in ff_offsets:
        if offset > ff:
            p += 1
        else:
            break
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="",
                    help="path to plan.json; overrides other args when set")
    ap.add_argument("--paper-dir", default="")
    ap.add_argument("--input-pdf", default="")
    ap.add_argument("--kinds", default="", help="comma-separated override/append")
    ap.add_argument("--skip-kinds", default="")
    args = ap.parse_args()

    plan_data = None
    if args.plan:
        plan_data = planlib.load(args.plan)
        if not args.paper_dir:
            args.paper_dir = plan_data["vars"]["paper_dir"]
        if not args.input_pdf:
            args.input_pdf = plan_data.get("input_pdf", "")
        if not args.kinds:
            raw = plan_data["vars"].get("extra_kinds", "")
            try:
                parsed = json.loads(raw) if raw else []
                args.kinds = ",".join(parsed)
            except (json.JSONDecodeError, TypeError):
                args.kinds = raw
        if not args.skip_kinds:
            raw = plan_data["vars"].get("skip_kinds", "")
            try:
                parsed = json.loads(raw) if raw else []
                args.skip_kinds = ",".join(parsed)
            except (json.JSONDecodeError, TypeError):
                args.skip_kinds = raw

    if not args.paper_dir:
        ap.error("--paper-dir is required (or provide --plan with vars.paper_dir)")
    if not args.input_pdf:
        ap.error("--input-pdf is required (or provide --plan with input_pdf set by check-prereqs)")

    paper_dir = Path(args.paper_dir)
    input_pdf = Path(args.input_pdf)
    text_path = paper_dir / "_text.txt"

    subprocess.check_call(["pdftotext", "-layout", str(input_pdf), str(text_path)])
    text = text_path.read_text()
    ff_offsets = [m.start() for m in re.finditer(r"\f", text)]

    extra = [k for k in args.kinds.split(",") if k]
    skip = [k for k in args.skip_kinds.split(",") if k]
    kinds = discover_kinds(text, extra, skip) if not extra else sorted(set(DEFAULT_KINDS) | set(extra) - set(skip))

    inventory = []
    kind_re = re.compile(
        r"^[ \t]*(" + "|".join(kinds) + r")\s+(\d+(?:\.\d+)*)"
        r"(?:\s+[\(\[][^\n]{0,120}[\)\]])?\.\s*(.{0,100})",
        re.MULTILINE,
    )
    for m in kind_re.finditer(text):
        inventory.append({"kind": m.group(1), "number": m.group(2),
                          "page": page_of(m.start(), ff_offsets),
                          "snippet": m.group(3).strip()})

    chap_re = re.compile(r"^[ \t]*(Chapter|Section|Subsection)\s+(\d+(?:\.\d+)*)\.?\s+(.{0,100})", re.MULTILINE)
    for m in chap_re.finditer(text):
        inventory.append({"kind": m.group(1), "number": m.group(2),
                          "page": page_of(m.start(), ff_offsets), "snippet": m.group(3).strip()})

    eq_re = re.compile(r"\((\d+\.\d+)\)\s*$", re.MULTILINE)
    for m in eq_re.finditer(text):
        inventory.append({"kind": "Equation", "number": m.group(1),
                          "page": page_of(m.start(), ff_offsets), "snippet": ""})

    ft_re = re.compile(r"^[ \t]*(Figure|Table)\s+(\d+(?:\.\d+)*)[:\.]?\s+(.{0,100})", re.MULTILINE)
    for m in ft_re.finditer(text):
        inventory.append({"kind": m.group(1), "number": m.group(2),
                          "page": page_of(m.start(), ff_offsets), "snippet": m.group(3).strip()})

    seen = set()
    deduped = []
    for e in inventory:
        key = (e["kind"], e["number"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    deduped.sort(key=lambda e: (e["page"], e["kind"], e["number"]))

    (paper_dir / "numbering.json").write_text(json.dumps(deduped, indent=2))

    counts = Counter(e["kind"] for e in deduped)
    print(f"kinds discovered: {kinds}")
    print(f"Total entries: {len(deduped)}")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")

    if plan_data is not None:
        plan_data = planlib.load(args.plan)
        plan_data["inventory_counts"] = dict(sorted(counts.items()))
        planlib.add_log(plan_data, "build-inventory",
                        f"Extracted {len(deduped)} entries; kinds={kinds}")
        planlib.save(args.plan, plan_data)


if __name__ == "__main__":
    main()
