"""
triad.debate - Multi-Round Deliberation & Cross-Critique Engine.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

from triad.client import (
    ModelEvaluationResult,
    extract_score_and_verdict,
    query_model_with_retry,
    run_multi_model_query,
)

DEFAULT_DEBATE_CONSULT_SYSTEM_PROMPT = """You are a Principal Software Systems Architect participating in a multi-model architectural deliberation panel.
Rigorously critique peer proposals, challenge weak assumptions or oversights, adopt superior solutions, and synthesize a refined, battle-tested architectural design.
"""

DEFAULT_DEBATE_REVIEW_SYSTEM_PROMPT = """You are an Elite Principal Code Reviewer participating in a multi-model peer review panel.
Rigorously evaluate peer review findings, verify or rebut flagged issues, calibrate severity and scores, and deliver your refined, post-debate code review.
"""


def build_cross_critique_prompt(
    mode: str,
    original_prompt: str,
    own_result: ModelEvaluationResult,
    peer_results: List[ModelEvaluationResult],
    round_idx: int,
    total_rounds: int,
    max_peer_chars: int = 30000,
) -> str:
    """
    Construct the deliberation prompt for a model in Round N (N >= 2).
    Includes the original objective, peer submissions from the previous round, and specific deliberation directives.
    """
    blocks = [
        f"### Multi-Model Deliberation & Cross-Critique (Round {round_idx} of {total_rounds})",
        "You are participating in an expert deliberation panel. In the previous round, all panelists evaluated the proposal independently.",
        "",
        "### Original Objective & Input Context:",
        original_prompt.strip(),
        "",
        "---",
        "### Preceding Round Submissions from Peer Panelists:",
    ]

    for peer in peer_results:
        peer_text = peer.final_text.strip()
        if len(peer_text) > max_peer_chars:
            peer_text = peer_text[:max_peer_chars] + "\n\n... [Truncated for length]"
        blocks.extend([
            f"#### Peer Panelist: {peer.label} (`{peer.model}`)",
            f"- Status: {'SUCCESS' if peer.success else 'FAILED'}",
            f"- Previous Verdict: **{peer.verdict}**",
            f"- Previous Score: {f'{peer.score:.1f}/10' if peer.score is not None else 'N/A'}",
            "",
            "```markdown",
            peer_text,
            "```",
            "",
        ])

    blocks.extend([
        "---",
        f"### Your Previous Submission ({own_result.label}):",
        f"- Previous Verdict: **{own_result.verdict}**",
        f"- Previous Score: {f'{own_result.score:.1f}/10' if own_result.score is not None else 'N/A'}",
        "",
        "```markdown",
        own_result.final_text.strip()[:max_peer_chars],
        "```",
        "",
        "---",
        f"### Directives for Deliberation Round {round_idx}:",
        "1. **Cross-Critique & Vulnerability Analysis:**",
        "   - Critically evaluate the peers' proposals. Did they identify critical risks, performance bottlenecks, edge cases, or platform incompatibilities you missed?",
        "   - Identify any flaws, false positives, or unworkable assumptions in the peers' proposals.",
        "2. **Point-by-Point Reconciliation:**",
        "   - Clearly acknowledge which peer recommendations you AGREE with and incorporate.",
        "   - State which peer points you DISAGREE with, giving concrete technical justification.",
        "3. **Final Refined Architecture & Verdict:**",
        "   - Deliver your updated, definitive technical recommendations and implementation plan.",
    ])

    if mode == "review":
        blocks.append(
            "   - Conclude with your calibrated numeric Score (X.X/10) and final Verdict "
            "(APPROVED, APPROVED WITH COMMENTS, or REJECTED)."
        )
    else:
        blocks.append(
            "   - Conclude with your calibrated Readiness Score (X.X/10) and final Verdict "
            "(APPROVED, APPROVED WITH COMMENTS, or REJECTED)."
        )

    return "\n\n".join(blocks)


async def run_multi_model_debate(
    mode: str,
    models: List[str],
    gateway_url: str,
    api_key: str,
    user_prompt: str,
    system_prompt: str,
    output_dir: Path,
    labels: Optional[List[str]] = None,
    total_rounds: int = 2,
    max_tokens: int = 6000,
    max_concurrency: int = 10,
) -> List[ModelEvaluationResult]:
    """
    Execute multi-round deliberation between models:
    - Round 1: Blind independent evaluation (isolated).
    - Round 2..N: Cross-critique with peer outputs and synthesis.
    """
    if len(models) < 2 or total_rounds < 2:
        if len(models) < 2:
            print("[WARN] Debate mode requires at least 2 models. Falling back to single-round evaluation.", flush=True)
        return await run_multi_model_query(
            mode=mode,
            models=models,
            gateway_url=gateway_url,
            api_key=api_key,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            output_dir=output_dir,
            labels=labels,
            max_tokens=max_tokens,
            max_concurrency=max_concurrency,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    assigned_labels = (
        labels
        if labels and len(labels) == len(models)
        else [f"Triad Panelist {i+1}" for i in range(len(models))]
    )

    # ---------------------------------------------------------
    # Round 1: Blind Independent Evaluation
    # ---------------------------------------------------------
    print(f"\n[DEBATE] === Round 1/{total_rounds}: Blind Independent Assessment ===", flush=True)
    round1_results = await run_multi_model_query(
        mode=mode,
        models=models,
        gateway_url=gateway_url,
        api_key=api_key,
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        output_dir=output_dir,
        labels=assigned_labels,
        max_tokens=max_tokens,
        max_concurrency=max_concurrency,
    )

    current_results: List[ModelEvaluationResult] = []
    for r in round1_results:
        clean_model_tag = re.sub(r"[^a-zA-Z0-9_\-]", "_", r.model).lower()
        r1_file = output_dir / f"{clean_model_tag}_{mode}_round1.md"
        if r.final_text:
            r1_file.write_text(r.final_text, encoding="utf-8")

        rounds_list = [{
            "round": 1,
            "score": r.score,
            "verdict": r.verdict,
            "duration_s": r.duration_s,
            "output_file": str(r1_file),
            "text": r.final_text,
        }]
        r.rounds = rounds_list
        score_str = f"Score: {r.score:.1f}/10" if r.score is not None else "Score: N/A"
        print(f"  [Round 1] {r.model} -> {r.verdict} ({score_str}) in {r.duration_s:.1f}s", flush=True)
        current_results.append(r)

    # ---------------------------------------------------------
    # Rounds 2..N: Cross-Critique & Deliberation
    # ---------------------------------------------------------
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=gateway_url)
    semaphore = asyncio.Semaphore(max_concurrency)

    debate_system_prompt = (
        DEFAULT_DEBATE_REVIEW_SYSTEM_PROMPT
        if mode == "review"
        else DEFAULT_DEBATE_CONSULT_SYSTEM_PROMPT
    )

    for round_idx in range(2, total_rounds + 1):
        print(f"\n[DEBATE] === Round {round_idx}/{total_rounds}: Cross-Critique & Deliberation ===", flush=True)

        async def _run_critique(idx: int) -> Tuple[int, bool, str, str, str, float, Optional[str]]:
            own_res = current_results[idx]
            peer_res = [res for j, res in enumerate(current_results) if j != idx and res.success]
            critique_prompt = build_cross_critique_prompt(
                mode=mode,
                original_prompt=user_prompt,
                own_result=own_res,
                peer_results=peer_res,
                round_idx=round_idx,
                total_rounds=total_rounds,
            )

            async with semaphore:
                t0 = time.perf_counter()
                success, content, reasoning, final_text, err = await query_model_with_retry(
                    client=client,
                    model=own_res.model,
                    system_prompt=debate_system_prompt,
                    user_prompt=critique_prompt,
                    max_tokens=max_tokens,
                )
                duration = time.perf_counter() - t0
                return idx, success, content, reasoning, final_text, duration, err

        tasks = [_run_critique(i) for i in range(len(current_results))]
        round_outputs = await asyncio.gather(*tasks)

        for idx, success, content, reasoning, final_text, duration, err in round_outputs:
            res = current_results[idx]
            clean_model_tag = re.sub(r"[^a-zA-Z0-9_\-]", "_", res.model).lower()
            round_file = output_dir / f"{clean_model_tag}_{mode}_round{round_idx}.md"

            if success and final_text:
                round_file.write_text(final_text, encoding="utf-8")
                score, verdict = extract_score_and_verdict(final_text)

                # Overwrite primary output file with final round output
                primary_file = output_dir / f"{clean_model_tag}_{mode}.md"
                primary_file.write_text(final_text, encoding="utf-8")

                res.success = True
                res.content = content
                res.reasoning = reasoning
                res.final_text = final_text
                res.score = score
                res.verdict = verdict
                res.duration_s += duration
                res.output_file = primary_file
                res.rounds.append({
                    "round": round_idx,
                    "score": score,
                    "verdict": verdict,
                    "duration_s": duration,
                    "output_file": str(round_file),
                    "text": final_text,
                })
                score_str = f"Score: {score:.1f}/10" if score is not None else "Score: N/A"
                print(f"  [Round {round_idx}] {res.model} -> {verdict} ({score_str}) in {duration:.1f}s", flush=True)
            else:
                res.rounds.append({
                    "round": round_idx,
                    "error": err or "Deliberation failed",
                    "duration_s": duration,
                })
                print(f"  [Round {round_idx} FAILED] {res.model}: {err}", flush=True)

    return current_results
