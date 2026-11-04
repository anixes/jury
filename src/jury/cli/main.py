"""
src/jury/cli/main.py - Click-based Dispatcher & CLI Entry Point for Jury Mesh v3.0.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, List, Optional

import click
from rich.console import Console

from jury.cli.ask_cmd import ask_cmd
from jury.cli.init_cmd import init_cmd
from jury.cli.logs_cmd import logs_cmd
from jury.cli.plan_cmd import plan_cmd
from jury.cli.review_cmd import review_cmd
from jury.cli.test_cmd import test_cmd
from jury.config import clear_config_cache, get_config


console = Console()


class JuryClickGroup(click.Group):
    """
    Custom Click Group enforcing strict deterministic exit codes:
    0: Success / Approved
    1: Rejected / Changes Requested
    2: Quorum Collapse / Outage
    3: Usage Error / Invalid Argument / Missing File
    4: Secret Detected / Security Violation
    """

    def get_command(self, ctx: click.Context, cmd_name: str) -> Optional[click.Command]:
        rv = super().get_command(ctx, cmd_name)
        if rv is not None:
            return rv
        matches = [c for c in self.list_commands(ctx) if c.startswith(cmd_name)]
        if len(matches) == 1:
            return super().get_command(ctx, matches[0])
        elif len(matches) > 1:
            ctx.fail(f"Ambiguous command '{cmd_name}': {', '.join(sorted(matches))}")
        ctx.fail(f"No such command '{cmd_name}'.")
        return None

    def resolve_command(self, ctx: click.Context, args: List[str]):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as e:
            e.exit_code = 3
            raise e


@click.group(cls=JuryClickGroup)
@click.option("--config", "config_path", default=None, type=click.Path(), help="Path to custom config.yaml.")
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose debug logging.")
@click.option("-q", "--quiet", is_flag=True, help="Suppress non-essential console output.")
@click.version_option(version="3.0.0", prog_name="jury")
@click.pass_context
def cli(ctx: click.Context, config_path: Optional[str], verbose: bool, quiet: bool) -> None:
    """Jury Mesh (`jury`) - Multi-Model Deliberation Mesh & Zero-Token Code Synthesis Engine."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet

    if config_path:
        cfg_p = Path(config_path).resolve()
        if cfg_p.is_file():
            os.environ["JURY_CONFIG_PATH"] = str(cfg_p)
            clear_config_cache()
        elif not cfg_p.exists():
            click.echo(f"Error: Specified configuration file not found: {config_path}", err=True)
            sys.exit(3)


# Register subcommands
cli.add_command(init_cmd)
cli.add_command(test_cmd)
cli.add_command(plan_cmd)
cli.add_command(review_cmd)
cli.add_command(ask_cmd)
cli.add_command(logs_cmd)


def main(args: Optional[List[str]] = None) -> None:
    """CLI dispatcher entry point with deterministic exit code mapping."""
    try:
        cli.main(args=args, prog_name="jury", standalone_mode=False)
        sys.exit(0)
    except click.UsageError as e:
        click.echo(f"Usage error: {e.format_message()}", err=True)
        sys.exit(3)
    except click.BadParameter as e:
        click.echo(f"Bad parameter: {e.format_message()}", err=True)
        sys.exit(3)
    except click.NoSuchOption as e:
        click.echo(f"Invalid option: {e.format_message()}", err=True)
        sys.exit(3)
    except click.ClickException as e:
        click.echo(f"Error: {e.format_message()}", err=True)
        exit_code = 3 if e.exit_code == 2 else e.exit_code
        sys.exit(exit_code)
    except SystemExit as e:
        sys.exit(e.code)
    except Exception as e:
        click.echo(f"Fatal error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
