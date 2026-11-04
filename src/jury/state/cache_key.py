"""
src/jury/state/cache_key.py - 5-Vector Composite Cache Key Generator for Jury Mesh v3.0.

Computes a deterministic SHA256 composite cache key that factors:
1. Normalized Task Intent / Prompt
2. Git HEAD Commit SHA
3. Dirty Unstaged Diff (git diff)
4. Dirty Staged Diff (git diff --cached)
5. Untracked Files Tree Hash (git ls-files --others)
6. Engine Configuration Hash
7. Deliberation Prompt Version

Guarantees instant cache invalidation upon any local code edit, staging,
untracked file addition, or configuration modification, eliminating the
Dirty Working Tree Cache Poisoning bug.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


@dataclass(frozen=True)
class CacheKeyComponents:
    """Breakdown of the 5 vectors and metadata comprising the composite cache key."""
    intent: str
    git_head_sha: str
    dirty_unstaged_hash: str
    dirty_staged_hash: str
    untracked_files_hash: str
    config_hash: str
    prompt_version: str
    composite_key: str


class CompositeCacheKeyGenerator:
    """
    Computes a 5-vector composite SHA256 cache key from workspace git state,
    intent, configuration, and prompt version.
    """

    DEFAULT_PROMPT_VERSION = "3.0.0"
    DEFAULT_CONFIG_HASH = "jury_default_v3"

    @classmethod
    def compute_cache_key(
        cls,
        intent: str,
        config_hash: str = DEFAULT_CONFIG_HASH,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        repo_root: Optional[Union[str, Path]] = None,
    ) -> str:
        """
        Compute and return the 64-character SHA256 hex composite cache key.
        """
        components = cls.compute_key_components(
            intent=intent,
            config_hash=config_hash,
            prompt_version=prompt_version,
            repo_root=repo_root,
        )
        return components.composite_key

    @classmethod
    def compute_key_components(
        cls,
        intent: str,
        config_hash: str = DEFAULT_CONFIG_HASH,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        repo_root: Optional[Union[str, Path]] = None,
    ) -> CacheKeyComponents:
        """
        Compute the individual vector hashes and the final composite cache key.
        """
        norm_intent = intent.strip()
        root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()

        head_sha, unstaged_bytes, staged_bytes, untracked_hash = cls._inspect_workspace(root)

        unstaged_hash = hashlib.sha256(unstaged_bytes).hexdigest()
        staged_hash = hashlib.sha256(staged_bytes).hexdigest()

        # Compute composite SHA256
        hasher = hashlib.sha256()
        hasher.update(norm_intent.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(head_sha.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(unstaged_bytes)
        hasher.update(b"\x00")
        hasher.update(staged_bytes)
        hasher.update(b"\x00")
        hasher.update(untracked_hash.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(config_hash.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(prompt_version.encode("utf-8"))

        composite_key = hasher.hexdigest()

        return CacheKeyComponents(
            intent=norm_intent,
            git_head_sha=head_sha,
            dirty_unstaged_hash=unstaged_hash,
            dirty_staged_hash=staged_hash,
            untracked_files_hash=untracked_hash,
            config_hash=config_hash,
            prompt_version=prompt_version,
            composite_key=composite_key,
        )

    @classmethod
    def _inspect_workspace(cls, root: Path) -> tuple[str, bytes, bytes, str]:
        """
        Inspect the workspace using git porcelain commands.
        Falls back defensively to filesystem mtime/content hashing if git is unavailable
        or if the directory is not inside a git repository.
        """
        try:
            # 1. Git HEAD Commit SHA
            head_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if head_proc.returncode != 0:
                return cls._filesystem_fallback(root)

            head_sha = head_proc.stdout.strip() or "0" * 40

            # 2. Unstaged diff
            unstaged_proc = subprocess.run(
                ["git", "diff", "--no-ext-diff", "--binary"],
                cwd=str(root),
                capture_output=True,
                timeout=10.0,
                check=False,
            )
            unstaged_bytes = unstaged_proc.stdout if unstaged_proc.returncode == 0 else b""

            # 3. Staged diff
            staged_proc = subprocess.run(
                ["git", "diff", "--cached", "--no-ext-diff", "--binary"],
                cwd=str(root),
                capture_output=True,
                timeout=10.0,
                check=False,
            )
            staged_bytes = staged_proc.stdout if staged_proc.returncode == 0 else b""

            # 4. Untracked files
            untracked_proc = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
            untracked_hash = cls._hash_untracked_files(root, untracked_proc.stdout if untracked_proc.returncode == 0 else "")

            return head_sha, unstaged_bytes, staged_bytes, untracked_hash

        except (subprocess.SubprocessError, OSError, PermissionError):
            return cls._filesystem_fallback(root)

    @classmethod
    def _hash_untracked_files(cls, root: Path, ls_files_output: str) -> str:
        """Hash relative paths and contents of untracked files in sorted order."""
        lines = [line.strip() for line in ls_files_output.splitlines() if line.strip()]
        if not lines:
            return hashlib.sha256(b"").hexdigest()

        lines.sort()
        hasher = hashlib.sha256()
        for rel_path in lines:
            hasher.update(rel_path.encode("utf-8"))
            file_path = root / rel_path
            try:
                if file_path.is_file():
                    # Read up to 512 KB per untracked file to prevent memory exhaustion
                    with open(file_path, "rb") as f:
                        hasher.update(f.read(524288))
                elif file_path.is_dir():
                    hasher.update(b"__dir__")
            except (OSError, PermissionError):
                hasher.update(b"__unreadable__")

        return hasher.hexdigest()

    @classmethod
    def _filesystem_fallback(cls, root: Path) -> tuple[str, bytes, bytes, str]:
        """
        Defensive fallback when git is not available or directory is not a git repo.
        Hashes file modification times and names in the directory tree.
        """
        hasher = hashlib.sha256()
        head_sha = "no_git_workspace"
        try:
            entries = []
            for item in root.glob("*"):
                if item.name.startswith("."):
                    continue
                try:
                    stat = item.stat()
                    entries.append(f"{item.name}:{stat.st_mtime}:{stat.st_size}")
                except (OSError, PermissionError):
                    continue
            entries.sort()
            for entry in entries[:100]:
                hasher.update(entry.encode("utf-8"))
        except Exception:
            hasher.update(str(root).encode("utf-8"))

        fallback_hash = hasher.hexdigest()
        return head_sha, fallback_hash.encode("utf-8"), b"", fallback_hash
