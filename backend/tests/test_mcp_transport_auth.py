# project/tests/test_mcp_transport_auth.py

"""Auth transport du serveur MCP (P5) — ``POST /mcp/sse``.

La surface MCP exécute des outils RÉELS : sans garde, ``MCP_FIRST=true``
gèle l'HTTP legacy mais laisse un canal d'exécution ouvert. Le transport
exige la MÊME clé API que la surface REST (``X-API-Key``, repli de
développement, comparaison à temps constant — module partagé
``app/infrastructure/security/api_key.py``), vérifiée AVANT toute lecture
du corps (fail-closed).

Rollback explicite : ``MCP_AUTH_REQUIRED=false`` (env, lu à l'appel).
Lance : pytest tests/test_mcp_transport_auth.py -v
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.infrastructure.mcp import mcp_server_sse
from app.infrastructure.mcp.mcp_server_sse import router as mcp_sse_router

API_KEY = "test-mcp-transport-key"
AUTH = {"X-API-Key": API_KEY}


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch):
    """Clé déterministe pendant chaque test (la clé est lue à l'appel)."""
    monkeypatch.setenv("API_KEY", API_KEY)
    yield


@pytest.fixture()
def client() -> TestClient:
    """Mini-app ne montant QUE le router MCP (isolée et rapide)."""
    test_app = FastAPI()
    test_app.include_router(mcp_sse_router)
    return TestClient(test_app)


def _ping() -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})


def test_missing_key_is_401(client):
    """Sans ``X-API-Key`` → 401, enveloppe v1 (code ``unauthorized``)."""
    response = client.post("/mcp/sse", content=_ping())
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert "X-API-Key" in body["error"]["message"]


def test_wrong_key_is_401(client):
    """Clé invalide → 401 (comparaison à temps constant, jamais 200)."""
    response = client.post("/mcp/sse", content=_ping(), headers={"X-API-Key": "wrong"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_valid_key_passes(client):
    """Bonne clé → flux SSE avec la réponse JSON-RPC (ping)."""
    response = client.post("/mcp/sse", content=_ping(), headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "Mcp-Session-Id" in response.headers
    assert "event: message" in response.text
    assert '"result"' in response.text


def test_auth_checked_before_body_parsing(client):
    """Fail-closed : un corps invalide sans clé → 401 (pas une Parse error)."""
    response = client.post("/mcp/sse", content="not-json")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_dev_fallback_key_accepted_when_api_key_unset(client, monkeypatch):
    """``API_KEY`` absente → la clé de dev publique est acceptée (parité REST)."""
    monkeypatch.delenv("API_KEY", raising=False)
    response = client.post(
        "/mcp/sse", content=_ping(), headers={"X-API-Key": "dev-local-api-key"}
    )
    assert response.status_code == 200


def test_flag_disabled_allows_unauthenticated(client, monkeypatch):
    """Rollback explicite ``MCP_AUTH_REQUIRED=0`` → accès sans clé."""
    monkeypatch.setenv("MCP_AUTH_REQUIRED", "0")
    response = client.post("/mcp/sse", content=_ping())
    assert response.status_code == 200


def test_flag_read_from_settings(monkeypatch):
    """Sans env, le défaut vient de ``Settings.mcp_auth_required``."""
    monkeypatch.delenv("MCP_AUTH_REQUIRED", raising=False)

    class _Settings:
        mcp_auth_required = False

    monkeypatch.setattr(mcp_server_sse, "get_settings", lambda: _Settings())
    test_app = FastAPI()
    test_app.include_router(mcp_sse_router)
    client = TestClient(test_app)
    response = client.post("/mcp/sse", content=_ping())
    assert response.status_code == 200


def test_disabled_server_short_circuits_before_auth(client, monkeypatch):
    """Ordre des gardes : ``MCP_SERVER_ENABLED=false`` → 503 AVANT le 401."""
    monkeypatch.setattr(mcp_server_sse, "mcp_server_enabled", lambda: False)
    response = client.post("/mcp/sse", content=_ping())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "mcp_disabled"
