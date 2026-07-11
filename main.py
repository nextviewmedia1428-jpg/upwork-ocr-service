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

# ponytail: Render's edge proxy hard-times-out around ~100s regardless of any
# timeout set in n8n — a 502 "Bad gateway" means the app didn't respond in time,
# not that the service spun down. Cold start (30-60s) + N frames of Tesseract on
# a throttled free-tier CPU can easily exceed that, so frame count is capped low
# and frames are downscaled (Tesseract speed scales with image size). Raise
# MAX_VIDEO_FRAMES only if cold-start mitigation (Workflow B's wake call) makes
# this the bottleneck again.
VIDEO_FPS = 1
MAX_VIDEO_FRAMES = 15
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


@app.post("/ocr")
async def ocr(req: OcrRequest, request: Request):
    if SHARED_SECRET and request.headers.get("x-ocr-secret") != SHARED_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

    per_image = []
    for b64 in req.images:
        image_bytes = base64.b64decode(b64)
        img = Image.open(io.BytesIO(image_bytes))
        per_image.append(pytesseract.image_to_string(img))

    return {"text": "\n\n---\n\n".join(per_image), "perImage": per_image}


@app.post("/ocr-video")
async def ocr_video(req: OcrVideoRequest, request: Request):
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
                    f"keep screen recordings under {MAX_VIDEO_FRAMES} seconds"
                ),
            )

        per_image = [
            pytesseract.image_to_string(Image.open(p), config=TESSERACT_CONFIG) for p in frame_paths
        ]
        return {"text": "\n\n---\n\n".join(per_image), "perImage": per_image}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
