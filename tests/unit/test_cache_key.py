"""
tests/unit/test_cache_key.py - Unit tests for 5-Vector Composite Cache Key Generator.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jury.state.cache_key import CacheKeyComponents, CompositeCacheKeyGenerator


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Fixture providing an initialized git repository with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # Configure git and commit initial file
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "jury-test@agenticai.local"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Jury Test Bot"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    initial_file = repo / "main.py"
    initial_file.write_text("print('hello world')\n", encoding="utf-8")

    subprocess.run(["git", "add", "main.py"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    return repo


def test_cache_key_determinism(git_repo: Path):
    """Verify identical inputs against the same git state produce identical keys."""
    key1 = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Review architecture plan",
        config_hash="cfg_v1",
        prompt_version="3.0.0",
        repo_root=git_repo,
    )
    key2 = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Review architecture plan",
        config_hash="cfg_v1",
        prompt_version="3.0.0",
        repo_root=git_repo,
    )

    assert key1 == key2
    assert len(key1) == 64  # SHA256 hex string


def test_intent_normalization_and_sensitivity(git_repo: Path):
    """Verify intent changes modify key, but leading/trailing whitespace is normalized."""
    key_base = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Review security invariants",
        repo_root=git_repo,
    )
    key_whitespace = CompositeCacheKeyGenerator.compute_cache_key(
        intent="  Review security invariants  \n",
        repo_root=git_repo,
    )
    key_different = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Review performance bottlenecks",
        repo_root=git_repo,
    )

    assert key_base == key_whitespace
    assert key_base != key_different


def test_dirty_tree_unstaged_diff_sensitivity(git_repo: Path):
    """Verify uncommitted unstaged file edits immediately invalidate the cache key."""
    key_clean = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Review diff",
        repo_root=git_repo,
    )

    # Modify file without staging (unstaged diff)
    file_path = git_repo / "main.py"
    file_path.write_text("print('hello modified unstaged')\n", encoding="utf-8")

    key_unstaged = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Review diff",
        repo_root=git_repo,
    )

    assert key_clean != key_unstaged


def test_dirty_tree_staged_diff_sensitivity(git_repo: Path):
    """Verify staging a file with git add immediately produces a distinct cache key."""
    # Modify file
    file_path = git_repo / "main.py"
    file_path.write_text("print('hello to be staged')\n", encoding="utf-8")

    key_unstaged = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Review diff",
        repo_root=git_repo,
    )

    # Stage the file
    subprocess.run(["git", "add", "main.py"], cwd=str(git_repo), check=True, capture_output=True)

    key_staged = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Review diff",
        repo_root=git_repo,
    )

    assert key_unstaged != key_staged


def test_untracked_files_sensitivity(git_repo: Path):
    """Verify adding an untracked file modifies the composite cache key."""
    key_before = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Review project",
        repo_root=git_repo,
    )

    # Create new untracked file
    new_file = git_repo / "untracked_helper.py"
    new_file.write_text("def secret_formula(): pass\n", encoding="utf-8")

    key_after = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Review project",
        repo_root=git_repo,
    )

    assert key_before != key_after


def test_config_hash_and_prompt_version_sensitivity(git_repo: Path):
    """Verify changes to config hash or prompt version invalidate the cache."""
    key_v1 = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Plan task",
        config_hash="config_a",
        prompt_version="3.0.0",
        repo_root=git_repo,
    )
    key_v2_cfg = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Plan task",
        config_hash="config_b",
        prompt_version="3.0.0",
        repo_root=git_repo,
    )
    key_v3_prompt = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Plan task",
        config_hash="config_a",
        prompt_version="3.1.0",
        repo_root=git_repo,
    )

    assert key_v1 != key_v2_cfg
    assert key_v1 != key_v3_prompt


def test_non_git_workspace_defensive_fallback(tmp_path: Path):
    """Verify non-git directories fall back cleanly without crashing and remain deterministic."""
    non_git = tmp_path / "plain_dir"
    non_git.mkdir()
    (non_git / "test.txt").write_text("plain text", encoding="utf-8")

    key1 = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Analyze plain directory",
        repo_root=non_git,
    )
    key2 = CompositeCacheKeyGenerator.compute_cache_key(
        intent="Analyze plain directory",
        repo_root=non_git,
    )

    assert key1 == key2
    assert len(key1) == 64
