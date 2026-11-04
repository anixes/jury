"""
src/jury/cli/templates.py - Rule Templates & Configuration Scaffolding for Jury Mesh v3.0.

Provides standard configurations, IDE agent rule files (AGENTS.md, .cursor/rules/jury.mdc, CLAUDE.md),
and .gitignore updates for `jury init`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Tuple


DEFAULT_CONFIG_YAML = """# Jury (jury-mcp) v3.0 Global Configuration
# Model-agnostic multi-provider council configuration.
version: "3.0.0"

# 1. Provider Credentials & Gateways
# API keys can be specified here or exported as environment variables.
providers:
  openrouter:
    base_url: "https://openrouter.ai/api/v1"
    api_key: "${OPENROUTER_API_KEY}"
  groq:
    base_url: "https://api.groq.com/openai/v1"
    api_key: "${GROQ_API_KEY}"
  deepseek:
    base_url: "https://api.deepseek.com/v1"
    api_key: "${DEEPSEEK_API_KEY}"
  constellation:
    base_url: "https://gateway.constellationgate.ai/v1"
    api_key: "${TRIAD_API_KEY}"
  ollama:
    base_url: "http://localhost:11434/v1"
    api_key: "ollama"

# 2. Deliberation Panel Topologies
# Assign any frontier model from any provider to each seat.
models:
  alpha:
    model_id: "deepseek/deepseek-chat"
    provider: "openrouter"
    role: "architect"
    max_diff_tokens: 16000
    description: "Systems Architect & Invariant Verifier"

  beta:
    model_id: "llama-3.3-70b-versatile"
    provider: "groq"
    role: "adversary"
    max_diff_tokens: 5500
    description: "Adversary, Security & Race Condition Auditor"

  gamma:
    model_id: "qwen/qwen-2.5-coder-32b-instruct"
    provider: "openrouter"
    role: "minimalist"
    max_diff_tokens: 16000
    description: "Simplicity Minimalist & Anti-Bloat Advocate"

  arbiter:
    model_id: "deepseek/deepseek-reasoner"
    provider: "deepseek"
    role: "arbiter"
    max_diff_tokens: 20000
    description: "Decoupled Judicial Arbiter & Consensus Synthesizer"

strict_arbiter_decoupling: false
quorum_min: 2
request_timeout_seconds: 60.0
contract_timeout_seconds: 30
isolated_network: true
"""

FREE_TIER_CONFIG_YAML = """# Jury Mesh (jury-mcp) v3.0 Free-Tier Community Configuration
version: "3.0.0"
preset: "free-tier-only"
models:
  alpha:
    model_id: "constellation/glm-5-3-flash-free"
    provider: "constellation"
    role: "architect"
    max_diff_tokens: 14000
    rpm_limit: 30
    tpm_limit: 120000
    description: "Free-tier Community Systems Architect (GLM-5.3 320B)"
  beta:
    model_id: "constellation/qwen3-8-flash-free"
    provider: "constellation"
    role: "adversary"
    max_diff_tokens: 14000
    rpm_limit: 30
    tpm_limit: 120000
    description: "Free-tier Community Adversary & Auditor (Qwen 3.8 180B)"
  gamma:
    model_id: "constellation/glm-5-3-flash-free"
    provider: "constellation"
    role: "minimalist"
    max_diff_tokens: 14000
    rpm_limit: 30
    tpm_limit: 120000
    description: "Free-tier Simplicity Minimalist & Anti-Bloat Advocate (GLM-5.3 320B)"
  arbiter:
    model_id: "constellation/qwen3-8-flash-free"
    provider: "constellation"
    role: "arbiter"
    max_diff_tokens: 16000
    rpm_limit: 30
    tpm_limit: 120000
    description: "Free-tier Judicial Arbiter & Consensus Synthesizer (Qwen 3.8 180B)"
strict_arbiter_decoupling: false
quorum_min: 2
request_timeout_seconds: 60.0
contract_timeout_seconds: 30
isolated_network: true
"""

AGENTS_MD_TEMPLATE = """# AGENTS.md — Jury Mesh Deliberation & Peer Review Protocol

This repository is protected by **Jury Mesh** (`jury-mcp`) v3.0 Architectural Deliberation and zero-token code synthesis.
All autonomous and human AI coding agents (Claude, Codex, Cursor, etc.) MUST adhere to the following architectural deliberation and peer review protocols:

## 1. Architectural Planning Gate
- **When**: Prior to executing any non-trivial refactor, database migration, or multi-file architectural change.
- **Action**: Run `jury plan "<task_description>" [--files <context_files>]`
- **Output**: Review the emitted `verdict`, `readiness_score`, and `recommendations`.
- **Enforcement**: Proceed only if the plan verdict is `APPROVED` or `APPROVED_WITH_CONDITIONS`.

