"""
src/jury/config.py - Jury Mesh v3.0 Global Configuration & Credentials Hierarchy.

Manages state directory resolution (~/.jury with JURY_STATE_DIR override),
hierarchical credentials cascade (CLI -> ENV -> ~/.triad/config.env -> .env),
and multi-model provider topologies.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union
from pydantic import BaseModel, Field


DEFAULT_STATE_DIR_NAME = ".jury"
DEFAULT_CONFIG_FILE_NAME = "config.yaml"
DEFAULT_CONSTELLATION_GATEWAY = "https://gateway.constellationgate.ai/v1"
DEFAULT_GROQ_GATEWAY = "https://api.groq.com/openai/v1"
DEFAULT_OPENROUTER_GATEWAY = "https://openrouter.ai/api/v1"
DEFAULT_DEEPSEEK_GATEWAY = "https://api.deepseek.com/v1"
DEFAULT_CEREBRAS_GATEWAY = "https://api.cerebras.ai/v1"


class ModelConfig(BaseModel):
    """Configuration for an individual deliberative model role."""
    model_id: str
    provider: str
    role: str
    max_diff_tokens: int
    rpm_limit: int = 30
    tpm_limit: int = 120000
    timeout_seconds: float = 60.0
    description: str = ""


class ProviderConfig(BaseModel):
    """Configuration for an upstream LLM gateway provider."""
    name: str
    base_url: str
    api_key: Optional[str] = None
    default_headers: Dict[str, str] = Field(default_factory=dict)


class JuryConfig(BaseModel):
    """Global configuration for the Jury Mesh engine."""
    version: str = "3.0.0"
    state_dir: Path
    db_path: Path
    lock_path: Path
    log_dir: Path
    providers: Dict[str, ProviderConfig] = Field(default_factory=dict)
    models: Dict[str, ModelConfig] = Field(default_factory=dict)
    quorum_min: int = 2
    strict_arbiter_decoupling: bool = False
    request_timeout_seconds: float = 60.0
    contract_timeout_seconds: int = 30
    isolated_network: bool = True

    model_config = {
        "arbitrary_types_allowed": True
    }


def resolve_state_dir(override: Optional[Union[str, Path]] = None) -> Path:
    """
    Resolve the Jury state directory with environment variable precedence:
    1. Explicit function override
    2. JURY_STATE_DIR environment variable
    3. Default ~/.jury directory
    """
    if override:
        path = Path(override).expanduser().resolve()
    elif os.getenv("JURY_STATE_DIR"):
        path = Path(os.environ["JURY_STATE_DIR"]).expanduser().resolve()
    else:
        path = (Path.home() / DEFAULT_STATE_DIR_NAME).resolve()
    return path


def resolve_config_path(
    override: Optional[Union[str, Path]] = None,
    workspace_root: Optional[Union[str, Path]] = None,
    state_dir: Optional[Union[str, Path]] = None,
) -> Optional[Path]:
    """
    Dynamically resolve YAML configuration file path according to hierarchy:
    1. Explicit function override (if file exists)
    2. JURY_CONFIG_PATH environment variable (set by --config in CLI)
    3. Active workspace root (.jury/config.yaml or config.yaml)
    4. Current working directory and parent directories (.jury/config.yaml)
    5. Resolved state directory (state_dir/config.yaml or state_dir/.jury/config.yaml,
       or default state_dir ~/.jury)
    """
    # 1. Explicit override
    if override:
        p = Path(override).expanduser().resolve()
        if p.is_file():
            return p

    # 2. JURY_CONFIG_PATH environment variable
    env_path = os.getenv("JURY_CONFIG_PATH")
    if env_path and env_path.strip():
        p = Path(env_path.strip()).expanduser().resolve()
        if p.is_file():
            return p

    # 3. Active workspace root
    if workspace_root:
        ws = Path(workspace_root).resolve()
        candidate = ws / DEFAULT_STATE_DIR_NAME / DEFAULT_CONFIG_FILE_NAME
        if candidate.is_file():
            return candidate
        candidate_direct = ws / DEFAULT_CONFIG_FILE_NAME
        if candidate_direct.is_file():
            return candidate_direct

    # 4. Current working directory and repository ancestors
    try:
        curr = Path.cwd().resolve()
        candidate = curr / DEFAULT_STATE_DIR_NAME / DEFAULT_CONFIG_FILE_NAME
        if candidate.is_file():
            return candidate
        for parent in curr.parents:
            candidate = parent / DEFAULT_STATE_DIR_NAME / DEFAULT_CONFIG_FILE_NAME
            if candidate.is_file():
                return candidate
    except Exception:
        pass

    # 5. Resolved state directory (explicit parameter or default resolve_state_dir())
    sd = Path(state_dir).resolve() if state_dir else resolve_state_dir()
    candidate = sd / DEFAULT_CONFIG_FILE_NAME
    if candidate.is_file():
        return candidate
    candidate_nested = sd / DEFAULT_STATE_DIR_NAME / DEFAULT_CONFIG_FILE_NAME
    if candidate_nested.is_file():
        return candidate_nested

    return None


def _expand_env_vars(data: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} placeholders in parsed YAML data."""
    if isinstance(data, str):
        def _sub(match: re.Match) -> str:
            expr = match.group(1)
            if ":-" in expr:
                k, default_val = expr.split(":-", 1)
                return os.getenv(k.strip(), default_val)
            return os.getenv(expr.strip(), "")
        return re.sub(r"\$\{([^}]+)\}", _sub, data)
    elif isinstance(data, dict):
        return {k: _expand_env_vars(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_expand_env_vars(v) for v in data]
    return data


def load_yaml_config(path: Optional[Union[str, Path]]) -> Dict[str, Any]:
    """
    Safely parse a YAML configuration file.
    Returns an empty dictionary on missing file, corrupt syntax, or missing PyYAML.
    Expands ${VAR} environment variable placeholders.
    """
    if not path:
        return {}
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return {}

    try:
        import yaml
    except ImportError:
        return {}

    try:
        content = p.read_text(encoding="utf-8", errors="ignore")
        if not content.strip():
            return {}
        data = yaml.safe_load(content)
        if isinstance(data, dict):
            return _expand_env_vars(data)
    except Exception:
        pass
    return {}


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a KEY=VALUE environment file safely without third-party side effects."""
    env_vars: Dict[str, str] = {}
    try:
        if not path.is_file():
            return env_vars
        content = path.read_text(encoding="utf-8", errors="ignore")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    env_vars[key] = val
    except (OSError, PermissionError):
        # Gracefully handle unreadable or locked files
        pass
    return env_vars


def resolve_credentials(
    cli_key: Optional[str] = None,
    provider: str = "constellation",
    workspace_root: Optional[Union[str, Path]] = None,
) -> Dict[str, str]:
    """
    Resolve credentials according to the strict 4-level hierarchy:
    1. CLI Argument (--api-key)
    2. Environment Variables (e.g. TRIAD_API_KEY, CONSTELLATION_API_KEY, GROQ_API_KEY)
    3. Global Config (~/.triad/config.env or ~/.jury/config.env)
    4. Local Workspace .env (.env, .triad.env, .jury.env)
    
    Returns a dictionary with 'api_key' and 'gateway_url'.
    """
    prov = provider.lower()
    root = Path(workspace_root).resolve() if workspace_root else Path.cwd()

    # 1. Check CLI Argument
    if cli_key and cli_key.strip():
        return {
            "api_key": cli_key.strip(),
            "gateway_url": _resolve_gateway_url(prov),
            "source": "cli"
        }

    # 2. Check Environment Variables
    env_key_candidates = {
        "constellation": ["TRIAD_API_KEY", "CONSTELLATION_API_KEY", "OXION_API_KEY", "GATE_API_KEY", "GATEWAY_KEY", "JURY_API_KEY"],
        "groq": ["GROQ_API_KEY"],
        "openrouter": ["OPENROUTER_API_KEY"],
        "deepseek": ["DEEPSEEK_API_KEY"],
        "cerebras": ["CEREBRAS_API_KEY"],
    }.get(prov, [f"{prov.upper()}_API_KEY", "JURY_API_KEY"])

    for env_var in env_key_candidates:
        val = os.getenv(env_var)
        if val and val.strip():
            return {
                "api_key": val.strip(),
                "gateway_url": _resolve_gateway_url(prov),
                "source": f"env:{env_var}"
            }

    # 3. Check Global Config (~/.triad/config.env, ~/.jury/config.env)
    try:
        home_path = Path.home()
    except Exception:
        home_path = Path("~").expanduser()

    global_candidates = [
        home_path / ".triad" / "config.env",
        home_path / ".jury" / "config.env",
        home_path / ".triad" / ".env",
    ]
    for gpath in global_candidates:
        try:
            if gpath.is_file():
                parsed = parse_env_file(gpath)
                for env_var in env_key_candidates:
                    if env_var in parsed and parsed[env_var].strip():
                        gw = _extract_gateway_url(parsed, prov) or _resolve_gateway_url(prov)
                        return {
                            "api_key": parsed[env_var].strip(),
                            "gateway_url": gw,
                            "source": f"global:{gpath}"
                        }
        except (OSError, PermissionError):
            continue

    # 4. Check Workspace .env files
    workspace_candidates = [
        root / ".env",
        root / ".triad.env",
        root / ".jury.env",
    ]
    for wpath in workspace_candidates:
        try:
            if wpath.is_file():
                parsed = parse_env_file(wpath)
                for env_var in env_key_candidates:
                    if env_var in parsed and parsed[env_var].strip():
                        gw = _extract_gateway_url(parsed, prov) or _resolve_gateway_url(prov)
                        return {
                            "api_key": parsed[env_var].strip(),
                            "gateway_url": gw,
                            "source": f"workspace:{wpath}"
                        }
        except (OSError, PermissionError):
            continue

    # Fallback when no credential found
    return {
        "api_key": "",
        "gateway_url": _resolve_gateway_url(prov),
        "source": "none"
    }


def _resolve_gateway_url(provider: str) -> str:
    prov = provider.lower()
    if prov in ("constellation", "triad"):
        return (
            os.getenv("TRIAD_GATEWAY_URL")
            or os.getenv("CONSTELLATION_GATEWAY_URL")
            or os.getenv("OXION_GATEWAY_URL")
            or DEFAULT_CONSTELLATION_GATEWAY
        )
    elif prov == "groq":
        return os.getenv("GROQ_BASE_URL", DEFAULT_GROQ_GATEWAY)
    elif prov == "openrouter":
        return os.getenv("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_GATEWAY)
    elif prov == "deepseek":
        return os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_GATEWAY)
    elif prov == "cerebras":
        return os.getenv("CEREBRAS_BASE_URL", DEFAULT_CEREBRAS_GATEWAY)
    return os.getenv(f"{prov.upper()}_BASE_URL", "")


def _extract_gateway_url(env_dict: Dict[str, str], provider: str) -> Optional[str]:
    prov = provider.lower()
    if prov in ("constellation", "triad"):
        for k in ("TRIAD_GATEWAY_URL", "CONSTELLATION_GATEWAY_URL", "OXION_GATEWAY_URL"):
            if k in env_dict and env_dict[k].strip():
                return env_dict[k].strip()
    elif prov == "groq" and "GROQ_BASE_URL" in env_dict:
        return env_dict["GROQ_BASE_URL"].strip()
    elif prov == "openrouter" and "OPENROUTER_BASE_URL" in env_dict:
        return env_dict["OPENROUTER_BASE_URL"].strip()
    elif prov == "deepseek" and "DEEPSEEK_BASE_URL" in env_dict:
        return env_dict["DEEPSEEK_BASE_URL"].strip()
    return None


_CACHED_CONFIG: Optional[JuryConfig] = None
_CACHED_FINGERPRINT: Optional[tuple] = None


def clear_config_cache() -> None:
    """Clear cached singleton configuration (useful in testing)."""
    global _CACHED_CONFIG, _CACHED_FINGERPRINT
    _CACHED_CONFIG = None
    _CACHED_FINGERPRINT = None


def _compute_config_fingerprint(
    state_dir_override: Optional[Union[str, Path]],
    cli_keys: Optional[Dict[str, str]],
    config_path_override: Optional[Union[str, Path]],
    workspace_root: Optional[Union[str, Path]],
    cfg_file_path: Optional[Path],
) -> tuple:
    env_items = tuple(
        sorted(
            (k, v)
            for k, v in os.environ.items()
            if k.startswith("JURY_")
            or k.startswith("TRIAD_")
            or k.startswith("CONSTELLATION_")
            or k.endswith("_API_KEY")
            or k.endswith("_BASE_URL")
            or k.endswith("_GATEWAY_URL")
        )
    )
    mtime = 0
    if cfg_file_path and cfg_file_path.is_file():
        try:
            mtime = cfg_file_path.stat().st_mtime_ns
        except OSError:
            pass
    return (
        str(state_dir_override),
        tuple(sorted((cli_keys or {}).items())),
        str(config_path_override),
        str(workspace_root),
        str(cfg_file_path),
        mtime,
        env_items,
    )


def get_config(
    state_dir_override: Optional[Union[str, Path]] = None,
    cli_keys: Optional[Dict[str, str]] = None,
    reload: bool = False,
    config_path_override: Optional[Union[str, Path]] = None,
    workspace_root: Optional[Union[str, Path]] = None,
) -> JuryConfig:
    """
    Obtain the validated JuryConfig instance.
    Dynamically loads configuration from YAML file and environment variable overrides,
    falling back cleanly to built-in defaults.
    """
    state_dir = resolve_state_dir(state_dir_override)
    db_path = state_dir / "state.db"
    lock_path = state_dir / "state.db.lock"
    log_dir = state_dir / "logs"

    # 1. Resolve and parse YAML configuration
    cfg_file_path = resolve_config_path(
        override=config_path_override,
        workspace_root=workspace_root,
        state_dir=state_dir,
    )

    fingerprint = _compute_config_fingerprint(
        state_dir_override=state_dir_override,
        cli_keys=cli_keys,
        config_path_override=config_path_override,
        workspace_root=workspace_root,
        cfg_file_path=cfg_file_path,
    )

    global _CACHED_CONFIG, _CACHED_FINGERPRINT
    if _CACHED_CONFIG is not None and not reload and fingerprint == _CACHED_FINGERPRINT:
        return _CACHED_CONFIG
    yaml_data: Dict[str, Any] = load_yaml_config(cfg_file_path) if cfg_file_path else {}

    # 2. Resolve provider credentials
    cli_dict = cli_keys or {}
    known_providers = ["constellation", "groq", "openrouter", "deepseek", "cerebras"]

    # Initialize default providers
    providers: Dict[str, ProviderConfig] = {}
    for prov in known_providers:
        cred = resolve_credentials(cli_dict.get(prov), prov, workspace_root=workspace_root)
        headers: Dict[str, str] = {}
        if prov == "openrouter":
            headers = {
                "HTTP-Referer": "https://jury-mcp.dev",
                "X-Title": "Jury Mesh",
            }
        providers[prov] = ProviderConfig(
            name=prov,
            base_url=cred["gateway_url"],
            api_key=cred["api_key"] or None,
            default_headers=headers,
        )

    # Merge YAML providers if present
    yaml_providers = yaml_data.get("providers")
    if isinstance(yaml_providers, dict):
        for p_name, p_val in yaml_providers.items():
            if not isinstance(p_val, dict):
                continue
            p_key = p_name.lower().strip()
            if p_key in providers:
                if p_val.get("base_url"):
                    providers[p_key].base_url = str(p_val["base_url"]).strip()
                if p_val.get("api_key"):
                    providers[p_key].api_key = str(p_val["api_key"]).strip()
                if isinstance(p_val.get("default_headers"), dict):
                    providers[p_key].default_headers.update(p_val["default_headers"])
            else:
                cred = resolve_credentials(cli_dict.get(p_key), p_key, workspace_root=workspace_root)
                base_url = str(p_val.get("base_url") or cred["gateway_url"] or _resolve_gateway_url(p_key)).strip()
                api_key = p_val.get("api_key") or cred["api_key"] or None
                headers = p_val.get("default_headers") if isinstance(p_val.get("default_headers"), dict) else {}
                providers[p_key] = ProviderConfig(
                    name=p_key,
                    base_url=base_url,
                    api_key=api_key,
                    default_headers=headers,
                )

    # 3. Build Model Topologies (Default -> YAML -> Env Overrides)
    default_models: Dict[str, ModelConfig] = {
        "alpha": ModelConfig(
            model_id="constellation/glm-5-3-flash-free",
            provider="constellation",
            role="architect",
            max_diff_tokens=14000,
            rpm_limit=30,
            tpm_limit=120000,
            description="Systems Architect & Invariant Verifier (GLM-5.3 320B MoE)",
        ),
        "beta": ModelConfig(
            model_id="constellation/qwen3-8-flash-free",
            provider="constellation",
            role="adversary",
            max_diff_tokens=14000,
            rpm_limit=30,
            tpm_limit=120000,
            description="Adversary, Security & Race Condition Auditor (Qwen 3.8 180B MoE)",
        ),
        "gamma": ModelConfig(
            model_id="constellation/glm-5-3-flash-free",
            provider="constellation",
            role="minimalist",
            max_diff_tokens=14000,
            rpm_limit=30,
            tpm_limit=120000,
            description="Simplicity Minimalist & Anti-Bloat Advocate (GLM-5.3 320B MoE)",
        ),
        "arbiter": ModelConfig(
            model_id="constellation/qwen3-8-flash-free",
            provider="constellation",
            role="arbiter",
            max_diff_tokens=16000,
            rpm_limit=30,
            tpm_limit=120000,
            description="Judicial Arbiter & Consensus Synthesizer (Qwen 3.8 180B MoE)",
        ),
    }

    models: Dict[str, ModelConfig] = dict(default_models)

    # Overlay YAML models if present
    yaml_models = yaml_data.get("models")
    if isinstance(yaml_models, dict):
        for role_name, m_entry in yaml_models.items():
            if isinstance(m_entry, str):
                m_entry = {"model_id": m_entry.strip()}
            elif not isinstance(m_entry, dict):
                continue
            m_clean = {k: v for k, v in m_entry.items() if v is not None}
            role_key = str(role_name).lower().strip()
            if role_key in default_models:
                merged = default_models[role_key].model_dump()
                merged.update(m_clean)
                models[role_key] = ModelConfig(**merged)
            else:
                new_entry = {
                    "model_id": str(m_clean.get("model_id", role_key)),
                    "provider": str(m_clean.get("provider", "constellation")),
                    "role": str(m_clean.get("role", role_key)),
                    "max_diff_tokens": int(m_clean.get("max_diff_tokens", 8000)),
                    "rpm_limit": int(m_clean.get("rpm_limit", 30)),
                    "tpm_limit": int(m_clean.get("tpm_limit", 120000)),
                    "timeout_seconds": float(m_clean.get("timeout_seconds", 60.0)),
                    "description": str(m_clean.get("description", "")),
                }
                models[role_key] = ModelConfig(**new_entry)

    # Overlay direct environment variable overrides:
    # JURY_ALPHA_MODEL, JURY_BETA_MODEL, JURY_GAMMA_MODEL, JURY_ARBITER_MODEL
    # JURY_ALPHA_PROVIDER, JURY_BETA_PROVIDER, JURY_GAMMA_PROVIDER, JURY_ARBITER_PROVIDER
    env_role_overrides = {
        "alpha": ("JURY_ALPHA_MODEL", "JURY_ALPHA_PROVIDER"),
        "beta": ("JURY_BETA_MODEL", "JURY_BETA_PROVIDER"),
        "gamma": ("JURY_GAMMA_MODEL", "JURY_GAMMA_PROVIDER"),
        "arbiter": ("JURY_ARBITER_MODEL", "JURY_ARBITER_PROVIDER"),
    }
    for role_name, (model_env_var, provider_env_var) in env_role_overrides.items():
        env_model = os.getenv(model_env_var)
        env_provider = os.getenv(provider_env_var)
        if (env_model and env_model.strip()) or (env_provider and env_provider.strip()):
            if role_name not in models:
                models[role_name] = default_models[role_name].model_copy()
            curr = models[role_name].model_dump()
            if env_model and env_model.strip():
                curr["model_id"] = env_model.strip()
            if env_provider and env_provider.strip():
                curr["provider"] = env_provider.strip().lower()
            models[role_name] = ModelConfig(**curr)

    # Generic check for any custom JURY_<ROLE>_MODEL / JURY_<ROLE>_PROVIDER
    for env_k, env_v in os.environ.items():
        if not env_v.strip():
            continue
        if env_k.startswith("JURY_") and env_k.endswith("_MODEL"):
            r_name = env_k[5:-6].lower()
            if r_name not in ("config", "state", "offline", "mock", "simulate_gateway_outage"):
                target_role = None
                if r_name in models:
                    target_role = r_name
                else:
                    for k_role, m_obj in models.items():
                        if m_obj.role.lower() == r_name:
                            target_role = k_role
                            break
                if target_role:
                    curr = models[target_role].model_dump()
                    curr["model_id"] = env_v.strip()
                    models[target_role] = ModelConfig(**curr)
        elif env_k.startswith("JURY_") and env_k.endswith("_PROVIDER"):
            r_name = env_k[5:-9].lower()
            if r_name not in ("config", "state"):
                target_role = None
                if r_name in models:
                    target_role = r_name
                else:
                    for k_role, m_obj in models.items():
                        if m_obj.role.lower() == r_name:
                            target_role = k_role
                            break
                if target_role:
                    curr = models[target_role].model_dump()
                    curr["provider"] = env_v.strip().lower()
                    models[target_role] = ModelConfig(**curr)

    # Ensure any provider referenced by any model exists in providers
    for m in models.values():
        prov_key = m.provider.lower().strip()
        if prov_key not in providers:
            cred = resolve_credentials(cli_dict.get(prov_key), prov_key, workspace_root=workspace_root)
            providers[prov_key] = ProviderConfig(
                name=prov_key,
                base_url=cred["gateway_url"] or _resolve_gateway_url(prov_key),
                api_key=cred["api_key"] or None,
            )

    # Parse remaining top-level scalar configurations from YAML
    version = str(yaml_data.get("version", "3.0.0"))
    quorum_min = int(yaml_data.get("quorum_min", 2))
    request_timeout = float(yaml_data.get("request_timeout_seconds", 60.0))
    contract_timeout = int(yaml_data.get("contract_timeout_seconds", 30))
    isolated_net = bool(yaml_data.get("isolated_network", True))

    strict_decoupling = False
    if "JURY_STRICT_DECOUPLING" in os.environ:
        strict_decoupling = os.getenv("JURY_STRICT_DECOUPLING", "0").strip().lower() in ("1", "true", "yes")
    elif "strict_arbiter_decoupling" in yaml_data:
        strict_decoupling = bool(yaml_data.get("strict_arbiter_decoupling", False))

    config = JuryConfig(
        version=version,
        state_dir=state_dir,
        db_path=db_path,
        lock_path=lock_path,
        log_dir=log_dir,
        providers=providers,
        models=models,
        quorum_min=quorum_min,
        strict_arbiter_decoupling=strict_decoupling,
        request_timeout_seconds=request_timeout,
        contract_timeout_seconds=contract_timeout,
        isolated_network=isolated_net,
    )

    _CACHED_CONFIG = config
    _CACHED_FINGERPRINT = fingerprint

    return config
