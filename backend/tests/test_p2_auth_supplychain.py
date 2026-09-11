"""Tests P2 durable â€” lots 13/15 (auth moderne + supply-chain).

Lance avec : pytest tests/test_p2_auth_supplychain.py -v
"""

from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "test-jwt-secret-0123456789abcdef"


# ============================================================================
# Lot 13 â€” tokens JWT courte durÃ©e
# ============================================================================


def test_token_roundtrip_and_claims() -> None:
    from app.domain.tokens import TokenClaims, create_access_token, verify_access_token

    token = create_access_token(subject="ci-bot", secret=SECRET, role="read", now=1_000_000)
    payload = verify_access_token(token, SECRET, now=1_000_060)
    claims = TokenClaims.from_payload(payload)
    assert claims.subject == "ci-bot"
    assert claims.role == "read"
    assert claims.is_read_only
    assert claims.expires_at - claims.issued_at == 900  # TTL par dÃ©faut 15 min


def test_token_expired_rejected() -> None:
    from app.domain.tokens import TokenExpiredError, create_access_token, verify_access_token

    token = create_access_token(subject="ci-bot", secret=SECRET, ttl_seconds=60, now=1_000_000)
    with pytest.raises(TokenExpiredError):
        verify_access_token(token, SECRET, now=1_000_061)


def test_token_tampered_rejected() -> None:
    from app.domain.tokens import TokenInvalidError, create_access_token, verify_access_token

    token = create_access_token(subject="ci-bot", secret=SECRET)
    head, _, sig = token.split(".")
    forged_payload = base64.urlsafe_b64encode(b'{"sub": "attacker"}').decode().rstrip("=")
    with pytest.raises(TokenInvalidError):
        verify_access_token(f"{head}.{forged_payload}.{sig}", SECRET)


def test_token_ttl_clamped_to_max() -> None:
    from app.domain.tokens import MAX_TTL_SECONDS, create_access_token, verify_access_token

    token = create_access_token(subject="ci-bot", secret=SECRET, ttl_seconds=10**9, now=0)
    payload = verify_access_token(token, SECRET, now=1)
    assert payload["exp"] - payload["iat"] == MAX_TTL_SECONDS


# ============================================================================
# Lot 13 â€” service accounts (Ã©mission + rÃ©vocation jti)
# ============================================================================


def _claims_of(token: str):
    from app.domain.tokens import TokenClaims, verify_access_token

    return TokenClaims.from_payload(verify_access_token(token, SECRET))


def test_service_account_issue_and_revoke(tmp_path) -> None:
    from app.infrastructure.security.service_accounts import reset_service_account_store

    store = reset_service_account_store(str(tmp_path / "sa.db"))
    created = store.create_account(name="ci-bot", role="read")
    assert created["client_secret"]  # secret en clair UNE seule fois

    issued = store.issue_token(
        client_id=created["id"],
        client_secret=created["client_secret"],
        jwt_secret=SECRET,
    )
    claims = _claims_of(issued["token"])
    assert claims.role == "read"
    assert not store.is_token_revoked(claims.jti)

    assert store.revoke_token(claims)
    assert store.is_token_revoked(claims.jti)
    # RÃ©vocation de l'account : plus aucune Ã©mission possible.
    assert store.revoke_account(created["id"])
    with pytest.raises(PermissionError):
        store.issue_token(
            client_id=created["id"],
            client_secret=created["client_secret"],
            jwt_secret=SECRET,
        )


def test_service_account_wrong_secret_denied(tmp_path) -> None:
    from app.infrastructure.security.service_accounts import reset_service_account_store

    store = reset_service_account_store(str(tmp_path / "sa.db"))
    created = store.create_account(name="ci-bot", role="read")
    with pytest.raises(PermissionError):
        store.issue_token(
            client_id=created["id"],
            client_secret="wrong-secret",
            jwt_secret=SECRET,
        )
# ============================================================================
# Lot 13 â€” vault (env + chaÃ®ne fail-over)
# ============================================================================


