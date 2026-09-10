# project/core/mcp_client_store.py

"""Persistence des clients MCP — registre + révocation + métriques (SQLite).

Ce module est la SOURCE DE VÉRITÉ des clients MCP inscrits. Il stocke :
- l'identité du client (client_id, secret_hash)
- le scope de sécurité (MCPSecurityScope, sérialisé JSON)
- les métriques d'usage (call_count, error_count, scope_usage JSON)

Conventions :
- thread-safe (RLock comme core/session_store.py)
- base SQLite dédiée, surchargeable via MCP_CLIENT_STORE_PATH
- secrets : hash SHA-256 (pas bcrypt ici — le domaine ne gère pas le crypto,
  l'infrastructure fait le hash/salt à l'entrée ; ce store reste simple pour S4)
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import UTC, datetime

from app.domain.ports.mcp_ports import MCPSecurityScope

MCP_CLIENT_STORE_PATH = os.getenv(
    "MCP_CLIENT_STORE_PATH",
    os.path.join("experiments", "mcp_clients.db"),
)


def _utcnow_iso() -> str:
    """Horodatage ISO 8601 UTC (millisecondes) — stable, triable."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ============================================================================
# DOMAIN ERRORS (légers, sans dépendance au domaine pour ce store)
# ============================================================================

class MCPClientStoreError(Exception):
    """Erreur de stockage du registre MCP."""

    pass


class MCPClientAlreadyExistsError(MCPClientStoreError):
    """Un client avec ce client_id existe déjà."""

    pass


class MCPClientNotFoundError(MCPClientStoreError):
    """Le client demandé n'existe pas."""

    pass


class MCPClientRevokedError(MCPClientStoreError):
    """Le client a été révoqué — opération non autorisée."""

    pass


# ============================================================================
# DOMAIN MODEL — représentation interne d'un client MCP (non exposé au domaine)
# ============================================================================

