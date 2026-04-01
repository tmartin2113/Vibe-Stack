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
