# project/tests/test_mcp_sse_done_always.py
"""Garantie de terminaison des flux SSE MCP (trace* + message + [DONE]).

Regression IHM chat mode MCP multi-agent : apres agent.worker.result /
agent.synthesizing / agent.phase, _stream_orchestrate pouvait se terminer
SANS "data: [DONE]" (return secs sur deconnexion) et SANS evenement final.
Le front sortait de boucle sans finalRpc -> "sans reponse finale".
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.infrastructure.mcp import mcp_server_sse as sse


def _collect(gen) -> list[str]:
    async def _run() -> list[str]:
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


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


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class _DisconnectedRequest:
    async def is_disconnected(self) -> bool:
        return True


def _freeze_orchestration_port(monkeypatch) -> None:
    """Fige la résolution du port d'orchestration (L1 : aucun durable en test).

    Sans ce freeze, la préparation du run durable tenterait l'adaptateur de
    production : dans les tests unitaires du transport, le streaming part sans
    run durable (``run_id`` absent de ``orchestrate.started``). Les attributs
    globaux sont posés via ``monkeypatch`` : restaurés à la fin du test.
    """
    monkeypatch.setattr(sse, "_orchestration_port", None, raising=True)
    monkeypatch.setattr(sse, "_orchestration_port_resolved", True, raising=True)


def test_stream_nominal_trace_then_done(monkeypatch) -> None:
    """Fin nominale : trace multi-agent, orchestrate.done, message, DONE."""

    def _fake_multi(prompt: str, **kwargs: Any) -> dict[str, Any]:
        on_event = kwargs["on_event"]
        on_event("agent.worker.result", {"worker_id": "w1", "summary": "ok"})
        on_event("agent.synthesizing", {"phase": "synthesis"})
        on_event("agent.phase", {"phase": "synthesis", "status": "ok"})
        return {"answer": "reponse finale"}

    _freeze_orchestration_port(monkeypatch)
    monkeypatch.setattr(sse, "orchestrate_multi_agent", _fake_multi, raising=True)
    chunks = _collect(sse._stream_orchestrate(
        _stream_payload("bonjour", mode="multi_agent"),
        client_id="test", request=_ConnectedRequest()))
    kinds = [kind for kind, _ in _events(chunks)]
    assert "agent.worker.result" in kinds
    assert "agent.synthesizing" in kinds
    assert "agent.phase" in kinds
    assert kinds.count("orchestrate.done") == 1
    assert kinds.count("message") == 1
    assert chunks[-1] == "data: [DONE]\n\n"
    message = next(d for k, d in _events(chunks) if k == "message")
    assert json.loads(message)["result"]["isError"] is False


def test_stream_disconnect_still_closes_with_done(monkeypatch) -> None:
    """Deconnexion permanente : le terminal déjà produit est quand même émis.

    Le worker tourne dans son thread même si le client est déconnecté dès
    l'ouverture : ``emit()`` conserve les terminaux (bypass disconnect) et
    la boucle les draine au heartbeat avant de clore par [DONE]. Le flux ne
    se termine donc JAMAIS sans [DONE] ; l'erreur synthétique n'est émise
    que si AUCUN terminal n'a été produit (voir test_sans_final).
    """

    def _fake_multi(prompt: str, **kwargs: Any) -> dict[str, Any]:
        kwargs["on_event"]("agent.phase", {"phase": "lead"})
        return {"answer": "trop tard"}

    _freeze_orchestration_port(monkeypatch)
    monkeypatch.setattr(sse, "orchestrate_multi_agent", _fake_multi, raising=True)
    chunks = _collect(sse._stream_orchestrate(
        _stream_payload("bonjour", mode="multi_agent"),
        client_id="test", request=_DisconnectedRequest()))
    assert chunks, "le flux deconnecte doit emettre sa cloture"
    assert chunks[-1] == "data: [DONE]\n\n"
    kinds = [kind for kind, _ in _events(chunks)]
    # Le worker a produit son terminal malgré la déconnexion : il est drainé.
    assert "orchestrate.done" in kinds
    assert "message" in kinds


def test_stream_minimal_granularity_keeps_terminal_events(monkeypatch) -> None:
    """Granularite minimal : trace filtree mais done/message/DONE conserves."""

    def _fake_multi(prompt: str, **kwargs: Any) -> dict[str, Any]:
        on_event = kwargs["on_event"]
        on_event("agent.worker.result", {"phase": "worker"})
        on_event("orchestrate.synthesis", {"phase": "synthesis"})
        return {"answer": "ok minimal"}

    _freeze_orchestration_port(monkeypatch)
    monkeypatch.setattr(sse, "orchestrate_multi_agent", _fake_multi, raising=True)
    chunks = _collect(sse._stream_orchestrate(
        _stream_payload("bonjour", mode="multi_agent", event_granularity="minimal"),
        client_id="test", request=_ConnectedRequest()))
    kinds = [kind for kind, _ in _events(chunks)]
    assert "agent.worker.result" not in kinds
    assert "orchestrate.done" in kinds
    assert "message" in kinds
    assert chunks[-1] == "data: [DONE]\n\n"


def test_stream_without_final_event_emits_synthetic_error_then_done(monkeypatch) -> None:
    """Pont qui avale le terminal : erreur synthetique + DONE (jamais vide).

    Simule un pont d'evenements qui ne relaie QUE la trace (ex. filtre de
    granularite defectueux cote adaptateur) : le SSE doit quand meme clore
    par orchestrate.error + message + DONE.
    L1 (SCRUM-152) : le pont est la classe ``_SseEventBridge`` (file asyncio
    annulable) — le test monkeypatche ``put`` au lieu de l'ancien
    ``queue.Queue`` (supprimé avec l'attente bloquante en thread).
    """

    def _fake_multi(prompt: str, **kwargs: Any) -> dict[str, Any]:
        kwargs["on_event"]("agent.worker.result", {"phase": "worker"})
        return {"answer": "perdu par le filtre"}

    _freeze_orchestration_port(monkeypatch)
    monkeypatch.setattr(sse, "orchestrate_multi_agent", _fake_multi, raising=True)

    real_bridge = sse._SseEventBridge

    class _SwallowTerminalBridge(real_bridge):
        def put(self, item, *a, **k):
            if isinstance(item, tuple) and item[0] in {
                "orchestrate.done", "orchestrate.error", "message"}:
                return
            return super().put(item, *a, **k)

    monkeypatch.setattr(sse, "_SseEventBridge", _SwallowTerminalBridge, raising=True)
    chunks = _collect(sse._stream_orchestrate(
        _stream_payload("bonjour", mode="multi_agent"),
        client_id="test", request=_ConnectedRequest()))
    kinds = [kind for kind, _ in _events(chunks)]
    assert "agent.worker.result" in kinds
    assert "orchestrate.error" in kinds
    assert "message" in kinds
    assert chunks[-1] == "data: [DONE]\n\n"
    synthetic = next(d for k, d in _events(chunks) if k == "message")
    rpc = json.loads(synthetic)
    assert rpc["result"]["isError"] is True
    inner = json.loads(rpc["result"]["content"][0]["text"])
    assert inner["reason"] == "orchestration_stream_interrupted"
    assert inner["failure_phase"] == "synthesis"


def test_non_streaming_path_ends_with_done(monkeypatch) -> None:
    """Chemin non-stream : message JSON-RPC + DONE (contrat uniforme)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class _S:
        @staticmethod
        def handle_text(raw, client_id=None, correlation_id=None):
            return '{"jsonrpc": "2.0", "id": 1, "result": {}}'

    monkeypatch.setattr(sse, "_server", _S(), raising=False)
    monkeypatch.setattr(sse, "mcp_auth_required", lambda: False, raising=True)
    app = FastAPI()
    app.include_router(sse.router)
    client = TestClient(app)
    response = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "orchestrate", "arguments": {"prompt": "x"}}})
    assert response.status_code == 200
    body = response.text
    assert "event: message" in body
    assert body.rstrip().endswith("data: [DONE]")
