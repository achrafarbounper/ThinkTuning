# project/api/routes/agent.py

"""Endpoints de l'agent IA intégrés au package api.

Reprend la logique du serveur autonome `ia/api_server.py` sous le préfixe
`/api/agent`, avec les conventions du package api (router, dépendance
`require_api_key`, middlewares CORS / rate limit / métriques partagés) :

    GET  /api/agent/status      statut + config + outils disponibles (public)
    GET  /api/agent/tools       outils et leurs arguments requis
    POST /api/agent/tools/run   exécution directe d'un outil (sans passer par le LLM)
    POST /api/agent/ask         prompt libre -> l'agent planifie les outils puis répond

Les endpoints sont des `def` (et non async) : FastAPI les exécute dans un
threadpool, donc l'appel bloquant vers Ollama ne gèle pas l'event loop.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.dependencies.auth import require_api_key
from core.agent_cache import REQUIRED_ARGS, TOOLS, agent_config, ask_agent

router = APIRouter(prefix="/api/agent", tags=["Agent IA"])


# --- Schémas Pydantic -------------------------------------------------------------

class AskRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Instruction envoyée à l'agent.")


class AskResponse(BaseModel):
    response: str
    model: str


class ToolInfo(BaseModel):
    name: str
    required_args: list[str]


class ToolRunRequest(BaseModel):
    tool: str = Field(..., description="Nom de l'outil (ex: 'add', 'write_file').")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments de l'outil.")


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
        "model": cfg["model"],
        "ollama_url": cfg["ollama_url"],
        "timeout_seconds": cfg["timeout"],
        "auth_required": True,  # l'API principale applique toujours X-API-Key
        "tools": sorted(TOOLS),
    }


@router.get("/tools", response_model=list[ToolInfo])
def list_tools(_: bool = Depends(require_api_key)):
    """Liste des outils que l'agent peut appeler, avec leurs arguments requis."""
    return [
        ToolInfo(name=name, required_args=REQUIRED_ARGS[name])
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
    """Prompt libre : l'agent décide des outils à appeler puis renvoie sa réponse finale.

    La traduction des erreurs réseau (Timeout -> 504, ConnectionError -> 502)
    est centralisée dans `core.agent_cache.ask_agent`.
    """
    answer = ask_agent(request.prompt)
    return AskResponse(response=answer, model=agent_config()["model"])