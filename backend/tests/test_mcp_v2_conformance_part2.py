# project/tests/test_mcp_v2_conformance_part2.py
"""Tests de conformite v2.0.0 — surface MCP (tache 18, S6). Partie 2."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.entities.mcp import MCPScopeRole, MCPVersion
from app.infrastructure.mcp.mcp_server import InMemoryToolProvider, MCPTool
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.mcp_server_sse import router as mcp_sse_router
from app.infrastructure.mcp.protocol import ErrorCode, empty_input_schema

API_KEY = "test-mcp-v2-key"


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch):
    """Clé transport MCP (P5) posée à chaque test (lecture à l'appel)."""
    monkeypatch.setenv("API_KEY", API_KEY)
    yield


@pytest.mark.parametrize("params,expected", [
    ({}, "messages"),
    ({"messages": []}, "non-empty"),
    ({"messages": "x"}, "non-empty"),
    ({"messages": [{"role": "u", "content": "x"}], "maxTokens": -1}, "positive"),
    ({"messages": [{"role": "u", "content": "x"}], "temperature": "hot"}, "number"),
    ({"messages": [{"role": "u", "content": "x"}], "systemPrompt": 123}, "string"),
])
def test_sampling_create_validates_params(params: dict, expected: str) -> None:
    from app.domain.entities.mcp import SamplingResponse

    class _Port:
        def create_message(self, request):
            return SamplingResponse(text="ok", model="m")

    server = build_mcp_server(
        version=MCPVersion(major=2, minor=0, patch=0), sampling_port=_Port(),  # type: ignore[arg-type]
        tool_provider=InMemoryToolProvider([
            MCPTool(
                name="t", description="d", input_schema=empty_input_schema(),
                annotations={
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                },
                required_scope=MCPScopeRole.READ_ONLY, handler=lambda _: "ok",
            )
        ]),
    )
    reply = json.loads(server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 13, "method": "sampling/create", "params": params,
    })))
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert expected.lower() in reply["error"]["message"].lower()


def test_sampling_create_handles_llm_error() -> None:
    from app.domain.errors import LLMClientError

    class _FailPort:
        def create_message(self, request):
            raise LLMClientError("LLM indisponible")

    server = build_mcp_server(
        version=MCPVersion(major=2, minor=0, patch=0), sampling_port=_FailPort(),  # type: ignore[arg-type]
        tool_provider=InMemoryToolProvider([
            MCPTool(
                name="t", description="d", input_schema=empty_input_schema(),
                annotations={
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                },
                required_scope=MCPScopeRole.READ_ONLY, handler=lambda _: "ok",
            )
        ]),
    )
    reply = json.loads(server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 14, "method": "sampling/create",
        "params": {"messages": [{"role": "user", "content": "hi"}]},
    })))
    assert reply["error"]["code"] == ErrorCode.INTERNAL_ERROR
    assert "LLM" in reply["error"]["message"]


def test_orchestrate_tool_visible_v2() -> None:
    server = build_mcp_server(
        scope=MCPScopeRole.CONTRIBUTOR,
        version=MCPVersion(major=2, minor=0, patch=0),
    )
    reply = json.loads(server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 15, "method": "tools/list", "params": {},
    })))
    names = {t["name"] for t in reply["result"]["tools"]}
    assert "orchestrate" in names


def test_orchestrate_not_visible_below_v2() -> None:
    server = build_mcp_server(version=MCPVersion(major=1, minor=0, patch=0))
    reply = json.loads(server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 16, "method": "tools/list", "params": {},
    })))
    names = {t["name"] for t in reply["result"]["tools"]}
    assert "orchestrate" not in names


def test_server_info_reports_v2() -> None:
    server = build_mcp_server(version=MCPVersion(major=2, minor=0, patch=0))
    reply = json.loads(server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 17, "method": "initialize", "params": {},
    })))
    assert reply["result"]["serverInfo"]["version"] == "2.0.0"


def test_tools_list_still_works() -> None:
    server = build_mcp_server(version=MCPVersion(major=2, minor=0, patch=0))
    reply = json.loads(server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 18, "method": "tools/list", "params": {},
    })))
    assert len(reply["result"]["tools"]) > 0


def test_ping_still_works() -> None:
    server = build_mcp_server(version=MCPVersion(major=2, minor=0, patch=0))
    reply = json.loads(server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 19, "method": "ping", "params": {},
    })))
    assert reply["result"] == {}


def test_transport_sse_announces_sampling() -> None:
    test_app = FastAPI()
    test_app.include_router(mcp_sse_router)
    client = TestClient(test_app)
    response = client.post(
        "/mcp/sse",
        content=json.dumps({
            "jsonrpc": "2.0", "id": 20, "method": "initialize", "params": {},
        }),
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY},
    )
    assert response.status_code == 200
    body = response.text
    payload = json.loads(body.split("data:")[1].strip())
    assert "sampling" in payload["result"]["capabilities"]
