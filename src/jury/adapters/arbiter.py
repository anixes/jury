"""
src/jury/adapters/arbiter.py - Decoupled Judicial Arbiter Adapter.

Implements independent Arbiter client for DeepSeek V3 / R1 via OpenRouter
or direct DeepSeek API. Strictly enforces architectural decoupling from
the panelist model families (GLM, Qwen, Llama) to eliminate circular
self-enhancement confirmation loops.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from jury.adapters.base import ArbiterCouplingError, OpenAICompatibleAdapter
from jury.adapters.clamping import clamp_diff
from jury.config import (
    DEFAULT_DEEPSEEK_GATEWAY,
    DEFAULT_OPENROUTER_GATEWAY,
    resolve_credentials,
)


DEFAULT_ARBITER_MODEL = "deepseek/deepseek-chat"


def identify_model_family(model_id_or_name: str) -> str:
    """Classify model identifier into its foundational model family."""
    mid = model_id_or_name.lower().strip()
    if "glm" in mid or "zhipu" in mid:
        return "glm"
    if "qwen" in mid or "alibaba" in mid:
        return "qwen"
    if "llama" in mid or "meta" in mid:
        return "llama"
    if "deepseek" in mid:
        return "deepseek"
    if "claude" in mid or "anthropic" in mid:
        return "anthropic"
    if "gpt" in mid or "openai" in mid or "o1" in mid or "o3" in mid:
        return "openai"
    if "mistral" in mid or "mixtral" in mid:
        return "mistral"
    # Fallback to model id prefix
    return mid.split("/")[0] if "/" in mid else mid


class DecoupledArbiterAdapter(OpenAICompatibleAdapter):
    """
    Decoupled Judicial Arbiter Adapter.
    Permanently isolated from panelist families (GLM, Qwen, Llama).
    Synthesizes final verdicts, Borda rankings, and machine patches.
    """

    def __init__(
        self,
        model: str = DEFAULT_ARBITER_MODEL,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        provider: Optional[str] = None,
        timeout_seconds: float = 60.0,
        max_diff_tokens: int = 16000,
        state_engine: Optional[Any] = None,
        default_headers: Optional[Dict[str, str]] = None,
        diff_clamper: Optional[Callable[[str, int], str]] = None,
        retry_attempts: int = 2,
        **kwargs: Any,
    ):
        # Determine provider: OpenRouter or DeepSeek
        if provider:
            effective_prov = provider.lower()
        elif "deepseek/" in model or "openrouter" in (base_url or ""):
            effective_prov = "openrouter"
        elif "deepseek" in model:
            effective_prov = "deepseek"
        else:
            effective_prov = "openrouter"

        # Resolve credentials
        resolved = resolve_credentials(cli_key=api_key, provider=effective_prov)
        effective_key = api_key or resolved.get("api_key", "")
        effective_url = base_url or resolved.get("gateway_url")

        if not effective_url:
            if effective_prov == "openrouter":
                effective_url = DEFAULT_OPENROUTER_GATEWAY
            else:
                effective_url = DEFAULT_DEEPSEEK_GATEWAY

        # OpenRouter-specific required headers
        headers = dict(default_headers or {})
        if effective_prov == "openrouter":
            if "HTTP-Referer" not in headers:
                headers["HTTP-Referer"] = "https://jury-mcp.dev"
            if "X-Title" not in headers:
                headers["X-Title"] = "Jury Mesh"

        effective_clamper = diff_clamper or (lambda d, max_tok: clamp_diff(d, max_tokens=max_tok))

        super().__init__(
            model=model,
            provider=effective_prov,
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

        self.is_decoupled: bool = True

    @property
    def model_family(self) -> str:
        """Foundational model family of the Arbiter."""
        return identify_model_family(self.model)

    def assert_decoupled_from(self, juror_model_ids: List[str], strict: bool = True) -> bool:
        """
        Verify that the Arbiter does not share a model lineage with any panel juror.
        Raises ArbiterCouplingError if an overlap is detected and strict is True.
        Emits a diagnostic warning if strict is False.
        """
        import logging
        import sys

        arbiter_fam = self.model_family
        for j_id in juror_model_ids:
            j_fam = identify_model_family(j_id)
            if j_fam == arbiter_fam:
                msg = (
                    f"Decoupled Arbiter warning: Arbiter model '{self.model}' "
                    f"(family: {arbiter_fam}) overlaps with panel juror '{j_id}' (family: {j_fam})."
                )
                if strict:
                    raise ArbiterCouplingError(
                        f"Decoupled Arbiter mandate violated! {msg} Arbiter must be an independent model family."
                    )
                else:
                    logging.getLogger("jury.arbiter").warning(msg)
                    sys.stderr.write(f"[JURY WARNING] {msg} Proceeding with shared model lineage.\n")
        return True
