# project/tests/test_agent_websocket.py
"""Tests Phase E : WebSocket bidirectionnel + journal d'audit (API)."""

import os

os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault(
    "AGENT_AUDIT_PATH",
    os.path.join(os.path.dirname(__file__), "..", ".agent_tmp", "test-audit-ws.db"),
)
os.environ.setdefault(
    "AGENT_APPROVAL_PATH",
    os.path.join(os.path.dirname(__file__), "..", ".agent_tmp", "test-approvals-ws.db"),
)

import pytest
from fastapi.testclient import TestClient

from api import app as api_app  # noqa: E402
from core.approval_store import reset_approval_store
from core.audit_store import get_audit_store, reset_audit_store

HEADERS = {"X-API-Key": "test-key"}
TOKEN = "test-key"


@pytest.fixture(autouse=True)
def _isolate():
    reset_approval_store()
    reset_audit_store()
    yield


def _ws_url(path: str) -> str:
    return f"/api/agent{path}?token={TOKEN}"


# --- Garde du canal WS --------------------------------------------------------------


def test_ws_closed_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_WEBSOCKET", raising=False)
    client = TestClient(api_app)
    with pytest.raises(Exception):
        with client.websocket_connect(_ws_url("/ws")):
            pass


def test_ws_rejects_bad_token(monkeypatch):
    monkeypatch.setenv("AGENT_WEBSOCKET", "1")
    client = TestClient(api_app)
    with pytest.raises(Exception):
        with client.websocket_connect("/api/agent/ws?token=WRONG"):
            pass


# --- Flux WS fonctionnel (LLM scripte via monkeypatch) -----------------------------


def test_ws_ping_pong_and_protocol_errors(monkeypatch):
    monkeypatch.setenv("AGENT_WEBSOCKET", "1")
    client = TestClient(api_app)
    with client.websocket_connect(_ws_url("/ws")) as ws:
        hello = ws.receive_json()
        assert hello["event"] == "hello"
        ws.send_json({"action": "ping"})
        assert ws.receive_json() == {"event": "pong"}
        ws.send_json({"action": "nope"})
        err = ws.receive_json()
        assert err["event"] == "error" and "nope" in err["detail"]
        ws.send_text("pas du json")
        err2 = ws.receive_json()
        assert err2["event"] == "error"


def test_ws_ask_streams_then_final(monkeypatch):
    monkeypatch.setenv("AGENT_WEBSOCKET", "1")

    # Noyau v2 (défaut) : le noyau scripté publie son cycle de vie sur le bus
    # par-run injecté ; le worker WS traduit vers le contrat WS historique.
    from app.agent.core import AgentRunResult, RunStatus

    class FakeCore:
        def __init__(self, *args, **kwargs):
            self._event_bus = kwargs.get("event_bus")

        def run(self, intent, history=None):
            if self._event_bus is not None:
                self._event_bus.emit("agent.thinking", chunk="Je reflechis...")
                self._event_bus.emit("agent.tool_start", tool="add", args={})
            return AgentRunResult(answer="Le total est 42.",
                                  status=RunStatus.COMPLETED,
                                  rounds_used=1, tool_calls_used=1)

    monkeypatch.setattr(
        "api.routes.agent.build_agent_core", lambda *a, **kw: FakeCore(*a, **kw)
    )
    client = TestClient(api_app)
    with client.websocket_connect(_ws_url("/ws")) as ws:
        assert ws.receive_json()["event"] == "hello"
        ws.send_json({
            "action": "ask", "prompt": "Additionne 12+30",
            "enable_thinking": True,
        })
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev["event"])
            if ev["event"] in ("final", "error"):
                break
    assert events[0] == "thinking"
    assert "tool_start" in events
    assert "delta" in events
    assert events[-1] == "final"


def test_ws_ask_error_yields_error_final(monkeypatch):
    monkeypatch.setenv("AGENT_WEBSOCKET", "1")

    def boom_build(*args, **kwargs):
        class BoomCore:
            def run(self, intent, history=None):
                raise ValueError("explosion simulee")

        return BoomCore()

    monkeypatch.setattr("api.routes.agent.build_agent_core", boom_build)
    client = TestClient(api_app)
    with client.websocket_connect(_ws_url("/ws")) as ws:
        ws.receive_json()
        ws.send_json({"action": "ask", "prompt": "x"})
        while True:
            ev = ws.receive_json()
            if ev["event"] in ("final", "error"):
                break
    assert ev["event"] == "final" and ev["status"] == "error"
    assert "explosion" in ev["detail"]


# --- GET /api/agent/audit (journal de conformite) -----------------------------------


def test_audit_endpoint_requires_auth():
    client = TestClient(api_app)
    r = client.get("/api/agent/audit")
    assert r.status_code == 401


def test_audit_endpoint_lists_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_AUDIT_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setenv("AGENT_AUDIT", "1")
    reset_audit_store()
    # Actions uniques : d'eventuels logs d'autres tests ne perturbent pas.
    get_audit_store().log(
        actor="tester", action="ws_audit_run", subject="ask",
        detail={"status": "started"},
    )
    get_audit_store().log(
        actor="tester", action="ws_audit_tool", subject="add",
        detail={"args": {"a": 1}},
    )
    client = TestClient(api_app)
    r = client.get("/api/agent/audit?action=ws_audit_run", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "ws_audit_run"
    assert body["items"][0]["actor"] == "tester"
    r2 = client.get("/api/agent/audit?action=ws_audit_tool", headers=HEADERS)
    assert r2.json()["total"] == 1
