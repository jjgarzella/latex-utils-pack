#!/usr/bin/env bash
#
# build-verify.sh — hard-gate LaTeX compile check for the latex-concat formula.
#
# Compiles a LaTeX project and FAILS (non-zero exit) unless the build is fully
# clean: no TeX errors, no undefined references, no undefined citations, and no
# multiply-defined labels. This is the formula's compile gate; it installs
# nothing and assumes a complete local toolchain (pdflatex + bibtex).
#
# Usage:
#   build-verify.sh <project-dir> [entry-tex]
#
#   <project-dir>  Directory containing the master document (required).
#   [entry-tex]    Master .tex filename, relative to project-dir. Default: main.tex
#
# Exit status:
#   0  clean build (PDF produced, no error/warning of interest in the log)
#   1  usage / environment error
#   2  build failed the gate (errors or undefined refs/citations/dup labels)
#
# Design notes (intentionally defensive — see the pack handoff):
#   - pdflatex runs with -interaction=nonstopmode -halt-on-error so a bad doc
#     never hangs waiting for terminal input and errors yield a non-zero exit.
#   - bibtex is run over EVERY *.aux that contains a \citation, not just the
#     master aux. This covers plain multi-file bibliographies as well as
#     chapterbib / bibunits style per-chapter bibs.
#   - pdflatex's own exit code does NOT catch undefined refs/citations or
#     multiply-defined labels (those are warnings, exit 0). We grep the .log
#     for the stable English warning phrases. grep -F matches the phrase, not
#     the (possibly line-wrapped) label name, so 79-col wrapping is harmless.
#     This is English-/TeX-Live-specific by design.

set -u

PROJECT_DIR="${1:-}"
ENTRY_TEX="${2:-main.tex}"

if [ -z "$PROJECT_DIR" ]; then
  echo "build-verify: usage: build-verify.sh <project-dir> [entry-tex]" >&2
  exit 1
fi
if [ ! -d "$PROJECT_DIR" ]; then
  echo "build-verify: not a directory: $PROJECT_DIR" >&2
  exit 1
fi

cd "$PROJECT_DIR" || { echo "build-verify: cannot cd into $PROJECT_DIR" >&2; exit 1; }

if [ ! -f "$ENTRY_TEX" ]; then
  echo "build-verify: entry file not found: $PROJECT_DIR/$ENTRY_TEX" >&2
  exit 1
fi

for tool in pdflatex bibtex; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "build-verify: required tool not found on PATH: $tool" >&2
    echo "build-verify: this gate assumes a complete local toolchain; install it and retry." >&2
    exit 1
  fi
done

JOBNAME="${ENTRY_TEX%.tex}"
LOG="${JOBNAME}.log"
PDFLATEX=(pdflatex -interaction=nonstopmode -halt-on-error "$ENTRY_TEX")

run_pdflatex() {
  local pass="$1"
  echo "build-verify: pdflatex pass ${pass}..."
  if ! "${PDFLATEX[@]}"; then
    echo "build-verify: FAIL — pdflatex returned an error on pass ${pass}." >&2
    echo "build-verify: tail of ${LOG}:" >&2
    tail -n 40 "$LOG" >&2 2>/dev/null || true
    exit 2
  fi
}

# Pass 1: generate aux files.
run_pdflatex 1

# Bibliography: run bibtex on every aux that actually cites something.
echo "build-verify: running bibtex over aux files with citations..."
ran_bibtex=0
while IFS= read -r auxfile; do
  if grep -q '\\citation' "$auxfile" 2>/dev/null; then
    base="${auxfile%.aux}"
    echo "build-verify: bibtex ${base}"
    if ! bibtex "$base"; then
      echo "build-verify: FAIL — bibtex errored on ${base}." >&2
      [ -f "${base}.blg" ] && { echo "build-verify: ${base}.blg errors:" >&2; grep -iE 'error|i found no' "${base}.blg" >&2 || true; }
      exit 2
    fi
    ran_bibtex=1
  fi
done < <(find . -name '*.aux' -type f)
[ "$ran_bibtex" -eq 0 ] && echo "build-verify: (no \\citation found in any aux; skipping bibtex)"

# Passes 2 and 3: resolve refs/citations and settle cross-references.
run_pdflatex 2
run_pdflatex 3

# Confirm a PDF was actually produced.
if [ ! -f "${JOBNAME}.pdf" ]; then
  echo "build-verify: FAIL — no ${JOBNAME}.pdf produced." >&2
  exit 2
fi

# Scan the final log for warnings that pdflatex exits 0 on but we treat as fatal.
echo "build-verify: scanning ${LOG} for undefined refs / citations / duplicate labels..."
status=0

scan() {
  local phrase="$1" label="$2"
  if grep -F "$phrase" "$LOG" >/dev/null 2>&1; then
    echo "build-verify: FAIL — ${label}:" >&2
    grep -F "$phrase" "$LOG" >&2
    status=2
  fi
}

scan "There were undefined references" "undefined references present"
scan "There were multiply-defined labels" "multiply-defined labels present"
# Per-occurrence phrases (caught even if the summary line is absent).
scan "Reference \`" "undefined reference(s)"     # 'Reference `foo' on page ... undefined'
scan "Citation \`"  "undefined citation(s)"      # 'Citation `bar' on page ... undefined'

if [ "$status" -eq 0 ]; then
  echo "build-verify: PASS — ${JOBNAME}.pdf built clean."
fi
exit "$status"
