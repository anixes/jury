# Project: Jury Mesh (`jury-mcp`) v3.0

## Architecture
Jury Mesh is a high-performance, daemon-less multi-model deliberation mesh and zero-token code synthesis engine. It subcontracts architectural planning, diff reviews, and dialectical falsification to an orchestrated council of frontier models (free 320B GLM-5.3, 180B Qwen 3.8, Groq Llama 3.3 70B, and independent Arbiters) without resident background daemon overhead, without burning paid tokens, and without tripping IDE timeouts.

### Core Architectural Pillars
1. **Daemon-less SQLite WAL State Engine (`~/.jury/state.db`):**
   - Zero resident process footprint (no background daemon).
   - Cross-process concurrency handled via two-tier locking: OS file locks (`filelock` on `~/.jury/state.db.lock`) + SQLite `BEGIN IMMEDIATE` transactions in WAL mode (`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=30000`).
   - Rate governor with Two-Phase continuous token bucket settlement (RPM and TPM) and circuit breakers protecting free-tier gateways (Groq 12k TPM, Constellation 120k TPM).
   - Decentralized Singleflight request coalescing with 15s heartbeat leasing and dead-leader takeover to eliminate swarm cache stampedes.
   - 5-vector composite cache key (`task_intent || git_head_sha || unstaged_diff || staged_diff || untracked_hash || config_hash || prompt_version`) ensuring instant cache invalidation on any dirty working tree modification.
2. **Dialectical Falsification & Decoupled Arbitration:**
   - 3-stage protocol: Blind Independent Ballots -> Adversarial Cross-Examination (unsubstantiated claims discarded) -> Decoupled Judicial Arbitration.
   - Decoupled Arbiter (DeepSeek V3/R1) permanently separated from panelist families (GLM, Qwen, Llama), eliminating circular self-enhancement confirmation loops.
   - Strict fail-closed quorum ($Q_{min} \ge 2$) preventing silent quorum collapse.
   - Balanced Position Calibration (BPC) 2-pass permutation canceling primacy/recency bias.
3. **Security, Sandboxing & Zero Host RCE:**
   - Completely abolished raw shell execution. Replaced with `TypedVerificationContract` with an immutable runner whitelist (`pytest`, `mypy`, `cargo_test`, `npm_test`, etc.) executed via safe argv (`shell=False`) or isolated Google gVisor (`runsc`).
   - Pre-flight secret scrubbing combining regex and Shannon entropy ($H \ge 4.5$) with synthetic CRC32 tokens (`<REDACTED_API_KEY:CRC32_HEX>`).
4. **Dual Form Factor & Tool Hygiene:**
   - Standalone CLI (`jury`): `jury init`, `jury test`, `jury plan`, `jury review --staged`, `jury ask`, and `jury logs`.
   - MCP Stdio Server (`jury-mcp`): JSON-RPC 2.0 exposing `jury_plan`, `jury_review`, `jury_ask`.
   - Stdio channel hygiene: stdout strictly isolated for JSON-RPC framing; all diagnostics routed to stderr and the SQLite audit log.
   - Workspace rules generator (`jury init`): generates deadlock-free `AGENTS.md`, `.cursor/rules/jury.mdc`, and `CLAUDE.md`.

