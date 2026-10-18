"""
tests/test_graph.py - Unit tests for Blast Radius and Dependency Graph (Phase 2).
"""

import pytest

from triad.ast_indexer import FileDigest, SymbolSignature, SymbolType
from triad.graph import (
    BlastRadiusResult,
    DependencyGraph,
    build_repo_dependency_graph,
    calculate_blast_radius,
)


def _make_sym(name: str, file_path: str = "app.py") -> SymbolSignature:
    return SymbolSignature(
        name=name,
        symbol_type=SymbolType.FUNCTION,
        file_path=file_path,
        line_number=1,
        parameters=["x: int"],
        return_type="int",
        signature_hash=f"hash_{name}",
    )


def test_graph_edges_and_direct_callers():
    graph = DependencyGraph()
    sym_a = _make_sym("caller_a")
    sym_b = _make_sym("target_b")

    graph.register_symbol(sym_a)
    graph.register_symbol(sym_b)
    graph.add_edge("caller_a", "target_b")

    callers = graph.get_direct_callers("target_b")
    assert "caller_a" in callers
    assert "target_b" in graph.get_direct_dependencies("caller_a")


def test_1hop_and_2hop_blast_radius():
    graph = DependencyGraph()
    # Chain: service -> business_logic -> low_level_dao
    dao = _make_sym("low_level_dao")
    logic = _make_sym("business_logic")
    service = _make_sym("service")
    unrelated = _make_sym("unrelated_func")

    for s in [dao, logic, service, unrelated]:
        graph.register_symbol(s)

    graph.add_edge("business_logic", "low_level_dao")
    graph.add_edge("service", "business_logic")

    # Seed is low_level_dao
    result = calculate_blast_radius(graph, seed_symbol_names=["low_level_dao"], token_budget=2000)

    # Tier 1 (Mandatory) must contain business_logic
    mandatory_names = {s.name for s in result.mandatory_symbols}
    assert "business_logic" in mandatory_names

    # Tier 2 (Prioritized) must contain service
    prioritized_names = {s.name for s in result.prioritized_symbols}
    assert "service" in prioritized_names

    # Unrelated must NOT be included
    assert "unrelated_func" not in mandatory_names
    assert "unrelated_func" not in prioritized_names


def test_circular_dependency_termination():
    graph = DependencyGraph()
    sym_x = _make_sym("func_x")
    sym_y = _make_sym("func_y")

    graph.register_symbol(sym_x)
    graph.register_symbol(sym_y)

    # Circular calls: X -> Y -> X
    graph.add_edge("func_x", "func_y")
    graph.add_edge("func_y", "func_x")

    result = calculate_blast_radius(graph, seed_symbol_names=["func_x"], token_budget=2000)
    # Must terminate without infinite loop and without duplicates
    assert len(result.mandatory_symbols) == 1
    assert result.mandatory_symbols[0].name == "func_y"


def test_token_budget_pruning():
    graph = DependencyGraph()
    target = _make_sym("core_lib")
    graph.register_symbol(target)

    # 10 direct callers (Tier 1)
    for i in range(5):
        caller = _make_sym(f"tier1_{i}")
        graph.register_symbol(caller)
        graph.add_edge(f"tier1_{i}", "core_lib")

        # Each Tier 1 has a Tier 2 caller
        t2 = _make_sym(f"tier2_{i}")
        graph.register_symbol(t2)
        graph.add_edge(f"tier2_{i}", f"tier1_{i}")

    # Set very tight budget
    result = calculate_blast_radius(graph, seed_symbol_names=["core_lib"], token_budget=150)

    # Some Tier 2 symbols must be omitted due to budget
    assert len(result.omitted_symbols) > 0
    assert result.total_estimated_tokens <= 250
