#!/usr/bin/env python3
"""Occlusion-aware keyframe extraction.

For any target timestamp where we want to extract a frame, `extract_best_keyframe`
first extracts a single sample at that exact time. If its variance score (a proxy
for 'is there chalk writing vs a smooth speaker silhouette') is above a threshold,
we accept it and return. Otherwise we expand search to a small neighborhood of
timestamps, extract each, score each, and replace the output with the best one.

The scoring heuristic is variance of grayscale pixel values in the central 40%
of the frame — chalk strokes on a dark board produce high-frequency texture
(high variance), while a smooth speaker silhouette produces low variance. This
was chosen as the starting heuristic; alternatives (center-minus-edge darkness,
color-histogram dissimilarity, lightweight person-detection) are noted as
candidates for a future revision.

Usage from other scripts:
    from occlusion import extract_best_keyframe
    extract_best_keyframe(video, t_target, output_png)
"""
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Optional

# Empirical starting value; tune based on real-data distribution (see rescue script).
DEFAULT_THRESHOLD = 800.0

# Seconds offset from target to try when the primary sample is below threshold.
DEFAULT_NEIGHBORHOOD = (-5.0, -3.0, -1.5, 1.5, 3.0, 5.0)

# Minimum factor by which a candidate must exceed the incumbent to replace it.
# Prevents swapping a clean-but-sparse board (low variance, e.g. a title board)
# for a marginally-more-textured but semantically-worse frame.
MIN_PROMOTION_FACTOR = 1.5


def extract_frame(video: Path, t: float, output: Path) -> bool:
    """Extract a single frame at timestamp t. Returns True on success."""
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(output)],
            check=True,
        )
        return output.exists() and output.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False


def _load_gray(png: Path):
    """Load as grayscale PIL image, or None on failure."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        return Image.open(png).convert("L")
    except Exception:
        return None


def variance_score(png: Path) -> float:
    """Variance of grayscale pixel values in the central 40%x40% region.

    Higher = more texture (chalk strokes on board) = likely un-occluded.
    Lower = smoother (speaker silhouette or empty region) = likely occluded.
    Returns -1.0 on image-load failure.
    """
    img = _load_gray(png)
    if img is None:
        return -1.0
    W, H = img.size
    x0, x1 = int(W * 0.3), int(W * 0.7)
    y0, y1 = int(H * 0.3), int(H * 0.7)
    crop = img.crop((x0, y0, x1, y1))
    if crop.width > 160:
        new_w = 160
        new_h = max(1, int(crop.height * 160 / crop.width))
        crop = crop.resize((new_w, new_h))
    pixels = list(crop.getdata())
    n = len(pixels)
    if n == 0:
        return 0.0
    mean = sum(pixels) / n
    return sum((p - mean) ** 2 for p in pixels) / n


def sharpness_score(png: Path) -> float:
    """Laplacian-like variance of the whole frame — a cheap blur detector.

    Higher = sharper (crisp chalk edges, focus well-locked).
    Lower = blurrier (motion blur during a camera pan, out-of-focus camera).
    Returns -1.0 on image-load failure.
    """
    try:
        from PIL import ImageFilter
    except ImportError:
        return -1.0
    img = _load_gray(png)
    if img is None:
        return -1.0
    if img.width > 480:
        new_w = 480
        new_h = max(1, int(img.height * 480 / img.width))
        img = img.resize((new_w, new_h))
    edges = img.filter(ImageFilter.FIND_EDGES)
    pixels = list(edges.getdata())
    n = len(pixels)
    if n == 0:
        return 0.0
    mean = sum(pixels) / n
    return sum((p - mean) ** 2 for p in pixels) / n


def extract_best_keyframe(
    video: Path, t_target: float, output: Path,
    threshold: float = DEFAULT_THRESHOLD,
    neighborhood: Iterable[float] = DEFAULT_NEIGHBORHOOD,
    duration: Optional[float] = None,
) -> tuple[float, float]:
    """Extract an occlusion-aware keyframe near t_target.

    Phase 1: sample at t_target. If score >= threshold, done.
    Phase 2: sample t_target + dt for each dt in neighborhood; keep the highest-
             scoring candidate. Candidates with t<0 or t>duration (if provided)
             are skipped.

    Returns (chosen_t, chosen_score).
    """
    if not extract_frame(video, t_target, output):
        return (t_target, -1.0)
    primary_var = variance_score(output)
    primary_sharp = sharpness_score(output)
    if primary_var >= threshold:
        return (t_target, primary_var)

    # Sharpness floor anchored to the PRIMARY. Comparing to current-best would
    # allow a staircase of progressively-blurrier swaps (A clears relative to
    # primary, B clears relative to A, etc). Anchoring to primary prevents
    # motion-blur pan frames from sneaking in no matter the swap order.
    sharp_floor = primary_sharp * 0.8 if primary_sharp > 0 else 0.0

    best_var = primary_var
    best_t = t_target
    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        for i, dt in enumerate(neighborhood):
            t_cand = t_target + dt
            if t_cand < 0:
                continue
            if duration is not None and t_cand > duration:
                continue
            tmp_path = tmp_dir / f"cand_{i}.png"
            if not extract_frame(video, t_cand, tmp_path):
                continue
            cand_var = variance_score(tmp_path)
            cand_sharp = sharpness_score(tmp_path)
            # Promotion requires:
            #   (1) meaningful variance improvement (not a marginal swap)
            #   (2) sharpness at least 80% of the ORIGINAL primary — rejects
            #       motion-blur pan frames and out-of-focus neighbors.
            var_ok = cand_var > max(best_var * MIN_PROMOTION_FACTOR,
                                    best_var + 50.0)
            sharp_ok = cand_sharp >= sharp_floor
            if var_ok and sharp_ok:
                best_var = cand_var
                best_t = t_cand
                shutil.copy(tmp_path, output)

    return (best_t, best_var)
