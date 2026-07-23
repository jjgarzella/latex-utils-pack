#!/usr/bin/env python3
"""Verify numbering fidelity of the rebuilt PDF.

Usage (standalone): verify.py --paper-dir DIR --input-pdf PDF [--final] [--engine ENGINE]
Usage (plan-driven): verify.py --plan PLAN_JSON [--final] [--engine ENGINE]

Without --final: check numbering fidelity only (assumes compile already ran).
With --final: compile paper.tex first (hard gate — exits 1 if PDF not produced),
then check numbering fidelity. This is the standard hard gate for the formula.

Hard gate (--final): compile exit 0 (PDF produced) + numbering checks.
Soft gate (no --final): every inventory entry in rebuilt PDF and wrapped in body.tex.
Soft warning: page count within +/- 20%.

Exit codes: 0 = all pass; 1 = compile failure (--final only); 2 = soft numbering failures.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
import planlib  # noqa: E402

THM_KINDS = {"Theorem", "Lemma", "Proposition", "Corollary", "Definition",
             "Remark", "Example", "Conjecture"}


def pdf_page_count(pdf):
    out = subprocess.check_output(["pdfinfo", str(pdf)]).decode()
    return int(out.split("Pages:")[1].split()[0])


def compile_paper(paper_dir: Path, tex_engine: str, log_file: Path) -> bool:
    """Run tex_engine twice on paper.tex; return True if paper.pdf was produced."""
    build_dir = paper_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    paper_tex = paper_dir / "paper.tex"
    env = {**os.environ, "TEXINPUTS": f".:{paper_dir}:"}
    cmd = [tex_engine, "-interaction=nonstopmode",
           f"-output-directory={build_dir}", str(paper_tex)]
    with open(log_file, "a") as logfh:
        for _ in range(2):
            subprocess.run(cmd, env=env, stdout=logfh, stderr=logfh)
    return (build_dir / "paper.pdf").exists()


def check_numbering(paper_dir: Path, input_pdf: Path):
    """Check numbering fidelity. Returns (missing_in_pdf, unwrapped_in_body, page_status, inv)."""
    inv = json.loads((paper_dir / "numbering.json").read_text())
    body = (paper_dir / "body.tex").read_text()

    rebuilt_pdf = paper_dir / "build" / "paper.pdf"
    if not rebuilt_pdf.exists():
        print(f"FAIL gate (a): {rebuilt_pdf} missing")
        sys.exit(1)

    rebuilt_txt = paper_dir / "build" / "_rebuilt.txt"
    subprocess.check_call(["pdftotext", str(rebuilt_pdf), str(rebuilt_txt)])
    rebuilt = rebuilt_txt.read_text()

    missing_in_pdf = []
    unwrapped_in_body = []
    for e in inv:
        if e["kind"] == "Equation":
            needle = f"({e['number']})"
        else:
            needle = f"{e['kind']} {e['number']}"
        if needle not in rebuilt:
            missing_in_pdf.append(needle)

        if e["kind"] in THM_KINDS:
            if f"\\textbf{{{e['kind']} {e['number']}" not in body:
                unwrapped_in_body.append(f"{e['kind']} {e['number']}")
        elif e["kind"] in {"Chapter", "Section", "Subsection"}:
            if e["number"] not in body:
                unwrapped_in_body.append(f"{e['kind']} {e['number']}")
        elif e["kind"] == "Equation":
            if f"\\tag{{{e['number']}}}" not in body and f"({e['number']})" not in body:
                unwrapped_in_body.append(f"Equation {e['number']}")
        elif e["kind"] in {"Figure", "Table"}:
            if f"{e['kind']} {e['number']}" not in body:
                unwrapped_in_body.append(f"{e['kind']} {e['number']}")

    orig_pages = pdf_page_count(input_pdf)
    rebuilt_pages = pdf_page_count(rebuilt_pdf)
    ratio = rebuilt_pages / orig_pages
    page_status = "OK" if 0.8 <= ratio <= 1.2 else "WARN"

    N = len(inv)
    print("=== VERIFY SUMMARY ===")
    print(f"Inventory size: {N}")
    print(f"Rendered in rebuilt PDF: {N - len(missing_in_pdf)}/{N}")
    print(f"Wrapped in body.tex:     {N - len(unwrapped_in_body)}/{N}")
    print(f"Page count: orig={orig_pages} rebuilt={rebuilt_pages} ratio={ratio:.2f} [{page_status}]")
    if missing_in_pdf:
        print(f"\nMissing in PDF ({len(missing_in_pdf)}):")
        for m in missing_in_pdf[:20]:
            print(f"  - {m}")
        if len(missing_in_pdf) > 20:
            print(f"  ... and {len(missing_in_pdf) - 20} more")
    if unwrapped_in_body:
        print(f"\nUnwrapped in body.tex ({len(unwrapped_in_body)}):")
        for m in unwrapped_in_body[:20]:
            print(f"  - {m}")
        if len(unwrapped_in_body) > 20:
            print(f"  ... and {len(unwrapped_in_body) - 20} more")

    return missing_in_pdf, unwrapped_in_body, page_status, inv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="",
                    help="path to plan.json; overrides other args when set")
    ap.add_argument("--paper-dir", default="")
    ap.add_argument("--input-pdf", default="")
    ap.add_argument("--final", action="store_true",
                    help="compile paper.tex first, then verify (hard gate)")
    ap.add_argument("--engine", default="",
                    help="TeX engine for --final compile; default from plan or xelatex")
    args = ap.parse_args()

    plan_data = None
    if args.plan:
        plan_data = planlib.load(args.plan)
        if not args.paper_dir:
            args.paper_dir = plan_data["vars"]["paper_dir"]
        if not args.input_pdf:
            args.input_pdf = plan_data.get("input_pdf", "")
        if not args.engine:
            args.engine = plan_data["vars"].get("tex_engine", "xelatex")

    if not args.engine:
        args.engine = "xelatex"
    if not args.paper_dir:
        ap.error("--paper-dir is required (or provide --plan with vars.paper_dir)")
    if not args.input_pdf:
        ap.error("--input-pdf is required (or provide --plan with input_pdf set by check-prereqs)")

    paper_dir = Path(args.paper_dir)
    input_pdf = Path(args.input_pdf)

    step_name = "compile-and-verify" if args.final else "verify"

    if args.final:
        log_file = paper_dir / "conversion.log"
        print(f"[compile] running {args.engine} twice on {paper_dir}/paper.tex ...")
        ok = compile_paper(paper_dir, args.engine, log_file)
        if not ok:
            msg = (f"FAIL gate (compile): {args.engine} produced no PDF at "
                   f"{paper_dir}/build/paper.pdf — see {log_file}")
            print(msg)
            if plan_data is not None:
                plan_data = planlib.load(args.plan)
                planlib.add_log(plan_data, step_name, f"FAIL: compile produced no PDF")
                planlib.save(args.plan, plan_data)
            sys.exit(1)
        print(f"[compile] PDF produced at {paper_dir}/build/paper.pdf")

    missing_in_pdf, unwrapped_in_body, page_status, inv = check_numbering(paper_dir, input_pdf)

    if plan_data is not None:
        plan_data = planlib.load(args.plan)
        N = len(inv)
        status = "PASS" if not (missing_in_pdf or unwrapped_in_body) else "SOFT_FAIL"
        planlib.add_log(plan_data, step_name,
                        f"{status}: {N - len(missing_in_pdf)}/{N} in PDF, "
                        f"{N - len(unwrapped_in_body)}/{N} wrapped; pages={page_status}")
        planlib.save(args.plan, plan_data)

    if missing_in_pdf or unwrapped_in_body:
        sys.exit(2)


if __name__ == "__main__":
    main()
