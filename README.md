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
  `x-ocr-secret: <shared secret>`. Extracts frames via `ffmpeg` at 1 frame/sec,
  downscaled to 1000px wide (capped at 15 frames — recordings over ~15s are
  rejected with `413`), then OCRs each frame with `--oem 1` (LSTM-only, faster
  than the default engine auto-detect). Returns the same `{"text", "perImage"}`
  shape as `/ocr`, so it's a drop-in alternative source for the same downstream
  parsing. The frame cap and downscaling exist because Render's edge proxy
  returns a `502 Bad gateway` if the app doesn't respond within ~100s
  regardless of the client's own timeout — keeping total OCR work small is the
  only way to stay under that ceiling on a free-tier CPU.

## Local testing

```bash
docker build -t upwork-ocr-service .
docker run -p 8000:8000 -e OCR_SHARED_SECRET=test upwork-ocr-service
curl http://localhost:8000/health
```
