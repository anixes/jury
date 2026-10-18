"""
tests/test_ast_indexer.py - Unit tests for AST Interface Extractor and Content-Addressed Digesting.
"""

from pathlib import Path
import pytest

from triad.ast_indexer import (
    FileDigest,
    PythonInterfaceExtractor,
    SymbolSignature,
    SymbolType,
    compute_hash,
    digest_source_file,
    extract_python_interfaces,
)

SAMPLE_CODE = '''"""Sample module docstring."""

from typing import List, Optional

TIMEOUT_DEFAULT: int = 30

def calculate_metric(name: str, values: List[float], scale: float = 1.0) -> float:
    """Calculate the average metric."""
    total = sum(values) * scale
    return total / len(values) if values else 0.0

async def fetch_async_data(url: str, retry: bool = True) -> Optional[dict]:
    """Fetch remote payload asynchronously."""
    # Implementation details that must be stripped
    x = 10
    return {"status": "ok"}

class DataProcessor:
    """Processes pipeline data."""
    buffer_size: int = 1024

    def __init__(self, capacity: int = 100):
        self.capacity = capacity
        self._data = []

    def process(self, item: dict) -> bool:
        """Process a single item."""
        if len(self._data) >= self.capacity:
            return False
        self._data.append(item)
        return True
'''


def test_extract_python_interfaces_functions():
    symbols, err = extract_python_interfaces(SAMPLE_CODE, "sample.py")
    assert err is None
    names = {s.name for s in symbols}

    assert "calculate_metric" in names
    assert "fetch_async_data" in names
    assert "DataProcessor" in names
    assert "DataProcessor.process" in names
    assert "DataProcessor.__init__" in names
    assert "TIMEOUT_DEFAULT" in names


def test_body_stripping():
    symbols, _ = extract_python_interfaces(SAMPLE_CODE, "sample.py")
    calc_sig = next(s for s in symbols if s.name == "calculate_metric")

    # Parameters and types are captured
    assert "name: str" in calc_sig.parameters
    assert "scale: float = 1.0" in calc_sig.parameters
    assert calc_sig.return_type == "float"
    assert calc_sig.docstring == "Calculate the average metric."

    # Crucial: verify canonical representation does NOT contain inner body logic ("total = sum")
    canonical = calc_sig.canonical_string()
    assert "sum(values)" not in canonical
    assert "total / len" not in canonical
    assert "calculate_metric" in canonical


def test_signature_stability_on_body_changes():
    """If a function's implementation changes but its signature remains identical, the signature hash must not change."""
    code_v1 = '''
def execute_task(task_id: str, priority: int = 1) -> bool:
    """Execute a priority task."""
    print("V1 algorithm")
    return True
'''
    code_v2 = '''
def execute_task(task_id: str, priority: int = 1) -> bool:
    """Execute a priority task."""
    # Completely revamped V2 algorithm
    logger.info("V2 fast path")
    res = 42 * priority
    return res > 0
'''
    symbols_v1, _ = extract_python_interfaces(code_v1, "task.py")
    symbols_v2, _ = extract_python_interfaces(code_v2, "task.py")

    assert len(symbols_v1) == 1
    assert len(symbols_v2) == 1
    assert symbols_v1[0].signature_hash == symbols_v2[0].signature_hash


def test_signature_changes_on_interface_modification():
    """If parameters, return type, or docstring change, signature hash must change."""
    code_v1 = 'def run(a: int) -> int:\n    return a'
    code_v2 = 'def run(a: int, b: int = 0) -> int:\n    return a + b'

    symbols_v1, _ = extract_python_interfaces(code_v1, "run.py")
    symbols_v2, _ = extract_python_interfaces(code_v2, "run.py")

    assert symbols_v1[0].signature_hash != symbols_v2[0].signature_hash


def test_malformed_python_graceful_handling():
    malformed = "def broken_func(:\n   bad syntax !!!"
    symbols, err = extract_python_interfaces(malformed, "broken.py")
    assert err is not None
    assert "Syntax error" in err
    assert len(symbols) == 0


def test_digest_source_file(tmp_path: Path):
    test_file = tmp_path / "service.py"
    test_file.write_text(SAMPLE_CODE, encoding="utf-8")

    digest = digest_source_file(test_file, base_dir=tmp_path)
    assert digest.error is None
    assert len(digest.content_hash) == 16
    assert len(digest.module_digest) == 16
    assert len(digest.symbols) >= 5
    assert digest.get_symbol_by_name("DataProcessor") is not None
