"""
src/jury/adapters/groq.py - Groq LPU Adapter with AST-Aware 5.5k Diff Clamping.

Implements OpenAI-compatible client targeting https://api.groq.com/openai/v1
for Llama 3.3 70B Versatile (Juror Gamma / Performance Specialist).
Enforces strict <= 5,500 diff token clamping to protect Groq's 12k TPM ceiling.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from jury.adapters.base import OpenAICompatibleAdapter
from jury.adapters.clamping import DiffClamper, clamp_diff
from jury.config import DEFAULT_GROQ_GATEWAY, resolve_credentials


DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_SAFE_TPM_DIFF_CEILING = 5500


class GroqAdapter(OpenAICompatibleAdapter):
    """
    Adapter for the Groq Cloud API.
    Specialized for Llama 3.3 70B (Performance / LPU Accelerator).
    Automatically enforces AST-aware diff clamping (<= 5,500 tokens)
    to safely fit within Groq's free-tier 12,000 TPM quota envelope.
    """

    def __init__(
        self,
        model: str = DEFAULT_GROQ_MODEL,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: float = 45.0,
        max_diff_tokens: int = GROQ_SAFE_TPM_DIFF_CEILING,
        state_engine: Optional[Any] = None,
        default_headers: Optional[Dict[str, str]] = None,
        diff_clamper: Optional[Callable[[str, int], str]] = None,
        retry_attempts: int = 1,
        **kwargs: Any,
    ):
        resolved = resolve_credentials(cli_key=api_key, provider="groq")
        effective_key = api_key or resolved.get("api_key", "")
        effective_url = base_url or resolved.get("gateway_url") or DEFAULT_GROQ_GATEWAY

        # Ensure AST-aware clamper is always active with <= 5,500 limit
        clamper_instance = DiffClamper(max_tokens=max_diff_tokens)
        effective_clamper = diff_clamper or (lambda d, max_tok: clamper_instance.clamp(d, max_tok))

        super().__init__(
            model=model,
            provider="groq",
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

    def clamp_payload(self, diff_text: str) -> str:
        """
        Enforce AST-aware diff clamping on diff payload.
        Guarantees result is <= self.max_diff_tokens (default 5,500 tokens).
        """
        return self.diff_clamper(diff_text, self.max_diff_tokens)
