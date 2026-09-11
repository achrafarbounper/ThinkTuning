# project/tests/test_mcp_audit.py

"""Tests de l'audit trail MCP (S4, tâche 12 — docs/mcp/IMPLEMENTATION_PLAN.md).

Contrat vérifié (docs/mcp/MCP_SECURITY.md) :
    chaque appel MCP d'ACTION est journalisé dans la table ``agent_audit``
    (``core/audit_store.py``) avec ``subject`` = ``client_id`` :

        - tools/call (hors orchestrate) → ``mcp_tool_call`` ;
        - tools/call sur ``orchestrate`` → ``mcp_orchestrate`` ;
        - resources/read                → ``mcp_resource_read`` ;
        - prompts/get                   → ``mcp_prompt_get`` ;
        - sampling/create               → ``mcp_sampling``.

Couverture :
    1. chaque appel est audité — y compris les ÉCHECS (``is_error: true``) ;
    2. les méthodes de catalogue/handshake (initialize, ping, tools/list…)
       ne produisent AUCUNE entrée ;
    3. l'audit est NON BLOQUANT : un hook qui lève n'altère jamais la réponse ;
    4. interrupteur ``MCP_AUDIT_ENABLED=false`` → aucune écriture ;
    5. ``AuditStore.mcp_metrics()`` : volume, répartition, error rate ;
    6. dashboard ``/api/v1/mcp/metrics`` : call volume, error rate, revoked.

Aucun import lourd (ni torch, ni transformers) — le socle MCP reste léger.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.entities.mcp import MCPScopeRole, MCPVersion
from app.infrastructure.mcp.mcp_audit import mcp_audit_enabled
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.protocol import ErrorCode
from core.audit_store import (
    ACT_MCP_ORCHESTRATE,
    ACT_MCP_PROMPT_GET,
    ACT_MCP_RESOURCE_READ,
    ACT_MCP_SAMPLING,
    ACT_MCP_TOOL_CALL,
    MCP_ACTIONS,
    reset_audit_store,
)

# Clé API fixée pour les tests dashboard (lue à chaque appel par auth.py).
_API_KEY = "test-mcp-audit-key"


@pytest.fixture(autouse=True)
def _audit_env(tmp_path, monkeypatch):
    """Base d'audit ISOLÉE par test + clé API déterministe.

    ``reset_audit_store`` recharge le singleton depuis l'env var — l'isolation
    ne dépend donc jamais de l'ordre d'exécution des tests.
    """
    monkeypatch.setenv("AGENT_AUDIT_PATH", str(tmp_path / "agent_audit.db"))
    monkeypatch.setenv("API_KEY", _API_KEY)
    reset_audit_store()
    yield
    reset_audit_store()  # le store suivant repartira de SON chemin


def _audit_rows() -> list[dict]:
    """Toutes les entrées d'audit MCP de la base de test (ordre chronologique)."""
    store = reset_audit_store()
    rows = [
        item
        for item in store.query(limit=200)["items"]
        if item["action"] in MCP_ACTIONS
    ]
    rows.reverse()  # query est DESC ; on raisonne en ordre d'appel
    return rows


def _server_with_audit(scope: MCPScopeRole = MCPScopeRole.READ_ONLY, **kw):
    """Serveur MCP branché sur le VRAI audit (``mcp_audit.audit_mcp_call``)."""
    from app.infrastructure.mcp.mcp_audit import audit_mcp_call

    return build_mcp_server(
        scope=scope, version=MCPVersion(major=0, minor=1, patch=0),
        audit=audit_mcp_call, **kw,
    )


def _call(server, method: str, params: dict | None = None, request_id: int = 1):
    raw = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                      "params": params or {}})
    return json.loads(server.handle_text(raw, client_id="client-alpha"))


# ============================================================================
# 1. Chaque appel est audité
# ============================================================================


def test_tools_call_is_audited_with_client_id_and_details():
    """tools/call → ACT_MCP_TOOL_CALL, subject=client_id, tool + run_id tracés."""
    server = _server_with_audit()
    reply = _call(server, "tools/call",
                  {"name": "mcp_version", "arguments": {}}, request_id=42)
    assert reply["result"]["isError"] is False

    rows = _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == ACT_MCP_TOOL_CALL
    assert row["subject"] == "client-alpha"
    assert row["actor"] == "mcp"
    assert row["run_id"] == "42"
    assert row["detail"]["tool"] == "mcp_version"
    assert row["detail"]["is_error"] is False
    assert row["detail"]["scope"] == "read_only"
    assert row["detail"]["method"] == "tools/call"


