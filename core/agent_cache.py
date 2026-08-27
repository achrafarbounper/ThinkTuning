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

from core.agent_settings import get_agent_settings

# --- Imports de l'agent (à placer APRÈS l'insertion du sys.path ci-dessus) ----
from agent.agent_core import AgentCore  # noqa: E402
from agent.llm_client import LLMClient  # noqa: E402
from agent.runner import AgentRunner  # noqa: E402
from tools.tool_registry import REQUIRED_ARGS, TOOL_META, TOOLS  # noqa: E402,F401

# File de validation humaine — ré-exportée pour les routes /api/agent/approvals.
from core.approval_store import ApprovalStore  # noqa: E402,F401
from core.approval_store import get_approval_store as _get_approval_store  # noqa: E402

# Ré-exportés pour que le reste de l'API consomme l'agent uniquement ici.
__all__ = [
    "AgentCore",
    "AgentRunner",
    "LLMClient",
    "ApprovalStore",
    "REQUIRED_ARGS",
    "TOOL_META",
    "TOOLS",
    "agent_config",
    "ask_agent",
    "ask_agent_decision",
    "ask_agent_decision_streaming",
    "ask_agent_detailed",
    "ask_agent_detailed_streaming",
    "get_agent_runner",
    "list_llm_models",
    "reload_agent_runner",
]

DEFAULT_OLLAMA_URL = "http://192.168.1.184:11434/api/chat"
DEFAULT_MODEL_NAME = "llama3.1:8b"
DEFAULT_TIMEOUT_SECONDS = 600.0

# Provider LLM de l'agent : « ollama » (historique) ou « openrouter ».
# Sélection via AGENT_PROVIDER ; la clé OpenRouter se règle via OPENROUTER_API_KEY
# (requise dès qu'AGENT_PROVIDER=openrouter).
DEFAULT_PROVIDER = "ollama"
DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Modèle par défaut si AGENT_MODEL_NAME n'est pas défini (IDs OpenRouter au
# format « vendor/model »).
DEFAULT_OPENROUTER_MODEL_NAME = "deepseek/deepseek-r1:free"

# Taille de fenêtre de contexte (tokens) appliquée par défaut à l'agent,
# transmise à Ollama via `options.num_ctx` (env AGENT_CONTEXT_LENGTH).
DEFAULT_CONTEXT_LENGTH = 2048

# Timeout (secondes) des appels GET listant les modèles (/api/tags Ollama,
# /api/v1/models OpenRouter).
LIST_MODELS_TIMEOUT_SECONDS = 10.0

# Runner du modèle par défaut (config env). Variable globale volontairement
# conservée : les tests offline l'injectent via monkeypatch.setattr(agent_cache,
# "_runner", ...) pour remplacer le LLM sans réseau.
_runner: AgentRunner | None = None

