# project/tests/test_api_llm_models.py

"""Tests offline de GET /api/models et de la sélection de modèle du chat.

Aucun appel réseau : les réponses d'Ollama (/api/tags) sont simulées par un
monkeypatch de ``requests.get`` dans ``core.agent_cache``, et le LLM par un
FakeLLM injecté dans le cache (cf. tests/test_api_ai_chat.py).
Lance avec : pytest tests/test_api_llm_models.py -v
"""

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402
import requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import app  # noqa: E402
from core import agent_cache  # noqa: E402  (point d'entrée historique, plus de hack sys.path)

HEADERS = {"X-API-Key": "test-key"}

client = TestClient(app)


class FakeLLM:
    """Remplace LLMClient : suit le protocole de l'agent (JSON puis réponse)."""

    def __init__(self):
        self.calls: list[list[dict]] = []

    def call(self, messages):
        self.calls.append([dict(m) for m in messages])
        last = messages[-1]["content"]
        if last.startswith("Dernier résultat"):
            return "Réponse finale de l'assistant."
        return '{"tool": "add", "args": {"a": 1, "b": 2}}'


class FakeTagsResponse:
    """Réponse factice de GET /api/tags côté Ollama."""

    def __init__(self, payload=None, status_error=None):
        self.payload = payload or {"models": []}
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error is not None:
            response = requests.Response()
            response.status_code = self.status_error
            raise requests.exceptions.HTTPError(response=response)

    def json(self):
        return self.payload


@pytest.fixture()
def fake_tags(monkeypatch):
    """Intercepte requests.get dans core.agent_cache (jamais de vrai réseau)."""
    captured = {}

    def install(response=None, exc=None):
        def fake_get(url, timeout=None):
            captured["url"] = url
            captured["timeout"] = timeout
            if exc is not None:
                raise exc
            return response

        monkeypatch.setattr(agent_cache.requests, "get", fake_get)
        return captured

    return install


def _tags_payload():
    return {
        "models": [
            {
                "name": "qwen2.5:7b",
                "size": 4700000000,
                "modified_at": "2026-08-20T10:00:00Z",
            },
            {
                "name": "llama3.1:8b",
                "size": 4900000000,
                "modified_at": "2026-08-21T09:00:00Z",
            },
        ]
    }


# --- GET /api/models ---------------------------------------------------------------


def test_models_requires_api_key():
    assert client.get("/api/models").status_code == 401


def test_models_lists_ollama_models_with_default_marker(fake_tags):
    captured = fake_tags(response=FakeTagsResponse(payload=_tags_payload()))

    resp = client.get("/api/models", headers=HEADERS)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"] == agent_cache.agent_config()["model"]
    assert [m["name"] for m in body["models"]] == ["llama3.1:8b", "qwen2.5:7b"]
    # Le modèle configuré côté serveur est marqué comme défaut.
    defaults = [m for m in body["models"] if m["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["name"] == "llama3.1:8b"
    # La liste provient bien du endpoint tags de la racine Ollama déduite.
    assert captured["url"].endswith("/api/tags")


def test_models_connection_error_returns_502(fake_tags):
    fake_tags(exc=requests.exceptions.ConnectionError("connection refused"))

    resp = client.get("/api/models", headers=HEADERS)

    assert resp.status_code == 502
    assert "injoignable" in resp.json()["detail"]


def test_models_timeout_returns_504(fake_tags):
    fake_tags(exc=requests.exceptions.Timeout("too slow"))

    resp = client.get("/api/models", headers=HEADERS)

    assert resp.status_code == 504


# --- Sélection de modèle dans POST /api/ai ------------------------------------------


@pytest.fixture()
def fake_override_llm():
    """Pré-remplit le cache des runners surchargés avec un FakeLLM dédié."""
    llm = FakeLLM()
    runner = agent_cache.AgentRunner(agent_cache.AgentCore(llm))
    previous = dict(agent_cache._override_runners)
    agent_cache._override_runners["custom-model:latest"] = runner
    yield llm
    agent_cache._override_runners.clear()
    agent_cache._override_runners.update(previous)


def test_ai_uses_selected_model_from_request(fake_override_llm):
    resp = client.post(
        "/api/ai",
        json={"message": "salut", "model": "custom-model:latest"},
        headers=HEADERS,
    )

    assert resp.status_code == 200, resp.text
    # Le flux SSE découpe la réponse par mots : on vérifie le dernier fragment
    # et surtout que le runner surchargé (modèle demandé) a bien servi la requête.
    assert "l'assistant." in resp.text
    assert len(fake_override_llm.calls) >= 2


def test_ai_without_model_keeps_default_runner(monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr(
        agent_cache, "_runner", agent_cache.AgentRunner(agent_cache.AgentCore(llm))
    )
    resp = client.post(
        "/api/ai",
        json={"message": "salut", "model": ""},
        headers=HEADERS,
    )

    assert resp.status_code == 200, resp.text
    assert len(llm.calls) >= 1
