# project/api/routes/agent.py

"""Endpoints de l'agent IA intégrés au package api.

.. deprecated:: v3.0.0 (MCP-First, S7 — tâche 20 : docs/mcp/IMPLEMENTATION_PLAN.md)

    La surface d'entrée privilégiée est désormais **MCP** :
    ``POST /mcp/sse`` (transport streamable HTTP) et ``thinktuning-mcp``
    (transport stdio) — serveur ``app/infrastructure/mcp/``. Ce module reste
    monté UNIQUEMENT comme adaptateur strangler : les délégations v1
    (``api/routes/v1/agent.py``) appellent encore ces handlers (source unique
    jusqu'à migration complète).

    Dépréciation active (verrouillée par ``tests/test_mcp_first.py``) :

    - un :class:`DeprecationWarning` est émis à l'import du module ;
    - chaque réponse HTTP du router porte les en-têtes ``Deprecation: true``,
      ``Sunset`` (RFC 8594) et ``Warning: 299`` ;
    - feature flag **``MCP_FIRST=true``** : la surface passe en mode
      LECTURE SEULE — tout endpoint mutant (POST/PUT/DELETE) répond 405 avec
      le code ``mcp_first_read_only`` et renvoie vers MCP. Exception
      documentée : l'approbation humaine (approve/reject) reste disponible,
      c'est le canal qui débloque les runs MCP ``pending_approval``.

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
import functools
import json
import os
import queue
import threading
import time
import warnings
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar, cast

import requests
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.dependencies.auth import require_api_key, ws_is_authorized

# Nouveau noyau agentique (app/) — activé par le flag AGENT_NEW_CORE.
from app.agent.core import RunStatus
from app.agent.factory import build_agent_core, new_core_enabled
from app.agent.settings import agent_flag, get_agent_config
from app.application.agent_settings_usecase import (
    get_effective_settings,
    update_settings,
)

# Use-cases (couche application) : la logique métier des runs vit ici,
# les routes ci-dessous ne sont plus que des adaptateurs HTTP minces.
from app.application.ask_usecase import run_ask_core
from app.application.multi_agent_usecase import run_multi_agent, run_multi_agent_streaming
from app.application.run_lifecycle import (
    core_api_status,
    core_store_status,
    core_tool_events,
    create_approval_request,
    make_approval_gateway,
    resolve_resume_hash,
)
from app.application.session_memory import load_session_history, persist_exchange
from app.domain.entities.plan import Intent
from app.domain.errors import AgentRunError
from app.infrastructure.events.in_memory import InMemoryEventBus
from app.infrastructure.legacy_approval_store import build_approval_store
from app.infrastructure.legacy_multi_agent_adapter import build_multi_agent_orchestrator
from app.infrastructure.legacy_settings_adapter import build_settings_port
from core.agent_cache import (
    REQUIRED_ARGS,
    TOOL_META,
    TOOLS,
    _hf_chat_url,
    _lm_studio_chat_url,
    _openrouter_chat_url,
    agent_config,
    reload_agent_runner,
)
from core.approval_store import (
    APPROVED,
    REJECTED,
    STATUSES,
    get_approval_store,
)
from core.audit_store import (  # Phase A (audit / conformité)
    ACT_APPROVAL,
    ACT_CONNECT,
    ACT_RUN,
    ACT_TOOL,
    get_audit_store,
)


def _flag(name: str) -> bool:
    """Lit un feature flag depuis la configuration de l'agent (module IHM).

    Source : ``app.agent.settings.agent_flag`` — base MongoDB (store IHM) en
    priorité, repli env ``AGENT_<NOM>``. Lecture à l'appel : les tests qui
    basculent un flag par ``monkeypatch.setenv`` n'ont plus besoin de vider un
    cache.
    """
    return agent_flag(name)


def _active_features() -> list[str]:
    """Liste ordonnée des flags activés (compatibilité avec l'ancien ``active_features()``)."""
    return [name for name, active in get_agent_config().active_flags().items() if active]
from core.flow_store import (
    AWAITING_APPROVAL as FLOW_AWAITING_APPROVAL,
)
from core.flow_store import (
    COMPLETED as FLOW_COMPLETED,
)
from core.flow_store import (
    ERROR as FLOW_ERROR,
)
from core.flow_store import (
    REJECTED as FLOW_REJECTED,
)
from core.flow_store import (
    STATUSES as FLOW_STATUSES,
)
from core.flow_store import (
    get_flow_store,
)
from core.run_store import (
    ERROR as RUN_ERROR,
)
from core.run_store import (
    STATUSES as RUN_STATUSES,
)
from core.run_store import (
    get_run_store,
)
from ia.copilot.feedback import get_feedback_store  # Phase D (copilot)
from ia.copilot.suggestions import (  # Phase D (copilot)
    complete_text,
    suggest_for_context,
)
from ia.tools.plugin import loaded_plugins  # Phase B (plugins)
from ia.tools.registry import (  # SCRUM-99 (tools personnalisés)
    ToolRegistryError,
    get_global_registry,
)
from ia.tools.tool_analytics import get_stats, record_call  # Phase B (analytique)
from ia.tools.tool_discovery import suggest_tools  # Phase B (découverte)
from ia.tools.tool_schema import validate_tool_definition  # SCRUM-99 (standard v1)

# --- Dépréciation de la surface HTTP legacy (S7 — MCP-First, tâche 20) -------

#: Date cible de retrait de la surface legacy (en-tête ``Sunset``, RFC 8594).
DEPRECATION_SUNSET = "Sat, 31 Dec 2026 23:59:59 GMT"

#: Notice courte injectée dans l'en-tête ``Warning: 299`` des réponses.
DEPRECATION_WARNING = (
    "Surface HTTP /api/agent deprecated : mutations via MCP (POST /mcp/sse)"
)

#: Notice complète émise en :class:`DeprecationWarning` à l'import du module.
DEPRECATION_NOTICE = (
    "api/routes/agent.py est la surface HTTP legacy de l'agent IA (v3.0.0 "
    "MCP-First). La surface d'entrée privilégiée est MCP (POST /mcp/sse, "
    "transport stdio `thinktuning-mcp`) ; ce module n'est conservé que comme "
    "adaptateur strangler des délégations v1. Activez MCP_FIRST=true pour "
    "geler la surface en lecture seule."
)

# Marquage @deprecated (tâche 20) : un avertissement à l'import — visible avec
# ``python -W`` / ``-W error::DeprecationWarning`` en CI, et vérifié par
# ``tests/test_mcp_first.py``.
warnings.warn(DEPRECATION_NOTICE, DeprecationWarning, stacklevel=2)

_F = TypeVar("_F", bound=Callable[..., Any])


async def _deprecated_surface_headers(request: Request, response: Response) -> None:
    """Injecte les en-têtes de dépréciation sur chaque réponse HTTP du router.

    Dépendance router-level : les clients HTTP directs découvrent la
    dépréciation sans lire la documentation (RFC 8594 ``Deprecation`` +
    ``Sunset`` + ``Warning: 299``). Le canal WebSocket (``/ws``) est
    court-circuité : aucun en-tête à poser, et dans ce contexte Starlette
    remplit ``response`` avec ``None``.
    """
    if request.scope.get("type") != "http":
        return  # canal WebSocket : rien à déprécier côté HTTP
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = DEPRECATION_SUNSET
    response.headers["Warning"] = f'299 - "{DEPRECATION_WARNING}"'


def writable_endpoint(func: _F) -> _F:
    """Garde « lecture seule » du mode MCP-First (tâche 20).

    Décore les endpoints MUTANTS de la surface legacy : quand le réglage
    ``MCP_FIRST=true`` est actif (persisté dans le module de configuration de
    l'IHM, ou env en repli), l'appel est refusé AVANT toute exécution — 405
    avec le code ``mcp_first_read_only`` et un renvoi vers la surface MCP
    (``POST /mcp/sse``). Les délégations v1 héritent du garde par appel direct
    des handlers (strangler, source unique).

    Exception documentée : l'approbation humaine (``/approvals/{id}/approve``,
    ``/reject``) n'est PAS décorée — c'est le canal qui débloque les runs MCP
    en ``pending_approval`` (policy APPROVE) ; le bloquer interdirait tout
    run MCP à risque de se terminer.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if get_agent_config().mcp_first:
            raise HTTPException(
                status_code=405,
                detail={
                    "code": "mcp_first_read_only",
                    "message": (
                        "API HTTP legacy en lecture seule (MCP_FIRST=true) : "
                        "les mutations passent par MCP (POST /mcp/sse)."
                    ),
                },
            )
        return func(*args, **kwargs)

    return cast(_F, wrapper)


router = APIRouter(
    prefix="/api/agent",
    tags=["Agent IA"],
    dependencies=[Depends(_deprecated_surface_headers)],
)

# Timeout (secondes) des sondes de connectivité du bouton « Tester ».
CONNECTIVITY_TIMEOUT_SECONDS = 8.0


def _audit_log(action: str, subject: str = "", detail: dict | None = None, **kw):
    """Journal d'audit activé par le flag ``AGENT_AUDIT`` (Phase A).

    No-op (retour None) quand le flag est inactif : zéro impact sur le
    comportement historique. ``kw`` peut porter actor/ip/request_id/run_id.
    """
    if not _flag("audit"):
        return None
    store = get_audit_store()
    return store.log(action, subject=subject, detail=detail, **kw)


# --- Schémas Pydantic -------------------------------------------------------------

class AskRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Instruction envoyée à l'agent.")
    resume_request_id: str | None = Field(
        None,
        description="Relance une tâche en attente : id donné par une réponse "
        "« awaiting_approval » après validation humaine (approve).",
    )
    session_id: str | None = Field(
        None,
        description="Session de conversation (core/session_store) où journaliser "
        "l'échange ; absent : aucune persistance côté serveur.",
    )
    model: str | None = Field(
        None, max_length=100,
        description="Modèle LLM ; absent/vide = défaut serveur "
        "(parité AskStreamRequest — le sélecteur du chat était auparavant "
        "ignoré sur ce chemin).",
    )
    enable_thinking: bool = Field(
        False, description="Mode « Réflexion » (parité AskStreamRequest).",
    )


class AskStreamRequest(BaseModel):
    """Corps de POST /api/agent/ask/core/stream (mode Agent temps réel).

    Même contrat que ``AskRequest`` avec en plus la sélection du modèle LLM
    et du mode « Réflexion » (sélecteurs de l'en-tête du chat).
    """

    prompt: str = Field(..., min_length=1, description="Instruction envoyée à l'agent.")
    resume_request_id: str | None = Field(None)
    model: str | None = Field(
        None, max_length=100, description="Modèle LLM ; absent/vide = défaut serveur."
    )
    enable_thinking: bool = Field(False, description="Mode « Réflexion ». ")
    session_id: str | None = Field(None, description="Conversation cible (persistance).")


class MultiAskRequest(BaseModel):
    """Corps de l'orchestration multi-agents (superviseur/workers)."""

    prompt: str = Field(..., min_length=1, description="Tâche globale soumise au superviseur.")
    model: str | None = Field(
        None, max_length=100, description="Modèle LLM ; absent/vide = défaut serveur."
    )
    parallel: bool = Field(
        True, description="Exécution parallèle des sous-tâches INDÉPENDANTES "
        "(défaut : activé — les dépendances déclarées restent séquentielles)."
    )
    resume_request_id: str | None = Field(
        None,
        description="REPRISE NATIVE multi-agents : relance l'orchestration "
        "interrompue sur une validation humaine. L'action approuvée est "
        "rejouée DANS le même worker (empreinte SHA-256 revérifiée), puis la "
        "synthèse finale intègre l'ensemble des résultats. Ne passe JAMAIS "
        "par le noyau mono-agent.",
    )
    enable_thinking: bool = Field(
        False, description="Mode « Réflexion » des workers (agent.worker.thinking)."
    )
    mode: str = Field(
        "full",
        description="Granularité du streaming SSE : « full » (tous événements) "
        "ou « compact » (plan / worker.* / done — les événements "
        "d'observabilité tool / synthesizing sont filtrés). La réflexion des "
        "workers (agent.worker.thinking) passe dans les deux modes : elle est "
        "requise par l'IHM dès que enable_thinking est actif.",
    )


# Événements « UX » : nécessaires au rendu, émis dans les deux modes.
_MULTI_UX_EVENTS = {
    "agent.plan",
    "agent.resuming",
    "agent.worker.start",
    "agent.worker.result",
    # Erreur d'un worker : le front (ChatWindow, case agent.worker.error)
    # clôture la ligne de trace du worker — la filtrer en compact laisserait
    # ce worker « running » à l'écran jusqu'à agent.done (SCRUM-101).
    "agent.worker.error",
    "agent.worker.approval",
    "agent.done",
    "agent.error",
}
# Événements « observabilité » : filtrés hors du mode « compact ».
#
# NB : « agent.worker.thinking » n'y figure PAS volontairement. La réflexion
# est une donnée d'INTERFACE (bloc « Réflexion en cours » du chat quand le
# mode « Réflexion » est activé) et son émission est déjà conditionnée à
# ``enable_thinking`` en amont (orchestrateur, thinking_hook) : aucune trace
# n'est émise sans opt-in explicite. La classer « observabilité » rendait le
# mode Réflexion muet en multi-agents (SCRUM-101).
_MULTI_OBSERVABILITY_EVENTS = {
    "agent.worker.tool",
    "agent.synthesizing",
    # SCRUM-99 : pipeline des tools personnalisés (observabilité).
    "agent.tool.proposed",
    "agent.tool.reviewed",
}


class AskResponse(BaseModel):
    response: str
    model: str
    status: str = "completed"
    request_id: str | None = None
    approval: dict | None = None


class ToolInfo(BaseModel):
    name: str
    required_args: list[str]
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolRunRequest(BaseModel):
    tool: str = Field(..., description="Nom de l'outil (ex: 'add', 'write_file').")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments de l'outil.")


class CustomToolCreateRequest(BaseModel):
    """Enregistrement d'un tool personnalisé (SCRUM-99, phase 1).

    ``definition`` suit le standard ``thinktuning.tool/v1`` (name,
    description, required_args, parameters, safety…). ``code`` est
    l'implémentation Python : elle DOIT définir une fonction du même nom que
    le tool. Source humaine authentifiée (flag + API key + audit) :
    l'orchestrateur, lui, n'enregistre jamais un tool de lui-même.
    """

    definition: dict[str, Any] = Field(
        ..., description="Définition thinktuning.tool/v1 complète du tool.",
    )
    code: str = Field(
        ..., min_length=1,
        description="Implémentation Python : doit définir une fonction du "
        "même nom que le tool.",
    )
    owner: str = Field("api", max_length=120, description="Société/origine du tool.")
    overwrite: bool = Field(
        False, description="Remplace un tool DYNAMIQUE existant (jamais un natif).",
    )
    allow_auto_approval: bool = Field(
        False,
        description="Honore la déclaration « safety » (auto-approbation) au "
        "lieu de forcer « manual ». Réservé aux sources humaines de confiance.",
    )


class AgentSettingsUpdate(BaseModel):
    """Mise à jour partielle des paramètres de l'agent IA.

    Champ absent ou ``null`` : inchangé. Chaîne vide pour les champs texte :
    retour à la valeur par défaut du serveur.

    SCRUM-138 : les réglages déplacés de ``app/config/settings.py`` (budgets,
    niveau de log, surface MCP, feature flags) sont désormais des clés de ce
    module de configuration IHM — stockées dans la base MongoDB et chargées
    à chaque lecture.
    """

    provider: str | None = Field(
        None, description="« ollama », « openrouter », « hf » ou « lm_studio »."
    )
    model: str | None = Field(None, max_length=200)
    ollama_url: str | None = Field(None, max_length=500)
    openrouter_url: str | None = Field(None, max_length=500)
    openrouter_api_key: str | None = Field(None, max_length=300)
    hf_url: str | None = Field(None, max_length=500)
    hf_api_key: str | None = Field(None, max_length=300)
    lm_studio_url: str | None = Field(None, max_length=500)
    timeout_seconds: float | None = Field(None, ge=10, le=3600)
    context_length: int | None = Field(None, ge=512, le=131072)
    temperature: float | None = Field(None, ge=0, le=2)
    # Défauts persistés du formulaire d'entraînement ML.
    train_max_per_lang: int | None = Field(None, ge=1, le=1000000)
    train_augment_fraction: float | None = Field(None, ge=0, le=1)
    train_variants_per_example: int | None = Field(None, ge=1, le=100)
    train_use_back_translation: bool | None = None
    train_epochs: int | None = Field(None, ge=1, le=100)
    train_batch_size: int | None = Field(None, ge=1, le=1024)
    train_num_workers: int | None = Field(None, ge=0, le=128)
    train_max_length: int | None = Field(None, ge=8, le=4096)
    train_learning_rate: float | None = Field(None, gt=0, le=1)
    train_weight_decay: float | None = Field(None, ge=0, le=1)
    train_warmup_ratio: float | None = Field(None, ge=0, le=1)
    train_device: str | None = Field(None, pattern="^(auto|cpu|cuda)$")
    # Déplacés de app/config/settings.py (SCRUM-138) : budgets & garde-fous.
    max_llm_rounds: int | None = Field(
        None, ge=1, le=50, description="Rounds LLM max par run."
    )
    max_tool_calls: int | None = Field(
        None, ge=1, le=200, description="Appels d'outils max par run."
    )
    # Observabilité : niveau du logger « thinktuning.agent ».
    log_level: str | None = Field(
        None, description="« DEBUG », « INFO », « WARNING » ou « ERROR »."
    )
    # Surface MCP.
    mcp_first: bool | None = Field(
        None, description="MCP-First : surface HTTP legacy de l'agent en read-only."
    )
    mcp_auth_required: bool | None = Field(
        None, description="Auth X-API-Key obligatoire sur POST /mcp/sse."
    )
    # Feature flags (convention AGENT_<NOM> historique, désormais persistés).
    flag_reliability: bool | None = None
    flag_audit: bool | None = None
    flag_tool_analytics: bool | None = None
    flag_context: bool | None = None
    flag_copilot: bool | None = None
    flag_websocket: bool | None = None
    flag_multi_agent: bool | None = None
    flag_custom_tools: bool | None = None
    flag_new_core: bool | None = None
    flag_llm_v2: bool | None = None


class ConnectivityTestRequest(BaseModel):
    """Sonde de connectivité ; champs absents -> valeurs effectives courantes."""

    provider: str | None = None
    ollama_url: str | None = None
    openrouter_url: str | None = None
    openrouter_api_key: str | None = None
    hf_url: str | None = None
    hf_api_key: str | None = None
    lm_studio_url: str | None = None


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
@writable_endpoint
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
        raise HTTPException(
            status_code=400, detail=f"Arguments invalides pour {tool} : {exc}"
        ) from exc

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
    if not _flag("tool_analytics"):
        raise HTTPException(status_code=404, detail="Fonction désactivée (AGENT_TOOL_ANALYTICS)")
    return {"query": q, "suggestions": suggest_tools(q, k=k)}


# --- Tools personnalisés (SCRUM-99, flag ``AGENT_CUSTOM_TOOLS_API``) -----------

ENV_TOOL_REGISTERED = "agent.tool.registered"  # via event_bus global
ENV_TOOL_REMOVED = "agent.tool.removed"


@router.get("/tools/custom")
def list_custom_tools(_: bool = Depends(require_api_key)):
    """Liste les tools DYNAMIQUES enregistrés (état runtime inclus)."""
    if not _flag("custom_tools"):
        raise HTTPException(
            status_code=404, detail="Fonction désactivée (AGENT_CUSTOM_TOOLS_API)",
        )
    registry = get_global_registry()
    tools = []
    for rt in registry.list_registered(dynamic_only=True):
        tools.append({
            "name": rt.name,
            "definition": rt.definition,
            "approval": rt.approval,
            "enabled": rt.enabled,
            "experimental": rt.experimental,
            "owner": rt.owner,
            "registered_at": rt.registered_at,
            "source_file": rt.source_file,
        })
    return {"tools": tools, "max_dynamic_tools": registry.max_dynamic_tools}


@router.post("/tools/custom", status_code=201)
@writable_endpoint
def create_custom_tool(
    request: CustomToolCreateRequest, _: bool = Depends(require_api_key),
):
    """Enregistre un tool personnalisé (décision HUMAINE — fail-closed).

    Le planner ne s'auto-équipe JAMAIS : les propositions relues « approve »
    par le reviewer sont finalisées ICI (définition + code), derrière le flag
    ``AGENT_CUSTOM_TOOLS_API`` et l'authentification API. Par défaut le tool
    dynamique est forcé « manual » (validation humaine de chaque appel) ;
    ``allow_auto_approval=true`` (source humaine de confiance) honore la
    déclaration « safety » de la définition.
    """
    if not _flag("custom_tools"):
        raise HTTPException(
            status_code=404, detail="Fonction désactivée (AGENT_CUSTOM_TOOLS_API)",
        )
    definition = request.definition
    ok, errors = validate_tool_definition(definition)
    if not ok:
        raise HTTPException(
            status_code=422,
            detail={"message": "Définition invalide.", "errors": errors},
        )
    name = str(definition.get("name", ""))
    registry = get_global_registry()
    if registry.has_native(name):
        raise HTTPException(
            status_code=409,
            detail=f"« {name} » est un tool natif du registre (non écrasable).",
        )
    if registry.has_tool(name) and not request.overwrite:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Le tool dynamique « {name} » existe déjà "
                "(overwrite=true pour le remplacer)."
            ),
        )

    # Implémentation : le code fourni définit une fonction `name`. Source
    # humaine authentifiée (flag + API key + audit) — niveau de confiance
    # d'un plugin installé manuellement.
    namespace: dict[str, Any] = {"__name__": f"custom_tool_{name}"}
    try:
        exec(  # noqa: S102 — see trust note above
            compile(request.code, f"<custom-tool:{name}>", "exec"), namespace,
        )
    except SyntaxError as exc:
        raise HTTPException(status_code=422, detail=f"Code invalide (syntaxe) : {exc}") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Code invalide (erreur au chargement) : {exc}",
        ) from exc
    func = namespace.get(name)
    if not callable(func):
        raise HTTPException(
            status_code=422,
            detail=f"Le code doit définir une fonction « {name} » callable.",
        )

    try:
        registered = registry.add_tool(
            func,
            definition,
            owner=request.owner or "api",
            overwrite=request.overwrite,
            allow_auto_approval=request.allow_auto_approval,
        )
    except ToolRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _audit_log(
        ACT_TOOL, subject=f"custom_tool:{name}",
        detail={
            "action": "register",
            "owner": registered.owner,
            "approval": registered.approval,
            "dynamic": True,
        },
    )
    try:
        from ia.agent.event_bus import emit as _emit
        _emit(
            ENV_TOOL_REGISTERED,
            name=name, owner=registered.owner, approval=registered.approval,
        )
    except Exception:  # noqa: BLE001 — l'événement ne doit jamais casser l'API
        pass
    return {
        "registered": True,
        "tool": {
            "name": name,
            "approval": registered.approval,
            "definition": registered.definition,
        },
    }


