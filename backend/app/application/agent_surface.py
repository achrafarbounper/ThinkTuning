# project/app/application/agent_surface.py
"""Use cases de la surface agent — extraits de ``api/routes/agent.py`` (B-5).

Absorption de la logique métier du routeur legacy (ADR-0003, écarts E-04/E-11,
Phase C documentée) : les corps d'endpoints vivent ICI, les deux surfaces HTTP
(``routes/agent.py`` legacy et ``routes/v1/agent.py``) ne sont plus que des
façades de délégation.

Contrat du module (même pattern que ``ask_usecase``/``session_memory``) :

    - AUCUN import ``app.infrastructure.*``, ni FastAPI, ni ``requests`` :
      TOUS les collaborateurs (stores, bus d'événements, factories,
      télémétrie, use cases et config agent) sont INJECTÉS en paramètre
      explicite par la surface HTTP, seule couche autorisée à toucher
      l'infrastructure ;
    - les erreurs métier lèvent les exceptions du domaine
      (``app/domain/errors.py``) : la surface legacy les mappe en
      ``HTTPException`` (``{"detail": ...}``), la surface v1 les laisse
      remonter au handler global (enveloppe ``{"error": {...}}``) — parité
      des statuts et des messages garantie par ``http_status`` ;
    - l'injection EST le seam de test des deux surfaces : les tests patchent
      les collaborateurs dans ``app.api.routes.agent`` (couche de wiring) et
      l'adaptateur les transmet tels quels à ces fonctions — aucun état
      mutable de module ici, donc aucune pollution d'ordre entre tests.

Note de migration (précédent ``run_lifecycle``) : les statuts persistés des
stores (``approved``/``rejected`` pour les approbations, ``completed``/
``awaiting_approval``/``rejected``/``error`` pour les flux) sont figés en
littéraux localement pour éviter tout import infrastructure ; un test de
contrat peut verrouiller l'alignement avec les constantes des stores.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from app.agent.settings import (
    DEFAULT_HF_URL,
    DEFAULT_LM_STUDIO_URL,
    DEFAULT_OPENROUTER_URL,
    normalize_chat_url,
)
from app.application.agent_settings_usecase import get_effective_settings
from app.application.run_lifecycle import (
    ACT_APPROVAL,
    ACT_RUN,
    RUN_ERROR,
    core_api_status,
    core_store_status,
    core_tool_events,
    create_approval_request,
    make_approval_gateway,
    resolve_resume_hash,
)
from app.domain.entities.plan import Intent
from app.domain.entities.run import RunStatus
from app.domain.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)

# Action d'audit outillage (valeur alignée sur
# app/infrastructure/persistence/audit_store.ACT_TOOL — précédent run_lifecycle).
ACT_TOOL = "tool_execution"

# Statuts persistés des stores (littéraux alignés — voir docstring de module).
APPROVED_STATUS = "approved"
REJECTED_STATUS = "rejected"
FLOW_COMPLETED = "completed"
FLOW_AWAITING_APPROVAL = "awaiting_approval"
FLOW_REJECTED = "rejected"
FLOW_ERROR = "error"


# ------------------------------------------------------------------
# Flags de fonctionnalités (source : app.agent.settings — autorisé ici)
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# Statut de l'agent (GET /status) — pur
# ------------------------------------------------------------------


def agent_status(cfg: Any, tools: Any) -> dict:
    """Statut public de l'agent : modèle visé, URL, timeout, outils dispo."""
    return {
        "status": "ok",
        "provider": str(cfg.provider),
        "model": cfg.model_name,
        "ollama_url": cfg.ollama_url,
        "timeout_seconds": cfg.timeout_seconds,
        "context_length": cfg.context_length,
        "auth_required": True,  # l'API principale applique toujours X-API-Key
        "tools": sorted(tools),
    }


# ------------------------------------------------------------------
# Providers LLM (CRUD + activation) — store injecté par la route
# ------------------------------------------------------------------


def mask_key(key: str) -> str:
    """Masque une clé API pour l'affichage : « sk-or-v1 » -> « sk-or-…abcd »."""
    key = key or ""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}"


def provider_public(document: dict) -> dict:
    """Projection publique d'un document provider (clé jamais en clair)."""
    result = dict(document)
    provider = dict(result.get("provider", {}))
    key = provider.pop("api_key", "") or ""
    provider["has_api_key"] = bool(key)
    provider["api_key_masked"] = mask_key(key)
    result["provider"] = provider
    if "_id" in result:
        result["id"] = str(result.pop("_id"))
    return result


