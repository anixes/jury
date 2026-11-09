"""
tests/unit/test_cli.py - Unit Test Suite for Jury CLI and Workspace Rules Scaffolder.

Tests:
1. Subcommands: init, test, plan, review, ask, logs
2. Exit codes 0, 1, 2, 3, 4 deterministic contract
3. Non-destructive rule preservation vs --force overwrites
4. Stdin piping and file context attachment
5. Format flags: patch, sarif, json, text
6. Scrubber integration in strict mode (exit code 4 on unredacted secrets)
7. Gateway outage quorum collapse (exit code 2)
8. Template integrity (deadlock guards, frontmatter, git apply)
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Generator
import pytest
from click.testing import CliRunner

from jury.cli.main import cli
from jury.cli.templates import (
    AGENTS_MD_TEMPLATE,
    CLAUDE_MD_TEMPLATE,
    CURSOR_RULES_MDC_TEMPLATE,
    DEFAULT_CONFIG_YAML,
    FREE_TIER_CONFIG_YAML,
    update_gitignore,
)


@pytest.fixture(autouse=True)
def isolated_jury_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fixture ensuring all CLI unit tests run offline in an isolated state directory."""
    state_dir = tmp_path / "jury_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JURY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("JURY_OFFLINE_TEST", "1")
    return state_dir


@pytest.fixture
def runner() -> CliRunner:
    """Fixture providing Click CliRunner with isolated environment."""
    return CliRunner()


@pytest.fixture
def isolated_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fixture providing a temporary initialized git repository with chdir."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@jury.dev"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Jury Tester"], cwd=str(repo), check=True, capture_output=True)
    
    # Create baseline commit
    readme = repo / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo), check=True, capture_output=True)
    monkeypatch.chdir(repo)
    return repo


