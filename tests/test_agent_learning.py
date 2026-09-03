"""Tests Phase D — suggestions « Copilot », boucle d'apprentissage, API.

Hors-ligne : aucun appel réseau. Le LLM de ``complete_text`` est un FakeLLM ;
la base de feedback est isolée via AGENT_COPILOT_PATH (tmp_path).
"""

import os
import sys

os.environ.setdefault("API_KEY", "test-key")  # avant l'import de l'app

import pytest
from ia.copilot.feedback import FeedbackStore, get_feedback_store, reset_feedback_store
from ia.copilot.suggestions import (
    args_skeleton,
    complete_text,
    nl_to_tool,
    suggest_for_context,
)

# ---------------------------------------------------------------------------
# FeedbackStore (SQLite)
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    return FeedbackStore(str(tmp_path / "copilot.db"))


def test_feedback_record_and_stats(store):
    store.record("calc", True)
    store.record("calc", True)
    store.record("calc", False)
    stats = store.stats()
    assert stats["calc"]["accepts"] == 2
    assert stats["calc"]["rejects"] == 1
    assert stats["calc"]["accept_rate"] == round(2 / 3, 3)


def test_feedback_boost_positive_negative_neutral(store):
    assert store.boost("calc") == 0.0  # jamais évalué
    store.record("calc", True)
    assert store.boost("calc") == pytest.approx(0.1)
    store.record("calc", False)
    store.record("calc", False)
    assert store.boost("calc") == -0.15  # refus dominants


def test_feedback_boost_caps_at_0_3(store):
    for _ in range(10):
        store.record("add", True)
    assert store.boost("add") == 0.3


def test_feedback_store_isolated_paths(tmp_path):
    a = FeedbackStore(str(tmp_path / "a.db"))
    b = FeedbackStore(str(tmp_path / "b.db"))
    a.record("calc", True)
    assert a.count() == 1
    assert b.count() == 0


# ---------------------------------------------------------------------------
# Squelette d'arguments / pré-remplissage
# ---------------------------------------------------------------------------

def test_args_skeleton_lists_required_args():
    sk = args_skeleton("read_file")
    assert "path" in sk
    assert all(v == "" for v in sk.values())


def test_args_skeleton_prefills_single_quoted_value():
    sk = args_skeleton("read_file", 'lis le fichier "notes.txt" stp')
    assert sk["path"] == "notes.txt"


def test_args_skeleton_no_prefill_when_multiple_required():
    sk = args_skeleton("add", 'calcule "2+2"')
    assert all(v == "" for v in sk.values())


# ---------------------------------------------------------------------------
# Suggestions de contexte (+ boost d'apprentissage)
# ---------------------------------------------------------------------------

def test_suggest_for_context_ranks_tools():
    out = suggest_for_context(query="évalue l'expression 2+3", k=3)
    assert out["suggestions"]
    assert out["suggestions"][0]["tool"] == "calc"
    assert "args" in out["suggestions"][0]
    assert out["suggestions"][0]["score"] >= out["suggestions"][0]["base_score"] - 0.001


def test_suggest_for_context_uses_last_user_message():
    out = suggest_for_context(
        messages=[
            {"role": "assistant", "content": "Bonjour"},
            {"role": "user", "content": "donne-moi l'heure"},
        ],
        k=3,
    )
    assert any(s["tool"] == "now" for s in out["suggestions"])
    assert "heure" in out["query"]


def test_suggest_for_context_empty_query_returns_empty():
    assert suggest_for_context(messages=[], draft="")["suggestions"] == []


def test_feedback_boost_reorders_suggestions(store, monkeypatch):
    monkeypatch.setattr("ia.copilot.suggestions.get_feedback_store", lambda: store)
    base = suggest_for_context(query="lire le contenu d'un fichier", k=3)
    assert base["suggestions"], "suggestions attendues"
    top = base["suggestions"][0]["tool"]
    second = base["suggestions"][1]
    # L'utilisateur refuse le 1er et accepte le 2e : le 2e doit passer premier.
    store.record(top, False)
    store.record(second["tool"], True)
    store.record(second["tool"], True)
    after = suggest_for_context(query="lire le contenu d'un fichier", k=3)
    assert after["suggestions"][0]["tool"] == second["tool"]


