"""
triad.client - Resilient Multi-Model Gateway Client for Triad CLI.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

try:
    from openai import AsyncOpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


@dataclass
class ModelEvaluationResult:
    model: str
    label: str
    success: bool
    content: str = ""
    reasoning: str = ""
    final_text: str = ""
    score: Optional[float] = None
    verdict: str = "UNKNOWN"
    duration_s: float = 0.0
    error: Optional[str] = None
    output_file: Optional[Path] = None


def extract_score_and_verdict(text: str) -> Tuple[Optional[float], str]:
    """Parse numeric score and verdict from model response text."""
    score: Optional[float] = None
    verdict: str = "UNKNOWN"

    # Score patterns: "Score: 8.5/10", "Production Readiness Score: 9/10", "8.5 / 10"
    score_match = re.search(r"(?:score|readiness|rating):\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*10", text, re.IGNORECASE)
    if not score_match:
        score_match = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*/\s*10\b", text)
    if score_match:
        try:
            score = float(score_match.group(1))
        except ValueError:
            pass

    # Verdict patterns: "APPROVED WITH COMMENTS", "APPROVED", "REJECTED"
    if re.search(r"\bAPPROVED\s+WITH\s+COMMENTS\b", text, re.IGNORECASE):
        verdict = "APPROVED WITH COMMENTS"
    elif re.search(r"\bREJECTED\b", text, re.IGNORECASE):
        verdict = "REJECTED"
    elif re.search(r"\bAPPROVED\b", text, re.IGNORECASE):
        verdict = "APPROVED"

    return score, verdict


def extract_response_text(choice: Any) -> Tuple[str, str, str]:
    """
    Uniformly extract standard content, reasoning tokens, and merged final text.
    Handles extended-thinking models where review output may be in reasoning/reasoning_content.
    """
    content = getattr(choice.message, "content", "") or ""
    reasoning = (
        getattr(choice.message, "reasoning", None)
        or getattr(choice.message, "reasoning_content", None)
        or ""
    )
    final_text = content.strip() if content.strip() else reasoning.strip()
    return content, reasoning, final_text


async def query_model_with_retry(
    client: Any,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 6000,
    temperature: float = 0.2,
    max_retries: int = 3,
    initial_backoff: float = 2.0,
) -> Tuple[bool, str, str, str, Optional[str]]:
    """Query a model with exponential backoff on HTTP 429 and transient 5xx errors."""
    backoff = initial_backoff
    last_err = None

    for attempt in range(1, max_retries + 1):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            choice = response.choices[0]
            content, reasoning, final_text = extract_response_text(choice)
            return True, content, reasoning, final_text, None
        except Exception as e:
            last_err = str(e)
            is_rate_limit = "429" in last_err or "rate limit" in last_err.lower()
            is_server_err = "500" in last_err or "502" in last_err or "503" in last_err
            if (is_rate_limit or is_server_err) and attempt < max_retries:
                await asyncio.sleep(backoff)
                backoff *= 2.0
                continue
            break

    return False, "", "", "", last_err


async def run_multi_model_query(
    mode: str,
    models: List[str],
    gateway_url: str,
    api_key: str,
    user_prompt: str,
    system_prompt: str,
    output_dir: Path,
    labels: Optional[List[str]] = None,
    max_tokens: int = 6000,
    max_concurrency: int = 10,
) -> List[ModelEvaluationResult]:
    """Execute evaluation across all configured models concurrently with bounded concurrency."""
    if not HAS_OPENAI:
        raise RuntimeError("openai package is required. Install via 'pip install openai'.")

    output_dir.mkdir(parents=True, exist_ok=True)
    client = AsyncOpenAI(api_key=api_key, base_url=gateway_url)
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _worker(model_id: str, label_name: str) -> ModelEvaluationResult:
        async with semaphore:
            t0 = time.perf_counter()
            clean_model_tag = re.sub(r"[^a-zA-Z0-9_\-]", "_", model_id).lower()
            out_file = output_dir / f"{clean_model_tag}_{mode}.md"

            success, content, reasoning, final_text, err = await query_model_with_retry(
                client=client,
                model=model_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
            )
            duration = time.perf_counter() - t0
            score, verdict = extract_score_and_verdict(final_text)

            if success and final_text:
                out_file.write_text(final_text, encoding="utf-8")
                return ModelEvaluationResult(
                    model=model_id,
                    label=label_name,
                    success=True,
                    content=content,
                    reasoning=reasoning,
                    final_text=final_text,
                    score=score,
                    verdict=verdict,
                    duration_s=duration,
                    output_file=out_file,
                )
            else:
                return ModelEvaluationResult(
                    model=model_id,
                    label=label_name,
                    success=False,
                    duration_s=duration,
                    error=err or "Empty response",
                    output_file=out_file,
                )

    tasks = []
    for idx, m in enumerate(models):
        lbl = labels[idx] if (labels and idx < len(labels)) else f"Triad Model #{idx+1}"
        tasks.append(_worker(m, lbl))

    results = await asyncio.gather(*tasks)
    return list(results)
