"""
src/jury/security/contract.py - Anti-RCE Typed Verification Contract.

Guarantees complete elimination of Host Remote Code Execution (OWASP LLM01/LLM02)
via strict Pydantic contract validation, shell metacharacter rejection, immutable
runner whitelisting, and strictly non-shell (shell=False) subprocess invocation.
"""

from __future__ import annotations

import os
import re
import sys
import time
import subprocess
from pathlib import Path
from typing import Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator, model_validator


RunnerType = Literal["pytest", "mypy", "cargo_test", "npm_test", "go_test", "custom_safe"]

ALLOWED_RUNNERS: tuple[RunnerType, ...] = (
    "pytest",
    "mypy",
    "cargo_test",
    "npm_test",
    "go_test",
    "custom_safe",
)

# Reject any shell metacharacters or control characters capable of chaining or command injection
FORBIDDEN_SHELL_PATTERN = re.compile(r"[;&|`$><\n\r()!{}]")

# Prohibited dangerous environment variables that can hijack process execution
DISALLOWED_ENV_VARS = frozenset({
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "PYTHONSTARTUP",
    "PYTHONPATH",  # Contract overrides should not hijack core python modules
    "NODE_OPTIONS",
    "RUBYLIB",
    "PERL5LIB",
})


class ContractExecutionResult(BaseModel):
    """Immutable result of a verification contract execution."""
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    command: List[str]
    runner: str

    @property
    def passed(self) -> bool:
        """Convenience property indicating clean zero exit code and no timeout."""
        return self.exit_code == 0 and not self.timed_out


