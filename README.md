# Jury

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![M8ven Score](https://m8ven.ai/badge/mcp/anixes-jury-oxbz01)](https://m8ven.ai/mcp/anixes-jury-oxbz01?s=readme)
[![CLI: jury](https://img.shields.io/badge/CLI-jury-cyan.svg)](#standalone-cli-usage)
[![MCP: jury-mcp](https://img.shields.io/badge/MCP-jury--mcp-green.svg)](#model-context-protocol-mcp-integration)
[![Architecture: Daemon--less](https://img.shields.io/badge/State%20Engine-SQLite%20WAL%20(Daemon--less)-purple.svg)](#architecture-overview)

> **The Multi-Model Deliberation Engine & Consensus Substrate for AI Coding Agents and Developers.**

Jury offloads high-stakes architectural planning, diff reviews, and adversarial verification from local coding agents to an orchestrated council of frontier models—operating with **zero resident daemon footprint**, **zero host RCE vulnerabilities**, and **zero IDE timeouts**.

---

## Architecture Overview

```
                      ┌──────────────────────────────────────┐
                      │    Host Coding Agent / User IDE      │
                      │  (Cursor, Claude Desktop, Antigravity)│
                      └──────────────────┬───────────────────┘
                                         │
                   ┌─────────────────────┴─────────────────────┐
                   │                                           │
          [ Standalone CLI ]                           [ MCP Stdio Server ]
         `jury review | plan`                              `jury-mcp`
                   │                                           │
                   └─────────────────────┬─────────────────────┘
                                         ▼
                     ┌──────────────────────────────────────┐
                     │          Jury Mesh Core Engine       │
                     ├──────────────────────────────────────┤
                     │ • Secret Redaction (Entropy H ≥ 4.5) │
                     │ • POSIX Diff Clamper                 │
                     │ • 5-Vector Composite Cache Key       │
                     │ • Singleflight Request Coalescing    │
                     └───────────────────┬──────────────────┘
                                         ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │                  3-Stage Dialectical Falsification                     │
   │                                                                        │
   │  [Stage 1: Blind Ballots]                                              │
   │   Alpha (Architect)         Beta (Adversary)        Gamma (Minimalist) │
   │   e.g. DeepSeek / Claude    e.g. Llama 3.3 / Qwen   e.g. Qwen / GLM    │
   │            │                         │                       │         │
   │            └─────────────────────────┼───────────────────────┘         │
   │                                      ▼                                 │
   │  [Stage 2: Cross-Examination]                                          │
   │   • Unsubstantiated claims discarded                                   │
   │   • Balanced Position Calibration (BPC) 2-pass permutation             │
   │                                      │                                 │
   │                                      ▼                                 │
   │  [Stage 3: Decoupled Arbitration]                                      │
   │   Arbiter: Decoupled Judge (e.g. DeepSeek R1 / o3-mini / Qwen)         │
   │   • Reconciles conflicts & produces definitive verdict                 │
   │   • Synthesizes machine-actionable POSIX patch (`git apply`)           │
   │   • Generates OASIS SARIF v2.1.0 diagnostics                           │
   └──────────────────────────────────────┬─────────────────────────────────┘
                                          ▼
                     ┌──────────────────────────────────────┐
                     │   Daemon-less State Engine (WAL)     │
                     │        `~/.jury/state.db`            │
                     │ • OS FileLock + BEGIN IMMEDIATE      │
                     │ • Continuous Two-Phase Rate Limiting │
                     │ • Persistent Audit Ledger            │
                     └──────────────────────────────────────┘
```

---

## Key Capabilities

- **Zero-Daemon Architecture**: Operates purely in-process using an embedded SQLite WAL database (`~/.jury/state.db`) guarded by OS file locks. No fragile Docker daemons, background services, or memory leaks.
- **Differentiated 4-Role Council**:
  - **Seat 1 (`alpha`)**: Systems Architect & Invariant Verifier (e.g. DeepSeek V3, Claude 3.7 Sonnet, GLM-5.3)
  - **Seat 2 (`beta`)**: Adversary, Security & Race Condition Auditor (e.g. Llama 3.3 70B, Qwen 3.8, GPT-4o)
  - **Seat 3 (`gamma`)**: Simplicity Minimalist & Anti-Bloat / YAGNI Advocate (e.g. Qwen 2.5 Coder, GLM-5.3)
  - **Judge (`arbiter`)**: Decoupled Consensus Synthesizer & Patch Generator (e.g. DeepSeek R1, o3-mini)
- **Zero Host RCE Security**: Raw shell execution is prohibited. Verification tests execute strictly via `TypedVerificationContract` with an immutable runner whitelist (`pytest`, `mypy`, `cargo_test`, `npm_test`, `go_test`) running under `shell=False`.
- **Pre-Flight Secret Redaction**: All diffs and prompts are sanitized using high-confidence regex combined with Shannon entropy detection ($H \ge 4.5$), replacing credentials with deterministic synthetic tokens (`<REDACTED_API_KEY:CRC32_HEX>`).
- **5-Vector Composite Cache Key**: SHA-256 fingerprint over `task_intent || git_head || unstaged_diff || staged_diff || untracked_hash || config_hash || prompt_version`. Modifying any file instantly triggers cache invalidation.
- **Machine-Actionable Outputs**:
  - Valid POSIX unified diffs (`diff --git`) ready for `git apply`.
  - Industry-standard **OASIS SARIF v2.1.0** diagnostics for IDE problem panes and CI gates.

---

## Installation

```bash
# Clone repository
git clone https://github.com/anixes/jury.git
cd jury

# Install in editable mode
pip install -e .

# Or with dev dependencies (pytest, etc.)
pip install -e ".[dev]"
```

Verify the installation:
```bash
jury --version
# Outputs: jury, version 3.0.0
```

---

## Quickstart

### 1. Initialize Configuration & Agent Rules
Generate project-local configuration and IDE instruction files (`AGENTS.md`, `.cursor/rules/jury.mdc`, `CLAUDE.md`):

```bash
jury init
```

### 2. Configure Credentials & Providers

Jury is **100% provider-agnostic**. You can use any OpenAI-compatible provider (OpenRouter, Groq, DeepSeek, Cerebras, OpenAI, Anthropic, Constellation Gate, or local Ollama / vLLM).

Set your API keys via environment variables:

```bash
# OpenRouter (Unified multi-model access)
export OPENROUTER_API_KEY="sk-or-v1-..."

# Groq (Ultra-fast LPU inference)
export GROQ_API_KEY="gsk_..."

# DeepSeek / OpenAI / Cerebras
export DEEPSEEK_API_KEY="sk-..."
export OPENAI_API_KEY="sk-..."

# Constellation Gate (Free frontier tier)
export TRIAD_API_KEY="cg-..."
```

Or configure your multi-provider council in `.jury/config.yaml`:

```yaml
version: "3.0.0"

# 1. Register only the providers you use
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

# 2. Assign any model from any provider to each seat
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
    description: "Adversary & Security Auditor"

  gamma:
    model_id: "qwen/qwen-2.5-coder-32b-instruct"
    provider: "openrouter"
    role: "minimalist"
    max_diff_tokens: 16000
    description: "Simplicity Minimalist & Anti-Bloat"

  arbiter:
    model_id: "deepseek/deepseek-reasoner"
    provider: "deepseek"
    role: "arbiter"
    max_diff_tokens: 20000
    description: "Decoupled Judicial Arbiter"

strict_arbiter_decoupling: false
quorum_min: 2
```

### 3. Verify Health
Run pre-flight connectivity, token bucket, and database lock diagnostics:

```bash
jury test
```

---

## Standalone CLI Usage

### Code Review (`jury review`)
Audit working tree modifications before committing:

```bash
# Review staged changes
jury review --staged

# Review unstaged changes
jury review

# Strict CI Gate: fail build on non-approval or security findings
jury review --staged --strict

# Output machine-readable JSON or SARIF report
jury review --staged --json
jury review --staged --sarif report.sarif
```

### Architectural Planning (`jury plan`)
Submit an architectural specification or feature request for rigorous multi-model critique:

```bash
jury plan --spec docs/rfc-caching.md
jury plan --spec "Design a distributed write-ahead log with raft consensus"
```

### Freeform Consultation (`jury ask`)
Ask specific architectural or security questions:

```bash
jury ask "What are the edge-case failure modes of two-tier file locking on Windows NTFS?"
```

### Audit Logs & Telemetry (`jury logs`)
Inspect deliberation history and metrics stored in SQLite WAL:

```bash
# View last 10 deliberation tickets
jury logs --limit 10

# View detailed JSON audit trail
jury logs --json
```

---

## Model Context Protocol (MCP) Integration

Jury Mesh exposes native tools over stdio JSON-RPC for Claude Desktop, Cursor, Antigravity, Cline, and Roo Code.

### Server Tools
- `jury_review`: Reviews working tree changes or raw git diffs.
- `jury_plan`: Conducts multi-model architectural falsification on a specification.
- `jury_ask`: Deliberates on complex technical queries across juror models.

### Claude Desktop Configuration
Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "jury": {
      "command": "jury-mcp",
      "args": [],
      "env": {
        "TRIAD_API_KEY": "your-api-key"
      }
    }
  }
}
```

### Cursor / Cline Configuration
In `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "jury": {
      "command": "jury-mcp",
      "args": []
    }
  }
}
```

> **Note on Stdio Hygiene**: `jury-mcp` strictly isolates `stdout` exclusively for JSON-RPC framing. All internal logging, probe messages, and warnings are redirected to `stderr`.

---

## Deterministic CLI Exit Codes

Jury Mesh provides deterministic, script-friendly exit codes for seamless CI/CD integration:

| Exit Code | Classification | Description |
| :---: | :--- | :--- |
| **`0`** | `APPROVED` | Consensus reached, review approved, or command succeeded cleanly. |
| **`1`** | `REJECTED` | Changes requested, architectural objections raised, or test failed. |
| **`2`** | `QUORUM_COLLAPSE` | Upstream provider outage or failure to meet minimum quorum ($Q < Q_{min}$). |
| **`3`** | `USAGE_ERROR` | Invalid CLI arguments, missing configuration, or unreadable files. |
| **`4`** | `SECURITY_VIOLATION` | Secret leak detected or contract validation failure. |

---

## Development & Testing

Run the comprehensive test suite:

```bash
# Run unit tests
pytest tests/unit/ -v

# Run E2E suites
pytest tests/e2e/ -v

# Run full test suite with coverage
pytest --cov=src/jury tests/
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
