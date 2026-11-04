"""
src/jury/cli/logs_cmd.py - Audit Log Inspection, Aggregate Metrics & Database Pruning (`jury logs`).
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jury.config import get_config
from jury.state.engine import AuditLogEntry, JuryStateEngine


console = Console()


@click.command(name="logs")
@click.option("--limit", default=10, type=int, help="Maximum number of deliberation records to display.")
@click.option("--stats", is_flag=True, help="Display aggregate telemetry: tokens saved, cache hit ratio, and average latency.")
@click.option("--ticket", "ticket_id", default=None, help="Inspect a specific deliberation ticket by ID.")
@click.option("--verdict", "verdict_filter", default=None, help="Filter audit logs by verdict (e.g. APPROVED, REJECTED).")
@click.option("--clean", "clean_days", default=None, type=int, help="Delete deliberations older than N days and vacuum SQLite database.")
def logs_cmd(
    limit: int,
    stats: bool,
    ticket_id: Optional[str],
    verdict_filter: Optional[str],
    clean_days: Optional[int],
) -> None:
    """Inspect the deliberation audit ledger, view swarm telemetry, or clean old records."""
    config = get_config()
    state_engine = JuryStateEngine(db_path=config.db_path, lock_path=config.lock_path)

    # 1. Database Prune / Clean Operation
    if clean_days is not None:
        if clean_days < 0:
            click.echo("Error: --clean days must be a non-negative integer.", err=True)
            sys.exit(3)

        cutoff_timestamp = time.time() - (clean_days * 86400.0)
        with state_engine.lock:
            conn = state_engine.get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                cur1 = conn.execute("DELETE FROM jury_audit_log WHERE timestamp < ?", (cutoff_timestamp,))
                deleted_audits = cur1.rowcount

                cur2 = conn.execute("DELETE FROM jury_tickets WHERE created_at < ?", (cutoff_timestamp,))
                deleted_tickets = cur2.rowcount

                cur3 = conn.execute("DELETE FROM jury_cache WHERE expires_at < ?", (time.time(),))
                deleted_cache = cur3.rowcount

                conn.commit()
                # Run vacuum to compact SQLite WAL storage
                conn.execute("VACUUM;")
            finally:
                conn.close()

        console.print(
            Panel(
                f"[bold green]Successfully pruned records older than {clean_days} days:[/bold green]\n"
                f"- Audit records purged: [bold cyan]{deleted_audits}[/bold cyan]\n"
                f"- In-flight tickets purged: [bold cyan]{deleted_tickets}[/bold cyan]\n"
                f"- Expired cache records purged: [bold cyan]{deleted_cache}[/bold cyan]\n"
                f"- SQLite database vacuumed and compacted.",
                title="Database Cleanup",
                border_style="green",
            )
        )
        sys.exit(0)

    # 2. Aggregate Stats Mode
    if stats:
        conn = state_engine.get_connection()
        try:
            row = conn.execute(
                """
                SELECT
                    count(*) as total_count,
                    sum(tokens_used) as total_tokens_used,
                    sum(tokens_saved) as total_tokens_saved,
                    sum(case when cache_hit = 1 then 1 else 0 end) as total_cache_hits,
                    avg(latency_ms) as avg_latency_ms
                FROM jury_audit_log
                """
            ).fetchone()

            total_count = row["total_count"] if row and row["total_count"] else 0
            tokens_used = row["total_tokens_used"] if row and row["total_tokens_used"] else 0
            tokens_saved = row["total_tokens_saved"] if row and row["total_tokens_saved"] else 0
            cache_hits = row["total_cache_hits"] if row and row["total_cache_hits"] else 0
            avg_latency = row["avg_latency_ms"] if row and row["avg_latency_ms"] else 0.0

            hit_ratio = (cache_hits / total_count * 100.0) if total_count > 0 else 0.0
        finally:
            conn.close()

        stats_table = Table(title="Jury Mesh Deliberation Metrics & Aggregate Telemetry", show_header=True, header_style="bold cyan")
        stats_table.add_column("Metric", style="bold")
        stats_table.add_column("Value", style="bold green", justify="right")

        stats_table.add_row("Total Deliberations", str(total_count))
        stats_table.add_row("Tokens Saved", f"{tokens_saved:,} tokens")
        stats_table.add_row("Tokens Consumed", f"{tokens_used:,} tokens")
        stats_table.add_row("Cache Hit Ratio", f"{hit_ratio:.1f}% ({cache_hits}/{total_count})")
        stats_table.add_row("Average Deliberation Latency", f"{avg_latency:.1f} ms")

        console.print(stats_table)
        console.print(
            Panel(
                f"[bold cyan]Swarm Efficiency:[/bold cyan] {tokens_saved:,} tokens saved | "
                f"Cache hit ratio: {hit_ratio:.1f}% | Average latency: {avg_latency:.1f}ms",
                border_style="cyan",
            )
        )
        sys.exit(0)

    # 3. Specific Ticket Inspection
    if ticket_id:
        conn = state_engine.get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM jury_audit_log WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()

            if not row:
                # Also check tickets table
                t_row = conn.execute(
                    "SELECT * FROM jury_tickets WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()
                if not t_row:
                    click.echo(f"Notice: Deliberation ticket '{ticket_id}' was not found in audit ledger.")
                    sys.exit(0)

                # Format from tickets table
                console.print(
                    Panel(
                        f"[bold cyan]Ticket ID:[/bold cyan] {t_row['ticket_id']}\n"
                        f"[bold green]Status:[/bold green] {t_row['status']}\n"
                        f"[bold magenta]Command:[/bold magenta] {t_row['command']}\n"
                        f"[bold yellow]Leader PID:[/bold yellow] {t_row['leader_pid']}\n"
                        f"[bold white]Intent:[/bold white] {t_row['intent']}",
                        title="In-Flight Ticket Details",
                        border_style="cyan",
                    )
                )
                sys.exit(0)

            # Format from audit log
            status_color = "green" if "APPROVED" in row["verdict"] else "red"
            ts_str = datetime.fromtimestamp(row["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            juror_scores = json.loads(row["juror_scores"]) if row["juror_scores"] else {}

            console.print(
                Panel(
                    f"[bold cyan]Ticket ID:[/bold cyan] {row['ticket_id']}\n"
                    f"[bold {status_color}]Verdict: {row['verdict']}[/bold {status_color}]\n"
                    f"[bold magenta]Command:[/bold magenta] {row['command']} | [bold white]Tool:[/bold white] {row['caller_tool']}\n"
                    f"[bold white]Timestamp:[/bold white] {ts_str}\n"
                    f"[bold white]Latency:[/bold white] {row['latency_ms']}ms | [bold white]Tokens Saved:[/bold white] {row['tokens_saved']:,}\n"
                    f"[bold white]Prompt / Intent:[/bold white] {row['prompt']}\n"
                    f"[bold white]Juror Scores:[/bold white] {juror_scores}",
                    title="Deliberation Audit Record",
                    border_style=status_color,
                )
            )
            sys.exit(0)
        finally:
            conn.close()

    # 4. Standard Audit Log Listing (with optional verdict filter)
    conn = state_engine.get_connection()
    try:
        query = "SELECT * FROM jury_audit_log"
        params: List[Any] = []

        if verdict_filter:
            query += " WHERE UPPER(verdict) = UPPER(?)"
            params.append(verdict_filter.strip())

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    table = Table(
        title=f"Jury Mesh Deliberation Audit Ledger ({len(rows)} recent records)" + (f" [Filter: {verdict_filter}]" if verdict_filter else ""),
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Ticket ID", style="cyan", width=18)
    table.add_column("Command", style="magenta", width=8)
    table.add_column("Verdict", width=22)
    table.add_column("Latency", justify="right", width=10)
    table.add_column("Tokens Saved", justify="right", width=14)
    table.add_column("Timestamp (UTC)", style="dim")

    for r in rows:
        v_color = "green" if "APPROVED" in r["verdict"] else "red"
        ts_str = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(
            r["ticket_id"],
            r["command"],
            f"[{v_color}]{r['verdict']}[/{v_color}]",
            f"{r['latency_ms']}ms",
            f"{r['tokens_saved']:,}",
            ts_str,
        )

    console.print(table)
    sys.exit(0)
