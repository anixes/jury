"""
src/jury/state/rate_limiter.py - Two-Phase Continuous Rate Limiter & Circuit Breaker.

Implements continuous leaky/token bucket replenishment (RPM and TPM) with Two-Phase
Settlement (pre-flight upper-bound token reservation, post-flight reconciliation & refund)
and tri-state circuit breakers (CLOSED, OPEN, HALF_OPEN) with Retry-After adherence.
"""

from __future__ import annotations

import random
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple, Union


@dataclass
class RateLimitAcquisition:
    """Outcome of a rate limit reservation attempt."""
    granted: bool
    wait_seconds: float = 0.0
    bucket_id: str = ""
    circuit_state: str = "CLOSED"
    reason: Optional[str] = None
    tpm_remaining: float = 0.0
    rpm_remaining: float = 0.0
    in_flight: int = 0


class TwoPhaseRateLimiter:
    """
    Continuous Token Bucket Governor with Two-Phase Settlement & Circuit Breakers.
    Uses SQLite BEGIN IMMEDIATE transactions within OS file locks for cross-process
    swarm coordination.
    """

    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        lock_context: Callable[[], any],
    ):
        """
        Initialize rate limiter with connection factory and cross-process file lock.
        """
        self._get_connection = conn_factory
        self._lock = lock_context

    def _resolve_bucket_id(self, conn: sqlite3.Connection, provider_or_bucket: str) -> str:
        """
        Resolve provider name or bucket ID to a canonical bucket_id in rate_limit_buckets.
        Creates a default bucket if none exists.
        """
        target = provider_or_bucket.strip().lower()

        # Direct match by bucket_id
        row = conn.execute(
            "SELECT bucket_id FROM rate_limit_buckets WHERE bucket_id = ?",
            (target,),
        ).fetchone()
        if row:
            return row["bucket_id"]

        # Match by provider_id
        row = conn.execute(
            "SELECT bucket_id FROM rate_limit_buckets WHERE provider_id = ? ORDER BY bucket_id ASC LIMIT 1",
            (target,),
        ).fetchone()
        if row:
            return row["bucket_id"]

        # Prefix match (e.g. 'groq' matches 'groq:llama-3.3-70b-versatile')
        row = conn.execute(
            "SELECT bucket_id FROM rate_limit_buckets WHERE bucket_id LIKE ? LIMIT 1",
            (f"{target}:%",),
        ).fetchone()
        if row:
            return row["bucket_id"]

        # If not found, dynamically seed a safe default bucket
        now = time.time()
        bucket_id = f"{target}:default"
        rpm = 30.0
        tpm = 12000.0 if "groq" in target else 120000.0
        concurrency = 1 if "groq" in target else 2

        conn.execute(
            """
            INSERT OR IGNORE INTO rate_limit_buckets
            (bucket_id, provider_id, model_id, rpm_limit, rpm_current, rpm_last_refill,
             tpm_limit, tpm_current, tpm_last_refill, concurrency_limit, in_flight_count,
             circuit_state, updated_at)
            VALUES (?, ?, 'default', ?, ?, ?, ?, ?, ?, ?, 0, 'CLOSED', ?)
            """,
            (bucket_id, target, rpm, rpm, now, tpm, tpm, now, concurrency, now),
        )
        return bucket_id

    def check_admissibility(
        self,
        provider_or_bucket: str,
        estimated_tokens: int,
    ) -> RateLimitAcquisition:
        """
        Single-shot atomic check and reserve.
        Returns RateLimitAcquisition indicating whether quota was granted.
        """
        with self._lock():
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                now = time.time()
                bucket_id = self._resolve_bucket_id(conn, provider_or_bucket)

                row = conn.execute(
                    "SELECT * FROM rate_limit_buckets WHERE bucket_id = ?",
                    (bucket_id,),
                ).fetchone()

                if not row:
                    conn.commit()
                    return RateLimitAcquisition(
                        granted=True,
                        bucket_id=bucket_id,
                        reason="No bucket restrictions",
                    )

                circuit_state = row["circuit_state"]
                cooldown_until = row["cooldown_until"] or 0.0

                # 1. Circuit breaker evaluation
                if circuit_state == "OPEN":
                    if cooldown_until > now:
                        wait_sec = cooldown_until - now
                        conn.commit()
                        return RateLimitAcquisition(
                            granted=False,
                            wait_seconds=wait_sec,
                            bucket_id=bucket_id,
                            circuit_state="OPEN",
                            reason=f"Circuit OPEN cooldown ({wait_sec:.1f}s remaining)",
                            in_flight=row["in_flight_count"],
                        )
                    else:
                        # Cooldown has elapsed -> transition to HALF_OPEN probe state
                        circuit_state = "HALF_OPEN"
                        conn.execute(
                            "UPDATE rate_limit_buckets SET circuit_state = 'HALF_OPEN', updated_at = ? WHERE bucket_id = ?",
                            (now, bucket_id),
                        )

                # In HALF_OPEN state, restrict active in-flight count to 0 or 1
                max_in_flight = 1 if circuit_state == "HALF_OPEN" else row["concurrency_limit"]

                # 2. Continuous token replenishment
                dt_rpm = max(0.0, now - row["rpm_last_refill"])
                rpm_rate = row["rpm_limit"] / 60.0
                accrued_rpm = min(row["rpm_limit"], row["rpm_current"] + rpm_rate * dt_rpm)

                dt_tpm = max(0.0, now - row["tpm_last_refill"])
                tpm_rate = row["tpm_limit"] / 60.0
                accrued_tpm = min(row["tpm_limit"], row["tpm_current"] + tpm_rate * dt_tpm)

                in_flight = row["in_flight_count"]

                # 3. Admissibility check
                has_rpm = accrued_rpm >= 1.0
                has_tpm = accrued_tpm >= estimated_tokens
                has_concurrency = in_flight < max_in_flight

                if has_rpm and has_tpm and has_concurrency:
                    # Grant reservation and deduct
                    new_rpm = accrued_rpm - 1.0
                    new_tpm = accrued_tpm - estimated_tokens
                    new_in_flight = in_flight + 1

                    conn.execute(
                        """
                        UPDATE rate_limit_buckets
                        SET rpm_current = ?,
                            rpm_last_refill = ?,
                            tpm_current = ?,
                            tpm_last_refill = ?,
                            in_flight_count = ?,
                            updated_at = ?
                        WHERE bucket_id = ?
                        """,
                        (new_rpm, now, new_tpm, now, new_in_flight, now, bucket_id),
                    )
                    conn.commit()
                    return RateLimitAcquisition(
                        granted=True,
                        wait_seconds=0.0,
                        bucket_id=bucket_id,
                        circuit_state=circuit_state,
                        tpm_remaining=new_tpm,
                        rpm_remaining=new_rpm,
                        in_flight=new_in_flight,
                    )

                # 4. Quota insufficient - calculate required wait time
                wait_rpm = (1.0 - accrued_rpm) / rpm_rate if not has_rpm else 0.0
                wait_tpm = (estimated_tokens - accrued_tpm) / tpm_rate if not has_tpm else 0.0
                wait_conc = 0.5 if not has_concurrency else 0.0

                wait_sec = max(wait_rpm, wait_tpm, wait_conc, 0.1)

                # Persist accrued tokens without deduction
                conn.execute(
                    """
                    UPDATE rate_limit_buckets
                    SET rpm_current = ?,
                        rpm_last_refill = ?,
                        tpm_current = ?,
                        tpm_last_refill = ?,
                        updated_at = ?
                    WHERE bucket_id = ?
                    """,
                    (accrued_rpm, now, accrued_tpm, now, now, bucket_id),
                )
                conn.commit()

                reason = []
                if not has_rpm:
                    reason.append(f"RPM exhausted ({accrued_rpm:.1f}/{row['rpm_limit']})")
                if not has_tpm:
                    reason.append(f"TPM exhausted ({accrued_tpm:.0f}/{estimated_tokens} needed)")
                if not has_concurrency:
                    reason.append(f"Concurrency ceiling reached ({in_flight}/{max_in_flight})")

                return RateLimitAcquisition(
                    granted=False,
                    wait_seconds=wait_sec,
                    bucket_id=bucket_id,
                    circuit_state=circuit_state,
                    reason=", ".join(reason),
                    tpm_remaining=accrued_tpm,
                    rpm_remaining=accrued_rpm,
                    in_flight=in_flight,
                )
            finally:
                conn.close()

    def reserve(
        self,
        provider_or_bucket: str,
        estimated_tokens: int,
        max_wait_seconds: float = 25.0,
        poll_interval: float = 0.1,
    ) -> RateLimitAcquisition:
        """
        Pre-flight reservation with exponential backoff & jitter wait loop.
        Blocks up to max_wait_seconds until tokens and concurrency become available.
        """
        start_time = time.time()
        last_acq = RateLimitAcquisition(granted=False, bucket_id=provider_or_bucket)

        while (time.time() - start_time) <= max_wait_seconds:
            acq = self.check_admissibility(provider_or_bucket, estimated_tokens)
            if acq.granted:
                return acq

            last_acq = acq
            remaining_time = max_wait_seconds - (time.time() - start_time)
            if remaining_time <= 0:
                break

            # Sleep duration bounded by required wait and remaining timeout
            sleep_time = min(acq.wait_seconds, remaining_time, 2.0)
            sleep_with_jitter = max(0.05, sleep_time + random.uniform(0.02, 0.08))
            time.sleep(sleep_with_jitter)

        return RateLimitAcquisition(
            granted=False,
            wait_seconds=last_acq.wait_seconds,
            bucket_id=last_acq.bucket_id,
            circuit_state=last_acq.circuit_state,
            reason=f"Timeout after {max_wait_seconds:.1f}s: {last_acq.reason}",
            tpm_remaining=last_acq.tpm_remaining,
            rpm_remaining=last_acq.rpm_remaining,
            in_flight=last_acq.in_flight,
        )

    def settle(
        self,
        provider_or_bucket: str,
        estimated_tokens: int,
        actual_tokens: int,
        success: bool = True,
        retry_after: Optional[float] = None,
        error_msg: Optional[str] = None,
    ) -> None:
        """
        Post-flight reconciliation & refund.
        - On success: refunds (estimated - actual) unused tokens, decrements in_flight,
          resets consecutive failures, closes circuit breaker.
        - On failure: refunds all estimated tokens, decrements in_flight, increments
          consecutive failures, trips circuit breaker to OPEN with backoff / Retry-After.
        """
        with self._lock():
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                now = time.time()
                bucket_id = self._resolve_bucket_id(conn, provider_or_bucket)

                row = conn.execute(
                    "SELECT * FROM rate_limit_buckets WHERE bucket_id = ?",
                    (bucket_id,),
                ).fetchone()

                if not row:
                    conn.commit()
                    return

                # Calculate refund
                if success:
                    refund = max(0.0, float(estimated_tokens - actual_tokens))
                    new_tpm = min(row["tpm_limit"], row["tpm_current"] + refund)
                    new_in_flight = max(0, row["in_flight_count"] - 1)

                    conn.execute(
                        """
                        UPDATE rate_limit_buckets
                        SET in_flight_count = ?,
                            tpm_current = ?,
                            consecutive_failures = 0,
                            circuit_state = 'CLOSED',
                            cooldown_until = NULL,
                            last_failure_reason = NULL,
                            last_success_at = ?,
                            updated_at = ?
                        WHERE bucket_id = ?
                        """,
                        (new_in_flight, new_tpm, now, now, bucket_id),
                    )
                else:
                    # On failure, refund all estimated tokens so budget is not wasted
                    refund = float(estimated_tokens)
                    new_tpm = min(row["tpm_limit"], row["tpm_current"] + refund)
                    new_in_flight = max(0, row["in_flight_count"] - 1)
                    failures = row["consecutive_failures"] + 1

                    if retry_after is not None and retry_after > 0:
                        cooldown_until = now + retry_after
                    else:
                        # Exponential backoff: 5s, 10s, 20s, 40s, max 60s + jitter
                        backoff = min(60.0, 5.0 * (2 ** min(failures - 1, 4))) + random.uniform(0.1, 0.5)
                        cooldown_until = now + backoff

                    conn.execute(
                        """
                        UPDATE rate_limit_buckets
                        SET in_flight_count = ?,
                            tpm_current = ?,
                            consecutive_failures = ?,
                            circuit_state = 'OPEN',
                            cooldown_until = ?,
                            last_failure_reason = ?,
                            updated_at = ?
                        WHERE bucket_id = ?
                        """,
                        (new_in_flight, new_tpm, failures, cooldown_until, error_msg, now, bucket_id),
                    )

                conn.commit()
            finally:
                conn.close()
