"""
Genesia - LLM backend abstraction layer.
"""

__version__ = "1.0.0"

from genesia.backends.base import BackendBase
from genesia.backends.vllm import VLLMBackend

__all__ = [
    "__version__",
    "BackendBase",
    "VLLMBackend",
]
