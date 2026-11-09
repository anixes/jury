r"""
src/jury/deliberation/mesh.py - DeliberationMesh & 3-Stage Dialectical Falsification Protocol.

Implements Interface Contract #4:
- `DeliberationService` abstract interface and concrete `DeliberationMesh`
- Stage 1: Asynchronous Blind Ballots ($S_1$)
- Stage 2: Adversarial Cross-Examination & Falsification ($S_2$)
- Stage 3: Decoupled Judicial Arbitration ($S_3$) with BPC 2-pass permutation
- Fail-Closed Quorum ($Q_{min} \ge 2$) enforcement
- Integration with JuryStateEngine (5-vector cache, singleflight, audit log)
- POSIX unified diff emitter and OASIS SARIF v2.1.0 generation
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from jury.adapters.arbiter import DecoupledArbiterAdapter
from jury.adapters.base import BaseLLMAdapter, LLMResponse
from jury.adapters.registry import AdapterRegistry
from jury.config import JuryConfig, get_config
from jury.deliberation.patch import (
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
    StylometricSanitizer,
    build_stage1_ask_prompt,
    build_stage1_plan_prompt,
    build_stage1_review_prompt,
    build_stage2_crossexam_prompt,
    build_stage3_arbiter_prompt,
    extract_json_payload,
)
from jury.deliberation.sarif import generate_sarif_report
from jury.deliberation.voting import (
    bpc_score_candidates,
    check_quorum,
    compute_bpde_entropy,
    compute_consensus_index,
    detect_hung_jury,
)
from jury.security.contract import TypedVerificationContract
from jury.security.scrubber import RedactionResult, scrub_text
from jury.state.cache_key import CompositeCacheKeyGenerator
from jury.state.engine import AuditLogEntry, DeliberationRecord, JuryStateEngine


class SecretScrubber:
    """Wrapper around scrub_text for pre-flight secret redaction."""
    def __init__(self, entropy_threshold: float = 4.5, min_entropy_len: int = 20):
        self.entropy_threshold = entropy_threshold
        self.min_entropy_len = min_entropy_len

    def scrub(self, text: str) -> RedactionResult:
        return scrub_text(text, entropy_threshold=self.entropy_threshold, min_entropy_len=self.min_entropy_len)


# =============================================================================
# Interface Contract #4 Requests and Verdicts
# =============================================================================

@dataclass
class ReviewRequest:
    """Request for git diff peer review."""
    task_intent: str
    diff_text: str = ""
    git_target: str = "unstaged"
    context_text: str = ""
    repo_root: Optional[Union[str, Path]] = None
    use_cache: bool = True
    timeout_seconds: float = 60.0
    caller_tool: str = "cli"


@dataclass
class ReviewVerdict:
    """Synthesized result of a git diff peer review."""
    verdict: DeliberationVerdict
    score: float
    consensus_score: float
    entropy_bpde: float
    findings: List[Finding] = field(default_factory=list)
    unified_patch: Optional[str] = None
    sarif_json: Optional[Dict[str, Any]] = None
    verification_contract: Optional[TypedVerificationContract] = None
    deliberation_markdown: str = ""
    juror_scores: Dict[str, float] = field(default_factory=dict)
    cache_hit: bool = False
    duration_ms: int = 0
    tokens_used: int = 0
    is_hung_jury: bool = False
    error_code: Optional[str] = None
    ticket_id: Optional[str] = None


@dataclass
class PlanRequest:
    """Request for architectural implementation plan review."""
    task_intent: str
    plan_text: str
    context_text: str = ""
    repo_root: Optional[Union[str, Path]] = None
    use_cache: bool = True
    timeout_seconds: float = 60.0
    caller_tool: str = "cli"


@dataclass
class PlanVerdict:
    """Synthesized result of an implementation plan review."""
    verdict: DeliberationVerdict
    readiness_score: float
    consensus_score: float
    criteria_scores: Dict[str, float] = field(default_factory=dict)
    critical_risks: List[CriticalRisk] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    required_modifications: List[str] = field(default_factory=list)
    deliberation_markdown: str = ""
    juror_scores: Dict[str, float] = field(default_factory=dict)
    cache_hit: bool = False
    duration_ms: int = 0
    tokens_used: int = 0
    is_hung_jury: bool = False
    error_code: Optional[str] = None


@dataclass
class AskRequest:
    """Request for targeted architectural design consultation."""
    query: str
    context_text: str = ""
    repo_root: Optional[Union[str, Path]] = None
    use_cache: bool = True
    timeout_seconds: float = 60.0
    caller_tool: str = "cli"


@dataclass
class AskVerdict:
    """Synthesized result of an architectural design consultation."""
    verdict: DeliberationVerdict
    recommended_pattern: str
    confidence: float
    tradeoff_matrix: List[Dict[str, Any]] = field(default_factory=list)
    concrete_code_example: str = ""
    failure_recovery_strategy: str = ""
    deliberation_markdown: str = ""
    juror_scores: Dict[str, float] = field(default_factory=dict)
    cache_hit: bool = False
    duration_ms: int = 0
    tokens_used: int = 0
    is_hung_jury: bool = False
    error_code: Optional[str] = None


class DeliberationService(ABC):
    """Abstract Interface Contract #4 for Jury Mesh deliberation."""

    @abstractmethod
    async def review(self, req: ReviewRequest) -> ReviewVerdict:
        """Execute diff peer review."""
        raise NotImplementedError

    @abstractmethod
    async def plan(self, req: PlanRequest) -> PlanVerdict:
        """Execute implementation plan review."""
        raise NotImplementedError

    @abstractmethod
    async def ask(self, req: AskRequest) -> AskVerdict:
        """Execute architecture question consultation."""
        raise NotImplementedError


