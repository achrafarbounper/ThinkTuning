# project/core/agent_cache.py

"""Intégration de l'agent IA du paquet `ia/` dans l'API principale.

Même schéma que `core/predictor_cache.py` : une instance unique construite
paresseusement au premier appel puis mise en cache (accès protégé par un
verrou), avec rechargement explicite via `reload_agent_runner()`.

Les modules de l'agent utilisent des imports absolus racinés sur le dossier
`ia/` (`from agent...`, `from tools...`) : ce fichier ajoute donc ce dossier
au `sys.path` AVANT d'importer l'agent. Il constitue le point d'entrée
unique : le reste de l'API n'accède à l'agent que via ce module, jamais par
un import direct de `agent.*` / `tools.*`.
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
    "ask_agent_detailed",
    "ask_agent_detailed_streaming",
    "get_agent_runner",
    "list_llm_models",
    "reload_agent_runner",
]

DEFAULT_OLLAMA_URL = "http://192.168.1.184:11434/api/chat"
DEFAULT_MODEL_NAME = "llama3.1:8b"
DEFAULT_TIMEOUT_SECONDS = 600.0

# Taille de fenêtre de contexte (tokens) appliquée par défaut à l'agent,
# transmise à Ollama via `options.num_ctx` (env AGENT_CONTEXT_LENGTH).
DEFAULT_CONTEXT_LENGTH = 2048

# Timeout (secondes) de l'appel GET /api/tags utilisé pour lister les modèles.
OLLAMA_TAGS_TIMEOUT_SECONDS = 10.0

# Runner du modèle par défaut (config env). Variable globale volontairement
# conservée : les tests offline l'injectent via monkeypatch.setattr(agent_cache,
# "_runner", ...) pour remplacer le LLM sans réseau.
_runner: AgentRunner | None = None

# Runners dédiés aux modèles explicitement demandés (sélecteur du chat),
# mis en cache par nom de modèle pour éviter de reconstruire à chaque message.
_override_runners: dict[str, AgentRunner] = {}
_runner_lock = threading.Lock()


def agent_config() -> dict:
    """Configuration courante de l'agent, relue à chaque appel.

    Variables d'environnement :
        AGENT_OLLAMA_URL       URL du endpoint chat Ollama
        AGENT_MODEL_NAME       nom du modèle (ex: llama3.1:8b)
        AGENT_TIMEOUT_SECONDS  timeout en secondes des appels LLM
        AGENT_CONTEXT_LENGTH   taille de fenêtre de contexte (tokens), défaut 2048
    """
    return {
        "ollama_url": os.getenv("AGENT_OLLAMA_URL", DEFAULT_OLLAMA_URL),
        "model": os.getenv("AGENT_MODEL_NAME", DEFAULT_MODEL_NAME),
        "timeout": float(os.getenv("AGENT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
        "context_length": int(
            os.getenv("AGENT_CONTEXT_LENGTH", DEFAULT_CONTEXT_LENGTH)
        ),
    }


def _ollama_base_url() -> str:
    """Racine du serveur Ollama déduite de l'URL du endpoint chat (/api/chat)."""
    url = agent_config()["ollama_url"]
    marker = url.find("/api/")
    return url[:marker].rstrip("/") if marker != -1 else url.rstrip("/")


def _build_runner(model_name: str | None = None, enable_thinking: bool = False) -> AgentRunner:
    """Fabrique un runner de l'agent pour le modèle demandé (ou la config env).

    ``enable_thinking=True`` active le mode « Réflexion » des deux côtés :
    paramètre « think » côté Ollama (LLMClient) et section de prompt +
    extraction <think> côté noyau (AgentCore).
    """
    cfg = agent_config()
    llm = LLMClient(
        cfg["ollama_url"],
        model_name or cfg["model"],
        timeout=cfg["timeout"],
        think=enable_thinking,
        context_length=cfg["context_length"],
    )
    return AgentRunner(AgentCore(llm, enable_thinking=enable_thinking))


def get_agent_runner(model: str | None = None, enable_thinking: bool = False) -> AgentRunner:
    """Renvoie le runner mis en cache, ou le construit au premier appel.

    Sans ``model`` (ou pour le modèle configuré par défaut) et sans mode
    « Réflexion » : runner partagé, injectable dans les tests via
    ``monkeypatch.setattr(agent_cache, "_runner", ...)``. Avec un nom de
    modèle explicite ou ``enable_thinking=True`` : runner dédié, mis en cache
    (clé « <modèle>::thinking » pour ce dernier) afin de supporter le
    sélecteur de modèle et le toggle « Réflexion » du dashboard.
    """
    requested = (model or "").strip()

    if enable_thinking:
        # Runner séparé du chemin historique : le toggle « Réflexion » du chat
        # ne doit ni remplacer ni polluer le runner partagé (injecté en tests).
        with _runner_lock:
            cache_key = f"{requested}::thinking"
            if cache_key not in _override_runners:
                _override_runners[cache_key] = _build_runner(
                    requested or None, enable_thinking=True
                )
            return _override_runners[cache_key]

    if not requested or requested == agent_config()["model"]:
        global _runner
        with _runner_lock:
            if _runner is None:
                _runner = _build_runner()
            return _runner

    with _runner_lock:
        if requested not in _override_runners:
            _override_runners[requested] = _build_runner(requested)
        return _override_runners[requested]


def reload_agent_runner() -> AgentRunner:
    """Reconstruit le runner par défaut et purge les runners surchargés."""
    global _runner
    with _runner_lock:
        _runner = _build_runner()
        _override_runners.clear()
        return _runner


