#!/usr/bin/env python3
"""verify — the mol-latex-concat compile gate: per-source baseline + final-merge.

Generalizes ``assets/scripts/build-verify.sh`` into a parameterized, two-mode gate
(build-verify.sh stays as the working pdflatex+bibtex gate until this fully
supersedes it). This is the Tier-1 compile orchestration AND the safety net: a
construct a Tier-2 helper could not soundly transform surfaces here as an
``undefined reference`` / ``undefined control sequence`` / multiply-defined label,
and the agent (Tier 3) fixes it.

Two modes, one gate:
  --baseline  Compile ONE source standalone. Each source MUST build on its own
              before the merge touches it; a won't-compile source is the SOURCE's
              defect, so fail early (exit 2) with its own log. Records
              ``sources[].baseline_compiled`` in plan.json.
  --final     Compile the merged destination as the HARD GATE. Exit 2 on any TeX
              error, undefined reference, undefined citation, or multiply-defined
              label.

Engine (``--engine`` > plan ``vars.tex_engine`` > per-source hint > pdflatex):
pdflatex | xelatex | lualatex — each run ``-interaction=nonstopmode -halt-on-error
-file-line-error`` so a bad doc never blocks on terminal input and TeX errors yield
a non-zero exit.

Bib backend is AUTO-DETECTED from post-pass-1 artifacts (authoritative — reflects
what the compile actually did, immune to commented-out source):
  * a ``.bcf`` control file => biblatex/biber -> ``biber <jobname>`` per .bcf
  * ``\\bibdata`` in an .aux  => bibtex        -> ``bibtex <base>`` over EVERY citing
                                                 aux (covers chapterbib / bibunits
                                                 multi-aux); else a .tex source scan.
Undefined refs/cites and multiply-defined labels are caught by scanning the engine
``.log`` for the backend-agnostic kernel summary lines (``There were undefined
references`` / ``... multiply-defined labels``) plus the per-occurrence phrases; for
biber, the ``.blg`` is also scanned for a missing database entry (= undefined
citation). A custom ``.bst`` shipped in the tree raises a best-effort Tier-2
``exotic-bst`` flag.

Breadth caveat (see the formula): biblatex/biber and xelatex/lualatex are
implemented but validated only on the pdflatex+bibtex path.

This gate installs nothing; it assumes a complete local TeX toolchain.

CLI:
  verify.py [--plan PLAN] --baseline --source DIR [--engine ENGINE] [--entry FILE]
  verify.py [--plan PLAN] --final --dest DIR [--entry main.tex] [--engine ENGINE]

Exit status: 0 clean · 1 usage/environment · 2 failed the gate.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

# planlib is the sibling reference implementation of the plan.json contract.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import planlib  # noqa: E402
except Exception:  # pragma: no cover - planlib should always be present
    planlib = None

ENGINES = ("pdflatex", "xelatex", "lualatex")
ENGINE_ARGS = ["-interaction=nonstopmode", "-halt-on-error", "-file-line-error"]

EXIT_OK = 0
EXIT_USAGE = 1   # usage / environment (missing tool) — not a source defect
EXIT_GATE = 2    # the build failed the gate

# Kernel summary + per-occurrence phrases that pdflatex/xelatex/lualatex exit 0 on
# but we treat as fatal. The two summary lines are backend-agnostic (the LaTeX
# kernel prints them for undefined refs AND undefined cites, bibtex or biblatex).
# grep matches the phrase, not the (line-wrapped) label, so 79-col wrapping is
# harmless. English-/TeX-Live-specific by design.
FATAL_PHRASES = [
    ("There were undefined references", "undefined references present"),
    ("There were multiply-defined labels", "multiply-defined labels present"),
    ("Reference `", "undefined reference(s)"),    # 'Reference `foo' on page ... undefined'
    ("Citation `", "undefined citation(s)"),      # 'Citation `bar' on page ... undefined'
]


# --------------------------------------------------------------------------- io

def which(tool: str) -> bool:
    return shutil.which(tool) is not None


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _grep_lines(path: str, pred) -> list:
    """Stripped lines of `path` satisfying `pred` (empty list if no such file)."""
    try:
        return [ln.strip() for ln in read_text(path).splitlines() if pred(ln)]
    except OSError:
        return []


def _walk_ext(root: str, ext: str) -> list:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(ext):
                out.append(os.path.join(dirpath, f))
    return out


def _strip_comments(text: str) -> str:
    """Drop TeX line comments so a commented-out \\bibliography can't fool the
    source-scan fallback. (Artifact detection needs no stripping.)"""
    return re.sub(r"(?<!\\)%.*", "", text)


def log_tail(log: str, n: int = 40) -> list:
    try:
        lines = read_text(log).splitlines()
    except OSError:
        return ["(no log at %s)" % log]
    return ["--- tail of %s ---" % os.path.basename(log)] + [ln for ln in lines[-n:]]


# --------------------------------------------------------------- compile + gate

def run_engine(engine: str, entry: str, cwd: str, label: str) -> int:
    print("verify: %s pass %s on %s ..." % (engine, label, entry), flush=True)
    return subprocess.run([engine] + ENGINE_ARGS + [entry], cwd=cwd).returncode


def aux_files_with(cwd: str, *needles: str) -> list:
    """Relative paths of every .aux under `cwd` containing ALL `needles`."""
    hits = []
    for aux in _walk_ext(cwd, ".aux"):
        text = read_text(aux)
        if all(n in text for n in needles):
            hits.append(os.path.relpath(aux, cwd))
    return sorted(hits)


def detect_backend(cwd: str):
    """Auto-detect the bibliography backend. Post-pass-1 artifacts are
    authoritative; fall back to a comment-stripped source scan. Returns
    "biblatex" | "bibtex" | None."""
    if _walk_ext(cwd, ".bcf"):
        return "biblatex"
    if aux_files_with(cwd, "\\bibdata"):
        return "bibtex"
    texts = [_strip_comments(read_text(t)) for t in _walk_ext(cwd, ".tex")]
    biblatex_pkg = re.compile(r"\\usepackage(\[[^\]]*\])?\{[^}]*\bbiblatex\b[^}]*\}")
    for t in texts:
        if "\\addbibresource" in t or biblatex_pkg.search(t):
            return "biblatex"
    for t in texts:
        if "\\bibliography{" in t:
            return "bibtex"
    return None


def run_bibtex(cwd: str):
    """Run bibtex over every aux that actually cites something (chapterbib /
    bibunits => many such aux). Returns (ok, error_lines)."""
    errs = []
    citing = aux_files_with(cwd, "\\citation", "\\bibdata")
    if not citing:
        print("verify: (no \\citation+\\bibdata aux found; skipping bibtex)")
        return True, errs
    for aux in citing:
        base = aux[:-4]  # strip .aux; bibtex accepts the relative path
        print("verify: bibtex %s" % base)
        rc = subprocess.run(["bibtex", base], cwd=cwd).returncode
        blg = os.path.join(cwd, base + ".blg")
        hard = _grep_lines(blg, lambda ln: re.search(r"error|i found no", ln, re.I))
        if rc != 0 or hard:
            errs.append("bibtex failed on %s (exit %d)" % (base, rc))
            errs.extend("  " + h for h in hard[:20])
    return (not errs), errs


def run_biber(cwd: str):
    """Run biber over every .bcf control file. Returns (ok, error_lines).
    (Missing-entry warnings are scanned separately in the final gate.)"""
    errs = []
    bcfs = _walk_ext(cwd, ".bcf")
    if not bcfs:
        print("verify: (no .bcf control file found; skipping biber)")
        return True, errs
    for bcf in bcfs:
        base = os.path.relpath(bcf, cwd)[:-4]  # biber reads <base>.bcf
        print("verify: biber %s" % base)
        rc = subprocess.run(["biber", base], cwd=cwd).returncode
        if rc != 0:
            errs.append("biber failed on %s (exit %d)" % (base, rc))
    return (not errs), errs


def scan_log(log: str) -> list:
    """Scan the engine .log for fatal warnings pdflatex exits 0 on."""
    try:
        lines = read_text(log).splitlines()
    except OSError:
        return ["engine log not found: %s" % log]
    problems = []
    for phrase, label in FATAL_PHRASES:
        hits = [ln.strip() for ln in lines if phrase in ln]
        if hits:
            problems.append("%s:" % label)
            problems.extend("  " + h for h in hits[:20])
    return problems


def scan_biber_citations(cwd: str) -> list:
    """biber reports an undefined citation as a WARN (exit 0); surface it as a
    gate failure to match bibtex's behaviour. Also catch biber ERROR lines."""
    problems = []
    for blg in _walk_ext(cwd, ".blg"):
        rel = os.path.relpath(blg, cwd)
        miss = _grep_lines(blg, lambda ln: "I didn't find a database entry" in ln)
        err = _grep_lines(blg, lambda ln: re.search(r"\bERROR\b", ln))
        if miss:
            problems.append("undefined citation(s) [biber %s]:" % rel)
            problems.extend("  " + m for m in miss[:20])
        if err:
            problems.append("biber errors [%s]:" % rel)
            problems.extend("  " + e for e in err[:20])
    return problems


