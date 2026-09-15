# project/tests/test_mcp_flow.py

"""Tests du traçage Flow Map MCP (plan Flow Map MCP — É3 : hook ``MCPServer``).

Contrat vérifié (``app/infrastructure/mcp/mcp_flow.py``) : chaque appel MCP
d'ACTION crée une session du journal « Agent Flow Map » (``app/infrastructure/persistence/flow_store``,
collection ``agent_flows``) à côté des sessions ``agent.*`` / ``core.*`` :

    - ``tools/call orchestrate`` → session RICHE ``source="mcp"`` :
        ``mcp.orchestrate.start`` → (événements du run) → ``mcp.done`` /
        ``mcp.error`` ; statut final mappé depuis le payload JSON du tool
        (``completed`` / ``awaiting_approval`` / ``error``) ;
    - ``tools/call`` (hors orchestrate) → MINI-session ``source="mcp"`` :
        ``mcp.tool`` (tool_start + tool_result) ;
    - ``resources/read`` / ``prompts/get`` / ``sampling/create`` →
        MINI-session ``source="mcp"`` : ``mcp.call`` + ``mcp.result``.

Couverture :
    1. chaque méthode d'action crée une session Flow Map (orchestrate RICHE
       + actions simples en mini-session) ;
    2. les méthodes de catalogue/handshake (initialize, ping, tools/list…)
       ne produisent AUCUNE session (même policy que l'audit) ;
    3. le traçage est NON BLOQUANT : une écriture qui échoue n'altère jamais
       la réponse MCP ;
    4. interrupteur ``MCP_FLOW_ENABLED=false`` → aucune écriture ;
    5. le statut de session reflète le run orchestrate (completed /
       awaiting_approval / error).

Aucun import lourd (ni torch, ni transformers) — le socle MCP reste léger.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.core import AgentRunResult, RunStatus
from app.domain.entities.mcp import MCPScopeRole, MCPTool, MCPVersion
from app.infrastructure.mcp import mcp_flow
from app.infrastructure.mcp.mcp_flow import mcp_flow_enabled
from app.infrastructure.mcp.mcp_server import ToolError
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.protocol import empty_input_schema
from app.infrastructure.persistence import flow_store as fs


@pytest.fixture(autouse=True)
def _flow_env(monkeypatch):
    """Store de flux ISOLÉ par test + interrupteur actif par défaut.

    ``get_flow_store()`` résout le singleton partagé (MongoFlowStore sur le
    provider mongomock des tests, cf. tests/conftest.py) : on le vide et on
    repose l'interrupteur du module (les tests qui le neutralisent le
    monkeypatchent explicitement).
    """
    monkeypatch.setattr(mcp_flow, "_MCP_FLOW_ENABLED", True)
    monkeypatch.setattr(mcp_flow, "_MCP_HOST_STANDALONE", True)
    fs.reset_flow_store()
    yield
    fs.reset_flow_store()


def _server(*, orchestrate: dict | None = None, scope: MCPScopeRole = MCPScopeRole.READ_ONLY):
    """Serveur MCP v2.0.0 (orchestrate CONTRIBUTOR ou tool stub injecté)."""
    kwargs = {"version": MCPVersion(major=2, minor=0, patch=0)}
    if orchestrate is not None:
        scope = MCPScopeRole.CONTRIBUTOR
        kwargs["orchestrate_tool"] = MCPTool(
            name="orchestrate",
            description="outil orchestrate stub pour les tests de flux",
            input_schema={"type": "object", "properties": {}},
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
            required_scope=MCPScopeRole.CONTRIBUTOR,
            handler=lambda __, _payload=orchestrate: json.dumps(_payload),
        )
    return build_mcp_server(scope=scope, **kwargs)


def _call(server, method: str, params: dict | None = None, request_id: int = 1):
    raw = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                      "params": params or {}})
    return json.loads(server.handle_text(raw, client_id="client-alpha"))


def _rows() -> list[dict]:
    """Toutes les sessions Flow Map de la base de test (récentes d'abord)."""
    return fs.get_flow_store().list(limit=200, status=None)


def _row(flow_id: str) -> dict:
    return fs.get_flow_store().get(flow_id)


def _events(row: dict) -> list[str]:
    return [e.get("event") for e in row.get("events") or []]


# ============================


def test_mcp_flow_enabled_by_default():
    """L'interrupteur du traçage est actif par défaut (rollback explicite)."""
    assert mcp_flow_enabled() is True


def test_simple_tool_call_creates_mini_session():
    """``tools/call`` hors orchestrate → MINI-session ``source="mcp"``."""
    server = _server()
    reply = _call(server, "tools/call", {"name": "mcp_version", "arguments": {}})
    assert reply.get("error") is None

    rows = _rows()
    assert len(rows) == 1
    row = _row(rows[0]["id"])
    assert row["source"] == "mcp"
    assert row["status"] == fs.COMPLETED
    assert _events(row) == ["mcp.tool", "mcp.tool"]
    assert row["events"][0]["data"]["event"] == "tool_start"
    assert row["events"][0]["data"]["tool"] == "mcp_version"
    assert row["events"][1]["data"]["status"] == "ok"
def test_simple_tool_call_error_is_error_status():
    """``tools/call`` qui lève ToolError → session ``error`` (isError)."""
    def _boom(_: dict) -> str:
        raise ToolError("API externe injoignable")

    tool = MCPTool(
        name="flaky", description="Tool qui échoue", input_schema=empty_input_schema(),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        required_scope=MCPScopeRole.READ_ONLY, handler=_boom,
    )
    from app.infrastructure.mcp.mcp_server import InMemoryToolProvider
    server = build_mcp_server(tool_provider=InMemoryToolProvider([tool]))
    reply = _call(server, "tools/call", {"name": "flaky", "arguments": {}})
    assert reply["result"]["isError"] is True

    rows = _rows()
    assert len(rows) == 1
    row = _row(rows[0]["id"])
    assert row["status"] == fs.ERROR
    assert row["events"][1]["data"]["status"] == "error"


def test_orchestrate_creates_rich_session():
    """``tools/call orchestrate`` → session RICHE + statut `completed`."""
    server = _server(orchestrate={"answer": "fait", "status": "completed"})
    reply = _call(server, "tools/call", {
        "name": "orchestrate",
        "arguments": {"prompt": "résume", "session_id": "s1", "scope": "default"},
    }, request_id=41)
    assert reply.get("error") is None

    rows = _rows()
    assert len(rows) == 1
    row = _row(rows[0]["id"])
    assert row["source"] == "mcp"
    assert row["status"] == fs.COMPLETED
    assert row["prompt"] == "résume"
    assert _events(row)[:2] == ["mcp.orchestrate.start", "mcp.done"]
    assert row["events"][0]["data"]["client_id"] == "client-alpha"
    assert row["events"][0]["data"]["session_id"] == "s1"
    assert "Agent MCP" in rows[0]["agents"]


def test_orchestrate_pending_approval_maps_status():
    """``awaiting_approval`` renvoyé par le tool → session ``awaiting_approval``."""
    server = _server(orchestrate={"answer": "en attente", "status": "awaiting_approval"})
    _call(server, "tools/call", {
        "name": "orchestrate", "arguments": {"prompt": "écris", "scope": "default"},
    })

    rows = _rows()
    assert len(rows) == 1
    row = _row(rows[0]["id"])
    assert row["status"] == fs.AWAITING_APPROVAL
    assert row["events"][-1]["event"] == "mcp.done"
    assert row["events"][-1]["data"]["status"] == "awaiting_approval"


def test_orchestrate_error_is_error():
    """``orchestrate`` en échec (isError) → session ``error`` + ``mcp.error``."""
    def _boom(_: dict) -> str:
        raise ToolError("le LLM est injoignable")

    tool = MCPTool(
        name="orchestrate", description="échec", input_schema={"type": "object"},
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
        required_scope=MCPScopeRole.CONTRIBUTOR, handler=_boom,
    )
    server = build_mcp_server(
        scope=MCPScopeRole.CONTRIBUTOR,
        version=MCPVersion(major=2, minor=0, patch=0),
        orchestrate_tool=tool,
    )
    reply = _call(server, "tools/call", {
        "name": "orchestrate", "arguments": {"prompt": "x"},
    })
    assert reply["result"]["isError"] is True

    rows = _rows()
    assert len(rows) == 1
    row = _row(rows[0]["id"])
    assert row["status"] == fs.ERROR
    assert "mcp.error" in _events(row)


def test_resources_read_creates_mini_session():
    """``resources/read`` → MINI-session ``mcp.call`` + ``mcp.result``."""
    server = _server()
    reply = _call(server, "resources/read", {
        "uri": "thinktuning://health",
    })
    assert reply.get("error") is None

    rows = _rows()
    assert len(rows) == 1
    row = _row(rows[0]["id"])
    assert row["source"] == "mcp"
    assert _events(row) == ["mcp.call", "mcp.result"]
    assert row["events"][0]["data"]["method"] == "resources/read"
    assert row["events"][1]["data"]["status"] == "ok"


def test_catalog_methods_create_no_session():
    """initialize / ping / tools/list : aucune écriture (même policy audit)."""
    server = _server()
    _call(server, "initialize", {})
    _call(server, "ping", {})
    _call(server, "tools/list", {})
    assert _rows() == []


def test_flow_non_blocking_when_store_fails(monkeypatch):
    """Une écriture qui échoue n'altère JAMAIS la réponse MCP."""
    class _BrokenStore:
        def start_flow(self, *a, **k):
            raise RuntimeError("écriture impossible")

    def _broken_get_flow_store():
        return _BrokenStore()

    # Les hooks flow résolvent le store en paresseux (``from app.infrastructure.persistence.flow_store
    # import get_flow_store``) : on casse le getter partagé du module.
    monkeypatch.setattr(fs, "get_flow_store", _broken_get_flow_store)

    server = _server()
    reply = _call(server, "tools/call", {"name": "mcp_version", "arguments": {}})
    assert reply.get("error") is None
    assert "result" in reply

    # Session riche : l'ouverture échoue aussi → la réponse reste intacte.
    rich = _server(orchestrate={"answer": "ok", "status": "completed"})
    reply = _call(rich, "tools/call", {
        "name": "orchestrate", "arguments": {"prompt": "p"},
    }, request_id=2)
    assert reply.get("error") is None
    assert "result" in reply


def test_flow_disabled_switch_writes_nothing(monkeypatch):
    """``MCP_FLOW_ENABLED=false`` → aucune session créée (rollback)."""
    monkeypatch.setattr(mcp_flow, "_MCP_FLOW_ENABLED", False)
    server = _server()
    _call(server, "tools/call", {"name": "mcp_version", "arguments": {}})
    _call(server, "initialize", {})
    assert _rows() == []

# --- Chemin SSE streaming (Assistant IA MCP) ------------------------------------

_SSE_API_KEY = "test-mcp-flow-key"
_SSE_AUTH = {"X-API-Key": _SSE_API_KEY}


@pytest.fixture
def sse_client(monkeypatch):
    """Mini-app FastAPI ne montant QUE le router MCP SSE (transport streaming)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.infrastructure.mcp.mcp_server_sse import router as mcp_sse_router

    monkeypatch.setenv("API_KEY", _SSE_API_KEY)
    app = FastAPI()
    app.include_router(mcp_sse_router)
    return TestClient(app)


def _sse_orchestrate_body(**extra) -> str:
    arguments = {"prompt": "Quelle heure ?", "stream": True, "enable_thinking": True}
    arguments.update(extra)
    return json.dumps({
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "orchestrate", "arguments": arguments},
    })


def test_sse_streaming_orchestrate_persists_rich_flow(sse_client, monkeypatch):
    """Le chemin streaming (Assistant IA MCP) persiste UNE session riche.

    Ce chemin bypass ``MCPServer._handle_method`` : sans le câblage du worker
    SSE, aucune session n'atterrissait dans ``agent_flows`` (bug remonté).
    """
    from app.infrastructure.mcp import mcp_server_sse

    def fake_orchestrate_stream(prompt, **kwargs):
        kwargs["on_event"]("orchestrate.thinking", {"thinking_delta": "j'analyse…"})
        kwargs["on_event"]("orchestrate.tool", {"event": "tool_start", "tool": "now", "args": {}})
        kwargs["on_event"](
            "orchestrate.tool",
            {"event": "tool_result", "tool": "now", "status": "ok", "summary": "12:00"},
        )
        return AgentRunResult(
            answer="Il est 12:00.", thinking="j'analyse…", status=RunStatus.COMPLETED
        )

    monkeypatch.setattr(mcp_server_sse, "orchestrate_stream", fake_orchestrate_stream)
    response = sse_client.post("/mcp/sse", content=_sse_orchestrate_body(), headers=_SSE_AUTH)

    assert response.status_code == 200
    assert '"isError": false' in response.text

    rows = _rows()
    assert len(rows) == 1  # exactement UNE session (pas de doublon handle_text)
    row = _row(rows[0]["id"])
    assert row["source"] == "mcp"
    assert row["status"] == fs.COMPLETED
    assert row["prompt"] == "Quelle heure ?"
    events = _events(row)
    assert events[0] == "mcp.orchestrate.start"
    assert "mcp.thinking" in events
    assert events.count("mcp.tool") == 2  # tool_start + tool_result
    assert events[-1] == "mcp.done"
    assert rows[0]["tool_calls"] == 1
    assert "Agent MCP" in rows[0]["agents"]


def test_sse_streaming_multi_agent_emits_terminal_result(sse_client, monkeypatch):
    """Le chemin multi-agent ne doit pas référencer le résultat mono-agent."""
    from app.infrastructure.mcp import mcp_server_sse

    def fake_orchestrate_multi_agent(prompt, **kwargs):
        kwargs["on_event"]("orchestrate.synthesis", {"phase": "synthesis"})
        return {
            "answer": "CPU et GPU détectés.",
            "status": "completed",
            "workers": [],
            "worker_errors": [],
            "orchestration": {"mode": "multi_agent"},
        }

    monkeypatch.setattr(mcp_server_sse, "orchestrate_multi_agent", fake_orchestrate_multi_agent)
    response = sse_client.post(
        "/mcp/sse",
        content=_sse_orchestrate_body(mode="multi_agent"),
        headers=_SSE_AUTH,
    )

    assert response.status_code == 200
    assert "orchestrate.done" in response.text
    assert "orchestrate.error" not in response.text
    assert "CPU et GPU détectés." in response.text
    rows = _rows()
    assert len(rows) == 1
    assert _row(rows[0]["id"])["status"] == fs.COMPLETED


def test_sse_durable_replay_uses_injected_store(monkeypatch):
    from app.infrastructure.mcp import mcp_server_sse

    class FakeStore:
        def list_events_after(self, run_id, after_sequence=0):
            assert run_id == "run-1"
            assert after_sequence == 3
            return [{"sequence": 4, "event": "worker.completed"}]

    mcp_server_sse.configure_mcp_durable_run_store(FakeStore())
    payload = {
        "params": {
            "arguments": {
                "run_id": "run-1",
                "after_sequence": 3,
            }
        }
    }

    async def collect():
        return [event async for event in mcp_server_sse._replay_durable_events(payload)]

    events = asyncio.run(collect())
    assert '"run_id": "run-1"' in events[0]
    assert "worker.completed" in events[1]
    assert '"last_sequence": 4' in events[2]
    mcp_server_sse.configure_mcp_durable_run_store(None)


def test_sse_durable_replay_rejects_invalid_cursor(sse_client):
    response = sse_client.post(
        "/mcp/sse",
        content=json.dumps({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "orchestrate_events",
                "arguments": {"run_id": "run-1", "after_sequence": "invalid", "stream": True},
            },
        }),
        headers=_SSE_AUTH,
    )
    assert response.status_code == 200
    assert "after_sequence must be an integer" in response.text
    assert "data: [DONE]" in response.text


def test_sse_orchestrate_rejects_invalid_event_granularity(sse_client):
    response = sse_client.post(
        "/mcp/sse",
        content=_sse_orchestrate_body(event_granularity="all"),
        headers=_SSE_AUTH,
    )
    assert response.status_code == 200
    assert '"code": -32602' in response.text
    assert "data: [DONE]" in response.text

def test_sse_streaming_orchestrate_error_persists_error_flow(sse_client, monkeypatch):
    """Un run streaming en échec clôture la session en ``error`` (jamais bloquant)."""
    from app.infrastructure.mcp import mcp_server_sse

    def fake_boom(prompt, **kwargs):
        raise RuntimeError("LLM injoignable")

    monkeypatch.setattr(mcp_server_sse, "orchestrate_stream", fake_boom)
    response = sse_client.post("/mcp/sse", content=_sse_orchestrate_body(), headers=_SSE_AUTH)

    assert response.status_code == 200
    assert '"isError": true' in response.text

    rows = _rows()
    assert len(rows) == 1
    row = _row(rows[0]["id"])
    assert row["status"] == fs.ERROR
    assert "mcp.error" in _events(row)

def test_sse_streaming_awaiting_approval_persists_approval(sse_client, monkeypatch):
    """Un run streaming ``pending_approval`` trace l'approbation puis clôture."""
    from app.domain.entities.plan import Action
    from app.infrastructure.mcp import mcp_server_sse

    def fake_pending(prompt, **kwargs):
        kwargs["on_event"](
            "orchestrate.tool", {"event": "tool_start", "tool": "write_file", "args": {}}
        )
        return AgentRunResult(
            answer="",
            status=RunStatus.PENDING_APPROVAL,
            awaiting_action=Action(tool="write_file", args={}, category="write"),
        )

    monkeypatch.setattr(mcp_server_sse, "orchestrate_stream", fake_pending)
    response = sse_client.post("/mcp/sse", content=_sse_orchestrate_body(), headers=_SSE_AUTH)

    assert response.status_code == 200
    rows = _rows()
    assert len(rows) == 1
    row = _row(rows[0]["id"])
    assert row["status"] == fs.AWAITING_APPROVAL
    approval_events = [e for e in row["events"] if e["event"] == "mcp.approval"]
    assert approval_events[0]["data"]["approval"]["tool"] == "write_file"

def test_orchestrate_relays_recorder_callbacks(monkeypatch):
    """Chemin non-streaming : ``orchestrate()`` alimente le recorder du transport.

    ``MCPServer._handle_method`` ouvre la session mais n'injecte pas les
    callbacks : sans ce relais, la session riche ne contenait que start+done
    (aucun outil, aucune réflexion).
    """
    from app.infrastructure.mcp.mcp_flow import (
        MCPCallContext,
        begin_orchestrate_flow,
        clear_call_context,
        set_call_context,
    )
    from app.infrastructure.mcp.tools.orchestrate_tool import orchestrate

    token = set_call_context(MCPCallContext(client_id="cli-relay", request_id=None))
    recorder = None
    try:
        recorder = begin_orchestrate_flow(prompt="relais")
        assert recorder is not None
        captured: dict = {}

        class _FakeCore:
            def run(self, intent):
                return AgentRunResult(answer="ok", status=RunStatus.COMPLETED)

        def _fake_build_agent_core(**kwargs):
            captured.update(kwargs)
            return _FakeCore()

        monkeypatch.setattr("app.agent.factory.build_agent_core", _fake_build_agent_core)
        result = orchestrate("relais")
        assert result.answer == "ok"
        # Les callbacks passés au noyau sont CEUX du recorder (relais automatique).
        assert captured["on_tool_event"] == recorder.record_tool
        assert captured["on_thinking"] == recorder.record_thinking
        # Les événements relayés par le noyau atterrissent dans la session.
        captured["on_tool_event"]({"event": "tool_start", "tool": "now"})
        captured["on_thinking"]("réflexion")
        assert "mcp.tool" in _events(_row(recorder.flow_id))
        assert "mcp.thinking" in _events(_row(recorder.flow_id))
    finally:
        if recorder is not None:
            recorder.finish(fs.COMPLETED)
        clear_call_context(token)
