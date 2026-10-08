"""Optional post-processing step: ask Claude to label each extracted frame,
suggest groups, and describe the overall flow. Runs inside the same background
thread as freezedetect extraction, right before a session flips to "ready" — if
this fails for any reason (network, auth, a malformed response), the session
still becomes ready with its frames; labeling is a nice-to-have, not on the
critical path, so failures here are swallowed rather than surfaced as a
session-level error.

Long recordings can produce more screenshots than one request allows (the API
takes at most 100 images per request), so frames are sent in batches, in flow
order. Each batch is told the group names used so far and which group the
previous batch ended on, so names stay consistent and a part of the flow that
spans two batches comes back as one group. A final text-only request writes the
overall flow label and summary from all the screen labels.
"""

import base64
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

import anthropic
from pydantic import BaseModel

MODEL = "claude-sonnet-5"

# Downscale before sending — labeling doesn't need full-res screenshots, and
# capping this keeps image tokens (and cost) bounded regardless of how the
# recording was captured (e.g. retina/high-DPI source video).
MAX_EDGE_PIXELS = 1024

# Most screenshots per request. Well under the API's 100-image limit, and with
# compressed JPEGs it keeps each request far below the request-size limit.
MAX_FRAMES_PER_BATCH = 40


class FrameLabel(BaseModel):
    filename: str
    label: str


class FrameGroup(BaseModel):
    name: str
    filenames: List[str]


class BatchLabels(BaseModel):
    frames: List[FrameLabel]
    groups: List[FrameGroup]


class FlowSummary(BaseModel):
    flow_label: str
    flow_summary: str


INSTRUCTIONS = (
    "These are screenshots from a UX research screen recording, in flow order "
    "(the order the user experienced them). For each screenshot, give a short "
    "(2-5 word) label describing what screen or state it shows. Then split the "
    "screenshots into groups, one per stage of the flow (for example "
    "Onboarding, Browsing, Cart, Checkout). Keep the groups in flow order, put "
    "every screenshot in exactly one group, refer to screenshots by their "
    "filename, and give each group a short (1-3 word) name. Use as few groups "
    "as make sense; one group is fine if the flow is a single stage. Loading "
    "states of the same screen belong together."
)


def _downscale(src: Path, dest: Path) -> None:
    """Writes a JPEG no larger than MAX_EDGE_PIXELS on its long edge."""
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf",
        f"scale='min({MAX_EDGE_PIXELS},iw)':'min({MAX_EDGE_PIXELS},ih)':force_original_aspect_ratio=decrease",
        "-q:v", "3",  # good-quality JPEG; far smaller than PNG for screenshots with photos
        str(dest),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def _encode_image(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("utf-8")


def _image_block(frames_dir: Path, tmp_dir: Path, filename: str) -> dict:
    scaled_path = tmp_dir / (Path(filename).stem + ".jpg")
    media_type = "image/jpeg"
    try:
        _downscale(frames_dir / filename, scaled_path)
    except subprocess.CalledProcessError:
        scaled_path, media_type = frames_dir / filename, "image/png"  # fall back to the original
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": _encode_image(scaled_path)},
    }


def _batches(frames: List[dict]) -> List[List[dict]]:
    """Splits frames into evenly sized batches of at most MAX_FRAMES_PER_BATCH."""
    count = math.ceil(len(frames) / MAX_FRAMES_PER_BATCH)
    size = math.ceil(len(frames) / count)
    return [frames[i:i + size] for i in range(0, len(frames), size)]


def _continuation_note(groups_so_far: List[dict]) -> str:
    """Tells a later batch what came before, so names stay consistent."""
    names = list(dict.fromkeys(g["name"] for g in groups_so_far))
    last = groups_so_far[-1]["name"]
    return (
        "These screenshots continue straight on from earlier ones in the same "
        f"recording. Group names used so far: {', '.join(repr(n) for n in names)}. "
        f"The previous screenshots ended in the group {last!r}: if the first "
        "screenshots here continue that part of the flow, put them in a group "
        f"named exactly {last!r}. If the user returns to a part of the flow seen "
        "earlier, reuse that part's name exactly."
    )


def _merge_neighbours(groups: List[dict]) -> List[dict]:
    """Joins back-to-back groups with the same name (e.g. one part of the flow
    that was split across two batches)."""
    merged: List[dict] = []
    for group in groups:
        if merged and merged[-1]["name"].strip().lower() == group["name"].strip().lower():
            merged[-1]["filenames"].extend(group["filenames"])
        else:
            merged.append({"name": group["name"], "filenames": list(group["filenames"])})
    return merged


def _summarise(client: anthropic.Anthropic, frames: List[dict], labels: dict, groups: List[dict]) -> FlowSummary:
    """One cheap text-only request for the overall flow label and summary."""
    lines = [f"- {labels.get(f['filename'], '?')}" for f in frames]
    parts = ", ".join(g["name"] for g in groups)
    response = client.messages.parse(
        model=MODEL,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                "These are the screens of a UX research screen recording, in order:\n"
                + "\n".join(lines)
                + f"\n\nIt's split into these parts: {parts}.\n"
                "Give a short label for the overall flow, and a one-sentence summary "
                "of what the user is doing across these screens."
            ),
        }],
        output_format=FlowSummary,
    )
    return response.parsed_output


def label_session(
    frames_dir: Path,
    frames: List[dict],
    on_batch: Optional[Callable[[int, int], None]] = None,
) -> Optional[dict]:
    """Labels every frame, suggests groups and describes the overall flow.

    on_batch(done, total) is called before each batch and once at the end, so
    the caller can show progress.

    Returns {"flowLabel": str, "flowSummary": str, "labels": {filename: label},
    "groups": [{name, filenames}]} on success, or None if labeling failed for
    any reason (in which case no partial labels are used). The groups are raw
    model output; see grouping.normalise_groups.
    """
    if not frames:
        return None

    try:
        client = anthropic.Anthropic()
        batches = _batches(frames)
        labels: dict = {}
        groups: List[dict] = []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for number, batch in enumerate(batches):
                if on_batch:
                    on_batch(number, len(batches))
                content: list = [{"type": "text", "text": INSTRUCTIONS}]
                if groups:
                    content.append({"type": "text", "text": _continuation_note(groups)})
                for frame in batch:
                    content.append({"type": "text", "text": f"Screenshot: {frame['filename']}"})
                    content.append(_image_block(frames_dir, tmp_dir, frame["filename"]))

                response = client.messages.parse(
                    model=MODEL,
                    max_tokens=4096,
                    messages=[{"role": "user", "content": content}],
                    output_format=BatchLabels,
                )
                result = response.parsed_output
                labels.update({f.filename: f.label for f in result.frames})
                groups.extend(g.model_dump() for g in result.groups)

        groups = _merge_neighbours(groups)
        if on_batch:
            on_batch(len(batches), len(batches))

        try:
            summary = _summarise(client, frames, labels, groups)
            flow_label, flow_summary = summary.flow_label, summary.flow_summary
        except Exception as exc:  # noqa: BLE001 - the summary is the least important part
            print(f"[labeling] flow summary skipped: {exc}")
            flow_label, flow_summary = None, None

        return {"flowLabel": flow_label, "flowSummary": flow_summary, "labels": labels, "groups": groups}
    except Exception as exc:  # noqa: BLE001 - best-effort step, never blocks extraction
        print(f"[labeling] skipped due to error: {exc}")
        return None
