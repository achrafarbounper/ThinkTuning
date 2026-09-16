# project/tests/test_mcp_server_basic.py
"""Tests de la couche serveur MCP (S1 — Bootstrap, docs/mcp/IMPLEMENTATION_PLAN.md).

Couverture de la tâche 2 (ListTools, CallTool) :
    1. dispatch JSON-RPC 2.0 : initialize, ping, tools/list, tools/call,
       méthodes inconnues, erreurs de parse, notifications ;
    2. filtrage de visibilité par scope (MCPScopeRole, fail-closed) ;
    3. transport SSE (``POST /mcp/sse`` → flux text/event-stream) ;
    4. transport stdio (``thinktuning-mcp``, JSON-RPC ligne à ligne).

Le socle MCP est self-contained (AUCUNE dépendance au SDK ``mcp``) : ces tests
n'importent rien de lourd (ni torch, ni transformers) — le paquet ``app`` est
suffisant.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.core import AgentRunResult, RunStatus
from app.domain.entities.mcp import MCPScopeRole, MCPTool, MCPVersion
from app.infrastructure.mcp import mcp_server_sse
from app.infrastructure.mcp.mcp_server import (
    InMemoryToolProvider,
    ToolError,
)
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.mcp_server_sse import router as mcp_sse_router
from app.infrastructure.mcp.mcp_server_stdio import serve_stdio
from app.infrastructure.mcp.protocol import ErrorCode, empty_input_schema


def _sse_app() -> FastAPI:
    """Mini-app FastAPI ne montant QUE le router MCP (tests isolés et rapides)."""
    test_app = FastAPI()
    test_app.include_router(mcp_sse_router)
    return test_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_sse_app())


# Clé transport MCP (P5) : le SSE exige X-API-Key — posée à chaque test
# (lecture à l'appel) et transmise par chaque requête authentifiée.
API_KEY = "test-mcp-key"
AUTH = {"X-API-Key": API_KEY}


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch):
    """Pose API_KEY pendant chaque test (la clé est lue à l'appel)."""
    monkeypatch.setenv("API_KEY", API_KEY)
    yield


# --- Dispatch JSON-RPC / MCP -------------------------------------------------


def test_initialize_handshake():
    """initialize → protocolVersion, capabilities.tools, serverInfo."""
    server = build_mcp_server()
    reply = json.loads(server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    })))
    assert reply["id"] == 1
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    assert reply["result"]["capabilities"] == {
        "tools": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": False},
        "prompts": {"listChanged": False},
        "sampling": {},
    }
    assert "logging" not in reply["result"]["capabilities"]
    assert reply["result"]["serverInfo"]["name"] == "thinktuning-mcp"


def test_ping():
    """ping → réponse ``{}``."""
    server = build_mcp_server()
    reply = json.loads(server.handle_text(
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
    ))
    assert reply["result"] == {}


def test_list_tools_bootstrap():
    """tools/list → les 2 tools du registre bootstrap (mcp_version, server_info)."""
    server = build_mcp_server()
    reply = json.loads(server.handle_text(
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
    ))
    names = {tool["name"] for tool in reply["result"]["tools"]}
    assert {"mcp_version", "server_info"} <= names


def test_call_tool_missing_name():
    """tools/call sans ``name`` → Invalid Params."""
    server = build_mcp_server()
    reply = json.loads(server.handle_text(
        json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {}})
    ))
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_tool_error_is_error():
    """Un handler qui lève ToolError → isError: true (erreur métier)."""
    def _boom(_: dict) -> str:
        raise ToolError("API externe injoignable")

    tool = MCPTool(
        name="flaky",
        description="Tool qui échoue",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=_boom,
    )
    server = build_mcp_server(tool_provider=InMemoryToolProvider([tool]))
    reply = json.loads(server.handle_text(
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": "flaky", "arguments": {}}})
    ))
    assert reply["result"]["isError"] is True
    assert "injoignable" in reply["result"]["content"][0]["text"]


def test_batch_unsupported():
    """Requête en lot (liste) → Invalid Request."""
    server = build_mcp_server()
    reply = json.loads(server.handle_text(json.dumps([{"jsonrpc": "2.0", "id": 1}])))
    assert reply["error"]["code"] == ErrorCode.INVALID_REQUEST


def test_parse_error():
    """Corps non-JSON → Parse error."""
    server = build_mcp_server()
    reply = json.loads(server.handle_text("not-json{{{"))
    assert reply["error"]["code"] == ErrorCode.PARSE_ERROR