def test_vault_env_source_reads_env(monkeypatch) -> None:
    from app.infrastructure.security.vault import build_secret_source

    monkeypatch.setenv("VAULT_BACKEND", "env")
    monkeypatch.setenv("MY_SECRET_TEST", "valeur")
    assert build_secret_source().get_secret("MY_SECRET_TEST") == "valeur"


def test_vault_chain_fails_over_to_env(monkeypatch) -> None:
    from app.infrastructure.security.vault import build_secret_source

    monkeypatch.setenv("VAULT_BACKEND", "infisical,env")
    monkeypatch.setenv("MY_SECRET_TEST", "valeur-env")
    # Infisical non configurÃ© (pas de token) : la chaÃ®ne retombe sur env.
    assert build_secret_source().get_secret("MY_SECRET_TEST") == "valeur-env"


def test_vault_unknown_backend_rejected() -> None:
    from app.infrastructure.security.vault import build_secret_source

    with pytest.raises(ValueError):
        build_secret_source("unknown-backend")


# ============================================================================
# Lot 13 â€” endpoint /api/v1/auth/token (client credentials)
# ============================================================================


@pytest.fixture()
def auth_client(tmp_path):
    from api.routes.v1.auth import router
    from app.infrastructure.security.service_accounts import reset_service_account_store

    reset_service_account_store(str(tmp_path / "sa.db"))
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def test_auth_token_endpoint_flow(auth_client) -> None:
    from app.infrastructure.security.service_accounts import get_service_account_store

    created = get_service_account_store().create_account(name="dashboard-ci", role="read")
    resp = auth_client.post(
        "/api/v1/auth/token",
        json={"client_id": created["id"], "client_secret": created["client_secret"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 900
    # /verify accepte le jeton et renvoie les claims.
    verify = auth_client.get(
        "/api/v1/auth/verify", headers={"Authorization": f"Bearer {body['token']}"}
    )
    assert verify.status_code == 200
    assert verify.json()["role"] == "read"
    # Secret faux -> 401.
    bad = auth_client.post(
        "/api/v1/auth/token",
        json={"client_id": created["id"], "client_secret": "nope"},
    )
    assert bad.status_code == 401


# ============================================================================
# Lot 15 â€” signature des modÃ¨les
# ============================================================================


def _mini_model_dir(tmp_path) -> str:
    d = tmp_path / "model-v1"
    d.mkdir()
    (d / "config.json").write_text('{"architectures": ["X"]}', encoding="utf-8")
    (d / "model.safetensors").write_bytes(b"weights")
    return str(d)


def test_model_signature_write_and_verify(tmp_path) -> None:
    from core.model_signing import verify_model_signature, write_signature_manifest

    d = _mini_model_dir(tmp_path)
    write_signature_manifest(d)
    report = verify_model_signature(d)
    assert report["ok"] and report["checked"] and report["files"] == 2


def test_model_signature_tamper_detected(tmp_path) -> None:
    from core.model_signing import (
        ModelSignatureError,
        verify_model_signature,
        write_signature_manifest,
    )

    d = _mini_model_dir(tmp_path)
    write_signature_manifest(d)
    (tmp_path / "model-v1" / "model.safetensors").write_bytes(b"TAMPERED")
    with pytest.raises(ModelSignatureError):
        verify_model_signature(d)


def test_model_signature_unsigned_file_detected(tmp_path) -> None:
    from core.model_signing import (
        ModelSignatureError,
        verify_model_signature,
        write_signature_manifest,
    )

    d = _mini_model_dir(tmp_path)
    write_signature_manifest(d)
    (tmp_path / "model-v1" / "evil.py").write_text("import os", encoding="utf-8")
    with pytest.raises(ModelSignatureError):
        verify_model_signature(d)


def test_model_signature_required_policy(tmp_path, monkeypatch) -> None:
    from core.model_signing import ModelSignatureError, verify_model_signature

    monkeypatch.setenv("MODEL_SIGNING_REQUIRED", "1")
    with pytest.raises(ModelSignatureError):
        verify_model_signature(_mini_model_dir(tmp_path))
