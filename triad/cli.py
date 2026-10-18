"""
triad.cli - Command Line Interface for Standalone Triad Tool.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

# Support UTF-8 encoding on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from triad.client import run_multi_model_query
from triad.config import get_global_config_path, init_config_file, resolve_config
from triad.consultant import DEFAULT_CONSULT_SYSTEM_PROMPT, build_consult_prompt, load_spec_document
from triad.report import generate_json_report, generate_markdown_report
from triad.reviewer import DEFAULT_REVIEW_SYSTEM_PROMPT, build_review_prompt, get_git_diff, load_test_files


def handle_init(args: argparse.Namespace) -> int:
    """Handle `triad init` command to create starter configuration."""
    if args.is_global:
        target = get_global_config_path()
        loc_desc = "User Global Config"
    else:
        target = Path.cwd() / ".triad.env"
        loc_desc = "Project Local Config"

    if target.exists() and not args.force:
        print(f"[EXISTS] Configuration file already exists at: {target.resolve().as_posix()}", file=sys.stderr)
        print("Use --force to overwrite.", file=sys.stderr)
        return 1

    created = init_config_file(target)
    print(f"\n[OK] Initialized {loc_desc}: {created.resolve().as_posix()}")
    print("Please edit this file to configure your TRIAD_API_KEY and TRIAD_MODELS.\n")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="triad",
        description="Triad: Universal Multi-Model Peer Review & Architectural Consultation Tool",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. init command
    init_parser = subparsers.add_parser("init", help="Create a starter Triad configuration file")
    init_parser.add_argument(
        "--global",
        dest="is_global",
        action="store_true",
        help="Create user-global config at ~/.triad/config.env instead of project .triad.env",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing configuration file",
    )

    # 2. review command
    review_parser = subparsers.add_parser("review", help="Run multi-model peer review on git changes")
    review_parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Target git repository path (default: current working directory)",
    )
    review_parser.add_argument(
        "--target",
        type=str,
        default="unstaged",
        help="Git diff target: unstaged, staged, working, HEAD~1, origin/main... (default: unstaged)",
    )
    review_parser.add_argument(
        "--tests",
        nargs="*",
        default=[],
        help="Paths to test files to evaluate alongside diff",
    )
    review_parser.add_argument(
        "--instructions",
        type=str,
        default="",
        help="Additional specific review directives or focus areas",
    )
    review_parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with non-zero code if any model rejects or gives score < threshold",
    )
    review_parser.add_argument(
        "--min-score",
        type=float,
        default=7.0,
        help="Minimum passing score for strict mode (default: 7.0)",
    )

    # 3. consult command
    consult_parser = subparsers.add_parser("consult", help="Run multi-model architectural consultation")
    consult_parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Target project directory path (default: current working directory)",
    )
    consult_parser.add_argument(
        "--spec",
        type=str,
        default=None,
        help="Path to markdown specification or design document to consult on",
    )
    consult_parser.add_argument(
        "--prompt",
        type=str,
        default="",
        help="Text prompt describing the architectural task or questions",
    )

    # Common options for review and consult
    for p in (review_parser, consult_parser):
        p.add_argument(
            "--models",
            type=str,
            default=None,
            help="Comma-separated model identifiers (default: from TRIAD_MODELS env)",
        )
        p.add_argument(
            "--gateway-url",
            type=str,
            default=None,
            help="Base URL for OpenAI-compatible gateway (default: from TRIAD_GATEWAY_URL env)",
        )
        p.add_argument(
            "--api-key",
            type=str,
            default=None,
            help="API Key for gateway (default: from TRIAD_API_KEY env)",
        )
        p.add_argument(
            "--output-dir",
            type=str,
            default=None,
            help="Directory to save reviews and consolidated report (default: reports/triad)",
        )
        p.add_argument(
            "--format",
            choices=["markdown", "json", "all"],
            default="all",
            help="Report format to generate (default: all)",
        )

    args = parser.parse_args(argv)

    if args.command == "init":
        return handle_init(args)

    # Resolve configuration
    config = resolve_config(
        cli_models=args.models,
        cli_api_key=args.api_key,
        cli_gateway_url=args.gateway_url,
        cli_output_dir=args.output_dir,
        cli_min_score=getattr(args, "min_score", 7.0),
        cli_strict=getattr(args, "strict", False),
        cli_repo=getattr(args, "repo", None),
    )

    if not config.models:
        print("[ERROR] No models specified.", file=sys.stderr)
        print("Pass --models or configure TRIAD_MODELS in .triad.env or ~/.triad/config.env.", file=sys.stderr)
        print("Run 'triad init' to generate a starter configuration file.", file=sys.stderr)
        return 1

    if not config.api_key:
        print("[ERROR] Missing API key.", file=sys.stderr)
        print("Pass --api-key or configure TRIAD_API_KEY in .triad.env or ~/.triad/config.env.", file=sys.stderr)
        return 1

    print(f"\n{'='*60}")
    print(f"Triad: Running '{args.command.upper()}' on {config.repo_dir.as_posix()}")
    print(f"Gateway: {config.gateway_url}")
    print(f"Models:  {', '.join(config.models)}")
    print(f"Output:  {config.output_dir.as_posix()}")
    print(f"{'='*60}\n")

    if args.command == "review":
        diff_text = get_git_diff(config.repo_dir, args.target)
        test_text = load_test_files(config.repo_dir, args.tests)
        user_prompt = build_review_prompt(diff_text, test_text, args.instructions)
        system_prompt = DEFAULT_REVIEW_SYSTEM_PROMPT
    else:
        spec_text = load_spec_document(config.repo_dir, args.spec)
        user_prompt = build_consult_prompt(args.prompt, spec_text)
        if not user_prompt.strip():
            print("[ERROR] Consult mode requires either --prompt or --spec.", file=sys.stderr)
            return 1
        system_prompt = DEFAULT_CONSULT_SYSTEM_PROMPT

    results = asyncio.run(
        run_multi_model_query(
            mode=args.command,
            models=config.models,
            gateway_url=config.gateway_url,
            api_key=config.api_key,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            output_dir=config.output_dir,
            labels=[f"Triad Reviewer {i+1}" for i in range(len(config.models))],
        )
    )

    # Format reports
    if args.format in ("markdown", "all"):
        md_path = config.output_dir / f"consolidated_{args.command}_report.md"
        generate_markdown_report(args.command, results, md_path)
        print(f"Markdown Report: {md_path.as_posix()}")

    if args.format in ("json", "all"):
        json_path = config.output_dir / f"consolidated_{args.command}_report.json"
        generate_json_report(args.command, results, json_path)
        print(f"JSON Report:     {json_path.as_posix()}")

    print("\n" + "=" * 60)
    print(f"TRIAD {args.command.upper()} RESULTS SUMMARY")
    print("=" * 60)
    gate_failures = []

    for r in results:
        status_tag = "[PASS]" if r.success else "[FAIL]"
        score_tag = f"Score: {r.score:.1f}/10" if r.score is not None else "Score: N/A"
        print(f"{status_tag} {r.model} -> {r.verdict} ({score_tag}) in {r.duration_s:.1f}s")

        if not r.success:
            gate_failures.append(f"{r.model}: Model evaluation failed ({r.error or 'unknown error'})")
        elif r.verdict == "REJECTED":
            gate_failures.append(f"{r.model}: Changes were explicitly REJECTED")
        elif r.verdict == "UNKNOWN":
            gate_failures.append(f"{r.model}: Failed to parse a valid verdict (UNKNOWN)")

        if args.command == "review":
            if r.score is None:
                gate_failures.append(f"{r.model}: Could not parse a numeric score")
            elif r.score < config.min_score:
                gate_failures.append(f"{r.model}: Score {r.score:.1f}/10 is below minimum threshold {config.min_score:.1f}")

    if config.strict and gate_failures:
        print("\n[STRICT GATE FAILURE] Execution failed gate criteria:", file=sys.stderr)
        for fail_msg in gate_failures:
            print(f"  - {fail_msg}", file=sys.stderr)
        return 1

    print("\n[OK] Triad execution completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