## 2. Pre-Commit / Pre-Push Diff Review Gate
- **When**: Prior to committing git changes or pushing to shared branches.
- **Action**: Run `jury review --staged` (or `jury review --unstaged` for local working trees).
- **Enforcement**:
  * Exit code `0`: Approved. Commit permitted.
  * Exit code `1`: Changes requested or rejected under `--strict`. The agent must address surviving findings or apply the generated patch.
  * Exit code `4`: Secret detected or security policy violation. Commit blocked until secrets are redacted.

## 3. Zero-Token Patch Application
- Apply synthesized patches directly via git:
  `jury review --staged --format patch | git apply`

## 4. Deadlock-Free Multi-Agent Coordination
- Subagents and parallel workers must not run conflicting long-running daemons.
- All state coordination is daemon-less via SQLite WAL and OS file locks (`.jury/state.db`).
- Coalescing: Identical in-flight review requests automatically coalesce under singleflight leader election.

## 5. Non-Blocking Fallback Policy
- If Jury Mesh reports `QUORUM_COLLAPSE` (exit code `2`) or gateway outage:
  * Agents MUST NOT deadlock or halt indefinitely.
  * Fall back to standard local unit testing (`pytest`) and log an outage warning.
"""

CURSOR_RULES_MDC_TEMPLATE = """---
description: Jury Mesh Peer Review & Architectural Deliberation Rules
globs: *
alwaysApply: true
---

# Jury Mesh Rules for Cursor Agent

Jury Mesh provides multi-model architectural deliberation, peer review, and zero-token patch synthesis.

## Rules:
1. **Plan Deliberation**: Before generating extensive new code or restructuring packages, run `jury plan "<task intent>"`.
2. **Pre-Commit Review**: Before committing staged changes, run `jury review --staged --strict`.
3. **Patch Application**: Apply emitted unified diff patches directly with `git apply`.
4. **Exit Code Policy**:
   - Exit 0: Deliberation passed. Proceed with commit.
   - Exit 1: Deliberation rejected. Inspect findings table and resolve identified defects.
   - Exit 2: Quorum collapse / network failure. Non-blocking fallback: warn and proceed with local test suite.
   - Exit 3: CLI usage error or missing input file.
   - Exit 4: Unredacted secret detected. Abort immediately and scrub credentials.
"""

CLAUDE_MD_TEMPLATE = """# CLAUDE.md — Jury Mesh Guidance for Claude Code

## Multi-Model Architectural Deliberation
- **Plan Review**: Run `jury plan "<intent>"` before starting multi-file refactors.
- **Diff Review**: Run `jury review --staged` before creating git commits.
- **Architectural Q&A**: Run `jury ask "<question>"` to consult the multi-model architecture council.

## Zero-Token Code Synthesis & Patch Application
- Review and apply unified patches directly with `git apply`:
  ```bash
  jury review --staged --format patch | git apply
  ```
- To test patches safely before applying:
  ```bash
  jury review --staged --format patch | git apply --check
  ```

## Exit Code Protocol
- `0`: Approved / Success
- `1`: Rejected / Changes requested
- `2`: Quorum collapse / gateway outage (non-blocking fallback to pytest)
- `3`: Usage error or invalid argument
- `4`: Secret detected / security policy violation
"""


def get_config_template(preset: str = "default") -> str:
    """Return YAML configuration template based on requested preset."""
    if preset in ("free-tier-only", "free", "community"):
        return FREE_TIER_CONFIG_YAML
    return DEFAULT_CONFIG_YAML


def get_rule_templates() -> Dict[str, str]:
    """Return dictionary of relative file paths and their rule template contents."""
    return {
        "AGENTS.md": AGENTS_MD_TEMPLATE,
        ".cursor/rules/jury.mdc": CURSOR_RULES_MDC_TEMPLATE,
        "CLAUDE.md": CLAUDE_MD_TEMPLATE,
    }


def update_gitignore(repo_path: Path) -> Tuple[bool, str]:
    """
    Ensure .gitignore includes .jury/deliberations/ and .jury/state.db
    while guaranteeing .jury/config.yaml is NOT ignored.
    Returns (updated_bool, status_message).
    """
    gitignore_path = repo_path / ".gitignore"
    patterns_to_add = [
        ".jury/deliberations/",
        ".jury/state.db",
        ".jury/state.db*",
    ]

    existing_content = ""
    if gitignore_path.exists():
        existing_content = gitignore_path.read_text(encoding="utf-8")

    lines = [line.strip() for line in existing_content.splitlines()]
    missing = [pat for pat in patterns_to_add if pat not in lines and pat.rstrip("*") not in lines]

    if not missing:
        return False, ".gitignore already contains Jury state ignore patterns."

    append_text = "\n# Jury Mesh local state & deliberations\n" + "\n".join(missing) + "\n"
    if not existing_content.endswith("\n") and existing_content:
        append_text = "\n" + append_text

    gitignore_path.write_text(existing_content + append_text, encoding="utf-8")
    return True, f"Appended {len(missing)} patterns to .gitignore."
