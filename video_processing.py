"""Freeze-detection and frame-extraction primitives, shared by the grab_states.py
CLI and the local backend's API. Pulled out so there's one source of truth for
the ffmpeg calls instead of duplicating them between the two entry points.
"""

import re
import subprocess
from pathlib import Path

# How far before freeze_end to grab the frame. Freezedetect reports
# freeze_end once the freeze has already broken, so stepping back a bit
# guarantees we're still inside the settled frame rather than the start
# of the next transition.
EXTRACTION_OFFSET_SECONDS = 0.2

FREEZE_START_RE = re.compile(r"freeze_start:\s*([0-9.]+)")
FREEZE_END_RE = re.compile(r"freeze_end:\s*([0-9.]+)")


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
