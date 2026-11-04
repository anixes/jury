"""
src/jury/cli/__init__.py - CLI Package Exports for Jury Mesh v3.0.
"""

from __future__ import annotations

# Defer imports of main to avoid runpy circular import warning when executing python -m jury.cli.main
def __getattr__(name: str):
    if name in ("main", "cli"):
        from jury.cli.main import cli, main
        return {"main": main, "cli": cli}[name]
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = [
    "main",
    "cli",
]