def test_nl_to_tool_mapping():
    match = nl_to_tool("quelle heure est-il ?")
    assert match is not None
    assert match["tool"] in {"now", "env_info"}
    assert isinstance(match["args"], dict)


def test_nl_to_tool_no_match_returns_none():
    assert nl_to_tool("xyzzy qwerty zzzz") is None


# ---------------------------------------------------------------------------
# Complétion en ligne (FakeLLM)
# ---------------------------------------------------------------------------

class FakeLLM:
    def __init__(self, reply="... et voilà la suite."):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def call(self, messages):
        self.calls.append(messages)
        return self.reply


def test_complete_text_returns_llm_suffix():
    llm = FakeLLM(reply=" la suite probable.")
    out = complete_text(llm, [{"role": "user", "content": "blabla"}], "Bonjour,")
    assert out == "la suite probable."
    assert llm.calls, "un appel LLM attendu"


def test_complete_text_empty_draft_skips_llm():
    llm = FakeLLM()
    assert complete_text(llm, [], "  ") == ""
    assert llm.calls == []


def test_complete_text_llm_failure_degrades_softly():
    class Boom:
        def call(self, messages):
            raise RuntimeError("down")

    assert complete_text(Boom(), [], "Bonjour") == ""


# ---------------------------------------------------------------------------
# Endpoints API (gated par AGENT_COPILOT)
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from api import app as api_app  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture()
def client():
    with TestClient(api_app) as test_client:
        yield test_client


@pytest.fixture()
def isolated_feedback(tmp_path, monkeypatch):
    fb = reset_feedback_store(str(tmp_path / "api-copilot.db"))
    monkeypatch.setattr("api.routes.agent.get_feedback_store", lambda: fb)
    yield fb
    reset_feedback_store()


def test_suggest_endpoints_require_flag(client):
    assert client.post(
        "/api/agent/suggest", json={"query": "calcule"}, headers=HEADERS
    ).status_code == 404
    assert client.post(
        "/api/agent/suggest/feedback",
        json={"tool": "calc", "accepted": True},
        headers=HEADERS,
    ).status_code == 404
    assert client.post(
        "/api/agent/complete", json={"draft": "Bonjour"}, headers=HEADERS
    ).status_code == 404


def test_suggest_endpoint(client, isolated_feedback, monkeypatch):
    monkeypatch.setenv("AGENT_COPILOT", "on")
    resp = client.post(
        "/api/agent/suggest",
        json={"query": "lire le contenu d'un fichier", "k": 2},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["suggestions"]
    assert {"tool", "score", "args"} <= set(body["suggestions"][0].keys())


def test_feedback_endpoint_and_learning_effect(client, isolated_feedback, monkeypatch):
    monkeypatch.setenv("AGENT_COPILOT", "on")
    resp = client.post(
        "/api/agent/suggest/feedback",
        json={"tool": "calc", "accepted": True, "session_id": "s1"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["recorded"] is True
    assert isolated_feedback.stats()["calc"]["accepts"] == 1
    # Le boost est bien visible dans les suggestions suivantes.
    resp = client.post("/api/agent/suggest", json={"query": "calcule 2+2"}, headers=HEADERS)
    tools = [s["tool"] for s in resp.json()["suggestions"]]
    assert "calc" in tools


def test_complete_endpoint_uses_real_runner_llm(client, monkeypatch):
    monkeypatch.setenv("AGENT_COPILOT", "on")

    class FakeRunnerLLM:
        def call(self, messages):
            return " suite simulée."

    class FakeCore:
        llm = FakeRunnerLLM()

    class FakeRunner:
        core = FakeCore()

    import core.agent_cache as agent_cache

    monkeypatch.setattr(agent_cache, "get_agent_runner", lambda: FakeRunner())
    resp = client.post("/api/agent/complete", json={"draft": "Bonjour,"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["completion"] == "suite simulée."


def test_complete_endpoint_empty_draft_short_circuits(client, monkeypatch):
    monkeypatch.setenv("AGENT_COPILOT", "on")
    resp = client.post("/api/agent/complete", json={"draft": "  "}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["completion"] == ""


def test_get_feedback_store_singleton():
    assert get_feedback_store() is get_feedback_store()

