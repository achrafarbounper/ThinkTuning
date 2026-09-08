# project/app/infrastructure/mcp/__init__.py
"""Adapters MCP (S1 — Bootstrap, cf. docs/mcp/IMPLEMENTATION_PLAN.md).

- ``load_mcp_version`` : lecture de ``[tool.mcp] version`` dans
  ``pyproject.toml`` avec fallback tolérant sur ``DEFAULT_MCP_VERSION``
  (tâche 1) ; sera réutilisé par la couche serveur MCP (tâche 2).
"""

from __future__ import annotations

from app.domain.entities.mcp import DEFAULT_MCP_VERSION, MCPVersion
from app.infrastructure.mcp.version_loader import load_mcp_version

__all__ = [
    "DEFAULT_MCP_VERSION",
    "MCPVersion",
    "load_mcp_version",
]
