"""
Tests offline du streaming du mode Agent (/api/agent/ask/stream) et du
journal des exécutions (core/run_store).

Aucun réseau : la fonction `ask_agent_decision_streaming` est remplacée par
une version scriptée (monkeypatch) et le LLM reste factice ; la base SQLite
des runs est isolée dans tmp_path (AGENT_RUN_PATH / reset_run_store).
Lance avec : pytest tests/test_agent_runs_stream.py -v
"""

import os

# Config test AVANT tout import (le cache insère ia/ dans sys.path).
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")  # port factice

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402,F401  (initialise le job store avant le routage)
from api.routes import agent as agent_routes  # noqa: E402
from core import run_store  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    """TestClient avec une base de runs neuve par test."""
    run_store.reset_run_store(str(tmp_path / "agent_runs.db"))
    return TestClient(api.app)


HEADERS = {"X-API-Key": "test-key"}

# --- RunStore ----------------------------------------------------------------------


def test_run_store_lifecycle(tmp_path):
    store = run_store.RunStore(str(tmp_path / "runs.db"))

    row = store.start_run("Additionne 12+30", model="llama3.1:8b", source="ask-stream")
    assert row["status"] == run_store.RUNNING

    store.append_tool_event(row["id"], {"event": "tool_start", "tool": "add"})
    store.append_tool_event(
        row["id"],
        {
            "event": "tool_result",
            "tool": "add",
            "status": "ok",
            "summary": "42",
            "duration_ms": 1,
        },
    )
    store.finish_run(row["id"], run_store.COMPLETED, answer_summary="Le total est 42.")

    detail = store.get(row["id"])
    assert detail is not None
    assert detail["status"] == run_store.COMPLETED
    assert [t["event"] for t in detail["tools"]] == ["tool_start", "tool_result"]
    assert all("at" in t for t in detail["tools"]), "événements horodatés"

    rows = store.list()
    assert len(rows) == 1 and rows[0]["id"] == row["id"]
    # Filtre par outil (recherche dans le journal JSON des outils).
    assert store.list(tool="add")[0]["id"] == row["id"]
    assert store.list(tool="web_search") == []
    # Statut invalide refusé.
    with pytest.raises(ValueError):
        store.finish_run(row["id"], "n'importe quoi")


def test_run_store_get_unknown_returns_none(tmp_path):
    store = run_store.RunStore(str(tmp_path / "runs.db"))
    assert store.get("absent") is None
    assert store.list() == []


# --- POST /api/agent/ask/stream ----------------------------------------------------


def _fake_streaming_factory(events_log):
    """Fabrique un remplaçant scripté de ask_agent_decision_streaming."""

    def fake(
        prompt,
        model=None,
        enable_thinking=False,
        resume_request_id=None,
        on_thinking=None,
        on_tool_event=None,
        history_messages=None,
    ):
        events_log.append({"prompt": prompt, "model": model})
        if enable_thinking and on_thinking is not None:
            on_thinking("Je raisonne…")
        if on_tool_event is not None:
            on_tool_event(
                {
                    "event": "tool_start",
                    "tool": "calc",
                    "args": '{"expression": "12+30"}',
                }
            )
            on_tool_event(
                {
                    "event": "tool_result",
                    "tool": "calc",
                    "status": "ok",
                    "summary": "42",
                    "duration_ms": 2,
                }
            )
        return {
            "answer": "La somme est 42.",
            "thinking": "",
            "response": "La somme est 42.",
            "model": model or "llama3.1:8b",
            "status": "completed",
        }

    return fake


def test_ask_stream_emits_tool_events_then_final(client, monkeypatch):
    log: list[dict] = []
    monkeypatch.setattr(
        agent_routes, "ask_agent_decision_streaming", _fake_streaming_factory(log)
    )

    with client.stream(
        "POST", "/api/agent/ask/stream", headers=HEADERS,
        json={"prompt": "Calcule 12+30"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(chunk for chunk in response.iter_text())

    # La fonction remplacée a bien reçu le prompt.
    assert log[0]["prompt"] == "Calcule 12+30"

    # Séquence SSE attendue : outils -> delta(s) -> final -> DONE.
    assert '"tool_start"' in body and '"tool": "calc"' in body
    assert '"tool_result"' in body and '"status": "ok"' in body
    assert '"delta"' in body
    assert '"final"' in body and '"status": "completed"' in body
    assert body.rstrip().endswith("data: [DONE]")

    # Le run est journalisé et clôturé avec les deux événements d'outils.
    runs = run_store.get_run_store().list(limit=10)
    assert len(runs) == 1
    recorded = runs[0]
    assert recorded["source"] == "ask-stream"
    assert recorded["status"] == run_store.COMPLETED
    assert len(recorded["tools"]) == 2


def test_ask_stream_early_error_is_http(client, monkeypatch):
    """Une erreur précoce (resume_request_id invalide) reste une vraie HTTP 400."""

    def failing(*args, **kwargs):
        raise ValueError("Demande d'approbation introuvable : nope")

    monkeypatch.setattr(agent_routes, "ask_agent_decision_streaming", failing)

    response = client.post(
        "/api/agent/ask/stream", headers=HEADERS, json={"prompt": "relance"}
    )
    assert response.status_code == 400
    assert "introuvable" in response.json()["detail"]

    # Le run correspondant est clôturé en erreur (pas de ligne « running »).
    runs = run_store.get_run_store().list()
    assert all(r["status"] != run_store.RUNNING for r in runs)


def test_ask_requires_and_writes_run_journal(client, monkeypatch):
    """POST /api/agent/ask conserve son contrat ET alimente le journal des runs."""
    monkeypatch.setattr(
        agent_routes,
        "ask_agent_decision",
        lambda prompt, resume_request_id=None, **kwargs: {
            "response": "Fait.",
            "status": "completed",
        },
    )
    response = client.post("/api/agent/ask", headers=HEADERS, json={"prompt": "ok"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"

    runs = run_store.get_run_store().list()
    assert len(runs) == 1
    assert runs[0]["source"] == "ask"
    assert runs[0]["status"] == run_store.COMPLETED
    assert runs[0]["answer_summary"] == "Fait."


# --- GET /api/agent/runs ------------------------------------------------------------


def test_runs_endpoints(client):
    store = run_store.get_run_store()
    run_row = store.start_run("Analyse ce dataset", model="m", source="ask")
    store.append_tool_event(run_row["id"], {"event": "tool_start", "tool": "dataset_stats"})
    store.finish_run(run_row["id"], run_store.AWAITING_APPROVAL)

    listing = client.get("/api/agent/runs", headers=HEADERS)
    assert listing.status_code == 200
    body = listing.json()
    assert len(body["runs"]) == 1
    assert set(body["statuses"]) >= {"completed", "error", "running"}

    detail = client.get(f"/api/agent/runs/{run_row['id']}", headers=HEADERS)
    assert detail.status_code == 200
    assert detail.json()["tools"][0]["tool"] == "dataset_stats"

    missing = client.get("/api/agent/runs/does-not-exist", headers=HEADERS)
    assert missing.status_code == 404

    bad_status = client.get("/api/agent/runs", headers=HEADERS, params={"status": "bogus"})
    assert bad_status.status_code == 400

