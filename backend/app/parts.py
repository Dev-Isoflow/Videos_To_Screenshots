"""Splits long recordings into parts of at most PART_LIMIT_SECONDS of video.

Each part is labeled and grouped on its own, and the Figma plugin places each
one as its own work area. Split points are chosen at natural breaks: near
where an even split would fall, the cut goes after the screen that stayed
still the longest (a long pause usually means one task ended and another is
about to start). No part ever spans more than PART_LIMIT_SECONDS.
"""

import math
from typing import List

# Longest stretch of recording in one part.
PART_LIMIT_SECONDS = 5 * 60

# Recordings longer than this are refused outright (they'd take a very long
# time and produce more screenshots than anyone wants to review at once).
MAX_RECORDING_SECONDS = 30 * 60

# A split may land anywhere from this share of an even part's length up to
# the limit, so there's room to find a pause rather than cutting mid-task.
EARLIEST_SPLIT_SHARE = 0.6


def plan_parts(frames: List[dict], video_length: float) -> List[dict]:
    """Returns [{"number", "start", "end", "filenames"}] covering every frame.

    frames: [{filename, timestamp, stillSeconds?}] in flow order.
    """
    if not frames or video_length <= PART_LIMIT_SECONDS:
        return [{"number": 1, "start": 0.0, "end": round(video_length, 3), "filenames": [f["filename"] for f in frames]}]

    parts: List[dict] = []
    start_index = 0
    start_time = 0.0
    while start_index < len(frames):
        # Skip a long stretch with no screens: a part starts no earlier than it needs to.
        if frames[start_index]["timestamp"] > start_time + PART_LIMIT_SECONDS:
            start_time = frames[start_index]["timestamp"]
        remaining = video_length - start_time
        if remaining <= PART_LIMIT_SECONDS:
            end_index = len(frames)  # everything left fits in this part
        else:
            # Aim for even parts across what's left, then look for the longest pause
            # in the window between "a bit early" and the hard limit.
            parts_left = math.ceil(remaining / PART_LIMIT_SECONDS)
            ideal = remaining / parts_left
            window = [
                i for i in range(start_index, len(frames))
                if start_time + ideal * EARLIEST_SPLIT_SHARE <= frames[i]["timestamp"] <= start_time + PART_LIMIT_SECONDS
            ]
            if window:
                best = max(window, key=lambda i: (frames[i].get("stillSeconds") or 0, i))
            else:
                # No screens in the window: take everything up to the limit. There's always at
                # least one, because the part starts no later than its first screen.
                fitting = [i for i in range(start_index, len(frames)) if frames[i]["timestamp"] <= start_time + PART_LIMIT_SECONDS]
                best = fitting[-1]
            end_index = best + 1

        # The part ends where the next part's first screen begins, but never more than
        # the limit after it started (the gap may be a long stretch with no screens).
        next_start = frames[end_index]["timestamp"] if end_index < len(frames) else video_length
        end_time = min(next_start, start_time + PART_LIMIT_SECONDS)
        parts.append({
            "number": len(parts) + 1,
            "start": round(start_time, 3),
            "end": round(end_time, 3),
            "filenames": [f["filename"] for f in frames[start_index:end_index]],
        })
        start_index, start_time = end_index, end_time
    return parts
