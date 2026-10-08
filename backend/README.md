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

Requires `ffmpeg` on PATH (same requirement as `grab_states.py`), and an
`ANTHROPIC_API_KEY` for the labeling step (see below) — the server still
runs fine without one, it just skips labeling. The free grouping rules used
when labeling doesn't run also need `tesseract` (on a Mac: `brew install ffmpeg
tesseract`); without it they still group by pauses and repeated screens.

Put the key in `backend/.env` (gitignored — never commit this file):

```bash
cp backend/.env.example backend/.env
# then edit backend/.env and paste your real key in place of sk-ant-...
```

`backend/app/main.py` loads this file automatically on startup via
`python-dotenv` — no need to `export` it in your shell each session.

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
  "flowLabel": "Checkout flow",
  "flowSummary": "User reviews their cart, enters payment details, and confirms the order.",
  "frames": [
    { "url": "http://localhost:8000/media/AB12CD/frames/01.png", "timestamp": 3.2, "filename": "01.png", "label": "Cart review" }
  ]
}
```

## Screen/flow labeling

After extraction finishes (still inside the same background thread), the
backend sends the frames to Claude to label each screen and describe the
overall flow (`backend/app/labeling.py`, using `claude-sonnet-5`). Frames go
in batches of up to 40 (the API takes at most 100 images per request), as
compressed JPEGs no larger than 1024 px. Each batch is told the group names
used so far and where the previous batch ended, so a stage of the flow that
spans two batches comes back as one group; a final text-only request writes
the flow label and summary. If any batch fails, no partial labels are used. This is a nice-to-have, not on the critical path:
if it fails for any reason (no API key, network error, rate limit), the
session still becomes `"ready"` with its frames, just without `label` /
`flowLabel` / `flowSummary` populated. Check the server log for
`[labeling] skipped due to error: ...` if labels aren't showing up and you
expected them to.

## Suggested groups

The same Claude request that labels each screen also splits the screens into
groups (e.g. "Search", "Cart"), one per stage of the flow. `GET
/api/sessions/{code}` returns them as `groups`, in flow order:

```json
"groups": [
  { "name": "Search", "method": "ai", "filenames": ["01.png", "02.png"] },
  { "name": "Cart",   "method": "ai", "filenames": ["03.png"] }
]
```

The model's answer is tidied in `backend/app/grouping.py` before it's stored:
screens it invented or listed twice are dropped, groups are put in flow order,
and any screen it forgot lands in "Other screens". Frames excluded in review
disappear from their group.

When labeling doesn't run (no API key, no credit, an error), free rules group
the screens instead (`method: "rules"`), starting a new group wherever:

1. **Screen text** changes topic (OCR via Tesseract; words on most screens,
   like a logo or menu bar, are ignored, as is OCR garble not in the system
   word list),
