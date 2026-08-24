"""
API FastAPI pour exposer l'agent (paquet `ia/`) en HTTP.

Lancement depuis la racine du projet :

    # Windows PowerShell
    # $env:AGENT_OLLAMA_URL="http://192.168.1.184:11434/api/chat"
    # $env:AGENT_MODEL_NAME="llama3.1:8b"
    # $env:AGENT_API_KEY="change-me-agent-key"   # optionnel -> active X-API-Key
    uvicorn ia.api_server:app --host 0.0.0.0 --port 8001

Routes :
    GET  /health       statut + config + outils disponibles
    GET  /tools        outils et leurs arguments requis
    POST /tools/run    exécution directe d'un outil (sans passer par le LLM)
    POST /ask          prompt libre -> l'agent planifie les outils puis répond
"""

import os
import sys
from contextlib import asynccontextmanager
from typing import Any

# --- Imports du paquet agent -----------------------------------------------
# Le code de l'agent utilise des imports absolus (`from agent...`,
# `from tools...`) écrits pour être lancés depuis le dossier `ia/`.
# On ajoute donc ce dossier au sys.path pour que l'API fonctionne quel que
# soit le répertoire depuis lequel uvicorn est lancé.
IA_DIR = os.path.dirname(os.path.abspath(__file__))
if IA_DIR not in sys.path:
    sys.path.insert(0, IA_DIR)

import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent.agent_core import AgentCore
from agent.llm_client import LLMClient
from agent.runner import AgentRunner
from tools.tool_registry import REQUIRED_ARGS, TOOLS

# --- Configuration -----------------------------------------------------------
OLLAMA_URL = os.getenv("AGENT_OLLAMA_URL", "http://192.168.1.184:11434/api/chat")
MODEL_NAME = os.getenv("AGENT_MODEL_NAME", "llama3.1:8b")
LLM_TIMEOUT = float(os.getenv("AGENT_TIMEOUT_SECONDS", "120"))
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if origin.strip()
]


def _agent_api_key() -> str | None:
    """Clé API de l'agent — relue à chaque requête (comme api.dependencies.auth)."""
    return os.getenv("AGENT_API_KEY") or None


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> bool:
    """Auth optionnelle : active uniquement si AGENT_API_KEY est définie."""
    expected_key = _agent_api_key()
    if expected_key is None:
        return True  # auth désactivée (dev local)
    if x_api_key != expected_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")
    return True


# --- Schémas Pydantic ----------------------------------------------------------
class AskRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Instruction envoyée à l'agent.")


class AskResponse(BaseModel):
    response: str
    model: str


class ToolRunRequest(BaseModel):
    tool: str = Field(..., description="Nom de l'outil (ex: 'add', 'write_file').")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments de l'outil.")


class ToolInfo(BaseModel):
    name: str
    required_args: list[str]


# --- Cycle de vie ---------------------------------------------------------------
def build_runner() -> AgentRunner:
    """Fabrique le runner de l'agent (monkeypatchable dans les tests)."""
    llm = LLMClient(OLLAMA_URL, MODEL_NAME, timeout=LLM_TIMEOUT)
    return AgentRunner(AgentCore(llm))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # L'agent est instancié UNE seule fois au démarrage, pas à chaque requête.
    app.state.runner = build_runner()
    yield


app = FastAPI(
    title="Agent API",
    description="Expose l'agent LLM (Ollama) et ses outils Python en HTTP.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Endpoints -------------------------------------------------------------------
@app.get("/health", tags=["Health"])
def health():
    """Statut rapide de l'agent : modèle visé, outils dispo, état de l'auth."""
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "ollama_url": OLLAMA_URL,
        "auth_enabled": _agent_api_key() is not None,
        "tools": sorted(TOOLS),
    }


@app.get("/tools", response_model=list[ToolInfo], tags=["Tools"])
def list_tools(_: bool = Depends(require_api_key)):
    """Liste des outils que l'agent peut appeler, avec leurs arguments requis."""
    return [
        ToolInfo(name=name, required_args=REQUIRED_ARGS[name])
        for name in sorted(TOOLS)
    ]


@app.post("/tools/run", tags=["Tools"])
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


@app.post("/ask", response_model=AskResponse, tags=["Agent"])
def ask(request: AskRequest, _: bool = Depends(require_api_key)):
    """Prompt libre : l'agent décide des outils à appeler puis renvoie sa réponse finale.

    Les endpoints sont des `def` (et non async) : FastAPI les exécute dans un
    threadpool, donc l'appel bloquant vers Ollama ne gèle pas l'event loop.
    """
    runner: AgentRunner = app.state.runner
    try:
        answer = runner.ask(request.prompt)
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail=f"Le LLM ({MODEL_NAME}) n'a pas répondu en {LLM_TIMEOUT:.0f}s.",
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(
            status_code=502,
            detail=f"LLM injoignable sur {OLLAMA_URL}. Vérifiez qu'Ollama tourne.",
        )
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(status_code=502, detail=f"Erreur renvoyée par le LLM (HTTP {status}).")

    return AskResponse(response=answer, model=MODEL_NAME)