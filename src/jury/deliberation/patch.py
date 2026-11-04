"""
src/jury/deliberation/patch.py - Machine-Actionable POSIX Unified Diff Emitter & Validator.

Implements POSIX unified diff parsing, synthesis, extraction, and validation:
- Extracts unified diffs from model outputs and markdown code blocks.
- Synthesizes compliant `diff --git a/... b/...` unified patches using difflib.
- Validates patches against standard `git apply --check` syntax (zero hunk header corruption).
- In-memory patch application and verification.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# Regex for standard unified diff hunk header: @@ -12,5 +12,7 @@ optional section heading
HUNK_HEADER_REGEX = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
)

# Standard git diff file header
GIT_DIFF_HEADER_REGEX = re.compile(
    r"^diff --git a/(.*?) b/(.*?)$"
)


@dataclass
class DiffHunk:
    """Represents a single unified diff hunk."""
    orig_start: int
    orig_length: int
    new_start: int
    new_length: int
    heading: str
    lines: List[str] = field(default_factory=list)


@dataclass
class DiffFile:
    """Represents changes to a single file in a multi-file unified diff."""
    orig_path: str
    new_path: str
    hunks: List[DiffHunk] = field(default_factory=list)


@dataclass
class PatchValidationResult:
    """Validation report checking compliance with `git apply --check` rules."""
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    file_count: int = 0
    hunk_count: int = 0


# =============================================================================
# 1. Diff Extraction Utility
# =============================================================================

def extract_unified_diff(text: str) -> Optional[str]:
    """
    Extract a clean unified diff from arbitrary text or markdown code blocks.
    Detects ```diff ... ``` or raw `diff --git` / `--- a/` blocks.
    """
    if not text:
        return None

    # 1. Search for markdown code fences ```diff ... ``` or ```patch ... ```
    fence_pattern = re.compile(r"```(?:diff|patch)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)
    for match in fence_pattern.finditer(text):
        content = match.group(1).strip()
        if "--- " in content or "diff --git" in content:
            return content

    # 2. Search for direct diff header starting in raw text
    diff_start = text.find("diff --git")
    if diff_start != -1:
        # Find end of diff block (next non-diff section or markdown heading)
        sub = text[diff_start:].strip()
        return sub

    # 3. Search for classic unified diff starting with `--- `
    classic_start = text.find("--- ")
    if classic_start != -1:
        sub = text[classic_start:].strip()
        return sub

    return None


# =============================================================================
# 2. Diff Synthesis
# =============================================================================

def synthesize_unified_diff(
    file_path: str,
    original_content: str,
    replacement_content: str,
    context_lines: int = 3,
) -> str:
    """
    Synthesize a valid POSIX unified diff directly applicable via `git apply`.
    Generates standard headers:
      diff --git a/{file_path} b/{file_path}
      --- a/{file_path}
      +++ b/{file_path}
    """
    norm_path = file_path.replace("\\", "/").lstrip("/")

    orig_lines = original_content.splitlines(keepends=True)
    repl_lines = replacement_content.splitlines(keepends=True)

    # Ensure trailing newline consistency
    if orig_lines and not orig_lines[-1].endswith("\n"):
        orig_lines[-1] = orig_lines[-1] + "\n"
    if repl_lines and not repl_lines[-1].endswith("\n"):
        repl_lines[-1] = repl_lines[-1] + "\n"

    diff_gen = difflib.unified_diff(
        orig_lines,
        repl_lines,
        fromfile=f"a/{norm_path}",
        tofile=f"b/{norm_path}",
        n=context_lines,
    )

    hunk_lines = list(diff_gen)
    if not hunk_lines:
        return ""

    # Prepend canonical git diff header
    output = [f"diff --git a/{norm_path} b/{norm_path}"]
    output.extend(line.rstrip("\r\n") for line in hunk_lines)
    return "\n".join(output) + "\n"


# =============================================================================
# 3. Diff Parser & AST
# =============================================================================

def parse_unified_diff(diff_text: str) -> List[DiffFile]:
    """
    Parse a unified diff into structured DiffFile and DiffHunk objects.
    """
    files: List[DiffFile] = []
    if not diff_text or not diff_text.strip():
        return files

    lines = diff_text.splitlines()
    current_file: Optional[DiffFile] = None
    current_hunk: Optional[DiffHunk] = None

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check for diff --git a/... b/...
        git_match = GIT_DIFF_HEADER_REGEX.match(line)
        if git_match:
            if current_hunk and current_file:
                current_file.hunks.append(current_hunk)
                current_hunk = None
            if current_file:
                files.append(current_file)

            current_file = DiffFile(
                orig_path=git_match.group(1),
                new_path=git_match.group(2),
            )
            i += 1
            continue

        # Check for --- a/... and +++ b/...
        if line.startswith("--- "):
            raw_orig = line[4:].strip()
            orig_p = raw_orig[2:] if raw_orig.startswith("a/") else raw_orig
            # Next line should be +++
            new_p = orig_p
            if i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                raw_new = lines[i + 1][4:].strip()
                new_p = raw_new[2:] if raw_new.startswith("b/") else raw_new
                i += 1

            if current_hunk and current_file:
                current_file.hunks.append(current_hunk)
                current_hunk = None

            if not current_file:
                current_file = DiffFile(orig_path=orig_p, new_path=new_p)
            else:
                current_file.orig_path = orig_p
                current_file.new_path = new_p

            i += 1
            continue

        # Check for hunk header @@ -start,len +start,len @@
        hunk_match = HUNK_HEADER_REGEX.match(line)
        if hunk_match:
            if current_hunk and current_file:
                current_file.hunks.append(current_hunk)
                current_hunk = None

            if not current_file:
                current_file = DiffFile(orig_path="unknown", new_path="unknown")

            o_start = int(hunk_match.group(1))
            o_len = int(hunk_match.group(2)) if hunk_match.group(2) is not None else 1
            n_start = int(hunk_match.group(3))
            n_len = int(hunk_match.group(4)) if hunk_match.group(4) is not None else 1
            heading = hunk_match.group(5).strip()

            current_hunk = DiffHunk(
                orig_start=o_start,
                orig_length=o_len,
                new_start=n_start,
                new_length=n_len,
                heading=heading,
                lines=[],
            )
            i += 1
            continue

        # Hunk content lines (context ' ', addition '+', deletion '-')
        if current_hunk is not None:
            if line.startswith(("+", "-", " ", "\\")):
                current_hunk.lines.append(line)
            elif not line.strip():
                # Some diff tools produce empty lines instead of ' '
                current_hunk.lines.append(" ")
            else:
                # Preserve non-prefix line inside hunk so validate_patch flags corrupt hunks and illegal prefixes
                current_hunk.lines.append(line)

        i += 1

    if current_hunk and current_file:
        current_file.hunks.append(current_hunk)
    if current_file:
        files.append(current_file)

    return files


# =============================================================================
# 4. Git Apply Syntax Validation
# =============================================================================

def validate_patch(diff_text: str) -> PatchValidationResult:
    """
    Validate that a unified diff adheres strictly to POSIX `git apply --check` syntax.
    Enforces:
    1. Presence of recognizable diff headers.
    2. Proper hunk header syntax `@@ -start,len +start,len @@`.
    3. Exact reconciliation between hunk line counts and header length specifications.
    4. Valid change indicator prefixes (+, -, ' ', \\).
    """
    if not diff_text or not diff_text.strip():
        return PatchValidationResult(
            is_valid=False,
            errors=["Diff payload is empty."],
        )

    errors: List[str] = []
    files = parse_unified_diff(diff_text)

    if not files:
        return PatchValidationResult(
            is_valid=False,
            errors=["No valid diff files or hunks could be parsed."],
        )

    total_hunks = 0
    for f_idx, file_diff in enumerate(files, start=1):
        if not file_diff.hunks:
            errors.append(f"File '{file_diff.orig_path}' has no change hunks.")
            continue

        for h_idx, hunk in enumerate(file_diff.hunks, start=1):
            total_hunks += 1
            orig_count = 0
            new_count = 0

            for l_idx, line in enumerate(hunk.lines, start=1):
                if not line:
                    prefix = " "
                else:
                    prefix = line[0]

                if prefix == " ":
                    orig_count += 1
                    new_count += 1
                elif prefix == "-":
                    orig_count += 1
                elif prefix == "+":
                    new_count += 1
                elif prefix == "\\":
                    # "\\ No newline at end of file" line does not increment line count
                    continue
                else:
                    errors.append(
                        f"File '{file_diff.new_path}' hunk #{h_idx} line #{l_idx}: "
                        f"Illegal prefix character '{prefix}' (expected ' ', '+', or '-')."
                    )

            # Check original line count reconciliation
            if orig_count != hunk.orig_length:
                errors.append(
                    f"File '{file_diff.orig_path}' hunk #{h_idx}: Header specified "
                    f"-{hunk.orig_start},{hunk.orig_length} but found {orig_count} original lines."
                )

            # Check new line count reconciliation
            if new_count != hunk.new_length:
                errors.append(
                    f"File '{file_diff.new_path}' hunk #{h_idx}: Header specified "
                    f"+{hunk.new_start},{hunk.new_length} but found {new_count} replacement lines."
                )

    return PatchValidationResult(
        is_valid=(len(errors) == 0),
        errors=errors,
        file_count=len(files),
        hunk_count=total_hunks,
    )


# =============================================================================
# 5. In-Memory Patch Application
# =============================================================================

def apply_patch_in_memory(original_content: str, diff_text: str) -> str:
    """
    Apply a unified diff directly to source code in memory.
    Useful for local validation and verification runs without filesystem mutation.
    """
    files = parse_unified_diff(diff_text)
    if not files or not files[0].hunks:
        return original_content

    orig_lines = original_content.splitlines(keepends=True)
    # Ensure all lines end with \n
    norm_lines = [l if l.endswith("\n") else l + "\n" for l in orig_lines]

    target_file = files[0]
    out_lines = list(norm_lines)

    # Apply hunks in reverse order (bottom-up) to preserve earlier line indices
    sorted_hunks = sorted(target_file.hunks, key=lambda h: h.orig_start, reverse=True)

    for hunk in sorted_hunks:
        start_idx = max(0, hunk.orig_start - 1)
        # Collect new lines for this hunk
        replacement_lines: List[str] = []
        for line in hunk.lines:
            if not line:
                replacement_lines.append("\n")
            elif line.startswith(("+", " ")):
                content = line[1:]
                if not content.endswith("\n"):
                    content += "\n"
                replacement_lines.append(content)
            elif line.startswith("-"):
                # Deleted line, skip
                pass
            elif line.startswith("\\"):
                # No newline at EOF marker
                pass

        # Replace slice in output
        delete_count = hunk.orig_length
        out_lines[start_idx : start_idx + delete_count] = replacement_lines

    return "".join(out_lines)
