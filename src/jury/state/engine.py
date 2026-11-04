"""
src/jury/state/engine.py - JuryStateEngine: Embedded SQLite-WAL & Concurrency Coordinator.

Implements Interface Contract #2 for Jury Mesh v3.0:
- Two-tier locking (filelock on db_path + ".lock" + SQLite BEGIN IMMEDIATE)
- SQLite WAL mode with optimized PRAGMAs (synchronous=NORMAL, busy_timeout=30000, 64MB cache)
- Continuous rate limiting with Two-Phase settlement and circuit breakers
- Decentralized singleflight coalescing with 15-second heartbeat lease takeover
- 5-vector composite cache key integration
- Permanent deliberation audit logging
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Union

from filelock import FileLock

from jury.state.cache_key import CompositeCacheKeyGenerator
from jury.state.rate_limiter import RateLimitAcquisition, TwoPhaseRateLimiter
from jury.state.singleflight import SingleflightCoalescer, SingleflightTicket


@dataclass
class DeliberationRecord:
    """Canonical cached outcome of a completed multi-model deliberation."""
    verdict: str  # 'APPROVED', 'APPROVED_WITH_CONDITIONS', 'REJECTED'
    deliberation_markdown: str
    patch_unified_diff: Optional[str] = None
    sarif_json: Optional[Union[Dict[str, Any], list]] = None
    verification_contract_json: Optional[Dict[str, Any]] = None
    juror_payloads_json: Optional[Dict[str, Any]] = None
    cache_key: str = ""
    intent: str = ""
    git_head_sha: str = ""
    dirty_unstaged_hash: str = ""
    dirty_staged_hash: str = ""
    untracked_files_hash: str = ""
    config_hash: str = ""
    prompt_version: str = "3.0.0"
    ttl_seconds: int = 300
    tokens_saved: int = 0
    computation_duration_ms: int = 0
    hit_count: int = 0
    created_at: float = 0.0
    expires_at: float = 0.0
    last_accessed_at: float = 0.0
    from_cache: bool = False


@dataclass
class AuditLogEntry:
    """Audit ledger entry recording full deliberation metadata, latency, and tokens."""
    ticket_id: str
    command: str
    prompt: str
    verdict: str
    latency_ms: int
    tokens_used: int
    timestamp: float = 0.0
    diff_summary: Optional[str] = ""
    juror_scores: Dict[str, Any] = field(default_factory=dict)
    tokens_saved: int = 0
    caller_process_id: Optional[int] = None
    caller_tool: Optional[str] = "cli"
    cache_hit: bool = False
    error_code: Optional[str] = None
    error_details: Optional[str] = None


DEFAULT_BUCKET_DEFINITIONS = [
    # bucket_id, provider_id, model_id, rpm, tpm, concurrency
    ("groq:llama-3.3-70b-versatile", "groq", "llama-3.3-70b-versatile", 30.0, 12000.0, 1),
    ("constellation:glm-5-3-flash-free", "constellation", "glm-5-3-flash-free", 30.0, 120000.0, 2),
    ("constellation:qwen3-8-flash-free", "constellation", "qwen3-8-flash-free", 30.0, 120000.0, 2),
    ("cerebras:llama-3.3-70b", "cerebras", "llama-3.3-70b", 30.0, 60000.0, 2),
    ("openrouter:deepseek/deepseek-v3", "openrouter", "deepseek/deepseek-v3", 60.0, 300000.0, 4),
    ("local:ollama", "local", "ollama", 1000.0, 1000000.0, 2),
]


class JuryStateEngine:
    """
    Embedded SQLite-WAL State Engine & Concurrency Governor.
    Provides cross-process rate limiting, singleflight coalescing, cache storage,
    and audit logging with zero resident daemon overhead.
    """

    PRAGMA_SEQUENCE = [
        "PRAGMA journal_mode = WAL;",
        "PRAGMA synchronous = NORMAL;",
        "PRAGMA busy_timeout = 30000;",
        "PRAGMA temp_store = MEMORY;",
        "PRAGMA cache_size = -64000;",      # 64 MB RAM cache
        "PRAGMA mmap_size = 268435456;",    # 256 MB memory-mapped I/O
        "PRAGMA wal_autocheckpoint = 1000;",# ~4 MB checkpoint
        "PRAGMA foreign_keys = ON;",
    ]

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        lock_path: Optional[Union[str, Path]] = None,
        timeout: float = 30.0,
    ):
        self.db_path = Path(db_path).resolve() if db_path else self.resolve_db_path()
        self.lock_path = Path(lock_path).resolve() if lock_path else Path(str(self.db_path) + ".lock")
        self.timeout = timeout

        # Ensure parent directories exist
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        # OS File Lock (Tier 1 Concurrency Control)
        self.lock = FileLock(str(self.lock_path), timeout=self.timeout)

        # Subsystems
        self.rate_limiter = TwoPhaseRateLimiter(self.get_connection, self._lock_context)
        self.singleflight = SingleflightCoalescer(self.get_connection, self._lock_context)

        # Initialize schema and pre-seed buckets
        self._init_database()

    @staticmethod
    def resolve_db_path(cwd: Optional[Union[str, Path]] = None) -> Path:
        """
        Resolve database path via 3-tier hierarchy:
        1. JURY_STATE_DIR environment variable
        2. Repo-local .jury directory (if present in cwd or git root)
        3. User home default (~/.jury/state.db)
        """
        # 1. Environment variable override
        env_dir = os.environ.get("JURY_STATE_DIR")
        if env_dir:
            return Path(env_dir).expanduser().resolve() / "state.db"

        # 2. Repo-local .jury directory
        try:
            curr = Path(cwd).resolve() if cwd else Path.cwd().resolve()
            if (curr / ".jury").is_dir():
                return curr / ".jury" / "state.db"
            for parent in curr.parents:
                if (parent / ".jury").is_dir():
                    return parent / ".jury" / "state.db"
        except Exception:
            pass

        # 3. User home default (~/.jury/state.db)
        return (Path.home() / ".jury" / "state.db").resolve()

    def _lock_context(self) -> FileLock:
        return self.lock

    def get_connection(self) -> sqlite3.Connection:
        """
        Create a new SQLite connection with full WAL PRAGMA sequence.
        """
        conn = sqlite3.connect(str(self.db_path), timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        for pragma in self.PRAGMA_SEQUENCE:
            conn.execute(pragma)
        return conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Two-tier transactional context manager:
        1. Acquires OS FileLock (~/.jury/state.db.lock)
        2. Executes BEGIN IMMEDIATE on SQLite WAL connection
        3. Commits transaction and releases locks on exit
        """
        with self.lock:
            conn = self.get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                yield conn
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn.close()

    def _init_database(self) -> None:
        """
        Load and apply schema.sql, then pre-seed default provider rate limit buckets.
        """
        schema_path = Path(__file__).parent / "schema.sql"
        if not schema_path.is_file():
            raise FileNotFoundError(f"Jury schema definition not found at {schema_path}")

        schema_ddl = schema_path.read_text(encoding="utf-8")

        with self.lock:
            conn = self.get_connection()
            try:
                conn.executescript(schema_ddl)
                now = time.time()
                for bucket_id, provider_id, model_id, rpm, tpm, concurrency in DEFAULT_BUCKET_DEFINITIONS:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO rate_limit_buckets
                        (bucket_id, provider_id, model_id, rpm_limit, rpm_current, rpm_last_refill,
                         tpm_limit, tpm_current, tpm_last_refill, concurrency_limit, in_flight_count,
                         circuit_state, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'CLOSED', ?)
                        """,
                        (bucket_id, provider_id, model_id, rpm, rpm, now, tpm, tpm, now, concurrency, now),
                    )
                conn.commit()
            finally:
                conn.close()

    # =========================================================================
    # Interface Contract #2: Rate Limiting & Two-Phase Settlement
    # =========================================================================
    def acquire_rate_limit(
        self,
        provider: str,
        estimated_tokens: int,
        max_wait_seconds: float = 25.0,
    ) -> RateLimitAcquisition:
        """
        Pre-flight reservation: checks continuous leaky bucket quota (RPM and TPM)
        and concurrency limits with exponential backoff & jitter loop.
        """
        return self.rate_limiter.reserve(
            provider_or_bucket=provider,
            estimated_tokens=estimated_tokens,
            max_wait_seconds=max_wait_seconds,
        )

    def refund_tokens(
        self,
        provider: str,
        actual_tokens: int,
        estimated_tokens: Optional[int] = None,
        success: bool = True,
        retry_after: Optional[float] = None,
        error_msg: Optional[str] = None,
    ) -> None:
        """
        Post-flight settlement: reconciles actual token usage and updates circuit breaker.
        """
        est = estimated_tokens if estimated_tokens is not None else actual_tokens
        self.rate_limiter.settle(
            provider_or_bucket=provider,
            estimated_tokens=est,
            actual_tokens=actual_tokens,
            success=success,
            retry_after=retry_after,
            error_msg=error_msg,
        )

    # =========================================================================
    # Interface Contract #2: Singleflight In-Flight Coalescing
    # =========================================================================
    def singleflight_enter(
        self,
        cache_key: str,
        timeout_seconds: float = 60.0,
        command: str = "review",
        intent: str = "",
        diff_summary: str = "",
    ) -> SingleflightTicket:
        """
        Enter decentralized singleflight state machine for a deliberation key.
        Elects a Leader or joins as a Follower.
        """
        return self.singleflight.coalesce_or_lead(
            cache_key=cache_key,
            command=command,
            intent=intent,
            diff_summary=diff_summary,
            lease_seconds=timeout_seconds,
        )

    # =========================================================================
    # Interface Contract #2: Composite Cache Operations
    # =========================================================================
    def cache_get(self, cache_key: str) -> Optional[DeliberationRecord]:
        """
        Retrieve cached deliberation verdict if present and unexpired.
        Sub-millisecond query leveraging memory-mapped I/O and page cache.
        """
        conn = self.get_connection()
        try:
            now = time.time()
            row = conn.execute(
                "SELECT * FROM jury_cache WHERE cache_key = ? AND expires_at > ?",
                (cache_key, now),
            ).fetchone()

            if not row:
                return None

            # Update access statistics atomically
            conn.execute(
                "UPDATE jury_cache SET hit_count = hit_count + 1, last_accessed_at = ? WHERE cache_key = ?",
                (now, cache_key),
            )
            conn.commit()

            return DeliberationRecord(
                verdict=row["verdict"],
                deliberation_markdown=row["deliberation_markdown"],
                patch_unified_diff=row["patch_unified_diff"],
                sarif_json=json.loads(row["sarif_json"]) if row["sarif_json"] else None,
                verification_contract_json=json.loads(row["verification_contract_json"]) if row["verification_contract_json"] else None,
                juror_payloads_json=json.loads(row["juror_payloads_json"]) if row["juror_payloads_json"] else None,
                cache_key=row["cache_key"],
                intent=row["intent"],
                git_head_sha=row["git_head_sha"],
                dirty_unstaged_hash=row["dirty_unstaged_hash"],
                dirty_staged_hash=row["dirty_staged_hash"],
                untracked_files_hash=row["untracked_files_hash"],
                config_hash=row["config_hash"],
                prompt_version=row["prompt_version"],
                ttl_seconds=row["ttl_seconds"],
                tokens_saved=row["tokens_saved"],
                computation_duration_ms=row["computation_duration_ms"],
                hit_count=row["hit_count"] + 1,
                created_at=row["created_at"],
                expires_at=row["expires_at"],
                last_accessed_at=now,
                from_cache=True,
            )
        finally:
            conn.close()

    def cache_put(
        self,
        cache_key: str,
        record: DeliberationRecord,
        ttl_seconds: int = 300,
    ) -> None:
        """
        Store a completed deliberation verdict into jury_cache.
        """
        now = time.time()
        expires_at = now + ttl_seconds

        with self.lock:
            conn = self.get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO jury_cache
                    (cache_key, created_at, expires_at, ttl_seconds, intent, git_head_sha,
                     dirty_unstaged_hash, dirty_staged_hash, untracked_files_hash, config_hash,
                     prompt_version, verdict, patch_unified_diff, sarif_json,
                     verification_contract_json, deliberation_markdown, juror_payloads_json,
                     tokens_saved, computation_duration_ms, hit_count, last_accessed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cache_key,
                        now,
                        expires_at,
                        ttl_seconds,
                        record.intent,
                        record.git_head_sha,
                        record.dirty_unstaged_hash,
                        record.dirty_staged_hash,
                        record.untracked_files_hash,
                        record.config_hash,
                        record.prompt_version,
                        record.verdict,
                        record.patch_unified_diff,
                        json.dumps(record.sarif_json) if record.sarif_json is not None else None,
                        json.dumps(record.verification_contract_json) if record.verification_contract_json is not None else None,
                        record.deliberation_markdown,
                        json.dumps(record.juror_payloads_json) if record.juror_payloads_json is not None else None,
                        record.tokens_saved,
                        record.computation_duration_ms,
                        record.hit_count,
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    # =========================================================================
    # Interface Contract #2: Deliberation Audit Ledger
    # =========================================================================
    def log_audit(self, entry: AuditLogEntry) -> None:
        """
        Record a permanent deliberation audit log entry.
        """
        with self.lock:
            conn = self.get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                now = entry.timestamp if entry.timestamp > 0 else time.time()
                pid = entry.caller_process_id if entry.caller_process_id is not None else os.getpid()

                conn.execute(
                    """
                    INSERT OR REPLACE INTO jury_audit_log
                    (ticket_id, timestamp, command, prompt, diff_summary, verdict,
                     juror_scores, latency_ms, tokens_used, tokens_saved, caller_process_id,
                     caller_tool, cache_hit, error_code, error_details)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.ticket_id,
                        now,
                        entry.command,
                        entry.prompt,
                        entry.diff_summary or "",
                        entry.verdict,
                        json.dumps(entry.juror_scores or {}),
                        entry.latency_ms,
                        entry.tokens_used,
                        entry.tokens_saved,
                        pid,
                        entry.caller_tool or "cli",
                        1 if entry.cache_hit else 0,
                        entry.error_code,
                        entry.error_details,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def get_audit_logs(
        self,
        limit: int = 50,
        command: Optional[str] = None,
    ) -> List[AuditLogEntry]:
        """
        Retrieve historical audit logs ordered newest first.
        """
        conn = self.get_connection()
        try:
            if command:
                rows = conn.execute(
                    "SELECT * FROM jury_audit_log WHERE command = ? ORDER BY timestamp DESC LIMIT ?",
                    (command, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jury_audit_log ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()

            entries: List[AuditLogEntry] = []
            for r in rows:
                entries.append(
                    AuditLogEntry(
                        ticket_id=r["ticket_id"],
                        timestamp=r["timestamp"],
                        command=r["command"],
                        prompt=r["prompt"],
                        diff_summary=r["diff_summary"],
                        verdict=r["verdict"],
                        juror_scores=json.loads(r["juror_scores"]) if r["juror_scores"] else {},
                        latency_ms=r["latency_ms"],
                        tokens_used=r["tokens_used"],
                        tokens_saved=r["tokens_saved"],
                        caller_process_id=r["caller_process_id"],
                        caller_tool=r["caller_tool"],
                        cache_hit=bool(r["cache_hit"]),
                        error_code=r["error_code"],
                        error_details=r["error_details"],
                    )
                )
            return entries
        finally:
            conn.close()

    # =========================================================================
    # High-Level Singleflight Execution Orchestrator
    # =========================================================================
    def execute_with_singleflight(
        self,
        cache_key: str,
        intent: str,
        command: str,
        deliberate_fn: Callable[[], DeliberationRecord],
        diff_summary: str = "",
        ttl_seconds: int = 300,
        timeout_seconds: float = 60.0,
    ) -> DeliberationRecord:
        """
        High-level orchestration:
        1. Checks cache for instant return.
        2. If miss, enters singleflight.
        3. If elected LEADER:
           - Spawns periodic heartbeat thread (every 3s).
           - Executes deliberate_fn() outside of database locks.
           - Writes result to jury_cache.
           - Marks singleflight ticket COMPLETED.
           - Logs audit ledger entry.
           - Returns DeliberationRecord.
        4. If FOLLOWER:
           - Awaits leader completion via await_ticket().
           - Returns DeliberationRecord from cache.
        """
        # 1. Fast cache check
        cached = self.cache_get(cache_key)
        if cached:
            return cached

        # 2. Singleflight entry
        ticket = self.singleflight_enter(
            cache_key=cache_key,
            timeout_seconds=timeout_seconds,
            command=command,
            intent=intent,
            diff_summary=diff_summary,
        )

        if ticket.cached:
            cached = self.cache_get(cache_key)
            if cached:
                return cached

        if ticket.is_leader:
            # LEADER EXECUTION PATH
            stop_heartbeat = self.singleflight.start_heartbeat_loop(ticket.ticket_id, interval=3.0)
            t_start = time.time()
            try:
                record = deliberate_fn()
                duration_ms = int((time.time() - t_start) * 1000)

                # Enrich record metadata
                record.cache_key = cache_key
                record.intent = intent
                record.computation_duration_ms = duration_ms
                record.ttl_seconds = ttl_seconds

                # Cache put and ticket completion
                self.cache_put(cache_key, record, ttl_seconds=ttl_seconds)
                self.singleflight.mark_completed(ticket.ticket_id)

                # Audit log
                self.log_audit(
                    AuditLogEntry(
                        ticket_id=ticket.ticket_id,
                        command=command,
                        prompt=intent,
                        diff_summary=diff_summary,
                        verdict=record.verdict,
                        latency_ms=duration_ms,
                        tokens_used=getattr(record, "tokens_used", 0) or 0,
                        tokens_saved=record.tokens_saved,
                        cache_hit=False,
                    )
                )
                return record
            except Exception as e:
                self.singleflight.mark_failed(ticket.ticket_id, str(e))
                self.log_audit(
                    AuditLogEntry(
                        ticket_id=ticket.ticket_id,
                        command=command,
                        prompt=intent,
                        diff_summary=diff_summary,
                        verdict="ERROR",
                        latency_ms=int((time.time() - t_start) * 1000),
                        tokens_used=0,
                        error_code="DELIBERATION_EXCEPTION",
                        error_details=str(e),
                    )
                )
                raise
            finally:
                stop_heartbeat()

        else:
            # FOLLOWER PATH
            status, takeover_ticket = self.singleflight.await_ticket(
                ticket_id=ticket.ticket_id,
                cache_key=cache_key,
                timeout_seconds=timeout_seconds,
                allow_takeover=True,
            )

            if status == "TAKEOVER" and takeover_ticket and takeover_ticket.is_leader:
                # Follower took over as leader! Recursively execute
                return self.execute_with_singleflight(
                    cache_key=cache_key,
                    intent=intent,
                    command=command,
                    deliberate_fn=deliberate_fn,
                    diff_summary=diff_summary,
                    ttl_seconds=ttl_seconds,
                    timeout_seconds=timeout_seconds,
                )

            # Record is now ready in cache
            cached_result = self.cache_get(cache_key)
            if cached_result:
                return cached_result

            raise TimeoutError(f"Deliberation completed for {cache_key} but cache record was missing.")

    def clear_cache(self, expired_only: bool = False) -> int:
        """
        Delete cache records. Returns number of rows deleted.
        """
        with self.lock:
            conn = self.get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                if expired_only:
                    cur = conn.execute("DELETE FROM jury_cache WHERE expires_at <= ?", (time.time(),))
                else:
                    cur = conn.execute("DELETE FROM jury_cache")
                deleted = cur.rowcount
                conn.commit()
                return deleted
            finally:
                conn.close()
