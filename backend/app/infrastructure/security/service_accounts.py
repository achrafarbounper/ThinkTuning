# project/app/infrastructure/security/service_accounts.py

"""Service accounts (P2 durable, lot 13) — store SQLite + authentification.

Machines (CI, dashboards, batch, MCP clients) authentifiées par **client
credentials** (client_id + client_secret) échangeés contre un **JWT courte
durée** (15 min par défaut — cf. ``app/domain/tokens.py``).

Design (aligné sur les conventions de ``core/audit_store.py``) :
    - base SQLite dédiée ``experiments/service_accounts.db`` (surchargeable
      via ``SERVICE_ACCOUNTS_PATH``, isolation des tests) ;
    - store thread-safe, singleton paresseux ``get_service_account_store()``
      + ``reset_service_account_store()`` ;
    - le secret n'est JAMAIS stocké en clair : ``sha256(secret)`` uniquement
      (comme les mots de passe — un dump SQLite ne livre rien) ;
    - comparaison à temps constant à l'authentification ;
    - révocation : hard-delete de l'account ET jti en table
      ``revoked_tokens`` (court séjour : exp court — purge par TTL simple) ;
    - chaque authentification / création / révocation est AUDITÉE (store
      d'audit partagé, action ``service_account_*``) ;
    - least-privilege : rôle ``read`` pour le monitoring/CI, ``admin`` pour
      les pipelines d'écriture (hiérarchie identique à API_KEY/API_KEY_READ).

Sécurité du secret : généré par ``secrets.token_urlsafe(32)``, affiché UNE
seule fois à la création (le store ne le garde pas en clair).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import UTC, datetime

from app.domain.tokens import TokenClaims, create_access_token

logger = logging.getLogger(__name__)

# Email pragmatique (aligne la validation backend sur le frontend, sans
# dépendance email-validator) : local-part@domaine.tld, ASCII simple.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", re.IGNORECASE)

# Bornes du mot de passe d'inscription (choisi par l'utilisateur) — alignées
# sur la validation frontend (min 8) et le champ Pydantic de la route.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


class RegistrationError(ValueError):
    """Inscription invalide (email ou mot de passe non conforme)."""


class EmailAlreadyTakenError(RegistrationError):
    """Un compte existe déjà pour cet email (l'email est le ``name`` unique)."""

SERVICE_ACCOUNTS_PATH = os.getenv(
    "SERVICE_ACCOUNTS_PATH", os.path.join("experiments", "service_accounts.db")
)

# Durée de vie par défaut du jeton émis (courte) — surchargeable.
DEFAULT_TOKEN_TTL_SECONDS = 900  # 15 minutes
MAX_TOKEN_TTL_SECONDS = 86400  # 24 h (plafond dur, cf. domaine)

_ACTIONS = {
    "created": "service_account_created",
    "registered": "service_account_registered",
    "authed": "service_account_authenticated",
    "revoked": "service_account_revoked",
    "list": "service_account_listed",
    "token_revoked": "service_account_token_revoked",
}


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _audit(action: str, subject: str, detail: dict) -> None:
    """Audite une opération service account (best-effort, jamais bloquant)."""
    try:
        from core.audit_store import get_audit_store

        get_audit_store().log(
            action=action,
            subject=subject or "service_accounts",
            detail=detail,
            actor="service_accounts",
        )
    except Exception as exc:  # pragma: no cover - défensif
        logger.warning("Audit service_accounts indisponible : %s", exc)


class ServiceAccountStore:
    """Service accounts chiffrés (SQLite, thread-safe)."""

    def __init__(self, path: str = SERVICE_ACCOUNTS_PATH):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._ensure_db()

    # --- Infra -----------------------------------------------------------------

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30.0)

    def _ensure_db(self):
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS service_accounts (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE,
                role        TEXT NOT NULL CHECK (role IN ('admin', 'read')),
                scopes_json TEXT NOT NULL DEFAULT '[]',
                secret_hash TEXT NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL,
                last_used_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS revoked_tokens (
                jti        TEXT PRIMARY KEY,
                expires_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_revoked_expires ON revoked_tokens(expires_at)"
        )
        conn.commit()
        conn.close()

    def _purge_expired_revocations(self) -> None:
        """Nettoie les jti révokés expirés (TTL court => purge fréquente)."""
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM revoked_tokens WHERE expires_at < ?",
                (int(datetime.now(UTC).timestamp()),),
            )
            conn.commit()
        finally:
            conn.close()

    # --- Écriture --------------------------------------------------------------

    def create_account(
        self, *, name: str, role: str = "read", scopes: list[str] | None = None
    ) -> dict:
        """Crée un service account. Retourne le secret EN CLAIR (une seule fois).

        Le secret généré (256 bits) n'est jamais persisté : seul son hash
        SHA-256 est stocké. ``name`` est unique (fail-fast en doublon).
        """
        name = (name or "").strip()
        if not name or len(name) < 3:
            raise ValueError("nom du service account requis (>= 3 caractères)")
        if role not in ("admin", "read"):
            raise ValueError(f"rôle inconnu : {role!r} (admin|read)")
        account_id = str(uuid.uuid4())
        secret = secrets.token_urlsafe(32)
        secret_hash = _hash_secret(secret)
        now = _utcnow_iso()
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO service_accounts "
                        "(id, name, role, scopes_json, secret_hash, enabled, created_at)"
                        " VALUES (?, ?, ?, ?, ?, 1, ?)",
                        (
                            account_id,
                            name,
                            role,
                            json.dumps(list(scopes or [])),
                            secret_hash,
                            now,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"nom de service account déjà pris : {name}") from exc
            finally:
                conn.close()
        _audit(
            _ACTIONS["created"],
            subject=name,
            detail={"account_id": account_id, "role": role, "scopes": list(scopes or [])},
        )
        logger.info("service_account_created name=%s role=%s", name, role)
        return {
            "id": account_id,
            "name": name,
            "role": role,
            "scopes": list(scopes or []),
            "client_secret": secret,  # AFFICHÉ UNE FOIS — à conserver par l'appelant
        }

    def revoke_account(self, account_id: str) -> bool:
        """Supprime un service account (révocation immédiate de ses secrets)."""
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cursor = conn.execute(
                        "DELETE FROM service_accounts WHERE id = ?", (str(account_id),)
                    )
            finally:
                conn.close()
        deleted = cursor.rowcount > 0
        if deleted:
            _audit(_ACTIONS["revoked"], subject=account_id, detail={})
            logger.warning("service_account_revoked id=%s", account_id)
        return deleted

    def register_account(
        self,
        *,
        email: str,
        password: str,
        role: str = "read",
        scopes: list[str] | None = None,
    ) -> dict:
        """Inscription publique : crée un compte utilisateur à partir d'un email.

        Différence clé avec ``create_account`` (admin) : le SECRET est choisi
        par l'utilisateur (son mot de passe) au lieu d'être généré. Stockage
        identique — seul ``sha256(secret)`` est persisté, jamais le mot de
        passe en clair.

        Règles :
            - l'email est normalisé (minuscules, espaces retirés) et devient
              le ``name`` UNIQUE du compte (connexion possible par email via
              ``issue_token``, qui résout aussi par ``name``) ;
            - le rôle est ``read`` par défaut (least-privilege : la montée en
              rôle reste une décision ADMIN) ;
            - ``EmailAlreadyTakenError`` (409) si l'email est déjà pris ;
              ``RegistrationError`` (422) si l'email ou le mot de passe ne
              satisfont pas les règles de validation.
        """
        email = (email or "").strip().lower()
        if not email or not _EMAIL_RE.match(email):
            raise RegistrationError("adresse email invalide")
        if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
            raise RegistrationError(
                f"mot de passe requis ({MIN_PASSWORD_LENGTH} à {MAX_PASSWORD_LENGTH} caractères)"
            )
        if role not in ("admin", "read"):
            raise RegistrationError(f"rôle inconnu : {role!r} (admin|read)")
        account_id = str(uuid.uuid4())
        secret_hash = _hash_secret(password)
        now = _utcnow_iso()
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO service_accounts "
                        "(id, name, role, scopes_json, secret_hash, enabled, created_at)"
                        " VALUES (?, ?, ?, ?, ?, 1, ?)",
                        (
                            account_id,
                            email,
                            role,
                            json.dumps(list(scopes or [])),
                            secret_hash,
                            now,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise EmailAlreadyTakenError(f"un compte existe déjà avec cet email : {email}") from exc
            finally:
                conn.close()
        _audit(
            _ACTIONS["registered"],
            subject=email,
            detail={"account_id": account_id, "role": role, "scopes": list(scopes or [])},
        )
        logger.info("service_account_registered email=%s role=%s", email, role)
        return {"id": account_id, "email": email, "role": role}

    # --- Lecture -----------------------------------------------------------------

    def list_accounts(self) -> list[dict]:
        """Listing (le hash de secret n'est JAMAIS exposé)."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, name, role, scopes_json, enabled, created_at, "
                    "last_used_at FROM service_accounts ORDER BY created_at"
                ).fetchall()
            finally:
                conn.close()
        accounts = []
        for row in rows:
            _id, name, role, scopes_json, enabled, created_at, last_used = row
            accounts.append(
                {
                    "id": _id,
                    "name": name,
                    "role": role,
                    "scopes": json.loads(scopes_json or "[]"),
                    "enabled": bool(enabled),
                    "created_at": created_at,
                    "last_used_at": last_used or "",
                }
            )
        _audit(_ACTIONS["list"], subject="service_accounts", detail={"count": len(accounts)})
        return accounts

    def _get_by_client_id(self, account_id: str) -> dict | None:
        """Résout un identifiant par ``id`` (comptes admin, UUID) OU ``name``.

        ``name`` = email normalisé pour les comptes inscrits via
        ``register_account`` : le dashboard se connecte ainsi avec son adresse
        email, sans dépendre de l'UUID interne.

        ``client_id`` est normalisé (strip + minuscules) pour aligner la
        connexion sur la normalisation de l'inscription : les emails sont
        stockés en minuscules, la résolution doit donc matcher quelle que soit
        la casse saisie (les UUID générés sont déjà en minuscules — sans
        effet de bord).
        """
        client_id = str(account_id).strip().lower()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, name, role, scopes_json, secret_hash, enabled "
                    "FROM service_accounts WHERE id = ? OR name = ?",
                    (client_id, client_id),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        _id, name, role, scopes_json, secret_hash, enabled = row
        return {
            "id": _id,
            "name": name,
            "role": role,
            "scopes": json.loads(scopes_json or "[]"),
            "secret_hash": secret_hash,
            "enabled": bool(enabled),
        }
# --- Authentification -----------------------------------------------------------

    def issue_token(
        self,
        *,
        client_id: str,
        client_secret: str,
        jwt_secret: str,
        ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    ) -> dict:
        """Échange client credentials contre un JWT courte durée.

        Lève ``PermissionError`` (identifiants inconnus / account désactivé /
        secret invalide — message unique anti-énumération). Audit de chaque
        tentative (ok/ko), cohérent avec l'exigence d'audit des accès.
        """
        account = self._get_by_client_id(client_id)
        if account is None or not account["enabled"]:
            _audit(_ACTIONS["authed"], subject=client_id, detail={"ok": False})
            raise PermissionError("client_id ou secret invalide")
        if not secrets.compare_digest(account["secret_hash"], _hash_secret(client_secret)):
            _audit(_ACTIONS["authed"], subject=client_id, detail={"ok": False})
            raise PermissionError("client_id ou secret invalide")

        token = create_access_token(
            subject=account["name"],
            secret=jwt_secret,
            role=account["role"],
            scopes=account["scopes"],
            ttl_seconds=ttl_seconds,
        )
        # Trace last_used_at (best-effort).
        try:
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        "UPDATE service_accounts SET last_used_at = ? WHERE id = ?",
                        (_utcnow_iso(), account["id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception:  # pragma: no cover - défensif
            pass
        _audit(
            _ACTIONS["authed"],
            subject=account["name"],
            detail={"ok": True, "role": account["role"]},
        )
        return {"token": token, "role": account["role"]}

    # --- Révocation par jti ---------------------------------------------------------

    def revoke_token(self, claims: TokenClaims) -> bool:
        """Révoque un jti (le reste de la durée de vie du jeton est annulé)."""
        if not claims.jti:
            return False
        self._purge_expired_revocations()
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO revoked_tokens (jti, expires_at) "
                        "VALUES (?, ?)",
                        (claims.jti, claims.expires_at),
                    )
            finally:
                conn.close()
        _audit(_ACTIONS["token_revoked"], subject=claims.subject, detail={"jti": claims.jti})
        return True

    def is_token_revoked(self, jti: str) -> bool:
        """True si le jti a été révoqué (vérifié à chaque requête)."""
        if not jti:
            return False
        self._purge_expired_revocations()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT 1 FROM revoked_tokens WHERE jti = ?", (str(jti),)
                ).fetchone()
            finally:
                conn.close()
        return row is not None


def _hash_secret(secret: str) -> str:
    """SHA-256 hex du secret (stockage — jamais en clair)."""
    import hashlib

    return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()


# --- Store partagé (lazy, surchargeable en tests) ------------------------------------

_store: ServiceAccountStore | None = None
_store_lock = threading.Lock()


def _current_path() -> str:
    return os.getenv("SERVICE_ACCOUNTS_PATH") or SERVICE_ACCOUNTS_PATH


def get_service_account_store() -> ServiceAccountStore:
    """Store partagé de l'application (instance unique paresseuse)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = ServiceAccountStore(_current_path())
        return _store


def reset_service_account_store(path: str | None = None) -> ServiceAccountStore:
    """Remplace le store partagé par une base neuve (isolation des tests)."""
    global _store
    with _store_lock:
        _store = ServiceAccountStore(path or _current_path())
        return _store


__all__ = [
    "DEFAULT_TOKEN_TTL_SECONDS",
    "EmailAlreadyTakenError",
    "MAX_PASSWORD_LENGTH",
    "MAX_TOKEN_TTL_SECONDS",
    "MIN_PASSWORD_LENGTH",
    "RegistrationError",
    "SERVICE_ACCOUNTS_PATH",
    "ServiceAccountStore",
    "get_service_account_store",
    "reset_service_account_store",
]
