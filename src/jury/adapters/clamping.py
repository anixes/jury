"""
src/jury/adapters/clamping.py - Pluggable AST-Aware Diff Clamper.

Provides intelligent, multi-stage token clamping for diffs to guarantee
payloads fit safely within provider token limits (e.g. Groq 12k TPM ceiling with <= 5,500 tokens).

Multi-stage pruning pipeline:
1. Short-circuit if within token ceiling.
2. Stage 1: Prune unchanged diff context lines.
3. Stage 2: AST-aware collapse of large function/method/class bodies while preserving
   signatures, decorators, headers, and docstrings.
4. Stage 3: Priority-based file truncation (preserves core source over tests/docs/lockfiles).
5. Stage 4: Strict token budget ceiling guarantee.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from jury.adapters.base import estimate_tokens


# Regex patterns for AST construct recognition across languages
FUNC_OR_CLASS_SIG = re.compile(
    r"^(?:[\+\-\s])\s*(?:@\w+|async\s+def|def|class|function|pub\s+fn|fn|func|public\s+class|interface|struct)\b"
)
DOCSTRING_START = re.compile(r'^(?:[\+\-\s])\s*(?:"""|\'\'\'|/\*\*|//\s*)')
DIFF_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)|^--- (?:a/)?(\S+)|^=== (?:a/)?(\S+)")
HUNK_HEADER = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@")


