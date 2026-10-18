"""
triad.graph - Blast Radius & Two-Tier Dependency Graph Engine.
Constructs symbol dependency topologies and calculates 1-hop mandatory and 2-hop prioritized context.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from triad.ast_indexer import FileDigest, SymbolSignature, SymbolType, digest_source_file


@dataclass(frozen=True)
class DependencyEdge:
    source_symbol: str
    target_symbol: str
    relationship: str = "calls"  # "calls", "imports", "inherits"


@dataclass
class BlastRadiusResult:
    seed_symbols: List[SymbolSignature]
    mandatory_symbols: List[SymbolSignature]   # 1-hop direct callers & types
    prioritized_symbols: List[SymbolSignature] # 2-hop transitive callers fitting budget
    omitted_symbols: List[SymbolSignature]     # Excluded due to token budget cap
    total_estimated_tokens: int = 0


class SymbolReferenceCollector(ast.NodeVisitor):
    """AST visitor that detects function calls and attribute references within a symbol body."""

    def __init__(self, current_symbol: str):
        self.current_symbol = current_symbol
        self.references: Set[str] = set()

    def visit_Call(self, node: ast.Call):
        # Direct function call: foo()
        if isinstance(node.func, ast.Name):
            self.references.add(node.func.id)
        # Method or attribute call: obj.method() or module.func()
        elif isinstance(node.func, ast.Attribute):
            self.references.add(node.func.attr)
            if isinstance(node.func.value, ast.Name):
                self.references.add(f"{node.func.value.id}.{node.func.attr}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        self.references.add(node.attr)
        self.generic_visit(node)


class DependencyGraph:
    """Directed graph mapping call and dependency edges between code symbols."""

    def __init__(self):
        # target -> set of sources (callers)
        self.incoming_edges: Dict[str, Set[str]] = defaultdict(set)
        # source -> set of targets (dependencies)
        self.outgoing_edges: Dict[str, Set[str]] = defaultdict(set)
        # symbol_name -> SymbolSignature
        self.symbol_table: Dict[str, SymbolSignature] = {}

    def register_symbol(self, symbol: SymbolSignature):
        self.symbol_table[symbol.name] = symbol

    def add_edge(self, source: str, target: str, relationship: str = "calls"):
        self.outgoing_edges[source].add(target)
        self.incoming_edges[target].add(source)

    def get_direct_callers(self, symbol_name: str) -> Set[str]:
        """Return all symbols that directly call or depend on symbol_name."""
        res = set(self.incoming_edges.get(symbol_name, set()))
        # Check unqualified name as well
        short_name = symbol_name.split(".")[-1]
        if short_name != symbol_name:
            res.update(self.incoming_edges.get(short_name, set()))
        return res

    def get_direct_dependencies(self, symbol_name: str) -> Set[str]:
        """Return all symbols that symbol_name directly calls or references."""
        return set(self.outgoing_edges.get(symbol_name, set()))


def build_repo_dependency_graph(file_digests: List[FileDigest], source_texts: Dict[str, str]) -> DependencyGraph:
    """Build cross-symbol dependency graph by analyzing references in function bodies."""
    graph = DependencyGraph()

    # 1. Register all symbols in the graph
    for fd in file_digests:
        for sym in fd.symbols:
            graph.register_symbol(sym)

    # 2. Extract references from each file's AST
    for fd in file_digests:
        content = source_texts.get(fd.file_path, "")
        if not content:
            continue
        try:
            tree = ast.parse(content, filename=fd.file_path)
        except Exception:
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_name = node.name
                # Collect references in function body
                collector = SymbolReferenceCollector(func_name)
                collector.visit(node)

                # Match references to known symbols
                for ref in collector.references:
                    if ref in graph.symbol_table and ref != func_name:
                        graph.add_edge(source=func_name, target=ref, relationship="calls")

    return graph


def estimate_symbol_tokens(symbol: SymbolSignature) -> int:
    """Estimate token footprint of an interface signature (chars / 4 + overhead)."""
    text = symbol.canonical_string()
    return max(15, len(text) // 4 + 10)


def calculate_blast_radius(
    graph: DependencyGraph,
    seed_symbol_names: List[str],
    token_budget: int = 4000,
) -> BlastRadiusResult:
    """
    Calculate 2-tier blast radius starting from seed symbols:
    - Tier 1 (Mandatory): Direct callers and referenced dependencies (1-hop).
    - Tier 2 (Prioritized): Transitive callers (2-hop) prioritized by incoming degree.
    """
    visited_names: Set[str] = set()
    seed_symbols: List[SymbolSignature] = []
    for name in seed_symbol_names:
        if name in graph.symbol_table:
            seed_symbols.append(graph.symbol_table[name])
            visited_names.add(name)

    # 1. Collect Tier 1 (1-hop)
    tier1_names: Set[str] = set()
    for s_name in seed_symbol_names:
        direct_callers = graph.get_direct_callers(s_name)
        direct_deps = graph.get_direct_dependencies(s_name)
        tier1_names.update(direct_callers)
        tier1_names.update(direct_deps)

    tier1_names -= visited_names
    visited_names.update(tier1_names)

    mandatory_symbols: List[SymbolSignature] = []
    current_tokens = sum(estimate_symbol_tokens(s) for s in seed_symbols)

    for name in sorted(tier1_names):
        sym = graph.symbol_table.get(name)
        if sym:
            mandatory_symbols.append(sym)
            current_tokens += estimate_symbol_tokens(sym)

    # 2. Collect Tier 2 (2-hop)
    tier2_candidates: Set[str] = set()
    for t1_name in tier1_names:
        t2_callers = graph.get_direct_callers(t1_name)
        tier2_candidates.update(t2_callers)

    tier2_candidates -= visited_names

    # Rank Tier 2 candidates by importance score (in-degree + public API bonus)
    def _rank_key(cand_name: str) -> float:
        in_degree = len(graph.get_direct_callers(cand_name))
        is_public = not cand_name.split(".")[-1].startswith("_")
        return in_degree * 2.0 + (1.0 if is_public else 0.0)

    ranked_tier2 = sorted(tier2_candidates, key=_rank_key, reverse=True)

    prioritized_symbols: List[SymbolSignature] = []
    omitted_symbols: List[SymbolSignature] = []

    for cand_name in ranked_tier2:
        sym = graph.symbol_table.get(cand_name)
        if not sym:
            continue
        cost = estimate_symbol_tokens(sym)
        if current_tokens + cost <= token_budget:
            prioritized_symbols.append(sym)
            current_tokens += cost
        else:
            omitted_symbols.append(sym)

    return BlastRadiusResult(
        seed_symbols=seed_symbols,
        mandatory_symbols=mandatory_symbols,
        prioritized_symbols=prioritized_symbols,
        omitted_symbols=omitted_symbols,
        total_estimated_tokens=current_tokens,
    )
