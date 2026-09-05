# project/core/agent_cache.py

"""Intégration de l'agent IA du paquet `ia/` dans l'API principale.

Même schéma que `core/predictor_cache.py` : une instance unique construite
paresseusement au premier appel puis mise en cache (accès protégé par un
verrou), avec rechargement explicite via `reload_agent_runner()`.

Les modules de l'agent sont importés via l'identité de PAQUET réel
(``ia.agent.*``, ``ia.tools.*``) : plus aucun hack ``sys.path`` (Phase 2 de
la migration, cf. tests/test_sys_path_guard.py). Ce module reste le point
d'entrée unique : le reste de l'API n'accède à l'agent que via ce module,
jamais par un import direct de ``ia.agent.*``.
"""

import threading

import requests
from fastapi import HTTPException

from core.agent_settings import get_agent_settings
from core.run_store import (
    AWAITING_APPROVAL as MULTI_RUN_AWAITING,
    COMPLETED as MULTI_RUN_COMPLETED,
    ERROR as MULTI_RUN_ERROR,
    get_run_store,
)
from ia.agent.agent_core import AgentCore  # noqa: E402
from ia.agent.llm_client import LLMClient  # noqa: E402
from ia.agent.orchestrator import MultiAgentCoordinator  # noqa: E402
from ia.agent.runner import AgentRunner  # noqa: E402
from ia.tools.tool_registry import REQUIRED_ARGS, TOOL_META, TOOLS  # noqa: E402,F401

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
    "ask_agent_openrouter",
    "ask_agent_detailed",
    "ask_agent_detailed_streaming",
    "ask_multi_agent",
    "ask_multi_agent_streaming",
    "get_multi_agent_coordinator",
    "reload_multi_agent_coordinator",
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
DEFAULT_OPENROUTER_MODEL_NAME = "openrouter/free"

# Hugging Face Inference Providers : endpoint compatible OpenAI (auth Bearer
# HF_TOKEN/HF_API_KEY). Le dashboard enregistre la racine « /v1 » tandis que
# la config serveur peut porter l'endpoint chat complet — normalisé par
# _hf_chat_url().
DEFAULT_HF_URL = "https://router.huggingface.co/v1/chat/completions"
DEFAULT_HF_MODEL_NAME = "meta-llama/Llama-3.1-8B-Instruct"

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


