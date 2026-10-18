"""
triad.ast_indexer - Deterministic AST Interface Extractor and Content-Addressed Digesting.
Extracts public signatures, types, and docstrings while stripping implementation bodies.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


class SymbolType(str, Enum):
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"
    CLASS = "class"
    METHOD = "method"
    INTERFACE = "interface"
    CONSTANT = "constant"


@dataclass(frozen=True)
class SymbolSignature:
    """Normalized, body-stripped representation of a code entity."""
    name: str
    symbol_type: SymbolType
    file_path: str
    line_number: int
    parameters: List[str] = field(default_factory=list)
    return_type: Optional[str] = None
    modifiers: List[str] = field(default_factory=list)
    docstring: Optional[str] = None
    signature_hash: str = ""

    def canonical_string(self) -> str:
        """Generate a deterministic canonical string representation for content hashing."""
        params_str = ",".join(self.parameters)
        mods_str = ",".join(sorted(self.modifiers))
        ret_str = self.return_type or "None"
        doc_summary = (self.docstring.strip().split("\n")[0]) if self.docstring else ""
        return f"{self.symbol_type.value}|{self.name}|({params_str})->{ret_str}|mods:[{mods_str}]|doc:{doc_summary}"


def compute_hash(text: str) -> str:
    """Compute a compact 16-character SHA-256 digest."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class FileDigest:
    """Content-addressed digest of a source file and its exported symbols."""
    file_path: str
    content_hash: str
    symbols: List[SymbolSignature] = field(default_factory=list)
    module_digest: str = ""
    error: Optional[str] = None

    def get_symbol_by_name(self, name: str) -> Optional[SymbolSignature]:
        for s in self.symbols:
            if s.name == name:
                return s
        return None


