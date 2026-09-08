# project/app/infrastructure/mcp/protocol.py
"""Protocole MCP (Model Context Protocol) — couche JSON-RPC 2.0 minimale.

Choix d'architecture : AUCUNE dépendance au SDK officiel ``mcp`` (non installé,
cf. requirements.txt). Le bootstrap S1 (docs/mcp/IMPLEMENTATION_PLAN.md,
tâche 2) livre un socle self-contained et typé : les deux transports (SSE et
stdio) partagent ce module — enveloppes, méthodes et codes d'erreur ne sont
écrits qu'une fois.

Conformité :
    - enveloppe JSON-RPC 2.0 (https://www.jsonrpc.org/specification) ;
    - MCP ``protocolVersion`` = "2025-06-18" (streamable HTTP) ;
    - methods couvertes : initialize, ping, tools/list, tools/call,
      resources/list, resources/read, prompts/list, prompts/get,
      notifications/initialized.
"""

from __future__ import annotations

import json
from typing import Any

JSONRPC_VERSION = "2.0"

# Version du protocole MCP négociée à l'initialize. La roadmap versionne la
# SURFACE ThinkTuning (MCPVersion, [tool.mcp] version) indépendamment de ce
# constant : MCPVersion = 0.1.0 (bootstrap) ne changera pas à chaque evo du
# protocole transporté.
MCP_PROTOCOL_VERSION = "2025-06-18"

# Nom du serveur exposé dans serverInfo (identité MCP, pas le nom du paquet).
MCP_SERVER_NAME = "thinktuning-mcp"


class MCPMethod:
    """Identifiants des méthodes MCP servies au bootstrap S1."""

    INITIALIZE = "initialize"
    PING = "ping"
    TOOLS_LIST = "tools/list"
    TOOLS_CALL = "tools/call"
    RESOURCES_LIST = "resources/list"
    RESOURCES_READ = "resources/read"
    PROMPTS_LIST = "prompts/list"
    PROMPTS_GET = "prompts/get"
    NOTIFICATIONS_INITIALIZED = "notifications/initialized"


class ErrorCode:
    """Codes d'erreur JSON-RPC 2.0 (les -32xxx sont réservés aux serveurs)."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


class ProtocolError(RuntimeError):
    """Erreur de protocole → réponse JSON-RPC ``error`` (id: null)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def success_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Enveloppe de réussite JSON-RPC ``{result: ...}``."""
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def error_result(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Enveloppe d'erreur JSON-RPC ``{error: {code, message}}``."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def parse_jsonrpc(raw: str) -> dict[str, Any] | list[Any]:
    """Parse un corps JSON-RPC brut ou lève ``ProtocolError(PARSE_ERROR)``.

    Les tableaux (batch) et les scalaires sont des JSON valides mais pas des
    REQUÊTES supportées : le serveur les rejette avec ``Invalid Request``.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(ErrorCode.PARSE_ERROR, f"Parse error: {exc}") from exc
    return payload


def empty_input_schema() -> dict[str, Any]:
    """Schéma d'entrée par défaut d'un tool SANS arguments (MCP inputSchema)."""
    return {"type": "object", "properties": {}, "additionalProperties": False}


__all__ = [
    "ErrorCode",
    "JSONRPC_VERSION",
    "MCP_PROTOCOL_VERSION",
    "MCPMethod",
    "MCP_SERVER_NAME",
    "ProtocolError",
    "empty_input_schema",
    "error_result",
    "parse_jsonrpc",
    "success_result",
]