class DiffClamper:
    """
    Pluggable AST-aware diff clamping engine.
    Ensures that unified diff payloads never exceed designated token limits.
    """

    def __init__(self, max_tokens: int = 5500):
        self.max_tokens = max_tokens

    def clamp(self, diff_text: str, max_tokens: Optional[int] = None) -> str:
        """
        Clamp diff_text to at most max_tokens (defaults to self.max_tokens).
        Returns unmodified text if already within limit.
        """
        limit = max_tokens if max_tokens is not None else self.max_tokens
        if not diff_text or estimate_tokens(diff_text) <= limit:
            return diff_text

        # Stage 1: Prune unchanged context lines
        pruned_stage1 = self._prune_context_lines(diff_text)
        if estimate_tokens(pruned_stage1) <= limit:
            return pruned_stage1

        # Stage 2: AST-aware collapse of large function/class bodies
        collapsed_stage2 = self._collapse_function_bodies(pruned_stage1)
        if estimate_tokens(collapsed_stage2) <= limit:
            return collapsed_stage2

        # Stage 3: Priority file pruning (core source > configs > tests > docs)
        file_pruned_stage3 = self._prune_low_priority_files(collapsed_stage2, limit)
        if estimate_tokens(file_pruned_stage3) <= limit:
            return file_pruned_stage3

        # Stage 4: Hard budget truncation guarantee
        return self._hard_truncate(file_pruned_stage3, limit)

    def _prune_context_lines(self, diff_text: str) -> str:
        """
        Prune unchanged context lines (lines starting with ' ') from diff hunks.
        Retains at most 1 context line adjacent to additions/deletions.
        """
        lines = diff_text.splitlines()
        result_lines: List[str] = []
        i = 0
        n = len(lines)

        while i < n:
            line = lines[i]

            # Preserve diff metadata and hunk headers
            if (
                line.startswith("diff ")
                or line.startswith("--- ")
                or line.startswith("+++ ")
                or line.startswith("index ")
                or line.startswith("new file ")
                or line.startswith("deleted file ")
                or line.startswith("similarity ")
                or line.startswith("@@")
            ):
                result_lines.append(line)
                i += 1
                continue

            # Check if this is an unchanged context line
            if line.startswith(" "):
                # Count consecutive context lines
                start_ctx = i
                while i < n and lines[i].startswith(" "):
                    i += 1
                ctx_block = lines[start_ctx:i]

                if len(ctx_block) <= 2:
                    result_lines.extend(ctx_block)
                else:
                    # Keep first line and last line of context block, collapse middle
                    result_lines.append(ctx_block[0])
                    result_lines.append(" ... [context lines omitted for token efficiency]")
                    result_lines.append(ctx_block[-1])
                continue

            # Addition (+) or Deletion (-)
            result_lines.append(line)
            i += 1

        return "\n".join(result_lines)

    def _collapse_function_bodies(self, diff_text: str) -> str:
        """
        AST-aware body collapse:
        Preserves signatures (def/class/fn), decorators, and docstrings,
        while collapsing large multi-line bodies in added code blocks.
        """
        lines = diff_text.splitlines()
        result_lines: List[str] = []
        i = 0
        n = len(lines)

        while i < n:
            line = lines[i]

            # Detect function or class signature in added line
            if (line.startswith("+") or line.startswith(" ")) and FUNC_OR_CLASS_SIG.search(line):
                result_lines.append(line)
                i += 1

                # If signature spans multiple lines (e.g. multi-line params), capture them
                while i < n and (lines[i].startswith("+") or lines[i].startswith(" ")) and not lines[i - 1].rstrip().endswith(":") and not lines[i - 1].rstrip().endswith("{"):
                    result_lines.append(lines[i])
                    i += 1
                    if lines[i - 1].rstrip().endswith(":") or lines[i - 1].rstrip().endswith("{"):
                        break

                # Capture docstring if present immediately after signature
                if i < n and DOCSTRING_START.search(lines[i]):
                    first_doc_line = lines[i]
                    result_lines.append(first_doc_line)
                    i += 1

                    is_single_line = (
                        first_doc_line.count('"""') >= 2
                        or first_doc_line.count("'''") >= 2
                        or ("/*" in first_doc_line and "*/" in first_doc_line)
                        or first_doc_line.strip().startswith(("+ //", "  //", "//"))
                    )

                    if not is_single_line:
                        delim = '"""' if '"""' in first_doc_line else ("'''" if "'''" in first_doc_line else "*/")
                        while i < n:
                            doc_l = lines[i]
                            result_lines.append(doc_l)
                            i += 1
                            if delim in doc_l:
                                break

                # Now collect body lines until next signature, hunk header, or file header
                body_lines: List[str] = []
                while i < n:
                    curr = lines[i]
                    if (
                        curr.startswith("@@")
                        or curr.startswith("diff ")
                        or curr.startswith("--- ")
                        or curr.startswith("+++ ")
                        or FUNC_OR_CLASS_SIG.search(curr)
                    ):
                        break
                    body_lines.append(curr)
                    i += 1

                # If body is substantial (> 4 lines), collapse interior
                if len(body_lines) > 4:
                    result_lines.append(body_lines[0])  # First line of body
                    result_lines.append(
                        "+        ... # [COLLAPSED: "
                        f"{len(body_lines) - 2} body lines omitted for token ceiling]"
                    )
                    result_lines.append(body_lines[-1])  # Last line (e.g. return)
                else:
                    result_lines.extend(body_lines)
                continue

            result_lines.append(line)
            i += 1

        return "\n".join(result_lines)

    def _prune_low_priority_files(self, diff_text: str, limit: int) -> str:
        """
        Split multi-file diff and prune lowest-priority files first.
        Priority:
        1. Core source code (src/, *.py, *.ts, etc.)
        2. Configs / Schemas (*.toml, *.json, *.yaml, *.sql)
        3. Tests (tests/, test_*.py, *.spec.ts)
        4. Docs / Lockfiles (docs/, *.md, *.lock)
        """
        file_chunks = self._split_diff_into_files(diff_text)
        if len(file_chunks) <= 1:
            return diff_text

        # Classify and score files
        scored_files: List[Tuple[int, str, str]] = []
        for header, body in file_chunks:
            path = self._extract_filepath(header)
            score = self._score_file_priority(path)
            scored_files.append((score, header, body))

        # Sort: lower score = lower priority (pruned first)
        scored_files.sort(key=lambda x: x[0])

        current_diff = diff_text
        for score, header, body in scored_files:
            if estimate_tokens(current_diff) <= limit:
                return current_diff

            # If score is low (tests, docs, configs), stub out the body
            if score <= 2:
                path = self._extract_filepath(header)
                stub = f"{header}\n# [Diff body for {path} omitted to satisfy token ceiling]\n"
                current_diff = current_diff.replace(f"{header}\n{body}", stub)

        return current_diff

    def _split_diff_into_files(self, diff_text: str) -> List[Tuple[str, str]]:
        """Split unified diff text into list of (file_header, file_body) tuples."""
        chunks: List[Tuple[str, str]] = []
        raw_sections = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)

        for sec in raw_sections:
            sec = sec.strip()
            if not sec:
                continue
            lines = sec.splitlines()
            header_lines: List[str] = []
            body_lines: List[str] = []
            in_body = False

            for line in lines:
                if not in_body and (line.startswith("@@")):
                    in_body = True
                if in_body:
                    body_lines.append(line)
                else:
                    header_lines.append(line)

            chunks.append(("\n".join(header_lines), "\n".join(body_lines)))

        return chunks

    def _extract_filepath(self, header: str) -> str:
        """Extract file path from diff header."""
        match = DIFF_FILE_HEADER.search(header)
        if match:
            for g in match.groups():
                if g:
                    return g
        return "unknown"

    def _score_file_priority(self, path: str) -> int:
        """
        Score file importance:
        0 = docs / lockfiles (pruned first)
        1 = tests
        2 = configs / scripts
        3 = core source code
        """
        p = path.lower()
        if p.endswith(".lock") or "package-lock" in p or p.endswith(".md") or "docs/" in p:
            return 0
        if "test" in p or "spec" in p:
            return 1
        if p.endswith(".toml") or p.endswith(".json") or p.endswith(".yaml") or p.endswith(".yml"):
            return 2
        return 3

    def _hard_truncate(self, diff_text: str, limit: int) -> str:
        """
        Strict token budget ceiling guarantee:
        Truncates cleanly at a line break before the budget limit.
        """
        max_chars = int(limit * 3.6)
        if len(diff_text) <= max_chars:
            return diff_text

        truncated = diff_text[:max_chars]
        last_nl = truncated.rfind("\n")
        if last_nl > 0:
            truncated = truncated[:last_nl]

        truncation_notice = (
            f"\n\n# [... DIFF TRUNCATED: Clamped to <= {limit} tokens for provider TPM quota ...]"
        )
        final_text = truncated + truncation_notice

        # Final safety check: if still slightly above due to notice, shrink further
        while estimate_tokens(final_text) > limit and len(truncated) > 200:
            truncated = truncated[: int(len(truncated) * 0.9)]
            last_nl = truncated.rfind("\n")
            if last_nl > 0:
                truncated = truncated[:last_nl]
            final_text = truncated + truncation_notice

        return final_text


# Global convenience function
_DEFAULT_CLAMPER = DiffClamper(max_tokens=5500)


def clamp_diff(diff_text: str, max_tokens: int = 5500) -> str:
    """Convenience function to clamp diff text to designated token ceiling."""
    if max_tokens == 5500:
        return _DEFAULT_CLAMPER.clamp(diff_text)
    return DiffClamper(max_tokens=max_tokens).clamp(diff_text)
