"""
tests/unit/test_state_engine.py - Unit tests for JuryStateEngine SQLite-WAL state engine.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from jury.state.engine import (
    AuditLogEntry,
    DeliberationRecord,
    JuryStateEngine,
)


@pytest.fixture
def temp_engine(tmp_path: Path) -> JuryStateEngine:
    """Fixture providing a fresh JuryStateEngine on an isolated temporary database."""
    db_file = tmp_path / "state.db"
    engine = JuryStateEngine(db_path=db_file)
    return engine


def test_state_engine_schema_and_pragmas(temp_engine: JuryStateEngine):
    """Verify SQLite WAL pragmas, tables, and version metadata upon initialization."""
    conn = temp_engine.get_connection()
    try:
        # Check WAL mode
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal"

        # Check synchronous NORMAL (1)
        sync_mode = conn.execute("PRAGMA synchronous;").fetchone()[0]
        assert sync_mode == 1

        # Check busy timeout
        timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert timeout >= 30000

        # Check schema version
        ver = conn.execute("PRAGMA user_version;").fetchone()[0]
        assert ver == 1

        # Check table existence
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        ]
        assert "rate_limit_buckets" in tables
        assert "jury_tickets" in tables
        assert "jury_cache" in tables
        assert "jury_audit_log" in tables

        # Check indexes
        indexes = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index';"
            ).fetchall()
        ]
        assert "idx_tickets_cache_status" in indexes
        assert "idx_tickets_heartbeat" in indexes
        assert "idx_cache_expires" in indexes
        assert "idx_audit_timestamp" in indexes
    finally:
        conn.close()


def test_default_buckets_preseeding(temp_engine: JuryStateEngine):
    """Verify standard provider buckets are pre-seeded with canonical limits."""
    conn = temp_engine.get_connection()
    try:
        groq_row = conn.execute(
            "SELECT * FROM rate_limit_buckets WHERE bucket_id = 'groq:llama-3.3-70b-versatile'"
        ).fetchone()
        assert groq_row is not None
        assert groq_row["tpm_limit"] == 12000.0
        assert groq_row["concurrency_limit"] == 1
        assert groq_row["circuit_state"] == "CLOSED"

        constellation_row = conn.execute(
            "SELECT * FROM rate_limit_buckets WHERE bucket_id = 'constellation:glm-5-3-flash-free'"
        ).fetchone()
        assert constellation_row is not None
        assert constellation_row["tpm_limit"] == 120000.0
        assert constellation_row["concurrency_limit"] == 2
    finally:
        conn.close()


def test_two_tier_transaction_commit_and_rollback(temp_engine: JuryStateEngine):
    """Verify transaction context manager commits on success and rolls back on failure."""
    future_exp = time.time() + 999.0
    # Successful transaction
    with temp_engine.transaction() as conn:
        conn.execute(
            """
            INSERT INTO jury_cache 
            (cache_key, created_at, expires_at, ttl_seconds, intent, git_head_sha,
             dirty_unstaged_hash, dirty_staged_hash, untracked_files_hash, config_hash,
             prompt_version, verdict, deliberation_markdown, computation_duration_ms,
             hit_count, last_accessed_at)
            VALUES ('k1', 1, ?, 300, 'intent1', 'h1', 'u1', 's1', 'o1', 'cfg', '3.0', 'APPROVED', 'md', 10, 0, 1)
            """,
            (future_exp,),
        )

    # Verify committed
    cached = temp_engine.cache_get("k1")
    assert cached is not None
    assert cached.verdict == "APPROVED"

    # Transaction with exception -> rollback
    with pytest.raises(ValueError, match="simulated failure"):
        with temp_engine.transaction() as conn:
            conn.execute(
                """
                INSERT INTO jury_cache 
                (cache_key, created_at, expires_at, ttl_seconds, intent, git_head_sha,
                 dirty_unstaged_hash, dirty_staged_hash, untracked_files_hash, config_hash,
                 prompt_version, verdict, deliberation_markdown, computation_duration_ms,
                 hit_count, last_accessed_at)
                VALUES ('k2', 1, ?, 300, 'intent2', 'h2', 'u2', 's2', 'o2', 'cfg', '3.0', 'APPROVED', 'md', 10, 0, 1)
                """,
                (future_exp,),
            )
            raise ValueError("simulated failure")

    # Verify rollback
    assert temp_engine.cache_get("k2") is None


def test_cache_crud_and_expiration(temp_engine: JuryStateEngine):
    """Verify cache storage, retrieval, TTL expiration, and deletion."""
    record = DeliberationRecord(
        verdict="APPROVED_WITH_CONDITIONS",
        deliberation_markdown="## Deliberation Summary\nApproved with nitpicks.",
        patch_unified_diff="--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n",
        sarif_json={"version": "2.1.0", "runs": []},
        verification_contract_json={"runner": "pytest", "timeout_seconds": 30},
        intent="Refactor authentication handler",
        git_head_sha="abcdef1234567890",
        ttl_seconds=300,
        tokens_saved=3500,
        computation_duration_ms=450,
    )

    # Put in cache
    temp_engine.cache_put("test_cache_key_1", record, ttl_seconds=300)

    # Retrieve
    cached = temp_engine.cache_get("test_cache_key_1")
    assert cached is not None
    assert cached.from_cache is True
    assert cached.verdict == "APPROVED_WITH_CONDITIONS"
    assert "Deliberation Summary" in cached.deliberation_markdown
    assert cached.patch_unified_diff is not None
    assert cached.sarif_json == {"version": "2.1.0", "runs": []}
    assert cached.tokens_saved == 3500
    assert cached.hit_count >= 1

    # Second read increments hit count
    cached_again = temp_engine.cache_get("test_cache_key_1")
    assert cached_again is not None
    assert cached_again.hit_count == cached.hit_count + 1

    # Test expired entry
    temp_engine.cache_put("expired_key", record, ttl_seconds=-10)
    assert temp_engine.cache_get("expired_key") is None

    # Test cache clearance
    deleted = temp_engine.clear_cache(expired_only=True)
    assert deleted >= 1
    # Active entry should remain
    assert temp_engine.cache_get("test_cache_key_1") is not None

    temp_engine.clear_cache(expired_only=False)
    assert temp_engine.cache_get("test_cache_key_1") is None


def test_deliberation_audit_logging(temp_engine: JuryStateEngine):
    """Verify writing and querying permanent deliberation audit entries."""
    entry1 = AuditLogEntry(
        ticket_id="tkt_audit_100",
        command="review",
        prompt="Review staged PR changes",
        diff_summary="src/auth.py (+15, -2)",
        verdict="APPROVED",
        juror_scores={"alpha": 95, "beta": 90, "gamma": 88},
        latency_ms=1240,
        tokens_used=4200,
        tokens_saved=1800,
        caller_process_id=1234,
        caller_tool="mcp:jury_review",
        cache_hit=False,
    )
    entry2 = AuditLogEntry(
        ticket_id="tkt_audit_101",
        command="plan",
        prompt="Synthesize migration architecture",
        diff_summary="",
        verdict="APPROVED_WITH_CONDITIONS",
        juror_scores={"alpha": 85, "beta": 80},
        latency_ms=2100,
        tokens_used=8900,
        tokens_saved=0,
        caller_process_id=1234,
        caller_tool="cli",
        cache_hit=False,
    )

    temp_engine.log_audit(entry1)
    temp_engine.log_audit(entry2)

    logs = temp_engine.get_audit_logs(limit=10)
    assert len(logs) == 2
    assert logs[0].ticket_id in ["tkt_audit_100", "tkt_audit_101"]

    # Filter by command
    review_logs = temp_engine.get_audit_logs(command="review")
    assert len(review_logs) == 1
    assert review_logs[0].ticket_id == "tkt_audit_100"
    assert review_logs[0].juror_scores["alpha"] == 95
    assert review_logs[0].tokens_used == 4200
    assert review_logs[0].caller_tool == "mcp:jury_review"


def test_state_dir_resolution_hierarchy(monkeypatch, tmp_path: Path):
    """Verify resolve_db_path follows JURY_STATE_DIR -> local .jury -> home directory."""
    # 1. Environment variable
    env_dir = tmp_path / "env_jury_dir"
    monkeypatch.setenv("JURY_STATE_DIR", str(env_dir))
    resolved = JuryStateEngine.resolve_db_path()
    assert resolved == (env_dir / "state.db").resolve()

    monkeypatch.delenv("JURY_STATE_DIR", raising=False)

    # 2. Local .jury directory in cwd
    local_jury = tmp_path / ".jury"
    local_jury.mkdir(parents=True, exist_ok=True)
    resolved_local = JuryStateEngine.resolve_db_path(cwd=tmp_path)
    assert resolved_local == (local_jury / "state.db").resolve()


def test_concurrent_multithread_transactions(temp_engine: JuryStateEngine):
    """Verify concurrent worker threads safely perform transactions under two-tier lock."""
    errors = []
    iterations = 20
    threads = []

    def _worker(thread_id: int):
        try:
            for i in range(iterations):
                key = f"key_{thread_id}_{i}"
                record = DeliberationRecord(
                    verdict="APPROVED",
                    deliberation_markdown=f"Thread {thread_id} Iteration {i}",
                )
                temp_engine.cache_put(key, record, ttl_seconds=60)
                got = temp_engine.cache_get(key)
                if not got or got.deliberation_markdown != record.deliberation_markdown:
                    errors.append(f"Mismatch on {key}")
        except Exception as e:
            errors.append(str(e))

    for tid in range(4):
        t = threading.Thread(target=_worker, args=(tid,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=15.0)

    assert errors == [], f"Concurrent thread errors encountered: {errors}"
