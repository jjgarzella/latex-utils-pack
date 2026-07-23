#!/usr/bin/env python3
"""Transcribe a single audio clip into a segment_<id>.transcript.json record.

Usage:
  transcribe.py --paper-dir DIR --segment-id ID --audio WAV --engine ENGINE

Engines:
  faster-whisper  (default; pip install faster-whisper; large-v3 model)
  whisper-cpp     (NOT IMPLEMENTED in v1; falls through with error)
  openai-api      (NOT IMPLEMENTED in v1; falls through with error)
"""
import argparse
import json
import sys
from pathlib import Path


def transcribe_faster_whisper(audio: Path) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("[transcribe] faster-whisper not installed. "
              "Run: pip install faster-whisper", file=sys.stderr)
        sys.exit(1)

    # Prefer Metal/CUDA if available via ctranslate2's autodetection; fall back to CPU.
    model = WhisperModel("large-v3", device="auto", compute_type="auto")
    segments_iter, info = model.transcribe(
        str(audio),
        beam_size=5,
        vad_filter=True,  # cuts silences; nice for long lectures
        language="en",    # IHES lectures: English; change if we add a lang var
    )
    segments = []
    full_text_parts = []
    for s in segments_iter:
        segments.append({
            "start": round(s.start, 3),
            "end": round(s.end, 3),
            "text": s.text.strip(),
        })
        full_text_parts.append(s.text.strip())
    return {
        "engine": "faster-whisper",
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "duration": round(info.duration, 3),
        "text": " ".join(full_text_parts),
        "segments": segments,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-dir", required=True)
    ap.add_argument("--segment-id", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--engine", default="faster-whisper")
    args = ap.parse_args()

    paper_dir = Path(args.paper_dir).resolve()
    audio = Path(args.audio).resolve()
    seg_id = args.segment_id

    if not audio.exists():
        print(f"[transcribe] audio file missing: {audio}", file=sys.stderr)
        sys.exit(1)

    if args.engine == "faster-whisper":
        result = transcribe_faster_whisper(audio)
    else:
        print(f"[transcribe] engine {args.engine!r} not implemented in v1", file=sys.stderr)
        sys.exit(2)

    result["segment_id"] = seg_id
    out = paper_dir / "pass1" / f"segment_{seg_id}.transcript.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(f"[transcribe] wrote {out} ({len(result['segments'])} sub-segments, "
          f"{len(result['text'])} chars)")


if __name__ == "__main__":
    main()