def list_providers(store: Any) -> dict:
    """Liste des providers enregistrés, avec secrets masqués."""
    return {"providers": [provider_public(doc) for doc in store.list_all()]}


def save_provider_document(store: Any, document: dict) -> dict:
    """Nettoie le document issu du payload puis le persiste (upsert)."""
    document.pop("id", None)
    document["provider"].pop("has_api_key", None)
    document["provider"].pop("api_key_masked", None)
    document["provider"].setdefault(
        "streaming_sse",
        {"first_event_seconds": 60, "heartbeat_seconds": 10},
    )
    if not document["provider"].get("api_key"):
        document["provider"].pop("api_key", None)
    saved = store.upsert(document)
    return {"provider": provider_public(saved)}


def delete_provider(store: Any, provider_id: str) -> dict:
    """Supprime un document provider (404 si absent)."""
    if not store.delete(provider_id):
        raise NotFoundError("Provider introuvable.")
    return {"deleted": True}


def provider_settings_values(document: dict) -> dict:
    """Dérive les réglages agent effectifs d'un document provider (pur).

    Le fournisseur est reconnu par l'URL de base (openrouter / huggingface /
    lmstudio / 192.168.* -> lm_studio, sinon ollama) — même heuristique que
    l'implémentation historique de l'endpoint d'activation.
    """
    provider_config = document["provider"]
    base_url = provider_config["base_url"]
    provider = (
        "openrouter"
        if "openrouter" in base_url
        else "hf"
        if "huggingface" in base_url
        else "lm_studio"
        if "lmstudio" in base_url or "192.168." in base_url
        else "ollama"
    )
    values = {
        "provider": provider,
        "model": provider_config["model_id"],
        "timeout_seconds": provider_config["timeout_seconds"],
        "context_length": provider_config["context_length_tokens"],
        "temperature": provider_config["temperature"],
    }
    if provider == "openrouter":
        values.update(
            {
                "openrouter_url": base_url,
                "openrouter_api_key": provider_config.get("api_key", ""),
            }
        )
    elif provider == "hf":
        values.update({"hf_url": base_url, "hf_api_key": provider_config.get("api_key", "")})
    elif provider == "lm_studio":
        values["lm_studio_url"] = base_url
    else:
        values["ollama_url"] = base_url
    return values


def activate_provider(document: dict, apply_settings: Callable[[dict], Any]) -> Any:
    """Active un provider : dérive les réglages puis les applique.

    ``apply_settings`` est fourni par la route : il enchaîne la sauvegarde
    des réglages (use case ``update_settings_values``) et renvoie la charge
    utile HTTP — même réponse que PUT /settings.
    """
    return apply_settings(provider_settings_values(document))


# ------------------------------------------------------------------
# Paramètres de l'agent (GET/PUT /settings) — port injecté par la route
# ------------------------------------------------------------------


def settings_payload(port: Any) -> dict:
    """Formate la config effective pour le dashboard (clé jamais en clair)."""
    settings = get_effective_settings(port)
    return settings_payload_from(settings, port)


def settings_payload_from(effective: dict, port: Any) -> dict:
    """Formate un dict effectif en payload HTTP (factoring avec settings_payload).

    Source de chaque clé : « sqlite » si persistée dans le port, sinon « env ».
    """
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
            "openrouter_api_key_masked": mask_key(api_key),
            "has_hf_api_key": bool(hf_api_key),
            "hf_api_key_masked": mask_key(hf_api_key),
        },
        "sources": sources,
    }


