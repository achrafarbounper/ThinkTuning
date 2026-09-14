"""Module neutre de la persistance MongoDB (ADR-0004, B-4).

Contient les éléments partagés par tous les adaptateurs MongoDB, extraits du
hub historique ``persistence.mongodb`` pour casser les cycles d'imports :

  - ``MongoConfig`` / ``MongoClientProvider`` : configuration Atlas et client
    (mongomock accepté via ``MONGODB_MOCK`` — les tests n'ouvrent jamais de
    connexion réseau) ;
  - singleton provider : ``get_mongo_provider`` / ``set_mongo_provider`` /
    ``reset_mongo_provider`` ;
  - erreurs et helpers d'URI : ``AtlasConnectionError``,
    ``ATLAS_CONNECTION_HINT``, ``_normalize_atlas_uri``, ``_safe_host`` ;
  - helper d'horodatage ``_utcnow`` et décodage des valeurs de paramètres
    agent ``_decode_settings_value`` (parité SQLite, SCRUM-137) ;
  - registre late-binding des implémentations Mongo des stores
    (``register_mongo_store`` / ``get_mongo_store_class``) : les modules de
    store (``audit_store``, ``run_store``, ``flow_store``, ``agent_settings``,
    ``mcp_client_store``) n'importent plus ``persistence.mongodb`` — leurs
    factories résolvent l'implémentation via ce registre, rempli au chargement
    de ``persistence.mongodb``. Dépendance unidirectionnelle :
    ``mongodb -> common`` et ``stores -> common``, jamais l'inverse.

Ce module n'importe AUCUN autre module de ``persistence`` ni de ``app.*``
(hors stdlib) : c'est la condition de neutralité qui autorise tous les
stores à en dépendre sans recréer de cycle.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("thinktuning.persistence.common")

# Aide actionnable jointe aux erreurs de connexion Atlas : Render n'expose
# aucune IP sortante fixe → l'IP Access List d'Atlas doit accepter 0.0.0.0/0,
# sinon le handshake TLS est coupé (TLSV1_ALERT_INTERNAL_ERROR) avant toute
# authentification. Voir README → « Déploiement (Render) — MongoDB Atlas ».
ATLAS_CONNECTION_HINT = (
    "Causes probables : 1) IP Access List Atlas restrictive — ajouter 0.0.0.0/0 "
    "(Atlas → Network Access → Add IP Access List Entry), Render n'expose aucune "
    "IP sortante fixe ; 2) URI invalide — attendue : "
    "mongodb+srv://<user>:<pass>@<cluster>.mongodb.net/?retryWrites=true&w=majority "
    "(mot de passe URL-encodé)."
)


class AtlasConnectionError(RuntimeError):
    """Échec de connexion MongoDB Atlas avec diagnostic actionnable."""


def _normalize_atlas_uri(uri: str) -> str:
    """Force le schéma SRV pour les hôtes Atlas (``*.mongodb.net``).

    ``mongodb://`` (sans ``+srv``) sur un cluster partagé Atlas casse le
    handshake TLS (SNI requis) ; ``mongodb+srv://`` active TLS + SNI +
    résolution DNS SRV en une fois. Les URI hors Atlas (localhost, mongomock,
    base self-managed) sont laissées telles quelles.
    """
    srv_prefix = "mongodb+srv://"
    plain_prefix = "mongodb://"
    if uri.startswith(plain_prefix) and ".mongodb.net" in uri:
        return srv_prefix + uri[len(plain_prefix) :]
    return uri


def _safe_host(uri: str) -> str:
    """Hôte(s) d'une URI sans jamais exposer le userinfo (mot de passe)."""
    netloc = urlsplit(uri).netloc
    return netloc.split("@", 1)[1] if "@" in netloc else netloc


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class MongoConfig:
    """Validated, environment-based Atlas configuration.

    Source unique : ``backend/.env`` (gitignoré) chargé paresseusement ici
    aussi — importer ``persistence.mongodb`` directement (sans passer par
    ``app.config.settings``) doit quand même voir ``MONGODB_URI``. En dernier
    recours, repli sur ``Settings`` (pydantic-settings lit l'env + `.env`).
    Priorité : arg explicite > variable d'environnement > Settings.
    """

    def __init__(self, uri: str | None = None, database: str | None = None) -> None:
        if not uri:
            try:
                from app.config.settings import _load_dotenv_to_environ

                _load_dotenv_to_environ()
            except Exception:
                pass
        settings_uri: str | None = None
        settings_db: str | None = None
        if not os.getenv("MONGODB_URI", "") or not os.getenv("MONGODB_DATABASE", ""):
            try:
                from app.config.settings import get_settings

                _s = get_settings()
                settings_uri = _s.mongodb_uri
                settings_db = _s.mongodb_database
            except Exception:
                pass
        self.uri = uri or os.getenv("MONGODB_URI", "") or settings_uri or ""
        self.database = (
            database or os.getenv("MONGODB_DATABASE", "") or settings_db or "thinktuning"
        )
        if not self.uri:
            raise ValueError("MONGODB_URI is required for MongoDB persistence")
        # Toute URI « mongodb://…*.mongodb.net » est réécrite en
        # « mongodb+srv:// » (TLS + SNI + SRV, cf. _normalize_atlas_uri) : la
        # forme sans +srv échoue au handshake TLS sur les clusters partagés.
        self.uri = _normalize_atlas_uri(self.uri)


