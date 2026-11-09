"""
tests/unit/test_packaging.py - Unit tests for packaging, imports, entry points, and configuration.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
import pytest

import jury
from jury.config import (
    JuryConfig,
    ModelConfig,
    ProviderConfig,
    clear_config_cache,
    get_config,
    resolve_credentials,
    resolve_state_dir,
)
from jury.security import (
    ContractExecutionResult,
    TypedVerificationContract,
    scrub_text,
)


def test_package_version_and_exports():
    """Verify package version and top-level exported symbols."""
    assert jury.__version__ == "3.0.0"
    assert hasattr(jury, "get_config")
    assert hasattr(jury, "resolve_credentials")
    assert hasattr(jury, "resolve_state_dir")
    assert hasattr(jury, "resolve_config_path")
    assert hasattr(jury, "load_yaml_config")
    assert hasattr(jury, "clear_config_cache")
    assert hasattr(jury, "TypedVerificationContract")
    assert hasattr(jury, "ContractExecutionResult")
    assert hasattr(jury, "scrub_text")


def test_pyproject_toml_configuration():
    """Verify pyproject.toml defines required dependencies and console script entry points."""
    pyproject_path = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    assert pyproject_path.is_file(), f"pyproject.toml not found at {pyproject_path}"

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    project = data.get("project", {})
    assert project.get("name") == "jury-mcp"
    assert project.get("version") == "3.0.0"

    # Check console scripts
    scripts = project.get("scripts", {})
    assert scripts.get("jury") == "jury.cli.main:main"
    assert scripts.get("jury-mcp") == "jury.mcp_server.server:main"
    assert scripts.get("triad") == "triad.cli:main"

    # Check dependencies
    deps = project.get("dependencies", [])
    for expected in [
        "pydantic",
        "httpx",
        "filelock",
        "rich",
        "python-dotenv",
        "pyyaml",
        "click",
        "mcp",
    ]:
        assert any(expected in d for d in deps), f"Expected dependency '{expected}' missing from pyproject.toml"


def test_state_dir_resolution_default(monkeypatch, tmp_path):
    """Verify state directory defaults to ~/.jury when no override is provided."""
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("JURY_STATE_DIR", raising=False)

    default_dir = resolve_state_dir()
    assert default_dir == (fake_home / ".jury").resolve()


def test_state_dir_resolution_env_override(monkeypatch, tmp_path):
    """Verify state directory respects JURY_STATE_DIR override."""
    override = tmp_path / "custom_jury_dir"
    monkeypatch.setenv("JURY_STATE_DIR", str(override))
    resolved = resolve_state_dir()
    assert resolved == override.resolve()


def test_state_dir_resolution_explicit_param(tmp_path):
    """Verify state directory respects explicit function argument."""
    override = tmp_path / "explicit_dir"
    resolved = resolve_state_dir(override=override)
    assert resolved == override.resolve()


def test_credentials_hierarchy_cli_precedence(monkeypatch):
    """Verify CLI argument takes highest precedence over environment variables."""
    monkeypatch.setenv("TRIAD_API_KEY", "env-key-should-be-ignored")
    cred = resolve_credentials(cli_key="sk-cli-explicit-key", provider="constellation")
    assert cred["api_key"] == "sk-cli-explicit-key"
    assert cred["source"] == "cli"


def test_credentials_hierarchy_env_vars(monkeypatch):
    """Verify environment variables are resolved when no CLI key is given."""
    monkeypatch.setenv("CONSTELLATION_API_KEY", "constellation-env-key-456")
    cred = resolve_credentials(provider="constellation")
    assert cred["api_key"] == "constellation-env-key-456"
    assert cred["source"].startswith("env:")


def test_credentials_hierarchy_global_config(monkeypatch, tmp_path):
    """Verify resolution from ~/.triad/config.env when CLI and ENV are unset."""
    for var in ["TRIAD_API_KEY", "CONSTELLATION_API_KEY", "OXION_API_KEY", "GATE_API_KEY", "GATEWAY_KEY", "JURY_API_KEY"]:
        monkeypatch.delenv(var, raising=False)

    fake_home = tmp_path / "fake_home"
    triad_dir = fake_home / ".triad"
    triad_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = triad_dir / "config.env"
    cfg_file.write_text("TRIAD_API_KEY=global-triad-key-123\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    cred = resolve_credentials(provider="constellation")
    assert cred["api_key"] == "global-triad-key-123"
    assert cred["source"] == f"global:{cfg_file}"


def test_credentials_hierarchy_workspace_env(monkeypatch, tmp_path):
    """Verify fallback to workspace .env file when CLI, ENV, and global config are unset."""
    for var in ["TRIAD_API_KEY", "CONSTELLATION_API_KEY", "OXION_API_KEY", "GATE_API_KEY", "GATEWAY_KEY", "JURY_API_KEY"]:
        monkeypatch.delenv(var, raising=False)

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    env_file = tmp_path / ".env"
    env_file.write_text("TRIAD_API_KEY=workspace-test-key-999\n", encoding="utf-8")

    cred = resolve_credentials(provider="constellation", workspace_root=tmp_path)
    assert cred["api_key"] == "workspace-test-key-999"
    assert cred["source"] == f"workspace:{env_file}"


def test_get_config_model_topology(monkeypatch, tmp_path):
    """Verify full JuryConfig model topology and provider configurations."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("JURY_CONFIG_PATH", raising=False)

    clear_config_cache()
    cfg = get_config(state_dir_override=tmp_path, reload=True)

    assert isinstance(cfg, JuryConfig)
    assert cfg.state_dir == tmp_path.resolve()
    assert cfg.db_path == tmp_path.resolve() / "state.db"
    assert cfg.lock_path == tmp_path.resolve() / "state.db.lock"
    assert cfg.quorum_min == 2

    # Check panel model mappings
    assert "alpha" in cfg.models
    assert cfg.models["alpha"].model_id == "deepseek/deepseek-chat"
    assert cfg.models["alpha"].role == "architect"
    assert cfg.models["alpha"].max_diff_tokens == 16000

    assert "beta" in cfg.models
    assert cfg.models["beta"].model_id == "llama-3.3-70b-versatile"
    assert cfg.models["beta"].role == "adversary"

    assert "gamma" in cfg.models
    assert cfg.models["gamma"].model_id == "qwen/qwen-2.5-coder-32b-instruct"
    assert cfg.models["gamma"].max_diff_tokens == 16000

    assert "arbiter" in cfg.models
    assert cfg.models["arbiter"].role == "arbiter"

    # Check providers
    assert "constellation" in cfg.providers
    assert "groq" in cfg.providers
    assert "openrouter" in cfg.providers
