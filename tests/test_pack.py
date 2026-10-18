"""
tests/test_pack.py - Unit and Integration tests for Context Pack Compiler (Phase 4 & 5).
"""

import json
from pathlib import Path
import pytest

from triad.ast_indexer import SymbolSignature, SymbolType
from triad.cli import main
from triad.pack import (
    ContextPack,
    compile_context_pack,
    extract_modified_files_from_diff,
)


def test_extract_modified_files_from_diff():
    diff_sample = """diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -10,2 +10,4 @@
+def new_token():
+    pass
diff --git a/tests/test_auth.py b/tests/test_auth.py
--- a/tests/test_auth.py
+++ b/tests/test_auth.py
"""
    files = extract_modified_files_from_diff(diff_sample)
    assert "src/auth.py" in files
    assert "tests/test_auth.py" in files


def test_context_pack_json_serialization():
    sig = SymbolSignature(
        name="test_func",
        symbol_type=SymbolType.FUNCTION,
        file_path="service.py",
        line_number=12,
        parameters=["x: int"],
        return_type="int",
        signature_hash="abc12345",
    )
    pack = ContextPack(
        version="1.0",
        git_commit_sha="abcdef1234567890",
        target_diff="staged",
        seed_symbols=[sig],
        mandatory_symbols=[sig],
        prioritized_symbols=[],
        total_estimated_tokens=50,
    )

    json_str = pack.to_json()
    data = json.loads(json_str)
    assert data["version"] == "1.0"
    assert data["git_commit_sha"] == "abcdef1234567890"
    assert len(data["seed_symbols"]) == 1
    assert data["seed_symbols"][0]["name"] == "test_func"


def test_context_pack_markdown_formatting():
    sig = SymbolSignature(
        name="authenticate_user",
        symbol_type=SymbolType.FUNCTION,
        file_path="auth/jwt.py",
        line_number=45,
        parameters=["token: str"],
        return_type="bool",
        docstring="Validate JWT token.",
        signature_hash="f00ba4",
    )
    pack = ContextPack(
        version="1.0",
        git_commit_sha="1234567890",
        target_diff="origin/main...HEAD",
        seed_symbols=[sig],
        mandatory_symbols=[sig],
        total_estimated_tokens=120,
    )

    md = pack.to_markdown()
    assert "# Context Pack (v1.0)" in md
    assert "authenticate_user" in md
    assert "Tier 1 Mandatory Context" in md
    assert "Validate JWT token." in md


def test_cli_pack_subcommand(tmp_path: Path, monkeypatch):
    out_file = tmp_path / "pack.json"
    args = [
        "pack",
        "--repo", str(Path.cwd()),
        "--format", "json",
        "--output", str(out_file),
    ]
    exit_code = main(args)
    assert exit_code == 0
    assert out_file.exists()

    content = json.loads(out_file.read_text(encoding="utf-8"))
    assert "version" in content
    assert "total_estimated_tokens" in content
