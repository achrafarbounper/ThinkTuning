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
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.dependencies.auth import require_api_key
from core.agent_settings import get_agent_settings, save_agent_settings
from core.agent_cache import (
    REQUIRED_ARGS,
    TOOL_META,
    TOOLS,
    _openrouter_chat_url,
    agent_config,
    ask_agent_decision,
    ask_agent_decision_streaming,
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

router = APIRouter(prefix="/api/agent", tags=["Agent IA"])

# Timeout (secondes) des sondes de connectivité du bouton « Tester ».
CONNECTIVITY_TIMEOUT_SECONDS = 8.0


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

    provider: Optional[str] = Field(None, description="« ollama » ou « openrouter ».")
    model: Optional[str] = Field(None, max_length=200)
    ollama_url: Optional[str] = Field(None, max_length=500)
    openrouter_url: Optional[str] = Field(None, max_length=500)
    openrouter_api_key: Optional[str] = Field(None, max_length=300)
    timeout_seconds: Optional[float] = Field(None, ge=10, le=3600)
    context_length: Optional[int] = Field(None, ge=512, le=131072)
    temperature: Optional[float] = Field(None, ge=0, le=2)


class ConnectivityTestRequest(BaseModel):
    """Sonde de connectivité ; champs absents -> valeurs effectives courantes."""

    provider: Optional[str] = None
    ollama_url: Optional[str] = None
    openrouter_url: Optional[str] = None
    openrouter_api_key: Optional[str] = None


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
        result = TOOLS[tool](**request.args)
    except TypeError as exc:
        raise HTTPException(status_code=400, detail=f"Arguments invalides pour {tool} : {exc}")

    return {"tool": tool, "result": result}


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
            return []
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
    return kept[-MAX_SESSION_CONTEXT_TURNS * 2 :]


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
    return {
        "settings": {
            **values,
            "has_openrouter_api_key": bool(api_key),
            "openrouter_api_key_masked": _mask_key(api_key),
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
        return {
            "ok": False,
            "detail": f"Délai dépassé ({CONNECTIVITY_TIMEOUT_SECONDS:.0f}s) sur {probe_url}.",
        }
    except requests.exceptions.ConnectionError:
        return {"ok": False, "detail": f"Injoignable : {probe_url}.{hint}"}
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        extra = " Clé API invalide ?" if status == 401 else hint
        return {"ok": False, "detail": f"HTTP {status} sur {probe_url}.{extra}"}
    except ValueError:
        return {"ok": False, "detail": f"Réponse illisible de {probe_url}."}

    return {"ok": True, "detail": success_detail}