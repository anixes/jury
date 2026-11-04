-- ============================================================================
-- JURY MESH v3.0 DAEMON-LESS SCHEMA SPECIFICATION
-- ============================================================================
-- Embedded SQLite database schema for cross-process swarm coordination,
-- continuous rate limiting, singleflight ticket coalescing, composite caching,
-- and permanent deliberation audit logging.
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 30000;
PRAGMA temp_store = MEMORY;
PRAGMA foreign_keys = ON;

-- 1. Rate Limit Buckets & Circuit Breakers
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    bucket_id TEXT PRIMARY KEY,             -- e.g. 'groq:llama-3.3-70b-versatile', 'constellation:glm-5-3-flash-free'
    provider_id TEXT NOT NULL,              -- e.g. 'groq', 'constellation', 'cerebras', 'openrouter', 'local'
    model_id TEXT NOT NULL,                 -- e.g. 'llama-3.3-70b-versatile'
    rpm_limit REAL NOT NULL,                -- Requests Per Minute ceiling (e.g. 30.0)
    rpm_current REAL NOT NULL,              -- Currently accrued RPM tokens (up to rpm_limit)
    rpm_last_refill REAL NOT NULL,          -- Unix epoch timestamp of last RPM calculation
    tpm_limit REAL NOT NULL,                -- Tokens Per Minute ceiling (e.g. 12000.0 for Groq free tier)
    tpm_current REAL NOT NULL,              -- Currently accrued TPM tokens (up to tpm_limit)
    tpm_last_refill REAL NOT NULL,          -- Unix epoch timestamp of last TPM calculation
    concurrency_limit INTEGER NOT NULL DEFAULT 2, -- Max concurrent outbound HTTP streams (e.g. 1 for Groq, 2 for Constellation)
    in_flight_count INTEGER NOT NULL DEFAULT 0,   -- Current active HTTP connections
    circuit_state TEXT NOT NULL DEFAULT 'CLOSED', -- 'CLOSED', 'OPEN', 'HALF_OPEN'
    cooldown_until REAL,                    -- Unix epoch timestamp until which circuit is open (from Retry-After)
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_reason TEXT,
    last_success_at REAL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_buckets_provider ON rate_limit_buckets(provider_id);

-- 2. Distributed Singleflight In-Flight Tickets
CREATE TABLE IF NOT EXISTS jury_tickets (
    ticket_id TEXT PRIMARY KEY,             -- e.g. 'tkt_1730485200_a1b2c3'
    cache_key TEXT NOT NULL,                -- 5-Vector Composite Cache Key (SHA256 hex)
    status TEXT NOT NULL,                   -- 'IN_FLIGHT', 'COMPLETED', 'FAILED', 'TIMED_OUT'
    leader_pid INTEGER NOT NULL,            -- OS Process ID executing the deliberation
    leader_hostname TEXT NOT NULL,          -- Machine hostname for auditability
    command TEXT NOT NULL,                  -- 'init', 'test', 'plan', 'review', 'ask'
    intent TEXT NOT NULL,                   -- Task intent / prompt
    diff_summary TEXT,                      -- Summary of files and changes
    created_at REAL NOT NULL,               -- Ticket creation timestamp
    heartbeat_at REAL NOT NULL,             -- Updated periodically by leader (dead leader detection)
    expires_at REAL NOT NULL,               -- Hard lease expiry (e.g. created_at + 60.0s)
    completed_at REAL,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_tickets_cache_status ON jury_tickets(cache_key, status);
CREATE INDEX IF NOT EXISTS idx_tickets_heartbeat ON jury_tickets(status, heartbeat_at);

-- 3. Composite Key Cache Store
CREATE TABLE IF NOT EXISTS jury_cache (
    cache_key TEXT PRIMARY KEY,             -- 5-Vector Composite SHA256 Key
    created_at REAL NOT NULL,               -- Timestamp when deliberation finished
    expires_at REAL NOT NULL,               -- created_at + ttl_seconds
    ttl_seconds INTEGER NOT NULL DEFAULT 300, -- Default 300s (5 minutes)
    intent TEXT NOT NULL,                   -- Normalized intent string
    git_head_sha TEXT NOT NULL,             -- Commit SHA at time of review
    dirty_unstaged_hash TEXT NOT NULL,      -- SHA256 of unstaged git diff
    dirty_staged_hash TEXT NOT NULL,        -- SHA256 of staged git diff
    untracked_files_hash TEXT NOT NULL,     -- SHA256 of untracked files tree
    config_hash TEXT NOT NULL,              -- Hash of active jury models & prompts
    prompt_version TEXT NOT NULL,           -- Jury prompt version (e.g. 'v3.0.0')
    verdict TEXT NOT NULL,                  -- 'APPROVED', 'APPROVED_WITH_CONDITIONS', 'REJECTED'
    patch_unified_diff TEXT,                -- Machine-applicable unified diff ('diff --git')
    sarif_json TEXT,                        -- OASIS SARIF v2.1.0 JSON diagnostics
    verification_contract_json TEXT,        -- Typed verification runner schema
    deliberation_markdown TEXT NOT NULL,    -- Human-readable synthesis & debate summary
    juror_payloads_json TEXT,               -- Raw individual responses from jurors
    tokens_saved INTEGER NOT NULL DEFAULT 0,-- Cumulative tokens saved by serving from cache
    computation_duration_ms INTEGER NOT NULL DEFAULT 0, -- Time taken to compute deliberation
    hit_count INTEGER NOT NULL DEFAULT 0,   -- Number of times served from cache
    last_accessed_at REAL NOT NULL          -- Used for LRU cache eviction
);

CREATE INDEX IF NOT EXISTS idx_cache_expires ON jury_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_cache_last_accessed ON jury_cache(last_accessed_at);

-- 4. Audit Log & Deliberation Ledger
CREATE TABLE IF NOT EXISTS jury_audit_log (
    ticket_id TEXT PRIMARY KEY,             -- References jury_tickets(ticket_id)
    timestamp REAL NOT NULL,                -- Invocation timestamp
    command TEXT NOT NULL,                  -- 'init', 'test', 'plan', 'review', 'ask'
    prompt TEXT NOT NULL,                   -- Task prompt / intent
    diff_summary TEXT,                      -- Files modified, +/- lines
    verdict TEXT NOT NULL,                  -- Final verdict
    juror_scores TEXT,                      -- JSON object of individual juror scores
    latency_ms INTEGER NOT NULL,            -- Total end-to-end execution latency
    tokens_used INTEGER NOT NULL,           -- Actual remote tokens consumed
    tokens_saved INTEGER NOT NULL DEFAULT 0,-- Tokens spared via caching or diff patch
    caller_process_id INTEGER,              -- OS PID of calling agent
    caller_tool TEXT,                       -- 'mcp:jury_review', 'cli:jury review'
    cache_hit INTEGER NOT NULL DEFAULT 0,   -- 1 = served from cache, 0 = remote deliberation
    error_code TEXT,
    error_details TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON jury_audit_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_verdict ON jury_audit_log(verdict);
CREATE INDEX IF NOT EXISTS idx_audit_command ON jury_audit_log(command);

-- Schema version tracking
PRAGMA user_version = 1;
