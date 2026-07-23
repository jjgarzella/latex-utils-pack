#!/usr/bin/env python3
"""Merge per-segment transcript.json + board.tex into segment_<id>.json records.

Produces the unified per-segment artifact that both build_transcript.py and the
polish pass read. Non-LLM — mechanical merge; section_hint and brief_summary
are left empty here and can be filled in later by an LLM step if desired
(polish can operate without them).

Usage:
  merge_records.py --paper-dir DIR
"""
import argparse
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-dir", required=True)
    args = ap.parse_args()

    paper_dir = Path(args.paper_dir).resolve()
    pass1 = paper_dir / "pass1"

    segments_list = json.loads((paper_dir / "segments.json").read_text())
    segments_by_id = {s["id"]: s for s in segments_list}

    written = 0
    skipped = 0
    for sid, seg_meta in sorted(segments_by_id.items()):
        transcript_path = pass1 / f"segment_{sid}.transcript.json"
        board_path = pass1 / f"segment_{sid}.board.tex"
        out_path = pass1 / f"segment_{sid}.json"

        if not transcript_path.exists():
            print(f"[merge] skipping seg {sid}: no transcript.json")
            skipped += 1
            continue

        transcript = json.loads(transcript_path.read_text())
        board_tex = board_path.read_text() if board_path.exists() else ""

        record = {
            "segment_id": sid,
            "t_start": seg_meta["t_start"],
            "t_end": seg_meta["t_end"],
            "key_frames": seg_meta["key_frames"],
            "audio_transcript": transcript.get("text", ""),
            "audio_segments": transcript.get("segments", []),
            "language": transcript.get("language", ""),
            "engine": transcript.get("engine", ""),
            "board_tex": board_tex,
            "section_hint": "",
            "brief_summary": "",
        }
        out_path.write_text(json.dumps(record, indent=2))
        written += 1

    print(f"[merge] wrote {written} records, skipped {skipped}")


if __name__ == "__main__":
    main()