@router.delete("/tools/custom/{name}")
@writable_endpoint
def delete_custom_tool(name: str, _: bool = Depends(require_api_key)):
    """Retire un tool DYNAMIQUE (les tools natifs ne sont jamais retirables)."""
    if not _flag("custom_tools"):
        raise HTTPException(
            status_code=404, detail="Fonction désactivée (AGENT_CUSTOM_TOOLS_API)",
        )
    registry = get_global_registry()
    if not registry.has_tool(name):
        raise HTTPException(status_code=404, detail=f"Tool inconnu : « {name} ».")
    if registry.has_native(name):
        raise HTTPException(
            status_code=409,
            detail=f"« {name} » est un tool natif : retrait interdit.",
        )
    try:
        registry.remove_tool(name)
    except ToolRegistryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit_log(
        ACT_TOOL, subject=f"custom_tool:{name}", detail={"action": "unregister"},
    )
    try:
        from ia.agent.event_bus import emit as _emit
        _emit(ENV_TOOL_REMOVED, name=name)
    except Exception:  # noqa: BLE001
        pass
    return {"removed": True, "name": name}


@router.get("/tools/stats")
def tool_stats(reset: bool = False, _: bool = Depends(require_api_key)):
    """Analytique d'usage des outils (appels, erreurs, durée moyenne).

    Phase B (flag ``AGENT_TOOL_ANALYTICS``) — compteurs in-process depuis le
    démarrage (volatils) ; ``reset=1`` les remet à zéro.
    """
    if not _flag("tool_analytics"):
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
@writable_endpoint
def suggest(request: SuggestRequest, _: bool = Depends(require_api_key)):
    """Suggestions d'outils + squelette d'arguments pour le contexte courant.

    Phase D (flag ``AGENT_COPILOT``) : réutilise la découverte d'outils
    (Phase B) et le boost d'apprentissage issu des acceptations/refus
    enregistrés via ``/suggest/feedback``.
    """
    if not _flag("copilot"):
        raise HTTPException(status_code=404, detail="Fonction désactivée (AGENT_COPILOT)")
    return suggest_for_context(
        messages=request.messages,
        draft=request.draft,
        query=request.query,
        k=request.k,
    )


