"""ComfyUI Image Generation Tool — generate images via a local ComfyUI instance."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)


class ImageGenerationTool(Tool):
    """Generate images using a self-hosted ComfyUI instance with Stable Diffusion.

    Submits a workflow prompt to ComfyUI's API, polls for completion,
    and returns the generated image data.  ComfyUI shares the GPU with
    vLLM and uses Docker Compose profiles (``gpu-comfyui``) so it
    doesn't start by default.

    Requires ``COMFYUI_URL`` environment variable pointing to the
    ComfyUI instance (e.g. ``http://comfyui:8188``).
    """

    def __init__(self, base_url: Optional[str] = None):
        super().__init__(
            name="image_generation",
            description=(
                "Generate images using Stable Diffusion via ComfyUI. "
                "Provide a text prompt and optional parameters to create images. "
                "GPU-intensive — only available when ComfyUI profile is active."
            ),
            category=ToolCategory.EXTERNAL_SERVICE,
        )
        self._base_url = (base_url or os.environ.get("COMFYUI_URL", "")).rstrip("/")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Text description of the image to generate",
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "What to avoid in the image (default: '')",
                    "default": "",
                },
                "width": {
                    "type": "integer",
                    "description": "Image width in pixels (default 512)",
                    "default": 512,
                },
                "height": {
                    "type": "integer",
                    "description": "Image height in pixels (default 512)",
                    "default": 512,
                },
                "steps": {
                    "type": "integer",
                    "description": "Number of diffusion steps (default 20)",
                    "default": 20,
                },
                "seed": {
                    "type": "integer",
                    "description": "Random seed (-1 for random, default -1)",
                    "default": -1,
                },
            },
            "required": ["prompt"],
        }

    def execute(
        self,
        prompt: str,
        negative_prompt: str = "",
        width: int = 512,
        height: int = 512,
        steps: int = 20,
        seed: int = -1,
        **kwargs: Any,
    ) -> ToolResult:
        if not prompt or not prompt.strip():
            return ToolResult(success=False, output="", error="No prompt provided")
        if not self._base_url:
            return ToolResult(
                success=False, output="",
                error="COMFYUI_URL not set. Enable ComfyUI with: docker compose --profile gpu-comfyui up",
            )

        # Clamp dimensions
        width = max(64, min(width, 2048))
        height = max(64, min(height, 2048))
        steps = max(1, min(steps, 100))

        # Build a basic txt2img ComfyUI workflow
        workflow = self._build_workflow(prompt, negative_prompt, width, height, steps, seed)

        try:
            # Queue the prompt
            payload = json.dumps({"prompt": workflow}).encode()
            req = urllib.request.Request(
                f"{self._base_url}/prompt",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                queue_data = json.loads(resp.read().decode())

            prompt_id = queue_data.get("prompt_id", "")
            if not prompt_id:
                return ToolResult(
                    success=False, output="",
                    error=f"ComfyUI did not return a prompt_id: {queue_data}",
                )

            # Poll for completion
            image_data = self._poll_result(prompt_id, timeout=steps * 5 + 60)

            if image_data:
                return ToolResult(
                    success=True,
                    output=f"Image generated successfully (prompt_id: {prompt_id})",
                    metadata={
                        "prompt_id": prompt_id,
                        "width": width,
                        "height": height,
                        "steps": steps,
                        "image_url": image_data,
                    },
                )

            return ToolResult(
                success=False, output="",
                error=f"Image generation timed out (prompt_id: {prompt_id})",
            )

        except (urllib.error.URLError, OSError) as e:
            return ToolResult(
                success=False, output="",
                error=f"ComfyUI image generation failed: {e}",
            )

    def _build_workflow(
        self,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        steps: int,
        seed: int,
    ) -> Dict[str, Any]:
        """Build a minimal ComfyUI txt2img workflow."""
        if seed == -1:
            import random
            seed = random.randint(0, 2**32 - 1)

        return {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": steps,
                    "cfg": 7.0,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": 1.0,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                },
            },
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"},
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1},
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["4", 1]},
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative_prompt, "clip": ["4", 1]},
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": "vibe", "images": ["8", 0]},
            },
        }

    def _poll_result(self, prompt_id: str, timeout: int = 120) -> Optional[str]:
        """Poll ComfyUI for prompt completion and return image URL."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(
                    f"{self._base_url}/history/{prompt_id}",
                    headers={"User-Agent": "Vibe/1.0"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    history = json.loads(resp.read().decode())

                if prompt_id in history:
                    outputs = history[prompt_id].get("outputs", {})
                    for node_id, node_output in outputs.items():
                        images = node_output.get("images", [])
                        if images:
                            img = images[0]
                            filename = img.get("filename", "")
                            subfolder = img.get("subfolder", "")
                            params = urllib.parse.urlencode({
                                "filename": filename,
                                "subfolder": subfolder,
                                "type": "output",
                            })
                            return f"{self._base_url}/view?{params}"
                    return None  # Completed but no images
            except (urllib.error.URLError, OSError):
                pass  # Server not ready yet

            time.sleep(2)

        return None
