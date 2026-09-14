# project/app/api/routes/v1/agent.py

"""Surface versionnée de l'agent IA (découplage agent — Phase 3d-4, B-5).

Les routes v1 ne délèguent PLUS aux handlers legacy : elles appellent
directement les use cases ``app.application.agent_surface`` (absorption B-5,
écart E-11) et n'utilisent que des DTOs d'``app.api.schemas.agent``. Le module
legacy ``app.api.routes.agent`` n'est plus appelé QUE comme module de WIRING :
ses collaborateurs (stores, factories, bus, seams de test) sont lus à l'appel.

Invariants conservés (verrouillés par les tests de contrat) :

    - mêmes chemins, méthodes, dépendances d'auth (``X-API-Key`` OU Bearer JWT :
      scope read pour les GET, scope action pour les mutations) ;
    - erreurs métier = ``DomainError`` du domaine, sérialisées par le handler
      global en enveloppe unifiée ``{"error": {"code", "message", "details"}}``
      (la table statut -> erreur n'est plus déduite d'une ``HTTPException``) ;
    - garde MCP-First PARTAGÉE avec la surface legacy
      (``app.api.dependencies.mcp_first.writable_endpoint``) : les mutations
      répondent 405 ``mcp_first_read_only`` quand ``MCP_FIRST=true``, sauf
      l'approbation humaine (canal de déblocage des runs MCP).

Pourquoi lire le wiring depuis ``app.api.routes.agent`` ? Les tests des deux
surfaces patchent les collaborateurs sur ce module (cible historique
``app.api.routes.agent.<nom>``) ; centraliser le câblage là-bas évite deux
vérités pour les mêmes dépendances (ADR-0003 §1, « pas de duplication d'état »).
"""

from __future__ import annotations

import functools

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.dependencies.agent_probe import run_connectivity_probe
from app.api.dependencies.auth import require_api_key_or_jwt, require_read_api_key_or_jwt
from app.api.dependencies.mcp_first import writable_endpoint
from app.api.routes import agent as agent_wiring
from app.api.schemas.agent import (
    AgentProviderPayload,
    AgentSettingsUpdate,
    AskRequest,
    AskResponse,
    AskStreamRequest,
    ConnectivityTestRequest,
    MultiAskRequest,
)
from app.application import agent_surface
from app.domain.errors import AgentRunError, NotFoundError

router = APIRouter(prefix="/agent", tags=["Agent IA (v1)"])

# Erreurs du flux SSE converties en erreur de domaine v1 (502 → AgentRunError).
_STREAM_ERROR_STATUS = 502


def _provider_or_404(provider_id: str):
    """Document provider ou 404 explicite (contrat legacy conservé)."""
    document = agent_wiring.MongoAgentProviderStore().get(provider_id)
    if not document:
        raise NotFoundError("Provider introuvable.")
    return document


def _apply_provider_settings(values: dict):
    """Enchaîne l'activation d'un provider : réglages dérivés -> PUT /settings."""
    return update_agent_settings(AgentSettingsUpdate(**values), True)


# --- Paramètres -------------------------------------------------------------


@router.get("/settings")
def read_agent_settings(_: bool = Depends(require_read_api_key_or_jwt)):
    """Paramètres effectifs de l'agent (valeurs + source, clés masquées)."""
    return agent_wiring._settings_payload()


@router.put("/settings")
@writable_endpoint
def update_agent_settings(update: AgentSettingsUpdate, _: bool = Depends(require_api_key_or_jwt)):
    """Sauvegarde partielle puis rechargement immédiat de l'agent."""
    return agent_surface.update_settings_values(
        update.model_dump(exclude_none=True),
        agent_wiring.build_settings_port(),
        update_settings_fn=agent_wiring.update_settings,
        reload_runner=agent_wiring.reload_agent_runner,
        payload_from=agent_wiring._settings_payload_from,
    )


