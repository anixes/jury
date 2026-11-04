"""
src/jury/adapters/__init__.py - Jury Mesh v3.0 Unified Model Adapter Subsystem.
"""

from jury.adapters.arbiter import DecoupledArbiterAdapter, identify_model_family
from jury.adapters.base import (
    ArbiterCouplingError,
    BaseLLMAdapter,
    LLMAuthError,
    LLMError,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
    OpenAICompatibleAdapter,
    estimate_tokens,
)
from jury.adapters.cerebras import CerebrasAdapter
from jury.adapters.clamping import DiffClamper, clamp_diff
from jury.adapters.constellation import ConstellationAdapter
from jury.adapters.groq import GroqAdapter
from jury.adapters.openrouter import OpenRouterAdapter
from jury.adapters.registry import AdapterRegistry, MockLLMAdapter

__all__ = [
    "BaseLLMAdapter",
    "OpenAICompatibleAdapter",
    "LLMResponse",
    "estimate_tokens",
    "LLMError",
    "LLMProviderError",
    "LLMTimeoutError",
    "LLMRateLimitError",
    "LLMAuthError",
    "ArbiterCouplingError",
    "ConstellationAdapter",
    "GroqAdapter",
    "DecoupledArbiterAdapter",
    "CerebrasAdapter",
    "OpenRouterAdapter",
    "MockLLMAdapter",
    "AdapterRegistry",
    "DiffClamper",
    "clamp_diff",
    "identify_model_family",
]