def test_orchestrate_call_is_audited_as_orchestrate():
    """tools/call sur ``orchestrate`` → ACT_MCP_ORCHESTRATE (action dédiée)."""
    server = _server_with_audit()
    _call(server, "tools/call", {"name": "orchestrate", "arguments": {}})

    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["action"] == ACT_MCP_ORCHESTRATE
    assert rows[0]["detail"]["tool"] == "orchestrate"


def test_failed_tool_call_is_audited_with_is_error():
    """Un tool inconnu (échec) est AUSSI audité — l'audit porte sur l'APPEL."""
    server = _server_with_audit()
    reply = _call(server, "tools/call", {"name": "ghost", "arguments": {}})
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS

    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["detail"]["is_error"] is True


def test_resource_read_is_audited_with_uri():
    """resources/read → ACT_MCP_RESOURCE_READ, ``uri`` dans le détail."""
    server = _server_with_audit()
    _call(server, "resources/read", {"uri": "thinktuning://jobs"})

    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["action"] == ACT_MCP_RESOURCE_READ
    assert rows[0]["detail"]["uri"] == "thinktuning://jobs"

def test_prompt_get_is_audited_with_prompt_name():
    """prompts/get → ACT_MCP_PROMPT_GET, nom du prompt dans le détail."""
    server = _server_with_audit()
    _call(server, "prompts/get",
          {"name": "analyze-sentiment", "arguments": {"text": "super produit"}})

    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["action"] == ACT_MCP_PROMPT_GET
    assert rows[0]["detail"]["prompt"] == "analyze-sentiment"
    assert rows[0]["detail"]["arguments"] == {"text": "super produit"}


def test_sampling_create_is_audited_even_when_rejected():
    """sampling/create → ACT_MCP_SAMPLING : l'appel fail-closed reste tracé."""
    server = _server_with_audit()
    reply = _call(server, "sampling/create", {"messages": []})
    # v2.0.0 : sans SamplingPort, le rejet est INTERNAL_ERROR (-32603)
    assert reply["error"]["code"] == ErrorCode.INTERNAL_ERROR

    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["action"] == ACT_MCP_SAMPLING
    assert rows[0]["detail"]["is_error"] is True


def test_catalogue_and_handshake_methods_are_not_audited():
    """initialize / ping / lists / notifications → AUCUNE entrée d'audit."""
    server = _server_with_audit()
    _call(server, "initialize", {})
    _call(server, "ping")
    _call(server, "tools/list")
    _call(server, "resources/list")
    _call(server, "prompts/list")
    assert _audit_rows() == []


def test_all_mcp_actions_are_declared():
    """Le contrat tâche 12 : les 5 actions normalisées sont déclarées."""
    assert MCP_ACTIONS == (
        "mcp_tool_call",
        "mcp_resource_read",
        "mcp_prompt_get",
        "mcp_sampling",
        "mcp_orchestrate",
    )
    assert {
        ACT_MCP_TOOL_CALL, ACT_MCP_RESOURCE_READ, ACT_MCP_PROMPT_GET,
        ACT_MCP_SAMPLING, ACT_MCP_ORCHESTRATE,
    } == set(MCP_ACTIONS)


# ============================================================================
# 2. Non-bloquant & interrupteur
# ============================================================================


def test_audit_hook_failure_never_breaks_the_response():
    """Un hook d'audit qui lève → réponse MCP inchangée (non bloquant)."""

    def _boom(*args, **kwargs):
        raise RuntimeError("audit down")

    server = build_mcp_server(
        version=MCPVersion(major=0, minor=1, patch=0), audit=_boom,
    )
    reply = _call(server, "tools/call", {"name": "mcp_version", "arguments": {}})
    assert reply["result"]["isError"] is False


def test_audit_disabled_writes_nothing(monkeypatch):
    """MCP_AUDIT_ENABLED=false → audit_mcp_call n'écrit RIEN (rollback explicite)."""
    import app.infrastructure.mcp.mcp_audit as mcp_audit

    monkeypatch.setattr(mcp_audit, "_MCP_AUDIT_ENABLED", False)
    assert mcp_audit_enabled() is False

    server = _server_with_audit()
    _call(server, "tools/call", {"name": "mcp_version", "arguments": {}})
    assert _audit_rows() == []


# ============================================================================
# 3. Métriques agrégées (AuditStore.mcp_metrics)
# ============================================================================


def test_mcp_metrics_aggregates_volume_and_error_rate():
    """3 appels dont 1 erreur → total=3, errors=1, error_rate≈0.3333."""
    server = _server_with_audit()
    _call(server, "tools/call", {"name": "mcp_version", "arguments": {}})
    _call(server, "tools/call", {"name": "ghost", "arguments": {}})   # erreur
    _call(server, "resources/read", {"uri": "thinktuning://jobs"})

    metrics = reset_audit_store().mcp_metrics()
    assert metrics["total"] == 3
    assert metrics["by_action"][ACT_MCP_TOOL_CALL] == 2
    assert metrics["by_action"][ACT_MCP_RESOURCE_READ] == 1
    assert metrics["by_action"][ACT_MCP_SAMPLING] == 0  # clé toujours présente
    assert metrics["errors"] == 1
    assert metrics["error_rate"] == round(1 / 3, 4)


