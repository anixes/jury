"""
src/jury/mcp_server/server.py - JSON-RPC 2.0 Stdio MCP Server for Jury Mesh v3.0.

Provides standard Model Context Protocol (MCP) server over stdin/stdout:
- Strict channel isolation: sys.stdout is reserved exclusively for single-line JSON-RPC frames.
- All diagnostics, logging, and stderr streams are routed to sys.stderr.
- Supported methods: initialize, notifications/initialized, ping, tools/list, tools/call.
- Error codes: -32700 (Parse error), -32600 (Invalid request), -32601 (Method not found),
  -32602 (Invalid params), -32603 (Internal error).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Union

from jury.deliberation.mesh import DeliberationMesh
from jury.mcp_server.tools import (
    InvalidParamsError,
    execute_jury_ask,
    execute_jury_plan,
    execute_jury_review,
    get_tool_definitions,
)
from jury.state.engine import JuryStateEngine

logger = logging.getLogger("jury.mcp.server")


class McpServer:
    """
    High-performance, daemon-less MCP Stdio server adhering to JSON-RPC 2.0.
    """

    def __init__(
        self,
        mesh: Optional[DeliberationMesh] = None,
        state_engine: Optional[JuryStateEngine] = None,
        repo_root: Optional[Path] = None,
    ):
        self.repo_root = repo_root or Path.cwd()
        is_mock = os.environ.get("JURY_OFFLINE_TEST") == "1" or os.environ.get("JURY_MOCK_MODE") == "1"
        self.mesh = mesh or DeliberationMesh(mock_mode=is_mock)
        self.state_engine = state_engine or JuryStateEngine()
        self._real_stdout: TextIO = sys.stdout
        self._setup_hygiene()

    def _setup_hygiene(self) -> None:
        """
        Configure stream encoding, redirect stdout to stderr for rogue prints,
        and ensure sys.stderr receives all logging output.
        """
        # Save reference to genuine process stdout before redirecting
        self._real_stdout = sys.stdout

        # Reconfigure streams to utf-8 if supported (essential for Windows)
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8")
                except Exception:
                    pass

        # Configure root logger to output exclusively to stderr
        logging.basicConfig(
            stream=sys.stderr,
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

        # Redirect sys.stdout to sys.stderr so any unbuffered prints or third-party output
        # never leak non-JSON bytes into the JSON-RPC communication channel
        sys.stdout = sys.stderr

    def send_frame(self, data: Union[Dict[str, Any], List[Dict[str, Any]]]) -> None:
        """
        Emit a strictly valid JSON-RPC frame line to the real process stdout.
        """
        payload = json.dumps(data) + "\n"
        self._real_stdout.write(payload)
        self._real_stdout.flush()

    async def handle_request(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Dispatch a single parsed JSON-RPC request dictionary to the appropriate handler.
        """
        if not isinstance(message, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: expected JSON object"},
            }

        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        if not method or not isinstance(method, str):
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32600, "message": "Invalid Request: missing or invalid 'method'"},
            }

        # 1. MCP initialize handshake
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {
                            "listChanged": False,
                        },
                    },
                    "serverInfo": {
                        "name": "jury-mcp",
                        "version": "3.0.0",
                    },
                },
            }

        # 2. MCP initialized notification
        if method == "notifications/initialized":
            if msg_id is not None:
                return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            return None

        # 3. Ping
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        # 4. tools/list
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": get_tool_definitions(),
                },
            }

        # 5. tools/call
        if method == "tools/call":
            if not isinstance(params, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": "Invalid params: expected object"},
                }

            tool_name = params.get("name")
            tool_args = params.get("arguments")
            if tool_args is None:
                tool_args = {}
            elif not isinstance(tool_args, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": "Invalid params: 'arguments' must be an object"},
                }

            try:
                if tool_name == "jury_plan":
                    res = await execute_jury_plan(tool_args, self.mesh, self.state_engine, self.repo_root)
                elif tool_name == "jury_review":
                    res = await execute_jury_review(tool_args, self.mesh, self.state_engine, self.repo_root)
                elif tool_name == "jury_ask":
                    res = await execute_jury_ask(tool_args, self.mesh, self.state_engine, self.repo_root)
                else:
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"Method not found: unknown tool '{tool_name}'"},
                    }

                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": res,
                }
            except InvalidParamsError as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": str(exc)},
                }
            except Exception as exc:
                logger.exception("Error executing tool %s: %s", tool_name, exc)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32603, "message": f"Internal error: {str(exc)}"},
                }

        # 6. Unknown method
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: '{method}'"},
        }

    async def handle_line(self, line: str) -> Optional[Union[Dict[str, Any], List[Dict[str, Any]]]]:
        """
        Parse raw line as JSON and dispatch.
        """
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {str(exc)}"},
            }

        # Batch request support
        if isinstance(parsed, list):
            if not parsed:
                return {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid Request: empty batch"},
                }
            responses: List[Dict[str, Any]] = []
            for req in parsed:
                resp = await self.handle_request(req)
                if resp is not None:
                    responses.append(resp)
            return responses if responses else None

        # Single request
        return await self.handle_request(parsed)

    def run(self, input_stream: Optional[TextIO] = None) -> None:
        """
        Process incoming JSON-RPC lines until EOF.
        """
        stream = input_stream or sys.stdin
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            for raw_line in stream:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    response = loop.run_until_complete(self.handle_line(line))
                    if response is not None:
                        self.send_frame(response)
                except Exception as exc:
                    logger.exception("Unexpected error handling line: %s", exc)
                    self.send_frame({
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32603, "message": f"Internal error: {str(exc)}"},
                    })
        finally:
            loop.close()


def main() -> None:
    """
    Console script entry point for jury-mcp.
    """
    server = McpServer()
    server.run()


if __name__ == "__main__":
    main()