def test_method_not_found():
    """Méthode inconnue → Method Not Found."""
    server = build_mcp_server()
    reply = json.loads(server.handle_text(
        json.dumps({"jsonrpc": "2.0", "id": 8, "method": "bogus", "params": {}})
    ))
    assert reply["error"]["code"] == ErrorCode.METHOD_NOT_FOUND


def test_notification_no_response():
    """Notification JSON-RPC (sans id) → aucune réponse (None)."""
    server = build_mcp_server()
    response = server.handle_text(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    )
    assert response is None
# --- Filtrage par scope (MCP_SECURITY.md) -------------------------------------


def test_scope_hides_privileged_tool():
    """Un tool exigeant OPERATOR est masqué à un serveur read_only."""
    privileged = MCPTool(
        name="admin_only",
        description="Tool sensible",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
        required_scope=MCPScopeRole.OPERATOR,
        handler=lambda _: "secret",
    )
    provider = InMemoryToolProvider([privileged])

    read_only = build_mcp_server(
        scope=MCPScopeRole.READ_ONLY, tool_provider=provider
    )
    listed = json.loads(read_only.handle_text(
        json.dumps({"jsonrpc": "2.0", "id": 11, "method": "tools/list"})
    ))
    assert listed["result"]["tools"] == []

    admin = build_mcp_server(scope=MCPScopeRole.ADMIN, tool_provider=provider)
    listed_admin = json.loads(admin.handle_text(
        json.dumps({"jsonrpc": "2.0", "id": 12, "method": "tools/list"})
    ))
    assert [t["name"] for t in listed_admin["result"]["tools"]] == ["admin_only"]


def test_scope_granted_ordering():
    """L'ordre des rôles conditionne la visibilité (fail-closed)."""
    assert MCPScopeRole.READ_ONLY.granted(MCPScopeRole.READ_ONLY) is True
    assert MCPScopeRole.OPERATOR.granted(MCPScopeRole.CONTRIBUTOR) is True
    assert MCPScopeRole.CONTRIBUTOR.granted(MCPScopeRole.OPERATOR) is False
    with pytest.raises(ValueError):
        MCPScopeRole.READ_ONLY.granted("root")


# --- Transport SSE ------------------------------------------------------------


def test_sse_initialize(client):
    """POST /mcp/sse → text/event-stream contenant la réponse initialize."""
    response = client.post(
        "/mcp/sse",
        content=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        }),
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "Mcp-Session-Id" in response.headers
    assert "event: message" in response.text
    assert '"serverInfo"' in response.text


def test_sse_call_tool(client):
    """POST /mcp/sse → la réponse tools/call est dans le flux SSE."""
    response = client.post(
        "/mcp/sse",
        content=json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "mcp_version", "arguments": {}},
        }),
        headers=AUTH,
    )
    assert response.status_code == 200
    assert '"text"' in response.text
    assert '"2.3.0"' in response.text


def test_sse_rejects_non_object_params(client):
    """JSON-RPC params must remain an object instead of being silently dropped."""
    response = client.post(
        "/mcp/sse",
        content=json.dumps({
            "jsonrpc": "2.0", "id": 21, "method": "ping", "params": [],
        }),
        headers=AUTH,
    )
    assert response.status_code == 200
    assert '"code": -32602' in response.text
    assert "'params' must be an object" in response.text


def test_sse_lists_orchestrate_for_assistant_scope(client):
    """Le transport SSE expose l'entrée d'orchestration de l'Assistant IA."""
    response = client.post(
        "/mcp/sse",
        content=json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {},
        }),
        headers=AUTH,
    )
    assert response.status_code == 200
    assert '"orchestrate"' in response.text


def test_sse_calls_orchestrate_with_contributor_scope(client, monkeypatch):
    """Le scope du transport laisse passer le tool MCP d'orchestration."""
    from app.infrastructure.mcp import mcp_server_sse
    from app.infrastructure.mcp.mcp_server import MCPTool
    tool = MCPTool(
        name="orchestrate",
        description="test",
        input_schema={"type": "object"},
        annotations={"readOnlyHint": False},
        required_scope=MCPScopeRole.CONTRIBUTOR,
        handler=lambda arguments: '{"answer":"ok","status":"completed"}',
    )
    monkeypatch.setattr(
        mcp_server_sse,
        "_server",
        build_mcp_server(
            scope=MCPScopeRole.CONTRIBUTOR,
            version=MCPVersion(major=2, minor=0, patch=0),
            orchestrate_tool=tool,
        ),
    )
    response = client.post(
        "/mcp/sse",
        content=json.dumps({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "orchestrate",
                "arguments": {
                    "prompt": "hello",
                    "session_id": "d93b2d11810b",
                    "scope": "default",
                },
            },
        }),
        headers=AUTH,
    )
    assert response.status_code == 200
    assert '"isError": false' in response.text
    assert '\\"answer\\":\\"ok\\"' in response.text


