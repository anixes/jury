"""
src/jury/security/__init__.py - Jury Security & Host Protection Subsystem.
"""

from jury.security.contract import (
    ALLOWED_RUNNERS,
    ContractExecutionResult,
    TypedVerificationContract,
)
from jury.security.scrubber import (
    RedactionItem,
    RedactionResult,
    calculate_shannon_entropy,
    scrub_text,
)

__all__ = [
    "ALLOWED_RUNNERS",
    "ContractExecutionResult",
    "RedactionItem",
    "RedactionResult",
    "TypedVerificationContract",
    "calculate_shannon_entropy",
    "scrub_text",
]
