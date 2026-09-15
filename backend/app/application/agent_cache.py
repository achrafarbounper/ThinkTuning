# project/core/agent_cache.py

"""Intégration de l'agent IA du paquet `ia/` dans l'API principale.

Même schéma que `app/application/predictor_cache.py` : une instance unique construite
paresseusement au premier appel puis mise en cache (accès protégé par un
verrou), avec rechargement explicite via `reload_agent_runner()`.

Les symboles du runtime v1 (``AgentCore``, ``AgentRunner``,
``MultiAgentCoordinator``) sont résolus PAresseusement (module-level
``__getattr__``, PEP 562) : importer ce module ne charge plus
``app.agent.legacy.*`` — seul le premier usage d'un runner/coordinateur les
importe, depuis leurs paquets réels (aucun hack ``sys.path``, cf.
tests/test_sys_path_guard.py). Ce module reste le point d'entrée strangler
unique : le reste de l'API n'accède à l'agent v1 que via ce module, jamais par
un import direct du runtime (verrouillé par
tests/test_no_direct_legacy_imports.py).
"""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING, Any

import requests
from fastapi import HTTPException

from app.agent.settings import normalize_chat_url  # source canonique (S1, strangler)
from app.infrastructure.llm.legacy_client import LLMClient  # noqa: E402
from app.infrastructure.persistence.agent_settings import get_agent_settings

# File de validation humaine — ré-exportée pour les routes /api/agent/approvals.
from app.infrastructure.persistence.approval_store import ApprovalStore  # noqa: E402,F401
from app.infrastructure.persistence.run_store import (
    AWAITING_APPROVAL as MULTI_RUN_AWAITING,
)
from app.infrastructure.persistence.run_store import (
    COMPLETED as MULTI_RUN_COMPLETED,
)
from app.infrastructure.persistence.run_store import (
    ERROR as MULTI_RUN_ERROR,
)
from app.infrastructure.persistence.run_store import (
    get_run_store,
)
from app.infrastructure.tools.tool_registry import (  # noqa: E402,F401
    REQUIRED_ARGS,
    TOOL_META,
    TOOLS,
)

# ---------------------------------------------------------------------------
# Résolution PAresseuse du runtime v1 (strangler — S1 : réduction legacy)
# ---------------------------------------------------------------------------
# ``app.agent.legacy.*`` n'est plus importé au chargement de ce module : les
# symboles ne sont résolus qu'au premier usage (assemblage d'un runner /
# coordinateur), puis mis en cache dans l'espace de noms. L'import passe
# TOUJOURS par les paquets hexagonaux réels — aucun hack ``sys.path`` — et ce
# module reste le SEUL point d'entrée production vers le v1
# (verrouillé par tests/test_no_direct_legacy_imports.py).
_LEGACY_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AgentCore": ("app.agent.legacy.agent_core", "AgentCore"),
    "AgentRunner": ("app.agent.legacy.runner", "AgentRunner"),
    "MultiAgentCoordinator": ("app.agent.legacy.orchestrator", "MultiAgentCoordinator"),
}

if TYPE_CHECKING:  # satisfaction statique F821/F822 — jamais exécuté (lazy).
    from app.agent.legacy.agent_core import AgentCore
    from app.agent.legacy.orchestrator import MultiAgentCoordinator
    from app.agent.legacy.runner import AgentRunner


def __getattr__(name: str) -> Any:
    """Résout paresseusement un symbole du runtime v1 (PEP 562).

    Premier accès à ``AgentCore`` / ``AgentRunner`` / ``MultiAgentCoordinator`` :
    import du module legacy correspondant puis mise en cache dans l'espace de
    noms (les accès suivants ne repassent plus par ``__getattr__``). Tout autre
    nom inconnu déclenche l'``AttributeError`` standard.
    """
    entry = _LEGACY_LAZY_IMPORTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(entry[0]), entry[1])
    globals()[name] = value
    return value


