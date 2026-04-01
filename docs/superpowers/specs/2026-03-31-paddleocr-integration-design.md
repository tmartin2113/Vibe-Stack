# PaddleOCR Integration Design Spec

**Date:** 2026-03-31
**Goal:** Add PaddleOCR as an infrastructure service so all agents can extract text, analyze layout, and parse tables from images and PDFs.

---

## Architecture

PaddleOCR runs as a single stateless Docker container (CPU-only, port 8868) on the compose default network. Agents invoke it via a new `OCRTool` that accepts file paths or URLs, detects the input type (image vs PDF), base64-encodes the content, and POSTs to PaddleOCR's HTTP API.

```
Agent → OCRTool → base64 encode → POST http://paddleocr:8868/predict/ocr_system → parsed result
```

CPU-only avoids GPU contention with vLLM. OCR inference is fast on CPU — typical latency is <2s per image.

## Tool API

```python
OCRTool.execute(
    source: str,       # File path or URL (required)
    mode: str,         # "ocr" | "layout" | "table" (default: "ocr")
    language: str,     # Language hint (default: "en")
    page_range: str,   # For PDFs only: "1-5", "3", etc. (optional)
)
```

### Input Handling

- **File path** — read from disk, base64-encode, detect MIME type from extension
- **URL** — fetch via `requests.get`, base64-encode the response body
- **PDF detection** — if extension is `.pdf` or MIME is `application/pdf`, pass `page_range` to PaddleOCR. If no `page_range` given for a PDF, default to first 5 pages to avoid unbounded processing

### Output by Mode

**`mode=ocr`** (default):
```
Extracted text from <source>:

Line 1: "detected text" (confidence: 0.97)
Line 2: "more text" (confidence: 0.94)
...
```

**`mode=layout`**:
```
Layout analysis of <source>:

Region 1: [text] (bbox: x1,y1,x2,y2)
  "text content in this region..."
Region 2: [table] (bbox: x1,y1,x2,y2)
Region 3: [figure] (bbox: x1,y1,x2,y2)
...
```

**`mode=table`**:
```
Tables extracted from <source>:

Table 1:
<table><tr><td>Header 1</td><td>Header 2</td></tr>...</table>

Table 2:
<table>...</table>
```

### Error Cases

- Source file not found → `ToolResult(success=False, error="File not found: ...")`
- URL fetch fails → `ToolResult(success=False, error="Failed to fetch URL: ...")`
- PaddleOCR service unreachable → `ToolResult(success=False, error="OCR service unavailable: ...")`
- Unsupported file type → `ToolResult(success=False, error="Unsupported file type: ...")`
- PDF with no page_range and >20 pages → default to pages 1-5, note in output

## Docker Service

```yaml
paddleocr:
  image: paddleocr:cpu
  build:
    context: ./deploy/paddleocr
    dockerfile: Dockerfile
  restart: unless-stopped
  ports:
    - "8868:8868"
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8868/predict/ocr_system"]
    interval: 15s
    timeout: 10s
    retries: 5
    start_period: 30s
```

Note: PaddleOCR doesn't publish an official Docker image on GHCR/DockerHub for the serving mode. We'll need a lightweight Dockerfile that installs `paddlepaddle`, `paddleocr`, and `paddlehub`, then starts the hub serving. This keeps the build self-contained.

### Dockerfile (`deploy/paddleocr/Dockerfile`)

```dockerfile
FROM python:3.11-slim

RUN pip install --no-cache-dir paddlepaddle paddleocr paddlehub==2.1.0

# Install the ocr_system module
RUN hub install deploy/hubserving/ocr_system || true

EXPOSE 8868

CMD ["hub", "serving", "start", "-m", "ocr_system", "--port", "8868"]
```

The exact Dockerfile will need verification against PaddleOCR's current install process — the plan should include a step to validate the build.

## Tool Registration

Register `OCRTool` for **all roles** in both `create_default_tool_registry` and `create_subprocess_tool_registry`, env-gated on `PADDLEOCR_URL`:

```python
if os.environ.get("PADDLEOCR_URL"):
    from .ocr_tool import OCRTool
    registry.register(OCRTool())
```

Add `"OCRTool"` to every role's frozenset in `ROLE_TOOL_SETS` (frontend, backend, QA, UX, security). CTO already gets all tools via `None`.

## Environment Variables

```env
# ── PaddleOCR ─────────────────────────────────────────────────
# PADDLEOCR_URL=http://paddleocr:8868    # PaddleOCR service URL
# PADDLEOCR_TIMEOUT=30                   # Request timeout in seconds
# PADDLEOCR_MAX_PDF_PAGES=5              # Default max pages for PDFs without page_range
```

## File Changes

| File | Action |
|------|--------|
| `docker-compose.infra.yml` | Add `paddleocr` service |
| `deploy/paddleocr/Dockerfile` | Create — installs paddleocr + hub serving |
| `agents/tools/ocr_tool.py` | Create — OCRTool implementation |
| `tests/test_ocr_tool.py` | Create — tests for all modes, input types, errors |
| `agents/tools/registry.py` | Register OCRTool for all roles |
| `.env.example` | Add `PADDLEOCR_*` env vars |
| `README.md` | Add PaddleOCR to infrastructure table |
| `CLAUDE.md` | Add OCR subsystem entry |

## Supported File Types

Images: `.png`, `.jpg`, `.jpeg`, `.bmp`, `.webp`, `.tiff`
Documents: `.pdf`

## Design Decisions

- **CPU-only** — OCR is fast on CPU, avoids GPU contention with vLLM. No `docker-compose.gpu.yml` changes needed.
- **All roles** — every engineer benefits: QA reads error screenshots, frontend reads mockups, backend reads API docs, CTO reads diagrams.
- **URL support** — Paperclip issue attachments are often URLs. Agents shouldn't need a separate download step before OCR.
- **Default page limit for PDFs** — prevents unbounded processing when an agent OCRs a 200-page PDF. Configurable via env var.
- **Build from Dockerfile** — no official serving image exists. A thin Dockerfile keeps the dependency contained and version-pinnable.
