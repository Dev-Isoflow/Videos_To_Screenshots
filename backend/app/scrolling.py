"""Turns a scrolled page into one long, full-page screenshot.

Screenshots are taken whenever the screen stays still, so when someone scrolls
down a long page and pauses along the way, the page arrives as several separate
shots. This module spots that and stitches those shots into one tall image.

How it works:
  1. track_motion() follows the video at low resolution (10 frames a second)
     and labels each step: "still", "scroll" (with how far), or "change"
     (anything else: a click, a new page, a menu opening).
  2. find_scroll_runs() looks at the steps between neighbouring screenshots.
     If nothing but scrolling happened in between, they're the same page.
  3. stitch_run() lines the screenshots up at full resolution and pastes them
     into one tall image. Where the person scrolled further than a screen's
     height between pauses, extra frames are taken from the video to fill the
     gap. Rows that never move (the browser bar, a phone's status bar or tab
     bar) appear once, at the top or bottom.

Anything uncertain counts as "change", so the worst case is that shots stay
separate, exactly as before.
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
from PIL import Image

from video_processing import extract_frame

# Motion tracking works on a small grey copy of the video.
TRACK_FPS = 10
TRACK_WIDTH = 256

# A pixel counts as "changed" if its brightness (0-255) moved by more than this.
PIXEL_CHANGE = 25
# If less than this share of the screen changed, it's a small, local change (the
# mouse pointer, a hover effect, the scrollbar fading, the first or last fraction of a
# pixel of a smooth scroll): the screen counts as still.
MINOR_CHANGE_SHARE = 0.05
# Rows that differ by less than this (average, 0-255) between two frames don't move.
FIXED_ROW_DIFFERENCE = 1.5
# For a step to count as a scroll, at most this share of the lined-up pixels may differ...
# (Measured on a real recording: scroll steps leave 1-4%, because a page moving by part
# of a pixel can't line up exactly; real changes leave 9% or more.)
SCROLL_MISMATCH_LIMIT = 0.05
# ...and clearly fewer than without lining up (filters out lucky matches on plain pages).
SCROLL_MISMATCH_RATIO = 0.6
# Right-hand strip ignored when comparing: where scrollbars appear and fade.
SCROLLBAR_SHARE = 0.04
# Smallest scroll step counted (in tracking pixels): the slow start and end of a scroll.
MIN_STEP = 1
# A page must move at least this far in total (tracking pixels) to count as scrolled.
MIN_SCROLL = 2
# Longest scroll looked for in one step, as a share of the visible content.
MAX_STEP_SHARE = 0.6


@dataclass
class Motion:
    """What happened between each pair of tracking frames."""
    times: List[float] = field(default_factory=list)  # time of the later frame in each step
    kinds: List[str] = field(default_factory=list)  # "still" | "scroll" | "change"
    shifts: List[int] = field(default_factory=list)  # scroll distance in tracking pixels (+ = down the page)
    width: int = TRACK_WIDTH
    height: int = 0


def _video_size(video_path: Path) -> Tuple[int, int]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True, check=True,
    )
    width, height = (int(v) for v in result.stdout.strip().split(",")[:2])
    return width, height


def _fixed_rows(a: np.ndarray, b: np.ndarray, tolerance: float) -> Tuple[int, int]:
    """How many rows at the top and bottom are the same in both frames."""
    row_diff = np.abs(a - b).mean(axis=1)
    top = 0
    while top < len(row_diff) and row_diff[top] < tolerance:
        top += 1
    bottom = 0
    while bottom < len(row_diff) - top and row_diff[len(row_diff) - 1 - bottom] < tolerance:
        bottom += 1
    return top, bottom


def _mismatch(a: np.ndarray, b: np.ndarray) -> float:
    """Share of pixels that differ noticeably between two same-sized images."""
    return float((np.abs(a - b) > PIXEL_CHANGE).mean())


def _best_shift(a: np.ndarray, b: np.ndarray, max_shift: int,
                candidates: Optional[range] = None) -> Tuple[int, float]:
    """The vertical shift that best lines b up with a, and the share of pixels that
    still differ once lined up.

    A shift of +d means b's content is a's content moved up by d rows, i.e. the
    page was scrolled down by d.
    """
    height = len(a)
    best = (0, float("inf"))
    for shift in candidates or range(-max_shift, max_shift + 1):
        if shift == 0 or abs(shift) >= height:
            continue
        if shift > 0:
            error = _mismatch(a[shift:], b[:height - shift])
        else:
            error = _mismatch(a[:height + shift], b[-shift:])
        if error < best[1]:
            best = (shift, error)
    return best


def classify_step(a: np.ndarray, b: np.ndarray) -> Tuple[str, int]:
    """Decides whether going from frame a to frame b was still, a scroll, or a change."""
    keep = slice(0, round(a.shape[1] * (1 - SCROLLBAR_SHARE)))
    a, b = a[:, keep], b[:, keep]
    if _mismatch(a, b) < MINOR_CHANGE_SHARE:
        return "still", 0
    top, bottom = _fixed_rows(a, b, FIXED_ROW_DIFFERENCE)
    content_a, content_b = a[top:len(a) - bottom], b[top:len(b) - bottom]
    if len(content_a) < 20:
        return "change", 0
    unshifted = _mismatch(content_a, content_b)
    shift, error = _best_shift(content_a, content_b, int(len(content_a) * MAX_STEP_SHARE))
    if abs(shift) >= MIN_STEP and error < SCROLL_MISMATCH_LIMIT and error < unshifted * SCROLL_MISMATCH_RATIO:
        return "scroll", shift
    return "change", 0


def track_motion(video_path: Path, crop_top: int = 0,
                 on_progress: Optional[Callable[[float], None]] = None,
                 duration: Optional[float] = None) -> Motion:
    """Follows the whole video at low resolution, step by step."""
    width, height = _video_size(video_path)
    height -= crop_top
    track_height = max(2, round(height * TRACK_WIDTH / width / 2) * 2)
    filters = []
    if crop_top > 0:
        filters.append(f"crop=iw:ih-{crop_top}:0:{crop_top}")
    filters += [f"fps={TRACK_FPS}", f"scale={TRACK_WIDTH}:{track_height}", "format=gray"]
    process = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(video_path), "-vf", ",".join(filters),
         "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    frame_bytes = TRACK_WIDTH * track_height
    motion = Motion(width=TRACK_WIDTH, height=track_height)
    previous = None
    index = 0
    while True:
        raw = process.stdout.read(frame_bytes)
        if len(raw) < frame_bytes:
            break
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(track_height, TRACK_WIDTH).astype(np.float32)
        if previous is not None:
            kind, shift = classify_step(previous, frame)
            motion.times.append(index / TRACK_FPS)
            motion.kinds.append(kind)
            motion.shifts.append(shift)
        previous = frame
        index += 1
        if on_progress and duration and index % TRACK_FPS == 0:
            on_progress(min(1.0, (index / TRACK_FPS) / duration))
    process.wait()
    return motion


def find_scroll_runs(frames: List[dict], motion: Motion) -> List[List[Tuple[int, int]]]:
    """Groups neighbouring screenshots that are the same page, scrolled.

    Two neighbours are the same page if nothing but scrolling (or nothing at all,
    apart from small local changes like a hover effect) happened between them.
    A run only counts if the page actually scrolled somewhere within it.

    frames: kept screenshots in flow order, each with "timestamp".
    Returns runs of (index into frames, page position in tracking pixels).
    Only runs of two or more screenshots are returned.
    """
    runs: List[List[Tuple[int, int]]] = []
    current: List[Tuple[int, int]] = [(0, 0)] if frames else []
    step = 0  # index into motion steps
    for i in range(1, len(frames)):
        start, end = frames[i - 1]["timestamp"], frames[i]["timestamp"]
        while step < len(motion.times) and motion.times[step] <= start:
            step += 1
        moved, changed = 0, False
        while step < len(motion.times) and motion.times[step] <= end:
            if motion.kinds[step] == "change":
                changed = True
            elif motion.kinds[step] == "scroll":
                moved += motion.shifts[step]
            step += 1
        if not changed:
            current.append((i, current[-1][1] + moved))
        else:
            runs.append(current)
            current = [(i, 0)]
    runs.append(current)

    def scrolled(run: List[Tuple[int, int]]) -> bool:
        positions = [pos for _, pos in run]
        return len(run) > 1 and max(positions) - min(positions) >= MIN_SCROLL

    return [run for run in runs if scrolled(run)]


def _load_grey(path: Path) -> np.ndarray:
    """The middle half of a screenshot, in grey: enough to line pages up, at half the memory."""
    with Image.open(path) as img:
        width = img.width
        middle = img.convert("L").crop((width // 4, 0, width * 3 // 4, img.height))
        return np.asarray(middle, dtype=np.float32)


def _refine(a: np.ndarray, b: np.ndarray, top: int, bottom: int, estimate: int, margin: int) -> int:
    """Pinpoints the shift between two full-resolution frames near an estimate."""
    # The middle of the (already halved) strip: plenty to line things up, and much faster.
    width = a.shape[1]
    strip = slice(width // 4, width * 3 // 4)
    content_a = a[top:a.shape[0] - bottom, strip]
    content_b = b[top:b.shape[0] - bottom, strip]
    shift, _ = _best_shift(content_a, content_b, 0, range(estimate - margin, estimate + margin + 1))
    # (_best_shift scores by mismatch share, which is just as good at full resolution.)
    return shift if shift != 0 else estimate


def stitch_run(frames_dir: Path, video_path: Path, run_frames: List[dict], positions: List[int],
               motion: Motion, output_path: Path) -> Tuple[int, int, int]:
    """Stitches one scrolled page into a single tall PNG.

    run_frames: the screenshots in the run (flow order), positions: their page
    positions in tracking pixels. Returns (width, height, viewport_height) of the
    stitched image, where viewport_height is the height of one ordinary screenshot.
    """
    stills = [frames_dir / f["filename"] for f in run_frames]
    with Image.open(stills[0]) as first:
        full_width, full_height = first.size
    scale = full_width / motion.width

    # Rows that never move across the whole run (browser bar, status bar, tab bar).
    greys = [_load_grey(p) for p in stills]
    top, bottom = full_height, full_height
    for a, b in zip(greys, greys[1:]):
        t, bt = _fixed_rows(a, b, FIXED_ROW_DIFFERENCE)
        top, bottom = min(top, t), min(bottom, bt)
    content_height = full_height - top - bottom

    # Where each screenshot sits on the page, in full-resolution pixels. Start from the
    # tracking estimate, then pinpoint each neighbour-to-neighbour step at full size.
    shots: List[Tuple[int, Path, np.ndarray]] = [(0, stills[0], greys[0])]
    for k in range(1, len(stills)):
        estimate = round((positions[k] - positions[k - 1]) * scale)
        previous_pos, previous_path, previous_grey = shots[-1]
        if abs(estimate) < content_height * 0.9:
            step = _refine(previous_grey, greys[k], top, bottom, estimate, margin=max(8, round(scale * 2)))
        else:
            # Scrolled further than a screen between pauses: take in-between frames from the
            # video so there's no gap, then line the screenshot up against the last of them.
            start_t = run_frames[k - 1]["timestamp"]
            end_t = run_frames[k]["timestamp"]
            pieces = max(1, int(abs(estimate) // (content_height * 0.6)))
            for p in range(1, pieces + 1):
                t = start_t + (end_t - start_t) * p / (pieces + 1)
                filler = output_path.with_name(f"{output_path.stem}.fill{k}-{p}.png")
                extract_frame(video_path, t, filler)
                filler_grey = _load_grey(filler)
                previous_pos, previous_path, previous_grey = shots[-1]
                guess = _position_at(motion, t, run_frames[0]["timestamp"], positions[0])
                filler_estimate = round(guess * scale) - previous_pos
                refined = _refine(previous_grey, filler_grey, top, bottom, filler_estimate,
                                  margin=max(16, round(scale * 4)))
                shots.append((previous_pos + refined, filler, filler_grey))
            previous_pos, previous_path, previous_grey = shots[-1]
            remaining = round(positions[k] * scale) - previous_pos
            step = _refine(previous_grey, greys[k], top, bottom, remaining, margin=max(16, round(scale * 4)))
        shots.append((shots[-1][0] + step, stills[k], greys[k]))

    lowest = min(pos for pos, _, _ in shots)
    highest = max(pos for pos, _, _ in shots)
    page_height = highest - lowest + content_height
    out = Image.new("RGB", (full_width, top + page_height + bottom), "white")
    # Paste down the page. Each join sits halfway through the overlap between two pieces,
    # away from either piece's edge, where browsers often draw fades, shadows or dividers.
    ordered = sorted(shots, key=lambda s: s[0])
    seam = lowest  # page position where the next piece starts being used
    for k, (pos, path, _) in enumerate(ordered):
        if k + 1 < len(ordered):
            next_pos = ordered[k + 1][0]
            overlap_end = pos + content_height
            end = (next_pos + overlap_end) // 2 if next_pos < overlap_end else overlap_end
        else:
            end = pos + content_height
        start = max(seam, pos)
        if end <= start:
            continue
        with Image.open(path) as img:
            img = img.convert("RGB")
            piece = img.crop((0, top + start - pos, full_width, top + end - pos))
            out.paste(piece, (0, top + start - lowest))
        seam = end
    with Image.open(stills[0]) as img:
        out.paste(img.convert("RGB").crop((0, 0, full_width, top)), (0, 0))
    with Image.open(stills[-1]) as img:
        if bottom > 0:
            out.paste(img.convert("RGB").crop((0, full_height - bottom, full_width, full_height)),
                      (0, top + page_height))
    out.save(output_path)

    for _, path, _ in shots:  # in-between frames are only needed for stitching
        if ".fill" in path.name:
            path.unlink(missing_ok=True)
    return out.width, out.height, full_height


def _position_at(motion: Motion, time: float, start_time: float, start_position: int) -> int:
    """The page position (tracking pixels) at a moment, counting scrolls since start_time."""
    position = start_position
    for t, kind, shift in zip(motion.times, motion.kinds, motion.shifts):
        if t <= start_time:
            continue
        if t > time:
            break
        if kind == "scroll":
            position += shift
    return position
