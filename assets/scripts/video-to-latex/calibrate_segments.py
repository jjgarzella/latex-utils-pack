#!/usr/bin/env python3
"""Auto-tune ffmpeg scene-change threshold and emit segments.json + primary key frames.

Usage:
  calibrate_segments.py --paper-dir DIR --input-video VIDEO \
      [--scene-threshold auto|<float>] [--intro-trim-sec 30] [--outro-trim-sec 0] \
      [--max-segment-sec 900] [--min-segments 15] [--max-segments 40]

Outputs:
  <paper_dir>/segments.json        list of {id, t_start, t_end, key_frames: [abs-paths]}
  <paper_dir>/keyframes/seg_<id>_main.png    primary key frame per segment
  (supplementary pre-erase key frames added by detect_ink_keyframes.py)
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SWEEP_THRESHOLDS = [0.20, 0.25, 0.30, 0.35]
SCENE_RE = re.compile(r"pts_time:(\d+(?:\.\d+)?)")
DURATION_RE = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")


def video_duration_sec(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def detect_scene_changes(video: Path, threshold: float) -> list[float]:
    """Run ffmpeg scene detection; return sorted list of scene-change timestamps (sec)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
         "-vf", f"scale=480:-1,fps=2,select='gt(scene,{threshold})',metadata=print:file=-",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    # metadata=print:file=- writes to stdout
    lines = proc.stdout.splitlines() + proc.stderr.splitlines()
    times = []
    for line in lines:
        m = SCENE_RE.search(line)
        if m:
            times.append(float(m.group(1)))
    return sorted(set(times))


def merge_short_segments(
    segments: list[tuple[float, float]], min_segment_sec: float,
) -> list[tuple[float, float]]:
    """Merge any segment shorter than min_segment_sec into an adjacent one.

    First segment → merged forward; last segment → merged backward; interior short
    segments → merged forward (arbitrary convention). Preserves total time coverage.
    """
    result = list(segments)
    i = 0
    while i < len(result):
        t0, t1 = result[i]
        if t1 - t0 < min_segment_sec and len(result) > 1:
            if i == len(result) - 1:
                result[i - 1] = (result[i - 1][0], t1)
            else:
                result[i + 1] = (t0, result[i + 1][1])
            result.pop(i)
            continue
        i += 1
    return result


