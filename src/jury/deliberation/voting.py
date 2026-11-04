r"""
src/jury/deliberation/voting.py - Quorum Evaluation, BPC Order Invariance & Consensus Mathematics.

Implements mathematical consensus protocols for Jury Mesh v3.0:
1. Strict Fail-Closed Quorum ($Q_{min} \ge 2$) preventing single-model collapse.
2. Balanced Position Calibration (BPC) 2-pass permutation canceling primacy/recency bias.
3. Positional Borda count with self-vote exclusion.
4. Pairwise Condorcet / Bradley-Terry preference analysis.
5. Consensus Index $C \in [0.0, 1.0]$ and BPDE (Balanced Position Diversity Entropy) hung-jury detection.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from jury.adapters.arbiter import identify_model_family
from jury.deliberation.rubrics import JurorBallot


# =============================================================================
# Data Structures & Exceptions
# =============================================================================

class QuorumError(RuntimeError):
    """Raised when juror panel fails to achieve minimum required quorum."""

    def __init__(
        self,
        message: str = "Quorum collapse: Insufficient valid juror ballots.",
        quorum_result: Optional[QuorumResult] = None,
        error_code: str = "ERR_QUORUM_COLLAPSE",
    ):
        super().__init__(message)
        self.message = message
        self.quorum_result = quorum_result
        self.error_code = error_code


@dataclass
class QuorumResult:
    """Outcome of fail-closed quorum evaluation."""
    is_valid: bool
    operational_jurors: int
    distinct_model_families: int
    distinct_providers: int
    error_code: Optional[str] = None
    reason: str = ""


@dataclass
class BPCResult:
    """Outcome of Borda-Pairwise-Condorcet consensus aggregation."""
    borda_scores: Dict[str, float]
    ranked_candidates: List[str]
    condorcet_winner: Optional[str]
    pairwise_matrix: Dict[str, Dict[str, int]]
    permutations_evaluated: int


@dataclass
class ConsensusMetrics:
    """Consensus and disagreement metrics across juror panel."""
    mean_score: float
    std_dev: float
    consensus_index: float  # C in [0.0, 1.0]
    bpde_entropy: float     # Shannon entropy across verdict distribution
    is_hung_jury: bool
    hung_jury_reason: Optional[str] = None


# =============================================================================
# 1. Fail-Closed Quorum Evaluation
# =============================================================================

def check_quorum(
    ballots: List[JurorBallot],
    min_quorum: int = 2,
    require_distinct_families: bool = True,
) -> QuorumResult:
    """
    Evaluate juror panel quorum with strict fail-closed semantics.

    To eliminate silent quorum collapse (where timed-out models leave a single
    monocultural model voting on itself), this function mandates:
    1. At least `min_quorum` (default 2) operational ballots.
    2. At least `min_quorum` distinct foundational model families (e.g. GLM, Qwen, Llama).
    """
    if not ballots:
        return QuorumResult(
            is_valid=False,
            operational_jurors=0,
            distinct_model_families=0,
            distinct_providers=0,
            error_code="ERR_QUORUM_COLLAPSE",
            reason="Quorum collapse: Zero juror ballots received.",
        )

    # Valid ballots have positive score or valid verdict
    valid_ballots = [b for b in ballots if b.score >= 0.0 and b.verdict]
    num_valid = len(valid_ballots)

    if num_valid < min_quorum:
        return QuorumResult(
            is_valid=False,
            operational_jurors=num_valid,
            distinct_model_families=len({identify_model_family(b.model) for b in valid_ballots}),
            distinct_providers=len({b.provider for b in valid_ballots}),
            error_code="ERR_QUORUM_COLLAPSE",
            reason=f"Quorum collapse: Received {num_valid} valid ballots, minimum required is {min_quorum}.",
        )

    # Check distinct model lineages and providers
    families = {identify_model_family(b.model) for b in valid_ballots}
    providers = {b.provider for b in valid_ballots}

    if require_distinct_families and len(families) < min_quorum:
        return QuorumResult(
            is_valid=False,
            operational_jurors=num_valid,
            distinct_model_families=len(families),
            distinct_providers=len(providers),
            error_code="ERR_QUORUM_COLLAPSE",
            reason=(
                f"Quorum collapse: Received {num_valid} ballots but only {len(families)} distinct "
                f"model family ({', '.join(families)}). Minimum {min_quorum} distinct families required."
            ),
        )

    return QuorumResult(
        is_valid=True,
        operational_jurors=num_valid,
        distinct_model_families=len(families),
        distinct_providers=len(providers),
        reason=f"Quorum satisfied: {num_valid} jurors from {len(families)} distinct families.",
    )


# =============================================================================
# 2. Consensus Index & BPDE Entropy Mathematics
# =============================================================================

def compute_consensus_index(scores: List[float], max_score: float = 10.0) -> Tuple[float, float, float]:
    """
    Compute Consensus Index $C = 1 - (\\sigma / \\sigma_{max})$.

    For scores bounded in $[0, max_score]$, the maximum possible standard
    deviation occurs when half the scores are 0 and half are max_score, giving
    $\\sigma_{max} = max_score / 2.0 = 5.0$.

    Returns:
        (mean, std_dev, consensus_index) where consensus_index is in [0.0, 1.0].
    """
    if not scores:
        return (0.0, 0.0, 1.0)
    if len(scores) == 1:
        return (scores[0], 0.0, 1.0)

    n = len(scores)
    mean = sum(scores) / n
    variance = sum((s - mean) ** 2 for s in scores) / n
    std_dev = math.sqrt(variance)

    sigma_max = max_score / 2.0  # 5.0 for [0, 10]
    raw_c = 1.0 - (std_dev / sigma_max)
    consensus_index = max(0.0, min(1.0, raw_c))

    return (mean, std_dev, consensus_index)


def compute_bpde_entropy(verdicts: List[str], base: float = 2.0) -> float:
    """
    Compute Balanced Position Diversity Entropy (BPDE).
    Calculates Shannon entropy across the discrete verdict distribution in bits (base 2 by default):
    $H_{BPDE} = - \\sum_{v} P(v) \\log_2 P(v)$
    """
    if not verdicts:
        return 0.0

    total = len(verdicts)
    counts: Dict[str, int] = {}
    for v in verdicts:
        norm_v = v.upper().strip()
        counts[norm_v] = counts.get(norm_v, 0) + 1

    entropy = 0.0
    for count in counts.values():
        p = count / total
        if p > 0.0:
            if base == 2.0:
                entropy -= p * math.log2(p)
            elif base == math.e:
                entropy -= p * math.log(p)
            else:
                entropy -= p * (math.log(p) / math.log(base))

    return entropy


def detect_hung_jury(
    scores: List[float],
    verdicts: List[str],
    threshold_consensus: float = 0.50,
    threshold_entropy: float = 0.45,
) -> ConsensusMetrics:
    r"""
    Analyze panel deliberations to detect whether a Hung Jury condition exists.

    Hung jury conditions:
    1. Score Consensus Index $C < threshold_consensus$ (high numerical score variance).
    2. Verdict Entropy $H_{BPDE} \ge threshold_entropy$ (irreconcilable verdict split).
    3. Severe polarity split (e.g. APPROVED vs REJECTED deadlock).
    """
    mean_s, std_dev, c_index = compute_consensus_index(scores)
    entropy = compute_bpde_entropy(verdicts)

    is_hung = False
    reason: Optional[str] = None

    # Check for direct polar split: both APPROVED and REJECTED present
    norm_verdicts = [v.upper().strip() for v in verdicts]
    has_approved = any("APPROV" in v for v in norm_verdicts)
    has_rejected = any("REJECT" in v for v in norm_verdicts)

    if c_index < threshold_consensus:
        is_hung = True
        reason = f"Numerical score divergence: Consensus Index {c_index:.2f} < threshold {threshold_consensus:.2f} (std_dev: {std_dev:.2f})"
    elif entropy >= threshold_entropy and has_approved and has_rejected:
        is_hung = True
        reason = f"Verdict polarity deadlock: BPDE Entropy {entropy:.2f} >= threshold {threshold_entropy:.2f} with conflicting verdicts ({set(norm_verdicts)})"
    elif entropy >= 0.68:  # Maximum possible binary entropy is ~0.693
        is_hung = True
        reason = f"High verdict entropy: BPDE Entropy {entropy:.2f} indicates deep panel disagreement"

    return ConsensusMetrics(
        mean_score=mean_s,
        std_dev=std_dev,
        consensus_index=c_index,
        bpde_entropy=entropy,
        is_hung_jury=is_hung,
        hung_jury_reason=reason,
    )


# =============================================================================
# 3. BPC Scoring Algorithm with 2-Pass Permutation
# =============================================================================

def bpc_score_candidates(
    rankings_per_juror: Dict[str, List[str]],
    candidate_proposers: Optional[Dict[str, str]] = None,
    permutations: Optional[List[List[str]]] = None,
) -> BPCResult:
    """
    Balanced Position Calibration (BPC) with Self-Vote Exclusion and Condorcet Pairwise analysis.

    Parameters:
        rankings_per_juror: Mapping from juror identifier to ordered list of candidate IDs
                            ranked best-first, e.g. {"alpha": ["C1", "C2", "C3"], ...}.
        candidate_proposers: Optional mapping from candidate ID to proposing model family
                             (used for self-vote exclusion).
        permutations: Optional juror presentation orders to evaluate (defaults to 2-pass:
                      forward [alpha, beta, gamma] and reverse [gamma, beta, alpha]).

    Returns:
        BPCResult containing normalized Borda scores $\\in [0, 1]$, ranked candidates,
        Condorcet winner (if one exists), and the pairwise win matrix.
    """
    all_candidates: Set[str] = set()
    for ranking in rankings_per_juror.values():
        all_candidates.update(ranking)

    c_list = sorted(list(all_candidates))
    K = len(c_list)
    if K == 0:
        return BPCResult(
            borda_scores={},
            ranked_candidates=[],
            condorcet_winner=None,
            pairwise_matrix={},
            permutations_evaluated=0,
        )

    if K == 1:
        single = c_list[0]
        return BPCResult(
            borda_scores={single: 1.0},
            ranked_candidates=[single],
            condorcet_winner=single,
            pairwise_matrix={single: {}},
            permutations_evaluated=1,
        )

    juror_keys = list(rankings_per_juror.keys())
    if not permutations:
        # Default 2-pass permutation: forward and reversed
        permutations = [
            list(juror_keys),
            list(reversed(juror_keys)),
        ]

    candidate_proposers = candidate_proposers or {}

    # Initialize accumulators across permutations
    total_borda_accum: Dict[str, float] = {c: 0.0 for c in c_list}
    valid_vote_counts: Dict[str, int] = {c: 0 for c in c_list}

    # Initialize pairwise win matrix W[c_i][c_j]: count of times c_i preferred over c_j
    pairwise: Dict[str, Dict[str, int]] = {c1: {c2: 0 for c2 in c_list} for c1 in c_list}

    for perm in permutations:
        for j_key in perm:
            if j_key not in rankings_per_juror:
                continue
            ranking = rankings_per_juror[j_key]
            juror_family = identify_model_family(j_key)

            # Positional Borda scoring for this juror:
            # Rank r in {1, ..., K}. Score B(c) = (K - r) / (K - 1) in [0, 1]
            for rank_idx, candidate in enumerate(ranking, start=1):
                # Self-Vote Exclusion: If candidate was proposed by this juror's family, exclude!
                proposer_family = candidate_proposers.get(candidate)
                if proposer_family and identify_model_family(proposer_family) == juror_family:
                    continue

                b_score = (K - rank_idx) / (K - 1)
                total_borda_accum[candidate] += b_score
                valid_vote_counts[candidate] += 1

            # Update pairwise preferences for this permutation pass
            for i, c_i in enumerate(ranking):
                for c_j in ranking[i + 1:]:
                    pairwise[c_i][c_j] += 1

    # Normalize Borda scores by valid vote counts
    final_borda: Dict[str, float] = {}
    for c in c_list:
        v_count = valid_vote_counts[c]
        if v_count > 0:
            final_borda[c] = round(total_borda_accum[c] / v_count, 4)
        else:
            final_borda[c] = 0.0

    # Sort candidates by Borda score descending
    ranked = sorted(c_list, key=lambda c: final_borda[c], reverse=True)

    # Condorcet Winner Detection:
    # A candidate is the Condorcet winner if they strictly defeat every other candidate
    # in pairwise head-to-head comparison
    condorcet_winner: Optional[str] = None
    for cand in c_list:
        beats_all = True
        for opponent in c_list:
            if opponent == cand:
                continue
            wins = pairwise[cand][opponent]
            losses = pairwise[opponent][cand]
            if wins <= losses:
                beats_all = False
                break
        if beats_all:
            condorcet_winner = cand
            break

    return BPCResult(
        borda_scores=final_borda,
        ranked_candidates=ranked,
        condorcet_winner=condorcet_winner,
        pairwise_matrix=pairwise,
        permutations_evaluated=len(permutations),
    )