def update_settings_values(
    values: dict,
    port: Any,
    *,
    update_settings_fn: Callable[..., Any],
    reload_runner: Callable[[], Any],
    payload_from: Callable[..., dict],
) -> dict:
    """Flux complet de PUT /settings : sauvegarde + rechargement immédiat.

    ``update_settings_fn`` / ``reload_runner`` / ``payload_from`` sont les
    collaborateurs injectés par la surface HTTP (use case de persistance,
    façade de rechargement ``agent_cache``, formatage du payload) : ils
    restent les points de monkeypatch des tests, sans import infrastructure
    ici (B-3/B-5).

    ``ValueError`` (validation) -> ``BadRequestError`` (400). Le rechargement
    de l'agent est dégradé en warning (jamais un 500) : ``ValueError`` pour
    une valeur persistée invalide, ou panne réseau LLM signalée par
    ``agent_cache`` sous forme d'erreur HTTP (détectée par canard — la couche
    application n'importe pas FastAPI) — SCRUM-137.
    """
    try:
        effective, errors, written_keys = update_settings_fn(port, values)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc

    if errors:
        raise BadRequestError("; ".join(errors))

    payload = payload_from(effective, port)
    try:
        reload_runner()
    except Exception as exc:  # noqa: BLE001 — voir ci-dessus : dégradé en warning
        is_http_like = getattr(exc, "status_code", None) is not None and hasattr(exc, "detail")
        if not is_http_like and not isinstance(exc, ValueError):
            raise
        detail = getattr(exc, "detail", None)
        detail = detail if detail is not None else str(exc)
        payload["warning"] = f"Paramètres enregistrés, mais agent non rechargé : {detail}"
        payload["reload_ok"] = False
    else:
        payload["reload_ok"] = True
    payload["written_keys"] = written_keys
    return payload


# ------------------------------------------------------------------
# Sonde de connectivité (POST /settings/test) — plan pur, HTTP dans la route
# ------------------------------------------------------------------


@dataclass
class ConnectivityPlan:
    """Plan de sonde : URL à interroger, en-têtes, messages attendus."""

    provider: str
    probe_url: str
    headers: dict[str, str] | None
    success_detail: str
    hint: str


def connectivity_plan(
    provider: str,
    *,
    fields: dict,
    cfg: Any,
) -> ConnectivityPlan:
    """Dérive le plan de sonde du provider demandé (pur, sans I/O).

    ``fields`` porte les valeurs optionnelles du payload (url/clé par
    provider) ; les absents retombent sur la config effective ``cfg``.
    """
    if provider == "openrouter":
        url = (fields.get("openrouter_url") or "").strip() or cfg.openrouter_url
        chat_url = normalize_chat_url(url, default=DEFAULT_OPENROUTER_URL)
        base = chat_url[: -len("/chat/completions")].rstrip("/")
        probe_url = f"{base}/models"
        api_key = (fields.get("openrouter_api_key") or cfg.openrouter_api_key or "").strip()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        success_detail = f"OpenRouter joignable sur {probe_url}"
        hint = ""
    elif provider == "hf":
        url = (fields.get("hf_url") or "").strip() or cfg.hf_url
        chat_url = normalize_chat_url(url, default=DEFAULT_HF_URL)
        base = chat_url[: -len("/chat/completions")].rstrip("/")
        probe_url = f"{base}/models"
        api_key = (fields.get("hf_api_key") or cfg.hf_api_key or "").strip()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        success_detail = f"Hugging Face joignable sur {probe_url}"
        hint = ""
    elif provider == "lm_studio":
        url = (fields.get("lm_studio_url") or "").strip() or cfg.lm_studio_url
        chat_url = normalize_chat_url(url, default=DEFAULT_LM_STUDIO_URL)
        base = chat_url[: -len("/chat/completions")].rstrip("/")
        probe_url = f"{base}/models"
        headers = None  # serveur local : aucune authentification
        success_detail = f"LM Studio joignable sur {probe_url}"
        hint = " Vérifiez que le serveur LM Studio tourne (Developer > Local Server)."
    else:
        base_url = (fields.get("ollama_url") or "").strip() or cfg.ollama_url
        marker = base_url.find("/api/")
        root = base_url[:marker] if marker != -1 else base_url.rstrip("/")
        probe_url = f"{root}/api/tags"
        headers = None
        success_detail = f"Ollama joignable sur {probe_url}"
        hint = " Vérifiez qu'Ollama tourne."
    return ConnectivityPlan(
        provider=provider,
        probe_url=probe_url,
        headers=headers,
        success_detail=success_detail,
        hint=hint,
    )


# ------------------------------------------------------------------
# Outils (catalogue, exécution, tools personnalisés SCRUM-99)
# ------------------------------------------------------------------


