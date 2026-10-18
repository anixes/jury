"""
triad.assumptions - Explicit Uncertainty and Assumption Manifest Engine.
Tracks unindexed external libraries and dynamic calls to prevent LLM hallucinations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set


class AssumptionImpact(str, Enum):
    HIGH = "HIGH"        # Missing critical auth, payment, or external RPC signature
    MEDIUM = "MEDIUM"    # Standard library or well-documented external package
    LOW = "LOW"          # Internal utility or non-critical helper


@dataclass(frozen=True)
class AssumptionRecord:
    id: str
    symbol_name: str
    reason: str             # "external_package", "dynamic_dispatch", "unindexed_file"
    impact: AssumptionImpact
    action_required: str

    def to_markdown(self) -> str:
        icon = "🚨" if self.impact == AssumptionImpact.HIGH else ("⚠️" if self.impact == AssumptionImpact.MEDIUM else "ℹ️")
        return (
            f"- {icon} **[{self.id}]** `{self.symbol_name}` ({self.impact.value} IMPACT)\n"
            f"  - **Reason:** {self.reason}\n"
            f"  - **Directive:** {self.action_required}"
        )


KNOWN_STDLIB_OR_SAFE = {
    "os", "sys", "json", "time", "re", "math", "pathlib", "typing",
    "dataclasses", "enum", "asyncio", "logging", "hashlib", "collections",
    "uuid", "copy", "itertools", "functools", "abc", "datetime",
}


def build_assumption_manifest(
    referenced_symbols: Set[str],
    indexed_symbol_names: Set[str],
) -> List[AssumptionRecord]:
    """Detect unindexed symbols and compile structured AssumptionRecords."""
    assumptions: List[AssumptionRecord] = []
    counter = 1

    for sym in sorted(referenced_symbols):
        if sym in indexed_symbol_names:
            continue

        base_pkg = sym.split(".")[0]
        if base_pkg in KNOWN_STDLIB_OR_SAFE:
            continue

        # Classify impact
        is_security_or_payment = any(
            k in sym.lower() for k in ["auth", "token", "payment", "charge", "stripe", "jwt", "crypto", "sql", "db"]
        )
        impact = AssumptionImpact.HIGH if is_security_or_payment else AssumptionImpact.MEDIUM

        action = (
            f"Verify external API contract for '{sym}'. Do not assume undocumented behavior."
            if impact == AssumptionImpact.HIGH
            else f"Refer to standard documentation for external package '{base_pkg}'."
        )

        rec = AssumptionRecord(
            id=f"ASM-{counter:03d}",
            symbol_name=sym,
            reason="external_package",
            impact=impact,
            action_required=action,
        )
        assumptions.append(rec)
        counter += 1

    return assumptions


def format_assumptions_block(assumptions: List[AssumptionRecord]) -> str:
    """Format assumptions into markdown block for injection into prompt."""
    if not assumptions:
        return ""

    lines = [
        "### ⚠️ Explicit Architectural Assumptions (Unindexed Dependencies)",
        "The following symbols are external or dynamic. Models must NOT hallucinate unverified behaviors:",
        "",
    ]
    for asm in assumptions:
        lines.append(asm.to_markdown())

    return "\n".join(lines)