class MongoClientProvider:
    def __init__(self, config: MongoConfig | None = None, client: Any | None = None) -> None:
        if client is not None:
            self.client = client
            cfg = config
            database = cfg.database if cfg else os.getenv("MONGODB_DATABASE", "thinktuning")
            self.db = client[database]
            self._ensure_indexes()
            return
        try:
            from pymongo import MongoClient
            from pymongo.errors import ConfigurationError, ServerSelectionTimeoutError
        except ImportError as exc:  # pragma: no cover - exercised in deployments
            raise RuntimeError("pymongo is required for MongoDB persistence") from exc
        cfg = config or MongoConfig()
        try:
            self.client = MongoClient(
                cfg.uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                retryWrites=True,
            )
            self.db = self.client[cfg.database]
            # MongoClient est lazy : on force un ping immédiat pour échouer AU
            # DÉMARRAGE avec un diagnostic actionnable plutôt qu'au premier store
            # (ServerSelectionTimeout différé et illisible).
            self._ping()
            self._ensure_indexes()
        except (ConfigurationError, ServerSelectionTimeoutError) as exc:
            logger.error(
                "Connexion MongoDB Atlas impossible : %s (%s)",
                exc,
                type(exc).__name__,
            )
            message = f"MongoDB Atlas injoignable : {type(exc).__name__}. {ATLAS_CONNECTION_HINT}"
            raise AtlasConnectionError(message) from exc
        logger.info("MongoDB Atlas joignable (db=%s, hôte=%s)", cfg.database, _safe_host(cfg.uri))

    def _ping(self) -> None:
        """Force une opération réseau : le constructeur ``MongoClient`` est lazy.

        Sans ce ping, un hôte injoignable n'échoue qu'à la première opération
        des stores (erreur ``ServerSelectionTimeoutError`` brute et tardive).
        """
        self.client.admin.command("ping")

    def _ensure_indexes(self) -> None:
        indexes = {
            "agent_sessions": [[("updated_at", -1)]],
            "agent_session_messages": [[("session_id", 1), ("_order", 1)]],
            "agent_runs": [[("created_at", -1)], [("status", 1), ("created_at", -1)]],
            "agent_flows": [[("created_at", -1)], [("status", 1), ("created_at", -1)]],
            "agent_approvals": [[("status", 1), ("created_at", -1)]],
            "agent_audit": [[("ts", -1)], [("action", 1), ("ts", -1)], [("run_id", 1), ("ts", -1)]],
            "jobs": [[("updated_at", -1)], [("payload.status", 1), ("updated_at", -1)]],
            "train_metrics": [[("job_id", 1), ("epoch", 1)]],
            "scheduled_jobs": [[("updated_at", 1)]],
            "mcp_clients": [[("revoked", 1)]],
        }
        for collection_name, specs in indexes.items():
            collection = self.db[collection_name]
            for spec in specs:
                collection.create_index(spec)

    def collection(self, name: str):
        return self.db[name]