def run_tool_execution(
    tools: Any,
    required_args: dict,
    tool: str,
    args: dict,
    *,
    record_ctx: Callable[[str], Any],
) -> dict:
    """Exécute directement un outil (télémétrie ``record_ctx`` injectée).

    ``record_ctx(tool)`` est un context manager fourni par la route
    (analytics Phase B — infrastructure). Erreurs : 400 (tool inconnu /
    arguments manquants / invalides) via ``BadRequestError``.
    """
    if tool not in tools:
        raise BadRequestError(f"Tool inconnu : '{tool}'. Tools disponibles : {sorted(tools)}")
    missing = [key for key in required_args[tool] if key not in args]
    if missing:
        raise BadRequestError(f"Arguments manquants pour {tool} : {missing}")
    try:
        with record_ctx(tool):
            result = tools[tool](**args)
    except TypeError as exc:
        raise BadRequestError(f"Arguments invalides pour {tool} : {exc}") from exc
    return {"tool": tool, "result": result}


# ------------------------------------------------------------------
# Approbation humaine (GET/POST /approvals) — store injecté par la route
# ------------------------------------------------------------------


def list_approvals(store: Any, status: str | None, statuses: tuple) -> dict:
    """Liste des demandes d'approbation, filtrées par statut (400 si inconnu)."""
    if status is not None and status not in statuses:
        raise BadRequestError(f"Statut inconnu : '{status}'. Valeurs : {', '.join(statuses)}")
    return {"approvals": store.list(status)}


def decide_approval(
    store: Any,
    request_id: str,
    decision: str,
    audit_log: Callable[..., Any],
) -> dict:
    """Valide ou refuse une demande ``pending`` (approve / reject).

    ``decision`` : « approve » ou « reject ». Erreurs : 404 introuvable,
    409 demande non en attente (``ConflictError``). L'issue est auditée
    (action ``approval``, détail historique préservé).
    """
    if decision == "approve":
        row = store.approve(request_id)
    else:
        row = store.reject(request_id)
    if row is None:
        raise NotFoundError(f"Demande introuvable : {request_id}")
    expected = APPROVED_STATUS if decision == "approve" else REJECTED_STATUS
    if row["status"] != expected:
        if decision == "approve":
            raise ConflictError("Cette demande n'était pas en attente (approbation impossible).")
        raise ConflictError("Demande non en attente (impossible de réfuter).")
    audit_log(
        ACT_APPROVAL,
        subject=row["tool"],
        detail={
            "request_id": request_id,
            "decision": "approved" if decision == "approve" else "rejected",
            "tool": row["tool"],
            "args_hash": row.get("args_hash", ""),
        },
    )
    return {"status": expected, "approval": row}


# ------------------------------------------------------------------
# Noyau agentique v2 — tour bloquant (POST /ask/core)
# ------------------------------------------------------------------


def ask_core_turn(
    *,
    prompt: str,
    session_id: str | None,
    resume_request_id: str | None,
    model: str,
    enable_thinking: bool,
    run_store: Any,
    approval_store: Any,
    build_core: Callable[..., Any],
    audit_log: Callable[..., Any],
    core_enabled: Callable[[], bool],
    run_core: Callable[..., Any],
    load_history: Callable[..., Any],
    save_exchange: Callable[..., Any],
) -> dict:
    """Un tour d'agent bloquant via le noyau v2 (flag ``AGENT_NEW_CORE``).

    Délègue au use case ``run_ask_core`` (injecté : stores + factory du
    noyau + audit). 503 si le flag noyau est désactivé
    (``ServiceUnavailableError``), 502 si le run échoue
    (``AgentRunError`` déjà levée par le use case). Retour : dict compatible
    ``AskResponse``.
    """
    if not core_enabled():
        raise ServiceUnavailableError(
            "Nouveau noyau agentique désactivé (AGENT_NEW_CORE non activé)."
        )
    outcome = run_core(
        prompt=prompt,
        session_id=session_id,
        resume_request_id=resume_request_id,
        model=model,
        run_store=run_store,
        approval_store=approval_store,
        build_core=build_core,
        load_history=load_history,
        persist_exchange=save_exchange,
        audit_log=audit_log,
    )
    return {
        "response": outcome.answer,
        "model": outcome.model,
        "status": outcome.api_status,
        "request_id": outcome.request_id,
        "approval": outcome.approval,
    }


# ------------------------------------------------------------------
# Noyau agentique v2 — streaming SSE (POST /ask/core/stream)
# ------------------------------------------------------------------


