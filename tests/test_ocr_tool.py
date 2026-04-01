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
