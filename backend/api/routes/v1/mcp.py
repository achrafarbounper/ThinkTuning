# project/api/routes/v1/mcp.py

"""Surface versionnée MCP — dashboard interne (strangler — S4, tâche 12).

Délègue au handler legacy ``api.routes.mcp`` : même code path, donc PARITÉ
GARANTIE PAR CONSTRUCTION (convention ``api/routes/v1/*``). L'endpoint est
protégé par clé API : les métriques MCP exposent le registre des clients
(identités, révocations) — surface d'administration interne, pas de télémétrie
publique (contrairement à ``/api/v1/metrics``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.dependencies.auth import require_api_key
from api.routes import mcp as legacy

router = APIRouter(prefix="/mcp", tags=["MCP (v1)"])


@router.get("/metrics")
def get_mcp_metrics(_: bool = Depends(require_api_key)) -> dict:
    """Métriques internes MCP : error rate, call volume, revoked clients.

    Voir ``api.routes.mcp.mcp_metrics`` pour la molécule complète.
    """
    return legacy.mcp_metrics()


__all__ = ["router"]