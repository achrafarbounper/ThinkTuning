# project/tests/test_api_ai_chat.py

"""Tests offline du chat IA (/api/ai) et des endpoints agent (/api/agent/*)
intégrés au package `api`.

Aucun appel réseau : le LLM (Ollama) est remplacé par un FakeLLM scripté,
injecté dans le cache `core.agent_cache`.
Lance avec : pytest tests/test_api_ai_chat.py -v
"""

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402
import requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import agent_cache  # noqa: E402  (insère ia/ dans sys.path)
from api import app  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}

client = TestClient(app)


class FakeLLM:
    """Remplace LLMClient (cf. tests/test_agent_api.py).

    Comportement par défaut calqué sur AgentCore :
      1er appel -> bloc JSON d'appel d'outil ;
      2e appel (prompt commençant par 'Dernier résultat') -> explication finale.
    """

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.responses: list[str] = []
        self.replies: list[str] = []
        self.error: Exception | None = None

    def call(self, messages):
        self.calls.append([dict(m) for m in messages])
        if self.error is not None:
            raise self.error
        if self.replies:
            answer = self.replies.pop(0)
            self.responses.append(answer)
            return answer
        last = messages[-1]["content"]
        if last.startswith("Dernier résultat"):
            answer = "J'ai additionné les deux nombres avec l'outil add."
        else:
            answer = '{"tool": "add", "args": {"a": 12, "b": 30}}'
        self.responses.append(answer)
        return answer




# --- POST /api/ai (chat SSE du dashboard) -----------------------------------------


def test_ai_streams_sse_deltas_then_done(fake_llm):
    # Flux réel de l'agent : 1er appel = planification JSON d'un outil,
    # 2e appel = explication finale qui sera diffusée en SSE.
    fake_llm.replies = [
        '{"tool": "add", "args": {"a": 12, "b": 30}}',
        "Bonjour cher utilisateur.",
    ]
    resp = client.post("/api/ai", json={"message": "salut"}, headers=HEADERS)

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert 'data: {"delta": "Bonjour ' in resp.text
    assert '"delta"' in resp.text
    assert "data: [DONE]" in resp.text
    # Deux appels LLM : planification JSON puis réponse finale en SSE.
    assert len(fake_llm.calls) == 2


def test_ai_replays_recent_history_in_prompt(fake_llm):
    fake_llm.replies = ["D'accord."]
    payload = {
        "message": "et maintenant ?",
        "history": [
            {"role": "user", "content": "Quel temps fait-il ?"},
            {"role": "assistant", "content": "Il fait beau."},
        ],
    }
    resp = client.post("/api/ai", json=payload, headers=HEADERS)

    assert resp.status_code == 200, resp.text
    prompt = fake_llm.calls[0][-1]["content"]
    assert "Quel temps fait-il ?" in prompt
    assert "Il fait beau." in prompt
    assert "et maintenant ?" in prompt


def test_ai_empty_message_rejected(fake_llm):
    resp = client.post("/api/ai", json={"message": ""}, headers=HEADERS)
    assert resp.status_code == 422


def test_ai_requires_api_key(fake_llm):
    resp = client.post("/api/ai", json={"message": "salut"})
    assert resp.status_code == 401


def test_ai_connection_error_returns_502(fake_llm):
    fake_llm.error = requests.exceptions.ConnectionError("connection refused")
    resp = client.post("/api/ai", json={"message": "salut"}, headers=HEADERS)

    assert resp.status_code == 502
    assert "injoignable" in resp.json()["detail"]


def test_ai_timeout_returns_504(fake_llm):
    fake_llm.error = requests.exceptions.Timeout("too slow")
    resp = client.post("/api/ai", json={"message": "salut"}, headers=HEADERS)

    assert resp.status_code == 504


# --- GET /api/agent/status ---------------------------------------------------------


def test_agent_status_is_public_and_informative(fake_llm):
    resp = client.get("/api/agent/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["tools"] == sorted(agent_cache.TOOLS)
    assert "add" in body["tools"]
    assert body["model"]


# --- Auth des routes protégées ------------------------------------------------------


def test_agent_protected_routes_reject_missing_key():
    assert client.get("/api/agent/tools").status_code == 401
    assert (
        client.post("/api/agent/tools/run", json={"tool": "add", "args": {}}).status_code
        == 401
    )
    assert client.post("/api/agent/ask", json={"prompt": "salut"}).status_code == 401
@pytest.fixture()
def fake_llm(monkeypatch):
    """Injecte un FakeLLM dans le cache de l'agent (restauré après le test)."""
    llm = FakeLLM()
    monkeypatch.setattr(
        agent_cache, "_runner", agent_cache.AgentRunner(agent_cache.AgentCore(llm))
    )
    return llm


# --- GET /api/agent/tools -----------------------------------------------------------


def test_list_tools_returns_required_args(fake_llm):
    resp = client.get("/api/agent/tools", headers=HEADERS)

    assert resp.status_code == 200
    tools = {item["name"]: item["required_args"] for item in resp.json()}
    assert tools["add"] == ["a", "b"]
    assert tools["write_file"] == ["filename", "content"]


def test_list_tools_exposes_json_description_and_parameters(fake_llm):
    """Les métadonnées déclaratives (tools_config.json) sont exposées par l'API."""
    resp = client.get("/api/agent/tools", headers=HEADERS)

    assert resp.status_code == 200
    by_name = {item["name"]: item for item in resp.json()}
    calc = by_name["calc"]
    assert "expression" in calc["parameters"]
    assert calc["parameters"]["expression"]["required"] is True
    assert calc["description"]  # description non vide issue du JSON


# --- POST /api/agent/tools/run --------------------------------------------------------


def test_run_tool_add_directly(fake_llm):
    resp = client.post(
        "/api/agent/tools/run",
        json={"tool": "add", "args": {"a": 12, "b": 30}},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    assert resp.json() == {"tool": "add", "result": 42.0}


def test_run_tool_unknown_returns_400(fake_llm):
    resp = client.post(
        "/api/agent/tools/run",
        json={"tool": "division", "args": {}},
        headers=HEADERS,
    )

    assert resp.status_code == 400
    assert "Tool inconnu" in resp.json()["detail"]


def test_run_tool_missing_args_returns_400(fake_llm):
    resp = client.post(
        "/api/agent/tools/run",
        json={"tool": "add", "args": {"a": 1}},
        headers=HEADERS,
    )

    assert resp.status_code == 400
    assert "'b'" in resp.json()["detail"]


# --- POST /api/agent/ask -----------------------------------------------------------------


def test_ask_executes_tool_then_explains(fake_llm):
    resp = client.post(
        "/api/agent/ask",
        json={"prompt": "Additionne 12 + 30 avec l'outil add."},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["model"]
    assert "additionné" in body["response"]
    # Deux appels LLM : planification JSON puis explication finale.
    assert len(fake_llm.calls) == 2
    assert fake_llm.calls[1][-1]["content"].startswith("Dernier résultat")
    assert "42" in fake_llm.calls[1][-1]["content"]


def test_ask_empty_prompt_rejected(fake_llm):
    resp = client.post("/api/agent/ask", json={"prompt": ""}, headers=HEADERS)
    assert resp.status_code == 422
