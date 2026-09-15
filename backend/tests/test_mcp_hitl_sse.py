# project/tests/test_mcp_hitl_sse.py
"""P0 (SCRUM-151) : flux SSE MCP — HITL, repli explicite, sentinelle [DONE].

Couvre les critères d'acceptation L0 :
    1. approbation HITL mono-agent streamée → ``request_id`` (demande
       d'approbation) ET ``run_id`` (run durable) distincts dans le message
       JSON-RPC final — la carte de validation peut s'afficher puis relancer ;
    2. repli mono-agent EXPLICITE : événement ``orchestration_fallback`` + payload
       ``orchestration`` IDENTIQUES côté stream et côté handler (même
       ``resolve_orchestration``) ;
    3. initialize / ping / tools/list répondent en SSE terminé par
       ``data: [DONE]``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.core import AgentRunResult, RunStatus
from app.domain.entities.plan import Action, Intent
from app.infrastructure.mcp import mcp_server_sse as sse
from app.infrastructure.mcp.mcp_server_sse import router as mcp_sse_router
from app.infrastructure.mcp.tools.orchestrate_tool import (
    MonoAgentOutcome,
    build_orchestrate_tool,
)

API_KEY = "test-mcp-key"
AUTH = {"X-API-Key": API_KEY}


def _app() -> FastAPI:
    """Mini-app FastAPI ne montant QUE le router MCP (tests isolés)."""
    test_app = FastAPI()
    test_app.include_router(mcp_sse_router)
    return test_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app())


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch):
    monkeypatch.setenv("API_KEY", API_KEY)
    yield


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


def _collect(gen) -> list[str]:
    async def _run() -> list[str]:
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def _stream_payload(prompt: str, request_id: Any = 1, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": "orchestrate",
            "arguments": {"prompt": prompt, "stream": True, **arguments},
        },
    }


def _events(chunks: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for chunk in chunks:
        event = "message"
        data = ""
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip() or "message"
            elif line.startswith("data:"):
                data = line[len("data:"):].lstrip()
        if not data or data.startswith(":") or data == "[DONE]":
            continue
        out.append((event, data))
    return out


def _final_payload(chunks: list[str]) -> dict[str, Any]:
    """Texte JSON du bloc de contenu de la réponse JSON-RPC terminale."""
    message = next(d for k, d in _events(chunks) if k == "message")
    return json.loads(json.loads(message)["result"]["content"][0]["text"])


def test_stream_mono_approval_carries_distinct_request_and_run_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HITL streamée : ``request_id`` (demande) ≠ ``run_id`` (run durable)."""

    def _fake_stream(prompt: str, **kwargs: Any) -> MonoAgentOutcome:
        return MonoAgentOutcome(
            result=AgentRunResult(
                answer="",
                status=RunStatus.PENDING_APPROVAL,
                awaiting_action=Action(tool="write_file", args={}, category="write"),
            ),
            approval={"request_id": "req-1", "tool": "write_file", "args": {}},
        )

    monkeypatch.setattr(sse, "orchestrate_stream", _fake_stream, raising=True)
    chunks = _collect(sse._stream_orchestrate(
        _stream_payload("fais une action", run_id="run-77"),
        client_id="test",
        request=_ConnectedRequest(),
    ))
    assert chunks[-1] == "data: [DONE]\n\n"
    payload = _final_payload(chunks)
    assert payload["awaiting_approval"] is True
    assert payload["request_id"] == "req-1"
    assert payload["run_id"] == "run-77"
    assert payload["run_id"] != payload["request_id"]
    assert payload["approval"]["tool"] == "write_file"


def test_stream_fallback_is_explicit_and_identical_to_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repli mono-agent explicite, identique stream vs non-stream."""
    import app.infrastructure.persistence.agent_settings as agent_settings

    class FakeStore:
        def get_all(self) -> dict[str, Any]:
            return {"flag_multi_agent": False}

    monkeypatch.delenv("MCP_MULTI_AGENT_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_MULTI_AGENT", raising=False)
    monkeypatch.setattr(agent_settings, "get_settings_store", lambda: FakeStore())

    class _Core:
        def run(self, intent: Intent) -> AgentRunResult:
            return AgentRunResult(answer="mono", status=RunStatus.COMPLETED)

    def _fake_stream(prompt: str, **kwargs: Any) -> MonoAgentOutcome:
        return MonoAgentOutcome(
            result=AgentRunResult(answer="mono", status=RunStatus.COMPLETED)
        )

    monkeypatch.setattr(sse, "orchestrate_stream", _fake_stream, raising=True)

    # --- chemin stream -------------------------------------------------------
    chunks = _collect(sse._stream_orchestrate(
        _stream_payload("bonjour", mode="multi_agent"),
        client_id="test",
        request=_ConnectedRequest(),
    ))
    assert chunks[-1] == "data: [DONE]\n\n"
    kinds = [k for k, _ in _events(chunks)]
    assert "orchestration_fallback" in kinds
    fallback_event = json.loads(
        next(d for k, d in _events(chunks) if k == "orchestration_fallback")
    )
    assert fallback_event["reason"] == "multi_agent_disabled"
    stream_payload = _final_payload(chunks)
    assert stream_payload["answer"] == "mono"
    assert stream_payload["orchestration"]["reason"] == "multi_agent_disabled"

    # --- chemin non-stream (handler du tool) ---------------------------------
    tool = build_orchestrate_tool(core_factory=lambda: _Core())
    handler_payload = json.loads(
        tool.handler({"prompt": "bonjour", "mode": "multi_agent"})
    )
    assert handler_payload["answer"] == "mono"
    # PARITÉ : le payload de repli est IDENTIQUE sur les deux transports.
    assert handler_payload["orchestration"] == stream_payload["orchestration"]
    assert handler_payload["orchestration"]["fallback"] == "orchestration_fallback"


def test_sse_initialize_ping_tools_list_always_end_with_done(client: TestClient) -> None:
    """initialize, ping et tools/list → SSE terminé par ``data: [DONE]``."""
    for method, params in (
        ("initialize", {}),
        ("ping", {}),
        ("tools/list", {}),
    ):
        response = client.post(
            "/mcp/sse",
            content=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
            }),
            headers=AUTH,
        )
        assert response.status_code == 200, method
        assert response.headers["content-type"].startswith("text/event-stream"), method
        assert "event: message" in response.text, method
        assert response.text.endswith("data: [DONE]\n\n"), method
