"""Optional post-processing step: ask Claude to label each extracted frame
and describe the overall flow. Runs inside the same background thread as
freezedetect extraction, right before a session flips to "ready" — if this
fails for any reason (network, auth, a malformed response), the session
still becomes ready with its frames; labeling is a nice-to-have, not on the
critical path, so failures here are swallowed rather than surfaced as a
session-level error.
"""

import base64
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

import anthropic
from pydantic import BaseModel

MODEL = "claude-sonnet-5"

# Downscale before sending — labeling doesn't need full-res screenshots, and
# capping this keeps image tokens (and cost) bounded regardless of how the
# recording was captured (e.g. retina/high-DPI source video).
MAX_EDGE_PIXELS = 1024


class FrameLabel(BaseModel):
    filename: str
    label: str


class FrameGroup(BaseModel):
    name: str
    filenames: List[str]


class SessionLabels(BaseModel):
    flow_label: str
    flow_summary: str
    frames: List[FrameLabel]
    groups: List[FrameGroup]


def _downscale(src: Path, dest: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf",
        f"scale='min({MAX_EDGE_PIXELS},iw)':'min({MAX_EDGE_PIXELS},ih)':force_original_aspect_ratio=decrease",
        str(dest),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def _encode_image(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("utf-8")


def label_session(frames_dir: Path, frames: List[dict]) -> Optional[dict]:
    """Labels every frame, names the overall flow, and splits the screens into
    groups, all in a single request (so Claude can label and group consistently
    across screens rather than in isolation).

    Returns {"flowLabel": str, "flowSummary": str, "labels": {filename: label},
    "groups": [FrameGroup, ...]} on success, or None if the request failed for
    any reason. The groups are raw model output; see grouping.normalise_groups.
    """
    if not frames:
        return None

    try:
        client = anthropic.Anthropic()

        content: list = [
            {
                "type": "text",
                "text": (
                    "These are screenshots from a UX research screen recording, "
                    "in flow order (the order the user experienced them). For "
                    "each screenshot, give a short (2-5 word) label describing "
                    "what screen or state it shows. Then give a short label for "
                    "the overall flow, and a one-sentence summary of what the "
                    "user is doing across these screens. Finally, split the "
                    "screenshots into groups, one per stage of the flow (for "
                    "example Onboarding, Browsing, Cart, Checkout). Keep the "
                    "groups in flow order, put every screenshot in exactly one "
                    "group, refer to screenshots by their filename, and give "
                    "each group a short (1-3 word) name. Use as few groups as "
                    "make sense; one group is fine if the flow is a single "
                    "stage."
                ),
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for frame in frames:
                filename = frame["filename"]
                scaled_path = tmp_dir / filename
                try:
                    _downscale(frames_dir / filename, scaled_path)
                except subprocess.CalledProcessError:
                    scaled_path = frames_dir / filename  # fall back to full-res

                content.append({"type": "text", "text": f"Screenshot: {filename}"})
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": _encode_image(scaled_path),
                        },
                    }
                )

            response = client.messages.parse(
                model=MODEL,
                max_tokens=4096,
                messages=[{"role": "user", "content": content}],
                output_format=SessionLabels,
            )

        result = response.parsed_output
        return {
            "flowLabel": result.flow_label,
            "flowSummary": result.flow_summary,
            "labels": {frame.filename: frame.label for frame in result.frames},
            "groups": result.groups,
        }
    except Exception as exc:  # noqa: BLE001 - best-effort step, never blocks extraction
        print(f"[labeling] skipped due to error: {exc}")
        return None
