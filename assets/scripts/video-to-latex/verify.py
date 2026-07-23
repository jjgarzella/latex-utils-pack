#!/usr/bin/env python3
"""Verify the video-to-latex output: compile, coverage, notation consistency, board coverage.

Usage:
  verify.py --paper-dir DIR

Exit codes:
  0  — all gates pass
  2  — soft failures only (hard gate passes, some soft warnings)
  1  — hard gate fails (no PDF)
"""
import argparse
import json
import re
import sys
from pathlib import Path


def gate_compile(paper_dir: Path) -> tuple[bool, str]:
    pdf = paper_dir / "build" / "paper.pdf"
    if not pdf.exists():
        return False, f"HARD FAIL: {pdf} missing"
    if pdf.stat().st_size < 1024:
        return False, f"HARD FAIL: {pdf} is suspiciously small ({pdf.stat().st_size} bytes)"
    return True, f"OK: {pdf} ({pdf.stat().st_size} bytes)"


def gate_coverage(paper_dir: Path, window_sec: float = 60.0) -> tuple[bool, list[str]]:
    """Every window_sec window of the video should have ≥1 sentence of transcript."""
    warnings = []
    pass1 = paper_dir / "pass1"
    records = []
    for p in sorted(pass1.glob("segment_*.json")):
        try:
            records.append(json.loads(p.read_text()))
        except Exception:
            pass
    if not records:
        return False, ["no pass1 records found"]

    # Build a list of (t_start, t_end, sentence_count). Use the count of
    # whisper sub-segments (audio_segments) as a proxy for sentence count —
    # each whisper sub is roughly an utterance, and re-splitting on [.!?]
    # underdetects because whisper often omits terminal punctuation.
    covered = []
    for rec in records:
        t0 = rec.get("t_start", 0.0)
        t1 = rec.get("t_end", 0.0)
        sentences = len(rec.get("audio_segments", []) or [])
        covered.append((t0, t1, sentences))

    # Check each 60s window from 0 to max t_end
    t_max = max(t1 for _, t1, _ in covered)
    t = 0.0
    while t < t_max:
        window_end = t + window_sec
        overlapping = [(t0, t1, sc) for (t0, t1, sc) in covered
                       if t0 < window_end and t1 > t]
        # Attribute sentences pro-rata by overlap fraction
        score = 0.0
        for t0, t1, sc in overlapping:
            overlap = max(0.0, min(t1, window_end) - max(t0, t))
            if t1 > t0:
                score += sc * (overlap / (t1 - t0))
        if score < 1.0:
            warnings.append(f"SOFT: window {int(t)}s–{int(window_end)}s has <1 sentence "
                            f"(score={score:.2f})")
        t += window_sec
    return True, warnings


def gate_notation_consistency(paper_dir: Path) -> tuple[bool, list[str]]:
    """Heuristic scan of body.tex for likely-inconsistent macros (same base, different forms)."""
    warnings = []
    body = paper_dir / "body.tex"
    if not body.exists():
        return False, [f"body.tex missing at {body}"]
    text = body.read_text()
    # Collect simple math macros like \mathcal{X}, \mathbb{X}, \mathrm{X}, \X
    macro_re = re.compile(r"\\(math(?:cal|bb|bf|frak|sf|rm))\{([A-Za-z])\}")
    uses = {}  # letter -> set of full LaTeX strings
    for m in macro_re.finditer(text):
        letter = m.group(2)
        uses.setdefault(letter, set()).add(m.group(0))
    for letter, forms in uses.items():
        if len(forms) > 1:
            warnings.append(f"SOFT: letter {letter} used with multiple font styles: "
                            + ", ".join(sorted(forms)))
    return True, warnings


