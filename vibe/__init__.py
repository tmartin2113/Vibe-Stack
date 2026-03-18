"""
Vibe - LLM backend abstraction layer.
"""

__version__ = "1.0.0"

from vibe.backends.base import BackendBase
from vibe.backends.vllm import VLLMBackend

__all__ = [
    "__version__",
    "BackendBase",
    "VLLMBackend",
]