@router.post("/settings/test")
@writable_endpoint
def test_agent_connectivity(
    request: ConnectivityTestRequest, _: bool = Depends(require_api_key_or_jwt)
):
    """Sonde le provider demandé — résultat 200 même en échec (affiché tel quel)."""
    return run_connectivity_probe(
        request,
        get_config=agent_wiring.get_agent_config,
        audit_log=agent_wiring._audit_log,
    )


# --- Providers LLM ----------------------------------------------------------


@router.get("/providers")
def list_agent_providers(_: bool = Depends(require_read_api_key_or_jwt)):
    """Providers LLM enregistrés, avec secrets masqués."""
    return agent_surface.list_providers(agent_wiring.MongoAgentProviderStore())


@router.post("/providers")
@writable_endpoint
def save_agent_provider(provider: AgentProviderPayload, _: bool = Depends(require_api_key_or_jwt)):
    """Crée ou met à jour un document provider."""
    return agent_surface.save_provider_document(
        agent_wiring.MongoAgentProviderStore(), provider.model_dump(exclude_none=True)
    )


@router.delete("/providers/{provider_id}")
@writable_endpoint
def delete_agent_provider(provider_id: str, _: bool = Depends(require_api_key_or_jwt)):
    """Supprime un document provider."""
    return agent_surface.delete_provider(agent_wiring.MongoAgentProviderStore(), provider_id)


@router.post("/providers/{provider_id}/activate")
@writable_endpoint
def activate_agent_provider(provider_id: str, _: bool = Depends(require_api_key_or_jwt)):
    """Active un provider et applique sa clé côté serveur."""
    return agent_surface.activate_provider(_provider_or_404(provider_id), _apply_provider_settings)


# --- Noyau agentique v2 -----------------------------------------------------


@router.post("/ask/core", response_model=AskResponse)
@writable_endpoint
def ask_core(request: AskRequest, _: bool = Depends(require_api_key_or_jwt)):
    """Prompt libre via le noyau agentique (réponse bloquante)."""
    return AskResponse(
        **agent_surface.ask_core_turn(
            prompt=request.prompt,
            session_id=request.session_id,
            resume_request_id=request.resume_request_id,
            model=request.model or agent_wiring.get_agent_config().model_name,
            enable_thinking=request.enable_thinking,
            run_store=agent_wiring.get_run_store(),
            approval_store=agent_wiring.build_approval_store(),
            build_core=functools.partial(
                agent_wiring.build_agent_core,
                model=request.model,
                enable_thinking=request.enable_thinking,
            ),
            audit_log=agent_wiring._audit_log,
            core_enabled=agent_wiring.new_core_enabled,
            run_core=agent_wiring.run_ask_core,
            load_history=agent_wiring._load_session_history,
            save_exchange=agent_wiring._persist_exchange,
        )
    )


@router.post("/ask/core/stream")
@writable_endpoint
def ask_core_stream(request: AskStreamRequest, _: bool = Depends(require_api_key_or_jwt)):
    """Noyau agentique en streaming SSE (tool_start / tool_result / delta / final)."""
    sse_settings = agent_wiring.get_effective_settings(agent_wiring.build_settings_port())
    plan = agent_surface.prepare_core_stream(
        prompt=request.prompt,
        session_id=request.session_id,
        resume_request_id=request.resume_request_id,
        enable_thinking=request.enable_thinking,
        model=request.model,
        run_store=agent_wiring.get_run_store(),
        approval_store=agent_wiring.build_approval_store(),
        flow_store=agent_wiring.get_flow_store(),
        bus_factory=agent_wiring.InMemoryEventBus,
        build_core=functools.partial(
            agent_wiring.build_agent_core,
            model=request.model,
            enable_thinking=request.enable_thinking,
        ),
        audit_log=agent_wiring._audit_log,
        core_enabled=agent_wiring.new_core_enabled,
        get_config=agent_wiring.get_agent_config,
        load_history=agent_wiring._load_session_history,
        save_exchange=agent_wiring._persist_exchange,
        first_event_timeout_s=float(sse_settings.get("agent_sse_first_event_timeout", 25)),
        heartbeat_interval_s=float(sse_settings.get("agent_sse_heartbeat", 10)),
    )

    kind, payload, ready = agent_surface.wait_first_core_event(plan)
    if ready and kind in ("http_error", "error"):
        # Panne précoce : erreur de domaine v1 (enveloppe {"error": {...}}).
        raise AgentRunError(str(getattr(payload, "detail", payload)))

    return StreamingResponse(
        agent_surface.core_sse_stream(plan, kind, payload, ready),
        media_type="text/event-stream",
        headers=agent_wiring._SSE_HEADERS,
    )


