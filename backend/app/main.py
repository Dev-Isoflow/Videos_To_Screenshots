"""Local dev backend for the UX-research screenshot pipeline.

Run from the repo root (so `video_processing` resolves) with:

    uvicorn backend.app.main:app --reload --port 8000

Flow:
    1. POST   /api/sessions          — upload a video, kicks off freezedetect
                                        extraction in a background thread.
    2. GET    /review?code=XXXXXX    — optional: a plain HTML page to
                                        review/reorder/exclude frames.
    3. GET    /api/sessions/{code}   — the contract the Figma plugin
                                        consumes: { sessionCode, videoName,
                                        status, frames }.
    4. DELETE /api/sessions/{code}   — called by the plugin once every frame
                                        has been placed on canvas; the
                                        session's data is disposable at that
                                        point since it now lives in Figma.
"""

import shutil
import string
import threading
import time
from pathlib import Path
from secrets import choice as secret_choice
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from video_processing import (
    EXTRACTION_OFFSET_SECONDS,
    extract_frame,
    parse_freezes,
    run_freezedetect_with_progress,
    video_duration,
)

from . import duplicates, grouping, labeling, parts, scrolling, storage

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "backend" / "data"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Explicit path rather than a bare load_dotenv(), so this finds
# backend/.env regardless of the cwd uvicorn was started from.
load_dotenv(REPO_ROOT / "backend" / ".env")
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="UX Research Screenshot Backend")

# Wide open for local dev: the plugin's UI iframe and the review page both
# need to fetch this API cross-origin. Tighten this before deploying anywhere
# that isn't your own machine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/media", StaticFiles(directory=DATA_DIR), name="media")


def generate_session_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secret_choice(alphabet) for _ in range(length))
        if not (DATA_DIR / code).exists():
            return code


class SessionGone(Exception):
    """The session was deleted while it was still processing (e.g. the plugin gave up)."""


class ProgressReporter:
    """Saves what stage processing is at, so the plugin can show it and can tell
    "slow but moving" apart from "stuck". Writes at most about once a second."""

    def __init__(self, session_dir: Path):
        self.session_dir = session_dir
        self.last_write = 0.0

    def __call__(
        self,
        stage: str,
        done: Optional[float] = None,
        total: Optional[float] = None,
        force: bool = False,
        part: Optional[int] = None,
        parts: Optional[int] = None,
    ):
        if not self.session_dir.exists():
            raise SessionGone()
        now = time.monotonic()
        if not force and now - self.last_write < 1.0:
            return
        self.last_write = now
        progress = {"stage": stage, "done": done, "total": total, "part": part, "parts": parts}
        storage.update_session(self.session_dir, progress=progress)