def compile_and_gate(project_dir: str, entry: str, engine: str):
    """Compile `entry` in `project_dir` with `engine`, run the detected bib
    backend, settle over three passes, then scan the logs.

    Returns (exit_code, problems, backend):
      0 clean · 1 missing tool (environment) · 2 failed the gate.
    """
    jobname = os.path.splitext(os.path.basename(entry))[0]
    log = os.path.join(project_dir, jobname + ".log")
    pdf = os.path.join(project_dir, jobname + ".pdf")

    if not which(engine):
        return EXIT_USAGE, ["required engine not on PATH: %s "
                            "(this gate assumes a complete toolchain; install it and retry)"
                            % engine], None

    # Pass 1 — generate aux / .bcf.
    if run_engine(engine, entry, project_dir, "1/3") != 0:
        return EXIT_GATE, ["%s errored on pass 1" % engine] + log_tail(log), None

    backend = detect_backend(project_dir)
    if backend == "bibtex":
        if not which("bibtex"):
            return EXIT_USAGE, ["bibtex backend detected but bibtex not on PATH"], backend
        ok, msgs = run_bibtex(project_dir)
        if not ok:
            return EXIT_GATE, msgs, backend
    elif backend == "biblatex":
        if not which("biber"):
            return EXIT_USAGE, ["biblatex/biber backend detected but biber not on PATH"], backend
        ok, msgs = run_biber(project_dir)
        if not ok:
            return EXIT_GATE, msgs, backend
    else:
        print("verify: (no bibliography backend detected; skipping bib step)")

    # Passes 2 and 3 — resolve refs/cites and settle cross-references.
    for label in ("2/3", "3/3"):
        if run_engine(engine, entry, project_dir, label) != 0:
            return EXIT_GATE, ["%s errored on pass %s" % (engine, label)] + log_tail(log), backend

    if not os.path.isfile(pdf):
        return EXIT_GATE, ["no %s.pdf produced" % jobname], backend

    problems = scan_log(log)
    if backend == "biblatex":
        problems += scan_biber_citations(project_dir)

    return (EXIT_GATE if problems else EXIT_OK), problems, backend


