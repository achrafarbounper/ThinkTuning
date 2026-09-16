"""Contrat MCP 2.3.0 : capacités réelles et compatibilité des clients 2.2.x."""

from __future__ import annotations

import io
import json
from itertools import product
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.entities.mcp import MCPScopeRole, MCPVersion
from app.domain.ports.mcp_ports import (
    MCPPromptRegistryPort,
    MCPResourceRegistryPort,
    SamplingPort,
)
from app.infrastructure.mcp import mcp_server_sse, mcp_server_stdio
from app.infrastructure.mcp.mcp_server import InMemoryToolProvider, MCPServer
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.protocol import MCP_PROTOCOL_VERSION, ErrorCode

CATALOG_NOTIFICATIONS = [
    "notifications/tools/list_changed",
    "notifications/resources/list_changed",
    "notifications/prompts/list_changed",
]


def _request(method: str, request_id: int = 1, **params: object) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})


def _capabilities(server: MCPServer, **params: object) -> dict:
    reply = server.handle_text(_request("initialize", **params))
    assert reply is not None
    return json.loads(reply)["result"]["capabilities"]


@pytest.mark.parametrize("resources,prompts,sampling", list(product([False, True], repeat=3)))
def test_initialize_reflects_wired_providers(resources: bool, prompts: bool, sampling: bool):
    resource = Mock(spec=MCPResourceRegistryPort) if resources else None
    prompt = Mock(spec=MCPPromptRegistryPort) if prompts else None
    sampler = Mock(spec=SamplingPort) if sampling else None
    server = MCPServer(
        version=MCPVersion.parse("2.3.0"),
        scope=MCPScopeRole.READ_ONLY,
        tool_provider=InMemoryToolProvider([]),
        resource_provider=resource,
        prompt_provider=prompt,
        sampling_port=sampler,
    )
    expected = {"tools": {"listChanged": False}}
    if resources:
        expected["resources"] = {"subscribe": False, "listChanged": False}
    if prompts:
        expected["prompts"] = {"listChanged": False}
    if sampling:
        expected["sampling"] = {}
    assert _capabilities(server) == expected
    # Aucun accès au catalogue, aux ressources ou au LLM pendant initialize.
    for provider in (resource, prompt, sampler):
        if provider is not None:
            assert provider.mock_calls == []


@pytest.mark.parametrize("client_version", ["2.2.0", "2.2.1", "2.2.99"])
def test_legacy_client_handshake_and_catalogs(client_version: str):
    server = build_mcp_server()
    expected = _capabilities(server)  # params vides toujours acceptés
    assert _capabilities(
        server,
        protocolVersion=MCP_PROTOCOL_VERSION,
        clientInfo={"name": "legacy-client", "version": client_version},
        capabilities={"experimental": {"unknown": True}},
    ) == expected
    assert server.handle_text(json.dumps({
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })) is None
    for catalog in ("tools", "resources", "prompts"):
        reply = server.handle_text(_request(f"{catalog}/list"))
        assert reply is not None
        assert isinstance(json.loads(reply)["result"][catalog], list)


@pytest.mark.parametrize("method,params", [
    ("resources/subscribe", {"uri": "thinktuning://health"}),
    ("resources/unsubscribe", {"uri": "thinktuning://health"}),
    ("logging/setLevel", {"level": "debug"}),
])
def test_unadvertised_methods_remain_unsupported(method: str, params: dict):
    server = build_mcp_server()
    assert "logging" not in _capabilities(server)
    reply = server.handle_text(_request(method, **params))
    assert reply is not None
    assert json.loads(reply)["error"]["code"] == ErrorCode.METHOD_NOT_FOUND


@pytest.mark.parametrize("method", CATALOG_NOTIFICATIONS)
def test_incoming_catalog_notifications_are_ignored(method: str):
    server = build_mcp_server()
    before = _capabilities(server)
    assert server.handle_text(json.dumps({"jsonrpc": "2.0", "method": method})) is None
    assert _capabilities(server) == before


def test_stdio_catalog_exchange_has_no_unsolicited_notifications(monkeypatch):
    server = build_mcp_server()
    monkeypatch.setattr(mcp_server_stdio, "build_mcp_server", lambda **kwargs: server)
    requests = [_request("initialize")]
    requests += [json.dumps({"jsonrpc": "2.0", "method": method})
                 for method in ["notifications/initialized", *CATALOG_NOTIFICATIONS]]
    requests += [_request(f"{catalog}/list", index)
                 for index, catalog in enumerate(("tools", "resources", "prompts"), 2)]
    output = io.StringIO()
    assert mcp_server_stdio.serve_stdio(
        input_stream=io.StringIO("\n".join(requests)), output_stream=output,
    ) == 0
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3, 4]
    assert all("method" not in reply and "result" in reply for reply in replies)
    assert replies[0]["result"]["capabilities"] == _capabilities(server)


def test_sse_catalog_exchange_has_no_unsolicited_notifications(monkeypatch):
    server = build_mcp_server()
    monkeypatch.setattr(mcp_server_sse, "_server", server)
    monkeypatch.setenv("API_KEY", "capabilities-test-key")
    monkeypatch.setenv("MCP_SERVER_ENABLED", "true")
    app = FastAPI()
    app.include_router(mcp_server_sse.router)
    with TestClient(app) as client:
        for index, method in enumerate(
            ("initialize", "tools/list", "resources/list", "prompts/list"), 1,
        ):
            response = client.post(
                "/mcp/sse", content=_request(method, index),
                headers={"X-API-Key": "capabilities-test-key"},
            )
            assert response.status_code == 200
            data = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
            assert data[-1] == "[DONE]"
            replies = [json.loads(item) for item in data[:-1]]
            assert len(replies) == 1
            assert replies[0]["id"] == index
            assert "method" not in replies[0]
            if method == "initialize":
                assert replies[0]["result"]["capabilities"] == _capabilities(server)
                assert replies[0]["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
            else:
                assert isinstance(replies[0]["result"][method.split("/")[0]], list)
