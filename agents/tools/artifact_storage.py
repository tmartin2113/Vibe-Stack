"""MinIO Artifact Storage Tool — store and retrieve artifacts via S3-compatible API."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)

# Default MinIO bucket used when callers don't specify one. Module-level so it's
# in scope inside class methods that build the parameter schema (referencing it
# as a bare name from inside _get_parameters_schema previously caused a
# NameError that cascaded into every tool-registry schema build).
_DEFAULT_BUCKET = "vibe-artifacts"


class ArtifactStorageTool(Tool):
    """Store and retrieve artifacts using a self-hosted MinIO instance.

    Provides S3-compatible object storage for build artifacts, generated
    files, screenshots, exports, and other binary/text data produced
    during agent workflows.

    Requires ``MINIO_URL`` environment variable pointing to the MinIO
    instance (e.g. ``http://minio:9000``).
    """

    def __init__(self, base_url: Optional[str] = None):
        super().__init__(
            name="artifact_storage",
            description=(
                "Store and retrieve artifacts (files, images, exports) in S3-compatible "
                "object storage. Use for persisting build outputs, screenshots, "
                "generated code, and other artifacts."
            ),
            category=ToolCategory.EXTERNAL_SERVICE,
        )
        self._base_url = (base_url or os.environ.get("MINIO_URL", "")).rstrip("/")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "Storage action: put, get, list, delete, presign"
                    ),
                },
                "key": {
                    "type": "string",
                    "description": (
                        "Object key (path) in the bucket. "
                        "e.g. 'builds/2024-01/output.zip' or 'screenshots/page.png'"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Content to store (for 'put' action). Text content or base64 for binary.",
                },
                "content_type": {
                    "type": "string",
                    "description": "MIME type of the content (default: application/octet-stream)",
                    "default": "application/octet-stream",
                },
                "bucket": {
                    "type": "string",
                    "description": f"Bucket name (default: {_DEFAULT_BUCKET})",
                    "default": _DEFAULT_BUCKET,
                },
                "prefix": {
                    "type": "string",
                    "description": "Key prefix for listing objects (for 'list' action)",
                    "default": "",
                },
            },
            "required": ["action"],
        }

    def execute(
        self,
        action: str,
        key: str = "",
        content: str = "",
        content_type: str = "application/octet-stream",
        bucket: str = "",
        prefix: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        if not action or not action.strip():
            return ToolResult(success=False, output="", error="No action provided")
        if not self._base_url:
            return ToolResult(
                success=False, output="",
                error="MINIO_URL not set. Configure the MinIO service URL.",
            )

        bucket = bucket or self._DEFAULT_BUCKET
        valid_actions = {"put", "get", "list", "delete", "presign"}
        if action not in valid_actions:
            return ToolResult(
                success=False, output="",
                error=f"Invalid action '{action}'. Valid: {', '.join(sorted(valid_actions))}",
            )

        access_key = os.environ.get("MINIO_ROOT_USER", "vibe")
        secret_key = os.environ.get("MINIO_ROOT_PASSWORD", "")

        try:
            if action == "put":
                if not key:
                    return ToolResult(success=False, output="", error="key required")
                if not content:
                    return ToolResult(success=False, output="", error="content required")
                return self._put_object(bucket, key, content, content_type, access_key, secret_key)

            elif action == "get":
                if not key:
                    return ToolResult(success=False, output="", error="key required")
                return self._get_object(bucket, key, access_key, secret_key)

            elif action == "list":
                return self._list_objects(bucket, prefix, access_key, secret_key)

            elif action == "delete":
                if not key:
                    return ToolResult(success=False, output="", error="key required")
                return self._delete_object(bucket, key, access_key, secret_key)

            elif action == "presign":
                if not key:
                    return ToolResult(success=False, output="", error="key required")
                # For presigned URLs, return the direct URL (works within Docker network)
                url = f"{self._base_url}/{bucket}/{key}"
                return ToolResult(
                    success=True,
                    output=f"Direct URL: {url}",
                    metadata={"url": url, "bucket": bucket, "key": key},
                )

            return ToolResult(success=False, output="", error=f"Unhandled action: {action}")

        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"MinIO operation failed: {e}",
            )

    def _put_object(
        self, bucket: str, key: str, content: str,
        content_type: str, access_key: str, secret_key: str,
    ) -> ToolResult:
        """Upload an object to MinIO."""
        data = content.encode("utf-8")
        url = f"{self._base_url}/{bucket}/{key}"
        req = urllib.request.Request(url, data=data, method="PUT")
        req.add_header("Content-Type", content_type)
        self._add_auth(req, "PUT", bucket, key, access_key, secret_key)

        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()

        return ToolResult(
            success=True,
            output=f"Stored {len(data)} bytes at {bucket}/{key}",
            metadata={"bucket": bucket, "key": key, "size": len(data)},
        )

    def _get_object(
        self, bucket: str, key: str, access_key: str, secret_key: str,
    ) -> ToolResult:
        """Download an object from MinIO."""
        url = f"{self._base_url}/{bucket}/{key}"
        req = urllib.request.Request(url, method="GET")
        self._add_auth(req, "GET", bucket, key, access_key, secret_key)

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()

        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            import base64
            content = base64.b64encode(data).decode()

        return ToolResult(
            success=True,
            output=content,
            metadata={"bucket": bucket, "key": key, "size": len(data)},
        )

    def _list_objects(
        self, bucket: str, prefix: str, access_key: str, secret_key: str,
    ) -> ToolResult:
        """List objects in a MinIO bucket."""
        params = f"?prefix={urllib.parse.quote(prefix)}&max-keys=100" if prefix else "?max-keys=100"
        url = f"{self._base_url}/{bucket}{params}"
        req = urllib.request.Request(url, method="GET")
        self._add_auth(req, "GET", bucket, "", access_key, secret_key)

        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()

        # Parse the XML response for key names
        import re
        keys = re.findall(r"<Key>(.*?)</Key>", body)
        sizes = re.findall(r"<Size>(.*?)</Size>", body)

        items = []
        for i, k in enumerate(keys):
            size = sizes[i] if i < len(sizes) else "?"
            items.append(f"  {k}  ({size} bytes)")

        output = f"Objects in {bucket}/{prefix}:\n" + "\n".join(items) if items else f"No objects found in {bucket}/{prefix}"

        return ToolResult(
            success=True,
            output=output,
            metadata={"bucket": bucket, "prefix": prefix, "count": len(keys)},
        )

    def _delete_object(
        self, bucket: str, key: str, access_key: str, secret_key: str,
    ) -> ToolResult:
        """Delete an object from MinIO."""
        url = f"{self._base_url}/{bucket}/{key}"
        req = urllib.request.Request(url, method="DELETE")
        self._add_auth(req, "DELETE", bucket, key, access_key, secret_key)

        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()

        return ToolResult(
            success=True,
            output=f"Deleted {bucket}/{key}",
            metadata={"bucket": bucket, "key": key},
        )

    def _add_auth(
        self, req: urllib.request.Request, method: str,
        bucket: str, key: str, access_key: str, secret_key: str,
    ) -> None:
        """Add basic S3 authentication headers (S3v2 style for simplicity)."""
        if not access_key or not secret_key:
            return

        import hmac
        import base64

        date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        content_type = req.get_header("Content-type") or ""
        resource = f"/{bucket}/{key}" if key else f"/{bucket}/"

        string_to_sign = f"{method}\n\n{content_type}\n{date}\n{resource}"
        signature = base64.b64encode(
            hmac.new(
                secret_key.encode(), string_to_sign.encode(), hashlib.sha1
            ).digest()
        ).decode()

        req.add_header("Date", date)
        req.add_header("Authorization", f"AWS {access_key}:{signature}")
