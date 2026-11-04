r"""
src/jury/deliberation - Multi-Model Deliberation Engine & Synthesis Protocol.

Implements Milestone 4 (Features 15-19) for Jury Mesh (`jury-mcp`) v3.0:
- 3-stage dialectical falsification protocol
- Specialized multi-model rubrics (Alpha, Beta, Gamma, Arbiter)
- Fail-closed quorum evaluation ($Q_{min} \ge 2$)
- Balanced Position Calibration (BPC) 2-pass permutation
- Normalized Borda count with self-vote exclusion
- BPDE hung-jury entropy detection
- Machine-actionable POSIX patch emitter (`diff --git`)
- OASIS SARIF v2.1.0 diagnostics generator
- Interface Contract #4 compliance (`DeliberationService`, `DeliberationMesh`)
"""

from jury.deliberation.mesh import (
    AskRequest,
    AskVerdict,
    DeliberationMesh,
    DeliberationService,
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
    CriticalRisk,
    CrossExamChallenge,
    CrossExamResult,
    DeliberationVerdict,
    Finding,
    JurorBallot,
    PlanBallot,
    ReviewBallot,
    SeverityLevel,
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
    QuorumError,
    QuorumResult,
    bpc_score_candidates,
    check_quorum,
    compute_bpde_entropy,
    compute_consensus_index,
    detect_hung_jury,
)


__all__ = [
    # Mesh & Service
    "DeliberationService",
    "DeliberationMesh",
    "ReviewRequest",
    "ReviewVerdict",
    "PlanRequest",
    "PlanVerdict",
    "AskRequest",
    "AskVerdict",
    # Rubrics & Data Models
    "Finding",
    "CriticalRisk",
    "PlanBallot",
    "ReviewBallot",
    "AskBallot",
    "JurorBallot",
    "CrossExamChallenge",
    "CrossExamResult",
    "ArbitrationVerdict",
    "DeliberationVerdict",
    "SeverityLevel",
    "StylometricSanitizer",
    "extract_json_payload",
    # Voting & Quorum
    "QuorumError",
    "QuorumResult",
    "BPCResult",
    "ConsensusMetrics",
    "check_quorum",
    "compute_consensus_index",
    "compute_bpde_entropy",
    "detect_hung_jury",
    "bpc_score_candidates",
    # Patch Emitter
    "DiffFile",
    "DiffHunk",
    "PatchValidationResult",
    "extract_unified_diff",
    "synthesize_unified_diff",
    "parse_unified_diff",
    "validate_patch",
    "apply_patch_in_memory",
    # SARIF Generator
    "SarifReport",
    "generate_sarif_report",
    "severity_to_sarif_level",
    "SARIF_SCHEMA_URI",
    "SARIF_VERSION",
]
