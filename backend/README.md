# UX Research Screenshot Backend (local, v0)

Takes a screen-recording, runs the freezedetect pipeline from
[`video_processing.py`](../video_processing.py) (shared with `grab_states.py`),
and exposes the extracted frames as a session the Figma plugin can pull in.

This is intentionally a local-only v0: no cloud hosting, no database — just a
FastAPI server writing to disk, meant to validate the workflow before
deciding where (if anywhere) it needs to actually live.

## Setup

```bash
cd /Users/squeezey/Documents/Videos_To_Screenshots
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

Requires `ffmpeg` on PATH (same requirement as `grab_states.py`).

## Run

From the **repo root** (important — `video_processing` is a repo-root module):

```bash
uvicorn backend.app.main:app --reload --port 8000
```

## Using it

**1. Upload a video** (multipart form, `video` is the file field):

```bash
curl -X POST http://localhost:8000/api/sessions \
  -F "video=@MrPTestVideo.mov" \
  -F "video_name=Checkout Flow Walkthrough"
```

Returns immediately with `{"sessionCode": "AB12CD", "videoName": "...", "status": "processing"}`
— freezedetect + frame extraction runs in a background thread, since a
multi-minute video can take a while.

**2. Review / scrub the frames** — open in a browser:

```
http://localhost:8000/review?code=AB12CD
```

Reload if it still says "processing". From there you can exclude frames,
reorder them (↑/↓), and Save — this rewrites the session's frame list, which
is what step 3 reads.

**3. Fetch the session** (this is the endpoint the Figma plugin calls):

```bash
curl http://localhost:8000/api/sessions/AB12CD
```

```json
{
  "sessionCode": "AB12CD",
  "videoName": "Checkout Flow Walkthrough",
  "status": "ready",
  "frames": [
    { "url": "http://localhost:8000/media/AB12CD/frames/01.png", "timestamp": 3.2, "filename": "01.png" }
  ]
}
```

## Pointing the plugin at this

In the plugin repo:

1. `src/ui.ts` — set `USE_MOCK = false`, `BACKEND_BASE_URL = 'http://localhost:8000'`.
2. `manifest.json` — add a `devAllowedDomains` entry so Figma allows the plugin
   to reach localhost during development:

   ```json
   "networkAccess": {
     "allowedDomains": ["*"],
     "devAllowedDomains": ["http://localhost:8000"]
   }
   ```

3. Rebuild the plugin and reload it in Figma.

## Data layout

Everything lives under `backend/data/` (gitignored):

```
backend/data/<sessionCode>/
  source.mov                 # the uploaded video
  session.json                # { sessionCode, videoName, status, frames: [...] }
  frames/01.png, 02.png, ...  # extracted frames
```

## Known v0 limitations (by design)

- Single always-on local process, no auth — fine for one machine, not for
  exposing over the internet as-is.
- No queue/retry: if the process is killed mid-extraction, the session is
  left with `status: "error"` or stuck at `"processing"` — just re-upload.
- No deletion/cleanup endpoint yet — old sessions just accumulate in
  `backend/data/` until you delete them by hand.
