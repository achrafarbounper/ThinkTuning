"""Tests du backend « Agent Flow Map » — persistance des sessions multi-agents.

Couvre ``core/flow_store.py`` et les endpoints :
    - GET  /api/agent/flow         liste des sessions (résumé + compteurs) ;
    - GET  /api/agent/flow/{id}    détail avec la timeline horodatée ;
    - DELETE /api/agent/flow/{id}  nettoyage ;
    - POST /api/agent/multi/ask/stream  enregistre automatiquement chaque
      événement SSE (mode full ET compact) puis clôture la session.

L'orchestration LLM est remplacée par un fake via ``monkeypatch`` sur
``ask_multi_agent_streaming`` (aucun réseau), comme ``test_agent_multi_api.py``.

Lance : pytest tests/test_agent_flow.py -v
"""

import os

os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import api.routes.agent as agent_routes  # noqa: E402
from app.agent.core import AgentRunResult, RunStatus  # noqa: E402
from core import flow_store as fs  # noqa: E402

API_KEY = "test-flow-key"
HEADERS = {"X-API-Key": API_KEY}


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch):
    """Pose API_KEY pendant le test puis restaure APRÈS (aucune pollution)."""
    monkeypatch.setenv("API_KEY", API_KEY)
    yield


@pytest.fixture()
def store(tmp_path):
    """FlowStore sur une base neuve par test (isolation)."""
    return fs.FlowStore(str(tmp_path / "flow.db"))


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """App FastAPI avec le routeur agent + un FlowStore isolé + un fake stream."""
    # L'endpoint lit le store partagé : on le pointe sur une base neuve.
    fs.reset_flow_store(str(tmp_path / "api_flow.db"))

    def _streaming(prompt, model=None, parallel=False, on_event=None):
        if on_event is not None:
            on_event("agent.plan", {"plan": [
                {"task_id": "task-1", "role": "web", "subtask": "cherche A"},
                {"task_id": "task-2", "role": "math", "subtask": "calcule B"},
            ]})
            on_event("agent.worker.start", {"task_id": "task-1", "role": "web"})
            on_event("agent.worker.tool", {"task_id": "task-1", "role": "web",
                                           "event": "tool_start", "tool": "web_search"})
            on_event("agent.worker.result", {"task_id": "task-1", "role": "web",
                                             "status": "ok", "summary": "INFO-WEB"})
            on_event("agent.synthesizing", {"worker_errors": 0})
        return {
            "status": "completed",
            "final_answer": "Réponse finale synthétisée.",
            "plan": [{"task_id": "task-1", "role": "web", "subtask": "cherche A"}],
            "workers": [], "unexecuted": [], "thinking": "", "duration_ms": 50.0,
        }

    monkeypatch.setattr(agent_routes, "ask_multi_agent_streaming", _streaming)

    app = FastAPI()
    app.include_router(agent_routes.router)
    return TestClient(app)


# --- Store -------------------------------------------------------------------


def test_store_roundtrip(store):
    row = store.start_flow("Analyse.", model="m:1")
    assert row["id"] and row["status"] == fs.RUNNING
    store.append_event(row["id"], "agent.plan", {"plan": []}, 12.0)
    store.append_event(row["id"], "agent.worker.tool", {"tool": "web_search"}, 45.0)
    store.finish_flow(row["id"], fs.COMPLETED, answer_summary="Résumé.")

    got = store.get(row["id"])
    assert got["status"] == fs.COMPLETED
    assert got["answer_summary"] == "Résumé."
    assert len(got["events"]) == 2
    assert got["events"][0]["event"] == "agent.plan"
    assert got["events"][0]["at_ms"] == 12.0

    listed = store.list(limit=10)
    assert listed[0]["id"] == row["id"]
    # La liste n'expose pas la timeline, mais fournit les compteurs.
    assert "events" not in listed[0]
    assert listed[0]["tool_calls"] == 1
    assert listed[0]["agents"] == []


def test_store_append_unknown_is_noop(store):
    store.append_event("inconnu", "agent.plan", {}, 0.0)  # ne doit pas lever
    assert store.get("inconnu") is None


def test_store_delete(store):
    row = store.start_flow("q")
    assert store.delete(row["id"]) is True
    assert store.delete(row["id"]) is False
    assert store.get(row["id"]) is None


def test_store_finish_invalid_status(store):
    row = store.start_flow("q")
    with pytest.raises(ValueError):
        store.finish_flow(row["id"], "n'importe")
# --- Endpoints ----------------------------------------------------------------


