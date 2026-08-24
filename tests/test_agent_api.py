"""
Tests offline de l'API agent (`ia/api_server.py`).

Aucun appel réseau : le LLM (Ollama) est remplacé par un FakeLLM scripté.
Lance avec : pytest tests/test_agent_api.py -v
"""

import os

# Config test AVANT tout import de l'app (auth activée pour couvrir la 401).
os.environ["AGENT_API_KEY"] = "test-agent-key"
os.environ["AGENT_OLLAMA_URL"] = "http://127.0.0.1:9/api/chat"  # port factice

import pytest
import requests
from fastapi.testclient import TestClient

from ia import api_server
from ia.agent.agent_core import AgentCore
from ia.agent.runner import AgentRunner
from ia.tools.tool_registry import TOOLS as ALL_TOOLS


class FakeLLM:
    """Remplace LLMClient.

    Comportement par défaut calqué sur AgentCore :
      1er appel -> renvoie un bloc JSON d'appel d'outil ;
      2e appel (prompt commençant par 'Dernier résultat') -> explication finale.
    `replies` permet de scripter des réponses arbitraires, `error` de simuler
    une exception réseau (Timeout, ConnectionError...).
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


@pytest.fixture()
def fake_llm():
    return FakeLLM()


@pytest.fixture()
def client(fake_llm, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "test-agent-key")
    monkeypatch.setattr(
        api_server, "build_runner", lambda: AgentRunner(AgentCore(fake_llm))
    )
    with TestClient(api_server.app) as test_client:  # context manager -> lifespan
        yield test_client


# --- /health -------------------------------------------------------------------

def test_health_is_public_and_informative(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # La liste des outils est générée depuis le registre : on la compare à la source de vérité.
    assert body["tools"] == sorted(ALL_TOOLS)
    assert "add" in body["tools"] and "write_file" in body["tools"]
    assert body["auth_enabled"] is True


def test_health_works_without_api_key_configured(client, monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["auth_enabled"] is False


# --- Auth ----------------------------------------------------------------------

def test_protected_routes_reject_missing_key(client):
    assert client.get("/tools").status_code == 401
    assert client.post("/ask", json={"prompt": "salut"}).status_code == 401
    assert (
        client.post("/tools/run", json={"tool": "add", "args": {}}).status_code == 401
    )


def test_auth_disabled_when_env_var_unset(client, monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    resp = client.get("/tools")
    assert resp.status_code == 200


# --- /tools --------------------------------------------------------------------

def test_list_tools(client):
    resp = client.get("/tools", headers={"X-API-Key": "test-agent-key"})
    assert resp.status_code == 200
    tools = resp.json()
    by_name = {t["name"]: t["required_args"] for t in tools}
    # Le registre est la source de vérité : /tools doit le refléter en entier.
    assert set(by_name) == set(ALL_TOOLS)
    assert by_name["add"] == ["a", "b"]
    assert by_name["write_file"] == ["filename", "content"]
    assert by_name["gpu_info"] == []
    assert by_name["docker_exec"] == ["container", "command"]


# --- /tools/run ------------------------------------------------------------------

def test_run_tool_add_directly(client):
    resp = client.post(
        "/tools/run",
        json={"tool": "add", "args": {"a": 12, "b": 30}},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"tool": "add", "result": 42.0}


def test_run_tool_unknown_returns_400(client):
    resp = client.post(
        "/tools/run",
        json={"tool": "division", "args": {}},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert resp.status_code == 400
    assert "Tool inconnu" in resp.json()["detail"]


def test_run_tool_missing_args_returns_400(client):
    resp = client.post(
        "/tools/run",
        json={"tool": "add", "args": {"a": 1}},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert resp.status_code == 400
    assert "'b'" in resp.json()["detail"]


# --- /ask ------------------------------------------------------------------------

def test_ask_executes_tool_then_explains(client, fake_llm):
    resp = client.post(
        "/ask",
        json={"prompt": "Additionne 12 + 30 avec l'outil add."},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"]
    assert "additionné" in body["response"]
    # Deux appels LLM : 1) planification JSON, 2) explication finale.
    assert len(fake_llm.calls) == 2
    # 1er appel : le LLM a planifié un appel d'outil en JSON strict.
    assert fake_llm.responses[0].startswith('{"tool": "add"')
    # 2e appel : déclenché après exécution effective du tool (résultat 42).
    assert fake_llm.calls[1][-1]["content"].startswith("Dernier résultat")
    assert "42" in fake_llm.calls[1][-1]["content"]


def test_ask_reports_unusable_llm_answer(client, fake_llm):
    fake_llm.replies = ["Bonjour ! Pas de JSON ici."]
    resp = client.post(
        "/ask",
        json={"prompt": "salut"},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert resp.status_code == 200
    assert "Réponse non exploitable" in resp.json()["response"]


def test_ask_reports_unknown_tool_from_llm(client, fake_llm):
    fake_llm.replies = ['{"tool": "division", "args": {"a": 1}}']
    resp = client.post(
        "/ask",
        json={"prompt": "divise 1 par 2"},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert resp.status_code == 200
    assert "Tool inconnu" in resp.json()["response"]


def test_ask_llm_connection_error_returns_502(client, fake_llm):
    fake_llm.error = requests.exceptions.ConnectionError("connection refused")
    resp = client.post(
        "/ask",
        json={"prompt": "salut"},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert resp.status_code == 502
    assert "injoignable" in resp.json()["detail"]


def test_ask_llm_timeout_returns_504(client, fake_llm):
    fake_llm.error = requests.exceptions.Timeout("too slow")
    resp = client.post(
        "/ask",
        json={"prompt": "salut"},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert resp.status_code == 504


def test_ask_empty_prompt_rejected(client):
    resp = client.post(
        "/ask",
        json={"prompt": ""},
        headers={"X-API-Key": "test-agent-key"},
    )
    assert resp.status_code == 422