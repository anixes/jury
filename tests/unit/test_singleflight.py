"""
tests/unit/test_singleflight.py - Unit tests for SingleflightCoalescer and distributed lease recovery.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from jury.state.engine import DeliberationRecord, JuryStateEngine
from jury.state.singleflight import SingleflightCoalescer, SingleflightTicket


@pytest.fixture
def state_engine(tmp_path: Path) -> JuryStateEngine:
    db_file = tmp_path / "state.db"
    return JuryStateEngine(db_path=db_file)


def test_singleflight_leader_election_and_follower_coalescing(state_engine: JuryStateEngine):
    """Verify first caller is elected Leader and second caller coalesces as Follower."""
    coalescer = state_engine.singleflight
    cache_key = "test_sf_key_100"

    # Process 1: Elected Leader
    tkt1 = coalescer.coalesce_or_lead(
        cache_key=cache_key,
        command="review",
        intent="Review API refactor",
    )
    assert tkt1.is_leader is True
    assert tkt1.status == "IN_FLIGHT"
    assert tkt1.cache_key == cache_key
    assert tkt1.ticket_id.startswith("tkt_")

    # Process 2: Coalesces as Follower for the same key
    tkt2 = coalescer.coalesce_or_lead(
        cache_key=cache_key,
        command="review",
        intent="Review API refactor",
    )
    assert tkt2.is_leader is False
    assert tkt2.status == "IN_FLIGHT"
    assert tkt2.ticket_id == tkt1.ticket_id


def test_singleflight_heartbeat_extension(state_engine: JuryStateEngine):
    """Verify leader can actively extend its heartbeat and lease."""
    coalescer = state_engine.singleflight
    cache_key = "test_sf_hb_key"

    ticket = coalescer.coalesce_or_lead(cache_key=cache_key)
    assert ticket.is_leader is True

    time.sleep(0.1)
    extended = coalescer.heartbeat(ticket.ticket_id, extension_seconds=30.0)
    assert extended is True

    conn = state_engine.get_connection()
    try:
        row = conn.execute(
            "SELECT heartbeat_at, expires_at FROM jury_tickets WHERE ticket_id = ?",
            (ticket.ticket_id,),
        ).fetchone()
        assert row["heartbeat_at"] > ticket.heartbeat_at
        assert row["expires_at"] > time.time()
    finally:
        conn.close()


def test_singleflight_follower_await_completion(state_engine: JuryStateEngine):
    """Verify follower cleanly polls and retrieves verdict upon leader completion."""
    coalescer = state_engine.singleflight
    cache_key = "test_sf_await_key"

    ticket = coalescer.coalesce_or_lead(cache_key=cache_key)
    assert ticket.is_leader is True

    outcome = []

    def _follower_poll():
        try:
            status, _ = coalescer.await_ticket(
                ticket_id=ticket.ticket_id,
                cache_key=cache_key,
                timeout_seconds=5.0,
                poll_interval=0.05,
            )
            outcome.append(status)
        except Exception as e:
            outcome.append(str(e))

    thread = threading.Thread(target=_follower_poll)
    thread.start()

    # Simulate leader computing deliberation for 150ms
    time.sleep(0.15)
    # Leader caches record and marks ticket completed
    record = DeliberationRecord(verdict="APPROVED", deliberation_markdown="Consensus reached.")
    state_engine.cache_put(cache_key, record)
    coalescer.mark_completed(ticket.ticket_id)

    thread.join(timeout=3.0)
    assert outcome in [["COMPLETED"], ["CACHE_HIT"]]


def test_singleflight_leader_failure_propagates_to_follower(state_engine: JuryStateEngine):
    """Verify leader failure raises RuntimeError in follower."""
    coalescer = state_engine.singleflight
    cache_key = "test_sf_fail_key"

    ticket = coalescer.coalesce_or_lead(cache_key=cache_key)
    assert ticket.is_leader is True

    outcome = []

    def _follower():
        try:
            coalescer.await_ticket(
                ticket_id=ticket.ticket_id,
                cache_key=cache_key,
                timeout_seconds=2.0,
                poll_interval=0.05,
            )
        except Exception as e:
            outcome.append(type(e).__name__)

    thread = threading.Thread(target=_follower)
    thread.start()

    time.sleep(0.1)
    coalescer.mark_failed(ticket.ticket_id, "Upstream network timeout")

    thread.join(timeout=3.0)
    assert outcome == ["RuntimeError"]


def test_singleflight_dead_leader_takeover(state_engine: JuryStateEngine):
    """Verify dead leader (heartbeat > 15s stale) is taken over by another process."""
    coalescer = state_engine.singleflight
    cache_key = "test_dead_leader_key"

    # Leader 1 creates ticket
    leader1_ticket = coalescer.coalesce_or_lead(cache_key=cache_key)
    assert leader1_ticket.is_leader is True

    # Simulate Leader 1 crash: freeze heartbeat 20 seconds in the past
    with state_engine.lock:
        conn = state_engine.get_connection()
        try:
            conn.execute(
                """
                UPDATE jury_tickets
                SET heartbeat_at = ?,
                    expires_at = ?
                WHERE ticket_id = ?
                """,
                (time.time() - 20.0, time.time() - 5.0, leader1_ticket.ticket_id),
            )
            conn.commit()
        finally:
            conn.close()

    # Next process requests same key: detects dead leader and executes takeover
    leader2_ticket = coalescer.coalesce_or_lead(cache_key=cache_key)
    assert leader2_ticket.is_leader is True
    assert leader2_ticket.ticket_id == leader1_ticket.ticket_id
    assert leader2_ticket.heartbeat_at > time.time() - 2.0


def test_execute_with_singleflight_orchestration(state_engine: JuryStateEngine):
    """Verify execute_with_singleflight executes function once across 4 concurrent threads."""
    call_count = 0
    lock = threading.Lock()
    cache_key = "test_orchestrator_coalescing_key"

    def _mock_deliberate() -> DeliberationRecord:
        nonlocal call_count
        with lock:
            call_count += 1
        time.sleep(0.2)  # Simulate network latency
        return DeliberationRecord(
            verdict="APPROVED",
            deliberation_markdown="Multi-model synthesis passed.",
        )

    results = []
    threads = []

    def _worker():
        res = state_engine.execute_with_singleflight(
            cache_key=cache_key,
            intent="Synthesize migration plan",
            command="plan",
            deliberate_fn=_mock_deliberate,
            ttl_seconds=300,
            timeout_seconds=5.0,
        )
        results.append(res.verdict)

    for _ in range(4):
        t = threading.Thread(target=_worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=5.0)

    # All 4 threads must return APPROVED, while remote deliberate was called exactly ONCE
    assert len(results) == 4
    assert all(v == "APPROVED" for v in results)
    assert call_count == 1, f"Expected exactly 1 remote invocation, got {call_count}"