def _resolve_runtime(name: str) -> Any:
    """Résout un symbole du runtime v1 pour un usage INTERNE au module.

    ⚠ Subtilité PEP 562 : ``__getattr__`` ci-dessus n'est déclenché QUE par un
    accès *attribut* sur le module depuis l'extérieur (``from
    app.application.agent_cache import AgentRunner``, ``agent_cache.AgentRunner``).
    Une référence « nue » dans une fonction de ce module (bytecode
    ``LOAD_GLOBAL``) lit directement ``globals()`` et lève ``NameError`` sans
    JAMAIS passer par ``__getattr__`` : tant que le symbole n'a pas été résolu
    une première fois via un accès attribut, toute instanciation interne plante
    (SCRUM-141 : ``NameError: name 'AgentRunner' is not defined`` sur
    ``PUT /api/v1/agent/settings`` → ``reload_agent_runner()``).

    Ce helper contourne le problème en effectuant l'accès attribut sur le
    module lui-même (``sys.modules[__name__]``) : premier appel → ``__getattr__``
    importe le module legacy et met le symbole en cache dans ``globals()`` ;
    appels suivants → lecture directe du cache. Les annotations restent
    satisfaites statiquement par les imports sous ``TYPE_CHECKING``.

    TOUTE fonction de ce module qui instancie un symbole paresseux
    (``AgentCore`` / ``AgentRunner`` / ``MultiAgentCoordinator``) doit passer
    par ce helper — verrouillé par tests/test_agent_cache_lazy_resolution.py.
    """
    return getattr(sys.modules[__name__], name)


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

# LM Studio : serveur LOCAL compatible OpenAI (aucune clé requise ; la
# fenêtre de contexte se règle dans l'UI LM Studio). Le dashboard enregistre
# la racine « /v1 » — normalisée par _lm_studio_chat_url().
DEFAULT_LM_STUDIO_URL = "http://192.168.1.184:1234/v1/chat/completions"
# Modèle par défaut : LM Studio sert le modèle chargé quand l'identifiant ne
# correspond pas — on laisse vide pour que GET /v1/models alimente le
# sélecteur du chat (surcharge via AGENT_MODEL_NAME).
DEFAULT_LM_STUDIO_MODEL_NAME = ""

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
    """Délègue à ``app.agent.settings.normalize_chat_url`` (OpenRouter).

    Source canonique unique depuis S1 (réduction legacy) : la logique vit dans
    le module de configuration v2 — ce helper n'est conservé que pour la
    compatibilité des consommateurs existants (routes v1, tests).
    """
    return normalize_chat_url(url, default=DEFAULT_OPENROUTER_URL)


def _hf_chat_url(url: str | None) -> str:
    """Délègue à ``app.agent.settings.normalize_chat_url`` (Hugging Face)."""
    return normalize_chat_url(url, default=DEFAULT_HF_URL)


def _lm_studio_chat_url(url: str | None) -> str:
    """Délègue à ``app.agent.settings.normalize_chat_url`` (LM Studio)."""
    return normalize_chat_url(url, default=DEFAULT_LM_STUDIO_URL)