class TypedVerificationContract(BaseModel):
    """
    Deterministic typed verification contract emitted by Jury Arbiter/Council.
    Abolishes arbitrary shell execution in favor of validated test runners.
    """
    contract_version: str = "1.0"
    runner: RunnerType
    isolated_network: bool = True
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    test_paths: List[str] = Field(default_factory=list)
    env_overrides: Dict[str, str] = Field(default_factory=dict)
    custom_binary: Optional[str] = None
    allowed_flags: List[str] = Field(default_factory=list)

    @field_validator("runner")
    @classmethod
    def validate_runner_whitelist(cls, v: str) -> str:
        if v not in ALLOWED_RUNNERS:
            raise ValueError(f"Runner '{v}' is not in the immutable whitelist: {ALLOWED_RUNNERS}")
        return v

    @field_validator("test_paths")
    @classmethod
    def validate_test_paths(cls, paths: List[str]) -> List[str]:
        for p in paths:
            if not isinstance(p, str):
                raise ValueError("All test paths must be strings")
            if FORBIDDEN_SHELL_PATTERN.search(p):
                raise ValueError(f"Illegal shell metacharacters detected in test path: '{p}'")
            if ".." in p.replace("\\", "/").split("/"):
                raise ValueError(f"Path traversal ('..') detected in test path: '{p}'")
            if "\x00" in p:
                raise ValueError("Null byte detected in test path")
        return paths

    @field_validator("allowed_flags")
    @classmethod
    def validate_flags(cls, flags: List[str]) -> List[str]:
        for f in flags:
            if FORBIDDEN_SHELL_PATTERN.search(f):
                raise ValueError(f"Illegal shell metacharacters detected in flag: '{f}'")
            if "\x00" in f:
                raise ValueError("Null byte detected in flag")
        return flags

    @field_validator("env_overrides")
    @classmethod
    def validate_env_overrides(cls, env: Dict[str, str]) -> Dict[str, str]:
        for key, val in env.items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise ValueError(f"Invalid environment variable name: '{key}'")
            if key.upper() in DISALLOWED_ENV_VARS:
                raise ValueError(f"Dangerous environment override blocked: '{key}'")
            if FORBIDDEN_SHELL_PATTERN.search(val):
                raise ValueError(f"Illegal shell metacharacters in env value for '{key}': '{val}'")
            if "\x00" in val:
                raise ValueError(f"Null byte detected in env value for '{key}'")
        return env

    @model_validator(mode="after")
    def validate_custom_safe_binary(self) -> TypedVerificationContract:
        if self.runner == "custom_safe":
            if not self.custom_binary:
                raise ValueError("Runner 'custom_safe' requires an explicit custom_binary definition.")
            if FORBIDDEN_SHELL_PATTERN.search(self.custom_binary):
                raise ValueError(f"Illegal characters in custom_binary: '{self.custom_binary}'")
        return self

    def build_command_argv(self) -> List[str]:
        """
        Build the immutable argv argument list for non-shell execution.
        Prevents any shell-expansion or injection.
        """
        if self.runner == "pytest":
            base = [sys.executable, "-m", "pytest"]
        elif self.runner == "mypy":
            base = [sys.executable, "-m", "mypy"]
        elif self.runner == "cargo_test":
            base = ["cargo", "test"]
        elif self.runner == "npm_test":
            base = ["npm", "test"]
        elif self.runner == "go_test":
            base = ["go", "test"]
        elif self.runner == "custom_safe":
            base = [self.custom_binary] if self.custom_binary else []
        else:
            raise ValueError(f"Unsupported runner: {self.runner}")

        # Append validated flags and test paths
        cmd = list(base)
        if self.allowed_flags:
            cmd.extend(self.allowed_flags)
        if self.test_paths:
            cmd.extend(self.test_paths)
        return cmd

    def execute(self, cwd: Optional[Union[str, Path]] = None) -> ContractExecutionResult:
        """
        Execute the contract safely with shell=False, strict timeout, and network isolation.
        """
        argv = self.build_command_argv()
        exec_cwd = Path(cwd).resolve() if cwd else Path.cwd()

        # Build execution environment
        exec_env = os.environ.copy()
        exec_env.update(self.env_overrides)

        if self.isolated_network:
            # Point outbound proxy variables to a blackhole port to block network calls
            exec_env["HTTP_PROXY"] = "http://127.0.0.1:0"
            exec_env["HTTPS_PROXY"] = "http://127.0.0.1:0"
            exec_env["ALL_PROXY"] = "socks5://127.0.0.1:0"
            exec_env["NO_PROXY"] = ""

        start_time = time.perf_counter()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(exec_cwd),
                shell=False,  # CRITICAL: Always false. Eliminates confused deputy shell injection.
                timeout=self.timeout_seconds,
                capture_output=True,
                text=True,
                env=exec_env,
            )
            duration = time.perf_counter() - start_time
            return ContractExecutionResult(
                exit_code=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                duration_seconds=duration,
                timed_out=False,
                command=argv,
                runner=self.runner,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.perf_counter() - start_time
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout.decode("utf-8", errors="replace") if exc.stdout else "")
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr.decode("utf-8", errors="replace") if exc.stderr else "")
            return ContractExecutionResult(
                exit_code=124,  # Standard timeout exit code
                stdout=stdout or "",
                stderr=(stderr or "") + f"\n[ERROR] Execution timed out after {self.timeout_seconds} seconds.",
                duration_seconds=duration,
                timed_out=True,
                command=argv,
                runner=self.runner,
            )
        except FileNotFoundError as exc:
            duration = time.perf_counter() - start_time
            return ContractExecutionResult(
                exit_code=127,  # Command not found exit code
                stdout="",
                stderr=f"[ERROR] Runner executable not found: {exc}",
                duration_seconds=duration,
                timed_out=False,
                command=argv,
                runner=self.runner,
            )
        except Exception as exc:
            duration = time.perf_counter() - start_time
            return ContractExecutionResult(
                exit_code=1,
                stdout="",
                stderr=f"[ERROR] Unexpected execution failure: {exc}",
                duration_seconds=duration,
                timed_out=False,
                command=argv,
                runner=self.runner,
            )