def test_mcp_metrics_on_empty_store_is_a_stable_molecule():
    """Aucun appel → molécule complète (jamais de clé manquante)."""
    metrics = reset_audit_store().mcp_metrics()
    assert metrics == {
        "total": 0,
        "by_action": {action: 0 for action in MCP_ACTIONS},
        "errors": 0,
        "error_rate": 0.0,
    }


# ============================================================================
# 4. Dashboard interne (/api/v1/mcp/metrics)
# ============================================================================


def _dashboard_app() -> TestClient:
    """Mini-app ne montant QUE le router v1 MCP (tests isolés)."""
    from api.routes.v1 import mcp as mcp_v1

    app = FastAPI()
    app.include_router(mcp_v1.router)
    return TestClient(app)


def _register_dashboard_clients(tmp_path, monkeypatch) -> None:
    """Registre clients isolé : 1 actif + 1 révoqué (avec usage enregistré).

    Le dashboard lit le singleton ``get_mcp_client_store()`` (MongoDB) : c'est
    DANS ce store qu'on enregistre les clients — l'isolation par test vient du
    provider mock de conftest (plus de fichier ``MCP_CLIENT_STORE_PATH``)."""
    from app.domain.ports.mcp_ports import MCPSecurityScope
    from core.mcp_client_store import get_mcp_client_store

    store = get_mcp_client_store()
    store.register(
        "cli-active", "secret-a",
        MCPSecurityScope(
            client_id="cli-active", tenant_id="default", role="read_only",
            visible_tools=[], visible_resources=[], visible_prompts=[],
            sampling_enabled=False, rate_limit_per_minute=60,
            destructive_quota=5, revoked=False, revoked_at=None,
            revoked_reason="",
        ),
    )
    store.register(
        "cli-revoked", "secret-b",
        MCPSecurityScope(
            client_id="cli-revoked", tenant_id="default", role="contributor",
            visible_tools=[], visible_resources=[], visible_prompts=[],
            sampling_enabled=False, rate_limit_per_minute=60,
            destructive_quota=5, revoked=False, revoked_at=None,
            revoked_reason="",
        ),
    )
    store.record_call("cli-active", "mcp_version", success=True)
    store.record_call("cli-active", "mcp_version", success=False)
    store.revoke("cli-revoked", reason="compromised")


def test_dashboard_requires_api_key():
    """Sans X-API-Key → 401 (surface d'administration interne)."""
    response = _dashboard_app().get("/mcp/metrics")
    assert response.status_code == 401


def test_dashboard_metrics_volume_error_rate_and_revoked(tmp_path, monkeypatch):
    """Dashboard : call volume, error rate MCP et clients révoqués."""
    _register_dashboard_clients(tmp_path, monkeypatch)

    server = _server_with_audit()
    _call(server, "tools/call", {"name": "mcp_version", "arguments": {}})
    _call(server, "tools/call", {"name": "ghost", "arguments": {}})   # erreur
    _call(server, "prompts/get", {"name": "analyze-sentiment",
                                  "arguments": {"text": "ok"}})

    response = _dashboard_app().get(
        "/mcp/metrics", headers={"X-API-Key": _API_KEY}
    )
    assert response.status_code == 200
    payload = response.json()

    # Call volume + error rate (source : table agent_audit).
    assert payload["call_volume"]["total"] == 3
    assert payload["call_volume"]["by_action"][ACT_MCP_TOOL_CALL] == 2
    assert payload["call_volume"]["by_action"][ACT_MCP_PROMPT_GET] == 1
    assert payload["call_volume"]["errors"] == 1
    assert payload["error_rate"] == round(1 / 3, 4)
    assert "scrape_at_ms" in payload

    # Revoked clients (source : MCPClientStore).
    assert payload["clients"] == {"total": 2, "active": 1, "revoked": 1}
    by_id = {c["client_id"]: c for c in payload["clients_detail"]}
    assert by_id["cli-revoked"]["revoked"] is True
    assert by_id["cli-revoked"]["revoked_reason"] == "compromised"
    assert by_id["cli-active"]["error_rate"] == 0.5  # 1 erreur / 2 appels
    # Tri dashboard : volume décroissant (clients les plus actifs d'abord).
    counts = [c["call_count"] for c in payload["clients_detail"]]
    assert counts == sorted(counts, reverse=True)