2. a **"home base"** screen reappears (e.g. the app's home tab), or
3. the screen stayed still for a long **pause**.

Rule groups are named after the words that stand out in them. The thresholds
are at the top of `grouping.py`.

## Before labeling: duplicates and scrolled pages

- **Duplicates** (`duplicates.py`): a frame that repeats the screen just
  before it (under 0.8% of the picture differs, ignoring the status bar) is
  hidden: `"included": false, "duplicateOf": "<filename>"`. The response
  reports `duplicatesRemoved`.
- **Scrolled pages** (`scrolling.py`): the video is followed at low
  resolution, step by step, to tell scrolling apart from other changes. When
  nothing but scrolling happened between neighbouring frames, they're stitched
  into one full-page PNG (`<first>-full.png`, using in-between video frames if
  the person scrolled more than a screen between pauses). The first frame keeps
  its filename (it's what labeling and OCR look at); in the response its `url`
  points at the stitched image, with `"fullPage": true` and `viewportHeight`
  (the height of one ordinary screen). The other frames are hidden with
  `"mergedInto": "<filename>"`.

## Long recordings

Recordings over 5 minutes are split into parts of at most 5 minutes
(`parts.py`), cutting after the longest pause near each even split. Each part
is labeled and grouped on its own; the response lists `parts` (`number`,
`start`, `end`) and every group has a `part`. Recordings over 30 minutes are
refused with an error.

## Progress

While a session is `processing`, the response includes `progress`:
`{"stage", "done", "total", "part", "parts"}`, where `stage` is `scanning`
(fraction of the video), `extracting` (frame N of M), `tracking` (fraction),
`stitching` (page N of M), `labeling` (batch N of M) or `grouping`. The scan
looks at a 10 fps, 1280 px-wide copy of the video, and frames are extracted by
seeking before decoding, so a 6-minute 4K recording processes in a few
minutes.

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

## Deploying to Fly.io

The repo root has a `Dockerfile`, `.dockerignore` and `fly.toml`. Nothing here
has been deployed yet, and the image hasn't been built locally (no Docker on
the dev machine), so expect to fix small things on the first build.

You do these steps yourself (account and billing are yours):

```bash
brew install flyctl
fly auth login

# 1. Pick a unique app name: edit `app = ...` in fly.toml, then
fly apps create <your-app-name>

# 2. A volume for sessions (same region as primary_region in fly.toml;
#    check regions with `fly platform regions`)
fly volumes create session_data --size 3 --region jnb

# 3. Secrets. The token is what stops strangers uploading videos and
#    spending your Anthropic credit — keep a copy, the plugin needs it.
export API_TOKEN=$(openssl rand -hex 24) && echo "$API_TOKEN"
fly secrets set ANTHROPIC_API_KEY=sk-ant-... API_TOKEN="$API_TOKEN"

# 4. Deploy exactly one machine (see below for why)
fly deploy --ha=false

curl https://<your-app-name>.fly.dev/healthz     # {"ok":true}
```

Things that behave differently once it's on a server:

- **Auth.** When `API_TOKEN` is set, every `/api/*` and `/media/*` request
  needs `Authorization: Bearer <token>`; `/healthz` stays open. Locally, with
  no `API_TOKEN`, nothing changes. The `/review` page can't send the header,
  so treat it as local-debug only.
- **One machine only.** Sessions are JSON files on that machine's volume and
  processing runs in threads, so a second machine would have its own separate
  set of sessions. `--ha=false` matters on the first deploy; keep it at one
  with `fly scale count 1`.
- **Restarts and deploys** kill in-flight processing. On startup the server
  marks any session still `processing` as `error` ("please upload it again")
  rather than leaving the plugin polling forever.
- **Cleanup.** A background sweep deletes sessions untouched for
  `SESSION_TTL_HOURS` (24 by default; 0 disables), so abandoned uploads don't
  fill the volume. Sessions are still deleted immediately after placement.
- **Auto-stop.** The machine stops when idle and starts on the next request,
  so the first request after a quiet spell is slow. If you close the plugin
  mid-processing it can stop before finishing; that session is then marked
  `error` on next start.
- **Machine size.** `shared-cpu-2x` with 2 GB in `fly.toml`. freezedetect
  decodes the whole video, so a longer recording means a longer wait rather
  than more memory; bump the CPU if it feels slow.

Still to do on the plugin side before it can talk to a deployed server
(none of this is in the plugin repo yet): point `BACKEND_BASE_URL` at the
`https://<app>.fly.dev` URL, replace the `"*"` entry in the manifest's
`allowedDomains` with that host, send the bearer token on every request, and
load frame thumbnails through `fetch` instead of `<img src>` (an `<img>`
can't send the header). The "Codes only work on the machine that did the
upload" error copy also stops being true once the server is shared.

## Known v0 limitations (by design)

- No per-user accounts: one shared bearer token for the whole team, and a
  session code is a 6-character code, not a secret. Fine for an internal
  tool, not for anything public-facing.
- No queue/retry: a restart mid-extraction fails that session; re-upload.
- No cap on upload size beyond the volume itself.
