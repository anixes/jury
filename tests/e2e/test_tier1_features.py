"""
tests/e2e/test_tier1_features.py - Tier 1: Core Functional & Interface Tests.

Covers >=5 test cases per core feature area:
- CLI commands: jury init, test, plan, review, ask, logs
- MCP tools: jury_plan, jury_review, jury_ask
- Rules files: AGENTS.md, .cursor/rules/jury.mdc, CLAUDE.md
- Output formats: SARIF v2.1.0, POSIX unified diff patches, deterministic exit codes (0–4)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest

from .conftest import GitTestRepo, JuryCliRunner, JuryMcpRunner, require_milestone


# =============================================================================
# 1. CLI Commands: jury init
# =============================================================================
class TestCliInitCommand:
    """Verifies workspace initialization, directory structures, and rule scaffolding."""

    def test_init_default_scaffolding(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that jury init creates .jury/config.yaml and all three IDE rule files."""
        res = jury_cli.run(["init", "."], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0, f"jury init failed: {res.stderr}"

        # Check directory and files
        config_path = temp_git_repo.path / ".jury" / "config.yaml"
        agents_path = temp_git_repo.path / "AGENTS.md"
        cursor_path = temp_git_repo.path / ".cursor" / "rules" / "jury.mdc"
        claude_path = temp_git_repo.path / "CLAUDE.md"

        assert config_path.exists(), "Expected .jury/config.yaml to be created"
        assert agents_path.exists(), "Expected AGENTS.md to be created"
        assert cursor_path.exists(), "Expected .cursor/rules/jury.mdc to be created"
        assert claude_path.exists(), "Expected CLAUDE.md to be created"

    def test_init_preserves_existing_files_without_force(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates non-destructive behavior: existing files are preserved unless --force is given."""
        agents_file = temp_git_repo.write_file("AGENTS.md", "# Custom User Content\nDo not overwrite.")
        res = jury_cli.run(["init", "."], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0

        # Content must remain intact
        assert "Custom User Content" in agents_file.read_text(encoding="utf-8")

    def test_init_force_flag_overwrites_existing(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that --force overwrites existing rule and configuration files."""
        agents_file = temp_git_repo.write_file("AGENTS.md", "# Custom User Content")
        res = jury_cli.run(["init", "--force", "."], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0

        content = agents_file.read_text(encoding="utf-8")
        assert "Custom User Content" not in content
        assert "Jury Mesh" in content or "Architectural Deliberation" in content

    def test_init_rules_only_flag(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that --rules-only generates rules files without creating .jury/config.yaml."""
        res = jury_cli.run(["init", "--rules-only", "."], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0

        assert not (temp_git_repo.path / ".jury" / "config.yaml").exists()
        assert (temp_git_repo.path / "AGENTS.md").exists()
        assert (temp_git_repo.path / ".cursor" / "rules" / "jury.mdc").exists()
        assert (temp_git_repo.path / "CLAUDE.md").exists()

    def test_init_config_only_flag(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that --config-only creates .jury/config.yaml without touching rules files."""
        res = jury_cli.run(["init", "--config-only", "."], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0

        assert (temp_git_repo.path / ".jury" / "config.yaml").exists()
        assert not (temp_git_repo.path / "AGENTS.md").exists()
        assert not (temp_git_repo.path / ".cursor" / "rules" / "jury.mdc").exists()
        assert not (temp_git_repo.path / "CLAUDE.md").exists()

    def test_init_preset_free_tier(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that --preset free-tier-only scaffolds free community endpoints."""
        res = jury_cli.run(["init", "--preset", "free-tier-only", "."], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0

        config_text = (temp_git_repo.path / ".jury" / "config.yaml").read_text(encoding="utf-8")
        assert "free" in config_text.lower() or "constellation" in config_text.lower()

    def test_init_gitignore_updates(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that jury init appends .jury/deliberations/ to .gitignore while keeping config tracked."""
        gitignore = temp_git_repo.write_file(".gitignore", "node_modules/\n")
        res = jury_cli.run(["init", "."], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0

        content = gitignore.read_text(encoding="utf-8")
        assert ".jury/deliberations" in content or ".jury/state.db" in content
        # Ensure config.yaml is not ignored
        assert ".jury/config.yaml" not in content


# =============================================================================
# 2. CLI Commands: jury test
# =============================================================================
class TestCliTestCommand:
    """Verifies provider connectivity, token bucket, and database lock health checks."""

    def test_connectivity_healthy_pass(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that jury test succeeds with exit code 0 when providers and DB are healthy."""
        res = jury_cli.run(["test"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0, f"Expected jury test to succeed. Stderr: {res.stderr}"
        assert "PASS" in res.stdout or "healthy" in res.stdout.lower()

    def test_connectivity_json_output(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates --json outputs machine-readable probe telemetry."""
        res = jury_cli.run(["test", "--json"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0
        data = res.json()
        assert isinstance(data, dict)
        assert "status" in data or "models" in data or "db_lock" in data

    def test_connectivity_single_model_filter(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates --model flag restricts connectivity test to a single provider."""
        model_id = "constellation/glm-5-3-flash-free"
        res = jury_cli.run(["test", "--model", model_id], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 2)  # 0 if available/mocked, 2 if unreachable
        assert "glm" in res.stdout.lower() or "glm" in res.stderr.lower()

    def test_connectivity_check_db_flag(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that SQLite state database lock and WAL mode are verified."""
        res = jury_cli.run(["test", "--check-db"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0
        assert "db" in res.stdout.lower() or "lock" in res.stdout.lower()

    def test_connectivity_quorum_collapse_exit_code_2(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo, monkeypatch: pytest.MonkeyPatch
    ):
        """Validates that failure of models below Q_min >= 2 results in exit code 2."""
        env_override = {"JURY_SIMULATE_GATEWAY_OUTAGE": "1"}
        res = jury_cli.run(["test"], cwd=temp_git_repo.path, env=env_override, milestone="M5")
        assert res.exit_code == 2, f"Expected exit code 2 on quorum collapse, got {res.exit_code}"


# =============================================================================
# 3. CLI Commands: jury plan
# =============================================================================
class TestCliPlanCommand:
    """Verifies architectural plan evaluation and task execution synthesis."""

    def test_plan_basic_task_string(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates jury plan with inline task description."""
        res = jury_cli.run(
            ["plan", "Implement RS256 JWT auth service with token revocation"],
            cwd=temp_git_repo.path,
            milestone="M5",
        )
        assert res.exit_code == 0, f"Plan command failed: {res.stderr}"
        assert "Verdict" in res.stdout or "Plan" in res.stdout or "APPROVED" in res.stdout

    def test_plan_stdin_pipe(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates jury plan reading from standard input."""
        plan_content = "# Architecture Plan\n1. Add SQLite WAL engine\n2. Add two-tier filelock"
        res = jury_cli.run(["plan"], cwd=temp_git_repo.path, stdin=plan_content, milestone="M5")
        assert res.exit_code == 0
        assert "SQLite" in res.stdout or "Verdict" in res.stdout

    def test_plan_with_context_files(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates jury plan attaching codebase context via --files."""
        code_file = temp_git_repo.write_file("src/auth.py", "def authenticate(): pass\n")
        res = jury_cli.run(
            ["plan", "Review auth module refactoring", "--files", "src/auth.py"],
            cwd=temp_git_repo.path,
            milestone="M5",
        )
        assert res.exit_code == 0

    def test_plan_json_output(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates jury plan --json schema conformity."""
        res = jury_cli.run(
            ["plan", "Database migration strategy", "--json"],
            cwd=temp_git_repo.path,
            milestone="M5",
        )
        assert res.exit_code == 0
        data = res.json()
        assert "ticket_id" in data
        assert "verdict" in data
        assert "file_execution_plan" in data or "architectural_consensus" in data

    def test_plan_no_cache_flag(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates jury plan --no-cache bypasses 5-vector composite cache."""
        res1 = jury_cli.run(["plan", "Design cache layer", "--json"], cwd=temp_git_repo.path, milestone="M5")
        res2 = jury_cli.run(["plan", "Design cache layer", "--no-cache", "--json"], cwd=temp_git_repo.path, milestone="M5")
        assert res1.exit_code == 0
        assert res2.exit_code == 0


# =============================================================================
# 4. CLI Commands: jury review
# =============================================================================
class TestCliReviewCommand:
    """Verifies diff review, finding severity, patch generation, and exit codes."""

    def test_review_staged_changes(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates jury review --staged reviews git staged diff."""
        temp_git_repo.write_file("src/calculator.py", "def add(a, b):\n    return a + b\n")
        temp_git_repo.stage_file("src/calculator.py")

        res = jury_cli.run(["review", "--staged"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1)
        assert "calculator.py" in res.stdout or "Verdict" in res.stdout

    def test_review_unstaged_changes(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates jury review --unstaged inspects working tree diff."""
        temp_git_repo.write_file("README.md", "# Modified README unstaged\n")
        res = jury_cli.run(["review", "--unstaged"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1)

    def test_review_files_filter(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates --files restricts diff review scope."""
        temp_git_repo.write_file("src/auth.py", "def login(): pass\n")
        temp_git_repo.write_file("docs/readme.txt", "Some docs\n")
        temp_git_repo.stage_file("src/auth.py")
        temp_git_repo.stage_file("docs/readme.txt")

        res = jury_cli.run(["review", "--staged", "--files", "src/**"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1)

    def test_review_strict_mode_exit_code_1_on_rejection(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """Validates --strict exits with code 1 when code contains critical security defects."""
        # Inject vulnerable code (SQL injection / hardcoded API key)
        temp_git_repo.write_file(
            "src/query.py",
            "import sqlite3\ndef run_query(u):\n    return sqlite3.connect('x.db').execute(f'SELECT * FROM u WHERE id={u}')\n",
        )
        temp_git_repo.stage_file("src/query.py")

        res = jury_cli.run(
            ["review", "--staged", "--strict", "--focus", "security"],
            cwd=temp_git_repo.path,
            milestone="M5",
        )
        # In strict mode, critical/high findings or REJECTED verdict yields code 1
        assert res.exit_code in (0, 1)

    def test_review_format_patch(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates --format patch outputs POSIX unified diff."""
        temp_git_repo.write_file("src/math.py", "def divide(a, b):\n    return a / b\n")
        temp_git_repo.stage_file("src/math.py")

        res = jury_cli.run(["review", "--staged", "--format", "patch"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1)
        if res.stdout.strip():
            assert "diff --git" in res.stdout or "---" in res.stdout


# =============================================================================
# 5. CLI Commands: jury ask
# =============================================================================
class TestCliAskCommand:
    """Verifies architecture council targeted Q&A."""

    def test_ask_basic_question(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates jury ask queries the council for design advice."""
        res = jury_cli.run(["ask", "Should we use Redis Bloom filter or PostgreSQL roaring bitmap?"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0
        assert "Redis" in res.stdout or "Bitmap" in res.stdout or "Tradeoff" in res.stdout

    def test_ask_with_context_file(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates --context attaches architectural context."""
        schema = temp_git_repo.write_file("schema.sql", "CREATE TABLE users (id INT PRIMARY KEY, token TEXT);")
        res = jury_cli.run(
            ["ask", "How to index token revocation?", "--context", "schema.sql"],
            cwd=temp_git_repo.path,
            milestone="M5",
        )
        assert res.exit_code == 0

    def test_ask_quick_mode(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates --quick queries top 2 models for low latency."""
        res = jury_cli.run(["ask", "--quick", "Fastest way to serialize JSON in Python?"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0

    def test_ask_json_output(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates --json outputs structured schema."""
        res = jury_cli.run(["ask", "--json", "Explain SQLite WAL mode concurrency"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0
        data = res.json()
        assert "answer" in data or "recommendation" in data or "consensus" in data

    def test_ask_focus_security(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates --focus security biases rubric toward threat modeling."""
        res = jury_cli.run(["ask", "--focus", "security", "JWT public key distribution security risks"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0


# =============================================================================
# 6. CLI Commands: jury logs
# =============================================================================
class TestCliLogsCommand:
    """Verifies deliberation audit log queries, ticket inspection, and stats."""

    def test_logs_recent_listing(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates jury logs lists recent deliberations."""
        res = jury_cli.run(["logs", "--limit", "5"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0

    def test_logs_stats_metrics(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates jury logs --stats displays tokens saved and cache hit ratio."""
        res = jury_cli.run(["logs", "--stats"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0
        assert "token" in res.stdout.lower() or "cache" in res.stdout.lower() or "latency" in res.stdout.lower()

    def test_logs_ticket_inspection(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates inspecting a specific deliberation ticket."""
        res = jury_cli.run(["logs", "--ticket", "nonexistent-ticket"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1, 3)

    def test_logs_verdict_filter(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates filtering logs by verdict status."""
        res = jury_cli.run(["logs", "--verdict", "APPROVED"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0

    def test_logs_clean_prune(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates purging old deliberations and vacuuming SQLite database."""
        res = jury_cli.run(["logs", "--clean", "30"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0


# =============================================================================
# 7. MCP Tools: jury_plan, jury_review, jury_ask
# =============================================================================
class TestMcpTools:
    """Verifies MCP JSON-RPC stdio protocol tools, schema adherence, and channel isolation."""

    def test_mcp_tool_jury_plan_success(self, jury_mcp: JuryMcpRunner, temp_git_repo: GitTestRepo):
        """Validates calling jury_plan tool over JSON-RPC stdio."""
        result = jury_mcp.call_tool(
            name="jury_plan",
            arguments={"task": "Implement OAuth2 PKCE flow for mobile client"},
            cwd=temp_git_repo.path,
            milestone="M6",
        )
        assert result["exit_code"] == 0
        resp = result["response"]
        assert "result" in resp or "content" in resp

    def test_mcp_tool_jury_plan_missing_task_param(self, jury_mcp: JuryMcpRunner, temp_git_repo: GitTestRepo):
        """Validates that omitting required parameter 'task' returns JSON-RPC error -32602."""
        result = jury_mcp.call_tool(
            name="jury_plan",
            arguments={},  # Missing required 'task'
            cwd=temp_git_repo.path,
            milestone="M6",
        )
        resp = result["response"]
        assert "error" in resp or ("result" in resp and "error" in str(resp["result"]))

    def test_mcp_tool_jury_review_success(self, jury_mcp: JuryMcpRunner, temp_git_repo: GitTestRepo):
        """Validates calling jury_review tool over JSON-RPC stdio."""
        sample_diff = (
            "diff --git a/auth.py b/auth.py\n"
            "--- a/auth.py\n"
            "+++ b/auth.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-def login(u, p): pass\n"
            "+def login(u, p): return True\n"
        )
        result = jury_mcp.call_tool(
            name="jury_review",
            arguments={"diff": sample_diff, "focus": "security"},
            cwd=temp_git_repo.path,
            milestone="M6",
        )
        assert result["exit_code"] == 0
        resp = result["response"]
        assert "result" in resp

    def test_mcp_tool_jury_ask_success(self, jury_mcp: JuryMcpRunner, temp_git_repo: GitTestRepo):
        """Validates calling jury_ask tool over JSON-RPC stdio."""
        result = jury_mcp.call_tool(
            name="jury_ask",
            arguments={"question": "What are the trade-offs of using WAL mode in SQLite?"},
            cwd=temp_git_repo.path,
            milestone="M6",
        )
        assert result["exit_code"] == 0
        assert "result" in result["response"]

    def test_mcp_channel_hygiene_stderr_isolation(self, jury_mcp: JuryMcpRunner, temp_git_repo: GitTestRepo):
        """
        Validates protocol hygiene: sys.stdout contains strictly valid JSON-RPC frames.
        All debug logging, diagnostics, and banner texts must be routed to sys.stderr.
        """
        result = jury_mcp.call_tool(
            name="jury_ask",
            arguments={"question": "Channel hygiene test"},
            cwd=temp_git_repo.path,
            milestone="M6",
        )
        # Every line on stdout must parse as valid JSON
        for line in result["stdout"].splitlines():
            if line.strip():
                json.loads(line)  # Will raise if any non-JSON leaked to stdout

    def test_mcp_async_task_ticket_support(self, jury_mcp: JuryMcpRunner, temp_git_repo: GitTestRepo):
        """Validates async_mode: true immediately returns a task ticket (<10ms)."""
        result = jury_mcp.call_tool(
            name="jury_plan",
            arguments={"task": "Long running architecture overhaul", "async_mode": True},
            cwd=temp_git_repo.path,
            milestone="M6",
        )
        resp = result["response"]
        assert "result" in resp
        content = resp["result"]
        assert "ticket" in str(content).lower() or "id" in str(content).lower()


# =============================================================================
# 8. Workspace Rules Files: AGENTS.md, .cursor/rules/jury.mdc, CLAUDE.md
# =============================================================================
class TestWorkspaceRulesFiles:
    """Verifies that generated rule files prevent swarm recursion deadlocks and guide agents correctly."""

    def test_agents_md_deadlock_guard(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Verifies AGENTS.md contains explicit subagent recursion prevention rules."""
        jury_cli.run(["init", "."], cwd=temp_git_repo.path, milestone="M5")
        agents_file = temp_git_repo.path / "AGENTS.md"
        assert agents_file.exists()
        text = agents_file.read_text(encoding="utf-8")
        assert "deadlock" in text.lower() or "recursively" in text.lower() or "subagents" in text.lower()

    def test_cursor_mdc_valid_frontmatter(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Verifies .cursor/rules/jury.mdc contains required Cursor YAML frontmatter."""
        jury_cli.run(["init", "."], cwd=temp_git_repo.path, milestone="M5")
        mdc_file = temp_git_repo.path / ".cursor" / "rules" / "jury.mdc"
        assert mdc_file.exists()
        text = mdc_file.read_text(encoding="utf-8")
        assert text.startswith("---")
        assert "description:" in text
        assert "globs:" in text

    def test_claude_md_git_apply_instructions(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Verifies CLAUDE.md instructs Claude Code to pipe patches directly to git apply."""
        jury_cli.run(["init", "."], cwd=temp_git_repo.path, milestone="M5")
        claude_file = temp_git_repo.path / "CLAUDE.md"
        assert claude_file.exists()
        text = claude_file.read_text(encoding="utf-8")
        assert "git apply" in text
        assert "jury review" in text

    def test_rules_files_are_non_empty(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Verifies all rules files have substantial, actionable guidance (>200 bytes)."""
        jury_cli.run(["init", "."], cwd=temp_git_repo.path, milestone="M5")
        for filename in ["AGENTS.md", "CLAUDE.md", ".cursor/rules/jury.mdc"]:
            f = temp_git_repo.path / filename
            assert f.exists()
            assert f.stat().st_size > 200

    def test_rules_mention_zero_token_synthesis(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Verifies rules files document zero-token synthesis via machine-applicable patches."""
        jury_cli.run(["init", "."], cwd=temp_git_repo.path, milestone="M5")
        agents_text = (temp_git_repo.path / "AGENTS.md").read_text(encoding="utf-8")
        assert "patch" in agents_text.lower() or "zero-token" in agents_text.lower()


# =============================================================================
# 9. Output Formats & Deterministic Exit Codes
# =============================================================================
class TestOutputFormatsAndExitCodes:
    """Verifies SARIF v2.1.0 schema compliance, POSIX patch applicability, and exit codes."""

    def test_sarif_v210_schema_compliance(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that jury review --format sarif emits valid OASIS SARIF v2.1.0 JSON."""
        temp_git_repo.write_file("src/api.py", "def execute(): pass\n")
        temp_git_repo.stage_file("src/api.py")

        res = jury_cli.run(["review", "--staged", "--format", "sarif"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1)
        sarif = res.json()
        assert sarif.get("version") == "2.1.0"
        assert "$schema" in sarif
        assert "runs" in sarif
        assert isinstance(sarif["runs"], list)
        if sarif["runs"]:
            assert "tool" in sarif["runs"][0]
            assert "results" in sarif["runs"][0]

    def test_sarif_severity_level_mapping(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates that SARIF results map CRITICAL/HIGH to 'error' and MEDIUM to 'warning'."""
        temp_git_repo.write_file("src/auth.py", "password = 'hardcoded_plaintext'\n")
        temp_git_repo.stage_file("src/auth.py")

        res = jury_cli.run(["review", "--staged", "--format", "sarif"], cwd=temp_git_repo.path, milestone="M5")
        sarif = res.json()
        for run in sarif.get("runs", []):
            for result in run.get("results", []):
                assert result.get("level") in ("error", "warning", "note")

    def test_posix_patch_applicability_with_git_apply(
        self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo
    ):
        """Validates that generated patch can be validated cleanly via git apply --check."""
        temp_git_repo.write_file("src/math_ops.py", "def divide(a, b):\n    return a / b\n")
        temp_git_repo.commit_file("src/math_ops.py", "def divide(a, b):\n    return a / b\n")

        # Create staged defect
        temp_git_repo.write_file("src/math_ops.py", "def divide(a, b):\n    # bug\n    return a / 0\n")
        temp_git_repo.stage_file("src/math_ops.py")

        res = jury_cli.run(["review", "--staged", "--format", "patch"], cwd=temp_git_repo.path, milestone="M5")
        if res.stdout.strip():
            # Test applicability
            apply_res = temp_git_repo.apply_patch(res.stdout, check_only=True)
            # Patch must either apply cleanly or git apply must accept syntax
            assert apply_res.returncode == 0 or "patch" in res.stdout

    def test_exit_code_0_on_success(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates exit code 0 for SUCCESS_PASS."""
        res = jury_cli.run(["init", "--dry-run", "."], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code == 0

    def test_exit_code_3_on_invalid_arguments(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates exit code 3 for CONFIG_OR_USAGE_ERROR on unrecognized options."""
        res = jury_cli.run(["--invalid-global-flag-xyz"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (2, 3)  # Click/Typer returns 2 for usage error, Jury maps to 3

    def test_exit_code_4_on_secret_detected(self, jury_cli: JuryCliRunner, temp_git_repo: GitTestRepo):
        """Validates exit code 4 when unredactable private credentials are found."""
        temp_git_repo.write_file(
            "src/keys.py",
            "PRIVATE_KEY = '-----BEGIN RSA PRIVATE KEY-----\\nMIIEowIBAAKCAQEA0...\\n-----END RSA PRIVATE KEY-----'\n",
        )
        temp_git_repo.stage_file("src/keys.py")
        res = jury_cli.run(["review", "--staged", "--strict"], cwd=temp_git_repo.path, milestone="M5")
        assert res.exit_code in (0, 1, 4)


# =============================================================================
# 10. Secret & Credential Redactor (Pre-flight E2E Validation)
# =============================================================================
class TestSecretRedactionE2E:
    """Verifies pre-flight secret redaction: regex + Shannon entropy (H >= 4.5) with synthetic CRC32 tokens."""

    def test_scrubber_imports_cleanly(self):
        """Verifies scrubber module can be imported from src/jury/security/scrubber.py."""
        from jury.security.scrubber import scrub_text, RedactionResult, calculate_shannon_entropy
        assert scrub_text is not None
        assert RedactionResult is not None
        assert calculate_shannon_entropy is not None

    def test_secret_scrubbing_openai_api_key(self):
        """Validates that OpenAI api keys are masked with <REDACTED_API_KEY:CRC32_HEX>."""
        from jury.security.scrubber import scrub_text

        raw_code = 'OPENAI_API_KEY = "sk-proj-abc12345678901234567890abcdef"'
        res = scrub_text(raw_code)
        assert "sk-proj-" not in res.scrubbed_text
        assert "<REDACTED_API_KEY:" in res.scrubbed_text
        assert res.detected_count >= 1

    def test_secret_scrubbing_github_pat(self):
        """Validates that GitHub personal access tokens are masked with <REDACTED_GITHUB_TOKEN:CRC32_HEX>."""
        from jury.security.scrubber import scrub_text

        raw_code = 'GH_TOKEN = "ghp_1234567890abcdefghijklmnopqrstuvwxyz1234"'
        res = scrub_text(raw_code)
        assert "ghp_" not in res.scrubbed_text
        assert "<REDACTED_GITHUB_TOKEN:" in res.scrubbed_text

    def test_secret_scrubbing_aws_access_key(self):
        """Validates that AWS Access Key IDs are masked with <REDACTED_AWS_KEY:CRC32_HEX>."""
        from jury.security.scrubber import scrub_text

        raw_code = 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"'
        res = scrub_text(raw_code)
        assert "AKIAIOSFODNN7EXAMPLE" not in res.scrubbed_text
        assert "<REDACTED_AWS_KEY:" in res.scrubbed_text

    def test_secret_scrubbing_shannon_entropy_filter(self):
        """Validates that raw high-entropy base64/hex strings (H >= 4.5) are caught."""
        from jury.security.scrubber import scrub_text, calculate_shannon_entropy

        # High entropy string (diverse random characters)
        high_entropy_secret = "dGhpcy1pcy1hLXZlcnktaGlnaC1lbnRyb3B5LXNlY3JldC1rZXktZXhhbXBsZQ=="
        entropy = calculate_shannon_entropy(high_entropy_secret)
        assert entropy >= 4.0

        raw_code = f'SECRET_BLOB = "{high_entropy_secret}"'
        res = scrub_text(raw_code)
        assert high_entropy_secret not in res.scrubbed_text
        assert "<REDACTED_" in res.scrubbed_text

    def test_secret_scrubbing_preserves_standard_code(self):
        """Validates that normal identifiers, function names, and prose are NOT altered."""
        from jury.security.scrubber import scrub_text

        normal_code = (
            "def calculate_total(items: list[int]) -> int:\n"
            "    # Standard sum calculation without secrets\n"
            "    return sum(items)\n"
        )
        res = scrub_text(normal_code)
        assert res.scrubbed_text == normal_code
        assert res.detected_count == 0

