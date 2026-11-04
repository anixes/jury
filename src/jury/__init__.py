"""
src/jury/__init__.py - Jury Mesh (`jury-mcp`) v3.0 Master Package.

A resilient multi-model deliberation mesh, MCP server, and zero-token code synthesis engine.
"""

from jury.config import (
    JuryConfig,
    ModelConfig,
    ProviderConfig,
    clear_config_cache,
    get_config,
    load_yaml_config,
    resolve_config_path,
    resolve_credentials,
    resolve_state_dir,
)
from jury.security.contract import (
    ContractExecutionResult,
    TypedVerificationContract,
)
from jury.security.scrubber import (
    RedactionItem,
    RedactionResult,
    scrub_text,
)

__version__ = "3.0.0"

__all__ = [
    "__version__",
    "JuryConfig",
    "ModelConfig",
    "ProviderConfig",
    "clear_config_cache",
    "get_config",
    "load_yaml_config",
    "resolve_config_path",
    "resolve_credentials",
    "resolve_state_dir",
    "TypedVerificationContract",
    "ContractExecutionResult",
    "scrub_text",
    "RedactionResult",
    "RedactionItem",
]
