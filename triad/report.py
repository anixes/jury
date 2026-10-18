"""
triad.report - Markdown and JSON Report Generation for Triad CLI.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List

from triad.client import ModelEvaluationResult


def generate_markdown_report(
    mode: str,
    results: List[ModelEvaluationResult],
    output_path: Path,
) -> str:
    """Format consolidated summary table and markdown report."""
    lines = [
        f"# Triad Evaluation Report: {mode.upper()}",
        f"**Date:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Summary Table",
        "| Model / Reviewer | Status | Score | Verdict | Latency | Report File |",
        "| :--- | :---: | :---: | :---: | :---: | :--- |",
    ]

    for r in results:
        status_icon = "✔ SUCCESS" if r.success else f"✘ FAILED ({r.error})"
        score_str = f"{r.score:.1f}/10" if r.score is not None else "N/A"
        file_link = f"[{r.output_file.name}]({r.output_file.as_posix()})" if r.output_file else "None"
        lines.append(
            f"| **{r.label}** (`{r.model}`) | {status_icon} | {score_str} | **{r.verdict}** | {r.duration_s:.1f}s | {file_link} |"
        )

    lines.append("\n---\n")
    lines.append("## Individual Review Summaries\n")

    for r in results:
        lines.append(f"### {r.label} (`{r.model}`)")
        if r.success and r.output_file and r.output_file.exists():
            content = r.output_file.read_text(encoding="utf-8")
            summary_snippet = content[:1500].strip()
            lines.append(f"```markdown\n{summary_snippet}\n...\n```\n")
        else:
            lines.append(f"**Error:** {r.error}\n")

    full_report = "\n".join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(full_report, encoding="utf-8")
    return full_report


def generate_json_report(
    mode: str,
    results: List[ModelEvaluationResult],
    output_path: Path,
) -> dict:
    """Format structured JSON payload for CI pipelines and automation."""
    payload = {
        "mode": mode,
        "timestamp": time.time(),
        "date_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": [
            {
                "model": r.model,
                "label": r.label,
                "success": r.success,
                "score": r.score,
                "verdict": r.verdict,
                "duration_seconds": r.duration_s,
                "error": r.error,
                "report_file": str(r.output_file) if r.output_file else None,
            }
            for r in results
        ],
        "all_passed": all(r.success and r.verdict != "REJECTED" for r in results),
        "average_score": (
            sum(r.score for r in results if r.score is not None) / len([r for r in results if r.score is not None])
            if any(r.score is not None for r in results)
            else None
        ),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
