"""
Tests offline des scénarios avancés de POST /api/agent/ask (package `api`).

Complète tests/test_api_ai_chat.py (cas basiques) avec l'auto-correction de
l'agent et la traduction des erreurs réseau en codes HTTP. Aucun appel
réseau : le LLM (Ollama) est remplacé par un FakeLLM scripté injecté dans
le cache `core.agent_cache`.
Lance avec : pytest tests/test_agent_api.py -v
"""

import os

# Config test AVANT tout import de l'app (clé API principale + port factice).
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")

import pytest  # noqa: E402
import requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import agent_cache  # noqa: E402  (insère ia/ dans sys.path)
from api import app as api_app  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}

client = TestClient(api_app)


class FakeLLM:
    """Remplace LLMClient.

    Réponses scriptées via `replies` (dépilées une par une), ou exception
    réseau simulée via `error` (Timeout, ConnectionError...).
    """

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.replies: list[str] = []
        self.error: Exception | None = None

    def call(self, messages):
        self.calls.append([dict(m) for m in messages])
        if self.error is not None:
            raise self.error
        assert self.replies, "FakeLLM interrogé sans réponse scriptée"
        return self.replies.pop(0)


@pytest.fixture()
def fake_llm(monkeypatch):
    """Injecte un FakeLLM dans le cache de l'agent (restauré après le test)."""
    llm = FakeLLM()
    monkeypatch.setattr(
        agent_cache, "_runner", agent_cache.AgentRunner(agent_cache.AgentCore(llm))
    )
    return llm


# --- Auto-correction -----------------------------------------------------------------


def test_ask_self_corrects_after_unknown_tool(fake_llm):
    """Le feedback d'auto-correction permet au LLM de corriger son appel."""
    fake_llm.replies = [
        '{"tool": "division", "args": {"a": 12, "b": 30}}',
        '{"tool": "add", "args": {"a": 12, "b": 30}}',
        "J'ai additionné les deux nombres avec l'outil add.",
    ]
    resp = client.post(
        "/api/agent/ask",
        json={"prompt": "calcule 12 + 30"},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    # Le tool corrigé a tourné -> explication finale scriptée.
    assert "additionné" in resp.json()["response"]

    # Le 2e appel LLM contenait bien le message de correction.
    feedback = fake_llm.calls[1][-1]["content"]
    assert "[auto-correction]" in feedback
    assert "Tool inconnu : 'division'." in feedback
    assert "Renvoie UN SEUL JSON corrigé" in feedback


def test_ask_reports_unusable_llm_answer(fake_llm):
    # MAX_TOOL_ROUNDS réponses sans aucun JSON : l'agent finit par exposer
    # l'échec au lieu de boucler indéfiniment (auto-correction épuisée).
    fake_llm.replies = [
        "Bonjour ! Pas de JSON ici.",
        "Toujours rien de JSON.",
        "Troisième essai infructueux.",
    ]
    resp = client.post(
        "/api/agent/ask",
        json={"prompt": "salut"},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    assert "Réponse non exploitable" in resp.json()["response"]
    assert len(fake_llm.calls) == 3


def test_ask_reports_unknown_tool_from_llm(fake_llm):
    # Le LLM insiste sur un outil inexistant pendant TOUS les tours de
    # correction : l'échec est exposé après épuisement des tentatives.
    fake_llm.replies = ['{"tool": "division", "args": {"a": 1}}'] * 3
    resp = client.post(
        "/api/agent/ask",
        json={"prompt": "divise 1 par 2"},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    assert "Tool inconnu" in resp.json()["response"]


# --- Erreurs réseau -> codes HTTP ------------------------------------------------------


def test_ask_llm_connection_error_returns_502(fake_llm):
    fake_llm.error = requests.exceptions.ConnectionError("connection refused")
    resp = client.post(
        "/api/agent/ask",
        json={"prompt": "salut"},
        headers=HEADERS,
    )

    assert resp.status_code == 502
    assert "injoignable" in resp.json()["detail"]


def test_ask_llm_timeout_returns_504(fake_llm):
    fake_llm.error = requests.exceptions.Timeout("too slow")
    resp = client.post(
        "/api/agent/ask",
        json={"prompt": "salut"},
        headers=HEADERS,
    )

    assert resp.status_code == 504