class PythonInterfaceExtractor(ast.NodeVisitor):
    """AST visitor that extracts symbol signatures without implementation bodies."""

    def __init__(self, relative_path: str):
        self.relative_path = relative_path.replace("\\", "/")
        self.symbols: List[SymbolSignature] = []
        self._current_class: Optional[str] = None

    def _format_arg(self, arg: ast.arg, default: Optional[ast.AST] = None) -> str:
        res = arg.arg
        if arg.annotation:
            try:
                res += f": {ast.unparse(arg.annotation)}"
            except Exception:
                res += ": Any"
        if default:
            try:
                res += f" = {ast.unparse(default)}"
            except Exception:
                pass
        return res

    def _extract_params(self, args: ast.arguments) -> List[str]:
        params = []
        num_defaults = len(args.defaults)
        first_default_idx = len(args.args) - num_defaults

        for idx, arg in enumerate(args.args):
            default = None
            if idx >= first_default_idx:
                default = args.defaults[idx - first_default_idx]
            params.append(self._format_arg(arg, default))

        if args.vararg:
            vararg_str = f"*{args.vararg.arg}"
            if args.vararg.annotation:
                try:
                    vararg_str += f": {ast.unparse(args.vararg.annotation)}"
                except Exception:
                    pass
            params.append(vararg_str)

        for idx, kwarg in enumerate(args.kwonlyargs):
            default = args.kw_defaults[idx] if idx < len(args.kw_defaults) else None
            params.append(self._format_arg(kwarg, default))

        if args.kwarg:
            kwarg_str = f"**{args.kwarg.arg}"
            if args.kwarg.annotation:
                try:
                    kwarg_str += f": {ast.unparse(args.kwarg.annotation)}"
                except Exception:
                    pass
            params.append(kwarg_str)

        return params

    def _extract_modifiers(self, decorator_list: List[ast.AST]) -> List[str]:
        mods = []
        for dec in decorator_list:
            try:
                mods.append(f"@{ast.unparse(dec)}")
            except Exception:
                pass
        return mods

    def visit_ClassDef(self, node: ast.ClassDef):
        # Extract class signature
        modifiers = self._extract_modifiers(node.decorator_list)
        bases = []
        for b in node.bases:
            try:
                bases.append(ast.unparse(b))
            except Exception:
                pass
        if bases:
            modifiers.append(f"inherits:[{','.join(bases)}]")

        docstring = ast.get_docstring(node)
        raw_sig = SymbolSignature(
            name=node.name,
            symbol_type=SymbolType.CLASS,
            file_path=self.relative_path,
            line_number=node.lineno,
            parameters=bases,
            return_type=None,
            modifiers=modifiers,
            docstring=docstring,
        )
        sig = SymbolSignature(
            name=raw_sig.name,
            symbol_type=raw_sig.symbol_type,
            file_path=raw_sig.file_path,
            line_number=raw_sig.line_number,
            parameters=raw_sig.parameters,
            return_type=raw_sig.return_type,
            modifiers=raw_sig.modifiers,
            docstring=raw_sig.docstring,
            signature_hash=compute_hash(raw_sig.canonical_string()),
        )
        self.symbols.append(sig)

        # Visit class methods and fields
        prev_class = self._current_class
        self._current_class = node.name
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.AnnAssign)):
                self.visit(item)
        self._current_class = prev_class

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._handle_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._handle_function(node, is_async=True)

    def _handle_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool):
        stype = SymbolType.METHOD if self._current_class else (
            SymbolType.ASYNC_FUNCTION if is_async else SymbolType.FUNCTION
        )
        name = f"{self._current_class}.{node.name}" if self._current_class else node.name
        params = self._extract_params(node.args)
        ret_type = None
        if node.returns:
            try:
                ret_type = ast.unparse(node.returns)
            except Exception:
                ret_type = "Any"

        modifiers = self._extract_modifiers(node.decorator_list)
        if is_async and self._current_class:
            modifiers.append("async")

        docstring = ast.get_docstring(node)
        raw_sig = SymbolSignature(
            name=name,
            symbol_type=stype,
            file_path=self.relative_path,
            line_number=node.lineno,
            parameters=params,
            return_type=ret_type,
            modifiers=modifiers,
            docstring=docstring,
        )
        sig = SymbolSignature(
            name=raw_sig.name,
            symbol_type=raw_sig.symbol_type,
            file_path=raw_sig.file_path,
            line_number=raw_sig.line_number,
            parameters=raw_sig.parameters,
            return_type=raw_sig.return_type,
            modifiers=raw_sig.modifiers,
            docstring=raw_sig.docstring,
            signature_hash=compute_hash(raw_sig.canonical_string()),
        )
        self.symbols.append(sig)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        # Extract annotated constant/attribute
        if isinstance(node.target, ast.Name):
            name = f"{self._current_class}.{node.target.id}" if self._current_class else node.target.id
            ret_type = None
            try:
                ret_type = ast.unparse(node.annotation)
            except Exception:
                pass
            raw_sig = SymbolSignature(
                name=name,
                symbol_type=SymbolType.CONSTANT,
                file_path=self.relative_path,
                line_number=node.lineno,
                parameters=[],
                return_type=ret_type,
                modifiers=[],
                docstring=None,
            )
            sig = SymbolSignature(
                name=raw_sig.name,
                symbol_type=raw_sig.symbol_type,
                file_path=raw_sig.file_path,
                line_number=raw_sig.line_number,
                parameters=raw_sig.parameters,
                return_type=raw_sig.return_type,
                modifiers=raw_sig.modifiers,
                docstring=raw_sig.docstring,
                signature_hash=compute_hash(raw_sig.canonical_string()),
            )
            self.symbols.append(sig)


def extract_python_interfaces(content: str, relative_path: str = "module.py") -> Tuple[List[SymbolSignature], Optional[str]]:
    """Parse Python source code and extract public symbol signatures with bodies stripped."""
    try:
        tree = ast.parse(content, filename=relative_path)
    except SyntaxError as e:
        return [], f"Syntax error at line {e.lineno}: {e.msg}"
    except Exception as e:
        return [], f"Parse error: {e}"

    extractor = PythonInterfaceExtractor(relative_path)
    extractor.visit(tree)
    return extractor.symbols, None


def digest_source_file(file_path: Path, base_dir: Optional[Path] = None) -> FileDigest:
    """Read a source file, compute content hash and extract symbol interface digest."""
    rel_path = file_path.resolve()
    if base_dir:
        try:
            rel_path = file_path.resolve().relative_to(base_dir.resolve())
        except ValueError:
            pass
    rel_path_str = str(rel_path).replace("\\", "/")

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return FileDigest(file_path=rel_path_str, content_hash="", error=f"Read error: {e}")

    content_hash = compute_hash(content)

    if file_path.suffix.lower() == ".py":
        symbols, err = extract_python_interfaces(content, rel_path_str)
    else:
        # Generic fallback for non-python files
        symbols, err = [], None

    # Calculate overall module digest from symbol hashes
    if symbols:
        combined = "|".join(s.signature_hash for s in symbols)
        module_digest = compute_hash(combined)
    else:
        module_digest = content_hash

    return FileDigest(
        file_path=rel_path_str,
        content_hash=content_hash,
        symbols=symbols,
        module_digest=module_digest,
        error=err,
    )
