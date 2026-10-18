"""
triad.reviewer - Git Diff and Test Suite Collection for Code Reviews.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional

DEFAULT_REVIEW_SYSTEM_PROMPT = """You are a Principal Software Quality and Security Architect.
Conduct a strict, thorough peer review of the provided code diff and test suite:
1. Architectural soundness & fidelity to specifications.
2. Cross-platform safety (Windows and POSIX compatibility, path handling, process management).
3. Security posture (path confinement, injection risks, resource leakages).
4. Edge cases & error handling (timeouts, unexpected process terminations, state leaks).
5. Code style, maintainability, and clean separation of concerns.
6. Test suite completeness (coverage of edge cases, regression resilience).
7. Production Readiness Score (format as 'Score: X/10') and Verdict ('APPROVED', 'APPROVED WITH COMMENTS', or 'REJECTED').
Keep your review structured, highly technical, and direct with concrete code suggestions.
"""


def get_git_diff(
    repo_dir: Path,
    target: str = "unstaged",
    max_lines: int = 2500,
    timeout_s: float = 30.0,
) -> str:
    """Extract git diff with compact 2-line context from any target repository."""
    cmd = ["git", "diff", "-U2"]
    if target == "staged":
        cmd.append("--cached")
    elif target not in ("unstaged", "working"):
        cmd.append(target)

    try:
        res = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_s,
        )
        diff_text = res.stdout
    except subprocess.TimeoutExpired:
        return f"Error gathering git diff from {repo_dir}: Git process timed out after {timeout_s}s"
    except Exception as e:
        return f"Error gathering git diff from {repo_dir}: {e}"

    lines = diff_text.splitlines()
    if len(lines) > max_lines:
        truncated_header = (
            f"# [Notice] Git diff truncated to first {max_lines} of {len(lines)} lines.\n"
        )
        return truncated_header + "\n".join(lines[:max_lines])
    return diff_text


def load_test_files(repo_dir: Path, test_paths: List[str], max_total_chars: int = 35000) -> str:
    """Load specified test files up to a safe character budget, enforcing path confinement."""
    output_blocks = []
    total_chars = 0
    base_dir = repo_dir.resolve()

    for path_str in test_paths:
        raw_p = Path(path_str)
        p = raw_p.resolve() if raw_p.is_absolute() else (base_dir / raw_p).resolve()

        # Enforce path confinement within repository boundary
        try:
            is_confined = p.is_relative_to(base_dir)
        except AttributeError:
            is_confined = str(p).startswith(str(base_dir))

        if not is_confined:
            output_blocks.append(
                f"### File: {raw_p} (Security Warning: Path traversal detected outside repository root)"
            )
            continue

        if not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8")
            if total_chars + len(content) > max_total_chars:
                budget_left = max(0, max_total_chars - total_chars)
                output_blocks.append(
                    f"### File: {p.name} (Truncated):\n```\n{content[:budget_left]}\n...\n```"
                )
                break
            output_blocks.append(
                f"### File: {p.name}:\n```\n{content}\n```"
            )
            total_chars += len(content)
        except Exception as e:
            output_blocks.append(f"### File: {p.name}: (Read error: {e})")

    return "\n\n".join(output_blocks)


def build_review_prompt(diff_text: str, test_code: str, custom_instructions: str = "") -> str:
    """Assemble code review prompt payload."""
    blocks = [
        "Please conduct a comprehensive, technical peer review of the following changes:\n",
        "### 1. Git Changes (-U2 concise context):",
        "```diff",
        diff_text.strip() or "(No changes detected in target diff)",
        "```",
    ]
    if test_code.strip():
        blocks.extend([
            "\n### 2. Accompanying Unit & Integration Test Suites:",
            test_code.strip(),
        ])
    if custom_instructions.strip():
        blocks.extend([
            "\n### 3. Additional Specific Review Directives:",
            custom_instructions.strip(),
        ])
    blocks.append("\nPlease output your structured review with clear findings, a numeric score (Score: X/10), and final verdict.")
    return "\n".join(blocks)
