"""
src/jury/adapters/cerebras.py - Cerebras Wafer-Scale Engine Adapter.

Implements OpenAI-compatible client targeting https://api.cerebras.ai/v1
for ultra-high-speed token generation (1000+ tok/s) with Llama 3.3 70B.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from jury.adapters.base import OpenAICompatibleAdapter
from jury.adapters.clamping import clamp_diff
from jury.config import DEFAULT_CEREBRAS_GATEWAY, resolve_credentials


DEFAULT_CEREBRAS_MODEL = "llama3.3-70b"


class CerebrasAdapter(OpenAICompatibleAdapter):
    """
    Adapter for Cerebras Cloud API.
    Specialized for ultra-fast generation and rapid Stage 2 cross-examination rounds.
    """

    def __init__(
        self,
        model: str = DEFAULT_CEREBRAS_MODEL,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: float = 30.0,
        max_diff_tokens: int = 10000,
        state_engine: Optional[Any] = None,
        default_headers: Optional[Dict[str, str]] = None,
        diff_clamper: Optional[Callable[[str, int], str]] = None,
        retry_attempts: int = 2,
        **kwargs: Any,
    ):
        resolved = resolve_credentials(cli_key=api_key, provider="cerebras")
        effective_key = api_key or resolved.get("api_key", "")
        effective_url = base_url or resolved.get("gateway_url") or DEFAULT_CEREBRAS_GATEWAY

        effective_clamper = diff_clamper or (lambda d, max_tok: clamp_diff(d, max_tokens=max_tok))

        super().__init__(
            model=model,
            provider="cerebras",
            api_key=effective_key,
            base_url=effective_url,
            timeout_seconds=timeout_seconds,
            max_diff_tokens=max_diff_tokens,
            state_engine=state_engine,
            default_headers=default_headers,
            retry_attempts=retry_attempts,
            diff_clamper=effective_clamper,
            **kwargs,
        )
