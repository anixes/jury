"""
tests/e2e/conftest.py - Shared E2E Fixtures & Test Harness for Jury Mesh (jury-mcp) v3.0.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import pytest

# Ensure triad and src/ are in sys.path for test discovery and in-process validation
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass
class CliResult:
    """Captured result of a CLI execution."""
    exit_code: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def json(self) -> Dict[str, Any]:
        """Parse stdout as JSON."""
        return json.loads(self.stdout)


class GitTestRepo:
    """Helper wrapper for an isolated git repository."""
    def __init__(self, path: Path):
        self.path = path
        self._init_repo()

    def _init_repo(self) -> None:
        subprocess.run(["git", "init"], cwd=str(self.path), check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "jury-test@example.com"], cwd=str(self.path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Jury Test Bot"], cwd=str(self.path), check=True, capture_output=True)

    def write_file(self, relpath: str, content: str) -> Path:
        target = self.path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def commit_file(self, relpath: str, content: str, message: str = "commit") -> Path:
        target = self.write_file(relpath, content)
        subprocess.run(["git", "add", relpath], cwd=str(self.path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", message], cwd=str(self.path), check=True, capture_output=True)
        return target

    def stage_file(self, relpath: str) -> None:
        subprocess.run(["git", "add", relpath], cwd=str(self.path), check=True, capture_output=True)

    def unstage_file(self, relpath: str) -> None:
        subprocess.run(["git", "restore", "--staged", relpath], cwd=str(self.path), check=True, capture_output=True)

    def get_diff(self, staged: bool = False) -> str:
        cmd = ["git", "diff", "--cached"] if staged else ["git", "diff"]
        res = subprocess.run(cmd, cwd=str(self.path), check=True, capture_output=True, text=True)
        return res.stdout

    def apply_patch(self, patch_text: str, check_only: bool = False) -> subprocess.CompletedProcess:
        cmd = ["git", "apply"]
        if check_only:
            cmd.append("--check")
        return subprocess.run(
            cmd,
            input=patch_text,
            cwd=str(self.path),
            capture_output=True,
            text=True,
        )


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> GitTestRepo:
    """Fixture providing a clean, initialized git repository with an initial commit."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    repo = GitTestRepo(repo_dir)
    # Add an initial baseline commit so HEAD exists
    repo.commit_file("README.md", "# Test Project\nInitial baseline commit.\n", "Initial commit")
    return repo


@pytest.fixture
def empty_git_repo(tmp_path: Path) -> GitTestRepo:
    """Fixture providing an empty git repository with 0 commits."""
    repo_dir = tmp_path / "empty_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    return GitTestRepo(repo_dir)


@pytest.fixture
def isolated_jury_env(tmp_path: Path) -> Generator[Dict[str, str], None, None]:
    """
    Fixture providing an isolated environment dictionary for Jury state DB
    and token bucket records, guaranteeing zero pollution of ~/.jury/state.db.
    """
    state_dir = tmp_path / "jury_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["JURY_STATE_DIR"] = str(state_dir)
    env["JURY_OFFLINE_TEST"] = "1"
    # Ensure src is on PYTHONPATH for subprocess calls
    py_path = str(SRC_DIR) + os.pathsep + str(PROJECT_ROOT)
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = py_path + os.pathsep + env["PYTHONPATH"]
    else:
        env["PYTHONPATH"] = py_path

    old_env = os.environ.copy()
    os.environ.update(env)
    try:
        yield env
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def is_milestone_available(milestone: str) -> bool:
    """
    Progressive Testability check:
    Returns True if the required milestone modules exist in src/jury.
    """
    if milestone == "M1":
        return (SRC_DIR / "jury" / "security" / "contract.py").exists() and \
               (SRC_DIR / "jury" / "security" / "scrubber.py").exists()
    elif milestone == "M2":
        return (SRC_DIR / "jury" / "state" / "engine.py").exists() or \
               (SRC_DIR / "jury" / "state" / "rate_limiter.py").exists()
    elif milestone == "M3":
        return (SRC_DIR / "jury" / "adapters" / "base.py").exists()
    elif milestone == "M4":
        return (SRC_DIR / "jury" / "deliberation" / "mesh.py").exists() or \
               (SRC_DIR / "jury" / "deliberation" / "patch.py").exists()
    elif milestone == "M5":
        return (SRC_DIR / "jury" / "cli" / "main.py").exists() or \
               shutil.which("jury") is not None
    elif milestone == "M6":
        return (SRC_DIR / "jury" / "mcp_server" / "server.py").exists() or \
               shutil.which("jury-mcp") is not None
    return False


