# PaddleOCR Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add PaddleOCR as a CPU-only Docker service with an OCRTool available to all agent roles, supporting text extraction, layout analysis, and table parsing from images and PDFs.

**Architecture:** PaddleOCR runs as a Docker service with a thin FastAPI wrapper exposing a single `/ocr` endpoint that handles all three modes. Agents invoke it via an `OCRTool` that reads file paths or URLs, base64-encodes the content, and POSTs to the service. Hub serving is avoided — a custom server gives us one port, one endpoint, all modes.

**Tech Stack:** Docker, Python, PaddleOCR, FastAPI, uvicorn

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `deploy/paddleocr/Dockerfile` | Create | Container image with paddleocr + FastAPI server |
| `deploy/paddleocr/server.py` | Create | FastAPI wrapper around paddleocr Python API |
| `deploy/paddleocr/requirements.txt` | Create | Python dependencies for the server |
| `docker-compose.infra.yml` | Modify | Add paddleocr service |
| `agents/tools/ocr_tool.py` | Create | OCRTool implementation |
| `tests/test_ocr_tool.py` | Create | Tests for all modes, input types, error cases |
| `agents/tools/registry.py` | Modify | Register OCRTool for all roles |
| `.env.example` | Modify | Add PADDLEOCR_* env vars |
| `README.md` | Modify | Add PaddleOCR to infrastructure table |
| `CLAUDE.md` | Modify | Add OCR subsystem entry |

---

### Task 1: Create PaddleOCR Docker service

**Files:**
- Create: `deploy/paddleocr/requirements.txt`
- Create: `deploy/paddleocr/server.py`
- Create: `deploy/paddleocr/Dockerfile`
- Modify: `docker-compose.infra.yml`

- [ ] **Step 1: Create the deploy directory**

```bash
mkdir -p deploy/paddleocr
```

- [ ] **Step 2: Create requirements.txt**

Create `deploy/paddleocr/requirements.txt`:

```
paddlepaddle==3.0.0
paddleocr==3.0.0
fastapi==0.115.0
uvicorn==0.30.0
python-multipart==0.0.9
```

- [ ] **Step 3: Create the FastAPI server**

Create `deploy/paddleocr/server.py`:

```python
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
```

- [ ] **Step 4: Create the Dockerfile**

Create `deploy/paddleocr/Dockerfile`:

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Pre-download models at build time so startup is fast
RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(use_angle_cls=True, lang='en', show_log=False)"
RUN python -c "from paddleocr import PPStructure; PPStructure(show_log=False, layout=True)"

EXPOSE 8868

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8868"]
```

- [ ] **Step 5: Add paddleocr service to docker-compose.infra.yml**

Add before the SSH Relay service:

```yaml
  # ── PaddleOCR (text extraction from images and PDFs) ──────────────────
  paddleocr:
    build:
      context: ./deploy/paddleocr
      dockerfile: Dockerfile
    restart: unless-stopped
    ports:
      - "8868:8868"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8868/health"]
      interval: 15s
      timeout: 10s
      retries: 5
      start_period: 30s
```

- [ ] **Step 6: Validate compose config**

Run: `cd ~/Repos/Vibe-Stack && docker compose -f docker-compose.infra.yml config --quiet`
Expected: Exit 0, no errors.

- [ ] **Step 7: Commit**

```bash
git add deploy/paddleocr/ docker-compose.infra.yml
git commit -m "infra: add PaddleOCR service with FastAPI wrapper

CPU-only OCR on port 8868. Custom FastAPI server wraps paddleocr
Python API for ocr/layout/table modes on a single endpoint.
Pre-downloads models at build time for fast startup."
```

---

### Task 2: Create the OCRTool

**Files:**
- Create: `tests/test_ocr_tool.py`
- Create: `agents/tools/ocr_tool.py`

- [ ] **Step 1: Write the test file**

Create `tests/test_ocr_tool.py`:

```python
"""Tests for OCRTool."""

import base64
import os
from unittest.mock import MagicMock, patch

import pytest

from agents.tools.ocr_tool import OCRTool


