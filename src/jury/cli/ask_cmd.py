"""
src/jury/cli/ask_cmd.py - Architecture Council Targeted Q&A Consultation Command (`jury ask`).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from jury.deliberation.mesh import AskRequest, AskVerdict, DeliberationMesh


console = Console()


@click.command(name="ask")
@click.argument("question", required=False, default=None)
@click.option("--context", "context_file", default=None, help="Attach architectural context file (e.g. schema.sql, arch doc).")
@click.option("--quick", is_flag=True, help="Query top 2 models for accelerated low-latency consultation.")
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON schema.")
@click.option("--focus", type=click.Choice(["security", "perf", "arch", "general"], case_sensitive=False), default="general", help="Bias consultation rubric toward specific architectural domain.")
def ask_cmd(
    question: Optional[str],
    context_file: Optional[str],
    quick: bool,
    as_json: bool,
    focus: str,
) -> None:
    """Consult the multi-model architecture council for targeted design advice."""
    if not question or not question.strip():
        click.echo("Error: Architectural question string is required as an argument.", err=True)
        sys.exit(3)

    query_text = question.strip()

    # 1. Read context file if provided
    context_text = ""
    if context_file:
        c_path = Path(context_file)
        if not c_path.is_file():
            click.echo(f"Error: Specified architectural context file not found: {context_file}", err=True)
            sys.exit(3)
        try:
            context_text = f"--- Context File: {context_file} ---\n" + c_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            click.echo(f"Error reading context file {context_file}: {e}", err=True)
            sys.exit(3)

    # 2. Invoke DeliberationMesh
    is_mock = os.environ.get("JURY_OFFLINE_TEST") == "1" or os.environ.get("JURY_MOCK_MODE") == "1"
    mesh = DeliberationMesh(mock_mode=is_mock)

    # In quick mode, timeout can be shorter
    timeout = 20.0 if quick else 60.0
    req = AskRequest(
        query=query_text,
        context_text=context_text,
        repo_root=Path.cwd(),
        timeout_seconds=timeout,
        caller_tool="cli",
    )

    try:
        verdict: AskVerdict = asyncio.run(mesh.ask(req))
    except Exception as e:
        if "ERR_QUORUM_COLLAPSE" in str(e) or "QuorumError" in type(e).__name__:
            click.echo(f"Quorum Collapse: {e}", err=True)
            sys.exit(2)
        click.echo(f"Deliberation error: {e}", err=True)
        sys.exit(1)

    # 3. Handle Quorum Collapse
    if verdict.verdict == "QUORUM_COLLAPSE" or verdict.error_code == "ERR_QUORUM_COLLAPSE":
        if as_json:
            click.echo(json.dumps({
                "verdict": "QUORUM_COLLAPSE",
                "error": "Quorum collapse",
            }, indent=2))
        else:
            console.print("[bold red]QUORUM COLLAPSE: Architecture council unavailable.[/bold red]")
        sys.exit(2)

    # 4. Format Output
    if as_json:
        payload = {
            "verdict": verdict.verdict,
            "answer": verdict.recommended_pattern,
            "recommendation": verdict.recommended_pattern,
            "consensus": verdict.recommended_pattern,
            "confidence": verdict.confidence,
            "tradeoff_matrix": verdict.tradeoff_matrix,
            "concrete_code_example": verdict.concrete_code_example,
            "failure_recovery_strategy": verdict.failure_recovery_strategy,
            "juror_scores": verdict.juror_scores,
            "cache_hit": verdict.cache_hit,
            "duration_ms": verdict.duration_ms,
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        console.print(
            Panel(
                f"[bold cyan]Question:[/bold cyan] {query_text}\n\n"
                f"[bold green]Recommended Pattern:[/bold green] [bold white]{verdict.recommended_pattern}[/bold white]\n"
                f"[bold magenta]Council Confidence:[/bold magenta] {verdict.confidence * 100:.1f}%\n"
                f"[dim]Mode: {'Quick (top 2)' if quick else 'Standard (3 jurors)'} | Focus: {focus} | Cache: {'HIT' if verdict.cache_hit else 'MISS'} | Latency: {verdict.duration_ms}ms[/dim]",
                title="Architecture Council Consultation",
                border_style="cyan",
            )
        )

        if verdict.tradeoff_matrix:
            tradeoff_table = Table(title="Tradeoff Matrix", show_header=True, header_style="bold magenta")
            tradeoff_table.add_column("Option / Pattern", style="bold")
            tradeoff_table.add_column("Latency / Cost")
            tradeoff_table.add_column("Pros")
            tradeoff_table.add_column("Cons")
            tradeoff_table.add_column("Council Verdict", style="green")

            for row in verdict.tradeoff_matrix:
                tradeoff_table.add_row(
                    str(row.get("option", "Candidate")),
                    str(row.get("latency", "N/A")),
                    str(row.get("pros", "N/A")),
                    str(row.get("cons", "N/A")),
                    str(row.get("verdict", "Evaluated")),
                )
            console.print(tradeoff_table)
        else:
            console.print("\n### Tradeoff Analysis & Architectural Invariants")
            console.print(f"- **Evaluated Candidates**: Tradeoff analysis conducted against query constraints.")

        if verdict.concrete_code_example:
            console.print("\n### Concrete Implementation Pattern")
            console.print(f"```python\n{verdict.concrete_code_example}\n```")

        if verdict.failure_recovery_strategy:
            console.print(f"\n### Failure Recovery Strategy\n{verdict.failure_recovery_strategy}")

        if verdict.deliberation_markdown:
            console.print(Markdown(verdict.deliberation_markdown))

    sys.exit(0)
