"""
tests/test_sanitizer.py - Unit tests for Secret Sanitizer and Assumption Manifest (Phase 3).
"""

import pytest

from triad.assumptions import (
    AssumptionImpact,
    AssumptionRecord,
    build_assumption_manifest,
    format_assumptions_block,
)
from triad.sanitizer import sanitize_text


def test_sanitize_openai_api_key():
    text = 'client = OpenAI(api_key="sk-proj-1234567890abcdef1234567890abcdef")'
    res = sanitize_text(text)
    assert "[REDACTED_API_KEY]" in res.sanitized_text
    assert "sk-proj" not in res.sanitized_text
    assert res.detected_count >= 1
    assert "API_KEY" in res.detected_types


def test_sanitize_github_token():
    text = "Authorization: token ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"
    res = sanitize_text(text)
    assert "[REDACTED_GITHUB_TOKEN]" in res.sanitized_text
    assert "ghp_" not in res.sanitized_text
    assert "GITHUB_TOKEN" in res.detected_types


def test_sanitize_aws_key():
    text = 'AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"'
    res = sanitize_text(text)
    assert "[REDACTED_AWS_KEY]" in res.sanitized_text
    assert "AKIAIOSFODNN7EXAMPLE" not in res.sanitized_text


def test_sanitize_private_key():
    key_text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Y1+exampleFakePrivateKeyBodyHere1234567890abcdef\n"
        "-----END RSA PRIVATE KEY-----"
    )
    res = sanitize_text(f"Key data:\n{key_text}\nEnd of key")
    assert "[REDACTED_PRIVATE_KEY]" in res.sanitized_text
    assert "MIIEow" not in res.sanitized_text
    assert "PRIVATE_KEY" in res.detected_types


def test_no_false_positives_on_normal_code():
    code = 'keys = my_dict.keys()\nuser_token_param: str = "placeholder_value"'
    res = sanitize_text(code)
    assert "keys = my_dict.keys()" in res.sanitized_text
    assert res.detected_count == 0


def test_assumption_manifest_classification():
    refs = {"stripe.Charge.create", "requests.get", "os.path.join", "local_helper"}
    indexed = {"local_helper"}

    assumptions = build_assumption_manifest(refs, indexed)
    # os is stdlib, local_helper is indexed. So only stripe and requests should be in assumptions.
    symbols = {a.symbol_name for a in assumptions}
    assert "stripe.Charge.create" in symbols
    assert "requests.get" in symbols
    assert "os.path.join" not in symbols
    assert "local_helper" not in symbols

    # Stripe should be HIGH impact (payment)
    stripe_asm = next(a for a in assumptions if a.symbol_name == "stripe.Charge.create")
    assert stripe_asm.impact == AssumptionImpact.HIGH

    # Format check
    block = format_assumptions_block(assumptions)
    assert "Explicit Architectural Assumptions" in block
    assert "stripe.Charge.create" in block
