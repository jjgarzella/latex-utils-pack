#!/usr/bin/env python3
"""Uniform-sample frames within each segment; dedup against existing keyframes via aHash.

For each segment, extract a frame every --interval-sec seconds (skipping the first and
last 15s to avoid overlap with the primary keyframe and transition blur). Compute a
64-bit average-hash (aHash) for each candidate and drop ones within --hamming-threshold
bits of any already-kept keyframe for that segment.

Usage:
  uniform_sample_keyframes.py --paper-dir DIR --input-video VIDEO \
      [--interval-sec 90] [--hamming-threshold 10]

Updates <paper_dir>/segments.json in place, appending new keyframe paths.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def ahash(png_path: Path) -> int:
    """64-bit average hash of a PNG."""
    from PIL import Image
    img = Image.open(png_path).convert("L").resize((8, 8))
    pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for i, p in enumerate(pixels):
        if p > avg:
            bits |= 1 << i
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def extract_frame(video: Path, t: float, output: Path) -> bool:
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-dir", required=True)
    ap.add_argument("--input-video", required=True)
    ap.add_argument("--interval-sec", type=float, default=90.0)
    ap.add_argument("--hamming-threshold", type=int, default=10,
                    help="Drop samples within this many bits of any existing keyframe.")
    ap.add_argument("--edge-skip-sec", type=float, default=15.0,
                    help="Skip this many seconds at start/end of each segment.")
    args = ap.parse_args()

    paper_dir = Path(args.paper_dir).resolve()
    video = Path(args.input_video).resolve()
    keyframes_dir = paper_dir / "keyframes"
    segments_path = paper_dir / "segments.json"
    segs = json.loads(segments_path.read_text())

    total_added = 0
    for seg in segs:
        t0, t1 = seg["t_start"], seg["t_end"]
        duration = t1 - t0

        # Hash existing keyframes (primary + supplementaries + any prior uniforms)
        existing_hashes = []
        for kf_path in seg["key_frames"]:
            try:
                existing_hashes.append(ahash(Path(kf_path)))
            except Exception:
                pass

        # Build sample times. Policy:
        #   - Long enough for interval sampling: uniform every `interval_sec` in the
        #     trimmed window.
        #   - Short segments: always sample at least the midpoint (if the segment is
        #     longer than ~10s — very brief shots offer little new info and their
        #     midpoint hashes similarly to the primary).
        # pHash dedup later drops near-duplicates so sampling is safe to over-request.
        sample_start = t0 + args.edge_skip_sec
        sample_end = t1 - args.edge_skip_sec

        sample_times: list[float] = []
        if duration >= args.interval_sec + 2 * args.edge_skip_sec:
            # Regular interval sampling
            n_samples = int((sample_end - sample_start) / args.interval_sec)
            sample_times = [
                sample_start + i * args.interval_sec
                for i in range(1, n_samples + 1)
                if sample_start + i * args.interval_sec < sample_end
            ]
        elif duration >= 10.0:
            # Short segment: at least the midpoint as a fallback against
            # speaker-occluded primary keyframes.
            sample_times = [t0 + duration / 2.0]
        # else: truly tiny segment, skip.

        if not sample_times:
            continue

        new_kfs: list[str] = []
        for t_sample in sample_times:
            sample_path = keyframes_dir / f"seg_{seg['id']}_t{int(t_sample)}.png"

            # Idempotent: if already extracted AND in segments.json, skip.
            if str(sample_path) in seg["key_frames"]:
                continue
            if sample_path.exists():
                # File already on disk but not listed: hash and dedup conservatively.
                try:
                    h = ahash(sample_path)
                except Exception:
                    continue
            else:
                # Use occlusion-aware extraction so if the target moment happens
                # to catch the speaker in frame, we fall back to a nearby clean
                # frame automatically.
                try:
                    import sys as _sys
                    _sys.path.insert(0, str(Path(__file__).parent))
                    from occlusion import extract_best_keyframe
                    extract_best_keyframe(video, t_sample, sample_path)
                    if not (sample_path.exists() and sample_path.stat().st_size > 0):
                        continue
                except ImportError:
                    if not extract_frame(video, t_sample, sample_path):
                        continue
                try:
                    h = ahash(sample_path)
                except Exception:
                    continue

            # Dedup against existing + already-kept-this-segment samples
            is_dup = any(hamming(h, eh) <= args.hamming_threshold
                         for eh in existing_hashes)
            if is_dup:
                sample_path.unlink(missing_ok=True)
                continue
            new_kfs.append(str(sample_path))
            existing_hashes.append(h)
            total_added += 1

        if new_kfs:
            seg["key_frames"].extend(new_kfs)
            # Sort by embedded timestamp in filename for readability
            def kf_sort_key(p: str) -> tuple:
                name = Path(p).name
                if "_main" in name:
                    return (0,)
                if "_preerase_" in name:
                    n = int(name.split("_preerase_")[1].split(".")[0])
                    return (1, n)
                if "_t" in name:
                    t = int(name.split("_t")[1].split(".")[0])
                    return (2, t)
                return (3,)
            seg["key_frames"].sort(key=kf_sort_key)
            print(f"[uniform] seg {seg['id']}: +{len(new_kfs)} samples (total {len(seg['key_frames'])})")

    segments_path.write_text(json.dumps(segs, indent=2))
    print(f"[uniform] done; added {total_added} keyframes total")


if __name__ == "__main__":
    main()
