# project/api/routes/agent.py

"""Endpoints de l'agent IA intégrés au package api.

Exposent l'agent du paquet `ia/` sous le préfixe `/api/agent`, avec les
conventions du package api (router, dépendance `require_api_key`,
middlewares CORS / rate limit / métriques partagés) :

    GET  /api/agent/status      statut + config + outils disponibles (public)
    GET  /api/agent/tools       outils et leurs arguments requis
    POST /api/agent/tools/run   exécution directe d'un outil (sans passer par le LLM)
    POST /api/agent/ask         prompt libre -> l'agent planifie les outils puis répond

Les endpoints sont des `def` (et non async) : FastAPI les exécute dans un
threadpool, donc l'appel bloquant vers Ollama ne gèle pas l'event loop.
"""

import asyncio
import json
import queue
import threading
import time
from typing import Any, Optional
from collections.abc import AsyncIterator

import requests
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.dependencies.auth import require_api_key, _get_api_key
from core.agent_settings import get_agent_settings, save_agent_settings
from core.agent_cache import (
    REQUIRED_ARGS,
    TOOL_META,
    TOOLS,
    _openrouter_chat_url,
    _hf_chat_url,
    agent_config,
    ask_agent_decision,
    ask_agent_decision_streaming,
    ask_multi_agent,
    ask_multi_agent_streaming,
    reload_agent_runner,
)
from core.approval_store import (
    APPROVED,
    PENDING,
    REJECTED,
    STATUSES,
    get_approval_store,
)
from core.run_store import (
    AWAITING_APPROVAL as RUN_AWAITING_APPROVAL,
    COMPLETED as RUN_COMPLETED,
    ERROR as RUN_ERROR,
    REJECTED as RUN_REJECTED,
    STATUSES as RUN_STATUSES,
    get_run_store,
)
from core.session_store import get_session_store
from core.feature_flags import active_features, flag  # Phase A (flags)
from tools.tool_analytics import get_stats, record_call  # Phase B (analytique)
from tools.tool_discovery import suggest_tools  # Phase B (découverte)
from tools.plugin import loaded_plugins  # Phase B (plugins)
from ia.agent.context import (  # Phase C (contexte / mémoire)
    DEFAULT_HISTORY_BUDGET_TOKENS,
    format_memory_note,
    optimize_history,
    summarize_conversation,
    update_memory_summary,
)
from core.audit_store import (  # Phase A (audit / conformité)
    ACT_APPROVAL,
    ACT_CONFIG,
    ACT_CONNECT,
    ACT_RUN,
    ACT_TOOL,
    get_audit_store,
)
from copilot.feedback import get_feedback_store  # Phase D (copilot)
from copilot.suggestions import (  # Phase D (copilot)
    complete_text,
    suggest_for_context,
)

# Nouveau noyau agentique (app/) — activé par le flag AGENT_NEW_CORE.
from app.agent.core import RunStatus
from app.agent.factory import build_agent_core, new_core_enabled
from app.domain.entities.plan import Intent
from app.infrastructure.legacy_approval_store import build_approval_store

router = APIRouter(prefix="/api/agent", tags=["Agent IA"])

# Timeout (secondes) des sondes de connectivité du bouton « Tester ».
CONNECTIVITY_TIMEOUT_SECONDS = 8.0


def _audit_log(action: str, subject: str = "", detail: dict | None = None, **kw):
    """Journal d'audit activé par le flag ``AGENT_AUDIT`` (Phase A).

    No-op (retour None) quand le flag est inactif : zéro impact sur le
    comportement historique. ``kw`` peut porter actor/ip/request_id/run_id.
    """
    if not flag("audit"):
        return None
    store = get_audit_store()
    return store.log(action, subject=subject, detail=detail, **kw)


# --- Schémas Pydantic -------------------------------------------------------------

class AskRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Instruction envoyée à l'agent.")
    resume_request_id: Optional[str] = Field(
        None,
        description="Relance une tâche en attente : id donné par une réponse "
        "« awaiting_approval » après validation humaine (approve).",
    )
    session_id: Optional[str] = Field(
        None,
        description="Session de conversation (core/session_store) où journaliser "
        "l'échange ; absent : aucune persistance côté serveur.",
    )


class AskStreamRequest(BaseModel):
    """Corps de POST /api/agent/ask/stream (mode Agent temps réel).

    Même contrat que ``AskRequest`` avec en plus la sélection du modèle LLM
    et du mode « Réflexion » (sélecteurs de l'en-tête du chat).
    """

    prompt: str = Field(..., min_length=1, description="Instruction envoyée à l'agent.")
    resume_request_id: Optional[str] = Field(None)
    model: Optional[str] = Field(
        None, max_length=100, description="Modèle LLM ; absent/vide = défaut serveur."
    )
    enable_thinking: bool = Field(False, description="Mode « Réflexion ». ")
    session_id: Optional[str] = Field(None, description="Conversation cible (persistance).")


class MultiAskRequest(BaseModel):
    """Corps de l'orchestration multi-agents (superviseur/workers)."""

    prompt: str = Field(..., min_length=1, description="Tâche globale soumise au superviseur.")
    model: Optional[str] = Field(
        None, max_length=100, description="Modèle LLM ; absent/vide = défaut serveur."
    )
    parallel: bool = Field(
        False, description="Exécution parallèle des workers (défaut : séquentielle)."
    )
    mode: str = Field(
        "full",
        description="Granularité du streaming SSE : « full » (tous événements) "
        "ou « compact » (plan / worker.result / done uniquement).",
    )


# Événements « UX » : nécessaires au rendu, émis dans les deux modes.
_MULTI_UX_EVENTS = {
    "agent.plan",
    "agent.worker.start",
    "agent.worker.result",
    "agent.worker.approval",
    "agent.done",
    "agent.error",
}
# Événements « observabilité » : filtrés hors du mode « compact ».
_MULTI_OBSERVABILITY_EVENTS = {
    "agent.worker.tool",
    "agent.worker.error",
    "agent.synthesizing",
}


class AskResponse(BaseModel):
    response: str
    model: str
    status: str = "completed"
    request_id: Optional[str] = None
    approval: Optional[dict] = None


class ToolInfo(BaseModel):
    name: str
    required_args: list[str]
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolRunRequest(BaseModel):
    tool: str = Field(..., description="Nom de l'outil (ex: 'add', 'write_file').")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments de l'outil.")


class AgentSettingsUpdate(BaseModel):
    """Mise à jour partielle des paramètres de l'agent IA.

    Champ absent ou ``null`` : inchangé. Chaîne vide pour les champs texte :
    retour à la valeur par défaut du serveur.
    """

    provider: Optional[str] = Field(None, description="« ollama », « openrouter » ou « hf ».")
    model: Optional[str] = Field(None, max_length=200)
    ollama_url: Optional[str] = Field(None, max_length=500)
    openrouter_url: Optional[str] = Field(None, max_length=500)
    openrouter_api_key: Optional[str] = Field(None, max_length=300)
    hf_url: Optional[str] = Field(None, max_length=500)
    hf_api_key: Optional[str] = Field(None, max_length=300)
    timeout_seconds: Optional[float] = Field(None, ge=10, le=3600)
    context_length: Optional[int] = Field(None, ge=512, le=131072)
    temperature: Optional[float] = Field(None, ge=0, le=2)


