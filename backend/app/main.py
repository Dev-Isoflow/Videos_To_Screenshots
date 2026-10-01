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
from pathlib import Path
from secrets import choice as secret_choice
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from video_processing import EXTRACTION_OFFSET_SECONDS, extract_frame, parse_freezes, run_freezedetect

from . import labeling, storage

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


def process_video(session_dir: Path, video_path: Path, crop: int, noise: float, duration: float) -> None:
    frames_dir = session_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    try:
        stderr = run_freezedetect(video_path, crop, noise, duration)
        freezes = parse_freezes(stderr)

        frames = []
        for i, (freeze_start, freeze_end) in enumerate(freezes, start=1):
            timestamp = max(freeze_start, freeze_end - EXTRACTION_OFFSET_SECONDS)
            filename = f"{i:02d}.png"
            extract_frame(video_path, timestamp, frames_dir / filename)
            frames.append({"filename": filename, "timestamp": round(timestamp, 3), "included": True})

        session_update = {"status": "ready", "frames": frames}

        labels = labeling.label_session(frames_dir, frames)
        if labels:
            for frame in frames:
                frame["label"] = labels["labels"].get(frame["filename"])
            session_update["flowLabel"] = labels["flowLabel"]
            session_update["flowSummary"] = labels["flowSummary"]

        storage.update_session(session_dir, **session_update)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
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
            "url": f"{base}/media/{session_code}/frames/{frame['filename']}",
            "timestamp": frame["timestamp"],
            "filename": frame["filename"],
            "label": frame.get("label"),
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
    }


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