def list_llm_models() -> dict:
    """Liste les modèles installés sur le serveur Ollama (GET /api/tags).

    Retourne ``{"active": <modèle par défaut>, "models": [...]}`` où chaque
    entrée porte ``name``, ``size``, ``modified_at`` et un marqueur
    ``is_default`` sur le modèle configuré côté serveur. Les erreurs réseau
    sont traduites en HTTPException (502 / 504) comme pour ``ask_agent``.
    """
    base_url = _ollama_base_url()
    try:
        response = requests.get(f"{base_url}/api/tags", timeout=OLLAMA_TAGS_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Le serveur Ollama ({base_url}) n'a pas répondu "
                f"dans les {OLLAMA_TAGS_TIMEOUT_SECONDS:.0f}s."
            ),
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(
            status_code=502,
            detail=f"Serveur Ollama injoignable sur {base_url}. Vérifiez qu'il tourne.",
        )
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502, detail=f"Erreur renvoyée par Ollama (HTTP {status})."
        )
    except ValueError as exc:  # réponse non JSON
        raise HTTPException(
            status_code=502, detail=f"Réponse illisible du serveur Ollama ({exc})."
        )

    active_model = agent_config()["model"]
    models = []
    default_seen = False
    for entry in payload.get("models", []):
        name = entry.get("name") or entry.get("model") or ""
        if not name:
            continue
        # Ollama peut renvoyer « llama3.1:latest » quand la config demande
        # « llama3.1 » : correspondance exacte OU même identifiant avant tag.
        is_default = not default_seen and (
            name == active_model or name.split(":", 1)[0] == active_model.split(":", 1)[0]
        )
        default_seen = default_seen or is_default
        models.append(
            {
                "name": name,
                "size": entry.get("size"),
                "modified_at": entry.get("modified_at"),
                "is_default": is_default,
            }
        )
    models.sort(key=lambda item: item["name"])

    return {"active": active_model, "models": models}


def _ask_runner_with_http_errors(runner: AgentRunner, prompt: str, effective_model: str):
    """Exécute runner.ask_detailed(prompt) en traduisant les erreurs réseau.

    Sémantique HTTP :
        Timeout LLM          -> 504
        Ollama injoignable   -> 502
        Erreur HTTP d'Ollama -> 502
    """
    try:
        return runner.ask_detailed(prompt)
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Le LLM ({effective_model}) n'a pas répondu en "
                f"{agent_config()['timeout']:.0f}s."
            ),
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(
            status_code=502,
            detail=(
                f"LLM injoignable sur {agent_config()['ollama_url']}. "
                "Vérifiez qu'Ollama tourne."
            ),
        )
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(status_code=502, detail=f"Erreur renvoyée par le LLM (HTTP {status}).")


def ask_agent(prompt: str, model: str | None = None) -> str:
    """Envoie le prompt à l'agent et traduit les erreurs réseau en HTTP.

    Renvoie la réponse finale seule ; voir ``ask_agent_detailed`` pour la
    version incluant la trace de réflexion.

    ``model`` permet d'utiliser un autre modèle que celui de la configuration
    (sélecteur de modèle du chat) sans toucher aux variables AGENT_*.
    """
    effective_model = (model or "").strip() or agent_config()["model"]
    runner = get_agent_runner(effective_model)
    return _ask_runner_with_http_errors(runner, prompt, effective_model).answer


def ask_agent_detailed(
    prompt: str,
    model: str | None = None,
    enable_thinking: bool = False,
) -> dict:
    """Comme ``ask_agent``, mais renvoie aussi la trace de réflexion.

    Retour : ``{"answer": str, "thinking": str}`` où ``thinking`` vaut ""
    quand le mode « Réflexion » est désactivé ou que le modèle n'a rien émis.
    Même sémantique HTTP que ``ask_agent`` ; ``enable_thinking`` bascule sur
    un runner dédié (prompt enrichi + paramètre « think » Ollama).
    """
    effective_model = (model or "").strip() or agent_config()["model"]
    runner = get_agent_runner(effective_model, enable_thinking)
    result = _ask_runner_with_http_errors(runner, prompt, effective_model)
    return {"answer": result.answer, "thinking": result.thinking}


def ask_agent_detailed_streaming(
    prompt: str,
    model: str | None = None,
    enable_thinking: bool = False,
    on_thinking=None,
) -> dict:
    """Comme ``ask_agent_detailed``, mais la réflexion est diffusée EN TEMPS RÉEL.

    ``on_thinking`` (optionnel — à activer quand ``enable_thinking``) est
    invoqué pour chaque fragment de la trace de raisonnement dès sa production
    par Ollama (l'appel est `stream: true`). Retour : ``{"answer", "thinking"}``
    comme ``ask_agent_detailed`` ; même sémantique HTTP (504/502).
    """
    effective_model = (model or "").strip() or agent_config()["model"]
    runner = get_agent_runner(effective_model, enable_thinking)
    try:
        result = runner.ask_detailed_streaming(prompt, on_thinking=on_thinking)
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Le LLM ({effective_model}) n'a pas répondu en "
                f"{agent_config()['timeout']:.0f}s."
            ),
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(
            status_code=502,
            detail=(
                f"LLM injoignable sur {agent_config()['ollama_url']}. "
                "Vérifiez qu'Ollama tourne."
            ),
        )
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502, detail=f"Erreur renvoyée par le LLM (HTTP {status})."
        )
    return {"answer": result.answer, "thinking": result.thinking}