class ConnectivityTestRequest(BaseModel):
    """Sonde de connectivité ; champs absents -> valeurs effectives courantes."""

    provider: Optional[str] = None
    ollama_url: Optional[str] = None
    openrouter_url: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    hf_url: Optional[str] = None
    hf_api_key: Optional[str] = None


# --- Endpoints ----------------------------------------------------------------------

@router.get("/status")
def agent_status():
    """Statut de l'agent : modèle visé, URL Ollama, timeout, outils dispo.

    Public (comme /health) : ne révèle aucune donnée sensible, permet au
    dashboard d'afficher la config sans clé API.
    """
    cfg = agent_config()
    return {
        "status": "ok",
        "provider": cfg["provider"],
        "model": cfg["model"],
        "ollama_url": cfg["ollama_url"],
        "timeout_seconds": cfg["timeout"],
        "context_length": cfg["context_length"],
        "auth_required": True,  # l'API principale applique toujours X-API-Key
        "tools": sorted(TOOLS),
    }


@router.get("/tools", response_model=list[ToolInfo])
def list_tools(_: bool = Depends(require_api_key)):
    """Liste des outils que l'agent peut appeler, avec leurs arguments requis,
    leur description et leur schéma de paramètres (issus de tools_config.json)."""
    return [
        ToolInfo(
            name=name,
            required_args=REQUIRED_ARGS[name],
            description=TOOL_META.get(name, {}).get("description", ""),
            parameters=TOOL_META.get(name, {}).get("parameters", {}),
        )
        for name in sorted(TOOLS)
    ]


@router.post("/tools/run")
def run_tool(request: ToolRunRequest, _: bool = Depends(require_api_key)):
    """Exécute directement un outil (utile pour tester sans dépendre du LLM)."""
    tool = request.tool
    if tool not in TOOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Tool inconnu : '{tool}'. Tools disponibles : {sorted(TOOLS)}",
        )

    missing = [key for key in REQUIRED_ARGS[tool] if key not in request.args]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Arguments manquants pour {tool} : {missing}",
        )

    try:
        with record_call(tool):  # Phase B : télémétrie d'usage
            result = TOOLS[tool](**request.args)
    except TypeError as exc:
        raise HTTPException(status_code=400, detail=f"Arguments invalides pour {tool} : {exc}")

    return {"tool": tool, "result": result}


@router.get("/tools/recommend")
def recommend_tools(
    q: str,
    k: int = 5,
    _: bool = Depends(require_api_key),
):
    """Recommandation d'outils à partir d'un besoin en langage naturel.

    Phase B (flag ``AGENT_TOOL_ANALYTICS``) : score lexical déterministe du
    catalogue contre la requête ``q`` — type « Copilot » pour aider l'UI à
    suggérer l'outil pertinent avant même un appel LLM.
    """
    if not flag("tool_analytics"):
        raise HTTPException(status_code=404, detail="Fonction désactivée (AGENT_TOOL_ANALYTICS)")
    return {"query": q, "suggestions": suggest_tools(q, k=k)}


@router.get("/tools/stats")
def tool_stats(reset: bool = False, _: bool = Depends(require_api_key)):
    """Analytique d'usage des outils (appels, erreurs, durée moyenne).

    Phase B (flag ``AGENT_TOOL_ANALYTICS``) — compteurs in-process depuis le
    démarrage (volatils) ; ``reset=1`` les remet à zéro.
    """
    if not flag("tool_analytics"):
        raise HTTPException(status_code=404, detail="Fonction désactivée (AGENT_TOOL_ANALYTICS)")
    return {"tools": get_stats(reset=reset), "plugins": loaded_plugins()}


# --- Copilot : suggestions & apprentissage (Phase D, flag AGENT_COPILOT) ------

class SuggestRequest(BaseModel):
    """Requête de suggestions « Copilot » : contexte de conversation + brouillon."""

    messages: list[dict] = Field(default_factory=list)
    draft: str = ""
    query: str = ""
    k: int = Field(default=3, ge=1, le=10)


class SuggestFeedbackRequest(BaseModel):
    """Issue d'une suggestion : acceptée ou refusée (boucle d'apprentissage)."""

    tool: str
    accepted: bool
    session_id: str = ""
    suggestion: dict = Field(default_factory=dict)


@router.post("/suggest")
def suggest(request: SuggestRequest, _: bool = Depends(require_api_key)):
    """Suggestions d'outils + squelette d'arguments pour le contexte courant.

    Phase D (flag ``AGENT_COPILOT``) : réutilise la découverte d'outils
    (Phase B) et le boost d'apprentissage issu des acceptations/refus
    enregistrés via ``/suggest/feedback``.
    """
    if not flag("copilot"):
        raise HTTPException(status_code=404, detail="Fonction désactivée (AGENT_COPILOT)")
    return suggest_for_context(
        messages=request.messages,
        draft=request.draft,
        query=request.query,
        k=request.k,
    )


@router.post("/suggest/feedback")
def suggest_feedback(request: SuggestFeedbackRequest, _: bool = Depends(require_api_key)):
    """Enregistre l'issue d'une suggestion (acceptée / refusée)."""
    if not flag("copilot"):
        raise HTTPException(status_code=404, detail="Fonction désactivée (AGENT_COPILOT)")
    entry = get_feedback_store().record(
        tool=request.tool,
        accepted=request.accepted,
        session_id=request.session_id,
        suggestion=request.suggestion,
    )
    return {"recorded": True, "id": entry["id"], "stats": get_feedback_store().stats()}


@router.post("/complete")
def complete(request: SuggestRequest, _: bool = Depends(require_api_key)):
    """Complétion en ligne (suite probable du brouillon, via le LLM)."""
    if not flag("copilot"):
        raise HTTPException(status_code=404, detail="Fonction désactivée (AGENT_COPILOT)")
    if not request.draft.strip():
        return {"completion": ""}
    try:
        from core.agent_cache import get_agent_runner

        llm = get_agent_runner().core.llm
    except Exception:
        raise HTTPException(status_code=503, detail="LLM indisponible pour la complétion")
    return {"completion": complete_text(llm, request.messages, request.draft)}