def build_segments(
    scene_times: list[float], duration: float,
    intro_trim: float, outro_trim: float, max_segment_sec: float,
    min_segment_sec: float = 10.0,
) -> list[tuple[float, float]]:
    """Collapse scene times into segment bounds. Returns list of (t_start, t_end)."""
    # Usable range
    start = intro_trim
    end = duration - outro_trim
    if end <= start:
        raise ValueError(f"intro_trim+outro_trim leaves no usable video "
                         f"(duration={duration}, start={start}, end={end})")
    # Filter to trimmed window; treat times ≤ start as "skip".
    cuts = [t for t in scene_times if start < t < end]
    # Build segments: start, cut1, cut2, ..., end
    boundaries = [start] + cuts + [end]
    segments: list[tuple[float, float]] = []
    for i in range(len(boundaries) - 1):
        segments.append((boundaries[i], boundaries[i + 1]))
    # Merge short boundary artifacts (tiny slivers next to intro/outro trims,
    # or close-together scene events).
    segments = merge_short_segments(segments, min_segment_sec)
    # Enforce max_segment_sec: force-split long segments
    final = []
    for t0, t1 in segments:
        if t1 - t0 <= max_segment_sec:
            final.append((t0, t1))
            continue
        # Force-split into equal-ish chunks of <= max_segment_sec
        n_splits = int((t1 - t0) // max_segment_sec) + 1
        chunk = (t1 - t0) / n_splits
        for j in range(n_splits):
            final.append((t0 + j * chunk, t0 + (j + 1) * chunk))
    return final


def evaluate_threshold(
    scene_times: list[float], duration: float,
    intro_trim: float, outro_trim: float, max_segment_sec: float,
    min_segments: int, max_segments: int,
) -> tuple[bool, int, float]:
    """Return (satisfies, count, max_shot_duration)."""
    segs = build_segments(scene_times, duration, intro_trim, outro_trim, max_segment_sec)
    count = len(segs)
    max_dur = max((t1 - t0) for t0, t1 in segs) if segs else 0.0
    satisfies = (min_segments <= count <= max_segments) and (max_dur <= max_segment_sec + 1e-3)
    return satisfies, count, max_dur


def extract_keyframe(video: Path, t: float, output: Path,
                     duration: float | None = None) -> None:
    """Extract an occlusion-aware keyframe near timestamp t to output PNG.

    Delegates to occlusion.extract_best_keyframe so that if the frame at t looks
    occluded (low variance = smooth speaker silhouette), we sample a small
    neighborhood and pick the best frame. Falls back to a plain ffmpeg extract
    if the occlusion helper is unavailable.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from occlusion import extract_best_keyframe
        extract_best_keyframe(video, t, output, duration=duration)
    except ImportError:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(output)],
            check=True,
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-dir", required=True)
    ap.add_argument("--input-video", required=True)
    ap.add_argument("--scene-threshold", default="auto")
    ap.add_argument("--intro-trim-sec", type=float, default=30.0)
    ap.add_argument("--outro-trim-sec", type=float, default=0.0)
    ap.add_argument("--max-segment-sec", type=float, default=900.0)
    ap.add_argument("--min-segment-sec", type=float, default=10.0,
                    help="Segments shorter than this are merged into a neighbor.")
    ap.add_argument("--min-segments", type=int, default=15)
    ap.add_argument("--max-segments", type=int, default=40)
    args = ap.parse_args()

    paper_dir = Path(args.paper_dir).resolve()
    video = Path(args.input_video).resolve()
    keyframes_dir = paper_dir / "keyframes"
    keyframes_dir.mkdir(parents=True, exist_ok=True)

    duration = video_duration_sec(video)
    print(f"[calibrate] video duration: {duration:.1f}s")

    # Sweep or use fixed threshold
    if args.scene_threshold == "auto":
        chosen = None
        for thr in SWEEP_THRESHOLDS:
            print(f"[calibrate] trying threshold={thr}")
            scene_times = detect_scene_changes(video, thr)
            ok, count, max_dur = evaluate_threshold(
                scene_times, duration, args.intro_trim_sec, args.outro_trim_sec,
                args.max_segment_sec, args.min_segments, args.max_segments,
            )
            print(f"[calibrate]   → {count} segments, max shot {max_dur:.1f}s, "
                  f"satisfies={ok}")
            if ok:
                chosen = (thr, scene_times, count, max_dur)
                break
        if chosen is None:
            # Take the one closest to mid-range
            print("[calibrate] no threshold satisfied constraints; falling back to 0.25")
            thr = 0.25
            scene_times = detect_scene_changes(video, thr)
            _, count, max_dur = evaluate_threshold(
                scene_times, duration, args.intro_trim_sec, args.outro_trim_sec,
                args.max_segment_sec, args.min_segments, args.max_segments,
            )
            chosen = (thr, scene_times, count, max_dur)
        threshold, scene_times, count, max_dur = chosen
    else:
        threshold = float(args.scene_threshold)
        scene_times = detect_scene_changes(video, threshold)
        _, count, max_dur = evaluate_threshold(
            scene_times, duration, args.intro_trim_sec, args.outro_trim_sec,
            args.max_segment_sec, args.min_segments, args.max_segments,
        )

    print(f"[calibrate] chosen threshold={threshold}, segments={count}, "
          f"max shot {max_dur:.1f}s")

    segs = build_segments(
        scene_times, duration, args.intro_trim_sec, args.outro_trim_sec,
        args.max_segment_sec, args.min_segment_sec,
    )

    # Build segments.json and extract primary keyframes
    entries = []
    for i, (t0, t1) in enumerate(segs, start=1):
        seg_id = f"{i:03d}"
        # Primary keyframe: 1 second before segment end (last stable frame).
        kf_t = max(t0, t1 - 1.0)
        main_path = keyframes_dir / f"seg_{seg_id}_main.png"
        try:
            extract_keyframe(video, kf_t, main_path, duration=duration)
        except subprocess.CalledProcessError as e:
            print(f"[calibrate] warning: keyframe extraction failed for seg {seg_id}: {e}")
            continue
        entries.append({
            "id": seg_id,
            "t_start": round(t0, 3),
            "t_end": round(t1, 3),
            "key_frames": [str(main_path)],
        })

    # Persist entries so the downstream helpers can read them
    (paper_dir / "segments.json").write_text(json.dumps(entries, indent=2))

    # Run supplementary key frame detection (ink-erasure-driven)
    detect_script = Path.home() / ".beads/formulas/video-to-latex/detect_ink_keyframes.py"
    if detect_script.exists():
        print("[calibrate] running detect_ink_keyframes.py for supplementary keyframes")
        subprocess.run(
            ["python3", str(detect_script),
             "--paper-dir", str(paper_dir),
             "--input-video", str(video)],
            check=False,  # supplementary key frames are nice-to-have
        )

    # Run uniform time-sampling with pHash dedup (catches content missed by the primary
    # last-stable-frame keyframe, e.g. scrolled-away content, content visible only early
    # in a long shot, and occlusion-balanced by giving the VLM multiple views).
    uniform_script = Path.home() / ".beads/formulas/video-to-latex/uniform_sample_keyframes.py"
    if uniform_script.exists():
        print("[calibrate] running uniform_sample_keyframes.py for in-shot samples")
        subprocess.run(
            ["python3", str(uniform_script),
             "--paper-dir", str(paper_dir),
             "--input-video", str(video)],
            check=False,
        )

    # Reload entries to reflect any additions from helpers
    entries = json.loads((paper_dir / "segments.json").read_text())
    (paper_dir / "segments.json").write_text(json.dumps(entries, indent=2))
    print(f"[calibrate] wrote {paper_dir/'segments.json'} with {len(entries)} segments")
    print(f"[calibrate] keyframes in {keyframes_dir}")


if __name__ == "__main__":
    main()
