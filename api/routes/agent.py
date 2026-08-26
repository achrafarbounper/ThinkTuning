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

from typing import Any, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.dependencies.auth import require_api_key
from core.agent_settings import get_agent_settings, save_agent_settings
from core.agent_cache import (
    REQUIRED_ARGS,
    TOOLS,
    _openrouter_chat_url,
    agent_config,
    ask_agent,
    reload_agent_runner,
)

router = APIRouter(prefix="/api/agent", tags=["Agent IA"])

# Timeout (secondes) des sondes de connectivité du bouton « Tester ».
CONNECTIVITY_TIMEOUT_SECONDS = 8.0


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