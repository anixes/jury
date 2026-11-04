"""
tests/unit/test_adapters.py - Unit Test Suite for Jury Mesh Model Adapters & Gateway Integrations.

Tests:
1. LLMResponse & token estimation utility
2. BaseLLMAdapter interface, sync/async execution, and normalization
3. Two-phase rate limiter reservation/settle integration
4. OpenAICompatibleAdapter HTTP transport, headers, reasoning tokens, and error handling
5. ConstellationAdapter presets and credential resolution
6. GroqAdapter & AST-aware diff clamping (<= 5,500 tokens)
7. DecoupledArbiterAdapter and decoupling invariant assertion
8. AdapterRegistry dynamic instantiation, caching, mock/offline testing mode
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import httpx
import pytest

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
from jury.config import get_config


# =============================================================================
# 1. LLMResponse and Token Estimation Tests
# =============================================================================

def test_llm_response_structure_and_defaults():
    resp = LLMResponse(
        content="Approval rationale",
        model="glm-5.3",
        provider="constellation",
        prompt_tokens=120,
        completion_tokens=45,
        total_tokens=165,
        latency_ms=350,
        cached=False,
    )
    assert resp.content == "Approval rationale"
    assert resp.model == "glm-5.3"
    assert resp.provider == "constellation"
    assert resp.prompt_tokens == 120
    assert resp.completion_tokens == 45
    assert resp.total_tokens == 165
    assert resp.latency_ms == 350
    assert resp.cached is False
    assert resp.reasoning_content == ""


def test_estimate_tokens_string_and_messages():
    # Empty
    assert estimate_tokens("") == 0
    assert estimate_tokens([]) == 0

    # Short string
    text = "def calculate_sum(a: int, b: int) -> int:\n    return a + b"
    est = estimate_tokens(text)
    assert est > 5
    assert est < 30

    # Message list
    messages = [
        {"role": "system", "content": "You are an architecture reviewer."},
        {"role": "user", "content": "Review this diff."},
    ]
    est_msgs = estimate_tokens(messages)
    assert est_msgs > 10


# =============================================================================
# 2. Base Adapter & Sync Wrapper Tests
# =============================================================================

class DummyAdapter(BaseLLMAdapter):
    """Concrete test adapter for base class testing."""
    def __init__(self, **kwargs):
        super().__init__(model="test-model", provider="test-provider", **kwargs)

    async def _execute_completion(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(
            content=f"Echo: {messages[-1]['content']}",
            model=self.model,
            provider=self.provider,
            prompt_tokens=10,
            completion_tokens=10,
            total_tokens=20,
        )


def test_base_adapter_normalize_messages():
    adapter = DummyAdapter()

    # From string with system
    msgs1 = adapter._normalize_messages("Hello", system="You are an expert.")
    assert len(msgs1) == 2
    assert msgs1[0] == {"role": "system", "content": "You are an expert."}
    assert msgs1[1] == {"role": "user", "content": "Hello"}

    # From string without system
    msgs2 = adapter._normalize_messages("Just user")
    assert len(msgs2) == 1
    assert msgs2[0] == {"role": "user", "content": "Just user"}

    # From existing message list
    msgs3 = adapter._normalize_messages([{"role": "user", "content": "Existing"}], system="New sys")
    assert len(msgs3) == 2
    assert msgs3[0] == {"role": "system", "content": "New sys"}


def test_base_adapter_complete_sync():
    adapter = DummyAdapter()
    resp = adapter.complete_sync(prompt="Sync prompt test", system="Sys prompt")
    assert resp.content == "Echo: Sync prompt test"
    assert resp.provider == "test-provider"
    assert resp.latency_ms >= 0


def test_base_adapter_complete_sync_inside_event_loop():
    adapter = DummyAdapter()

    async def _run_inside_loop():
        # Calling complete_sync from inside an active async event loop
        return adapter.complete_sync(prompt="Inside async loop")

    result = asyncio.run(_run_inside_loop())
    assert result.content == "Echo: Inside async loop"


# =============================================================================
# 3. Two-Phase Rate Limiter Integration Tests
# =============================================================================

def test_two_phase_rate_limiter_success_lifecycle():
    mock_engine = MagicMock()
    # Mock successful acquisition
    mock_acq = MagicMock(granted=True, wait_seconds=0.0)
    mock_engine.acquire_rate_limit.return_value = mock_acq

    adapter = DummyAdapter(state_engine=mock_engine)
    resp = adapter.complete_sync(prompt="Test rate limiting", max_tokens=100)

    assert resp.content == "Echo: Test rate limiting"
    # Pre-flight reserve must have been called
    mock_engine.acquire_rate_limit.assert_called_once()
    call_args = mock_engine.acquire_rate_limit.call_args[1]
    assert call_args["provider"] == "test-provider"
    assert call_args["estimated_tokens"] >= 100

    # Post-flight settle must have been called with success=True
    mock_engine.refund_tokens.assert_called_once()
    settle_args = mock_engine.refund_tokens.call_args[1]
    assert settle_args["provider"] == "test-provider"
    assert settle_args["actual_tokens"] == 20
    assert settle_args["success"] is True


def test_two_phase_rate_limiter_rejection_lifecycle():
    mock_engine = MagicMock()
    # Mock rejected acquisition
    mock_acq = MagicMock(granted=False, wait_seconds=12.5, reason="TPM ceiling reached")
    mock_engine.acquire_rate_limit.return_value = mock_acq

    adapter = DummyAdapter(state_engine=mock_engine)

    with pytest.raises(LLMRateLimitError) as exc_info:
        adapter.complete_sync(prompt="Test rate limiting rejection")

    assert "Rate limit reservation rejected" in str(exc_info.value)
    assert exc_info.value.retry_after == 12.5
    # Should not settle since execution never started
    mock_engine.refund_tokens.assert_not_called()


class FailingAdapter(BaseLLMAdapter):
    """Adapter that fails during execution."""
    def __init__(self, error_to_raise: Exception, **kwargs):
        super().__init__(model="fail-model", provider="fail-provider", retry_attempts=0, **kwargs)
        self.error_to_raise = error_to_raise

    async def _execute_completion(self, messages, max_tokens, temperature, **kwargs):
        raise self.error_to_raise


def test_two_phase_rate_limiter_failure_settlement():
    mock_engine = MagicMock()
    mock_engine.acquire_rate_limit.return_value = MagicMock(granted=True)

    err = LLMProviderError("Upstream 503 Service Unavailable", status_code=503)
    adapter = FailingAdapter(error_to_raise=err, state_engine=mock_engine)

    with pytest.raises(LLMProviderError):
        adapter.complete_sync(prompt="Failing invocation")

    # Settlement must be called with success=False and actual_tokens=0
    mock_engine.refund_tokens.assert_called_once()
    settle_args = mock_engine.refund_tokens.call_args[1]
    assert settle_args["success"] is False
    assert settle_args["actual_tokens"] == 0
    assert "Upstream 503" in settle_args["error_msg"]


# =============================================================================
# 4. Universal OpenAICompatibleAdapter HTTP Wire Tests
# =============================================================================

def test_openai_compatible_adapter_http_success():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-sk-123"
        body = json.loads(request.read())
        assert body["model"] == "test-llm"
        assert len(body["messages"]) == 2

        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "model": "test-llm",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "All invariants satisfied.",
                            "reasoning_content": "Detailed step-by-step reasoning trace.",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 150,
                    "completion_tokens": 50,
                    "total_tokens": 200,
                },
            },
        )

    transport = httpx.MockTransport(mock_handler)
    client = httpx.AsyncClient(transport=transport)

    adapter = OpenAICompatibleAdapter(
        model="test-llm",
        provider="custom_gateway",
        api_key="test-sk-123",
        base_url="https://mock.gateway.ai/v1",
        http_client=client,
    )

    resp = adapter.complete_sync(prompt="Analyze invariants", system="You are an auditor")
    assert resp.content == "All invariants satisfied."
    assert resp.reasoning_content == "Detailed step-by-step reasoning trace."
    assert resp.total_tokens == 200
    assert resp.provider == "custom_gateway"


def test_openai_compatible_adapter_reasoning_fallback_when_content_empty():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "reasoning_content": "Reasoning trace is the actual answer.",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    adapter = OpenAICompatibleAdapter(
        model="reasoner-model",
        provider="reasoner-provider",
        base_url="https://mock.ai/v1",
        http_client=client,
    )

    resp = adapter.complete_sync(prompt="Thinking prompt")
    assert resp.content == "Reasoning trace is the actual answer."
    assert resp.reasoning_content == "Reasoning trace is the actual answer."


def test_openai_compatible_adapter_http_429_rate_limit():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "8.5"},
            text='{"error": {"message": "Rate limit reached for requests per minute"}}',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    adapter = OpenAICompatibleAdapter(
        model="rate-limited-model",
        provider="groq_mock",
        base_url="https://mock.ai/v1",
        http_client=client,
        retry_attempts=0,
    )

    with pytest.raises(LLMRateLimitError) as exc_info:
        adapter.complete_sync(prompt="Trigger 429")

    assert exc_info.value.retry_after == 8.5
    assert "Rate limit exceeded" in str(exc_info.value)


def test_openai_compatible_adapter_http_401_auth_error():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized: Invalid API key")

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    adapter = OpenAICompatibleAdapter(
        model="secure-model",
        provider="auth_mock",
        base_url="https://mock.ai/v1",
        http_client=client,
        retry_attempts=0,
    )

    with pytest.raises(LLMAuthError) as exc_info:
        adapter.complete_sync(prompt="Invalid key test")

    assert "Authentication failed" in str(exc_info.value)


def test_openai_compatible_adapter_http_500_provider_error():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    adapter = OpenAICompatibleAdapter(
        model="failing-model",
        provider="500_mock",
        base_url="https://mock.ai/v1",
        http_client=client,
        retry_attempts=0,
    )

    with pytest.raises(LLMProviderError) as exc_info:
        adapter.complete_sync(prompt="Trigger 502")

    assert "Upstream provider error" in str(exc_info.value)
    assert exc_info.value.status_code == 502


# =============================================================================
# 5. Constellation Gateway Adapter Preset Tests
# =============================================================================

def test_constellation_adapter_initialization_and_url():
    adapter = ConstellationAdapter(
        model="constellation/glm-5-3-flash-free",
        api_key="constellation-mock-key",
    )
    assert adapter.provider_name == "constellation"
    assert adapter.model_name == "constellation/glm-5-3-flash-free"
    assert "constellationgate.ai" in adapter.base_url
    assert adapter.max_diff_tokens == 14000


def test_constellation_adapter_execution():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "GLM architectural verdict"}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    adapter = ConstellationAdapter(
        model="constellation/glm-5-3-flash-free",
        api_key="test-key",
        http_client=client,
    )

    resp = adapter.complete_sync("Evaluate system structure")
    assert resp.content == "GLM architectural verdict"
    assert resp.provider == "constellation"


# =============================================================================
# 6. Groq Adapter & AST-Aware Diff Clamping Tests
# =============================================================================

def test_groq_adapter_initialization():
    adapter = GroqAdapter(api_key="groq-mock-key")
    assert adapter.provider_name == "groq"
    assert adapter.model_name == "llama-3.3-70b-versatile"
    assert adapter.max_diff_tokens == 5500


def test_diff_clamper_small_diff_unmodified():
    small_diff = (
        "diff --git a/src/math.py b/src/math.py\n"
        "--- a/src/math.py\n"
        "+++ b/src/math.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def add(a, b):\n"
        "+    # Added comment\n"
        "     return a + b\n"
    )
    clamped = clamp_diff(small_diff, max_tokens=5500)
    assert clamped == small_diff


def test_diff_clamper_prunes_context_lines():
    # Construct diff with 100 consecutive context lines
    context_lines = "\n".join(f" line_{i} = {i}" for i in range(100))
    diff_with_lots_of_context = (
        "diff --git a/src/large.py b/src/large.py\n"
        "--- a/src/large.py\n"
        "+++ b/src/large.py\n"
        "@@ -1,100 +1,102 @@\n"
        f"{context_lines}\n"
        "+new_var = 42\n"
    )

    initial_tokens = estimate_tokens(diff_with_lots_of_context)
    # Set limit below initial tokens to force pruning
    clamper = DiffClamper(max_tokens=initial_tokens // 2)
    clamped = clamper.clamp(diff_with_lots_of_context)

    assert estimate_tokens(clamped) <= initial_tokens // 2
    assert "context lines omitted" in clamped
    assert "+new_var = 42" in clamped


def test_diff_clamper_collapses_function_bodies_preserving_signatures():
    # Construct a diff introducing a 200-line function
    body_lines = "\n".join(f"+        x_{i} = compute_val({i})" for i in range(200))
    large_func_diff = (
        "diff --git a/src/service.py b/src/service.py\n"
        "--- a/src/service.py\n"
        "+++ b/src/service.py\n"
        "@@ -10,5 +10,210 @@\n"
        "+def massive_computation(data: dict) -> int:\n"
        '+    """Critical docstring explaining algorithm invariants."""\n'
        f"{body_lines}\n"
        "+    return 42\n"
    )

    # Clamping with a 500-token limit
    clamped = clamp_diff(large_func_diff, max_tokens=500)
    assert estimate_tokens(clamped) <= 500
    # Signature and docstring must be preserved
    assert "def massive_computation(data: dict) -> int:" in clamped
    assert "Critical docstring explaining algorithm invariants." in clamped
    assert "[COLLAPSED:" in clamped
    assert "body lines omitted" in clamped


def test_diff_clamper_multi_file_prunes_tests_and_docs_first():
    # Multi-file diff: 1 core file + 1 test file + 1 doc file
    src_hunk = "\n".join(f"+    core_op_{i}()" for i in range(50))
    test_hunk = "\n".join(f"+    def test_case_{i}(): pass" for i in range(50))
    doc_hunk = "\n".join(f"+# Documentation section {i}" for i in range(50))

    multi_file_diff = (
        "diff --git a/src/core.py b/src/core.py\n"
        "--- a/src/core.py\n"
        "+++ b/src/core.py\n"
        "@@ -1,5 +1,55 @@\n"
        f"{src_hunk}\n"
        "diff --git a/tests/test_core.py b/tests/test_core.py\n"
        "--- a/tests/test_core.py\n"
        "+++ b/tests/test_core.py\n"
        "@@ -1,5 +1,55 @@\n"
        f"{test_hunk}\n"
        "diff --git a/docs/guide.md b/docs/guide.md\n"
        "--- a/docs/guide.md\n"
        "+++ b/docs/guide.md\n"
        "@@ -1,5 +1,55 @@\n"
        f"{doc_hunk}\n"
    )

    initial_tokens = estimate_tokens(multi_file_diff)
    clamped = clamp_diff(multi_file_diff, max_tokens=initial_tokens // 2)

    assert estimate_tokens(clamped) <= initial_tokens // 2
    # Core source diff must be maintained or prioritized
    assert "src/core.py" in clamped
    # Tests or docs should have been stubbed out
    assert "omitted to satisfy token ceiling" in clamped or "omitted" in clamped


def test_diff_clamper_strict_5500_ceiling_on_giant_diff():
    # Construct an extreme 40,000-token synthetic diff
    giant_diff_lines = ["diff --git a/src/giant.py b/src/giant.py", "--- a/src/giant.py", "+++ b/src/giant.py", "@@ -1,1 +1,5000 @@"]
    for i in range(2500):
        giant_diff_lines.append(f"+def func_{i}(x: int) -> int:\n+    return x * {i}")

    giant_diff = "\n".join(giant_diff_lines)
    assert estimate_tokens(giant_diff) > 10000

    groq_adapter = GroqAdapter(api_key="mock")
    clamped = groq_adapter.clamp_payload(giant_diff)

    assert estimate_tokens(clamped) <= 5500
    assert len(clamped) > 500


# =============================================================================
# 7. Decoupled Arbiter & Decoupling Invariants
# =============================================================================

def test_identify_model_family():
    assert identify_model_family("constellation/glm-5-3-flash-free") == "glm"
    assert identify_model_family("constellation/qwen3-8-flash-free") == "qwen"
    assert identify_model_family("llama-3.3-70b-versatile") == "llama"
    assert identify_model_family("deepseek/deepseek-chat") == "deepseek"
    assert identify_model_family("deepseek-reasoner") == "deepseek"
    assert identify_model_family("anthropic/claude-3.5-sonnet") == "anthropic"


def test_decoupled_arbiter_assertion_success():
    arbiter = DecoupledArbiterAdapter(model="deepseek/deepseek-chat", api_key="test-key")
    assert arbiter.is_decoupled is True
    assert arbiter.model_family == "deepseek"

    juror_panel = [
        "constellation/glm-5-3-flash-free",
        "constellation/qwen3-8-flash-free",
        "llama-3.3-70b-versatile",
    ]
    # DeepSeek Arbiter is distinct from GLM, Qwen, and Llama
    assert arbiter.assert_decoupled_from(juror_panel) is True


def test_decoupled_arbiter_assertion_raises_on_coupling():
    arbiter = DecoupledArbiterAdapter(model="deepseek/deepseek-chat", api_key="test-key")

    coupled_panel = [
        "constellation/glm-5-3-flash-free",
        "deepseek/deepseek-v3",  # Contaminates panel with arbiter's family!
    ]

    with pytest.raises(ArbiterCouplingError) as exc_info:
        arbiter.assert_decoupled_from(coupled_panel, strict=True)

    assert "Decoupled Arbiter mandate violated" in str(exc_info.value)
    assert "overlaps with panel juror" in str(exc_info.value)

    # When strict=False, it should warn and return True without raising
    assert arbiter.assert_decoupled_from(coupled_panel, strict=False) is True


# =============================================================================
# 8. AdapterRegistry & Mock Mode Tests
# =============================================================================

def test_adapter_registry_resolves_roles():
    registry = AdapterRegistry()

    alpha = registry.get_adapter("alpha")
    assert isinstance(alpha, ConstellationAdapter)
    assert "glm" in alpha.model.lower()

    beta = registry.get_adapter("beta")
    assert isinstance(beta, ConstellationAdapter)
    assert "qwen" in beta.model.lower()

    gamma = registry.get_adapter("gamma")
    assert isinstance(gamma, GroqAdapter)
    assert "llama" in gamma.model.lower()

    arbiter = registry.get_adapter("arbiter")
    assert isinstance(arbiter, DecoupledArbiterAdapter)
    assert "deepseek" in arbiter.model.lower()


def test_adapter_registry_caching():
    registry = AdapterRegistry()
    first = registry.get_adapter("alpha")
    second = registry.get_adapter("alpha")
    assert first is second

    registry.clear()
    third = registry.get_adapter("alpha")
    assert third is not first


def test_adapter_registry_mock_mode_and_fixtures():
    registry = AdapterRegistry(mock_mode=True)

    # Register custom mock responses
    registry.register_mock_response(
        "alpha",
        json.dumps({
            "verdict": "APPROVED",
            "score": 9.5,
            "readiness_score": 9.5,
            "summary": "Alpha mock architecture approved.",
        }),
    )

    adapter = registry.get_adapter("alpha")
    assert isinstance(adapter, MockLLMAdapter)

    resp = adapter.complete_sync(prompt="Mock request")
    assert resp.provider == "mock"
    assert "Alpha mock architecture approved" in resp.content
    assert len(adapter.call_history) == 1
    assert adapter.call_history[0]["messages"][-1]["content"] == "Mock request"


def test_adapter_registry_universal_custom_provider():
    # User configures an arbitrary local vLLM or Ollama endpoint
    registry = AdapterRegistry()
    custom_adapter = registry.get_adapter(
        role_or_provider="ollama",
        model_id="qwen2.5-coder:32b",
    )
    assert isinstance(custom_adapter, OpenAICompatibleAdapter)
    assert custom_adapter.model == "qwen2.5-coder:32b"
    assert custom_adapter.provider == "ollama"
    assert "11434" in custom_adapter.base_url


def test_cerebras_and_openrouter_adapters():
    cerebras = CerebrasAdapter(api_key="cerebras-mock")
    assert cerebras.provider_name == "cerebras"
    assert "cerebras.ai" in cerebras.base_url

    openrouter = OpenRouterAdapter(api_key="openrouter-mock")
    assert openrouter.provider_name == "openrouter"
    assert "openrouter.ai" in openrouter.base_url
    assert "HTTP-Referer" in openrouter.default_headers