@dataclass
class CoreStreamPlan:
    """État d'un flux noyau v2 : file d'événements + réglages SSE.

    ``events`` porte des tuples ``(kind, payload)`` ; le sentinelle final est
    ``("done", None)``. Les erreurs voyagent DANS le flux (``http_error`` avec
    l'exception d'origine-like ``status_code``/``detail``, ou ``error``
    texte) au lieu d'un 502 tardif.
    """

    events: queue.Queue
    effective_model: str
    first_event_timeout_s: float
    heartbeat_interval_s: float
    run_id: str = ""


def wait_first_core_event(plan: CoreStreamPlan) -> tuple[str, Any, bool]:
    """Premier événement AVEC timeout (fix Render) : (kind, payload, ready).

    Sans événement dans le délai ``plan.first_event_timeout_s`` -> ``pending``
    : le flux SSE démarre quand même (prélude + heartbeats), l'erreur
    éventuelle voyageant dans le flux.
    """
    try:
        kind, payload = plan.events.get(timeout=plan.first_event_timeout_s)
        return kind, payload, True
    except queue.Empty:
        return "pending", None, False


def _sse(payload: dict | str) -> str:
    """Formate une charge utile en événement SSE (``data: ...``)."""
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return "data: " + data + "\n\n"


def _stream_fragments(text: str) -> Iterator[str]:
    """Découpe un texte en fragments mot à mot (générateur synchrone)."""
    for word in text.split(" "):
        yield word + " "


def _stream_error_detail(payload: Any, kind: str) -> str:
    """Détail texte d'une erreur de flux (duck-typing sans import FastAPI)."""
    if kind == "http_error":
        detail = payload.detail if hasattr(payload, "detail") else str(payload)
        return str(detail)
    return str(payload)


def _is_http_like(exc: BaseException) -> bool:
    """Détecte une exception « HTTP-like » (status_code + detail) sans FastAPI.

    Les pannes réseau traduites par ``agent_cache`` arrivent sous forme
    d'erreurs HTTP (502/504) : le worker les distingue ainsi pour les faire
    voyager comme ``http_error`` (la surface HTTP les re-lève telles quelles,
    statut préservé).
    """
    return getattr(exc, "status_code", None) is not None and hasattr(exc, "detail")


def _http_like_detail(exc: BaseException) -> str:
    """Détail d'une exception « HTTP-like » (canard ``detail``, repli ``str``)."""
    detail = getattr(exc, "detail", None)
    return str(detail) if detail is not None else str(exc)


# Champs d'événements du flux /ask/core/stream (queue -> SSE).
_CORE_STREAM_FIELDS = {
    "tool": "core_tool",
    "thinking": "thinking_delta",
    "delta": "delta",
    "final": "final",
}

# Cadence (secondes) de l'émission mot à mot de la réponse finale ; la
# réflexion et les événements d'outils, eux, sont diffusés en temps réel.
ANSWER_STREAM_CADENCE_SECONDS = 0.02


