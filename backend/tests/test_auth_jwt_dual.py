# project/tests/test_auth_jwt_dual.py
"""Tests auth bipolaire (X-API-Key OU Bearer JWT) — câblage v1 (P2 lot 13).

Verrouille la sémantique attendue par le dashboard (login via
``POST /api/v1/auth/token`` puis ``Authorization: Bearer``) :

  - un jeton JWT valide remplace la clé API sur les routes câblées
    ``require_api_key_or_jwt`` / ``require_read_api_key_or_jwt`` ;
  - hiérarchie des rôles = celle des clés API (``admin`` surclasse
    ``read``) : un jeton read est REFUSÉ (403) sur une route d'action, un
    jeton admin ouvre aussi les canaux de lecture ;
  - sans auth / jeton altéré / révoqué -> 401 ; jeton read + action -> 403 ;
  - repli défensif : un Bearer invalide n'exclut PAS une clé API valide ;
  - WebSocket : même hiérarchie via ``ws_is_authorized`` ;
  - e2e sur l'app complète : /auth/token -> Bearer -> 200 en lecture,
    403 sur une action (avant tout effet de bord).

Lance avec : pytest tests/test_auth_jwt_dual.py -v
"""

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.dependencies.auth import (
    require_api_key_or_jwt,
    require_read_api_key_or_jwt,
    ws_is_authorized,
)
from app.domain.tokens import (
    TokenClaims,
    create_access_token,
    verify_access_token,
)

