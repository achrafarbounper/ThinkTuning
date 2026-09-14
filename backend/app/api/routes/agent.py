# project/app/api/routes/agent.py

"""Adaptateur HTTP legacy de la surface agent — façade de délégation (B-5).

.. deprecated:: v3.0.0 (MCP-First, S7 — tâche 20 : docs/mcp/IMPLEMENTATION_PLAN.md)

    Surface d'entrée privilégiée : **MCP** (``POST /mcp/sse`` / stdio
    ``thinktuning-mcp``). Ce router n'est plus monté par ``app.api.main`` ;
    il survit comme adaptateur strangler (délégations v1 + tests de contrat).

Absorption B-5 (ADR-0003, écarts E-04/E-11) : la LOGIQUE MÉTIER vit dans les
use cases ``app/application/agent_surface``, le CONTRAT DE PRÉSENTATION dans
``app/api/schemas/agent`` + ``app/api/dependencies/mcp_first`` (garde lecture
seule partagée). Il ne reste ici que le CÂBLAGE des collaborateurs (stores,
factories, bus, télémétrie — seule l'``api/`` touche l'infrastructure) et la
traduction ``DomainError`` -> ``HTTPException`` legacy ``{"detail": ...}``.

Seam de test : les tests patchent les collaborateurs DANS CE MODULE
(``app.api.routes.agent.<nom>``) ; les endpoints les lisent à l'appel et les
transmettent aux use cases — aucune logique métier, aucun état dupliqué.

Surface RÉDUITE aux endpoints encore requis par les tests de contrat
(``test_agent_flow``, ``test_mcp_first``) — le reste est RETIRÉ (B-5) :

    - settings / providers / approvals (liste) / ask/core bloquant -> **v1**
      (``routes/v1/agent.py``, même contrat, enveloppe ``{"error": ...}``) ;
    - tools (liste/custom/recommend/stats), suggest + complete (copilot),
      runs, audit, features, ws, multi/ask bloquant -> use cases conservés
      dans l'historique git / équivalents MCP (``tools/call``, audit,
      ``orchestrate``) — plus aucune liaison HTTP.

Dépréciation active (verrouillée par ``tests/test_mcp_first.py``) :
DeprecationWarning à l'import ; en-têtes ``Deprecation`` / ``Sunset`` (RFC
8594) / ``Warning: 299`` sur chaque réponse ; flag ``MCP_FIRST=true`` ->
lecture seule 405 ``mcp_first_read_only`` (exception : approbation humaine).

Les endpoints sont des ``def`` : FastAPI les exécute en threadpool, l'appel
bloquant vers le LLM ne gèle pas l'event loop.
"""

from __future__ import annotations

import functools
import warnings
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.agent.factory import build_agent_core, new_core_enabled
from app.agent.settings import agent_flag, get_agent_config
from app.api.dependencies.auth import require_api_key
from app.api.dependencies.mcp_first import writable_endpoint
from app.api.schemas.agent import AskStreamRequest, MultiAskRequest, ToolRunRequest
from app.application import agent_surface

# --- Câblage partagé avec la surface v1 (``routes/v1/agent.py``) -------------
# Ces symboles ne servent plus d'endpoint legacy direct : ils sont lus à
# l'appel par la surface v1 (``agent_wiring.<nom>``) et restent LES SEAMS DE
# TEST patchés par ``tests/test_api_v1_agent.py`` (source unique du câblage,
# ADR-0003 §1 — pas de duplication d'état entre surfaces).
from app.application.agent_cache import reload_agent_runner  # noqa: F401 — câblage v1
from app.application.agent_settings_usecase import (
    get_effective_settings,
    update_settings,  # noqa: F401 — câblage v1
)
from app.application.ask_usecase import run_ask_core  # noqa: F401 — câblage v1 (seam ask/core)
from app.application.multi_agent_usecase import run_multi_agent_streaming
from app.application.session_memory import load_session_history, persist_exchange
from app.domain.errors import DomainError
from app.infrastructure.events.in_memory import InMemoryEventBus
from app.infrastructure.legacy_approval_store import build_approval_store
from app.infrastructure.legacy_multi_agent_adapter import build_multi_agent_orchestrator
from app.infrastructure.legacy_settings_adapter import build_settings_port
from app.infrastructure.persistence.approval_store import (
    STATUSES,  # noqa: F401 — câblage partagé avec la surface v1
    get_approval_store,
)
from app.infrastructure.persistence.audit_store import (
    get_audit_store,
)
from app.infrastructure.persistence.flow_store import (
    STATUSES as FLOW_STATUSES,
)
from app.infrastructure.persistence.flow_store import (
    get_flow_store,
)
from app.infrastructure.persistence.mongodb import (  # noqa: F401 — câblage partagé v1
    MongoAgentProviderStore,
)
from app.infrastructure.persistence.run_store import (
    get_run_store,
)
from app.infrastructure.tools.tool_analytics import record_call
from app.infrastructure.tools.tool_registry import REQUIRED_ARGS, TOOLS

