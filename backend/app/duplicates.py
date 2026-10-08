"""Spots screenshots that are duplicates of the one just before them.

Freezedetect sometimes finds two still moments on what is really the same
screen (e.g. the person paused, nudged the scrollbar, paused again). Those
frames look identical apart from tiny details like the clock or a scrollbar.

Each frame is compared with the last frame that was KEPT, so a run of
identical screens collapses to its first one. A screen the person comes back
to later in the video is NOT a duplicate — returning to it is a real step in
the flow — because something different will have been kept in between.

Duplicates aren't deleted: they're marked "included": False (and
"duplicateOf": <filename>), so the review page can switch them back on.
"""

from pathlib import Path
from typing import List

from PIL import Image

# Share of the picture (0-1) that must differ for two frames to count as
# different screens. Measured on real recordings: duplicates differ by ~0.25%,
# a loading screen gaining one row of content by ~1.6%, different screens by 9%+.
DUPLICATE_CHANGE_LIMIT = 0.008

# Ignore the top of the screen, where the status bar clock and icons change.
STATUS_BAR_SHARE = 0.07

# A pixel counts as "changed" if its brightness moved by more than this (0-255),
# which ignores video compression noise.
PIXEL_CHANGE = 25


def _thumbnail(path: Path) -> List[int]:
    with Image.open(path) as img:
        width, height = img.size
        body = img.crop((0, int(height * STATUS_BAR_SHARE), width, height))
        # Small, but enough detail to notice one new row of content.
        return list(body.convert("L").resize((48, 96)).getdata())


def changed_share(a: List[int], b: List[int]) -> float:
    """Share of thumbnail pixels that differ noticeably between two frames."""
    return sum(abs(p - q) > PIXEL_CHANGE for p, q in zip(a, b)) / len(a)


def mark_duplicates(frames_dir: Path, frames: List[dict]) -> int:
    """Marks duplicate frames as excluded, in place. Returns how many were found."""
    found = 0
    kept_thumb = None
    kept_name = None
    for frame in frames:
        try:
            thumb = _thumbnail(frames_dir / frame["filename"])
        except Exception as exc:  # noqa: BLE001 - never block processing on this
            print(f"[duplicates] skipped {frame['filename']}: {exc}")
            continue
        if kept_thumb is not None and changed_share(kept_thumb, thumb) < DUPLICATE_CHANGE_LIMIT:
            frame["included"] = False
            frame["duplicateOf"] = kept_name
            found += 1
        else:
            kept_thumb, kept_name = thumb, frame["filename"]
    return found
