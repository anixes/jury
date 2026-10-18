"""
triad.report - Markdown and JSON Report Generation for Triad CLI.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List

from triad.client import ModelEvaluationResult


def format_score_progression(
    rounds: Optional[List[dict]] = None,
    fallback_score: Optional[float] = None,
    arrow: str = " → ",
) -> str:
    """Format numeric score history across deliberation rounds (e.g. '8.0 → 9.0/10')."""
    if rounds:
        scores = [
            f"{rnd['score']:.1f}"
            for rnd in rounds
            if isinstance(rnd, dict) and rnd.get("score") is not None
        ]
        if len(scores) > 1:
            return f"{arrow.join(scores)}/10"
        elif scores:
            return f"{scores[0]}/10"

    if fallback_score is not None:
        return f"{fallback_score:.1f}/10"
    return "N/A"


def generate_markdown_report(
    mode: str,
    results: List[ModelEvaluationResult],
    output_path: Path,
) -> str:
    """Format consolidated summary table and markdown report."""
    is_debate = any(len(r.rounds) > 1 for r in results)
    mode_title = f"{mode.upper()} (MULTI-MODEL DEBATE)" if is_debate else mode.upper()

    lines = [
        f"# Triad Evaluation Report: {mode_title}",
        f"**Date:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Summary Table",
    ]

    if is_debate:
        lines.extend([
            "| Model / Reviewer | Status | Score Progression | Final Verdict | Total Latency | Final Report |",
            "| :--- | :---: | :---: | :---: | :---: | :--- |",
        ])
    else:
        lines.extend([
            "| Model / Reviewer | Status | Score | Verdict | Latency | Report File |",
            "| :--- | :---: | :---: | :---: | :---: | :--- |",
        ])

    for r in results:
        status_icon = "✔ SUCCESS" if r.success else f"✘ FAILED ({r.error})"
        file_link = f"[{r.output_file.name}]({r.output_file.as_posix()})" if r.output_file else "None"
        score_str = format_score_progression(r.rounds if is_debate else None, r.score, arrow=" → ")
        lines.append(
            f"| **{r.label}** (`{r.model}`) | {status_icon} | {score_str} | **{r.verdict}** | {r.duration_s:.1f}s | {file_link} |"
        )

    if is_debate:
        lines.append("\n---\n")
        lines.append("## Deliberation Progression by Round\n")
        for r in results:
            lines.append(f"### {r.label} (`{r.model}`)")
            if r.rounds:
                for rnd in r.rounds:
                    if not isinstance(rnd, dict):
                        continue
                    r_num = rnd.get("round", "?")
                    r_verdict = rnd.get("verdict", "N/A")
                    r_score = f"{rnd['score']:.1f}/10" if rnd.get("score") is not None else "N/A"
                    r_file = Path(rnd["output_file"]).name if rnd.get("output_file") else "N/A"
                    r_dur = f"{rnd['duration_s']:.1f}s" if "duration_s" in rnd else ""
                    dur_part = f" ({r_dur})" if r_dur else ""
                    lines.append(
                        f"- **Round {r_num}:** Verdict: `{r_verdict}` | Score: `{r_score}` | Output: `{r_file}`{dur_part}"
                    )
            else:
                lines.append("- *No round history available.*")
            lines.append("")

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
    is_debate = any(len(r.rounds) > 1 for r in results)
    payload = {
        "mode": mode,
        "debate_enabled": is_debate,
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
                "rounds": r.rounds,
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