@router.post("/ask", response_model=AskResponse)
def ask(request: AskRequest, _: bool = Depends(require_api_key)):
    """Prompt libre : l'agent décide des outils, puis renvoie sa réponse finale.

    Ponte le gate de décision (auto_approve / approve / reject) : quand une
    action requiert une validation humaine, la réponse porte
    ``status="awaiting_approval"`` (avec ``request_id``) — l'agent n'attend
    pas, il s'arrête et l'UI peut proposer approve/reject puis relancer via
    ``resume_request_id``. S'il y a un refus (policy), ``status="rejected"``.

    Traduction des erreurs réseau (Timeout -> 504, ConnectionError -> 502)
    centralisée dans ``core.agent_cache``.
    """
    run_store = get_run_store()
    run_row = run_store.start_run(
        request.prompt, model=agent_config()["model"], source="ask"
    )
    _audit_log(
        ACT_RUN,
        subject="ask",
        detail={"status": "started", "model": agent_config()["model"]},
        run_id=run_row["id"],
    )
    try:
        decision = ask_agent_decision(
            request.prompt,
            resume_request_id=request.resume_request_id,
            # Mémoire de session : rejoue les tours précédents en contexte pour
            # que l'agent se souvienne de la conversation (ex. le nom).
            history_messages=_load_session_history(
                request.session_id, request.resume_request_id
            ),
        )
    except ValueError as exc:
        run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))

    status_map = {
        "completed": RUN_COMPLETED,
        "awaiting_approval": RUN_AWAITING_APPROVAL,
        "rejected": RUN_REJECTED,
    }
    run_store.finish_run(
        run_row["id"],
        status_map.get(decision.get("status", "completed"), RUN_COMPLETED),
        answer_summary=(decision.get("response") or "")[:300],
    )
    _audit_log(
        ACT_RUN,
        subject="ask",
        detail={
            "status": decision.get("status", "completed"),
            "model": agent_config()["model"],
        },
        run_id=run_row["id"],
    )
    _persist_exchange(
        request.session_id, request.prompt, decision.get("response") or ""
    )

    return AskResponse(
        response=decision["response"] or "",
        model=agent_config()["model"],
        status=decision.get("status", "completed"),
        request_id=decision.get("request_id"),
        approval=decision.get("approval"),
    )


# --- Nouveau noyau agentique (POST /api/agent/ask/core) ------------------------------
# Endpoint ADDITIF : utilise app/agent/core.py (Intent -> Plan -> Policy ->
# Budget -> Action) sans modifier le comportement de /ask. Tant que le flag
# AGENT_NEW_CORE est absent, il répond 503 (bascule incrémentale).


@router.post("/ask/core", response_model=AskResponse)
def ask_core(request: AskRequest, _: bool = Depends(require_api_key)):
    """Prompt libre via le nouveau noyau agentique (flag ``AGENT_NEW_CORE``).

    Réutilise les conventions de /ask : run_store, audit, persistance de
    session, réponse AskResponse (status : completed / awaiting_approval /
    rejected / error)."""
    if not new_core_enabled():
        raise HTTPException(
            status_code=503,
            detail="Nouveau noyau agentique désactivé (AGENT_NEW_CORE non activé).",
        )

    run_store = get_run_store()
    run_row = run_store.start_run(
        request.prompt, model=agent_config()["model"], source="ask_core"
    )
    _audit_log(ACT_RUN, subject="ask_core",
               detail={"status": "started"}, run_id=run_row["id"])

    # Gateway d'approbation : si le run reprend après validation humaine
    # (resume_request_id -> action approuvée), la gateway accorde UNIQUEMENT
    # l'action dont l'empreinte (args_hash) correspond à la demande approuvée.
    approval_store = build_approval_store()
    resume_record = (
        approval_store.get(request.resume_request_id)
        if request.resume_request_id else None
    )
    resume_hash = (
        resume_record.get("args_hash")
        if resume_record and resume_record.get("status") == APPROVED else None
    )

    def _approval_gateway(action) -> bool:
        return bool(resume_hash and action.fingerprint() == resume_hash)

    try:
        core = build_agent_core(approval_gateway=_approval_gateway)
        history = _load_session_history(request.session_id, request.resume_request_id)
        result = core.run(
            Intent(prompt=request.prompt, session_id=request.session_id or "default"),
            history=history,
        )
    except Exception as exc:
        run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    approval_payload = None
    if result.status is RunStatus.PENDING_APPROVAL and result.awaiting_action:
        action = result.awaiting_action
        record = approval_store.create(
            action.tool,
            action.args,
            action.category.value,
            "approve",
            "Policy : validation humaine requise",
            prompt=request.prompt,
            args_hash=action.fingerprint(),
        )
        approval_payload = {
            "request_id": record.get("request_id") or record.get("id"),
            "tool": action.tool,
            "args": action.args,
        }
        _audit_log(
            ACT_APPROVAL, subject="ask_core",
            detail={"request_id": approval_payload["request_id"], "tool": action.tool},
            run_id=run_row["id"],
        )

    status_map = {
        RunStatus.COMPLETED: "completed",
        RunStatus.PENDING_APPROVAL: "awaiting_approval",
        RunStatus.REJECTED_LOOP: "rejected",
        RunStatus.BUDGET_EXHAUSTED: "error",
        RunStatus.FAILED: "error",
    }
    api_status = status_map.get(result.status, "completed")
    run_store.finish_run(
        run_row["id"], status_map.get(result.status, RUN_COMPLETED),
        answer_summary=(result.answer or "")[:300],
    )
    _audit_log(
        ACT_RUN, subject="ask_core",
        detail={"status": api_status, "actions": len(result.actions),
                "rounds": result.rounds_used, "tool_calls": result.tool_calls_used},
        run_id=run_row["id"],
    )
    if api_status != "error":
        _persist_exchange(request.session_id, request.prompt, result.answer or "")

    return AskResponse(
        response=result.answer or "",
        model=agent_config()["model"],
        status=api_status,
        request_id=approval_payload["request_id"] if approval_payload else run_row["id"],
        approval=approval_payload,
    )


