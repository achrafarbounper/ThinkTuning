# project/tests/test_mcp_first.py
"""Tests « MCP-First » (S7 — v3.0.0, tâche 20 : docs/mcp/IMPLEMENTATION_PLAN.md).

Contrat du sprint S7 « HTTP API legacy + MCP actif » :

    1. ``api/routes/agent.py`` est marqué @deprecated : DeprecationWarning à
       l'import + en-têtes ``Deprecation`` / ``Sunset`` / ``Warning: 299`` ;
    2. feature flag ``MCP_FIRST=true`` : les endpoints mutants (POST/PUT/DELETE)
       de la surface legacy répondent 405 (code ``mcp_first_read_only``), les
       lectures (GET) restent servies — y compris via les délégations v1
       (strangler) qui appellent les mêmes handlers ;
    3. l'approbation humaine (approve/reject) reste écrivable : c'est le canal
       qui débloque les runs MCP ``pending_approval`` (policy APPROVE) ;
    4. le serveur MCP (``POST /mcp/sse``) reste ACTIF sous MCP_FIRST.

Aucun réseau, aucun LLM, aucun modèle : mini-apps FastAPI (même pattern que
``test_mcp_server_basic.py`` / ``test_agent_flow.py``) et fakes en mémoire.
Lance avec : pytest tests/test_mcp_first.py -v
"""

from __future__ import annotations

import importlib
import json
import os

# Config test AVANT tout import de l'application (URL LLM factice : port 9).
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import api.routes.agent as agent_routes  # noqa: E402
from api.routes.v1 import agent as v1_agent_routes  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.infrastructure.mcp.mcp_server_sse import router as mcp_sse_router  # noqa: E402

API_KEY = "test-mcp-first-key"
HEADERS = {"X-API-Key": API_KEY}