# Runners dédiés aux modèles explicitement demandés (sélecteur du chat),
# mis en cache par nom de modèle pour éviter de reconstruire à chaque message.
_override_runners: dict[str, AgentRunner] = {}
_runner_lock = threading.Lock()
def _openrouter_chat_url(url: str | None) -> str:
    """Normalise une URL OpenRouter vers l'endpoint chat complet.

    La page Paramètres du dashboard enregistre la RACINE de l'API
    (« https://openrouter.ai/api/v1 ») tandis que la configuration serveur
    (AGENT_OPENROUTER_URL) et les tests utilisent l'endpoint complet
    (« https://openrouter.ai/api/v1/chat/completions »). On accepte les deux :
    un suffixe « /chat/completions » manquant est ajouté.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_OPENROUTER_URL
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"





def agent_config() -> dict:
    """Configuration courante de l'agent, relue à chaque appel.

    Sources par priorité décroissante (via ``core.agent_settings``) :
        1. base SQLite des paramètres (page Paramètres du dashboard) ;
        2. variables d'environnement ;
        3. défauts historiques du module.

    Variables d'environnement utilisées en repli :
        AGENT_PROVIDER         « ollama » (défaut) ou « openrouter »
        AGENT_OLLAMA_URL       URL du endpoint chat Ollama
        AGENT_OPENROUTER_URL   URL du endpoint chat OpenRouter (compatible OpenAI)
        OPENROUTER_API_KEY     clé API OpenRouter (requise si provider=openrouter)
        AGENT_MODEL_NAME       nom du modèle (ex: llama3.1:8b ou vendor/model)
        AGENT_TIMEOUT_SECONDS  timeout en secondes des appels LLM
        AGENT_CONTEXT_LENGTH   taille de fenêtre de contexte (tokens), défaut 2048
    """
    settings = get_agent_settings()

    def val(key: str):
        return settings[key]["value"]

    provider = val("provider") or DEFAULT_PROVIDER
    # Défaut dépendant du provider : les modèles OpenRouter portent un
    # identifiant « vendor/model » incompatible avec la convention Ollama.
    model = val("model") or (
        DEFAULT_OPENROUTER_MODEL_NAME if provider == "openrouter" else DEFAULT_MODEL_NAME
    )
    timeout_raw = val("timeout_seconds")
    context_raw = val("context_length")
    temperature_raw = val("temperature")

    return {
        "provider": provider,
        "ollama_url": val("ollama_url") or DEFAULT_OLLAMA_URL,
        "openrouter_url": _openrouter_chat_url(
            val("openrouter_url") or DEFAULT_OPENROUTER_URL
        ),
        "openrouter_api_key": val("openrouter_api_key") or "",
        "model": model,
        "timeout": (
            float(timeout_raw) if timeout_raw is not None else DEFAULT_TIMEOUT_SECONDS
        ),
        "context_length": (
            int(context_raw) if context_raw is not None else DEFAULT_CONTEXT_LENGTH
        ),
        # None -> LLMClient applique son DEFAULT_TEMPERATURE historique.
        "temperature": float(temperature_raw) if temperature_raw is not None else None,
    }


def _llm_endpoint(cfg: dict) -> tuple[str, str | None]:
    """URL d'appel et clé API correspondant au provider configuré.

    Retourne ``(url, api_key)`` : clé ``None`` pour Ollama ; pour OpenRouter,
    la clé vient de la config effective (SQLite puis env OPENROUTER_API_KEY)
    et une HTTPException 500 est levée quand elle manque (mauvaise
    configuration serveur).
    """
    if cfg["provider"] == "openrouter":
        api_key = (cfg["openrouter_api_key"] or "").strip()
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Provider LLM « openrouter » sélectionné mais aucune clé API "
                    "n'est définie. Renseignez OPENROUTER_API_KEY (ou enregistrez-la "
                    "dans la page Paramètres)."
                ),
            )
        return cfg["openrouter_url"], api_key
    return cfg["ollama_url"], None


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
    url, api_key = _llm_endpoint(cfg)
    llm = LLMClient(
        url,
        model_name or cfg["model"],
        timeout=cfg["timeout"],
        temperature=cfg["temperature"],
        think=enable_thinking,
        context_length=cfg["context_length"],
        provider=cfg["provider"],
        api_key=api_key,
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
    """Liste les modèles disponibles chez le provider configuré.

    Retourne ``{"active": <modèle par défaut>, "models": [...]}`` où chaque
    entrée porte ``name``, ``size``, ``modified_at`` et un marqueur
    ``is_default`` sur le modèle configuré côté serveur. Les erreurs réseau
    sont traduites en HTTPException (502 / 504) comme pour ``ask_agent``.
    """
    cfg = agent_config()
    if cfg["provider"] == "openrouter":
        return _list_openrouter_models(cfg)

    base_url = _ollama_base_url()
    try:
        response = requests.get(
            f"{base_url}/api/tags", timeout=LIST_MODELS_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Le serveur Ollama ({base_url}) n'a pas répondu "
                f"dans les {LIST_MODELS_TIMEOUT_SECONDS:.0f}s."
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


def _openrouter_base_api() -> str:
    """Racine de l'API OpenRouter déduite de l'URL du endpoint chat.

    « https://openrouter.ai/api/v1/chat/completions » -> « https://openrouter.ai/api/v1 ».
    """
    url = agent_config()["openrouter_url"]
    marker = url.find("/chat/completions")
    if marker != -1:
        return url[:marker].rstrip("/")
    return url.rsplit("/", 1)[0] if "/" in url.rstrip("/") else url


def _list_openrouter_models(cfg: dict) -> dict:
    """Liste les modèles OpenRouter (GET /api/v1/models).

    L'endpoint est public mais l'entête Bearer est envoyé quand la clé est
    définie. Mapping sur le même contrat que la liste Ollama : ``name`` porte
    l'identifiant complet (« vendor/model ») attendu par le chat ; ``size`` et
    ``modified_at`` n'existent pas côté OpenRouter (``None``).
    """
    url = f"{_openrouter_base_api()}/models"
    # Clé effective (SQLite puis env) : entête Bearer envoyé seulement si définie.
    api_key = (cfg["openrouter_api_key"] or "").strip()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    try:
        response = requests.get(url, headers=headers, timeout=LIST_MODELS_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail=(
                f"L'API OpenRouter ({url}) n'a pas répondu "
                f"dans les {LIST_MODELS_TIMEOUT_SECONDS:.0f}s."
            ),
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(
            status_code=502,
            detail=f"API OpenRouter injoignable sur {url}.",
        )
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502,
            detail=(
                f"Erreur renvoyée par OpenRouter (HTTP {status})."
                + (" Clé OPENROUTER_API_KEY invalide ?" if status == 401 else "")
            ),
        )
    except ValueError as exc:  # réponse non JSON
        raise HTTPException(
            status_code=502, detail=f"Réponse illisible de l'API OpenRouter ({exc})."
        )

    active_model = cfg["model"]
    models = []
    for entry in payload.get("data", []):
        name = entry.get("id") or ""
        if not name:
            continue
        models.append(
            {
                "name": name,
                "size": None,
                "modified_at": None,
                "is_default": name == active_model,
            }
        )
    models.sort(key=lambda item: item["name"])

    return {"active": active_model, "models": models}


def _ask_runner_with_http_errors(
    runner: AgentRunner,
    prompt: str,
    effective_model: str,
    resume_request_id: str | None = None,
    history_messages: list | None = None,
):
    """Exécute runner.ask_detailed(prompt) en traduisant les erreurs réseau.

    ``history_messages`` (optionnel) est transmis au runner (mémoire de
    session, voir AgentCore.run_detailed).

    Sémantique HTTP :
        Timeout LLM          -> 504
        Ollama injoignable   -> 502
        Erreur HTTP d'Ollama -> 502
    """
    try:
        return runner.ask_detailed(
            prompt,
            resume_request_id=resume_request_id,
            history_messages=history_messages,
        )
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


def ask_agent_decision(
    prompt: str,
    model: str | None = None,
    resume_request_id: str | None = None,
    history_messages: list | None = None,
) -> dict:
    """Version consciente du gate de décision (auto_approve/approve/reject).

    Alimente le flux API « approbation humaine » :
        - ``completed``  : l'agent a répondu sans bloquer (réponse finale) ;
        - ``awaiting_approval`` : une action attend `/approve` (request_id) ;
        - ``rejected``  : une action a été bloquée par la policy (request_id).

    ``history_messages`` (optionnel) : messages de la conversation rejoués en
    tête du contexte LLM (mémoire de session, voir AgentCore.run_detailed).

    Retourne toujours un dict JSON provenable pour les endpoints.
    """
    effective_model = (model or "").strip() or agent_config()["model"]
    runner = get_agent_runner(effective_model)
    result = _ask_runner_with_http_errors(
        runner,
        prompt,
        effective_model,
        resume_request_id=resume_request_id,
        history_messages=history_messages,
    )
    agent = runner.agent
    approval = agent.last_approval.to_dict() if agent.last_approval is not None else None

    if agent.awaiting_request_id:
        return {
            "status": "awaiting_approval",
            "request_id": agent.awaiting_request_id,
            "approval": approval,
            "response": result.answer,
        }
    if agent.rejected_request_id:
        return {
            "status": "rejected",
            "request_id": agent.rejected_request_id,
            "approval": approval,
            "response": result.answer,
        }
    return {"status": "completed", "response": result.answer}


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


def ask_agent_decision_streaming(
    prompt: str,
    model: str | None = None,
    enable_thinking: bool = False,
    resume_request_id: str | None = None,
    on_thinking=None,
    on_tool_event=None,
    history_messages: list | None = None,
) -> dict:
    """Comme ``ask_agent_decision``, mais avec diffusion temps réel.

    Combine les deux flux temps réel de l'agent :
        - ``on_thinking`` : fragments de la trace de réflexion (mode activé) ;
        - ``on_tool_event`` : dicts ``tool_start`` / ``tool_result`` émis par
          AgentCore à chaque appel d'outil (voir ``ia/agent/runner.py``).

    ``history_messages`` (optionnel) : mémoire de conversation rejouée en tête
    du contexte LLM (voir AgentCore.run_detailed).

    Retour : même contrat que ``ask_agent_decision`` + ``thinking`` —
    ``{"answer", "thinking", "status", "request_id", "approval"}``. Les
    erreurs réseau sont traduites en HTTPException ; un ``ValueError`` sur
    ``resume_request_id`` est propagé (traduit en 400 par la route).
    """
    effective_model = (model or "").strip() or agent_config()["model"]
    runner = get_agent_runner(effective_model, enable_thinking)
    try:
        result = runner.run(
            prompt,
            resume_request_id=resume_request_id,
            on_thinking=on_thinking,
            on_tool_event=on_tool_event,
            history_messages=history_messages,
        )
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

    agent = runner.agent
    approval = agent.last_approval.to_dict() if agent.last_approval is not None else None

    payload: dict = {
        "answer": result.answer,
        "thinking": result.thinking,
        "response": result.answer,
        "model": effective_model,
    }
    if agent.awaiting_request_id:
        payload.update(
            {
                "status": "awaiting_approval",
                "request_id": agent.awaiting_request_id,
                "approval": approval,
            }
        )
    elif agent.rejected_request_id:
        payload.update(
            {
                "status": "rejected",
                "request_id": agent.rejected_request_id,
                "approval": approval,
            }
        )
    else:
        payload["status"] = "completed"
    return payload