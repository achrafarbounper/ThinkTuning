# project/app/api/routes/mcp.py
"""Routes internes MCP — observabilité du dashboard (S4, tâche 12).

Dashboard interne (docs/mcp/MCP_SECURITY.md, « Observabilité MCP ») :

    - MCP error rate    → ``app/infrastructure/persistence/audit_store`` (table agent_audit) ;
    - MCP call volume   → ``app/infrastructure/persistence/audit_store`` (répartition par action) ;
    - Revoked clients   → ``app/infrastructure/persistence/mcp_client_store``.

Surface historique NON versionnée : consommée via le délégué v1
(``app/api/routes/v1/mcp.py`` — strangler, même handler = parité garantie).
La révocation d'un client (``MCPClientStore.revoke``) est une opération
d'administration opérée en base/CLI — elle n'est PAS exposée ici : ce
endpoint est en lecture seule.
"""

from __future__ import annotations

import time

from fastapi import APIRouter

router = APIRouter(tags=["MCP"])


def _client_store():
    """Resolve the Mongo-backed MCP client store at call time."""
    from app.infrastructure.persistence.mcp_client_store import get_mcp_client_store

    return get_mcp_client_store()


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
             "error_count", "error_rate", "scope_usage"}, ...],
          "latency": {
            "tools/call": {"count": 12, "p50_ms": 42.1, "p95_ms": 180.4,
                           "p99_ms": 240.0, "max_ms": 250.7}, ...          # MCP 2.3.0
          },
          "runtime": {
            "sessions_active": 2, "sse_streams_active": 1,
            "runs_active": 3, "runs_awaiting_approval": 1                # HITL
          }
        }

    ``clients_detail`` est trié par volume décroissant (les clients les plus
    actifs d'abord — lecture dashboard). Le taux d'erreur MCP agrège les
    entrées d'audit marquées ``is_error`` (échecs journalisés ET marqués par
    le serveur MCP — voir ``app/infrastructure/mcp/mcp_server.py``).

    MCP 2.3.0 (SCRUM-161) — observabilité : ``latency`` expose les quantiles
    p50/p95/p99 lus SANS PromQL (fenêtre glissante ``mcp_metrics``, par
    méthode : ``tools/call``, ``orchestrate_events``…), et ``runtime`` les
    jauges temps réel (sessions SSE actives, flux ouverts, runs actifs, runs en
    attente d'approbation humaine). Les compteurs/jauges Prometheus
    correspondants restent exposés par ``GET /metrics``.
    """
    from app.infrastructure.mcp import mcp_metrics as mcp_metrics_module
    from app.infrastructure.persistence.audit_store import get_audit_store

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
        "latency": mcp_metrics_module.latency_quantiles(),
        "runtime": mcp_metrics_module.gauge_snapshot(),
    }


__all__ = ["mcp_metrics", "router"]