@router.post("/suggest/feedback")
@writable_endpoint
def suggest_feedback(request: SuggestFeedbackRequest, _: bool = Depends(require_api_key)):
    """Enregistre l'issue d'une suggestion (acceptée / refusée)."""
    if not _flag("copilot"):
        raise HTTPException(status_code=404, detail="Fonction désactivée (AGENT_COPILOT)")
    entry = get_feedback_store().record(
        tool=request.tool,
        accepted=request.accepted,
        session_id=request.session_id,
        suggestion=request.suggestion,
    )
    return {"recorded": True, "id": entry["id"], "stats": get_feedback_store().stats()}


@router.post("/complete")
@writable_endpoint
def complete(request: SuggestRequest, _: bool = Depends(require_api_key)):
    """Complétion en ligne (suite probable du brouillon, via le LLM)."""
    if not _flag("copilot"):
        raise HTTPException(status_code=404, detail="Fonction désactivée (AGENT_COPILOT)")
    if not request.draft.strip():
        return {"completion": ""}
    try:
        from core.agent_cache import get_agent_runner

        llm = get_agent_runner().agent.llm
    except Exception:
        raise HTTPException(status_code=503, detail="LLM indisponible pour la complétion") from None
    return {"completion": complete_text(llm, request.messages, request.draft)}


