"""
src/jury/adapters/constellation.py - Constellation Gateway Adapter.

Implements OpenAI-compatible client targeting https://gateway.constellationgate.ai/v1
for GLM-5.3 320B (Juror Alpha) and Qwen 3.8 180B (Juror Beta).
Integrates credential resolution cascade from jury.config.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from jury.adapters.base import OpenAICompatibleAdapter
from jury.adapters.clamping import clamp_diff
from jury.config import DEFAULT_CONSTELLATION_GATEWAY, resolve_credentials


DEFAULT_GLM_MODEL = "constellation/glm-5-3-flash-free"
DEFAULT_QWEN_MODEL = "constellation/qwen3-8-flash-free"


class ConstellationAdapter(OpenAICompatibleAdapter):
    """
    Adapter for the Constellation Gateway.
    Specialized for GLM-5.3 320B (Architect/Proposer) and Qwen 3.8 180B (Adversary).
    Automatically resolves credentials via the 4-level hierarchy.
    """

    def __init__(
        self,
        model: str = DEFAULT_GLM_MODEL,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: float = 60.0,
        max_diff_tokens: int = 14000,
        state_engine: Optional[Any] = None,
        default_headers: Optional[Dict[str, str]] = None,
        diff_clamper: Optional[Callable[[str, int], str]] = None,
        retry_attempts: int = 2,
        **kwargs: Any,
    ):
        # Resolve credentials if not explicitly passed
        resolved = resolve_credentials(cli_key=api_key, provider="constellation")
        effective_key = api_key or resolved.get("api_key", "")
        effective_url = base_url or resolved.get("gateway_url") or DEFAULT_CONSTELLATION_GATEWAY

        # Default clamper clamps to max_diff_tokens (14,000)
        effective_clamper = diff_clamper or (lambda d, max_tok: clamp_diff(d, max_tokens=max_tok))

        super().__init__(
            model=model,
            provider="constellation",
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