---

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Python Packaging & Entry Points | `src/jury/` layout, `pyproject.toml` with dual console scripts `jury` and `jury-mcp` | M1 | Survey 3 |
| 2 | Host RCE Prevention Contract | `TypedVerificationContract` with runner whitelist, sanitized argv, and `shell=False` execution | M1 | Survey 3 |
| 3 | Secret & Credential Redactor | Pre-flight regex + Shannon entropy ($H \ge 4.5$) scrubber with synthetic CRC32 tokens | M1 | Survey 3 |
| 4 | Daemon-less SQLite WAL Engine | Embedded SQLite in WAL mode with PRAGMA sequence and `~/.jury/state.db` storage | M2 | Survey 2 |
| 5 | Cross-Process Two-Tier File Lock | Cross-process coordination using `filelock` + SQLite `BEGIN IMMEDIATE` transactions | M2 | Survey 2 |
| 6 | Continuous Rate Limiter & Leaky Bucket | RPM and TPM token buckets with Two-Phase settlement (pre-flight reserve, post-flight refund) | M2 | Survey 2 |
| 7 | Provider Circuit Breakers | Tri-state breaker (CLOSED, OPEN, HALF_OPEN) with `Retry-After` adherence and jitter | M2 | Survey 2 |
| 8 | Singleflight Request Coalescing | In-flight ticket state machine with 15s heartbeat leasing and dead-leader takeover | M2 | Survey 2 |
| 9 | 5-Vector Composite Cache Key | SHA256 of intent, HEAD, unstaged, staged, untracked, config, and prompt version | M2 | Survey 2 |
| 10 | Permanent Deliberation Audit Ledger | SQLite table `jury_audit_log` recording ticket ID, prompt, latency, tokens, scores, and verdicts | M2 | Survey 2 |
| 11 | BaseLLMAdapter & Adapter Registry | Extensible adapter abstraction with unified chat completion, retry, and failover | M3 | Survey 3 |
| 12 | Constellation Gateway Adapter | Adapter for GLM-5.3 320B and Qwen 3.8 180B with credential cascade from `consult_triad.py` | M3 | Survey 3 |
| 13 | Groq Adapter with Token Clamping | Llama 3.3 70B adapter with AST-aware diff clamper enforcing $\le 5,500$ tokens (12k TPM safe) | M3 | Survey 3 |
| 14 | Decoupled Arbiter Adapter | Independent adapter for DeepSeek V3/R1 or external model family | M3 | Survey 3 |
| 15 | Multi-Model Juror Panel Engine | Juror Alpha (Proposer), Juror Beta (Adversary), Juror Gamma (Performance) dispatch | M4 | Survey 3 |
| 16 | 3-Stage Dialectical Falsification Protocol | Blind ballots, adversarial cross-examination, and judicial arbitration | M4 | Survey 3 |
| 17 | Fail-Closed Quorum & BPC Order Invariance | Enforces $Q_{min} \ge 2$, BPC 2-pass permutation, and BPDE hung-jury entropy detection | M4 | Survey 3 |
| 18 | Machine-Actionable Patch Emitter | Synthesizes POSIX unified diffs (`diff --git`) directly applicable via `git apply` | M4 | Survey 1 |
| 19 | OASIS SARIF v2.1.0 Diagnostics | Emits structured SARIF diagnostics for IDE problem panes and CI gates | M4 | Survey 1 |
| 20 | CLI Commands (`jury test`, `plan`, `review`, `ask`, `logs`) | Comprehensive Click/Typer CLI with formatting, exit codes (0–4), and scoping flags | M5 | Survey 1 |
| 21 | Workspace Rules Generator (`jury init`) | Generates deadlock-free `AGENTS.md`, `.cursor/rules/jury.mdc`, and `CLAUDE.md` | M5 | Survey 1 |
| 22 | MCP Stdio Server (`jury-mcp`) | JSON-RPC 2.0 stdio server exposing `jury_plan`, `jury_review`, `jury_ask` | M6 | Survey 1 |
| 23 | Stdio Channel Isolation | Stdout strictly reserved for JSON-RPC frames, all logging routed to stderr | M6 | Survey 1 |
| 24 | E2E Suite & Adversarial Hardening | 100% pass of Tiers 1-4 tests, followed by Tier 5 adversarial coverage hardening | M7 | Dual Track |

---

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Foundation, Packaging & Security | `src/jury/` layout, `pyproject.toml`, `TypedVerificationContract`, Secret Scrubber | none | DONE |
| M2 | Daemon-less State Engine & Concurrency | SQLite WAL, `filelock`, Rate Limiting, Singleflight, 5-Vector Cache, Audit Log | M1 | DONE |
| M3 | Model Adapters & Gateway Integrations | `BaseLLMAdapter`, Constellation Gateway, Groq 5.5k clamper, Arbiter adapter | M1 | DONE |
| M4 | Deliberation Engine & Synthesis Protocol | Dialectical falsification, Decoupled Arbiter, Quorum, POSIX patch emitter, SARIF | M2, M3 | DONE |
| M5 | Standalone CLI & Workspace Rules Generator | `jury init`, `test`, `plan`, `review`, `ask`, `logs`, Rules templates (`AGENTS.md`, etc.) | M4 | DONE |
| M6 | MCP Stdio Server & Bridge Integration | `jury-mcp` stdio JSON-RPC server (`jury_plan`, `jury_review`, `jury_ask`), stderr isolation | M4, M5 | DONE |
| M7 | Final E2E Pass & Adversarial Hardening | 100% pass of E2E suite (Tiers 1-4) + Tier 5 white-box adversarial hardening | M5, M6 | DONE |

---

## Interface Contracts

