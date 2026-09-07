# project/tests/test_agent_context.py

"""Phase C — gestion avancée du contexte et mémoire inter-sessions.

Couvre ``ia/agent/context.py`` (estimation de jetons, fenêtre glissante,
résumé, mémoire glissante), les helpers mémoire de ``core/session_store.py``
et l'intégration API derrière le flag ``AGENT_CONTEXT`` (défaut : OFF).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

os.environ.setdefault("API_KEY", "test-key")  # avant l'import de l'app

from api import app as api_app  # noqa: E402
from core.feature_flags import flag  # noqa: E402
from core.session_store import reset_session_store  # noqa: E402
from ia.agent.context import (  # noqa: E402
    estimate_messages_tokens,
    estimate_tokens,
    format_memory_note,
    optimize_history,
    summarize_conversation,
    update_memory_summary,
)

client = TestClient(api_app)
HEADERS = {"X-API-Key": "test-key"}


# --- estimation ---------------------------------------------------------------


def test_estimate_tokens_empty_and_short():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1


def test_estimate_messages_tokens_grows_with_content():
    small = [{"role": "user", "content": "a" * 40}]
    big = [{"role": "user", "content": "a" * 400}]
    assert estimate_messages_tokens(small) < estimate_messages_tokens(big)


# --- fenêtre glissante --------------------------------------------------------


def _turns(n, size=200):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * size}
        for i in range(n)
    ]


def test_optimize_history_keeps_everything_within_budget():
    messages = _turns(4)
    kept, meta = optimize_history(messages, max_tokens=10_000)
    assert kept == messages
    assert meta["dropped"] == 0 and meta["summarized"] is False


def test_optimize_history_drops_oldest_without_summarizer():
    messages = _turns(6)  # ~50 jetons/tour
    kept, meta = optimize_history(messages, max_tokens=120)
    assert meta["dropped"] > 0
    assert meta["summarized"] is False
    # Les plus récents sont conservés, dans l'ordre chronologique.
    assert kept[-1] == messages[-1]
    assert len(kept) < len(messages)
    # Une note de troncature ouvre le contexte.
    assert kept[0]["content"].startswith("[Contexte tronqu")


def test_optimize_history_summarizes_overflow():
    messages = _turns(6)

    def fake_summarizer(transcript: str) -> str:
        assert "user:" in transcript or "assistant:" in transcript
        return "Résumé de test."

    kept, meta = optimize_history(
        messages, max_tokens=120, summarize_fn=fake_summarizer
    )
    assert meta["summarized"] is True
    assert kept[0]["content"].startswith(
        "[Résumé des tours précédents] Résumé de test."
    )
    assert kept[-1] == messages[-1]


def test_optimize_history_summarizer_failure_degrades_gracefully():
    messages = _turns(6)

    def broken(_transcript):
        raise RuntimeError("LLM down")

    kept, meta = optimize_history(messages, max_tokens=120, summarize_fn=broken)
    assert meta["summarized"] is False
    assert kept[0]["content"].startswith("[Contexte tronqu")


def test_summarize_conversation_uses_llm_call():
    class FakeLLM:
        def __init__(self):
            self.calls = []

        def call(self, messages):
            self.calls.append(messages)
            return "  Voilà le résumé.  "

    llm = FakeLLM()
    assert summarize_conversation(llm, "User: bonjour") == "Voilà le résumé."
    assert len(llm.calls) == 1


def test_summarize_conversation_failure_returns_empty():
    class BrokenLLM:
        def call(self, messages):
            raise TimeoutError("no network")

    assert summarize_conversation(BrokenLLM(), "User: bonjour") == ""


# --- mémoire glissante --------------------------------------------------------


def test_update_memory_summary_bounds_size():
    merged = ""
    for _ in range(50):
        merged = update_memory_summary(merged, "q" * 100, "a" * 100, max_chars=500)
    assert len(merged) <= 500
    assert "Assistant:" in merged


def test_format_memory_note_empty_returns_none():
    assert format_memory_note("") is None
    assert format_memory_note("   ") is None


def test_format_memory_note_wraps_content():
    note = format_memory_note("L'utilisateur aime Python.")
    assert note is not None
    assert note["role"] == "user"
    assert "L'utilisateur aime Python." in note["content"]


# --- store mémoire --------------------------------------------------------------


def test_session_store_memory_roundtrip():
    store = reset_session_store()
    assert store.get_memory("k1") == ""
    store.save_memory("k1", "premier")
    store.save_memory("k1", "second")  # upsert
    assert store.get_memory("k1") == "second"
    store.delete_memory("k1")
    assert store.get_memory("k1") == ""


# --- intégration API (flag AGENT_CONTEXT) ---------------------------------------


def test_history_load_unchanged_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_CONTEXT", raising=False)
    assert flag("context") is False
    store = reset_session_store()
    session = store.create_session(title="t")
    store.append_message(session["id"], "user", "q1")
    store.append_message(session["id"], "assistant", "a1")
    from api.routes.agent import _load_session_history

    history = _load_session_history(session["id"], None)
    assert [m["content"] for m in history] == ["q1", "a1"]


def test_history_budget_applied_when_flag_on(monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT", "1")
    monkeypatch.setenv("AGENT_CONTEXT_BUDGET_TOKENS", "60")
    store = reset_session_store()
    session = store.create_session(title="t")
    for i in range(10):
        store.append_message(session["id"], "user", "question " + str(i) * 50)
        store.append_message(session["id"], "assistant", "réponse " + str(i) * 50)
    from api.routes.agent import _load_session_history

    history = _load_session_history(session["id"], None)
    assert history, "l'historique optimisé ne doit pas être vide"
    # Les plus récents sont conservés.
    assert "réponse 9" in history[-1]["content"]
    # Un débordement a été tronqué (note) ou résumé.
    assert history[0]["content"].startswith(
        ("[Contexte tronqu", "[Résumé des tours précédents]")
    )


def test_new_session_injects_cross_session_memory(monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT", "1")
    store = reset_session_store()
    store.save_memory("global", "Prénom de l'utilisateur : Achraf.")
    # Session neuve : aucun message -> la mémoire doit être injectée.
    from api.routes.agent import _load_session_history

    history = _load_session_history("inexistant", None)
    assert len(history) == 1
    assert "Achraf" in history[0]["content"]
