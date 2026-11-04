"""
src/jury/adapters/openrouter.py - OpenRouter Multi-Model Gateway Adapter.

Implements OpenAI-compatible client targeting https://openrouter.ai/api/v1
for DeepSeek, Claude, Mistral, and custom frontier models.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from jury.adapters.base import OpenAICompatibleAdapter
from jury.adapters.clamping import clamp_diff
from jury.config import DEFAULT_OPENROUTER_GATEWAY, resolve_credentials


DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-chat"


class OpenRouterAdapter(OpenAICompatibleAdapter):
    """
    Adapter for OpenRouter API.
    Injects required HTTP-Referer and X-Title metadata headers.
    """

    def __init__(
        self,
        model: str = DEFAULT_OPENROUTER_MODEL,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: float = 60.0,
        max_diff_tokens: int = 16000,
        state_engine: Optional[Any] = None,
        default_headers: Optional[Dict[str, str]] = None,
        diff_clamper: Optional[Callable[[str, int], str]] = None,
        retry_attempts: int = 2,
        **kwargs: Any,
    ):
        resolved = resolve_credentials(cli_key=api_key, provider="openrouter")
        effective_key = api_key or resolved.get("api_key", "")
        effective_url = base_url or resolved.get("gateway_url") or DEFAULT_OPENROUTER_GATEWAY

        headers = dict(default_headers or {})
        if "HTTP-Referer" not in headers:
            headers["HTTP-Referer"] = "https://jury-mcp.dev"
        if "X-Title" not in headers:
            headers["X-Title"] = "Jury Mesh"

        effective_clamper = diff_clamper or (lambda d, max_tok: clamp_diff(d, max_tokens=max_tok))

        super().__init__(
            model=model,
            provider="openrouter",
            api_key=effective_key,
            base_url=effective_url,
            timeout_seconds=timeout_seconds,
            max_diff_tokens=max_diff_tokens,
            state_engine=state_engine,
            default_headers=headers,
            retry_attempts=retry_attempts,
            diff_clamper=effective_clamper,
            **kwargs,
        )
