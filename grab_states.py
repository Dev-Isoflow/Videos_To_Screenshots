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
import re
import subprocess
import sys
from pathlib import Path

# How far before freeze_end to grab the frame. Freezedetect reports
# freeze_end once the freeze has already broken, so stepping back a bit
# guarantees we're still inside the settled frame rather than the start
# of the next transition.
EXTRACTION_OFFSET_SECONDS = 0.2

FREEZE_START_RE = re.compile(r"freeze_start:\s*([0-9.]+)")
FREEZE_END_RE = re.compile(r"freeze_end:\s*([0-9.]+)")


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


def run_freezedetect(video_path: Path, crop_top: int, noise: float, duration: float) -> str:
    """Run ffmpeg with the freezedetect filter and return its stderr output.

    freezedetect writes its findings to stderr as log lines, e.g.:
        [Parsed_freezedetect_1 @ 0x...] freeze_start: 12.345
        [Parsed_freezedetect_1 @ 0x...] freeze_duration: 3.21
        [Parsed_freezedetect_1 @ 0x...] freeze_end: 15.555
    We don't need an actual output file, so we discard the encoded frames
    with `-f null -`.
    """
    filters = []
    if crop_top > 0:
        # crop=width:height:x:y — keep full width, drop crop_top rows from the top.
        filters.append(f"crop=iw:ih-{crop_top}:0:{crop_top}")
    filters.append(f"freezedetect=n={noise}:d={duration}")

    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", ",".join(filters),
        "-an", "-f", "null", "-",
    ]
    print(f"Running: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stderr


def parse_freezes(ffmpeg_stderr: str) -> list[tuple[float, float]]:
    """Pull (freeze_start, freeze_end) pairs out of ffmpeg's log output.

    freezedetect logs freeze_start as soon as a still stretch begins, and
    freeze_end only once it's over, so the two are matched up in order:
    the Nth freeze_end closes the Nth freeze_start. A trailing freeze_start
    with no matching freeze_end (freeze runs to end of file) is dropped —
    we only extract from freezes that actually completed.
    """
    starts = [float(m.group(1)) for m in FREEZE_START_RE.finditer(ffmpeg_stderr)]
    ends = [float(m.group(1)) for m in FREEZE_END_RE.finditer(ffmpeg_stderr)]
    return list(zip(starts, ends))


def extract_frame(video_path: Path, timestamp: float, output_path: Path):
    """Grab a single full-resolution frame at `timestamp` seconds as a PNG.

    -ss is placed after -i so ffmpeg decodes and seeks precisely to the
    timestamp (frame-accurate) rather than jumping to the nearest keyframe.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-ss", f"{timestamp:.3f}",
        "-frames:v", "1",
        str(output_path),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


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
