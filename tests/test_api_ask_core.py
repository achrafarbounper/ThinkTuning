# project/tests/test_api_ask_core.py
"""Tests d'intégration de l'endpoint POST /api/agent/ask/core.

Le LLM est monkeypatché via la factory : AUCUN appel réseau. Vérifie la
bascule par flag, la réponse standard AskResponse et le mapping des statuts.
"""

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
