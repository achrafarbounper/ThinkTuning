"""Mini-backend de test pour l'interface de chat — 100 % autonome.

Ne dépend d'aucun module du projet : il sert uniquement à tester l'interface
de chat (streaming SSE) sans lancer toute l'API FastAPI ni les modèles ML.

Lancement (depuis la racine du projet) :

    venv\\Scripts\\python scripts\\mock_ai_backend.py

Le serveur écoute sur http://127.0.0.1:8000 et expose exactement le même
contrat que `api/routes/ai_chat.py` :

    GET  /api/models  -> {"active": "...", "models": [{"name", ...}]}
    POST /api/ai  {"message": "…", "history": [...], "model": "…"}
      -> flux SSE : data: {"delta": "…"} ... data: [DONE]

Astuce : si le port 8000 est déjà occupé par la vraie API, lancez
`PORT=8001 venv\\Scripts\\python scripts\\mock_ai_backend.py` et adaptez le
`target` du proxy dans `dashboard/vite.config.js`.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Mock AI Chat Backend", version="1.0.0")

# Utile uniquement si vous accédez directement au serveur (sans le proxy Vite).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatMessageIn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessageIn] = []
    model: str | None = None  # modèle choisi dans le sélecteur (ignoré ici)


# Modèles factices servis par GET /api/models pour alimenter le sélecteur.
MOCK_MODELS = [
    {"name": "mock-echo-mini", "size": 1_000_000_000, "modified_at": "2026-08-01T00:00:00Z"},
    {"name": "mock-echo-large", "size": 4_000_000_000, "modified_at": "2026-08-15T00:00:00Z"},
]


@app.get("/api/models")
def list_models() -> dict:
    """Même contrat que api/routes/ai_chat.py : liste + modèle actif."""
    return {"active": "mock-echo-mini", "models": MOCK_MODELS}


def _build_reply(req: ChatRequest) -> str:
    """Réponse factice : met en écho le dernier message utilisateur."""
    turn = len(req.history) // 2 + 1
    return (
        f"Bonjour ! Vous avez écrit (tour {turn}) : « {req.message} ».\n\n"
        "Ceci est une réponse **factice** générée par le mini-serveur de test "
        "`scripts/mock_ai_backend.py`, diffusée fragment par fragment pour "
        "démontrer le streaming côté interface."
    )


def _sse(payload: dict | str) -> str:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n"


async def _sse_generator(req: ChatRequest) -> AsyncIterator[str]:
    reply = _build_reply(req)
    try:
        for word in reply.split(" "):
            yield _sse({"delta": word + " "})
            await asyncio.sleep(0.04)
        yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        raise


@app.post("/api/ai")
async def ai_chat(req: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        _sse_generator(req),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
