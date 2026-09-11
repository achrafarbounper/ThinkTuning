"""MongoDB-backed service accounts and token helpers."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from datetime import UTC, datetime

from app.infrastructure.persistence.mongodb import (
    MongoServiceAccountStore as _MongoServiceAccountStore,
)

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
        from core.audit_store import get_audit_store

        get_audit_store().log(
            action=action,
            subject=subject or "service_accounts",
            detail=detail,
            actor="service_accounts",
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Audit service_accounts indisponible : %s", exc)


class ServiceAccountStore(_MongoServiceAccountStore):
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
