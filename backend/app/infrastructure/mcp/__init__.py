# project/app/infrastructure/mcp/__init__.py
"""Adapters MCP (S1 — Bootstrap + S2 — Tools, cf. docs/mcp/IMPLEMENTATION_PLAN.md).

- ``load_mcp_version`` : lecture de ``[tool.mcp] version`` dans
  ``pyproject.toml`` avec fallback tolérant sur ``DEFAULT_MCP_VERSION``
  (tâche 1) ;
- ``build_mcp_server`` : fabrique du serveur MCP (scope de sécurité, registre
  de tools bootstrap) et transports :
  - ``mcp_server_sse.py``   → ``POST /mcp/sse`` (flux SSE, tâche 2) ;
  - ``mcp_server_stdio.py`` → entry point ``thinktuning-mcp`` (tâche 2) ;
- ``manifest_generator`` (tâche 4) : compile ``ia/tools/tools_config.json``
  (standard ``thinktuning.tool/v1``) → manifeste MCP (``inputSchema`` réutilisant
  ``to_json_schema``, ``safety`` → annotations) + catalogue ``docs/mcp/MANIFEST.md`` ;
- ``policy_adapter`` (tâche 5) : projection RUNTIME de la policy de sandbox
  (``app/agent/policies/sandbox_policy.py``) vers MCP — verdicts typés
  ``PolicyVerdict`` (délégation stricte, zéro règle dupliquée), annotations
  ``readOnlyHint``/``destructiveHint``/``idempotentHint``, filtre de scope
  (``visible_tools``) et providers sécurisés (``ScopeFilteredToolProvider``,
  ``PolicyGateToolProvider`` : MCP n'est pas un bypass de la security interne).
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
from app.infrastructure.mcp.policy_adapter import (
    PolicyGateToolProvider,
    PolicyVerdict,
    ScopeFilteredToolProvider,
    decide,
    decide_action,
    decision_to_annotations,
    visible_tools,
)
from app.infrastructure.mcp.protocol import (
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    ErrorCode,
    MCPMethod,
    ProtocolError,
)
from app.infrastructure.mcp.version_loader import load_mcp_version

# NOTE : ``manifest_generator`` n'est PAS ré-exporté ici (comme ``mcp_server_stdio``)
# : ``python -m app.infrastructure.mcp.manifest_generator`` importerait le module
# DEUX FOIS (via ce paquet, puis comme __main__) → RuntimeWarning + instance double.

__all__ = [
    "DEFAULT_MCP_VERSION",
    "ErrorCode",
    "InMemoryToolProvider",
    "MCPMethod",
    "MCP_PROTOCOL_VERSION",
    "MCPTool",
    "MCPVersion",
    "MCPServer",
    "MCP_SERVER_NAME",
    "PolicyGateToolProvider",
    "PolicyVerdict",
    "ProtocolError",
    "ScopeFilteredToolProvider",
    "ToolError",
    "ToolProvider",
    "build_mcp_server",
    "decide",
    "decide_action",
    "decision_to_annotations",
    "load_mcp_version",
    "mcp_sse_router",
    "visible_tools",
]