def agent_config() -> dict:
    """Configuration courante de l'agent, relue à chaque appel.

    Sources par priorité décroissante (via ``app.infrastructure.persistence.agent_settings``) :
        1. base SQLite des paramètres (page Paramètres du dashboard) ;
        2. variables d'environnement ;
        3. défauts historiques du module.

    Variables d'environnement utilisées en repli :
        AGENT_PROVIDER         « ollama » (défaut), « openrouter », « hf »
                               ou « lm_studio »
        AGENT_OLLAMA_URL       URL du endpoint chat Ollama
        AGENT_OPENROUTER_URL   URL du endpoint chat OpenRouter (compatible OpenAI)
        AGENT_LM_STUDIO_URL    URL du endpoint chat LM Studio (compatible OpenAI)
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
    # identifiant « vendor/model » incompatible avec la convention Ollama ;
    # LM Studio laisse le champ vide (le modèle chargé est servi par défaut).
    model = val("model") or (
        DEFAULT_OPENROUTER_MODEL_NAME
        if provider == "openrouter"
        else DEFAULT_HF_MODEL_NAME
        if provider == "hf"
        else DEFAULT_LM_STUDIO_MODEL_NAME
        if provider == "lm_studio"
        else DEFAULT_MODEL_NAME
    )
    timeout_raw = val("timeout_seconds")
    context_raw = val("context_length")
    temperature_raw = val("temperature")

    return {
        "provider": provider,
        "ollama_url": val("ollama_url") or DEFAULT_OLLAMA_URL,
        "openrouter_url": _openrouter_chat_url(val("openrouter_url") or DEFAULT_OPENROUTER_URL),
        "openrouter_api_key": val("openrouter_api_key") or "",
        "hf_url": _hf_chat_url(val("hf_url") or DEFAULT_HF_URL),
        "hf_api_key": val("hf_api_key") or "",
        "lm_studio_url": _lm_studio_chat_url(val("lm_studio_url") or DEFAULT_LM_STUDIO_URL),
        "model": model,
        "timeout": (float(timeout_raw) if timeout_raw is not None else DEFAULT_TIMEOUT_SECONDS),
        "context_length": (int(context_raw) if context_raw is not None else DEFAULT_CONTEXT_LENGTH),
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
    if cfg["provider"] == "lm_studio":
        # LM Studio : serveur local SANS authentification — aucune clé
        # requise, la config ne porte que l'URL du endpoint chat.
        return cfg["lm_studio_url"], None
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
    # Résolution EXPLICITE (accès attribut) : une référence nue ne déclenche
    # pas __getattr__ (PEP 562 = accès attribut uniquement) — cf.
    # _resolve_runtime pour la subtilité NameError/LOAD_GLOBAL.
    runner_cls = _resolve_runtime("AgentRunner")
    core_cls = _resolve_runtime("AgentCore")
    return runner_cls(core_cls(llm, enable_thinking=enable_thinking))


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
    if cfg["provider"] == "lm_studio":
        return _list_lm_studio_models(cfg)

    base_url = _ollama_base_url()
    try:
        response = requests.get(f"{base_url}/api/tags", timeout=LIST_MODELS_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.Timeout as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Le serveur Ollama ({base_url}) n'a pas répondu "
                f"dans les {LIST_MODELS_TIMEOUT_SECONDS:.0f}s."
            ),
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Serveur Ollama injoignable sur {base_url}. Vérifiez qu'il tourne.",
        ) from exc
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502, detail=f"Erreur renvoyée par Ollama (HTTP {status})."
        ) from exc
    except ValueError as exc:  # réponse non JSON
        raise HTTPException(
            status_code=502, detail=f"Réponse illisible du serveur Ollama ({exc})."
        ) from exc

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
    except requests.exceptions.Timeout as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"L'API OpenRouter ({url}) n'a pas répondu "
                f"dans les {LIST_MODELS_TIMEOUT_SECONDS:.0f}s."
            ),
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"API OpenRouter injoignable sur {url}.",
        ) from exc
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502,
            detail=(
                f"Erreur renvoyée par OpenRouter (HTTP {status})."
                + (" Clé OPENROUTER_API_KEY invalide ?" if status == 401 else "")
            ),
        ) from exc
    except ValueError as exc:  # réponse non JSON
        raise HTTPException(
            status_code=502, detail=f"Réponse illisible de l'API OpenRouter ({exc})."
        ) from exc

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
    except requests.exceptions.Timeout as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"L'API Hugging Face ({url}) n'a pas répondu "
                f"dans les {LIST_MODELS_TIMEOUT_SECONDS:.0f}s."
            ),
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"API Hugging Face injoignable sur {url}.",
        ) from exc
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502,
            detail=(
                f"Erreur renvoyée par Hugging Face (HTTP {status})."
                + (" Clé HF_API_KEY invalide ?" if status == 401 else "")
            ),
        ) from exc
    except ValueError as exc:  # réponse non JSON
        raise HTTPException(
            status_code=502, detail=f"Réponse illisible de l'API Hugging Face ({exc})."
        ) from exc

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


def _lm_studio_base_api() -> str:
    """Racine de l'API LM Studio déduite de l'URL du endpoint chat.

    « http://192.168.1.184:1234/v1/chat/completions » -> « .../v1 ».
    """
    url = agent_config()["lm_studio_url"]
    marker = url.find("/chat/completions")
    if marker != -1:
        return url[:marker].rstrip("/")
    return url.rsplit("/", 1)[0] if "/" in url.rstrip("/") else url


def _list_lm_studio_models(cfg: dict) -> dict:
    """Liste les modèles LM Studio (GET /v1/models, compatible OpenAI).

    Aucune authentification : le serveur local expose les modèles chargés.
    Mapping sur le même contrat que la liste OpenRouter : ``name`` porte
    l'identifiant complet du modèle ; ``size`` et ``modified_at`` n'existent
    pas côté LM Studio.
    """
    url = f"{_lm_studio_base_api()}/models"
    try:
        response = requests.get(url, timeout=LIST_MODELS_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.Timeout as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Le serveur LM Studio ({url}) n'a pas répondu "
                f"dans les {LIST_MODELS_TIMEOUT_SECONDS:.0f}s."
            ),
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Serveur LM Studio injoignable sur {url}. Vérifiez qu'il "
                "tourne (LM Studio > Developer > Local Server)."
            ),
        ) from exc
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502, detail=f"Erreur renvoyée par LM Studio (HTTP {status})."
        ) from exc
    except ValueError as exc:  # réponse non JSON
        raise HTTPException(
            status_code=502, detail=f"Réponse illisible du serveur LM Studio ({exc})."
        ) from exc

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
    except requests.exceptions.Timeout as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Le LLM ({effective_model}) n'a pas répondu en {agent_config()['timeout']:.0f}s."
            ),
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"LLM injoignable sur {agent_config()['ollama_url']}. Vérifiez qu'Ollama tourne."
            ),
        ) from exc
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502, detail=f"Erreur renvoyée par le LLM (HTTP {status})."
        ) from exc


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
    except requests.exceptions.Timeout as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Le LLM ({effective_model}) n'a pas répondu en {agent_config()['timeout']:.0f}s."
            ),
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"LLM injoignable sur {agent_config()['ollama_url']}. Vérifiez qu'Ollama tourne."
            ),
        ) from exc
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502, detail=f"Erreur renvoyée par le LLM (HTTP {status})."
        ) from exc
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
    ``MCP_ORCHESTRATION_DEADLINE_SECONDS`` : garde-fou de durée totale du run ;
    absent ou nul = désactivé.
    Lecture env directe transitoire — à migrer vers ``app/config/settings.py``
    quand celui-ci sera chargeable sans exigence de clé API (fail-fast actuel).
    """
    import os as _os

    try:
        total = int(_os.getenv("AGENT_MULTI_MAX_TOOL_CALLS", "0") or 0)
    except ValueError:
        total = 0
    kwargs: dict = {"max_total_tool_calls": total} if total > 0 else {}
    try:
        deadline = float(_os.getenv("MCP_ORCHESTRATION_DEADLINE_SECONDS", "0") or 0)
    except ValueError:
        deadline = 0.0
    if deadline > 0:
        kwargs["orchestration_deadline_seconds"] = deadline
    _true = {"1", "true", "yes", "on"}
    if _os.getenv("AGENT_MULTI_PARALLEL", "").strip().lower() in _true:
        kwargs["parallel"] = True
    if _os.getenv("AGENT_MULTI_THINKING", "").strip().lower() in _true:
        kwargs["enable_thinking"] = True
    if _os.getenv("AGENT_MULTI_INTENT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
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
        from app.infrastructure.ml.classifiers.intent_classifier import IntentClassifier

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
                store.append_tool_event(
                    row["id"],
                    {
                        "event": "worker_approval",
                        "task_id": worker.get("task_id"),
                        "role": worker.get("role"),
                        "request_id": worker.get("request_id"),
                        "tool": (worker.get("approval") or {}).get("tool", ""),
                    },
                )
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
    coordinator_cls = _resolve_runtime("MultiAgentCoordinator")
    key = _coordinator_key(model)
    if key is not None:
        with _multi_coordinator_lock:
            coord = _override_coordinators.get(key)
            if coord is None:
                coord = coordinator_cls(_build_llm_client(key), **_multi_coordinator_kwargs())
                _override_coordinators[key] = coord
        return coord
    global _multi_coordinator
    if _multi_coordinator is None:
        with _multi_coordinator_lock:
            if _multi_coordinator is None:
                llm = _build_llm_client(model)
                _multi_coordinator = coordinator_cls(llm, **_multi_coordinator_kwargs())
    return _multi_coordinator


def reload_multi_agent_coordinator(model: str | None = None) -> MultiAgentCoordinator:
    # Reconstruit le coordinateur (nouveau LLMClient) pour le modele demande.
    coordinator_cls = _resolve_runtime("MultiAgentCoordinator")
    key = _coordinator_key(model)
    global _multi_coordinator
    with _multi_coordinator_lock:
        if key is not None:
            _override_coordinators.pop(key, None)
            coord = coordinator_cls(_build_llm_client(key), **_multi_coordinator_kwargs())
            _override_coordinators[key] = coord
            return coord
        _multi_coordinator = None
        llm = _build_llm_client(model)
        _multi_coordinator = coordinator_cls(llm, **_multi_coordinator_kwargs())
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
    except requests.exceptions.Timeout as exc:
        raise HTTPException(
            status_code=504,
            detail="Le LLM n'a pas répondu pendant l'orchestration multi-agents.",
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail="LLM injoignable. Vérifiez que le serveur de modèles tourne.",
        ) from exc
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502, detail=f"Erreur renvoyée par le LLM (HTTP {status})."
        ) from exc


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
    except requests.exceptions.Timeout as exc:
        raise HTTPException(
            status_code=504,
            detail="Le LLM n'a pas répondu pendant l'orchestration multi-agents.",
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail="LLM injoignable. Vérifiez que le serveur de modèles tourne.",
        ) from exc
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise HTTPException(
            status_code=502, detail=f"Erreur renvoyée par le LLM (HTTP {status})."
        ) from exc