class TestInputDetection:
    """Test source type detection (file path vs URL) and file type detection."""

    def test_detects_file_path(self):
        tool = OCRTool()
        assert tool._is_url("/tmp/screenshot.png") is False

    def test_detects_http_url(self):
        tool = OCRTool()
        assert tool._is_url("http://example.com/image.png") is True

    def test_detects_https_url(self):
        tool = OCRTool()
        assert tool._is_url("https://example.com/image.png") is True

    def test_detects_pdf_by_extension(self):
        tool = OCRTool()
        assert tool._is_pdf("/tmp/spec.pdf") is True

    def test_detects_image_not_pdf(self):
        tool = OCRTool()
        assert tool._is_pdf("/tmp/screenshot.png") is False

    def test_supported_image_extensions(self):
        tool = OCRTool()
        for ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff"):
            assert tool._is_supported(f"/tmp/file{ext}") is True

    def test_unsupported_extension(self):
        tool = OCRTool()
        assert tool._is_supported("/tmp/file.mp4") is False

    def test_pdf_is_supported(self):
        tool = OCRTool()
        assert tool._is_supported("/tmp/doc.pdf") is True


class TestOCRExecution:
    """Test the tool's execute method."""

    @patch("agents.tools.ocr_tool.requests.post")
    @patch("agents.tools.ocr_tool.os.path.isfile", return_value=True)
    @patch("builtins.open", create=True)
    def test_successful_ocr_from_file(self, mock_open, mock_isfile, mock_post):
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        mock_open.return_value.read = MagicMock(return_value=b"fake image bytes")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "results": [
                {"text": "Hello World", "confidence": 0.98, "bbox": [[0,0],[100,0],[100,30],[0,30]], "page": 0},
            ],
            "error": None,
        }
        mock_post.return_value = mock_response

        tool = OCRTool()
        result = tool.execute(source="/tmp/test.png")
        assert result.success is True
        assert "Hello World" in result.output
        assert "0.98" in result.output

    @patch("agents.tools.ocr_tool.requests.post")
    @patch("agents.tools.ocr_tool.requests.get")
    def test_successful_ocr_from_url(self, mock_get, mock_post):
        mock_get_response = MagicMock()
        mock_get_response.status_code = 200
        mock_get_response.content = b"fake image bytes"
        mock_get_response.headers = {"Content-Type": "image/png"}
        mock_get.return_value = mock_get_response

        mock_post_response = MagicMock()
        mock_post_response.status_code = 200
        mock_post_response.json.return_value = {
            "success": True,
            "results": [
                {"text": "URL text", "confidence": 0.95, "bbox": [[0,0],[50,0],[50,20],[0,20]], "page": 0},
            ],
            "error": None,
        }
        mock_post.return_value = mock_post_response

        tool = OCRTool()
        result = tool.execute(source="https://example.com/image.png")
        assert result.success is True
        assert "URL text" in result.output

    @patch("agents.tools.ocr_tool.requests.post")
    @patch("agents.tools.ocr_tool.os.path.isfile", return_value=True)
    @patch("builtins.open", create=True)
    def test_layout_mode(self, mock_open, mock_isfile, mock_post):
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        mock_open.return_value.read = MagicMock(return_value=b"fake image bytes")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "results": [
                {"type": "text", "bbox": [10, 20, 300, 100], "text": "Some paragraph"},
                {"type": "table", "bbox": [10, 120, 300, 400]},
                {"type": "figure", "bbox": [10, 420, 300, 600]},
            ],
            "error": None,
        }
        mock_post.return_value = mock_response

        tool = OCRTool()
        result = tool.execute(source="/tmp/doc.png", mode="layout")
        assert result.success is True
        assert "text" in result.output
        assert "table" in result.output
        assert "figure" in result.output

    @patch("agents.tools.ocr_tool.requests.post")
    @patch("agents.tools.ocr_tool.os.path.isfile", return_value=True)
    @patch("builtins.open", create=True)
    def test_table_mode(self, mock_open, mock_isfile, mock_post):
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        mock_open.return_value.read = MagicMock(return_value=b"fake image bytes")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "results": [
                {"type": "table", "bbox": [10, 10, 500, 300], "html": "<table><tr><td>A</td><td>B</td></tr></table>"},
            ],
            "error": None,
        }
        mock_post.return_value = mock_response

        tool = OCRTool()
        result = tool.execute(source="/tmp/table.png", mode="table")
        assert result.success is True
        assert "<table>" in result.output

    def test_missing_source(self):
        tool = OCRTool()
        result = tool.execute()
        assert result.success is False
        assert "source is required" in result.error

    def test_file_not_found(self):
        tool = OCRTool()
        result = tool.execute(source="/nonexistent/file.png")
        assert result.success is False
        assert "not found" in result.error.lower() or "No such file" in result.error

    def test_unsupported_file_type(self):
        tool = OCRTool()
        result = tool.execute(source="/tmp/video.mp4")
        assert result.success is False
        assert "Unsupported" in result.error

    @patch("agents.tools.ocr_tool.requests.post")
    @patch("agents.tools.ocr_tool.os.path.isfile", return_value=True)
    @patch("builtins.open", create=True)
    def test_service_error(self, mock_open, mock_isfile, mock_post):
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        mock_open.return_value.read = MagicMock(return_value=b"fake image bytes")

        mock_post.side_effect = Exception("Connection refused")

        tool = OCRTool()
        result = tool.execute(source="/tmp/test.png")
        assert result.success is False
        assert "Connection refused" in result.error

    @patch("agents.tools.ocr_tool.requests.get")
    def test_url_fetch_failure(self, mock_get):
        mock_get.side_effect = Exception("DNS resolution failed")

        tool = OCRTool()
        result = tool.execute(source="https://broken.example.com/img.png")
        assert result.success is False
        assert "DNS resolution failed" in result.error

    def test_invalid_mode(self):
        tool = OCRTool()
        # Invalid mode should still be sent to the server (server validates)
        # but source must exist first — test with missing source
        result = tool.execute(source="/tmp/nonexistent.png", mode="invalid")
        assert result.success is False


