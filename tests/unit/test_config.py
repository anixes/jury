"""
tests/unit/test_config.py - Comprehensive Unit Tests for Dynamic YAML & Env Config.

Verifies:
1. Dynamic loading of model configurations from .jury/config.yaml in workspace root and state directory
2. Precedence and loading via JURY_CONFIG_PATH environment variable
3. Direct environment variable model overrides:
   - JURY_ALPHA_MODEL, JURY_BETA_MODEL, JURY_GAMMA_MODEL, JURY_ARBITER_MODEL
   - JURY_ALPHA_PROVIDER, JURY_BETA_PROVIDER, JURY_GAMMA_PROVIDER, JURY_ARBITER_PROVIDER
4. Clean fallback to built-in defaults when no YAML file or env overrides exist
5. YAML provider parsing and custom upstream endpoints
6. Precedence hierarchy (Env > YAML > Defaults)
7. Error resilience on corrupt or missing YAML configuration
8. CLI integration with --config flag
"""

from __future__ import annotations

import os
from pathlib import Path
import pytest
from click.testing import CliRunner

from jury.cli.main import cli
from jury.config import (
    DEFAULT_CONSTELLATION_GATEWAY,
    DEFAULT_GROQ_GATEWAY,
    DEFAULT_OPENROUTER_GATEWAY,
    JuryConfig,
    ModelConfig,
    ProviderConfig,
    clear_config_cache,
    get_config,
    load_yaml_config,
    resolve_config_path,
)


ENV_KEYS_TO_CLEAN = [
    "JURY_CONFIG_PATH",
    "JURY_STATE_DIR",
    "JURY_ALPHA_MODEL",
    "JURY_BETA_MODEL",
    "JURY_GAMMA_MODEL",
    "JURY_ARBITER_MODEL",
    "JURY_ALPHA_PROVIDER",
    "JURY_BETA_PROVIDER",
    "JURY_GAMMA_PROVIDER",
    "JURY_ARBITER_PROVIDER",
]


@pytest.fixture(autouse=True)
def clean_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Ensure clean configuration state for each test."""
    clear_config_cache()
    for env_k in ENV_KEYS_TO_CLEAN:
        monkeypatch.delenv(env_k, raising=False)
        os.environ.pop(env_k, None)

    state_dir = tmp_path / "jury_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JURY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("JURY_OFFLINE_TEST", "1")
    try:
        yield
    finally:
        clear_config_cache()
        for env_k in ENV_KEYS_TO_CLEAN:
            monkeypatch.delenv(env_k, raising=False)
            os.environ.pop(env_k, None)


def test_fallback_to_defaults_when_no_config_exists(tmp_path: Path):
    """Verify get_config returns built-in defaults when no YAML or env overrides exist."""
    cfg = get_config(reload=True)

    assert isinstance(cfg, JuryConfig)
    # Built-in model defaults
    assert cfg.models["alpha"].model_id == "constellation/glm-5-3-flash-free"
    assert cfg.models["alpha"].provider == "constellation"
    assert cfg.models["alpha"].role == "architect"
    assert cfg.models["alpha"].max_diff_tokens == 14000

    assert cfg.models["beta"].model_id == "constellation/qwen3-8-flash-free"
    assert cfg.models["beta"].provider == "constellation"
    assert cfg.models["beta"].role == "adversary"

    assert cfg.models["gamma"].model_id == "constellation/glm-5-3-flash-free"
    assert cfg.models["gamma"].provider == "constellation"
    assert cfg.models["gamma"].role == "minimalist"

    assert cfg.models["arbiter"].model_id == "constellation/qwen3-8-flash-free"
    assert cfg.models["arbiter"].provider == "constellation"
    assert cfg.models["arbiter"].role == "arbiter"
    assert cfg.strict_arbiter_decoupling is False

    # Default providers
    assert "constellation" in cfg.providers
    assert "groq" in cfg.providers
    assert "openrouter" in cfg.providers
    assert "deepseek" in cfg.providers
    assert "cerebras" in cfg.providers
    assert cfg.quorum_min == 2


def test_loading_models_from_workspace_config_yaml(tmp_path: Path):
    """Verify get_config dynamically loads and overrides models from .jury/config.yaml."""
    ws = tmp_path / "workspace"
    jury_dir = ws / ".jury"
    jury_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = jury_dir / "config.yaml"

    yaml_content = """# Custom workspace configuration