def process_video(session_dir: Path, video_path: Path, crop: int, noise: float, duration: float) -> None:
    frames_dir = session_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    report = ProgressReporter(session_dir)

    try:
        length = video_duration(video_path) or 0.0
        if length > parts.MAX_RECORDING_SECONDS:
            minutes = parts.MAX_RECORDING_SECONDS // 60
            raise ValueError(
                f"This recording is {length / 60:.0f} minutes long. The limit is {minutes} minutes, "
                "so please trim it or split it into shorter recordings."
            )

        # Stage 1: scan the video for still moments ("done" is the fraction scanned).
        report("scanning", 0, 1, force=True)
        stderr = run_freezedetect_with_progress(
            video_path, crop, noise, duration, on_progress=lambda fraction: report("scanning", fraction, 1)
        )
        freezes = parse_freezes(stderr)

        # Stage 2: save a full-quality screenshot of each still moment.
        frames = []
        for i, (freeze_start, freeze_end) in enumerate(freezes, start=1):
            report("extracting", i - 1, len(freezes))
            timestamp = max(freeze_start, freeze_end - EXTRACTION_OFFSET_SECONDS)
            filename = f"{i:02d}.png"
            extract_frame(video_path, timestamp, frames_dir / filename)
            frames.append({
                "filename": filename,
                "timestamp": round(timestamp, 3),
                # How long the screen stayed still — long stills hint at a pause between tasks.
                "stillSeconds": round(freeze_end - freeze_start, 3),
                "included": True,
            })

        # Hide frames that repeat the screen just before them. Done before labeling,
        # so the AI isn't asked (or paid) to look at the same screen twice.
        duplicate_count = duplicates.mark_duplicates(frames_dir, frames)
        kept = [frame for frame in frames if frame["included"]]

        # A page scrolled through with pauses arrives as several screenshots: stitch each
        # one into a single full-page screenshot. The top screen keeps its filename (and is
        # what the AI and the free rules look at); the others are hidden, like duplicates.
        report("tracking", 0, 1, force=True)
        motion = scrolling.track_motion(
            video_path, crop, on_progress=lambda fraction: report("tracking", fraction, 1), duration=length or None
        )
        runs = scrolling.find_scroll_runs(kept, motion)
        for number, run in enumerate(runs):
            report("stitching", number, len(runs), force=True)
            run_frames = [kept[i] for i, _ in run]
            first = run_frames[0]
            full_name = f"{Path(first['filename']).stem}-full.png"
            try:
                width, height, viewport_height = scrolling.stitch_run(
                    frames_dir, video_path, run_frames, [pos for _, pos in run], motion, frames_dir / full_name
                )
            except Exception as exc:  # noqa: BLE001 - leave the screenshots separate instead
                print(f"[scrolling] couldn't stitch {first['filename']}: {exc}")
                continue
            first.update(fullPageFile=full_name, fullPageSize=[width, height], viewportHeight=viewport_height)
            for frame in run_frames[1:]:
                frame["included"] = False
                frame["mergedInto"] = first["filename"]
        kept = [frame for frame in frames if frame["included"]]

        # Long recordings are split into parts of at most 5 minutes, at natural pauses.
        # Each part is named and grouped on its own, and placed as its own work area.
        plan = parts.plan_parts(kept, length or (frames[-1]["timestamp"] if frames else 0.0))
        part_of = {name: part["number"] for part in plan for name in part["filenames"]}
        for frame in frames:  # hidden duplicates belong to the part of the frame they repeat
            frame["part"] = (
                part_of.get(frame["filename"])
                or part_of.get(frame.get("duplicateOf", ""))
                or part_of.get(frame.get("mergedInto", ""), 1)
            )
        session_update = {
            "status": "ready",
            "frames": frames,
            "duplicatesRemoved": duplicate_count,
            "parts": [{k: part[k] for k in ("number", "start", "end")} for part in plan],
        }

        all_groups = []
        for part in plan:
            number, count = part["number"], len(plan)
            part_frames = [f for f in kept if f["part"] == number]

            # Stage 3: name the screens and suggest groups with AI (in batches for long parts).
            report("labeling", 0, 1, force=True, part=number, parts=count)
            labels = labeling.label_session(
                frames_dir,
                part_frames,
                on_batch=lambda done, total: report("labeling", done, total, force=True, part=number, parts=count),
            )
            if labels:
                for frame in part_frames:
                    frame["label"] = labels["labels"].get(frame["filename"])
                session_update.setdefault("flowLabel", labels["flowLabel"])
                session_update.setdefault("flowSummary", labels["flowSummary"])

            # Stage 4: suggested groups. AI first, free rules when AI isn't available.
            report("grouping", force=True, part=number, parts=count)
            ai_groups = grouping.normalize_ai_groups(labels.get("groups") or [], part_frames) if labels else []
            part_groups = ai_groups or grouping.group_by_rules(frames_dir, part_frames)
            all_groups.extend({**group, "part": number} for group in part_groups)
        session_update["groups"] = all_groups

        if not session_dir.exists():
            raise SessionGone()
        storage.update_session(session_dir, **session_update)
    except SessionGone:
        print(f"[processing] {session_dir.name} was deleted while processing; stopped.")
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
        if session_dir.exists():
            storage.update_session(session_dir, status="error", error=str(exc))


@app.post("/api/sessions")
async def create_session(
    video: UploadFile = File(...),
    video_name: Optional[str] = Form(None),
    crop: int = Form(0),
    noise: float = Form(0.003),
    duration: float = Form(0.5),
):
    session_code = generate_session_code()
    session_dir = DATA_DIR / session_code
    session_dir.mkdir(parents=True)

    original_name = video.filename or "video.mp4"
    suffix = Path(original_name).suffix or ".mp4"
    video_path = session_dir / f"source{suffix}"
    with video_path.open("wb") as out:
        shutil.copyfileobj(video.file, out)

    resolved_name = video_name or Path(original_name).stem

    storage.create_session(
        session_dir,
        sessionCode=session_code,
        videoName=resolved_name,
        status="processing",
    )

    thread = threading.Thread(
        target=process_video,
        args=(session_dir, video_path, crop, noise, duration),
        daemon=True,
    )
    thread.start()

    return {"sessionCode": session_code, "videoName": resolved_name, "status": "processing"}