@router.post("/ask/core/stream")
def ask_core_stream(request: AskStreamRequest, _: bool = Depends(require_api_key)):
    """Nouveau noyau agentique en streaming SSE (flag ``AGENT_NEW_CORE``).

    Même contrat de flux que /ask/stream :
        data: {"tool_start":  {tool, args}}             appel d'outil annoncé
        data: {"tool_result": {tool, status, summary…}} résultat (ok/error)
        data: {"delta": "..."}                          réponse finale, mot à mot
        data: {"final": {...AskResponse...}}            statut + ids d'approbation
        data: [DONE]

    Le run ``AgentCore`` est exécuté dans un thread worker ; le callback
    ``on_tool_event`` du noyau alimente la queue en temps réel, la réponse
    finale est rejouée mot à mot (même cadence que /ask/stream).
    """
    if not new_core_enabled():
        raise HTTPException(
            status_code=503,
            detail="Nouveau noyau agentique désactivé (AGENT_NEW_CORE non activé).",
        )

    events: queue.Queue[tuple[str, object]] = queue.Queue()
    run_store = get_run_store()
    run_row = run_store.start_run(request.prompt, model=agent_config()["model"],
                                  source="ask_core_stream")
    _audit_log(ACT_RUN, subject="ask_core_stream",
               detail={"status": "started"}, run_id=run_row["id"])

    approval_store = build_approval_store()
    resume_record = (
        approval_store.get(request.resume_request_id)
        if request.resume_request_id else None
    )
    resume_hash = (
        resume_record.get("args_hash")
        if resume_record and resume_record.get("status") == APPROVED else None
    )

    def _approval_gateway(action) -> bool:
        return bool(resume_hash and action.fingerprint() == resume_hash)

    def _on_tool_event(event: dict) -> None:
        events.put(("tool", event))
        try:
            run_store.append_tool_event(run_row["id"], event)
        except Exception:  # pragma: no cover - le journal ne doit jamais bloquer
            pass

    def _on_thinking(chunk: str) -> None:
        events.put(("thinking", chunk))

    def worker() -> None:
        try:
            core = build_agent_core(
                approval_gateway=_approval_gateway,
                on_tool_event=_on_tool_event,
                enable_thinking=request.enable_thinking,
                on_thinking=_on_thinking,
            )
            history = _load_session_history(request.session_id, request.resume_request_id)
            result = core.run(
                Intent(prompt=request.prompt,
                       session_id=request.session_id or "default"),
                history=history,
            )

            # Création de la demande d'approbation le cas échéant (même
            # logique que /ask/core) : l'IHM affichera la carte de validation.
            approval_payload = None
            if result.status is RunStatus.PENDING_APPROVAL and result.awaiting_action:
                action = result.awaiting_action
                record = approval_store.create(
                    action.tool, action.args, action.category.value,
                    "approve", "Policy : validation humaine requise",
                    prompt=request.prompt, args_hash=action.fingerprint(),
                )
                approval_payload = {
                    "request_id": record.get("request_id") or record.get("id"),
                    "tool": action.tool,
                    "args": action.args,
                }
                _audit_log(
                    ACT_APPROVAL, subject="ask_core_stream",
                    detail={"request_id": approval_payload["request_id"],
                            "tool": action.tool},
                    run_id=run_row["id"],
                )

            status_map = {
                RunStatus.COMPLETED: "completed",
                RunStatus.PENDING_APPROVAL: "awaiting_approval",
                RunStatus.REJECTED_LOOP: "rejected",
                RunStatus.BUDGET_EXHAUSTED: "error",
                RunStatus.FAILED: "error",
            }
            api_status = status_map.get(result.status, "completed")
            run_store.finish_run(
                run_row["id"], status_map.get(result.status, RUN_COMPLETED),
                answer_summary=(result.answer or "")[:300],
            )
            _audit_log(
                ACT_RUN, subject="ask_core_stream",
                detail={"status": api_status,
                        "actions": len(result.actions),
                        "rounds": result.rounds_used,
                        "tool_calls": result.tool_calls_used},
                run_id=run_row["id"],
            )
            if api_status != "error":
                _persist_exchange(request.session_id, request.prompt,
                                  result.answer or "")

            # Rejoue la réponse finale mot à mot (convention /ask/stream).
            for word in _stream_fragments(result.answer or ""):
                events.put(("delta", word))
                time.sleep(ANSWER_STREAM_CADENCE_SECONDS)

            events.put(("final", {
                "response": result.answer or "",
                "model": agent_config()["model"],
                "status": api_status,
                "request_id": approval_payload["request_id"] if approval_payload else run_row["id"],
                "approval": approval_payload,
            }))
        except HTTPException as exc:  # panne réseau déjà traduite par agent_cache
            run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc.detail))
            events.put(("http_error", exc))
        except Exception as exc:
            run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc))
            events.put(("error", str(exc)))
        finally:
            events.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()

    # Réception SYNCHRONE du premier événement : une panne précoce (LLM
    # injoignable, resume_request_id invalide) reste une vraie erreur HTTP.
    first_kind, first_payload = events.get()
    if first_kind == "http_error":
        raise first_payload  # noqa: TRY201 - re-lever l'HTTPException d'origine
    if first_kind == "error":
        raise HTTPException(status_code=502, detail=str(first_payload))

    async def _sse_stream() -> AsyncIterator[str]:
        try:
            if first_kind != "done":
                field = _CORE_STREAM_FIELDS.get(first_kind, first_kind)
                yield _sse({field: first_payload})
            while True:
                kind, payload = await asyncio.to_thread(events.get)
                if kind == "done":
                    break
                if kind in ("http_error", "error"):
                    detail = payload.detail if kind == "http_error" else str(payload)
                    yield _sse({"error": detail})
                    break
                field = _CORE_STREAM_FIELDS.get(kind, kind)
                yield _sse({field: payload})
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        _sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# Champs d'événements du flux /ask/core/stream (queue -> SSE).
_CORE_STREAM_FIELDS = {
    "tool": "core_tool",
    "thinking": "thinking_delta",
    "delta": "delta",
    "final": "final",
}


# --- Streaming du mode Agent (POST /api/agent/ask/stream) ----------------------------

# Cadence (secondes) de l'émission mot à mot de la réponse finale ; la
# réflexion et les événements d'outils, eux, sont diffusés en temps réel.
ANSWER_STREAM_CADENCE_SECONDS = 0.02


def _stream_fragments(text: str):
    """Découpe un texte en fragments mot à mot (générateur synchrone)."""
    for word in text.split(" "):
        yield word + " "


def _sse(payload: dict | str) -> str:
    """Formate une charge utile en événement SSE (`data: ...`)."""
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return "data: " + data + "\n\n"


# Nombre maximal de paires user/assistant rejouées comme contexte de session
# (mémoire de conversation en mode Agent). Borné pour ne pas exploser la
# fenêtre de contexte LLM sur les longues conversations.
MAX_SESSION_CONTEXT_TURNS = 5


