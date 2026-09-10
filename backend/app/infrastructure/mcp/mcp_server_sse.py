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
    - erreur de protocole : réponse JSON-RPC ``error`` (id: null) dans le flux ;
    - auth transport (P5) : ``X-API-Key`` exigée par défaut
      (``MCP_AUTH_REQUIRED``, fail-closed) — même clé, même repli dev et même
      comparaison à temps constant que la surface REST ; 401 en enveloppe v1.

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

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response

from app.config.settings import get_settings
from app.domain.entities.mcp import MCPScopeRole
from app.infrastructure.mcp.mcp_audit import audit_mcp_call
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.security.api_key import is_valid_api_key

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

# Instance partagée du serveur (stateless, thread-safe). Le transport de
# l'Assistant IA utilise contributor afin de voir `orchestrate`; les mutations
# restent protégées par la policy du noyau et l'approbation humaine.
# Tâche 12 : le hook d'audit journalise chaque appel d'action MCP dans
# ``agent_audit`` (subject = client_id de l'en-tête ``X-Client-Id``).
# The Assistant IA uses the MCP orchestrator as its controlled entry point.
# CONTRIBUTOR is required to see `orchestrate`; mutations remain gated by the
# AgentCore sandbox policy and human approval.
_server = build_mcp_server(scope=MCPScopeRole.CONTRIBUTOR, audit=audit_mcp_call)


def mcp_server_enabled() -> bool:
    """Le serveur MCP est-il activé ? (interrupteur de rollback)."""
    return _MCP_SERVER_ENABLED


def mcp_auth_required() -> bool:
    """Auth transport obligatoire sur ``POST /mcp/sse`` ? (P5 — défaut : oui).

    La surface MCP exécute des outils RÉELS : sans garde, ``MCP_FIRST=true``
    gèle l'HTTP legacy mais laisse un canal d'exécution ouvert. Lecture de
    l'environnement à l'appel (compatibilité ``monkeypatch.setenv`` des
    tests), puis ``Settings.mcp_auth_required`` ; repli ``True``
    (fail-closed) si les Settings ne sont pas chargeables. Rollback
    explicite : ``MCP_AUTH_REQUIRED=false``.
    """
    env = os.getenv("MCP_AUTH_REQUIRED")
    if env is not None:
        return env.strip().lower() not in {"false", "0", "no", "off"}
    try:
        return get_settings().mcp_auth_required
    except Exception:
        return True


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
    sinon ``anonymous``). Auth transport (P5) : ``X-API-Key`` exigée par
    défaut (``MCP_AUTH_REQUIRED=0`` pour un rollback explicite) — vérifiée
    AVANT toute lecture du corps (fail-closed) ; le secret client store
    (révocation par client) reste un durcissement S4+.
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
    session_id = mcp_session_id or f"tt-{uuid.uuid4().hex[:16]}"
    client_id = (x_client_id or session_id).strip() or "anonymous"
    # Auth transport (P5) : même mécanisme que la surface REST (X-API-Key,
    # repli dev, comparaison à temps constant — module partagé
    # app/infrastructure/security/api_key.py). Vérifié AVANT la lecture du
    # corps : aucune ressource n'est consommée pour une requête non authentifiée.
    if mcp_auth_required() and not is_valid_api_key(request.headers.get("X-API-Key")):
        logger.warning(
            "Requête MCP rejetée (X-API-Key absente/invalide) : client_id=%s session=%s",
            client_id,
            session_id,
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing X-API-Key header.",
                }
            },
        )
    raw = (await request.body()).decode("utf-8", errors="replace")
    # MCP tools may execute synchronous LLM/tool work for several seconds.
    # Keep that work off FastAPI's event loop so independent requests remain
    # responsive while a run is in progress.
    response_payload = await asyncio.to_thread(
        _server.handle_text,
        raw,
        client_id=client_id,
    )
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


__all__ = ["mcp_auth_required", "mcp_server_enabled", "router"]