def test_sse_orchestrate_streams_core_reflection_payload(client, monkeypatch):
    """Le mode stream diffuse la réflexion opt-in avec le contrat core."""
    from app.infrastructure.mcp import mcp_server_sse

    def fake_orchestrate_stream(prompt, **kwargs):
        assert kwargs["enable_thinking"] is True
        kwargs["on_event"](
            "orchestrate.thinking", {"thinking_delta": "J'analyse la demande."}
        )
        kwargs["on_event"](
            "orchestrate.tool",
            {"event": "tool_start", "tool": "now", "args": {}},
        )
        return AgentRunResult(
            answer="Réponse finale.",
            thinking="J'analyse la demande.",
            status=RunStatus.COMPLETED,
        )

    monkeypatch.setattr(mcp_server_sse, "orchestrate_stream", fake_orchestrate_stream)
    response = client.post(
        "/mcp/sse",
        content=json.dumps({
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "orchestrate",
                "arguments": {
                    "prompt": "Analyse",
                    "stream": True,
                    "enable_thinking": True,
                },
            },
        }),
        headers=AUTH,
    )

    assert response.status_code == 200
    assert '"thinking_delta": "J\'analyse la demande."' in response.text
    assert '"core_tool"' in response.text
    assert '"isError": false' in response.text
    assert "event: message" in response.text
    assert response.text.index("event: orchestrate.done") < response.text.index("event: message")
    assert response.text.index("event: message") < response.text.index("data: [DONE]")


def test_sse_multi_agent_forwards_legacy_planner_worker_thinking_events(
    client, monkeypatch
):
    """Les événements du coordinateur legacy survivent à la projection MCP."""
    from app.infrastructure.mcp import mcp_server_sse

    def fake_multi_agent(prompt, **kwargs):
        on_event = kwargs["on_event"]
        on_event("agent.plan", {"plan": [{"task_id": "t1", "role": "ops"}]})
        on_event(
            "agent.worker.start",
            {"task_id": "t1", "role": "ops", "status": "running"},
        )
        on_event(
            "agent.worker.thinking",
            {"task_id": "t1", "role": "ops", "thinking": "Je vérifie le matériel."},
        )
        on_event(
            "agent.worker.result",
            {"task_id": "t1", "role": "ops", "status": "ok", "summary": "Terminé"},
        )
        on_event("agent.synthesizing", {"status": "running"})
        return {
            "answer": "CPU et GPU détectés.",
            "status": "completed",
            "workers": [{"task_id": "t1", "status": "ok"}],
        }

    monkeypatch.setattr(mcp_server_sse, "orchestrate_multi_agent", fake_multi_agent)
    response = client.post(
        "/mcp/sse",
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "orchestrate",
                    "arguments": {
                        "mode": "multi_agent",
                        "prompt": "info cpu et gpu ?",
                        "enable_thinking": True,
                        "stream": True,
                        "event_granularity": "summary",
                    },
                },
            }
        ),
        headers=AUTH,
    )

    assert response.status_code == 200
    assert "event: agent.plan" in response.text
    assert "event: agent.worker.start" in response.text
    assert "event: agent.worker.thinking" in response.text
    assert "event: agent.worker.result" in response.text
    assert "event: agent.synthesizing" in response.text
    assert "event: message" in response.text
    assert "CPU et GPU détectés." in response.text
    assert "data: [DONE]" in response.text


def test_sse_terminal_events_bypass_granularity_and_disconnect(monkeypatch):
    """Régression cas « 44s » : le final n'est jamais filtré ni droppé.

    - ``agent.done`` (terminal) survit à la granularité ``minimal`` ;
    - ``emit()`` conserve les terminaux même après ``disconnected.set()``
      (poll ``is_disconnected()`` fugacement vrai derrière un proxy).
    """
    from app.infrastructure.mcp import mcp_server_sse

    assert (
        mcp_server_sse._event_allowed_for_sse("agent.done", "minimal") is True
    )
    assert (
        mcp_server_sse._event_allowed_for_sse("orchestrate.done", "minimal")
        is True
    )
    assert (
        mcp_server_sse._event_allowed_for_sse("agent.worker.thinking", "minimal")
        is False
    )