version: "3.0.0"
models:
  gamma:
    model_id: "cerebras/llama-3.3-70b"
    provider: "cerebras"
    max_diff_tokens: 8192
    description: "Cerebras Ultra-Fast LPU Inference"
  arbiter:
    model_id: "deepseek/deepseek-r1"
    provider: "deepseek"
    role: "arbiter"
    max_diff_tokens: 20000
quorum_min: 3
"""
    cfg_file.write_text(yaml_content, encoding="utf-8")

    cfg = get_config(workspace_root=ws, reload=True)

    # Gamma and Arbiter should be loaded from YAML
    assert cfg.models["gamma"].model_id == "cerebras/llama-3.3-70b"
    assert cfg.models["gamma"].provider == "cerebras"
    assert cfg.models["gamma"].max_diff_tokens == 8192
    assert cfg.models["gamma"].role == "minimalist"  # Preserved from defaults

    assert cfg.models["arbiter"].model_id == "deepseek/deepseek-r1"
    assert cfg.models["arbiter"].provider == "deepseek"
    assert cfg.models["arbiter"].max_diff_tokens == 20000

    # Alpha and Beta should retain built-in defaults
    assert cfg.models["alpha"].model_id == "constellation/glm-5-3-flash-free"
    assert cfg.models["beta"].model_id == "constellation/qwen3-8-flash-free"

    # Scalar setting from YAML
    assert cfg.quorum_min == 3


def test_loading_via_jury_config_path_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify JURY_CONFIG_PATH environment variable directs get_config to custom YAML."""
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    custom_yaml = custom_dir / "my_team_config.yaml"

    yaml_content = """version: "3.0.0"
models:
  alpha:
    model_id: "anthropic/claude-3-7-sonnet"
    provider: "openrouter"
    role: "architect"
    max_diff_tokens: 16000
  arbiter:
    model_id: "openai/o3-mini"
    provider: "openrouter"
"""
    custom_yaml.write_text(yaml_content, encoding="utf-8")
    monkeypatch.setenv("JURY_CONFIG_PATH", str(custom_yaml))

    cfg = get_config(reload=True)

    assert cfg.models["alpha"].model_id == "anthropic/claude-3-7-sonnet"
    assert cfg.models["alpha"].provider == "openrouter"
    assert cfg.models["arbiter"].model_id == "openai/o3-mini"
    assert cfg.models["arbiter"].provider == "openrouter"
    # Unspecified models remain defaults
    assert cfg.models["gamma"].model_id == "constellation/glm-5-3-flash-free"


def test_direct_env_var_model_and_provider_overrides(monkeypatch: pytest.MonkeyPatch):
    """Verify JURY_<ROLE>_MODEL and JURY_<ROLE>_PROVIDER overrides."""
    monkeypatch.setenv("JURY_ALPHA_MODEL", "custom-alpha-model-v1")
    monkeypatch.setenv("JURY_ALPHA_PROVIDER", "groq")
    monkeypatch.setenv("JURY_BETA_MODEL", "custom-beta-model-v2")
    monkeypatch.setenv("JURY_BETA_PROVIDER", "cerebras")
    monkeypatch.setenv("JURY_GAMMA_MODEL", "custom-gamma-model-v3")
    monkeypatch.setenv("JURY_GAMMA_PROVIDER", "constellation")
    monkeypatch.setenv("JURY_ARBITER_MODEL", "custom-arbiter-model-v4")
    monkeypatch.setenv("JURY_ARBITER_PROVIDER", "deepseek")

    cfg = get_config(reload=True)

    assert cfg.models["alpha"].model_id == "custom-alpha-model-v1"
    assert cfg.models["alpha"].provider == "groq"

    assert cfg.models["beta"].model_id == "custom-beta-model-v2"
    assert cfg.models["beta"].provider == "cerebras"

    assert cfg.models["gamma"].model_id == "custom-gamma-model-v3"
    assert cfg.models["gamma"].provider == "constellation"

    assert cfg.models["arbiter"].model_id == "custom-arbiter-model-v4"
    assert cfg.models["arbiter"].provider == "deepseek"


