"""
Tests offline des conversations persistées (core/session_store.py) et de
leur route CRUD (/api/sessions/*) + intégration session_id sur /api/agent/ask.

Aucun réseau : LLM factice / fonctions agent remplacées par monkeypatch.
Lance avec : pytest tests/test_agent_sessions.py -v
"""

import os

# Config test AVANT tout import (le cache insère ia/ dans sys.path).
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")  # port factice

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402,F401  (initialise le job store avant le routage)
from api.routes import agent as agent_routes  # noqa: E402
from core import session_store  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    """TestClient avec une base de sessions neuve par test."""
    session_store.reset_session_store(str(tmp_path / "agent_sessions.db"))
    return TestClient(api.app)


HEADERS = {"X-API-Key": "test-key"}


def test_session_store_titles_and_messages():
    store = session_store.SessionStore(":memory:") if False else None
    # « :memory: » ouvrirait une base par connexion ; on passe par un fichier temp.
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "tt_sessions_unit.db")
    if os.path.exists(path):
        os.remove(path)
    store = session_store.SessionStore(path)

    session = store.create_session(model="llama3.1:8b")
    assert session["title"].startswith("Conversation du ")

    user_msg = store.append_message(session["id"], "user", "Analyse ce dataset CSV")
    assert user_msg is not None and user_msg["role"] == "user"
    # Premier message utilisateur -> titre auto-dérivé.
    assert store.get_session(session["id"])["title"] == "Analyse ce dataset CSV"

    tools = [{"event": "tool_start", "tool": "dataset_stats"}]
    store.append_message(session["id"], "assistant", "Voici l'analyse.", tool_calls=tools)

    messages = store.get_messages(session["id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["tool_calls"][0]["tool"] == "dataset_stats"

    # Rôle invalide refusé ; session inconnue ignorée (None).
    with pytest.raises(ValueError):
        store.append_message(session["id"], "systeme", "nope")
    assert store.append_message("absent", "user", "?") is None

    assert store.rename_session(session["id"], "Mon analyse")["title"] == "Mon analyse"
    assert store.list_sessions()[0]["id"] == session["id"]
    assert store.delete_session(session["id"]) is True
    assert store.get_messages(session["id"]) == []


def test_sessions_crud_api(client):
    created = client.post(
        "/api/sessions", headers=HEADERS, json={"title": "Revue", "model": "m1"}
    )
    assert created.status_code == 200
    session = created.json()

    listing = client.get("/api/sessions")
    assert listing.status_code == 200
    assert any(s["id"] == session["id"] for s in listing.json()["sessions"])

    renamed = client.patch(
        f"/api/sessions/{session['id']}", headers=HEADERS, json={"title": "Nouveau titre"}
    )
    assert renamed.status_code == 200 and renamed.json()["title"] == "Nouveau titre"

    messages = client.get(f"/api/sessions/{session['id']}/messages")
    assert messages.status_code == 200 and messages.json()["messages"] == []

    missing = client.patch("/api/sessions/absent", headers=HEADERS, json={"title": "x"})
    assert missing.status_code == 404

    deleted = client.delete(f"/api/sessions/{session['id']}", headers=HEADERS)
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True


def test_ask_persists_exchange_in_session(client, monkeypatch):
    """POST /api/agent/ask avec session_id journalise l'échange user/assistant."""
    monkeypatch.setattr(
        agent_routes,
        "ask_agent_decision",
        lambda prompt, resume_request_id=None, **kwargs: {
            "response": "Réponse finale.",
            "status": "completed",
        },
    )

    session = client.post(
        "/api/sessions", headers=HEADERS, json={"model": "llama3.1:8b"}
    ).json()

    response = client.post(
        "/api/agent/ask",
        headers=HEADERS,
        json={"prompt": "Calcule 2+2", "session_id": session["id"]},
    )
    assert response.status_code == 200

    messages = client.get(f"/api/sessions/{session['id']}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Calcule 2+2"
    assert messages[1]["content"] == "Réponse finale."

    # Le titre de session a été dérivé du premier message utilisateur.
    refreshed = [s for s in client.get("/api/sessions").json()["sessions"]
                 if s["id"] == session["id"]][0]
    assert refreshed["title"] == "Calcule 2+2"


def test_ask_with_unknown_session_still_succeeds(client, monkeypatch):
    """Une session absente ne fait pas échouer le tour (persistance best-effort)."""
    monkeypatch.setattr(
        agent_routes,
        "ask_agent_decision",
        lambda prompt, resume_request_id=None, **kwargs: {"response": "ok", "status": "completed"},
    )
    response = client.post(
        "/api/agent/ask",
        headers=HEADERS,
        json={"prompt": "salut", "session_id": "inexistant"},
    )
    assert response.status_code == 200


def test_load_session_history_purges_tool_calls_and_trims():
    """`_load_session_history` ne garde que rôle/content non vide, écarte les
    tool_calls et borne aux MAX dernières paires de la conversation."""
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "tt_sessions_hist.db")
    if os.path.exists(path):
        os.remove(path)
    store = session_store.SessionStore(path)
    session = store.create_session(model="m")
    sid = session["id"]
    # Tour 1 avec tool_call sur l'assistant (doit être écarté du contexte).
    store.append_message(sid, "user", "je suis mahrez bou derbela")
    store.append_message(
        sid,
        "assistant",
        "Salut Mahrez !",
        tool_calls=[{"event": "tool_start", "tool": "calc"}],
    )
    # Tour 2 + message utilisateur vide (écarté aussi).
    store.append_message(sid, "user", "calcule 2+2")
    store.append_message(sid, "assistant", "4.")
    store.append_message(sid, "user", "   ")

    # La fonction de route lit via le singleton get_session_store() : on
    # pointe celui-ci sur la même base temporaire que l'instance ci-dessus.
    session_store.reset_session_store(path)

    # Sans resume : on recharge l'historique de la session.
    history = agent_routes._load_session_history(sid, resume_request_id=None)
    assert [m["role"] for m in history] == [
        "user", "assistant", "user", "assistant",
    ]
    assert history[0]["content"] == "je suis mahrez bou derbela"
    assert history[2]["content"] == "calcule 2+2"

    # Avec resume_request_id : pas d'historique (l'agent reprend son état).
    assert agent_routes._load_session_history(sid, resume_request_id="abc") == []

    # Sans session_id : pas d'historique.
    assert agent_routes._load_session_history(None, resume_request_id=None) == []


def test_ask_injects_history_messages_for_memory(client, monkeypatch):
    """La route /api/agent/ask passe bien l'historique de session à l'agent
    (mémoire de conversation : l'agent doit se souvenir du nom)."""
    captured = {}

    def fake_decision(prompt, resume_request_id=None, history_messages=None, **kwargs):
        captured["history"] = history_messages or []
        return {"response": "Je me souviens de toi, Mahrez.", "status": "completed"}

    monkeypatch.setattr(agent_routes, "ask_agent_decision", fake_decision)

    session = client.post("/api/sessions", headers=HEADERS, json={}).json()
    sid = session["id"]
    # Deux tours déjà présents dans la session (même base que l'API via la
    # fixture client, qui a déjà réinitialisé le store sur un fichier temp).
    s = session_store.get_session_store()
    s.append_message(sid, "user", "je suis mahrez bou derbela")
    s.append_message(sid, "assistant", "Salut Mahrez !")

    response = client.post(
        "/api/agent/ask",
        headers=HEADERS,
        json={"prompt": "comment je m'appelle ?", "session_id": sid},
    )
    assert response.status_code == 200
    assert captured["history"] == [
        {"role": "user", "content": "je suis mahrez bou derbela"},
        {"role": "assistant", "content": "Salut Mahrez !"},
    ]
    # Le prompt courant n'est pas rejoué dans l'historique.
    assert all(
        m["content"] != "comment je m'appelle ?" for m in captured["history"]
    )


def test_ask_no_history_without_session(client, monkeypatch):
    """Sans session_id, aucun historique n'est injecté (comportement d'origine)."""
    captured = {}

    def fake_decision(prompt, resume_request_id=None, history_messages=None, **kwargs):
        captured["history"] = history_messages or []
        return {"response": "ok", "status": "completed"}

    monkeypatch.setattr(agent_routes, "ask_agent_decision", fake_decision)
    resp = client.post("/api/agent/ask", headers=HEADERS, json={"prompt": "salut"})
    assert resp.status_code == 200
    assert captured["history"] == []


def test_store_repairs_mojibake_on_read():
    """Un contenu persisté en mojibake (UTF-8 relu Latin-1) doit être relu
    corrigé — aussi bien dans les messages que dans le titre et la mémoire."""
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "tt_sessions_mojibake.db")
    if os.path.exists(path):
        os.remove(path)
    store = session_store.SessionStore(path)

    expected = "Bonjour, tête désolée. À l'aide !"
    mojibake = expected.encode("utf-8").decode("latin-1")

    sid = store.create_session(title=mojibake)["id"]
    store.append_message(sid, "user", "salut")
    store.append_message(sid, "assistant", mojibake)
    store.save_memory("global", mojibake)

    # Lecture corrige automatiquement.
    assert store.get_messages(sid)[1]["content"] == expected
    assert store.get_session(sid)["title"] == expected
    assert store.get_memory("global") == expected

    # Un texte Latin-1 légitime n'est pas altéré.
    store.append_message(sid, "assistant", "café coração")
    assert store.get_messages(sid)[-1]["content"] == "café coração"