# ----------------------------------------------------------------- plan.json io

def load_plan(path):
    if not path:
        return None
    if planlib is None:
        sys.stderr.write("verify: planlib unavailable; running compile-only (no plan.json I/O).\n")
        return None
    try:
        return planlib.load(path)
    except OSError as exc:
        sys.stderr.write("verify: plan.json not readable (%s); running compile-only.\n" % exc)
        return None


def save_plan(path, plan):
    if plan is None or planlib is None or not path:
        return
    try:
        planlib.save(path, plan)
    except OSError as exc:
        sys.stderr.write("verify: could not write plan.json (%s).\n" % exc)


def find_source(plan, source_dir):
    """The source-inventory entry for `source_dir` — exact match first (what the
    formula passes verbatim from plan.json), then a realpath fallback."""
    if not plan:
        return None
    s = planlib.source_by_dir(plan, source_dir)
    if s is not None:
        return s
    target = os.path.realpath(source_dir)
    for cand in plan.get("sources", []):
        d = cand.get("dir")
        if d and os.path.realpath(d) == target:
            return cand
    return None


def resolve_engine(override, plan, src_obj=None):
    if override:
        eng = override
    elif plan and plan.get("vars", {}).get("tex_engine"):
        eng = plan["vars"]["tex_engine"]
    elif src_obj and src_obj.get("tex_engine"):
        eng = src_obj["tex_engine"]
    else:
        eng = "pdflatex"
    if eng not in ENGINES:
        sys.stderr.write("verify: unknown engine '%s'; falling back to pdflatex.\n" % eng)
        eng = "pdflatex"
    return eng


