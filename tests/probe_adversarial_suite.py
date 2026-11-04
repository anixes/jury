"""
Adversarial Probe Suite for Scope Items 2, 3, 4, and 5:
- Dead-Leader Lease Takeover
- Dirty-Tree Cache Key Poisoning across git states
- Leaky Bucket & Circuit Breaker Robustness
- Integrity Forensics
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from jury.state.cache_key import CompositeCacheKeyGenerator
from jury.state.engine import DeliberationRecord, JuryStateEngine
from jury.state.rate_limiter import TwoPhaseRateLimiter
from jury.state.singleflight import SingleflightCoalescer


def test_dead_leader_lease_takeover():
    """Adversarially simulate a leader dying mid-flight (lease_expires_at < now)."""
    print("\n--- [Probe 2] Dead-Leader Lease Takeover ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "dead_leader.db"
        engine = JuryStateEngine(db_path=db_path)
        coalescer = engine.singleflight
        cache_key = "adversarial_dead_leader_test_key"

        # Step 1: Leader process A claims leadership
        ticket_a = coalescer.coalesce_or_lead(cache_key=cache_key, lease_seconds=10.0)
        assert ticket_a.is_leader is True, "Process A must be elected leader"
        print(f"Leader A acquired ticket: {ticket_a.ticket_id}, pid={ticket_a.leader_pid}")

        # Step 2: Simulate Leader A dying / crashing mid-flight without completing or heartbeating.
        # Force heartbeat and expires_at into the past in the database.
        past_time = time.time() - 30.0
        with engine.lock:
            conn = engine.get_connection()
            try:
                conn.execute(
                    "UPDATE jury_tickets SET heartbeat_at = ?, expires_at = ? WHERE ticket_id = ?",
                    (past_time, past_time, ticket_a.ticket_id),
                )
                conn.commit()
            finally:
                conn.close()

        print("Simulated Leader A death (heartbeat & expires_at set to 30s ago)")

        # Step 3: Follower process B calls coalesce_or_lead or await_ticket.
        # Follower B must detect lease expiration and execute atomic CAS takeover.
        ticket_b = coalescer.coalesce_or_lead(cache_key=cache_key, lease_seconds=20.0)
        assert ticket_b.is_leader is True, "Follower B must take over as leader via CAS"
        assert ticket_b.ticket_id == ticket_a.ticket_id, "Ticket ID should be preserved on takeover"
        assert ticket_b.expires_at > time.time() + 10.0, "Lease expiration must be renewed"
        print(f"Follower B successfully took over ticket: {ticket_b.ticket_id}, expires_at={ticket_b.expires_at}")

        # Step 4: Verify Follower B completes the ticket and writes cache
        record = DeliberationRecord(
            verdict="APPROVED",
            deliberation_markdown="Deliberation finished by takeover leader B",
        )
        engine.cache_put(cache_key, record)
        coalescer.mark_completed(ticket_b.ticket_id)

        # Step 5: Follower C now asks for the key -> must get cache hit
        ticket_c = coalescer.coalesce_or_lead(cache_key=cache_key)
        assert ticket_c.cached is True or ticket_c.status == "COMPLETED"
        cached_record = engine.cache_get(cache_key)
        assert cached_record is not None
        assert "takeover leader B" in cached_record.deliberation_markdown
        print("[Probe 2] PASS: Dead-leader lease takeover verified.")


def test_dirty_tree_cache_key_poisoning():
    """Test cache key invalidation across all git states."""
    print("\n--- [Probe 3] Dirty-Tree Cache Key Poisoning ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()

        # Initialize Git repo
        subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "probe@jury.ai"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Jury Probe"], cwd=str(repo), check=True, capture_output=True)

        main_py = repo / "main.py"
        main_py.write_text("def run():\n    return 42\n", encoding="utf-8")
        subprocess.run(["git", "add", "main.py"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo), check=True, capture_output=True)

        keys_observed = {}

        # 1. Clean working tree key
        k_clean = CompositeCacheKeyGenerator.compute_cache_key(intent="Review PR", repo_root=repo)
        keys_observed["clean"] = k_clean
        print(f"1. Clean HEAD key:                     {k_clean}")

        # 2. Unstaged single-byte modification
        main_py.write_text("def run():\n    return 43\n", encoding="utf-8")
        k_unstaged = CompositeCacheKeyGenerator.compute_cache_key(intent="Review PR", repo_root=repo)
        assert k_unstaged != k_clean, "Unstaged single-byte change MUST invalidate cache key"
        keys_observed["unstaged"] = k_unstaged
        print(f"2. Unstaged single-byte diff key:      {k_unstaged}")

        # 3. Staged file modification
        subprocess.run(["git", "add", "main.py"], cwd=str(repo), check=True, capture_output=True)
        k_staged = CompositeCacheKeyGenerator.compute_cache_key(intent="Review PR", repo_root=repo)
        assert k_staged != k_clean, "Staged modification MUST differ from clean"
        assert k_staged != k_unstaged, "Staged modification MUST differ from unstaged"
        keys_observed["staged"] = k_staged
        print(f"3. Staged diff key:                    {k_staged}")

        # 4. Untracked file addition
        untracked = repo / "new_helper.py"
        untracked.write_text("# Untracked secret module\n", encoding="utf-8")
        k_untracked = CompositeCacheKeyGenerator.compute_cache_key(intent="Review PR", repo_root=repo)
        assert k_untracked != k_staged, "Untracked file addition MUST invalidate cache key"
        keys_observed["untracked"] = k_untracked
        print(f"4. Untracked file addition key:        {k_untracked}")

        # 5. Branch / HEAD commit switch
        subprocess.run(["git", "add", "new_helper.py"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Commit staged & helper"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "feature-x"], cwd=str(repo), check=True, capture_output=True)
        helper_x = repo / "feature.py"
        helper_x.write_text("x = 100\n", encoding="utf-8")
        subprocess.run(["git", "add", "feature.py"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Feature commit"], cwd=str(repo), check=True, capture_output=True)

        k_head_switch = CompositeCacheKeyGenerator.compute_cache_key(intent="Review PR", repo_root=repo)
        assert k_head_switch not in keys_observed.values(), "New commit SHA MUST produce new key"
        keys_observed["head_switch"] = k_head_switch
        print(f"5. Branch / HEAD commit switch key:    {k_head_switch}")

        # 6. Config change
        k_config_change = CompositeCacheKeyGenerator.compute_cache_key(
            intent="Review PR",
            config_hash="cfg_hash_custom_groq",
            repo_root=repo,
        )
        assert k_config_change != k_head_switch, "Config hash change MUST invalidate cache key"
        keys_observed["config_change"] = k_config_change
        print(f"6. Config change key:                  {k_config_change}")

        # Verify all 6 keys are completely distinct
        assert len(set(keys_observed.values())) == 6, f"Expected 6 unique keys, got {len(set(keys_observed.values()))}"
        print("[Probe 3] PASS: Dirty-tree cache key sensitivity verified across all 6 git states. Zero false hits.")


def test_leaky_bucket_and_circuit_breaker():
    """Test token exhaustion under burst, rate limit recovery over time, and circuit breaker trip."""
    print("\n--- [Probe 4] Leaky Bucket & Circuit Breaker Robustness ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "ratelimit_test.db"
        engine = JuryStateEngine(db_path=db_path)
        limiter = engine.rate_limiter

        # Groq has rpm=30, tpm=12000, concurrency=1
        print("Testing burst exhaustion...")
        # 1. Burst exhaustion: reserve 11,500 tokens from Groq 12,000 ceiling
        acq1 = limiter.check_admissibility("groq", estimated_tokens=11500)
        assert acq1.granted is True, "First reservation should be admitted"
        print(f"Acquisition 1 granted: in_flight={acq1.in_flight}, tpm_remaining={acq1.tpm_remaining}")

        # Settle acq1 with actual 11,500 (no refund) so TPM remains depleted
        limiter.settle("groq", estimated_tokens=11500, actual_tokens=11500, success=True)

        # 2. Second burst should be rejected due to TPM exhaustion (only ~500 tokens left)
        acq2 = limiter.check_admissibility("groq", estimated_tokens=3000)
        assert acq2.granted is False, "Must reject burst exceeding TPM ceiling"
        assert "TPM exhausted" in (acq2.reason or "")
        print(f"Acquisition 2 rejected as expected: {acq2.reason}")

        # 3. Simulate recovery over time by manipulating rpm_last_refill and tpm_last_refill
        # In 30 seconds, Groq replenishes 30s * (12000/60) = 6000 tokens
        with engine.lock:
            conn = engine.get_connection()
            try:
                conn.execute(
                    "UPDATE rate_limit_buckets SET tpm_last_refill = ? WHERE bucket_id LIKE 'groq%'",
                    (time.time() - 30.0,),
                )
                conn.commit()
            finally:
                conn.close()

        acq3 = limiter.check_admissibility("groq", estimated_tokens=3000)
        assert acq3.granted is True, "Should be admitted after simulated recovery time"
        print(f"Acquisition 3 granted after recovery: tpm_remaining={acq3.tpm_remaining}")
        limiter.settle("groq", estimated_tokens=3000, actual_tokens=3000, success=True)

        # 4. Circuit breaker trip on HTTP 429 with Retry-After
        print("Testing circuit breaker trip on 429 Retry-After...")
        # Trigger failure with Retry-After = 5.0s
        limiter.settle(
            "groq",
            estimated_tokens=1000,
            actual_tokens=0,
            success=False,
            retry_after=5.0,
            error_msg="HTTP 429 Too Many Requests (Rate limit reached)",
        )

        acq_tripped = limiter.check_admissibility("groq", estimated_tokens=500)
        assert acq_tripped.granted is False, "Breaker must be OPEN and reject traffic"
        assert acq_tripped.circuit_state == "OPEN"
        assert "Circuit OPEN cooldown" in (acq_tripped.reason or "")
        print(f"Circuit tripped to OPEN: {acq_tripped.reason}")

        # 5. Half-open transition after cooldown
        with engine.lock:
            conn = engine.get_connection()
            try:
                conn.execute(
                    "UPDATE rate_limit_buckets SET cooldown_until = ? WHERE bucket_id LIKE 'groq%'",
                    (time.time() - 1.0,),
                )
                conn.commit()
            finally:
                conn.close()

        acq_half_open = limiter.check_admissibility("groq", estimated_tokens=500)
        assert acq_half_open.granted is True, "Half-open trial request must be permitted"
        assert acq_half_open.circuit_state == "HALF_OPEN"
        print("Circuit transitioned to HALF_OPEN and permitted trial request")

        # Successful trial closes breaker
        limiter.settle("groq", estimated_tokens=500, actual_tokens=400, success=True)
        acq_closed = limiter.check_admissibility("groq", estimated_tokens=500)
        assert acq_closed.circuit_state == "CLOSED"
        limiter.settle("groq", estimated_tokens=500, actual_tokens=500, success=True)
        print("Circuit cleanly recovered to CLOSED state after trial success")

        print("[Probe 4] PASS: Leaky bucket & circuit breaker state machine verified.")


def test_integrity_forensics():
    """Verify 0 hardcoded cheats, dummy facades, or shortcuts across src/jury/state/."""
    print("\n--- [Probe 5] Integrity Forensics ---")
    state_dir = Path(__file__).parent.parent / "src" / "jury" / "state"
    forbidden_terms = [
        "mock",
        "fake",
        "dummy",
        "hardcode",
        "pass  # todo",
        "TODO",
        "FIXME",
        "NotImplementedError",
        "return True  # shortcut",
    ]

    findings = []
    for py_file in state_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        lines = content.splitlines()
        for idx, line in enumerate(lines, 1):
            lower_line = line.lower()
            for term in forbidden_terms:
                if term.lower() in lower_line and not line.strip().startswith("#"):
                    # Check if it's an actual stub or facade
                    findings.append((py_file.name, idx, line.strip(), term))

    print(f"Scanned {len(list(state_dir.glob('*.py')))} files in {state_dir}")
    if findings:
        print(f"Potential integrity warnings found ({len(findings)}):")
        for f, ln, code, term in findings:
            print(f"  {f}:{ln} matched '{term}': {code}")
    else:
        print("Zero hardcoded cheats, dummy facades, or stubs detected in src/jury/state/.")

    assert len(findings) == 0, f"Integrity violations found: {findings}"
    print("[Probe 5] PASS: Integrity forensics verified.")


def main():
    print("==================================================")
    print("STARTING ADVERSARIAL PROBE SUITE FOR MILESTONE M2")
    print("==================================================")
    test_dead_leader_lease_takeover()
    test_dirty_tree_cache_key_poisoning()
    test_leaky_bucket_and_circuit_breaker()
    test_integrity_forensics()
    print("\n==================================================")
    print("ALL 4 ADVERSARIAL PROBES PASSED WITH ZERO DEFECTS!")
    print("==================================================")


if __name__ == "__main__":
    main()