def test_precedence_env_overrides_beat_yaml_and_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify strict precedence: Env Overrides > YAML config > Defaults."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """models:
  gamma:
    model_id: "yaml-gamma-model"
    provider: "groq"
  arbiter:
    model_id: "yaml-arbiter-model"
    provider: "openrouter"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("JURY_CONFIG_PATH", str(cfg_file))

    # Env override for gamma model only
    monkeypatch.setenv("JURY_GAMMA_MODEL", "env-winning-gamma-model")

    cfg = get_config(reload=True)

    # Gamma model_id comes from env (beats YAML)
    assert cfg.models["gamma"].model_id == "env-winning-gamma-model"
    assert cfg.models["gamma"].provider == "groq"

    # Arbiter comes from YAML (beats default)
    assert cfg.models["arbiter"].model_id == "yaml-arbiter-model"

    # Alpha remains default
    assert cfg.models["alpha"].model_id == "constellation/glm-5-3-flash-free"


def test_custom_providers_in_yaml(tmp_path: Path):
    """Verify custom upstream providers defined in YAML are added to config.providers."""
    ws = tmp_path / "workspace"
    jury_dir = ws / ".jury"
    jury_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = jury_dir / "config.yaml"

    yaml_content = """version: "3.0.0"
providers:
  local_ollama:
    base_url: "http://localhost:11434/v1"
    api_key: "ollama-key"
    default_headers:
      X-Custom-Env: "dev"
models:
  gamma:
    model_id: "qwen2.5-coder:32b"
    provider: "local_ollama"
"""
    cfg_file.write_text(yaml_content, encoding="utf-8")

    cfg = get_config(workspace_root=ws, reload=True)

    assert "local_ollama" in cfg.providers
    assert cfg.providers["local_ollama"].base_url == "http://localhost:11434/v1"
    assert cfg.providers["local_ollama"].api_key == "ollama-key"
    assert cfg.providers["local_ollama"].default_headers.get("X-Custom-Env") == "dev"
    assert cfg.models["gamma"].provider == "local_ollama"
    assert cfg.models["gamma"].model_id == "qwen2.5-coder:32b"


def test_corrupt_yaml_graceful_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify invalid/corrupt YAML syntax falls back gracefully to defaults without crash."""
    corrupt_file = tmp_path / "corrupt.yaml"
    corrupt_file.write_text("models: [broken yaml ::: \n  - unexpected", encoding="utf-8")
    monkeypatch.setenv("JURY_CONFIG_PATH", str(corrupt_file))

    cfg = get_config(reload=True)

    # Should safely return default model configuration
    assert cfg.models["alpha"].model_id == "constellation/glm-5-3-flash-free"
    assert cfg.models["gamma"].model_id == "constellation/glm-5-3-flash-free"
    assert cfg.models["arbiter"].model_id == "constellation/qwen3-8-flash-free"


def test_cli_config_flag_invokes_custom_configuration(tmp_path: Path):
    """Verify `jury --config custom.yaml test --json` parses and respects the passed config."""
    custom_yaml = tmp_path / "cli_test_config.yaml"
    custom_yaml.write_text(
        """version: "3.0.0"
models:
  alpha:
    model_id: "constellation/glm-5-3-flash-free"
    provider: "constellation"
  beta:
    model_id: "constellation/qwen3-8-flash-free"
    provider: "constellation"
  gamma:
    model_id: "groq-custom-speed"
    provider: "groq"
  arbiter:
    model_id: "deepseek/deepseek-chat"
    provider: "openrouter"
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["--config", str(custom_yaml), "test", "--json"])
    assert result.exit_code == 0
    # Also verify get_config reflects JURY_CONFIG_PATH set by CLI
    cfg = get_config()
    assert cfg.models["gamma"].model_id == "groq-custom-speed"


