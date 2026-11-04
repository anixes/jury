"""
tests/unit/test_mcp_server.py - Unit Test Suite for MCP Stdio Server & Tool Handlers.

Tests:
1. Protocol lifecycle: initialize, notifications/initialized, ping
2. Tool schema definitions: tools/list validation
3. Tool execution: jury_plan (sync & async), jury_review (with & without diff), jury_ask
4. Error handling: -32700 (parse error), -32601 (unknown method/tool), -32602 (invalid params)
5. Channel hygiene: stdout reserved exclusively for valid JSON-RPC frames, stderr routing
6. Batch request processing
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List
import pytest

from jury.mcp_server.server import McpServer
from jury.mcp_server.tools import (
    InvalidParamsError,
    execute_jury_ask,
    execute_jury_plan,
    execute_jury_review,
    get_tool_definitions,
)
from jury.deliberation.mesh import DeliberationMesh
from jury.state.engine import JuryStateEngine


@pytest.fixture(autouse=True)
def isolated_mcp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fixture ensuring all MCP server unit tests run offline in isolated state dir."""
    state_dir = tmp_path / "jury_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JURY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("JURY_OFFLINE_TEST", "1")
    return state_dir


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Fixture providing a temporary git repository."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "mcp-test@jury.dev"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Jury MCP Tester"], cwd=str(repo), check=True, capture_output=True)
    readme = repo / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)
    return repo


@pytest.fixture
def mcp_server(temp_git_repo: Path) -> McpServer:
    """Fixture providing an McpServer instance bound to temp_git_repo."""
    mesh = DeliberationMesh(mock_mode=True)
    state_engine = JuryStateEngine()
    server = McpServer(mesh=mesh, state_engine=state_engine, repo_root=temp_git_repo)
    return server


# =============================================================================
# 1. Tool Schemas (tools/list)
# =============================================================================

class TestToolSchemas:
    """Verifies MCP tool schemas and definitions."""

    def test_get_tool_definitions_count_and_names(self):
        tools = get_tool_definitions()
        assert len(tools) == 3
        names = {t["name"] for t in tools}
        assert names == {"jury_plan", "jury_review", "jury_ask"}

    def test_jury_plan_schema_requirements(self):
        tools = {t["name"]: t for t in get_tool_definitions()}
        plan_tool = tools["jury_plan"]
        assert "task" in plan_tool["inputSchema"]["properties"]
        assert "required" in plan_tool["inputSchema"]
        assert "task" in plan_tool["inputSchema"]["required"]
        assert "async_mode" in plan_tool["inputSchema"]["properties"]
        assert "context_files" in plan_tool["inputSchema"]["properties"]

    def test_jury_review_schema_properties(self):
        tools = {t["name"]: t for t in get_tool_definitions()}
        review_tool = tools["jury_review"]
        props = review_tool["inputSchema"]["properties"]
        assert "diff" in props
        assert "staged" in props
        assert "branch" in props
        assert "focus" in props
        assert "strict" in props

    def test_jury_ask_schema_properties(self):
        tools = {t["name"]: t for t in get_tool_definitions()}
        ask_tool = tools["jury_ask"]
        props = ask_tool["inputSchema"]["properties"]
        assert "question" in props
        assert "query" in props
        assert "context" in props
        assert "quick" in props
        assert "focus" in props


# =============================================================================
# 2. JSON-RPC Protocol Lifecycle & Methods
# =============================================================================

