# project/app/infrastructure/mcp/mcp_server.py
"""Cœur serveur MCP — dispatch JSON-RPC 2.0 partagé par les transports SSE et stdio.

Séparation des responsabilités (hexagonale, docs/mcp/MCP_IMPLEMENTATION_MAPPING.md) :
    - ce module connaît la SÉMANTIQUE MCP (initialize, ping, tools/list,
      tools/call…) et le scope de sécurité — mais AUCUN transport ;
    - ``mcp_server_sse.py`` / ``mcp_server_stdio.py`` ne font que câbler ce
      cœur sur leur entrée/sortie ;
        - la source de vérité des tools est formalisée en port domaine
      ``MCPToolRegistryPort`` (app/domain/ports/mcp_ports.py, tâche 3) :
      ``ToolProvider`` est un alias rétrocompatible. La fabrique
      (``mcp_server_factory.py``) fournit le registre bootstrap de la S1.

S1 = dispatch SYNCHRONE : tous les handlers de tools du bootstrap sont purs.
L'asynchronicité des futurs tools (sampling / orchestrate, S6) s'ajoutera par
évolution des handlers, sans changer le contrat ``handle_text``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from app.domain.entities.mcp import MCPScopeRole, MCPTool, MCPVersion
from app.domain.ports.mcp_ports import MCPToolRegistryPort
from app.infrastructure.mcp.protocol import (
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    ErrorCode,
    MCPMethod,
    ProtocolError,
    error_result,
    parse_jsonrpc,
    success_result,
)

logger = logging.getLogger("thinktuning.mcp.server")


class ToolError(RuntimeError):
    """Erreur métier d'un tool → réponse MCP ``isError: true``.

    Distinguée des erreurs de PROTOCOLE : un tool peut échouer (API externe
    injoignable, argument refusé…) sans que le transport JSON-RPC soit fautif.
    """


# MCPTool et ToolProvider sont désormais dans le domaine (tâche 3) :
#   - MCPTool ← app/domain/entities/mcp.py (entité pure, déplacée de l'infra)
#   - MCPToolRegistryPort ← app/domain/ports/mcp_ports.py (Protocol)
# ToolProvider est conservé comme alias rétrocompatible pour les imports existants.
ToolProvider = MCPToolRegistryPort


class MCPServer:
    """Dispatch JSON-RPC 2.0 / MCP, stateless et thread-safe.

        Instance par client ou partagée : aucun état mutable entre requêtes (les
    handlers de tools passent par le ``MCPToolRegistryPort`` injecté). Le scope
    de sécurité est FIXÉ à la construction (fabrique, build with scope).

    Attributes:
        name:           nom du serveur (``serverInfo.name``) ;
        version:        version de la surface MCP (``serverInfo.version``) ;
        scope:          rôle du client (filtre la visibilité des tools) ;
        tool_provider:  source des tools (port ``MCPToolRegistryPort``, alias
            rétrocompatible ``ToolProvider``).
    """

    def __init__(
        self,
        *,
        name: str = MCP_SERVER_NAME,
        version: MCPVersion,
        scope: MCPScopeRole,
        tool_provider: MCPToolRegistryPort,
    ) -> None:
        self.name = name
        self.version = version
        self.scope = scope
        self.tool_provider = tool_provider

    # --- Surface publique --------------------------------------------------------

    def handle_text(self, raw: str) -> str | None:
        """Parse un message JSON-RPC (texte brut) et retourne la réponse encodée.

        Returns:
            La réponse JSON-RPC sérialisée à émettre, ou ``None`` pour une
            notification (MCP : aucune réponse attendue sur le transport).
        """
        try:
            payload = parse_jsonrpc(raw)
        except ProtocolError as exc:
            return self._encode(error_result(None, exc.code, exc.message))
        try:
            response = self._dispatch(payload)
        except ProtocolError as exc:
            response = error_result(None, exc.code, exc.message)
        return self._encode(response)

    # --- Dispatch -------------------------------------------------------------------

    def _dispatch(self, payload: dict[str, Any] | list[Any]) -> dict[str, Any] | None:
        """Dispatch d'un message déjà parsé → enveloppe JSON-RPC (ou None)."""
        if not isinstance(payload, dict):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "Invalid Request: batch requests are not supported",
            )
        method = payload.get("method")
        is_notification = "id" not in payload
        if not isinstance(method, str):
            if is_notification:
                return None
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "Invalid Request: 'method' must be a string"
            )
        if is_notification:
            self._handle_notification(method)
            return None
        params = payload.get("params", {})
        request_id = payload.get("id")
        if not isinstance(params, dict):
            params = {}
        return self._handle_method(method, request_id, params)

    def _handle_method(
        self, method: str, request_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Dispatch d'une méthode de REQUÊTE (id présent) → réponse JSON-RPC."""
        if method == MCPMethod.INITIALIZE:
            return success_result(request_id, self._initialize_result())
        if method == MCPMethod.PING:
            return success_result(request_id, {})
        if method == MCPMethod.TOOLS_LIST:
            return success_result(
                request_id, {"tools": [tool.to_dict() for tool in self._visible_tools()]}
            )
        if method == MCPMethod.TOOLS_CALL:
            return self._handle_tools_call(request_id, params)
        if method == MCPMethod.RESOURCES_LIST:
            # v0.1.0 (feuille de route) : aucune ressource exposée.
            return success_result(request_id, {"resources": []})
        if method == MCPMethod.PROMPTS_LIST:
            # v0.1.0 (feuille de route) : aucun prompt exposé.
            return success_result(request_id, {"prompts": []})
        raise ProtocolError(ErrorCode.METHOD_NOT_FOUND, f"Method not found: {method}")

    def _handle_notification(self, method: str) -> None:
        """Notifications JSON-RPC : AUCUN acquittement (faible coût, tracé log)."""
        if method == MCPMethod.NOTIFICATIONS_INITIALIZED:
            logger.info("MCP client initialized (scope=%s)", self.scope.value)
            return
        logger.info("Notification MCP ignorée : %s", method)

    # --- initialize -----------------------------------------------------------------

    def _initialize_result(self) -> dict[str, Any]:
        """Résultat de l'handshake : protocole, capabilities, serverInfo."""
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": str(self.version)},
        }

    # --- tools/list & tools/call ------------------------------------------------------

    def _visible_tools(self) -> list[MCPTool]:
        """Tools projetés pour le scope courant (fail-closed via ``granted``)."""
        return [
            tool
            for tool in self.tool_provider.list_tools()
            if self.scope.granted(tool.required_scope)
        ]

    def _handle_tools_call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Exécution d'un tool : validation paramètres → dispatch → isError."""
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'name' (str) is required",
            )
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'arguments' must be an object",
            )
        if name not in {tool.name for tool in self._visible_tools()}:
            # Indiscernable d'un tool absent : aucun oracle de visibilité (scope).
            return error_result(
                request_id, ErrorCode.INVALID_PARAMS, f"Unknown tool: {name}"
            )
        try:
            text = self.tool_provider.call_tool(name, dict(arguments or {}))
        except ToolError as exc:
            logger.info("MCP tool %s → isError: %s", name, exc)
            return self._tool_result(request_id, str(exc), is_error=True)
        except Exception:  # fail-closed : aucune fuite d'exception protocole
            logger.exception("MCP tool a échoué (erreur interne)")
            return self._tool_result(request_id, "Internal tool error", is_error=True)
        return self._tool_result(request_id, text, is_error=False)

    # --- Helpers -----------------------------------------------------------------------

    @staticmethod
    def _tool_result(request_id: Any, text: str, *, is_error: bool) -> dict[str, Any]:
        """Réponse MCP ``CallToolResult`` : contenu text + drapeau isError."""
        return success_result(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": is_error},
        )

    @staticmethod
    def _encode(payload: dict[str, Any] | None) -> str | None:
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "InMemoryToolProvider",
    "MCPServer",
    "MCPTool",
    "ToolError",
    "ToolProvider",
]


class InMemoryToolProvider:
    """Registre de tools en mémoire, immutable après construction.

        Bootstrap S1 : suffisant pour ListTools/CallTool en attendant la
    projection du registre legacy ``ToolRegistry`` sur le port
    ``MCPToolRegistryPort`` (S2, tâche 6 : 12 tools read-only).
    """

    def __init__(self, tools: Iterable[MCPTool]) -> None:
        self._tools: dict[str, MCPTool] = {tool.name: tool for tool in tools}

    def list_tools(self) -> list[MCPTool]:
        return list(self._tools.values())

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        return tool.handler(dict(arguments or {}))
