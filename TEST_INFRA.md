# TEST_INFRA — Jury Mesh (`jury-mcp`) v3.0 End-to-End Test Infrastructure

## 1. Overview & Test Architecture

This document specifies the end-to-end (E2E) testing infrastructure for **Jury Mesh (`jury-mcp`) v3.0**. The test suite verifies the end-to-end functionality, security, concurrency, and performance of Jury strictly across public interfaces—mirroring the operational environment of human software engineers, IDE agent swarms (Cursor, Claude Code, Antigravity), and CI/CD pipelines.

### Core Testing Principles
1. **Opaque-Box & Requirement-Driven**: Tests interact exclusively with Jury through public boundaries:
   - Standalone CLI: `jury init`, `jury test`, `jury plan`, `jury review`, `jury ask`, `jury logs`.
   - MCP Server: Stdio JSON-RPC 2.0 calls (`jury_plan`, `jury_review`, `jury_ask`).
   - Workspace rules files: `AGENTS.md`, `.cursor/rules/jury.mdc`, `CLAUDE.md`.
   - Output artifacts: OASIS SARIF v2.1.0, POSIX unified diffs (`diff --git`), `verification-contract.json`.
   - Deterministic Exit Codes (0, 1, 2, 3, 4).
2. **Deterministic & Network-Isolated**: Upstream LLM gateway calls (Constellation Gate, Groq, OpenRouter) are intercepted by local mock fixtures and deterministic responders by default. Tests never require paid tokens, external network access, or external API keys to run cleanly in CI/CD. Live gateway calls can be enabled via `JURY_LIVE_KEYS=1`.
3. **Multi-Agent State & Concurrency Isolation**: Each test runs with an isolated temporary state directory (`$env:JURY_STATE_DIR` / `tmp_path / ".jury"`), ensuring zero contamination of `~/.jury/state.db`. Cross-process locks, token buckets, and singleflight tickets are validated using isolated SQLite databases in WAL mode.
4. **Progressive Testability & Interface Compatibility**: Tests run against either the installed `jury` console scripts, `python -m jury.cli.main`, or in-process interface handlers. Tests are resilient to milestone progression, identifying implemented capabilities while tracking unfulfilled milestones without crashing test discovery.

---

## 2. Directory Layout

The E2E test suite resides under `tests/e2e/`:

```
d:\AgenticAI\triad\
├── TEST_INFRA.md                       # This document: Test architecture & 24-feature matrix
├── TEST_READY.md                       # Verification summary & test execution command
├── pyproject.toml                      # Pytest configuration & console scripts
└── tests/
    └── e2e/
        ├── __init__.py                 # E2E test package marker
        ├── conftest.py                 # Shared fixtures: temp git repos, mock gateway, runners
        ├── test_tier1_features.py      # Tier 1: Core CLI, MCP tools, rules, output formats (>=5 per area)
        ├── test_tier2_boundaries.py    # Tier 2: Boundaries, git states, diff clamping, 429 backoff
        ├── test_tier3_interactions.py  # Tier 3: Cross-process singleflight, 5-vector cache, token buckets
        └── test_tier4_real_world.py    # Tier 4: Full developer workflow & 10-agent swarm simulations
```

---

## 3. Feature Coverage Matrix (24 Features)

All 24 core architectural capabilities defined in `PROJECT.md` are mapped to explicit E2E test tiers and test functions:

