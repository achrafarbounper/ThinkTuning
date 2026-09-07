# project/tests/test_api_v1_sessions.py
"""Tests des endpoints sessions v1 (Phase 3d-4 — découplage agent/sessions).

Les routes v1 délèguent aux handlers legacy (parité par construction) ; les
tests substituent le store par un FAKE en mémoire via monkeypatch (même
technique que les tests legacy) : aucun fichier SQLite réel n'est touché.
"""

import os
import uuid

os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402, F401
from api import app  # noqa: E402

client = TestClient(app)
AUTH = {"X-API-Key": "test-key"}


class FakeSessionStore:
    """Miroir du contrat HTTP de core.session_store.SessionStore."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}

    def _row(self, session_id: str) -> dict:
        s = self.sessions[session_id]
        return {
            "id": session_id,
            "title": s["title"],
            "model": s["model"],
            "created_at": s["created_at"],
            "updated_at": s["updated_at"],
        }

    def create_session(self, title: str = "", model: str = "") -> dict:
        session_id = uuid.uuid4().hex[:12]
        now = "2026-01-01T00:00:00Z"
        self.sessions[session_id] = {
            "title": (title or "").strip() or f"Conversation du {now[:10]}",
            "model": model or "",
            "created_at": now,
            "updated_at": now,
        }
        return self._row(session_id)

    def get_session(self, session_id: str) -> dict | None:
        if session_id not in self.sessions:
            return None
        return self._row(session_id)

    def list_sessions(self, limit: int = 100) -> list[dict]:
        rows = [self._row(sid) for sid in list(self.sessions)[-limit:]]
        return sorted(rows, key=lambda r: r["updated_at"], reverse=True)

    def delete_session(self, session_id: str) -> bool:
        if session_id not in self.sessions:
            return False
        del self.sessions[session_id]
        self.messages.pop(session_id, None)
        return True

    def get_messages(self, session_id: str, limit: int = 200) -> list[dict]:
        return self.messages.get(session_id, [])[-limit:]


def _install_fake(monkeypatch) -> FakeSessionStore:
    fake = FakeSessionStore()
    monkeypatch.setattr("api.routes.sessions.get_session_store", lambda: fake)
    return fake


def test_list_sessions_is_public(monkeypatch):
    fake = _install_fake(monkeypatch)
    fake.create_session(title="Première")
    response = client.get("/api/v1/sessions")
    assert response.status_code == 200
    body = response.json()
    assert body["sessions"][0]["title"] == "Première"


def test_create_session_requires_api_key(monkeypatch):
    _install_fake(monkeypatch)
    assert client.post("/api/v1/sessions", json={"title": "T"}).status_code == 401


def test_session_lifecycle(monkeypatch):
    _install_fake(monkeypatch)
    created = client.post("/api/v1/sessions", json={"title": "Projet"}, headers=AUTH)
    assert created.status_code == 200
    session_id = created.json()["id"]
    assert session_id

    listed = client.get("/api/v1/sessions").json()["sessions"]
    assert any(s["id"] == session_id for s in listed)

    messages = client.get(f"/api/v1/sessions/{session_id}/messages", headers=AUTH)
    assert messages.status_code == 200
    assert messages.json()["messages"] == []

    deleted = client.delete(f"/api/v1/sessions/{session_id}", headers=AUTH)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    missing = client.delete(f"/api/v1/sessions/{session_id}", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_messages_of_missing_session_is_404(monkeypatch):
    _install_fake(monkeypatch)
    response = client.get("/api/v1/sessions/absent/messages")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