def prepare_core_stream(
    *,
    prompt: str,
    session_id: str | None,
    resume_request_id: str | None,
    enable_thinking: bool,
    model: str | None,
    run_store: Any,
    approval_store: Any,
    flow_store: Any,
    bus_factory: Callable[[], Any],
    build_core: Callable[..., Any],
    audit_log: Callable[..., Any],
    core_enabled: Callable[[], bool],
    get_config: Callable[[], Any],
    load_history: Callable[..., Any],
    save_exchange: Callable[..., Any],
    first_event_timeout_s: float,
    heartbeat_interval_s: float,
) -> CoreStreamPlan:
    """Prépare le flux noyau v2 : run + Flow Map + worker thread (flag check).

    Le noyau publie son cycle de vie sur un bus PAR RUN (``bus_factory``,
    injecté — infrastructure events) : les abonnés reconstruisent EXACTEMENT
    les frames SSE historiques (``core_tool`` / ``thinking_delta``), la
    réponse finale est rejouée mot à mot. 503 si le flag noyau est désactivé.
    """
    if not core_enabled():
        raise ServiceUnavailableError(
            "Nouveau noyau agentique désactivé (AGENT_NEW_CORE non activé)."
        )

    events: queue.Queue = queue.Queue()
    # Modèle effectif : surcharge explicite du client (sélecteur du chat,
    # champ ``model`` d'AskStreamRequest) sinon défaut de la config serveur.
    effective_model = model or get_config().model_name
    run_row = run_store.start_run(prompt, model=effective_model, source="ask_core_stream")
    audit_log(
        ACT_RUN, subject="ask_core_stream", detail={"status": "started"}, run_id=run_row["id"]
    )

    # --- Persistance « Agent Flow Map » ---------------------------------------
    # Même convention que /multi/ask/stream : chaque run du noyau v2 crée une
    # session de flux (timeline horodatée rejouable dans le dashboard). La
    # persistance est défensive et ne doit JAMAIS faire échouer le streaming.
    flow_record = flow_store.start_flow(prompt, effective_model)
    flow_t0 = time.perf_counter()

    def _flow_record(event_type: str, data: dict) -> None:
        try:
            flow_store.append_event(
                flow_record["id"],
                event_type,
                data,
                (time.perf_counter() - flow_t0) * 1000.0,
            )
        except Exception:  # pragma: no cover - persistance jamais bloquante
            pass

    _flow_record("core.start", {"role": "noyau", "prompt": prompt})

    resume_hash = resolve_resume_hash(approval_store, resume_request_id)
    _approval_gateway = make_approval_gateway(resume_hash)

    tool_events: list[dict] = []

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

    def _on_bus_tool_end(*, tool, status, summary="", error="", duration_ms=None, **_event) -> None:
        payload = {
            "event": "tool_result",
            "tool": tool,
            "status": status,
            "duration_ms": duration_ms,
        }
        payload["summary" if status == "ok" else "error"] = summary if status == "ok" else error
        _push_tool(payload)

    def _on_bus_thinking(*, chunk, **_event) -> None:
        # Faiblesse #4 (noyau v2) : la réflexion est diffusée en SSE
        # (thinking_delta) ET persistée dans la timeline du Flow Map.
        _flow_record("core.thinking", {"chunk": chunk})
        events.put(("thinking", chunk))

    bus = bus_factory()
    bus.on("agent.tool_start", _on_bus_tool_start)
    bus.on("agent.tool_end", _on_bus_tool_end)
    bus.on("agent.thinking", _on_bus_thinking)

    def worker() -> None:
        try:
            core = build_core(
                approval_gateway=_approval_gateway,
                enable_thinking=enable_thinking,
                event_bus=bus,
                model=effective_model,
            )
            history = load_history(session_id, resume_request_id)
            result = core.run(
                Intent(prompt=prompt, session_id=session_id or "default"),
                history=history,
            )

            # Création de la demande d'approbation le cas échéant (même
            # logique que /ask/core) : l'IHM affichera la carte de validation.
            approval_payload = None
            if result.status is RunStatus.PENDING_APPROVAL and result.awaiting_action:
                action = result.awaiting_action
                approval_payload = create_approval_request(approval_store, action, prompt)
                audit_log(
                    ACT_APPROVAL,
                    subject="ask_core_stream",
                    detail={
                        "request_id": approval_payload["request_id"],
                        "tool": action.tool,
                    },
                    run_id=run_row["id"],
                )
                _flow_record(
                    "core.approval",
                    {
                        "role": "noyau",
                        "request_id": approval_payload["request_id"],
                        "tool": action.tool,
                        "message": "Policy : validation humaine requise",
                    },
                )

            api_status = core_api_status(result.status)
            run_store.finish_run(
                run_row["id"],
                core_store_status(result.status),
                answer_summary=(result.answer or "")[:300],
            )
            audit_log(
                ACT_RUN,
                subject="ask_core_stream",
                detail={
                    "status": api_status,
                    "actions": len(result.actions),
                    "rounds": result.rounds_used,
                    "tool_calls": result.tool_calls_used,
                },
                run_id=run_row["id"],
            )
            if api_status != "error":
                save_exchange(
                    session_id,
                    prompt,
                    result.answer or "",
                    tool_events=tool_events or core_tool_events(result) or None,
                    thinking=result.thinking or "",
                )

            # Rejoue la réponse finale mot à mot (convention /ask/stream).
            for word in _stream_fragments(result.answer or ""):
                events.put(("delta", word))
                time.sleep(ANSWER_STREAM_CADENCE_SECONDS)

            events.put(
                (
                    "final",
                    {
                        "response": result.answer or "",
                        "model": effective_model,
                        "status": api_status,
                        "request_id": approval_payload["request_id"]
                        if approval_payload
                        else run_row["id"],
                        "approval": approval_payload,
                    },
                )
            )

            # Clôture de la session de flux (mapping statut run -> statut flux).
            _flow_record("core.done", {"answer": result.answer or "", "status": api_status})
            flow_status = {
                "completed": FLOW_COMPLETED,
                "awaiting_approval": FLOW_AWAITING_APPROVAL,
                "rejected": FLOW_REJECTED,
            }.get(api_status, FLOW_ERROR)
            flow_store.finish_flow(
                flow_record["id"],
                flow_status,
                answer_summary=(result.answer or "")[:300] or "",
            )
        except Exception as exc:  # noqa: BLE001
            if _is_http_like(exc):  # panne réseau déjà traduite (agent_cache)
                run_store.finish_run(run_row["id"], RUN_ERROR, error=_http_like_detail(exc))
                flow_store.finish_flow(flow_record["id"], FLOW_ERROR, error=_http_like_detail(exc))
                events.put(("http_error", exc))
            else:
                run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc))
                _flow_record("core.error", {"message": f"{type(exc).__name__}: {exc}"})
                flow_store.finish_flow(
                    flow_record["id"], FLOW_ERROR, error=f"{type(exc).__name__}: {exc}"
                )
                events.put(("error", str(exc)))
        finally:
            events.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()

    return CoreStreamPlan(
        events=events,
        effective_model=effective_model,
        first_event_timeout_s=first_event_timeout_s,
        heartbeat_interval_s=heartbeat_interval_s,
        run_id=run_row["id"],
    )