def test_cli_config_flag_does_not_hijack_state_dir(tmp_path: Path):
    """Verify `jury --config custom.yaml` does not clobber an existing JURY_STATE_DIR."""
    orig_state = tmp_path / "original_state_dir"
    orig_state.mkdir(parents=True, exist_ok=True)
    os.environ["JURY_STATE_DIR"] = str(orig_state)

    custom_yaml = tmp_path / "other_dir" / "cli_test_config.yaml"
    custom_yaml.parent.mkdir(parents=True, exist_ok=True)
    custom_yaml.write_text("version: '3.0.0'\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["--config", str(custom_yaml), "test", "--json"])
    assert result.exit_code == 0
    # JURY_STATE_DIR must not be hijacked to other_dir
    assert os.environ.get("JURY_STATE_DIR") == str(orig_state)


def test_loading_models_from_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify get_config dynamically loads models from state directory config.yaml."""
    state_dir = tmp_path / "isolated_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = state_dir / "config.yaml"
    cfg_file.write_text(
        """models:
  alpha:
    model_id: "model-from-state-dir"
    provider: "constellation"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("JURY_STATE_DIR", str(state_dir))

    cfg = get_config(reload=True)
    assert cfg.models["alpha"].model_id == "model-from-state-dir"
    # Unspecified models remain defaults
    assert cfg.models["beta"].model_id == "constellation/qwen3-8-flash-free"


def test_resolve_config_path_default_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify resolve_config_path() with no arguments resolves config in state_dir."""
    state_dir = tmp_path / "default_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = state_dir / "config.yaml"
    cfg_file.write_text("version: '3.0.0'\n", encoding="utf-8")
    monkeypatch.setenv("JURY_STATE_DIR", str(state_dir))

    found = resolve_config_path()
    assert found == cfg_file


def test_automatic_cache_invalidation_on_env_change(monkeypatch: pytest.MonkeyPatch):
    """Verify get_config() automatically detects environment variable changes without explicit reload."""
    c1 = get_config()
    assert c1.models["alpha"].model_id == "constellation/glm-5-3-flash-free"

    # Mutate environment override
    monkeypatch.setenv("JURY_ALPHA_MODEL", "dynamically-updated-alpha")
    c2 = get_config()
    assert c2.models["alpha"].model_id == "dynamically-updated-alpha"


def test_yaml_env_var_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify ${VAR} and ${VAR:-default} placeholders expand inside YAML config."""
    monkeypatch.setenv("EXPANDED_API_KEY", "secret-test-token-123")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """providers:
  custom_gw:
    base_url: "${CUSTOM_GW_URL:-https://gateway.example.com/v1}"
    api_key: "${EXPANDED_API_KEY}"
models:
  gamma:
    model_id: "test-model"
    provider: "custom_gw"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("JURY_CONFIG_PATH", str(cfg_file))

    cfg = get_config(reload=True)
    assert "custom_gw" in cfg.providers
    assert cfg.providers["custom_gw"].base_url == "https://gateway.example.com/v1"
    assert cfg.providers["custom_gw"].api_key == "secret-test-token-123"


def test_yaml_string_shorthand_and_none_resilience(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify shorthand model declarations and resilience to null YAML fields."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """models:
  alpha: "shorthand-alpha-model-id"
  gamma:
    model_id: "gamma-null-test"
    description: null
    max_diff_tokens: null
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("JURY_CONFIG_PATH", str(cfg_file))

    cfg = get_config(reload=True)
    assert cfg.models["alpha"].model_id == "shorthand-alpha-model-id"
    assert cfg.models["alpha"].provider == "constellation"  # Default preserved

    assert cfg.models["gamma"].model_id == "gamma-null-test"
    assert cfg.models["gamma"].max_diff_tokens == 14000  # Default preserved despite null in YAML


def test_strict_arbiter_decoupling_toggle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify strict_arbiter_decoupling toggle via YAML and environment variable."""
    # 1. Default is False
    cfg = get_config(reload=True)
    assert cfg.strict_arbiter_decoupling is False

    # 2. YAML sets True
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text('strict_arbiter_decoupling: true\n', encoding="utf-8")
    monkeypatch.setenv("JURY_CONFIG_PATH", str(cfg_file))
    cfg = get_config(reload=True)
    assert cfg.strict_arbiter_decoupling is True

    # 3. Env var overrides YAML to False
    monkeypatch.setenv("JURY_STRICT_DECOUPLING", "0")
    cfg = get_config(reload=True)
    assert cfg.strict_arbiter_decoupling is False


def test_semantic_role_name_env_override(monkeypatch: pytest.MonkeyPatch):
    """Verify JURY_<SEMANTIC_ROLE>_MODEL (e.g. JURY_ARCHITECT_MODEL) overrides the matching model."""
    monkeypatch.setenv("JURY_ARCHITECT_MODEL", "custom-architect-id")
    cfg = get_config(reload=True)
    assert cfg.models["alpha"].model_id == "custom-architect-id"