def _load_session_history(
    session_id: Optional[str],
    resume_request_id: Optional[str],
) -> list[dict]:
    """Recharge les messages précédents d'une session pour nourrir le contexte.

    Mémoire de session en mode Agent : sans cela chaque tour est atomique et
    l'agent « oublie » ce qui a été dit avant (ex. le nom de l'utilisateur).

    Règles :
        - ``session_id`` absent -> historique vide (pas de conversation à relire) ;
        - ``resume_request_id`` présent -> historique vide : la reprise relance
          l'état interne de l'agent, pas besoin de rejouer la conversation ;
        - sinon, on relit les ``get_messages`` de la session et on garde les
          ``MAX_SESSION_CONTEXT_TURNS`` dernières paires. Le tour courant n'y
          figure pas encore (``_persist_exchange`` écrit après coup).
        - Les ``tool_calls`` sont écartés (pas de JSON d'outils en contexte).

    Retourne une liste de ``{"role": "user"|"assistant", "content": str}``,
    vide par défaut.
    """
    if not session_id or resume_request_id:
        return []
    try:
        store = get_session_store()
        if store.get_session(session_id) is None:
            messages = []  # session inconnue : la mémoire inter-sessions
            # (Phase C) pourra tout de même être injectée plus bas.
        else:
            messages = store.get_messages(session_id, limit=500)
    except Exception:  # pragma: no cover - mémoire optionnelle, jamais bloquante
        return []
    kept: list[dict] = []
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
        text = content if isinstance(content, str) else str(content or "")
        if role not in ("user", "assistant") or not text.strip():
            continue
        kept.append({"role": role, "content": text.strip()})
    # Conserve seulement les N dernières paires user/assistant (les plus récents).
    kept = kept[-MAX_SESSION_CONTEXT_TURNS * 2 :]

    # Phase C (flag ``AGENT_CONTEXT``) : budget en jetons avec résumé LLM des
    # tours débordants, puis mémoire inter-sessions si la session est neuve.
    # Sans le flag : comportement historique strictement préservé.
    if not flag("context"):
        return kept
    try:
        import os as _os

        from core.agent_cache import get_agent_runner

        budget = int(_os.getenv("AGENT_CONTEXT_BUDGET_TOKENS", "0")) or (
            DEFAULT_HISTORY_BUDGET_TOKENS
        )
        summarize_fn = lambda transcript: summarize_conversation(
            get_agent_runner().core.llm, transcript
        )  # noqa: E731 - injection paresseuse, jamais appelée si pas de débordement
        optimized, _meta = optimize_history(kept, max_tokens=budget, summarize_fn=summarize_fn)
    except Exception:  # pragma: no cover - mémoire optionnelle, jamais bloquante
        optimized = kept
    if not optimized:
        try:
            note = format_memory_note(store.get_memory("global"))
            if note is not None:
                optimized = [note]
        except Exception:  # pragma: no cover
            pass
    return optimized


def _persist_exchange(
    session_id: Optional[str],
    prompt: str,
    answer: str,
    tool_events: Optional[list[dict]] = None,
) -> None:
    """Journalise l'échange (user + assistant) dans la session demandée.

    Best-effort : une session absente ou une erreur de base ne doivent jamais
    faire échouer le tour de chat lui-même.
    """
    if not session_id:
        return
    try:
        store = get_session_store()
        store.append_message(session_id, "user", prompt)
        store.append_message(session_id, "assistant", answer or "", tool_calls=tool_events)
        # Phase C (flag ``AGENT_CONTEXT``) : mémoire glissante inter-sessions.
        # Résumé déterministe (sans LLM) conservé sous la clé « global » et
        # réinjecté uniquement dans les NOUVELLES sessions (cf.
        # _load_session_history). Best-effort : ne casse jamais le tour.
        if flag("context"):
            previous = store.get_memory("global")
            store.save_memory(
                "global",
                update_memory_summary(previous, prompt, answer or ""),
            )
    except Exception:  # pragma: no cover - persistance optionnelle
        pass


@router.post("/ask/stream")
def ask_stream(request: AskStreamRequest, _: bool = Depends(require_api_key)):
    """Mode Agent en streaming SSE — le temps réel de /api/ai pour l'agent.

    Contrat d'entrée : POST JSON ``AskStreamRequest``.
    Contrat de sortie : flux SSE ->
        data: {"tool_start":  {tool, args}}             appel d'outil annoncé
        data: {"tool_result": {tool, status, summary…}} résultat (ok/error)
        data: {"thinking_delta": "..."}                 réflexion (mode activé)
        data: {"delta": "..."}                          réponse finale, mot à mot
        data: {"final": {...AskResponse...}}            statut du gate + ids
        data: [DONE]

    Réutilisation du schéma éprouvé de /api/ai (queue.Queue + thread worker +
    générateur asynchrone) complété par le journal des runs : chaque appel
    crée une ligne ``running`` dans core/run_store, enrichie au vol via le
    callback d'événements d'outils.
    """
    events: queue.Queue[tuple[str, object]] = queue.Queue()
    run_store = get_run_store()
    run_row = run_store.start_run(
        request.prompt,
        model=(request.model or "").strip() or agent_config()["model"],
        source="ask-stream",
    )
    _audit_log(
        ACT_RUN,
        subject="ask-stream",
        detail={
            "status": "started",
            "model": (request.model or "").strip() or agent_config()["model"],
            "enable_thinking": request.enable_thinking,
        },
        run_id=run_row["id"],
    )

    def _emit_thinking(chunk: str) -> None:
        events.put(("thinking", chunk))

    def _on_tool_event(event: dict) -> None:
        kind = event.get("event", "tool_result")
        events.put((kind, event))
        tool_events.append(event)
        try:
            run_store.append_tool_event(run_row["id"], event)
        except Exception:  # pragma: no cover - le journal ne doit jamais bloquer
            pass

    tool_events: list[dict] = []

    def worker() -> None:
        try:
            result = ask_agent_decision_streaming(
                request.prompt,
                model=request.model or None,
                enable_thinking=request.enable_thinking,
                resume_request_id=request.resume_request_id,
                # Sans « Réflexion », aucun thinking_delta n'est émis.
                on_thinking=_emit_thinking if request.enable_thinking else None,
                on_tool_event=_on_tool_event,
                # Mémoire de session : rejoue les tours précédents en contexte.
                history_messages=_load_session_history(
                    request.session_id, request.resume_request_id
                ),
            )
            answer = result.get("answer") or ""
            for word in _stream_fragments(answer):
                events.put(("delta", word))
                time.sleep(ANSWER_STREAM_CADENCE_SECONDS)
            status_map = {
                "completed": RUN_COMPLETED,
                "awaiting_approval": RUN_AWAITING_APPROVAL,
                "rejected": RUN_REJECTED,
            }
            run_store.finish_run(
                run_row["id"],
                status_map.get(result.get("status", "completed"), RUN_COMPLETED),
                answer_summary=answer[:300],
            )
            _audit_log(
                ACT_RUN,
                subject="ask-stream",
                detail={
                    "status": result.get("status", "completed"),
                    "n_tools": len(tool_events),
                },
                run_id=run_row["id"],
            )
            # Persistance de l'échange dans la session demandée (best-effort),
            # avec la trace complète des événements d'outils observés.
            _persist_exchange(
                request.session_id,
                request.prompt,
                answer,
                tool_events=tool_events or None,
            )
            events.put(
                (
                    "final",
                    {
                        "response": answer,
                        "model": result.get("model"),
                        "status": result.get("status", "completed"),
                        "request_id": result.get("request_id"),
                        "approval": result.get("approval"),
                    },
                )
            )
        except ValueError as exc:
            run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc))
            events.put(("bad_request", str(exc)))
        except HTTPException as exc:  # panne réseau déjà traduite par agent_cache
            run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc.detail))
            events.put(("http_error", exc))
        except Exception as exc:
            run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc))
            events.put(("error", str(exc)))
        finally:
            events.put(("done", None))

    # Thread daemon : l'agent se termine seul même si le client coupe (Stop).
    threading.Thread(target=worker, daemon=True).start()

    # Réception SYNCHRONE du premier événement : une panne précoce (LLM
    # injoignable, resume_request_id invalide) reste une vraie erreur HTTP.
    first_kind, first_payload = events.get()
    if first_kind == "bad_request":
        raise HTTPException(status_code=400, detail=str(first_payload))
    if first_kind == "http_error":
        raise first_payload  # noqa: TRY201 - re-lever l'HTTPException d'origine
    if first_kind == "error":
        raise HTTPException(status_code=502, detail=str(first_payload))

    async def _sse_stream() -> AsyncIterator[str]:
        try:
            if first_kind != "done":
                # Rejoue le premier événement déjà consommé ci-dessus.
                field = "thinking_delta" if first_kind == "thinking" else first_kind
                yield _sse({field: first_payload})

            while True:
                kind, payload = await asyncio.to_thread(events.get)
                if kind == "done":
                    break
                if kind in ("bad_request", "http_error", "error"):
                    detail = payload.detail if kind == "http_error" else str(payload)
                    yield _sse({"error": detail})
                    break
                field = "thinking_delta" if kind == "thinking" else kind
                yield _sse({field: payload})
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            # Le client a interrompu la génération (bouton Stop du chat).
            raise

    return StreamingResponse(
        _sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # désactive le buffering nginx
        },
    )