| # | Feature Inventory Item | Milestone | Test File | Test Class & Target Function |
|---|---|---|---|---|
| **1** | Python Packaging & Entry Points | M1 | `test_tier1_features.py` | `TestPackagingAndEntryPoints::test_entry_points_*` |
| **2** | Host RCE Prevention Contract | M1 | `test_tier3_interactions.py` | `TestVerificationContractSecurity::test_shell_injection_blocking_*` |
| **3** | Secret & Credential Redactor | M1 | `test_tier1_features.py` | `TestSecretRedactionE2E::test_secret_scrubbing_*` |
| **4** | Daemon-less SQLite WAL Engine | M2 | `test_tier2_boundaries.py` | `TestDatabaseWALEngine::test_wal_pragmas_and_recovery_*` |
| **5** | Cross-Process Two-Tier File Lock | M2 | `test_tier3_interactions.py` | `TestCrossProcessLocking::test_two_tier_filelock_concurrency_*` |
| **6** | Continuous Rate Limiter & Leaky Bucket | M2 | `test_tier3_interactions.py` | `TestRateLimiterInteractions::test_two_phase_settlement_refund_*` |
| **7** | Provider Circuit Breakers | M2 | `test_tier2_boundaries.py` | `TestProviderCircuitBreakers::test_429_circuit_breaker_tripping_*` |
| **8** | Singleflight Request Coalescing | M2 | `test_tier3_interactions.py` | `TestSingleflightCoalescing::test_coalescing_across_processes_*` |
| **9** | 5-Vector Composite Cache Key | M2 | `test_tier3_interactions.py` | `TestCompositeCacheKey::test_dirty_tree_invalidation_*` |
| **10** | Deliberation Audit Ledger | M2 | `test_tier1_features.py` | `TestCliLogsCommand::test_logs_inspection_and_stats_*` |
| **11** | BaseLLMAdapter & Registry | M3 | `test_tier1_features.py` | `TestAdapterRegistryE2E::test_provider_dispatch_and_fallback_*` |
| **12** | Constellation Gateway Adapter | M3 | `test_tier1_features.py` | `TestConstellationAdapter::test_glm_qwen_deliberation_*` |
| **13** | Groq Adapter with Clamping | M3 | `test_tier2_boundaries.py` | `TestGroqTokenClamping::test_ast_diff_clamping_under_5500_*` |
| **14** | Decoupled Arbiter Adapter | M3 | `test_tier1_features.py` | `TestDecoupledArbiter::test_decoupled_synthesis_family_*` |
| **15** | Multi-Model Juror Panel Engine | M4 | `test_tier1_features.py` | `TestJurorPanelEngine::test_triad_juror_dispatch_*` |
| **16** | 3-Stage Dialectical Falsification | M4 | `test_tier4_real_world.py` | `TestDialecticalDeliberation::test_refute_or_concede_workflow_*` |
| **17** | Fail-Closed Quorum & BPC Invariance | M4 | `test_tier2_boundaries.py` | `TestQuorumAndBPC::test_fail_closed_quorum_collapse_*` |
| **18** | Machine-Actionable Patch Emitter | M4 | `test_tier1_features.py` | `TestOutputFormats::test_posix_patch_git_apply_*` |
| **19** | OASIS SARIF v2.1.0 Diagnostics | M4 | `test_tier1_features.py` | `TestOutputFormats::test_sarif_v210_schema_compliance_*` |
| **20** | CLI Commands (`init`, `test`, `plan`, `review`, `ask`, `logs`) | M5 | `test_tier1_features.py` | `TestCliCommands::test_cli_*` (all 6 commands) |
| **21** | Workspace Rules Generator (`init`) | M5 | `test_tier1_features.py` | `TestWorkspaceRulesGenerator::test_rules_generation_*` |
| **22** | MCP Stdio Server (`jury-mcp`) | M6 | `test_tier1_features.py` | `TestMcpStdioServer::test_mcp_tools_*` |
| **23** | Stdio Channel Isolation | M6 | `test_tier1_features.py` | `TestMcpStdioServer::test_channel_hygiene_stderr_isolation_*` |
| **24** | E2E Suite & Swarm Hardening | M7 | `test_tier4_real_world.py` | `TestSwarmSimulation::test_parallel_agent_swarm_*` |

---

## 4. Test Tiers & Execution Structure

### Tier 1: Core Functional & Interface Tests (`test_tier1_features.py`)
Covers primary behaviors (happy path, validation, and standard outputs) with $\ge 5$ explicit test cases per area:
1. **CLI Commands**:
   - `jury init`: Scaffolds `.jury/config.yaml`, handles `--force`, `--rules-only`, `--config-only`, git repository detection.
   - `jury test`: Provider connectivity, SQLite WAL lock check, exit code 0 on healthy quorum, exit code 2 on quorum collapse.
   - `jury plan`: Markdown plan input, context files attachment, `--json` format, `--no-cache` bypass.
   - `jury review`: Staged review, unstaged review, `--files` glob scoping, `--format` selection, `--strict` mode.
   - `jury ask`: Targeted architecture query, `--quick` mode, context file attachment, structured JSON output.
   - `jury logs`: Recent ticket listing, `--ticket` inspection, `--verdict` filtering, `--stats` aggregate metrics, `--clean` pruning.
2. **MCP Stdio Server & Tools**:
   - `jury_plan`: JSON-RPC parameter validation, consensus score, file execution plan, typed verification contract.
   - `jury_review`: Staged/unstaged diff review, finding severity classification, unified diff generation, SARIF mapping.
   - `jury_ask`: Single/multi-model consultation, tradeoff synthesis, code snippet extraction.
   - Protocol Hygiene: `sys.stdout` exclusively contains valid JSON-RPC frames; all diagnostics directed to `sys.stderr`.
   - Async Task Support: Immediate ticket return (`<10ms`) for non-blocking task polling.
3. **Workspace Rules Generator**:
   - Generates `AGENTS.md` with deadlock guards for subagents.
   - Generates `.cursor/rules/jury.mdc` with frontmatter and MDC triggers.
   - Generates `CLAUDE.md` with CLI guidance and pipe-to-`git apply` instructions.
   - Non-destructive safety: Existing rule files preserved unless `--force` is specified.
   - `.gitignore` integration: Adds `.jury/deliberations/` while keeping `.jury/config.yaml`.
4. **Output Formats & Exit Codes**:
   - Human-readable markdown reports.
   - OASIS SARIF v2.1.0 output valid against the official SARIF schema.
   - Machine-actionable POSIX patch (`diff --git`) tested for valid application via `git apply`.
   - Deterministic Exit Code Contract:
     * `0`: SUCCESS_PASS
     * `1`: VERDICT_REJECTED / strict failure
     * `2`: QUORUM_COLLAPSE ($Q < 2$)
     * `3`: CONFIG_OR_USAGE_ERROR
     * `4`: SECRET_DETECTED

