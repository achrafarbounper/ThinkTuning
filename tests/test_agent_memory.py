# project/tests/test_agent_memory.py
"""Tests de la mémoire agentique (short-term / long-term).

Utilise un fake SessionStorePort en mémoire : pas de SQLite, tests rapides
et déterministes. Un test vérifie aussi la conformité du fake au Protocol.
"""

import pytest

from app.agent.memory import LongTermMemory, ShortTermMemory
from app.domain.ports import SessionStorePort


class FakeSessionStore:
    """Store en mémoire implémentant SessionStorePort."""

    def __init__(self) -> None:
        self.messages: dict[str, list[dict]] = {}
        self.memories: dict[str, str] = {}

    # --- sessions -----------------------------------------------------------
    def create_session(self, title: str = "", model: str = ""):
        return {"session_id": "s1"}

    def get_session(self, session_id: str):
        return {"session_id": session_id} if session_id in self.messages else None

    def list_sessions(self, limit: int = 100):
        return []

    def rename_session(self, session_id: str, title: str):
        return None

    def delete_session(self, session_id: str):
        return True

    def append_message(self, session_id: str, role: str, content: str, tool_calls=None):
        self.messages.setdefault(session_id, []).append(
            {"role": role, "content": content, "tool_calls": tool_calls}
        )
        return {"role": role, "content": content}

    def get_messages(self, session_id: str, limit: int = 200):
        return self.messages.get(session_id, [])[-limit:]

    # --- mémoire long-term ----------------------------------------------------
    def save_memory(self, key: str, summary: str) -> None:
        self.memories[key] = summary

    def get_memory(self, key: str) -> str:
        return self.memories.get(key, "")

    def delete_memory(self, key: str) -> None:
        self.memories.pop(key, None)


@pytest.fixture()
def store() -> FakeSessionStore:
    return FakeSessionStore()


def test_fake_satisfies_protocol(store) -> None:
    assert isinstance(store, SessionStorePort)


# --- Short-term -------------------------------------------------------------


def test_short_term_window(store) -> None:
    for i in range(30):
        store.append_message("s1", "user" if i % 2 == 0 else "assistant", f"msg {i}")
    memory = ShortTermMemory(store, session_id="s1", window=5)
    context = memory.load_context()
    # Fenêtre pleine : 5 messages récents + injection du premier message
    # utilisateur ("msg 0", intention initiale évincée).
    assert len(context) == 6
    assert context[-1]["content"] == "msg 29"
    assert context[0]["content"] == "msg 0"


def test_short_term_keeps_first_user_message(store) -> None:
    store.append_message("s1", "user", "INTENTION INITIALE")
    for i in range(20):
        store.append_message("s1", "user" if i % 2 == 0 else "assistant", f"msg {i}")
    memory = ShortTermMemory(store, session_id="s1", window=10)
    context = memory.load_context()
    assert any(m["content"] == "INTENTION INITIALE" for m in context)
    # Injection additive : le contexte le plus récent n'est jamais évincé.
    assert context[-1]["content"] == "msg 19"
    assert len(context) == 11


def test_short_term_small_window_no_injection(store) -> None:
    store.append_message("s1", "user", "premier")
    store.append_message("s1", "assistant", "réponse")
    memory = ShortTermMemory(store, session_id="s1", window=10)
    context = memory.load_context()
    assert [m["content"] for m in context] == ["premier", "réponse"]


def test_short_term_empty_session(store) -> None:
    memory = ShortTermMemory(store, session_id="inconnue")
    assert memory.load_context() == []


def test_short_term_store_failure_is_graceful(store) -> None:
    class BrokenStore(FakeSessionStore):
        def get_messages(self, session_id, limit=200):
            raise RuntimeError("db locked")

        def append_message(self, session_id, role, content, tool_calls=None):
            raise RuntimeError("db locked")

    memory = ShortTermMemory(BrokenStore(), session_id="s1")
    assert memory.load_context() == []
    assert memory.record("user", "hello") is False


def test_short_term_invalid_window(store) -> None:
    with pytest.raises(ValueError, match="window"):
        ShortTermMemory(store, session_id="s1", window=0)


# --- Long-term --------------------------------------------------------------


def test_long_term_roundtrip(store) -> None:
    memory = LongTermMemory(store)
    memory.remember("session:s1:summary", "L'utilisateur préfère le FR")
    assert memory.recall("session:s1:summary") == "L'utilisateur préfère le FR"
    memory.forget("session:s1:summary")
    assert memory.recall("session:s1:summary") == ""


def test_long_term_recall_missing_key_is_empty(store) -> None:
    assert LongTermMemory(store).recall("absente") == ""


def test_long_term_forget_idempotent(store) -> None:
    memory = LongTermMemory(store)
    memory.forget("jamais-écrite")  # no-op, pas d'exception
    assert memory.recall("jamais-écrite") == ""
