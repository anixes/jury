"""
triad.sanitizer - High-Confidence Secret Detection and Redaction Engine.
Protects sensitive tokens, private keys, and passwords from leaking into LLM context packs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple


# High-confidence regular expressions for credentials and keys
SECRET_PATTERNS = [
    # OpenAI & Gateway keys: sk-...
    (r"\bsk-[a-zA-Z0-9_\-]{20,}\b", "[REDACTED_API_KEY]"),
    # GitHub Personal Access Tokens: ghp_..., gho_..., github_pat_...
    (r"\b(ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9]{36,}\b", "[REDACTED_GITHUB_TOKEN]"),
    (r"\bgithub_pat_[a-zA-Z0-9_]{40,}\b", "[REDACTED_GITHUB_TOKEN]"),
    # AWS Access Key IDs
    (r"\b(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b", "[REDACTED_AWS_KEY]"),
    # Generic Bearer Tokens
    (r"Bearer\s+[a-zA-Z0-9_\-\.]{25,}", "Bearer [REDACTED_BEARER_TOKEN]"),
    # PEM Private Keys
    (
        r"-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----[\s\S]+?-----END\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----",
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Hardcoded passwords/secrets in assignments: api_key = "..." or password: "..."
    (
        r"""(?i)(?:api_key|secret_key|client_secret|auth_token|password|passwd)\s*[:=]\s*["']([^"'\s]{8,})["']""",
        '\\g<0>'[:0] + '[REDACTED_CREDENTIAL]',
    ),
]


@dataclass(frozen=True)
class SanitizationResult:
    sanitized_text: str
    detected_count: int
    detected_types: List[str]


def sanitize_text(text: str) -> SanitizationResult:
    """Scan text and replace detected secrets with redaction placeholders."""
    if not text:
        return SanitizationResult(sanitized_text="", detected_count=0, detected_types=[])

    sanitized = text
    detected_types = []
    total_count = 0

    # 1. Private keys
    if "-----BEGIN" in sanitized and "PRIVATE KEY-----" in sanitized:
        matches = re.findall(SECRET_PATTERNS[5][0], sanitized)
        if matches:
            sanitized = re.sub(SECRET_PATTERNS[5][0], SECRET_PATTERNS[5][1], sanitized)
            detected_types.append("PRIVATE_KEY")
            total_count += len(matches)

    # 2. OpenAI / Gateway keys
    sk_matches = re.findall(SECRET_PATTERNS[0][0], sanitized)
    if sk_matches:
        sanitized = re.sub(SECRET_PATTERNS[0][0], SECRET_PATTERNS[0][1], sanitized)
        detected_types.append("API_KEY")
        total_count += len(sk_matches)

    # 3. GitHub tokens
    gh_matches = re.findall(SECRET_PATTERNS[1][0], sanitized) + re.findall(SECRET_PATTERNS[2][0], sanitized)
    if gh_matches:
        sanitized = re.sub(SECRET_PATTERNS[1][0], SECRET_PATTERNS[1][1], sanitized)
        sanitized = re.sub(SECRET_PATTERNS[2][0], SECRET_PATTERNS[2][1], sanitized)
        detected_types.append("GITHUB_TOKEN")
        total_count += len(gh_matches)

    # 4. AWS keys
    aws_matches = re.findall(SECRET_PATTERNS[3][0], sanitized)
    if aws_matches:
        sanitized = re.sub(SECRET_PATTERNS[3][0], SECRET_PATTERNS[3][1], sanitized)
        detected_types.append("AWS_KEY")
        total_count += len(aws_matches)

    # 5. Bearer tokens
    bearer_matches = re.findall(SECRET_PATTERNS[4][0], sanitized)
    if bearer_matches:
        sanitized = re.sub(SECRET_PATTERNS[4][0], SECRET_PATTERNS[4][1], sanitized)
        detected_types.append("BEARER_TOKEN")
        total_count += len(bearer_matches)

    # 6. Hardcoded secret assignments
    def _redact_assignment(match: re.Match) -> str:
        nonlocal total_count
        full = match.group(0)
        secret_val = match.group(1)
        # Avoid redacting common placeholders like 'YOUR_API_KEY' or 'test1234'
        if any(w in secret_val.lower() for w in ["placeholder", "example", "replace", "dummy"]):
            return full
        total_count += 1
        return full.replace(secret_val, "[REDACTED_CREDENTIAL]")

    assignment_pattern = r"""(?i)\b(api_key|secret_key|client_secret|auth_token|password)\s*([:=])\s*["']([^"'\s]{8,})["']"""
    if re.search(assignment_pattern, sanitized):
        sanitized = re.sub(assignment_pattern, _redact_assignment, sanitized)
        detected_types.append("CREDENTIAL_ASSIGNMENT")

    return SanitizationResult(
        sanitized_text=sanitized,
        detected_count=total_count,
        detected_types=list(set(detected_types)),
    )