# --- Approbation humaine (approve / reject) -----------------------------------------


@router.get("/approvals")
def list_approvals(
    status: Optional[str] = None, _: bool = Depends(require_api_key)
):
    """Liste des demandes d'approbation (toutes ou filtrées par statut)."""
    if status is not None and status not in STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Statut inconnu : '{status}'. Valeurs : {', '.join(STATUSES)}",
        )
    return {"approvals": get_approval_store().list(status)}


@router.post("/approvals/{request_id}/approve")
def approve_request(request_id: str, _: bool = Depends(require_api_key)):
    """Valide une demande `pending` → `approved` (l'outil sera exécuté au résumé)."""
    row = get_approval_store().approve(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Demande introuvable : {request_id}")
    if row["status"] != APPROVED:
        raise HTTPException(
            status_code=409,
            detail="Cette demande n'était pas en attente (approbation impossible).",
        )
    _audit_log(
        ACT_APPROVAL,
        subject=row["tool"],
        detail={
            "request_id": request_id,
            "decision": "approved",
            "tool": row["tool"],
            "args_hash": row.get("args_hash", ""),
        },
    )
    return {"status": APPROVED, "approval": row}


@router.post("/approvals/{request_id}/reject")
def reject_request(request_id: str, _: bool = Depends(require_api_key)):
    """Refuse une demande `pending` → rejected (aucune exécution)."""
    row = get_approval_store().reject(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Demande introuvable : {request_id}")
    if row["status"] != REJECTED:
        raise HTTPException(
            status_code=409,
            detail="Demande non en attente (impossible de réfuter).",
        )
    _audit_log(
        ACT_APPROVAL,
        subject=row["tool"],
        detail={
            "request_id": request_id,
            "decision": "rejected",
            "tool": row["tool"],
            "args_hash": row.get("args_hash", ""),
        },
    )
    return {"status": REJECTED, "approval": row}


# --- Journal des exécutions (runs) ----------------------------------------------------


@router.get("/runs")
def list_runs(
    limit: int = 50,
    status: Optional[str] = None,
    tool: Optional[str] = None,
    _: bool = Depends(require_api_key),
):
    """Liste paginée des exécutions de l'agent (les plus récentes d'abord).

    Filtres optionnels : ``status`` (completed/error/awaiting_approval/
    rejected/running) et ``tool`` (nom exact d'un outil présent dans la trace).
    """
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit doit être entre 1 et 200.")
    if status is not None and status not in RUN_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Statut inconnu : '{status}'. Valeurs : {', '.join(RUN_STATUSES)}",
        )
    return {
        "runs": get_run_store().list(limit=limit, status=status, tool=tool),
        "statuses": list(RUN_STATUSES),
    }


@router.get("/runs/{run_id}")
def get_run(run_id: str, _: bool = Depends(require_api_key)):
    """Détail complet d'un run : prompt, statut, chaîne d'outils horodatée."""
    run_row = get_run_store().get(run_id)
    if run_row is None:
        raise HTTPException(status_code=404, detail=f"Run introuvable : {run_id}")
    return run_row


# --- Paramètres de l'agent (persistés en SQLite) ------------------------------------


def _mask_key(key: str) -> str:
    """Masque une clé API pour l'affichage : « sk-or-v1 » -> « sk-or-…abcd »."""
    key = key or ""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}"


def _settings_payload() -> dict:
    """Formate la config effective pour le dashboard (clé jamais en clair)."""
    settings = get_agent_settings()
    values = {key: entry["value"] for key, entry in settings.items()}
    sources = {key: entry["source"] for key, entry in settings.items()}
    api_key = values.pop("openrouter_api_key") or ""
    hf_api_key = values.pop("hf_api_key") or ""
    return {
        "settings": {
            **values,
            "has_openrouter_api_key": bool(api_key),
            "openrouter_api_key_masked": _mask_key(api_key),
            "has_hf_api_key": bool(hf_api_key),
            "hf_api_key_masked": _mask_key(hf_api_key),
        },
        "sources": sources,
    }


@router.get("/settings")
def read_agent_settings(_: bool = Depends(require_api_key)):
    """Paramètres effectifs de l'agent : valeurs + source (sqlite/env/default).

    La clé OpenRouter n'est JAMAIS renvoyée en clair : uniquement un indicateur
    ``has_openrouter_api_key`` et une version masquée pour confirmation visuelle.
    """
    return _settings_payload()


