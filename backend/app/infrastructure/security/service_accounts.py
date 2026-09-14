"""MongoDB-backed service accounts and token helpers."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from datetime import UTC, datetime

from app.infrastructure.persistence.common import _utcnow, get_mongo_provider

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", re.IGNORECASE)
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
DEFAULT_TOKEN_TTL_SECONDS = 900
MAX_TOKEN_TTL_SECONDS = 86400
SERVICE_ACCOUNTS_PATH = ""


class RegistrationError(ValueError):
    """Inscription invalide."""


class EmailAlreadyTakenError(RegistrationError):
    """Un compte existe déjà pour cet email."""


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()


def _audit(action: str, subject: str, detail: dict) -> None:
    try:
        from app.infrastructure.persistence.audit_store import get_audit_store

        get_audit_store().log(
            action=action,
            subject=subject or "service_accounts",
            detail=detail,
            actor="service_accounts",
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Audit service_accounts indisponible : %s", exc)


class MongoServiceAccountStore:
    """Service-account store preserving the public SQLite API.

    Rapatrié de ``persistence.mongodb`` (B-4 / ADR-0004) : le module
    ``service_accounts`` n'importe plus le hub MongoDB — il possède sa propre
    implémentation et n'expose plus de symboles privés à l'extérieur
    (``_audit`` / ``_hash_secret`` restent locaux).
    """

    def __init__(self, provider=None):
        self.c = (provider or get_mongo_provider()).collection("service_accounts")
        self.revoked = (provider or get_mongo_provider()).collection("revoked_tokens")

    def create_account(self, *, name, role="read", scopes=None):
        import secrets

        if not name or len(name.strip()) < 3 or role not in ("admin", "read"):
            raise ValueError("nom ou rôle de service account invalide")
        name = name.strip()
        if self.c.find_one({"name": name}):
            raise ValueError(f"nom de service account déjà pris : {name}")
        secret = secrets.token_urlsafe(32)
        d = {
            "_id": str(uuid.uuid4()),
            "name": name,
            "role": role,
            "scopes": list(scopes or []),
            "secret_hash": _hash_secret(secret),
            "enabled": True,
            "created_at": _utcnow(),
            "last_used_at": "",
        }
        self.c.insert_one(d)
        _audit(
            "service_account_created",
            name,
            {"account_id": d["_id"], "role": role, "scopes": d["scopes"]},
        )
        return {
            "id": d["_id"],
            "name": name,
            "role": role,
            "scopes": d["scopes"],
            "client_secret": secret,
        }

    def register_account(self, *, email, password, role="read", scopes=None):

        email = (email or "").strip().lower()
        if "@" not in email:
            raise RegistrationError("adresse email invalide")
        if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
            raise RegistrationError("mot de passe de longueur invalide")
        if self.c.find_one({"name": email}):
            raise EmailAlreadyTakenError(f"un compte existe déjà avec cet email : {email}")

        d = {
            "_id": str(uuid.uuid4()),
            "name": email,
            "role": role,
            "scopes": list(scopes or []),
            "secret_hash": _hash_secret(password),
            "enabled": True,
            "created_at": _utcnow(),
            "last_used_at": "",
        }
        self.c.insert_one(d)
        _audit("service_account_registered", email, {"account_id": d["_id"], "role": role})
        return {"id": d["_id"], "email": email, "role": role}

    def list_accounts(self):
        out = []
        for d in self.c.find({}):
            out.append(
                {
                    "id": d["_id"],
                    "name": d.get("name", ""),
                    "role": d.get("role", ""),
                    "scopes": d.get("scopes", []),
                    "enabled": bool(d.get("enabled")),
                    "created_at": d.get("created_at", ""),
                    "last_used_at": d.get("last_used_at", ""),
                }
            )
        return out

    def _get_by_client_id(self, account_id):
        return self.c.find_one(
            {
                "$or": [
                    {"_id": str(account_id)},
                    {"name": str(account_id).strip().lower()},
                ]
            }
        )

    def issue_token(self, *, client_id, client_secret, jwt_secret, ttl_seconds=900):
        import secrets

        d = self._get_by_client_id(client_id)
        if (
            not d
            or not d.get("enabled")
            or not secrets.compare_digest(d["secret_hash"], _hash_secret(client_secret))
        ):
            _audit("service_account_authenticated", client_id, {"ok": False})
            raise PermissionError("client_id ou secret invalide")
        from app.domain.tokens import create_access_token

        token = create_access_token(
            subject=d["name"],
            secret=jwt_secret,
            role=d["role"],
            scopes=d.get("scopes", []),
            ttl_seconds=ttl_seconds,
        )
        self.c.update_one({"_id": d["_id"]}, {"$set": {"last_used_at": _utcnow()}})
        return {"token": token, "role": d["role"]}

    def revoke_account(self, account_id):
        return bool(self.c.delete_one({"_id": str(account_id)}).deleted_count)

    def revoke_token(self, claims):
        if not claims.jti:
            return False
        self.revoked.update_one(
            {"_id": claims.jti},
            {"$set": {"expires_at": claims.expires_at}},
            upsert=True,
        )
        return True

    def is_token_revoked(self, jti):
        return bool(jti and self.revoked.find_one({"_id": str(jti)}))


class ServiceAccountStore(MongoServiceAccountStore):
    """Compatibility facade over the MongoDB implementation."""

    def __init__(self, path: str | None = None, provider=None):
        super().__init__(provider=provider)


_store: ServiceAccountStore | None = None
_store_lock = threading.Lock()


def get_service_account_store() -> ServiceAccountStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = ServiceAccountStore()
        return _store


def reset_service_account_store(path: str | None = None) -> ServiceAccountStore:
    global _store
    with _store_lock:
        _store = ServiceAccountStore()
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
