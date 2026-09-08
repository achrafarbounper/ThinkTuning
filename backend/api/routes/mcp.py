# project/api/routes/mcp.py
"""Routes internes MCP — observabilité du dashboard (S4, tâche 12).

Dashboard interne (docs/mcp/MCP_SECURITY.md, « Observabilité MCP ») :

    - MCP error rate    → ``core/audit_store`` (table agent_audit) ;
    - MCP call volume   → ``core/audit_store`` (répartition par action) ;
    - Revoked clients   → ``core/mcp_client_store``.

Surface historique NON versionnée : consommée via le délégué v1
(``api/routes/v1/mcp.py`` — strangler, même handler = parité garantie).
La révocation d'un client (``MCPClientStore.revoke``) est une opération
d'administration opérée en base/CLI — elle n'est PAS exposée ici : ce
endpoint est en lecture seule.
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter

router = APIRouter(tags=["MCP"])


def _client_store():
    """``MCPClientStore`` résolu À L'APPEL — l'env var (tests) prime.

    Le défaut du constructeur (``MCP_CLIENT_STORE_PATH``) est figé à l'import
    du module : résoudre le chemin ici permet l'isolation des tests via
    ``MCP_CLIENT_STORE_PATH`` sans recharger le module.
    """
    from core.mcp_client_store import MCP_CLIENT_STORE_PATH, MCPClientStore

    return MCPClientStore(path=os.getenv("MCP_CLIENT_STORE_PATH") or MCP_CLIENT_STORE_PATH)


def _client_view(client: dict) -> dict:
    """Projection d'un client pour le dashboard (métriques + révocation)."""
    call_count = int(client.get("call_count") or 0)
    error_count = int(client.get("error_count") or 0)
    return {
        "client_id": client.get("client_id", ""),
        "role": client.get("role", ""),
        "revoked": bool(client.get("revoked")),
        "revoked_reason": client.get("revoked_reason") or "",
        "call_count": call_count,
        "error_count": error_count,
        "error_rate": round(error_count / call_count, 4) if call_count else 0.0,
        "scope_usage": client.get("scope_usage") or {},
    }


@router.get("/mcp/metrics")
def mcp_metrics() -> dict:
    """Métriques internes MCP (dashboard) : error rate, call volume, revoked.

    Molécule stable (aucune clé manquante) :

        {
          "scrape_at_ms": 1710000000000,
          "call_volume": {
            "total": 42,
            "by_action": {"mcp_tool_call": 30, "mcp_resource_read": 8, ...},
            "errors": 3
          },
          "error_rate": 0.0714,
          "clients": {"total": 5, "active": 4, "revoked": 1},
          "clients_detail": [
            {"client_id", "role", "revoked", "revoked_reason", "call_count",
             "error_count", "error_rate", "scope_usage"}, ...
          ]
        }

    ``clients_detail`` est trié par volume décroissant (les clients les plus
    actifs d'abord — lecture dashboard). Le taux d'erreur MCP agrège les
    entrées d'audit marquées ``is_error`` (échecs journalisés ET marqués par
    le serveur MCP — voir ``app/infrastructure/mcp/mcp_server.py``).
    """
    from core.audit_store import get_audit_store

    audit = get_audit_store().mcp_metrics()
    clients = [_client_view(client) for client in _client_store().list()]
    clients.sort(key=lambda item: (-item["call_count"], item["client_id"]))
    revoked = sum(1 for client in clients if client["revoked"])
    return {
        "scrape_at_ms": int(time.time() * 1000),
        "call_volume": {
            "total": audit["total"],
            "by_action": audit["by_action"],
            "errors": audit["errors"],
        },
        "error_rate": audit["error_rate"],
        "clients": {
            "total": len(clients),
            "active": len(clients) - revoked,
            "revoked": revoked,
        },
        "clients_detail": clients,
    }


__all__ = ["mcp_metrics", "router"]