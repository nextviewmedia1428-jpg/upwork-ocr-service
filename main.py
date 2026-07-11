import base64
import glob
import io
import os
import shutil
import subprocess
import tempfile
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
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
VIDEO_FPS = 0.5  # one sample every 2s — leaves real overlap for stitching, unlike 1fps+dedup
MAX_VIDEO_FRAMES = 10  # 10 frames at 0.5fps = up to 20s of recording
FRAME_WIDTH = 1000  # downscale before OCR; job-posting text is legible well below this
TESSERACT_CONFIG = "--oem 1"  # LSTM-only, skips slower legacy-engine fallback attempts

# ponytail: measured directly against the live Render free-tier instance —
# a single realistic 1000px-wide job-posting frame takes 37-43s through
# Tesseract there (roughly 20-40x slower than local Docker testing showed,
# and unrelated to --oem 1 — /ocr doesn't even set that flag and is equally
# slow). No synchronous HTTP timeout can cover N frames at that rate, so
# /ocr-video is now a submit-then-poll job queue instead of one blocking
# call: it returns a jobId immediately and does the real work in a
# BackgroundTasks callback. In-memory dict is fine — single Render
# instance, internal tool, job state loss on restart is acceptable.
jobs: dict[str, dict] = {}


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


def _process_video_job(job_id: str, video_b64: str, file_extension: str):
    tmp_dir = tempfile.mkdtemp()
    try:
        ext = (file_extension or "mp4").lstrip(".")
        video_path = os.path.join(tmp_dir, f"input.{ext}")
        with open(video_path, "wb") as f:
            f.write(base64.b64decode(video_b64))

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
            jobs[job_id] = {"status": "error", "detail": f"Could not read video: {stderr_tail}"}
            return

        if len(frame_paths) > MAX_VIDEO_FRAMES:
            jobs[job_id] = {
                "status": "error",
                "detail": (
                    f"Recording produced {len(frame_paths)} frames at {VIDEO_FPS}fps — "
                    f"keep screen recordings under {int(MAX_VIDEO_FRAMES / VIDEO_FPS)} seconds"
                ),
            }
            return

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

        jobs[job_id] = {"status": "done", "text": "\n\n---\n\n".join(per_image), "perImage": per_image}
    except Exception as e:
        jobs[job_id] = {"status": "error", "detail": str(e)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/ocr-video")
def ocr_video(req: OcrVideoRequest, request: Request, background_tasks: BackgroundTasks):
    if SHARED_SECRET and request.headers.get("x-ocr-secret") != SHARED_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "pending"}
    background_tasks.add_task(_process_video_job, job_id, req.video, req.fileExtension)
    return {"jobId": job_id, "status": "pending"}


@app.get("/ocr-video/status/{job_id}")
def ocr_video_status(job_id: str, request: Request):
    if SHARED_SECRET and request.headers.get("x-ocr-secret") != SHARED_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job id")
    return {"jobId": job_id, **job}
