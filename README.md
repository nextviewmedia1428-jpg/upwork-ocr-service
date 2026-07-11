# Upwork OCR Service

Tiny FastAPI + Tesseract OCR microservice. Accepts base64-encoded screenshot
images (or a screen-recording video, frame-extracted via `ffmpeg`), returns
raw extracted text. Used by n8n Workflow F to pull text out of Upwork job
captures before regex-parsing it into sheet fields.

## Deploying to Render (free tier)

1. Go to [dashboard.render.com](https://dashboard.render.com) → **New** → **Web Service**
2. Connect this GitHub repo
3. Render will auto-detect the `Dockerfile` — leave build/start commands blank
4. Set the **Instance Type** to **Free**
5. Under **Environment Variables**, add:
   - `OCR_SHARED_SECRET` = (the secret value provided separately in chat — do not commit it here)
6. Click **Create Web Service** and wait for the build to finish
7. Copy the resulting URL (e.g. `https://upwork-ocr-service.onrender.com`) and send it back — it needs to be wired into Workflow F's HTTP Request node

Note: Render's free tier spins the service down after ~15 minutes of
inactivity. The first request after a cold start takes 30-60s to wake up;
subsequent requests are fast. Fine for an occasional-use internal tool.

## Endpoints

- `GET /health` — returns `{"status": "ok"}`
- `POST /ocr` — body `{"images": ["<base64>", ...]}`, header `x-ocr-secret: <shared secret>`,
  returns `{"text": "<concatenated OCR text>", "perImage": ["<text per image>"]}`
- `POST /ocr-video` — body `{"video": "<base64>", "fileExtension": "mp4"}`, header
  `x-ocr-secret: <shared secret>`. **Async job queue, not a blocking call** —
  returns immediately with `{"jobId": "...", "status": "pending"}` and does
  the real work in a background task. Extracts frames via `ffmpeg` at a
  sparse 0.5 frames/sec (one every 2s, capped at 10 frames = up to 20s of
  recording — longer recordings resolve to `status: "error"`), downscaled to
  1000px wide, OCR'd with `--oem 1`. (An earlier version tried `mpdecimate` to
  skip "duplicate" frames, but real thumb-scrolling is continuous motion — no
  two frames are pixel-identical — so it decimated nothing; sparse time-based
  sampling is the actual fix, with the overlap-anchor stitching in n8n's
  `Parse OCR Text` reassembling the partially-overlapping content between
  samples, same as it does for multi-screenshot uploads.)

  **Why async:** measured directly against the live Render free-tier
  instance, a single realistic 1000px-wide frame takes 37-43s through
  Tesseract there — 20-40x slower than local Docker testing suggested, and
  unrelated to `--oem 1` (`/ocr` doesn't even set that flag and is equally
  slow). No synchronous HTTP timeout can cover 10 frames at that rate
  (~400-600s worst case), so the client submits once and polls instead.

- `GET /ocr-video/status/{jobId}` — header `x-ocr-secret: <shared secret>`.
  Returns `{"jobId", "status": "pending"}` while running, or on completion
  `{"jobId", "status": "done", "text", "perImage"}` (same shape as `/ocr`) or
  `{"jobId", "status": "error", "detail"}`. `404` for an unknown job id. Job
  state is an in-memory dict — fine for a single-instance internal tool, but
  is lost if Render restarts the service mid-job.

## Local testing

```bash
docker build -t upwork-ocr-service .
docker run -p 8000:8000 -e OCR_SHARED_SECRET=test upwork-ocr-service
curl http://localhost:8000/health
```