def autodetect_entry(src_dir):
    """The .tex holding \\begin{document} (preferring main.tex), for ad-hoc runs
    where plan.json has not recorded an entry_file."""
    candidates = []
    try:
        names = sorted(os.listdir(src_dir))
    except OSError:
        return None
    for name in names:
        if name.endswith(".tex"):
            text = read_text(os.path.join(src_dir, name))
            if "\\begin{document}" in text and "\\documentclass" in text:
                candidates.append(name)
    if "main.tex" in candidates:
        return "main.tex"
    if candidates:
        return candidates[0]
    if os.path.isfile(os.path.join(src_dir, "main.tex")):
        return "main.tex"
    return None


def _flag_once(plan, *, tier, stage, kind, message, slug=None, severity="warn", location=None):
    """add_flag, but idempotent across retries (dedup on kind/location/slug)."""
    for f in plan.get("flags", []):
        if f.get("kind") == kind and f.get("location") == location and f.get("slug") == slug:
            return
    planlib.add_flag(plan, tier=tier, stage=stage, kind=kind, message=message,
                     slug=slug, severity=severity, location=location)


def flag_exotic_bst(plan, cwd, slug=None):
    """Best-effort Tier-2 flag: a custom .bst shipped inside the tree (its
    \\bibstyle resolves to a local file, not a standard TeX Live style). The
    compile still validates the actual output; this only tells the reviewer the
    bibliography style was not checked against a known-good standard."""
    if plan is None:
        return
    styles = set()
    for aux in aux_files_with(cwd, "\\bibstyle"):
        for m in re.finditer(r"\\bibstyle\{([^}]*)\}", read_text(os.path.join(cwd, aux))):
            styles.add(m.group(1))
    local = {os.path.splitext(os.path.basename(p))[0] for p in _walk_ext(cwd, ".bst")}
    for st in sorted(styles & local):
        _flag_once(plan, tier=2, stage="verify", kind="exotic-bst", severity="info",
                   slug=slug, location="%s.bst" % st,
                   message=("custom .bst '%s.bst' is shipped in the tree; the compile "
                            "validates output but it was not checked against a standard "
                            "TeX Live bibliography style." % st))


# ------------------------------------------------------------------------ modes

def report(code, problems, what):
    if code == EXIT_OK:
        print("verify: PASS — %s built clean." % what)
        return
    kind = "environment error" if code == EXIT_USAGE else "FAILED the gate"
    sys.stderr.write("verify: %s — %s:\n" % (what, kind))
    for p in problems:
        sys.stderr.write(p + "\n")


def usage(msg):
    sys.stderr.write("verify: %s\n" % msg)
    return EXIT_USAGE


