"""
src/jury/mcp_server/__init__.py - MCP Stdio Server & Bridge Integration package for Jury Mesh v3.0.
"""

from __future__ import annotations

from jury.mcp_server.tools import (
    InvalidParamsError,
    execute_jury_ask,
    execute_jury_plan,
    execute_jury_review,
    get_tool_definitions,
)
from jury.mcp_server.server import McpServer, main

__all__ = [
    "McpServer",
    "main",
    "InvalidParamsError",
    "get_tool_definitions",
    "execute_jury_plan",
    "execute_jury_review",
    "execute_jury_ask",
]
