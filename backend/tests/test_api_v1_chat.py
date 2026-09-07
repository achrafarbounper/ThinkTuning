# project/tests/test_api_v1_chat.py
"""Tests des endpoints chat v1 (Phase 3d-4 — découplage agent/sessions).

Les routes v1 délèguent aux handlers legacy ``api.routes.ai_chat`` (parité par
construction) ; les tests substituent les collaborateurs LLM par des fakes
(``list_llm_models``, ``ask_agent_detailed_streaming``) : aucun appel Ollama
réel n'est fait. La conversion d'erreur pré-stream 502 → enveloppe v1
(``llm_client_error``) est vérifiée.
"""

import os

os.environ.setdefault("API_KEY", "test-key")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402, F401
from api import app  # noqa: E402

client = TestClient(app)
AUTH = {"X-API-Key": "test-key"}


def test_list_llm_models(monkeypatch):
    monkeypatch.setattr(
        "api.routes.ai_chat.list_llm_models",
        lambda: {
            "active": "qwen2.5",
            "models": [
                {
                    "name": "qwen2.5",
                    "size": 5_000_000_000,
                    "modified_at": "2026-01-01T00:00:00Z",
                    "is_default": True,
                }
            ],
        },
    )
    response = client.get("/api/v1/chat/models", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["active"] == "qwen2.5"
    assert body["models"][0]["name"] == "qwen2.5"


def test_list_llm_models_requires_key(monkeypatch):
    monkeypatch.setattr("api.routes.ai_chat.list_llm_models", lambda: {})
    assert client.get("/api/v1/chat/models").status_code == 401


def test_ai_chat_requires_key(monkeypatch):
    monkeypatch.setattr(
        "api.routes.ai_chat.ask_agent_detailed_streaming",
        lambda *args, **kwargs: {"answer": "x", "thinking": ""},
    )
    assert client.post("/api/v1/chat/ai", json={"message": "salut"}).status_code == 401


def test_ai_chat_streams_answer_sse(monkeypatch):
    monkeypatch.setattr(
        "api.routes.ai_chat.ask_agent_detailed_streaming",
        lambda prompt, model, enable_thinking, on_thinking=None: {
            "answer": "Bonjour",
            "thinking": "",
        },
    )
    response = client.post(
        "/api/v1/chat/ai", json={"message": "salut", "enable_thinking": False}, headers=AUTH
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert '"delta"' in response.text
    assert "data: [DONE]" in response.text


def test_ai_chat_streams_thinking_sse(monkeypatch):
    def _fake_ask(prompt, model, enable_thinking, on_thinking=None):
        if enable_thinking and on_thinking:
            on_thinking("raisonnement ")
        return {"answer": "Oui", "thinking": "raisonnement "}

    monkeypatch.setattr("api.routes.ai_chat.ask_agent_detailed_streaming", _fake_ask)
    response = client.post(
        "/api/v1/chat/ai", json={"message": "question", "enable_thinking": True}, headers=AUTH
    )
    assert response.status_code == 200
    assert '"thinking_delta"' in response.text
    assert '"delta"' in response.text


def test_ai_chat_maps_prestream_failure_to_502(monkeypatch):
    def _boom(*args, **kwargs):
        raise HTTPException(status_code=502, detail="provider down")

    monkeypatch.setattr("api.routes.ai_chat.ask_agent_detailed_streaming", _boom)
    response = client.post("/api/v1/chat/ai", json={"message": "salut"}, headers=AUTH)
    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "llm_client_error"
    assert "provider down" in body["error"]["message"]
