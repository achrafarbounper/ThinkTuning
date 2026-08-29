# project/api/routes/ai_chat.py

"""Endpoint de chat streaming (/api/ai) pour l'interface Copilot du dashboard.

Contrat d'entrée  : POST /api/ai  {"message": str, "history": [{"role", "content"}, ...],
                                   "enable_thinking": bool}
Contrat de sortie : flux SSE ->
    data: {"thinking_delta": "..."}   réflexion (mode activé), émise EN TEMPS RÉEL
    data: {"delta": "..."}            réponse finale, fragment par fragment
    data: [DONE]

L'agent IA réel (paquet `ia/`, exposé via `core.agent_cache`) tourne dans un
thread de travail ; chaque événement est poussé dans une file puis réémis en
SSE par un générateur asynchrone, donc l'appel vers Ollama ne gèle pas
l'event loop. L'appel LLM est `stream: true` : la trace `message.thinking`
est diffusée pendant la génération, avant la réponse finale.
La réception bloquante du PREMIER événement traduit une panne réseau
d'avant-stream (Ollama indisponible, timeout) en vraie réponse HTTP 502/504.
"""

import asyncio
import json
import queue
import threading
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.dependencies.auth import require_api_key
from core.agent_cache import ask_agent_detailed_streaming, list_llm_models
from core.session_store import get_session_store
from ia.agent.encoding import repair_utf8_mojibake

router = APIRouter(prefix="/api", tags=["AI Chat"])

# Nombre maximal d'échanges (paires user/assistant) rejoués à l'agent.
MAX_HISTORY_TURNS = 5


class ChatMessageIn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Message de l'utilisateur.")
    history: list[ChatMessageIn] = []
    model: str | None = Field(
        None,
        max_length=100,
        description=(
            "Nom du modèle LLM Ollama à utiliser (sélecteur du chat). "
            "Absent ou vide : modèle par défaut de la configuration serveur."
        ),
    )
    enable_thinking: bool = Field(
        False,
        description=(
            "Mode « Réflexion » : l'agent raisonne avant de répondre et la "
            "trace est diffusée via les événements SSE thinking_delta. "
            "Désactivé par défaut (comportement historique inchangé)."
        ),
    )
    session_id: str | None = Field(
        None,
        description=(
            "Session de conversation (core/session_store) où journaliser "
            "l'échange ; absent : aucune persistance côté serveur."
        ),
    )


def _build_prompt(req: ChatRequest) -> str:
    """Aplatit l'historique récent + le message en un prompt unique.

    AgentCore.run() est mono-tour : on rejoue les derniers échanges en tête
    de prompt pour donner un minimum de contexte conversationnel à l'agent.

    Chaque contenu est au préalable normalisé via ``repair_utf8_mojibake`` :
    sans cela, un historique vieillissant déjà corrompu resservirait des
    caractères cassés au LLM, qui les réémettrait (double encodage cumulatif).
    """
    recent = req.history[-MAX_HISTORY_TURNS * 2 :]
    if not recent:
        return repair_utf8_mojibake(req.message)
    transcript = "\n".join(
        f"{'Utilisateur' if msg.role == 'user' else 'Assistant'} : "
        f"{repair_utf8_mojibake(msg.content)}"
        for msg in recent
    )
    return (
        "Historique récent de la conversation :\n"
        f"{transcript}\n\n"
        f"Nouvelle question de l'utilisateur : {repair_utf8_mojibake(req.message)}"
    )


def _sse(payload: dict | str) -> str:
    """Formate une charge utile en événement SSE (`data: ...` + saut de ligne)."""
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return "data: " + data + "\n\n"


def _stream_fragments(text: str):
    """Découpe un texte en fragments mot à mot (générateur synchrone)."""
    for word in text.split(" "):
        yield word


@router.get("/models")
def list_available_llm_models(_: bool = Depends(require_api_key)) -> dict:
    """Liste les modèles LLM installés sur le serveur Ollama (sélecteur du chat).

    Retourne ``{"active": ..., "models": [{"name", "size", "modified_at",
    "is_default"}, ...]}``. Les erreurs Ollama sont déjà traduites en
    502/504 par ``core.agent_cache.list_llm_models``.
    """
    return list_llm_models()


