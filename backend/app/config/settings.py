"""Configuration centralisée de l'infrastructure applicative (source unique).

Porte UNIQUEMENT les réglages d'infrastructure — plus AUCUNE configuration de
l'agent (SCRUM-138 : la configuration de l'agent vit désormais dans le module
de configuration de l'IHM, entièrement stockée et chargée depuis la base
MongoDB) :

    - module IHM (store persistant) : ``core/agent_settings.py`` ;
    - modèle typé du noyau v2        : ``app/agent/settings.py`` ;
    - use case API                   : ``app/application/agent_settings_usecase.py``.

Variables d'environnement portées ici :
    - API          : API_KEY, CORS_ALLOWED_ORIGINS, DASHBOARD_WS_TOKEN
    - Persistence  : PERSISTENCE_BACKEND, MONGODB_URI, MONGODB_DATABASE
    - Streams ML   : TRAIN_STREAM_STALL_MINUTES, MODEL_SANITY_MIN_CONFIDENCE

Règles :
    - Lecture paresseuse (get_settings() mis en cache) : les tests peuvent
      modifier l'environnement puis réinitialiser le cache.
    - AUCUNE valeur sensible par défaut : les clés API des providers LLM ne
      passent plus par ici (clés Optionnel du module IHM, cohérence
      provider/clé validée au chargement — fail-fast).
    - Compatibilité : ce module n'affecte PAS le comportement existant tant que
      les modules historiques lisent encore os.getenv directement ; il reste la
      source unique des réglages d'infrastructure (API, persistence, ML).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _load_dotenv_to_environ() -> None:
    """Charge backend/.env (et .env racine en repli) dans ``os.environ``.

    Pourquoi : les stores legacy (``core/*_store.py``) et ``MongoConfig``
    lisent ``os.getenv`` DIRECTEMENT, sans passer par pydantic-settings.
    Sans ce chargement, ``PERSISTENCE_BACKEND=mongodb`` posé uniquement dans
    un fichier ``.env`` serait invisible pour eux et le runtime resterait en
    SQLite. Parser stdlib uniquement (pas de dépendance ``python-dotenv``) :
    lignes ``CLE=valeur``, `#` commentaires, guillemets simples/doubles
    retirés. Ne surcharge JAMAIS une variable déjà exportée (l'env réel
    garde la priorité sur le fichier).
    """

    def _parse(path) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value

    from pathlib import Path as _Path

    _backend_dir = _Path(__file__).resolve().parents[2]  # backend/
    _parse(_backend_dir / ".env")  # backend/.env d'abord (le plus spécifique)
    _parse(_backend_dir.parent / ".env")  # racine repo en repli


_load_dotenv_to_environ()


class Settings(BaseSettings):
    """Réglages d'INFRASTRUCTURE de l'application, validés (pydantic-settings).

    Configuration de l'agent : voir ``app/agent/settings.py`` (modèle typé
    ``AgentConfig`` chargé depuis le store IHM / MongoDB) — délibérément
    ABSENTE d'ici (SCRUM-138) : aucun paramètre ``agent_*``, aucune clé LLM,
    aucun feature flag agent ni réglage MCP ne doit revenir dans ce module.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # tolère les variables hors périmètre (.env utilisateur)
    )

    # Persistence SQLite par défaut ; Atlas activé explicitement via
    # PERSISTENCE_BACKEND=mongodb (cf. backend/.env, gitignoré — jamais de
    # secret en dur ici). MongoConfig (persistence/mongodb.py) lit MONGODB_URI
    # depuis l'environnement, alimenté ci-dessous par _load_dotenv_to_environ().
    persistence_backend: Literal["sqlite", "mongodb"] = "sqlite"
    mongodb_uri: str | None = None
    mongodb_database: str = "thinktuning"

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

    # --- Streams / ML ---------------------------------------------------------
    train_stream_stall_minutes: int = Field(default=5, ge=1)
    model_sanity_min_confidence: float = Field(default=0.4, ge=0.0, le=1.0)

    # --- Helpers -------------------------------------------------------------

    @property
    def effective_ws_token(self) -> str:
        """Jeton WebSocket effectif : DASHBOARD_WS_TOKEN sinon api_key (historique)."""
        return self.dashboard_ws_token or self.api_key


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
