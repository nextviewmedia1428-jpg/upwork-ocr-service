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
  `x-ocr-secret: <shared secret>`. Extracts frames via `ffmpeg` at a sparse
  0.5 frames/sec (one every 2s, capped at 10 frames = up to 20s of recording —
  longer recordings are rejected with `413`), downscaled to 1000px wide, OCR'd
  with `--oem 1` (LSTM-only, faster than the default engine auto-detect).
  Returns the same `{"text", "perImage"}` shape as `/ocr`, so it's a drop-in
  alternative source for the same downstream parsing — including the
  overlap-anchor text stitching in n8n's `Parse OCR Text`, which reassembles
  the partially-overlapping content between sparse samples exactly like it
  does for multi-screenshot uploads. (An earlier version tried `mpdecimate`
  to skip "duplicate" frames, but real thumb-scrolling is continuous motion —
  no two frames are pixel-identical — so it decimated nothing; sparse
  time-based sampling is the actual fix.) The 20s cap is set to what's been
  measured live against Render's free-tier CPU (a 20s/10-frame recording
  takes ~125s end-to-end there — roughly 4-5x slower than local Docker
  testing suggested), with n8n's own HTTP node timeout for this call set to
  180s to leave real margin above that.

## Local testing

```bash
docker build -t upwork-ocr-service .
docker run -p 8000:8000 -e OCR_SHARED_SECRET=test upwork-ocr-service
curl http://localhost:8000/health
```