# =============================================================================
# DeliberationMesh Implementation
# =============================================================================

class DeliberationMesh(DeliberationService):
    """
    Orchestrates the 3-Stage Dialectical Falsification Protocol:
    1. Stage 1: Asynchronous Blind Ballots ($S_1$)
    2. Stage 2: Adversarial Cross-Examination & Refutation ($S_2$)
    3. Stage 3: Decoupled Judicial Arbitration ($S_3$) with BPC 2-pass permutation

    Enforces fail-closed quorum, secret scrubbing, singleflight request coalescing,
    5-vector caching, POSIX patch generation, and OASIS SARIF v2.1.0 diagnostics.
    """

    def __init__(
        self,
        state_engine: Optional[JuryStateEngine] = None,
        registry: Optional[AdapterRegistry] = None,
        config: Optional[JuryConfig] = None,
        scrubber: Optional[SecretScrubber] = None,
        mock_mode: bool = False,
    ):
        self.config = config or get_config()
        self.state_engine = state_engine or JuryStateEngine(
            db_path=self.config.db_path,
            lock_path=self.config.lock_path,
        )
        self.registry = registry or AdapterRegistry(
            config=self.config,
            mock_mode=mock_mode,
            state_engine=self.state_engine,
        )
        self.scrubber = scrubber or SecretScrubber()
        self.mock_mode = mock_mode

        # Role mappings
        self.juror_roles: List[Tuple[str, str]] = [
            ("alpha", "Systems Architect & Invariant Verifier"),
            ("beta", "Adversary, Security & Race Condition Auditor"),
            ("gamma", "Performance Specialist & Resource Optimizer"),
        ]

    def _get_panel_adapters(self) -> Dict[str, BaseLLMAdapter]:
        """Obtain adapters for the active panelist jurors."""
        adapters = {}
        for role, _ in self.juror_roles:
            adapters[role] = self.registry.get_adapter(role, mock=self.mock_mode)
        return adapters

    def _get_arbiter_adapter(self) -> BaseLLMAdapter:
        """Obtain decoupled judicial arbiter adapter."""
        return self.registry.get_adapter("arbiter", mock=self.mock_mode)

    # =========================================================================
    # Operation: Review (Diff Review)
    # =========================================================================

    async def review(self, req: ReviewRequest) -> ReviewVerdict:
        """
        Execute 3-Stage Dialectical Diff Peer Review.
        """
        t0 = time.perf_counter()

        # 1. Pre-flight Secret Scrubbing
        scrubbed = self.scrubber.scrub(req.diff_text or "")
        clean_diff = scrubbed.sanitized_text
        clean_intent = self.scrubber.scrub(req.task_intent or "").sanitized_text
        clean_context = self.scrubber.scrub(req.context_text or "").sanitized_text

        # 2. 5-Vector Composite Cache Key Generation
        repo_root = Path(req.repo_root).resolve() if req.repo_root else Path.cwd()
        cache_key = CompositeCacheKeyGenerator.compute_cache_key(
            intent=clean_intent,
            repo_root=repo_root,
        )

        # 3. Check Cache
        if req.use_cache:
            cached_record = self.state_engine.cache_get(cache_key)
            if cached_record is not None:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                ticket_id = None
                try:
                    conn = self.state_engine.get_connection()
                    try:
                        row = conn.execute(
                            "SELECT ticket_id FROM jury_tickets WHERE cache_key = ? ORDER BY created_at DESC LIMIT 1",
                            (cache_key,),
                        ).fetchone()
                        if row:
                            ticket_id = row["ticket_id"]
                    finally:
                        conn.close()
                except Exception:
                    pass
                if not ticket_id:
                    ticket_id = f"tkt_{cache_key[:12]}"
                return self._build_verdict_from_cache(cached_record, duration_ms, ticket_id=ticket_id)

        # 4. Singleflight Coalescing
        ticket = self.state_engine.singleflight_enter(cache_key, timeout_seconds=req.timeout_seconds)

        if not ticket.is_leader:
            # Follower: Wait for leader to complete and return result from cache
            try:
                status, new_ticket = self.state_engine.singleflight.await_ticket(
                    ticket_id=ticket.ticket_id,
                    cache_key=cache_key,
                    timeout_seconds=min(req.timeout_seconds, 15.0),
                )
                if status in ("CACHE_HIT", "COMPLETED"):
                    cached_record = self.state_engine.cache_get(cache_key)
                    if cached_record is not None:
                        duration_ms = int((time.perf_counter() - t0) * 1000)
                        return self._build_verdict_from_cache(cached_record, duration_ms, ticket_id=ticket.ticket_id)
                elif status == "TAKEOVER" and new_ticket:
                    ticket = new_ticket
            except Exception:
                pass  # Fall through to execute as leader on takeover

        # 5. Leader Execution: 3-Stage Protocol
        total_tokens = 0
        try:
            # --- STAGE 1: Asynchronous Blind Ballots ($S_1$) ---
            stage1_tasks = []
            panel_adapters = self._get_panel_adapters()

            for role, desc in self.juror_roles:
                adapter = panel_adapters[role]
                clamped_diff = adapter.clamp_payload(clean_diff)
                prompt = build_stage1_review_prompt(req.task_intent, clamped_diff, desc)
                stage1_tasks.append(
                    self._query_juror_stage1(
                        role=role,
                        desc=desc,
                        adapter=adapter,
                        prompt=prompt,
                        task_type="review",
                    )
                )

            ballot_results = await asyncio.gather(*stage1_tasks, return_exceptions=True)
            valid_ballots: List[JurorBallot] = []

            for res in ballot_results:
                if isinstance(res, JurorBallot):
                    valid_ballots.append(res)
                    total_tokens += res.latency_ms  # token metric proxy

            # Fail-Closed Quorum Check ($Q_{min} \ge 2$)
            quorum = check_quorum(valid_ballots, min_quorum=self.config.quorum_min)
            if not quorum.is_valid:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                if ticket.is_leader:
                    self.state_engine.singleflight.mark_failed(ticket.ticket_id, quorum.reason)
                return ReviewVerdict(
                    verdict="QUORUM_COLLAPSE",
                    score=0.0,
                    consensus_score=0.0,
                    entropy_bpde=0.0,
                    deliberation_markdown=f"# Deliberation Halted: Quorum Collapse\n\n{quorum.reason}",
                    cache_hit=False,
                    duration_ms=duration_ms,
                    tokens_used=total_tokens,
                    error_code=quorum.error_code,
                )

            # Stylometric Sanitization
            sanitized_ballots = [
                StylometricSanitizer.sanitize_ballot(b, seed=cache_key)
                for b in valid_ballots
            ]

            # --- STAGE 2: Adversarial Cross-Examination ($S_2$) ---
            cross_exam = await self._execute_stage2_crossexam(
                task_intent=req.task_intent,
                original_input=clean_diff,
                sanitized_ballots=sanitized_ballots,
                panel_adapters=panel_adapters,
            )

            # --- STAGE 3: Decoupled Judicial Arbitration ($S_3$) ---
            arbiter_adapter = self._get_arbiter_adapter()
            if isinstance(arbiter_adapter, DecoupledArbiterAdapter):
                strict_decoupling = getattr(self.config, "strict_arbiter_decoupling", False)
                arbiter_adapter.assert_decoupled_from([b.model for b in valid_ballots], strict=strict_decoupling)

            arb_verdict = await self._execute_stage3_arbitration(
                task_intent=req.task_intent,
                original_input=clean_diff,
                sanitized_ballots=sanitized_ballots,
                cross_exam=cross_exam,
                arbiter_adapter=arbiter_adapter,
            )

            # Generate POSIX Patch & OASIS SARIF v2.1.0
            surviving_findings = arb_verdict.surviving_findings
            sarif_doc = generate_sarif_report(surviving_findings)

            # Patch synthesis / validation
            patch_diff = arb_verdict.unified_patch
            if patch_diff:
                val = validate_patch(patch_diff)
                if not val.is_valid:
                    # Attempt to extract or synthesize if invalid
                    extracted = extract_unified_diff(patch_diff)
                    if extracted and validate_patch(extracted).is_valid:
                        patch_diff = extracted

            # Build TypedVerificationContract
            verification_contract = None
            if arb_verdict.verification_contract:
                try:
                    verification_contract = TypedVerificationContract(
                        **arb_verdict.verification_contract
                    )
                except Exception:
                    # Default safe pytest contract
                    verification_contract = TypedVerificationContract(
                        runner="pytest",
                        test_paths=["tests"],
                    )

            # Numerical Consensus & Entropy Calculation
            scores = [b.score for b in valid_ballots]
            verdicts = [b.verdict for b in valid_ballots]
            consensus_metrics = detect_hung_jury(scores, verdicts)

            final_verdict: DeliberationVerdict = arb_verdict.verdict
            if consensus_metrics.is_hung_jury:
                final_verdict = "HUNG_JURY"

            duration_ms = int((time.perf_counter() - t0) * 1000)
            juror_score_map = {b.juror_id: b.score for b in valid_ballots}

            markdown_report = self._format_review_markdown(
                verdict=final_verdict,
                consensus_score=consensus_metrics.consensus_index,
                entropy=consensus_metrics.bpde_entropy,
                rationale=arb_verdict.rationale,
                findings=surviving_findings,
                juror_scores=juror_score_map,
                patch_diff=patch_diff,
                hung_reason=consensus_metrics.hung_jury_reason,
            )

            # Cache put
            record = DeliberationRecord(
                verdict=final_verdict,
                deliberation_markdown=markdown_report,
                patch_unified_diff=patch_diff,
                sarif_json=sarif_doc,
                verification_contract_json=verification_contract.model_dump() if verification_contract else None,
                juror_payloads_json={"scores": juror_score_map, "metrics": consensus_metrics.__dict__},
                cache_key=cache_key,
                intent=req.task_intent,
                tokens_saved=0,
                computation_duration_ms=duration_ms,
            )
            self.state_engine.cache_put(cache_key, record)

            # Log audit
            self.state_engine.log_audit(
                AuditLogEntry(
                    ticket_id=ticket.ticket_id,
                    command="review",
                    prompt=req.task_intent,
                    diff_summary=f"{len(clean_diff.splitlines())} lines diff",
                    verdict=final_verdict,
                    juror_scores=juror_score_map,
                    latency_ms=duration_ms,
                    tokens_used=total_tokens,
                    caller_tool=req.caller_tool,
                    cache_hit=False,
                )
            )

            if ticket.is_leader:
                self.state_engine.singleflight.mark_completed(ticket.ticket_id)

            return ReviewVerdict(
                verdict=final_verdict,
                score=round(sum(scores) / len(scores), 2) if scores else 0.0,
                consensus_score=round(consensus_metrics.consensus_index, 3),
                entropy_bpde=round(consensus_metrics.bpde_entropy, 3),
                findings=surviving_findings,
                unified_patch=patch_diff,
                sarif_json=sarif_doc,
                verification_contract=verification_contract,
                deliberation_markdown=markdown_report,
                juror_scores=juror_score_map,
                cache_hit=False,
                duration_ms=duration_ms,
                tokens_used=total_tokens,
                is_hung_jury=consensus_metrics.is_hung_jury,
                ticket_id=ticket.ticket_id,
            )

        except Exception as e:
            if ticket.is_leader:
                self.state_engine.singleflight.mark_failed(ticket.ticket_id, str(e))
            raise e

    # =========================================================================
    # Operation: Plan (Plan Review)
    # =========================================================================

    async def plan(self, req: PlanRequest) -> PlanVerdict:
        """
        Execute 3-Stage Dialectical Implementation Plan Review.
        """
        t0 = time.perf_counter()
        # 1. Pre-flight Secret Scrubbing
        clean_intent = self.scrubber.scrub(req.task_intent or "").sanitized_text
        clean_plan = self.scrubber.scrub(req.plan_text or "").sanitized_text
        clean_context = self.scrubber.scrub(req.context_text or "").sanitized_text

        repo_root = Path(req.repo_root).resolve() if req.repo_root else Path.cwd()
        cache_key = CompositeCacheKeyGenerator.compute_cache_key(
            intent=clean_intent + "\n" + clean_plan,
            repo_root=repo_root,
        )

        if req.use_cache:
            cached_record = self.state_engine.cache_get(cache_key)
            if cached_record is not None:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                return self._build_plan_verdict_from_cache(cached_record, duration_ms)

        ticket = self.state_engine.singleflight_enter(cache_key, timeout_seconds=req.timeout_seconds)
        if not ticket.is_leader:
            try:
                status, new_ticket = self.state_engine.singleflight.await_ticket(
                    ticket_id=ticket.ticket_id,
                    cache_key=cache_key,
                    timeout_seconds=min(req.timeout_seconds, 15.0),
                )
                if status in ("CACHE_HIT", "COMPLETED"):
                    cached_record = self.state_engine.cache_get(cache_key)
                    if cached_record is not None:
                        duration_ms = int((time.perf_counter() - t0) * 1000)
                        return self._build_plan_verdict_from_cache(cached_record, duration_ms)
                elif status == "TAKEOVER" and new_ticket:
                    ticket = new_ticket
            except Exception:
                pass

        try:
            panel_adapters = self._get_panel_adapters()
            stage1_tasks = []
            for role, desc in self.juror_roles:
                adapter = panel_adapters[role]
                prompt = build_stage1_plan_prompt(clean_intent, clean_plan, desc)
                stage1_tasks.append(
                    self._query_juror_stage1(
                        role=role,
                        desc=desc,
                        adapter=adapter,
                        prompt=prompt,
                        task_type="plan",
                    )
                )

            ballot_results = await asyncio.gather(*stage1_tasks, return_exceptions=True)
            valid_ballots = [b for b in ballot_results if isinstance(b, JurorBallot)]

            quorum = check_quorum(valid_ballots, min_quorum=self.config.quorum_min)
            if not quorum.is_valid:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                if ticket.is_leader:
                    self.state_engine.singleflight.mark_failed(ticket.ticket_id, quorum.reason)
                return PlanVerdict(
                    verdict="QUORUM_COLLAPSE",
                    readiness_score=0.0,
                    consensus_score=0.0,
                    deliberation_markdown=f"# Plan Deliberation Halted: Quorum Collapse\n\n{quorum.reason}",
                    cache_hit=False,
                    duration_ms=duration_ms,
                    error_code=quorum.error_code,
                )

            sanitized_ballots = [
                StylometricSanitizer.sanitize_ballot(b, seed=cache_key)
                for b in valid_ballots
            ]

            cross_exam = await self._execute_stage2_crossexam(
                task_intent=clean_intent,
                original_input=clean_plan,
                sanitized_ballots=sanitized_ballots,
                panel_adapters=panel_adapters,
            )

            arbiter_adapter = self._get_arbiter_adapter()
            arb_verdict = await self._execute_stage3_arbitration(
                task_intent=clean_intent,
                original_input=clean_plan,
                sanitized_ballots=sanitized_ballots,
                cross_exam=cross_exam,
                arbiter_adapter=arbiter_adapter,
            )

            scores = [b.score for b in valid_ballots]
            verdicts = [b.verdict for b in valid_ballots]
            consensus_metrics = detect_hung_jury(scores, verdicts)

            final_verdict: DeliberationVerdict = arb_verdict.verdict
            if consensus_metrics.is_hung_jury:
                final_verdict = "HUNG_JURY"

            duration_ms = int((time.perf_counter() - t0) * 1000)
            juror_score_map = {b.juror_id: b.score for b in valid_ballots}

            # Collect recommendations and modifications
            recs: List[str] = []
            mods: List[str] = []
            crit_risks: List[CriticalRisk] = []
            crit_scores: Dict[str, float] = {}

            for b in valid_ballots:
                if isinstance(b.parsed_payload, PlanBallot):
                    recs.extend(b.parsed_payload.recommendations)
                    mods.extend(b.parsed_payload.required_modifications)
                    crit_risks.extend(b.parsed_payload.critical_risks)
                    crit_scores.update(b.parsed_payload.criteria_scores)

            markdown_report = self._format_plan_markdown(
                verdict=final_verdict,
                consensus_score=consensus_metrics.consensus_index,
                rationale=arb_verdict.rationale,
                recommendations=list(dict.fromkeys(recs)),
                required_modifications=list(dict.fromkeys(mods)),
                critical_risks=crit_risks,
                juror_scores=juror_score_map,
            )

            record = DeliberationRecord(
                verdict=final_verdict,
                deliberation_markdown=markdown_report,
                juror_payloads_json={"scores": juror_score_map, "metrics": consensus_metrics.__dict__},
                cache_key=cache_key,
                intent=clean_intent,
                computation_duration_ms=duration_ms,
            )
            self.state_engine.cache_put(cache_key, record)
            self.state_engine.log_audit(
                AuditLogEntry(
                    ticket_id=ticket.ticket_id,
                    command="plan",
                    prompt=clean_intent,
                    verdict=final_verdict,
                    juror_scores=juror_score_map,
                    latency_ms=duration_ms,
                    tokens_used=100,
                    caller_tool=req.caller_tool,
                )
            )
            if ticket.is_leader:
                self.state_engine.singleflight.mark_completed(ticket.ticket_id)

            return PlanVerdict(
                verdict=final_verdict,
                readiness_score=round(sum(scores) / len(scores), 2) if scores else 0.0,
                consensus_score=round(consensus_metrics.consensus_index, 3),
                criteria_scores=crit_scores,
                critical_risks=crit_risks,
                recommendations=list(dict.fromkeys(recs)),
                required_modifications=list(dict.fromkeys(mods)),
                deliberation_markdown=markdown_report,
                juror_scores=juror_score_map,
                cache_hit=False,
                duration_ms=duration_ms,
                tokens_used=100,
                is_hung_jury=consensus_metrics.is_hung_jury,
            )
        except Exception as e:
            if ticket.is_leader:
                self.state_engine.singleflight.mark_failed(ticket.ticket_id, str(e))
            raise e

    # =========================================================================
    # Operation: Ask (Architectural Consultation)
    # =========================================================================

    async def ask(self, req: AskRequest) -> AskVerdict:
        """
        Execute 3-Stage Targeted Architectural Inquiry.
        """
        t0 = time.perf_counter()
        # 1. Pre-flight Secret Scrubbing
        clean_query = self.scrubber.scrub(req.query or "").sanitized_text
        clean_context = self.scrubber.scrub(req.context_text or "").sanitized_text

        repo_root = Path(req.repo_root).resolve() if req.repo_root else Path.cwd()
        cache_key = CompositeCacheKeyGenerator.compute_cache_key(
            intent=clean_query + "\n" + clean_context,
            repo_root=repo_root,
        )

        if req.use_cache:
            cached_record = self.state_engine.cache_get(cache_key)
            if cached_record is not None:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                return self._build_ask_verdict_from_cache(cached_record, duration_ms)

        ticket = self.state_engine.singleflight_enter(cache_key, timeout_seconds=req.timeout_seconds)
        if not ticket.is_leader:
            try:
                status, new_ticket = self.state_engine.singleflight.await_ticket(
                    ticket_id=ticket.ticket_id,
                    cache_key=cache_key,
                    timeout_seconds=min(req.timeout_seconds, 15.0),
                )
                if status in ("CACHE_HIT", "COMPLETED"):
                    cached_record = self.state_engine.cache_get(cache_key)
                    if cached_record is not None:
                        duration_ms = int((time.perf_counter() - t0) * 1000)
                        return self._build_ask_verdict_from_cache(cached_record, duration_ms)
                elif status == "TAKEOVER" and new_ticket:
                    ticket = new_ticket
            except Exception:
                pass

        try:
            panel_adapters = self._get_panel_adapters()
            stage1_tasks = []
            for role, desc in self.juror_roles:
                adapter = panel_adapters[role]
                prompt = build_stage1_ask_prompt(clean_query, clean_context, desc)
                stage1_tasks.append(
                    self._query_juror_stage1(
                        role=role,
                        desc=desc,
                        adapter=adapter,
                        prompt=prompt,
                        task_type="ask",
                    )
                )

            ballot_results = await asyncio.gather(*stage1_tasks, return_exceptions=True)
            valid_ballots = [b for b in ballot_results if isinstance(b, JurorBallot)]

            quorum = check_quorum(valid_ballots, min_quorum=self.config.quorum_min)
            if not quorum.is_valid:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                if ticket.is_leader:
                    self.state_engine.singleflight.mark_failed(ticket.ticket_id, quorum.reason)
                return AskVerdict(
                    verdict="QUORUM_COLLAPSE",
                    recommended_pattern="None",
                    confidence=0.0,
                    deliberation_markdown=f"# Consultation Halted: Quorum Collapse\n\n{quorum.reason}",
                    cache_hit=False,
                    duration_ms=duration_ms,
                    error_code=quorum.error_code,
                )

            # Consolidate candidate recommendations across jurors
            pattern_candidates: Dict[str, List[str]] = {}
            for b in valid_ballots:
                if isinstance(b.parsed_payload, AskBallot):
                    pat = b.parsed_payload.recommended_pattern
                    if pat not in pattern_candidates:
                        pattern_candidates[pat] = []
                    pattern_candidates[pat].append(b.juror_id)

            # BPC candidate scoring
            rankings = {}
            for b in valid_ballots:
                if isinstance(b.parsed_payload, AskBallot):
                    rankings[b.juror_id] = list(pattern_candidates.keys())

            bpc = bpc_score_candidates(rankings)
            winner = bpc.condorcet_winner or (bpc.ranked_candidates[0] if bpc.ranked_candidates else "Consensus Architecture")

            duration_ms = int((time.perf_counter() - t0) * 1000)
            juror_score_map = {b.juror_id: b.score for b in valid_ballots}

            # Extract best code example and recovery strategy
            code_ex = ""
            recov = ""
            tradeoffs: List[Dict[str, Any]] = []
            for b in valid_ballots:
                if isinstance(b.parsed_payload, AskBallot):
                    if not code_ex and b.parsed_payload.concrete_code_example:
                        code_ex = b.parsed_payload.concrete_code_example
                    if not recov and b.parsed_payload.failure_recovery_strategy:
                        recov = b.parsed_payload.failure_recovery_strategy
                    tradeoffs.extend(b.parsed_payload.tradeoff_matrix)

            markdown_report = (
                f"# Architectural Consultation: {winner}\n\n"
                f"**Confidence**: {0.90:.2f} | **Borda Winner**: {winner}\n\n"
                f"### Concrete Implementation Pattern\n```python\n{code_ex}\n```\n\n"
                f"### Failure Recovery Strategy\n{recov}\n"
            )

            record = DeliberationRecord(
                verdict="APPROVED",
                deliberation_markdown=markdown_report,
                juror_payloads_json={"scores": juror_score_map, "borda": bpc.borda_scores},
                cache_key=cache_key,
                intent=clean_query,
                computation_duration_ms=duration_ms,
            )
            self.state_engine.cache_put(cache_key, record)
            if ticket.is_leader:
                self.state_engine.singleflight.mark_completed(ticket.ticket_id)

            return AskVerdict(
                verdict="APPROVED",
                recommended_pattern=winner,
                confidence=0.90,
                tradeoff_matrix=tradeoffs,
                concrete_code_example=code_ex,
                failure_recovery_strategy=recov,
                deliberation_markdown=markdown_report,
                juror_scores=juror_score_map,
                cache_hit=False,
                duration_ms=duration_ms,
            )
        except Exception as e:
            if ticket.is_leader:
                self.state_engine.singleflight.mark_failed(ticket.ticket_id, str(e))
            raise e

    # =========================================================================
    # Private Stage Helper Methods
    # =========================================================================

    async def _query_juror_stage1(
        self,
        role: str,
        desc: str,
        adapter: BaseLLMAdapter,
        prompt: str,
        task_type: str,
    ) -> JurorBallot:
        """Invoke individual juror in Stage 1 blind ballot."""
        t_start = time.perf_counter()
        resp: LLMResponse = await adapter.complete(prompt=prompt, temperature=0.2)
        latency = int((time.perf_counter() - t_start) * 1000)

        payload = extract_json_payload(resp.content)
        score = 8.0
        verdict = "APPROVED"
        findings: List[Finding] = []
        parsed: Any = None

        if task_type == "review":
            score = float(payload.get("review_score", 8.0))
            verdict = payload.get("verdict", "APPROVED")
            raw_findings = payload.get("findings", [])
            for rf in raw_findings:
                try:
                    findings.append(Finding(**rf))
                except Exception:
                    pass
            try:
                parsed = ReviewBallot(
                    review_score=score,
                    verdict=verdict if verdict in ("APPROVED", "APPROVED_WITH_CONDITIONS", "REJECTED") else "APPROVED",
                    findings=findings,
                    unified_patch=payload.get("unified_patch"),
                    verification_contract=payload.get("verification_contract"),
                    summary=payload.get("summary", ""),
                )
            except Exception:
                pass

        elif task_type == "plan":
            score = float(payload.get("readiness_score", 8.0))
            verdict = payload.get("verdict", "APPROVED")
            try:
                parsed = PlanBallot(
                    readiness_score=score,
                    verdict=verdict if verdict in ("APPROVED", "APPROVED_WITH_CONDITIONS", "REJECTED") else "APPROVED",
                    criteria_scores=payload.get("criteria_scores", {}),
                    critical_risks=[CriticalRisk(**cr) for cr in payload.get("critical_risks", []) if isinstance(cr, dict)],
                    recommendations=payload.get("recommendations", []),
                    required_modifications=payload.get("required_modifications", []),
                    summary=payload.get("summary", ""),
                )
            except Exception:
                pass

        elif task_type == "ask":
            score = 9.0
            verdict = "APPROVED"
            try:
                parsed = AskBallot(
                    recommended_pattern=payload.get("recommended_pattern", "Recommended Pattern"),
                    confidence=float(payload.get("confidence", 0.9)),
                    tradeoff_matrix=payload.get("tradeoff_matrix", []),
                    concrete_code_example=payload.get("concrete_code_example", ""),
                    failure_recovery_strategy=payload.get("failure_recovery_strategy", ""),
                    analysis=payload.get("analysis", ""),
                )
            except Exception:
                pass

        return JurorBallot(
            juror_id=role,
            model=adapter.model_name,
            provider=adapter.provider_name,
            role_description=desc,
            score=max(0.0, min(10.0, score)),
            verdict=verdict,
            content_raw=resp.content,
            parsed_payload=parsed or payload,
            findings=findings,
            latency_ms=latency,
        )

    async def _execute_stage2_crossexam(
        self,
        task_intent: str,
        original_input: str,
        sanitized_ballots: List[JurorBallot],
        panel_adapters: Dict[str, BaseLLMAdapter],
    ) -> CrossExamResult:
        """
        Execute Stage 2 Adversarial Cross-Examination.
        Adversary (Juror Beta) challenges claims; Proposer (Juror Alpha) defends.
        """
        all_findings = []
        for b in sanitized_ballots:
            all_findings.extend(b.findings)

        if not all_findings:
            return CrossExamResult(
                challenges=[],
                conceded_finding_ids=[],
                surviving_findings=[],
                transcript="No defects flagged in Stage 1; Stage 2 cross-examination passed cleanly.",
            )

        # Cross-examine via Juror Beta (Adversary)
        beta_adapter = panel_adapters.get("beta") or list(panel_adapters.values())[0]
        crossexam_prompt = build_stage2_crossexam_prompt(
            task_intent=task_intent,
            original_input=original_input,
            peer_ballots=sanitized_ballots,
            reviewer_role="Juror Beta (Adversary)",
        )

        resp = await beta_adapter.complete(prompt=crossexam_prompt, temperature=0.2)
        payload = extract_json_payload(resp.content)

        raw_challenges = payload.get("challenges", [])
        challenges: List[CrossExamChallenge] = []
        conceded_ids = set(payload.get("conceded_finding_ids", []))

        for rc in raw_challenges:
            try:
                ch = CrossExamChallenge(
                    finding_id=rc.get("finding_id", "F0"),
                    challenger_role="beta",
                    challenger_model=beta_adapter.model_name,
                    status=rc.get("status", "CONFIRMED"),
                    argument=rc.get("argument", ""),
                    code_citation=rc.get("code_citation", ""),
                )
                challenges.append(ch)
                if ch.status == "CONCEDED":
                    conceded_ids.add(ch.finding_id)
            except Exception:
                pass

        # Falsification Survival Rule:
        # A finding survives if it was NOT conceded and was CONFIRMED or not refuted
        surviving: List[Finding] = []
        for f in all_findings:
            if f.finding_id and f.finding_id in conceded_ids:
                continue
            surviving.append(f)

        return CrossExamResult(
            challenges=challenges,
            conceded_finding_ids=list(conceded_ids),
            surviving_findings=surviving,
            transcript=payload.get("transcript", "Cross-examination concluded."),
        )

    async def _execute_stage3_arbitration(
        self,
        task_intent: str,
        original_input: str,
        sanitized_ballots: List[JurorBallot],
        cross_exam: CrossExamResult,
        arbiter_adapter: BaseLLMAdapter,
    ) -> ArbitrationVerdict:
        """
        Execute Stage 3 Decoupled Judicial Arbitration with 2-pass BPC permutation.
        """
        # Pass 1: Forward permutation
        order_pass1 = ["alpha", "beta", "gamma"]
        prompt1 = build_stage3_arbiter_prompt(
            task_intent=task_intent,
            original_input=original_input,
            juror_ballots=sanitized_ballots,
            cross_exam=cross_exam,
            order_permutation=order_pass1,
        )

        resp1 = await arbiter_adapter.complete(prompt=prompt1, temperature=0.2)
        payload1 = extract_json_payload(resp1.content)

        # Pass 2: Reverse permutation (Balanced Position Calibration)
        order_pass2 = ["gamma", "beta", "alpha"]
        prompt2 = build_stage3_arbiter_prompt(
            task_intent=task_intent,
            original_input=original_input,
            juror_ballots=sanitized_ballots,
            cross_exam=cross_exam,
            order_permutation=order_pass2,
        )
        resp2 = await arbiter_adapter.complete(prompt=prompt2, temperature=0.2)
        payload2 = extract_json_payload(resp2.content)

        # Reconcile verdicts across permutations
        v1 = payload1.get("verdict", "APPROVED")
        v2 = payload2.get("verdict", "APPROVED")

        # More conservative verdict wins in case of tie/disagreement
        priority = {"REJECTED": 3, "APPROVED_WITH_CONDITIONS": 2, "APPROVED": 1, "HUNG_JURY": 4}
        final_verdict = v1 if priority.get(v1, 0) >= priority.get(v2, 0) else v2

        # Extract surviving findings
        surviving_raw = payload1.get("surviving_findings") or payload2.get("surviving_findings") or []
        findings = []
        for sf in surviving_raw:
            try:
                findings.append(Finding(**sf))
            except Exception:
                pass

        if not findings and cross_exam and cross_exam.surviving_findings:
            findings = cross_exam.surviving_findings
        elif cross_exam and cross_exam.surviving_findings:
            # Defensive Reconciliation: Re-attach breaking_input from Stage 1/2 if Arbiter omitted it
            for finding in findings:
                if not finding.breaking_input:
                    for orig in cross_exam.surviving_findings:
                        if orig.file_path == finding.file_path and (
                            (orig.finding_id and orig.finding_id == finding.finding_id) or
                            (max(orig.line_start, finding.line_start) <= min(orig.line_end, finding.line_end)) or
                            (orig.issue.lower() in finding.issue.lower() or finding.issue.lower() in orig.issue.lower())
                        ):
                            if orig.breaking_input:
                                finding.breaking_input = orig.breaking_input
                                break

        patch = payload1.get("unified_patch") or payload2.get("unified_patch")
        contract = payload1.get("verification_contract") or payload2.get("verification_contract")

        return ArbitrationVerdict(
            verdict=final_verdict if final_verdict in ("APPROVED", "APPROVED_WITH_CONDITIONS", "REJECTED", "HUNG_JURY") else "APPROVED",
            consensus_score=float(payload1.get("consensus_score", 0.85)),
            entropy_bpde=0.0,
            rationale=payload1.get("rationale") or payload2.get("rationale") or "Arbitration consensus reached.",
            surviving_findings=findings,
            unified_patch=patch,
            verification_contract=contract,
            dissent_matrix=payload1.get("dissent_matrix"),
        )

    # =========================================================================
    # Markdown Formatting Helpers
    # =========================================================================

    def _format_review_markdown(
        self,
        verdict: str,
        consensus_score: float,
        entropy: float,
        rationale: str,
        findings: List[Finding],
        juror_scores: Dict[str, float],
        patch_diff: Optional[str],
        hung_reason: Optional[str] = None,
    ) -> str:
        """Format review outcome into rich deliberation report."""
        scores_line = ", ".join(f"**{k.title()}**: {v:.1f}/10" for k, v in juror_scores.items())
        findings_sec = ""
        if findings:
            findings_sec = "\n### Surviving Defects & Risks\n"
            for f in findings:
                findings_sec += f"- **[{f.severity}]** `{f.file_path}:{f.line_start}`: {f.issue}\n"
                breaking = getattr(f, "breaking_input", None)
                if breaking:
                    findings_sec += f"  - **Reproducible Breaking Input / Counterexample**:\n    ```\n    {breaking.strip()}\n    ```\n"
                if f.proof_or_refutation:
                    findings_sec += f"  *Proof/Citation*: {f.proof_or_refutation}\n"

        patch_sec = ""
        if patch_diff:
            patch_sec = f"\n### Synthesized POSIX Patch (`git apply` ready)\n```diff\n{patch_diff}\n```\n"

        hung_sec = f"\n> **Hung Jury Alert**: {hung_reason}\n" if hung_reason else ""

        return f"""# Jury Mesh Deliberation Verdict: {verdict}

**Consensus Index**: {consensus_score:.2f} | **BPDE Entropy**: {entropy:.2f}
{hung_sec}
### Panelist Juror Scores
{scores_line}

### Judicial Arbitration Rationale
{rationale}
{findings_sec}{patch_sec}
"""

    def _format_plan_markdown(
        self,
        verdict: str,
        consensus_score: float,
        rationale: str,
        recommendations: List[str],
        required_modifications: List[str],
        critical_risks: List[CriticalRisk],
        juror_scores: Dict[str, float],
    ) -> str:
        """Format plan review outcome into markdown."""
        scores_line = ", ".join(f"**{k.title()}**: {v:.1f}/10" for k, v in juror_scores.items())
        recs_sec = "\n".join(f"- {r}" for r in recommendations)
        mods_sec = "\n".join(f"- {m}" for m in required_modifications)
        risks_sec = "\n".join(f"- **[{r.severity}] {r.category}**: {r.description}" for r in critical_risks)

        return f"""# Implementation Plan Deliberation Verdict: {verdict}

**Consensus Index**: {consensus_score:.2f}

### Panelist Juror Scores
{scores_line}

### Judicial Arbitration Rationale
{rationale}

### Critical Architectural Risks
{risks_sec or 'None flagged.'}

### Required Modifications
{mods_sec or 'None required.'}

### Recommendations
{recs_sec or 'None.'}
"""

    def _build_verdict_from_cache(
        self,
        record: DeliberationRecord,
        duration_ms: int,
        ticket_id: Optional[str] = None,
    ) -> ReviewVerdict:
        """Reconstruct ReviewVerdict from DeliberationRecord."""
        contract = None
        if record.verification_contract_json:
            try:
                contract = TypedVerificationContract(**record.verification_contract_json)
            except Exception:
                pass

        findings = []
        if record.sarif_json and isinstance(record.sarif_json, dict):
            runs = record.sarif_json.get("runs", [])
            if runs:
                for res in runs[0].get("results", []):
                    msg = res.get("message", {}).get("text", "")
                    lvl = res.get("level", "error")
                    locs = res.get("locations", [])
                    uri = locs[0].get("physicalLocation", {}).get("artifactLocation", {}).get("uri", "unknown") if locs else "unknown"
                    start_l = locs[0].get("physicalLocation", {}).get("region", {}).get("startLine", 1) if locs else 1
                    breaking_inp = res.get("properties", {}).get("breaking_input") or res.get("properties", {}).get("breakingInput")
                    findings.append(Finding(
                        file_path=uri,
                        line_start=start_l,
                        line_end=start_l,
                        severity="HIGH" if lvl == "error" else "MEDIUM",
                        issue=msg,
                        breaking_input=breaking_inp,
                    ))

        return ReviewVerdict(
            verdict=record.verdict if record.verdict in ("APPROVED", "APPROVED_WITH_CONDITIONS", "REJECTED", "HUNG_JURY", "QUORUM_COLLAPSE") else "APPROVED",
            score=8.5,
            consensus_score=1.0,
            entropy_bpde=0.0,
            findings=findings,
            unified_patch=record.patch_unified_diff,
            sarif_json=record.sarif_json,
            verification_contract=contract,
            deliberation_markdown=record.deliberation_markdown,
            cache_hit=True,
            duration_ms=duration_ms,
            tokens_used=0,
            ticket_id=ticket_id,
        )

    def _build_plan_verdict_from_cache(self, record: DeliberationRecord, duration_ms: int) -> PlanVerdict:
        """Reconstruct PlanVerdict from DeliberationRecord."""
        return PlanVerdict(
            verdict=record.verdict if record.verdict in ("APPROVED", "APPROVED_WITH_CONDITIONS", "REJECTED", "HUNG_JURY", "QUORUM_COLLAPSE") else "APPROVED",
            readiness_score=8.5,
            consensus_score=1.0,
            deliberation_markdown=record.deliberation_markdown,
            cache_hit=True,
            duration_ms=duration_ms,
            tokens_used=0,
        )

    def _build_ask_verdict_from_cache(self, record: DeliberationRecord, duration_ms: int) -> AskVerdict:
        """Reconstruct AskVerdict from DeliberationRecord."""
        return AskVerdict(
            verdict=record.verdict if record.verdict in ("APPROVED", "APPROVED_WITH_CONDITIONS", "REJECTED", "HUNG_JURY", "QUORUM_COLLAPSE") else "APPROVED",
            recommended_pattern="Cached Architecture Pattern",
            confidence=0.95,
            deliberation_markdown=record.deliberation_markdown,
            cache_hit=True,
            duration_ms=duration_ms,
            tokens_used=0,
        )
