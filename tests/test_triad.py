"""
packages/triad/tests/test_standalone.py - Standalone unit and integration test suite for Triad CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure triad package is importable
TRIAD_PKG_DIR = Path(__file__).resolve().parent.parent
if str(TRIAD_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(TRIAD_PKG_DIR))

from triad.cli import handle_init, main
from triad.client import (
    ModelEvaluationResult,
    extract_response_text,
    extract_score_and_verdict,
    query_model_with_retry,
    run_multi_model_query,
)
from triad.config import (
    TriadConfig,
    find_project_env_file,
    get_global_config_path,
    init_config_file,
    load_env_dict,
    resolve_config,
)
from triad.consultant import build_consult_prompt, load_spec_document
from triad.report import generate_json_report, generate_markdown_report
from triad.reviewer import build_review_prompt, get_git_diff, load_test_files


# ============================================================================
# 1. Configuration Resolution Tests
# ============================================================================

def test_config_dataclass_validity():
    cfg_empty = TriadConfig()
    assert not cfg_empty.is_valid

    cfg_valid = TriadConfig(api_key="sk-test", models=["model-1", "model-2"])
    assert cfg_valid.is_valid


def test_init_config_file(tmp_path: Path):
    target = tmp_path / "subdir" / ".triad.env"
    created = init_config_file(target)
    assert created.is_file()
    content = created.read_text(encoding="utf-8")
    assert "TRIAD_GATEWAY_URL=" in content
    assert "TRIAD_API_KEY=" in content
    assert "TRIAD_MODELS=" in content


def test_resolve_config_cli_overrides(tmp_path: Path):
    env_mock = {
        "TRIAD_API_KEY": "env-key",
        "TRIAD_MODELS": "env-model-1",
        "USERPROFILE": str(tmp_path),
        "HOME": str(tmp_path),
    }
    with patch.dict("os.environ", env_mock, clear=True):
        cfg = resolve_config(
            cli_models="cli-m1,cli-m2",
            cli_api_key="cli-key",
            cli_gateway_url="https://cli.gateway/v1",
            cli_min_score=8.5,
            cli_strict=True,
            cli_repo=str(tmp_path),
        )
        assert cfg.api_key == "cli-key"
        assert cfg.models == ["cli-m1", "cli-m2"]
        assert cfg.gateway_url == "https://cli.gateway/v1"
        assert cfg.min_score == 8.5
        assert cfg.strict is True
        assert cfg.repo_dir == tmp_path.resolve()


def test_resolve_config_local_env_file(tmp_path: Path):
    env_file = tmp_path / ".triad.env"
    env_file.write_text("TRIAD_API_KEY=local-file-key\nTRIAD_MODELS=local-m1,local-m2\n", encoding="utf-8")

    env_mock = {
        "USERPROFILE": str(tmp_path),
        "HOME": str(tmp_path),
    }
    with patch.dict("os.environ", env_mock, clear=True):
        cfg = resolve_config(cli_repo=str(tmp_path))
        assert cfg.api_key == "local-file-key"
        assert cfg.models == ["local-m1", "local-m2"]


def test_resolve_config_env_overrides_local_file(tmp_path: Path):
    env_file = tmp_path / ".triad.env"
    env_file.write_text("TRIAD_API_KEY=stale-file-key\nTRIAD_MODELS=stale-m1\n", encoding="utf-8")

    env_mock = {
        "TRIAD_API_KEY": "env-ci-key",
        "TRIAD_MODELS": "env-ci-m1,env-ci-m2",
        "USERPROFILE": str(tmp_path),
        "HOME": str(tmp_path),
    }
    with patch.dict("os.environ", env_mock, clear=True):
        cfg = resolve_config(cli_repo=str(tmp_path))
        assert cfg.api_key == "env-ci-key"
        assert cfg.models == ["env-ci-m1", "env-ci-m2"]


def test_resolve_config_global_fallback(tmp_path: Path):
    fake_home = tmp_path / "fakehome"
    fake_global = fake_home / ".triad" / "config.env"
    fake_global.parent.mkdir(parents=True)
    fake_global.write_text("TRIAD_API_KEY=global-key\nTRIAD_MODELS=global-m1\n", encoding="utf-8")

    with patch("pathlib.Path.home", return_value=fake_home):
        with patch.dict("os.environ", {}, clear=True):
            cfg = resolve_config(cli_repo=str(tmp_path))
            assert cfg.api_key == "global-key"
            assert cfg.models == ["global-m1"]


# ============================================================================
# 2. Parsing and Evaluation Client Tests
# ============================================================================

def test_extract_score_and_verdict_approved():
    sample = """
    ## Review Summary
    Architectural decomposition is stellar and zero hardcoded dependencies.
    Score: 9.5 / 10
    Verdict: APPROVED
    """
    score, verdict = extract_score_and_verdict(sample)
    assert score == 9.5
    assert verdict == "APPROVED"


def test_extract_score_and_verdict_rejected():
    sample = """
    Security vulnerability found.
    Production Readiness Score: 3.0/10
    Verdict: REJECTED
    """
    score, verdict = extract_score_and_verdict(sample)
    assert score == 3.0
    assert verdict == "REJECTED"


def test_extract_response_text_reasoning_fallback():
    choice = MagicMock()
    choice.message.content = ""
    choice.message.reasoning = "Deep internal chain of thought analysis."
    choice.message.reasoning_content = None

    c, r, final = extract_response_text(choice)
    assert c == ""
    assert r == "Deep internal chain of thought analysis."
    assert final == "Deep internal chain of thought analysis."


def test_query_model_with_retry_backoff_on_429():
    mock_client = MagicMock()
    mock_create = AsyncMock()

    # Fail with 429 twice, then succeed
    choice_ok = MagicMock()
    choice_ok.message.content = "Success review. Score: 8.5/10 Verdict: APPROVED"
    choice_ok.message.reasoning = None
    choice_ok.message.reasoning_content = None
    resp_ok = MagicMock(choices=[choice_ok])

    mock_create.side_effect = [
        Exception("HTTP 429: Too Many Requests"),
        Exception("HTTP 429: Rate limited"),
        resp_ok,
    ]
    mock_client.chat.completions.create = mock_create

    async def _run():
        return await query_model_with_retry(
            client=mock_client,
            model="test-model",
            system_prompt="sys",
            user_prompt="usr",
            max_retries=3,
            initial_backoff=0.01,
        )

    success, content, reasoning, final, err = asyncio.run(_run())
    assert success is True
    assert "Success review" in final
    assert err is None
    assert mock_create.call_count == 3


# ============================================================================
# 3. Reviewer and Consultant Tests
# ============================================================================

def test_git_diff_unstaged(tmp_path: Path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="+ def new_function(): pass\n", returncode=0)
        diff = get_git_diff(tmp_path, target="unstaged")
        assert "+ def new_function(): pass" in diff
        assert mock_run.call_args[0][0] == ["git", "diff", "-U2"]


def test_load_test_files_budget(tmp_path: Path):
    f1 = tmp_path / "test_1.py"
    f1.write_text("def test_one(): pass\n" * 5, encoding="utf-8")
    loaded = load_test_files(tmp_path, [str(f1)], max_total_chars=1000)
    assert "test_1.py" in loaded
    assert "def test_one" in loaded


def test_consultant_spec_loading(tmp_path: Path):
    spec_file = tmp_path / "design.md"
    spec_file.write_text("# System Architecture\nScalable event loop design.", encoding="utf-8")

    loaded = load_spec_document(tmp_path, "design.md")
    assert "# System Architecture" in loaded

    prompt = build_consult_prompt("Review failure modes", loaded)
    assert "Review failure modes" in prompt
    assert "Scalable event loop design" in prompt


# ============================================================================
# 4. Report Generation Tests
# ============================================================================

def test_markdown_and_json_reports(tmp_path: Path):
    res1 = ModelEvaluationResult(
        model="model-alpha",
        label="Reviewer 1",
        success=True,
        content="Content A",
        final_text="Review text A. Score: 9.0/10 Verdict: APPROVED",
        score=9.0,
        verdict="APPROVED",
        duration_s=2.5,
        output_file=tmp_path / "model_alpha_review.md",
    )
    res1.output_file.write_text(res1.final_text, encoding="utf-8")

    res2 = ModelEvaluationResult(
        model="model-beta",
        label="Reviewer 2",
        success=True,
        content="Content B",
        final_text="Review text B. Score: 8.0/10 Verdict: APPROVED WITH COMMENTS",
        score=8.0,
        verdict="APPROVED WITH COMMENTS",
        duration_s=3.0,
        output_file=tmp_path / "model_beta_review.md",
    )
    res2.output_file.write_text(res2.final_text, encoding="utf-8")

    md_out = tmp_path / "report.md"
    md_content = generate_markdown_report("review", [res1, res2], md_out)
    assert "| **Reviewer 1** (`model-alpha`) |" in md_content
    assert "9.0/10" in md_content
    assert "APPROVED" in md_content
    assert md_out.is_file()

    json_out = tmp_path / "report.json"
    payload = generate_json_report("review", [res1, res2], json_out)
    assert payload["all_passed"] is True
    assert payload["average_score"] == 8.5
    assert len(payload["results"]) == 2
    assert json_out.is_file()


# ============================================================================
# 5. CLI Execution & Gate Tests
# ============================================================================

def test_cli_init_command(tmp_path: Path):
    target = tmp_path / ".triad.env"
    args = argparse.Namespace(is_global=False, force=False)
    with patch("pathlib.Path.cwd", return_value=tmp_path):
        code = handle_init(args)
        assert code == 0
        assert target.is_file()

        # Running without force should return 1
        code_exists = handle_init(args)
        assert code_exists == 1

        # Running with force should succeed
        args.force = True
        code_force = handle_init(args)
        assert code_force == 0


def test_cli_missing_models_or_key_exit(tmp_path: Path):
    with patch.dict("os.environ", {}, clear=True):
        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch("triad.config.find_project_env_file", return_value=None):
                code = main(["review", "--repo", str(tmp_path)])
                assert code == 1


def test_cli_review_strict_mode_gate_passes(tmp_path: Path):
    mock_result = ModelEvaluationResult(
        model="model-gate-pass",
        label="Reviewer",
        success=True,
        score=9.0,
        verdict="APPROVED",
        duration_s=1.0,
        output_file=tmp_path / "out.md",
    )
    mock_result.output_file.write_text("Verdict: APPROVED Score: 9/10", encoding="utf-8")

    with patch("triad.cli.run_multi_model_query", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = [mock_result]
        with patch("triad.cli.get_git_diff", return_value="+ new line"):
            code = main([
                "review",
                "--repo", str(tmp_path),
                "--models", "model-gate-pass",
                "--api-key", "sk-mock",
                "--strict",
                "--min-score", "8.0",
            ])
            assert code == 0


def test_cli_review_strict_mode_gate_fails_on_rejection(tmp_path: Path):
    mock_rejected = ModelEvaluationResult(
        model="model-gate-fail",
        label="Reviewer",
        success=True,
        score=4.0,
        verdict="REJECTED",
        duration_s=1.0,
        output_file=tmp_path / "out.md",
    )
    mock_rejected.output_file.write_text("Verdict: REJECTED Score: 4/10", encoding="utf-8")

    with patch("triad.cli.run_multi_model_query", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = [mock_rejected]
        with patch("triad.cli.get_git_diff", return_value="+ new line"):
            code = main([
                "review",
                "--repo", str(tmp_path),
                "--models", "model-gate-fail",
                "--api-key", "sk-mock",
                "--strict",
                "--min-score", "8.0",
            ])
            assert code == 1


def test_cli_review_strict_mode_gate_fails_on_model_error(tmp_path: Path):
    mock_err = ModelEvaluationResult(
        model="model-err",
        label="Reviewer",
        success=False,
        error="Gateway timeout",
        duration_s=1.0,
    )
    with patch("triad.cli.run_multi_model_query", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = [mock_err]
        with patch("triad.cli.get_git_diff", return_value="+ new line"):
            code = main([
                "review",
                "--repo", str(tmp_path),
                "--models", "model-err",
                "--api-key", "sk-mock",
                "--strict",
            ])
            assert code == 1


def test_cli_review_strict_mode_gate_fails_on_unknown_verdict_or_none_score(tmp_path: Path):
    mock_unknown = ModelEvaluationResult(
        model="model-unparsed",
        label="Reviewer",
        success=True,
        score=None,
        verdict="UNKNOWN",
        duration_s=1.0,
        output_file=tmp_path / "out.md",
    )
    mock_unknown.output_file.write_text("Unstructured gibberish without score", encoding="utf-8")

    with patch("triad.cli.run_multi_model_query", new_callable=AsyncMock) as mock_query:
        mock_query.return_value = [mock_unknown]
        with patch("triad.cli.get_git_diff", return_value="+ new line"):
            code = main([
                "review",
                "--repo", str(tmp_path),
                "--models", "model-unparsed",
                "--api-key", "sk-mock",
                "--strict",
            ])
            assert code == 1


def test_git_diff_timeout_handled(tmp_path: Path):
    import subprocess
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30.0)):
        diff = get_git_diff(tmp_path, timeout_s=30.0)
        assert "timed out after 30.0s" in diff


def test_load_test_files_traversal_blocked(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("sensitive data", encoding="utf-8")

    output = load_test_files(repo_dir, ["../secret.txt"])
    assert "Security Warning: Path traversal detected" in output
    assert "sensitive data" not in output


def test_consultant_path_traversal_blocked(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    outside_spec = tmp_path / "outside_spec.md"
    outside_spec.write_text("sensitive spec", encoding="utf-8")

    output = load_spec_document(repo_dir, "../outside_spec.md")
    assert "Security Error" in output
    assert "Path traversal detected" in output
    assert "sensitive spec" not in output