class TestProtocolLifecycle:
    """Verifies initialize, ping, notifications, and method routing."""

    def test_initialize_handshake(self, mcp_server: McpServer):
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert resp["id"] == 1
        assert "result" in resp
        assert resp["result"]["protocolVersion"] == "2024-11-05"
        assert resp["result"]["serverInfo"]["name"] == "jury-mcp"
        assert "tools" in resp["result"]["capabilities"]

    def test_notifications_initialized(self, mcp_server: McpServer):
        req = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        # Notifications without id must not produce a response
        assert resp is None

    def test_ping(self, mcp_server: McpServer):
        req = {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "ping",
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert resp["id"] == 99
        assert resp["result"] == {}

    def test_tools_list_method(self, mcp_server: McpServer):
        req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert resp["id"] == 2
        assert "tools" in resp["result"]
        assert len(resp["result"]["tools"]) == 3

    def test_unknown_method_returns_32601(self, mcp_server: McpServer):
        req = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "non_existent_method",
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert resp["id"] == 5
        assert "error" in resp
        assert resp["error"]["code"] == -32601


# =============================================================================
# 3. Tool Calls (tools/call)
# =============================================================================

class TestToolCalls:
    """Verifies calling jury_plan, jury_review, and jury_ask."""

    def test_jury_plan_sync_success(self, mcp_server: McpServer):
        req = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "jury_plan",
                "arguments": {"task": "Build rate limited SQLite state cache"},
            },
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert resp["id"] == 10
        assert "result" in resp
        result = resp["result"]
        assert "verdict" in result
        assert "readiness_score" in result
        assert "content" in result
        assert len(result["content"]) > 0

    def test_jury_plan_async_ticket_returns_immediately(self, mcp_server: McpServer):
        req = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "jury_plan",
                "arguments": {
                    "task": "Long running distributed transaction orchestrator",
                    "async_mode": True,
                },
            },
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert "result" in resp
        result = resp["result"]
        assert "ticket_id" in result
        assert result["status"] == "PENDING"
        assert "content" in result

    def test_jury_plan_missing_task_returns_32602(self, mcp_server: McpServer):
        req = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "jury_plan",
                "arguments": {},
            },
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert resp["id"] == 12
        assert "error" in resp
        assert resp["error"]["code"] == -32602
        assert "task" in resp["error"]["message"].lower()

    def test_jury_plan_with_context_file(self, mcp_server: McpServer, temp_git_repo: Path):
        doc = temp_git_repo / "arch.md"
        doc.write_text("# Design Document\nTwo-tier locking specification.\n", encoding="utf-8")
        req = {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "jury_plan",
                "arguments": {
                    "task": "Review design doc",
                    "context_files": ["arch.md"],
                },
            },
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert "result" in resp

    def test_jury_review_explicit_diff(self, mcp_server: McpServer):
        sample_diff = (
            "diff --git a/test.py b/test.py\n"
            "--- a/test.py\n"
            "+++ b/test.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-x = 1\n"
            "+x = 2\n"
        )
        req = {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "jury_review",
                "arguments": {"diff": sample_diff, "focus": "security"},
            },
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert "result" in resp
        result = resp["result"]
        assert "verdict" in result
        assert "score" in result
        assert "findings" in result
        assert "unified_patch" in result
        assert "sarif_json" in result

    def test_jury_review_git_auto_diff(self, mcp_server: McpServer, temp_git_repo: Path):
        # Modify a file in temp git repo to create unstaged diff
        f = temp_git_repo / "README.md"
        f.write_text("# Modified Header\nNew line added.\n", encoding="utf-8")
        req = {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {
                "name": "jury_review",
                "arguments": {"staged": False},
            },
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert "result" in resp
        assert "verdict" in resp["result"]

    def test_jury_ask_with_question(self, mcp_server: McpServer):
        req = {
            "jsonrpc": "2.0",
            "id": 30,
            "method": "tools/call",
            "params": {
                "name": "jury_ask",
                "arguments": {"question": "Should we use SQLite WAL or RocksDB?"},
            },
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert "result" in resp
        result = resp["result"]
        assert "recommended_pattern" in result
        assert "confidence" in result
        assert "tradeoff_matrix" in result

    def test_jury_ask_with_query_alias(self, mcp_server: McpServer):
        req = {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {
                "name": "jury_ask",
                "arguments": {"query": "How to prevent double-submit with singleflight?"},
            },
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert "result" in resp

    def test_jury_ask_missing_question_returns_32602(self, mcp_server: McpServer):
        req = {
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {
                "name": "jury_ask",
                "arguments": {},
            },
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    def test_unknown_tool_returns_32601(self, mcp_server: McpServer):
        req = {
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {
                "name": "jury_unknown_tool",
                "arguments": {},
            },
        }
        resp = asyncio.run(mcp_server.handle_request(req))
        assert resp is not None
        assert "error" in resp
        assert resp["error"]["code"] == -32601


# =============================================================================
# 4. Error Handling & Malformed Payloads
# =============================================================================

class TestErrorHandlingAndHygiene:
    """Verifies error handling and channel hygiene."""

    def test_malformed_json_line_returns_32700(self, mcp_server: McpServer):
        res = asyncio.run(mcp_server.handle_line("INVALID_NOT_JSON{"))
        assert res is not None
        assert "error" in res
        assert res["error"]["code"] == -32700

    def test_batch_request_handling(self, mcp_server: McpServer):
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ]
        line = json.dumps(batch)
        res = asyncio.run(mcp_server.handle_line(line))
        assert isinstance(res, list)
        assert len(res) == 2
        assert res[0]["id"] == 1
        assert res[1]["id"] == 2

    def test_run_stdio_stream_channel_hygiene(self, mcp_server: McpServer):
        """Validates that running against an input stream produces strictly valid JSON-RPC frames on stdout."""
        input_data = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}) + "\n"
        )
        fake_stdin = io.StringIO(input_data)
        fake_stdout = io.StringIO()

        # Direct frames to fake_stdout
        mcp_server._real_stdout = fake_stdout
        mcp_server.run(input_stream=fake_stdin)

        output_str = fake_stdout.getvalue()
        lines = [line.strip() for line in output_str.splitlines() if line.strip()]
        assert len(lines) == 3
        for line in lines:
            parsed = json.loads(line)
            assert "jsonrpc" in parsed
            assert parsed["jsonrpc"] == "2.0"
            assert "id" in parsed
            assert "result" in parsed
