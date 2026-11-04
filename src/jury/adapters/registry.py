"""
src/jury/adapters/registry.py - Dynamic Model Adapter Registry & Factory.

Provides dynamic, config-driven model adapter creation and caching.
Supports arbitrary OpenAI-compatible endpoints, custom models, and an offline
MockLLMAdapter for deterministic testing without external API credentials.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Union

from jury.adapters.arbiter import DecoupledArbiterAdapter
from jury.adapters.base import (
    BaseLLMAdapter,
    LLMError,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
    OpenAICompatibleAdapter,
)
from jury.adapters.cerebras import CerebrasAdapter
from jury.adapters.clamping import DiffClamper, clamp_diff
from jury.adapters.constellation import ConstellationAdapter
from jury.adapters.groq import GroqAdapter
from jury.adapters.openrouter import OpenRouterAdapter
from jury.config import JuryConfig, ModelConfig, ProviderConfig, get_config


# =============================================================================
# Mock / Offline Adapter
# =============================================================================

class MockLLMAdapter(BaseLLMAdapter):
    """
    In-memory mock adapter for deterministic offline testing and CI/CD pipelines.
    Tracks invocation history and returns pre-configured fixture responses.
    """

    def __init__(
        self,
        model: str = "mock-model",
        provider: str = "mock",
        default_response: Optional[Union[str, LLMResponse, Callable[..., LLMResponse]]] = None,
        simulate_error: Optional[Exception] = None,
        simulate_latency_ms: int = 10,
        max_diff_tokens: int = 14000,
        state_engine: Optional[Any] = None,
    ):
        super().__init__(
            model=model,
            provider=provider,
            api_key="mock-key",
            base_url="mock://localhost",
            max_diff_tokens=max_diff_tokens,
            state_engine=state_engine,
            diff_clamper=lambda d, max_tok: clamp_diff(d, max_tokens=max_tok),
        )
        self.default_response = default_response
        self.simulate_error = simulate_error
        self.simulate_latency_ms = simulate_latency_ms
        self.call_history: List[Dict[str, Any]] = []

    def set_response(self, response: Union[str, LLMResponse, Callable[..., LLMResponse]]) -> None:
        """Update mock response target."""
        self.default_response = response

    def set_error(self, error: Optional[Exception]) -> None:
        """Set or clear simulated exception."""
        self.simulate_error = error

    async def _execute_completion(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> LLMResponse:
        """Execute mock completion, recording history."""
        self.call_history.append({
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "kwargs": kwargs,
        })

        if self.simulate_error is not None:
            raise self.simulate_error

        # If a callable generator is provided
        if callable(self.default_response):
            result = self.default_response(messages=messages, max_tokens=max_tokens, **kwargs)
            if isinstance(result, LLMResponse):
                return result
            content = str(result)
        elif isinstance(self.default_response, LLMResponse):
            return self.default_response
        elif isinstance(self.default_response, str):
            content = self.default_response
        else:
            # Deterministic default JSON response matching plan/review schema
            content = json.dumps({
                "verdict": "APPROVED",
                "score": 9.0,
                "summary": "Mock deliberation review approved without defects.",
                "findings": [],
            })

        prompt_toks = sum(len(m.get("content", "")) // 4 for m in messages) or 20
        comp_toks = len(content) // 4 or 15

        return LLMResponse(
            content=content,
            model=self.model,
            provider=self.provider,
            prompt_tokens=prompt_toks,
            completion_tokens=comp_toks,
            total_tokens=prompt_toks + comp_toks,
            latency_ms=self.simulate_latency_ms,
            cached=False,
            raw_response={"mock": True},
        )


# =============================================================================
# Dynamic Adapter Registry
# =============================================================================

class AdapterRegistry:
    """
    Central factory and cache for model adapters.
    Instantiates adapters on-demand based on JuryConfig or direct specifications,
    supporting pluggable endpoints and offline mock modes.
    """

    def __init__(
        self,
        config: Optional[JuryConfig] = None,
        mock_mode: bool = False,
        state_engine: Optional[Any] = None,
    ):
        self.config = config or get_config()
        self.mock_mode = mock_mode
        self.state_engine = state_engine
        self._cache: Dict[str, BaseLLMAdapter] = {}
        self._mock_responses: Dict[str, Union[str, LLMResponse, Callable]] = {}

    def set_mock_mode(self, enabled: bool) -> None:
        """Enable or disable global mock mode."""
        self.mock_mode = enabled

    def register_mock_response(
        self,
        target: str,
        response: Union[str, LLMResponse, Callable],
    ) -> None:
        """Register a fixture response for a specific role or provider."""
        self._mock_responses[target.lower().strip()] = response

    def clear(self) -> None:
        """Clear cached adapter instances."""
        self._cache.clear()

    def register_adapter(self, name: str, adapter: BaseLLMAdapter) -> None:
        """Manually register an adapter instance in the registry."""
        self._cache[name.lower().strip()] = adapter

    def get_adapter(
        self,
        role_or_provider: str,
        model_id: Optional[str] = None,
        mock: bool = False,
        state_engine: Optional[Any] = None,
    ) -> BaseLLMAdapter:
        """
        Resolve or instantiate an adapter by role name ('alpha', 'beta', 'gamma', 'arbiter')
        or provider name ('constellation', 'groq', 'openrouter', 'deepseek', 'cerebras', 'ollama').
        """
        key = role_or_provider.lower().strip()
        effective_mock = mock or self.mock_mode or key.startswith("mock")
        engine = state_engine or self.state_engine

        if key in self._cache:
            return self._cache[key]

        cache_key = f"{key}:{model_id or 'default'}:{'mock' if effective_mock else 'live'}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if effective_mock:
            mock_resp = self._mock_responses.get(key)
            adapter = MockLLMAdapter(
                model=model_id or f"mock-{key}",
                provider="mock",
                default_response=mock_resp,
                state_engine=engine,
            )
            self._cache[cache_key] = adapter
            return adapter

        # 1. Check if key is a configured role ('alpha', 'beta', 'gamma', 'arbiter')
        if key in self.config.models:
            model_cfg = self.config.models[key]
            adapter = self._create_adapter_from_model_config(
                role=key,
                model_cfg=model_cfg,
                override_model_id=model_id,
                state_engine=engine,
            )
            self._cache[cache_key] = adapter
            return adapter

        # 2. Check if key matches a known provider preset
        adapter = self._create_adapter_by_provider(
            provider_name=key,
            model_id=model_id,
            state_engine=engine,
        )
        self._cache[cache_key] = adapter
        return adapter

    def _create_adapter_from_model_config(
        self,
        role: str,
        model_cfg: ModelConfig,
        override_model_id: Optional[str] = None,
        state_engine: Optional[Any] = None,
    ) -> BaseLLMAdapter:
        """Build adapter from ModelConfig defined in JuryConfig."""
        mid = override_model_id or model_cfg.model_id
        prov = model_cfg.provider.lower()
        prov_cfg = self.config.providers.get(prov)

        base_url = prov_cfg.base_url if prov_cfg else None
        api_key = prov_cfg.api_key if prov_cfg else None
        headers = prov_cfg.default_headers if prov_cfg else None

        if role == "arbiter" or prov in ("openrouter", "deepseek"):
            return DecoupledArbiterAdapter(
                model=mid,
                provider=prov,
                api_key=api_key,
                base_url=base_url,
                timeout_seconds=model_cfg.timeout_seconds,
                max_diff_tokens=model_cfg.max_diff_tokens,
                state_engine=state_engine,
                default_headers=headers,
            )
        elif prov == "groq":
            return GroqAdapter(
                model=mid,
                api_key=api_key,
                base_url=base_url,
                timeout_seconds=model_cfg.timeout_seconds,
                max_diff_tokens=model_cfg.max_diff_tokens,
                state_engine=state_engine,
                default_headers=headers,
            )
        elif prov == "constellation":
            return ConstellationAdapter(
                model=mid,
                api_key=api_key,
                base_url=base_url,
                timeout_seconds=model_cfg.timeout_seconds,
                max_diff_tokens=model_cfg.max_diff_tokens,
                state_engine=state_engine,
                default_headers=headers,
            )
        elif prov == "cerebras":
            return CerebrasAdapter(
                model=mid,
                api_key=api_key,
                base_url=base_url,
                timeout_seconds=model_cfg.timeout_seconds,
                max_diff_tokens=model_cfg.max_diff_tokens,
                state_engine=state_engine,
                default_headers=headers,
            )
        else:
            # Universal fallback for any custom OpenAI-compatible provider (Ollama, vLLM, etc.)
            return OpenAICompatibleAdapter(
                model=mid,
                provider=prov,
                api_key=api_key,
                base_url=base_url,
                timeout_seconds=model_cfg.timeout_seconds,
                max_diff_tokens=model_cfg.max_diff_tokens,
                state_engine=state_engine,
                default_headers=headers,
                diff_clamper=lambda d, max_tok: clamp_diff(d, max_tokens=max_tok),
            )

    def _create_adapter_by_provider(
        self,
        provider_name: str,
        model_id: Optional[str] = None,
        state_engine: Optional[Any] = None,
    ) -> BaseLLMAdapter:
        """Instantiate adapter directly by provider name."""
        prov = provider_name.lower()
        prov_cfg = self.config.providers.get(prov)

        base_url = prov_cfg.base_url if prov_cfg else None
        api_key = prov_cfg.api_key if prov_cfg else None
        headers = prov_cfg.default_headers if prov_cfg else None

        if prov == "constellation":
            return ConstellationAdapter(
                model=model_id or "constellation/glm-5-3-flash-free",
                api_key=api_key,
                base_url=base_url,
                state_engine=state_engine,
                default_headers=headers,
            )
        elif prov == "groq":
            return GroqAdapter(
                model=model_id or "llama-3.3-70b-versatile",
                api_key=api_key,
                base_url=base_url,
                state_engine=state_engine,
                default_headers=headers,
            )
        elif prov == "openrouter":
            return OpenRouterAdapter(
                model=model_id or "deepseek/deepseek-chat",
                api_key=api_key,
                base_url=base_url,
                state_engine=state_engine,
                default_headers=headers,
            )
        elif prov == "deepseek":
            return DecoupledArbiterAdapter(
                model=model_id or "deepseek-chat",
                provider="deepseek",
                api_key=api_key,
                base_url=base_url,
                state_engine=state_engine,
                default_headers=headers,
            )
        elif prov == "cerebras":
            return CerebrasAdapter(
                model=model_id or "llama3.3-70b",
                api_key=api_key,
                base_url=base_url,
                state_engine=state_engine,
                default_headers=headers,
            )
        else:
            # Universal OpenAI-compatible endpoint
            return OpenAICompatibleAdapter(
                model=model_id or "default",
                provider=prov,
                api_key=api_key,
                base_url=base_url or f"http://localhost:11434/v1",  # e.g. Ollama default
                state_engine=state_engine,
                default_headers=headers,
                diff_clamper=lambda d, max_tok: clamp_diff(d, max_tokens=max_tok),
            )