_provider: MongoClientProvider | None = None
_provider_lock = threading.Lock()


def get_mongo_provider() -> MongoClientProvider:
    global _provider
    with _provider_lock:
        if _provider is None:
            if os.getenv("MONGODB_MOCK", "").lower() in {"1", "true", "yes"}:
                try:
                    import mongomock
                except ImportError as exc:  # pragma: no cover - deployment guard
                    raise RuntimeError(
                        "mongomock is required when MONGODB_MOCK is enabled"
                    ) from exc
                _provider = MongoClientProvider(
                    MongoConfig(uri="mongodb://localhost/thinktuning"),
                    client=mongomock.MongoClient(),
                )
            else:
                _provider = MongoClientProvider()
        return _provider


def set_mongo_provider(provider: MongoClientProvider | None) -> None:
    """Inject a provider for tests and close the previous provider safely."""
    global _provider
    with _provider_lock:
        if _provider is not None and _provider is not provider:
            _provider.client.close()
        _provider = provider


def reset_mongo_provider() -> None:
    global _provider
    with _provider_lock:
        if _provider is not None:
            _provider.client.close()
        _provider = None


# --- Registre late-binding des implémentations Mongo des stores (ADR-0004) ---
#
# ``persistence.mongodb`` enregistre ses classes (``register_mongo_store``) au
# chargement du module ; les factories des stores (``get_audit_store`` & co)
# résolvent l'implémentation via ``get_mongo_store_class`` au moment de
# l'appel — sans importer ``persistence.mongodb``. Même technique que le
# late-binding ``_tool=tool`` de ``agent_core`` (B-8) : la dépendance reste
# injectée au runtime, jamais syntaxique au chargement.
_MONGO_STORE_CLASSES: dict[str, type] = {}


def register_mongo_store(key: str, cls: type) -> None:
    """Enregistre l'implémentation Mongo d'un store (idempotent)."""
    _MONGO_STORE_CLASSES[str(key)] = cls


def get_mongo_store_class(key: str) -> type:
    """Résout l'implémentation Mongo d'un store enregistrée par ``mongodb``."""
    try:
        return _MONGO_STORE_CLASSES[str(key)]
    except KeyError as exc:
        raise RuntimeError(
            f"Aucune implémentation Mongo enregistrée pour '{key}' : importer "
            "``app.infrastructure.persistence.mongodb`` avant d'utiliser le "
            "backend PERSISTENCE_BACKEND=mongodb."
        ) from exc


def _decode_settings_value(value: Any) -> Any:
    """Décode une valeur de paramètre agent lue dans Mongo (SCRUM-137).

    Parité avec le store SQLite
    (``app/infrastructure/persistence/agent_settings.AgentSettingsStore``),
    qui persiste ses valeurs JSON-encodées (``json.dumps``) et les relit via
    ``json.loads``. La migration ``scripts/migrate_sqlite_to_mongodb.py``
    copie les documents SQLite TELS QUELS : une valeur arrivée par ce chemin
    est donc une chaîne JSON (ex. ``'"openrouter"'`` avec guillemets) alors
    que les écritures runtime les stockent natives. Ce décodage tolérant
    normalise les TROIS états possibles :

      - chaîne JSON         → valeur décodée (``'"openrouter"'`` → ``openrouter``) ;
      - chaîne brute        → inchangée (``openrouter`` seul n'est pas du JSON valide) ;
      - valeur typée        → inchangée (int/float/bool/None conservés tels quels).
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value
