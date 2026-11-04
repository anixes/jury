"""
src/jury/security/scrubber.py - 2-Stage Hybrid Secret Scrubbing Engine.

Performs pre-flight redaction to prevent secret and credential leakage to external LLM gateways:
1. High-confidence regex rules for OpenAI, GitHub, AWS, JWT, SSH/RSA keys, and passwords.
2. Shannon entropy filter H = -sum(p_i * log2(p_i)) detecting high-entropy cryptographic strings.
3. Formats replacements with synthetic, syntax-preserving CRC32 tokens: <REDACTED_<TYPE>:<CRC32_HEX>>.
"""

from __future__ import annotations

import math
import re
import zlib
from collections import Counter
from typing import List, NamedTuple, Optional, Set
from pydantic import BaseModel, Field


def calculate_shannon_entropy(data: str) -> float:
    """
    Calculate Shannon entropy in bits per character:
    H(X) = - sum_{i=1}^n P(x_i) * log2(P(x_i))
    """
    if not data:
        return 0.0
    length = len(data)
    counts = Counter(data)
    entropy = 0.0
    for count in counts.values():
        p = count / length
        entropy -= p * math.log2(p)
    return entropy


class RedactionItem(BaseModel):
    """Record of a detected and redacted secret."""
    secret_type: str
    original_fingerprint: str
    replacement_token: str
    start_index: int
    end_index: int
    line_number: int
    entropy: Optional[float] = None


class RedactionResult(BaseModel):
    """Result of scrubbing text containing redacted text and metadata."""
    scrubbed_text: str
    redactions: List[RedactionItem] = Field(default_factory=list)

    @property
    def sanitized_text(self) -> str:
        """Alias for compatibility with legacy sanitizer consumers."""
        return self.scrubbed_text

    @property
    def detected_count(self) -> int:
        """Total number of redacted secrets."""
        return len(self.redactions)

    @property
    def detected_types(self) -> Set[str]:
        """Unique secret types detected."""
        return {r.secret_type for r in self.redactions}


class _Span(NamedTuple):
    start: int
    end: int
    secret_type: str
    raw_secret: str
    entropy: Optional[float]


# Regex patterns for Stage 1 High-Confidence Scanning
REGEX_PATTERNS = [
    # SSH / RSA / EC Private Keys
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9\s]+ )?PRIVATE KEY-----[\s\S]+?-----END (?:[A-Z0-9\s]+ )?PRIVATE KEY-----"
        ),
        0,
    ),
    # OpenAI & LLM API Keys (sk-..., sk-proj-..., sk-ant-...)
    (
        "API_KEY",
        re.compile(r"\b(sk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,})\b"),
        1,
    ),
    # GitHub Personal Access Tokens (ghp_, gho_, ghu_, ghs_, ghr_, github_pat_)
    (
        "GITHUB_TOKEN",
        re.compile(r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b"),
        1,
    ),
    # AWS Access Key IDs
    (
        "AWS_KEY",
        re.compile(r"\b((?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16})\b"),
        1,
    ),
    # JWT Bearer tokens
    (
        "JWT_TOKEN",
        re.compile(r"Bearer\s+([A-Za-z0-9_\-\.]{25,})"),
        1,
    ),
    # Standalone JSON Web Tokens (header.payload.signature)
    (
        "JWT_TOKEN",
        re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"),
        1,
    ),
    # Generic password or secret assignments (e.g. password = "...", db_password: '...', api_key = "...")
    (
        "PASSWORD",
        re.compile(
            r"""(?i)(?:^|[^A-Za-z0-9_])([A-Za-z0-9_]*(?:api_key|secret_key|client_secret|auth_token|password|passwd|access_token|private_token)[A-Za-z0-9_]*)\s*[:=]\s*(['"])([^'"]{8,})\2"""
        ),
        3,  # Group 3 is the raw secret string inside the quotes
    ),
]


def _overlaps(start: int, end: int, spans: List[_Span]) -> bool:
    """Check if the given range overlaps with any existing accepted span."""
    for s in spans:
        if max(start, s.start) < min(end, s.end):
            return True
    return False


