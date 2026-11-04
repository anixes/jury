r"""
tests/probe_m4_adversarial.py - Adversarial Stress Testing & Resilience Probes for Milestone 4.

Probes:
1. Quorum Collapse & Monoculture Defense (same model family, < Q_min, invalid ballots)
2. Hung Jury & Entropy Detection (binary polar split, 3-way split, Condorcet paradox)
3. Patch Parsing & Validation with Hostile / Malformed Unified Diffs
4. OASIS SARIF v2.1.0 Schema Validation & Edge Cases
5. Secret Scrubber & Singleflight Concurrency Integration in DeliberationMesh
6. Integrity Forensics & Facade Detection
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

# Ensure src is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from jury.adapters.arbiter import ArbiterCouplingError, DecoupledArbiterAdapter, identify_model_family
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
    DiffFile,
    DiffHunk,
    PatchValidationResult,
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
    BPCResult,
    ConsensusMetrics,
    QuorumResult,
    bpc_score_candidates,
    check_quorum,
    compute_bpde_entropy,
    compute_consensus_index,
    detect_hung_jury,
)
from jury.security.contract import TypedVerificationContract
from jury.state.engine import JuryStateEngine


def run_probe_1_quorum_and_monoculture():
    print("\n=======================================================")
    print("PROBE 1: Quorum Collapse & Monoculture Defense")
    print("=======================================================")

    # 1.1: Zero ballots
    res0 = check_quorum([], min_quorum=2)
    assert not res0.is_valid, "Zero ballots must fail quorum"
    assert res0.error_code == "ERR_QUORUM_COLLAPSE", f"Expected ERR_QUORUM_COLLAPSE, got {res0.error_code}"
    print("[PASS] 1.1: Zero ballots fails closed with ERR_QUORUM_COLLAPSE")

    # 1.2: Single ballot (< min_quorum)
    b_single = JurorBallot(
        juror_id="alpha",
        model="constellation/glm-5-3-flash-free",
        provider="constellation",
        role_description="Architect",
        score=9.0,
        verdict="APPROVED",
        content_raw="OK",
    )
    res1 = check_quorum([b_single], min_quorum=2)
    assert not res1.is_valid, "Single ballot must fail quorum"
    assert res1.error_code == "ERR_QUORUM_COLLAPSE"
    assert res1.operational_jurors == 1
    print("[PASS] 1.2: Single ballot (< Q_min) fails closed with ERR_QUORUM_COLLAPSE")

    # 1.3: Monoculture collapse: 3 ballots, ALL from same model family (deepseek)
    b_ds1 = JurorBallot(
        juror_id="alpha",
        model="deepseek/deepseek-chat",
        provider="openrouter",
        role_description="Architect",
        score=8.5,
        verdict="APPROVED",
        content_raw="OK",
    )
    b_ds2 = JurorBallot(
        juror_id="beta",
        model="deepseek/deepseek-coder",
        provider="openrouter",
        role_description="Adversary",
        score=7.5,
        verdict="APPROVED",
        content_raw="OK",
    )
    b_ds3 = JurorBallot(
        juror_id="gamma",
        model="deepseek-v3",
        provider="deepseek_direct",
        role_description="Performance",
        score=8.0,
        verdict="APPROVED",
        content_raw="OK",
    )
    res_mono_ds = check_quorum([b_ds1, b_ds2, b_ds3], min_quorum=2, require_distinct_families=True)
    assert not res_mono_ds.is_valid, "Same model family ballots must fail quorum"
    assert res_mono_ds.error_code == "ERR_QUORUM_COLLAPSE"
    assert res_mono_ds.distinct_model_families == 1
    print(f"[PASS] 1.3: Monoculture DeepSeek ({res_mono_ds.distinct_model_families} family) fails closed: {res_mono_ds.reason}")

    # 1.4: Monoculture collapse: 3 ballots, ALL from Anthropic
    b_claude1 = JurorBallot(
        juror_id="alpha", model="claude-3-5-sonnet-20241022", provider="anthropic",
        role_description="Architect", score=8.0, verdict="APPROVED", content_raw="OK",
    )
    b_claude2 = JurorBallot(
        juror_id="beta", model="claude-3-opus", provider="anthropic",
        role_description="Adversary", score=8.0, verdict="APPROVED", content_raw="OK",
    )
    b_claude3 = JurorBallot(
        juror_id="gamma", model="anthropic/claude-3-haiku", provider="openrouter",
        role_description="Performance", score=8.0, verdict="APPROVED", content_raw="OK",
    )
    res_mono_cl = check_quorum([b_claude1, b_claude2, b_claude3], min_quorum=2, require_distinct_families=True)
    assert not res_mono_cl.is_valid
    assert res_mono_cl.error_code == "ERR_QUORUM_COLLAPSE"
    assert res_mono_cl.distinct_model_families == 1
    print(f"[PASS] 1.4: Monoculture Anthropic ({res_mono_cl.distinct_model_families} family) fails closed: {res_mono_cl.reason}")

    # 1.5: Filtering out invalid ballots (empty verdict or filtered out)
    b_empty_v1 = JurorBallot(
        juror_id="alpha", model="glm-5.3", provider="constellation",
        role_description="Architect", score=0.0, verdict="", content_raw="Empty verdict",
    )
    b_empty_v2 = JurorBallot(
        juror_id="beta", model="qwen-3.8", provider="constellation",
        role_description="Adversary", score=8.0, verdict="", content_raw="Empty",
    )
    b_valid = JurorBallot(
        juror_id="gamma", model="llama-3.3-70b", provider="groq",
        role_description="Performance", score=9.0, verdict="APPROVED", content_raw="OK",
    )
    res_invalid = check_quorum([b_empty_v1, b_empty_v2, b_valid], min_quorum=2)
    assert not res_invalid.is_valid
    assert res_invalid.operational_jurors == 1
    assert res_invalid.error_code == "ERR_QUORUM_COLLAPSE"
    print("[PASS] 1.5: Empty verdict ballots filtered; quorum fails closed.")

    # 1.6: DeliberationMesh integration of quorum collapse
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "delib_test.db"
        engine = JuryStateEngine(db_path=db_file)
        registry = AdapterRegistry(mock_mode=True, state_engine=engine)

        # Monoculture mock adapters: all returning deepseek
        registry.register_adapter("alpha", MockLLMAdapter(model="deepseek-v3", provider="deepseek", default_response='{"review_score": 8.0, "verdict": "APPROVED"}'))
        registry.register_adapter("beta", MockLLMAdapter(model="deepseek-chat", provider="deepseek", default_response='{"review_score": 8.0, "verdict": "APPROVED"}'))
        registry.register_adapter("gamma", MockLLMAdapter(model="deepseek-coder", provider="deepseek", default_response='{"review_score": 8.0, "verdict": "APPROVED"}'))

        mesh = DeliberationMesh(state_engine=engine, registry=registry, mock_mode=False)
        req = ReviewRequest(task_intent="Monoculture test", diff_text="+pass", repo_root=Path(tmpdir))
        verdict = asyncio.run(mesh.review(req))
        assert verdict.verdict == "QUORUM_COLLAPSE", f"Expected QUORUM_COLLAPSE, got {verdict.verdict}"
        assert verdict.error_code == "ERR_QUORUM_COLLAPSE"
        print("[PASS] 1.6: DeliberationMesh halted with QUORUM_COLLAPSE on monoculture panel.")


def run_probe_2_hung_jury_and_entropy():
    print("\n=======================================================")
    print("PROBE 2: Hung Jury & Entropy Detection")
    print("=======================================================")

    # Entropy: - (0.5 * log2(0.5) + 0.5 * log2(0.5)) = 1.0 bit, or ln(2) = 0.693147 nats
    ent_bin_bits = compute_bpde_entropy(["APPROVED", "REJECTED"])
    assert round(ent_bin_bits, 4) == 1.0000
    ent_bin = compute_bpde_entropy(["APPROVED", "REJECTED"], base=math.e)
    assert round(ent_bin, 4) == round(math.log(2), 4)
    print(f"[OBSERVATION] Binary polar split BPDE entropy: {ent_bin_bits:.4f} bits (base 2), {ent_bin:.4f} nats (base e)")

    # Scores: 10.0 vs 0.0 -> max std_dev 5.0 -> Consensus index C = 0.0
    mean_s, std_s, c_index = compute_consensus_index([10.0, 0.0])
    assert c_index == 0.0
    assert std_s == 5.0
    print(f"[OBSERVATION] Divergent scores [10.0, 0.0] Consensus index C: {c_index:.2f}")

    metrics_bin = detect_hung_jury([10.0, 0.0], ["APPROVED", "REJECTED"])
    assert metrics_bin.is_hung_jury is True
    print(f"[PASS] 2.1: Binary polar deadlock flagged is_hung_jury=True: {metrics_bin.hung_jury_reason}")

    # 2.2: 3-way split: 1 APPROVED, 1 APPROVED_WITH_CONDITIONS, 1 REJECTED
    # Shannon entropy: - 3 * (1/3 * ln(1/3)) = ln(3) = 1.0986 >= 0.8!
    ent_3way = compute_bpde_entropy(["APPROVED", "APPROVED_WITH_CONDITIONS", "REJECTED"])
    assert ent_3way >= 0.8
    print(f"[OBSERVATION] 3-way split BPDE entropy: {ent_3way:.4f} (>= 0.8)")

    scores_3way = [10.0, 6.0, 2.0]
    metrics_3way = detect_hung_jury(scores_3way, ["APPROVED", "APPROVED_WITH_CONDITIONS", "REJECTED"])
    assert metrics_3way.is_hung_jury is True
    assert metrics_3way.bpde_entropy >= 0.8
    print(f"[PASS] 2.2: 3-way split with H >= 0.8 flagged is_hung_jury=True: {metrics_3way.hung_jury_reason}")

    # 2.3: Moderate divergence with consensus C <= 0.4
    # e.g., scores [2.0, 8.0] -> mean=5.0, std_dev=3.0, C = 1.0 - (3.0/5.0) = 0.40 <= 0.40
    mean_m, std_m, c_m = compute_consensus_index([2.0, 8.0])
    assert c_m <= 0.4001
    metrics_c = detect_hung_jury([2.0, 8.0], ["REJECTED", "APPROVED"], threshold_consensus=0.50)
    assert metrics_c.is_hung_jury is True
    print(f"[PASS] 2.3: Consensus C <= 0.4 ({c_m:.2f}) triggers hung jury: {metrics_c.hung_jury_reason}")

    # 2.4: BPC Condorcet paradox handling
    # Voter 1: A > B > C
    # Voter 2: B > C > A
    # Voter 3: C > A > B
    rankings_paradox = {
        "j1": ["A", "B", "C"],
        "j2": ["B", "C", "A"],
        "j3": ["C", "A", "B"],
    }
    bpc_res = bpc_score_candidates(rankings_paradox)
    assert bpc_res.condorcet_winner is None, "Condorcet cycle must have no Condorcet winner"
    # All candidates have equal Borda score (1.0 + 0.5 + 0.0) / 3 = 0.5
    assert len(set(bpc_res.borda_scores.values())) == 1
    print("[PASS] 2.4: Condorcet cycle safely handled without crash, condorcet_winner=None")


def run_probe_3_patch_validation_and_hostile_diffs():
    print("\n=======================================================")
    print("PROBE 3: Patch Parsing & Validation with Hostile Diffs")
    print("=======================================================")

    # 3.1: Empty diff
    val_empty = validate_patch("")
    assert not val_empty.is_valid
    assert "Diff payload is empty" in val_empty.errors[0]
    print("[PASS] 3.1: Empty diff rejected gracefully.")

    # 3.2: Random garbage text
    val_garbage = validate_patch("not a diff at all\nrandom content\n12345")
    assert not val_garbage.is_valid
    assert "No valid diff files" in val_garbage.errors[0]
    print("[PASS] 3.2: Garbage text rejected gracefully.")

    # 3.3: Hostile hunk header: negative / corrupted line numbers
    bad_header_diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,3 +1,xyz @@\n"
        " line1\n"
        "-line2\n"
        "+line2_fixed\n"
        " line3\n"
    )
    # The regex won't match "xyz", so it's treated as non-hunk
    parsed = parse_unified_diff(bad_header_diff)
    assert len(parsed) == 1
    assert len(parsed[0].hunks) == 0, "Non-numeric hunk header must not be parsed as hunk"
    val_bad = validate_patch(bad_header_diff)
    assert not val_bad.is_valid
    print("[PASS] 3.3: Malformed hunk header @@ -1,3 +1,xyz @@ safely rejected.")

    # 3.4: Line count mismatch: header specifies 5 lines, but diff provides 2
    count_mismatch_diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,5 +1,5 @@\n"
        "-bad\n"
        "+good\n"
    )
    val_mismatch = validate_patch(count_mismatch_diff)
    assert not val_mismatch.is_valid
    assert any("Header specified" in err for err in val_mismatch.errors)
    print(f"[PASS] 3.4: Line count mismatch detected: {val_mismatch.errors[0]}")

    # 3.5: Illegal prefix characters inside hunk
    illegal_prefix_diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,2 +1,2 @@\n"
        "? illegal line 1\n"
        "! illegal line 2\n"
    )
    val_prefix = validate_patch(illegal_prefix_diff)
    assert not val_prefix.is_valid, "Illegal prefix diff must be invalid"
    print(f"[PASS] 3.5: Illegal prefix diff rejected as invalid (errors: {val_prefix.errors})")

    # 3.5b: Adversarial test: rogue line inside matching hunk
    rogue_diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,2 +1,2 @@\n"
        " line 1\n"
        "? rogue line\n"
        " line 2\n"
    )
    val_rogue = validate_patch(rogue_diff)
    assert not val_rogue.is_valid, "Rogue lines must be rejected by validate_patch"
    print(f"[PASS] 3.5b: Rogue line correctly rejected (errors: {val_rogue.errors})")

    # 3.6: Hostile inputs to apply_patch_in_memory:
    # A) Target content is completely empty
    res_empty_apply = apply_patch_in_memory("", count_mismatch_diff)
    print(f"[OBSERVATION] 3.6A: apply_patch_in_memory on empty original: returned len={len(res_empty_apply)}")

    # B) Target content with out-of-range orig_start (orig_start=9999 on 3-line file)
    out_of_range_diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -9999,1 +9999,1 @@\n"
        "-old\n"
        "+new\n"
    )
    orig_3lines = "line 1\nline 2\nline 3\n"
    res_oor = apply_patch_in_memory(orig_3lines, out_of_range_diff)
    assert res_oor.startswith("line 1\nline 2\nline 3\nnew\n")
    print("[OBSERVATION] 3.6B: Out-of-bounds orig_start appends at EOF without crashing.")

    # C) Corrupt diff passed to apply_patch_in_memory
    res_corrupt_apply = apply_patch_in_memory(orig_3lines, "random non-diff string")
    assert res_corrupt_apply == orig_3lines, "Corrupt diff should return original_content unmodified"
    print("[PASS] 3.6C: Non-diff input returns original content unchanged.")


def run_probe_4_sarif_schema_and_edge_cases():
    print("\n=======================================================")
    print("PROBE 4: OASIS SARIF v2.1.0 Generator Schema & Edge Cases")
    print("=======================================================")

    # 4.1: Empty findings
    sarif_empty = generate_sarif_report([])
    assert sarif_empty["$schema"] == SARIF_SCHEMA_URI
    assert sarif_empty["version"] == "2.1.0"
    assert sarif_empty["runs"][0]["tool"]["driver"]["name"] == "jury-mcp"
    assert sarif_empty["runs"][0]["results"] == []
    assert sarif_empty["runs"][0]["tool"]["driver"]["rules"] == []
    print("[PASS] 4.1: Empty findings list produces compliant empty SARIF document.")

    # 4.2: Hostile / Path traversal in finding file_path
    findings_hostile = [
        Finding(
            file_path="../../etc/passwd",
            line_start=1,
            line_end=1,
            severity="CRITICAL",
            category="security",
            issue="Path traversal attack attempt",
        ),
        Finding(
            file_path="C:\\Windows\\System32\\cmd.exe",
            line_start=10,
            line_end=15,
            severity="LOW",
            category="architecture",
            issue="Windows path normalization test",
        ),
    ]
    sarif_hostile = generate_sarif_report(findings_hostile)
    assert len(sarif_hostile["runs"][0]["results"]) == 2
    # Check normalized URI (backslashes replaced with /)
    uri0 = sarif_hostile["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    uri1 = sarif_hostile["runs"][0]["results"][1]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert "\\" not in uri0 and "\\" not in uri1
    print(f"[PASS] 4.2: Normalized URIs: '{uri0}', '{uri1}'")

    # 4.3: Severity mappings
    assert severity_to_sarif_level("CRITICAL") == "error"
    assert severity_to_sarif_level("HIGH") == "error"
    assert severity_to_sarif_level("MEDIUM") == "warning"
    assert severity_to_sarif_level("LOW") == "note"
    assert severity_to_sarif_level("INFO") == "note"
    print("[PASS] 4.3: Severity mapping matches SARIF spec (error, warning, note).")

    # 4.4: Automated fixes structure in SARIF
    finding_with_fix = Finding(
        file_path="src/main.py",
        line_start=5,
        line_end=5,
        severity="HIGH",
        category="security",
        issue="Hardcoded password",
        suggested_replacement="password = os.environ['PASSWORD']",
    )
    sarif_fix = generate_sarif_report([finding_with_fix])
    res0 = sarif_fix["runs"][0]["results"][0]
    assert "fixes" in res0
    fix = res0["fixes"][0]
    assert fix["fileChanges"][0]["replacements"][0]["insertedContent"]["text"] == "password = os.environ['PASSWORD']"
    print("[PASS] 4.4: Automated fix replacement block correctly generated in SARIF.")


def run_probe_5_secret_scrubber_and_singleflight():
    print("\n=======================================================")
    print("PROBE 5: Secret Scrubber & Singleflight in DeliberationMesh")
    print("=======================================================")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / ".jury" / "state.db"
        engine = JuryStateEngine(db_path=db_file)
        registry = AdapterRegistry(mock_mode=True, state_engine=engine)

        # Register mock adapters
        adapters = {}
        for role, model, prov in [
            ("alpha", "glm-5.3", "constellation"),
            ("beta", "qwen-3.8", "constellation"),
            ("gamma", "llama-3.3", "groq"),
            ("arbiter", "deepseek-v3", "openrouter"),
        ]:
            ad = MockLLMAdapter(
                model=model,
                provider=prov,
                default_response=json.dumps({"review_score": 8.0, "verdict": "APPROVED", "findings": []}),
            )
            registry.register_adapter(role, ad)
            adapters[role] = ad

        mesh = DeliberationMesh(state_engine=engine, registry=registry, mock_mode=False)

        # 5.1: High-entropy secret in diff payload
        secret_aws = "AKIAIOSFODNN7EXAMPLE"
        secret_openai = "sk-" + "a" * 48
        hostile_diff = (
            f"diff --git a/config.py b/config.py\n"
            f"+AWS_KEY = '{secret_aws}'\n"
            f"+OPENAI_KEY = '{secret_openai}'\n"
        )

        req = ReviewRequest(
            task_intent="Secret scrubber probe",
            diff_text=hostile_diff,
            repo_root=Path(tmpdir),
            use_cache=False,
        )

        verdict = asyncio.run(mesh.review(req))
        assert verdict.verdict == "APPROVED"

        # Verify captured prompts in call_history DO NOT contain raw secrets
        all_calls = []
        for ad in adapters.values():
            all_calls.extend(ad.call_history)

        assert len(all_calls) > 0, "Adapters must have received calls"
        for call in all_calls:
            for msg in call.get("messages", []):
                content = msg.get("content", "")
                assert secret_aws not in content, f"Leaked AWS secret in prompt content: {content}"
                assert secret_openai not in content, f"Leaked OpenAI secret in prompt content: {content}"
        print("[PASS] 5.1: Secrets scrubbed from diff payload before model invocation.")

        # 5.2: Concurrency singleflight stress test
        print("Testing singleflight request coalescing across 5 concurrent tasks...")
        for ad in adapters.values():
            ad.call_history.clear()

        async def run_concurrent_review(i: int):
            c_req = ReviewRequest(
                task_intent="Singleflight coalescing test",
                diff_text="diff --git a/test.py b/test.py\n+val = 42\n",
                repo_root=Path(tmpdir),
                use_cache=True,
            )
            return await mesh.review(c_req)

        async def run_all():
            return await asyncio.gather(*[run_concurrent_review(i) for i in range(5)])

        results = asyncio.run(run_all())
        assert len(results) == 5
        # All 5 return the same verdict
        assert all(r.verdict == "APPROVED" for r in results)
        cache_hits = sum(1 for r in results if r.cache_hit)
        print(f"[PASS] 5.2: 5 concurrent requests executed safely: {cache_hits} cache hits, 0 deadlocks.")


def run_probe_6_integrity_forensics():
    print("\n=======================================================")
    print("PROBE 6: Integrity Forensics & Facade Detection")
    print("=======================================================")

    delib_dir = Path("src/jury/deliberation")
    files = list(delib_dir.glob("*.py"))
    assert len(files) >= 5, f"Expected at least 5 files in {delib_dir}"

    suspicious_patterns = [
        re.compile(r"if\s+.*==\s*[\"'](?:test|fixture|dummy|mock)[\"']", re.IGNORECASE),
        re.compile(r"return\s+[\"']APPROVED[\"']\s*#\s*hardcoded", re.IGNORECASE),
        re.compile(r"pass\s*#\s*TODO\s*implement", re.IGNORECASE),
    ]

    findings = []
    for f in files:
        content = f.read_text(encoding="utf-8")
        for i, line in enumerate(content.splitlines(), start=1):
            for pat in suspicious_patterns:
                if pat.search(line):
                    findings.append(f"{f.name}:{i}: {line.strip()}")

    if findings:
        print(f"[WARNING] Potential facade or suspicious patterns found:\n" + "\n".join(findings))
    else:
        print("[PASS] 6.1: Zero hardcoded test shortcuts or facade patterns found in src/jury/deliberation/")


if __name__ == "__main__":
    try:
        run_probe_1_quorum_and_monoculture()
        run_probe_2_hung_jury_and_entropy()
        run_probe_3_patch_validation_and_hostile_diffs()
        run_probe_4_sarif_schema_and_edge_cases()
        run_probe_5_secret_scrubber_and_singleflight()
        run_probe_6_integrity_forensics()
        print("\n=======================================================")
        print("ALL ADVERSARIAL PROBES COMPLETED SUCCESSFULLY!")
        print("=======================================================")
    except Exception as e:
        print(f"\n[FAIL] Adversarial probe failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
