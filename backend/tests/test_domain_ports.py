# project/tests/test_domain_ports.py
"""Tests des ports du domaine.

Deux niveaux de vérification :
    1. isinstance runtime_checkable : les stores legacy (core/) et le client
       LLM legacy (ia/agent/llm_client.py) SATISFONT déjà les Protocols —
       preuve que les contrats collent à l'existant, sans adaptateur ;
    2. fakes en mémoire implémentant les ports pour les futurs use-cases.
"""

from app.domain.ports import (
    ApprovalStorePort,
    AuditStorePort,
    LLMClientPort,
    RunStorePort,
    SessionStorePort,
    ToolRegistryPort,
)


def test_legacy_stores_satisfy_ports() -> None:
    """Vérification structurelle : les classes legacy ont les méthodes requises."""
    from core.approval_store import ApprovalStore
    from core.audit_store import AuditStore
    from core.run_store import RunStore
    from core.session_store import SessionStore

    # On n'ouvre PAS de vraie base : isinstance(Protocol) vérifie la surface
    # des méthodes déclarées (runtime_checkable), pas les arguments.
    assert issubclass(SessionStore, SessionStorePort)
    assert issubclass(AuditStore, AuditStorePort)
    assert issubclass(RunStore, RunStorePort)
    assert issubclass(ApprovalStore, ApprovalStorePort)


def test_legacy_llm_client_satisfies_port() -> None:
    from ia.agent.llm_client import LLMClient

    assert issubclass(LLMClient, LLMClientPort)


class FakeLLM:
    """Fake minimal pour les tests de use-cases (LLM déterministe)."""

    def __init__(self, reply: str = '{"tasks": []}') -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []

    def call(self, messages):
        self.calls.append(messages)
        return self.reply

    def call_stream(self, messages, on_thinking=None, on_content=None):
        self.calls.append(messages)
        if on_content:
            on_content(self.reply)
        return self.reply


class FakeToolRegistry:
    def tool_names(self):
        return ["now"]

    def get(self, tool):
        import datetime

        return (lambda: datetime.datetime.now().isoformat()) if tool == "now" else None

    def meta(self, tool):
        return {"description": "heure courante"} if tool == "now" else None


def test_fakes_satisfy_ports_at_runtime() -> None:
    fake_llm = FakeLLM()
    fake_registry = FakeToolRegistry()
    assert isinstance(fake_llm, LLMClientPort)
    assert isinstance(fake_registry, ToolRegistryPort)
    assert fake_registry.get("now") is not None
    assert fake_registry.get("unknown_tool") is None
    assert fake_registry.meta("now")["description"] == "heure courante"


def test_fake_llm_stream_callbacks() -> None:
    fragments: list[str] = []
    llm = FakeLLM(reply="bonjour")
    result = llm.call_stream([{"role": "user", "content": "hi"}], on_content=fragments.append)
    assert result == "bonjour"
    assert fragments == ["bonjour"]
    assert llm.calls == [[{"role": "user", "content": "hi"}]]