@app.get("/api/sessions/{session_code}")
async def get_session(session_code: str, request: Request):
    session_dir = DATA_DIR / session_code
    data = storage.read_session(session_dir)
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found")

    base = str(request.base_url).rstrip("/")
    frames = [
        {
            # A scrolled page's url is its stitched, full-page screenshot.
            "url": f"{base}/media/{session_code}/frames/{frame.get('fullPageFile') or frame['filename']}",
            "timestamp": frame["timestamp"],
            "filename": frame["filename"],
            "label": frame.get("label"),
            "fullPage": bool(frame.get("fullPageFile")),
            # Height of one ordinary screen, so a full-page screenshot can be shown at the same scale.
            "viewportHeight": frame.get("viewportHeight"),
        }
        for frame in data.get("frames", [])
        if frame.get("included", True)
    ]

    return {
        "sessionCode": data["sessionCode"],
        "videoName": data.get("videoName"),
        "status": data.get("status", "ready"),
        "error": data.get("error"),
        "flowLabel": data.get("flowLabel"),
        "flowSummary": data.get("flowSummary"),
        "frames": frames,
        "groups": _visible_groups(data.get("groups") or [], frames),
        "duplicatesRemoved": data.get("duplicatesRemoved", 0),
        # While processing: {"stage": "scanning" | "extracting" | "tracking" | "stitching" | "labeling" | "grouping",
        # "done", "total", "part", "parts"}.
        "progress": data.get("progress"),
        # Long recordings are split into parts: [{"number", "start", "end"}]. Groups say which part they're in.
        "parts": data.get("parts") or [{"number": 1, "start": 0, "end": None}],
    }


def _visible_groups(groups: list, frames: list) -> list:
    """Keeps the suggested groups in step with the review page: excluded
    frames drop out, the frames' (possibly reordered) order wins, and a frame
    no group mentions joins the group of the frame before it."""
    owner = {}
    for index, group in enumerate(groups):
        for filename in group.get("filenames", []):
            owner.setdefault(filename, index)

    result = []
    last = None
    for frame in frames:
        index = owner.get(frame["filename"], last)
        if index is None:
            index = 0 if groups else -1
        if not result or result[-1]["_index"] != index:
            source = groups[index] if index >= 0 else {"name": "All screens", "method": "none"}
            result.append({
                "_index": index,
                "name": source["name"],
                "method": source["method"],
                "part": source.get("part", frame.get("part", 1)),
                "filenames": [],
            })
        result[-1]["filenames"].append(frame["filename"])
        last = index
    for group in result:
        del group["_index"]
    return result


@app.get("/api/sessions/{session_code}/frames")
async def list_frames(session_code: str, request: Request):
    session_dir = DATA_DIR / session_code
    data = storage.read_session(session_dir)
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found")

    base = str(request.base_url).rstrip("/")
    frames = [
        {
            **frame,
            "url": f"{base}/media/{session_code}/frames/{frame['filename']}",
        }
        for frame in data.get("frames", [])
    ]

    return {
        "sessionCode": data["sessionCode"],
        "videoName": data.get("videoName"),
        "status": data.get("status", "ready"),
        "flowLabel": data.get("flowLabel"),
        "flowSummary": data.get("flowSummary"),
        "frames": frames,
    }


class FrameUpdate(BaseModel):
    filename: str
    timestamp: float
    included: bool
    label: Optional[str] = None


class FramesUpdateBody(BaseModel):
    frames: list[FrameUpdate]


@app.patch("/api/sessions/{session_code}/frames")
async def update_frames(session_code: str, body: FramesUpdateBody):
    session_dir = DATA_DIR / session_code
    data = storage.read_session(session_dir)
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # The list's order *is* the flow order — reordering in the review UI
    # means resubmitting the frames in the new order.
    storage.update_session(session_dir, frames=[frame.model_dump() for frame in body.frames])
    return {"ok": True}


@app.get("/review")
async def review_page():
    return FileResponse(STATIC_DIR / "review.html")


@app.delete("/api/sessions/{session_code}")
async def delete_session(session_code: str):
    session_dir = DATA_DIR / session_code
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    shutil.rmtree(session_dir)
    return {"ok": True}
