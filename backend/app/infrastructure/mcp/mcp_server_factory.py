# project/app/infrastructure/mcp/mcp_server_factory.py
"""Fabrique du serveur MCP — construction avec scope de sécurité (S1, tâche 2).

Le bootstrap S1 livre un registre de tools EN MÉMOIRE (``InMemoryToolProvider``)
avec deux tools de démonstration/contrat :

    - ``mcp_version``  : version de la surface MCP (read-only) ;
    - ``server_info``  : identité du serveur MCP (read-only).

Ces tools valident ListTools/CallTool de bout en bout (transport compris) et
seront REMPLACÉS par la projection du registre legacy via ``MCPToolRegistryPort``
(tâche 3) puis les 12 tools read-only de la S2 (tâche 6). Le scope (rôle) est
fixé à la construction : ``build_mcp_server(scope=...)`` filtre la visibilité
des tools (fail-closed via ``MCPScopeRole.granted``).

La version MCP est résolue par ``load_mcp_version`` (tâche 1) — le serveur
démarre même si ``pyproject.toml`` est absent (fallback ``DEFAULT_MCP_VERSION``).
"""

from __future__ import annotations

import logging

from app.domain.entities.mcp import MCPScopeRole, MCPTool, MCPVersion
from app.infrastructure.mcp.mcp_server import (
    InMemoryToolProvider,
    MCPServer,
    ToolProvider,
)
from app.infrastructure.mcp.protocol import MCP_SERVER_NAME, empty_input_schema
from app.infrastructure.mcp.version_loader import load_mcp_version

logger = logging.getLogger("thinktuning.mcp.factory")


def _bootstrap_tools(version: MCPVersion) -> list[MCPTool]:
    """Tools de contrat du bootstrap S1 (read-only, scope minimal)."""
    version_text = str(version)

    def _mcp_version(_: dict) -> str:
        return version_text

    def _server_info(_: dict) -> str:
        return f"{MCP_SERVER_NAME}@{version_text}"

    return [
        MCPTool(
            name="mcp_version",
            description=(
                "Version de la surface MCP ThinkTuning (feuille de route "
                "docs/mcp/IMPLEMENTATION_PLAN.md)."
            ),
            input_schema=empty_input_schema(),
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
            required_scope=MCPScopeRole.READ_ONLY,
            handler=_mcp_version,
        ),
        MCPTool(
            name="server_info",
            description="Identité du serveur MCP (nom + version).",
            input_schema=empty_input_schema(),
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
            required_scope=MCPScopeRole.READ_ONLY,
            handler=_server_info,
        ),
    ]


def build_mcp_server(
    scope: MCPScopeRole = MCPScopeRole.READ_ONLY,
    *,
    version: MCPVersion | None = None,
    tool_provider: ToolProvider | None = None,
) -> MCPServer:
    """Construit un ``MCPServer`` prêt à l'emploi pour un transport.

    Args:
        scope:          rôle du client (docs/mcp/MCP_SECURITY.md) — filtre la
            visibilité des tools exposés par ListTools/CallTool ;
        version:        version de la surface MCP ; ``None`` → résolue via
            ``load_mcp_version`` (pyproject.toml ``[tool.mcp] version``) ;
        tool_provider:  source de vérité des tools ; ``None`` → registre
            bootstrap S1 (``mcp_version``, ``server_info``).

    Returns:
        Un ``MCPServer`` configuré (dispatch JSON-RPC, prêt pour SSE/stdio).
    """
    resolved_version = version or load_mcp_version()
    provider = tool_provider or InMemoryToolProvider(_bootstrap_tools(resolved_version))
    server = MCPServer(
        name=MCP_SERVER_NAME,
        version=resolved_version,
        scope=scope,
        tool_provider=provider,
    )
    logger.info(
        "Serveur MCP construit : %s@%s (scope=%s, tools=%d)",
        server.name,
        resolved_version,
        scope.value,
        len(provider.list_tools()),
    )
    return server


__all__ = ["build_mcp_server"]
