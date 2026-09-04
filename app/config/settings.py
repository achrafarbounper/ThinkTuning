"""Configuration centralisée de l'application (source unique de vérité).

Unifie les variables d'environnement aujourd'hui éparpillées :
    - API          : API_KEY, CORS_ALLOWED_ORIGINS, DASHBOARD_WS_TOKEN
    - Agent LLM    : AGENT_PROVIDER, AGENT_MODEL_NAME, AGENT_OLLAMA_URL,
                     AGENT_OPENROUTER_URL, OPENROUTER_API_KEY, HF_API_KEY/HF_TOKEN,
                     AGENT_TIMEOUT_SECONDS, AGENT_CONTEXT_LENGTH, AGENT_LOG_LEVEL
    - Agent flags  : AGENT_<FEATURE> — cf. core/feature_flags.py
    - Streams ML   : TRAIN_STREAM_STALL_MINUTES, MODEL_SANITY_MIN_CONFIDENCE

Règles :
    - Lecture paresseuse (get_settings() mis en cache) : les tests peuvent
      modifier l'environnement puis réinitialiser le cache.
    - AUCUNE valeur sensible par défaut : les clés API sont Optionnel et
      la cohérence provider/clé est validée au chargement (fail-fast).
    - Compatibilité : ce module n'affecte PAS le comportement existant tant que
      les modules historiques lisent encore os.getenv directement ; il devient
      la source unique à mesure de la migration (Phase 0 → 3).
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AgentProvider(StrEnum):
    """Providers LLM supportés par le client de l'agent (cf. ia/agent/llm_client.py)."""

    OLLAMA = "ollama"
    OPENROUTER = "openrouter"
    HF = "hf"


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

_FLAG_NAMES = (
    "reliability",
    "audit",
    "tool_analytics",
    "context",
    "copilot",
    "websocket",
    "multi_agent",
    "new_core",  # bascule du noyau agentique v2 (AGENT_NEW_CORE)
    "llm_v2",    # client LLM propre vs legacy (AGENT_LLM_V2)
)


