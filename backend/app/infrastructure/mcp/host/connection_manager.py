"""Configuration, lifecycle and bounded restart policy for MCP servers."""

from __future__ import annotations

import atexit
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from app.domain.ports.mcp_ports import MCPHostTool, MCPRemoteCall

from .stdio_session import StdioSession, StdioSessionError

logger = logging.getLogger("thinktuning.mcp.host")


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    token_env: str | None = None
    timeout: float = 10.0
    max_restarts: int = 2
    enabled: bool = True


class ConnectionManager:
    def __init__(self, configs: list[MCPServerConfig]) -> None:
        self.configs = {c.name: c for c in configs if c.enabled}
        self.sessions: dict[str, StdioSession] = {}
        self.restarts: dict[str, int] = {}
        atexit.register(self.stop)

    def _configured(self, config: MCPServerConfig) -> bool:
        if not config.command or not config.command[0]:
            logger.warning("Skipping MCP server %s: missing command", config.name)
            return False
        if config.token_env and not os.getenv(config.token_env):
            logger.warning(
                "Skipping MCP server %s: missing token %s", config.name, config.token_env
            )
            return False
        return True

    def session(self, name: str) -> StdioSession | None:
        config = self.configs.get(name)
        if config is None or not self._configured(config):
            return None
        current = self.sessions.get(name)
        if current and current.process and current.process.poll() is None:
            return current
        if self.restarts.get(name, 0) > config.max_restarts:
            logger.error("MCP server %s exceeded restart limit", name)
            return None
        env = os.environ.copy()
        env.update(config.env)
        if config.token_env:
            env[config.token_env] = os.environ[config.token_env]
        session = StdioSession(config.command, env, config.timeout)
        try:
            session.start()
        except StdioSessionError:
            self.restarts[name] = self.restarts.get(name, 0) + 1
            session.stop()
            logger.exception("MCP server %s failed to start", name)
            return None
        self.sessions[name] = session
        return session

    def call(
        self,
        request: MCPRemoteCall | str,
        method: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(request, MCPRemoteCall):
            name, method, params = request.server, request.method, request.params
        else:
            name = request
        if method is None:
            raise ValueError("MCP method is required")
        session = self.session(name)
        if session is None:
            raise StdioSessionError(f"MCP backend unavailable: {name}")
        try:
            return session.request(method, params)
        except StdioSessionError:
            session.stop()
            self.sessions.pop(name, None)
            self.restarts[name] = self.restarts.get(name, 0) + 1
            raise

    def health(self) -> dict[str, Any]:
        return {
            name: bool(session.process and session.process.poll() is None)
            for name, session in self.sessions.items()
        }

    def list_tools(self, server: str | None = None) -> list[MCPHostTool]:
        names = [server] if server else list(self.configs)
        tools: list[MCPHostTool] = []
        for name in names:
            if self.session(name) is None:
                continue
            payload = self.call(name, "tools/list")
            for tool in payload.get("tools", []):
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    tools.append(
                        MCPHostTool(
                            name=tool["name"],
                            description=str(tool.get("description", "")),
                            input_schema=tool.get("inputSchema", {"type": "object"}),
                            read_only=bool(tool.get("annotations", {}).get("readOnlyHint", True)),
                        )
                    )
        return tools

    def stop(self) -> None:
        for session in list(self.sessions.values()):
            session.stop()
        self.sessions.clear()
