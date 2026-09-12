"""Configuration effective de l'agent — lecture du module de configuration IHM.

Modèle TYpé consommé par le noyau agentique v2 (``app/agent/factory.py``, le
client LLM ``app/infrastructure/llm``, la mémoire de session, la surface MCP) :
``AgentConfig`` porte TOUTE la configuration de l'agent — provider LLM, modèle,
URLs, clés API, timeout/contexte, budgets (rounds LLM / appels d'outils), niveau
de log, surface MCP et feature flags.

Source des valeurs (SCRUM-138 — plus AUCUNE configuration d'agent dans
``app/config/settings.py``) : le store persistant du module de configuration
de l'IHM (``core/agent_settings.py``), c'est-à-dire la collection
``agent_settings`` de **MongoDB** en mode ``PERSISTENCE_BACKEND=mongodb`` —
la même base que la page Paramètres du dashboard. Priorité décroissante :

    1. valeurs persistées en base (dashboard) ;
    2. variables d'environnement ``AGENT_*`` / ``MCP_*`` — repli de
       compatibilité CI / déploiements (jamais de valeur codée en dur ici) ;
    3. défauts du module (bascules en production terminées : noyau v2,
       client LLM v2, tous les flags actifs).

Le fail-fast historique est conservé : ``provider=openrouter`` sans clé API
OU ``provider=hf`` sans jeton lève une ``ValueError`` explicite au chargement.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.ports import AgentSettingsPort

__all__ = [
    "AgentProvider",
    "AgentConfig",
    "agent_flag",
    "get_agent_config",
]


class AgentProvider(StrEnum):
    """Providers LLM supportés par le client de l'agent (cf. ia/agent/llm_client.py)."""

    OLLAMA = "ollama"
    OPENROUTER = "openrouter"
    HF = "hf"
    LM_STUDIO = "lm_studio"  # serveur local LM Studio, compatible OpenAI


# Noms canoniques des feature flags (convention ``AGENT_<NOM>``,
# cf. core/feature_flags.py — les valeurs vivent désormais en base).
AGENT_FLAG_NAMES = (
    "reliability",
    "audit",
    "tool_analytics",
    "context",
    "copilot",
    "websocket",
    "multi_agent",
    "custom_tools",
    "new_core",
    "llm_v2",
)

# Défauts du module = défauts historiques de app/config/settings.py (déplacés
# ici) : ils ne servent qu'au démarrage à froid, AVANT la première sauvegarde
# côté dashboard — ensuite la base (MongoDB) est la source de vérité.
_DEFAULTS: dict[str, Any] = {
    "provider": "ollama",
    "model_name": "openrouter/free",
    "ollama_url": "http://192.168.1.184:11434/api/chat",
    "openrouter_url": "https://openrouter.ai/api/v1/chat/completions",
    "openrouter_api_key": None,
    "hf_url": "https://router.huggingface.co/v1/chat/completions",
    "hf_api_key": None,
    "lm_studio_url": "http://192.168.1.184:1234/v1/chat/completions",
    "timeout_seconds": 600,
    "context_length": 2048,
    "temperature": None,
    "max_llm_rounds": 6,
    "max_tool_calls": 20,
    # Sécurité réseau (bac à sable SSRF) — défaut fail-closed aligné sandbox.
    "ssrf_enabled": True,
    "ssrf_allowlist": "",
    "log_level": "INFO",
    "mcp_first": False,
    "mcp_auth_required": True,
    **{f"flag_{name}": True for name in AGENT_FLAG_NAMES},
}

# Clés du store IHM renommées vers les champs du modèle (clé ``model`` du
# dashboard → champ ``model_name`` du noyau).
_RENAMES = {"model": "model_name"}