class Settings(BaseSettings):
    """Toutes les variables d'environnement de l'application, validées."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # tolère les variables hors périmètre (.env utilisateur)
    )

    # --- API / sécurité -----------------------------------------------------
    api_key: str = Field(default="change-me", description="Clé API des endpoints protégés")
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v):
        """Accepte aussi bien du CSV (« a,b ») qu'un tableau JSON pour CORS_ALLOWED_ORIGINS."""
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v
    dashboard_ws_token: str = Field(
        default="", description="Jeton dédié au WebSocket /train/stream (défaut : api_key)"
    )

    # --- Agent : provider LLM -----------------------------------------------
    agent_provider: AgentProvider = AgentProvider.OPENROUTER
    agent_model_name: str = "openrouter/free"
    agent_ollama_url: str = "http://192.168.1.184:11434/api/chat"
    agent_openrouter_url: str = "https://openrouter.ai/api/v1/chat/completions"
    # Aucune clé par défaut : le secret vient de l'environnement OPENROUTER_API_KEY.
    # Le validateur `_validate_provider` échoue vite si le provider l'exige sans clé.
    openrouter_api_key: str | None = None
    hf_api_key: str | None = None
    hf_token: str | None = None  # repli historique si HF_API_KEY absent
    agent_timeout_seconds: int = 600
    agent_context_length: int = 2048

    # --- Agent : budgets & garde-fous ---------------------------------------
    agent_max_llm_rounds: int = Field(default=6, ge=1, description="Rounds LLM max par run")
    agent_max_tool_calls: int = Field(default=20, ge=1, description="Appels d'outils max par run")

    # --- Observabilité -------------------------------------------------------
    agent_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Streams / ML ---------------------------------------------------------
    train_stream_stall_minutes: int = Field(default=5, ge=1)
    model_sanity_min_confidence: float = Field(default=0.4, ge=0.0, le=1.0)

    # --- Feature flags agent (AGENT_<NOM> = 1/true/yes/on) -------------------
    flag_reliability: bool = 1
    flag_audit: bool = 1
    flag_tool_analytics: bool = 1
    flag_context: bool = 1
    flag_copilot: bool = 1
    flag_websocket: bool = 1
    flag_multi_agent: bool = 1
    # Bascule du noyau agentique v2 : ACTIVÉ par défaut depuis la bascule en
    # production (rollout terminé). Peut venir de l'environnement
    # (AGENT_NEW_CORE) ou du fichier .env (lu par pydantic-settings,
    # contrairement à os.getenv) ; ``AGENT_NEW_CORE=0`` conserve le repli
    # legacy tant que le chemin v1 n'est pas décommissionné.
    flag_new_core: bool = True

    # Bascule du client LLM v2 (AGENT_LLM_V2). ACTIVÉ par défaut depuis la
    # bascule en production : ``HttpLLMClient`` (implémentation propre du port
    # LLMClientPort, retry + circuit breaker réutilisés) remplace le legacy.
    # ``AGENT_LLM_V2=0`` conserve le repli legacy tant que le chemin v1 vit.
    flag_llm_v2: bool = True

    @model_validator(mode="before")
    @classmethod
    def _load_flags(cls, data: object) -> object:
        """Alimente les flags depuis AGENT_<NOM> (convention core/feature_flags.py).

        Les noms de champs `flag_*` ne correspondant pas à des variables d'env
        directes, on les alimente manuellement depuis AGENT_<NOM>."""
        if isinstance(data, dict):
            for name in _FLAG_NAMES:
                env = os.getenv(f"AGENT_{name.upper()}", "").strip().lower()
                # N'écrase que si la variable est réellement définie : sinon
                # on laisse la valeur par défaut du champ s'appliquer (le
                # validator "before" reçoit uniquement les inputs fournis,
                # jamais les defaults — un setdefault forcerait donc False).
                if env:
                    data[f"flag_{name}"] = env in _TRUE_VALUES
        return data

    @model_validator(mode="after")
    def _validate_provider(self) -> Settings:
        """Fail-fast : cohérence provider / clés API (message explicite)."""
        if self.agent_provider is AgentProvider.OPENROUTER and not self.openrouter_api_key:
            raise ValueError(
                "AGENT_PROVIDER=openrouter exige OPENROUTER_API_KEY (https://openrouter.ai/keys)"
            )
        if self.agent_provider is AgentProvider.HF and not (self.hf_api_key or self.hf_token):
            raise ValueError(
                "AGENT_PROVIDER=hf exige HF_API_KEY (ou HF_TOKEN en repli) "
                "(https://huggingface.co/settings/tokens)"
            )
        return self

    # --- Helpers -------------------------------------------------------------

    @property
    def effective_ws_token(self) -> str:
        """Jeton WebSocket effectif : DASHBOARD_WS_TOKEN sinon api_key (historique)."""
        return self.dashboard_ws_token or self.api_key

    @property
    def effective_hf_key(self) -> str | None:
        """Clé HF effective : HF_API_KEY prioritaire, HF_TOKEN en repli."""
        return self.hf_api_key or self.hf_token

    def active_flags(self) -> dict[str, bool]:
        """Snapshot des feature flags (compatibilité core/feature_flags.features())."""
        return {name: getattr(self, f"flag_{name}") for name in _FLAG_NAMES}


@lru_cache(maxsize=8)
def get_settings(*, env_file: str | None = ".env") -> Settings:
    """Instance Settings mise en cache.

    ``env_file`` : fichier d'environnement lu par pydantic-settings (défaut
    ``.env``) ; passer ``None`` pour l'ignorer. Permet aux tests de rester
    déterministes même si un ``.env`` local embarque des secrets.

    Pour les tests : ``get_settings.cache_clear()`` après modification de
    l'environnement (une entrée de cache par valeur de ``env_file``).
    """
    return Settings(_env_file=env_file)  # type: ignore[call-arg]
