"""
triad.pack - Deterministic Context Pack Compiler.
Compiles AST signatures, 2-tier blast radius, and assumption manifests into LLM context packages.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from triad.ast_indexer import (
    FileDigest,
    SymbolSignature,
    SymbolType,
    compute_hash,
    digest_source_file,
    extract_python_interfaces,
)
from triad.assumptions import (
    AssumptionImpact,
    AssumptionRecord,
    build_assumption_manifest,
    format_assumptions_block,
)
from triad.graph import (
    BlastRadiusResult,
    DependencyGraph,
    build_repo_dependency_graph,
    calculate_blast_radius,
    estimate_symbol_tokens,
)
from triad.reviewer import get_git_diff
from triad.sanitizer import sanitize_text


@dataclass
class ContextPack:
    version: str = "1.0"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    git_commit_sha: str = ""
    target_diff: str = "unstaged"
    seed_symbols: List[SymbolSignature] = field(default_factory=list)
    mandatory_symbols: List[SymbolSignature] = field(default_factory=list)
    prioritized_symbols: List[SymbolSignature] = field(default_factory=list)
    omitted_symbols_count: int = 0
    assumptions: List[AssumptionRecord] = field(default_factory=list)
    total_estimated_tokens: int = 0
    secrets_redacted_count: int = 0

    def to_markdown(self) -> str:
        """Render context pack into a structured Markdown document for LLM consumption."""
        blocks = [
            f"# Context Pack (v{self.version})",
            f"**Commit:** `{self.git_commit_sha[:8]}` | **Diff Target:** `{self.target_diff}` | **Token Estimate:** ~{self.total_estimated_tokens}",
            "",
        ]

        if self.assumptions:
            blocks.append(format_assumptions_block(self.assumptions))
            blocks.append("")

        # 1. Seed Symbols (Directly modified in diff)
        blocks.append("### 🎯 Modified Symbols (Change Seeds)")
        if self.seed_symbols:
            for s in self.seed_symbols:
                blocks.append(f"- `{s.name}` ({s.symbol_type.value}) in `{s.file_path}:{s.line_number}`")
                if s.docstring:
                    first_line = s.docstring.strip().split("\n")[0]
                    blocks.append(f"  *{first_line}*")
        else:
            blocks.append("*No specific named symbols modified in diff.*")
        blocks.append("")

        # 2. Tier 1 Mandatory Context
        blocks.append("### 🔗 Tier 1 Mandatory Context (Direct Callers & Dependencies)")
        if self.mandatory_symbols:
            for s in self.mandatory_symbols:
                blocks.append(f"#### `{s.name}` ({s.symbol_type.value})")
                blocks.append(f"Location: `{s.file_path}:{s.line_number}` | Hash: `{s.signature_hash}`")
                blocks.append(f"```python\n# Signature digest\n{s.canonical_string()}\n```")
        else:
            blocks.append("*No 1-hop callers or dependencies detected.*")
        blocks.append("")

        # 3. Tier 2 Prioritized Context
        blocks.append("### 🌐 Tier 2 Prioritized Context (Transitive Callers)")
        if self.prioritized_symbols:
            for s in self.prioritized_symbols:
                blocks.append(f"- `{s.name}` ({s.symbol_type.value}) in `{s.file_path}` [Hash: `{s.signature_hash}`]")
        else:
            blocks.append("*No 2-hop transitive callers fitting token budget.*")
        blocks.append("")

        if self.omitted_symbols_count > 0:
            blocks.append(f"*(Note: {self.omitted_symbols_count} transitive symbols omitted to preserve token budget)*\n")

        return sanitize_text("\n".join(blocks)).sanitized_text

    def to_json(self) -> str:
        """Render context pack into machine-readable JSON."""
        data = {
            "version": self.version,
            "created_at": self.created_at,
            "git_commit_sha": self.git_commit_sha,
            "target_diff": self.target_diff,
            "seed_symbols": [asdict(s) for s in self.seed_symbols],
            "mandatory_symbols": [asdict(s) for s in self.mandatory_symbols],
            "prioritized_symbols": [asdict(s) for s in self.prioritized_symbols],
            "omitted_symbols_count": self.omitted_symbols_count,
            "assumptions": [asdict(a) for a in self.assumptions],
            "total_estimated_tokens": self.total_estimated_tokens,
            "secrets_redacted_count": self.secrets_redacted_count,
        }
        return json.dumps(data, indent=2)


def get_current_commit_sha(repo_dir: Path) -> str:
    """Return current git commit HEAD SHA."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10.0,
        )
        return res.stdout.strip() if res.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def extract_modified_files_from_diff(diff_text: str) -> Set[str]:
    """Parse unified git diff output to extract modified file paths."""
    modified_files = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path != "/dev/null":
                modified_files.add(path)
        elif line.startswith("--- a/") and not modified_files:
            path = line[6:].strip()
            if path != "/dev/null":
                modified_files.add(path)
    return modified_files


