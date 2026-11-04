"""
Adversarial Multi-Process Concurrency Probe for JuryStateEngine.
Tests cross-process SQLite WAL + filelock under heavy concurrent mutation.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import random
import sys
import tempfile
import time
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from jury.state.engine import AuditLogEntry, DeliberationRecord, JuryStateEngine


def worker_process(db_path: str, worker_id: int, iterations: int, error_queue: mp.Queue):
    """Worker process simultaneously mutating tickets, rate limits, cache, and audit logs."""
    try:
        engine = JuryStateEngine(db_path=db_path)
        for i in range(iterations):
            # 1. Cache operations
            cache_key = f"probe_key_{worker_id}_{i % 10}"
            record = DeliberationRecord(
                verdict="APPROVED",
                deliberation_markdown=f"Worker {worker_id} iteration {i}",
                tokens_saved=100 * (i + 1),
            )
            engine.cache_put(cache_key, record, ttl_seconds=60)
            cached = engine.cache_get(cache_key)
            if not cached or "Worker" not in cached.deliberation_markdown:
                error_queue.put(f"[Worker {worker_id}] Cache read mismatch on {cache_key}")

            # 2. Rate limiter reserve & settle
            acq = engine.acquire_rate_limit(
                provider="constellation",
                estimated_tokens=500,
                max_wait_seconds=10.0,
            )
            if acq.granted:
                time.sleep(random.uniform(0.005, 0.02))
                engine.refund_tokens(
                    provider="constellation",
                    actual_tokens=300,
                    estimated_tokens=500,
                    success=True,
                )

            # 3. Singleflight ticket state machine
            sf_key = f"sf_probe_key_{i % 5}"
            ticket = engine.singleflight_enter(cache_key=sf_key, timeout_seconds=10.0)
            if ticket.is_leader:
                time.sleep(random.uniform(0.005, 0.015))
                engine.singleflight.heartbeat(ticket.ticket_id)
                engine.singleflight.mark_completed(ticket.ticket_id)

            # 4. Audit logging
            engine.log_audit(
                AuditLogEntry(
                    ticket_id=f"tkt_probe_{worker_id}_{i}",
                    command="review",
                    prompt=f"Probe audit worker {worker_id}",
                    verdict="APPROVED",
                    latency_ms=25,
                    tokens_used=300,
                    caller_process_id=os.getpid(),
                )
            )

    except Exception as e:
        error_queue.put(f"[Worker {worker_id}] Exception: {type(e).__name__}: {e}")


def run_probe():
    mp.set_start_method("spawn", force=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "probe_state.db")
        # Initialize engine
        init_engine = JuryStateEngine(db_path=db_path)
        init_engine.clear_cache()

        num_processes = 6
        iterations_per_worker = 25
        error_queue = mp.Queue()
        processes = []

        print(f"Starting {num_processes} parallel processes with {iterations_per_worker} iterations each...")
        start_time = time.time()

        for wid in range(num_processes):
            p = mp.Process(
                target=worker_process,
                args=(db_path, wid, iterations_per_worker, error_queue),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=30.0)
            if p.is_alive():
                p.terminate()
                error_queue.put(f"Process {p.pid} timed out after 30s")

        duration = time.time() - start_time
        print(f"All processes finished in {duration:.2f}s.")

        # Check errors
        errors = []
        while not error_queue.empty():
            errors.append(error_queue.get())

        # Verify SQLite integrity
        conn = init_engine.get_connection()
        try:
            integrity = conn.execute("PRAGMA integrity_check;").fetchall()
            print(f"PRAGMA integrity_check: {integrity}")
            assert len(integrity) == 1 and integrity[0][0] == "ok", f"Integrity check failed: {integrity}"

            # Check audit log count
            audit_count = conn.execute("SELECT COUNT(*) FROM jury_audit_log;").fetchone()[0]
            print(f"Total audit logs recorded: {audit_count}")
            assert audit_count > 0, "No audit logs recorded!"

            # Check rate limiter state
            buckets = conn.execute("SELECT bucket_id, in_flight_count, tpm_current, circuit_state FROM rate_limit_buckets;").fetchall()
            print("Rate limit buckets:")
            for b in buckets:
                print(f"  {b['bucket_id']}: in_flight={b['in_flight_count']}, tpm={b['tpm_current']}, circuit={b['circuit_state']}")
        finally:
            conn.close()

        if errors:
            print(f"FAIL: {len(errors)} errors encountered:")
            for err in errors:
                print(f"  - {err}")
            sys.exit(1)
        else:
            print("PASS: Concurrency probe passed with ZERO errors and full database integrity.")


if __name__ == "__main__":
    run_probe()
