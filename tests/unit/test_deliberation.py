r"""
tests/unit/test_deliberation.py - Unit Test Suite for Deliberation Engine & Synthesis Protocol.

Tests:
1. Rubrics, Pydantic schemas, StylometricSanitizer, and JSON extraction
2. Fail-closed quorum evaluation ($Q_{min} \ge 2$)
3. Consensus Index $C$, BPDE entropy, and hung-jury detection
4. BPC (Borda-Pairwise-Condorcet) scoring with 2-pass permutation & self-vote exclusion
5. Machine-actionable POSIX patch emitter, parser, validator, and in-memory application
6. OASIS SARIF v2.1.0 schema generator and file persistence
7. DeliberationMesh 3-stage dialectical protocol (review, plan, ask)
8. State engine integration: 5-vector composite cache, singleflight coalescing, audit logging
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any, Dict, List
import pytest

from jury.adapters.arbiter import ArbiterCouplingError, DecoupledArbiterAdapter
from jury.adapters.base import LLMResponse
from jury.adapters.registry import AdapterRegistry, MockLLMAdapter
from jury.config import JuryConfig
from jury.deliberation.mesh import (
    AskRequest,
    AskVerdict,
    DeliberationMesh,
    PlanRequest,
    PlanVerdict,
    ReviewRequest,
    ReviewVerdict,
)
from jury.deliberation.patch import (
    apply_patch_in_memory,
    extract_unified_diff,
    parse_unified_diff,
    synthesize_unified_diff,
    validate_patch,
)
from jury.deliberation.rubrics import (
    ArbitrationVerdict,
    AskBallot,
    Finding,
    JurorBallot,
    PlanBallot,
    ReviewBallot,
    StylometricSanitizer,
    extract_json_payload,
)
from jury.deliberation.sarif import (
    SARIF_SCHEMA_URI,
    SARIF_VERSION,
    SarifReport,
    generate_sarif_report,
    severity_to_sarif_level,
)
from jury.deliberation.voting import (
    bpc_score_candidates,
    check_quorum,
    compute_bpde_entropy,
    compute_consensus_index,
    detect_hung_jury,
)
from jury.security.contract import TypedVerificationContract
from jury.state.engine import JuryStateEngine


# =============================================================================
# 1. Rubrics, Data Models & Sanitizer Tests
# =============================================================================

def test_finding_model_validation():
    f = Finding(
        file_path="src/auth.py",
        line_start=10,
        line_end=15,
        severity="HIGH",
        category="security",
        issue="Unvalidated token parameter",
        proof_or_refutation="Line 12 accepts None",
        suggested_replacement="if token is None: raise AuthError()",
    )
    assert f.file_path == "src/auth.py"
    assert f.line_start == 10
    assert f.line_end == 15
    assert f.severity == "HIGH"
    assert f.status == "OPEN"

    # Line end auto-adjustment if less than line_start
    f2 = Finding(
        file_path="src/main.py",
        line_start=20,
        line_end=5,  # Invalid line end
        severity="LOW",
        issue="Minor comment typo",
    )
    assert f2.line_end == 20


def test_stylometric_sanitizer():
    raw_text = (
        "Hello! As an AI developed by Zhipu, I am pleased to review this code.\n\n"
        "Here is my review:\n"
        "The authentication handler has a race condition on line 42.\n"
        "As an AI, I recommend adding a mutex."
    )
    sanitized = StylometricSanitizer.sanitize_text(raw_text)
    assert "as an ai" not in sanitized.lower()
    assert "zhipu" not in sanitized.lower()
    assert "hello" not in sanitized.lower()
    assert "race condition on line 42" in sanitized

    # Anonymized ID generation
    anon_id1 = StylometricSanitizer.generate_anonymized_id("alpha", "glm-5.3", seed="seed1")
    anon_id2 = StylometricSanitizer.generate_anonymized_id("alpha", "glm-5.3", seed="seed1")
    anon_id3 = StylometricSanitizer.generate_anonymized_id("beta", "qwen-3.8", seed="seed1")

    assert anon_id1.startswith("Juror #")
    assert anon_id1 == anon_id2  # Deterministic
    assert anon_id1 != anon_id3  # Distinct per role/model


def test_extract_json_payload():
    # Direct JSON
    d1 = extract_json_payload('{"score": 8.5, "verdict": "APPROVED"}')
    assert d1["score"] == 8.5
    assert d1["verdict"] == "APPROVED"

    # Markdown code fence JSON
    fence_text = (
        "Here is the evaluation result:\n```json\n"
        '{"readiness_score": 9.0, "verdict": "APPROVED"}\n```\nHope this helps!'
    )
    d2 = extract_json_payload(fence_text)
    assert d2["readiness_score"] == 9.0

    # Embedded JSON in messy prose without fence
    messy_text = 'Analysis: The design is robust. Output: {"confidence": 0.92, "pattern": "CQRS"} Done.'
    d3 = extract_json_payload(messy_text)
    assert d3["confidence"] == 0.92
    assert d3["pattern"] == "CQRS"

    # Malformed text fallback
    assert extract_json_payload("Random non-json text") == {}


# =============================================================================
# 2. Fail-Closed Quorum & Voting Mathematics Tests
# =============================================================================

def test_check_quorum_fail_closed():
    # Case 1: Empty ballots -> ERR_QUORUM_COLLAPSE
    res1 = check_quorum([])
    assert res1.is_valid is False
    assert res1.error_code == "ERR_QUORUM_COLLAPSE"

    # Case 2: Only 1 valid ballot -> ERR_QUORUM_COLLAPSE
    b_single = JurorBallot(
        juror_id="alpha",
        model="constellation/glm-5-3-flash-free",
        provider="constellation",
        role_description="Architect",
        score=8.5,
        verdict="APPROVED",
        content_raw="OK",
    )
    res2 = check_quorum([b_single], min_quorum=2)
    assert res2.is_valid is False
    assert res2.operational_jurors == 1
    assert res2.error_code == "ERR_QUORUM_COLLAPSE"

    # Case 3: 2 ballots from SAME model family (monocultural collapse)
    b_glm1 = JurorBallot(
        juror_id="alpha",
        model="constellation/glm-5-3-flash-free",
        provider="constellation",
        role_description="Architect",
        score=8.0,
        verdict="APPROVED",
        content_raw="OK",
    )
    b_glm2 = JurorBallot(
        juror_id="beta",
        model="zhipu/glm-5",
        provider="zhipu",
        role_description="Adversary",
        score=7.5,
        verdict="APPROVED",
        content_raw="OK",
    )
    res3 = check_quorum([b_glm1, b_glm2], min_quorum=2, require_distinct_families=True)
    assert res3.is_valid is False
    assert res3.distinct_model_families == 1
    assert res3.error_code == "ERR_QUORUM_COLLAPSE"

    # Case 4: 2 ballots from distinct model families -> PASS
    b_qwen = JurorBallot(
        juror_id="beta",
        model="constellation/qwen3-8-flash-free",
        provider="constellation",
        role_description="Adversary",
        score=7.0,
        verdict="APPROVED_WITH_CONDITIONS",
        content_raw="OK",
    )
    res4 = check_quorum([b_glm1, b_qwen], min_quorum=2)
    assert res4.is_valid is True
    assert res4.operational_jurors == 2
    assert res4.distinct_model_families == 2


def test_consensus_index_and_bpde_entropy():
    # Unanimous scores
    mean1, std1, c1 = compute_consensus_index([8.0, 8.0, 8.0])
    assert mean1 == 8.0
    assert std1 == 0.0
    assert c1 == 1.0

    # Divergent scores (0.0 and 10.0 -> max std_dev 5.0 -> consensus 0.0)
    mean2, std2, c2 = compute_consensus_index([0.0, 10.0])
    assert mean2 == 5.0
    assert std2 == 5.0
    assert c2 == 0.0

    # Typical moderate divergence
    mean3, std3, c3 = compute_consensus_index([8.0, 7.5, 9.0])
    assert round(mean3, 2) == 8.17
    assert c3 > 0.80

    # BPDE Entropy: unanimous
    assert compute_bpde_entropy(["APPROVED", "APPROVED", "APPROVED"]) == 0.0

    # BPDE Entropy: 50/50 split -> log2(2) = 1.0 bit (bits default), or ln(2) ~= 0.693 nats
    ent_split = compute_bpde_entropy(["APPROVED", "REJECTED"])
    assert round(ent_split, 3) == 1.000
    ent_split_nats = compute_bpde_entropy(["APPROVED", "REJECTED"], base=math.e)
    assert round(ent_split_nats, 3) == 0.693


def test_detect_hung_jury():
    # Case 1: Unanimous consensus -> Not hung
    m1 = detect_hung_jury([8.5, 8.0, 9.0], ["APPROVED", "APPROVED", "APPROVED"])
    assert m1.is_hung_jury is False
    assert m1.consensus_index > 0.85

    # Case 2: Severe numerical score divergence -> Hung jury
    m2 = detect_hung_jury([1.0, 9.0], ["REJECTED", "APPROVED"])
    assert m2.is_hung_jury is True
    assert "divergence" in m2.hung_jury_reason.lower() or "polarity" in m2.hung_jury_reason.lower()

    # Case 3: Polar split with entropy >= 0.45
    m3 = detect_hung_jury([5.0, 5.0], ["APPROVED", "REJECTED"])
    assert m3.is_hung_jury is True


def test_bpc_score_candidates_with_self_vote_exclusion():
    # Three candidates: C1, C2, C3
    # Alpha (GLM) proposed C1
    # Beta (Qwen) proposed C2
    # Gamma (Llama) proposed C3
    candidate_proposers = {
        "C1": "glm-5.3",
        "C2": "qwen3.8",
        "C3": "llama3.3",
    }
    rankings = {
        "glm-juror": ["C1", "C2", "C3"],    # GLM ranks own C1 first, but self-vote excluded!
        "qwen-juror": ["C2", "C1", "C3"],   # Qwen ranks own C2 first, self-vote excluded!
        "llama-juror": ["C1", "C2", "C3"],  # Llama ranks C1 > C2 > C3
    }

    result = bpc_score_candidates(
        rankings_per_juror=rankings,
        candidate_proposers=candidate_proposers,
    )

    assert result.permutations_evaluated == 2  # 2-pass permutation
    assert "C1" in result.borda_scores
    assert "C2" in result.borda_scores
    assert "C3" in result.borda_scores

    # C1 receives highest legitimate non-self votes
    assert result.ranked_candidates[0] == "C1"
    assert result.condorcet_winner == "C1"


# =============================================================================
# 3. Machine-Actionable POSIX Patch Emitter & Validator Tests
# =============================================================================

def test_synthesize_and_validate_unified_diff():
    file_path = "src/calculator.py"
    original = "def add(a, b):\n    return a - b\n"  # Bug: minus instead of plus
    replacement = "def add(a, b):\n    return a + b\n"

    diff = synthesize_unified_diff(
        file_path=file_path,
        original_content=original,
        replacement_content=replacement,
        context_lines=1,
    )

    assert diff.startswith("diff --git a/src/calculator.py b/src/calculator.py")
    assert "--- a/src/calculator.py" in diff
    assert "+++ b/src/calculator.py" in diff
    assert "@@ -1,2 +1,2 @@" in diff
    assert "-    return a - b" in diff
    assert "+    return a + b" in diff

    # Validate patch against git apply syntax
    val_res = validate_patch(diff)
    assert val_res.is_valid is True
    assert val_res.file_count == 1
    assert val_res.hunk_count == 1
    assert len(val_res.errors) == 0

    # In-memory application test
    patched = apply_patch_in_memory(original, diff)
    assert patched == replacement


def test_validate_patch_detects_corrupted_hunk_header():
    # Corrupt hunk header: specifies 5 lines but provides only 2
    corrupt_diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,5 +1,5 @@\n"
        "-bad\n"
        "+good\n"
    )
    val = validate_patch(corrupt_diff)
    assert val.is_valid is False
    assert any("Header specified" in err for err in val.errors)


def test_extract_unified_diff_from_markdown():
    text_with_diff = (
        "Here is the synthesized fix for the security flaw:\n\n"
        "```diff\n"
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -10,3 +10,3 @@\n"
        " def check():\n"
        "-    pass\n"
        "+    return True\n"
        "```\n\n"
        "Please apply this patch."
    )
    extracted = extract_unified_diff(text_with_diff)
    assert extracted is not None
    assert extracted.startswith("diff --git a/app.py b/app.py")
    assert "+    return True" in extracted


# =============================================================================
# 4. OASIS SARIF v2.1.0 Diagnostics Tests
# =============================================================================

def test_sarif_v210_report_schema_compliance(tmp_path: Path):
    findings = [
        Finding(
            file_path="src/auth/jwt.py",
            line_start=42,
            line_end=45,
            severity="CRITICAL",
            category="security",
            issue="Unvalidated JWT algorithm parameter allows alg:none bypass",
            proof_or_refutation="jwt.decode() lacks algorithms parameter",
            suggested_replacement="jwt.decode(token, key, algorithms=['RS256'])",
        ),
        Finding(
            file_path="src/cache.py",
            line_start=15,
            line_end=18,
            severity="MEDIUM",
            category="performance",
            issue="Unbounded in-memory dict cache leads to memory exhaustion",
            proof_or_refutation="No eviction policy implemented",
        ),
    ]

    report = SarifReport(findings=findings)
    doc = report.to_dict()

    # Schema & Version validation
    assert doc["$schema"] == SARIF_SCHEMA_URI
    assert doc["version"] == SARIF_VERSION
    assert len(doc["runs"]) == 1

    driver = doc["runs"][0]["tool"]["driver"]
    assert driver["name"] == "jury-mcp"
    assert driver["version"] == "3.0.0"
    assert len(driver["rules"]) == 2

    # Verify severity mapping
    results = doc["runs"][0]["results"]
    assert len(results) == 2
    assert results[0]["level"] == "error"    # CRITICAL -> error
    assert results[1]["level"] == "warning"  # MEDIUM -> warning

    # Verify physical location
    loc0 = results[0]["locations"][0]["physicalLocation"]
    assert loc0["artifactLocation"]["uri"] == "src/auth/jwt.py"
    assert loc0["region"]["startLine"] == 42
    assert loc0["region"]["endLine"] == 45

    # Verify automated fix inclusion
    assert "fixes" in results[0]
    assert results[0]["fixes"][0]["fileChanges"][0]["replacements"][0]["insertedContent"]["text"] == "jwt.decode(token, key, algorithms=['RS256'])"

    # Test file persistence
    sarif_file = tmp_path / "results.sarif"
    report.save_to_file(sarif_file)
    assert sarif_file.is_file()
    loaded_json = json.loads(sarif_file.read_text(encoding="utf-8"))
    assert loaded_json["version"] == "2.1.0"


# =============================================================================
# 5. Multi-Model DeliberationMesh Protocol Tests
# =============================================================================

@pytest.fixture
def mock_deliberation_mesh(tmp_path: Path):
    """Fixture providing an isolated DeliberationMesh with mock adapters and state engine."""
    db_file = tmp_path / ".jury" / "test_jury_mesh.db"
    engine = JuryStateEngine(db_path=db_file)
    registry = AdapterRegistry(mock_mode=True, state_engine=engine)

    # Pre-configure deterministic fixture responses for roles
    registry.register_mock_response(
        "alpha",
        json.dumps({
            "review_score": 8.5,
            "readiness_score": 8.5,
            "verdict": "APPROVED",
            "findings": [],
            "summary": "Alpha approves architecture.",
        }),
    )
    registry.register_mock_response(
        "beta",
        json.dumps({
            "review_score": 8.0,
            "readiness_score": 8.0,
            "verdict": "APPROVED",
            "findings": [
                {
                    "file_path": "src/api.py",
                    "line_start": 20,
                    "line_end": 22,
                    "severity": "HIGH",
                    "category": "security",
                    "issue": "Missing authorization check on endpoint",
                    "proof_or_refutation": "Line 20 does not call require_auth()",
                    "suggested_replacement": "@require_auth\ndef get_data():",
                }
            ],
            "challenges": [],
            "summary": "Beta flagged authorization gap.",
        }),
    )
    registry.register_mock_response(
        "gamma",
        json.dumps({
            "review_score": 9.0,
            "readiness_score": 9.0,
            "verdict": "APPROVED",
            "findings": [],
            "summary": "Gamma verifies optimal performance.",
        }),
    )
    registry.register_mock_response(
        "arbiter",
        json.dumps({
            "verdict": "APPROVED_WITH_CONDITIONS",
            "consensus_score": 0.88,
            "rationale": "Council approves patch with condition that endpoint auth is enforced.",
            "surviving_findings": [
                {
                    "file_path": "src/api.py",
                    "line_start": 20,
                    "line_end": 22,
                    "severity": "HIGH",
                    "category": "security",
                    "issue": "Missing authorization check on endpoint",
                    "proof_or_refutation": "Confirmed in cross-examination",
                }
            ],
            "unified_patch": (
                "diff --git a/src/api.py b/src/api.py\n"
                "--- a/src/api.py\n"
                "+++ b/src/api.py\n"
                "@@ -19,3 +19,4 @@\n"
                " # Endpoint\n"
                "+@require_auth\n"
                " def get_data():\n"
                "     return {}\n"
            ),
            "verification_contract": {
                "runner": "pytest",
                "test_paths": ["tests/test_api.py"],
                "allowed_flags": ["-v"],
            },
        }),
    )

    mesh = DeliberationMesh(
        state_engine=engine,
        registry=registry,
        mock_mode=True,
    )
    return mesh, engine


def test_deliberation_mesh_review_lifecycle(mock_deliberation_mesh, tmp_path: Path):
    mesh, engine = mock_deliberation_mesh

    req = ReviewRequest(
        task_intent="Refactor authentication endpoints",
        diff_text="diff --git a/src/api.py b/src/api.py\n+def get_data():\n+    return {}\n",
        repo_root=tmp_path,
        use_cache=True,
    )

    # Execution 1: Cold deliberation run
    verdict1: ReviewVerdict = asyncio.run(mesh.review(req))

    assert verdict1.verdict == "APPROVED_WITH_CONDITIONS"
    assert verdict1.cache_hit is False
    assert len(verdict1.findings) == 1
    assert verdict1.findings[0].file_path == "src/api.py"
    assert verdict1.sarif_json is not None
    assert verdict1.sarif_json["version"] == "2.1.0"
    assert verdict1.verification_contract is not None
    assert verdict1.verification_contract.runner == "pytest"
    assert verdict1.unified_patch is not None

    # Execution 2: Cache hit test
    verdict2: ReviewVerdict = asyncio.run(mesh.review(req))
    assert verdict2.cache_hit is True
    assert verdict2.verdict == verdict1.verdict
    assert verdict2.duration_ms < 1000  # Instant cache return

    # Verify audit ledger recorded the deliberation
    audit_logs = engine.get_audit_logs(limit=10)
    assert len(audit_logs) >= 1
    assert audit_logs[0].command == "review"
    assert audit_logs[0].verdict == "APPROVED_WITH_CONDITIONS"


def test_deliberation_mesh_plan_review(mock_deliberation_mesh, tmp_path: Path):
    mesh, engine = mock_deliberation_mesh

    req = PlanRequest(
        task_intent="Migrate SQLite to distributed Raft consensus",
        plan_text="Phase 1: Abstract storage engine.\nPhase 2: Add Raft node network layer.",
        repo_root=tmp_path,
        use_cache=True,
    )

    plan_verdict: PlanVerdict = asyncio.run(mesh.plan(req))
    assert plan_verdict.verdict in ("APPROVED", "APPROVED_WITH_CONDITIONS")
    assert plan_verdict.readiness_score > 7.0
    assert plan_verdict.cache_hit is False
    assert "Implementation Plan Deliberation Verdict" in plan_verdict.deliberation_markdown


def test_deliberation_mesh_ask_consultation(mock_deliberation_mesh, tmp_path: Path):
    mesh, engine = mock_deliberation_mesh

    # Register Ask response
    mesh.registry.register_mock_response(
        "alpha",
        json.dumps({
            "recommended_pattern": "Two-Phase Commit with Leaky Bucket",
            "confidence": 0.94,
            "tradeoff_matrix": [
                {"option": "2PC", "latency": "Low", "verdict": "Selected"}
            ],
            "concrete_code_example": "class TwoPhaseLimiter:\n    pass",
            "failure_recovery_strategy": "Rollback on timeout",
        }),
    )

    req = AskRequest(
        query="What is the optimal concurrency limiter for zero resident process overhead?",
        repo_root=tmp_path,
    )

    ask_verdict: AskVerdict = asyncio.run(mesh.ask(req))
    assert ask_verdict.verdict == "APPROVED"
    assert "Two-Phase" in ask_verdict.recommended_pattern
    assert ask_verdict.confidence >= 0.90
    assert "TwoPhaseLimiter" in ask_verdict.concrete_code_example


def test_deliberation_mesh_quorum_collapse(tmp_path: Path):
    """Verify that if panel jurors fail or crash below Q_min, mesh halts with QUORUM_COLLAPSE."""
    db_file = tmp_path / "test_quorum_collapse.db"
    engine = JuryStateEngine(db_path=db_file)
    registry = AdapterRegistry(mock_mode=True, state_engine=engine)

    # Simulate catastrophic provider outages: Alpha and Beta crash
    registry.register_adapter(
        "alpha",
        MockLLMAdapter(model="glm", provider="constellation", simulate_error=RuntimeError("Gateway timeout")),
    )
    registry.register_adapter(
        "beta",
        MockLLMAdapter(model="qwen", provider="constellation", simulate_error=RuntimeError("HTTP 500")),
    )
    # Only Gamma succeeds
    registry.register_adapter(
        "gamma",
        MockLLMAdapter(
            model="llama",
            provider="groq",
            default_response=json.dumps({"review_score": 8.0, "verdict": "APPROVED"}),
        ),
    )

    mesh = DeliberationMesh(
        state_engine=engine,
        registry=registry,
        mock_mode=False,
    )

    req = ReviewRequest(
        task_intent="Security audit with collapsed quorum",
        diff_text="diff --git a/test.py b/test.py\n+x = 1\n",
        repo_root=tmp_path,
        use_cache=False,
    )

    verdict = asyncio.run(mesh.review(req))
    assert verdict.verdict == "QUORUM_COLLAPSE"
    assert verdict.error_code == "ERR_QUORUM_COLLAPSE"
    assert "Quorum Collapse" in verdict.deliberation_markdown


# =============================================================================
# 6. Additional Invariant & Boundary Tests
# =============================================================================

def test_decoupled_arbiter_assertion_raises_on_family_overlap():
    """Verify ArbiterCouplingError is raised if Arbiter overlaps with panelist family."""
    # Arbiter is DeepSeek
    arbiter = DecoupledArbiterAdapter(model="deepseek/deepseek-chat")

    # Panel has GLM, Qwen, Llama -> OK
    assert arbiter.assert_decoupled_from(["glm-5.3", "qwen-3.8", "llama-3.3"]) is True

    # If panel also includes DeepSeek -> VIOLATION
    with pytest.raises(ArbiterCouplingError) as exc:
        arbiter.assert_decoupled_from(["glm-5.3", "deepseek-coder", "llama-3.3"])
    assert "Decoupled Arbiter mandate violated" in str(exc.value)


def test_multi_file_patch_synthesis_and_validation():
    """Verify multi-file patch parsing, synthesis, and validation."""
    orig1 = "def one():\n    return 1\n"
    repl1 = "def one():\n    return 10\n"

    orig2 = "class Service:\n    def run(self):\n        pass\n"
    repl2 = "class Service:\n    def run(self):\n        return True\n"

    diff1 = synthesize_unified_diff("src/one.py", orig1, repl1)
    diff2 = synthesize_unified_diff("src/two.py", orig2, repl2)
    combined = diff1 + diff2

    parsed_files = parse_unified_diff(combined)
    assert len(parsed_files) == 2
    assert parsed_files[0].orig_path == "src/one.py"
    assert parsed_files[1].orig_path == "src/two.py"

    val = validate_patch(combined)
    assert val.is_valid is True
    assert val.file_count == 2
    assert val.hunk_count == 2


def test_apply_patch_in_memory_multi_hunk():
    """Verify in-memory application on multiple hunks across a file."""
    original = (
        "line 1\n"
        "line 2\n"
        "line 3\n"
        "line 4\n"
        "line 5\n"
        "line 6\n"
        "line 7\n"
        "line 8\n"
        "line 9\n"
        "line 10\n"
    )
    # Modify line 2 and line 9
    replacement = (
        "line 1\n"
        "line 2 MODIFIED\n"
        "line 3\n"
        "line 4\n"
        "line 5\n"
        "line 6\n"
        "line 7\n"
        "line 8\n"
        "line 9 MODIFIED\n"
        "line 10\n"
    )

    diff = synthesize_unified_diff("test.txt", original, replacement, context_lines=1)
    val = validate_patch(diff)
    assert val.is_valid is True

    result = apply_patch_in_memory(original, diff)
    assert result == replacement


def test_sarif_rule_categorization_and_helpers():
    """Verify rule category mapping and severity levels."""
    assert severity_to_sarif_level("CRITICAL") == "error"
    assert severity_to_sarif_level("HIGH") == "error"
    assert severity_to_sarif_level("MEDIUM") == "warning"
    assert severity_to_sarif_level("LOW") == "note"
    assert severity_to_sarif_level("INFO") == "note"

    findings = [
        Finding(
            file_path="src/race.py",
            line_start=12,
            line_end=14,
            severity="HIGH",
            category="concurrency",
            issue="Data race detected between reader and writer thread",
        ),
        Finding(
            file_path="src/slow.py",
            line_start=50,
            line_end=55,
            severity="MEDIUM",
            category="performance",
            issue="Quadratic nested iteration over database cursor",
        ),
    ]

    sarif = generate_sarif_report(findings, tool_name="custom-jury", tool_version="3.1.0")
    assert sarif["runs"][0]["tool"]["driver"]["name"] == "custom-jury"
    rules = sarif["runs"][0]["tool"]["driver"]["rules"]
    assert any("CONC" in r["id"] for r in rules)
    assert any("PERF" in r["id"] for r in rules)


def test_check_quorum_with_invalid_and_negative_scores():
    """Verify that ballots with missing verdicts are filtered from quorum."""
    b_invalid1 = JurorBallot(
        juror_id="alpha",
        model="glm",
        provider="constellation",
        role_description="Architect",
        score=0.0,
        verdict="",  # Empty verdict: invalid
        content_raw="",
    )
    b_invalid2 = JurorBallot(
        juror_id="beta",
        model="qwen",
        provider="constellation",
        role_description="Adversary",
        score=7.0,
        verdict="",  # Empty verdict: invalid
        content_raw="",
    )
    b_valid = JurorBallot(
        juror_id="gamma",
        model="llama",
        provider="groq",
        role_description="Performance",
        score=8.5,
        verdict="APPROVED",
        content_raw="OK",
    )

    # 1 valid ballot remaining -> fails quorum ($Q_{min}=2$)
    q = check_quorum([b_invalid1, b_invalid2, b_valid], min_quorum=2)
    assert q.is_valid is False
    assert q.operational_jurors == 1
    assert q.error_code == "ERR_QUORUM_COLLAPSE"
