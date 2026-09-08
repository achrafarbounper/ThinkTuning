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
- ``legacy_tool_provider`` (tâches 6/7) : projection de la SÉLECTION read-only
  v0.1.0 (``V010_READ_ONLY_TOOLS``, 13 tools) étendue en v1.0.0
  (``V100_READ_ONLY_TOOLS``, 25 tools — checklist tâche 7) du registre legacy
  ``ia/tools`` sur le port ``MCPToolRegistryPort`` — compilation REUSE
  (``compile_tool`` → ``entry_to_mcp_tool``), handlers délégués aux
  implémentations legacy (garde-fous sandbox/SSRF portés par délégation),
  erreurs → ``ToolError``, fail-closed à la construction (jamais de tool
  mutation exposé). ``build_mcp_server()`` expose la sélection v1.0.0 par
  défaut ; ``build_v010_read_only_provider()`` reste disponible.
- ``resources/`` (tâche 8) : ``LegacyResourceProvider`` (port
  ``MCPResourceRegistryPort``) — 5 resources ``thinktuning://`` (jobs,
  jobs/{job_id}, models, datasets/{path}/stats, config) résolues par
  délégation aux tools internes read-only ; sécurité : parsing strict des
  URI (anti-traversée, anti double-encodage), ``safe_resolve`` porté par
  délégation, SQLite ``mode=ro`` + ``query_only``, clés API masquées.
  ``build_mcp_server()`` branche ce registre par défaut.
- ``prompts/`` (tâche 9) : ``PromptProvider`` (port ``MCPPromptRegistryPort``)
  — 2 prompts résolus localement (aucune I/O, aucun tool, aucun LLM) :
  ``analyze-sentiment`` (``text``) et ``plan-training`` (``dataset``).
  Sécurité : fail-closed (nom inconnu → ``NotFoundError``, argument requis
  manquant / valeur non-string → ``ValidationError``), templates possédés
  par le serveur. ``build_mcp_server()`` branche ce registre par défaut.
- ``security/`` (tâche 11) : enforceur de sécurité MCP — ``scope_enforcer.py``
  (``check_scope`` / ``check_quota`` / ``check_rate_limit``, 4 catalogues par
  rôle read_only 12 < contributor 25 < operator 35 < admin 40) et
  ``rate_limit_bucket.py`` (primitive ``TokenBucket`` PARTAGÉE avec le
  middleware REST ``api/middlewares/rate_limit.py`` — zéro duplication).
  Le paquet security n'importe JAMAIS ``api`` (suite MCP légère).
"""

from __future__ import annotations

from app.domain.entities.mcp import DEFAULT_MCP_VERSION, MCPVersion
from app.infrastructure.mcp.legacy_tool_provider import (
    V010_READ_ONLY_TOOLS,
    V100_READ_ONLY_TOOLS,
    LegacyRegistryToolProvider,
    build_v010_read_only_provider,
    build_v100_read_only_provider,
)
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
from app.infrastructure.mcp.prompts.prompt_provider import (
    PromptProvider,
    build_prompt_provider,
)
from app.infrastructure.mcp.protocol import (
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    ErrorCode,
    MCPMethod,
    ProtocolError,
)
from app.infrastructure.mcp.resources.resource_provider import (
    LegacyResourceProvider,
    build_legacy_resource_provider,
)
from app.infrastructure.mcp.version_loader import load_mcp_version

# NOTE : ``manifest_generator`` n'est PAS ré-exporté ici (comme ``mcp_server_stdio``)
# : ``python -m app.infrastructure.mcp.manifest_generator`` importerait le module
# DEUX FOIS (via ce paquet, puis comme __main__) → RuntimeWarning + instance double.

__all__ = [
    "DEFAULT_MCP_VERSION",
    "ErrorCode",
    "InMemoryToolProvider",
    "LegacyRegistryToolProvider",
    "LegacyResourceProvider",
    "MCPMethod",
    "MCP_PROTOCOL_VERSION",
    "MCPTool",
    "MCPVersion",
    "MCPServer",
    "MCP_SERVER_NAME",
    "PolicyGateToolProvider",
    "PolicyVerdict",
    "PromptProvider",
    "ProtocolError",
    "ScopeFilteredToolProvider",
    "ToolError",
    "ToolProvider",
    "V010_READ_ONLY_TOOLS",
    "V100_READ_ONLY_TOOLS",
    "build_legacy_resource_provider",
    "build_mcp_server",
    "build_prompt_provider",
    "build_v010_read_only_provider",
    "build_v100_read_only_provider",
    "decide",
    "decide_action",
    "decision_to_annotations",
    "load_mcp_version",
    "mcp_sse_router",
    "visible_tools",
]
