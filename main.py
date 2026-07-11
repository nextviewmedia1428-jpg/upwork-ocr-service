import base64
import glob
import io
import os
import shutil
import subprocess
import tempfile

from fastapi import FastAPI, HTTPException, Request
from PIL import Image
from pydantic import BaseModel
import pytesseract

app = FastAPI()

SHARED_SECRET = os.environ.get("OCR_SHARED_SECRET", "")

# ponytail: mpdecimate (tried first) assumes near-duplicate = pixel-identical,
# which only happens if the user pauses mid-scroll. Real thumb-scrolling is
# continuous motion — every sampled frame differs a little — so it decimated
# nothing and a real recording still produced 41 frames (execution 9299).
# The fix isn't detecting "key frames", it's sampling sparsely and letting the
# same overlap-anchor text stitching already used for multi-screenshot merges
# (Parse OCR Text's mergeWithOverlap) reassemble the partially-overlapping
# content — a low-fps video sample IS just an auto-taken screenshot series.
# Measured directly against the live Render free-tier instance (not just
# locally, where it's ~4-5x faster): a real 1080x1920 20s/10-frame recording
# takes ~125s end-to-end — much slower than local Docker testing suggested.
# The cap below is set to what's actually been verified live, not guessed;
# raise it only after measuring the new worst case against the live service,
# and keep n8n's HTTP node timeout comfortably above it (see workflow_f.py).
VIDEO_FPS = 0.5  # one sample every 2s — leaves real overlap for stitching, unlike 1fps+dedup
MAX_VIDEO_FRAMES = 10  # 10 frames at 0.5fps = up to 20s of recording — matches measured live timing
FRAME_WIDTH = 1000  # downscale before OCR; job-posting text is legible well below this
TESSERACT_CONFIG = "--oem 1"  # LSTM-only, skips slower legacy-engine fallback attempts


class OcrRequest(BaseModel):
    images: list[str]  # base64-encoded image strings, no data: URI prefix


class OcrVideoRequest(BaseModel):
    video: str  # base64-encoded video file, no data: URI prefix
    fileExtension: str = "mp4"


@app.get("/health")
def health():
    return {"status": "ok"}


# ponytail: routes are plain `def`, not `async def` — every path below is
# blocking sync code (subprocess.run, pytesseract), and an async route runs
# straight on the single event loop thread, so one slow request freezes the
# ENTIRE service (confirmed live: a 1x1 pixel image on /ocr hung with zero
# response while a prior video request was still processing). Plain `def`
# lets Starlette dispatch each request onto its own threadpool worker instead.
OCR_TIMEOUT_SECONDS = 60  # per-frame safety net; a hung Tesseract call has no timeout otherwise


@app.post("/ocr")
def ocr(req: OcrRequest, request: Request):
    if SHARED_SECRET and request.headers.get("x-ocr-secret") != SHARED_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

    per_image = []
    for b64 in req.images:
        image_bytes = base64.b64decode(b64)
        img = Image.open(io.BytesIO(image_bytes))
        try:
            per_image.append(pytesseract.image_to_string(img, timeout=OCR_TIMEOUT_SECONDS))
        except RuntimeError:
            per_image.append("")  # image timed out; skip it rather than fail the whole batch

    return {"text": "\n\n---\n\n".join(per_image), "perImage": per_image}


@app.post("/ocr-video")
def ocr_video(req: OcrVideoRequest, request: Request):
    if SHARED_SECRET and request.headers.get("x-ocr-secret") != SHARED_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

    tmp_dir = tempfile.mkdtemp()
    try:
        ext = (req.fileExtension or "mp4").lstrip(".")
        video_path = os.path.join(tmp_dir, f"input.{ext}")
        with open(video_path, "wb") as f:
            f.write(base64.b64decode(req.video))

        frame_pattern = os.path.join(tmp_dir, "frame_%04d.jpg")
        result = subprocess.run(
            [
                "ffmpeg", "-i", video_path,
                "-vf", f"fps={VIDEO_FPS},scale={FRAME_WIDTH}:-1",
                "-q:v", "3", frame_pattern,
            ],
            capture_output=True,
            timeout=90,
        )
        frame_paths = sorted(glob.glob(os.path.join(tmp_dir, "frame_*.jpg")))

        if result.returncode != 0 or not frame_paths:
            stderr_tail = result.stderr.decode(errors="ignore")[-500:]
            raise HTTPException(status_code=422, detail=f"Could not read video: {stderr_tail}")

        if len(frame_paths) > MAX_VIDEO_FRAMES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Recording produced {len(frame_paths)} frames at {VIDEO_FPS}fps — "
                    f"keep screen recordings under {int(MAX_VIDEO_FRAMES / VIDEO_FPS)} seconds"
                ),
            )

        per_image = []
        for p in frame_paths:
            try:
                per_image.append(
                    pytesseract.image_to_string(
                        Image.open(p), config=TESSERACT_CONFIG, timeout=OCR_TIMEOUT_SECONDS
                    )
                )
            except RuntimeError:
                per_image.append("")  # frame timed out; skip it rather than fail the whole batch

        return {"text": "\n\n---\n\n".join(per_image), "perImage": per_image}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
