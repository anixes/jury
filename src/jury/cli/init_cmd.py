"""
src/jury/cli/init_cmd.py - Workspace Rules & Configuration Scaffolding Command (`jury init`).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jury.cli.templates import get_config_template, get_rule_templates, update_gitignore


console = Console()


@click.command(name="init")
@click.argument("target_path", default=".", type=click.Path(file_okay=False, dir_okay=True))
@click.option("--rules-only", is_flag=True, help="Scaffold only agent rule files (AGENTS.md, Cursor, Claude).")
@click.option("--config-only", is_flag=True, help="Scaffold only .jury/config.yaml configuration.")
@click.option("--force", is_flag=True, help="Overwrite existing configuration and rule files.")
@click.option("--preset", default="default", help="Configuration preset (e.g. 'free-tier-only', 'default').")
@click.option("--dry-run", is_flag=True, help="Simulate scaffolding without modifying files on disk.")
def init_cmd(
    target_path: str,
    rules_only: bool,
    config_only: bool,
    force: bool,
    preset: str,
    dry_run: bool,
) -> None:
    """Initialize a repository workspace with Jury configuration and IDE agent rules."""
    root = Path(target_path).resolve()
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)

    created_files = []
    skipped_files = []

    # 1. Scaffolding .jury/config.yaml
    if not rules_only:
        jury_dir = root / ".jury"
        config_path = jury_dir / "config.yaml"

        if config_path.exists() and not force:
            skipped_files.append(str(config_path.relative_to(root)))
        else:
            if not dry_run:
                jury_dir.mkdir(parents=True, exist_ok=True)
                config_content = get_config_template(preset)
                config_path.write_text(config_content, encoding="utf-8")
            created_files.append(f".jury/config.yaml{' (dry-run)' if dry_run else ''}")

    # 2. Scaffolding Agent Rule Files
    if not config_only:
        rule_templates = get_rule_templates()
        for rel_str, template_content in rule_templates.items():
            dest_path = root / rel_str

            if dest_path.exists() and not force:
                skipped_files.append(rel_str)
            else:
                if not dry_run:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    dest_path.write_text(template_content, encoding="utf-8")
                created_files.append(f"{rel_str}{' (dry-run)' if dry_run else ''}")

    # 3. Update .gitignore if not in config-only mode or dry-run
    gitignore_updated = False
    gitignore_msg = ""
    if not config_only and not dry_run:
        try:
            gitignore_updated, gitignore_msg = update_gitignore(root)
        except Exception as e:
            gitignore_msg = f"Warning: Failed to update .gitignore: {e}"

    # Render summary table
    table = Table(
        title=f"Jury Mesh Workspace Scaffolding{' [DRY RUN]' if dry_run else ''}",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("File / Component", style="bold")
    table.add_column("Status", style="green")

    for f in created_files:
        status_lbl = "[cyan]PLAN: CREATE/OVERWRITE[/cyan]" if dry_run else ("[green]CREATED / OVERWRITTEN[/green]" if force else "[green]CREATED[/green]")
        table.add_row(f, status_lbl)
    for f in skipped_files:
        table.add_row(f, "[yellow]PRESERVED (use --force to overwrite)[/yellow]")

    if gitignore_updated:
        table.add_row(".gitignore", f"[green]UPDATED[/green] ({gitignore_msg})")

    console.print(table)
    mode_str = f"Preset: {preset} | Mode: {'rules-only' if rules_only else 'config-only' if config_only else 'full'}"
    if dry_run:
        mode_str += " | DRY RUN"

    console.print(
        Panel(
            f"[bold green]Workspace initialization completed for[/bold green] [bold white]{root}[/bold white]\n"
            f"[dim]{mode_str}[/dim]",
            title="Jury Init" if not dry_run else "Jury Init Dry Run",
            border_style="green",
        )
    )
    sys.exit(0)