class TestToolMetadata:
    """Test tool registration metadata."""

    def test_tool_name(self):
        tool = OCRTool()
        assert tool.name == "OCRTool"

    def test_tool_category(self):
        from agents.tools.base import ToolCategory
        tool = OCRTool()
        assert tool.category == ToolCategory.EXTERNAL_SERVICE

    def test_parameter_schema(self):
        tool = OCRTool()
        schema = tool._get_parameters_schema()
        assert "source" in schema["properties"]
        assert "mode" in schema["properties"]
        assert "language" in schema["properties"]
        assert "page_range" in schema["properties"]
        assert schema["required"] == ["source"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Repos/Vibe-Stack && python3 -m pytest tests/test_ocr_tool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.tools.ocr_tool'`

- [ ] **Step 3: Write the OCRTool implementation**

Create `agents/tools/ocr_tool.py`:

```python
"""
OCR Tool

Extracts text, analyzes layout, and parses tables from images and PDFs
via the PaddleOCR service.

Modes:
  - ocr:    text detection + recognition (default)
  - layout: document region analysis (text, table, figure, formula)
  - table:  table extraction as HTML
"""

import base64
import logging
import os
from pathlib import Path
from typing import Any, Dict

import requests

from .base import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".pdf",
}

_PADDLEOCR_URL = os.getenv("PADDLEOCR_URL", "http://paddleocr:8868")
_TIMEOUT = int(os.getenv("PADDLEOCR_TIMEOUT", "30"))
_MAX_PDF_PAGES = int(os.getenv("PADDLEOCR_MAX_PDF_PAGES", "5"))


class OCRTool(Tool):
    """Extract text, layout, or tables from images and PDFs."""

    def __init__(self):
        super().__init__(
            name="OCRTool",
            description=(
                "Extract text from images and PDFs using OCR. Supports three modes: "
                "'ocr' for text recognition, 'layout' for document region analysis "
                "(text blocks, tables, figures), and 'table' for extracting tables "
                "as HTML. Accepts file paths or URLs."
            ),
            category=ToolCategory.EXTERNAL_SERVICE,
        )

    @staticmethod
    def _is_url(source: str) -> bool:
        return source.startswith("http://") or source.startswith("https://")

    @staticmethod
    def _is_pdf(source: str) -> bool:
        return Path(source.split("?")[0]).suffix.lower() == ".pdf"

    @staticmethod
    def _is_supported(source: str) -> bool:
        ext = Path(source.split("?")[0]).suffix.lower()
        return ext in _SUPPORTED_EXTENSIONS

    def _read_source(self, source: str) -> bytes:
        """Read image/PDF bytes from a file path or URL."""
        if self._is_url(source):
            resp = requests.get(source, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.content
        else:
            if not os.path.isfile(source):
                raise FileNotFoundError(f"File not found: {source}")
            with open(source, "rb") as f:
                return f.read()

    def _format_ocr_results(self, results: list, source: str) -> str:
        """Format OCR results as readable text."""
        if not results:
            return f"No text detected in {source}"
        lines = [f"Extracted text from {source}:\n"]
        for item in results:
            text = item.get("text", "")
            confidence = item.get("confidence", 0)
            lines.append(f'  "{text}" (confidence: {confidence})')
        return "\n".join(lines)

    def _format_layout_results(self, results: list, source: str) -> str:
        """Format layout analysis results."""
        if not results:
            return f"No layout regions detected in {source}"
        lines = [f"Layout analysis of {source}:\n"]
        for i, item in enumerate(results, 1):
            region_type = item.get("type", "unknown")
            bbox = item.get("bbox", [])
            text = item.get("text", "")
            lines.append(f"Region {i}: [{region_type}] (bbox: {bbox})")
            if text:
                lines.append(f"  {text}")
        return "\n".join(lines)

    def _format_table_results(self, results: list, source: str) -> str:
        """Format table extraction results."""
        tables = [r for r in results if r.get("html")]
        if not tables:
            return f"No tables detected in {source}"
        lines = [f"Tables extracted from {source}:\n"]
        for i, item in enumerate(tables, 1):
            lines.append(f"Table {i}:")
            lines.append(item["html"])
            lines.append("")
        return "\n".join(lines)

    def execute(self, **kwargs) -> ToolResult:
        """Execute OCR on an image or PDF."""
        source = kwargs.get("source", "")
        mode = kwargs.get("mode", "ocr")
        language = kwargs.get("language", "en")
        page_range = kwargs.get("page_range", "")

        if not source:
            return ToolResult(success=False, output="", error="source is required")

        if not self._is_supported(source):
            ext = Path(source.split("?")[0]).suffix
            return ToolResult(
                success=False, output="",
                error=f"Unsupported file type: {ext}. Supported: {sorted(_SUPPORTED_EXTENSIONS)}",
            )

        # Read the source bytes
        try:
            img_bytes = self._read_source(source)
        except FileNotFoundError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"Failed to read source: {e}",
            )

        # Build request
        is_pdf = self._is_pdf(source)
        page_end = _MAX_PDF_PAGES
        if page_range and is_pdf:
            # Parse "3-7" or "5" format
            parts = page_range.split("-")
            page_end = int(parts[-1]) if parts else _MAX_PDF_PAGES

        payload = {
            "image": base64.b64encode(img_bytes).decode("utf-8"),
            "mode": mode,
            "lang": language,
            "is_pdf": is_pdf,
            "page_start": 0,
            "page_end": page_end if is_pdf else None,
        }

        try:
            response = requests.post(
                f"{_PADDLEOCR_URL}/ocr",
                json=payload,
                timeout=_TIMEOUT,
            )

            if response.status_code != 200:
                return ToolResult(
                    success=False, output="",
                    error=f"OCR service returned {response.status_code}: {response.text[:500]}",
                )

            data = response.json()

            if not data.get("success"):
                return ToolResult(
                    success=False, output="",
                    error=f"OCR processing failed: {data.get('error', 'unknown error')}",
                )

            results = data.get("results", [])

            # Format based on mode
            if mode == "ocr":
                output = self._format_ocr_results(results, source)
            elif mode == "layout":
                output = self._format_layout_results(results, source)
            elif mode == "table":
                output = self._format_table_results(results, source)
            else:
                output = str(results)

            return ToolResult(
                success=True,
                output=output,
                metadata={"mode": mode, "result_count": len(results), "source": source},
            )

        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"OCR request failed: {e}",
            )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": (
                        "File path or URL of the image/PDF to process. "
                        "Supported formats: PNG, JPG, JPEG, BMP, WEBP, TIFF, PDF."
                    ),
                },
                "mode": {
                    "type": "string",
                    "description": (
                        "Processing mode: 'ocr' (text extraction, default), "
                        "'layout' (document region analysis), "
                        "'table' (extract tables as HTML)."
                    ),
                    "default": "ocr",
                    "enum": ["ocr", "layout", "table"],
                },
                "language": {
                    "type": "string",
                    "description": "Language hint for OCR (default: 'en'). Supports 100+ languages.",
                    "default": "en",
                },
                "page_range": {
                    "type": "string",
                    "description": "Page range for PDFs (e.g. '1-5', '3'). Defaults to first 5 pages.",
                    "default": "",
                },
            },
            "required": ["source"],
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Repos/Vibe-Stack && python3 -m pytest tests/test_ocr_tool.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tools/ocr_tool.py tests/test_ocr_tool.py
git commit -m "feat: add OCRTool with text/layout/table modes

Supports file paths and URLs. Images + PDFs. Three modes:
ocr (text extraction), layout (region analysis), table (HTML).
14 tests covering all modes, input types, and error cases."
```

---

### Task 3: Register OCRTool in the tool registry

**Files:**
- Modify: `agents/tools/registry.py`

- [ ] **Step 1: Add OCRTool to all role tool sets**

In `ROLE_TOOL_SETS`, add `"OCRTool"` to every role's frozenset:

In `frontend_engineer`, add after `"image_generation"`:
```python
        # OCR
        "OCRTool",
```

In `backend_engineer`, add after `"MiroFishSimulation"`:
```python
        # OCR
        "OCRTool",
```

In `qa_engineer`, add after `"MiroFishSimulation"`:
```python
        # OCR
        "OCRTool",
```

In `ux_engineer`, add after `"image_generation"`:
```python
        # OCR
        "OCRTool",
```

In `security_engineer`, add after `"dependency_scanner"`:
```python
        # OCR
        "OCRTool",
```

- [ ] **Step 2: Register in create_default_tool_registry**

Add after the MiroFish registration block:

```python
    if os.environ.get("PADDLEOCR_URL"):
        from .ocr_tool import OCRTool
        registry.register(OCRTool())
        logger.info("Tool registry: OCRTool enabled")
```

- [ ] **Step 3: Register in create_subprocess_tool_registry**

Add after the MiroFish registration block:

```python
    if os.environ.get("PADDLEOCR_URL"):
        from .ocr_tool import OCRTool
        registry.register(OCRTool())
```

- [ ] **Step 4: Run tool system tests**

Run: `cd ~/Repos/Vibe-Stack && python3 -m pytest tests/test_tool_system.py -v -x`
Expected: All existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tools/registry.py
git commit -m "feat: register OCRTool for all agent roles

Env-gated on PADDLEOCR_URL. Available to all roles — every
engineer benefits from reading images and PDFs."
```

---

### Task 4: Update documentation and env vars

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add PaddleOCR env vars to .env.example**

Add after the Zep section at the end of the file:

```env

# ── PaddleOCR (text extraction from images and PDFs) ──────────
# PADDLEOCR_URL=http://paddleocr:8868    # PaddleOCR service URL
# PADDLEOCR_TIMEOUT=30                   # Request timeout in seconds
# PADDLEOCR_MAX_PDF_PAGES=5              # Default max pages for PDFs without page_range
```

- [ ] **Step 2: Add PaddleOCR to README.md infrastructure table**

Add after the Zep row:

```markdown
| PaddleOCR | OCR text extraction | 8868 |
```

- [ ] **Step 3: Add OCR entry to CLAUDE.md**

In the Key Subsystems table, add after the Simulation row:

```markdown
| **OCR** | `agents/tools/ocr_tool.py` | PaddleOCR text extraction, layout analysis, table parsing from images and PDFs. CPU-only Docker service on port 8868 |
```

- [ ] **Step 4: Commit**

```bash
git add .env.example README.md CLAUDE.md
git commit -m "docs: add PaddleOCR to env vars, README, and CLAUDE.md"
```

---

### Task 5: Build verification

- [ ] **Step 1: Build the PaddleOCR Docker image**

```bash
cd ~/Repos/Vibe-Stack
docker build -t paddleocr:cpu deploy/paddleocr/
```

Expected: Build completes successfully. Model download happens during build.

If the build fails (dependency issues, missing system libs), fix `deploy/paddleocr/Dockerfile` and `requirements.txt` and retry.

- [ ] **Step 2: Verify the service starts and responds**

```bash
docker run -d --name paddleocr-test -p 8868:8868 paddleocr:cpu
# Wait for startup
sleep 10
curl -f http://localhost:8868/health
```

Expected: `{"status":"ok","models_loaded":true}`

- [ ] **Step 3: Run the full test suite**

```bash
cd ~/Repos/Vibe-Stack && python3 -m pytest tests/ -x -m "not e2e" --no-header -q
```

Expected: All tests pass.

- [ ] **Step 4: Clean up test container**

```bash
docker stop paddleocr-test && docker rm paddleocr-test
```

- [ ] **Step 5: Commit any fixes**

If any fixes were needed during build verification:

```bash
git add -A deploy/paddleocr/
git commit -m "fix: adjust PaddleOCR Dockerfile for build issues"
```