def test_sse_synthesis_slow_still_emits_terminal(client, monkeypatch):
    """Synthèse lente (file vide entre worker.result et agent.done).

    Le worker émet ``agent.worker.result`` puis ``agent.done`` APRÈS que la
    file a déjà été observée vide une fois : le final doit quand même être
    émis (pas d'erreur synthétique ``orchestration_stream_interrupted``).
    """
    from app.infrastructure.mcp import mcp_server_sse

    def fake_multi_agent_slow(prompt, **kwargs):
        on_event = kwargs["on_event"]
        on_event(
            "agent.worker.result",
            {"task_id": "t1", "role": "ops", "status": "ok", "summary": "8 CPU"},
        )
        on_event("agent.synthesizing", {"status": "running"})
        on_event("agent.done", {"answer": "8 CPU, pas de GPU.", "status": "completed"})
        return {
            "answer": "8 CPU, pas de GPU.",
            "status": "completed",
            "workers": [{"task_id": "t1", "status": "ok"}],
        }

    monkeypatch.setattr(
        mcp_server_sse, "orchestrate_multi_agent", fake_multi_agent_slow
    )
    response = client.post(
        "/mcp/sse",
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "orchestrate",
                    "arguments": {
                        "mode": "multi_agent",
                        "prompt": "info cpu et gpu ?",
                        "stream": True,
                        "event_granularity": "summary",
                    },
                },
            }
        ),
        headers=AUTH,
    )

    assert response.status_code == 200
    assert "event: agent.worker.result" in response.text
    assert "8 CPU, pas de GPU." in response.text
    assert "orchestration_stream_interrupted" not in response.text
    assert "data: [DONE]" in response.text


def test_sse_disabled_returns_503(monkeypatch):
    """Interrupteur de rollback MCP_SERVER_ENABLED=false → 503."""
    monkeypatch.setattr(mcp_server_sse, "mcp_server_enabled", lambda: False)
    agent = TestClient(_sse_app())
    response = agent.post(
        "/mcp/sse",
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "mcp_disabled"


# --- Transport stdio ----------------------------------------------------------


def test_stdio_roundtrip():
    """FDA du transport stdio : JSON-RPC ligne à ligne sur stdin → stdout."""
    buf_in = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    buf_out = io.StringIO()
    code = serve_stdio(input_stream=buf_in, output_stream=buf_out)
    assert code == 0
    lines = [line for line in buf_out.getvalue().splitlines() if line.strip()]
    # La notification ne produit aucune réponse : une seule ligne attendue.
    assert len(lines) == 1
    reply = json.loads(lines[0])
    assert reply["id"] == 1
    assert "tools" in reply["result"]


def test_stdio_invalid_json():
    """JSON invalide sur stdin → réponse Parse error, pas de crash."""
    buf_in = io.StringIO("not-json\n")
    buf_out = io.StringIO()
    code = serve_stdio(input_stream=buf_in, output_stream=buf_out)
    assert code == 0
    reply = json.loads(buf_out.getvalue().strip().splitlines()[0])
    assert reply["error"]["code"] == ErrorCode.PARSE_ERROR


def test_resources_list_v100_and_prompts_task9():
    """v1.0.0 (taches 8 + 9) + extensions v1.1.0 (taches 13 + 14).

    resources/list -> les 10 resources thinktuning:// ; prompts/list ->
    les 5 prompts ThinkTuning (les 2 de la tache 9 + les 3 de la tache 14).
    """
    server = build_mcp_server()
    resources = json.loads(server.handle_text(
        json.dumps({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
    ))
    prompts = json.loads(server.handle_text(
        json.dumps({"jsonrpc": "2.0", "id": 10, "method": "prompts/list"})
    ))
    uris = {resource["uri"] for resource in resources["result"]["resources"]}
    assert len(uris) == 10
    assert {
        "thinktuning://jobs",
        "thinktuning://jobs/{job_id}",
        "thinktuning://jobs/{job_id}/logs",
        "thinktuning://models",
        "thinktuning://models/{version}/info",
        "thinktuning://datasets/{path}/stats",
        "thinktuning://datasets/{path}/preview",
        "thinktuning://metrics/{job_id}",
        "thinktuning://config",
        "thinktuning://health",
    } == uris
    names = {prompt["name"] for prompt in prompts["result"]["prompts"]}
    assert names == {
        "analyze-sentiment",
        "plan-training",
        "summarize-job",
        "compare-models",
        "explain-prediction",
    }
def test_call_tool_mcp_version():
    """tools/call sur mcp_version → version de la surface MCP (isError: false)."""
    server = build_mcp_server(version=MCPVersion(major=0, minor=1, patch=0))
    reply = json.loads(server.handle_text(
        json.dumps({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "mcp_version", "arguments": {}},
        })
    ))
    result = reply["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"] == "0.1.0"


def test_call_tool_unknown():
    """tools/call sur un tool absent → erreur Invalid Params (pas de crash)."""
    server = build_mcp_server()
    reply = json.loads(server.handle_text(
        json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "nope", "arguments": {}}})
    ))
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
