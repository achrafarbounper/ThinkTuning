# project/api/routes/v1/agent.py

"""Surface versionnée de l'agent IA (strangler — Phase 3d-4).

Les routes v1 délèguent aux handlers legacy ``api.routes.agent`` : même code
path, donc PARITÉ GARANTIE PAR CONSTRUCTION. Chaque handler legacy porte une
dépendance bipolaire ``Depends(require_api_key_or_jwt)`` (X-API-Key OU
Bearer JWT) ; côté v1 l'auth est appliquée par la route, puis la valeur
factice ``True`` est transmise à l'appel direct (la dépendance FastAPI
n'est pas rejouable en appel de fonction). GET (settings, approvals,
flow) en scope read, POST/PUT (ask, approvals, settings) en action admin.

Erreurs : les ``HTTPException`` legacy sont converties en ``DomainError``
(enveloppe v1 unifiée ``{"error": {"code", "message", "details"}}``) via
``convert_legacy_http_error``, avec les overrides propres au domaine agent
(502 → AgentRunError, 503 → ServiceUnavailableError, 504 → GatewayTimeoutError).

Seule la surface CONSOMMÉE par le dashboard est migrée (settings,
ask/core ± stream, multi/ask/stream, approvals, flow) ; la longue traîne
(/tools, /suggest, /runs, /audit, /features, /status) reste sur le legacy —
migration au besoin réel, pas académique.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from api.dependencies.auth import require_api_key_or_jwt, require_read_api_key_or_jwt
from api.routes import agent as legacy
from app.domain.errors import (
    AgentRunError,
    GatewayTimeoutError,
    ServiceUnavailableError,
)
from app.infrastructure.legacy_errors import convert_legacy_http_error

router = APIRouter(prefix="/agent", tags=["Agent IA (v1)"])

# Overrides domaine agent : 502 run agentique / 503 flag désactivé ou
# dépendance down / 504 provider injoignable (parité des statuts legacy).
_STATUS_OVERRIDES = {
    502: AgentRunError,
    503: ServiceUnavailableError,
    504: GatewayTimeoutError,
}


def _call_guarded(func, *args):
    """Appelle un handler legacy protégé (dernier paramètre ``_`` factice).

    Convertit son ``HTTPException`` en erreur de domaine v1 (enveloppe unifiée)
    ; les statuts sans équivalent sont re-levés tels quels (parité totale).
    """
    try:
        return func(*args, True)
    except HTTPException as exc:
        raise convert_legacy_http_error(exc, status_overrides=_STATUS_OVERRIDES) from exc


@router.get("/settings")
def read_agent_settings(_: bool = Depends(require_read_api_key_or_jwt)):
    """Paramètres effectifs de l'agent (valeurs + source, clés masquées)."""
    return _call_guarded(legacy.read_agent_settings)


@router.put("/settings")
def update_agent_settings(
    update: legacy.AgentSettingsUpdate, _: bool = Depends(require_api_key_or_jwt)
):
    """Sauvegarde partielle puis rechargement immédiat de l'agent."""
    return _call_guarded(legacy.update_agent_settings, update)


@router.post("/settings/test")
def test_agent_connectivity(
    request: legacy.ConnectivityTestRequest, _: bool = Depends(require_api_key_or_jwt)
):
    """Sonde le provider demandé — résultat 200 même en échec (affiché tel quel)."""
    return _call_guarded(legacy.test_agent_connectivity, request)


@router.post("/ask/core", response_model=legacy.AskResponse)
def ask_core(request: legacy.AskRequest, _: bool = Depends(require_api_key_or_jwt)):
    """Prompt libre via le noyau agentique (réponse bloquante)."""
    return _call_guarded(legacy.ask_core, request)


@router.post("/ask/core/stream")
def ask_core_stream(
    request: legacy.AskStreamRequest, _: bool = Depends(require_api_key_or_jwt)
) -> StreamingResponse:
    """Noyau agentique en streaming SSE (tool_start / tool_result / delta / final)."""
    return _call_guarded(legacy.ask_core_stream, request)


@router.post("/multi/ask/stream")
def multi_ask_stream(
    request: legacy.MultiAskRequest, _: bool = Depends(require_api_key_or_jwt)
) -> StreamingResponse:
    """Orchestration multi-agents en streaming SSE (événements nommés)."""
    return _call_guarded(legacy.multi_ask_stream, request)


@router.get("/approvals")
def list_approvals(status: str | None = None, _: bool = Depends(require_read_api_key_or_jwt)):
    """Liste des demandes d'approbation (toutes ou filtrées par statut)."""
    return _call_guarded(legacy.list_approvals, status)


@router.post("/approvals/{request_id}/approve")
def approve_request(request_id: str, _: bool = Depends(require_api_key_or_jwt)):
    """Valide une demande pending → approved (l'outil sera exécuté au résumé)."""
    return _call_guarded(legacy.approve_request, request_id)


@router.post("/approvals/{request_id}/reject")
def reject_request(request_id: str, _: bool = Depends(require_api_key_or_jwt)):
    """Refuse une demande pending → rejected (aucune exécution)."""
    return _call_guarded(legacy.reject_request, request_id)


@router.get("/flow")
def list_flow_sessions(
    limit: int = 50, status: str | None = None, _: bool = Depends(require_read_api_key_or_jwt)
):
    """Liste paginée des sessions multi-agents (Flow Map), récentes d'abord."""
    return _call_guarded(legacy.list_flow_sessions, limit, status)


@router.get("/flow/{flow_id}")
def get_flow_session(flow_id: str, _: bool = Depends(require_read_api_key_or_jwt)):
    """Détail d'une session Flow Map : timeline horodatée des événements."""
    return _call_guarded(legacy.get_flow_session, flow_id)