def core_sse_stream(
    plan: CoreStreamPlan,
    first_kind: str,
    first_payload: Any,
    first_ready: bool,
) -> Iterator[str]:
    """Générateur SSE du flux noyau v2 (contrat byte-identique à l'historique).

    Prélude immédiat (le premier byte part dès l'ouverture — les proxies type
    Render/nginx voient une réponse vivante), heartbeats en cas de silence,
    erreurs voyageant dans le flux, terminaison ``data: [DONE]``.
    """
    try:
        yield _sse({"status": "started", "model": plan.effective_model})
        if first_ready and first_kind != "done":
            frame = _CORE_STREAM_FIELDS.get(first_kind, first_kind)
            yield _sse({frame: first_payload})
        elif not first_ready:
            yield _sse({"status": "waiting_for_model"})
        while True:
            try:
                kind, payload = plan.events.get(timeout=plan.heartbeat_interval_s)
            except queue.Empty:
                # Heartbeat : garde la connexion SSE vivante derrière les
                # proxies qui coupent les flux silencieux (>30s sans byte).
                yield ": heartbeat\n\n"
                continue
            if kind == "done":
                break
            if kind in ("http_error", "error"):
                yield _sse({"error": _stream_error_detail(payload, kind)})
                break
            frame = _CORE_STREAM_FIELDS.get(kind, kind)
            yield _sse({frame: payload})
        yield "data: [DONE]\n\n"
    except GeneratorExit:  # client déconnecté — nettoyage du générateur
        raise


# ------------------------------------------------------------------
# Orchestration multi-agents — streaming (POST /multi/ask/stream)
# ------------------------------------------------------------------

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


@dataclass
class MultiStreamPlan:
    """État d'un flux multi-agents : file d'événements (kind, payload).

    Le sentinelle de fin est ``("__done__", None)``. Les erreurs globales
    arrivent comme ``agent.error`` (la surface HTTP les convertit en 502
    quand elles surviennent AVANT le premier événement).
    """

    events: queue.Queue