def scrub_text(
    text: str,
    entropy_threshold: float = 4.5,
    min_entropy_len: int = 20,
) -> RedactionResult:
    """
    Scrub secrets from the given input text using the 2-stage scanning pipeline:
    1. High-confidence regex pattern matching.
    2. Shannon entropy scanning for cryptographic hexadecimal and base64 strings.
    
    Replaces detected secrets with synthetic format-preserving CRC32 tokens:
    <REDACTED_<TYPE>:<CRC32_HEX>>
    """
    if not text:
        return RedactionResult(scrubbed_text="", redactions=[])

    spans: List[_Span] = []

    # -------------------------------------------------------------------------
    # STAGE 1: High-Confidence Regex Scanning
    # -------------------------------------------------------------------------
    for secret_type, pattern, group_idx in REGEX_PATTERNS:
        for match in pattern.finditer(text):
            if group_idx == 0:
                start, end = match.start(), match.end()
                raw_secret = match.group(0)
            else:
                start, end = match.start(group_idx), match.end(group_idx)
                raw_secret = match.group(group_idx)

            if not _overlaps(start, end, spans):
                spans.append(_Span(
                    start=start,
                    end=end,
                    secret_type=secret_type,
                    raw_secret=raw_secret,
                    entropy=calculate_shannon_entropy(raw_secret),
                ))

    # -------------------------------------------------------------------------
    # STAGE 2: Shannon Entropy Filter
    # -------------------------------------------------------------------------
    # Candidate string literals in quotes or contiguous base64/hex token candidates
    token_pattern = re.compile(r"""(?:(['"])(?P<quoted>[^\r\n'"]{20,})\1|(?P<token>\b[A-Za-z0-9+/=_-]{20,}\b))""")

    for match in token_pattern.finditer(text):
        if match.group("quoted"):
            start, end = match.start("quoted"), match.end("quoted")
            candidate = match.group("quoted")
        else:
            start, end = match.start("token"), match.end("token")
            candidate = match.group("token")

        if _overlaps(start, end, spans):
            continue

        # Skip obvious non-secrets: URLs, file paths, standard prose with spaces
        if candidate.startswith(("http://", "https://", "ftp://", "file://")):
            continue
        # Skip pure identifier words (only lowercase letters and underscores)
        if re.match(r"^[a-z_]+$", candidate):
            continue

        entropy = calculate_shannon_entropy(candidate)

        # Base64 / high-alphabet tokens: H >= 4.5 and len >= 20
        # Hex tokens (0-9a-fA-F): theoretical max is 4.0; flag if len >= 32 and H >= 3.5
        is_hex = bool(re.match(r"^[0-9a-fA-F]+$", candidate))
        is_secret = False

        if is_hex and len(candidate) >= 32 and entropy >= 3.5:
            is_secret = True
        elif len(candidate) >= min_entropy_len and entropy >= entropy_threshold:
            is_secret = True

        if is_secret:
            spans.append(_Span(
                start=start,
                end=end,
                secret_type="HIGH_ENTROPY_SECRET",
                raw_secret=candidate,
                entropy=entropy,
            ))

    # -------------------------------------------------------------------------
    # STAGE 3: Synthetic Format-Preserving Masking
    # -------------------------------------------------------------------------
    # Sort spans in reverse order of start position for in-place string substitution
    spans.sort(key=lambda s: s.start, reverse=True)

    redactions: List[RedactionItem] = []
    modified_text = text

    for span in spans:
        # Generate 8-character uppercase CRC32 hex checksum
        crc32_hex = f"{zlib.crc32(span.raw_secret.encode('utf-8')):08X}"
        replacement_token = f"<REDACTED_{span.secret_type}:{crc32_hex}>"

        line_number = text[:span.start].count("\n") + 1
        fingerprint = f"{span.raw_secret[:4]}...{crc32_hex}" if len(span.raw_secret) > 8 else crc32_hex

        redactions.append(RedactionItem(
            secret_type=span.secret_type,
            original_fingerprint=fingerprint,
            replacement_token=replacement_token,
            start_index=span.start,
            end_index=span.end,
            line_number=line_number,
            entropy=span.entropy,
        ))

        # Perform format-preserving replacement
        modified_text = modified_text[:span.start] + replacement_token + modified_text[span.end:]

    # Reverse redactions list so it matches document forward order
    redactions.reverse()

    return RedactionResult(
        scrubbed_text=modified_text,
        redactions=redactions,
    )
