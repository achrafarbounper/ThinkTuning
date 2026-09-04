"""
Tests offline du streaming du noyau v2 (/api/agent/ask/core et
/api/agent/ask/core/stream) et du journal des exécutions (core/run_store).

Aucun réseau : le noyau est remplacé par une version scriptée (monkeypatch
de ``build_agent_core``) ; la base SQLite des runs est isolée dans tmp_path
(AGENT_RUN_PATH / reset_run_store).
Lance avec : pytest tests/test_agent_runs_stream.py -v
"""

import os

# Config test AVANT tout import de l'application.
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")  # port factice

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402,F401  (initialise le job store avant le routage)
from api.routes import agent as agent_routes  # noqa: E402
from app.agent.core import AgentRunResult, RunStatus  # noqa: E402
from core import run_store  # noqa: E402
from fastapi import HTTPException  # noqa: E402


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


# --- POST /api/agent/ask/core (+ /core/stream) ---------------------------------------


def _fake_core_factory(answer="Le total est 42.", tools=True):
    """Fabrique un noyau v2 scripté publiant son cycle de vie sur le bus injecté."""

    class FakeCore:
        def __init__(self, *args, **kwargs):
            # Le noyau publie ses événements sur le bus par-run injecté ; la
            # route s'abonne au port pour régénérer les frames SSE.
            self._event_bus = kwargs.get("event_bus")

        def run(self, intent, history=None):
            if self._event_bus is not None and tools:
                self._event_bus.emit(
                    "agent.tool_start", tool="calc", args={"expression": "12+30"}
                )
                self._event_bus.emit(
                    "agent.tool_end", tool="calc", status="ok",
                    summary="42", duration_ms=2,
                )
            return AgentRunResult(
                answer=answer,
                status=RunStatus.COMPLETED,
                rounds_used=1,
                tool_calls_used=1 if tools else 0,
            )

    return FakeCore


def test_core_stream_emits_tool_events_then_final(client, monkeypatch):
    monkeypatch.setattr(agent_routes, "build_agent_core", _fake_core_factory())

    with client.stream(
        "POST", "/api/agent/ask/core/stream", headers=HEADERS,
        json={"prompt": "Calcule 12+30"},
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(chunk for chunk in response.iter_text())

    # Séquence SSE attendue : core_tool -> delta(s) -> final -> DONE.
    assert '"core_tool"' in body and '"tool_start"' in body
    assert '"tool_result"' in body and '"status": "ok"' in body
    assert '"delta"' in body
    assert '"final"' in body and '"status": "completed"' in body
    assert body.rstrip().endswith("data: [DONE]")

    # Le run est journalisé et clôturé avec les deux événements d'outils.
    runs = run_store.get_run_store().list(limit=10)
    assert len(runs) == 1
    recorded = runs[0]
    assert recorded["source"] == "ask_core_stream"
    assert recorded["status"] == run_store.COMPLETED
    assert len(recorded["tools"]) == 2


def test_core_stream_early_error_is_http(client, monkeypatch):
    """Une erreur précoce (noyau injoignable) reste une vraie erreur HTTP."""

    def failing_build(*args, **kwargs):
        raise HTTPException(
            status_code=400, detail="Demande d'approbation introuvable : nope"
        )

    monkeypatch.setattr(agent_routes, "build_agent_core", failing_build)

    response = client.post(
        "/api/agent/ask/core/stream", headers=HEADERS, json={"prompt": "relance"}
    )
    assert response.status_code == 400
    assert "introuvable" in response.json()["detail"]

    # Le run correspondant est clôturé en erreur (pas de ligne « running »).
    runs = run_store.get_run_store().list()
    assert all(r["status"] != run_store.RUNNING for r in runs)


def test_ask_core_writes_run_journal(client, monkeypatch):
    """POST /api/agent/ask/core conserve son contrat ET alimente le journal."""
    monkeypatch.setattr(
        agent_routes,
        "build_agent_core",
        _fake_core_factory(answer="Fait.", tools=False),
    )
    response = client.post("/api/agent/ask/core", headers=HEADERS, json={"prompt": "ok"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"

    runs = run_store.get_run_store().list()
    assert len(runs) == 1
    assert runs[0]["source"] == "ask_core"
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