@router.put("/settings")
def update_agent_settings(
    update: AgentSettingsUpdate, _: bool = Depends(require_api_key)
):
    """Sauvegarde partielle des paramètres puis rechargement immédiat de l'agent.

    Seuls les champs fournis (non ``null``) sont écrits. Une chaîne vide sur un
    champ texte revient à réinitialiser ce paramètre au défaut serveur.
    Effet immédiat (reload du runner) : aucun redémarrage requis.
    """
    values = update.model_dump(exclude_none=True)
    try:
        save_agent_settings(values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    _audit_log(
        ACT_CONFIG,
        subject="settings",
        detail={"written_keys": sorted(values)},
    )

    payload = _settings_payload()
    # Rechargement immédiat ; une config encore incomplète (ex: openrouter
    # sans clé) n'est PAS une erreur de sauvegarde : on renvoie un avertissement
    # que l'UI affiche, l'utilisateur complète ensuite.
    try:
        reload_agent_runner()
    except HTTPException as exc:
        payload["warning"] = f"Paramètres enregistrés, mais agent non rechargé : {exc.detail}"
        payload["reload_ok"] = False
    else:
        payload["reload_ok"] = True
    payload["written_keys"] = sorted(values)
    return payload


@router.post("/settings/test")
def test_agent_connectivity(
    request: ConnectivityTestRequest, _: bool = Depends(require_api_key)
):
    """Sonde le provider demandé (valeurs fournies ou config effective).

    Retourne ``{"ok": bool, "detail": str}`` — aucune exception n'est levée :
    le résultat d'échec est un corps 200 que l'UI affiche comme tel.
    """
    cfg = agent_config()
    provider = (
        (request.provider or "").strip().lower() or cfg["provider"]
    )
    if provider == "openrouter":
        url = (request.openrouter_url or "").strip() or cfg["openrouter_url"]
        chat_url = _openrouter_chat_url(url)
        base = chat_url[: -len("/chat/completions")].rstrip("/")
        probe_url = f"{base}/models"
        api_key = (
            request.openrouter_api_key or cfg["openrouter_api_key"] or ""
        ).strip()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        success_detail = f"OpenRouter joignable sur {probe_url}"
        hint = ""
    elif provider == "hf":
        url = (request.hf_url or "").strip() or cfg["hf_url"]
        chat_url = _hf_chat_url(url)
        base = chat_url[: -len("/chat/completions")].rstrip("/")
        probe_url = f"{base}/models"
        api_key = (
            request.hf_api_key or cfg["hf_api_key"] or ""
        ).strip()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        success_detail = f"Hugging Face joignable sur {probe_url}"
        hint = ""
    else:
        base_url = (request.ollama_url or "").strip() or cfg["ollama_url"]
        marker = base_url.find("/api/")
        root = base_url[:marker] if marker != -1 else base_url.rstrip("/")
        probe_url = f"{root}/api/tags"
        headers = None
        success_detail = f"Ollama joignable sur {probe_url}"
        hint = " Vérifiez qu'Ollama tourne."

    try:
        response = requests.get(
            probe_url, headers=headers, timeout=CONNECTIVITY_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        response.json()
    except requests.exceptions.Timeout:
        outcome = {
            "ok": False,
            "detail": f"Délai dépassé ({CONNECTIVITY_TIMEOUT_SECONDS:.0f}s) sur {probe_url}.",
        }
    except requests.exceptions.ConnectionError:
        outcome = {"ok": False, "detail": f"Injoignable : {probe_url}.{hint}"}
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        extra = " Clé API invalide ?" if status == 401 else hint
        outcome = {"ok": False, "detail": f"HTTP {status} sur {probe_url}.{extra}"}
    except ValueError:
        outcome = {"ok": False, "detail": f"Réponse illisible de {probe_url}."}
    else:
        outcome = {"ok": True, "detail": success_detail}

    _audit_log(
        ACT_CONNECT,
        subject=provider,
        detail={"provider": provider, "probe_url": probe_url, "ok": outcome["ok"]},
    )
    return outcome


# --- Audit & conformité (Phase A, flag AGENT_AUDIT) ------------------------------------


@router.get("/audit")
def list_audit(
    action: Optional[str] = None,
    subject: Optional[str] = None,
    actor: Optional[str] = None,
    run_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    _: bool = Depends(require_api_key),
):
    """Journal d'audit paginé (conformité). Requiert le flag AGENT_AUDIT.

    Filtres AND sur ``action`` / ``subject`` / ``actor`` / ``run_id``. Sans le
    flag, renvoie une réponse 403 explicite (fonctionnalité désactivée).
    """
    if not flag("audit"):
        raise HTTPException(
            status_code=403,
            detail="Journal d'audit désactivé (flag AGENT_AUDIT inactif).",
        )
    return get_audit_store().query(
        action=action, subject=subject, actor=actor, run_id=run_id,
        limit=limit, offset=offset,
    )


@router.get("/features")
def agent_features(_: bool = Depends(require_api_key)):
    """État des flags d'enhancement (rollout incrémental)."""
    flags = {name: flag(name) for name in (
        "reliability", "audit", "tool_analytics", "context", "copilot", "websocket",
    )}
    return {"features": flags, "active": active_features()}


# --- WebSocket bidirectionnel (Phase E, flag AGENT_WEBSOCKET) -------------------------
#
# Canal temps réel duplex, miroir WebSocket de POST /api/agent/ask/stream :
#
#   client -> serveur : {"action": "ask", "prompt": "...", "session_id"?,
#                        "enable_thinking"?, "model"?, "resume_request_id"?}
#                       {"action": "approve"|"reject", "request_id": "..."}
#                       {"action": "ping"}
#   serveur -> client : {"event": "hello", "model"}
#                       {"event": "thinking", "text"}       réflexion
#                       {"event": "tool_start"|"tool_result", ...}
#                       {"event": "delta", "text"}          réponse mot à mot
#                       {"event": "final", ...}             statut du gate + ids
#                       {"event": "approval_done", ...}     validation humaine
#                       {"event": "error", "detail"}        erreur protocolaire
#
# Auth : les navigateurs ne peuvent pas poser de header sur un WebSocket, le
# jeton (même secret que l'API) est donc passé en query param `?token=`.


@router.websocket("/ws")
async def agent_ws(websocket: WebSocket):
    """Canal Agent bidirectionnel (requiert le flag AGENT_WEBSOCKET)."""
    if not flag("websocket"):
        await websocket.close(code=1008, reason="Fonction désactivée (AGENT_WEBSOCKET)")
        return
    if websocket.query_params.get("token") != _get_api_key():
        await websocket.close(code=1008, reason="Jeton invalide")
        return
    await websocket.accept()
    await websocket.send_json({"event": "hello", "model": agent_config()["model"]})
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    raise ValueError("objet attendu")
            except ValueError:
                await websocket.send_json({
                    "event": "error",
                    "detail": "Message JSON objet attendu.",
                })
                continue
            action = str(msg.get("action", "")).lower()
            if action == "ping":
                await websocket.send_json({"event": "pong"})
            elif action in ("approve", "reject"):
                await _ws_handle_approval(websocket, msg, action)
            elif action == "ask":
                await _ws_run_agent(websocket, msg)
            else:
                await websocket.send_json({
                    "event": "error",
                    "detail": f"Action inconnue : {action or '(absente)'}",
                })
    except WebSocketDisconnect:
        return


async def _ws_handle_approval(websocket: WebSocket, msg: dict, action: str) -> None:
    """Valide/refuse une demande d'approbation directement sur le canal WS."""
    request_id = str(msg.get("request_id") or "")
    store = get_approval_store()
    row = store.approve(request_id) if action == "approve" else store.reject(request_id)
    if row is None:
        await websocket.send_json({
            "event": "error",
            "detail": f"Demande introuvable : {request_id}",
        })
        return
    _audit_log(
        ACT_APPROVAL,
        subject=row.get("tool", ""),
        detail={
            "request_id": request_id,
            "decision": "approved" if action == "approve" else "rejected",
            "channel": "ws",
        },
    )
    await websocket.send_json({
        "event": "approval_done",
        "decision": "approved" if action == "approve" else "rejected",
        "approval": row,
    })


async def _ws_run_agent(websocket: WebSocket, msg: dict) -> None:
    """Exécute un tour d'agent en diffusant les événements en temps réel."""
    prompt = str(msg.get("prompt") or "").strip()
    if not prompt:
        await websocket.send_json({
            "event": "error",
            "detail": "'prompt' requis pour l'action 'ask'.",
        })
        return
    enable_thinking = bool(msg.get("enable_thinking"))
    resume_request_id = msg.get("resume_request_id") or None
    session_id = msg.get("session_id") or None
    model = str(msg.get("model") or "").strip() or None

    events: "queue.Queue[tuple]" = queue.Queue()
    run_store = get_run_store()
    run_row = run_store.start_run(
        prompt, model=model or agent_config()["model"], source="ws"
    )
    tool_events: list[dict] = []

    def on_thinking(chunk: str) -> None:
        events.put(("thinking", {"event": "thinking", "text": chunk}))

    def on_tool_event(event: dict) -> None:
        events.put(("tool", event))
        tool_events.append(event)
        try:
            run_store.append_tool_event(run_row["id"], event)
        except Exception:  # pragma: no cover - le journal ne doit jamais bloquer
            pass

    def worker() -> None:
        try:
            result = ask_agent_decision_streaming(
                prompt,
                model=model,
                enable_thinking=enable_thinking,
                resume_request_id=resume_request_id,
                on_thinking=on_thinking if enable_thinking else None,
                on_tool_event=on_tool_event,
                history_messages=_load_session_history(
                    session_id, resume_request_id
                ),
            )
            answer = result.get("answer") or ""
            for word in _stream_fragments(answer):
                events.put(("delta", {"event": "delta", "text": word}))
            status_map = {
                "completed": RUN_COMPLETED,
                "awaiting_approval": RUN_AWAITING_APPROVAL,
                "rejected": RUN_REJECTED,
            }
            run_store.finish_run(
                run_row["id"],
                status_map.get(result.get("status", "completed"), RUN_COMPLETED),
                answer_summary=answer[:300],
            )
            _persist_exchange(
                session_id, prompt, answer, tool_events=tool_events or None
            )
            events.put((
                "final",
                {
                    "event": "final",
                    "response": answer,
                    "model": result.get("model"),
                    "status": result.get("status", "completed"),
                    "request_id": result.get("request_id"),
                },
            ))
        except Exception as exc:  # pragma: no cover - défensif
            try:
                run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc))
            except Exception:
                pass
            events.put((
                "final",
                {
                    "event": "final",
                    "response": "",
                    "status": "error",
                    "detail": f"{type(exc).__name__}: {exc}",
                },
            ))
        finally:
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()
    loop = asyncio.get_event_loop()
    while True:
        item = await loop.run_in_executor(None, events.get)
        if item is None:
            break
        await websocket.send_json(item[1])
        
