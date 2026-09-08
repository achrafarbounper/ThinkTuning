# project/app/infrastructure/mcp/resources/__init__.py
"""Resources MCP « thinktuning:// » (tâche 8 — S3, v1.0.0 Beta).

- ``resource_provider`` : ``LegacyResourceProvider`` (port
  ``MCPResourceRegistryPort``, tâche 3) — 5 resources (3 statiques + 2
  paramétrées) résolues par délégation aux tools internes read-only
  (``job_list``, ``job_get``, ``model_versions``, ``dataset_stats``,
  ``agent_config``). Sécurité : parsing strict des URI (anti-traversée,
  anti double-encodage), ``safe_resolve`` porté par délégation
  (``ia/tools/sandbox.py``), SQLite ``mode=ro`` + ``PRAGMA query_only``
  (``ia/tools/ml_tools.py``), clés API de ``agent_config`` JAMAIS en clair
  (masquage, convention dashboard).
"""

from __future__ import annotations

from app.infrastructure.mcp.resources.resource_provider import (
    RESOURCE_MIME_TYPE,
    RESOURCE_SCHEME,
    LegacyResourceProvider,
    build_legacy_resource_provider,
)

__all__ = [
    "LegacyResourceProvider",
    "RESOURCE_MIME_TYPE",
    "RESOURCE_SCHEME",
    "build_legacy_resource_provider",
]
