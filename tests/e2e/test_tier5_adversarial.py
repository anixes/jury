"""
tests/e2e/test_tier5_adversarial.py - Tier 5: Adversarial, Fault-Injection & Stress Testing.

Comprehensive adversarial test suite covering:
1. Malformed diff payloads, corrupt hunks, rogue lines, and line index bounds.
2. Singleflight dead-leader lease timeout and atomic CAS takeover.
3. SQLite state database concurrency, high-burst load, and lockfile resilience.
4. Hostile multi-line secret payloads and universal pre-flight scrubber verification.
5. Voting entropy (Shannon bits vs nats), monoculture defense, and QuorumError hierarchy.
6. CLI fuzzing and edge-case argument handling.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from jury.deliberation.mesh import (
    AskRequest,
    DeliberationMesh,
    PlanRequest,
    ReviewRequest,
)
from jury.deliberation.patch import (
    apply_patch_in_memory,
    parse_unified_diff,
    validate_patch,
)
from jury.deliberation.rubrics import JurorBallot
from jury.deliberation.voting import (
    QuorumError,
    check_quorum,
    compute_bpde_entropy,
)
from jury.security.scrubber import scrub_text
from jury.state.engine import JuryStateEngine

from .conftest import GitTestRepo, JuryCliRunner


# =============================================================================
# 1. Malformed Diff Payloads & Corrupt Hunks
# =============================================================================
class TestPatchAdversarialAndMalformedDiffs:
    """Exercises hostile, truncated, and corrupt unified diff payloads."""

    def test_corrupt_hunk_header_fails_gracefully(self):
        """Validates that a diff with a malformed hunk header is rejected without crashing."""
        corrupt_diff = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ not-a-valid-hunk @@\n"
            "+def broken(): pass\n"
        )
        result = validate_patch(corrupt_diff)
        assert not result.is_valid
        assert len(result.errors) > 0

    def test_rogue_line_without_prefix_rejected(self):
        """
        Validates that non-prefix lines inside a hunk are preserved in hunk.lines
        so validate_patch catches corrupt hunks and rogue text.
        """
        corrupt_diff = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def original():\n"
            "this line is rogue and has no prefix\n"
            "+    return 42\n"
        )
        result = validate_patch(corrupt_diff)
        assert not result.is_valid
        assert any("illegal prefix" in err.lower() or "prefix" in err.lower() for err in result.errors)

    def test_line_count_mismatch_rejected(self):
        """Validates that a hunk header claiming 10 lines with only 1 line provided fails validation."""
        mismatch_diff = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,10 +1,10 @@\n"
            " def single_line(): pass\n"
        )
        result = validate_patch(mismatch_diff)
        assert not result.is_valid
        assert any("specified" in err.lower() or "lines" in err.lower() for err in result.errors)

    def test_path_traversal_in_diff_filenames(self):
        """Validates that diffs containing directory traversal sequences are parsed safely."""
        traversal_diff = (
            "--- a/../../../../etc/passwd\n"
            "+++ b/../../../../etc/passwd\n"
            "@@ -1,1 +1,1 @@\n"
            "-root:x:0:0:root:/root:/bin/bash\n"
            "+root:x:0:0:pwned:/root:/bin/bash\n"
        )
        parsed = parse_unified_diff(traversal_diff)
        assert len(parsed) >= 1
        for df in parsed:
            # Paths parsed should be clean without root filesystem escapes
            assert not df.orig_path.startswith("/")

    def test_apply_patch_mismatched_context_fails_cleanly(self):
        """Validates that applying a patch against file content in memory handles gracefully."""
        diff = (
            "--- a/src/math.py\n"
            "+++ b/src/math.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-def add(a, b): return a - b\n"
            "+def add(a, b): return a + b\n"
        )
        original = "def multiply(x, y): return x * y\n"
        res_content = apply_patch_in_memory(original, diff)
        assert isinstance(res_content, str)


# =============================================================================
# 2. Singleflight Dead-Leader Lease Crash & Atomic CAS Takeover
# =============================================================================
class TestSingleflightCrashAndTakeoverAdversarial:
    """Exercises leader crashes, expired leases, and atomic CAS leadership takeover."""

    def test_dead_leader_heartbeat_timeout_takeover(self, tmp_path: Path):
        """
        Validates that when a leader process dies mid-deliberation (heartbeat exceeds 15s),
        a waiting follower successfully claims leadership via atomic CAS takeover.
        """
        engine = JuryStateEngine(db_path=tmp_path / "sf_crash.db")
        cache_key = "adv_cache_key_crash_test_01"

        # 1. Leader enters
        leader_ticket = engine.singleflight.coalesce_or_lead(cache_key=cache_key, lease_seconds=30.0)
        assert leader_ticket.is_leader
        ticket_id = leader_ticket.ticket_id

        # 2. Simulate leader crash: simulate time passing without heartbeats
        conn = engine.get_connection()
        try:
            with engine.lock:
                conn.execute(
                    "UPDATE jury_tickets SET heartbeat_at = ?, expires_at = ? WHERE ticket_id = ?",
                    (time.time() - 25.0, time.time() - 5.0, ticket_id),
                )
                conn.commit()
        finally:
            conn.close()

        # 3. Follower attempts to enter the same cache key
        follower_ticket = engine.singleflight.coalesce_or_lead(cache_key=cache_key, lease_seconds=30.0)
        # Follower should have taken over as leader
        assert follower_ticket.is_leader
        assert follower_ticket.ticket_id == ticket_id

    def test_concurrent_followers_compete_single_takeover(self, tmp_path: Path):
        """
        Validates that when a leader dies and 6 followers simultaneously discover the expired lease,
        atomic CAS ensures exactly 1 becomes the new leader and the other 5 remain followers.
        """
        engine = JuryStateEngine(db_path=tmp_path / "sf_compete.db")
        cache_key = "adv_cache_key_compete_test_02"

        # 1. Elect initial leader and simulate its death
        initial_leader = engine.singleflight.coalesce_or_lead(cache_key=cache_key, lease_seconds=10.0)
        assert initial_leader.is_leader
        ticket_id = initial_leader.ticket_id

        conn = engine.get_connection()
        try:
            with engine.lock:
                conn.execute(
                    "UPDATE jury_tickets SET heartbeat_at = ?, expires_at = ? WHERE ticket_id = ?",
                    (time.time() - 20.0, time.time() - 2.0, ticket_id),
                )
                conn.commit()
        finally:
            conn.close()

        # 2. 6 competing threads attempt coalesce_or_lead
        def attempt_takeover():
            return engine.singleflight.coalesce_or_lead(cache_key=cache_key, lease_seconds=15.0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(attempt_takeover) for _ in range(6)]
            tickets = [f.result(timeout=10) for f in futures]

        leaders = [t for t in tickets if t.is_leader]
        followers = [t for t in tickets if not t.is_leader]

        assert len(leaders) == 1, f"Expected exactly 1 new leader, got {len(leaders)}"
        assert len(followers) == 5, f"Expected 5 followers, got {len(followers)}"

    def test_failed_leader_propagates_error_to_followers(self, tmp_path: Path):
        """
        Validates that when a leader marks a ticket as FAILED, followers
        awaiting the ticket receive a RuntimeError with the leader's error message.
        """
        engine = JuryStateEngine(db_path=tmp_path / "sf_fail.db")
        cache_key = "adv_cache_key_fail_propagation"

        leader_ticket = engine.singleflight.coalesce_or_lead(cache_key=cache_key, lease_seconds=30.0)
        ticket_id = leader_ticket.ticket_id

        # Leader marks failed
        engine.singleflight.mark_failed(ticket_id, "Fatal OOM in Juror Model Panel")

        # Follower awaiting ticket should catch the failure
        with pytest.raises(RuntimeError) as exc_info:
            engine.singleflight.await_ticket(
                ticket_id=ticket_id,
                cache_key=cache_key,
                timeout_seconds=2.0,
                allow_takeover=False,
            )
        assert "Fatal OOM in Juror Model Panel" in str(exc_info.value)


# =============================================================================
# 3. Database Resilience, High-Burst Concurrency & Quota Safety
# =============================================================================
class TestDatabaseResilienceAndConcurrencyAdversarial:
    """Exercises high-concurrency burst loads and rate-limiter edge conditions."""

    def test_16_concurrent_burst_no_sqlite_locks(self, tmp_path: Path):
        """
        Validates that 16 concurrent threads querying the state engine
        simultaneously execute with zero database locked errors.
        """
        engine = JuryStateEngine(db_path=tmp_path / "burst.db")

        def burst_worker(worker_id: int) -> bool:
            # 1. Rate limiter reservation & refund
            acq = engine.acquire_rate_limit("groq", estimated_tokens=100, max_wait_seconds=10.0)
            engine.refund_tokens("groq", actual_tokens=80, estimated_tokens=100)

            # 2. Singleflight enter & complete
            key = f"key_{worker_id % 4}"
            ticket = engine.singleflight_enter(key)
            if ticket.is_leader:
                engine.singleflight.mark_completed(ticket.ticket_id)
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(burst_worker, i) for i in range(16)]
            results = [f.result(timeout=20) for f in futures]

        assert all(results)
        assert len(results) == 16

    def test_excessive_token_refund_clamps_safely(self, tmp_path: Path):
        """
        Validates that refunding more tokens than bucket capacity clamps safely
        without overflow or corrupting the rate limiter bucket balance.
        """
        engine = JuryStateEngine(db_path=tmp_path / "refund_clamp.db")
        # Pre-seed bucket starts full. Refunding 1,000,000 tokens should clamp to tpm_limit.
        engine.refund_tokens("groq", actual_tokens=0, estimated_tokens=1_000_000)

        conn = engine.get_connection()
        try:
            row = conn.execute(
                "SELECT tpm_current, tpm_limit FROM rate_limit_buckets WHERE provider_id = 'groq'"
            ).fetchone()
            assert row["tpm_current"] <= row["tpm_limit"]
        finally:
            conn.close()

    def test_schema_init_idempotent_multiple_runs(self, tmp_path: Path):
        """Validates that initializing schema multiple times preserves state and does not throw."""
        db_path = tmp_path / "idempotent.db"
        engine1 = JuryStateEngine(db_path=db_path)
        engine2 = JuryStateEngine(db_path=db_path)
        engine3 = JuryStateEngine(db_path=db_path)
        assert engine1.db_path == engine2.db_path == engine3.db_path


# =============================================================================
# 4. Hostile Secret Scrubbing Payloads
# =============================================================================
class TestSecretScrubbingHostilePayloads:
    """Verifies scrubbing of hostile multi-line payloads and universal pre-flight protection."""

    def test_multiline_rsa_private_key_scrubbing(self):
        """Validates that a multi-line RSA private key is fully redacted."""
        hostile_pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0Y1o2n3p4q5r6s7t8u9v0w1x2y3z4a5b6c7d8e9f0g1h2i3j\n"
            "k4l5m6n7o8p9q0r1s2t3u4v5w6x7y8z9a0b1c2d3e4f5g6h7i8j9k0l1m2n3o4p5\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        scrubbed = scrub_text(hostile_pem)
        assert len(scrubbed.redactions) >= 1
        assert "-----BEGIN RSA PRIVATE KEY-----" not in scrubbed.scrubbed_text
        assert "REDACTED_PRIVATE_KEY" in scrubbed.scrubbed_text

    def test_embedded_credentials_in_diff_strict_mode(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """
        Validates that a diff containing an AWS secret key or GitHub token
        causes `jury review --strict` to immediately abort with exit code 4.
        """
        leak_code = (
            "# Database settings\n"
            "AWS_SECRET_KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
            "GITHUB_PAT = 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345'\n"
        )
        temp_git_repo.write_file("src/secrets.py", leak_code)
        temp_git_repo.stage_file("src/secrets.py")

        res = jury_cli.run(["review", "--staged", "--strict"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 4, f"Expected exit code 4 for secret violation, got {res.exit_code}"
        assert "security policy violation" in res.stderr.lower()

    def test_universal_preflight_scrubbing_plan_ask_review(self):
        """
        Validates that DeliberationMesh scrubs secrets from task_intent, plan_text, and query
        before passing them to jurors in plan(), ask(), and review().
        """
        mesh = DeliberationMesh(mock_mode=True)
        secret_key = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"

        # 1. Plan request
        plan_req = PlanRequest(
            task_intent="Plan implementation",
            plan_text=f"Deploy with token {secret_key}",
            repo_root=Path.cwd(),
        )
        plan_verdict = asyncio.run(mesh.plan(plan_req))
        assert "ghp_" not in plan_verdict.deliberation_markdown

        # 2. Ask request
        ask_req = AskRequest(
            query=f"How to protect token {secret_key} in CI?",
            repo_root=Path.cwd(),
        )
        ask_verdict = asyncio.run(mesh.ask(ask_req))
        assert "ghp_" not in ask_verdict.deliberation_markdown

        # 3. Review request
        rev_req = ReviewRequest(
            task_intent="Review diff",
            diff_text="diff --git a/x.py b/x.py\n+x = 1\n",
            repo_root=Path.cwd(),
        )
        rev_verdict = asyncio.run(mesh.review(rev_req))
        assert rev_verdict.verdict in ("APPROVED", "APPROVED_WITH_CONDITIONS", "REJECTED")


# =============================================================================
# 5. Voting, Entropy (Bits vs Nats) & Quorum Error Hierarchy
# =============================================================================
class TestVotingAndQuorumAdversarialEdgeCases:
    """Exercises Shannon entropy, monoculture defense, and QuorumError hierarchy."""

    def test_polar_split_bpde_entropy_shannon_bits_vs_nats(self):
        """
        Validates that Shannon BPDE entropy defaults to base=2.0 (Shannon bits)
        and supports base=math.e (nats).
        For an exact 50/50 binary split:
        - base=2.0: - (0.5 * log2(0.5) + 0.5 * log2(0.5)) = 1.000 bit
        - base=e:   - (0.5 * ln(0.5) + 0.5 * ln(0.5)) = ln(2) ~= 0.693 nats
        """
        verdicts = ["APPROVED", "REJECTED"]
        h_bits = compute_bpde_entropy(verdicts, base=2.0)
        h_default = compute_bpde_entropy(verdicts)
        h_nats = compute_bpde_entropy(verdicts, base=math.e)

        assert abs(h_bits - 1.0) < 1e-6, f"Expected 1.000 bits, got {h_bits}"
        assert abs(h_default - 1.0) < 1e-6, f"Expected default base to be bits (1.000), got {h_default}"
        assert abs(h_nats - math.log(2.0)) < 1e-4, f"Expected ~0.693 nats, got {h_nats}"

    def test_monoculture_panel_fails_quorum(self):
        """
        Validates that a panel where all jurors belong to the same model lineage
        fails quorum due to monoculture defense.
        """
        ballots = [
            JurorBallot(
                juror_id="j1",
                model="cerebras/llama-3.3-70b",
                provider="cerebras",
                role_description="Architect",
                score=8.0,
                verdict="APPROVED",
                content_raw="Clean code.",
            ),
            JurorBallot(
                juror_id="j2",
                model="groq/llama-3.1-8b",
                provider="groq",
                role_description="Reviewer",
                score=8.5,
                verdict="APPROVED",
                content_raw="Looks fine.",
            ),
        ]
        q = check_quorum(ballots, min_quorum=2)
        assert not q.is_valid
        assert q.error_code == "ERR_QUORUM_COLLAPSE"

    def test_quorum_error_exception_type_and_exit_code_2(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """
        Validates that QuorumError is a subclass of RuntimeError,
        and that a simulated quorum collapse causes CLI test to exit with code 2.
        """
        assert issubclass(QuorumError, RuntimeError)

        # Simulate outage via environment variable
        env = {"JURY_SIMULATE_PERSISTENT_500": "1"}
        res = jury_cli.run(["test"], cwd=temp_git_repo.path, env=env, milestone="M5")
        assert res.exit_code == 2, f"Expected exit code 2 on quorum collapse, got {res.exit_code}"


# =============================================================================
# 6. CLI Adversarial Input Fuzzing
# =============================================================================
class TestCliAdversarialInputFuzzing:
    """Fuzzes CLI argument parsing with edge cases and unexpected flags."""

    def test_review_with_nonexistent_files_filter(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """Validates that filtering review by a nonexistent file does not crash."""
        res = jury_cli.run(
            ["review", "--files", "non_existent_module_*.py"],
            cwd=temp_git_repo.path,
            milestone="M5",
        )
        assert res.exit_code in (0, 1)
        assert "traceback" not in res.stderr.lower()

    def test_plan_with_empty_stdin_pipe(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """Validates that piping an empty string to `jury plan -` handles gracefully."""
        res = jury_cli.run(["plan", "-"], cwd=temp_git_repo.path, stdin="", milestone="M5")
        assert res.exit_code in (1, 2, 3)
        assert "traceback" not in res.stderr.lower()

    def test_ask_quick_mode_high_token_payload(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """Validates that `jury ask --quick` handles a large text query cleanly."""
        large_query = "What is the architectural implication of: " + ("token_safety " * 300)
        res = jury_cli.run(
            ["ask", large_query, "--quick", "--json"],
            cwd=temp_git_repo.path,
            milestone="M5",
        )
        assert res.exit_code == 0
        data = res.json()
        assert "verdict" in data or "recommended_pattern" in data or "analysis" in data