def _hf_chat_url(url: str | None) -> str:
    """Normalise une URL Hugging Face vers l'endpoint chat complet.

    Accepte la racine de l'API (« https://router.huggingface.co/v1 ») ou
    l'endpoint complet (« .../v1/chat/completions »), comme _openrouter_chat_url.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_HF_URL
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
        DEFAULT_OPENROUTER_MODEL_NAME
        if provider == "openrouter"
        else DEFAULT_HF_MODEL_NAME if provider == "hf" else DEFAULT_MODEL_NAME
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
        "hf_url": _hf_chat_url(val("hf_url") or DEFAULT_HF_URL),
        "hf_api_key": val("hf_api_key") or "",
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
    if cfg["provider"] == "hf":
        api_key = (cfg["hf_api_key"] or "").strip()
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Provider LLM « hf » sélectionné mais aucune clé API "
                    "n'est définie. Renseignez HF_API_KEY (ou enregistrez-la "
                    "dans la page Paramètres)."
                ),
            )
        return cfg["hf_url"], api_key
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


def _build_openrouter_runner(
    model_name: str | None = None, enable_thinking: bool = False
) -> AgentRunner:
    """Fabrique un runner de l'agent FORCÉ sur le provider OpenRouter.

    Distingue du chemin historique contrôlé par la config globale
    (``AGENT_PROVIDER`` / base SQLite) : utilisé par POST /explain pour
    garantir une explication via OpenRouter même quand le provider par défaut
    reste Ollama.

    La clé OpenRouter provient de la config effective (base SQLite puis env
    ``OPENROUTER_API_KEY``) et une HTTPException 500 est levée quand elle
    manque (mauvaise configuration serveur) — même comportement qu'``_llm_endpoint``.
    Le modèle par défaut est ``DEFAULT_OPENROUTER_MODEL_NAME`` ; un ``model_name``
    explicite (ex. un ID OpenRouter « vendor/model ») le remplace.
    """
    cfg = agent_config()
    effective_model = (model_name or "").strip() or DEFAULT_OPENROUTER_MODEL_NAME
    url, api_key = _llm_endpoint(
        {
            **cfg,
            "provider": "openrouter",
        }
    )
    llm = LLMClient(
        url,
        effective_model,
        timeout=cfg["timeout"],
        temperature=cfg["temperature"],
        think=enable_thinking,
        context_length=cfg["context_length"],
        provider="openrouter",
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
    if cfg["provider"] == "hf":
        return _list_hf_models(cfg)

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


def _hf_base_api() -> str:
    """Racine de l'API HF déduite de l'URL du endpoint chat.

    « https://router.huggingface.co/v1/chat/completions » -> « .../v1 ».
    """
    url = agent_config()["hf_url"]
    marker = url.find("/chat/completions")
    if marker != -1:
        return url[:marker].rstrip("/")
    return url.rsplit("/", 1)[0] if "/" in url.rstrip("/") else url


def _list_hf_models(cfg: dict) -> dict:
    """Liste les modèles Hugging Face Inference Providers (GET /v1/models).

    L'entête Bearer est envoyé quand la clé est définie. Mapping sur le même
    contrat que la liste Ollama : ``name`` porte l'identifiant complet du
    modèle (« vendor/model ») ; ``size`` et ``modified_at`` n'existent pas.
    """
    url = f"{_hf_base_api()}/models"
    api_key = (cfg["hf_api_key"] or "").strip()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    try:
        response = requests.get(url, headers=headers, timeout=LIST_MODELS_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail=(
                f"L'API Hugging Face ({url}) n'a pas répondu "
                f"dans les {LIST_MODELS_TIMEOUT_SECONDS:.0f}s."
            ),
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(
            status_code=502,
            detail=f"API Hugging Face injoignable sur {url}.",
        )
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502,
            detail=(
                f"Erreur renvoyée par Hugging Face (HTTP {status})."
                + (" Clé HF_API_KEY invalide ?" if status == 401 else "")
            ),
        )
    except ValueError as exc:  # réponse non JSON
        raise HTTPException(
            status_code=502, detail=f"Réponse illisible de l'API Hugging Face ({exc})."
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


def ask_agent_openrouter(prompt: str, model: str | None = None) -> str:
    """Envoie le prompt à l'agent via un runner FORCÉ sur le provider OpenRouter.

    Utilisé par POST /explain pour garantir une explication en langage naturel
    via OpenRouter, indépendamment du provider par défaut de l'agent (Ollama).

    ``model`` : ID OpenRouter explicite (ex. « vendor/model ») ; absent ou
    vide, c'est ``DEFAULT_OPENROUTER_MODEL_NAME`` (= « openrouter/free ») qui
    est utilisé.

    Erreurs traduites en HTTPException : Timeout -> 504, LLM injoignable ou
    erreur HTTP -> 502, clé OpenRouter manquante -> 500.
    """
    effective_model = (model or "").strip() or DEFAULT_OPENROUTER_MODEL_NAME
    runner = _build_openrouter_runner(effective_model)
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


# --- Orchestration multi-agents (superviseur) -----------------------------

# Coordonnateur partagé, mis en cache paresseusement (même pattern que
# ``_runner``). Injectable dans les tests via monkeypatch.
_multi_coordinator: MultiAgentCoordinator | None = None
_multi_coordinator_lock = threading.Lock()


def _build_llm_client(model_name: str | None = None) -> LLMClient:
    """Construit un LLMClient selon la config effective du fournisseur."""
    cfg = agent_config()
    url, api_key = _llm_endpoint(cfg)
    return LLMClient(
        url,
        model_name or cfg["model"],
        timeout=cfg["timeout"],
        temperature=cfg["temperature"],
        think=False,
        context_length=cfg["context_length"],
        provider=cfg["provider"],
        api_key=api_key,
    )




# Coordinateurs dedies aux modeles explicitement demandes (selecteur du
# chat), mis en cache par nom de modele - meme pattern que _override_runners.
_override_coordinators: dict[str, MultiAgentCoordinator] = {}


def _coordinator_key(model: str | None) -> str | None:
    # Cle de cache : None (modele par defaut) -> singleton.
    name = (model or "").strip()
    if not name or name == agent_config()["model"]:
        return None
    return name


def _multi_coordinator_kwargs() -> dict:
    """Options du coordinateur multi-agents lues depuis l'environnement.

    ``AGENT_MULTI_MAX_TOOL_CALLS`` : plafond GLOBAL d'appels d'outils partagé
    par les workers d'un run (BudgetPool hiérarchique) ; ``0``/absent =
    désactivé (comportement V1). ``AGENT_MULTI_PARALLEL`` : parallélisme des
    sous-tâches indépendantes. ``AGENT_MULTI_THINKING`` : mode « Réflexion »
    des workers (agent.worker.thinking persisté dans le flow store).
    ``AGENT_MULTI_INTENT`` (défaut : activé) : classification d'intention
    chat/action au superviseur (Approche B) — filtrage par rôle au dispatch +
    repli conversationnel FALLBACK_CHAT ; ``0``/« false » = désactivé
    (comportement V1, aucun worker filtré).
    Lecture env directe transitoire — à migrer vers ``app/config/settings.py``
    quand celui-ci sera chargeable sans exigence de clé API (fail-fast actuel).
    """
    import os as _os

    try:
        total = int(_os.getenv("AGENT_MULTI_MAX_TOOL_CALLS", "0") or 0)
    except ValueError:
        total = 0
    kwargs: dict = {"max_total_tool_calls": total} if total > 0 else {}
    _true = {"1", "true", "yes", "on"}
    if _os.getenv("AGENT_MULTI_PARALLEL", "").strip().lower() in _true:
        kwargs["parallel"] = True
    if _os.getenv("AGENT_MULTI_THINKING", "").strip().lower() in _true:
        kwargs["enable_thinking"] = True
    if _os.getenv("AGENT_MULTI_INTENT", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }:
        # Approche B (multi-agents) : classification d'intention au superviseur.
        # Instance PARTAGÉE (le modèle n'est chargé qu'une fois) ; tout échec
        # de construction laisse le coordinateur sans classifieur (comportement
        # V1) au lieu de casser le démarrage.
        try:
            kwargs["intent_classifier"] = _get_shared_intent_classifier()
        except Exception:  # pragma: no cover - défensif
            pass
    return kwargs


_intent_classifier_shared = None


def _get_shared_intent_classifier():
    """Singleton paresseux du classifieur d'intention (partagé multi-agents).

    Instancié une seule fois et partagé par tous les coordinateurs (singleton
    et overrides par modèle) : le modèle torch/ONNX n'est chargé qu'à la
    première prédiction. Sans modèle entraîné, ``IntentClassifier`` bascule
    automatiquement sur les règles métier (continuité de service).
    """
    global _intent_classifier_shared
    if _intent_classifier_shared is None:
        from ia.agent.classifiers.intent_classifier import IntentClassifier
        _intent_classifier_shared = IntentClassifier()
    return _intent_classifier_shared


def _trace_multi_run(prompt: str, outcome: dict) -> None:
    """Persistance du run superviseur dans le run_store (traçabilité unifiée).

    Complète le flow_store (déjà écrit par la route) : ouvre un run
    ``source="multi"``, journalise chaque worker bloqué en approbation
    (``worker_approval`` : task_id / role / request_id / tool — résout la
    reprise et le dashboard) puis clôture avec le statut mappé. Défensif :
    la traçabilité ne doit JAMAIS faire échouer le run.
    """
    try:
        store = get_run_store()
        row = store.start_run(prompt, model="", source="multi")
        for worker in outcome.get("workers", []) or []:
            if worker.get("status") == "awaiting_approval":
                store.append_tool_event(row["id"], {
                    "event": "worker_approval",
                    "task_id": worker.get("task_id"),
                    "role": worker.get("role"),
                    "request_id": worker.get("request_id"),
                    "tool": (worker.get("approval") or {}).get("tool", ""),
                })
        status_map = {
            "completed": MULTI_RUN_COMPLETED,
            "awaiting_approval": MULTI_RUN_AWAITING,
            "error": MULTI_RUN_ERROR,
        }
        store.finish_run(
            row["id"],
            status_map.get(outcome.get("status", "completed"), MULTI_RUN_COMPLETED),
            answer_summary=(outcome.get("final_answer") or "")[:300],
        )
    except Exception:  # pragma: no cover - traçabilité jamais bloquante
        pass


def get_multi_agent_coordinator(model: str | None = None) -> MultiAgentCoordinator:
    # Coordonnateur multi-agents mis en cache (construction paresseuse).
    # Le modele par defaut (config SQLite/env) utilise le singleton
    # _multi_coordinator ; tout modele explicitement demande (selecteur du
    # chat) obtient un coordinateur dedie, mis en cache par nom de modele
    # (meme pattern que _override_runners) - sinon le parametre serait
    # ignore des que le singleton existe.
    key = _coordinator_key(model)
    if key is not None:
        with _multi_coordinator_lock:
            coord = _override_coordinators.get(key)
            if coord is None:
                coord = MultiAgentCoordinator(_build_llm_client(key), **_multi_coordinator_kwargs())
                _override_coordinators[key] = coord
        return coord
    global _multi_coordinator
    if _multi_coordinator is None:
        with _multi_coordinator_lock:
            if _multi_coordinator is None:
                llm = _build_llm_client(model)
                _multi_coordinator = MultiAgentCoordinator(llm, **_multi_coordinator_kwargs())
    return _multi_coordinator


def reload_multi_agent_coordinator(model: str | None = None) -> MultiAgentCoordinator:
    # Reconstruit le coordinateur (nouveau LLMClient) pour le modele demande.
    key = _coordinator_key(model)
    global _multi_coordinator
    with _multi_coordinator_lock:
        if key is not None:
            _override_coordinators.pop(key, None)
            coord = MultiAgentCoordinator(_build_llm_client(key), **_multi_coordinator_kwargs())
            _override_coordinators[key] = coord
            return coord
        _multi_coordinator = None
        llm = _build_llm_client(model)
        _multi_coordinator = MultiAgentCoordinator(llm, **_multi_coordinator_kwargs())
    return _multi_coordinator


def ask_multi_agent(
    prompt: str,
    model: str | None = None,
    parallel: bool = False,
    resume_request_id: str | None = None,
    enable_thinking: bool = False,
) -> dict:
    """Exécute (ou REPREND) la demande via l'orchestration multi-agents.

    Retourne le contrat de sortie stable de l'orchestrateur :
    ``{"status", "final_answer", "plan", "workers", "unexecuted", "thinking"}``.
    ``resume_request_id`` : reprise NATIVE d'un run interrompu sur une
    validation humaine — l'action approuvée est rejouée dans le MÊME worker
    (empreinte SHA-256 revérifiée) puis la synthèse finale est produite.
    Les erreurs réseau LLM sont traduites en HTTPException
    (Timeout -> 504, ConnectionError/HTTPError -> 502).
    """
    coordinator = get_multi_agent_coordinator(model)
    try:
        result = coordinator.run(
            prompt,
            resume_request_id=resume_request_id,
            enable_thinking=enable_thinking,
        )
        _trace_multi_run(prompt, result)
        return result
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail="Le LLM n'a pas répondu pendant l'orchestration multi-agents.",
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(
            status_code=502,
            detail="LLM injoignable. Vérifiez que le serveur de modèles tourne.",
        )
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502, detail=f"Erreur renvoyée par le LLM (HTTP {status})."
        )


def ask_multi_agent_streaming(
    prompt: str,
    model: str | None = None,
    parallel: bool = False,
    resume_request_id: str | None = None,
    enable_thinking: bool = False,
    on_event=None,
) -> dict:
    """Comme ``ask_multi_agent``, mais avec diffusion temps réel.

    ``on_event(event_type, data)`` reçoit les événements structurés de
    l'orchestrateur (``agent.plan``, ``agent.resuming``, ``agent.worker.start``,
    ``agent.worker.tool``, ``agent.worker.thinking``, ``agent.worker.error``,
    ``agent.worker.result``, ``agent.synthesizing``, ``agent.done``,
    ``agent.error``). ``resume_request_id`` : reprise native (voir
    ``ask_multi_agent``). Les erreurs réseau sont traduites en HTTPException
    (même politique que le reste de l'agent).
    """
    coordinator = get_multi_agent_coordinator(model)
    try:
        result = coordinator.run(
            prompt,
            on_event=on_event,
            resume_request_id=resume_request_id,
            enable_thinking=enable_thinking,
        )
        _trace_multi_run(prompt, result)
        return result
    except requests.exceptions.Timeout:
        raise HTTPException(
            status_code=504,
            detail="Le LLM n'a pas répondu pendant l'orchestration multi-agents.",
        )
    except requests.exceptions.ConnectionError:
        raise HTTPException(
            status_code=502,
            detail="LLM injoignable. Vérifiez que le serveur de modèles tourne.",
        )
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502, detail=f"Erreur renvoyée par le LLM (HTTP {status})."
        )