def require_milestone(milestone: str) -> None:
    """
    Enforces Progressive Testability guideline:
    If running during intermediate milestone implementation, tests requiring
    milestones not yet implemented are skipped with a clear status notice.
    If JURY_STRICT_E2E=1, skipping is disabled and tests run strictly.
    """
    if os.environ.get("JURY_STRICT_E2E", "0") == "1":
        return
    if not is_milestone_available(milestone):
        pytest.skip(f"Feature requires Milestone {milestone} which is not yet implemented.")


class JuryCliRunner:
    """Opaque-box CLI runner executing `jury` as an external end-user command."""
    def __init__(self, default_cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None):
        self.default_cwd = default_cwd or PROJECT_ROOT
        self.env = env or os.environ.copy()

    def run(
        self,
        args: List[str],
        cwd: Optional[Path] = None,
        stdin: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        milestone: str = "M5",
    ) -> CliResult:
        require_milestone(milestone)
        work_dir = str(cwd or self.default_cwd)
        run_env = self.env.copy()
        if env:
            run_env.update(env)

        # Determine CLI entry point: binary on PATH vs python -m jury.cli.main
        if shutil.which("jury"):
            cmd = ["jury"] + args
        else:
            cmd = [sys.executable, "-m", "jury.cli.main"] + args

        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            input=stdin,
            capture_output=True,
            text=True,
            env=run_env,
        )
        return CliResult(
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )


@pytest.fixture
def jury_cli(isolated_jury_env: Dict[str, str]) -> JuryCliRunner:
    """Fixture providing an opaque-box Jury CLI runner."""
    return JuryCliRunner(env=isolated_jury_env)


class JuryMcpRunner:
    """Opaque-box MCP stdio server runner communicating over JSON-RPC 2.0."""
    def __init__(self, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None):
        self.cwd = cwd or PROJECT_ROOT
        self.env = env or os.environ.copy()

    def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        milestone: str = "M6",
    ) -> Dict[str, Any]:
        require_milestone(milestone)
        work_dir = str(cwd or self.cwd)
        run_env = self.env.copy()
        if env:
            run_env.update(env)

        if shutil.which("jury-mcp"):
            cmd = ["jury-mcp"]
        else:
            cmd = [sys.executable, "-m", "jury.mcp_server.server"]

        request_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        }

        input_str = json.dumps(request_payload) + "\n"
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            input=input_str,
            capture_output=True,
            text=True,
            env=run_env,
            timeout=15,
        )

        # Channel hygiene assertion: stdout must be strictly JSON-RPC
        stdout_lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not stdout_lines:
            raise AssertionError(f"MCP server emitted no stdout response. Stderr: {proc.stderr}")

        try:
            response = json.loads(stdout_lines[0])
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"Stdout channel hygiene violation: MCP stdout is not valid JSON-RPC 2.0: '{stdout_lines[0]}'. "
                f"Full stdout: '{proc.stdout}', Stderr: '{proc.stderr}'"
            ) from exc

        return {
            "response": response,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "exit_code": proc.returncode,
        }


@pytest.fixture
def jury_mcp(isolated_jury_env: Dict[str, str]) -> JuryMcpRunner:
    """Fixture providing an opaque-box Jury MCP runner."""
    return JuryMcpRunner(env=isolated_jury_env)
