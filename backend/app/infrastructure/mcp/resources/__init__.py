# project/app/infrastructure/mcp/resources/__init__.py
"""Resources MCP « thinktuning:// » (tâches 8 — S3, et 13 — S5, v1.1.0).

- ``resource_provider`` : ``LegacyResourceProvider`` (port
  ``MCPResourceRegistryPort``, tâche 3) — 10 resources (4 statiques + 6
  paramétrées) résolues par délégation aux sources internes read-only :
  ``job_list``, ``job_get``, ``model_versions``, ``dataset_stats``,
  ``head_file`` (legacy) + ``core.job_logs`` (logs mémoire),
  ``core.agent_cache.agent_config`` (config) et le use case santé v1
  ``run_health_check`` (adaptateurs legacy par défaut).
  Sécurité : parsing strict des URI (anti-traversée, anti double-encodage),
  ``safe_resolve`` porté par délégation (``ia/tools/sandbox.py``), SQLite
  ``mode=ro`` + ``PRAGMA query_only`` (``ia/tools/ml_tools.py``) pour TOUTES
  les lectures SQL, formats dataset restreints pour l'aperçu (un ``.env``
  est refusé avant lecture), clés API de ``agent_config`` JAMAIS en clair
  (masquage, convention dashboard).
"""

from __future__ import annotations

from app.infrastructure.mcp.resources.resource_provider import (
    MAX_PREVIEW_LINES,
    RESOURCE_MIME_TYPE,
    RESOURCE_SCHEME,
    LegacyResourceProvider,
    build_legacy_resource_provider,
)

__all__ = [
    "MAX_PREVIEW_LINES",
    "LegacyResourceProvider",
    "RESOURCE_MIME_TYPE",
    "RESOURCE_SCHEME",
    "build_legacy_resource_provider",
]
