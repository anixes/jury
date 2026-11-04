"""
src/jury/cli/plan_cmd.py - Architectural Implementation Plan Deliberation Command (`jury plan`).
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from jury.deliberation.mesh import DeliberationMesh, PlanRequest, PlanVerdict
from jury.security.scrubber import scrub_text


console = Console()


@click.command(name="plan")
@click.argument("task_string", required=False, default=None)
@click.option("--files", "files_pattern", default=None, help="Attach file(s) as architectural context (glob/path).")
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON evaluation schema.")
@click.option("--no-cache", is_flag=True, help="Bypass 5-vector composite cache and force re-deliberation.")
@click.option("--strict", is_flag=True, help="Strict exit code mode (exit 1 on REJECTED or critical risks).")
def plan_cmd(
    task_string: Optional[str],
    files_pattern: Optional[str],
    as_json: bool,
    no_cache: bool,
    strict: bool,
) -> None:
    """Deliberate an architectural implementation plan with the multi-model jury."""
    # 1. Resolve task intent from argument or stdin pipe
    task_text = ""
    if task_string and task_string.strip() and task_string.strip() != "-":
        task_text = task_string.strip()
    elif not sys.stdin.isatty():
        task_text = sys.stdin.read().strip()

    if not task_text:
        click.echo("Error: Task description or plan content required as an argument or via standard input.", err=True)
        sys.exit(3)

    # 2. Attach context files if requested
    context_text = ""
    if files_pattern:
        matched = glob.glob(files_pattern, recursive=True)
        if not matched and Path(files_pattern).exists():
            matched = [files_pattern]
        if not matched:
            click.echo(f"Error: Specified context file or pattern not found: {files_pattern}", err=True)
            sys.exit(3)

        attached = []
        for f_path in matched[:20]:  # Limit to 20 files
            p = Path(f_path)
            if p.is_file():
                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                    attached.append(f"--- File: {p} ---\n{content}\n")
                except Exception as e:
                    click.echo(f"Error reading context file {p}: {e}", err=True)
                    sys.exit(3)
        context_text = "\n".join(attached)

    # 3. Pre-flight Secret Scrubbing Check
    scrubbed = scrub_text(task_text + "\n" + context_text)
    if scrubbed.redactions and strict:
        click.echo(
            f"Security Violation: Detected {len(scrubbed.redactions)} unredacted secret(s) in plan context.",
            err=True,
        )
        sys.exit(4)

    # 4. Invoke DeliberationMesh
    is_mock = os.environ.get("JURY_OFFLINE_TEST") == "1" or os.environ.get("JURY_MOCK_MODE") == "1"
    mesh = DeliberationMesh(mock_mode=is_mock)

    req = PlanRequest(
        task_intent=task_text,
        plan_text=task_text,
        context_text=context_text,
        use_cache=not no_cache,
        repo_root=Path.cwd(),
        caller_tool="cli",
    )

    try:
        verdict: PlanVerdict = asyncio.run(mesh.plan(req))
    except Exception as e:
        if "ERR_QUORUM_COLLAPSE" in str(e) or "QuorumError" in type(e).__name__:
            click.echo(f"Quorum Collapse: {e}", err=True)
            sys.exit(2)
        click.echo(f"Deliberation error: {e}", err=True)
        sys.exit(1)

    # 5. Handle Quorum Collapse
    if verdict.verdict == "QUORUM_COLLAPSE" or verdict.error_code == "ERR_QUORUM_COLLAPSE":
        if as_json:
            click.echo(json.dumps({
                "ticket_id": f"tkt_{uuid.uuid4().hex[:8]}",
                "verdict": "QUORUM_COLLAPSE",
                "error": "Quorum collapse",
            }, indent=2))
        else:
            console.print("[bold red]QUORUM COLLAPSE: Insufficient operational distinct model families.[/bold red]")
        sys.exit(2)

    # 6. Format and Emit Output
    ticket_id = f"tkt_{uuid.uuid4().hex[:8]}"

    # Synthesis of file execution plan steps
    file_execution_plan = []
    if verdict.recommendations:
        for idx, rec in enumerate(verdict.recommendations, 1):
            file_execution_plan.append({"step": idx, "description": rec})
    else:
        file_execution_plan.append({"step": 1, "description": f"Execute implementation for: {task_text[:60]}"})

    if as_json:
        payload = {
            "ticket_id": ticket_id,
            "verdict": verdict.verdict,
            "readiness_score": verdict.readiness_score,
            "consensus_score": verdict.consensus_score,
            "criteria_scores": verdict.criteria_scores,
            "critical_risks": [
                {
                    "risk_id": r.risk_id,
                    "title": r.title,
                    "severity": r.severity,
                    "description": r.description,
                    "mitigation": r.mitigation,
                }
                for r in verdict.critical_risks
            ],
            "recommendations": verdict.recommendations,
            "required_modifications": verdict.required_modifications,
            "file_execution_plan": file_execution_plan,
            "architectural_consensus": {
                "verdict": verdict.verdict,
                "readiness_score": verdict.readiness_score,
                "consensus_score": verdict.consensus_score,
                "juror_scores": verdict.juror_scores,
            },
            "cache_hit": verdict.cache_hit,
            "duration_ms": verdict.duration_ms,
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        status_color = "green" if "APPROVED" in verdict.verdict else "red"
        console.print(
            Panel(
                f"[bold {status_color}]Verdict: {verdict.verdict}[/bold {status_color}] | "
                f"Readiness Score: [bold cyan]{verdict.readiness_score}/10.0[/bold cyan] | "
                f"Consensus: [bold magenta]{verdict.consensus_score:.2f}[/bold magenta]\n"
                f"[dim]Task: {task_text[:50]}... | Cache: {'HIT' if verdict.cache_hit else 'MISS'} | Duration: {verdict.duration_ms}ms[/dim]",
                title="Architectural Implementation Plan Verdict",
                border_style=status_color,
            )
        )

        if verdict.deliberation_markdown:
            console.print(Markdown(verdict.deliberation_markdown))

        if verdict.recommendations:
            rec_table = Table(title="Synthesized Recommendations & Execution Steps", show_header=True)
            rec_table.add_column("Step #", style="bold", width=8)
            rec_table.add_column("Recommendation", style="white")
            for idx, r in enumerate(verdict.recommendations, 1):
                rec_table.add_row(str(idx), r)
            console.print(rec_table)

    # 7. Exit Code Evaluation
    if strict and (verdict.verdict == "REJECTED" or any(r.severity in ("CRITICAL", "HIGH") for r in verdict.critical_risks)):
        sys.exit(1)
    if verdict.verdict == "REJECTED":
        sys.exit(1)

    sys.exit(0)
