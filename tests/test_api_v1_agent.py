# project/tests/test_api_v1_agent.py
"""Tests des endpoints agent v1 (Phase 3d-4 — découplage agent/sessions).

Les routes v1 délèguent aux handlers legacy (parité par construction) ; les
tests font un double contrôle :
  - posture d'auth (401 sans X-API-Key) ;
  - conversion des erreurs legacy en enveloppe v1 ``{"error": {...}}``
    (flag noyau off → 503 service_unavailable, statut inconnu → 400
    bad_request, ressources absentes → 404 not_found, état incohérent →
    409 conflict) ;
  - happy paths avec stores/collaborateurs fake (même technique que les
    tests legacy : monkeypatch des globals du module `api.routes.agent`).
"""

import os

os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402, F401
from api import app  # noqa: E402

client = TestClient(app)
AUTH = {"X-API-Key": "test-key"}


# --- Fakes ------------------------------------------------------------------


class FakeApprovalStore:
    """Miroir du contrat HTTP de core.approval_store.ApprovalStore."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def create(self, request_id: str, tool: str = "read_file", args_hash: str = "h") -> dict:
        row = {
            "request_id": request_id,
            "prompt": "prompt",
            "tool": tool,
            "args": {},
            "args_hash": args_hash,
            "status": "pending",
            "created_at": "2026-01-01T00:00:00Z",
            "decided_at": None,
            "decided_by": None,
        }
        self.rows[request_id] = row
        return row

    def list(self, status: str | None = None) -> list[dict]:
        rows = list(reversed(list(self.rows.values())))  # récentes d'abord
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        return rows

    def get(self, request_id: str) -> dict | None:
        return self.rows.get(request_id)

    def approve(self, request_id: str, decided_by: str | None = None) -> dict | None:
        row = self.rows.get(request_id)
        if row is None:
            return None
        if row["status"] == "pending":
            row["status"] = "approved"
        return row

    def reject(self, request_id: str, decided_by: str | None = None) -> dict | None:
        row = self.rows.get(request_id)
        if row is None:
            return None
        if row["status"] == "pending":
            row["status"] = "rejected"
        return row


class FakeFlowStore:
    """Miroir du contrat HTTP de core.flow_store.FlowStore."""

    def __init__(self) -> None:
        self.flows: dict[str, dict] = {}

    def start_flow(self, prompt: str, model: str) -> dict:
        flow_id = f"flow-{len(self.flows) + 1}"
        self.flows[flow_id] = {
            "id": flow_id,
            "prompt": prompt,
            "model": model,
            "status": "running",
            "answer_summary": "",
            "error": None,
            "created_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
            "tool_calls": 0,
            "agents": [],
        }
        return self.flows[flow_id]

    def list(self, limit: int = 50, status: str | None = None) -> list[dict]:
        rows = list(self.flows.values())
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        return rows[-limit:]

    def get(self, flow_id: str) -> dict | None:
        return self.flows.get(flow_id)

    def delete(self, flow_id: str) -> bool:
        return self.flows.pop(flow_id, None) is not None


class _AskOutcome:
    """Objet de retour minimal du fake ``run_ask_core`` (mêmes attributs que
    l'objet réel consommé pour construire ``AskResponse``)."""

    def __init__(self) -> None:
        self.answer = "Réponse du noyau"
        self.model = "qwen2.5"
        self.api_status = "completed"
        self.request_id = "req-1"
        self.approval = None
# --- Paramètres (settings) ---------------------------------------------------


def test_read_settings_requires_key(monkeypatch):
    monkeypatch.setattr("api.routes.agent._settings_payload", lambda: {"settings": {}})
    assert client.get("/api/v1/agent/settings").status_code == 401


def test_read_settings(monkeypatch):
    monkeypatch.setattr(
        "api.routes.agent._settings_payload",
        lambda: {"settings": {"provider": "ollama"}, "reload_ok": True},
    )
    response = client.get("/api/v1/agent/settings", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["settings"]["provider"] == "ollama"


def test_update_settings(monkeypatch):
    saved: dict[str, object] = {}

    def _fake_save(values: dict) -> None:
        saved.update(values)

    monkeypatch.setattr("api.routes.agent.save_agent_settings", _fake_save)
    monkeypatch.setattr("api.routes.agent.reload_agent_runner", lambda: None)
    monkeypatch.setattr(
        "api.routes.agent._settings_payload",
        lambda: {"settings": {"provider": "ollama"}},
    )

    response = client.put(
        "/api/v1/agent/settings", json={"provider": "ollama"}, headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reload_ok"] is True
    assert saved["provider"] == "ollama"


def test_update_settings_rejects_invalid_value(monkeypatch):
    def _boom(values: dict) -> None:
        raise ValueError("Provider inconnu : 'toaster'")

    monkeypatch.setattr("api.routes.agent.save_agent_settings", _boom)

    response = client.put(
        "/api/v1/agent/settings", json={"provider": "toaster"}, headers=AUTH
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


# --- Noyau agentique (ask/core) ----------------------------------------------


def test_ask_core_requires_key(monkeypatch):
    monkeypatch.setattr("api.routes.agent.new_core_enabled", lambda: True)
    assert client.post("/api/v1/agent/ask/core", json={"prompt": "salut"}).status_code == 401


def test_ask_core_flag_off_is_503(monkeypatch):
    monkeypatch.setattr("api.routes.agent.new_core_enabled", lambda: False)
    response = client.post(
        "/api/v1/agent/ask/core", json={"prompt": "salut"}, headers=AUTH
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_ask_core_stream_flag_off_is_503(monkeypatch):
    monkeypatch.setattr("api.routes.agent.new_core_enabled", lambda: False)
    response = client.post(
        "/api/v1/agent/ask/core/stream", json={"prompt": "salut"}, headers=AUTH
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_ask_core_happy_path(monkeypatch):
    monkeypatch.setattr("api.routes.agent.new_core_enabled", lambda: True)
    monkeypatch.setattr("api.routes.agent.run_ask_core", lambda **kwargs: _AskOutcome())

    response = client.post(
        "/api/v1/agent/ask/core", json={"prompt": "salut"}, headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "Réponse du noyau"
    assert body["status"] == "completed"
    assert body["request_id"] == "req-1"
# --- Approbations ------------------------------------------------------------


def test_approvals_require_key(monkeypatch):
    monkeypatch.setattr("api.routes.agent.get_approval_store", lambda: FakeApprovalStore())
    assert client.get("/api/v1/agent/approvals").status_code == 401


def test_approvals_list_and_filter(monkeypatch):
    store = FakeApprovalStore()
    store.create("a1")
    store.create("a2")
    monkeypatch.setattr("api.routes.agent.get_approval_store", lambda: store)

    response = client.get("/api/v1/agent/approvals", headers=AUTH)
    assert response.status_code == 200
    assert [a["request_id"] for a in response.json()["approvals"]] == ["a2", "a1"]

    filtered = client.get("/api/v1/agent/approvals?status=pending", headers=AUTH)
    assert len(filtered.json()["approvals"]) == 2


def test_approvals_unknown_status_is_400(monkeypatch):
    store = FakeApprovalStore()
    monkeypatch.setattr("api.routes.agent.get_approval_store", lambda: store)
    response = client.get("/api/v1/agent/approvals?status=bogus", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_approve_flow(monkeypatch):
    store = FakeApprovalStore()
    store.create("a1")
    monkeypatch.setattr("api.routes.agent.get_approval_store", lambda: store)

    response = client.post("/api/v1/agent/approvals/a1/approve", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    # Déjà rejetée puis re-approbation → conflit (état non pending).
    store.create("a2")
    client.post("/api/v1/agent/approvals/a2/reject", headers=AUTH)
    conflict = client.post("/api/v1/agent/approvals/a2/approve", headers=AUTH)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"

    missing = client.post("/api/v1/agent/approvals/nope/approve", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_reject_flow(monkeypatch):
    store = FakeApprovalStore()
    store.create("a1")
    monkeypatch.setattr("api.routes.agent.get_approval_store", lambda: store)

    response = client.post("/api/v1/agent/approvals/a1/reject", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


# --- Flow Map (sessions multi-agents) ----------------------------------------


def test_flow_requires_key(monkeypatch):
    monkeypatch.setattr("api.routes.agent.get_flow_store", lambda: FakeFlowStore())
    assert client.get("/api/v1/agent/flow").status_code == 401


def test_flow_list_and_detail(monkeypatch):
    store = FakeFlowStore()
    store.start_flow("Analyse le sujet", "qwen2.5")
    monkeypatch.setattr("api.routes.agent.get_flow_store", lambda: store)

    listed = client.get("/api/v1/agent/flow", headers=AUTH)
    assert listed.status_code == 200
    body = listed.json()
    assert len(body["flows"]) == 1
    assert body["flows"][0]["prompt"] == "Analyse le sujet"
    assert "statuses" in body

    flow_id = body["flows"][0]["id"]
    detail = client.get(f"/api/v1/agent/flow/{flow_id}", headers=AUTH)
    assert detail.status_code == 200
    assert detail.json()["id"] == flow_id


def test_flow_validation_and_missing(monkeypatch):
    store = FakeFlowStore()
    monkeypatch.setattr("api.routes.agent.get_flow_store", lambda: store)

    bad_limit = client.get("/api/v1/agent/flow?limit=9999", headers=AUTH)
    assert bad_limit.status_code == 422
    assert bad_limit.json()["error"]["code"] == "validation_error"

    missing = client.get("/api/v1/agent/flow/absent", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