# --- Noyau agentique v2 (POST /api/agent/ask/core) -----------------------------------
# Utilise app/agent/core.py (Intent -> Plan -> Policy -> Budget -> Action).
# Bascule en production : le noyau v2 est le DÉFAUT ; ``AGENT_NEW_CORE=0``
# répond 503 (repli legacy, tant que le chemin v1 n'est pas décommissionné).


def _core_tool_events(result) -> list[dict]:
    """Délègue au use-case (app/application/run_lifecycle.core_tool_events)."""
    return core_tool_events(result)


@router.post("/ask/core", response_model=AskResponse)
@writable_endpoint
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

    try:
        outcome = run_ask_core(
            prompt=request.prompt,
            session_id=request.session_id,
            resume_request_id=request.resume_request_id,
            # Modèle demandé (sélecteur du chat) sinon défaut serveur —
            # métadonnées du run ET surcharge réelle du client LLM (partial
            # du factory : le modèle est appliqué à l'assemblage du noyau,
            # pas seulement journalisé).
            model=request.model or agent_config()["model"],
            run_store=get_run_store(),
            approval_store=build_approval_store(),
            build_core=functools.partial(
                build_agent_core,
                model=request.model,
                enable_thinking=request.enable_thinking,
            ),
            load_history=_load_session_history,
            persist_exchange=_persist_exchange,
            audit_log=_audit_log,
        )
    except AgentRunError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return AskResponse(
        response=outcome.answer,
        model=outcome.model,
        status=outcome.api_status,
        request_id=outcome.request_id,
        approval=outcome.approval,
    )


