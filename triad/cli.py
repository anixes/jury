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
from triad.config import TriadConfig, get_global_config_path, init_config_file, resolve_config
from triad.consultant import DEFAULT_CONSULT_SYSTEM_PROMPT, build_consult_prompt, load_spec_document
from triad.debate import run_multi_model_debate
from triad.pack import compile_context_pack
from triad.report import format_score_progression, generate_json_report, generate_markdown_report
from triad.reviewer import DEFAULT_REVIEW_SYSTEM_PROMPT, build_review_prompt, get_git_diff, load_test_files


def handle_pack(args: argparse.Namespace, config: TriadConfig) -> int:
    """Handle `triad pack` command to compile deterministic Context Pack."""
    print(f"\n{'='*60}")
    print(f"Triad: Compiling Context Pack for {config.repo_dir.as_posix()} (target: {args.target})")
    print(f"{'='*60}\n")
    pack = compile_context_pack(
        repo_dir=config.repo_dir,
        target=args.target,
        token_budget=args.budget,
    )
    if args.format == "json":
        output_content = pack.to_json()
    else:
        output_content = pack.to_markdown()

    if args.output:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = config.repo_dir / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_content, encoding="utf-8")
        print(f"[OK] Context Pack written to: {out_path.as_posix()}")
    else:
        print(output_content)

    print(
        f"\n[Summary] Seeds: {len(pack.seed_symbols)} | Tier 1: {len(pack.mandatory_symbols)} | "
        f"Tier 2: {len(pack.prioritized_symbols)} | Tokens: ~{pack.total_estimated_tokens}"
    )
    return 0



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
        "--pack",
        action="store_true",
        help="Compile and attach deterministic Context Pack to review prompt",
    )
    review_parser.add_argument(
        "--token-budget",
        type=int,
        default=4000,
        help="Token budget for Context Pack (default: 4000)",
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
        "--context-pack",
        type=str,
        default=None,
        help="Path to pre-compiled Context Pack file to attach to consultation",
    )
    consult_parser.add_argument(
        "--prompt",
        type=str,
        default="",
        help="Text prompt describing the architectural task or questions",
    )

    # 4. pack command
    pack_parser = subparsers.add_parser("pack", help="Compile a deterministic Context Pack from repo and git diff")
    pack_parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Target git repository path (default: current working directory)",
    )
    pack_parser.add_argument(
        "--target",
        type=str,
        default="unstaged",
        help="Git diff target: unstaged, staged, working, HEAD~1, origin/main... (default: unstaged)",
    )
    pack_parser.add_argument(
        "--budget",
        type=int,
        default=4000,
        help="Maximum estimated token budget for Context Pack (default: 4000)",
    )
    pack_parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Output format: markdown or json (default: markdown)",
    )
    pack_parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="File path to write compiled Context Pack to (default: stdout)",
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
        p.add_argument(
            "--debate",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Enable/disable multi-round deliberation and cross-critique between models",
        )
        p.add_argument(
            "--debate-rounds",
            type=int,
            default=None,
            help="Number of deliberation rounds when debate is enabled (default: 2, max: 5)",
        )

    args = parser.parse_args(argv)

    if args.command == "init":
        return handle_init(args)

    # Resolve configuration
    config = resolve_config(
        cli_models=getattr(args, "models", None),
        cli_api_key=getattr(args, "api_key", None),
        cli_gateway_url=getattr(args, "gateway_url", None),
        cli_output_dir=getattr(args, "output_dir", None),
        cli_min_score=getattr(args, "min_score", 7.0),
        cli_strict=getattr(args, "strict", False),
        cli_repo=getattr(args, "repo", None),
        cli_debate=getattr(args, "debate", None),
        cli_debate_rounds=getattr(args, "debate_rounds", None),
        cli_use_pack=getattr(args, "pack", None),
        cli_token_budget=getattr(args, "budget", getattr(args, "token_budget", None)),
        cli_context_pack=getattr(args, "context_pack", None),
    )

    if args.command == "pack":
        return handle_pack(args, config)

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
    if config.debate:
        print(f"Debate:  ENABLED ({config.debate_rounds} deliberation rounds)")
    else:
        print("Debate:  DISABLED (blind independent evaluation)")
    print(f"Output:  {config.output_dir.as_posix()}")
    print(f"{'='*60}\n")

    if args.command == "review":
        diff_text = get_git_diff(config.repo_dir, args.target)
        test_text = load_test_files(config.repo_dir, args.tests)
        context_pack_text = ""
        if getattr(args, "pack", False) or config.use_pack:
            print("Compiling targeted Context Pack for review...")
            pack = compile_context_pack(
                repo_dir=config.repo_dir,
                target=args.target,
                token_budget=config.token_budget,
            )
            context_pack_text = pack.to_markdown()
            print(
                f"[Context Pack] Attached {len(pack.mandatory_symbols)} Tier 1 and "
                f"{len(pack.prioritized_symbols)} Tier 2 symbols (~{pack.total_estimated_tokens} tokens)."
            )
        user_prompt = build_review_prompt(diff_text, test_text, args.instructions, context_pack_text=context_pack_text)
        system_prompt = DEFAULT_REVIEW_SYSTEM_PROMPT
    else:
        spec_text = load_spec_document(config.repo_dir, args.spec)
        context_pack_text = ""
        pack_path = getattr(args, "context_pack", None) or config.context_pack_path
        if pack_path:
            p = Path(pack_path)
            if not p.is_absolute():
                p = config.repo_dir / p
            if p.is_file():
                context_pack_text = p.read_text(encoding="utf-8")
        user_prompt = build_consult_prompt(args.prompt, spec_text, context_pack_text=context_pack_text)
        if not user_prompt.strip():
            print("[ERROR] Consult mode requires either --prompt, --spec, or --context-pack.", file=sys.stderr)
            return 1
        system_prompt = DEFAULT_CONSULT_SYSTEM_PROMPT


    if config.debate:
        results = asyncio.run(
            run_multi_model_debate(
                mode=args.command,
                models=config.models,
                gateway_url=config.gateway_url,
                api_key=config.api_key,
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                output_dir=config.output_dir,
                labels=[f"Triad Panelist {i+1}" for i in range(len(config.models))],
                total_rounds=config.debate_rounds,
            )
        )
    else:
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

    summary_title = f"TRIAD {args.command.upper()} RESULTS SUMMARY"
    if config.debate and any(len(r.rounds) > 1 for r in results):
        summary_title += f" (DEBATE MODE: {config.debate_rounds} ROUNDS)"
    print("\n" + "=" * 60)
    print(summary_title)
    print("=" * 60)
    gate_failures = []

    for r in results:
        status_tag = "[PASS]" if r.success else "[FAIL]"
        score_prog = format_score_progression(r.rounds if config.debate else None, r.score, arrow=" -> ")
        score_tag = f"Score: {score_prog}"
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