SECRET = "dual-auth-jwt-secret-0123456789abcdef"
ADMIN_KEY = "admin-key-0123456789abcdef"
READ_KEY = "read-key-0123456789"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _probe_client() -> TestClient:
    """Mini-app avec deux routes sondes (lecture / action)."""
    app = FastAPI()

    @app.get("/probe/read")
    def probe_read(_: bool = Depends(require_read_api_key_or_jwt)) -> dict:
        return {"ok": True}

    @app.get("/probe/action")
    def probe_action(_: bool = Depends(require_api_key_or_jwt)) -> dict:
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def probe_client(tmp_path, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("API_KEY", ADMIN_KEY)
    monkeypatch.setenv("API_KEY_READ", READ_KEY)
    from app.infrastructure.security.service_accounts import (
        reset_service_account_store,
    )

    reset_service_account_store(str(tmp_path / "sa.db"))
    return _probe_client()


# ============================================================================
# Matrice REST : jeton JWT sur routes lecture / action
# ============================================================================


def test_read_token_opens_read_route(probe_client) -> None:
    token = create_access_token(subject="u", secret=SECRET, role="read")
    assert probe_client.get("/probe/read", headers=_bearer(token)).status_code == 200


def test_read_token_rejected_on_action_route(probe_client) -> None:
    """Least-privilege : un jeton read ne peut PAS déclencher d'action (403)."""
    token = create_access_token(subject="u", secret=SECRET, role="read")
    resp = probe_client.get("/probe/action", headers=_bearer(token))
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"]


def test_admin_token_opens_action_and_read_routes(probe_client) -> None:
    """Admin surclasse read (même sémantique que la clé API admin)."""
    token = create_access_token(subject="root", secret=SECRET, role="admin")
    assert probe_client.get("/probe/action", headers=_bearer(token)).status_code == 200
    assert probe_client.get("/probe/read", headers=_bearer(token)).status_code == 200


def test_missing_and_invalid_tokens_rejected(probe_client) -> None:
    assert probe_client.get("/probe/read").status_code == 401
    assert probe_client.get("/probe/action").status_code == 401
    bad = probe_client.get("/probe/read", headers=_bearer("not.a.jwt"))
    assert bad.status_code == 401
    forged = create_access_token(
        subject="u", secret="autre-secret-jwt-0123456789abcdef", role="read"
    )
    assert probe_client.get("/probe/read", headers=_bearer(forged)).status_code == 401


def test_api_keys_keep_their_semantics(probe_client) -> None:
    """La clé admin ouvre tout ; la clé read reste hors des routes d'action."""
    admin_h = {"X-API-Key": ADMIN_KEY}
    read_h = {"X-API-Key": READ_KEY}
    assert probe_client.get("/probe/action", headers=admin_h).status_code == 200
    assert probe_client.get("/probe/read", headers=read_h).status_code == 200
    assert probe_client.get("/probe/action", headers=read_h).status_code == 401


def test_invalid_bearer_falls_back_to_valid_api_key(probe_client) -> None:
    """Défensif : un Bearer expiré/invalide n'exclut PAS la voie clé API."""
    from app.domain.tokens import TokenExpiredError

    expired = create_access_token(
        subject="u", secret=SECRET, role="read", ttl_seconds=60, now=1_000_000
    )
    # Prouve l'expiration du jeton (le même vérificateur que la dépendance).
    with pytest.raises(TokenExpiredError):
        verify_access_token(expired, SECRET, now=2_000_000)
    headers = {**_bearer(expired), "X-API-Key": ADMIN_KEY}
    assert probe_client.get("/probe/action", headers=headers).status_code == 200
    # Sans clé API valide -> 401 JWT actionnable.
    alone = probe_client.get("/probe/action", headers=_bearer(expired))
    assert alone.status_code == 401


def test_revoked_token_rejected(probe_client) -> None:
    from app.infrastructure.security.service_accounts import (
        get_service_account_store,
    )

    store = get_service_account_store()
    created = store.create_account(name="bot", role="read")
    issued = store.issue_token(
        client_id=created["id"],
        client_secret=created["client_secret"],
        jwt_secret=SECRET,
    )
    headers = _bearer(issued["token"])
    assert probe_client.get("/probe/read", headers=headers).status_code == 200
    claims = TokenClaims.from_payload(verify_access_token(issued["token"], SECRET))
    assert store.revoke_token(claims)
    assert probe_client.get("/probe/read", headers=headers).status_code == 401


# ============================================================================
# WebSocket : même hiérarchie via ws_is_authorized
# ============================================================================


class _FakeWs:
    """Faux WebSocket : headers/query_params minimaux (cf. test_auth_rbac)."""

    def __init__(self, token: str | None) -> None:
        self.headers: dict[str, str] = {}

        class _Q:
            def __init__(self, token: str | None) -> None:
                self._token = token

            def get(self, key: str) -> str | None:
                return self._token if key == "token" else None

        self.query_params = _Q(token)


def test_ws_jwt_role_hierarchy(probe_client) -> None:
    read_token = create_access_token(subject="u", secret=SECRET, role="read")
    admin_token = create_access_token(subject="root", secret=SECRET, role="admin")
    # Canal de LECTURE : read OU admin.
    assert ws_is_authorized(_FakeWs(read_token), read_scope=True)
    assert ws_is_authorized(_FakeWs(admin_token), read_scope=True)
    # Canal d'ACTION : admin uniquement.
    assert ws_is_authorized(_FakeWs(admin_token), read_scope=False)
    assert not ws_is_authorized(_FakeWs(read_token), read_scope=False)


# ============================================================================
# E2E — app complète : /auth/token -> Bearer sur la surface v1
# ============================================================================


def test_full_app_bearer_flow(tmp_path, monkeypatch) -> None:
    """Flux dashboard : login JWT puis appels v1 (lecture OK, action 403)."""
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("API_KEY", ADMIN_KEY)
    monkeypatch.delenv("API_KEY_OLD", raising=False)
    from api import app  # noqa: E402 (import après env — contrat api.app)
    from app.infrastructure.security.service_accounts import (  # noqa: E402
        get_service_account_store,
        reset_service_account_store,
    )
    from core.session_store import reset_session_store  # noqa: E402

    reset_service_account_store(str(tmp_path / "sa-e2e.db"))
    reset_session_store(str(tmp_path / "sessions-e2e.db"))

    account = get_service_account_store().create_account(name="dash", role="read")
    client = TestClient(app, raise_server_exceptions=False)

    issued = client.post(
        "/api/v1/auth/token",
        json={
            "client_id": account["id"],
            "client_secret": account["client_secret"],
        },
    )
    assert issued.status_code == 200, issued.text
    token = issued.json()["token"]

    # Lecture avec le Bearer : sessions (store isolé -> 200, liste vide).
    sessions = client.get("/api/v1/sessions", headers=_bearer(token))
    assert sessions.status_code == 200, sessions.text
    assert sessions.json() == {"sessions": []}

    # Action sans rôle admin : 403 AVANT tout effet de bord (reload modèle).
    denied = client.post("/api/v1/predict/reload", headers=_bearer(token))
    assert denied.status_code == 403
    assert "admin" in denied.json()["detail"]

    # Sans auth : 401 (message bipolaire actionnable).
    unauth = client.get("/api/v1/sessions")
    assert unauth.status_code == 401