### Tier 2: Boundary & Corner Conditions (`test_tier2_boundaries.py`)
Validates system resilience against extreme inputs and failure modes:
1. **Git State Boundaries**:
   - Empty git repositories with zero commits.
   - Working trees with unstaged changes only.
   - Working trees with staged changes only.
   - Working trees with untracked files only.
   - Repositories with detached HEAD or merge conflicts.
2. **Payload & Clamping Boundaries**:
   - Large diffs exceeding 16,000 tokens ($>100\text{ KB}$): AST-aware diff clamper enforces $\le 5,500$ tokens for Groq while retaining function signatures.
   - Empty diffs or binary file diffs: Graceful exit with informational verdict.
3. **Gateway Rate Limits & Backoff**:
   - Simulated HTTP 429 Too Many Requests: Exponential backoff with jitter and respect for `Retry-After` headers.
   - Circuit Breaker transitions: CLOSED $\to$ OPEN $\to$ HALF_OPEN $\to$ CLOSED.
4. **Timeouts & Dead-Leader Takeover**:
   - In-flight ticket leasing timeout: If leader process crashes or takes $>15\text{s}$, peer process takes over execution without deadlock.

### Tier 3: Concurrency & Interaction Tests (`test_tier3_interactions.py`)
Validates interactions between subsystem components:
1. **Cross-Process Singleflight Coalescing**:
   - 8 concurrent processes query identical diffs simultaneously: Exactly 1 LLM request dispatched; 7 processes wait and receive the coalesced verdict.
2. **5-Vector Composite Cache Key Invalidation**:
   - Editing 1 byte in an unstaged file, staging a new file, or modifying `.jury/config.yaml` produces a unique cache key, preventing stale cache hits.
3. **Two-Phase Continuous Rate Limiting**:
   - Pre-flight token reservation deducts from SQLite token bucket.
   - Post-flight refund reconciles actual consumed tokens, restoring excess reserved tokens.
4. **Typed Verification Contract Security**:
   - Rejects shell metacharacters (`;`, `&`, `|`, `` ` ``, `$`, `\n`) in runner, arguments, or paths, preventing Confused Deputy Host RCE (OWASP LLM01/LLM02).

### Tier 4: Real-World Workflows & Swarm Simulation (`test_tier4_real_world.py`)
Exercises production-grade developer and agent workflows:
1. **Developer End-to-End Workflow**:
   - `jury init` initializes repository and generates IDE rules.
   - Developer introduces code defect and stages changes (`git add`).
   - `jury review --staged --format patch` reviews diff and outputs POSIX patch.
   - Patch is piped directly to `git apply -v`.
   - `git diff` confirms bug is resolved with zero manually typed code tokens.
   - Audit log in `.jury/state.db` records complete deliberation history.
2. **IDE Agent Swarm Simulation**:
   - 10 parallel subagents simulate a swarm review burst.
   - Singleflight coalescer prevents gateway stampede.
   - Rate governor buffers requests within free-tier quotas (30 RPM / 12k TPM).
   - Zero deadlock, zero database lock exceptions (`busy_timeout=30000`), zero orphan processes.

---

## 5. Pass/Fail Semantics & Exit Code Verification

Every test case asserts specific observable contracts:

| Observable Boundary | Assertion Contract |
|---|---|
| **CLI Exit Code** | `result.exit_code == expected_code` (0 for pass, 1 for rejected, 2 for quorum collapse, 3 for usage error, 4 for secret). |
| **Stdout Formatting** | In text mode: Contains human verdict header; in JSON mode: Parseable JSON matching Pydantic schema; in patch mode: Valid `diff --git` hunk. |
| **Stderr Diagnostics** | No stack traces; warnings printed exclusively to stderr; zero pollution of stdout in MCP mode. |
| **SARIF Diagnostics** | Valid JSON with `$schema: "https://json.schemastore.org/sarif-2.1.0.json"`, `version: "2.1.0"`, and `runs[0].results` with `ruleId` and `locations`. |
| **POSIX Patch Applicability**| Validated via `git apply --check` or `git apply` against local temporary git working trees. |
| **SQLite State Integrity** | `PRAGMA integrity_check` returns `ok`; WAL mode verified via `PRAGMA journal_mode == 'wal'`. |

---

## 6. How to Run the Tests

### Fast Run (All E2E Tiers)
```bash
pytest tests/e2e -v
```

### Run by Specific Tier
```bash
pytest tests/e2e/test_tier1_features.py -v
pytest tests/e2e/test_tier2_boundaries.py -v
pytest tests/e2e/test_tier3_interactions.py -v
pytest tests/e2e/test_tier4_real_world.py -v
```

### Run with Live Gateways (Optional)
```bash
JURY_LIVE_KEYS=1 pytest tests/e2e -v
```
