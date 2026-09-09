# project/tests/test_mcp_v2_conformance.py
"""Tests de conformite v2.0.0 — surface MCP (tache 18, S6). Partie 1."""
from __future__ import annotations

import json

import pytest

from app.domain.entities.mcp import MCPScopeRole, MCPVersion, SamplingRequest, SamplingResponse
from app.domain.errors import LLMClientError
from app.infrastructure.mcp.mcp_server import (
    InMemoryToolProvider,
    MCPServer,
    MCPTool,
)
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.protocol import ErrorCode, empty_input_schema


class _FakeSamplingPort:
    def __init__(self, response: str = "ok", *, fail: bool = False) -> None:
        self._response = response
        self._fail = fail
        self.calls: list[SamplingRequest] = []

    def create_message(self, request: SamplingRequest) -> SamplingResponse:
        self.calls.append(request)
        if self._fail:
            raise LLMClientError("LLM indisponible")
        return SamplingResponse(text=self._response, model="test-model")

    def create_text(self, messages, **kwargs) -> str:
        return self._response


@pytest.fixture
def sampling_port() -> _FakeSamplingPort:
    return _FakeSamplingPort(response="reponse de test")


@pytest.fixture
def server_with_sampling(sampling_port: _FakeSamplingPort) -> MCPServer:
    return build_mcp_server(version=MCPVersion(major=2, minor=0, patch=0), sampling_port=sampling_port)


@pytest.fixture
def server_without_sampling() -> MCPServer:
    """Serveur MCP v2.0.0 SANS sampling (construction directe, sans factory)."""
    return MCPServer(
        name="test-server",
        version=MCPVersion(major=2, minor=0, patch=0),
        scope=MCPScopeRole.READ_ONLY,
        tool_provider=InMemoryToolProvider([
            MCPTool(
                name="only_tool", description="Tool seul",
                input_schema=empty_input_schema(),
                annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
                required_scope=MCPScopeRole.READ_ONLY, handler=lambda _: "ok",
            )
        ]),
        sampling_port=None,
    )


def test_initialize_announces_sampling_capability(server_with_sampling: MCPServer) -> None:
    reply = json.loads(server_with_sampling.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    })))
    assert "sampling" in reply["result"]["capabilities"]


def test_initialize_without_sampling_no_capability(server_without_sampling: MCPServer) -> None:
    reply = json.loads(server_without_sampling.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    })))
    assert "sampling" not in reply["result"]["capabilities"]


def test_sampling_create_returns_text(server_with_sampling: MCPServer) -> None:
    reply = json.loads(server_with_sampling.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 10, "method": "sampling/create",
        "params": {"messages": [{"role": "user", "content": "hello"}]},
    })))
    assert reply["result"]["content"]["text"] == "reponse de test"


def test_sampling_create_forwards_params(
    server_with_sampling: MCPServer, sampling_port: _FakeSamplingPort
) -> None:
    server_with_sampling.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 11, "method": "sampling/create",
        "params": {"messages": [{"role": "u", "content": "hi"}],
                 "maxTokens": 100, "temperature": 0.5, "systemPrompt": "Sys"},
    }))
    req = sampling_port.calls[0]
    assert req.max_tokens == 100
    assert req.system_prompt == "Sys"


def test_sampling_create_rejected_without_port(server_without_sampling: MCPServer) -> None:
    reply = json.loads(server_without_sampling.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 12, "method": "sampling/create",
        "params": {"messages": [{"role": "user", "content": "hi"}]},
    })))
    assert reply["error"]["code"] == ErrorCode.INTERNAL_ERROR
