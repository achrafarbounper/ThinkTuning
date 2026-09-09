"""Stable local names for optional remote MCP capabilities."""

from __future__ import annotations

import logging
from typing import Any

from app.domain.entities.plan import ActionCategory
from app.domain.ports.mcp_ports import MCPHostPort, MCPHostTool, MCPRemoteCall
from app.infrastructure.mcp.mcp_audit import audit_mcp_call
from app.infrastructure.mcp.policy_adapter import decide
from core.audit_store import ACT_MCP_CLIENT_TOOL_CALL

from .config import load_capability_routes

logger = logging.getLogger("thinktuning.mcp.host.router")

class CapabilityRouter:
    def __init__(self, host: MCPHostPort, routes: dict[str, dict[str, Any]] | None = None) -> None:
        self.host = host
        self.routes = routes if routes is not None else load_capability_routes()

    def list_tools(self, server: str | None = None) -> list[MCPHostTool]:
        return [MCPHostTool(name=name, description=f"Optional MCP capability: {name}",
                            input_schema={"type": "object"}, read_only=name != "git_commit")
                for name, spec in self.routes.items() if server is None or spec["server"] == server]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        spec = self.routes.get(name)
        if spec is None:
            raise ValueError(f"unknown MCP host tool: {name}")
        arguments = dict(arguments or {})
        verdict = decide(name, arguments, ActionCategory.WRITE if name == "git_commit"
                         else ActionCategory.READ)
        detail = {"tool": name, "arguments": arguments, "decision": verdict.to_dict()}
        if verdict.blocked:
            audit_mcp_call(
                ACT_MCP_CLIENT_TOOL_CALL,
                subject=name,
                detail={**detail, "is_error": True},
            )
            raise PermissionError(verdict.reason)
        if verdict.requires_approval:
            from core.approval_store import get_approval_store

            request_id = get_approval_store().create(
                name, arguments, "write", "approve", verdict.reason,
                prompt=f"Approve outbound MCP call {name}",
            )
            audit_mcp_call(
                ACT_MCP_CLIENT_TOOL_CALL,
                subject=name,
                detail={**detail, "is_error": True, "approval_request_id": request_id},
            )
            raise PermissionError(f"Manual approval required: {name} ({request_id})")
        server, method, remote_name = spec["server"], spec["method"], spec["remote"]
        try:
            result = self.host.call(MCPRemoteCall(server, method, {
                "name": remote_name, "arguments": arguments,
            }))
        except Exception:
            audit_mcp_call(
                ACT_MCP_CLIENT_TOOL_CALL,
                subject=name,
                detail={**detail, "server": server, "remote_tool": remote_name, "is_error": True},
            )
            raise
        audit_mcp_call(
            ACT_MCP_CLIENT_TOOL_CALL,
            subject=name,
            detail={**detail, "server": server, "remote_tool": remote_name, "is_error": False},
        )
        return result
