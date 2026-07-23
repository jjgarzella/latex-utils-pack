#!/usr/bin/env python3
"""Within-shot ink-density scan; add supplementary pre-erase key frames.

For each segment in segments.json, sample frames every 5 seconds, compute an ink-density
score (fraction of dark pixels on a grayscale frame), and detect substantive *drops* in
ink density (>25% decrease over a 10-second window). The frame just BEFORE each drop is
extracted as a supplementary key frame and appended to that segment's key_frames list.

Usage:
  detect_ink_keyframes.py --paper-dir DIR --input-video VIDEO \
      [--sample-interval-sec 5.0] [--drop-threshold 0.25] [--window-sec 10.0]

Depends on: PIL/Pillow (for ink density) and ffmpeg (for frame extraction).
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def extract_frame(video: Path, t: float, output: Path) -> bool:
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{t:.3f}", "-i", str(video), "-vf", "scale=320:-1",
             "-frames:v", "1", str(output)],
            check=True,
        )
        return output.exists() and output.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False


def ink_density(png: Path) -> float:
    """Fraction of pixels darker than threshold. 0.0 = all light, 1.0 = all dark."""
    try:
        from PIL import Image
    except ImportError:
        print("[detect_ink] PIL not installed; skipping ink detection "
              "(pip install pillow). Segments will have no supplementary key frames.",
              file=sys.stderr)
        return -1.0
    img = Image.open(png).convert("L")
    dark = 0
    total = 0
    for px in img.getdata():
        total += 1
        if px < 80:  # ink threshold on 0..255 grayscale
            dark += 1
    return dark / total if total else 0.0


def scan_segment(video: Path, seg: dict, keyframes_dir: Path,
                 sample_interval: float, drop_threshold: float,
                 window_sec: float, tmpdir: Path) -> list[str]:
    """Return list of supplementary key frame absolute paths added for this segment."""
    t0, t1 = seg["t_start"], seg["t_end"]
    if t1 - t0 < 2 * window_sec:
        return []  # too short to meaningfully sample

    samples: list[tuple[float, float]] = []  # (t, density)
    t = t0
    idx = 0
    while t <= t1:
        frame = tmpdir / f"scan_{seg['id']}_{idx:04d}.png"
        if extract_frame(video, t, frame):
            d = ink_density(frame)
            if d < 0:
                return []  # PIL missing
            samples.append((t, d))
            frame.unlink(missing_ok=True)
        t += sample_interval
        idx += 1

    # Detect drops: for each pair of samples window_sec apart, check if density dropped
    # by >= drop_threshold (absolute fraction drop or relative).
    supplementary_ts: list[float] = []
    step = max(1, int(round(window_sec / sample_interval)))
    for i in range(step, len(samples)):
        t_now, d_now = samples[i]
        t_prev, d_prev = samples[i - step]
        if d_prev > 0.02 and (d_prev - d_now) / d_prev >= drop_threshold:
            # Substantive decrease detected; grab the pre-drop frame
            pre_drop_t = t_prev
            # Suppress duplicates too close together
            if not supplementary_ts or pre_drop_t - supplementary_ts[-1] > window_sec:
                supplementary_ts.append(pre_drop_t)

    # For the final keyframe write (as opposed to density-sampling passes) we
    # use the occlusion-aware helper: if the pre-erase moment happens to have
    # the speaker in front of the board, a nearby frame is usually better.
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from occlusion import extract_best_keyframe
        _extract = extract_best_keyframe
    except ImportError:
        _extract = None

    added: list[str] = []
    for n, t_pre in enumerate(supplementary_ts, start=1):
        out = keyframes_dir / f"seg_{seg['id']}_preerase_{n}.png"
        if _extract is not None:
            _extract(video, t_pre, out)
            if out.exists() and out.stat().st_size > 0:
                added.append(str(out))
        else:
            if extract_frame(video, t_pre, out):
                added.append(str(out))
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-dir", required=True)
    ap.add_argument("--input-video", required=True)
    ap.add_argument("--sample-interval-sec", type=float, default=5.0)
    ap.add_argument("--drop-threshold", type=float, default=0.25)
    ap.add_argument("--window-sec", type=float, default=10.0)
    args = ap.parse_args()

    paper_dir = Path(args.paper_dir).resolve()
    video = Path(args.input_video).resolve()
    segments_path = paper_dir / "segments.json"
    if not segments_path.exists():
        print(f"[detect_ink] {segments_path} missing; run calibrate_segments.py first",
              file=sys.stderr)
        sys.exit(1)

    keyframes_dir = paper_dir / "keyframes"
    entries = json.loads(segments_path.read_text())
    total_added = 0
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        for seg in entries:
            added = scan_segment(
                video, seg, keyframes_dir,
                args.sample_interval_sec, args.drop_threshold, args.window_sec, tmpdir,
            )
            if added:
                seg["key_frames"].extend(added)
                total_added += len(added)
                print(f"[detect_ink] seg {seg['id']}: +{len(added)} supplementary keyframe(s)")

    segments_path.write_text(json.dumps(entries, indent=2))
    print(f"[detect_ink] done; added {total_added} supplementary keyframes total")


if __name__ == "__main__":
    main()
