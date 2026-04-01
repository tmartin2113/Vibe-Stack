"""
PaddleOCR API Server

Thin FastAPI wrapper around the paddleocr Python API.
Single /ocr endpoint handles all three modes: ocr, layout, table.
"""

import base64
import io
import logging
import tempfile
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from paddleocr import PaddleOCR, PPStructure

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="PaddleOCR API", version="1.0.0")

# Initialize models at startup (one-time download + load)
ocr_engine = None
structure_engine = None


@app.on_event("startup")
def load_models():
    global ocr_engine, structure_engine
    logger.info("Loading PaddleOCR models...")
    ocr_engine = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    structure_engine = PPStructure(show_log=False, layout=True)
    logger.info("Models loaded successfully")


class OCRRequest(BaseModel):
    image: str  # base64-encoded image or PDF bytes
    mode: str = "ocr"  # "ocr" | "layout" | "table"
    lang: str = "en"
    is_pdf: bool = False
    page_start: int = 0
    page_end: Optional[int] = None


class OCRResponse(BaseModel):
    success: bool
    results: list
    error: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok", "models_loaded": ocr_engine is not None}


@app.post("/ocr", response_model=OCRResponse)
def run_ocr(req: OCRRequest):
    try:
        # Decode base64 to bytes
        img_bytes = base64.b64decode(req.image)

        # Write to temp file (paddleocr works best with file paths)
        suffix = ".pdf" if req.is_pdf else ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(img_bytes)
            tmp_path = f.name

        if req.mode == "ocr":
            # Reinitialize with requested language if not English
            engine = ocr_engine
            if req.lang != "en":
                engine = PaddleOCR(use_angle_cls=True, lang=req.lang, show_log=False)

            if req.is_pdf:
                result = engine.ocr(
                    tmp_path, cls=True,
                    page_num=req.page_end or 5,
                )
            else:
                result = engine.ocr(tmp_path, cls=True)

            # Format results: list of {text, confidence, bbox}
            formatted = []
            pages = result if isinstance(result, list) else [result]
            for page_idx, page in enumerate(pages):
                if page is None:
                    continue
                for line in page:
                    if line and len(line) >= 2:
                        bbox = line[0]
                        text, confidence = line[1]
                        formatted.append({
                            "text": text,
                            "confidence": round(confidence, 4),
                            "bbox": bbox,
                            "page": page_idx,
                        })

            return OCRResponse(success=True, results=formatted)

        elif req.mode in ("layout", "table"):
            result = structure_engine(tmp_path)

            formatted = []
            for item in result:
                entry = {
                    "type": item.get("type", "unknown"),
                    "bbox": item.get("bbox", []),
                }
                # Layout mode: include region type and any text
                if req.mode == "layout":
                    res = item.get("res", [])
                    if isinstance(res, list):
                        texts = []
                        for r in res:
                            if isinstance(r, dict) and "text" in r:
                                texts.append(r["text"])
                            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                                texts.append(str(r[1][0]) if isinstance(r[1], tuple) else str(r[1]))
                        entry["text"] = " ".join(texts)
                    elif isinstance(res, dict) and "html" in res:
                        entry["html"] = res["html"]

                # Table mode: include HTML
                if req.mode == "table":
                    if item.get("type") != "table":
                        continue
                    res = item.get("res", {})
                    if isinstance(res, dict) and "html" in res:
                        entry["html"] = res["html"]

                formatted.append(entry)

            return OCRResponse(success=True, results=formatted)

        else:
            return OCRResponse(
                success=False, results=[],
                error=f"Unknown mode: {req.mode}. Use 'ocr', 'layout', or 'table'.",
            )

    except Exception as e:
        logger.exception("OCR processing failed")
        return OCRResponse(success=False, results=[], error=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8868)