def gate_board_readability(
    paper_dir: Path,
    illegible_ratio_threshold: float = 0.5,
    min_illegible_lines: int = 2,
) -> tuple[bool, list[str]]:
    """Flag segments whose board OCR is mostly '% ILLEGIBLE' markers.

    Signals a seg-030-style failure where every keyframe was occluded/erased/
    off-frame, leaving polish to work from audio alone. Surfaces the segment so
    a human can supply replacement keyframes before (re-)running polish.
    """
    warnings = []
    pass1 = paper_dir / "pass1"
    illegible_re = re.compile(r"^\s*%\s*ILLEGIBLE\b", re.IGNORECASE)
    for p in sorted(pass1.glob("segment_*.json")):
        try:
            rec = json.loads(p.read_text())
        except Exception:
            continue
        board = (rec.get("board_tex") or "").strip()
        if not board or board.startswith("% empty board"):
            continue
        illegible = 0
        content = 0
        for line in board.splitlines():
            s = line.strip()
            if not s:
                continue
            if illegible_re.match(s):
                illegible += 1
            elif not s.startswith("%"):
                content += 1
        total = illegible + content
        if total == 0 or illegible < min_illegible_lines:
            continue
        ratio = illegible / total
        if ratio >= illegible_ratio_threshold:
            kf = len(rec.get("key_frames", []) or [])
            warnings.append(
                f"SOFT: segment {rec.get('segment_id', '?')} board mostly "
                f"unreadable ({illegible}/{total} lines ILLEGIBLE, "
                f"keyframes: {kf}) — consider manual replacement keyframes"
            )
    return True, warnings


_IDENT_RE = re.compile(
    r"\\(?:mathcal|mathbb|mathrm|mathbf|mathfrak|mathsf|operatorname)\{([A-Za-z][A-Za-z0-9]*)\}"
)


def _board_identifiers(text: str) -> set[str]:
    """Extract the set of font-wrapped math identifiers from a TeX string."""
    return {f"{m.group(0)}" for m in _IDENT_RE.finditer(text)}


def gate_board_coverage(paper_dir: Path, min_overlap: float = 0.30) -> tuple[bool, list[str]]:
    """Per-segment board OCR identifiers should mostly reappear in body.tex.

    The polish step rewrites board OCR into prose + LaTeX environments, so a
    literal substring match is defeated by reformatting. Instead we extract the
    set of font-wrapped math identifiers (\\mathcal{X}, \\operatorname{Foo}, …)
    from each segment's board.tex and check what fraction appear somewhere in
    body.tex. Warn only if the overlap drops below `min_overlap`.
    """
    warnings = []
    body = paper_dir / "body.tex"
    if not body.exists():
        return False, [f"body.tex missing at {body}"]
    body_idents = _board_identifiers(body.read_text())
    pass1 = paper_dir / "pass1"
    for p in sorted(pass1.glob("segment_*.json")):
        try:
            rec = json.loads(p.read_text())
        except Exception:
            continue
        board = (rec.get("board_tex") or "").strip()
        if not board or board.startswith("% empty board"):
            continue
        board_idents = _board_identifiers(board)
        if len(board_idents) < 3:
            continue  # not enough signal to judge
        present = board_idents & body_idents
        ratio = len(present) / len(board_idents)
        if ratio < min_overlap:
            missing = sorted(board_idents - body_idents)[:5]
            warnings.append(
                f"SOFT: segment {rec.get('segment_id','?')} board identifiers "
                f"mostly absent from body.tex ({len(present)}/{len(board_idents)} "
                f"= {ratio:.0%} match; missing e.g. {missing})"
            )
    return True, warnings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-dir", required=True)
    args = ap.parse_args()
    paper_dir = Path(args.paper_dir).resolve()

    hard_ok, hard_msg = gate_compile(paper_dir)
    print(hard_msg)
    if not hard_ok:
        sys.exit(1)

    soft_warnings: list[str] = []
    for name, fn in [
        ("coverage", gate_coverage),
        ("notation consistency", gate_notation_consistency),
        ("board readability", gate_board_readability),
        ("board coverage", gate_board_coverage),
    ]:
        ok, warns = fn(paper_dir)
        if not ok:
            print(f"{name}: GATE ERROR: {warns}")
            sys.exit(1)
        if warns:
            print(f"{name}: {len(warns)} warning(s)")
            for w in warns[:20]:
                print(f"  {w}")
            if len(warns) > 20:
                print(f"  ... and {len(warns) - 20} more")
            soft_warnings.extend(warns)
        else:
            print(f"{name}: OK")

    if soft_warnings:
        print(f"\nTotal soft warnings: {len(soft_warnings)}")
        sys.exit(2)
    print("\nAll gates passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
