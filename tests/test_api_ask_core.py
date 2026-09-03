# project/tests/test_api_ask_core.py
"""Tests d'intégration de l'endpoint POST /api/agent/ask/core.

Le LLM est monkeypatché via la factory : AUCUN appel réseau. Vérifie la
bascule par flag, la réponse standard AskResponse et le mapping des statuts.
"""

import json

import pytest
from fastapi.testclient import TestClient

import api.routes.agent as agent_routes
from app.agent.core import AgentRunResult, RunStatus
from app.domain.entities.plan import Action, ActionCategory

API_HEADERS = {"x-api-key": "test-key"}


class FakeApprovalStore:
    """Store d'approbation en mémoire (contrat ApprovalStorePort)."""

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self._seq = 0

    def create(self, tool, args, category, decision, reason, prompt="", args_hash="", **kw):
        self._seq += 1
        rid = f"req-{self._seq}"
        self.records[rid] = {
            "request_id": rid, "tool": tool, "args": args, "category": category,
            "decision": decision, "reason": reason, "status": "pending",
            "prompt": prompt, "args_hash": args_hash,
        }
        return self.records[rid]

    def get(self, request_id):
        return self.records.get(request_id)

    def approve(self, request_id, decided_by=None):
        rec = self.records.get(request_id)
        if rec:
            rec["status"] = "approved"
        return rec

    def reject(self, request_id, decided_by=None):
        rec = self.records.get(request_id)
        if rec:
            rec["status"] = "rejected"
        return rec

    def list(self, status=None):
        return [r for r in self.records.values() if status is None or r["status"] == status]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    fake_store = FakeApprovalStore()
    monkeypatch.setattr(agent_routes, "build_approval_store", lambda: fake_store)
    # Import tardif après fixation de l'env : l'app lit API_KEY au démarrage.
    from api.main import app

    return TestClient(app), fake_store



def test_ask_core_disabled_without_flag(client, monkeypatch) -> None:
    http, _ = client
    monkeypatch.delenv("AGENT_NEW_CORE", raising=False)
    resp = http.post(
        "/api/agent/ask/core",
        json={"prompt": "bonjour", "session_id": "s-test"},
        headers=API_HEADERS,
    )
    assert resp.status_code == 503