# ------------------------------------------------------------------
# Câblage : collaborateurs + traduction des erreurs de domaine
# ------------------------------------------------------------------


def _flag(name: str) -> bool:
    """Lit un flag (base IHM en priorité, repli env) — lecture à l'appel."""
    return agent_flag(name)


def _audit_log(action: str, subject: str = "", detail: dict | None = None, **kw):
    """Journal d'audit (flag ``AGENT_AUDIT``) — no-op si inactif."""
    if not _flag("audit"):
        return None
    return get_audit_store().log(action, subject=subject, detail=detail, **kw)


def _legacy_detail(exc: DomainError) -> Any:
    """Détail ``{"detail": ...}`` historique (préserve ``{"message", "errors"}``)."""
    if "message" in exc.details:
        return {"message": exc.details["message"], "errors": exc.details.get("errors", [])}
    return exc.message


def _delegated(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Use case + traduction ``DomainError`` -> ``HTTPException`` (contrat legacy).

    Le statut vient de ``DomainError.http_status`` — source unique partagée
    avec l'enveloppe v1 ``{"error": {...}}`` (parité statuts ET messages).
    """
    try:
        return fn(*args, **kwargs)
    except DomainError as exc:
        raise HTTPException(status_code=exc.http_status, detail=_legacy_detail(exc)) from exc


def _settings_payload() -> dict:
    """Config effective formatée pour le dashboard (clés jamais en clair)."""
    return agent_surface.settings_payload(build_settings_port())


def _settings_payload_from(effective: dict, port: Any) -> dict:
    """Formate un dict effectif en payload HTTP (factoring avec ``_settings_payload``)."""
    return agent_surface.settings_payload_from(effective, port)


def _load_session_history(session_id: str | None, resume_request_id: str | None) -> list[dict]:
    """Mémoire conversationnelle (lecture) — use case ``session_memory``."""
    return load_session_history(session_id, resume_request_id)


def _persist_exchange(
    session_id: str | None,
    prompt: str,
    answer: str,
    tool_events: list[dict] | None = None,
    thinking: str = "",
) -> None:
    """Mémoire conversationnelle (écriture) — use case ``session_memory``."""
    persist_exchange(session_id, prompt, answer, tool_events=tool_events, thinking=thinking)


# --- Dépréciation de la surface HTTP legacy (S7 — MCP-First, tâche 20) -------

#: Date cible de retrait (en-tête ``Sunset``, RFC 8594) + notices client/CI.
DEPRECATION_SUNSET = "Sat, 31 Dec 2026 23:59:59 GMT"
DEPRECATION_WARNING = "Surface HTTP /api/agent deprecated : mutations via MCP (POST /mcp/sse)"
DEPRECATION_NOTICE = (
    "app/api/routes/agent.py est la surface HTTP legacy de l'agent IA (v3.0.0 "
    "MCP-First). La surface d'entrée privilégiée est MCP (POST /mcp/sse, "
    "transport stdio `thinktuning-mcp`) ; ce module n'est conservé que comme "
    "adaptateur strangler des délégations v1. Activez MCP_FIRST=true pour "
    "geler la surface en lecture seule."
)

# Marquage @deprecated : un avertissement à l'import (verrouillé par
# ``tests/test_mcp_first.py``, visible avec ``-W error::DeprecationWarning``).
warnings.warn(DEPRECATION_NOTICE, DeprecationWarning, stacklevel=2)


async def _deprecated_surface_headers(request: Request, response: Response) -> None:
    """En-têtes de dépréciation sur chaque réponse HTTP (RFC 8594 + Warning 299).

    Le canal WebSocket est court-circuité (``scope.type != "http"`` :
    Starlette remplit ``response`` avec ``None`` dans ce contexte).
    """
    if request.scope.get("type") != "http":
        return  # canal WebSocket : rien à déprécier côté HTTP
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = DEPRECATION_SUNSET
    response.headers["Warning"] = f'299 - "{DEPRECATION_WARNING}"'


router = APIRouter(
    prefix="/api/agent",
    tags=["Agent IA"],
    dependencies=[Depends(_deprecated_surface_headers)],
)

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # désactive le buffering nginx
}
# ------------------------------------------------------------------
# Endpoints — câblage puis délégation aux use cases (agent_surface)
# ------------------------------------------------------------------


# --- Statut & outils ---------------------------------------------------------


@router.get("/status")
def agent_status():
    """Statut de l'agent : modèle visé, URL Ollama, timeout, outils dispo.

    Public (comme /health) : ne révèle aucune donnée sensible, permet au
    dashboard d'afficher la config sans clé API.
    """
    return agent_surface.agent_status(get_agent_config(), TOOLS)


@router.post("/tools/run")
@writable_endpoint
def run_tool(request: ToolRunRequest, _: bool = Depends(require_api_key)):
    """Exécute directement un outil (utile pour tester sans dépendre du LLM)."""
    return _delegated(
        agent_surface.run_tool_execution,
        TOOLS,
        REQUIRED_ARGS,
        request.tool,
        request.args,
        record_ctx=record_call,  # Phase B : télémétrie d'usage
    )


# --- Noyau agentique v2 (POST /api/agent/ask/core) ---------------------------
# Utilise app/agent/core.py (Intent -> Plan -> Policy -> Budget -> Action).
# Bascule en production : le noyau v2 est le DÉFAUT ; ``AGENT_NEW_CORE=0``
# répond 503 (repli legacy, tant que le chemin v1 n'est pas décommissionné).


@router.post("/ask/core/stream")
@writable_endpoint
def ask_core_stream(request: AskStreamRequest, _: bool = Depends(require_api_key)):
    """Noyau agentique v2 en streaming SSE (flag ``AGENT_NEW_CORE``).

    Frames ``data:`` : ``tool_start`` / ``tool_result`` / ``delta`` (mot à
    mot) / ``final`` (statut + ids d'approbation) / ``[DONE]``. Premier
    événement attendu au plus ``agent_sse_first_event_timeout`` puis le flux
    démarre quoi qu'il arrive (prélude + heartbeats) : l'erreur voyage DANS
    le flux au lieu d'un 502 tardif (fix proxy Render).
    """
    sse_settings = get_effective_settings(build_settings_port())
    plan = _delegated(
        agent_surface.prepare_core_stream,
        prompt=request.prompt,
        session_id=request.session_id,
        resume_request_id=request.resume_request_id,
        enable_thinking=request.enable_thinking,
        model=request.model,
        run_store=get_run_store(),
        approval_store=build_approval_store(),
        flow_store=get_flow_store(),
        bus_factory=InMemoryEventBus,
        build_core=functools.partial(
            build_agent_core,
            model=request.model,
            enable_thinking=request.enable_thinking,
        ),
        audit_log=_audit_log,
        core_enabled=new_core_enabled,
        get_config=get_agent_config,
        load_history=_load_session_history,
        save_exchange=_persist_exchange,
        first_event_timeout_s=float(sse_settings.get("agent_sse_first_event_timeout", 25)),
        heartbeat_interval_s=float(sse_settings.get("agent_sse_heartbeat", 10)),
    )

    first_kind, first_payload, first_ready = agent_surface.wait_first_core_event(plan)
    if first_ready and first_kind == "http_error":
        if isinstance(first_payload, BaseException):
            raise first_payload  # noqa: TRY201 — statut d'origine préservé (parité legacy)
        raise HTTPException(status_code=502, detail=str(first_payload))
    if first_ready and first_kind == "error":
        raise HTTPException(status_code=502, detail=str(first_payload))

    return StreamingResponse(
        agent_surface.core_sse_stream(plan, first_kind, first_payload, first_ready),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# --- Approbation humaine (approve / reject) ---------------------------------


@router.post("/approvals/{request_id}/approve")
def approve_request(request_id: str, _: bool = Depends(require_api_key)):
    """Valide une demande `pending` → `approved` (l'outil sera exécuté au résumé)."""
    return _delegated(
        agent_surface.decide_approval,
        get_approval_store(),
        request_id,
        "approve",
        _audit_log,
    )


@router.post("/approvals/{request_id}/reject")
def reject_request(request_id: str, _: bool = Depends(require_api_key)):
    """Refuse une demande `pending` → rejected (aucune exécution)."""
    return _delegated(
        agent_surface.decide_approval,
        get_approval_store(),
        request_id,
        "reject",
        _audit_log,
    )


# --- Orchestration multi-agents (superviseur / workers) --------------------


@router.post("/multi/ask/stream")
@writable_endpoint
def multi_ask_stream(request: MultiAskRequest, _: bool = Depends(require_api_key)):
    """Orchestration multi-agents en streaming SSE (événements nommés ``agent.*``).

    ``agent.plan`` / ``agent.worker.start|tool|error|result|approval`` /
    ``agent.synthesizing`` / ``agent.done`` / ``agent.error``. ``mode`` :
    « full » (tous) ou « compact » (plan + cycle worker + done, sans
    l'observabilité tool/synthesizing) ; ``agent.worker.thinking`` passe dans
    les deux modes. Panne précoce = vraie erreur HTTP (502).
    """
    plan = agent_surface.prepare_multi_stream(
        prompt=request.prompt,
        model=request.model,
        parallel=request.parallel,
        resume_request_id=request.resume_request_id,
        enable_thinking=request.enable_thinking,
        mode=request.mode,
        flow_store=get_flow_store(),
        orchestrator_factory=build_multi_agent_orchestrator,
        run_streaming=run_multi_agent_streaming,
        get_config=get_agent_config,
    )

    # Réception SYNCHRONE du premier événement : une panne précoce reste une
    # vraie erreur HTTP (même politique que /ask/stream).
    first_kind, first_payload = agent_surface.wait_first_multi_event(plan)
    if first_kind == "agent.error":
        raise HTTPException(
            status_code=502, detail=first_payload.get("message", "Erreur multi-agents.")
        )
    if first_kind == "__done__":
        raise HTTPException(status_code=502, detail="Aucun événement produit.")

    return StreamingResponse(
        agent_surface.multi_sse_stream(plan, first_kind, first_payload),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# --- Journal « Agent Flow Map » (sessions multi-agents persistées) ---------


@router.get("/flow")
def list_flow_sessions(
    limit: int = 50,
    status: str | None = None,
    _: bool = Depends(require_api_key),
):
    """Liste paginée des sessions Flow Map (résumés sans timeline)."""
    return _delegated(
        agent_surface.flow_sessions,
        get_flow_store(),
        limit=limit,
        status=status,
        statuses=FLOW_STATUSES,
    )


@router.get("/flow/{flow_id}")
def get_flow_session(flow_id: str, _: bool = Depends(require_api_key)):
    """Détail d'une session Flow Map : timeline horodatée ``{"event", "data", "at_ms"}``."""
    return _delegated(agent_surface.flow_session, get_flow_store(), flow_id)


@router.delete("/flow/{flow_id}")
@writable_endpoint
def delete_flow_session(flow_id: str, _: bool = Depends(require_api_key)):
    """Supprime une session enregistrée (nettoyage du journal Flow Map)."""
    return _delegated(agent_surface.delete_flow_session, get_flow_store(), flow_id)