@router.post("/multi/ask/stream")
@writable_endpoint
def multi_ask_stream(request: MultiAskRequest, _: bool = Depends(require_api_key_or_jwt)):
    """Orchestration multi-agents en streaming SSE (événements nommés)."""
    plan = agent_surface.prepare_multi_stream(
        prompt=request.prompt,
        model=request.model,
        parallel=request.parallel,
        resume_request_id=request.resume_request_id,
        enable_thinking=request.enable_thinking,
        mode=request.mode,
        flow_store=agent_wiring.get_flow_store(),
        orchestrator_factory=agent_wiring.build_multi_agent_orchestrator,
        run_streaming=agent_wiring.run_multi_agent_streaming,
        get_config=agent_wiring.get_agent_config,
    )

    first_kind, first_payload = agent_surface.wait_first_multi_event(plan)
    if first_kind == "agent.error":
        raise AgentRunError(first_payload.get("message", "Erreur multi-agents."))
    if first_kind == "__done__":
        raise AgentRunError("Aucun événement produit.")

    return StreamingResponse(
        agent_surface.multi_sse_stream(plan, first_kind, first_payload),
        media_type="text/event-stream",
        headers=agent_wiring._SSE_HEADERS,
    )


# --- Approbation humaine ----------------------------------------------------


@router.get("/approvals")
def list_approvals(status: str | None = None, _: bool = Depends(require_read_api_key_or_jwt)):
    """Liste des demandes d'approbation (toutes ou filtrées par statut)."""
    return agent_surface.list_approvals(
        agent_wiring.get_approval_store(), status, agent_wiring.STATUSES
    )


@router.post("/approvals/{request_id}/approve")
def approve_request(request_id: str, _: bool = Depends(require_api_key_or_jwt)):
    """Valide une demande pending → approved (l'outil sera exécuté au résumé)."""
    return agent_surface.decide_approval(
        agent_wiring.get_approval_store(), request_id, "approve", agent_wiring._audit_log
    )


@router.post("/approvals/{request_id}/reject")
def reject_request(request_id: str, _: bool = Depends(require_api_key_or_jwt)):
    """Refuse une demande pending → rejected (aucune exécution)."""
    return agent_surface.decide_approval(
        agent_wiring.get_approval_store(), request_id, "reject", agent_wiring._audit_log
    )


# --- Flow Map ---------------------------------------------------------------


@router.get("/flow")
def list_flow_sessions(
    limit: int = 50,
    status: str | None = None,
    _: bool = Depends(require_read_api_key_or_jwt),
):
    """Liste paginée des sessions multi-agents (Flow Map), récentes d'abord."""
    return agent_surface.flow_sessions(
        agent_wiring.get_flow_store(),
        limit=limit,
        status=status,
        statuses=agent_wiring.FLOW_STATUSES,
    )


@router.get("/flow/{flow_id}")
def get_flow_session(flow_id: str, _: bool = Depends(require_read_api_key_or_jwt)):
    """Détail d'une session Flow Map : timeline horodatée des événements."""
    return agent_surface.flow_session(agent_wiring.get_flow_store(), flow_id)
