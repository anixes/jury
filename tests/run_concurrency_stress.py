"""
High-throughput Multi-Process Concurrency Stress Test for JuryStateEngine.
Spawns 4 independent OS child processes via subprocess to rigorously stress
SQLite WAL mode and cross-process file locks.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from jury.state.engine import JuryStateEngine


def run_worker_cli(db_path: str, worker_id: int, iterations: int):
    """Run in a child process."""
    engine = JuryStateEngine(db_path=db_path)
    for i in range(iterations):
        # 1. Mutate cache
        key = f"proc_key_{worker_id}_{i}"
        from jury.state.engine import DeliberationRecord
        rec = DeliberationRecord(verdict="APPROVED", deliberation_markdown=f"w{worker_id}_i{i}")
        engine.cache_put(key, rec, ttl_seconds=120)
        cached = engine.cache_get(key)
        assert cached is not None and cached.deliberation_markdown == f"w{worker_id}_i{i}"

        # 2. Mutate rate limits
        acq = engine.rate_limiter.check_admissibility("local:ollama", estimated_tokens=100)
        if acq.granted:
            engine.refund_tokens("local:ollama", actual_tokens=80, estimated_tokens=100, success=True)

        # 3. Mutate tickets
        tkt = engine.singleflight_enter(cache_key=f"sf_key_{i}", timeout_seconds=10.0)
        if tkt.is_leader:
            engine.singleflight.heartbeat(tkt.ticket_id)
            engine.singleflight.mark_completed(tkt.ticket_id)

        # 4. Mutate audit log
        from jury.state.engine import AuditLogEntry
        engine.log_audit(
            AuditLogEntry(
                ticket_id=f"audit_{worker_id}_{i}",
                command="test",
                prompt=f"p_{worker_id}_{i}",
                verdict="APPROVED",
                latency_ms=10,
                tokens_used=100,
            )
        )


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--worker":
        db_path = sys.argv[2]
        worker_id = int(sys.argv[3])
        iterations = int(sys.argv[4])
        run_worker_cli(db_path, worker_id, iterations)
        sys.exit(0)

    # Master orchestrator
    print("\n--- [Probe 1] Multi-Process Concurrency Stress Test ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "stress.db")
        # Pre-initialize database
        master_engine = JuryStateEngine(db_path=db_path)

        num_processes = 4
        iterations = 30
        print(f"Spawning {num_processes} child processes executing {iterations} mixed transactions each...")

        procs = []
        start_time = time.time()
        python_exe = sys.executable

        for wid in range(num_processes):
            cmd = [
                python_exe,
                str(Path(__file__).resolve()),
                "--worker",
                db_path,
                str(wid),
                str(iterations),
            ]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            procs.append((wid, p))

        # Wait for all processes to complete
        errors = []
        for wid, p in procs:
            stdout, stderr = p.communicate(timeout=25.0)
            if p.returncode != 0:
                errors.append(f"Worker {wid} failed (code {p.returncode}):\nStdout: {stdout}\nStderr: {stderr}")

        duration = time.time() - start_time
        print(f"All {num_processes} child processes finished in {duration:.2f}s.")

        if errors:
            print(f"FAILED with {len(errors)} errors:")
            for e in errors:
                print(e)
            sys.exit(1)

        # Verify SQLite integrity and transaction consistency
        conn = master_engine.get_connection()
        try:
            integrity = conn.execute("PRAGMA integrity_check;").fetchall()
            assert len(integrity) == 1 and integrity[0][0] == "ok", f"Integrity error: {integrity}"
            print(f"PRAGMA integrity_check: {integrity[0][0]} (100% healthy)")

            audit_count = conn.execute("SELECT COUNT(*) FROM jury_audit_log;").fetchone()[0]
            expected_audits = num_processes * iterations
            print(f"Audit log row count: {audit_count} (expected: {expected_audits})")
            assert audit_count == expected_audits, f"Audit count mismatch: {audit_count} != {expected_audits}"

            cache_count = conn.execute("SELECT COUNT(*) FROM jury_cache;").fetchone()[0]
            expected_cache = num_processes * iterations
            print(f"Cache row count: {cache_count} (expected: {expected_cache})")
            assert cache_count == expected_cache, f"Cache count mismatch: {cache_count} != {expected_cache}"
        finally:
            conn.close()

        print("[Probe 1] PASS: Multi-process concurrency stress test succeeded with ZERO database locks or corruption.")


if __name__ == "__main__":
    main()