class _MCPClientRecord:
    """Ligne du registre — utilisé en interne pour l'accès SQLite.

    Ce n'est PAS un modèle de domaine (il connaît le stockage JSON).
    Le domaine ne voit que MCPSecurityScope via les ports.
    """

    __slots__ = (
        "client_id",
        "secret_hash",
        "scope_json",
        "call_count",
        "error_count",
        "scope_usage_json",
        "created_at",
        "updated_at",
        "revoked",
        "revoked_at",
        "revoked_reason",
    )

    def __init__(self, row: sqlite3.Row) -> None:
        """Construit un record depuis une ligne SQLite (SELECT partiel autorisé).

        Les colonnes sont lues **par nom** : celles absentes de la requête
        (ex. ``metrics()`` ne charge pas ``scope_json``) sont initialisées
        avec une valeur neutre — le record n'expose que ce qui a été chargé.
        """
        cols = set(row.keys())
        self.client_id = row["client_id"]
        self.secret_hash = row["secret_hash"] if "secret_hash" in cols else ""
        self.scope_json = row["scope_json"] if "scope_json" in cols else "{}"
        self.call_count = row["call_count"] if "call_count" in cols else 0
        self.error_count = row["error_count"] if "error_count" in cols else 0
        self.scope_usage_json = (
            row["scope_usage_json"] if "scope_usage_json" in cols else "{}"
        ) or "{}"
        self.created_at = row["created_at"] if "created_at" in cols else ""
        self.updated_at = row["updated_at"] if "updated_at" in cols else ""
        self.revoked = bool(row["revoked"])
        self.revoked_at = row["revoked_at"] if "revoked_at" in cols else None
        self.revoked_reason = (
            row["revoked_reason"] if "revoked_reason" in cols else ""
        ) or ""

    @property
    def scope(self) -> MCPSecurityScope:
        """Reconstruit le MCPSecurityScope depuis le JSON stocké."""
        return MCPSecurityScope.model_validate_json(self.scope_json)

    @property
    def scope_usage(self) -> dict:
        """Métriques d'usage par scope (tool → count)."""
        return json.loads(self.scope_usage_json)

    def to_public_dict(self) -> dict:
        """Version publique (masque le secret_hash)."""
        return {
            "client_id": self.client_id,
            "tenant_id": self.scope.tenant_id,
            "role": self.scope.role,
            "visible_tools": self.scope.visible_tools,
            "visible_resources": self.scope.visible_resources,
            "visible_prompts": self.scope.visible_prompts,
            "sampling_enabled": self.scope.sampling_enabled,
            "rate_limit_per_minute": self.scope.rate_limit_per_minute,
            "destructive_quota": self.scope.destructive_quota,
            "revoked": self.revoked,
            "revoked_at": self.revoked_at,
            "revoked_reason": self.revoked_reason,
            "call_count": self.call_count,
            "error_count": self.error_count,
            "scope_usage": self.scope_usage,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ============================================================================
# MCP CLIENT STORE — registre thread-safe SQLite
# ============================================================================

class MCPClientStore:
    """Registre des clients MCP avec révocation et métriques (SQLite thread-safe).

    Responsabilités :register(client_id, secret, scope) → crée un client (erreur si existant)
    - revoke(client_id, reason) → révoque un client (erreur si déjà révoqué)
    - list() → liste tous les clients (public, sans secret)
    - metrics(client_id) → call_count, error_rate, scope_usage
    - record_call(client_id, tool_name, success) → incrémente les compteurs
    - get_scope(client_id) → scope du client (utile pour l'enforcement)
    - authenticate(client_id, secret) → vérifie le secret (hash)

    Le store est thread-safe (RLock) et persiste dans experiments/mcp_clients.db
    par défaut (surchargeable via MCP_CLIENT_STORE_PATH).
    """

    def __init__(self, path: str = MCP_CLIENT_STORE_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._ensure_db()

    # ------------------------------------------------------------------
    # Infra — connexion + schéma
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Connexion SQLite (journal mode WAL pour la concurrence)."""
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_db(self) -> None:
        """Crée les tables si absentes (idempotent)."""
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_clients (
                    client_id       TEXT PRIMARY KEY,
                    secret_hash     TEXT NOT NULL,
                    scope_json      TEXT NOT NULL,
                    call_count      INTEGER NOT NULL DEFAULT 0,
                    error_count     INTEGER NOT NULL DEFAULT 0,
                    scope_usage_json TEXT NOT NULL DEFAULT '{}',
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    revoked         INTEGER NOT NULL DEFAULT 0,
                    revoked_at      TEXT,
                    revoked_reason  TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mcp_clients_revoked
                ON mcp_clients(revoked)
                """
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Helpers internes
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_secret(secret: str) -> str:
        """Hash SHA-256 du secret (UTF-8). À usage identitaire, pas crypto fort.

        NOTE : en production, on utiliserait bcrypt/argon2 avec salt — ce store
        reste délibérément simple pour le bootstrap S4 (tâche 10).
        """
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def _scope_to_json(self, scope: MCPSecurityScope) -> str:
        """Sérialise un MCPSecurityScope en JSON (str)."""
        return scope.model_dump_json()

    def _json_to_scope(self, scope_json: str) -> MCPSecurityScope:
        """Désérialise du JSON vers MCPSecurityScope."""
        return MCPSecurityScope.model_validate_json(scope_json)

    # ------------------------------------------------------------------
    # CRUD — register
    # ------------------------------------------------------------------

    def register(
        self,
        client_id: str,
        secret: str,
        scope: MCPSecurityScope,
    ) -> dict:
        """Inscrit un nouveau client MCP.

        Args :
            client_id : identifiant unique (ex. "claude-desktop-prod").
            secret : secret brut (sera hashé SHA-256 en mémoire).
            scope : périmètre de sécurité du client.

        Returns :
            dict public du client créé (sans secret_hash).

        Raises :
            MCPClientAlreadyExistsError : si client_id est déjà inscrit.
            ValueError : si client_id ou secret sont vides.
        """
        if not client_id or not client_id.strip():
            raise ValueError("client_id ne peut pas être vide.")
        if not secret or not secret.strip():
            raise ValueError("secret ne peut pas être vide.")

        client_id = client_id.strip()
        now = _utcnow_iso()

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT client_id FROM mcp_clients WHERE client_id = ?",
                    (client_id,),
                ).fetchone()
                if row is not None:
                    raise MCPClientAlreadyExistsError(
                        f"Le client '{client_id}' existe déjà."
                    )

                scope_json = self._scope_to_json(scope)
                secret_hash = self._hash_secret(secret)

                conn.execute(
                    """
                    INSERT INTO mcp_clients
                        (client_id, secret_hash, scope_json, call_count,
                         error_count, scope_usage_json, created_at, updated_at,
                         revoked, revoked_at, revoked_reason)
                    VALUES (?, ?, ?, 0, 0, '{}', ?, ?, 0, NULL, '')
                    """,
                    (client_id, secret_hash, scope_json, now, now),
                )
                conn.commit()

                row = conn.execute(
                    """
                    SELECT client_id, secret_hash, scope_json, call_count,
                           error_count, scope_usage_json, created_at, updated_at,
                           revoked, revoked_at, revoked_reason
                    FROM mcp_clients WHERE client_id = ?
                    """,
                    (client_id,),
                ).fetchone()
                record = _MCPClientRecord(row)
                return record.to_public_dict()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # CRUD — revoke
    # ------------------------------------------------------------------

    def revoke(self, client_id: str, reason: str) -> dict:
        """Révoque un client MCP (refus immédiat des appels futurs).

        Args :
            client_id : identifiant du client à révoquer.
            reason : motif de la révocation (ex. "compromised_token").

        Returns :
            dict public du client révoqué (avec revoked=True, revoked_at, reason).

        Raises :
            MCPClientNotFoundError : si le client n'existe pas.
            MCPClientRevokedError : si le client est déjà révoqué.
            ValueError : si le motif est vide.
        """
        if not reason or not reason.strip():
            raise ValueError("Le motif de révocation ne peut pas être vide.")

        client_id = client_id.strip()
        now = _utcnow_iso()
        reason = reason.strip()

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT client_id, secret_hash, scope_json, call_count,
                           error_count, scope_usage_json, created_at, updated_at,
                           revoked, revoked_at, revoked_reason
                    FROM mcp_clients WHERE client_id = ?
                    """,
                    (client_id,),
                ).fetchone()
                if row is None:
                    raise MCPClientNotFoundError(
                        f"Le client '{client_id}' est introuvable."
                    )

                record = _MCPClientRecord(row)
                if record.revoked:
                    raise MCPClientRevokedError(
                        f"Le client '{client_id}' est déjà révoqué "
                        f"(motif : {record.revoked_reason})."
                    )

                conn.execute(
                    """
                    UPDATE mcp_clients
                    SET revoked = 1,
                        revoked_at = ?,
                        revoked_reason = ?,
                        updated_at = ?
                    WHERE client_id = ?
                    """,
                    (now, reason, now, client_id),
                )
                conn.commit()

                row = conn.execute(
                    """
                    SELECT client_id, secret_hash, scope_json, call_count,
                           error_count, scope_usage_json, created_at, updated_at,
                           revoked, revoked_at, revoked_reason
                    FROM mcp_clients WHERE client_id = ?
                    """,
                    (client_id,),
                ).fetchone()
                record = _MCPClientRecord(row)
                return record.to_public_dict()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # CRUD — list
    # ------------------------------------------------------------------

    def list(self) -> list[dict]:
        """Liste tous les clients (public, sans secret_hash).

        Returns :
            Liste de dicts publics triés par created_at décroissant.
        """
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT client_id, secret_hash, scope_json, call_count,
                           error_count, scope_usage_json, created_at, updated_at,
                           revoked, revoked_at, revoked_reason
                    FROM mcp_clients
                    ORDER BY rowid DESC
                    """
                ).fetchall()
                return [_MCPClientRecord(r).to_public_dict() for r in rows]
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Métriques — metrics
    # ------------------------------------------------------------------

    def metrics(self, client_id: str) -> dict:
        """Métriques d'usage d'un client.

        Args :
            client_id : identifiant du client.

        Returns :
            dict avec :
            - call_count : nombre total d'appels
            - error_count : nombre d'erreurs
            - error_rate : taux d'erreur (float 0..1)
            - scope_usage : dict tool_name → count
            - revoked : statut de révocation
            - revoked_reason : motif si révoqué

        Raises :
            MCPClientNotFoundError : si le client n'existe pas.
        """
        client_id = client_id.strip()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT client_id, call_count, error_count, scope_usage_json,
                           revoked, revoked_reason
                    FROM mcp_clients WHERE client_id = ?
                    """,
                    (client_id,),
                ).fetchone()
                if row is None:
                    raise MCPClientNotFoundError(
                        f"Le client '{client_id}' est introuvable."
                    )

                record = _MCPClientRecord(row)
                call_count = record.call_count
                error_count = record.error_count
                error_rate = (
                    error_count / call_count if call_count > 0 else 0.0
                )
                return {
                    "client_id": client_id,
                    "call_count": call_count,
                    "error_count": error_count,
                    "error_rate": round(error_rate, 4),
                    "scope_usage": record.scope_usage,
                    "revoked": record.revoked,
                    "revoked_reason": record.revoked_reason,
                }
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Métriques — record_call (incrémentation des compteurs)
    # ------------------------------------------------------------------

    def record_call(self, client_id: str, tool_name: str, success: bool) -> None:
        """Enregistre un appel MCP (incrémente call_count et scope_usage).

        Args :
            client_id : identifiant du client.
            tool_name : nom de l'outil appelé.
            success : True si l'appel a réussi, False en cas d'erreur.

        Raises :
            MCPClientNotFoundError : si le client n'existe pas.
            MCPClientRevokedError : si le client est révoqué.
        """
        client_id = client_id.strip()
        now = _utcnow_iso()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT client_id, call_count, error_count, scope_usage_json,
                           revoked, revoked_reason
                    FROM mcp_clients WHERE client_id = ?
                    """,
                    (client_id,),
                ).fetchone()
                if row is None:
                    raise MCPClientNotFoundError(
                        f"Le client '{client_id}' est introuvable."
                    )

                record = _MCPClientRecord(row)
                if record.revoked:
                    raise MCPClientRevokedError(
                        f"Le client '{client_id}' est révoqué "
                        f"(motif : {record.revoked_reason})."
                    )

                scope_usage = record.scope_usage
                scope_usage[tool_name] = scope_usage.get(tool_name, 0) + 1
                scope_usage_json = json.dumps(scope_usage, sort_keys=True)

                inc_error = 0 if success else 1
                conn.execute(
                    """
                    UPDATE mcp_clients
                    SET call_count = call_count + 1,
                        error_count = error_count + ?,
                        scope_usage_json = ?,
                        updated_at = ?
                    WHERE client_id = ?
                    """,
                    (inc_error, scope_usage_json, now, client_id),
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Scope + Authentification
    # ------------------------------------------------------------------

    def get_scope(self, client_id: str) -> MCPSecurityScope:
        """Retourne le scope de sécurité d'un client.

        Raises :
            MCPClientNotFoundError : si le client n'existe pas.
            MCPClientRevokedError : si le client est révoqué.
        """
        client_id = client_id.strip()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT client_id, scope_json, revoked, revoked_reason
                    FROM mcp_clients WHERE client_id = ?
                    """,
                    (client_id,),
                ).fetchone()
                if row is None:
                    raise MCPClientNotFoundError(
                        f"Le client '{client_id}' est introuvable."
                    )
                record = _MCPClientRecord(row)
                if record.revoked:
                    raise MCPClientRevokedError(
                        f"Le client '{client_id}' est révoqué "
                        f"(motif : {record.revoked_reason})."
                    )
                return record.scope
            finally:
                conn.close()

    def authenticate(self, client_id: str, secret: str) -> bool:
        """Vérifie la combinaison client_id / secret.

        Args :
            client_id : identifiant du client.
            secret : secret brut à vérifier.

        Returns :
            True si le secret correspond (et le client n'est pas révoqué),
            False sinon (client inexistant, secret invalide ou révoqué).

        Note : un client révoqué est traité comme non authentifiable
        (fail-closed : même avec le bon secret, la révocation prime).
        """
        client_id = client_id.strip()
        secret_hash = self._hash_secret(secret)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT client_id, secret_hash, revoked, revoked_reason
                    FROM mcp_clients WHERE client_id = ?
                    """,
                    (client_id,),
                ).fetchone()
                if row is None:
                    return False
                record = _MCPClientRecord(row)
                if record.revoked:
                    return False
                return record.secret_hash == secret_hash
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------

    def get_by_client_id(self, client_id: str) -> dict | None:
        """Retourne le dict public d'un client, ou None si inexistant."""
        client_id = client_id.strip()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT client_id, secret_hash, scope_json, call_count,
                           error_count, scope_usage_json, created_at, updated_at,
                           revoked, revoked_at, revoked_reason
                    FROM mcp_clients WHERE client_id = ?
                    """,
                    (client_id,),
                ).fetchone()
                if row is None:
                    return None
                return _MCPClientRecord(row).to_public_dict()
            finally:
                conn.close()

    def count(self) -> int:
        """Nombre total de clients inscrits."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM mcp_clients"
                ).fetchone()
                return row["cnt"]
            finally:
                conn.close()

    def count_active(self) -> int:
        """Nombre de clients non révoqués."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM mcp_clients WHERE revoked = 0"
                ).fetchone()
                return row["cnt"]
            finally:
                conn.close()

    def count_revoked(self) -> int:
        """Nombre de clients révoqués."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM mcp_clients WHERE revoked = 1"
                ).fetchone()
                return row["cnt"]
            finally:
                conn.close()


