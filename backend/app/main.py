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

import os
import re
import secrets
import shutil
import string
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import choice as secret_choice
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from video_processing import EXTRACTION_OFFSET_SECONDS, extract_frame, parse_freezes, run_freezedetect

from . import grouping, labeling, storage

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Explicit path rather than a bare load_dotenv(), so this finds
# backend/.env regardless of the cwd uvicorn was started from.
load_dotenv(REPO_ROOT / "backend" / ".env")

# Where sessions live. Locally that's backend/data; on Fly it's the mounted
# volume (DATA_DIR=/data).
DATA_DIR = Path(os.environ.get("DATA_DIR") or REPO_ROOT / "backend" / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Optional shared secret. When set (always, on a deployed server), every
# /api and /media request must send `Authorization: Bearer <token>`. Unset
# locally so dev needs no configuration.
API_TOKEN = os.environ.get("API_TOKEN") or None

# Sessions older than this are deleted by a background sweep, so abandoned
# uploads don't fill the volume. 0 disables the sweep.
SESSION_TTL_HOURS = float(os.environ.get("SESSION_TTL_HOURS") or 24)
SWEEP_INTERVAL_SECONDS = 30 * 60

# Session codes are generated as six uppercase letters/digits. Anything else
# is rejected before it gets near a filesystem path (a code like ".." would
# otherwise resolve to the parent of DATA_DIR).
SESSION_CODE_RE = re.compile(r"^[A-Z0-9]{6}$")


def session_dir_for(session_code: str) -> Path:
    if not SESSION_CODE_RE.fullmatch(session_code):
        raise HTTPException(status_code=404, detail="Session not found")
    return DATA_DIR / session_code


def recover_interrupted_sessions() -> None:
    """Processing runs in daemon threads, so a restart or deploy kills them
    silently. Mark those sessions as failed rather than leaving the plugin
    polling a session that will never finish."""
    for session_dir in DATA_DIR.iterdir():
        data = storage.read_session(session_dir) if session_dir.is_dir() else None
        if data and data.get("status") == "processing":
            storage.update_session(
                session_dir,
                status="error",
                error="The server restarted while this video was processing. Please upload it again.",
            )


def sweep_expired_sessions() -> None:
    cutoff = time.time() - SESSION_TTL_HOURS * 3600
    for session_dir in DATA_DIR.iterdir():
        if not session_dir.is_dir():
            continue
        marker = session_dir / storage.SESSION_FILE
        last_touched = marker.stat().st_mtime if marker.exists() else session_dir.stat().st_mtime
        if last_touched < cutoff:
            shutil.rmtree(session_dir, ignore_errors=True)


def _sweep_loop() -> None:
    while True:
        time.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            sweep_expired_sessions()
        except Exception as exc:  # noqa: BLE001 - never let the sweeper die
            print(f"[sweep] failed: {exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    recover_interrupted_sessions()
    if SESSION_TTL_HOURS > 0:
        sweep_expired_sessions()
        threading.Thread(target=_sweep_loop, daemon=True).start()
    yield


app = FastAPI(title="UX Research Screenshot Backend", lifespan=lifespan)


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    protected = request.url.path.startswith(("/api/", "/media/"))
    if API_TOKEN and protected and request.method != "OPTIONS":
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(supplied, API_TOKEN):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


# Added after the auth middleware so it wraps it: even a 401 then carries CORS
# headers, which the browser needs to show the plugin a real error instead of
# an opaque network failure. Wide open on purpose — the plugin's UI iframe has
# a null origin, and auth is a bearer token rather than cookies.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


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
            session_update["groups"] = grouping.normalise_groups(
                labels["groups"], [frame["filename"] for frame in frames]
            )

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
    session_dir = session_dir_for(session_code)
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
    included = {frame["filename"] for frame in frames}

    return {
        "sessionCode": data["sessionCode"],
        "videoName": data.get("videoName"),
        "status": data.get("status", "ready"),
        "error": data.get("error"),
        "flowLabel": data.get("flowLabel"),
        "flowSummary": data.get("flowSummary"),
        "frames": frames,
        # Suggested groups, in flow order. Empty when labeling didn't run.
        "groups": grouping.restrict_to(data.get("groups", []), included),
    }


@app.get("/api/sessions/{session_code}/frames")
async def list_frames(session_code: str, request: Request):
    session_dir = session_dir_for(session_code)
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
    session_dir = session_dir_for(session_code)
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
    session_dir = session_dir_for(session_code)
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    shutil.rmtree(session_dir)
    return {"ok": True}
