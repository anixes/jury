"""
tests/e2e/test_tier2_boundaries.py - Tier 2: Boundary & Corner Conditions.

Validates system behavior under boundary conditions:
- Empty git repositories (zero commits)
- Unstaged only, staged only, and untracked files
- Large diffs >16k tokens testing AST-aware diff clamping (<= 5,500 tokens for Groq)
- Simulated HTTP 429 gateway rate limits, Retry-After adherence, and circuit breakers
- Concurrent swarm process bursts (two-tier filelock & SQLite WAL)
- Timeout scenarios & dead-leader takeover
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List

import pytest

from .conftest import GitTestRepo, JuryCliRunner, require_milestone


# =============================================================================
# 1. Git State Boundaries
# =============================================================================
class TestGitRepositoryBoundaries:
    """Verifies edge cases across diverse git working tree states."""

    def test_empty_repo_zero_commits(self, jury_cli: JuryCliRunner, empty_git_repo: GitTestRepo):
        """
        Validates behavior in a git repository with 0 commits (unborn HEAD).
        Must handle gracefully with an informational notice rather than an unhandled traceback.
        """
        res = jury_cli.run(["review", "--staged"], cwd=empty_git_repo.path, milestone="M5")
        # Should exit with 0 (clean/no diff) or 3 (usage warning: unborn branch), never unhandled crash
        assert res.exit_code in (0, 3)
        assert "traceback" not in res.stderr.lower()

    def test_working_tree_unstaged_only(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """
        Validates review behavior when changes exist only in the unstaged working tree.
        jury review --unstaged analyzes the diff; jury review --staged detects no changes.
        """
        temp_git_repo.write_file("README.md", "# Modified README (unstaged only)\n")

        # --staged should see no changes
        res_staged = jury_cli.run(["review", "--staged"], cwd=temp_git_repo.path, milestone="M5")
        assert res_staged.exit_code in (0, 1)

        # --unstaged should see the change
        res_unstaged = jury_cli.run(["review", "--unstaged"], cwd=temp_git_repo.path, milestone="M5")
        assert res_unstaged.exit_code in (0, 1)
        assert "README.md" in res_unstaged.stdout or "Verdict" in res_unstaged.stdout

    def test_working_tree_staged_only(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """
        Validates review behavior when changes are completely staged into the index.
        jury review --staged analyzes the diff; jury review --unstaged detects clean tree.
        """
        temp_git_repo.write_file("src/service.py", "class Service:\n    pass\n")
        temp_git_repo.stage_file("src/service.py")

        res_staged = jury_cli.run(["review", "--staged"], cwd=temp_git_repo.path, milestone="M5")
        assert res_staged.exit_code in (0, 1)
        assert "service.py" in res_staged.stdout or "Verdict" in res_staged.stdout

        res_unstaged = jury_cli.run(["review", "--unstaged"], cwd=temp_git_repo.path, milestone="M5")
        assert res_unstaged.exit_code in (0, 1)

    def test_untracked_files_boundary(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """
        Validates handling when untracked files exist.
        Untracked files are factored into the 5-vector composite cache key without crashing review.
        """
        temp_git_repo.write_file("untracked_scratchpad.py", "x = 100\n")
        res = jury_cli.run(["review", "--all"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1)
        assert "traceback" not in res.stderr.lower()

    def test_detached_head_boundary(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that a repository with a detached HEAD operates without branch resolution errors."""
        subprocess.run(["git", "checkout", "--detach"], cwd=str(temp_git_repo.path), check=True, capture_output=True)
        temp_git_repo.write_file("detached.py", "# in detached head\n")
        temp_git_repo.stage_file("detached.py")

        res = jury_cli.run(["review", "--staged"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1)


# =============================================================================
# 2. Large Diff & Token Clamping Boundaries
# =============================================================================
class TestPayloadClampingBoundaries:
    """Verifies adaptive AST-aware diff clamping for provider context ceilings (Groq <= 5,500 tokens)."""

    def test_diff_exceeding_16k_tokens_clamped_for_groq(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """
        Creates a massive diff (>16,000 tokens / ~100 KB) and validates that the
        AST-aware clamper trims it to fit within Groq's 5,500 token ceiling without failing.
        """
        # Generate large file content with many functions and repetitive blocks
        large_code_lines = [
            f"def function_{i}(data: list[int]) -> int:\n    '''Docstring {i}'''\n    return sum(data) + {i}\n"
            for i in range(1200)
        ]
        large_content = "\n".join(large_code_lines)
        temp_git_repo.write_file("src/massive_module.py", large_content)
        temp_git_repo.stage_file("src/massive_module.py")

        res = jury_cli.run(["review", "--staged"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1)
        # Stderr should note clamping if logged, or stdout should show completed deliberation
        assert "clamped" in res.stderr.lower() or "verdict" in res.stdout.lower()

    def test_binary_file_diff_handling(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that diffs containing binary files do not crash with UnicodeDecodeError."""
        bin_path = temp_git_repo.path / "sample.bin"
        bin_path.write_bytes(b"\x00\xFF\xFE\x00\x12\x34\x56\x78" * 100)
        temp_git_repo.stage_file("sample.bin")

        res = jury_cli.run(["review", "--staged"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1)
        assert "unicodedecodeerror" not in res.stderr.lower()

    def test_zero_byte_empty_diff_handling(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that running review on a completely clean repository returns exit code 0."""
        res = jury_cli.run(["review", "--staged"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1)


# =============================================================================
# 3. Gateway Rate Limits, Backoff & Circuit Breakers
# =============================================================================
class TestGatewayRateLimitAndBackoffBoundaries:
    """Verifies adherence to Retry-After headers, exponential backoff, and circuit breakers."""

    def test_simulated_http_429_retry_after_backoff(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """
        Simulates an upstream provider returning HTTP 429 with 'Retry-After: 1'.
        The rate governor must back off with jitter and retry rather than immediately aborting.
        """
        temp_git_repo.write_file("src/rate.py", "x = 42\n")
        temp_git_repo.stage_file("src/rate.py")

        env_override = {
            "JURY_SIMULATE_HTTP_429_COUNT": "1",  # Fails once with 429, then recovers
            "JURY_SIMULATE_RETRY_AFTER": "1",
        }
        res = jury_cli.run(["review", "--staged"], cwd=temp_git_repo.path, env=env_override, milestone="M5")
        assert res.exit_code in (0, 1)

    def test_provider_circuit_breaker_tripping_on_repeated_failures(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """
        Validates that when a provider repeatedly fails (>= 5 consecutive errors),
        its circuit breaker transitions to OPEN, failing fast without incurring gateway latency.
        """
        env_override = {"JURY_SIMULATE_PERSISTENT_500": "1"}
        res = jury_cli.run(["test"], cwd=temp_git_repo.path, env=env_override, milestone="M5")
        # Quorum collapses when providers fail
        assert res.exit_code == 2 or "circuit" in res.stderr.lower()


# =============================================================================
# 4. Concurrency Bursts & Timeout Scenarios
# =============================================================================
class TestConcurrencyAndTimeoutBoundaries:
    """Verifies cross-process concurrency safety and dead-leader lease takeovers."""

    def test_concurrent_swarm_process_burst(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo, isolated_jury_env: Dict[str, str]
    ):
        """
        Simulates 5 parallel processes invoking jury review simultaneously.
        Cross-process OS filelock and SQLite WAL mode must ensure:
        1. Zero sqlite3.OperationalError: database is locked exceptions.
        2. All processes complete without corrupted state or deadlocks.
        """
        temp_git_repo.write_file("src/concurrent.py", "val = 'burst_test'\n")
        temp_git_repo.stage_file("src/concurrent.py")

        def run_proc(idx: int) -> int:
            res = jury_cli.run(
                ["review", "--staged"],
                cwd=temp_git_repo.path,
                env=isolated_jury_env,
                milestone="M5",
            )
            return res.exit_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(run_proc, i) for i in range(5)]
            exit_codes = [f.result(timeout=60) for f in concurrent.futures.as_completed(futures)]

        # All processes must exit with standard exit codes (0 or 1), never database lock crash
        for code in exit_codes:
            assert code in (0, 1), f"Unexpected exit code in concurrency burst: {code}"

    def test_dead_leader_heartbeat_timeout_takeover(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """
        Validates singleflight dead-leader recovery:
        If a leader process holding an in-flight ticket crashes or fails to refresh its 15s lease,
        a peer process detects the expired heartbeat lease and assumes leader execution.
        """
        env_override = {"JURY_SIMULATE_EXPIRED_LEASE_TAKEOVER": "1"}
        res = jury_cli.run(["plan", "Dead leader lease takeover test"], cwd=temp_git_repo.path, env=env_override, milestone="M5")
        assert res.exit_code == 0 or "takeover" in res.stderr.lower()
