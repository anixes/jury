"""
triad.consultant - Spec and Architecture Consultation Logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

DEFAULT_CONSULT_SYSTEM_PROMPT = """You are a Principal Software Systems Architect.
Provide rigorous, production-grade architectural guidance and design for the proposed system or task:
1. Architectural decomposition, modular interfaces, and data flow.
2. Edge case identification, failure modes, and recovery mechanisms.
3. Cross-platform considerations (Windows, macOS, Linux).
4. Concrete implementation examples and patterns.
5. Performance, scalability, and resource efficiency.
Deliver your analysis cleanly structured with concrete technical directives.
"""


def load_spec_document(repo_dir: Path, spec_path: Optional[str]) -> str:
    """Load architectural specification or design doc from file, enforcing path confinement."""
    if not spec_path:
        return ""
    base_dir = repo_dir.resolve()
    raw_p = Path(spec_path)
    p = raw_p.resolve() if raw_p.is_absolute() else (base_dir / raw_p).resolve()

    try:
        is_confined = p.is_relative_to(base_dir)
    except AttributeError:
        is_confined = str(p).startswith(str(base_dir))

    if not is_confined:
        return f"# [Security Error] Path traversal detected: '{spec_path}' resolves outside repository root."

    if not p.is_file():
        return f"# [Error] Spec file not found at: {p.as_posix()}"
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:
        return f"# [Error] Failed reading spec file: {e}"


def build_consult_prompt(prompt_text: str, spec_text: str = "") -> str:
    """Assemble architectural consultation prompt."""
    blocks = []
    if prompt_text.strip():
        blocks.append(f"### Consultation Objective & Questions:\n{prompt_text.strip()}")
    if spec_text.strip():
        blocks.append(f"### Reference Specification / Design Document:\n{spec_text.strip()}")
    return "\n\n".join(blocks)
