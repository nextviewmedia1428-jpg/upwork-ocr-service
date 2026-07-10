import base64
import io
import os

from fastapi import FastAPI, HTTPException, Request
from PIL import Image
from pydantic import BaseModel
import pytesseract

app = FastAPI()

SHARED_SECRET = os.environ.get("OCR_SHARED_SECRET", "")


class OcrRequest(BaseModel):
    images: list[str]  # base64-encoded image strings, no data: URI prefix


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
