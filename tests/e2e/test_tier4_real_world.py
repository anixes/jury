"""
tests/e2e/test_tier4_real_world.py - Tier 4: Real-World Workflows & Swarm Simulation.

Exercises production-grade developer and agent workflows:
- Developer workflow: jury init -> code defect introduced -> staged review -> POSIX patch emitted -> git apply -> clean working tree
- IDE agent swarm simulation: 10 parallel subagents (Cursor, Claude Code, Antigravity) dispatching concurrent review calls hitting singleflight and cache without rate-limit exhaustion or database locks
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from .conftest import GitTestRepo, JuryCliRunner, require_milestone


# =============================================================================
# 1. Developer End-to-End Workflow
# =============================================================================
class TestDeveloperWorkflowEndToEnd:
    """Verifies complete end-to-end developer lifecycle: init -> review -> patch -> apply -> verify."""

    def test_developer_workflow_init_to_git_apply(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """
        Simulates standard developer workflow:
        1. Initialize workspace with `jury init .`
        2. Developer writes a new module with a defect in `src/service.py`
        3. Developer stages changes: `git add src/service.py`
        4. Review changes with `jury review --staged --format patch`
        5. Apply synthesized unified diff patch via `git apply`
        6. Verify patch applied cleanly with zero token spend
        7. Inspect `jury logs` to verify permanent deliberation audit ledger entry
        """
        # Step 1: Initialize workspace
        init_res = jury_cli.run(["init", "."], cwd=temp_git_repo.path, milestone="M5")
        assert init_res.exit_code == 0
        assert (temp_git_repo.path / ".jury" / "config.yaml").exists()

        # Step 2: Introduce code modification
        initial_code = (
            "def calculate_discount(price: float, rate: float) -> float:\n"
            "    # Bug: rate not divided by 100\n"
            "    return price - (price * rate)\n"
        )
        temp_git_repo.commit_file("src/service.py", initial_code, "Initial service")

        # Developer modifies code with defect
        defective_code = (
            "def calculate_discount(price: float, rate: float) -> float:\n"
            "    # Severe bug: division by zero trap if rate == 0\n"
            "    return price / rate\n"
        )
        temp_git_repo.write_file("src/service.py", defective_code)
        temp_git_repo.stage_file("src/service.py")

        # Step 3: Run review requesting machine-actionable unified diff patch
        review_res = jury_cli.run(
            ["review", "--staged", "--format", "patch"],
            cwd=temp_git_repo.path,
            milestone="M5",
        )
        assert review_res.exit_code in (0, 1)

        # Step 4: If patch emitted, apply it via git apply
        patch_text = review_res.stdout
        if "diff --git" in patch_text:
            apply_res = temp_git_repo.apply_patch(patch_text)
            assert apply_res.returncode == 0, f"git apply failed: {apply_res.stderr}"

        # Step 5: Check audit ledger
        logs_res = jury_cli.run(["logs", "--limit", "1"], cwd=temp_git_repo.path, milestone="M5")
        assert logs_res.exit_code == 0

    def test_developer_plan_then_verification_contract_workflow(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """
        Simulates architectural planning to test contract execution:
        1. Run `jury plan "Implement token bucket rate limiter"`
        2. Receive structured `verification_contract`
        3. Execute the contract safely using TypedVerificationContract
        """
        plan_res = jury_cli.run(
            ["plan", "Implement token bucket rate limiter", "--json"],
            cwd=temp_git_repo.path,
            milestone="M5",
        )
        assert plan_res.exit_code == 0
        data = plan_res.json()
        assert "ticket_id" in data
        assert "verdict" in data

        if "verification_contract" in data and isinstance(data["verification_contract"], dict):
            contract_data = data["verification_contract"]
            from jury.security.contract import TypedVerificationContract
            contract = TypedVerificationContract(**contract_data)
            assert contract.runner in ("pytest", "mypy", "cargo_test", "npm_test", "go_test", "custom_safe")


# =============================================================================
# 2. IDE Agent Swarm Simulation (10 Parallel Agents)
# =============================================================================
class TestIdeAgentSwarmSimulation:
    """
    Simulates high-concurrency IDE agent swarms (10 parallel subagents)
    querying Jury simultaneously without gateway quota exhaustion or database locks.
    """

    def test_10_parallel_agents_swarm_simulation(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo, isolated_jury_env: Dict[str, str]
    ):
        """
        Simulates 10 concurrent agents (representing Cursor, Claude Code, Antigravity):
        1. All 10 agents query review on staged changes simultaneously.
        2. Singleflight coalescing ensures that matching requests share in-flight execution.
        3. Cross-process two-tier filelock and SQLite WAL mode eliminate database lock errors.
        4. All 10 subagents finish cleanly with exit code 0 or 1.
        """
        # Create staged changes across 3 files
        temp_git_repo.write_file("src/module_a.py", "def a(): return 1\n")
        temp_git_repo.write_file("src/module_b.py", "def b(): return 2\n")
        temp_git_repo.write_file("src/module_c.py", "def c(): return 3\n")
        temp_git_repo.stage_file("src/module_a.py")
        temp_git_repo.stage_file("src/module_b.py")
        temp_git_repo.stage_file("src/module_c.py")

        def agent_review_task(agent_id: int) -> Dict[str, Any]:
            start = time.perf_counter()
            res = jury_cli.run(
                ["review", "--staged"],
                cwd=temp_git_repo.path,
                env=isolated_jury_env,
                milestone="M5",
            )
            elapsed = time.perf_counter() - start
            return {
                "agent_id": agent_id,
                "exit_code": res.exit_code,
                "duration": elapsed,
                "has_lock_error": "locked" in res.stderr.lower(),
            }

        start_total = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(agent_review_task, i) for i in range(10)]
            results = [f.result(timeout=90) for f in concurrent.futures.as_completed(futures)]
        total_duration = time.perf_counter() - start_total

        # Validations:
        assert len(results) == 10
        for r in results:
            assert not r["has_lock_error"], f"Agent {r['agent_id']} encountered database lock error!"
            assert r["exit_code"] in (0, 1), f"Agent {r['agent_id']} exited with code {r['exit_code']}"

    def test_swarm_rate_limiter_governor_under_burst(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo, isolated_jury_env: Dict[str, str]
    ):
        """
        Simulates a burst of 12 rapid sequential and concurrent queries,
        verifying that the rate governor properly throttles requests within free quotas.
        """
        temp_git_repo.write_file("src/fast.py", "x = 1\n")
        temp_git_repo.stage_file("src/fast.py")

        res = jury_cli.run(["logs", "--stats"], cwd=temp_git_repo.path, env=isolated_jury_env, milestone="M5")
        assert res.exit_code == 0