@router.post("/ask/core/stream")
@writable_endpoint
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
    # Modèle effectif : surcharge explicite du client (sélecteur du chat,
    # champ ``model`` d'AskStreamRequest) sinon défaut de la config serveur.
    effective_model = request.model or agent_config()["model"]
    run_store = get_run_store()
    run_row = run_store.start_run(request.prompt, model=effective_model,
                                  source="ask_core_stream")
    _audit_log(ACT_RUN, subject="ask_core_stream",
               detail={"status": "started"}, run_id=run_row["id"])

    # --- Persistance « Agent Flow Map » ---------------------------------------
    # Même convention que /multi/ask/stream : chaque run du noyau v2 crée une
    # session de flux (timeline horodatée rejouable dans le dashboard). La
    # persistance est défensive et ne doit JAMAIS faire échouer le streaming.
    flow_record = get_flow_store().start_flow(request.prompt, effective_model)
    flow_t0 = time.perf_counter()

    def _flow_record(event_type: str, data: dict) -> None:
        try:
            get_flow_store().append_event(
                flow_record["id"],
                event_type,
                data,
                (time.perf_counter() - flow_t0) * 1000.0,
            )
        except Exception:  # pragma: no cover - persistance jamais bloquante
            pass

    _flow_record("core.start", {"role": "noyau", "prompt": request.prompt})

    approval_store = build_approval_store()
    resume_hash = resolve_resume_hash(approval_store, request.resume_request_id)
    _approval_gateway = make_approval_gateway(resume_hash)

    tool_events: list[dict] = []

    # --- Câblage SSE via EventBusPort -----------------------------------------
    # Le noyau publie ses événements de cycle de vie sur un bus PAR RUN
    # (InMemoryEventBus) : aucun cross-talk entre flux concurrents, et la route
    # n'est plus qu'un abonné. On reconstruit ici EXACTEMENT les frames SSE
    # historiques (core_tool / thinking_delta) pour ne rien casser côté IHM.
    bus = InMemoryEventBus()

    def _push_tool(payload: dict) -> None:
        events.put(("tool", payload))
        tool_events.append(payload)
        _flow_record("core.tool", payload)
        try:
            run_store.append_tool_event(run_row["id"], payload)
        except Exception:  # pragma: no cover - le journal ne doit jamais bloquer
            pass

    def _on_bus_tool_start(*, tool, args=None, **_event) -> None:
        _push_tool({"event": "tool_start", "tool": tool, "args": args or {}})

    def _on_bus_tool_end(*, tool, status, summary="", error="",
                         duration_ms=None, **_event) -> None:
        payload = {"event": "tool_result", "tool": tool,
                   "status": status, "duration_ms": duration_ms}
        payload["summary" if status == "ok" else "error"] = (
            summary if status == "ok" else error)
        _push_tool(payload)

    def _on_bus_thinking(*, chunk, **_event) -> None:
        # Faiblesse #4 (noyau v2) : la réflexion est non seulement diffusée en
        # SSE (thinking_delta) mais AUSSI persistée dans la timeline du Flow
        # Map (replay). La persistance est défensive, jamais bloquante.
        _flow_record("core.thinking", {"chunk": chunk})
        events.put(("thinking", chunk))

    bus.on("agent.tool_start", _on_bus_tool_start)
    bus.on("agent.tool_end", _on_bus_tool_end)
    bus.on("agent.thinking", _on_bus_thinking)

    def worker() -> None:
        try:
            core = build_agent_core(
                approval_gateway=_approval_gateway,
                enable_thinking=request.enable_thinking,
                event_bus=bus,
                model=effective_model,
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
                approval_payload = create_approval_request(
                    approval_store, action, request.prompt
                )
                _audit_log(
                    ACT_APPROVAL, subject="ask_core_stream",
                    detail={"request_id": approval_payload["request_id"],
                            "tool": action.tool},
                    run_id=run_row["id"],
                )
                _flow_record("core.approval", {
                    "role": "noyau",
                    "request_id": approval_payload["request_id"],
                    "tool": action.tool,
                    "message": "Policy : validation humaine requise",
                })

            api_status = core_api_status(result.status)
            run_store.finish_run(
                run_row["id"], core_store_status(result.status),
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
                                  result.answer or "",
                                  tool_events=(tool_events
                                               or _core_tool_events(result))
                                  or None,
                                  thinking=result.thinking or "")

            # Rejoue la réponse finale mot à mot (convention /ask/stream).
            for word in _stream_fragments(result.answer or ""):
                events.put(("delta", word))
                time.sleep(ANSWER_STREAM_CADENCE_SECONDS)

            events.put(("final", {
                "response": result.answer or "",
                "model": effective_model,
                "status": api_status,
                "request_id": approval_payload["request_id"] if approval_payload else run_row["id"],
                "approval": approval_payload,
            }))

            # Clôture de la session de flux (mapping statut run -> statut flux).
            _flow_record("core.done", {"answer": result.answer or "", "status": api_status})
            flow_status = {
                "completed": FLOW_COMPLETED,
                "awaiting_approval": FLOW_AWAITING_APPROVAL,
                "rejected": FLOW_REJECTED,
            }.get(api_status, FLOW_ERROR)
            get_flow_store().finish_flow(
                flow_record["id"],
                flow_status,
                answer_summary=(result.answer or "")[:300] or "",
            )
        except HTTPException as exc:  # panne réseau déjà traduite par agent_cache
            run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc.detail))
            get_flow_store().finish_flow(flow_record["id"], FLOW_ERROR, error=str(exc.detail))
            events.put(("http_error", exc))
        except Exception as exc:
            run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc))
            _flow_record("core.error", {"message": f"{type(exc).__name__}: {exc}"})
            get_flow_store().finish_flow(
                flow_record["id"], FLOW_ERROR, error=f"{type(exc).__name__}: {exc}"
            )
            events.put(("error", str(exc)))
        finally:
            events.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()

    # Premier événement avec TIMEOUT (fix déployé Render) : le handler est
    # SYNCHRONE (threadpool) et le worker peut mettre 30-120s avant le premier
    # event si le LLM est lent/injoignable. Sans timeout, le proxy Render
    # coupe la connexion avant le premier byte SSE → « KO » côté dashboard
    # alors que /multi/ask/stream (qui émet agent.plan en premier) survit.
    # On attend le premier événement au plus FIRST_EVENT_TIMEOUT_S puis on
    # démarre le flux SSE QUOI QU'IL ARRIVE (prélude immédiat + heartbeats),
    # l'erreur éventuelle voyageant DANS le flux (event error) au lieu d'un
    # 502 tardif qui ne part jamais.
    FIRST_EVENT_TIMEOUT_S = float(os.getenv("AGENT_SSE_FIRST_EVENT_TIMEOUT", "25"))
    HEARTBEAT_INTERVAL_S = float(os.getenv("AGENT_SSE_HEARTBEAT", "10"))
    try:
        first_kind, first_payload = events.get(timeout=FIRST_EVENT_TIMEOUT_S)
        first_ready = True
    except queue.Empty:
        first_kind, first_payload, first_ready = "pending", None, False
    if first_ready and first_kind == "http_error":
        if isinstance(first_payload, BaseException):
            raise first_payload  # noqa: TRY201 - re-lever l'HTTPException d'origine
        raise HTTPException(status_code=502, detail=str(first_payload))
    if first_ready and first_kind == "error":
        raise HTTPException(status_code=502, detail=str(first_payload))

    async def _sse_stream() -> AsyncIterator[str]:
        try:
            # Prélude immédiat : le premier byte part dès l'ouverture du flux,
            # les proxies intermédiaires (Render, nginx) voient une réponse
            # vivante même si le LLM rame.
            yield _sse({"status": "started", "model": effective_model})
            if first_ready and first_kind != "done":
                field = _CORE_STREAM_FIELDS.get(first_kind, first_kind)
                yield _sse({field: first_payload})
            elif not first_ready:
                yield _sse({"status": "waiting_for_model"})
            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        asyncio.to_thread(events.get), timeout=HEARTBEAT_INTERVAL_S
                    )
                except TimeoutError:
                    # Heartbeat : garde la connexion SSE vivante derrière les
                    # proxies qui coupent les flux silencieux (>30s sans byte).
                    yield ": heartbeat\n\n"
                    continue
                if kind == "done":
                    break
                if kind in ("http_error", "error"):
                    if kind == "http_error":
                        detail = (
                            str(payload.detail)
                            if isinstance(payload, HTTPException)
                            else str(payload)
                        )
                    else:
                        detail = str(payload)
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


