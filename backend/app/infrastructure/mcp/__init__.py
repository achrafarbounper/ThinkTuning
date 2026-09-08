# project/app/infrastructure/mcp/__init__.py
"""Adapters MCP (S1 — Bootstrap, cf. docs/mcp/IMPLEMENTATION_PLAN.md).

- ``load_mcp_version`` : lecture de ``[tool.mcp] version`` dans
  ``pyproject.toml`` avec fallback tolérant sur ``DEFAULT_MCP_VERSION``
  (tâche 1) ;
- ``build_mcp_server`` : fabrique du serveur MCP (scope de sécurité, registre
  de tools bootstrap) et transports :
  - ``mcp_server_sse.py``   → ``POST /mcp/sse`` (flux SSE, tâche 2) ;
  - ``mcp_server_stdio.py`` → entry point ``thinktuning-mcp`` (tâche 2).
"""

from __future__ import annotations

from app.domain.entities.mcp import DEFAULT_MCP_VERSION, MCPVersion
from app.infrastructure.mcp.mcp_server import (
    InMemoryToolProvider,
    MCPServer,
    MCPTool,
    ToolError,
    ToolProvider,
)
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.mcp_server_sse import router as mcp_sse_router
from app.infrastructure.mcp.protocol import (
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    ErrorCode,
    MCPMethod,
    ProtocolError,
)
from app.infrastructure.mcp.version_loader import load_mcp_version

__all__ = [
    "DEFAULT_MCP_VERSION",
    "ErrorCode",
    "InMemoryToolProvider",
    "MCPServer",
    "MCPMethod",
    "MCP_PROTOCOL_VERSION",
    "MCPTool",
    "MCPVersion",
    "MCP_SERVER_NAME",
    "ProtocolError",
    "ToolError",
    "ToolProvider",
    "build_mcp_server",
    "load_mcp_version",
    "mcp_sse_router",
]