@router.post("/ai")
def ai_chat(req: ChatRequest, _: bool = Depends(require_api_key)) -> StreamingResponse:
    """Chat avec l'agent IA, réponse RÉELLEMENT diffusée en Server-Sent Events.

    L'agent tourne dans un thread de travail (file d'attente intermédiaire) et
    l'appel Ollama est `stream: true` :
      - mode « Réflexion » : les fragments de ``message.thinking`` sont émis
        EN TEMPS RÉEL (événements ``thinking_delta``) pendant la génération ;
      - la réponse finale est ensuite diffusée fragment par fragment
        (événements ``delta``), jusqu'à ``data: [DONE]``.

    Une panne réseau D'AVANT-stream (Ollama indisponible, timeout) devient une
    vraie réponse HTTP 502/504 : le premier événement est reçu de façon
    bloquante avant d'ouvrir le flux SSE.
    """
    events: queue.Queue[tuple[str, object]] = queue.Queue()

    def _emit_thinking(chunk: str) -> None:
        events.put(("thinking", chunk))

    def _emit_answer(answer: str) -> None:
        for word in _stream_fragments(answer):
            events.put(("delta", word + " "))
            time.sleep(0.03)  # cadence d'émission des fragments de réponse

    def worker() -> None:
        try:
            result = ask_agent_detailed_streaming(
                _build_prompt(req),
                req.model,
                req.enable_thinking,
                # Sans « Réflexion », aucun événement thinking_delta n'est émis
                # (contrat historique préservé).
                on_thinking=_emit_thinking if req.enable_thinking else None,
            )
            answer = repair_utf8_mojibake(result["answer"])
            _emit_answer(answer)
            # Persistance best-effort de l'échange dans la session demandée.
            if req.session_id:
                try:
                    store = get_session_store()
                    store.append_message(req.session_id, "user", repair_utf8_mojibake(req.message))
                    store.append_message(req.session_id, "assistant", answer)
                except Exception:  # pragma: no cover - persistance optionnelle
                    pass
        except HTTPException as exc:  # panne réseau déjà traduite par agent_cache
            events.put(("http_error", exc))
        except Exception as exc:
            events.put(("error", str(exc)))
        finally:
            events.put(("done", None))

    # Thread de travail (daemon) : l'agent ne doit pas bloquer l'event loop.
    # Il se termine seul, même quand le client coupe la connexion (Stop).
    threading.Thread(target=worker, daemon=True).start()

    # Réception SYNCHRONE du premier événement : si le LLM est déjà en panne,
    # on renvoie une vraie erreur HTTP au lieu d'un flux SSE interrompu.
    first_kind, first_payload = events.get()
    if first_kind == "http_error":
        raise first_payload
    if first_kind == "error":
        raise HTTPException(status_code=502, detail=str(first_payload))

    async def _sse_stream() -> AsyncIterator[str]:
        try:
            # Cas limite : réponse vide (aucun thinking, aucun delta) — le
            # premier événement consommé est « done », le flux s'arrête net.
            if first_kind == "done":
                yield "data: [DONE]\n\n"
                return
            # Le premier événement a déjà été consommé : rejoué en tête du flux.
            if first_kind == "thinking":
                yield _sse({"thinking_delta": first_payload})
            elif first_kind == "delta":
                yield _sse({"delta": first_payload})

            while True:
                kind, payload = await asyncio.to_thread(events.get)
                if kind == "done":
                    break
                if kind in ("http_error", "error"):
                    # Panne SURVENUE en cours de flux : on ne peut plus
                    # changer le statut HTTP, on émet un événement d'erreur.
                    detail = payload.detail if kind == "http_error" else str(payload)
                    yield _sse({"error": detail})
                    yield "data: [DONE]\n\n"
                    return
                field = "thinking_delta" if kind == "thinking" else "delta"
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