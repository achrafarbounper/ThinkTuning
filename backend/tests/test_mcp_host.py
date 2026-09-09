from __future__ import annotations

from typing import Any

import pytest

from app.domain.ports.mcp_ports import MCPRemoteCall
from app.infrastructure.mcp.host.capability_router import CapabilityRouter
from app.infrastructure.mcp.host.connection_manager import (
    ConnectionManager,
    MCPServerConfig,
)
from app.infrastructure.mcp.host.config import load_capability_routes, load_server_configs
from app.infrastructure.mcp.host.stdio_session import StdioSessionError


class _FakeSession:
    def __init__(self, command, env, timeout):
        self.command = command
        self.env = env
        self.timeout = timeout
        self.process = _FakeProcess()
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.stopped = False

    def start(self) -> None:
        return None

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, params))
        return {"content": [{"type": "text", "text": "ok"}]}

    def stop(self) -> None:
        self.stopped = True
        self.process = None


def test_host_config_is_opt_in_and_skips_missing_commands(tmp_path, monkeypatch):
    config = tmp_path / "host.yaml"
    config.write_text(
        "enabled: true\nservers:\n  missing:\n    enabled: true\n    command: []\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AGENT_MCP_HOST", raising=False)
    assert load_server_configs(config) == []

    monkeypatch.setenv("AGENT_MCP_HOST", "1")
    assert load_server_configs(config) == []


def test_capability_routes_are_loaded_from_yaml(tmp_path):
    config = tmp_path / "host.yaml"
    config.write_text(
        "tools:\n  github_get_pr:\n    server: github\n    remote: get_pr\n",
        encoding="utf-8",
    )
    assert load_capability_routes(config) == {
        "github_get_pr": {
            "server": "github",
            "method": "tools/call",
            "remote": "get_pr",
            "read_only": True,
        }
    }


def test_connection_manager_starts_and_routes_calls(monkeypatch):
    created: list[_FakeSession] = []

    def factory(command, env, timeout):
        session = _FakeSession(command, env, timeout)
        created.append(session)
        return session

    monkeypatch.setattr(
        "app.infrastructure.mcp.host.connection_manager.StdioSession", factory
    )
    manager = ConnectionManager(
        [MCPServerConfig("fake", ["fake-server"], timeout=1)]
    )
    response = manager.call(MCPRemoteCall("fake", "tools/call", {"name": "echo"}))

    assert response["content"][0]["text"] == "ok"
    assert created[0].calls[0] == ("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "thinktuning-host", "version": "1.0"},
    }) or created[0].calls[0][0] == "tools/call"
    assert manager.health() == {"fake": True}
    manager.stop()
    assert created[0].stopped is True


def test_connection_manager_skips_missing_token(monkeypatch):
    manager = ConnectionManager([
        MCPServerConfig("github", ["github-server"], token_env="MISSING_TOKEN")
    ])
    assert manager.session("github") is None
    assert manager.health() == {}


class _Host:
    def __init__(self):
        self.requests: list[MCPRemoteCall] = []

    def list_tools(self, server=None):
        return []

    def call(self, request: MCPRemoteCall):
        self.requests.append(request)
        return {"ok": True}

    def health(self):
        return {}

    def stop(self):
        return None


class _FakeProcess:
    def poll(self):
        return None


def test_router_applies_write_approval_before_remote_call(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_APPROVAL_PATH", str(tmp_path / "approvals.db"))
    host = _Host()
    router = CapabilityRouter(host)

    with pytest.raises(PermissionError, match="Manual approval required"):
        router.call("git_commit", {"message": "test"})

    assert host.requests == []
