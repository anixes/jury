"""
src/jury/adapters/base.py - Base LLM Adapter & Unified Response Abstraction.

Implements Interface Contract #3 for Jury Mesh v3.0:
- LLMResponse data container
- BaseLLMAdapter abstract base class with async complete() and complete_sync()
- Universal OpenAICompatibleAdapter for any standard OpenAI-compatible gateway (Constellation, Groq, OpenRouter, DeepSeek, Ollama, vLLM, etc.)
- Two-phase rate limit reservation/settle integration with JuryStateEngine/TwoPhaseRateLimiter
- Unified error handling and exception hierarchy
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Union

import httpx


# =============================================================================
# Exception Hierarchy
# =============================================================================

class LLMError(Exception):
    """Base exception for all model adapter errors."""
    pass


class LLMProviderError(LLMError):
    """Server-side or HTTP 5xx error from an upstream gateway."""
    def __init__(self, message: str, status_code: Optional[int] = None, raw_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.raw_body = raw_body


class LLMTimeoutError(LLMError):
    """Gateway or read timeout during model invocation."""
    pass


class LLMRateLimitError(LLMError):
    """Rate limit exhaustion (HTTP 429 or continuous bucket rejection)."""
    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMAuthError(LLMError):
    """Authentication or authorization failure (HTTP 401/403)."""
    pass


class ArbiterCouplingError(LLMError):
    """Violation of the Decoupled Arbiter mandate (arbiter overlaps with juror panel)."""
    pass


# =============================================================================
# Response Container
# =============================================================================

@dataclass
class LLMResponse:
    """Unified container for LLM generation results across all providers."""
    content: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    cached: bool = False
    reasoning_content: str = ""
    raw_response: Optional[Dict[str, Any]] = None


# =============================================================================
# Token Estimation Utility
# =============================================================================

def estimate_tokens(payload: Union[str, List[Dict[str, str]]]) -> int:
    """
    Estimate token count for prompt strings or chat message lists.
    Calibrated for code and structured markdown (~3.7 characters per token).
    """
    if not payload:
        return 0

    if isinstance(payload, str):
        return max(1, int(len(payload) / 3.7))

    if isinstance(payload, list):
        total = 0
        for msg in payload:
            total += 4  # Standard OpenAI-style message envelope overhead
            content = msg.get("content", "") or ""
            role = msg.get("role", "") or ""
            total += max(1, int((len(content) + len(role)) / 3.7))
        return max(1, total)

    return 1


# =============================================================================
# Base Adapter Interface
# =============================================================================

class BaseLLMAdapter(ABC):
    """
    Abstract Base Class for all Jury Mesh model adapters.
    Provides standard async complete(), sync wrapper complete_sync(),
    payload clamping hooks, and two-phase rate limiter integration.
    """

    def __init__(
        self,
        model: str,
        provider: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: float = 60.0,
        max_diff_tokens: int = 14000,
        state_engine: Optional[Any] = None,
        default_headers: Optional[Dict[str, str]] = None,
        retry_attempts: int = 1,
        diff_clamper: Optional[Callable[[str, int], str]] = None,
    ):
        self.model = model
        self.provider = provider
        self.api_key = api_key or ""
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_diff_tokens = max_diff_tokens
        self.state_engine = state_engine
        self.default_headers = default_headers or {}
        self.retry_attempts = max(0, retry_attempts)
        self.diff_clamper = diff_clamper

    @property
    def provider_name(self) -> str:
        """Canonical provider name."""
        return self.provider

    @property
    def model_name(self) -> str:
        """Canonical model identifier."""
        return self.model

    def clamp_payload(self, diff_text: str) -> str:
        """
        Clamp diff payload to provider token envelope.
        If a diff_clamper callback is provided, uses it; otherwise returns unchanged.
        """
        if self.diff_clamper is not None:
            return self.diff_clamper(diff_text, self.max_diff_tokens)
        return diff_text

    def _normalize_messages(
        self,
        prompt: Union[str, List[Dict[str, str]]],
        system: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Normalize string prompt or message list into standard OpenAI message structure."""
        if isinstance(prompt, str):
            messages: List[Dict[str, str]] = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            return messages

        if isinstance(prompt, list):
            messages = [dict(m) for m in prompt]
            if system:
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] = system
                else:
                    messages.insert(0, {"role": "system", "content": system})
            return messages

        raise TypeError(f"Prompt must be str or list of dicts, got {type(prompt).__name__}")

    async def complete(
        self,
        prompt: Union[str, List[Dict[str, str]]],
        system: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        Asynchronously complete the prompt.
        Integrates two-phase rate limit reservation/settlement when state_engine is attached.
        """
        messages = self._normalize_messages(prompt, system=system)
        est_input_tokens = estimate_tokens(messages)
        estimated_tokens = est_input_tokens + max_tokens

        # Phase 1: Pre-flight Rate Limit Reservation
        has_state_engine = self.state_engine is not None
        if has_state_engine:
            if hasattr(self.state_engine, "acquire_rate_limit"):
                acq = self.state_engine.acquire_rate_limit(
                    provider=self.provider,
                    estimated_tokens=estimated_tokens,
                    max_wait_seconds=self.timeout_seconds,
                )
            elif hasattr(self.state_engine, "reserve"):
                acq = self.state_engine.reserve(
                    provider_or_bucket=self.provider,
                    estimated_tokens=estimated_tokens,
                    max_wait_seconds=self.timeout_seconds,
                )
            else:
                acq = None

            if acq is not None and not acq.granted:
                wait_sec = getattr(acq, "wait_seconds", 0.0)
                reason = getattr(acq, "reason", "Rate limit exhausted")
                raise LLMRateLimitError(
                    f"Rate limit reservation rejected for {self.provider}: {reason}",
                    retry_after=wait_sec,
                )

        t_start = time.perf_counter()
        attempts = 0
        last_error: Optional[Exception] = None

        while attempts <= self.retry_attempts:
            attempts += 1
            try:
                response = await self._execute_completion(
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **kwargs,
                )
                latency_ms = int((time.perf_counter() - t_start) * 1000)
                response.latency_ms = latency_ms

                # Phase 2: Post-flight Settlement on Success
                if has_state_engine:
                    actual_tokens = response.total_tokens or (est_input_tokens + max(1, len(response.content) // 4))
                    if hasattr(self.state_engine, "refund_tokens"):
                        self.state_engine.refund_tokens(
                            provider=self.provider,
                            actual_tokens=actual_tokens,
                            estimated_tokens=estimated_tokens,
                            success=True,
                        )
                    elif hasattr(self.state_engine, "settle"):
                        self.state_engine.settle(
                            provider_or_bucket=self.provider,
                            estimated_tokens=estimated_tokens,
                            actual_tokens=actual_tokens,
                            success=True,
                        )

                return response

            except (LLMRateLimitError, LLMAuthError) as direct_err:
                last_error = direct_err
                if has_state_engine:
                    retry_after = getattr(direct_err, "retry_after", None)
                    if hasattr(self.state_engine, "refund_tokens"):
                        self.state_engine.refund_tokens(
                            provider=self.provider,
                            actual_tokens=0,
                            estimated_tokens=estimated_tokens,
                            success=False,
                            retry_after=retry_after,
                            error_msg=str(direct_err),
                        )
                    elif hasattr(self.state_engine, "settle"):
                        self.state_engine.settle(
                            provider_or_bucket=self.provider,
                            estimated_tokens=estimated_tokens,
                            actual_tokens=0,
                            success=False,
                            retry_after=retry_after,
                            error_msg=str(direct_err),
                        )
                raise

            except (LLMProviderError, LLMTimeoutError) as err:
                last_error = err
                if attempts <= self.retry_attempts:
                    await asyncio.sleep(0.5 * (2 ** (attempts - 1)))
                    continue

                if has_state_engine:
                    if hasattr(self.state_engine, "refund_tokens"):
                        self.state_engine.refund_tokens(
                            provider=self.provider,
                            actual_tokens=0,
                            estimated_tokens=estimated_tokens,
                            success=False,
                            error_msg=str(err),
                        )
                    elif hasattr(self.state_engine, "settle"):
                        self.state_engine.settle(
                            provider_or_bucket=self.provider,
                            estimated_tokens=estimated_tokens,
                            actual_tokens=0,
                            success=False,
                            error_msg=str(err),
                        )
                raise

            except Exception as e:
                last_error = e
                if has_state_engine:
                    if hasattr(self.state_engine, "refund_tokens"):
                        self.state_engine.refund_tokens(
                            provider=self.provider,
                            actual_tokens=0,
                            estimated_tokens=estimated_tokens,
                            success=False,
                            error_msg=str(e),
                        )
                    elif hasattr(self.state_engine, "settle"):
                        self.state_engine.settle(
                            provider_or_bucket=self.provider,
                            estimated_tokens=estimated_tokens,
                            actual_tokens=0,
                            success=False,
                            error_msg=str(e),
                        )
                raise LLMProviderError(f"Unexpected adapter failure: {e}") from e

        if last_error:
            raise last_error
        raise LLMError("Completion failed without explicit error.")

    def complete_sync(
        self,
        prompt: Union[str, List[Dict[str, str]]],
        system: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> LLMResponse:
        """
        Synchronous wrapper around async complete().
        Safely executes regardless of whether an asyncio event loop is already active.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            def _runner() -> LLMResponse:
                return asyncio.run(
                    self.complete(
                        prompt=prompt,
                        system=system,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        **kwargs,
                    )
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(_runner).result()
        else:
            return asyncio.run(
                self.complete(
                    prompt=prompt,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **kwargs,
                )
            )

    @abstractmethod
    async def _execute_completion(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> LLMResponse:
        """Low-level provider invocation to be implemented by concrete adapters."""
        raise NotImplementedError


# =============================================================================
# Universal OpenAI-Compatible Adapter
# =============================================================================

class OpenAICompatibleAdapter(BaseLLMAdapter):
    """
    First-class, universal OpenAI-compatible adapter.
    Works seamlessly with any provider implementing the standard /v1/chat/completions wire format:
    Constellation Gateway, Groq, OpenRouter, DeepSeek, Cerebras, Ollama, vLLM, Together, etc.
    """

    def __init__(
        self,
        model: str,
        provider: str = "openai_compatible",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: float = 60.0,
        max_diff_tokens: int = 14000,
        state_engine: Optional[Any] = None,
        default_headers: Optional[Dict[str, str]] = None,
        retry_attempts: int = 1,
        diff_clamper: Optional[Callable[[str, int], str]] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        super().__init__(
            model=model,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_diff_tokens=max_diff_tokens,
            state_engine=state_engine,
            default_headers=default_headers,
            retry_attempts=retry_attempts,
            diff_clamper=diff_clamper,
        )
        self.extra_body = extra_body or {}
        self._http_client = http_client

    def _build_url(self) -> str:
        """Construct canonical /chat/completions endpoint."""
        url = self.base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        return url

    def _build_headers(self) -> Dict[str, str]:
        """Construct request headers including auth and defaults."""
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.default_headers)
        return headers

    def _extract_retry_after(self, response: httpx.Response) -> Optional[float]:
        """Extract Retry-After duration in seconds from HTTP headers."""
        retry_header = response.headers.get("retry-after") or response.headers.get("Retry-After")
        if retry_header:
            try:
                return float(retry_header)
            except ValueError:
                pass

        # Groq reset header: x-ratelimit-reset-requests e.g. "6m0s" or float
        groq_reset = response.headers.get("x-ratelimit-reset-requests")
        if groq_reset:
            try:
                if groq_reset.endswith("s"):
                    return float(groq_reset[:-1])
                return float(groq_reset)
            except ValueError:
                pass

        return None

    async def _execute_completion(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> LLMResponse:
        """Execute HTTP request against OpenAI-compatible endpoint."""
        url = self._build_url()
        headers = self._build_headers()

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        payload.update(self.extra_body)
        payload.update(kwargs)

        # Remove internal or None kwargs if any
        payload = {k: v for k, v in payload.items() if v is not None}

        async def _send(client: httpx.AsyncClient) -> httpx.Response:
            return await client.post(url, headers=headers, json=payload)

        try:
            if self._http_client is not None:
                resp = await _send(self._http_client)
            else:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    resp = await _send(client)
        except httpx.TimeoutException as te:
            raise LLMTimeoutError(f"Request to {self.provider} ({url}) timed out: {te}") from te
        except httpx.RequestError as re:
            raise LLMProviderError(f"Network error communicating with {self.provider}: {re}") from re

        # Handle HTTP error statuses
        if resp.status_code in (401, 403):
            raise LLMAuthError(
                f"Authentication failed for {self.provider} ({resp.status_code}): {resp.text}"
            )

        if resp.status_code == 429:
            retry_after = self._extract_retry_after(resp)
            raise LLMRateLimitError(
                f"Rate limit exceeded for {self.provider} (HTTP 429): {resp.text}",
                retry_after=retry_after,
            )

        if resp.status_code >= 500:
            raise LLMProviderError(
                f"Upstream provider error from {self.provider} (HTTP {resp.status_code}): {resp.text}",
                status_code=resp.status_code,
                raw_body=resp.text,
            )

        if resp.status_code != 200:
            raise LLMProviderError(
                f"Unexpected HTTP {resp.status_code} from {self.provider}: {resp.text}",
                status_code=resp.status_code,
                raw_body=resp.text,
            )

        try:
            data = resp.json()
        except Exception as e:
            raise LLMProviderError(f"Failed to parse JSON response from {self.provider}: {e}") from e

        # Extract content & reasoning_content
        choices = data.get("choices", [])
        if not choices:
            raise LLMProviderError(f"Empty choices returned from {self.provider}: {data}")

        first_choice = choices[0]
        message_dict = first_choice.get("message", {})
        content = message_dict.get("content") or ""
        reasoning_content = message_dict.get("reasoning_content") or ""

        # Fallback for models returning answer in reasoning_content
        if not content and reasoning_content:
            content = reasoning_content

        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

        return LLMResponse(
            content=content,
            model=data.get("model", self.model),
            provider=self.provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            reasoning_content=reasoning_content,
            raw_response=data,
        )