# --- Orchestration multi-agents (superviseur / workers) --------------------


@router.post("/multi/ask")
def multi_ask(
    request: MultiAskRequest, _: bool = Depends(require_api_key)
):
    """Orchestration multi-agents (mode bloquant).

    Exécute plan → dispatch → synthèse via le superviseur et renvoie le
    contrat de sortie stable de l'orchestrateur :
    ``{"status", "final_answer", "plan", "workers", "unexecuted", "thinking"}``.
    ``unexecuted`` liste explicitement les sous-tâches non exécutées (jamais
    noyées dans la réponse).
    """
    result = ask_multi_agent(
        request.prompt,
        model=request.model or None,
        parallel=request.parallel,
    )
    return result


@router.post("/multi/ask/stream")
def multi_ask_stream(
    request: MultiAskRequest, _: bool = Depends(require_api_key)
):
    """Orchestration multi-agents en streaming SSE.

    Événements SSE (``event:`` + ``data:``) :
      - ``agent.plan``          plan validé (sous-tâches assignées)
      - ``agent.worker.start``  une sous-tâche commence
      - ``agent.worker.tool``   appel/retour d'outil (observabilité)
      - ``agent.worker.error``  sous-tâche en erreur (observabilité)
      - ``agent.worker.result`` résultat d'une sous-tâche
      - ``agent.worker.approval`` sous-tâche bloquée sur une validation
        humaine (porte ``request_id`` + ``approval`` : la carte
        Approuver/Refuser du front est déclenchée puis la sous-tâche est
        relancée via ``resume_request_id``)
      - ``agent.synthesizing``  phase de synthèse
      - ``agent.done``          réponse finale
      - ``agent.error``         erreur globale (plan invalide, abort)

    ``mode`` : « full » (tous) ou « compact » (plan / worker.start /
    worker.result / done / error — les événements d'observabilité sont
    filtrés pour ne pas étouffer un front simple).

    Réutilise le schéma éprouvé (queue.Queue + thread worker + générateur
    async) et garde la traduction des pannes réseau en erreur HTTP précoce.
    """
    events: queue.Queue = queue.Queue()
    compact = (request.mode or "full").strip().lower() == "compact"

    def _emit(event_type: str, data: dict) -> None:
        if compact and event_type in _MULTI_OBSERVABILITY_EVENTS:
            return  # observabilité : aucun envoi au front en mode compact
        events.put((event_type, data))

    def worker() -> None:
        try:
            result = ask_multi_agent_streaming(
                request.prompt,
                model=request.model or None,
                parallel=request.parallel,
                on_event=_emit,
            )
            events.put(("agent.done", result))
        except HTTPException as exc:  # panne réseau déjà traduite
            events.put(("agent.error", {"message": str(exc.detail)}))
        except Exception as exc:  # pragma: no cover
            events.put(("agent.error", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
            events.put(("__done__", None))

    threading.Thread(target=worker, daemon=True).start()

    # Réception SYNCHRONE du premier événement : une panne précoce reste une
    # vraie erreur HTTP (même politique que /ask/stream).
    first_kind, first_payload = events.get()
    if first_kind == "agent.error":
        raise HTTPException(
            status_code=502, detail=first_payload.get("message", "Erreur multi-agents.")
        )
    if first_kind == "__done__":
        raise HTTPException(status_code=502, detail="Aucun événement produit.")

    async def _sse_stream() -> AsyncIterator[str]:
        try:
            # Rejoue le premier événement déjà consommé (sauf le sentinelle).
            if first_kind != "__done__":
                yield f"event: {first_kind}\n" + _sse(first_payload)
            while True:
                kind, payload = await asyncio.to_thread(events.get)
                if kind == "__done__":
                    break
                if kind == "agent.error":
                    yield f"event: {kind}\n" + _sse(payload)
                    break
                yield f"event: {kind}\n" + _sse(payload)
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            # Le client a interrompu la génération (bouton Stop du chat).
            raise

    return StreamingResponse(
        _sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # désactive le buffering nginx
        },
    )