### 1. `jury.security` ↔ `jury.deliberation`
```python
class TypedVerificationContract(pydantic.BaseModel):
    contract_version: str = "1.0"
    runner: Literal["pytest", "mypy", "cargo_test", "npm_test", "go_test", "custom_safe"]
    isolated_network: bool = True
    timeout_seconds: int = 30
    test_paths: list[str] = []
    env_overrides: dict[str, str] = {}
```

### 2. `jury.state` ↔ `jury.adapters` & `jury.deliberation`
```python
class JuryStateEngine:
    def acquire_rate_limit(self, provider: str, estimated_tokens: int) -> RateLimitAcquisition: ...
    def refund_tokens(self, provider: str, actual_tokens: int) -> None: ...
    def singleflight_enter(self, cache_key: str, timeout_seconds: float = 60.0) -> SingleflightTicket: ...
    def cache_get(self, cache_key: str) -> Optional[DeliberationRecord]: ...
    def cache_put(self, cache_key: str, record: DeliberationRecord, ttl_seconds: int = 300) -> None: ...
    def log_audit(self, entry: AuditLogEntry) -> None: ...
```

### 3. `jury.adapters` ↔ `jury.deliberation`
```python
class BaseLLMAdapter(abc.ABC):
    @abc.abstractmethod
    async def complete(self, messages: list[dict], temperature: float = 0.2, max_tokens: int = 4096) -> LLMResponse: ...
    @abc.abstractmethod
    def clamp_payload(self, diff_text: str) -> str: ...
```

### 4. `jury.deliberation` ↔ `jury.cli` & `jury.mcp_server`
```python
class DeliberationService:
    async def review(self, req: ReviewRequest) -> ReviewVerdict: ...
    async def plan(self, req: PlanRequest) -> PlanVerdict: ...
    async def ask(self, req: AskRequest) -> AskVerdict: ...
```

---

## Code Layout
```
d:\AgenticAI\triad\
├── pyproject.toml              # Packaging with console scripts: jury, jury-mcp
├── README.md                   # Project documentation
├── src\
│   └── jury\
│       ├── __init__.py
│       ├── config.py           # Configuration, provider endpoints, env cascade
│       ├── security\
│       │   ├── __init__.py
│       │   ├── contract.py     # TypedVerificationContract, runner whitelist
│       │   └── scrubber.py     # Regex + Shannon entropy secret redactor
│       ├── state\
│       │   ├── __init__.py
│       │   ├── engine.py       # Daemon-less SQLite WAL + filelock coordinator
│       │   ├── schema.sql      # DDL for rate_limit_buckets, tickets, cache, audit
│       │   ├── rate_limiter.py # Two-phase continuous token bucket + circuit breaker
│       │   ├── singleflight.py # In-flight ticket coalescer & heartbeat leasing
│       │   └── cache_key.py    # 5-vector composite SHA256 key generator
│       ├── adapters\
│       │   ├── __init__.py
│       │   ├── base.py         # BaseLLMAdapter & LLMResponse
│       │   ├── constellation.py# Constellation Gateway (GLM-5.3 & Qwen 3.8)
│       │   ├── groq.py         # Groq (Llama 3.3 70B with 5.5k diff clamper)
│       │   ├── cerebras.py     # Cerebras adapter
│       │   ├── openrouter.py   # OpenRouter / DeepSeek Arbiter
│       │   └── registry.py     # Multi-provider registry & fallback router
│       ├── deliberation\
│       │   ├── __init__.py
│       │   ├── mesh.py         # 3-stage dialectical falsification coordinator
│       │   ├── rubrics.py      # Prompt templates for plan, review, and ask
│       │   ├── voting.py       # Quorum check, BPC order permutation, Borda count
│       │   ├── patch.py        # POSIX unified diff parser & emitter
│       │   └── sarif.py        # OASIS SARIF v2.1.0 generator
│       ├── cli\
│       │   ├── __init__.py
│       │   ├── main.py         # Typer/Click CLI entry point (`jury`)
│       │   ├── init_cmd.py     # Workspace rules generator (`jury init`)
│       │   └── review_cmd.py   # Diff review CLI command
│       └── mcp_server\
│           ├── __init__.py
│           ├── server.py       # Stdio JSON-RPC MCP server (`jury-mcp`)
│           └── tools.py        # Tool implementations: jury_plan, jury_review, jury_ask
└── tests\
    ├── unit\                   # Fast unit tests for each module
    └── e2e\                    # E2E test suite (Tiers 1-4 requirement-driven)
        ├── tier1_features\
        ├── tier2_boundaries\
        ├── tier3_interactions\
        └── tier4_real_world\
```