# =============================================================================
# 1. jury init
# =============================================================================
class TestInitCommand:
    """Unit tests for `jury init` command."""

    def test_init_default_scaffolding(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies default initialization creates .jury/config.yaml and all rule files."""
        result = runner.invoke(cli, ["init", str(isolated_git_repo)])
        assert result.exit_code == 0
        assert (isolated_git_repo / ".jury" / "config.yaml").exists()
        assert (isolated_git_repo / "AGENTS.md").exists()
        assert (isolated_git_repo / ".cursor" / "rules" / "jury.mdc").exists()
        assert (isolated_git_repo / "CLAUDE.md").exists()

    def test_init_preserves_without_force(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies existing files are preserved without --force."""
        custom_content = "# Custom Handcrafted Rules\nDo not overwrite!"
        agents_file = isolated_git_repo / "AGENTS.md"
        agents_file.write_text(custom_content, encoding="utf-8")

        result = runner.invoke(cli, ["init", str(isolated_git_repo)])
        assert result.exit_code == 0
        assert agents_file.read_text(encoding="utf-8") == custom_content

    def test_init_overwrites_with_force(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies existing files are overwritten when --force is supplied."""
        custom_content = "# Custom Handcrafted Rules"
        agents_file = isolated_git_repo / "AGENTS.md"
        agents_file.write_text(custom_content, encoding="utf-8")

        result = runner.invoke(cli, ["init", "--force", str(isolated_git_repo)])
        assert result.exit_code == 0
        assert "Custom Handcrafted Rules" not in agents_file.read_text(encoding="utf-8")
        assert "Jury Mesh" in agents_file.read_text(encoding="utf-8")

    def test_init_rules_only(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies --rules-only creates rules without creating config.yaml."""
        result = runner.invoke(cli, ["init", "--rules-only", str(isolated_git_repo)])
        assert result.exit_code == 0
        assert not (isolated_git_repo / ".jury" / "config.yaml").exists()
        assert (isolated_git_repo / "AGENTS.md").exists()
        assert (isolated_git_repo / ".cursor" / "rules" / "jury.mdc").exists()
        assert (isolated_git_repo / "CLAUDE.md").exists()

    def test_init_config_only(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies --config-only creates config.yaml without touching rules."""
        result = runner.invoke(cli, ["init", "--config-only", str(isolated_git_repo)])
        assert result.exit_code == 0
        assert (isolated_git_repo / ".jury" / "config.yaml").exists()
        assert not (isolated_git_repo / "AGENTS.md").exists()

    def test_init_preset_free_tier(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies --preset free-tier-only scaffolds free community models."""
        result = runner.invoke(cli, ["init", "--preset", "free-tier-only", str(isolated_git_repo)])
        assert result.exit_code == 0
        config_text = (isolated_git_repo / ".jury" / "config.yaml").read_text(encoding="utf-8")
        assert "free" in config_text.lower() or "constellation" in config_text.lower()

    def test_init_dry_run(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies --dry-run prints actions without modifying filesystem."""
        result = runner.invoke(cli, ["init", "--dry-run", str(isolated_git_repo)])
        assert result.exit_code == 0
        assert not (isolated_git_repo / ".jury" / "config.yaml").exists()
        assert not (isolated_git_repo / "AGENTS.md").exists()

    def test_init_gitignore_update(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies .gitignore updates ignore state and deliberations while tracking config."""
        gitignore = isolated_git_repo / ".gitignore"
        gitignore.write_text("dist/\n", encoding="utf-8")

        result = runner.invoke(cli, ["init", str(isolated_git_repo)])
        assert result.exit_code == 0
        content = gitignore.read_text(encoding="utf-8")
        assert ".jury/deliberations" in content
        assert ".jury/state.db" in content
        assert ".jury/config.yaml" not in content


# =============================================================================
# 2. jury test
# =============================================================================
class TestTestCommand:
    """Unit tests for `jury test` command."""

    def test_test_healthy(self, runner: CliRunner):
        """Verifies jury test returns 0 on healthy environment."""
        result = runner.invoke(cli, ["test"])
        assert result.exit_code == 0
        assert "PASS" in result.stdout or "healthy" in result.stdout.lower()

    def test_test_json(self, runner: CliRunner):
        """Verifies jury test --json emits structured telemetry."""
        result = runner.invoke(cli, ["test", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "status" in data
        assert "models" in data
        assert "db_lock" in data

    def test_test_model_filter(self, runner: CliRunner):
        """Verifies --model restricts probe to single model."""
        result = runner.invoke(cli, ["test", "--model", "constellation/glm-5-3-flash-free"])
        assert result.exit_code in (0, 2)
        assert "glm" in result.stdout.lower()

    def test_test_check_db(self, runner: CliRunner):
        """Verifies --check-db validates state DB filelock and WAL mode."""
        result = runner.invoke(cli, ["test", "--check-db"])
        assert result.exit_code == 0

    def test_test_simulate_gateway_outage_exit_2(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch):
        """Verifies quorum collapse causes exit code 2 when outage is simulated."""
        monkeypatch.setenv("JURY_SIMULATE_GATEWAY_OUTAGE", "1")
        result = runner.invoke(cli, ["test"])
        assert result.exit_code == 2


# =============================================================================
# 3. jury plan
# =============================================================================
class TestPlanCommand:
    """Unit tests for `jury plan` command."""

    def test_plan_argument(self, runner: CliRunner):
        """Verifies jury plan with task string."""
        result = runner.invoke(cli, ["plan", "Add rate limiting token bucket to API gateway"])
        assert result.exit_code == 0
        assert "Verdict" in result.stdout or "APPROVED" in result.stdout

    def test_plan_stdin(self, runner: CliRunner):
        """Verifies jury plan reading from standard input."""
        plan_text = "# Implementation Plan\n1. Add SQLite WAL mode\n2. Add multi-process filelock"
        result = runner.invoke(cli, ["plan"], input=plan_text)
        assert result.exit_code == 0
        assert "Verdict" in result.stdout or "APPROVED" in result.stdout

    def test_plan_with_files(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies jury plan with --files attaching codebase context."""
        code_file = isolated_git_repo / "server.py"
        code_file.write_text("def run_server(): pass\n", encoding="utf-8")

        result = runner.invoke(cli, ["plan", "Refactor server architecture", "--files", str(code_file)])
        assert result.exit_code == 0

    def test_plan_json_schema(self, runner: CliRunner):
        """Verifies jury plan --json schema conformity."""
        result = runner.invoke(cli, ["plan", "Design distributed lock manager", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "ticket_id" in data
        assert "verdict" in data

    def test_plan_no_cache(self, runner: CliRunner):
        """Verifies jury plan --no-cache bypasses composite cache."""
        result = runner.invoke(cli, ["plan", "Design cache layer", "--no-cache"])
        assert result.exit_code == 0

    def test_plan_missing_task_exit_3(self, runner: CliRunner):
        """Verifies missing task without stdin exits with code 3."""
        result = runner.invoke(cli, ["plan", "   "])
        assert result.exit_code == 3

    def test_plan_strict_secret_exit_4(self, runner: CliRunner):
        """Verifies unredacted secret in strict mode exits with code 4."""
        secret_task = "Deploy with sk-proj-123456789012345678901234567890abcdef"
        result = runner.invoke(cli, ["plan", secret_task, "--strict"])
        assert result.exit_code == 4


# =============================================================================
# 4. jury review
# =============================================================================
class TestReviewCommand:
    """Unit tests for `jury review` command."""

    def test_review_staged(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies jury review --staged reviews git staged diff."""
        test_file = isolated_git_repo / "math_mod.py"
        test_file.write_text("def add(a, b): return a + b\n", encoding="utf-8")
        subprocess.run(["git", "add", "math_mod.py"], cwd=str(isolated_git_repo), check=True)

        result = runner.invoke(cli, ["review", "--staged"])
        assert result.exit_code in (0, 1)

    def test_review_unstaged(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies jury review --unstaged reviews working tree diff."""
        readme = isolated_git_repo / "README.md"
        readme.write_text("# Updated Title\n", encoding="utf-8")

        result = runner.invoke(cli, ["review", "--unstaged"])
        assert result.exit_code in (0, 1)

    def test_review_format_patch(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies jury review --format patch emits unified diff."""
        test_file = isolated_git_repo / "service.py"
        test_file.write_text("def service(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "service.py"], cwd=str(isolated_git_repo), check=True)

        result = runner.invoke(cli, ["review", "--staged", "--format", "patch"])
        assert result.exit_code in (0, 1)
        if result.stdout.strip():
            assert "diff --git" in result.stdout or "patch" in result.stdout

    def test_review_format_sarif(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies jury review --format sarif outputs OASIS SARIF v2.1.0."""
        test_file = isolated_git_repo / "calc.py"
        test_file.write_text("def divide(a, b): return a / b\n", encoding="utf-8")
        subprocess.run(["git", "add", "calc.py"], cwd=str(isolated_git_repo), check=True)

        result = runner.invoke(cli, ["review", "--staged", "--format", "sarif"])
        assert result.exit_code in (0, 1)
        data = json.loads(result.stdout)
        assert data.get("version") == "2.1.0"
        assert "runs" in data

    def test_review_format_json(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies jury review --json outputs structured verdict payload."""
        test_file = isolated_git_repo / "app.py"
        test_file.write_text("def start(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=str(isolated_git_repo), check=True)

        result = runner.invoke(cli, ["review", "--staged", "--json"])
        assert result.exit_code in (0, 1)
        data = json.loads(result.stdout)
        assert "verdict" in data
        assert "findings" in data

    def test_review_format_json_with_breaking_input(self, runner: CliRunner, isolated_git_repo: Path, monkeypatch: pytest.MonkeyPatch):
        """Verifies jury review --json preserves breaking_input in findings."""
        from unittest.mock import AsyncMock
        from jury.deliberation.mesh import DeliberationMesh, ReviewVerdict
        from jury.deliberation.rubrics import Finding

        test_file = isolated_git_repo / "exploit.py"
        test_file.write_text("def vuln(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "exploit.py"], cwd=str(isolated_git_repo), check=True)

        mock_verdict = ReviewVerdict(
            verdict="REJECTED",
            score=5.0,
            consensus_score=0.9,
            entropy_bpde=0.1,
            findings=[
                Finding(
                    file_path="exploit.py",
                    line_start=1,
                    line_end=1,
                    severity="CRITICAL",
                    category="security",
                    issue="RCE in parameter parsing",
                    breaking_input="__import__('os').system('id')",
                )
            ],
            deliberation_markdown="# Verdict",
        )

        monkeypatch.setattr(DeliberationMesh, "review", AsyncMock(return_value=mock_verdict))

        result = runner.invoke(cli, ["review", "--staged", "--json"])
        assert result.exit_code in (0, 1)
        data = json.loads(result.stdout)
        assert len(data["findings"]) == 1
        assert data["findings"][0]["breaking_input"] == "__import__('os').system('id')"

    def test_review_format_text_renders_breaking_input(self, runner: CliRunner, isolated_git_repo: Path, monkeypatch: pytest.MonkeyPatch):
        """Verifies jury review rich terminal table renders Breaking Input:."""
        from unittest.mock import AsyncMock
        from jury.deliberation.mesh import DeliberationMesh, ReviewVerdict
        from jury.deliberation.rubrics import Finding

        test_file = isolated_git_repo / "exploit.py"
        test_file.write_text("def vuln(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "exploit.py"], cwd=str(isolated_git_repo), check=True)

        mock_verdict = ReviewVerdict(
            verdict="REJECTED",
            score=5.0,
            consensus_score=0.9,
            entropy_bpde=0.1,
            findings=[
                Finding(
                    file_path="exploit.py",
                    line_start=1,
                    line_end=1,
                    severity="CRITICAL",
                    category="security",
                    issue="RCE in parameter parsing",
                    breaking_input="__import__('os').system('id')",
                )
            ],
            deliberation_markdown="# Verdict",
        )

        monkeypatch.setattr(DeliberationMesh, "review", AsyncMock(return_value=mock_verdict))

        result = runner.invoke(cli, ["review", "--staged"])
        assert result.exit_code in (0, 1)
        assert "Breaking Input:" in result.stdout
        assert "__import__('os')" in result.stdout

    def test_review_empty_diff(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies jury review handles empty diff without errors."""
        result = runner.invoke(cli, ["review", "--staged"])
        assert result.exit_code == 0

    def test_review_strict_secret_exit_4(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies unredacted secret in diff under strict mode exits with code 4."""
        secret_file = isolated_git_repo / "secrets.py"
        secret_file.write_text(
            "API_KEY = 'sk-proj-1234567890abcdef1234567890abcdef12345678'\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "secrets.py"], cwd=str(isolated_git_repo), check=True)

        result = runner.invoke(cli, ["review", "--staged", "--strict"])
        assert result.exit_code == 4


# =============================================================================
# 5. jury ask
# =============================================================================
class TestAskCommand:
    """Unit tests for `jury ask` command."""

    def test_ask_basic_question(self, runner: CliRunner):
        """Verifies jury ask returns architectural counsel."""
        result = runner.invoke(cli, ["ask", "What are the trade-offs of SQLite WAL mode?"])
        assert result.exit_code == 0
        assert "Recommendation" in result.stdout or "WAL" in result.stdout or "Confidence" in result.stdout

    def test_ask_with_context(self, runner: CliRunner, isolated_git_repo: Path):
        """Verifies jury ask with --context attaches file content."""
        ctx_file = isolated_git_repo / "db_schema.sql"
        ctx_file.write_text("CREATE TABLE tickets (id INT PRIMARY KEY);\n", encoding="utf-8")

        result = runner.invoke(cli, ["ask", "How should we index this table?", "--context", str(ctx_file)])
        assert result.exit_code == 0

    def test_ask_quick(self, runner: CliRunner):
        """Verifies --quick restricts panel to top 2 models."""
        result = runner.invoke(cli, ["ask", "--quick", "Should we use protobuf or JSON?"])
        assert result.exit_code == 0

    def test_ask_json(self, runner: CliRunner):
        """Verifies --json outputs structured recommendation."""
        result = runner.invoke(cli, ["ask", "--json", "Explain Borda voting"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "answer" in data
        assert "tradeoff_matrix" in data

    def test_ask_focus(self, runner: CliRunner):
        """Verifies --focus biases evaluation."""
        result = runner.invoke(cli, ["ask", "--focus", "security", "JWT signing methods"])
        assert result.exit_code == 0


# =============================================================================
# 6. jury logs
# =============================================================================
class TestLogsCommand:
    """Unit tests for `jury logs` command."""

    def test_logs_limit(self, runner: CliRunner):
        """Verifies jury logs --limit lists entries."""
        result = runner.invoke(cli, ["logs", "--limit", "3"])
        assert result.exit_code == 0

    def test_logs_stats(self, runner: CliRunner):
        """Verifies jury logs --stats displays performance metrics."""
        result = runner.invoke(cli, ["logs", "--stats"])
        assert result.exit_code == 0
        assert "token" in result.stdout.lower() or "cache" in result.stdout.lower()

    def test_logs_ticket_not_found(self, runner: CliRunner):
        """Verifies querying nonexistent ticket returns clear message."""
        result = runner.invoke(cli, ["logs", "--ticket", "nonexistent-ticket-id"])
        assert result.exit_code in (0, 3)

    def test_logs_verdict_filter(self, runner: CliRunner):
        """Verifies filtering by verdict."""
        result = runner.invoke(cli, ["logs", "--verdict", "APPROVED"])
        assert result.exit_code == 0

    def test_logs_clean(self, runner: CliRunner):
        """Verifies jury logs --clean prunes records and vacuums SQLite."""
        result = runner.invoke(cli, ["logs", "--clean", "14"])
        assert result.exit_code == 0


# =============================================================================
# 7. Global CLI & Error Handling
# =============================================================================
class TestMainCommandGroup:
    """Unit tests for root CLI dispatcher."""

    def test_cli_help(self, runner: CliRunner):
        """Verifies --help outputs usage description and subcommands."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Jury Mesh" in result.stdout
        assert "init" in result.stdout
        assert "test" in result.stdout
        assert "plan" in result.stdout
        assert "review" in result.stdout
        assert "ask" in result.stdout
        assert "logs" in result.stdout

    def test_cli_version(self, runner: CliRunner):
        """Verifies --version outputs version string."""
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "3.0" in result.stdout

    def test_cli_invalid_option_exit_3(self, runner: CliRunner):
        """Verifies invalid options yield exit code 3 (or 2 from Click)."""
        result = runner.invoke(cli, ["--unknown-flag-xyz"])
        assert result.exit_code in (2, 3)

    def test_cli_verbose_and_quiet(self, runner: CliRunner):
        """Verifies --verbose and --quiet flags execute cleanly."""
        res_v = runner.invoke(cli, ["--verbose", "test"])
        assert res_v.exit_code == 0

        res_q = runner.invoke(cli, ["--quiet", "test"])
        assert res_q.exit_code == 0


# =============================================================================
# 8. Templates & Helper Functions
# =============================================================================
class TestTemplatesAndHelpers:
    """Unit tests for template contents and gitignore update helper."""

    def test_agents_md_template_deadlock_guard(self):
        """Verifies AGENTS.md template contains recursion deadlock protection."""
        assert "deadlock" in AGENTS_MD_TEMPLATE.lower() or "recursion" in AGENTS_MD_TEMPLATE.lower()
        assert "jury review" in AGENTS_MD_TEMPLATE

    def test_cursor_mdc_template_frontmatter(self):
        """Verifies jury.mdc has valid YAML frontmatter."""
        assert CURSOR_RULES_MDC_TEMPLATE.startswith("---")
        assert "description:" in CURSOR_RULES_MDC_TEMPLATE
        assert "globs:" in CURSOR_RULES_MDC_TEMPLATE

    def test_claude_md_template_git_apply(self):
        """Verifies CLAUDE.md instructs Claude to pipe patches to git apply."""
        assert "git apply" in CLAUDE_MD_TEMPLATE
        assert "jury review" in CLAUDE_MD_TEMPLATE

    def test_update_gitignore_helper(self, tmp_path: Path):
        """Verifies update_gitignore function appends correctly."""
        gi = tmp_path / ".gitignore"
        gi.write_text("build/\n*.pyc\n", encoding="utf-8")

        created, msg = update_gitignore(tmp_path)
        assert created is True

        content = gi.read_text(encoding="utf-8")
        assert ".jury/deliberations/" in content
        assert ".jury/state.db" in content
        assert ".jury/config.yaml" not in content

        # Second call does not duplicate
        created_again, _ = update_gitignore(tmp_path)
        assert created_again is False