def prepare_multi_stream(
    *,
    prompt: str,
    model: str | None,
    parallel: bool,
    resume_request_id: str | None,
    enable_thinking: bool,
    mode: str,
    flow_store: Any,
    orchestrator_factory: Callable[[], Any],
    run_streaming: Callable[..., dict],
    get_config: Callable[[], Any],
) -> MultiStreamPlan:
    """Prépare le flux multi-agents : Flow Map + worker thread.

    Tous les événements SSE sont enregistrés dans la Flow Map (quel que soit
    le mode full/compact) avec un horodatage relatif : la persistance est
    défensive et ne doit JAMAIS faire échouer le streaming. Le filtrage
    « compact » (SCRUM-101) ne s'applique qu'à l'ENVOI au front.
    """
    events: queue.Queue = queue.Queue()
    compact = (mode or "full").strip().lower() == "compact"

    flow_record = flow_store.start_flow(prompt, model or get_config().model_name)
    flow_t0 = time.perf_counter()
    flow_errored = {"v": False}

    def _record(event_type: str, data: dict) -> None:
        try:
            flow_store.append_event(
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
            orchestrator = orchestrator_factory()
            result = run_streaming(
                orchestrator,
                prompt,
                model=model or None,
                parallel=parallel,
                resume_request_id=resume_request_id,
                enable_thinking=enable_thinking,
                on_event=_emit,
            )
            events.put(("agent.done", result))
            # Mapping statut orchestrateur -> statut flux (FAIBLESSE #1 corrigée) :
            # un run interrompu sur une validation humaine est persisté
            # « awaiting_approval » — JAMAIS « completed » tant qu'une
            # sous-tâche attend une validation (invariant vérifié sur workers).
            api_status = (result or {}).get("status")
            workers = (result or {}).get("workers") or []
            workers_awaiting = any(w.get("status") == "awaiting_approval" for w in workers)
            if flow_errored["v"] or api_status == "error":
                flow_store.finish_flow(
                    flow_record["id"],
                    FLOW_ERROR,
                    error=(result or {}).get("message") or "erreur multi-agents",
                )
            elif api_status == "awaiting_approval" or workers_awaiting:
                flow_store.finish_flow(
                    flow_record["id"],
                    FLOW_AWAITING_APPROVAL,
                    answer_summary=(result or {}).get("final_answer") or "",
                )
            else:
                flow_store.finish_flow(
                    flow_record["id"],
                    FLOW_COMPLETED,
                    answer_summary=(result or {}).get("final_answer")
                    or (result or {}).get("answer")
                    or "",
                )
        except Exception as exc:  # noqa: BLE001
            if _is_http_like(exc):  # panne réseau déjà traduite
                flow_store.finish_flow(flow_record["id"], FLOW_ERROR, error=_http_like_detail(exc))
                events.put(("agent.error", {"message": _http_like_detail(exc)}))
            else:
                flow_store.finish_flow(
                    flow_record["id"], FLOW_ERROR, error=f"{type(exc).__name__}: {exc}"
                )
                events.put(("agent.error", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
            events.put(("__done__", None))

    threading.Thread(target=worker, daemon=True).start()
    return MultiStreamPlan(events=events)


def wait_first_multi_event(plan: MultiStreamPlan) -> tuple[str, Any]:
    """Réception SYNCHRONE du premier événement (panne précoce = vraie erreur)."""
    return plan.events.get()


def multi_sse_stream(plan: MultiStreamPlan, first_kind: str, first_payload: Any) -> Iterator[str]:
    """Générateur SSE multi-agents (``event:`` nommés + ``data:``, byte-parité)."""
    try:
        # Rejoue le premier événement déjà consommé (sauf le sentinelle).
        if first_kind != "__done__":
            yield f"event: {first_kind}\n" + _sse(first_payload)
        while True:
            kind, payload = plan.events.get()
            if kind == "__done__":
                break
            if kind == "agent.error":
                yield f"event: {kind}\n" + _sse(payload)
                break
            yield f"event: {kind}\n" + _sse(payload)
        yield "data: [DONE]\n\n"
    except GeneratorExit:
        # Le client a interrompu la génération (bouton Stop du chat).
        raise


# ------------------------------------------------------------------
# Journal « Agent Flow Map » — sessions multi-agents (store injecté)
# ------------------------------------------------------------------


def flow_sessions(store: Any, *, limit: int, status: str | None, statuses: tuple) -> dict:
    """Liste paginée des sessions Flow Map (422 si ``limit`` hors bornes)."""
    if limit < 1 or limit > 200:
        raise ValidationError("limit doit être entre 1 et 200.")
    if status is not None and status not in statuses:
        raise BadRequestError(f"Statut inconnu : '{status}'. Valeurs : {', '.join(statuses)}")
    return {"flows": store.list(limit=limit, status=status), "statuses": list(statuses)}


def flow_session(store: Any, flow_id: str) -> dict:
    """Détail d'une session Flow Map (404 si introuvable)."""
    flow = store.get(flow_id)
    if flow is None:
        raise NotFoundError(f"Session introuvable : {flow_id}")
    return flow


def delete_flow_session(store: Any, flow_id: str) -> dict:
    """Supprime une session Flow Map (404 si introuvable)."""
    if not store.delete(flow_id):
        raise NotFoundError(f"Session introuvable : {flow_id}")
    return {"deleted": flow_id}
