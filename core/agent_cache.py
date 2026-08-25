# project/core/agent_cache.py

"""Intégration de l'agent IA du paquet `ia/` dans l'API principale.

Même schéma que `core/predictor_cache.py` : une instance unique construite
paresseusement au premier appel puis mise en cache (accès protégé par un
verrou), avec rechargement explicite via `reload_agent_runner()`.

Les modules de l'agent utilisent des imports absolus racinés sur le dossier
`ia/` (`from agent...`, `from tools...`, cf. ia/api_server.py) : ce fichier
ajoute donc ce dossier au `sys.path` AVANT d'importer l'agent. Il constitue
le point d'entrée unique : le reste de l'API n'accède à l'agent que via ce
module, jamais par un import direct de `agent.*` / `tools.*`.
"""

import os
import sys
import threading

import requests
from fastapi import HTTPException

IA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ia")
if IA_DIR not in sys.path:
    sys.path.insert(0, IA_DIR)

# --- Imports de l'agent (à placer APRÈS l'insertion du sys.path ci-dessus) ----
from agent.agent_core import AgentCore  # noqa: E402
from agent.llm_client import LLMClient  # noqa: E402
from agent.runner import AgentRunner  # noqa: E402
from tools.tool_registry import REQUIRED_ARGS, TOOLS  # noqa: E402,F401

# Ré-exportés pour que le reste de l'API consomme l'agent uniquement ici.
__all__ = [
    "AgentCore",
    "AgentRunner",
    "LLMClient",
    "REQUIRED_ARGS",
    "TOOLS",
    "agent_config",
    "ask_agent",
    "get_agent_runner",
    "reload_agent_runner",
]

DEFAULT_OLLAMA_URL = "http://192.168.1.184:11434/api/chat"
DEFAULT_MODEL_NAME = "llama3.1:8b"
DEFAULT_TIMEOUT_SECONDS = 600.0

_runner: AgentRunner | None = None
_runner_lock = threading.Lock()


def agent_config() -> dict:
    """Configuration courante de l'agent, relue à chaque appel.

    Variables d'environnement (identiques à ia/api_server.py) :
        AGENT_OLLAMA_URL       URL du endpoint chat Ollama
        AGENT_MODEL_NAME       nom du modèle (ex: llama3.1:8b)
        AGENT_TIMEOUT_SECONDS  timeout en secondes des appels LLM
    """
    return {
        "ollama_url": os.getenv("AGENT_OLLAMA_URL", DEFAULT_OLLAMA_URL),
        "model": os.getenv("AGENT_MODEL_NAME", DEFAULT_MODEL_NAME),
        "timeout": float(os.getenv("AGENT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
    }


def _build_runner() -> AgentRunner:
    """Fabrique le runner de l'agent à partir de la configuration env."""
    cfg = agent_config()
    llm = LLMClient(cfg["ollama_url"], cfg["model"], timeout=cfg["timeout"])
    return AgentRunner(AgentCore(llm))


def get_agent_runner() -> AgentRunner:
    """Renvoie le runner mis en cache, ou le construit au premier appel."""
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = _build_runner()
        return _runner


def reload_agent_runner() -> AgentRunner:
    """Reconstruit le runner (après un changement des variables AGENT_*)."""
    global _runner
    with _runner_lock:
        _runner = _build_runner()
        return _runner


def ask_agent(prompt: str) -> str:
    """Envoie le prompt à l'agent et traduit les erreurs réseau en HTTP.

    Même sémantique que POST /ask de ia/api_server.py :
        Timeout LLM          -> 504
        Ollama injoignable   -> 502
        Erreur HTTP d'Ollama -> 502
    """
    cfg = agent_config()
    runner = get_agent_runner()
    try:
        return runner.ask(prompt)
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail=f"Le LLM ({cfg['model']}) n'a pas répondu en {cfg['timeout']:.0f}s.",
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(
            status_code=502,
            detail=f"LLM injoignable sur {cfg['ollama_url']}. Vérifiez qu'Ollama tourne.",
        )
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(status_code=502, detail=f"Erreur renvoyée par le LLM (HTTP {status}).")