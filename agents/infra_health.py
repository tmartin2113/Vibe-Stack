"""
Infrastructure health probe module.

Provides health-check probing for all Vibe Stack infrastructure services.
Uses only stdlib — no external dependencies.

Usage:
    from agents.infra_health import check_all, check_service

    # Probe all registered services concurrently
    result = check_all(timeout=3)
    # => {"status": "ok"|"degraded"|"error", "timestamp": "...", "services": {...}, "summary": {...}}

    # Probe a single service by name
    result = check_service("ollama", timeout=3)
    # => {"status": "ok"|"degraded"|"error", "service": "ollama", "version": ..., ...}
"""

import json
import logging
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------

SERVICE_REGISTRY: Dict[str, Dict[str, str]] = {
    "mirofish": {
        "url": "http://mirofish:5001/health",
        "label": "MiroFish Simulation",
    },
    "paddleocr": {
        "url": "http://paddleocr:8868/health",
        "label": "PaddleOCR",
    },
    "searxng": {
        "url": "http://searxng:8080/healthz",
        "label": "SearXNG Search",
    },
    "playwright": {
        "url": "http://playwright:3003/json",
        "label": "Playwright Browser",
    },
    "penpot-backend": {
        "url": "http://penpot-backend:6060/",
        "label": "Penpot Backend",
    },
    "penpot-frontend": {
        "url": "http://penpot-frontend:80/",
        "label": "Penpot Frontend",
    },
    "minio": {
        "url": "http://minio:9000/minio/health/live",
        "label": "MinIO Object Storage",
    },
    "gitea": {
        "url": "http://gitea:3000/api/v1/version",
        "label": "Gitea Git Server",
    },
    "ollama": {
        "url": "http://host.docker.internal:11434/api/tags",
        "label": "Ollama LLM",
    },
    "prometheus": {
        "url": "http://prometheus:9090/-/healthy",
        "label": "Prometheus Monitoring",
    },
    "grafana": {
        "url": "http://grafana:3000/api/health",
        "label": "Grafana Dashboards",
    },
}

# Top-level fields that get promoted out of the raw response
_PROMOTED_KEYS = {"status", "version", "uptime_seconds"}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_response(
    service_name: str,
    raw_dict_or_none: Optional[Dict[str, Any]],
    http_status: int,
) -> Dict[str, Any]:
    """Normalize any service health response to a canonical shape.

    Returns:
        {
            "status":          "ok" | "degraded" | "error",
            "service":         <service_name>,
            "version":         <str or None>,
            "uptime_seconds":  <number or None>,
            "checks":          {<remaining fields>},
        }
    """
    result: Dict[str, Any] = {
        "status": "error",
        "service": service_name,
        "version": None,
        "uptime_seconds": None,
        "checks": {},
    }

    if raw_dict_or_none is None:
        return result

    raw = raw_dict_or_none

    # Determine status from HTTP code first, then from body
    if 200 <= http_status < 300:
        body_status = raw.get("status", "ok")
        if body_status in ("ok", "degraded", "error"):
            result["status"] = body_status
        else:
            result["status"] = "ok"
    else:
        result["status"] = "error"

    # Promote well-known fields
    if "version" in raw:
        result["version"] = raw["version"]
    if "uptime_seconds" in raw:
        result["uptime_seconds"] = raw["uptime_seconds"]

    # Everything else goes into checks
    for key, value in raw.items():
        if key not in _PROMOTED_KEYS:
            result["checks"][key] = value

    return result


# ---------------------------------------------------------------------------
# Single-service probe
# ---------------------------------------------------------------------------


def probe_service(name: str, url: str, timeout: int = 3) -> Dict[str, Any]:
    """HTTP GET a service health endpoint and return a normalized result.

    On connection failure / timeout, returns an error-status dict with the
    error message in ``checks.error``.
    """
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            http_status = resp.status
            try:
                body = json.loads(resp.read().decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {}
            return normalize_response(name, body, http_status)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:
            body = {}
        return normalize_response(name, body, exc.code)
    except Exception as exc:
        return {
            "status": "error",
            "service": name,
            "version": None,
            "uptime_seconds": None,
            "checks": {"error": str(exc)},
        }


# ---------------------------------------------------------------------------
# Aggregate check
# ---------------------------------------------------------------------------


def check_all(timeout: int = 3) -> Dict[str, Any]:
    """Probe all registered services concurrently.

    Returns:
        {
            "status":    "ok" | "degraded" | "error",
            "timestamp": "<ISO 8601>",
            "services":  {<name>: <normalized>, ...},
            "summary":   {"total": N, "ok": N, "degraded": N, "error": N},
        }
    """
    services: Dict[str, Dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=min(len(SERVICE_REGISTRY), 16)) as pool:
        futures = {
            pool.submit(probe_service, name, entry["url"], timeout): name
            for name, entry in SERVICE_REGISTRY.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                services[name] = future.result()
            except Exception as exc:
                logger.warning("Probe %s raised: %s", name, exc)
                services[name] = {
                    "status": "error",
                    "service": name,
                    "version": None,
                    "uptime_seconds": None,
                    "checks": {"error": str(exc)},
                }

    # Compute summary
    summary = {"total": len(services), "ok": 0, "degraded": 0, "error": 0}
    for svc_result in services.values():
        s = svc_result.get("status", "error")
        if s in summary:
            summary[s] += 1
        else:
            summary["error"] += 1

    # Overall status: error > degraded > ok
    if summary["error"] > 0:
        overall = "error"
    elif summary["degraded"] > 0:
        overall = "degraded"
    else:
        overall = "ok"

    return {
        "status": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": services,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Single named-service check
# ---------------------------------------------------------------------------


def check_service(name: str, timeout: int = 3) -> Dict[str, Any]:
    """Probe a single service by registry name.

    Returns an error dict if the name is not in the registry.
    """
    entry = SERVICE_REGISTRY.get(name)
    if entry is None:
        return {
            "status": "error",
            "service": name,
            "version": None,
            "uptime_seconds": None,
            "checks": {"error": f"Service '{name}' not found in registry"},
        }
    return probe_service(name, entry["url"], timeout=timeout)
