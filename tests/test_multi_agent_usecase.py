"""Tests du use case multi-agents (façade)."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

from app.application.multi_agent_usecase import (
    run_multi_agent,
    run_multi_agent_streaming,
)
from app.domain.ports import MultiAgentOrchestratorPort


class _FakeOrchestrator:
    """Fake minimal de MultiAgentOrchestratorPort."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self._result = result or {"status": "completed", "final_answer": "ok"}
        self.calls: list[dict[str, Any]] = []

    def run(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, **kwargs})
        return self._result

    def run_streaming(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, **kwargs})
        return self._result


def test_run_multi_agent_delegates_to_port() -> None:
    fake = _FakeOrchestrator({"status": "completed", "final_answer": "réponse"})
    result = run_multi_agent(fake, "question", model="gpt-4", parallel=True)
    assert result == {"status": "completed", "final_answer": "réponse"}
    assert len(fake.calls) == 1
    assert fake.calls[0]["prompt"] == "question"
    assert fake.calls[0]["model"] == "gpt-4"
    assert fake.calls[0]["parallel"] is True


def test_run_multi_agent_streaming_delegates_to_port() -> None:
    fake = _FakeOrchestrator({"status": "completed", "final_answer": "réponse"})
    events: list[tuple[str, dict[str, Any]]] = []

    def on_event(event_type: str, data: dict[str, Any]) -> None:
        events.append((event_type, data))

    result = run_multi_agent_streaming(
        fake, "question", model="gpt-4", on_event=on_event,
    )
    assert result == {"status": "completed", "final_answer": "réponse"}
    assert len(fake.calls) == 1
    assert fake.calls[0]["prompt"] == "question"
    assert fake.calls[0]["model"] == "gpt-4"
    assert fake.calls[0]["on_event"] is on_event


def test_run_multi_agent_passes_optional_args() -> None:
    fake = _FakeOrchestrator()
    run_multi_agent(
        fake, "q", resume_request_id="req-1", enable_thinking=True,
    )
    assert fake.calls[0]["resume_request_id"] == "req-1"
    assert fake.calls[0]["enable_thinking"] is True


def test_port_is_protocol() -> None:
    """Le port est un Protocol — vérifie la structure runtime_checkable."""
    fake = _FakeOrchestrator()
    # runtime_checkable permet isinstance à l'exécution
    assert isinstance(fake, MultiAgentOrchestratorPort)