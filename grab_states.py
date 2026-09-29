#!/usr/bin/env python3
"""
grab_states.py — detect "settled" (frozen) stretches in a screen-recording
and pull one full-res screenshot out of each, plus an HTML contact sheet
to eyeball the results.

Pipeline:
    1. Run ffmpeg's freezedetect filter over the video and parse
       freeze_start / freeze_end pairs from its stderr output.
    2. For each freeze, grab a frame a bit before freeze_end (so we land
       solidly inside the settled state, not on the edge of a transition).
    3. Write out 01.png, 02.png, ... plus an index.html contact sheet.

Usage:
    python grab_states.py flow.mp4
    python grab_states.py flow.mp4 --crop 80 --noise 0.005 --duration 0.75
"""

import argparse
import sys
from pathlib import Path

from video_processing import (
    EXTRACTION_OFFSET_SECONDS,
    extract_frame,
    parse_freezes,
    run_freezedetect,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Detect freeze points in a video and extract one screenshot per freeze."
    )
    parser.add_argument("video", type=Path, help="Path to the input video file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output folder (default: <video-name>_screens next to the video)",
    )
    parser.add_argument(
        "--crop", type=int, default=0,
        help="Pixels to crop off the top before detection, e.g. to ignore a phone status bar (default: 0)",
    )
    parser.add_argument(
        "--noise", type=float, default=0.003,
        help="freezedetect noise tolerance 'n' — higher is more tolerant of small pixel changes (default: 0.003)",
    )
    parser.add_argument(
        "--duration", type=float, default=0.5,
        help="freezedetect minimum still duration 'd' in seconds before a freeze counts (default: 0.5)",
    )
    return parser.parse_args()


def write_contact_sheet(output_dir: Path, shots: list[tuple[str, float]]):
    """Write a standalone index.html thumbnail grid, no server required."""
    rows = "\n".join(
        f'''    <figure>
      <img src="{filename}" loading="lazy">
      <figcaption>{filename} — {timestamp:.2f}s</figcaption>
    </figure>'''
        for filename, timestamp in shots
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Contact sheet — {len(shots)} frames</title>
<style>
  body {{ font-family: sans-serif; background: #222; color: #eee; margin: 0; padding: 20px; }}
  h1 {{ font-weight: normal; font-size: 16px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }}
  figure {{ margin: 0; background: #333; border-radius: 6px; overflow: hidden; }}
  img {{ width: 100%; display: block; }}
  figcaption {{ padding: 6px 8px; font-size: 12px; font-family: monospace; color: #aaa; }}
</style>
</head>
<body>
<h1>{len(shots)} frames extracted</h1>
<div class="grid">
{rows}
</div>
</body>
</html>
"""
    (output_dir / "index.html").write_text(html)


def main():
    args = parse_args()

    if not args.video.exists():
        sys.exit(f"Video not found: {args.video}")

    output_dir = args.output or args.video.parent / f"{args.video.stem}_screens"
    output_dir.mkdir(parents=True, exist_ok=True)

    stderr = run_freezedetect(args.video, args.crop, args.noise, args.duration)
    freezes = parse_freezes(stderr)

    shots = []
    for i, (freeze_start, freeze_end) in enumerate(freezes, start=1):
        timestamp = max(freeze_start, freeze_end - EXTRACTION_OFFSET_SECONDS)
        filename = f"{i:02d}.png"
        extract_frame(args.video, timestamp, output_dir / filename)
        shots.append((filename, timestamp))
        print(f"  [{i:02d}] freeze {freeze_start:.2f}s -> {freeze_end:.2f}s, grabbed frame at {timestamp:.2f}s")

    write_contact_sheet(output_dir, shots)

    print()
    print(f"Freezes found: {len(freezes)}")
    print(f"Output folder: {output_dir.resolve()}")
    print(f"Contact sheet: {(output_dir / 'index.html').resolve()}")


if __name__ == "__main__":
    main()
