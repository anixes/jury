"""
tests/unit/test_rate_limiter.py - Unit tests for TwoPhaseRateLimiter and Circuit Breakers.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from jury.state.engine import JuryStateEngine
from jury.state.rate_limiter import RateLimitAcquisition, TwoPhaseRateLimiter


@pytest.fixture
def state_engine(tmp_path: Path) -> JuryStateEngine:
    db_file = tmp_path / "state.db"
    return JuryStateEngine(db_path=db_file)


def test_rate_limiter_admissibility_and_token_deduction(state_engine: JuryStateEngine):
    """Verify initial token reservation deducts RPM, TPM, and increments in-flight count."""
    limiter = state_engine.rate_limiter
    acq = limiter.check_admissibility("groq:llama-3.3-70b-versatile", estimated_tokens=2000)

    assert acq.granted is True
    assert acq.bucket_id == "groq:llama-3.3-70b-versatile"
    assert acq.circuit_state == "CLOSED"
    assert acq.in_flight == 1
    assert acq.tpm_remaining == pytest.approx(10000.0, abs=10.0)
    assert acq.rpm_remaining == pytest.approx(29.0, abs=0.5)


def test_rate_limiter_concurrency_ceiling(state_engine: JuryStateEngine):
    """Verify provider concurrency ceiling rejects concurrent requests beyond limit."""
    limiter = state_engine.rate_limiter

    # Groq has strict concurrency_limit = 1
    acq1 = limiter.check_admissibility("groq", estimated_tokens=2000)
    assert acq1.granted is True
    assert acq1.in_flight == 1

    # Second concurrent reservation must be rejected
    acq2 = limiter.check_admissibility("groq", estimated_tokens=2000)
    assert acq2.granted is False
    assert acq2.wait_seconds > 0
    assert "Concurrency ceiling reached" in (acq2.reason or "")


def test_two_phase_settle_refund_on_success(state_engine: JuryStateEngine):
    """Verify Two-Phase Settlement refunds unused tokens on success."""
    limiter = state_engine.rate_limiter

    # Reserve 5,000 tokens from 12,000 TPM Groq bucket
    acq = limiter.check_admissibility("groq", estimated_tokens=5000)
    assert acq.granted is True
    assert acq.tpm_remaining == pytest.approx(7000.0, abs=10.0)

    # Actual usage was only 1,500 tokens -> 3,500 tokens should be refunded
    limiter.settle(
        provider_or_bucket="groq",
        estimated_tokens=5000,
        actual_tokens=1500,
        success=True,
    )

    conn = state_engine.get_connection()
    try:
        row = conn.execute(
            "SELECT in_flight_count, tpm_current, circuit_state FROM rate_limit_buckets WHERE bucket_id LIKE 'groq%'"
        ).fetchone()
        assert row["in_flight_count"] == 0
        assert row["tpm_current"] == pytest.approx(10500.0, abs=20.0)
        assert row["circuit_state"] == "CLOSED"
    finally:
        conn.close()


def test_two_phase_settle_failure_trips_circuit_breaker(state_engine: JuryStateEngine):
    """Verify failed request refunds all estimated tokens and trips circuit breaker to OPEN."""
    limiter = state_engine.rate_limiter

    acq = limiter.check_admissibility("constellation", estimated_tokens=10000)
    assert acq.granted is True

    # Upstream returned 429 with Retry-After 10s
    limiter.settle(
        provider_or_bucket="constellation",
        estimated_tokens=10000,
        actual_tokens=0,
        success=False,
        retry_after=10.0,
        error_msg="Rate limit exceeded (HTTP 429)",
    )

    conn = state_engine.get_connection()
    try:
        row = conn.execute(
            "SELECT in_flight_count, tpm_current, circuit_state, cooldown_until, consecutive_failures FROM rate_limit_buckets WHERE bucket_id LIKE 'constellation%'"
        ).fetchone()
        assert row["in_flight_count"] == 0
        assert row["circuit_state"] == "OPEN"
        assert row["consecutive_failures"] == 1
        assert row["cooldown_until"] > time.time()
    finally:
        conn.close()

    # Immediate next check should be denied due to OPEN circuit
    acq2 = limiter.check_admissibility("constellation", estimated_tokens=1000)
    assert acq2.granted is False
    assert acq2.circuit_state == "OPEN"
    assert "Circuit OPEN cooldown" in (acq2.reason or "")
    assert acq2.wait_seconds > 0


def test_circuit_breaker_half_open_recovery(state_engine: JuryStateEngine):
    """Verify circuit breaker transitions from OPEN -> HALF_OPEN -> CLOSED upon recovery."""
    limiter = state_engine.rate_limiter

    # Trip breaker with short past cooldown
    with state_engine.lock:
        conn = state_engine.get_connection()
        try:
            conn.execute(
                """
                UPDATE rate_limit_buckets
                SET circuit_state = 'OPEN',
                    cooldown_until = ?,
                    consecutive_failures = 2
                WHERE bucket_id = 'groq:llama-3.3-70b-versatile'
                """,
                (time.time() - 1.0,),  # Cooldown expired 1 second ago
            )
            conn.commit()
        finally:
            conn.close()

    # Admissibility check should transition breaker to HALF_OPEN and grant trial probe
    acq = limiter.check_admissibility("groq:llama-3.3-70b-versatile", estimated_tokens=1000)
    assert acq.granted is True
    assert acq.circuit_state == "HALF_OPEN"
    assert acq.in_flight == 1

    # Settle probe successfully -> transitions to CLOSED
    limiter.settle(
        provider_or_bucket="groq:llama-3.3-70b-versatile",
        estimated_tokens=1000,
        actual_tokens=900,
        success=True,
    )

    conn = state_engine.get_connection()
    try:
        row = conn.execute(
            "SELECT circuit_state, consecutive_failures FROM rate_limit_buckets WHERE bucket_id = 'groq:llama-3.3-70b-versatile'"
        ).fetchone()
        assert row["circuit_state"] == "CLOSED"
        assert row["consecutive_failures"] == 0
    finally:
        conn.close()


def test_continuous_leaky_replenishment_math(state_engine: JuryStateEngine):
    """Verify continuous token bucket math replenishes tokens proportionally over elapsed time."""
    limiter = state_engine.rate_limiter

    # Manually drain TPM tokens and set past timestamp
    with state_engine.lock:
        conn = state_engine.get_connection()
        try:
            conn.execute(
                """
                UPDATE rate_limit_buckets
                SET tpm_current = 0.0,
                    tpm_last_refill = ?
                WHERE bucket_id = 'groq:llama-3.3-70b-versatile'
                """,
                (time.time() - 30.0,),  # 30 seconds ago
            )
            conn.commit()
        finally:
            conn.close()

    # For Groq: 12,000 TPM ceiling = 200 tokens/sec. In 30 sec = ~6,000 tokens replenished.
    acq = limiter.check_admissibility("groq:llama-3.3-70b-versatile", estimated_tokens=1000)
    assert acq.granted is True
    assert acq.tpm_remaining >= 4800.0


def test_dynamic_bucket_creation_for_unknown_provider(state_engine: JuryStateEngine):
    """Verify requesting an unseeded provider dynamically creates a default bucket."""
    limiter = state_engine.rate_limiter
    acq = limiter.check_admissibility("unknown_ai_provider", estimated_tokens=500)

    assert acq.granted is True
    assert acq.bucket_id == "unknown_ai_provider:default"

    conn = state_engine.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM rate_limit_buckets WHERE bucket_id = 'unknown_ai_provider:default'"
        ).fetchone()
        assert row is not None
        assert row["rpm_limit"] == 30.0
    finally:
        conn.close()


def test_reserve_timeout_on_depleted_bucket(state_engine: JuryStateEngine):
    """Verify reserve() returns granted=False with timeout when bucket is heavily starved."""
    limiter = state_engine.rate_limiter

    # Completely drain bucket and hold concurrency
    with state_engine.lock:
        conn = state_engine.get_connection()
        try:
            conn.execute(
                """
                UPDATE rate_limit_buckets
                SET tpm_current = 0.0,
                    rpm_current = 0.0,
                    in_flight_count = 5,
                    concurrency_limit = 1,
                    tpm_last_refill = ?
                WHERE bucket_id = 'groq:llama-3.3-70b-versatile'
                """,
                (time.time(),),
            )
            conn.commit()
        finally:
            conn.close()

    acq = limiter.reserve("groq:llama-3.3-70b-versatile", estimated_tokens=10000, max_wait_seconds=0.2)
    assert acq.granted is False
    assert "Timeout after 0.2s" in (acq.reason or "")