# --- Fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch):
    """Pose API_KEY et réinitialise le cache Settings autour de chaque test."""
    monkeypatch.setenv("API_KEY", API_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def mcp_first(monkeypatch):
    """Active le flag MCP_FIRST pour la durée du test (rollback garanti)."""
    monkeypatch.setenv("MCP_FIRST", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("MCP_FIRST", raising=False)
    get_settings.cache_clear()


@pytest.fixture()
def legacy_client() -> TestClient:
    """Mini-app ne montant QUE le router legacy /api/agent."""
    app = FastAPI()
    app.include_router(agent_routes.router)
    return TestClient(app)


@pytest.fixture()
def legacy_and_mcp_client() -> TestClient:
    """HTTP API legacy + MCP actif dans la MÊME app (cible du sprint S7)."""
    app = FastAPI()
    app.include_router(mcp_sse_router)
    app.include_router(agent_routes.router)
    return TestClient(app)


# --- 1. Marquage @deprecated du module legacy ---------------------------------


def test_agent_module_emits_deprecation_warning():
    """L'import du module émet un DeprecationWarning (marquage @deprecated)."""
    with pytest.warns(DeprecationWarning, match="MCP-First"):
        importlib.reload(agent_routes)


def test_legacy_surface_sends_deprecation_headers(legacy_client):
    """Chaque réponse HTTP du router porte Deprecation / Sunset / Warning 299."""
    response = legacy_client.get("/api/agent/status")  # endpoint public
    assert response.status_code == 200
    assert response.headers["Deprecation"] == "true"
    assert response.headers["Sunset"]
    assert "299" in response.headers["Warning"]
    assert "/mcp/sse" in response.headers["Warning"]


# --- 2. Feature flag MCP_FIRST -> read-only -----------------------------------


def test_mcp_first_disabled_by_default(monkeypatch):
    """Sans MCP_FIRST, la surface HTTP reste pleinement écrivable."""
    monkeypatch.delenv("MCP_FIRST", raising=False)
    get_settings.cache_clear()
    assert get_settings().mcp_first is False


def test_mutating_endpoint_stays_writable_without_flag(legacy_client):
    """MCP_FIRST désactivé : POST /tools/run exécute l'outil (comportement inchangé)."""
    response = legacy_client.post(
        "/api/agent/tools/run", json={"tool": "add", "args": {"a": 40, "b": 2}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["result"] == 42


def test_mcp_first_blocks_mutating_legacy_endpoints(legacy_client, mcp_first):
    """MCP_FIRST=true : POST -> 405 mcp_first_read_only, GET -> 200."""
    blocked = legacy_client.post(
        "/api/agent/tools/run", json={"tool": "add", "args": {"a": 1, "b": 1}},
        headers=HEADERS,
    )
    assert blocked.status_code == 405
    detail = blocked.json()["detail"]
    assert detail["code"] == "mcp_first_read_only"
    assert "/mcp/sse" in detail["message"]

    read = legacy_client.get("/api/agent/status")
    assert read.status_code == 200


def test_mcp_first_read_only_covers_v1_delegates(mcp_first):
    """Les délégations v1 héritent du garde (mêmes handlers, strangler)."""
    app = FastAPI()
    app.include_router(v1_agent_routes.router, prefix="/api/v1")
    client = TestClient(app)

    response = client.post(
        "/api/v1/agent/ask/core", json={"prompt": "bonjour"}, headers=HEADERS
    )
    assert response.status_code == 405
    assert response.json()["detail"]["code"] == "mcp_first_read_only"


def test_human_approval_remains_writable_under_mcp_first(
    legacy_client, mcp_first, monkeypatch
):
    """Whitelist human-in-the-loop : approve n'est PAS bloqué (405 sinon).

    Le store est simulé (zéro SQLite) : une demande inconnue -> 404, ce qui
    prouve que la requête a franchi la garde read-only.
    """

    class _FakeApprovalStore:
        def approve(self, _request_id):
            return None

        def reject(self, _request_id):
            return None

        def list(self, _status=None):
            return []

    monkeypatch.setattr(agent_routes, "get_approval_store", _FakeApprovalStore)

    response = legacy_client.post(
        "/api/agent/approvals/unknown-request/approve", headers=HEADERS
    )
    assert response.status_code == 404  # 405 attendu si le garde était appliqué


# --- 3. MCP reste actif sous MCP_FIRST (HTTP API legacy + MCP actif) ----------


def _sse_jsonrpc_response(client: TestClient, message: dict):
    """POST /mcp/sse : JSON-RPC entrant -> événement SSE `message` (JSON-RPC)."""
    response = client.post(
        "/mcp/sse",
        content=json.dumps(message),
        headers={
            "X-Client-Id": "dashboard-mcp",
            "Mcp-Session-Id": "tt-test",
            "X-API-Key": API_KEY,
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    raise AssertionError(f"Aucune donnée SSE dans le flux : {response.text!r}")


def test_mcp_server_stays_active_under_mcp_first(legacy_and_mcp_client, mcp_first):
    """Sous MCP_FIRST : l'HTTP legacy est read-only ET le serveur MCP répond."""
    reply = _sse_jsonrpc_response(
        legacy_and_mcp_client,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert reply["id"] == 1
    assert reply["result"]["protocolVersion"] == "2025-06-18"

    tools = _sse_jsonrpc_response(
        legacy_and_mcp_client,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    names = {tool["name"] for tool in tools["result"]["tools"]}
    # Scope client par défaut read_only : le tool `orchestrate` (mutation,
    # required_scope CONTRIBUTOR) est volontairement filtré — cf. scope
    # enforcer S4. Les tools bootstrap (lecture) sont visibles : le serveur
    # est bien ACTIF.
    assert "mcp_version" in names
    assert "server_info" in names

    blocked = legacy_and_mcp_client.post(
        "/api/agent/tools/run", json={"tool": "add", "args": {"a": 1, "b": 1}},
        headers=HEADERS,
    )
    assert blocked.status_code == 405  # HTTP legacy gelé, MCP vivant
