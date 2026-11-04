"""
src/jury/state/singleflight.py - Decentralized In-Flight Request Coalescing.

Prevents multi-agent swarm dogpiles (cache stampedes) across parallel processes.
Coordinates leader election, follower poll/backoff loops, 15-second heartbeat
leases, and automated dead-leader takeover when a leader process terminates abruptly.
"""

from __future__ import annotations

import os
import random
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Tuple


@dataclass
class SingleflightTicket:
    """Represents the leadership and lifecycle state of an in-flight deliberation."""
    ticket_id: str
    cache_key: str
    is_leader: bool
    status: str = "IN_FLIGHT"  # IN_FLIGHT, COMPLETED, FAILED, TIMED_OUT
    leader_pid: int = 0
    leader_hostname: str = ""
    created_at: float = 0.0
    heartbeat_at: float = 0.0
    expires_at: float = 0.0
    error_message: Optional[str] = None
    cached: bool = False


class SingleflightCoalescer:
    """
    Decentralized Singleflight engine backed by SQLite WAL and cross-process file locks.
    Elects exactly 1 leader per unique cache key while coalescing all other concurrent
    processes as followers.
    """

    HEARTBEAT_TIMEOUT_SECONDS = 15.0
    DEFAULT_LEASE_SECONDS = 60.0

    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        lock_context: Callable[[], any],
    ):
        self._get_connection = conn_factory
        self._lock = lock_context
        self._hostname = socket.gethostname()
        self._pid = os.getpid()

    def coalesce_or_lead(
        self,
        cache_key: str,
        command: str = "review",
        intent: str = "",
        diff_summary: str = "",
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> SingleflightTicket:
        """
        Check cache and in-flight tickets.
        - If cache hit: returns cached ticket.
        - If no active ticket: elects caller as LEADER.
        - If active ticket with fresh heartbeat: joins as FOLLOWER.
        - If active ticket with expired heartbeat (>15s): takes over as LEADER.
        """
        with self._lock():
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                now = time.time()

                # 1. Fast path: Check if cache already contains unexpired result
                cache_row = conn.execute(
                    "SELECT cache_key, expires_at FROM jury_cache WHERE cache_key = ? AND expires_at > ?",
                    (cache_key, now),
                ).fetchone()

                if cache_row:
                    ticket_row = conn.execute(
                        "SELECT ticket_id FROM jury_tickets WHERE cache_key = ? ORDER BY created_at DESC LIMIT 1",
                        (cache_key,),
                    ).fetchone()
                    t_id = ticket_row["ticket_id"] if ticket_row else f"tkt_{cache_key[:12]}"
                    conn.commit()
                    return SingleflightTicket(
                        ticket_id=t_id,
                        cache_key=cache_key,
                        is_leader=False,
                        status="COMPLETED",
                        cached=True,
                    )

                # 2. Check for active in-flight ticket
                ticket_row = conn.execute(
                    "SELECT * FROM jury_tickets WHERE cache_key = ? AND status = 'IN_FLIGHT'",
                    (cache_key,),
                ).fetchone()

                if ticket_row is None:
                    # No active ticket -> Elect caller as Leader
                    ticket_id = f"tkt_{int(now)}_{uuid.uuid4().hex[:8]}"
                    expires_at = now + lease_seconds

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO jury_tickets
                        (ticket_id, cache_key, status, leader_pid, leader_hostname,
                         command, intent, diff_summary, created_at, heartbeat_at, expires_at)
                        VALUES (?, ?, 'IN_FLIGHT', ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ticket_id,
                            cache_key,
                            self._pid,
                            self._hostname,
                            command,
                            intent,
                            diff_summary,
                            now,
                            now,
                            expires_at,
                        ),
                    )
                    conn.commit()
                    return SingleflightTicket(
                        ticket_id=ticket_id,
                        cache_key=cache_key,
                        is_leader=True,
                        status="IN_FLIGHT",
                        leader_pid=self._pid,
                        leader_hostname=self._hostname,
                        created_at=now,
                        heartbeat_at=now,
                        expires_at=expires_at,
                    )

                # 3. Active ticket exists -> Check heartbeat for dead leader
                heartbeat_at = ticket_row["heartbeat_at"]
                expires_at = ticket_row["expires_at"]
                is_dead_leader = (now - heartbeat_at > self.HEARTBEAT_TIMEOUT_SECONDS) or (now >= expires_at)

                if is_dead_leader:
                    # Dead leader takeover via atomic CAS
                    new_expires = now + lease_seconds
                    dead_cutoff = now - self.HEARTBEAT_TIMEOUT_SECONDS

                    cursor = conn.execute(
                        """
                        UPDATE jury_tickets
                        SET leader_pid = ?,
                            leader_hostname = ?,
                            heartbeat_at = ?,
                            expires_at = ?,
                            command = ?,
                            intent = ?,
                            diff_summary = ?
                        WHERE ticket_id = ?
                          AND status = 'IN_FLIGHT'
                          AND (heartbeat_at < ? OR expires_at <= ?)
                        """,
                        (
                            self._pid,
                            self._hostname,
                            now,
                            new_expires,
                            command,
                            intent,
                            diff_summary,
                            ticket_row["ticket_id"],
                            dead_cutoff,
                            now,
                        ),
                    )

                    if cursor.rowcount == 1:
                        conn.commit()
                        return SingleflightTicket(
                            ticket_id=ticket_row["ticket_id"],
                            cache_key=cache_key,
                            is_leader=True,
                            status="IN_FLIGHT",
                            leader_pid=self._pid,
                            leader_hostname=self._hostname,
                            created_at=ticket_row["created_at"],
                            heartbeat_at=now,
                            expires_at=new_expires,
                        )

                # 4. Active leader is healthy -> Caller is FOLLOWER
                conn.commit()
                return SingleflightTicket(
                    ticket_id=ticket_row["ticket_id"],
                    cache_key=cache_key,
                    is_leader=False,
                    status="IN_FLIGHT",
                    leader_pid=ticket_row["leader_pid"],
                    leader_hostname=ticket_row["leader_hostname"],
                    created_at=ticket_row["created_at"],
                    heartbeat_at=ticket_row["heartbeat_at"],
                    expires_at=ticket_row["expires_at"],
                )
            finally:
                conn.close()

    def heartbeat(self, ticket_id: str, extension_seconds: float = 15.0) -> bool:
        """
        Extend ticket heartbeat and lease expiration.
        Called periodically (e.g. every 2-3s) by the active leader.
        """
        with self._lock():
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                now = time.time()
                new_expires = now + extension_seconds

                cursor = conn.execute(
                    """
                    UPDATE jury_tickets
                    SET heartbeat_at = ?,
                        expires_at = MAX(expires_at, ?)
                    WHERE ticket_id = ? AND status = 'IN_FLIGHT'
                    """,
                    (now, new_expires, ticket_id),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    def mark_completed(self, ticket_id: str) -> None:
        """Mark an in-flight ticket as successfully completed."""
        with self._lock():
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                now = time.time()
                conn.execute(
                    """
                    UPDATE jury_tickets
                    SET status = 'COMPLETED',
                        completed_at = ?
                    WHERE ticket_id = ?
                    """,
                    (now, ticket_id),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_failed(self, ticket_id: str, error_message: str) -> None:
        """Mark an in-flight ticket as failed with an error message."""
        with self._lock():
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                now = time.time()
                conn.execute(
                    """
                    UPDATE jury_tickets
                    SET status = 'FAILED',
                        completed_at = ?,
                        error_message = ?
                    WHERE ticket_id = ?
                    """,
                    (now, error_message, ticket_id),
                )
                conn.commit()
            finally:
                conn.close()

    def await_ticket(
        self,
        ticket_id: str,
        cache_key: str,
        timeout_seconds: float = 45.0,
        poll_interval: float = 0.2,
        allow_takeover: bool = True,
    ) -> Tuple[str, Optional[SingleflightTicket]]:
        """
        Poll loop for follower processes.
        Returns:
            - ("CACHE_HIT", None) if verdict is ready in cache
            - ("COMPLETED", None) if ticket marked completed
            - ("TAKEOVER", SingleflightTicket) if dead leader was detected and caller claimed leadership
        Raises:
            - RuntimeError if leader marked ticket as FAILED
            - TimeoutError if timeout_seconds elapsed without completion
        """
        start = time.time()

        while (time.time() - start) < timeout_seconds:
            # 1. Check cache fast-path
            conn = self._get_connection()
            try:
                now = time.time()
                c_row = conn.execute(
                    "SELECT cache_key FROM jury_cache WHERE cache_key = ? AND expires_at > ?",
                    (cache_key, now),
                ).fetchone()
                if c_row:
                    return "CACHE_HIT", None

                t_row = conn.execute(
                    "SELECT status, heartbeat_at, expires_at, error_message FROM jury_tickets WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()

                if t_row:
                    if t_row["status"] == "COMPLETED":
                        return "COMPLETED", None
                    if t_row["status"] == "FAILED":
                        raise RuntimeError(f"Leader deliberation failed: {t_row['error_message']}")

                    # Check for dead leader takeover
                    if allow_takeover and t_row["status"] == "IN_FLIGHT":
                        if (now - t_row["heartbeat_at"] > self.HEARTBEAT_TIMEOUT_SECONDS) or (now >= t_row["expires_at"]):
                            # Attempt dead leader takeover
                            takeover_ticket = self.coalesce_or_lead(
                                cache_key=cache_key,
                                lease_seconds=self.DEFAULT_LEASE_SECONDS,
                            )
                            if takeover_ticket.is_leader:
                                return "TAKEOVER", takeover_ticket
            finally:
                conn.close()

            sleep_time = poll_interval + random.uniform(0.02, 0.08)
            time.sleep(sleep_time)

        raise TimeoutError(
            f"Singleflight follower timed out after {timeout_seconds:.1f}s awaiting ticket {ticket_id} for key {cache_key}"
        )

    def start_heartbeat_loop(self, ticket_id: str, interval: float = 3.0) -> Callable[[], None]:
        """
        Spawn a background daemon thread that extends heartbeat every interval seconds.
        Returns a stop() function to terminate the heartbeat loop.
        """
        stop_event = threading.Event()

        def _runner():
            while not stop_event.wait(interval):
                try:
                    active = self.heartbeat(ticket_id, extension_seconds=self.HEARTBEAT_TIMEOUT_SECONDS)
                    if not active:
                        break
                except Exception:
                    pass

        t = threading.Thread(target=_runner, daemon=True, name=f"heartbeat-{ticket_id}")
        t.start()

        def stop():
            stop_event.set()
            t.join(timeout=1.0)

        return stop
