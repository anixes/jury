"""
triad.config - Hierarchical Configuration Resolution for Triad CLI.

Resolution Priority (Highest to Lowest):
1. Command Line Arguments (--models, --api-key, --gateway-url, etc.)
2. System Environment Variables (TRIAD_*, GATE_*, OPENAI_*)
3. Local Project Configuration (.triad.env, .env in repo or working directory)
4. User Global Configuration (~/.triad/config.env or ~/.config/triad/config.env)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

try:
    from dotenv import dotenv_values
except ImportError:
    dotenv_values = None


@dataclass
class TriadConfig:
    api_key: str = ""
    gateway_url: str = "https://gateway.constellationgate.ai/v1"
    models: List[str] = field(default_factory=list)
    output_dir: Path = field(default_factory=lambda: Path("reports/triad"))
    min_score: float = 7.0
    strict: bool = False
    repo_dir: Path = field(default_factory=lambda: Path.cwd())

    @property
    def is_valid(self) -> bool:
        return bool(self.api_key and self.models)


def get_global_config_path() -> Path:
    """Return standard path for global Triad configuration in user home."""
    try:
        home = Path.home()
    except Exception:
        home_str = os.environ.get("USERPROFILE") or os.environ.get("HOME") or os.environ.get("HOMEPATH") or "."
        home = Path(home_str)

    preferred = home / ".triad" / "config.env"
    try:
        if preferred.exists():
            return preferred
        xdg_config = home / ".config" / "triad" / "config.env"
        if xdg_config.exists():
            return xdg_config
    except Exception:
        pass
    return preferred


def find_project_env_file(start_dir: Optional[Path] = None) -> Optional[Path]:
    """Search for .triad.env or .env starting from start_dir up to git root."""
    current = (start_dir or Path.cwd()).resolve()

    while True:
        triad_env = current / ".triad.env"
        if triad_env.is_file():
            return triad_env
        dot_env = current / ".env"
        if dot_env.is_file():
            return dot_env

        if (current / ".git").exists() or current.parent == current:
            break
        current = current.parent

    return None


def load_env_dict(path: Optional[Path]) -> dict:
    """Safely load key-value pairs from an env file without polluting os.environ."""
    if not path or not path.is_file() or dotenv_values is None:
        return {}
    try:
        return {k: v for k, v in dotenv_values(path).items() if v is not None}
    except Exception:
        return {}


def resolve_config(
    cli_models: Optional[str] = None,
    cli_api_key: Optional[str] = None,
    cli_gateway_url: Optional[str] = None,
    cli_output_dir: Optional[str] = None,
    cli_min_score: Optional[float] = None,
    cli_strict: bool = False,
    cli_repo: Optional[str] = None,
) -> TriadConfig:
    """Resolve full TriadConfig combining global, local, env, and CLI sources."""
    repo_path = Path(cli_repo).resolve() if cli_repo else Path.cwd().resolve()

    # 1. Global config (~/.triad/config.env)
    global_env = load_env_dict(get_global_config_path())

    # 2. Local project config (.triad.env or .env)
    local_env_path = find_project_env_file(repo_path)
    local_env = load_env_dict(local_env_path)

    # Helper to look up key across sources in priority order: CLI -> os.environ -> Local -> Global
    def _get_val(key_names: List[str], cli_val: Optional[Any] = None) -> Optional[str]:
        if cli_val is not None and str(cli_val).strip():
            return str(cli_val).strip()
        for k in key_names:
            val = os.getenv(k)
            if val and val.strip():
                return val.strip()
        for k in key_names:
            if k in local_env and local_env[k]:
                return local_env[k].strip()
        for k in key_names:
            if k in global_env and global_env[k]:
                return global_env[k].strip()
        return None

    api_key = _get_val(["TRIAD_API_KEY", "GATE_API_KEY", "OPENAI_API_KEY", "GATEWAY_KEY"], cli_api_key) or ""
    gateway_url = (
        _get_val(["TRIAD_GATEWAY_URL", "GATE_API_BASE", "OPENAI_BASE_URL"], cli_gateway_url)
        or "https://gateway.constellationgate.ai/v1"
    )
    models_raw = _get_val(["TRIAD_MODELS", "GATE_MODELS"], cli_models) or ""
    models = [m.strip() for m in models_raw.split(",") if m.strip()]

    output_dir_str = _get_val(["TRIAD_OUTPUT_DIR"], cli_output_dir) or "reports/triad"
    output_dir = Path(output_dir_str)
    if not output_dir.is_absolute():
        output_dir = repo_path / output_dir

    min_score = float(cli_min_score) if cli_min_score is not None else 7.0

    return TriadConfig(
        api_key=api_key,
        gateway_url=gateway_url,
        models=models,
        output_dir=output_dir,
        min_score=min_score,
        strict=cli_strict,
        repo_dir=repo_path,
    )


def init_config_file(target_path: Path, is_global: bool = False) -> Path:
    """Create a starter Triad configuration file."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    template = """# Triad Multi-Model CLI Configuration
# ==========================================
# This file configures API access and model endpoints for Triad.

# Gateway Base URL (OpenAI-compatible)
TRIAD_GATEWAY_URL=https://gateway.constellationgate.ai/v1

# API Key for gateway
TRIAD_API_KEY=your-api-key-here

# Comma-separated list of models to run consultations and reviews across
# Example: provider/model-a,provider/model-b
TRIAD_MODELS=provider/model-1,provider/model-2

# Default output directory for reports
TRIAD_OUTPUT_DIR=reports/triad
"""
    target_path.write_text(template, encoding="utf-8")
    return target_path
