"""Tests offline des routes HTTP de l'orcheration multi-agents.

Couvre ``POST /api/agent/multi/ask`` (bloquant) et
``POST /api/agent/multi/ask/stream`` (SSE). Le LLM est remplacé en mockant les
fonctions d'orchestration au niveau des routes (``ask_multi_agent`` /
``ask_multi_agent_streaming``) pour un test déterministe et sans réseau.

On vérifie :
    - contrat de sortie stable renvoyé par /multi/ask ;
    - mode « compact » : les événements d'observabilité sont filtrés du SSE ;
    - l'authentification est exigée (X-API-Key).

IMPORTANT (non-régression) : ce fichier ne modifie JAMAIS os.environ de façon
persistante. L'API_KEY est posée via une fixture ``monkeypatch.autouse``
restaurauée après CHAQUE test, pour ne pas polluer la suite complète (les
routes entraînement relisent API_KEY à chaque requête).

Lance avec : pytest tests/test_agent_multi_api.py -v
"""

import os

# Config LLM factice (port 9 : aucune connexion). L'API_KEY est gérée par la
# fixture monkeypatch autouse, PAS ici (éviter la pollution inter-tests).
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import api.routes.agent as agent_routes  # noqa: E402

API_KEY = "test-multi-key"
HEADERS = {"X-API-Key": API_KEY}


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch):
    """Pose AKEY pendant le test puis restaura l'environnement APRÈS — aucun
    effet de bord persistant sur les autres tests de la suite."""
    monkeypatch.setenv("API_KEY", API_KEY)
    yield


@pytest.fixture()
def client(monkeypatch):
    """App FastAPI avec le routeur agent, où l'orchestration multi est scripté."""
    _plan = [
        {"task_id": "task-1", "role": "web", "subtask": "cherche A"},
        {"task_id": "task-2", "role": "math", "subtask": "calcule B"},
    ]
    _workers = [
        {"task_id": "task-1", "role": "web", "status": "ok", "result": "INFO-WEB",
         "duration_ms": 100},
        {"task_id": "task-2", "role": "math", "status": "error",
         "error_code": "MaxRoundsExceeded", "message": "boucle", "duration_ms": 50},
    ]
    _outcome = {
        "status": "completed",
        "final_answer": "Réponse finale synthétisée.",
        "plan": _plan,
        "workers": _workers,
        "unexecuted": [_workers[1]],
        "thinking": "",
        "duration_ms": 150.0,
    }

    def _streaming(prompt, model=None, parallel=False, on_event=None):
        if on_event is not None:
            on_event("agent.plan", {"plan": _plan})
            on_event("agent.worker.start", {"task_id": "task-1", "role": "web"})
            on_event("agent.worker.tool", {"task_id": "task-1", "role": "web",
                                           "event": "tool_start", "tool": "web_search"})
            on_event("agent.worker.result", {"task_id": "task-1", "role": "web",
                                             "status": "ok", "summary": "INFO-WEB"})
            on_event("agent.synthesizing", {"worker_errors": 1})
        return _outcome

    monkeypatch.setattr(agent_routes, "ask_multi_agent", lambda *a, **k: _outcome)
    monkeypatch.setattr(agent_routes, "ask_multi_agent_streaming", _streaming)

    app = FastAPI()
    app.include_router(agent_routes.router)
    return TestClient(app)
def test_multi_ask_blocking_contract(client):
    resp = client.post("/api/agent/multi/ask",
                       json={"prompt": "Analyse et calcule."}, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    # Contrat de sortie stable.
    assert body["status"] == "completed"
    assert body["final_answer"] == "Réponse finale synthétisée."
    assert len(body["plan"]) == 2
    assert len(body["workers"]) == 2
    # Le bloc ``unexecuted`` est explicite.
    assert len(body["unexecuted"]) == 1
    assert body["unexecuted"][0]["role"] == "math"


def test_multi_ask_requires_api_key(client):
    resp = client.post("/api/agent/multi/ask", json={"prompt": "q"})
    assert resp.status_code == 401


def test_stream_compact_filters_observability_events(monkeypatch):
    """En mode compact, seuls plan / worker.start / worker.result / done
    (et non tool / synthesizing) sont diffusés."""
    _plan = [{"task_id": "task-1", "role": "web", "subtask": "cherche A"}]
    _outcome = {
        "status": "completed", "final_answer": "FINAL", "plan": [_plan],
        "workers": [{"task_id": "task-1", "role": "web", "status": "ok",
                     "result": "R", "duration_ms": 10}],
        "unexecuted": [], "thinking": "", "duration_ms": 10.0,
    }

    def _streaming(prompt, model=None, parallel=False, on_event=None):
        if on_event is not None:
            on_event("agent.plan", {"plan": _plan})
            on_event("agent.worker.start", {"task_id": "task-1", "role": "web"})
            on_event("agent.worker.tool", {"task_id": "task-1", "role": "web",
                                           "event": "tool_start", "tool": "web_search"})
            on_event("agent.worker.result", {"task_id": "task-1", "role": "web",
                                             "status": "ok", "summary": "R"})
            on_event("agent.synthesizing", {"worker_errors": 0})
        return _outcome

    monkeypatch.setattr(agent_routes, "ask_multi_agent_streaming", _streaming)

    app = FastAPI()
    app.include_router(agent_routes.router)
    client = TestClient(app)

    with client.stream("POST", "/api/agent/multi/ask/stream",
                       json={"prompt": "q", "mode": "compact"},
                       headers=HEADERS) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode("utf-8", errors="replace")

    # Les événements d'observabilité (tool / synthesizing) sont ABSENTS.
    assert "agent.worker.tool" not in body
    assert "agent.synthesizing" not in body
    # Les événements UX essentiels sont présents.
    assert "agent.plan" in body
    assert "agent.worker.start" in body
    assert "agent.worker.result" in body
    assert "agent.done" in body