def compile_context_pack(
    repo_dir: Path,
    target: str = "unstaged",
    token_budget: int = 4000,
) -> ContextPack:
    """Compile a deterministic ContextPack for a given git repository and diff target."""
    base_dir = repo_dir.resolve()
    commit_sha = get_current_commit_sha(base_dir)

    # 1. Extract git diff
    raw_diff = get_git_diff(base_dir, target=target)
    modified_paths = extract_modified_files_from_diff(raw_diff)

    # 2. Discover python files in repository
    py_files: List[Path] = []
    for p in base_dir.rglob("*.py"):
        # Ignore tests, venvs, and cache directories
        rel = str(p.relative_to(base_dir)).replace("\\", "/")
        if any(seg in rel for seg in [".venv", "venv", "__pycache__", ".git", ".pytest_cache"]):
            continue
        py_files.append(p)

    # 3. Digest files and build graph
    file_digests: List[FileDigest] = []
    source_texts: Dict[str, str] = {}
    all_indexed_symbols: Set[str] = set()

    for f in py_files:
        try:
            rel_str = str(f.relative_to(base_dir)).replace("\\", "/")
            text = f.read_text(encoding="utf-8", errors="replace")
            source_texts[rel_str] = text
            digest = digest_source_file(f, base_dir=base_dir)
            file_digests.append(digest)
            for s in digest.symbols:
                all_indexed_symbols.add(s.name)
        except Exception:
            continue

    graph = build_repo_dependency_graph(file_digests, source_texts)

    # 4. Find seed symbols modified in diff
    seed_symbols: List[SymbolSignature] = []
    for fd in file_digests:
        if fd.file_path in modified_paths:
            for s in fd.symbols:
                seed_symbols.append(s)

    # 5. Compute blast radius
    seed_names = [s.name for s in seed_symbols]
    blast = calculate_blast_radius(graph, seed_names, token_budget=token_budget)

    # 6. Collect references and build assumptions
    all_refs = set()
    for s_name in seed_names:
        all_refs.update(graph.get_direct_dependencies(s_name))

    assumptions = build_assumption_manifest(all_refs, all_indexed_symbols)

    # 7. Check for redacted secrets in output
    sample_text = "\n".join(s.canonical_string() for s in blast.mandatory_symbols)
    san_res = sanitize_text(sample_text)

    return ContextPack(
        version="1.0",
        git_commit_sha=commit_sha,
        target_diff=target,
        seed_symbols=blast.seed_symbols,
        mandatory_symbols=blast.mandatory_symbols,
        prioritized_symbols=blast.prioritized_symbols,
        omitted_symbols_count=len(blast.omitted_symbols),
        assumptions=assumptions,
        total_estimated_tokens=blast.total_estimated_tokens,
        secrets_redacted_count=san_res.detected_count,
    )
