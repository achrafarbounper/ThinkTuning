# project/app/infrastructure/mcp/mcp_server_sse.py
"""Transport SSE (Server-Sent Events) du serveur MCP — ``POST /mcp/sse``.

Le client envoie un message JSON-RPC 2.0 dans le corps de la requête ; le
serveur répond en ``text/event-stream`` avec un unique événement ``message``
portant la réponse JSON-RPC (style « streamable HTTP » du protocole MCP
2025-06-18, sans dépendance à sse-starlette : la réponse est un flux SSE à
événement unique, construit à la main).

Conventions MCP respectées :
    - entête ``Mcp-Session-Id`` : écho de la session du client (S1 = serveur
      sans état : on renvoie l'identifiant reçu, ou on en génère un si absent) ;
    - notification JSON-RPC (pas d'``id``) : aucune réponse attendue — le flux
      SSE émet un commentaire de garde (``: ok``) pour rester bien formé ;
    - erreur de protocole : réponse JSON-RPC ``error`` (id: null) dans le flux.

Note strangler : l'endpoint MCP est déclaré ``include_in_schema=False`` — il
n'est PAS une route REST et n'a aucune raison d'apparaître dans
``openapi.json`` (source du client TypeScript, verrou
``test_aucune_route_hors_v1_montee``). MCP possède son propre discovery
(``initialize`` → ``capabilities``) : le transport SSE reste monté en
``/mcp/sse`` (voir docs/mcp/IMPLEMENTATION_PLAN.md) sans casser le contrat v1.

Rollback (docs/mcp/IMPLEMENTATION_PLAN.md) : si ``MCP_SERVER_ENABLED=false``,
toute requête reçoit ``503 Service Unavailable`` — le serveur MCP est désactivé
sans toucher au reste de l'API.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response

from app.infrastructure.mcp.mcp_audit import audit_mcp_call
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server

logger = logging.getLogger("thinktuning.mcp.sse")

router = APIRouter(prefix="/mcp", tags=["mcp"])

# Interrupteur de rollback (docs/mcp/IMPLEMENTATION_PLAN.md) : ``false``/``0``
# désactive le serveur MCP → 503 sur toutes les requêtes.
_MCP_SERVER_ENABLED = os.getenv("MCP_SERVER_ENABLED", "true").strip().lower() not in {
    "false",
    "0",
    "no",
    "off",
}

# Instance partagée du serveur (stateless, thread-safe) — scope par défaut
# read_only ; la politique CLIENT complète arrive avec le client store (S4).
# Tâche 12 : le hook d'audit journalise chaque appel d'action MCP dans
# ``agent_audit`` (subject = client_id de l'en-tête ``X-Client-Id``).
_server = build_mcp_server(audit=audit_mcp_call)


def mcp_server_enabled() -> bool:
    """Le serveur MCP est-il activé ? (interrupteur de rollback)."""
    return _MCP_SERVER_ENABLED


def _sse_message(payload: dict[str, Any] | str | None) -> str:
    """Sérialise une réponse JSON-RPC en événement SSE ``message``.

    ``handle_text`` retourne déjà le JSON-RPC SÉRIALISÉ (str) : il est émis
    brut après ``data:`` (jamais re-encodé comme chaîne JSON — sinon la réponse
    serait double-échappée pour le client MCP).
    """
    if payload is None:  # notification : commentaire de garde, pas de réponse
        return ": ok\n\n"
    if isinstance(payload, str):
        data = payload
    else:
        data = json.dumps(payload, ensure_ascii=False)
    return f"event: message\ndata: {data}\n\n"


@router.post("/sse", include_in_schema=False)
async def mcp_sse(
    request: Request,
    mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id"),
    x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
) -> Response:
    """Endpoint MCP SSE : JSON-RPC request → flux SSE avec la réponse.

    Le corps de la requête est le message JSON-RPC (texte). La réponse est un
    flux ``text/event-stream`` à événement unique (``message``) — conforme au
    protocole MCP streamable HTTP sans dépendance externe. L'entête
    ``Mcp-Session-Id`` est écho de la session (stateless en S1).

    Tâche 12 (audit) : l'entête optionnelle ``X-Client-Id`` identifie le
    client MCP appelant — chaque appel d'action est journalisé dans
    ``agent_audit`` avec ``subject`` = client_id (repli : id de session,
    sinon ``anonymous``). L'authentification forte (secret client store)
    reste à brancher en S4/S5.
    """
    if not mcp_server_enabled():
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "mcp_disabled",
                    "message": "MCP server disabled (MCP_SERVER_ENABLED=false)",
                }
            },
        )
    raw = (await request.body()).decode("utf-8", errors="replace")
    session_id = mcp_session_id or f"tt-{uuid.uuid4().hex[:16]}"
    client_id = (x_client_id or session_id).strip() or "anonymous"
    response_payload = _server.handle_text(raw, client_id=client_id)
    headers = {
        "Mcp-Session-Id": session_id,
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        iter([_sse_message(response_payload)]),
        media_type="text/event-stream",
        headers=headers,
    )


__all__ = ["mcp_server_enabled", "router"]