def test_ask_core_completed(client, monkeypatch) -> None:
    http, _ = client
    monkeypatch.setenv("AGENT_NEW_CORE", "1")

    class FakeCore:
        def __init__(self, *a, **kw) -> None:
            pass

        def run(self, intent, history=None):
            assert intent.prompt == "bonjour"
            return AgentRunResult(answer="Réponse du nouveau noyau.",
                                  status=RunStatus.COMPLETED,
                                  rounds_used=1, tool_calls_used=0)

    monkeypatch.setattr(agent_routes, "build_agent_core", lambda *a, **kw: FakeCore())
    resp = http.post(
        "/api/agent/ask/core",
        json={"prompt": "bonjour", "session_id": "s-test"},
        headers=API_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["response"] == "Réponse du nouveau noyau."


def test_ask_core_stream_emits_core_tool_frames(client, monkeypatch) -> None:
    """Le flux SSE diffuse les événements d'outils du noyau en frames « core_tool ».

    L'IHM s'appuie sur ces frames (tool_start / tool_result) pour afficher les
    outils exécutés dans le chat, exactement comme en mode Agent (/ask/stream).
    """
    http, _ = client
    monkeypatch.setenv("AGENT_NEW_CORE", "1")

    class FakeCore:
        def __init__(self, *args, **kwargs) -> None:
            self._on_tool_event = kwargs.get("on_tool_event")

        def run(self, intent, history=None):
            if self._on_tool_event is not None:
                self._on_tool_event({"event": "tool_start",
                                     "tool": "recherche",
                                     "args": {"q": "météo"}})
                self._on_tool_event({"event": "tool_result", "tool": "recherche",
                                     "status": "ok", "summary": "22°C, ensoleillé",
                                     "duration_ms": 123.45})
            return AgentRunResult(answer="Il fait 22°C.",
                                  status=RunStatus.COMPLETED,
                                  rounds_used=1, tool_calls_used=1)

    def fake_build(*args, **kwargs):
        return FakeCore(*args, **kwargs)

    monkeypatch.setattr(agent_routes, "build_agent_core", fake_build)
    # Les événements d'outils sont persistés dans la session : au rechargement,
    # l'IHM réaffiche les outils exécutés (comme en mode Agent). Le store
    # n'écrit que dans une session existante : on la crée explicitement.
    from api.routes.agent import get_session_store
    store = get_session_store()
    session = store.create_session(title="test core stream")
    session_id = str(session["id"])
    with http.stream(
        "POST",
        "/api/agent/ask/core/stream",
        json={"prompt": "quel temps ?", "session_id": session_id},
        headers=API_HEADERS,
    ) as resp:
        assert resp.status_code == 200
        frames = [line.removeprefix("data: ").strip()
                  for line in resp.iter_lines() if line.startswith("data:")]

    assert "[DONE]" in frames
    core_tools = [json.loads(f)["core_tool"] for f in frames
                  if f != "[DONE]" and "core_tool" in f]
    starts = [e for e in core_tools if e.get("event") == "tool_start"]
    results = [e for e in core_tools if e.get("event") == "tool_result"]
    assert starts and starts[0]["tool"] == "recherche"
    assert starts[0]["args"] == {"q": "météo"}
    assert results and results[0]["status"] == "ok"
    assert results[0]["summary"] == "22°C, ensoleillé"
    assert results[0]["duration_ms"] == 123.45
    # La réponse finale reste bien streamée.
    deltas = "".join(json.loads(f)["delta"] for f in frames
                     if f != "[DONE]" and "delta" in f)
    assert deltas.strip() == "Il fait 22°C."

    # Les événements d'outils sont persistés dans la session : au rechargement,
    # l'IHM réaffiche les outils exécutés (comme en mode Agent).
    from api.routes.agent import get_session_store
    store = get_session_store()
    messages = store.get_messages(session_id)
    assistant = [m for m in messages if m.get("role") == "assistant"]
    assert assistant, "la réponse de l'assistant doit être journalisée"
    stored_calls = assistant[-1].get("tool_calls") or []
    tools = [c.get("tool") for c in stored_calls]
    assert "recherche" in tools
    results_stored = [c for c in stored_calls if c.get("event") == "tool_result"]
    assert results_stored and results_stored[0]["status"] == "ok"


def test_ask_core_pending_approval_creates_request(client, monkeypatch) -> None:
    http, store = client
    monkeypatch.setenv("AGENT_NEW_CORE", "1")

    class FakeCore:
        def __init__(self, *a, **kw) -> None:
            pass

        def run(self, intent, history=None):
            return AgentRunResult(
                status=RunStatus.PENDING_APPROVAL, answer="En attente.",
                awaiting_action=Action(tool="echo", args={"text": "x"}),
            )

    monkeypatch.setattr(agent_routes, "build_agent_core", lambda *a, **kw: FakeCore())
    resp = http.post(
        "/api/agent/ask/core",
        json={"prompt": "écris", "session_id": "s-test"},
        headers=API_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "awaiting_approval"
    assert body["approval"]["tool"] == "echo"
    assert body["request_id"] == body["approval"]["request_id"]
    assert len(store.list("pending")) == 1


def test_resume_approved_action_executes(client, monkeypatch) -> None:
    """Reprise : une action approuvée (empreinte) passe la gateway du noyau."""
    http, store = client
    monkeypatch.setenv("AGENT_NEW_CORE", "1")

    action = Action(tool="echo", args={"text": "x"}, category=ActionCategory.WRITE)
    record = store.create("echo", {"text": "x"}, "write", "approve",
                          "test", args_hash=action.fingerprint())
    store.approve(record["request_id"], decided_by="human")

    captured: dict = {}

    class FakeCore:
        def __init__(self, approval_gateway=None, *a, **kw) -> None:
            captured["gateway"] = approval_gateway

        def run(self, intent, history=None):
            assert captured["gateway"](action) is True          # action approuvée
            other = Action(tool="echo", args={"text": "autre"})
            assert captured["gateway"](other) is False          # action différente
            return AgentRunResult(answer="Reprise exécutée.",
                                  status=RunStatus.COMPLETED)

    monkeypatch.setattr(
        agent_routes, "build_agent_core",
        lambda *a, **kw: FakeCore(approval_gateway=kw.get("approval_gateway")),
    )
    resp = http.post(
        "/api/agent/ask/core",
        json={"prompt": "écris", "session_id": "s-test",
              "resume_request_id": record["request_id"]},
        headers=API_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert resp.json()["response"] == "Reprise exécutée."


def test_ask_core_budget_exhausted_mapped(client, monkeypatch) -> None:
    http, _ = client
    monkeypatch.setenv("AGENT_NEW_CORE", "1")

    class FakeCore:
        def __init__(self, *a, **kw) -> None:
            pass

        def run(self, intent, history=None):
            return AgentRunResult(status=RunStatus.BUDGET_EXHAUSTED, answer="")

    monkeypatch.setattr(agent_routes, "build_agent_core", lambda *a, **kw: FakeCore())
    resp = http.post(
        "/api/agent/ask/core",
        json={"prompt": "boucle", "session_id": "s-test"},
        headers=API_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


def test_ask_core_requires_api_key(client) -> None:
    http, _ = client
    resp = http.post(
        "/api/agent/ask/core",
        json={"prompt": "x", "session_id": "s"},
    )
    assert resp.status_code in (401, 403)