# ============================================================================
# SINGLETON — accès paresseux au registre (pattern scope_enforcer.py)
# ============================================================================

_client_store_singleton: MCPClientStore | None = None
_client_store_singleton_lock = threading.Lock()

# Cache du store Mongo (mode PERSISTENCE_BACKEND=mongodb) : instancié UNE seule
# fois puis réutilisé — même sémantique que le singleton SQLite (_client_store_singleton).
# Annoté avec le type de retour du getter (la classe ``MongoMCPClientStore`` n'est
# importable qu'en lazy : import de module circulaire).
_mongo_client_store_singleton: "MCPClientStore | None" = None


def get_mcp_client_store() -> MCPClientStore:
    if os.getenv("PERSISTENCE_BACKEND", "sqlite").lower() == "mongodb":
        from app.infrastructure.persistence.mongodb import MongoMCPClientStore

        global _mongo_client_store_singleton
        with _client_store_singleton_lock:
            if _mongo_client_store_singleton is None:
                _mongo_client_store_singleton = MongoMCPClientStore()  # type: ignore[assignment]
            return _mongo_client_store_singleton  # type: ignore[return-value]
    """Instance unique paresseuse du registre des clients MCP.

    Le path est résolu à la première instanciation (``MCP_CLIENT_STORE_PATH``
    ou défaut ``experiments/mcp_clients.db``) — permet aux tests de redéfinir
    la variable d'environnement avant le premier accès.

    Returns :
        Le ``MCPClientStore`` singleton (thread-safe).
    """
    global _client_store_singleton
    with _client_store_singleton_lock:
        if _client_store_singleton is None:
            _client_store_singleton = MCPClientStore(
                path=os.getenv("MCP_CLIENT_STORE_PATH") or MCP_CLIENT_STORE_PATH
            )
        return _client_store_singleton