class AgentConfig(BaseModel):
    """Configuration effective et typée de l'agent (module de configuration IHM).

    ``extra="ignore"`` : tolère les clés contractuelles du payload IHM
    (``sources``, ``_written_keys``) sans les exposer ; ``protected_namespaces``
    exempte le champ ``model_name`` de la réservation ``model_`` de pydantic v2.
    """

    model_config = {"extra": "ignore", "protected_namespaces": ()}

    # --- Provider LLM --------------------------------------------------------
    provider: AgentProvider = AgentProvider.OLLAMA
    model_name: str = _DEFAULTS["model_name"]
    ollama_url: str = _DEFAULTS["ollama_url"]
    openrouter_url: str = _DEFAULTS["openrouter_url"]
    # Aucune clé par défaut : le secret vient de la base (page Paramètres) ou
    # de l'environnement OPENROUTER_API_KEY. Le validateur échoue vite si le
    # provider l'exige sans clé.
    openrouter_api_key: str | None = None
    hf_url: str = _DEFAULTS["hf_url"]
    hf_api_key: str | None = None
    # Endpoint chat LM Studio : serveur LOCAL compatible OpenAI (aucune clé ;
    # la fenêtre de contexte se règle dans l'UI LM Studio).
    lm_studio_url: str = _DEFAULTS["lm_studio_url"]
    # --- Réglages d'appel ----------------------------------------------------
    timeout_seconds: float = _DEFAULTS["timeout_seconds"]
    context_length: int = _DEFAULTS["context_length"]
    temperature: float | None = None
    # --- Budgets & garde-fous ------------------------------------------------
    max_llm_rounds: int = Field(default=_DEFAULTS["max_llm_rounds"], ge=1)
    max_tool_calls: int = Field(default=_DEFAULTS["max_tool_calls"], ge=1)
    # --- Sécurité réseau (bac à sable SSRF) -----------------------------------
    # Portés par la config typée pour la traçabilité v2 ; l'application au bac
    # à sable se fait via ``apply_persisted_network_policy`` (page Paramètres >
    # env, poussé à chaque lecture de la config effective).
    ssrf_enabled: bool = True
    ssrf_allowlist: str = ""
    # --- Observabilité -------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = _DEFAULTS["log_level"]
    # --- Surface MCP ----------------------------------------------------------
    mcp_first: bool = _DEFAULTS["mcp_first"]
    mcp_auth_required: bool = _DEFAULTS["mcp_auth_required"]
    # --- Feature flags (défauts : bascules en production terminées) -----------
    flag_reliability: bool = True
    flag_audit: bool = True
    flag_tool_analytics: bool = True
    flag_context: bool = True
    flag_copilot: bool = True
    flag_websocket: bool = True
    flag_multi_agent: bool = True
    flag_custom_tools: bool = True
    flag_new_core: bool = True
    flag_llm_v2: bool = True

    @field_validator("provider", mode="before")
    @classmethod
    def _coerce_provider(cls, value: Any) -> Any:
        """Accepte « ollama » (base/env) comme ``AgentProvider.OLLAMA``.

        Normalisation identique à ``core.agent_settings.get_agent_settings`` :
        trim + guillemets survivant à un double-encodage JSON + casse.
        """
        if isinstance(value, AgentProvider):
            return value
        normalized = str(value).strip().strip("\"'").lower() or AgentProvider.OLLAMA
        try:
            return AgentProvider(normalized)
        except ValueError as exc:
            raise ValueError(
                f"provider LLM inconnu : « {value} » "
                f"(attendu : {', '.join(p.value for p in AgentProvider)})."
            ) from exc

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> Any:
        """Accepte « debug » (env) comme « DEBUG » (insensible à la casse)."""
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator(
        "model_name",
        "ollama_url",
        "openrouter_url",
        "hf_url",
        "lm_studio_url",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        """Nettoie les chaînes (espaces environnants)."""
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_provider_keys(self) -> AgentConfig:
        """Fail-fast : cohérence provider / clés API (message explicite)."""
        if self.provider is AgentProvider.OPENROUTER and not self.openrouter_api_key:
            raise ValueError(
                "AGENT_PROVIDER=openrouter exige OPENROUTER_API_KEY (https://openrouter.ai/keys)"
            )
        if self.provider is AgentProvider.HF and not self.effective_hf_key:
            raise ValueError(
                "AGENT_PROVIDER=hf exige HF_API_KEY (ou HF_TOKEN en repli) "
                "(https://huggingface.co/settings/tokens)"
            )
        return self

    # --- Helpers -------------------------------------------------------------

    @property
    def effective_hf_key(self) -> str | None:
        """Clé HF effective : HF_API_KEY prioritaire (HF_TOKEN déjà résolu par
        la couche env du module IHM en repli)."""
        return self.hf_api_key or None

    def endpoint(self) -> tuple[str, str | None]:
        """Résout ``(url, api_key)`` selon le provider configuré.

        Clé ``None`` pour Ollama / LM Studio (serveur local sans auth) ;
        clé requise pour OpenRouter / Hugging Face (fail-fast au chargement).
        """
        url = {
            AgentProvider.OLLAMA: self.ollama_url,
            AgentProvider.OPENROUTER: self.openrouter_url,
            AgentProvider.HF: self.hf_url,
            AgentProvider.LM_STUDIO: self.lm_studio_url,
        }[self.provider]
        api_key: str | None = None
        if self.provider is AgentProvider.OPENROUTER:
            api_key = self.openrouter_api_key
        elif self.provider is AgentProvider.HF:
            api_key = self.effective_hf_key
        return url, api_key

    def active_flags(self) -> dict[str, bool]:
        """Snapshot des feature flags (compatibilité core/feature_flags.features())."""
        return {name: getattr(self, f"flag_{name}") for name in AGENT_FLAG_NAMES}


def get_agent_config(port: AgentSettingsPort | None = None) -> AgentConfig:
    """Charge la configuration effective de l'agent (base IHM → env → défauts).

    ``port`` : ``AgentSettingsPort`` injectable (tests) ; absent, le store
    partagé du module de configuration IHM est résolu (``get_settings_store``)
    — collection MongoDB ``agent_settings`` dès
    ``PERSISTENCE_BACKEND=mongodb``, sinon le store SQLite de développement.
    La base est TOUJOURS prioritaire : une sauvegarde du dashboard est
    immédiatement effective au prochain run (aucun cache, même sémantique que
    ``core.agent_cache.agent_config``).
    """
    from core.agent_settings import env_and_defaults

    if port is None:
        from app.infrastructure.legacy_settings_adapter import build_settings_port

        port = build_settings_port()

    values: dict[str, Any] = env_and_defaults()
    values.update(port.get_all())
    # Convention IHM : ``None`` / ``""`` signifient « non configuré » — la
    # valeur persistée vide (reset demandé par le dashboard) laisse le défaut
    # du modèle s'appliquer. Les bools (dont ``False``) restent explicites.
    mapped = {
        _RENAMES.get(key, key): value
        for key, value in values.items()
        if value is not None and value != ""
    }
    config = AgentConfig(**mapped)
    # Pilotage runtime du bac à sable : seules les valeurs PERSISTÉES (page
    # Paramètres) surclassent l'env — les clés absentes de la base repassent
    # sous contrôle env côté ``ia.tools.sandbox`` (env relue à chaque appel).
    try:
        from ia.tools.sandbox import apply_persisted_network_policy

        apply_persisted_network_policy(port.get_all())
    except ImportError:  # pragma: no cover — bac à sable facultatif
        pass
    return config


def agent_flag(name: str, port: AgentSettingsPort | None = None) -> bool:
    """État d'un feature flag (False si inconnu) — lecture live de la base."""
    if name not in AGENT_FLAG_NAMES:
        return False
    return bool(getattr(get_agent_config(port), f"flag_{name}", False))
