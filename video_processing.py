"""Freeze-detection and frame-extraction primitives, shared by the grab_states.py
CLI and the local backend's API. Pulled out so there's one source of truth for
the ffmpeg calls instead of duplicating them between the two entry points.
"""

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

# How far before freeze_end to grab the frame. Freezedetect reports
# freeze_end once the freeze has already broken, so stepping back a bit
# guarantees we're still inside the settled frame rather than the start
# of the next transition.
EXTRACTION_OFFSET_SECONDS = 0.2

FREEZE_START_RE = re.compile(r"freeze_start:\s*([0-9.]+)")
FREEZE_END_RE = re.compile(r"freeze_end:\s*([0-9.]+)")


def _freezedetect_filters(crop_top: int, noise: float, duration: float,
                          scan_fps: Optional[int], scan_width: Optional[int]) -> str:
    filters = []
    if crop_top > 0:
        # crop=width:height:x:y — keep full width, drop crop_top rows from the top.
        filters.append(f"crop=iw:ih-{crop_top}:0:{crop_top}")
    # Spotting still moments doesn't need every frame or full resolution, so the
    # scan can look at a smaller, lower frame-rate copy. On a 4K 120fps recording
    # this finds the same freezes (to within 1/scan_fps seconds) about a third
    # faster. The final screenshots still come from the original, full quality.
    if scan_fps:
        filters.append(f"fps={scan_fps}")
    if scan_width:
        filters.append(f"scale='min({scan_width},iw)':-2")
    filters.append(f"freezedetect=n={noise}:d={duration}")
    return ",".join(filters)


def video_duration(video_path: Path) -> Optional[float]:
    """Length of the video in seconds, or None if ffprobe can't tell."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def run_freezedetect_with_progress(
    video_path: Path,
    crop_top: int,
    noise: float,
    duration: float,
    on_progress: Callable[[float], None],
    scan_fps: Optional[int] = 10,
    scan_width: Optional[int] = 1280,
) -> str:
    """Like run_freezedetect, but calls on_progress(fraction 0-1) as the scan
    moves through the video, and scans a smaller copy by default."""
    total = video_duration(video_path)
    cmd = [
        "ffmpeg", "-nostdin", "-i", str(video_path),
        "-vf", _freezedetect_filters(crop_top, noise, duration, scan_fps, scan_width),
        "-an", "-f", "null", "-",
        # Machine-readable progress ("out_time_us=...") goes to stdout.
        "-progress", "pipe:1", "-nostats",
    ]
    # The freezedetect findings go to stderr; send them to a file so a long
    # video can't fill up the pipe while we're reading progress from stdout.
    with tempfile.TemporaryFile(mode="w+") as log:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True)
        assert process.stdout is not None
        for line in process.stdout:
            if total and line.startswith("out_time_us="):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                on_progress(max(0.0, min(1.0, seconds / total)))
        process.wait()
        log.seek(0)
        return log.read()


def run_freezedetect(video_path: Path, crop_top: int, noise: float, duration: float) -> str:
    """Run ffmpeg with the freezedetect filter and return its stderr output.

    freezedetect writes its findings to stderr as log lines, e.g.:
        [Parsed_freezedetect_1 @ 0x...] freeze_start: 12.345
        [Parsed_freezedetect_1 @ 0x...] freeze_duration: 3.21
        [Parsed_freezedetect_1 @ 0x...] freeze_end: 15.555
    We don't need an actual output file, so we discard the encoded frames
    with `-f null -`.
    """
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", _freezedetect_filters(crop_top, noise, duration, None, None),
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

    -ss is placed BEFORE -i, so ffmpeg jumps to the nearest keyframe and then
    decodes forward to the exact timestamp. That's still frame-accurate (the
    result is pixel-identical to putting -ss after -i), but it doesn't decode
    the whole video up to that point: on a 4K 120fps recording, a frame at
    5 minutes takes ~0.5s instead of ~19s.
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{timestamp:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        str(output_path),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