# --- Helpers de streaming SSE partagés (noyau v2) ------------------------------------

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
    session_id: str | None,
    resume_request_id: str | None,
) -> list[dict]:
    """Délègue au use-case de mémoire conversationnelle
    (app/application/session_memory.load_session_history)."""
    return load_session_history(session_id, resume_request_id)


def _persist_exchange(
    session_id: str | None,
    prompt: str,
    answer: str,
    tool_events: list[dict] | None = None,
    thinking: str = "",
) -> None:
    """Délègue au use-case de mémoire conversationnelle
    (app/application/session_memory.persist_exchange)."""
    persist_exchange(
        session_id, prompt, answer, tool_events=tool_events, thinking=thinking
    )


# --- Approbation humaine (approve / reject) -----------------------------------------


@router.get("/approvals")
def list_approvals(
    status: str | None = None, _: bool = Depends(require_api_key)
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
    status: str | None = None,
    tool: str | None = None,
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
    port = build_settings_port()
    settings = get_effective_settings(port)
    # Injecter la source (sqlite/env/default) pour chaque clé — le legacy le fait
    # via get_agent_settings() qui lit la base ; ici on marque "sqlite" si la clé
    # est persistée, sinon "env" si elle vient de Settings, sinon "default".
    persisted_keys = set(port.get_all().keys())
    sources = {}
    for key in settings:
        if key in persisted_keys:
            sources[key] = "sqlite"
        else:
            sources[key] = "env"  # simplifié : pourrait être "default"
    api_key = settings.pop("openrouter_api_key") or ""
    hf_api_key = settings.pop("hf_api_key") or ""
    return {
        "settings": {
            **settings,
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
@writable_endpoint
def update_agent_settings(
    update: AgentSettingsUpdate, _: bool = Depends(require_api_key)
):
    """Sauvegarde partielle des paramètres puis rechargement immédiat de l'agent."""
    values = update.model_dump(exclude_none=True)
    port = build_settings_port()
    try:
        payload, errors, written_keys = _save_settings(port, values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    # Rechargement immédiat ; une config encore incomplète n'est PAS une erreur.
    # ``ValueError`` : valeur persistée invalide (ex. provider double-encodé
    # resté en base) — les réglages sont quand même sauvés, seul le reload est
    # dégradé (warning dans la réponse, jamais un 500). (SCRUM-137)
    try:
        reload_agent_runner()
    except (HTTPException, ValueError) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        payload["warning"] = f"Paramètres enregistrés, mais agent non rechargé : {detail}"
        payload["reload_ok"] = False
    else:
        payload["reload_ok"] = True
    payload["written_keys"] = written_keys
    return payload


def _save_settings(port, values):
    """Valide et persiste les paramètres via le use case."""
    effective, errors, written_keys = update_settings(port, values)
    if errors:
        return _settings_payload_from(effective, port), errors, []
    return _settings_payload_from(effective, port), [], written_keys


def _settings_payload_from(effective, port):
    """Formate un dict effectif en payload HTTP (factoring avec _settings_payload)."""
    settings = dict(effective)
    persisted_keys = set(port.get_all().keys())
    sources = {}
    for key in settings:
        sources[key] = "sqlite" if key in persisted_keys else "env"
    api_key = settings.pop("openrouter_api_key") or ""
    hf_api_key = settings.pop("hf_api_key") or ""
    return {
        "settings": {
            **settings,
            "has_openrouter_api_key": bool(api_key),
            "openrouter_api_key_masked": _mask_key(api_key),
            "has_hf_api_key": bool(hf_api_key),
            "hf_api_key_masked": _mask_key(hf_api_key),
        },
        "sources": sources,
    }


@router.post("/settings/test")
@writable_endpoint
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
    elif provider == "lm_studio":
        url = (request.lm_studio_url or "").strip() or cfg["lm_studio_url"]
        chat_url = _lm_studio_chat_url(url)
        base = chat_url[: -len("/chat/completions")].rstrip("/")
        probe_url = f"{base}/models"
        headers = None  # serveur local : aucune authentification
        success_detail = f"LM Studio joignable sur {probe_url}"
        hint = (
            " Vérifiez que le serveur LM Studio tourne"
            " (Developer > Local Server)."
        )
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
    action: str | None = None,
    subject: str | None = None,
    actor: str | None = None,
    run_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    _: bool = Depends(require_api_key),
):
    """Journal d'audit paginé (conformité). Requiert le flag AGENT_AUDIT.

    Filtres AND sur ``action`` / ``subject`` / ``actor`` / ``run_id``. Sans le
    flag, renvoie une réponse 403 explicite (fonctionnalité désactivée).
    """
    if not _flag("audit"):
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
    flags = {name: _flag(name) for name in (
        "reliability", "audit", "tool_analytics", "context", "copilot", "websocket",
    )}
    return {"features": flags, "active": _active_features()}


# --- WebSocket bidirectionnel (Phase E, flag AGENT_WEBSOCKET) -------------------------
#
# Canal temps réel duplex, miroir WebSocket de POST /api/agent/ask/core/stream :
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
    if not _flag("websocket"):
        await websocket.close(code=1008, reason="Fonction désactivée (AGENT_WEBSOCKET)")
        return
    # P1 : X-API-Key (header) d'abord, ?token= en repli navigateur — canal
    # d'ACTION : scope admin uniquement (pas de clé read).
    if not ws_is_authorized(websocket, read_scope=False):
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
    """Exécute un tour d'agent via le noyau v2, événements en temps réel.

    Les événements temps réel (thinking, tool_start/tool_result, delta, final)
    sortent dans la même file puis sont poussés sur le canal WebSocket.
    """
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

    # Noyau v2 par défaut (bascule en production) : le flux WS legacy est
    # décommissionné ; les événements viennent du bus par-run du noyau.
    worker = _ws_core_worker(
        prompt=prompt, session_id=session_id,
        resume_request_id=resume_request_id,
        enable_thinking=enable_thinking, model=model,
    )
    # `worker` est (events_queue, thread) déjà lancé par la fabrique.
    events, _thread = worker
    loop = asyncio.get_event_loop()
    while True:
        item = await loop.run_in_executor(None, events.get)
        if item is None:
            break
        await websocket.send_json(item[1])

def _ws_core_worker(*, prompt, session_id, resume_request_id,
                    enable_thinking, model):
    """Worker WebSocket du noyau v2 — événements publiés sur un bus par-run.

    Le noyau ``AgentCore`` publie son cycle de vie sur un ``InMemoryEventBus``
    local ; les abonnés ci-dessous traduisent ces événements vers le contrat
    WebSocket historique (``thinking`` / ``tool_start`` / ``tool_result`` /
    ``delta`` / ``final``), puis les mettent dans la file. Un bus PAR RUN
    isole les tours d'agent concurrents (aucun cross-talk).
    """
    events = queue.Queue()
    run_store = get_run_store()
    effective_model = model or agent_config()["model"]
    run_row = run_store.start_run(
        prompt, model=effective_model, source="ws"
    )
    tool_events: list[dict] = []
    bus = InMemoryEventBus()

    def on_thinking(*, chunk, **_event) -> None:
        events.put(("thinking", {"event": "thinking", "text": chunk}))

    def on_tool_start(*, tool, args=None, **_event) -> None:
        payload = {"event": "tool_start", "tool": tool, "args": args or {}}
        events.put(("tool", payload))
        tool_events.append(payload)
        try:
            run_store.append_tool_event(run_row["id"], payload)
        except Exception:  # pragma: no cover - le journal ne doit jamais bloquer
            pass

    def on_tool_end(*, tool, status, summary="", error="",
                    duration_ms=None, **_event) -> None:
        payload = {"event": "tool_result", "tool": tool,
                   "status": status, "duration_ms": duration_ms}
        payload["summary" if status == "ok" else "error"] = (
            summary if status == "ok" else error)
        events.put(("tool", payload))
        tool_events.append(payload)
        try:
            run_store.append_tool_event(run_row["id"], payload)
        except Exception:  # pragma: no cover - le journal ne doit jamais bloquer
            pass

    bus.on("agent.thinking", on_thinking)
    bus.on("agent.tool_start", on_tool_start)
    bus.on("agent.tool_end", on_tool_end)

    def _worker() -> None:
        try:
            approval_store = build_approval_store()
            resume_hash = resolve_resume_hash(approval_store, resume_request_id)
            core = build_agent_core(
                approval_gateway=make_approval_gateway(resume_hash),
                enable_thinking=enable_thinking,
                event_bus=bus,
                model=model or None,
            )
            result = core.run(
                Intent(prompt=prompt,
                       session_id=session_id or "default")
            )
            answer = result.answer or ""
            for word in _stream_fragments(answer):
                events.put(("delta", {"event": "delta", "text": word}))
            run_store.finish_run(
                run_row["id"],
                core_store_status(result.status),
                answer_summary=answer[:300],
            )
            _persist_exchange(
                session_id, prompt, answer,
                tool_events=tool_events or core_tool_events(result),
                thinking=result.thinking or "",
            )
            events.put((
                "final",
                {
                    "event": "final",
                    "response": answer,
                    "model": effective_model,
                    "status": core_api_status(result.status),
                    "request_id": None,
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

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return events, thread
# --- Orchestration multi-agents (superviseur / workers) --------------------


@router.post("/multi/ask")
@writable_endpoint
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
    orchestrator = build_multi_agent_orchestrator()
    result = run_multi_agent(
        orchestrator,
        request.prompt,
        model=request.model or None,
        parallel=request.parallel,
        resume_request_id=request.resume_request_id,
        enable_thinking=request.enable_thinking,
    )
    return result


@router.post("/multi/ask/stream")
@writable_endpoint
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
    worker.result / worker.error / done — les événements d'observabilité
    tool / synthesizing / tool.proposed / tool.reviewed sont filtrés pour ne
    pas étouffer un front simple). ``agent.worker.thinking`` passe dans les
    deux modes : la réflexion est requise par l'IHM dès que
    ``enable_thinking`` est actif (bloc « Réflexion » du chat).

    Réutilise le schéma éprouvé (queue.Queue + thread worker + générateur
    async) et garde la traduction des pannes réseau en erreur HTTP précoce.
    """
    events: queue.Queue = queue.Queue()
    compact = (request.mode or "full").strip().lower() == "compact"

    # --- Persistance « Agent Flow Map » ---------------------------------------
    # Tous les événements SSE sont enregistrés (quel que soit le mode full/
    # compact) avec un horodatage relatif au début de session : la timeline
    # rejouable du dashboard (Replay / Heatmap). La persistance est défensive
    # et ne doit JAMAIS faire échouer le streaming.
    flow_record = get_flow_store().start_flow(
        request.prompt, request.model or agent_config()["model"]
    )
    flow_t0 = time.perf_counter()
    flow_errored = {"v": False}

    def _record(event_type: str, data: dict) -> None:
        try:
            get_flow_store().append_event(
                flow_record["id"],
                event_type,
                data,
                (time.perf_counter() - flow_t0) * 1000.0,
            )
            if event_type == "agent.error":
                flow_errored["v"] = True
        except Exception:  # pragma: no cover - persistance jamais bloquante
            pass

    def _emit(event_type: str, data: dict) -> None:
        _record(event_type, data)
        if compact and event_type in _MULTI_OBSERVABILITY_EVENTS:
            return  # observabilité : aucun envoi au front en mode compact
        events.put((event_type, data))

    def worker() -> None:
        try:
            orchestrator = build_multi_agent_orchestrator()
            result = run_multi_agent_streaming(
                orchestrator,
                request.prompt,
                model=request.model or None,
                parallel=request.parallel,
                resume_request_id=request.resume_request_id,
                enable_thinking=request.enable_thinking,
                on_event=_emit,
            )
            events.put(("agent.done", result))
            # Mapping statut orchestrateur -> statut flux (FAIBLESSE #1 corrigée) :
            # un run interrompu sur une validation humaine est persisté
            # « awaiting_approval » — JAMAIS « completed » tant qu'une
            # sous-tâche attend une validation (invariant vérifié sur workers).
            api_status = (result or {}).get("status")
            workers = (result or {}).get("workers") or []
            workers_awaiting = any(
                w.get("status") == "awaiting_approval" for w in workers
            )
            if flow_errored["v"] or api_status == "error":
                get_flow_store().finish_flow(
                    flow_record["id"],
                    FLOW_ERROR,
                    error=(result or {}).get("message") or "erreur multi-agents",
                )
            elif api_status == "awaiting_approval" or workers_awaiting:
                get_flow_store().finish_flow(
                    flow_record["id"],
                    FLOW_AWAITING_APPROVAL,
                    answer_summary=(result or {}).get("final_answer") or "",
                )
            else:
                get_flow_store().finish_flow(
                    flow_record["id"],
                    FLOW_COMPLETED,
                    answer_summary=(result or {}).get("final_answer")
                    or (result or {}).get("answer")
                    or "",
                )
        except HTTPException as exc:  # panne réseau déjà traduite
            get_flow_store().finish_flow(flow_record["id"], FLOW_ERROR, error=str(exc.detail))
            events.put(("agent.error", {"message": str(exc.detail)}))
        except Exception as exc:  # pragma: no cover
            get_flow_store().finish_flow(
                flow_record["id"], FLOW_ERROR, error=f"{type(exc).__name__}: {exc}"
            )
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

# --- Journal « Agent Flow Map » (sessions multi-agents persistées) -------------------


@router.get("/flow")
def list_flow_sessions(
    limit: int = 50,
    status: str | None = None,
    _: bool = Depends(require_api_key),
):
    """Liste paginée des sessions multi-agents (Flow Map), plus récentes d'abord.

    Chaque entrée porte un résumé (prompt, statut, réponse, compteurs
    ``tool_calls`` / ``agents``) sans la timeline complète : le détail est
    servi par ``GET /flow/{flow_id}``.
    """
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit doit être entre 1 et 200.")
    if status is not None and status not in FLOW_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Statut inconnu : '{status}'. Valeurs : {', '.join(FLOW_STATUSES)}",
        )
    return {
        "flows": get_flow_store().list(limit=limit, status=status),
        "statuses": list(FLOW_STATUSES),
    }


@router.get("/flow/{flow_id}")
def get_flow_session(flow_id: str, _: bool = Depends(require_api_key)):
    """Détail complet d'une session Flow Map : timeline horodatée des événements.

    ``events`` est une liste ``{"event", "data", "at_ms"}`` prête à rejouer par
    le dashboard (``at_ms`` = millisecondes relatives au début de la session).
    """
    flow = get_flow_store().get(flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail=f"Session introuvable : {flow_id}")
    return flow


@router.delete("/flow/{flow_id}")
@writable_endpoint
def delete_flow_session(flow_id: str, _: bool = Depends(require_api_key)):
    """Supprime une session enregistrée (nettoyage du journal Flow Map)."""
    if not get_flow_store().delete(flow_id):
        raise HTTPException(status_code=404, detail=f"Session introuvable : {flow_id}")
    return {"deleted": flow_id}
