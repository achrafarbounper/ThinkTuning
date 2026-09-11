# project/tests/test_api_auth_register.py
"""Tests de l'inscription publique — POST /api/v1/auth/register (SCRUM-138).

Couvre : création de compte (201) + connexion immédiate par email via
``/auth/token``, doublon d'email (409), validation (422), CGU (400),
interrupteur d'activation (403), et les invariants du store (email normalisé,
secret jamais stocké en clair).

Lance avec : pytest tests/test_api_auth_register.py -v
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

VALID_PAYLOAD = {
    "email": "nouvel@utilisateur.app",
    "password": "mot-de-passe-tres-long",
    "accept_terms": True,
}


@pytest.fixture()
def register_client(tmp_path, monkeypatch):
    from api.routes.v1.auth import router
    from app.infrastructure.security.service_accounts import reset_service_account_store

    monkeypatch.setenv("SERVICE_ACCOUNTS_PATH", str(tmp_path / "sa.db"))
    reset_service_account_store()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def _token(client, client_id: str, secret: str):
    """Échange client credentials (helper de connexion par email)."""
    return client.post(
        "/api/v1/auth/token",
        json={"client_id": client_id, "client_secret": secret},
    )


def test_register_creates_account_and_allows_login_by_email(register_client) -> None:
    resp = register_client.post("/api/v1/auth/register", json=VALID_PAYLOAD)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "nouvel@utilisateur.app"
    assert body["role"] == "read"  # least-privilege par défaut
    assert body["id"] and body["message"]

    # Le compte inscrit se connecte IMMÉDIATEMENT avec email + mot de passe.
    token = _token(register_client, "nouvel@utilisateur.app", VALID_PAYLOAD["password"])
    assert token.status_code == 200, token.text
    assert token.json()["role"] == "read"

    # Secret faux -> 401 (aucune fuite d'énumération).
    bad = _token(register_client, "nouvel@utilisateur.app", "mauvais-secret")
    assert bad.status_code == 401


def test_register_accept_terms_obligatoire(register_client) -> None:
    payload = {**VALID_PAYLOAD, "accept_terms": False}
    resp = register_client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 400
    assert "conditions" in resp.json()["detail"].lower()


def test_register_email_deja_pris(register_client) -> None:
    assert register_client.post("/api/v1/auth/register", json=VALID_PAYLOAD).status_code == 201
    dup = register_client.post("/api/v1/auth/register", json=VALID_PAYLOAD)
    assert dup.status_code == 409
    assert "déjà" in dup.json()["detail"]


def test_register_normalise_l_email_majuscule(register_client) -> None:
    payload = {**VALID_PAYLOAD, "email": "  Nouvel@Utilisateur.APP  "}
    resp = register_client.post("/api/v1/auth/register", json=payload)
    assert resp.status_code == 201, resp.text
    assert resp.json()["email"] == "nouvel@utilisateur.app"
    # La normalisation garantit l'unicité : la version en minuscules est prise.
    dup = register_client.post("/api/v1/auth/register", json=VALID_PAYLOAD)
    assert dup.status_code == 409


def test_register_rejette_email_invalide(register_client) -> None:
    resp = register_client.post(
        "/api/v1/auth/register", json={**VALID_PAYLOAD, "email": "pas-un-email"}
    )
    assert resp.status_code == 422


def test_register_rejette_mot_de_passe_trop_court(register_client) -> None:
    resp = register_client.post(
        "/api/v1/auth/register", json={**VALID_PAYLOAD, "password": "court"}
    )
    assert resp.status_code == 422


def test_register_desactive_par_env(register_client, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_REGISTRATION_ENABLED", "0")
    resp = register_client.post("/api/v1/auth/register", json=VALID_PAYLOAD)
    assert resp.status_code == 403
    assert "désactivée" in resp.json()["detail"]


def test_store_register_hashes_secret_jamais_en_clair(tmp_path) -> None:
    from app.infrastructure.security.service_accounts import (
        reset_service_account_store,
    )

    db = str(tmp_path / "sa.db")
    store = reset_service_account_store(db)
    created = store.register_account(email="user@example.com", password="s3cret-tres-long")
    assert created["id"] and created["role"] == "read"

    rows = store._get_by_client_id(created["id"])
    assert rows is not None
    assert rows["secret_hash"] != "s3cret-tres-long"  # hash stocké, jamais le clair
    assert "s3cret-tres-long" not in open(db, "rb").read().decode("latin1")

    # Le mot de passe (secret choisi par l'utilisateur) sert à s'authentifier.
    issued = store.issue_token(
        client_id="user@example.com",
        client_secret="s3cret-tres-long",
        jwt_secret="test-jwt-secret-0123456789",
    )
    assert issued["role"] == "read"


def test_store_register_invalide(tmp_path) -> None:
    from app.infrastructure.security.service_accounts import (
        EmailAlreadyTakenError,
        RegistrationError,
        reset_service_account_store,
    )

    store = reset_service_account_store(str(tmp_path / "sa.db"))
    with pytest.raises(RegistrationError):
        store.register_account(email="pas-un-email", password="s3cret-tres-long")
    with pytest.raises(RegistrationError):
        store.register_account(email="a@b.fr", password="court")

    store.register_account(email="a@b.fr", password="s3cret-tres-long")
    with pytest.raises(EmailAlreadyTakenError):
        store.register_account(email="a@b.fr", password="autre-secret")
