# project/api/routes/ai_chat.py

"""Endpoint de chat streaming (/api/ai) pour l'interface Copilot du dashboard.

Contrat d'entrée  : POST /api/ai  {"message": str, "history": [{"role", "content"}, ...]}
Contrat de sortie : flux SSE -> data: {"delta": "..."} ... data: [DONE]

L'agent IA réel (paquet `ia/`, exposé via `core.agent_cache`) produit la
réponse ; le découpage en fragments SSE est géré par `_sse_generator()`.
L'appel LLM étant bloquant, l'endpoint est un `def` : FastAPI l'exécute dans
un threadpool, donc l'appel vers Ollama ne gèle pas l'event loop.
L'appel a lieu AVANT le streaming afin que
les erreurs réseau deviennent des réponses HTTP propres (502/504).
"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.dependencies.auth import require_api_key
from core.agent_cache import ask_agent

router = APIRouter(prefix="/api", tags=["AI Chat"])

# Nombre maximal d'échanges (paires user/assistant) rejoués à l'agent.
MAX_HISTORY_TURNS = 5


class ChatMessageIn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Message de l'utilisateur.")
    history: list[ChatMessageIn] = []


def _build_prompt(req: ChatRequest) -> str:
    """Aplatit l'historique récent + le message en un prompt unique.

    AgentCore.run() est mono-tour : on rejoue les derniers échanges en tête
    de prompt pour donner un minimum de contexte conversationnel à l'agent.
    """
    recent = req.history[-MAX_HISTORY_TURNS * 2 :]
    if not recent:
        return req.message
    transcript = "\n".join(
        f"{'Utilisateur' if msg.role == 'user' else 'Assistant'} : {msg.content}"
        for msg in recent
    )
    return (
        "Historique récent de la conversation :\n"
        f"{transcript}\n\n"
        f"Nouvelle question de l'utilisateur : {req.message}"
    )


def _sse(payload: dict | str) -> str:
    """Formate une charge utile en événement SSE (`data: ...` + saut de ligne)."""
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return "data: " + data + "\n\n"


async def _sse_generator(reply: str) -> AsyncIterator[str]:
    """Diffuse la réponse fragment par fragment au format Server-Sent Events."""
    try:
        for word in reply.split(" "):
            yield _sse({"delta": word + " "})
            await asyncio.sleep(0.02)  # cadence d'émission des fragments
        yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        # Le client a interrompu la génération (bouton Stop du chat).
        raise


@router.post("/ai")
def ai_chat(req: ChatRequest, _: bool = Depends(require_api_key)) -> StreamingResponse:
    """Chat avec l'agent IA, réponse diffusée en Server-Sent Events."""
    reply = ask_agent(_build_prompt(req))
    return StreamingResponse(
        _sse_generator(reply),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # désactive le buffering nginx
        },
    )