def test_stream_persists_flow(client):
    with client.stream(
        "POST", "/api/agent/multi/ask/stream",
        json={"prompt": "Analyse.", "mode": "full"}, headers=HEADERS,
    ) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode("utf-8", errors="replace")
    assert "agent.plan" in body
    assert "agent.worker.tool" in body  # mode full : observabilité diffusée

    flows = client.get("/api/agent/flow", headers=HEADERS).json()["flows"]
    assert len(flows) == 1
    flow = flows[0]
    assert flow["status"] == fs.COMPLETED
    assert flow["tool_calls"] == 1
    assert flow["agents"] == ["web"]

    detail = client.get(f"/api/agent/flow/{flow['id']}", headers=HEADERS).json()
    events = detail["events"]
    kinds = [e["event"] for e in events]
    assert "agent.plan" in kinds
    assert "agent.worker.tool" in kinds
    assert detail["answer_summary"] == "Réponse finale synthétisée."


def test_flow_list_and_detail_require_auth(client):
    assert client.get("/api/agent/flow").status_code == 401
    assert client.get("/api/agent/flow/abc").status_code == 401


def test_flow_delete_endpoint(client):
    # Crée une session via le flux, puis la supprime.
    with client.stream("POST", "/api/agent/multi/ask/stream",
                       json={"prompt": "q"}, headers=HEADERS):
        pass
    flow_id = client.get("/api/agent/flow", headers=HEADERS).json()["flows"][0]["id"]
    resp = client.delete(f"/api/agent/flow/{flow_id}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": flow_id}
    # 404 sur une session déjà supprimée.
    assert client.delete(f"/api/agent/flow/{flow_id}", headers=HEADERS).status_code == 404


def test_flow_get_unknown_returns_404(client):
    assert client.get("/api/agent/flow/inconnu", headers=HEADERS).status_code == 404


def test_flow_list_invalid_status(client):
    resp = client.get("/api/agent/flow?status=bogus", headers=HEADERS)
    assert resp.status_code == 400


def test_core_stream_persists_flow(client, monkeypatch):
    """Le run Noyau v2 (POST /api/agent/ask/core/stream) crée aussi une
    session « Flow Map » : événements core.start / core.tool / core.done
    enregistrés puis session clôturée en ``completed``."""
    monkeypatch.setattr(agent_routes, "new_core_enabled", lambda: True)
    monkeypatch.setattr(agent_routes, "agent_config", lambda: {"model": "fake-model"})

    class _FakeApprovalStore:
        def get(self, request_id):
            return None

    class _FakeRunStore:
        def start_run(self, prompt, model="", source=""):
            return {"id": "run-core"}

        def finish_run(self, rid, status, answer_summary="", error=None):
            pass

        def append_tool_event(self, rid, event):
            pass

    class _FakeCore:
        def __init__(self, event_bus=None, **kw):
            self._bus = event_bus

        def run(self, intent, history=None):
            if self._bus is not None:
                self._bus.emit("agent.tool_start", tool="web_search",
                               args={"q": "x"})
                self._bus.emit("agent.tool_end", tool="web_search",
                               status="ok", summary="OK", duration_ms=5.0)
            return AgentRunResult(answer="Réponse noyau.",
                                  status=RunStatus.COMPLETED,
                                  rounds_used=1, tool_calls_used=1)

    monkeypatch.setattr(agent_routes, "build_approval_store", lambda: _FakeApprovalStore())
    monkeypatch.setattr(agent_routes, "get_run_store", lambda: _FakeRunStore())
    monkeypatch.setattr(agent_routes, "build_agent_core", lambda **kw: _FakeCore(**kw))
    monkeypatch.setattr(agent_routes, "_audit_log", lambda *a, **k: None)
    monkeypatch.setattr(agent_routes, "_persist_exchange", lambda *a, **k: None)
    monkeypatch.setattr(agent_routes, "_load_session_history", lambda *a, **k: [])

    with client.stream("POST", "/api/agent/ask/core/stream",
                       json={"prompt": "Analyse noyau."}, headers=HEADERS) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode("utf-8", errors="replace")
    assert "core_tool" in body  # le SSE n'est pas altéré par la persistance

    flows = client.get("/api/agent/flow", headers=HEADERS).json()["flows"]
    assert len(flows) == 1
    flow = flows[0]
    assert flow["prompt"] == "Analyse noyau."
    assert flow["status"] == fs.COMPLETED
    # 1 appel d'outil = 1 : seuls les « tool_start » sont comptés, les
    # « tool_result » du noyau v2 ne doublent plus le compteur.
    assert flow["tool_calls"] == 1
    assert flow["agents"] == ["noyau"]

    detail = client.get(f"/api/agent/flow/{flow['id']}", headers=HEADERS).json()
    kinds = [e["event"] for e in detail["events"]]
    assert kinds[0] == "core.start"
    assert "core.tool" in kinds
    assert kinds[-1] == "core.done"
    assert detail["answer_summary"] == "Réponse noyau."