def do_baseline(plan_path, source_dir, engine_override, entry_override):
    if not source_dir or not os.path.isdir(source_dir):
        return usage("--baseline needs an existing --source DIR (got %r)" % source_dir)
    plan = load_plan(plan_path)
    src_obj = find_source(plan, source_dir)
    entry = entry_override or (src_obj or {}).get("entry_file") or autodetect_entry(source_dir)
    if not entry:
        return usage("could not find an entry .tex (\\begin{document}) under %s" % source_dir)
    engine = resolve_engine(engine_override, plan, src_obj)

    print("verify: baseline compile of %s — entry=%s engine=%s" % (source_dir, entry, engine))
    code, problems, backend = compile_and_gate(source_dir, entry, engine)

    if plan is not None and src_obj is not None:
        if not src_obj.get("entry_file"):
            src_obj["entry_file"] = entry
        if backend and not src_obj.get("bib_backend"):
            src_obj["bib_backend"] = backend
        if code == EXIT_GATE:
            src_obj["baseline_compiled"] = False
        elif code == EXIT_OK:
            src_obj["baseline_compiled"] = True
        # EXIT_USAGE (missing tool) leaves baseline_compiled null — not a source defect.
        slug = src_obj.get("slug") or source_dir
        if code == EXIT_OK:
            planlib.add_log(plan, "baseline",
                            "%s: compiled standalone (%s/%s)." % (slug, engine, backend or "no-bib"))
        else:
            planlib.add_log(plan, "baseline",
                            "%s: baseline did NOT pass (%s/%s); %d problem(s)."
                            % (slug, engine, backend or "no-bib", len(problems)))
        save_plan(plan_path, plan)

    report(code, problems, "baseline %s" % source_dir)
    return code


def do_final(plan_path, dest_dir, entry_override, engine_override):
    if not dest_dir or not os.path.isdir(dest_dir):
        return usage("--final needs an existing --dest DIR (got %r)" % dest_dir)
    plan = load_plan(plan_path)
    entry = entry_override or (plan or {}).get("dest", {}).get("master_tex") or "main.tex"
    if not os.path.isfile(os.path.join(dest_dir, entry)):
        return usage("master entry not found: %s" % os.path.join(dest_dir, entry))
    engine = resolve_engine(engine_override, plan, None)

    print("verify: FINAL gate on %s — entry=%s engine=%s" % (dest_dir, entry, engine))
    code, problems, backend = compile_and_gate(dest_dir, entry, engine)

    if plan is not None:
        dest = plan.setdefault("dest", {})
        if backend and not dest.get("bib_backend"):
            dest["bib_backend"] = backend
        if backend == "bibtex":
            flag_exotic_bst(plan, dest_dir)
        if code == EXIT_OK:
            planlib.add_log(plan, "verify",
                            "merged dest compiled CLEAN (%s/%s); hard gate passed."
                            % (engine, backend or "no-bib"))
        else:
            planlib.add_log(plan, "verify",
                            "merged dest gate did NOT pass (%s/%s); %d problem(s)."
                            % (engine, backend or "no-bib", len(problems)))
        save_plan(plan_path, plan)

    report(code, problems, "final %s" % dest_dir)
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="LaTeX compile gate for mol-latex-concat: per-source baseline + final merge.")
    ap.add_argument("--plan", help="path to plan.json (the agent<->script contract)")
    ap.add_argument("--baseline", action="store_true",
                    help="per-source standalone baseline compile")
    ap.add_argument("--final", action="store_true", help="final merged-dest hard gate")
    ap.add_argument("--source", help="source directory (baseline mode)")
    ap.add_argument("--dest", help="destination project directory (final mode)")
    ap.add_argument("--entry", help="master .tex filename (final default: main.tex; "
                                     "baseline default: plan entry_file or auto-detect)")
    ap.add_argument("--engine", choices=ENGINES,
                    help="tex engine override (else plan vars.tex_engine, else pdflatex)")
    args = ap.parse_args(argv)

    if args.baseline == args.final:  # both set or neither set
        return usage("choose exactly one mode: --baseline or --final")
    if args.baseline:
        return do_baseline(args.plan, args.source, args.engine, args.entry)
    return do_final(args.plan, args.dest, args.entry, args.engine)


if __name__ == "__main__":
    raise SystemExit(main())
