"""
src/jury/mcp_server/tools.py - MCP Tool Handlers & Schema Definitions for Jury Mesh v3.0.

Exposes:
- `jury_plan`: Architecture implementation planning and review
- `jury_review`: Git diff peer review and machine-actionable patch/SARIF synthesis
- `jury_ask`: Targeted architecture council Q&A and tradeoff consultation
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from jury.deliberation.mesh import (
    AskRequest,
    AskVerdict,
    DeliberationMesh,
    PlanRequest,
    PlanVerdict,
    ReviewRequest,
    ReviewVerdict,
)
from jury.security.scrubber import scrub_text
from jury.state.cache_key import CompositeCacheKeyGenerator
from jury.state.engine import JuryStateEngine

logger = logging.getLogger("jury.mcp.tools")


class InvalidParamsError(Exception):
    """Raised when required parameters are missing or invalid (maps to JSON-RPC -32602)."""
    def __init__(self, message: str):
        super().__init__(message)
        self.code = -32602


def get_tool_definitions() -> List[Dict[str, Any]]:
    """
    Return MCP tool definitions with JSON schema input specifications for tools/list.
    """
    return [
        {
            "name": "jury_plan",
            "description": (
                "Deliberate an architectural implementation plan with the multi-model jury. "
                "Synthesizes consensus readiness score, critical risks, required modifications, "
                "and an actionable file execution plan."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The engineering task, specification, or implementation plan to deliberate.",
                    },
                    "context_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of context file paths or globs to attach to the deliberation.",
                    },
                    "async_mode": {
                        "type": "boolean",
                        "description": (
                            "If true, immediately registers a singleflight task ticket (<10ms) and returns "
                            "the ticket identifier for asynchronous follow-up."
                        ),
                        "default": False,
                    },
                    "options": {
                        "type": "object",
                        "description": "Optional execution flags: no_cache (bool), strict (bool), timeout_seconds (float).",
                        "properties": {
                            "no_cache": {"type": "boolean"},
                            "strict": {"type": "boolean"},
                            "timeout_seconds": {"type": "number"},
                        },
                    },
                },
                "required": ["task"],
            },
        },
        {
            "name": "jury_review",
            "description": (
                "Review code changes or git diffs with the multi-model jury panel. "
                "Performs blind ballots, adversarial cross-examination, and decoupled arbitration, "
                "emitting findings, POSIX unified diff patches, and OASIS SARIF v2.1.0 diagnostics."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "diff": {
                        "type": "string",
                        "description": "The unified git diff to review. If omitted, automatically reads from git.",
                    },
                    "staged": {
                        "type": "boolean",
                        "description": "Review staged git changes (git diff --cached). Default: False.",
                        "default": False,
                    },
                    "branch": {
                        "type": "string",
                        "description": "Target branch to diff against (e.g. main, master).",
                    },
                    "focus": {
                        "type": "string",
                        "enum": ["security", "perf", "arch", "general"],
                        "description": "Bias deliberation rubric toward a specific domain.",
                        "default": "general",
                    },
                    "strict": {
                        "type": "boolean",
                        "description": "Enforce strict security and finding checks (fails on unredacted secrets).",
                        "default": False,
                    },
                },
            },
        },
        {
            "name": "jury_ask",
            "description": (
                "Consult the multi-model architecture council for targeted design advice. "
                "Returns recommended architectural pattern, confidence score, tradeoff matrix, "
                "and concrete implementation examples."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The architectural question or design query to consult on.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Alternative alias for 'question'.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context string or relative file path to context documentation/code.",
                    },
                    "quick": {
                        "type": "boolean",
                        "description": "Query top 2 models for accelerated low-latency consultation.",
                        "default": False,
                    },
                    "focus": {
                        "type": "string",
                        "enum": ["security", "perf", "arch", "general"],
                        "description": "Focus domain for architecture council evaluation.",
                        "default": "general",
                    },
                },
                "required": ["question"],
            },
        },
    ]


async def execute_jury_plan(
    arguments: Dict[str, Any],
    mesh: DeliberationMesh,
    state_engine: JuryStateEngine,
    repo_root: Path,
) -> Dict[str, Any]:
    """
    Execute jury_plan tool invocation.
    """
    task = arguments.get("task")
    if not task or not isinstance(task, str) or not task.strip():
        raise InvalidParamsError("Missing required parameter 'task'")
    task = task.strip()

    # 1. Asynchronous ticket mode (<10ms return)
    if arguments.get("async_mode", False):
        cache_key = CompositeCacheKeyGenerator.compute_cache_key(
            intent=task,
            repo_root=repo_root,
            config_hash="",
            prompt_version="3.0.0",
        )
        ticket = state_engine.singleflight_enter(
            cache_key=cache_key,
            timeout_seconds=60.0,
            command="plan",
            intent=task,
        )
        ticket_id = (
            ticket.ticket_id
            if ticket.ticket_id and ticket.ticket_id != "cache_hit"
            else f"tkt_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        )
        return {
            "ticket_id": ticket_id,
            "status": "PENDING",
            "cache_key": cache_key,
            "task": task,
            "message": f"Async deliberation ticket registered: {ticket_id}",
            "content": [
                {
                    "type": "text",
                    "text": f"Async deliberation ticket registered: {ticket_id} (Status: PENDING)",
                }
            ],
        }

    # 2. Attach context files if provided
    context_files = arguments.get("context_files") or []
    context_parts: List[str] = []
    if isinstance(context_files, list):
        for cf in context_files[:20]:
            p = Path(cf) if Path(cf).is_absolute() else (repo_root / cf)
            if p.is_file():
                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                    context_parts.append(f"--- File: {cf} ---\n{content}\n")
                except Exception as exc:
                    logger.warning("Could not read context file %s: %s", p, exc)
    context_text = "\n".join(context_parts)

    options = arguments.get("options") or {}
    use_cache = not bool(options.get("no_cache", False))
    strict = bool(options.get("strict", False))
    timeout_seconds = float(options.get("timeout_seconds", 60.0))

    # Pre-flight secret check in strict mode
    if strict:
        scrubbed = scrub_text(task + "\n" + context_text)
        if scrubbed.redactions:
            raise InvalidParamsError(
                f"Security Violation: Detected {len(scrubbed.redactions)} unredacted secret(s) in plan context."
            )

    req = PlanRequest(
        task_intent=task,
        plan_text=task,
        context_text=context_text,
        repo_root=repo_root,
        use_cache=use_cache,
        timeout_seconds=timeout_seconds,
        caller_tool="mcp",
    )

    verdict: PlanVerdict = await mesh.plan(req)

    ticket_id = f"tkt_{uuid.uuid4().hex[:8]}"
    file_execution_plan = []
    if verdict.recommendations:
        for idx, rec in enumerate(verdict.recommendations, 1):
            file_execution_plan.append({"step": idx, "description": rec})
    else:
        file_execution_plan.append({"step": 1, "description": f"Execute plan: {task[:60]}"})

    critical_risks_data = [
        {
            "risk_id": r.risk_id,
            "title": r.title,
            "severity": r.severity,
            "mitigation": r.mitigation,
        }
        for r in verdict.critical_risks
    ]

    return {
        "ticket_id": ticket_id,
        "verdict": verdict.verdict,
        "readiness_score": verdict.readiness_score,
        "consensus_score": verdict.consensus_score,
        "criteria_scores": verdict.criteria_scores,
        "critical_risks": critical_risks_data,
        "recommendations": verdict.recommendations,
        "file_execution_plan": file_execution_plan,
        "deliberation_markdown": verdict.deliberation_markdown,
        "cache_hit": verdict.cache_hit,
        "duration_ms": verdict.duration_ms,
        "tokens_used": verdict.tokens_used,
        "content": [
            {
                "type": "text",
                "text": (
                    f"Jury Plan Verdict: {verdict.verdict} (Readiness Score: {verdict.readiness_score:.2f}, "
                    f"Consensus: {verdict.consensus_score:.2f})\n\n{verdict.deliberation_markdown}"
                ),
            }
        ],
    }


async def execute_jury_review(
    arguments: Dict[str, Any],
    mesh: DeliberationMesh,
    state_engine: JuryStateEngine,
    repo_root: Path,
) -> Dict[str, Any]:
    """
    Execute jury_review tool invocation.
    """
    diff_text = arguments.get("diff")
    staged = bool(arguments.get("staged", False))
    branch = arguments.get("branch")
    focus = arguments.get("focus", "general")
    strict = bool(arguments.get("strict", False))

    # If diff not provided directly, read via git diff
    if diff_text is None or (isinstance(diff_text, str) and not diff_text.strip()):
        git_cmd = ["git", "diff"]
        if staged:
            git_cmd.append("--cached")
        elif branch:
            git_cmd.append(str(branch))

        try:
            proc = subprocess.run(
                git_cmd,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            diff_text = proc.stdout
        except Exception as exc:
            logger.warning("Error running git diff: %s", exc)
            diff_text = ""

        # Auto-detect staged if unstaged is empty and no branch specified
        if not diff_text.strip() and not staged and not branch:
            try:
                proc_staged = subprocess.run(
                    ["git", "diff", "--cached"],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc_staged.stdout.strip():
                    diff_text = proc_staged.stdout
            except Exception:
                pass

    diff_text = diff_text or ""

    if strict:
        scrubbed = scrub_text(diff_text)
        if scrubbed.redactions:
            raise InvalidParamsError(
                f"Security Violation: Detected {len(scrubbed.redactions)} unredacted secret(s) in review diff."
            )

    target_desc = "staged changes" if staged else (f"branch {branch}" if branch else "working tree diff")
    req = ReviewRequest(
        task_intent=f"MCP review of {target_desc} (focus: {focus})",
        diff_text=diff_text,
        git_target="staged" if staged else ("branch" if branch else "unstaged"),
        context_text="",
        repo_root=repo_root,
        caller_tool="mcp",
    )

    verdict: ReviewVerdict = await mesh.review(req)

    ticket_id = f"tkt_{uuid.uuid4().hex[:8]}"
    findings_data = [
        {
            "id": f.id,
            "severity": f.severity,
            "title": f.title,
            "description": f.description,
            "file_path": f.file_path,
            "line_number": f.line_number,
            "suggested_fix": f.suggested_fix,
        }
        for f in verdict.findings
    ]

    return {
        "ticket_id": ticket_id,
        "verdict": verdict.verdict,
        "score": verdict.score,
        "consensus_score": verdict.consensus_score,
        "entropy_bpde": verdict.entropy_bpde,
        "findings": findings_data,
        "unified_patch": verdict.unified_patch,
        "sarif_json": verdict.sarif_json,
        "deliberation_markdown": verdict.deliberation_markdown,
        "cache_hit": verdict.cache_hit,
        "duration_ms": verdict.duration_ms,
        "tokens_used": verdict.tokens_used,
        "content": [
            {
                "type": "text",
                "text": (
                    f"Jury Review Verdict: {verdict.verdict} (Score: {verdict.score:.2f}, "
                    f"Consensus: {verdict.consensus_score:.2f})\n\n{verdict.deliberation_markdown}"
                ),
            }
        ],
    }


async def execute_jury_ask(
    arguments: Dict[str, Any],
    mesh: DeliberationMesh,
    state_engine: JuryStateEngine,
    repo_root: Path,
) -> Dict[str, Any]:
    """
    Execute jury_ask tool invocation.
    """
    q = arguments.get("question") or arguments.get("query")
    if not q or not isinstance(q, str) or not q.strip():
        raise InvalidParamsError("Missing required parameter 'question' or 'query'")
    question_text = q.strip()

    context = arguments.get("context", "")
    context_text = ""
    if context and isinstance(context, str):
        c_path = Path(context) if Path(context).is_absolute() else (repo_root / context)
        if c_path.is_file():
            try:
                context_text = f"--- Context File: {context} ---\n" + c_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                logger.warning("Error reading context file %s: %s", c_path, exc)
                context_text = str(context)
        else:
            context_text = str(context)

    quick = bool(arguments.get("quick", False))
    timeout = 20.0 if quick else 60.0

    req = AskRequest(
        query=question_text,
        context_text=context_text,
        repo_root=repo_root,
        timeout_seconds=timeout,
        caller_tool="mcp",
    )

    verdict: AskVerdict = await mesh.ask(req)

    ticket_id = f"tkt_{uuid.uuid4().hex[:8]}"

    return {
        "ticket_id": ticket_id,
        "verdict": verdict.verdict,
        "recommended_pattern": verdict.recommended_pattern,
        "confidence": verdict.confidence,
        "tradeoff_matrix": verdict.tradeoff_matrix,
        "concrete_code_example": verdict.concrete_code_example,
        "failure_recovery_strategy": verdict.failure_recovery_strategy,
        "deliberation_markdown": verdict.deliberation_markdown,
        "cache_hit": verdict.cache_hit,
        "duration_ms": verdict.duration_ms,
        "tokens_used": verdict.tokens_used,
        "content": [
            {
                "type": "text",
                "text": (
                    f"Jury Architecture Council Recommendation:\n{verdict.recommended_pattern}\n\n"
                    f"Confidence: {verdict.confidence:.2f}\n\n{verdict.deliberation_markdown}"
                ),
            }
        ],
    }
