"""Tests P1 SEC — RBAC minimal (clé de lecture), rotation de clé avec grâce
et auth WebSocket (``api/dependencies/auth.py`` + ``security/api_key.py``).

Lance avec : pytest tests/test_auth_rbac.py -v
"""

import pytest
from fastapi import HTTPException

from api.dependencies.auth import require_api_key, require_read_api_key, ws_is_authorized
from app.infrastructure.security.api_key import (
    DEV_FALLBACK_KEY,
    is_valid_api_key,
    is_valid_read_api_key,
    old_key_expired,
)

# --- Clé de lecture (least-privilege) -------------------------------------------


def test_read_key_opens_read_scope(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "admin-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY_READ", "read-key-0123456789")

    assert is_valid_read_api_key("read-key-0123456789")
    assert is_valid_read_api_key("admin-key-0123456789abcdef")  # admin surclasse read
    # Une clé read NE peut PAS ouvrir les routes admin :
    assert not is_valid_api_key("read-key-0123456789")


def test_read_key_absent_admin_only(monkeypatch) -> None:
    monkeypatch.delenv("API_KEY_READ", raising=False)
    monkeypatch.setenv("API_KEY", "admin-key-0123456789abcdef")
    assert is_valid_read_api_key("admin-key-0123456789abcdef")
    assert not is_valid_read_api_key("anything-else")


def test_none_and_unknown_fail_closed() -> None:
    assert not is_valid_api_key(None)
    assert not is_valid_read_api_key(None)
    assert not is_valid_api_key("intruder-key")
    assert not is_valid_read_api_key(DEV_FALLBACK_KEY)


# --- Rotation : API_KEY_OLD + grace ----------------------------------------------


def test_old_key_accepted_during_grace(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "new-admin-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY_OLD", "old-admin-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY_OLD_EXPIRES", "2099-01-01T00:00:00Z")

    assert not old_key_expired()
    # L'ancienne clé admin fonctionne PENDANT la grâce (admin ET read).
    assert is_valid_api_key("old-admin-key-0123456789abcdef")
    assert is_valid_read_api_key("old-admin-key-0123456789abcdef")


def test_old_key_rejected_after_expiry(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "new-admin-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY_OLD", "old-admin-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY_OLD_EXPIRES", "2020-01-01T00:00:00Z")

    assert old_key_expired()
    assert not is_valid_api_key("old-admin-key-0123456789abcdef")
    # Fail-closed : date INVALIDE = expirée.
    monkeypatch.setenv("API_KEY_OLD_EXPIRES", "pas-une-date")
    assert old_key_expired()


# --- Fail-closed au démarrage (prod) ---------------------------------------------


def _prod_env(monkeypatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("THINKTUNING_ENV", "prod")


def test_ensure_configured_fails_when_old_expired(monkeypatch) -> None:
    from app.infrastructure.security.api_key import ensure_api_key_configured

    _prod_env(monkeypatch)
    monkeypatch.setenv("API_KEY", "prod-admin-key-0123456789abcdef0123456789")
    monkeypatch.setenv("API_KEY_OLD", "prod-old-key-0123456789abcdef0123456789")
    monkeypatch.setenv("API_KEY_OLD_EXPIRES", "2020-01-01T00:00:00Z")
    with pytest.raises(RuntimeError):
        ensure_api_key_configured()
    # Sans date d'expiration : la rotation est simplement en cours (pas d'erreur).
    monkeypatch.delenv("API_KEY_OLD_EXPIRES", raising=False)
    assert ensure_api_key_configured()


def test_ensure_configured_fails_on_weak_read_key(monkeypatch) -> None:
    from app.infrastructure.security.api_key import ensure_api_key_configured

    _prod_env(monkeypatch)
    monkeypatch.setenv("API_KEY", "prod-admin-key-0123456789abcdef0123456789")
    monkeypatch.setenv("API_KEY_READ", "court")
    with pytest.raises(RuntimeError):
        ensure_api_key_configured()


# --- Dépendances FastAPI -----------------------------------------------------------


def test_require_read_api_key_dependency(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "admin-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY_READ", "read-key-0123456789")

    assert require_read_api_key(x_api_key="read-key-0123456789") is True
    assert require_read_api_key(x_api_key="admin-key-0123456789abcdef") is True
    with pytest.raises(HTTPException) as exc:
        require_read_api_key(x_api_key=None)
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        require_read_api_key(x_api_key="wrong-key")
    assert exc.value.status_code == 401


def test_require_api_key_rejects_read_key(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "admin-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY_READ", "read-key-0123456789")

    assert require_api_key(x_api_key="admin-key-0123456789abcdef") is True
    with pytest.raises(HTTPException) as exc:
        require_api_key(x_api_key="read-key-0123456789")
    assert exc.value.status_code == 401


# --- WebSocket ----------------------------------------------------------------------


class _FakeWs:
    """Faux WebSocket : headers/query_params minimaux."""

    def __init__(self, headers: dict | None = None, token: str | None = None) -> None:
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

        class _Q:
            def __init__(self, token: str | None) -> None:
                self._token = token

            def get(self, key: str) -> str | None:
                return self._token if key == "token" else None

        self.query_params = _Q(token)


def test_ws_header_takes_precedence(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "admin-key-0123456789abcdef")
    ws = _FakeWs(headers={"X-API-Key": "admin-key-0123456789abcdef"})
    assert ws_is_authorized(ws, read_scope=False)


def test_ws_query_fallback(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "admin-key-0123456789abcdef")
    ws = _FakeWs(token="admin-key-0123456789abcdef")
    assert ws_is_authorized(ws, read_scope=False)
    bad = _FakeWs(token="nope")
    assert not ws_is_authorized(bad, read_scope=False)


def test_ws_train_stream_accepts_read_key(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "admin-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY_READ", "read-key-0123456789")
    ws = _FakeWs(token="read-key-0123456789")
    assert ws_is_authorized(ws, read_scope=True)
    # Canal d'ACTION : la clé read ne suffit JAMAIS.
    assert not ws_is_authorized(ws, read_scope=False)
