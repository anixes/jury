"""
src/jury/cli/review_cmd.py - Git Diff Peer Review & Zero-Token Synthesis Command (`jury review`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from jury.deliberation.mesh import DeliberationMesh, ReviewRequest, ReviewVerdict
from jury.security.scrubber import scrub_text


console = Console()


@click.command(name="review")
@click.option("--staged", is_flag=True, help="Review git staged diff (git diff --cached).")
@click.option("--unstaged", is_flag=True, help="Review unstaged working tree diff (git diff).")
@click.option("--all", "all_changes", is_flag=True, help="Review all working tree changes (HEAD).")
@click.option("--branch", default=None, help="Review diff against target git branch.")
@click.option("--files", "files_pattern", default=None, help="Filter diff to specific files or pathspec.")
@click.option("--strict", is_flag=True, help="Strict mode: exit 1 on rejection/critical finding; exit 4 on secrets.")
@click.option("--focus", type=click.Choice(["security", "perf", "arch", "general"], case_sensitive=False), default="general", help="Bias rubric evaluation focus.")
@click.option("--format", "output_format", type=click.Choice(["text", "json", "patch", "sarif"], case_sensitive=False), default="text", help="Output format.")
@click.option("--json", "json_flag", is_flag=True, help="Shorthand for --format json.")
@click.option("--intent", default=None, help="Optional intent or task description for the review.")
@click.argument("extra_files", nargs=-1, type=click.Path())
def review_cmd(
    staged: bool,
    unstaged: bool,
    all_changes: bool,
    branch: Optional[str],
    files_pattern: Optional[str],
    strict: bool,
    focus: str,
    output_format: str,
    json_flag: bool,
    intent: Optional[str],
    extra_files: tuple[str, ...],
) -> None:
    """Deliberate code changes and review git diffs with the multi-model jury."""
    # Resolve format
    fmt = "json" if json_flag else output_format.lower()

    # 1. Collect git diff
    git_cmd = ["git", "diff"]
    target_desc = "unstaged working tree"

    if staged:
        git_cmd.append("--cached")
        target_desc = "staged changes"
    elif all_changes:
        git_cmd.append("HEAD")
        target_desc = "all changes"
    elif branch:
        git_cmd.append(branch)
        target_desc = f"branch {branch}"
    elif unstaged:
        target_desc = "unstaged working tree"

    pathspecs = []
    if files_pattern:
        pathspecs.append(files_pattern)
    if extra_files:
        pathspecs.extend(extra_files)

    if pathspecs:
        git_cmd.extend(["--", *pathspecs])

    try:
        proc = subprocess.run(git_cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 and all_changes:
            # Fall back to git diff if HEAD unborn
            proc = subprocess.run(["git", "diff"] + (["--", *pathspecs] if pathspecs else []), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            click.echo(f"Git error ({proc.returncode}): {proc.stderr.strip()}", err=True)
            sys.exit(3)
        diff_text = proc.stdout
    except FileNotFoundError:
        click.echo("Error: git executable not found in PATH.", err=True)
        sys.exit(3)
    except Exception as e:
        click.echo(f"Error executing git diff: {e}", err=True)
        sys.exit(3)

    # If diff is empty, check whether we should inspect unstaged by default
    if not diff_text.strip() and not staged and not branch and not all_changes:
        # Check if staged has changes
        try:
            staged_check = subprocess.run(["git", "diff", "--cached"], capture_output=True, text=True, check=False)
            if staged_check.stdout.strip():
                diff_text = staged_check.stdout
                target_desc = "staged changes (auto-detected)"
        except Exception:
            pass

    # Empty diff handling
    if not diff_text.strip():
        if fmt == "patch":
            sys.exit(0)
        elif fmt == "sarif":
            click.echo(json.dumps({
                "version": "2.1.0",
                "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
                "runs": [
                    {
                        "tool": {"driver": {"name": "jury", "version": "3.0.0", "rules": []}},
                        "results": [],
                    }
                ],
            }, indent=2))
            sys.exit(0)
        elif fmt == "json":
            click.echo(json.dumps({
                "ticket_id": f"tkt_{hashlib.sha256(b'empty').hexdigest()[:12]}",
                "verdict": "APPROVED",
                "score": 10.0,
                "summary": "No changes detected to review.",
                "findings": [],
            }, indent=2))
            sys.exit(0)
        else:
            console.print(f"[bold green]Verdict: APPROVED[/bold green] — No changes detected in {target_desc} to review.")
            sys.exit(0)

    # 2. Pre-flight Secret Scrubbing Check
    scrubbed = scrub_text(diff_text)
    if scrubbed.redactions and strict:
        click.echo(
            f"Security Policy Violation: Detected {len(scrubbed.redactions)} unredacted secret(s) in diff.",
            err=True,
        )
        sys.exit(4)

    # 3. Deliberation Execution
    task_intent = intent or f"Peer review of {target_desc} (focus: {focus})"
    is_mock = os.environ.get("JURY_OFFLINE_TEST") == "1" or os.environ.get("JURY_MOCK_MODE") == "1"
    mesh = DeliberationMesh(mock_mode=is_mock)

    req = ReviewRequest(
        task_intent=task_intent,
        diff_text=diff_text,
        git_target="staged" if staged else ("all" if all_changes else "unstaged"),
        repo_root=Path.cwd(),
        caller_tool="cli",
    )

    try:
        verdict: ReviewVerdict = asyncio.run(mesh.review(req))
    except Exception as e:
        if "ERR_QUORUM_COLLAPSE" in str(e) or "QuorumError" in type(e).__name__:
            click.echo(f"Quorum Collapse: {e}", err=True)
            sys.exit(2)
        click.echo(f"Deliberation error: {e}", err=True)
        sys.exit(1)

    # 4. Handle Quorum Collapse
    if verdict.verdict == "QUORUM_COLLAPSE" or verdict.error_code == "ERR_QUORUM_COLLAPSE":
        if fmt == "json":
            click.echo(json.dumps({"verdict": "QUORUM_COLLAPSE", "error": "ERR_QUORUM_COLLAPSE"}, indent=2))
        else:
            console.print("[bold red]QUORUM COLLAPSE: Deliberation aborted due to provider outage.[/bold red]")
        sys.exit(2)

    # 5. Format Output
    if fmt == "patch":
        if verdict.unified_patch:
            patch_out = verdict.unified_patch
            if "patch" not in patch_out:
                patch_out = f"# patch generated by Jury Mesh\n{patch_out}"
            click.echo(patch_out)

    elif fmt == "sarif":
        if verdict.sarif_json:
            click.echo(json.dumps(verdict.sarif_json, indent=2))
        else:
            from jury.deliberation.sarif import generate_sarif_report
            sarif_doc = generate_sarif_report(verdict.findings)
            click.echo(json.dumps(sarif_doc, indent=2))

    elif fmt == "json":
        ticket_id = verdict.ticket_id or f"tkt_{hashlib.sha256((diff_text or 'empty').encode()).hexdigest()[:12]}"
        payload = {
            "ticket_id": ticket_id,
            "verdict": verdict.verdict,
            "score": verdict.score,
            "consensus_score": verdict.consensus_score,
            "entropy_bpde": verdict.entropy_bpde,
            "findings": [
                {
                    "finding_id": f.finding_id,
                    "file_path": f.file_path,
                    "line_start": f.line_start,
                    "line_end": f.line_end,
                    "severity": f.severity,
                    "category": f.category,
                    "issue": f.issue,
                    "breaking_input": getattr(f, "breaking_input", None),
                    "proof_or_refutation": f.proof_or_refutation,
                    "suggested_replacement": f.suggested_replacement,
                }
                for f in verdict.findings
            ],
            "unified_patch": verdict.unified_patch,
            "sarif": verdict.sarif_json,
            "cache_hit": verdict.cache_hit,
            "duration_ms": verdict.duration_ms,
            "tokens_used": verdict.tokens_used,
            "is_hung_jury": verdict.is_hung_jury,
        }
        click.echo(json.dumps(payload, indent=2))

    else:
        # Text / Rich formatting
        status_color = "green" if verdict.verdict == "APPROVED" else "yellow" if verdict.verdict == "APPROVED_WITH_CONDITIONS" else "red"
        
        # Extract files from diff for clear reporting
        changed_files = []
        for line in diff_text.splitlines():
            if line.startswith("diff --git a/") and " b/" in line:
                part = line.split(" b/", 1)[1].strip()
                changed_files.append(part)
        file_summary = f"Files: {', '.join(changed_files)}" if changed_files else target_desc

        console.print(
            Panel(
                f"[bold {status_color}]Verdict: {verdict.verdict}[/bold {status_color}] | "
                f"Score: [bold cyan]{verdict.score:.1f}/10.0[/bold cyan] | "
                f"Consensus Index: [bold magenta]{verdict.consensus_score:.2f}[/bold magenta] | "
                f"Entropy: [dim]{verdict.entropy_bpde:.2f}[/dim]\n"
                f"[dim]{file_summary} | Cache: {'HIT' if verdict.cache_hit else 'MISS'} | Latency: {verdict.duration_ms}ms[/dim]",
                title="Jury Mesh Peer Review Verdict",
                border_style=status_color,
            )
        )

        if verdict.findings:
            table = Table(title="Surviving Deliberation Findings", show_header=True, header_style="bold cyan")
            table.add_column("Severity", style="bold", width=10)
            table.add_column("Location", style="cyan", width=25)
            table.add_column("Category", style="magenta", width=14)
            table.add_column("Issue & Suggested Fix")

            for f in verdict.findings:
                sev_color = "red" if f.severity in ("CRITICAL", "HIGH") else "yellow" if f.severity == "MEDIUM" else "blue"
                loc = f"{f.file_path}:{f.line_start}-{f.line_end}"
                parts = [f.issue]
                breaking = getattr(f, "breaking_input", None)
                if breaking:
                    b_clean = breaking.strip()
                    if "\n" in b_clean:
                        parts.append(f"[bold red]Breaking Input:[/bold red]\n[bold yellow]{b_clean}[/bold yellow]")
                    else:
                        parts.append(f"[bold red]Breaking Input:[/bold red] [bold yellow]{b_clean}[/bold yellow]")
                if f.suggested_replacement:
                    parts.append(f"[dim]Fix: {f.suggested_replacement}[/dim]")
                issue_text = "\n".join(parts)
                table.add_row(f"[{sev_color}]{f.severity}[/{sev_color}]", loc, f.category, issue_text)

            console.print(table)
        else:
            console.print("[dim]No defects or regressions flagged by the deliberation council.[/dim]")

        if verdict.deliberation_markdown:
            console.print(Markdown(verdict.deliberation_markdown))

    # 6. Exit code evaluation
    has_critical = any(f.severity in ("CRITICAL", "HIGH") for f in verdict.findings)
    if strict and (verdict.verdict == "REJECTED" or has_critical):
        sys.exit(1)
    if verdict.verdict == "REJECTED":
        sys.exit(1)

    sys.exit(0)
